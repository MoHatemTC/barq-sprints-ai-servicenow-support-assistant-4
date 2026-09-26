#!/usr/bin/env python3
"""
BARQ S3.5 - Multimodal PDF Ingestion CLI.

Target Architecture:
    For each PDF page:
        1. Render the entire page as an image.
        2. Make exactly ONE Gemini Vision call for that page.
        3. Gemini returns the page content as Markdown.
        4. Store/append the returned Markdown in page order.

    After ALL pages are processed:
        5. Concatenate all page Markdown into one complete document.
        6. ONLY THEN perform chunking on the assembled document.
        7. Embed the resulting chunks and index them into Qdrant.

No endpoint, key, or model name is hardcoded: everything comes from the environment / .env.
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
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pymupdf as fitz  # PyMuPDF
from PIL import Image

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
    base_url: str | None = None
    api_key: str | None = None
    embed_model: str | None = None
    embed_dim: int | None = None
    vision_model: str | None = None
    qdrant_url: str | None = None
    qdrant_key: str | None = None
    collection: str | None = None
    vector_name: str | None = None
    chunk_size: int = 900
    overlap: int = 120
    dpi: int = 200
    cache_file: Path | None = None

    @classmethod
    def from_env(cls, a: argparse.Namespace) -> "Config":
        dim = env("EMBEDDING_DIM", "EMBED_DIM")
        cache_path = getattr(a, "cache_file", None)
        if cache_path is None:
            cache_path = Path("out/.vision_cache.json")
        return cls(
            base_url=env("LITELLM_BASE_URL", "LITELLM_API_BASE", "OPENAI_BASE_URL", "OPENAI_API_BASE"),
            api_key=env("LITELLM_API_KEY", "LITELLM_MASTER_KEY", "OPENAI_API_KEY"),
            embed_model=env("EMBEDDING_MODEL", "EMBED_MODEL"),
            embed_dim=int(dim) if dim else None,
            vision_model=None if a.no_vision else env("VISION_MODEL", "LITELLM_VISION_MODEL"),
            qdrant_url=env("QDRANT_URL"),
            qdrant_key=env("QDRANT_API_KEY"),
            collection=a.collection or env("QDRANT_COLLECTION", "QDRANT_COLLECTION_NAME"),
            vector_name=env("QDRANT_VECTOR_NAME"),
            chunk_size=a.chunk_size,
            overlap=a.overlap,
            dpi=a.dpi,
            cache_file=Path(cache_path) if cache_path else None,
        )


@dataclass
class PageResult:
    """Result of processing one PDF page."""
    document_id: str
    source_filename: str
    page_number: int
    total_pages: int
    page_markdown: str
    image_hash: str | None = None
    vision_call_made: bool = False

    @property
    def markdown(self) -> str:
        return self.page_markdown


@dataclass
class Chunk:
    """A chunk of the assembled document."""
    page_range: str  # e.g. "1-3" or "5"
    content_type: str  # "text"
    text: str
    meta: dict = field(default_factory=dict)
    index: int = 0
    page_number: int = 1


# --------------------------------------------------------------------------- VISION
class Models:
    def __init__(self, cfg: Config, need_embed: bool):
        self.cfg = cfg
        self.client = None
        self.vision_calls = 0
        if cfg.base_url and cfg.api_key:
            from openai import OpenAI
            self.client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key, timeout=120)
        elif need_embed:
            raise SystemExit("Missing LITELLM_BASE_URL / LITELLM_API_KEY (or OPENAI_*) in environment/.env")

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs: list[list[float]] = []
        for i in range(0, len(texts), 32):
            kw = {"dimensions": self.cfg.embed_dim} if self.cfg.embed_dim else {}
            for attempt in range(3):
                try:
                    r = self.client.embeddings.create(
                        model=self.cfg.embed_model, input=texts[i:i + 32], **kw)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    log.warning("embed retry: %s", type(e).__name__)
                    time.sleep(2 ** attempt)
            vecs += [d.embedding for d in sorted(r.data, key=lambda d: d.index)]
        return vecs

    def vision_key(self, img: Image.Image) -> str:
        """Stable cache key: sha256(png bytes + model + prompt version)."""
        im = img.copy()
        im.thumbnail((2000, 2000))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "PNG")
        raw = buf.getvalue()
        return hashlib.sha256(
            raw + b"\x00" + (self.cfg.vision_model or "").encode()
            + b"\x00" + str(VISION_PROMPT_VERSION).encode()
        ).hexdigest()

    def vision(self, img: Image.Image) -> str:
        """Send ONE page image to Gemini Vision. Returns the page Markdown string."""
        if not (self.client and self.cfg.vision_model):
            return ""
        uri = "data:image/png;base64," + base64.b64encode(self._png_bytes(img)).decode()
        self.vision_calls += 1
        for attempt in range(3):
            try:
                r = self.client.chat.completions.create(
                    model=self.cfg.vision_model,
                    temperature=0,
                    max_tokens=4096,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": uri}}
                    ]}],
                )
                break
            except Exception as e:
                if attempt == 2:
                    log.warning("vision call failed (%s): %s", type(e).__name__, str(e)[:120])
                    raise
                time.sleep(2 ** attempt)
        raw = r.choices[0].message.content or ""
        return _strip_code_fences(raw).strip()

    def _png_bytes(self, img: Image.Image) -> bytes:
        buf = io.BytesIO()
        im = img.copy()
        im.thumbnail((2000, 2000))
        im.convert("RGB").save(buf, "PNG")
        return buf.getvalue()


def _strip_code_fences(s: str) -> str:
    """Remove outer Markdown code-fence wrappers if the model returned them."""
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*\n?", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


VISION_PROMPT = """You are an expert document understanding model. Transcribe the ENTIRE page image into clean, structured Markdown.

Requirements:
1. Output Format:
   - Return ONLY the page Markdown. Do NOT include JSON wrappers, metadata explanations, or enclosing code fences around the full response.
2. Text & Multilingual:
   - Transcribe all text verbatim in its natural visual reading order.
   - Preserve Arabic and all other non-Latin scripts in their correct, natural logical character order. NEVER reverse Arabic characters, words, or lines.
   - Keep mixed Arabic/English text, numbers, codes, KB identifiers (e.g., KB0010), ticket IDs (e.g., INC-4410), and URLs character-wise and semantically intact.
3. Tables:
   - Transcribe all tables as standard Markdown tables with headers and rows.
   - Faithfully preserve column hierarchy, row structure, cell contents, and relationships.
   - Do not convert tables to arbitrary key/value lists.
4. Diagrams & Flows:
   - For diagrams, flowcharts, swimlanes, network diagrams, and decision trees, transcribe all visible labels.
   - Describe the flows and connections concisely in Markdown prose using textual relationships like "A -> B" and "If <condition> -> <A>, else -> <B>".
   - Never invent or assume labels or connections not visibly shown on the page.
5. Rotated Content:
   - Read and interpret the page visually as rendered, including rotated, landscape, or upside-down sections. Transcribe the content upright in its natural reading flow.
6. Grounding:
   - Transcribe ONLY what is explicitly visible. Never fabricate article numbers, IDs, or content."""

# Bump whenever VISION_PROMPT changes — invalidates the vision cache.
VISION_PROMPT_VERSION = 1


# --------------------------------------------------------------------------- caching
def load_disk_cache(path: Path | None) -> dict[str, str]:
    if not path or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: str(v) for k, v in data.items()}
    except Exception as e:
        log.warning("failed to load cache file %s: %s", path, e)
    return {}


def save_disk_cache(path: Path | None, cache: dict[str, str]) -> None:
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("failed to save cache file %s: %s", path, e)


# --------------------------------------------------------------------------- page processing
def render_page(page: fitz.Page, dpi: int) -> Image.Image:
    """Render a single PDF page as an RGB PIL Image."""
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def process_page(
    page: fitz.Page,
    page_number: int,
    total_pages: int,
    doc_id: str,
    fname: str,
    cfg: Config,
    models: Models,
    vcache: dict[str, str] | None,
) -> PageResult:
    """Process one PDF page: render -> one Vision call -> Markdown.

    Invariant: one uncached page = exactly one Vision request.
    """
    if cfg.vision_model is None:
        # Vision skipped (--no-vision)
        return PageResult(
            document_id=doc_id,
            source_filename=fname,
            page_number=page_number,
            total_pages=total_pages,
            page_markdown=f"# Page {page_number}\n\n[Content omitted: --no-vision]",
            image_hash=None,
            vision_call_made=False,
        )

    img = render_page(page, cfg.dpi)
    vkey = models.vision_key(img)

    # Check cache first
    if vcache and vkey in vcache:
        log.info("p%d vision cache hit", page_number)
        return PageResult(
            document_id=doc_id,
            source_filename=fname,
            page_number=page_number,
            total_pages=total_pages,
            page_markdown=vcache[vkey],
            image_hash=vkey,
            vision_call_made=False,
        )

    # One Vision call per page
    markdown = models.vision(img)
    if vcache is not None:
        vcache[vkey] = markdown

    return PageResult(
        document_id=doc_id,
        source_filename=fname,
        page_number=page_number,
        total_pages=total_pages,
        page_markdown=markdown,
        image_hash=vkey,
        vision_call_made=True,
    )


def assemble_document(page_results: list[PageResult]) -> str:
    """Concatenate all page Markdown in page order with page separators."""
    parts = []
    for pr in page_results:
        parts.append(f"<!-- PAGE {pr.page_number} -->\n{pr.page_markdown.strip()}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- chunking
def split_text(text: str, size: int, overlap: int) -> list[str]:
    """Split text into chunks with overlap, respecting paragraph/sentence boundaries."""
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    # Split on double newlines (paragraphs/markdown blocks)
    blocks = [b.strip() for b in re.split(r"\n{2,}", text) if b.strip()]
    if not blocks:
        blocks = [text]

    pieces: list[str] = []
    for block in blocks:
        if len(block) <= size:
            pieces.append(block)
        else:
            # Split long blocks into sentences
            sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", block) if s.strip()]
            for s in sents:
                if len(s) > size:
                    for i in range(0, len(s), size - overlap):
                        pieces.append(s[i:i + size])
                else:
                    pieces.append(s)

    # Assemble into chunks of ~size
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        if cur and len(cur) + len(p) + 1 > size:
            chunks.append(cur)
            tail = cur[-overlap:] if overlap else ""
            tail = tail[tail.find(" ") + 1:] if " " in tail else tail
            cur = (tail + "\n\n" + p).strip()
        else:
            cur = (cur + "\n\n" + p).strip() if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def build_chunks(
    document: str,
    doc_id: str,
    fname: str,
    total_pages: int,
    cfg: Config,
) -> list[Chunk]:
    """Chunk the assembled document and attach page provenance."""
    # Find page spans in the assembled document
    page_markers = list(re.finditer(r"<!-- PAGE (\d+) -->", document))
    page_spans: list[tuple[int, int, int]] = []
    for i, m in enumerate(page_markers):
        pno = int(m.group(1))
        start = m.start()
        end = page_markers[i + 1].start() if i + 1 < len(page_markers) else len(document)
        page_spans.append((pno, start, end))

    # Split document text into chunks
    raw_chunks = split_text(document, cfg.chunk_size, cfg.overlap)
    result: list[Chunk] = []

    # Map each chunk to its character position and spanned pages
    search_pos = 0
    for i, text in enumerate(raw_chunks):
        # Locate chunk in document
        # Clean text without HTML comments for search
        probe = text[:min(80, len(text))].strip()
        pos = document.find(probe, search_pos)
        if pos < 0:
            pos = document.find(probe)
        if pos >= 0:
            search_pos = pos

        chunk_end = (pos + len(text)) if pos >= 0 else search_pos

        # Identify all pages overlapping [pos, chunk_end]
        overlapping_pages: list[int] = []
        for pno, p_start, p_end in page_spans:
            if pos < p_end and chunk_end > p_start:
                overlapping_pages.append(pno)

        if not overlapping_pages:
            overlapping_pages = [1]

        start_p = min(overlapping_pages)
        end_p = max(overlapping_pages)
        page_range = str(start_p) if start_p == end_p else f"{start_p}-{end_p}"

        # Clean PAGE markers from chunk text for clean indexing
        clean_text = re.sub(r"<!-- PAGE \d+ -->\s*", "", text).strip()
        if not clean_text:
            clean_text = text

        result.append(Chunk(
            page_range=page_range,
            content_type="text",
            text=clean_text,
            index=i,
            page_number=start_p,
        ))

    return result


def _infer_page_range(document: str, chunk_text: str) -> str:
    """Helper for inferring page range from document."""
    page_markers = list(re.finditer(r"<!-- PAGE (\d+) -->", document))
    chunk_clean = re.sub(r"<!--.*?-->", "", chunk_text).strip()
    if not page_markers or not chunk_clean:
        return "1"

    pos = document.find(chunk_clean[:60])
    if pos < 0:
        return str(page_markers[0].group(1))

    start_page = None
    end_page = None
    chunk_end = pos + len(chunk_clean)
    for m in page_markers:
        page_num = int(m.group(1))
        if m.start() <= pos:
            start_page = page_num
        if m.start() < chunk_end:
            end_page = page_num

    if start_page is None or end_page is None:
        return str(page_markers[0].group(1))
    if start_page == end_page:
        return str(start_page)
    return f"{start_page}-{end_page}"


# --------------------------------------------------------------------------- Qdrant
def payload_for(
    chunk: Chunk,
    doc_id: str,
    fname: str,
    total_pages: int,
    file_sha: str,
    model: str,
    embed_text: str,
) -> dict:
    """Build the Qdrant payload for one chunk."""
    return {
        "source_type": "pdf",
        "doc_id": doc_id,
        "source_filename": fname,
        "page_number": chunk.page_number,
        "page_range": chunk.page_range,
        "total_pages": total_pages,
        "chunk_index": chunk.index,
        "content_type": chunk.content_type,
        "title": f"{fname} - pages {chunk.page_range}",
        "text": chunk.text,
        "number": "",
        "article_number": "",
        "short_description": "",
        "category": "PDF",
        "heading_path": [],
        "file_sha256": file_sha,
        "embedding_model": model,
        "content_hash": hashlib.sha256((model + "\x00" + embed_text).encode()).hexdigest(),
    }


def point_id(doc_id: str, page: int, idx: int) -> str:
    return str(uuid.uuid5(POINT_NS, f"{doc_id}|{page}|{idx}"))


def connect_qdrant(cfg: Config):
    from qdrant_client import QdrantClient
    if not cfg.qdrant_url or not cfg.collection:
        raise SystemExit("Missing QDRANT_URL / QDRANT_COLLECTION")
    return QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_key, timeout=60)


def ensure_collection(client, cfg: Config, dim: int, create: bool) -> str | None:
    """Verify the shared collection has the same dimensionality. Returns the vector name."""
    from qdrant_client.models import Distance, VectorParams
    try:
        info = client.get_collection(cfg.collection)
    except Exception:
        if not create:
            raise SystemExit(f"Collection '{cfg.collection}' not found (use --create-collection)")
        vc = VectorParams(size=dim, distance=Distance.COSINE)
        if cfg.vector_name:
            vc = {cfg.vector_name: vc}
        client.create_collection(cfg.collection, vectors_config=vc)
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
        raise SystemExit(
            f"Dimension mismatch: collection={size}, embedding model={dim}. "
            f"Use the same EMBEDDING_MODEL/EMBEDDING_DIM as the KB indexer."
        )
    return name


def ensure_indexes(client, collection: str):
    from qdrant_client.models import PayloadSchemaType
    for f in ("source_type", "doc_id", "content_type", "page_number"):
        try:
            client.create_payload_index(collection, f, PayloadSchemaType.KEYWORD)
        except Exception:
            pass


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
    ap.add_argument("--no-vision", action="store_true", help="skip the vision LLM (no page content)")
    ap.add_argument("--no-prune", action="store_true", help="do not delete stale points of this doc_id")
    ap.add_argument("--chunk-size", type=int, default=900)
    ap.add_argument("--overlap", type=int, default=120)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--cache-file", type=Path, help="path to vision cache json file (default: out/.vision_cache.json)")
    ap.add_argument("--dump-chunks", type=Path, help="write chunks as JSONL (evidence / debugging)")
    ap.add_argument("--json-summary", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        from dotenv import load_dotenv
        load_dotenv(a.env_file)
    except ImportError:
        pass
    if not a.pdf.is_file():
        raise SystemExit(f"File not found: {a.pdf}")

    cfg = Config.from_env(a)
    if not a.dry_run and not cfg.embed_model:
        raise SystemExit("Missing EMBEDDING_MODEL")
    if not a.no_vision and not cfg.vision_model:
        raise SystemExit("Missing VISION_MODEL (set --no-vision to skip)")

    t0 = time.time()
    raw = a.pdf.read_bytes()
    file_sha = hashlib.sha256(raw).hexdigest()
    doc_id = a.doc_id or "pdf-" + re.sub(r"[^a-z0-9]+", "-", a.pdf.stem.lower()).strip("-")
    fname = a.pdf.name
    log.info(
        "doc_id=%s sha256=%s endpoint=%s",
        doc_id,
        file_sha[:12],
        urlparse(cfg.base_url).netloc if cfg.base_url else "-",
    )

    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        if doc.is_encrypted:
            raise ValueError("document is password-protected")
        total_pages = doc.page_count
    except SystemExit:
        raise
    except Exception as e:
        raise SystemExit(f"Cannot open PDF (corrupt or password-protected): {type(e).__name__}: {str(e)[:160]}")

    pages = parse_pages(a.pages, total_pages)
    models = Models(cfg, need_embed=not a.dry_run)

    # Load cache from disk
    vcache: dict[str, str] = load_disk_cache(cfg.cache_file)
    log.info("loaded %d cached vision entries from %s", len(vcache), cfg.cache_file)

    # Process each page: render -> one Vision call -> Markdown
    page_results: list[PageResult] = []
    total_vision_calls = 0
    for pno in pages:
        page = doc[pno - 1]
        result = process_page(page, pno, total_pages, doc_id, fname, cfg, models, vcache)
        page_results.append(result)
        if result.vision_call_made:
            total_vision_calls += 1

    log.info("processed %d pages, %d vision calls made", len(pages), total_vision_calls)

    # Save updated cache to disk
    if total_vision_calls > 0:
        save_disk_cache(cfg.cache_file, vcache)

    # Step 5: Concatenate all page Markdown into one complete document
    document = assemble_document(page_results)
    log.info("assembled document: %d characters", len(document))

    # Step 6: ONLY THEN perform chunking on the assembled document
    chunks = build_chunks(document, doc_id, fname, total_pages, cfg)
    log.info("extracted %d chunks from %d pages", len(chunks), len(pages))

    # Dump chunks if requested
    if a.dump_chunks:
        a.dump_chunks.parent.mkdir(parents=True, exist_ok=True)
        with a.dump_chunks.open("w", encoding="utf-8") as f:
            for c in chunks:
                f.write(json.dumps({
                    "id": point_id(doc_id, c.page_number, c.index),
                    "page": c.page_number,
                    "page_range": c.page_range,
                    "index": c.index,
                    "type": c.content_type,
                    "text": c.text,
                    "meta": c.meta,
                }, ensure_ascii=False) + "\n")

    # Step 7: Embed chunks and index them into Qdrant
    upserted = skipped = pruned = 0
    client = None
    if not a.dry_run and chunks:
        client = connect_qdrant(cfg)
        ids = [point_id(doc_id, c.page_number, c.index) for c in chunks]
        assert len(set(ids)) == len(ids), "point id collision"

        def _embed_text(c: Chunk) -> str:
            return f"[{fname} | pages {c.page_range}]\n{c.text}"

        payloads = [
            payload_for(c, doc_id, fname, total_pages, file_sha, cfg.embed_model, _embed_text(c))
            for c in chunks
        ]

        existing = {}
        try:
            for r in client.retrieve(cfg.collection, ids, with_payload=["content_hash"], with_vectors=False):
                existing[str(r.id)] = (r.payload or {}).get("content_hash")
        except Exception:
            pass  # collection may not exist yet

        todo = [i for i, (pid, p) in enumerate(zip(ids, payloads)) if existing.get(pid) != p["content_hash"]]
        skipped = len(chunks) - len(todo)

        if todo:
            probe_vec = models.embed([_embed_text(chunks[todo[0]])])[0]
            dim = len(probe_vec)
            vname = ensure_collection(client, cfg, dim, a.create_collection)
            ensure_indexes(client, cfg.collection)
            vecs = (
                [probe_vec]
                + models.embed([_embed_text(chunks[i]) for i in todo[1:]])
                if len(todo) > 1 else [probe_vec]
            )
            now = datetime.now(timezone.utc).isoformat()
            from qdrant_client.models import PointStruct
            pts = []
            for i, v in zip(todo, vecs):
                payloads[i]["markdown_text"] = chunks[i].text
                payloads[i]["ingested_at"] = now
                pts.append(PointStruct(id=ids[i], vector={vname: v} if vname else v, payload=payloads[i]))

            for k in range(0, len(pts), 64):
                client.upsert(cfg.collection, points=pts[k:k + 64], wait=True)
            upserted = len(pts)

        if not a.no_prune and not a.pages:
            from qdrant_client.models import FieldCondition, Filter, MatchValue, PointIdsList
            flt = Filter(must=[
                FieldCondition(key="source_type", match=MatchValue(value="pdf")),
                FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
            ])
            stale = []
            off = None
            while True:
                pts_, off = client.scroll(
                    cfg.collection,
                    scroll_filter=flt,
                    limit=256,
                    offset=off,
                    with_payload=False,
                    with_vectors=False,
                )
                stale += [str(p.id) for p in pts_ if str(p.id) not in set(ids)]
                if off is None:
                    break
            if stale:
                client.delete(cfg.collection, points_selector=PointIdsList(points=stale), wait=True)
                pruned = len(stale)

    by_type = Counter(c.content_type for c in chunks)
    summary = {
        "file": fname,
        "doc_id": doc_id,
        "sha256": file_sha,
        "pages": len(pages),
        "chunks": len(chunks),
        "chunks_by_type": dict(by_type),
        "vision_calls": models.vision_calls,
        "vision_calls_made": total_vision_calls,
        "upserted": upserted,
        "unchanged_skipped": skipped,
        "pruned_stale": pruned,
        "dry_run": a.dry_run,
        "seconds": round(time.time() - t0, 1),
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
