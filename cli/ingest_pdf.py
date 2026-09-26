#!/usr/bin/env python3
"""
BARQ S3.5 - Multimodal PDF ingestion CLI.

    python cli/ingest_pdf.py challenge/barq_challenge.pdf --create-collection
    python cli/ingest_pdf.py manual.pdf --dry-run --dump-chunks out/chunks.jsonl

Pipeline (per page):
    1. orientation   -> content angle from text direction vectors (+ /Rotate), OSD for scans
    2. tables        -> PyMuPDF find_tables -> merged-cell/header aware Markdown + record lines + JSON
    3. native text   -> blocks outside tables, Arabic presentation-form repair, header/footer removal
    4. images        -> OSD rotate -> Tesseract (ara+eng) -> vision LLM (classify / diagram summary)
    5. vector pages  -> drawing-heavy pages rendered upright and summarised by the vision LLM
    6. chunk -> embed (OpenAI-compatible, env-configured) -> deterministic upsert into Qdrant

No endpoint, key or model name is hardcoded: everything comes from the environment / .env.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pymupdf as fitz  # PyMuPDF
from PIL import Image, ImageOps

log = logging.getLogger("ingest_pdf")

# Fixed namespace => the same (doc_id, page, chunk_index) always yields the same point id.
POINT_NS = uuid.UUID("6f1b2c1e-8a44-4c5e-9d0a-2b7f5f0c1a11")


# --------------------------------------------------------------------------- config
def env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default


@dataclass
class Config:
    base_url: str | None
    api_key: str | None
    embed_model: str | None
    embed_dim: int | None
    vision_model: str | None
    qdrant_url: str | None
    qdrant_key: str | None
    collection: str | None
    vector_name: str | None
    ocr_lang: str = "ara+eng"
    chunk_size: int = 900
    overlap: int = 120
    dpi: int = 200
    min_image_frac: float = 0.01
    vector_min_drawings: int = 25

    @classmethod
    def from_env(cls, a: argparse.Namespace) -> "Config":
        vm = None if a.no_vision else env("VISION_MODEL", "LITELLM_VISION_MODEL")
        dim = env("EMBEDDING_DIM", "EMBED_DIM")
        return cls(
            base_url=env("LITELLM_BASE_URL", "LITELLM_API_BASE", "OPENAI_BASE_URL", "OPENAI_API_BASE"),
            api_key=env("LITELLM_API_KEY", "LITELLM_MASTER_KEY", "OPENAI_API_KEY"),
            embed_model=env("EMBEDDING_MODEL", "EMBED_MODEL"),
            embed_dim=int(dim) if dim else None,
            vision_model=vm,
            qdrant_url=env("QDRANT_URL"),
            qdrant_key=env("QDRANT_API_KEY"),
            collection=a.collection or env("QDRANT_COLLECTION", "QDRANT_COLLECTION_NAME"),
            vector_name=env("QDRANT_VECTOR_NAME"),
            ocr_lang=a.ocr_lang,
            chunk_size=a.chunk_size,
            overlap=a.overlap,
            dpi=a.dpi,
        )


@dataclass
class Chunk:
    page: int
    content_type: str  # text | table | image_ocr | image_text | diagram
    text: str
    meta: dict = field(default_factory=dict)
    index: int = 0


# Generic retired-content denylist: (pattern, superseded_by). Any chunk whose text
# matches is flagged retired=true + superseded_by in its payload so the retriever can
# filter it out or surface it as an explicit "do not apply". Keep patterns narrow
# (a full procedure step, not a historical mention) and add new rows, not one-off code.
RETIRED_PATTERNS: list[tuple] = [
    (re.compile(r"restart the order service application server to clear the pool", re.I),
     "KB0010 v2"),
]

# Generic end-of-life cues: advisory only (retired_hint, not retired) so the retriever can
# down-rank and a reviewer can check. Two tiers: STRONG cues assert end-of-life on their own;
# WEAK cues ("do not apply", "superseded", ...) only count beside a version marker, otherwise
# they fire on unrelated instructions (e.g. outage handling, document-control boilerplate).
RETIRED_STRONG = re.compile(r"retir|deprecat|obsolete|end.of.life", re.I)
RETIRED_WEAK = re.compile(r"do not apply|superseded|no longer (valid|supported|in effect)", re.I)
RETIRED_VERSION = re.compile(r"\bv\d+|version|revision|\brev\b", re.I)


def _weak_cue_near_version(text: str, window: int = 80) -> bool:
    """A WEAK end-of-life cue counts only beside a version marker in the same clause
    (generic proximity heuristic: keeps 'retired in version 2' hits, drops boilerplate
    like 'superseded without notice ... Version history' two sentences later)."""
    for m in RETIRED_WEAK.finditer(text):
        seg = text[max(0, m.start() - window):m.end() + window]
        if RETIRED_VERSION.search(seg):
            return True
    return False


# --------------------------------------------------------------------------- text helpers
_ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_PRES_FORMS = re.compile(r"[\uFB50-\uFDFF\uFE70-\uFEFF]")
_BIDI_MARKS = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_LATIN_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/:\-]*")


def fix_native_arabic(text: str) -> str:
    """Legacy PDFs store Arabic as visually-ordered presentation forms. Normalise to base
    letters (NFKC) and flip back to logical order. Heuristic: correct for Arabic runs, Latin/digit
    runs are re-reversed. Text that is already logical (modern ToUnicode) is left untouched."""
    out = []
    for line in text.split("\n"):
        if _PRES_FORMS.search(line):
            line = unicodedata.normalize("NFKC", line)[::-1]
            line = _LATIN_RUN.sub(lambda m: m.group()[::-1], line)
        out.append(line)
    return "\n".join(out)


def languages(text: str) -> list[str]:
    ar = len(_ARABIC.findall(text))
    lat = len(re.findall(r"[A-Za-z]", text))
    langs = []
    if ar >= 5:
        langs.append("ar")
    if lat >= 5:
        langs.append("en")
    return langs


def norm_key(s: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", s.strip().lower()))


def split_text(text: str, size: int, overlap: int) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    sents = [s.strip() for s in re.split(r"(?<=[.!?\u061F])\s+|\n{2,}", text) if s.strip()]
    pieces: list[str] = []
    for s in sents:  # hard-split monster sentences
        while len(s) > size:
            pieces.append(s[:size])
            s = s[size - overlap:]
        pieces.append(s)
    chunks, cur = [], ""
    for p in pieces:
        if cur and len(cur) + len(p) + 1 > size:
            chunks.append(cur)
            tail = cur[-overlap:] if overlap else ""
            tail = tail[tail.find(" ") + 1:] if " " in tail else tail
            cur = (tail + " " + p).strip()
        else:
            cur = (cur + " " + p).strip()
    if cur:
        chunks.append(cur)
    return chunks


# --------------------------------------------------------------------------- orientation
_CONTENT_ROT = {(1, 0): 0, (0, 1): 90, (-1, 0): 180, (0, -1): 270}  # text rotation, clockwise, unrotated page space


def content_rotation(page: fitz.Page) -> int:
    """Dominant text direction of the page content (weighted by characters), as clockwise degrees."""
    weight: Counter = Counter()
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            n = sum(len(s["text"].strip()) for s in ln["spans"])
            weight[(round(ln["dir"][0]), round(ln["dir"][1]))] += n
    return _CONTENT_ROT.get(weight.most_common(1)[0][0], 0) if weight else 0


def normalise_pages(doc: fitz.Document, pages: list[int]) -> tuple[fitz.Document, dict[int, tuple[int, int, fitz.Page]]]:
    """Reset /Rotate and re-emit sideways content upright as *vector* pages (show_pdf_page), so
    find_tables / text extraction / rendering all see upright content. Returns per page:
    (original /Rotate, content rotation, upright page)."""
    norm = fitz.open()
    info = {}
    for pno in pages:
        pg = doc[pno - 1]
        r0 = pg.rotation
        if r0:
            pg.set_rotation(0)
        cr = content_rotation(pg)
        if cr:
            w, h = (pg.rect.height, pg.rect.width) if cr in (90, 270) else (pg.rect.width, pg.rect.height)
            up = norm.new_page(width=w, height=h)
            up.show_pdf_page(up.rect, doc, pno - 1, rotate=cr)  # rotate = counter-clockwise degrees
        else:
            up = pg
        info[pno] = (r0, cr, up)
    return norm, info


def deskew(img: Image.Image) -> tuple[Image.Image, float]:
    """Small skew correction (±5°, 1° steps) via horizontal-projection variance on a
    thumbnail. Generic limits: text-like images only; returns (img, 0.0) when no clear
    peak (drawings, photos) or on any error."""
    try:
        w, h = img.size
        s = 400 / max(1, w)
        small = img.convert("L").resize((max(1, int(w * s)), max(1, int(h * s))))
        thr = sum(small.getdata()) / (small.width * small.height) * 0.9
        bw = small.point(lambda v: 0 if v < thr else 255)

        def energy(im: Image.Image) -> float:
            px = im.load()
            rows = [sum(1 for x in range(im.width) if px[x, y] == 0) for y in range(im.height)]
            m = sum(rows) / max(1, len(rows))
            return sum((r - m) ** 2 for r in rows) / max(1, len(rows))

        base = energy(bw)
        best_a, best_e = 0.0, base
        for a in (-5, -4, -3, -2, -1, 1, 2, 3, 4, 5):
            e = energy(bw.rotate(a, expand=True, fillcolor=255))
            if e > best_e:
                best_a, best_e = a, e
        if best_a and best_e >= 1.05 * base:
            return img.rotate(best_a, expand=True, fillcolor="white"), float(best_a)
    except Exception as e:  # best effort only
        log.debug("deskew skipped: %s", e)
    return img, 0.0


def reading_order(rects: list) -> list[int]:
    """Top-to-bottom row bands (by vertical overlap); x order resolved by the caller
    (LTR, or RTL when the page reads right-to-left). Generic geometry, no layout IDs."""
    bands: list[list[int]] = []
    for i in range(len(rects)):
        r = rects[i]
        placed = False
        for b in bands:
            rb = rects[b[0]]
            overlap = min(r.y1, rb.y1) - max(r.y0, rb.y0)
            if overlap > 0.3 * min(r.y1 - r.y0, rb.y1 - rb.y0):
                b.append(i)
                placed = True
                break
        if not placed:
            bands.append([i])
    bands.sort(key=lambda b: min(rects[i].y0 for i in b))
    return bands


def osd_rotate(img: Image.Image) -> tuple[Image.Image, int]:
    """Tesseract OSD for images with no text layer. Returns (upright image, degrees applied)."""
    try:
        import pytesseract
        osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
        angle, conf = int(osd.get("rotate", 0)), float(osd.get("orientation_conf", 0))
        if angle and conf >= 1.5:
            return img.rotate(-angle, expand=True, fillcolor="white"), angle
    except Exception as e:  # too little text, missing osd.traineddata, ...
        log.debug("OSD skipped: %s", e)
    return img, 0


# --------------------------------------------------------------------------- tables
def _fill_right(row: list) -> list:
    out, last = [], None
    for c in row:
        if c is None:
            out.append(last if last is not None else "")
        else:
            last = c
            out.append(c)
    return out


def _clean(c) -> str:
    if c is None:
        return ""
    return re.sub(r"\s*\n\s*", " ", str(c)).strip()  # multi-line cells stay one cell, space-joined


def compact_columns(rows: list[list]) -> tuple[list[list], bool]:
    """Styled tables (cell fills, no rules) come back from PyMuPDF as a *staggered* grid: one logical
    column is split over two grid columns and each row fills only one of them. Merge adjacent columns
    that are never occupied in the same row."""
    occ = lambda v: v not in (None, "")
    changed = False
    again = True
    while again and len(rows[0]) > 1:
        again = False
        for j in range(len(rows[0]) - 1):
            both = any(occ(r[j]) and occ(r[j + 1]) for r in rows)
            if not both and any(occ(r[j]) for r in rows) and any(occ(r[j + 1]) for r in rows):
                rows = [r[:j] + [r[j] if occ(r[j]) else r[j + 1]] + r[j + 2:] for r in rows]
                again = changed = True
                break
    return rows, changed


def table_to_structured(rows: list[list]) -> tuple[list[str], list[list[str]], dict]:
    """Rebuild header-cell relationships from PyMuPDF's grid (merged cells come back as None)."""
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        return [], [], {}
    ncol = max(len(r) for r in rows)
    rows = [list(r) + [None] * (ncol - len(r)) for r in rows]
    # nested tables / spans make PyMuPDF emit phantom columns: drop columns that are empty in every row
    keep = [j for j in range(ncol) if any(r[j] not in (None, "") for r in rows)]
    rows = [[r[j] for j in keep] for r in rows]
    ncol = len(keep)
    rows, compacted = compact_columns(rows)
    ncol = len(rows[0])
    n_head = 2 if (len(rows) > 2 and any(c is None for c in rows[0])) else 1  # colspan header => 2 header rows
    head_rows = [[_clean(c) for c in _fill_right(r)] for r in rows[:n_head]]
    headers = []
    for j in range(ncol):
        parts: list[str] = []
        for hr in head_rows:
            if hr[j] and (not parts or parts[-1] != hr[j]):
                parts.append(hr[j])
        headers.append(" > ".join(parts) or f"col{j + 1}")
    body, prev0 = [], ""
    for r in rows[n_head:]:
        r = list(r)
        if r[0] is None and prev0:  # rowspan in first column
            r[0] = prev0
        cells = [_clean(c) for c in r]
        prev0 = cells[0] or prev0
        body.append(cells)
    headers, body, merged = merge_phantom_columns(headers, body)
    empty = sum(1 for r in body for c in r if not c)
    quality = {"compacted": compacted, "nested_merged": merged, "rows": len(body), "cols": len(headers), "empty_ratio": round(empty / max(1, len(body) * ncol), 2),
               "header_rows": n_head}
    return headers, body, quality


def merge_phantom_columns(headers: list[str], body: list[list[str]]) -> tuple[list[str], list[list[str]], bool]:
    """A table nested in a cell makes PyMuPDF emit extra columns that inherit the parent header via
    colspan fill. Fold them back into the parent cell and join nested continuation rows, so the
    nested content stays attached to its parent header instead of becoming orphan columns."""
    dup = [j for j in range(1, len(headers)) if headers[j] == headers[j - 1]]
    if not dup:
        return headers, body, False
    parent = {}
    for j in dup:
        parent[j] = parent.get(j - 1, j - 1)
    keep = [j for j in range(len(headers)) if j not in parent]
    new_body: list[list[str]] = []
    nested_cols = set(parent.values())
    for r in body:
        row = {j: r[j] for j in keep}
        for j, pj in parent.items():
            if r[j]:
                row[pj] = (row[pj] + " / " if row[pj] else "") + r[j]
        others = [j for j in keep if j != 0 and j not in nested_cols and row[j]]
        if new_body and not others and any(row[j] for j in nested_cols):  # continuation of a nested table
            for j in nested_cols:
                if row[j]:
                    new_body[-1][keep.index(j)] += "; " + row[j]
            continue
        new_body.append([row[j] for j in keep])
    return [headers[j] for j in keep], new_body, True


def structural_flags(headers: list[str], body: list[list[str]], page: fitz.Page, tb,
                      skip_coverage: bool = False) -> tuple[bool, dict]:
    """Cheap generic sanity checks on a recovered grid. Any red flag => failed=True.
    Coverage applies only to large, non-normalised pages: on small samples the char ratio
    is unstable (measured 0.63-1.34 on good tables), and re-emitted (rotation-normalised)
    pages may vectorise text so clip extraction is unreliable. The [0.5, 2.0] band catches
    only gross loss/duplication. Filled-but-misaligned grids can still pass: known
    limitation, see failure case 3. All check values are stored in the payload for audit."""
    checks: dict = {}
    try:
        bbox_text = page.get_text("text", clip=fitz.Rect(tb.bbox))
    except Exception:
        bbox_text = ""
    cell_chars = sum(len(c) for r in body for c in r)
    base_chars = len(re.sub(r"\s+", "", bbox_text))
    checks["coverage"] = round(cell_chars / max(1, base_chars), 2)
    n = max(1, len(body))
    checks["rowlen_bad_ratio"] = round(sum(1 for r in body if len(r) != len(headers)) / n, 2)
    checks["dup_row_ratio"] = round((len(body) - len({tuple(r) for r in body})) / n, 2)
    nh = max(1, len(headers))
    checks["placeholder_header_ratio"] = round(
        sum(1 for h in headers if not h or re.fullmatch(r"col\d+", h)) / nh, 2)
    frag = sum(1 for r in body for c in r if c.endswith("-") or (0 < len(c) <= 2 and not c.isdigit()))
    checks["fragment_cell_ratio"] = round(frag / max(1, sum(len(r) for r in body)), 2)
    checks["header_dup_row_ratio"] = round(sum(1 for r in body if list(r) == list(headers)) / n, 2)
    cov_bad = (not skip_coverage and base_chars > 200
               and (checks["coverage"] < 0.5 or checks["coverage"] > 2.0))
    failed = (
        cov_bad
        or checks["rowlen_bad_ratio"] > 0.3
        or checks["dup_row_ratio"] > 0.5
        # placeholder ceiling 0.3 (not 0.5): word-salad "headers" from prose-line
        # alignment (e.g. a sideways schematic paragraph) otherwise pass; no measured
        # good grid exceeds 0.25, see §3 gate table
        or checks["placeholder_header_ratio"] > 0.3
        or checks["fragment_cell_ratio"] > 0.3
        or checks["header_dup_row_ratio"] > 0.3
    )
    return failed, checks


def _grid_score(headers: list[str], body: list[list[str]], quality: dict) -> float:
    """One scoring function for every grid candidate (PyMuPDF, Camelot, text-align).
    Lower is better. NB: PyMuPDF strategy='text' as a candidate was tried and rejected:
    it scores 'clean' but splits words mid-token on borderless tables."""
    if not body:
        return 9.0
    ph = sum(1 for x in headers if re.fullmatch(r"col\d+", x)) / max(1, len(headers))
    return round(quality.get("empty_ratio", 1.0) + 0.5 * ph, 2)


def best_table(page: fitz.Page, tb, skip_coverage: bool = False) -> tuple[list[str], list[list[str]], dict]:
    """Structure the ruled-line grid and score it (empty cells + placeholder headers)."""
    def score(res):
        h, b, q = res
        sc = _grid_score(h, b, q)
        res[2]["score"] = sc
        return sc, res
    cands = [score(table_to_structured(tb.extract()))]
    best = min(cands, key=lambda x: x[0])[1]
    if not best[2]:
        best = (best[0], best[1], {"score": 9.0})
    failed, checks = structural_flags(best[0], best[1], page, tb, skip_coverage)
    best[2]["checks"] = checks
    best[2]["failed"] = failed
    best[2].setdefault("grid_source", "pymupdf")
    return best


def _optional_import(name: str):
    try:
        return __import__(name)
    except ImportError:
        return None


_lattice_warned = False


def _lattice_page_tables(doc: "fitz.Document", pno: int) -> list:
    """All Camelot-lattice tables for one page (single temp export). [] when camelot or
    Ghostscript is unavailable, or on any error: the pipeline continues regardless."""
    global _lattice_warned
    camelot = _optional_import("camelot")
    if camelot is None:
        return []
    import shutil
    if not shutil.which("gs"):
        if not _lattice_warned:
            log.info("camelot-lattice skipped: ghostscript 'gs' not installed")
            _lattice_warned = True
        return []
    path = _single_page_tmp(doc, pno)
    try:
        try:
            return list(camelot.read_pdf(path, pages="1", flavor="lattice"))
        except Exception as e:
            log.debug("camelot-lattice failed p%d: %s", pno, e)
            return []
    finally:
        import os
        try:
            os.unlink(path)
        except OSError:
            pass


def match_lattice_table(tables: list, bbox: "fitz.Rect") -> list[list[str]] | None:
    """Best-overlap lattice grid for one region (IoU > 0.3), else the single table if
    the page has exactly one. No content assumptions."""
    import fitz as _fitz
    best, best_ov = None, 0.3
    for t in tables:
        try:
            x0, y0, x1, y1 = (float(v) for v in t._bbox.split(","))
        except Exception:
            continue
        r = _fitz.Rect(x0, y0, x1, y1)
        inter = (r & bbox).get_area()
        ov = inter / max(1.0, bbox.get_area())
        if ov > best_ov:
            best, best_ov = t, ov
    if best is None and len(tables) == 1:
        best = tables[0]
    if best is None:
        return None
    return [[str(c).strip() for c in row] for row in best.df.values.tolist()]


def lattice_for_grid(doc: "fitz.Document", pno: int, bbox: "fitz.Rect",
                     _cache: dict | None = None) -> list[list[str]] | None:
    """Lattice candidate for one region (temp single-page export, cached per page)."""
    if _cache is None:
        _cache = {}
    if pno not in _cache:
        _cache[pno] = _lattice_page_tables(doc, pno)
    return match_lattice_table(_cache[pno], bbox)


def contend_grids(doc: "fitz.Document", pno: int, page: "fitz.Page", tb,
                  r0: int, ang: int, lat_pages: dict, stats: Counter | None = None) -> tuple:
    """One ruled region, two candidates (PyMuPDF grid, Camelot-lattice): best score wins,
    ties keep PyMuPDF. Shared by extract_pages and run_eval so numbers match."""
    headers, body, q = best_table(page, tb, skip_coverage=bool(r0 or ang))
    lat = lattice_for_grid(doc, pno, fitz.Rect(tb.bbox), lat_pages)
    if lat:
        lh, lb, lq = table_to_structured(lat)
        lq["score"] = _grid_score(lh, lb, lq)
        lfailed, lchecks = structural_flags(lh, lb, page, tb, skip_coverage=bool(r0 or ang))
        lq.update(checks=lchecks, failed=lfailed)
        if (lq["score"], lfailed) < (q["score"], bool(q.get("failed"))):
            headers, body, q = lh, lb, lq
            q["grid_source"] = "camelot-lattice"
            if stats is not None:
                stats["grid_lattice"] += 1
            log.info("p%d lattice grid kept (score %.2f)", pno, lq["score"])
    return headers, body, q


def _single_page_tmp(doc: "fitz.Document", pno: int) -> str:
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    single = fitz.open()
    single.insert_pdf(doc, from_page=pno - 1, to_page=pno - 1)
    single.save(path)
    single.close()
    return path


def textalign_tables(page: "fitz.Page", exclude: list) -> list[dict]:
    """Borderless-table probe from word geometry only (no rules needed, no new dependency):
    words are banded into rows by y-proximity first (cell emitters often put one word per
    text line, so pymupdf line numbers can't be trusted), then runs of consecutive rows whose
    words align into >=2 stable columns form a grid. Words are never split, so the mid-token
    failure mode of text-strategy retries cannot occur. Generic."""
    try:
        words = [w for w in page.get_text("words") if (w[4] or "").strip()]
    except Exception:
        return []
    if not words:
        return []
    hs = sorted(w[3] - w[1] for w in words)
    med_h = hs[len(hs) // 2] or 10.0
    by_y = sorted(words, key=lambda w: (w[1], w[0]))
    rows, cur, cur_y = [], [], None
    for w in by_y:  # level 1: one text row per baseline band
        if cur_y is not None and w[1] - cur_y > 0.5 * med_h:
            rows.append(cur)
            cur = []
            cur_y = None
        cur.append(w)
        cur_y = w[1] if cur_y is None else min(cur_y, w[1])
    if cur:
        rows.append(cur)
    rows = [sorted(r, key=lambda w: w[0]) for r in rows if len(r) >= 2]
    out, run = [], []

    def columns_of(run_rows):
        xs = sorted(w[0] for r in run_rows for w in r)
        if not xs:
            return []
        cols, curx = [[xs[0]]], xs[0]
        for x in xs[1:]:
            if x - curx > 12:
                cols.append([x])
            else:
                cols[-1].append(x)
            curx = x
        edges = [sum(c) / len(c) for c in cols]
        if len(edges) < 2:
            return []
        ok = sum(1 for r in run_rows
                 if sum(1 for e in edges if any(abs(w[0] - e) < 8 for w in r)) >= 2)
        return edges if ok / max(1, len(run_rows)) >= 0.6 else []

    def flush():
        if len(run) < 3:
            return
        edges = columns_of(run)
        if not edges:
            return
        grid = []
        for r in run:
            cells = [""] * len(edges)
            for w in r:
                j = min(range(len(edges)), key=lambda e: abs(w[0] - edges[e]))
                if abs(w[0] - edges[j]) < 8:
                    cells[j] = (cells[j] + " " + w[4]).strip()
            grid.append(cells)
        grid = [r for r in grid if any(r)]
        if len(grid) < 3:
            return
        ys = [w[1] for r in run for w in r] + [w[3] for r in run for w in r]
        xs = [w[0] for r in run for w in r] + [w[2] for r in run for w in r]
        import fitz as _fitz
        rect = _fitz.Rect(min(xs), min(ys), max(xs), max(ys))
        if any((rect & e).get_area() > 0.3 * rect.get_area() for e in exclude):
            return
        out.append({"bbox": rect, "rows": grid})

    prev_top = None
    for r in rows:
        top = min(w[1] for w in r)
        if prev_top is not None and top - prev_top > 2.5 * med_h:
            flush()
            run = []
        run.append(r)
        prev_top = top
    flush()
    return out


def to_markdown(headers: list[str], body: list[list[str]]) -> str:
    esc = lambda s: s.replace("|", "\\|")
    lines = ["| " + " | ".join(esc(h) for h in headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in body]
    return "\n".join(lines)


def to_records(headers: list[str], body: list[list[str]]) -> str:
    """One line per row, every value carries its full header path -> survives chunking/embedding."""
    return "\n".join("; ".join(f"{h}: {c}" for h, c in zip(headers, r) if c) for r in body)


def table_chunks(headers, body, page_no, size) -> list[Chunk]:
    out, cur = [], []

    def flush():
        if cur:
            md, rec = to_markdown(headers, cur), to_records(headers, cur)
            out.append(Chunk(page_no, "table", f"{md}\n\nRows as records:\n{rec}",
                             {"table_json": {"headers": headers, "rows": [list(r) for r in cur]}}))
    for r in body:
        trial = cur + [r]
        if cur and len(to_markdown(headers, trial)) + len(to_records(headers, trial)) > size * 2:
            flush()
            cur = [r]
        else:
            cur = trial
    flush()
    return out


# --------------------------------------------------------------------------- vision / OCR
class Models:
    def __init__(self, cfg: Config, need_embed: bool):
        self.cfg = cfg
        self.client = None
        self.vision_calls = 0  # actual vision LLM calls issued (cache hits do not count)
        self.vision_failed = 0  # regions left to OCR-only fallback after retries
        if cfg.base_url and cfg.api_key:
            from openai import OpenAI
            self.client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key, timeout=120)
        elif need_embed or cfg.vision_model:
            raise SystemExit("Missing LITELLM_BASE_URL / LITELLM_API_KEY (or OPENAI_*) in environment/.env")

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs: list[list[float]] = []
        for i in range(0, len(texts), 32):
            kw = {"dimensions": self.cfg.embed_dim} if self.cfg.embed_dim else {}
            for attempt in range(3):
                try:
                    r = self.client.embeddings.create(model=self.cfg.embed_model, input=texts[i:i + 32], **kw)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    log.warning("embed retry: %s", type(e).__name__)
                    time.sleep(2 ** attempt)
            vecs += [d.embedding for d in sorted(r.data, key=lambda d: d.index)]
        return vecs

    def vision_key(self, img: Image.Image) -> tuple[str, str]:
        """Stable cache key for one vision input: sha256(png bytes + model + prompt version)."""
        im = img.copy()
        im.thumbnail((2000, 2000))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "PNG")
        raw = buf.getvalue()
        key = hashlib.sha256(raw + b"\x00" + (self.cfg.vision_model or "").encode()
                             + b"\x00" + str(VISION_PROMPT_VERSION).encode()).hexdigest()
        return key, "data:image/png;base64," + base64.b64encode(raw).decode()

    def vision(self, img: Image.Image, hint: str = "") -> dict | None:
        if not (self.client and self.cfg.vision_model):
            return None
        _, uri = self.vision_key(img)
        prompt = VISION_PROMPT + (f"\nContext: {hint}" if hint else "")
        self.vision_calls += 1
        for attempt in range(3):  # proxy timeouts / rate limits: backoff, then give up
            try:
                r = self.client.chat.completions.create(
                    model=self.cfg.vision_model, temperature=0, max_tokens=1500,
                    messages=[{"role": "user", "content": [{"type": "text", "text": prompt},
                                                            {"type": "image_url", "image_url": {"url": uri}}]}])
                break
            except Exception as e:
                if attempt == 2:
                    log.warning("vision call failed (%s): %s", type(e).__name__, str(e)[:120])
                    self.vision_failed += 1
                    return None
                time.sleep(2 ** attempt)
        raw = r.choices[0].message.content or ""
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            return json.loads(m.group()) if m else {"kind": "text", "transcription": raw, "summary": ""}
        except Exception as e:
            log.warning("vision non-JSON reply (%s)", type(e).__name__)
            return {"kind": "text", "transcription": raw, "summary": ""}


VISION_PROMPT = """You analyse ONE image taken from a document page. Reply with ONLY a JSON object:
{"kind": "diagram|text|table|photo|decorative",
 "transcription": "all readable text verbatim; non-Latin scripts stay in their own script in logical (reading) order; tables as Markdown with full header paths",
 "summary": "for flowcharts, network diagrams and infographics ONLY: (1) numbered steps in order, (2) each decision as 'If <condition> -> <A>, else -> <B>', (3) connectivity as 'A -> B (label)', (4) one-sentence purpose. Empty string otherwise."}
Never invent labels that are not visible."""

# Bump whenever VISION_PROMPT or the vision/OCR selection rules change: the vision cache
# key includes this version, so a change re-analyses every image instead of reusing stale output.
VISION_PROMPT_VERSION = 2


def norm_text(s: str) -> str:
    """Normalised text for agreement scoring (single source of truth with run_eval)."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = _BIDI_MARKS.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_tokens(s: str) -> list[str]:
    return norm_text(s).split()


# Selection thresholds (issue 1): chosen by sweeping on HELD-OUT regions, flat region.
# If vision's transcription materially disagrees with OCR, prefer vision; else keep OCR
# (cheaper, deterministic). See analysis doc §3 sweep table.
SELECT_TOKEN_DISAGREE = 0.10  # fraction of vision tokens absent from OCR
SELECT_LINE_COVERAGE = 0.80  # fraction of vision lines present in OCR (below => prefer vision)


def select_transcription(ocr_text: str, v: dict | None) -> tuple[str, str, float]:
    """Pick per region between OCR and vision transcription. Returns (text, via, disagreement)."""
    vt = (v or {}).get("transcription", "")
    if not v or not vt.strip():
        return ocr_text, "tesseract", 0.0
    ot, vtok = norm_tokens(ocr_text), norm_tokens(vt)
    oset = set(ot)
    dis = sum(1 for t in vtok if t not in oset) / max(1, len(vtok))
    o_lines = {ln for ln in norm_text(ocr_text).split("\n") if ln}
    v_lines = [ln for ln in norm_text(vt).split("\n") if ln]
    line_cov = sum(1 for ln in v_lines if ln in o_lines) / max(1, len(v_lines))
    if dis > SELECT_TOKEN_DISAGREE or line_cov < SELECT_LINE_COVERAGE:
        return v["transcription"], "vision", round(dis, 3)
    return ocr_text, "tesseract", round(dis, 3)


def ocr_image(img: Image.Image, lang: str) -> tuple[str, float]:
    import pytesseract
    g = ImageOps.autocontrast(img.convert("L"))
    if g.width < 1200:
        g = g.resize((g.width * 2, g.height * 2), Image.LANCZOS)
    d = pytesseract.image_to_data(g, lang=lang, config="--psm 3", output_type=pytesseract.Output.DICT)
    lines: dict[tuple, list[str]] = {}
    confs = []
    for i, w in enumerate(d["text"]):
        if not w.strip():
            continue
        lines.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append(w)
        if float(d["conf"][i]) >= 0:
            confs.append(float(d["conf"][i]))
    text = unicodedata.normalize("NFKC", "\n".join(" ".join(v) for v in lines.values()))
    text = _BIDI_MARKS.sub("", text)  # Tesseract inserts LRM/RLM/embedding marks; useless (and noisy) for embeddings
    return text, (sum(confs) / len(confs) if confs else 0.0)


# --------------------------------------------------------------------------- page processing
def page_upright(page: fitz.Page, dpi: int) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def extract_pages(doc: fitz.Document, pages: list[int], cfg: Config, models: Models, stats: Counter,
                  vcache: dict | None = None, doc_id: str = "") -> list[Chunk]:
    norm_doc, info = normalise_pages(doc, pages)
    log.info("[STEP 2/7] pages normalised: %d pages upright (rotation reset)", len(pages))
    # pass 1: text blocks per page (needed for repeated header/footer detection)
    log.info("[STEP 3/7] pass 1: scanning text blocks + table rects (no LLM, no OCR) ...")
    per_page = {}
    taprobe: dict[int, list[dict]] = {}
    for pno in pages:
        page = info[pno][2]
        tables = []
        try:
            tables = list(page.find_tables().tables)
        except Exception as e:
            log.warning("p%d find_tables failed: %s", pno, e)
        rects = [fitz.Rect(t.bbox) for t in tables]
        # borderless probe: only when ruled extraction found nothing on the page, so
        # native-text chunking elsewhere is untouched and the probe stays cheap
        taprobe[pno] = [] if tables else textalign_tables(page, rects)
        rects += [t["bbox"] for t in taprobe[pno]]
        blocks = []
        for b in page.get_text("blocks", sort=True):
            if b[6] != 0:
                continue
            r = fitz.Rect(b[:4])
            if any((r & tr).get_area() > 0.5 * max(r.get_area(), 1) for tr in rects):
                continue
            blocks.append(b[4].strip())
        per_page[pno] = (tables, blocks)
    freq = Counter(norm_key(x) for _, bl in per_page.values() for x in set(bl))
    repeated = {k for k, v in freq.items() if len(pages) >= 5 and v >= 0.4 * len(pages)}

    chunks: list[Chunk] = []
    prev_sig, prev_tid, prev_page, table_seq = None, None, 0, 0
    lat_pages: dict[int, list] = {}
    log.info("[STEP 4/7] pass 2: extracting text -> tables -> images/vision per page ...")
    for pno in pages:
        orig = doc[pno - 1]
        r0, ang, page = info[pno]
        tables, blocks = per_page[pno]
        if r0 or ang:
            stats["pages_rotated"] += 1
            log.info("p%d rotated: /Rotate=%d content=%d deg -> normalised upright", pno, r0, ang)
        page_chunks: list[Chunk] = []

        # -- native text
        text = "\n\n".join(b for b in blocks if norm_key(b) not in repeated and len(b) > 1)
        text = fix_native_arabic(text)
        rot_meta = {"original_rotate": r0, "content_rotation": ang, "rotation_applied": (r0 + ang) % 360}
        for t in split_text(text, cfg.chunk_size, cfg.overlap):
            page_chunks.append(Chunk(pno, "text", t, dict(rot_meta)))

        # -- tables
        for tb in tables:
            headers, body, q = contend_grids(doc, pno, page, tb, r0, ang, lat_pages, stats)
            # table identity for split-across-pages linking (same header signature on
            # consecutive pages => one logical table)
            sig = norm_text("|||".join(headers)) if headers else ""
            if sig and sig == prev_sig and pno == prev_page + 1:
                table_id, continued = prev_tid, True
            else:
                table_seq += 1
                table_id = str(uuid.uuid5(POINT_NS, f"{doc_id}|table|{table_seq}"))
                continued = False
            prev_sig, prev_tid, prev_page = sig, table_id, pno
            tmeta = {"table_id": table_id, **rot_meta}
            if continued:
                tmeta["continued_from"] = True
            if not body:  # header-only grid: keep the words, never silently drop
                hdr_text = " / ".join(h for h in headers if h and not re.fullmatch(r"col\d+", h))
                if hdr_text.strip():
                    page_chunks.append(Chunk(pno, "text", hdr_text.strip(), {**tmeta, "container": "box"}))
                else:
                    stats["tables_empty"] += 1
                    log.warning("p%d empty table grid dropped (no headers, no body)", pno)
                continue
            if len(headers) <= 1:
                # bordered callout/paragraph box, not a table: single column of prose
                rec = to_records(headers, body)
                page_chunks.append(Chunk(pno, "text", rec, {**tmeta, "container": "box", "quality": q}))
                stats["callout_boxes"] += 1
                continue
            # Every PyMuPDF grid gets a Camelot-lattice challenger (shared helper, so the
            # eval measures exactly what ingest runs). Best score wins, ties keep PyMuPDF.
            if q["score"] > 0.35 or q.get("failed"):  # grid recovery failed OR structurally suspect
                v = None
                vkey = None
                if models.cfg.vision_model:
                    z = cfg.dpi / 72
                    crop = page_upright(page, cfg.dpi).crop(tuple(int(x * z) for x in tb.bbox))
                    vkey, _ = models.vision_key(crop)
                    if vcache and vkey in vcache:
                        stats["vision_cache_hits"] += 1
                        v = dict(vcache[vkey])
                    else:
                        v = models.vision(crop, "This crop is a table.")
                if v and v.get("transcription"):
                    cmeta: dict = {"via": "vision", "quality": q, **tmeta}
                    if vkey:
                        cmeta["image_hash"] = vkey
                        cmeta["vision_result"] = {k: v.get(k, "") for k in ("kind", "transcription", "summary")}
                    page_chunks.append(Chunk(pno, "table", v["transcription"], cmeta))
                    stats["tables_vision"] += 1
                else:  # keep the words, flag the structure as unreliable
                    raw = page.get_text("text", clip=fitz.Rect(tb.bbox)).strip()
                    for t in split_text(raw, cfg.chunk_size, cfg.overlap):
                        page_chunks.append(Chunk(pno, "table", t, {"via": "text_fallback", "quality": q,
                                                                   "table_structure": "degraded", **tmeta}))
                    stats["tables_degraded"] += 1
                    log.warning("p%d table structure degraded (score %.2f) - text kept, no grid", pno, q["score"])
                continue
            for c in table_chunks(headers, body, pno, cfg.chunk_size):
                c.meta.update({"quality": q, **tmeta})
                page_chunks.append(c)
            stats["tables"] += 1

        # -- text-alignment tables (borderless pages only): word-geometry grids scored
        # with the same function + gate; accepted ones become table chunks, the rest
        # stays native text (blocks already excluded above, so nothing duplicates)
        for ta in taprobe.get(pno, []):
            th, tb2, tq = table_to_structured(ta["rows"])
            tq["score"] = _grid_score(th, tb2, tq)
            tfailed, tchecks = structural_flags(th, tb2, page, ta["bbox"])
            tq.update(checks=tchecks, failed=tfailed, grid_source="text-alignment")
            if tq["score"] > 0.35 or tfailed:
                continue
            table_seq += 1
            tmeta2 = {"table_id": str(uuid.uuid5(POINT_NS, f"{doc_id}|table|{table_seq}")),
                      "via": "text_alignment", "quality": tq, **rot_meta}
            for c in table_chunks(th, tb2, pno, cfg.chunk_size):
                c.meta.update(tmeta2)
                page_chunks.append(c)
            stats["grid_textalign"] += 1
            log.info("p%d text-alignment grid kept (%d rows)", pno, len(tb2))

        # -- embedded images (scans, Arabic screenshots, diagrams), in reading order:
        # top-to-bottom bands; within a band left-to-right, or right-to-left when the
        # page's native text reads RTL. Generic geometry, no per-document assumptions.
        seen = set()
        page_area = page.rect.get_area()
        cands = []
        for im_info in orig.get_image_info(xrefs=True):
            xref = im_info.get("xref", 0)
            if not xref or xref in seen or fitz.Rect(im_info["bbox"]).get_area() < cfg.min_image_frac * page_area:
                continue
            seen.add(xref)
            cands.append((fitz.Rect(im_info["bbox"]), xref))
        rtl_page = "ar" in languages(text)
        for band in reading_order([r for r, _ in cands]):
            for i in sorted(band, key=lambda j: cands[j][0].x0, reverse=rtl_page):
                rect, xref = cands[i]
                try:
                    raw = doc.extract_image(xref)["image"]
                    img = Image.open(io.BytesIO(raw)).convert("RGB")
                except Exception as e:
                    log.warning("p%d image xref %s unreadable: %s", pno, xref, e)
                    continue
                page_chunks += image_chunks(img, pno, models, cfg, stats, f"embedded image xref {xref}", vcache)

        # -- vector-only diagrams (no raster image, no table, many drawing ops)
        if not tables and not seen and len(page.get_drawings()) >= cfg.vector_min_drawings:
            if models.cfg.vision_model:
                img = page_upright(page, cfg.dpi)
                page_chunks += image_chunks(img, pno, models, cfg, stats, "vector drawing page", vcache)
            else:  # native labels are already captured; the flow itself needs a vision model
                stats["vector_pages_unsummarised"] += 1
                log.warning("p%d looks like a vector diagram; set VISION_MODEL to summarise the flow", pno)

        # -- whole-page scan fallback: no native text, no tables, no usable embedded
        # images (e.g. a page that is a single raster with no image XObjects).
        if not page_chunks and not text.strip() and not tables:
            img = page_upright(page, cfg.dpi)
            page_chunks += image_chunks(img, pno, models, cfg, stats, "full-page scan", vcache)
        # -- truly nothing extractable: say so, never silently drop
        if not page_chunks:
            stats["pages_empty"] += 1
            log.warning("p%d produced no content", pno)
        # chunk_index is assigned here, after the full page is assembled in a fixed
        # order (text, tables, images, vector page). The vision cache guarantees the
        # same vision outputs on re-runs, hence the same chunk counts and stable ids.
        for i, c in enumerate(page_chunks):
            c.index = i
        chunks += page_chunks
    return chunks


def image_chunks(img, pno, models: Models, cfg: Config, stats: Counter, label: str,
                 vcache: dict | None = None) -> list[Chunk]:
    src_img = img
    img, osd_ang = osd_rotate(img)
    if osd_ang:
        stats["images_rotated"] += 1
    img, skew_ang = deskew(img)
    # Dual-hash vision lookup: OSD/deskew sit on confidence thresholds and can flip the
    # same source image between runs. The final-bytes key is tried first; the pre-transform
    # source key is the fallback so a threshold flip reuses the stored analysis instead of
    # spending a call and churning the chunk. Tradeoff is documented in failure case 11.
    srckey = None
    if models.cfg.vision_model:
        srckey, _ = models.vision_key(src_img)
    try:
        ocr_text, conf = ocr_image(img, cfg.ocr_lang)
    except Exception as e:
        log.warning("p%d OCR failed (%s) - is tesseract + ara traineddata installed?", pno, e)
        ocr_text, conf = "", 0.0
    meta = {"ocr_confidence": round(conf, 1), "rotation_applied": osd_ang, "deskew_angle": skew_ang,
            "origin": label}
    # Vision cache: same image bytes + model + prompt version => reuse the stored
    # analysis instead of calling the LLM again (its output is nondeterministic).
    # Deterministic inputs (OCR text, extraction order) then reproduce identical chunks.
    vkey = None
    if models.cfg.vision_model:
        vkey, _ = models.vision_key(img)
        meta["image_hash"] = vkey
        if srckey and srckey != vkey:
            meta["image_hash_src"] = srckey
    v = None
    if vcache:
        hit = vcache.get(vkey) if vkey else None
        if hit is None and srckey:
            hit = vcache.get(srckey)
            if hit is not None:
                stats["vision_cache_src_hits"] += 1
        if hit is not None:
            stats["vision_cache_hits"] += 1
            v = dict(hit)
        else:
            v = models.vision(img, label)
    else:
        v = models.vision(img, label)
    if v is not None:
        meta["vision_result"] = {k: v.get(k, "") for k in ("kind", "transcription", "summary")}
    out: list[Chunk] = []
    kind = (v or {}).get("kind", "text")
    if kind in ("photo", "decorative") and len(ocr_text) < 30:
        stats["images_skipped"] += 1
        return out
    if v and kind == "diagram" and v.get("summary"):
        body = f"Diagram summary:\n{clean_summary(v['summary'])}"
        vt = (v.get("transcription") or "").strip()
        if vt:  # prefer the vision transcription for labels when it exists
            labels = vt
        else:  # otherwise OCR with below-floor tokens removed
            labels, _ = ocr_text_denoised(img, cfg.ocr_lang)
        if labels.strip():
            body += f"\n\nVisible labels:\n{labels.strip()}"
        out.append(Chunk(pno, "diagram", body, {**meta, "via": "vision+ocr"}))
        stats["diagrams"] += 1
        return out
    text, via, dis = select_transcription(ocr_text, v)
    meta["alt_via_disagreement"] = dis
    if text.strip():
        for t in split_text(text, cfg.chunk_size, cfg.overlap):
            out.append(Chunk(pno, "table" if kind == "table" else "image_ocr", t, {**meta, "via": via}))
        stats["images_ocr"] += 1
    return out


def clean_summary(summary: str) -> str:
    """Drop empty/"None"-style lines from a model summary. Generic: no section names hardcoded."""
    keep = []
    for ln in (summary or "").split("\n"):
        s = ln.strip().rstrip(".")
        if not s or s.lower() in ("none", "n/a", "null", "-"):
            continue
        keep.append(ln.rstrip())
    return "\n".join(keep).strip()


def ocr_text_denoised(img: Image.Image, lang: str, floor: float = 30.0) -> tuple[str, int]:
    """OCR text with below-floor-confidence tokens removed. Returns (text, dropped_count)."""
    import pytesseract
    from PIL import ImageOps
    g = ImageOps.autocontrast(img.convert("L"))
    if g.width < 1200:
        g = g.resize((g.width * 2, g.height * 2), Image.LANCZOS)
    d = pytesseract.image_to_data(g, lang=lang, config="--psm 3", output_type=pytesseract.Output.DICT)
    lines: dict[tuple, list[str]] = {}
    dropped = 0
    for i, w in enumerate(d["text"]):
        if not w.strip():
            continue
        try:
            c = float(d["conf"][i])
        except (TypeError, ValueError):
            c = -1.0
        if c >= 0 and c < floor:
            dropped += 1
            continue
        lines.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append(w)
    text = unicodedata.normalize("NFKC", "\n".join(" ".join(v) for v in lines.values()))
    return _BIDI_MARKS.sub("", text), dropped


# --------------------------------------------------------------------------- qdrant
def payload_for(c: Chunk, doc_id: str, fname: str, total: int, file_sha: str, model: str, embed_text: str) -> dict:
    # Keys number/article_number/short_description/category/heading_path mirror the
    # ServiceNow KB indexer (src/barq_ai_support/ingestion/chunker.py) so the
    # assistant's retriever (retrieval/retriever.py) reads PDF and KB points
    # identically: text via short_description or text, provenance via
    # number or article_number, category as plain string, heading_path as list.
    # short_description/number/article_number stay "" so they fall through to text
    # and a PDF point can never be mistaken for KB article "KB…".
    # (retriever.py:160 is the only payload→article_number mapping in the codebase.)
    p = {
        "source_type": "pdf",
        "doc_id": doc_id,
        "source_filename": fname,
        "page_number": c.page,
        "total_pages": total,
        "chunk_index": c.index,
        "content_type": c.content_type,
        "title": f"{fname} - page {c.page}",
        "text": c.text,
        "number": "",
        "article_number": "",
        "short_description": "",
        "category": "PDF",
        "heading_path": [],
        "languages": languages(c.text),
        "file_sha256": file_sha,
        "embedding_model": model,
        "content_hash": hashlib.sha256((model + "\x00" + embed_text).encode()).hexdigest(),
    }
    # Retired procedures (a denylisted revision inside the manual): flag, don't drop, so the
    # retriever can filter them out or show them as explicit "do not apply".
    for rx, sup in RETIRED_PATTERNS:
        if rx.search(c.text):
            p["retired"] = True
            p["superseded_by"] = sup
            break
    if not p.get("retired") and (RETIRED_STRONG.search(c.text)
                                  or _weak_cue_near_version(c.text)):
        p["retired_hint"] = True
    p.update({k: v for k, v in c.meta.items() if v not in (None, "")})
    return p


def point_id(doc_id: str, page: int, idx: int) -> str:
    return str(uuid.uuid5(POINT_NS, f"{doc_id}|{page}|{idx}"))


def connect_qdrant(cfg: Config):
    from qdrant_client import QdrantClient
    if not cfg.qdrant_url or not cfg.collection:
        raise SystemExit("Missing QDRANT_URL / QDRANT_COLLECTION")
    return QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_key, timeout=60)


def ensure_collection(client, cfg: Config, dim: int, create: bool) -> str | None:
    """Verify the shared collection has the same dimensionality. Returns the vector name (or None)."""
    from qdrant_client.models import Distance, VectorParams
    try:
        info = client.get_collection(cfg.collection)
    except Exception:
        if not create:
            raise SystemExit(f"Collection '{cfg.collection}' not found (use --create-collection for a local test)")
        client.create_collection(cfg.collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
        log.info("created collection %s (dim=%d)", cfg.collection, dim)
        return cfg.vector_name
    vecs = info.config.params.vectors
    name = cfg.vector_name
    if isinstance(vecs, dict):
        if not name:
            if len(vecs) != 1:
                raise SystemExit(f"Named vectors {list(vecs)}: set QDRANT_VECTOR_NAME")
            name = next(iter(vecs))
        size = vecs[name].size
    else:
        size = vecs.size
    if size != dim:
        raise SystemExit(f"Dimension mismatch: collection={size}, embedding model={dim}. "
                         f"Use the same EMBEDDING_MODEL/EMBEDDING_DIM as the KB indexer.")
    return name


def ensure_indexes(client, collection: str):
    from qdrant_client.models import PayloadSchemaType
    for f in ("source_type", "doc_id", "content_type"):
        try:
            client.create_payload_index(collection, f, PayloadSchemaType.KEYWORD)
        except Exception:
            pass  # already exists / not permitted


# --------------------------------------------------------------------------- main
def parse_pages(spec: str | None, n: int) -> list[int]:
    if not spec:
        return list(range(1, n + 1))
    out: list[int] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += range(int(a), int(b or a) + 1)
    return [p for p in out if 1 <= p <= n]


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest a PDF into the shared Qdrant collection.")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--doc-id", help="stable document identifier (default: slug of filename)")
    ap.add_argument("--collection")
    ap.add_argument("--pages", help="e.g. 1-3,7")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--dry-run", action="store_true", help="parse + chunk only; no embeddings, no Qdrant writes")
    ap.add_argument("--create-collection", action="store_true", help="create collection if missing (local tests only)")
    ap.add_argument("--no-vision", action="store_true", help="skip the vision LLM (Tesseract only)")
    ap.add_argument("--no-prune", action="store_true", help="do not delete stale points of this doc_id")
    ap.add_argument("--ocr-lang", default="ara+eng")
    ap.add_argument("--chunk-size", type=int, default=900)
    ap.add_argument("--overlap", type=int, default=120)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--dump-chunks", type=Path, help="write chunks as JSONL (evidence / debugging)")
    ap.add_argument("--json-summary", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(message)s",
                        stream=sys.stderr)
    try:
        from dotenv import load_dotenv
        load_dotenv(a.env_file)
    except ImportError:
        pass
    if not a.pdf.is_file():
        log.error("FAIL file not found: %s", a.pdf)
        raise SystemExit(f"File not found: {a.pdf}")

    cfg = Config.from_env(a)
    if not a.dry_run and not cfg.embed_model:
        log.error("FAIL missing EMBEDDING_MODEL (set it in .env)")
        raise SystemExit("Missing EMBEDDING_MODEL")
    t0 = time.time()
    raw = a.pdf.read_bytes()
    file_sha = hashlib.sha256(raw).hexdigest()
    doc_id = a.doc_id or "pdf-" + re.sub(r"[^a-z0-9]+", "-", a.pdf.stem.lower()).strip("-")
    fname = a.pdf.name
    log.info("doc_id=%s sha256=%s endpoint=%s", doc_id, file_sha[:12],
             urlparse(cfg.base_url).netloc if cfg.base_url else "-")

    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        if doc.is_encrypted:  # fitz opens lazily; fail fast with a clear message
            raise ValueError("document is password-protected")
        _ = doc.page_count
    except SystemExit:
        raise
    except Exception as e:
        log.error("FAIL cannot open PDF %s: %s: %s", fname, type(e).__name__, str(e)[:160])
        raise SystemExit(f"Cannot open PDF (corrupt or password-protected): {type(e).__name__}: {str(e)[:160]}")
    log.info("[STEP 1/7] opened %s: %d pages, doc_id=%s", fname, doc.page_count, doc_id)
    pages = parse_pages(a.pages, doc.page_count)
    models = Models(cfg, need_embed=not a.dry_run)
    stats: Counter = Counter()
    # Warm the vision cache from existing points of this doc_id so re-runs reuse
    # stored analyses (keyed by image content) instead of calling the LLM again.
    vcache: dict = {}
    client = None
    if not a.dry_run:
        try:
            client = connect_qdrant(cfg)
            from qdrant_client.models import FieldCondition, Filter, MatchValue
            cflt = Filter(must=[FieldCondition(key="source_type", match=MatchValue(value="pdf")),
                                FieldCondition(key="doc_id", match=MatchValue(value=doc_id))])
            off = None
            while True:
                pts_, off = client.scroll(cfg.collection, scroll_filter=cflt, limit=256, offset=off,
                                          with_payload=["image_hash", "image_hash_src", "vision_result"],
                                          with_vectors=False)
                for p in pts_:
                    h = (p.payload or {}).get("image_hash")
                    vr = (p.payload or {}).get("vision_result")
                    if h and isinstance(vr, dict) and h not in vcache:
                        vcache[h] = vr
                    hs = (p.payload or {}).get("image_hash_src")
                    if hs and isinstance(vr, dict) and hs not in vcache:
                        vcache[hs] = vr
                if off is None:
                    break
            log.info("vision cache: %d entries for doc_id=%s", len(vcache), doc_id)
        except Exception as e:
            log.warning("vision cache unavailable (%s); vision calls will run", type(e).__name__)
            client = None
    chunks = extract_pages(doc, pages, cfg, models, stats, vcache, doc_id)
    log.info("extracted %d chunks from %d pages", len(chunks), len(pages))
    _bt = Counter(c.content_type for c in chunks)
    log.info("[STEP 5/7] extraction done: %d chunks (%d text / %d table / %d image_ocr)",
             len(chunks), _bt.get("text", 0), _bt.get("table", 0), _bt.get("image_ocr", 0))

    def embed_text(c: Chunk) -> str:
        return f"[{fname} | page {c.page} | {c.content_type}]\n{c.text}"

    if a.dump_chunks:
        a.dump_chunks.parent.mkdir(parents=True, exist_ok=True)
        with a.dump_chunks.open("w", encoding="utf-8") as f:
            for c in chunks:
                f.write(json.dumps({"id": point_id(doc_id, c.page, c.index), "page": c.page, "index": c.index,
                                    "type": c.content_type, "text": c.text, "meta": c.meta},
                                   ensure_ascii=False) + "\n")

    upserted = skipped = pruned = 0
    dim = None
    log.info("[STEP 6/7] embed + upsert into collection '%s' (dry_run=%s) ...",
             cfg.collection, a.dry_run)
    if not a.dry_run and chunks:
        if client is None:
            client = connect_qdrant(cfg)
        ids = [point_id(doc_id, c.page, c.index) for c in chunks]
        assert len(set(ids)) == len(ids), "point id collision"
        payloads = [payload_for(c, doc_id, fname, doc.page_count, file_sha, cfg.embed_model, embed_text(c))
                    for c in chunks]
        existing = {}
        try:
            for r in client.retrieve(cfg.collection, ids, with_payload=["content_hash"], with_vectors=False):
                existing[str(r.id)] = (r.payload or {}).get("content_hash")
        except Exception:
            pass  # collection may not exist yet
        todo = [i for i, (pid, p) in enumerate(zip(ids, payloads)) if existing.get(pid) != p["content_hash"]]
        skipped = len(chunks) - len(todo)
        probe_vec = models.embed([embed_text(chunks[todo[0] if todo else 0])])[0]
        dim = len(probe_vec)
        vname = ensure_collection(client, cfg, dim, a.create_collection)
        ensure_indexes(client, cfg.collection)
        if todo:
            from qdrant_client.models import PointStruct
            vecs = [probe_vec] + models.embed([embed_text(chunks[i]) for i in todo[1:]]) if len(todo) > 1 else [probe_vec]
            now = datetime.now(timezone.utc).isoformat()
            pts = []
            for i, v in zip(todo, vecs):
                payloads[i]["ingested_at"] = now
                pts.append(PointStruct(id=ids[i], vector={vname: v} if vname else v, payload=payloads[i]))
            for k in range(0, len(pts), 64):
                client.upsert(cfg.collection, points=pts[k:k + 64], wait=True)
            upserted = len(pts)
        if not a.no_prune and not a.pages:
            from qdrant_client.models import FieldCondition, Filter, MatchValue, PointIdsList
            flt = Filter(must=[FieldCondition(key="source_type", match=MatchValue(value="pdf")),
                               FieldCondition(key="doc_id", match=MatchValue(value=doc_id))])
            stale, off = [], None
            while True:
                pts_, off = client.scroll(cfg.collection, scroll_filter=flt, limit=256, offset=off,
                                          with_payload=False, with_vectors=False)
                stale += [str(p.id) for p in pts_ if str(p.id) not in set(ids)]
                if off is None:
                    break
            if stale:
                client.delete(cfg.collection, points_selector=PointIdsList(points=stale), wait=True)
                pruned = len(stale)

    by_type = Counter(c.content_type for c in chunks)
    log.info("[STEP 7/7] done: upserted=%d skipped=%d pruned=%d", upserted, skipped, pruned)
    if models.vision_failed or stats["pages_empty"]:
        log.error("FAIL completed with losses: vision_failed=%d pages_empty=%d tables_degraded=%d",
                  models.vision_failed, stats["pages_empty"], stats["tables_degraded"])
    else:
        log.info("SUCCESS %s: %d chunks (upserted=%d skipped=%d pruned=%d vision_calls=%d) in %.1fs",
                 doc_id, len(chunks), upserted, skipped, pruned,
                 models.vision_calls, time.time() - t0)
    summary = {
        "file": fname, "doc_id": doc_id, "sha256": file_sha, "pages": len(pages),
        "chunks": len(chunks), "chunks_by_type": dict(by_type),
        "pages_rotated": stats["pages_rotated"], "images_rotated": stats["images_rotated"],
        "tables": stats["tables"], "tables_via_vision": stats["tables_vision"], "tables_degraded": stats["tables_degraded"],
        "image_ocr_regions": stats["images_ocr"], "diagrams_summarised": stats["diagrams"],
        "arabic_chunks": sum(1 for c in chunks if "ar" in languages(c.text)),
        "pages_empty": stats["pages_empty"], "vector_pages_unsummarised": stats["vector_pages_unsummarised"],
        "vector_dim": dim,
        "collection": None if a.dry_run else cfg.collection,
        "vision_calls": models.vision_calls, "vision_cache_hits": stats["vision_cache_hits"],
        "vision_cache_src_hits": stats["vision_cache_src_hits"], "vision_failed": models.vision_failed,
        "upserted": upserted, "unchanged_skipped": skipped, "pruned_stale": pruned,
        "dry_run": a.dry_run, "seconds": round(time.time() - t0, 1),
    }
    if a.json_summary:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print("\n=== ingestion summary ===")
        for k, v in summary.items():
            print(f"{k:22} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
