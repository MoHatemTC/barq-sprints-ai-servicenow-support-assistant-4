#!/usr/bin/env python3
"""Tool + gate + diagram comparison for the S3.5 PR (review issues 1-4, 6, 7).

Deps for competitors isolated in /tmp/opencode/evaltools; cli/requirements.txt untouched.
Pipeline pieces are IMPORTED from cli/ingest_pdf (single source of truth):
ocr_image, osd_rotate, deskew, norm_text, select_transcription, best_table.

Reports dev fixture vs held-out side by side. Inputs:
- dev tables: challenge p1/p3 + 10 manual tables (docs/eval/ground_truth.json)
- held-out tables: challenge-heldout/*.truth.json (from the generator, frozen seeds 11/22/33)
- OCR pages: challenge p2/p7 + every held-out truth "ocr" page
- diagrams: ground-truth fact lists (nodes, decisions+branches, edges); summaries read
  from indexed payloads (challenge + held-out, one TEST collection) — no extra vision calls.

Raw outputs: out/eval/. Timings per tool.
"""
import difflib
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cli"))
sys.path.insert(0, "/tmp/opencode/evaltools")

from ingest_pdf import (  # noqa: E402
    best_table, contend_grids, deskew, norm_text, normalise_pages, ocr_image, osd_rotate,
    select_transcription, structural_flags, table_to_structured, textalign_tables,
    to_records, _grid_score,
)

CHALLENGE = ROOT / "challenge" / "barq_challenge.pdf"
MANUAL = ROOT / "challenge" / "BARQ_IT_Service_Desk_Manual_Ed5.1.pdf"
HELDOUT = ROOT / "challenge-heldout"
GT = json.loads((ROOT / "docs" / "eval" / "ground_truth.json").read_text())
OUT = ROOT / "out" / "eval"
OUT.mkdir(parents=True, exist_ok=True)

DEV_TABLES = [  # (pdf, page, truth id)
    (CHALLENGE, 1, "ch-p1-sla"), (CHALLENGE, 3, "ch-p3-wifi"),
    (MANUAL, 10, "m-p10-lifecycle"), (MANUAL, 11, "m-p11-priority"),
    (MANUAL, 11, "m-p11-sla"), (MANUAL, 13, "m-p13-escalation"),
    (MANUAL, 15, "m-p15-catalogue"), (MANUAL, 17, "m-p17-index"),
    (MANUAL, 29, "m-p29-problems"), (MANUAL, 30, "m-p30-knownerrors"),
    (MANUAL, 33, "m-p33-actions"), (MANUAL, 50, "m-p50-directory"),
]
GT_TABLES = {t["id"]: t for t in GT["tables"]}


def records_of(t: dict) -> list[str]:
    if "records" in t:
        return t["records"]
    recs = []
    for row in t["rows"]:
        for h, v in zip(t["headers"], row):
            if h and str(v).strip():
                recs.append(f"{h}: {v}")
    return recs


def heldout_tables():
    out = []
    for tf in sorted(HELDOUT.glob("*.truth.json")):
        truth = json.loads(tf.read_text())
        pdf = tf.with_suffix("").with_suffix(".pdf")
        for i, t in enumerate(truth.get("tables", [])):
            pg = t["page"] if isinstance(t["page"], int) else int(str(t["page"]).split("-")[0])
            out.append((pdf, pg, f"{tf.stem}#tbl{i}", t))
    return out


def score_records(output_text: str, recs: list[str]):
    o = norm_text(output_text)
    hits, total, missing = 0, len(recs), []
    for rec in recs:
        h, _, v = rec.partition(":")
        if norm_text(h) and norm_text(v) and norm_text(h) in o and norm_text(v) in o:
            hits += 1
        else:
            missing.append(rec)
    return hits, total, missing


def run_pipeline_tables(targets):
    """Decisions exactly as ingest runs them: normalised pages, contend_grids (PyMuPDF +
    lattice challenger) plus the text-alignment probe on pages with no ruled grids."""
    import fitz
    from ingest_pdf import normalise_pages
    t0 = time.time()
    texts, decisions = {}, {}
    by_pdf: dict = {}
    for pdf, pno, tid, t in targets:
        by_pdf.setdefault(str(pdf), {}).setdefault(pno, []).append((tid, t))
    for f, pages in by_pdf.items():
        doc = fitz.open(f)
        norm, info = normalise_pages(doc, sorted(pages))
        lat_pages: dict = {}
        for pno, items in pages.items():
            r0, ang, page = info[pno]
            try:
                tables = list(page.find_tables().tables)
            except Exception:
                tables = []
            for tb in tables:
                try:
                    h, b, q = contend_grids(doc, pno, page, tb, r0, ang, lat_pages, None)
                except Exception:
                    continue
                fate = "accepted"
                if not b or len(h) <= 1:
                    fate = "reclassified-text"
                elif q.get("score", 9) > 0.35 or q.get("failed"):
                    fate = "vision-or-degraded"
                texts.setdefault((f, pno), []).append((h, b, q, fate))
                decisions.setdefault((f, pno), []).append(
                    {"headers": h, "body": b, "bad": fate == "vision-or-degraded",
                     "fate": fate, "source": q.get("grid_source", "pymupdf")})
            if not tables:
                for ta in textalign_tables(page, []):
                    th, tb2, tq = table_to_structured(ta["rows"])
                    tq["score"] = _grid_score(th, tb2, tq)
                    tf, tc = structural_flags(th, tb2, page, ta["bbox"])
                    tq.update(checks=tc, failed=tf)
                    if tq["score"] > 0.35 or tf:
                        continue
                    texts.setdefault((f, pno), []).append((th, tb2, tq, "accepted-textalign"))
                    decisions.setdefault((f, pno), []).append(
                        {"headers": th, "body": tb2, "bad": False,
                         "fate": "accepted-textalign", "source": "text-alignment"})
        doc.close()
    flat_texts, flat_grids = {}, {}
    for (pdf, pno, tid, t) in targets:
        parts = []
        doc2 = None
        try:
            for h, b, q, fate in texts.get((str(pdf), pno), []):
                if fate.startswith("accepted"):
                    parts.append(to_records(h, b))
                elif fate == "vision-or-degraded":
                    # no-vision floor, exactly what production emits without a model:
                    # raw page text inside the region (vision transcription does better)
                    if doc2 is None:
                        import fitz as _fitz
                        doc2 = _fitz.open(pdf)
                    clips = []
                    for tb in doc2[pno - 1].find_tables().tables:
                        clips.append(doc2[pno - 1].get_text("text", clip=fitz.Rect(tb.bbox)))
                    parts.append("\n".join(clips))
        finally:
            if doc2 is not None:
                doc2.close()
        flat_texts[(str(pdf), pno, tid)] = "\n".join(parts)
        flat_grids[(str(pdf), pno, tid)] = decisions.get((str(pdf), pno), [])
    return flat_texts, flat_grids, time.time() - t0


def run_pdfplumber(targets, tuned: bool):
    import pdfplumber
    kw = {"vertical_strategy": "text", "horizontal_strategy": "text", "snap_x_tolerance": 3,
          "snap_y_tolerance": 3, "join_tolerance": 3, "edge_min_length": 3} if tuned else {}
    t0 = time.time()
    texts = {}
    for pdf, pno, tid, t in targets:
        parts = []
        try:
            with pdfplumber.open(pdf) as doc:
                for tbl in (doc.pages[pno - 1].extract_tables(kw) or []):
                    if not tbl or not tbl[0]:
                        continue
                    headers = [(c or "").strip() for c in tbl[0]]
                    for row in tbl[1:]:
                        for h, c in zip(headers, row):
                            if h and (c or "").strip():
                                parts.append(f"{h}: {c.strip()}")
        except Exception as e:
            parts.append(f"__ERROR__ {e}")
        texts[(str(pdf), pno, tid)] = "\n".join(parts)
    return texts, time.time() - t0


def run_camelot(targets, flavor: str, tuned: bool):
    import camelot
    kw = {"edge_tol": 100, "row_tol": 10} if (tuned and flavor == "stream") else {}
    t0 = time.time()
    texts = {}
    for pdf, pno, tid, t in targets:
        try:
            tables = camelot.read_pdf(str(pdf), pages=str(pno), flavor=flavor, **kw)
            parts = []
            for tb in tables:
                df = tb.df
                if df.shape[0] < 2:
                    continue
                headers = [str(x).strip() for x in df.iloc[0].tolist()]
                for _, row in df.iloc[1:].iterrows():
                    for h, c in zip(headers, row.tolist()):
                        c = str(c).strip()
                        if h and c:
                            parts.append(f"{h}: {c}")
            texts[(str(pdf), pno, tid)] = "\n".join(parts)
        except Exception as e:
            texts[(str(pdf), pno, tid)] = f"__ERROR__ {type(e).__name__}: {e}"
    return texts, time.time() - t0


def normalised_copy(pdf: Path, pages: list[int], dest: Path):
    """Copy with /Rotate reset (content as stored). Competitors' 'normalised' input."""
    import fitz
    d = fitz.open(pdf)
    for p in pages:
        d[p - 1].set_rotation(0)
    d.save(dest, garbage=3, deflate=True)
    d.close()


def lev(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def render_page(pdf: Path, pno: int, dest: Path, dpi: int = 200) -> Path:
    import fitz
    from PIL import Image
    doc = fitz.open(pdf)
    pix = doc[pno - 1].get_pixmap(dpi=dpi, alpha=False)
    Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save(dest)
    doc.close()
    return dest


def tesseract_with_prefix(png: Path, lang: str, prefix: str | None):
    from PIL import Image
    old = os.environ.get("TESSDATA_PREFIX")
    try:
        if prefix:
            os.environ["TESSDATA_PREFIX"] = prefix
        elif "TESSDATA_PREFIX" in os.environ:
            del os.environ["TESSDATA_PREFIX"]
        img, _ = osd_rotate(Image.open(png).convert("RGB"))
        img, _ = deskew(img)
        return ocr_image(img, lang)[0]
    finally:
        if old is None:
            os.environ.pop("TESSDATA_PREFIX", None)
        else:
            os.environ["TESSDATA_PREFIX"] = old


def fresh_vision(png: Path) -> str:
    """One budgeted vision transcription for eval OCR pages (2 calls total)."""
    import base64
    import io
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from openai import OpenAI
    from PIL import Image
    from ingest_pdf import VISION_PROMPT
    oc = OpenAI(base_url=os.environ["LITELLM_BASE_URL"], api_key=os.environ["LITELLM_API_KEY"],
                timeout=120)
    im = Image.open(png).convert("RGB")
    im.thumbnail((2000, 2000))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    r = oc.chat.completions.create(
        model=os.environ["VISION_MODEL"], temperature=0, max_tokens=1500,
        messages=[{"role": "user", "content": [{"type": "text", "text": VISION_PROMPT},
                                               {"type": "image_url", "image_url": {"url": uri}}]}])
    raw = r.choices[0].message.content or ""
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        v = json.loads(m.group()) if m else {}
    except Exception:
        v = {}
    return v.get("transcription", "")


def qdrant_points(collection: str, doc_ids: list[str]):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from qdrant_client import QdrantClient
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    qc = QdrantClient(url=os.environ["QDRANT_URL"], api_key=os.environ.get("QDRANT_API_KEY"), timeout=60)
    pts = []
    for doc in doc_ids:
        flt = Filter(must=[FieldCondition(key="source_type", match=MatchValue(value="pdf")),
                           FieldCondition(key="doc_id", match=MatchValue(value=doc))])
        off = None
        while True:
            batch, off = qc.scroll(collection, scroll_filter=flt, limit=256, offset=off,
                                   with_payload=True, with_vectors=False)
            pts += batch
            if off is None:
                break
    return pts


def fuzzy_in(needle: str, hay: str, tol: float = 0.8) -> bool:
    n = norm_text(needle)
    h = norm_text(hay)
    if n and n in h:
        return True
    for line in h.split("\n"):
        if n and difflib.SequenceMatcher(None, n, line.strip()).ratio() >= tol:
            return True
    return False


def score_diagram(summary: str, facts: dict):
    found, total, missing = 0, 0, []
    node_names = [norm_text(n) for n in facts.get("nodes", [])]
    checks = []
    for n in facts.get("nodes", []):
        checks.append(("node", n, n))
    for dec in facts.get("decisions", []):
        for br, tgt in dec.get("branches", {}).items():
            checks.append(("decision", f"{dec['node']} --{br}--> {tgt}",
                           f"{dec['node']} {br} {tgt}"))
    for a, b, lab in facts.get("edges", []):
        checks.append(("edge", f"{a} -> {b} ({lab})", f"{a} {b} {lab}"))
    for kind, label, probe in checks:
        total += 1
        if fuzzy_in(probe, summary):
            found += 1
        else:
            missing.append(f"{kind}: {label}")
    arrows = re.findall(r"([A-Za-z0-9][\w ./?&+-]*)\s*->\s*([A-Za-z0-9][\w ./?&+-]*)", summary)
    hallu = 0
    for a, b in arrows:
        na, nb = norm_text(a), norm_text(b)
        if na and nb and not any(na in n or n in na for n in node_names) \
                and not any(nb in n or n in nb for n in node_names):
            hallu += 1
    return found, total, missing, hallu


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Table/OCR/diagram eval, dev vs held-out.")
    ap.add_argument("--collection", default=os.environ.get("PDF_TEST_COLLECTION", "pdf_test"),
                    help="Qdrant collection holding the TEST points (challenge + held-out). "
                         "Never the shared KB collection.")
    coll = ap.parse_args().collection
    missing = [str(p) for p in [CHALLENGE, MANUAL] if not p.is_file()]
    if not HELDOUT.is_dir():
        missing.append(str(HELDOUT) + "/")
    if missing:
        print("FAIL eval fixtures missing: " + ", ".join(missing), file=sys.stderr)
        print("Regenerate synthetics (deterministic): "
              "python cli/make_challenge_pdf.py challenge/barq_challenge.pdf && "
              "python cli/make_heldout_pdfs.py --seeds 11,22,33 --out-dir challenge-heldout",
              file=sys.stderr)
        raise SystemExit(2)
    report = {"tables": {}, "gate": {}, "ocr": {}, "diagrams": {}, "sweep": {}, "not_run": {},
              "timings": {}, "inputs": {}}
    raws = {}
    dev_targets = [(p, n, i, GT_TABLES[i]) for p, n, i in DEV_TABLES]
    held_targets = heldout_tables()
    report["inputs"] = {"dev_tables": len(dev_targets), "heldout_tables": len(held_targets),
                        "heldout_seeds": "11,22,33 (frozen)", "collection": coll}

    # ---- tables, all tools, dev + held-out
    tools = {}
    t0 = time.time()
    try:
        texts, grids, dt = run_pipeline_tables(dev_targets + held_targets)
        tools["pymupdf-pipeline"] = texts
        report["timings"]["pymupdf-pipeline"] = round(dt, 1)
    except Exception as e:
        report["not_run"]["pymupdf-pipeline"] = f"{type(e).__name__}: {e}"
        grids = {}
    for name, fn in [("pdfplumber-default", lambda t: run_pdfplumber(t, False)),
                     ("pdfplumber-tuned", lambda t: run_pdfplumber(t, True))]:
        try:
            texts, dt = fn(dev_targets + held_targets)
            tools[name] = texts
            report["timings"][name] = round(dt, 1)
        except Exception as e:
            report["not_run"][name] = f"{type(e).__name__}: {e}"
    for flavor in ("lattice", "stream"):
        for tuned in (False, True):
            name = f"camelot-{flavor}{'-tuned' if tuned else ''}"
            try:
                if flavor == "lattice" and not shutil.which("gs"):
                    raise RuntimeError("ghostscript ('gs') not installed")
                texts, dt = run_camelot(dev_targets + held_targets, flavor, tuned)
                tools[name] = texts
                report["timings"][name] = round(dt, 1)
            except Exception as e:
                report["not_run"][name] = f"{type(e).__name__}: {e}"

    def truth_of(tid, t):
        return t if isinstance(t, dict) and "headers" in t else GT_TABLES[tid]

    for (pdf, pno, tid, t) in dev_targets + held_targets:
        recs = records_of(truth_of(tid, t))
        if len(recs) > 400:
            recs = recs[:400]
        for name, texts in tools.items():
            v = texts.get((str(pdf), pno, tid), "")
            if v.startswith("__ERROR__"):
                r = {"score": "ERR", "ratio": 0.0, "note": v[:160]}
            else:
                h, n, miss = score_records(v, recs)
                r = {"score": f"{h}/{n}", "ratio": round(h / n, 3), "missing": miss[:4]}
            report["tables"].setdefault(tid, {})[name] = r
    raws["tables"] = {n: {f"{p}:{i}": v[:3000] for (f, p, i), v in t.items()} for n, t in tools.items()}

    # ---- funnel + gate precision/recall (item 5): each extracted grid matched to the
    # best truth table on its page by header overlap; correct = pair accuracy >= 0.8.
    # flat_grids[(pdf,pno,tid)] holds the page's decision list (same list per tid on a page).
    for split, targets in (("dev", dev_targets), ("heldout", held_targets)):
        tp = fp = tn = fn = 0
        fun = {"candidates": 0, "accepted": 0, "reclassified_text": 0, "vision_or_degraded": 0,
               "lattice_routed": 0, "textalign_routed": 0, "actually_correct": 0}
        seen_pages = set()
        for (pdf, pno, tid, t) in targets:
            if (str(pdf), pno) in seen_pages:
                continue
            seen_pages.add((str(pdf), pno))
            cands = [(i, truth_of(i, x)) for (pp, pn, i, x) in targets
                     if str(pp) == str(pdf) and pn == pno]
            for g in grids.get((str(pdf), pno, tid), []):
                gh = {w for h in g["headers"] for w in norm_text(h).split()}
                best_tid, best_ov = cands[0][0], -1
                for i, tt in cands:
                    th = {w for h in tt["headers"] for w in norm_text(h).split()}
                    ov = len(gh & th)
                    if ov > best_ov:
                        best_tid, best_ov = i, ov
                recs = records_of(dict([c for c in cands if c[0] == best_tid][0][1]))
                h, n, _ = score_records(to_records(g["headers"], g["body"]), recs)
                correct = (h / n) >= 0.8 if n else False
                bad = g["bad"]
                fun["candidates"] += 1
                if g["fate"] == "reclassified-text":
                    fun["reclassified_text"] += 1
                elif bad:
                    fun["vision_or_degraded"] += 1
                else:
                    fun["accepted"] += 1
                if g["source"] == "camelot-lattice":
                    fun["lattice_routed"] += 1
                if g["source"] == "text-alignment":
                    fun["textalign_routed"] += 1
                if bad and not correct:
                    tp += 1
                elif bad and correct:
                    fp += 1
                elif not bad and not correct:
                    fn += 1
                else:
                    tn += 1
                if correct:
                    fun["actually_correct"] += 1
        prec = round(tp / max(1, tp + fp), 3)
        rec = round(tp / max(1, tp + fn), 3)
        fun.update({"precision": prec, "recall": rec,
                    "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn}})
        report["gate"][split] = fun
        report["funnel"] = report.get("funnel", {})
        report["funnel"][split] = fun

    # ---- rotated pages: competitors on raw vs normalised copy
    norm_pdf = OUT / "challenge_p3_normalised.pdf"
    normalised_copy(CHALLENGE, [3], norm_pdf)
    for name, targets in [("raw", [(CHALLENGE, 3, "ch-p3-wifi", GT_TABLES["ch-p3-wifi"])]),
                          ("normalised", [(norm_pdf, 3, "ch-p3-wifi", GT_TABLES["ch-p3-wifi"])])]:
        try:
            texts, _ = run_pdfplumber(targets, False)
            v = texts[(str(targets[0][0]), 3, "ch-p3-wifi")]
            h, n, _ = score_records(v, records_of(GT_TABLES["ch-p3-wifi"]))
            report["tables"][f"ch-p3-wifi-rot-{name}"] = {"pdfplumber-default": {"score": f"{h}/{n}"}}
        except Exception as e:
            report["not_run"][f"rot-{name}"] = str(e)[:120]
    # ---- OCR: pipeline's own ocr_image (fast vs best), vision row, selected row
    ocr_pages = [(CHALLENGE, 2, "ch-p2", GT["ocr"]["challenge_p2"]),
                 (CHALLENGE, 7, "ch-p7", GT["ocr"]["challenge_p7"])]
    for tf in sorted(HELDOUT.glob("*.truth.json")):
        truth = json.loads(tf.read_text())
        pdf = tf.with_suffix("").with_suffix(".pdf")
        for o in truth.get("ocr", []):
            if isinstance(o.get("page"), int) and ("scan" in o["kind"] or "image" in o["kind"]):
                ocr_pages.append((pdf, o["page"], f"{tf.stem}#ocr{o['page']}", o["strings"]))
    report["inputs"]["ocr_pages"] = len(ocr_pages)
    best_dir = OUT / "tessdata_best"
    best_dir.mkdir(exist_ok=True)
    for f in ("ara.traineddata", "eng.traineddata", "osd.traineddata"):
        if not (best_dir / f).exists():
            try:
                import urllib.request
                urllib.request.urlretrieve(
                    f"https://github.com/tesseract-ocr/tessdata_best/raw/main/{f}", best_dir / f)
            except Exception as e:
                report["not_run"]["tesseract-best"] = f"download failed ({type(e).__name__})"
                break
    have_best = all((best_dir / f).exists() for f in ("ara.traineddata", "eng.traineddata", "osd.traineddata"))
    t0 = time.time()
    ocr_rows, vision_rows = {}, {}
    try:
        from PIL import Image
        for pdf, pno, key, refs in ocr_pages:
            png = render_page(pdf, pno, OUT / f"ocr-{key.replace('#', '-')}.png")
            ref = norm_text("\n".join(refs))
            hyp_fast = norm_text(tesseract_with_prefix(png, "ara+eng", None))
            ocr_rows[key] = {"fast": hyp_fast, "ref": ref}
            if have_best and "tesseract-best" not in report["not_run"]:
                ocr_rows[key]["best"] = norm_text(tesseract_with_prefix(png, "ara+eng", str(best_dir)))
        report["timings"]["tesseract-ocr-pages"] = round(time.time() - t0, 1)
    except Exception as e:
        report["not_run"]["tesseract-ocr"] = f"{type(e).__name__}: {e}"
    # vision transcriptions: challenge from indexed payloads (free), held-out fresh (budgeted)
    try:
        pts = qdrant_points(coll, ["pdf-barq-challenge"])
        vmap = {}
        for p in pts:
            vr = (p.payload or {}).get("vision_result") or {}
            if vr.get("transcription"):
                vmap.setdefault((p.payload.get("page_number"), None), vr["transcription"])
        chal_v = {}
        for p in pts:
            vr = (p.payload or {}).get("vision_result") or {}
            if vr.get("transcription") and p.payload.get("content_type") in ("image_ocr", "table"):
                chal_v.setdefault(p.payload.get("page_number"), vr["transcription"])
        vision_rows["ch-p2"] = chal_v.get(2, "")
        vision_rows["ch-p7"] = chal_v.get(7, "")
        for pdf, pno, key, refs in ocr_pages:
            if key.startswith("heldout"):
                png = OUT / f"ocr-{key.replace('#', '-')}.png"
                if png.exists():
                    vision_rows[key] = fresh_vision(png)
        report["timings"]["vision-transcriptions"] = "see ingest logs (held-out fresh calls)"
    except Exception as e:
        report["not_run"]["vision-row"] = f"{type(e).__name__}: {e}"
    for pdf, pno, key, refs in ocr_pages:
        ref = norm_text("\n".join(refs))
        row = {"ref_chars": len(ref)}
        for variant in ("fast", "best"):
            hyp = ocr_rows.get(key, {}).get(variant)
            if hyp is None:
                continue
            row[variant] = {"cer": round(lev(ref, hyp) / max(1, len(ref)), 3),
                            "hyp_chars": len(hyp)}
        vh = norm_text(vision_rows.get(key, ""))
        if vh:
            row["vision"] = {"cer": round(lev(ref, vh) / max(1, len(ref)), 3),
                             "hyp_chars": len(vh)}
            sel, via, dis = select_transcription(
                ocr_rows.get(key, {}).get("fast", ""),
                {"transcription": vision_rows.get(key, "")})
            sh = norm_text(sel)
            row["selected"] = {"via": via, "disagreement": dis,
                               "cer": round(lev(ref, sh) / max(1, len(ref)), 3)}
        report["ocr"][key] = row
    macro = {}
    for variant in ("fast", "best", "vision", "selected"):
        cers = [r[variant]["cer"] for r in report["ocr"].values() if variant in r]
        if cers:
            macro[variant] = round(sum(cers) / len(cers), 3)
    report["ocr_macro"] = macro
    worst = sorted(report["ocr"].items(),
                   key=lambda kv: kv[1].get("selected", kv[1].get("fast", {"cer": 0}))["cer"],
                   reverse=True)[:3]
    worst_out = []
    for k, v in worst:
        ref = hyp = ""
        for pdf, pno, key, refs in ocr_pages:
            if key == k:
                ref = norm_text("\n".join(refs))[:400]
                hyp = (ocr_rows.get(key, {}).get("fast", ""))[:400]
        worst_out.append({"page": k, "selected_cer": v.get("selected", {}).get("cer"),
                          "via": v.get("selected", {}).get("via"),
                          "ref_head": ref, "hyp_head": norm_text(hyp)[:400]})
    report["ocr_worst3"] = worst_out

    # ---- diagrams: fact scoring with document-aware greedy best-pair matching
    # (a chunk is scored only against truth from its own document+page, best match wins)
    dev_diagrams = GT.get("diagrams", {})
    dev_chunks = [(p.payload or {}) for p in qdrant_points(coll, ["pdf-barq-challenge"])
                  if (p.payload or {}).get("content_type") == "diagram"]
    for key, facts in dev_diagrams.items():
        cands = [pl for pl in dev_chunks if pl.get("page_number") == facts.get("page")]
        best = None
        for pl in cands:
            f, n, miss, hallu = score_diagram(pl.get("text", ""), facts)
            if best is None or (f / n if n else 0) > best[0]:
                best = (f / n if n else 0, pl, f, n, miss, hallu)
        if best is None:
            tnames = [pl for pl in dev_chunks if pl.get("page_number") == facts.get("page")
                      and ((pl.get("vision_result") or {}).get("transcription"))]
            ttext = "\n".join((pl.get("vision_result") or {}).get("transcription", "")
                               for pl in tnames)
            nn = sum(1 for n in facts.get("nodes", []) if fuzzy_in(n, ttext))
            tot = max(1, len(facts.get("nodes", [])))
            report["diagrams"][key] = [{"chunk": f"p{facts.get('page')}-transcription-only",
                                        "facts": f"{nn}/{tot} nodes",
                                        "ratio": round(nn / tot, 3), "missing": ["structured summary"],
                                        "hallucinations": 0}]
        else:
            ratio, pl, f, n, miss, hallu = best
            report["diagrams"][key] = [{"chunk": f"p{pl.get('page_number')}", "facts": f"{f}/{n}",
                                        "ratio": round(ratio, 3), "missing": miss[:6],
                                        "hallucinations": hallu}]
    held_pts = qdrant_points(coll, ["pdf-heldout-a-s11", "pdf-heldout-b-s22", "pdf-heldout-c-s33"])
    for tf in sorted(HELDOUT.glob("*.truth.json")):
        docstem = tf.stem.replace(".truth", "")
        truth = json.loads(tf.read_text())
        for di, d in enumerate(truth.get("diagrams", [])):
            key = f"{tf.stem}#d{di}"
            cands = [(p.payload or {}) for p in held_pts
                     if (p.payload or {}).get("page_number") == d["page"]
                     and (p.payload or {}).get("content_type") == "diagram"
                     and (p.payload or {}).get("source_filename", "").replace(".pdf", "") == docstem]
            best = None
            for pl in cands:
                f, n, miss, hallu = score_diagram(pl.get("text", ""), d)
                if best is None or (f / n if n else 0) > best[0]:
                    best = (f / n if n else 0, pl, f, n, miss, hallu)
            if best is None:
                # no structured summary: fall back to node recall over the page's vision
                # transcriptions (content preserved, flow semantics absent) — marked as such
                tnames = [p for p in held_pts
                          if (p.payload or {}).get("page_number") == d["page"]
                          and (p.payload or {}).get("source_filename", "").replace(".pdf", "") == docstem
                          and ((p.payload or {}).get("vision_result") or {}).get("transcription")]
                ttext = "\n".join((p.payload["vision_result"] or {}).get("transcription", "")
                                   for p in tnames)
                nn = sum(1 for n in d.get("nodes", []) if fuzzy_in(n, ttext))
                tot = max(1, len(d.get("nodes", [])))
                report["diagrams"][key] = [{"chunk": f"p{d['page']}-transcription-only",
                                            "facts": f"{nn}/{tot} nodes",
                                            "ratio": round(nn / tot, 3), "missing": ["structured summary"],
                                            "hallucinations": 0}]
            else:
                ratio, pl, f, n, miss, hallu = best
                report["diagrams"][key] = [{"chunk": f"p{d['page']}", "facts": f"{f}/{n}",
                                            "ratio": round(ratio, 3), "missing": miss[:6],
                                            "hallucinations": hallu}]

    # ---- issue-1 sweep: derivation on dev + scratch (frozen held-out NEVER used here),
    # validation on frozen held-out OCR pages. better = lower CER. Threshold rule:
    # pick vision iff dis > tok OR line_cov < line.
    deriv = []
    sf = OUT / "scratch" / "sweep_regions.json"
    if sf.exists():
        for r in json.loads(sf.read_text()):
            ref = norm_text("\n".join(r["refs"]))
            co = lev(ref, norm_text(r["ocr"])) / max(1, len(ref))
            cv = lev(ref, norm_text(r["vis"])) / max(1, len(ref))
            ot, vt = norm_text(r["ocr"]).split(), norm_text(r["vis"]).split()
            oset = set(ot)
            dis = sum(1 for t in vt if t not in oset) / max(1, len(vt))
            ol = {ln for ln in norm_text(r["ocr"]).split("\n") if ln}
            vl = [ln for ln in norm_text(r["vis"]).split("\n") if ln]
            lcov = sum(1 for ln in vl if ln in ol) / max(1, len(vl))
            deriv.append({"page": f"{r['set']}:{r['key'].split('/')[-1]}", "dis": round(dis, 3),
                          "line_cov": round(lcov, 3), "better": "vision" if cv < co else "ocr",
                          "cer_ocr": round(co, 3), "cer_vision": round(cv, 3)})
    frozen_val = []
    for key in ("heldout_b_s22.truth#ocr2", "heldout_c_s33.truth#ocr1"):
        v = report["ocr"].get(key, {})
        if "vision" not in v:
            continue
        ref = norm_text("\n".join(
            next(refs for _, _, k, refs in ocr_pages if k == key)))
        oh = ocr_rows.get(key, {}).get("fast", "")
        vh = vision_rows.get(key, "")
        co = lev(ref, norm_text(oh)) / max(1, len(ref))
        cv = lev(ref, norm_text(vh)) / max(1, len(ref))
        ot, vt = norm_text(oh).split(), norm_text(vh).split()
        oset = set(ot)
        dis = sum(1 for t in vt if t not in oset) / max(1, len(vt))
        ol = {ln for ln in norm_text(oh).split("\n") if ln}
        vl = [ln for ln in norm_text(vh).split("\n") if ln]
        lcov = sum(1 for ln in vl if ln in ol) / max(1, len(vl))
        frozen_val.append({"page": key, "dis": round(dis, 3), "line_cov": round(lcov, 3),
                           "better": "vision" if cv < co else "ocr"})
    grid = {}

    def _ok(rs, tt, tl):
        n = 0
        for r in rs:
            pick = "vision" if (r["dis"] > tt or r["line_cov"] < tl) else "ocr"
            n += pick == r["better"]
        return f"{n}/{len(rs)}" if rs else "n/a"

    for tt in (0.05, 0.10, 0.15, 0.20, 0.25):
        for tl in (0.5, 0.7, 0.8, 0.9):
            grid[f"tok>{tt}/line<{tl}"] = {"deriv": _ok(deriv, tt, tl),
                                           "frozen": _ok(frozen_val, tt, tl)}
    report["sweep"] = {"deriv_regions": deriv, "frozen_regions": frozen_val, "grid": grid,
                       "chosen": "tok>0.10/line<0.80; N=12 deriv + N=2 frozen: indicative only"}

    report["not_run"]["easyocr"] = "not run: torch (~800MB+) + model downloads; over ~5min budget (D4)"
    report["not_run"]["paddleocr"] = "not run: paddlepaddle (~500MB+) + models; over ~5min budget (D4)"
    report["not_run"]["docling"] = "not run: torch + transformers + GBs of weights; over budget (D4)"
    report["not_run"]["unstructured"] = "not run: heavy hi_res deps (torch/transformers); over budget (D4)"

    (OUT / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    (OUT / "raw-tables.json").write_text(
        json.dumps({n: {f"{p}:{i}": v[:2000] for (f, p, i), v in t.items()}
                    for n, t in tools.items()}, ensure_ascii=False, indent=1))

    all_tools = ["pymupdf-pipeline", "pdfplumber-default", "pdfplumber-tuned",
                 "camelot-lattice", "camelot-lattice-tuned",
                 "camelot-stream", "camelot-stream-tuned"]
    print("\n| table | " + " | ".join(all_tools) + " |")
    print("|" + "|".join(["---"] * (len(all_tools) + 1)) + "|")
    held_list = [(t[2], t[0], t[1]) for t in heldout_tables()]
    for tid in list(GT_TABLES) + [h[0] for h in held_list]:
        row = [tid]
        for t in all_tools:
            c = report["tables"].get(tid, {}).get(t, {})
            row.append(c.get("score", "n/a") if isinstance(c, dict) else "n/a")
        print("| " + " | ".join(row) + " |")
    print("\nRotated p3 input comparison (pdfplumber-default):")
    for k in ("ch-p3-wifi-rot-raw", "ch-p3-wifi-rot-normalised"):
        v = report["tables"].get(k, {}).get("pdfplumber-default", {})
        print(f"  {k}: {v.get('score', v)}")
    print("\nFunnel (candidates / accepted / reclassified / vision-or-degraded / lattice / textalign / correct) + gate P/R:")
    for split, g in report["gate"].items():
        f = report["funnel"][split]
        print(f"  {split}: candidates={f['candidates']} accepted={f['accepted']} "
              f"reclassified={f['reclassified_text']} vision_or_degraded={f['vision_or_degraded']} "
              f"lattice={f['lattice_routed']} textalign={f['textalign_routed']} "
              f"actually_correct={f['actually_correct']} precision={g['precision']} "
              f"recall={g['recall']} confusion={g['confusion']}")
    print("\nOCR rows (page CER; macro = mean):")
    for k, v in report["ocr"].items():
        bits = " ".join(f"{m}={d['cer']}" for m, d in v.items()
                        if isinstance(d, dict) and "cer" in d)
        print(f"  {k} (ref {v['ref_chars']}): {bits}")
    print("  macro:", json.dumps(macro))
    for w in report["ocr_worst3"]:
        print(f"  worst {w['page']}: selected_cer={w['selected_cer']} via={w['via']}")
        print(f"    ref: {w['ref_head'][:220]}")
        print(f"    hyp: {w['hyp_head'][:220]}")
    print("\nDiagrams (facts found/total, hallucinations):")
    for k, v in report["diagrams"].items():
        for e in v:
            print(f"  {k} {e['chunk']}: {e['facts']} hallu={e['hallucinations']} "
                  f"missing={e['missing'][:3]}")
    print("\nSelection sweep (correct selections / regions):")
    for k, v in grid.items():
        print(f"  {k}: {v}")
    print("  regions: deriv N=12 + frozen N=2 (see results.json sweep.deriv_regions)")
    print("\nNot run:")
    for k, v in report["not_run"].items():
        print(f"  {k}: {v}")
    print("\nTimings (s):", json.dumps(report["timings"]))


if __name__ == "__main__":
    main()
