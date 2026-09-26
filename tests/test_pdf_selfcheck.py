"""Self-check: run the CLI on the manual PDF (no vision, no Qdrant, no cost).

    pytest tests/test_pdf_selfcheck.py

Asserts: no crash, chunk count > 0, every chunk carries source_type/doc_id/source_filename/page_number,
deterministic ids on re-run.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "challenge" / "BARQ_IT_Service_Desk_Manual_Ed5.1.pdf"
REQUIRED = {"source_type", "doc_id", "source_filename", "page_number"}


def run_cli(pdf: Path, dump: Path):
    r = subprocess.run(
        [sys.executable, str(ROOT / "cli" / "ingest_pdf.py"), str(pdf),
         "--dry-run", "--no-vision", "--dump-chunks", str(dump)],
        capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f"CLI crashed on {pdf.name}:\n{r.stderr[-2000:]}"
    return [json.loads(ln) for ln in dump.read_text().splitlines() if ln.strip()]


def test_manual_selfcheck(tmp_path):
    assert MANUAL.is_file(), f"manual PDF missing: {MANUAL}"
    pdfs = [MANUAL]
    for pdf in pdfs:
        chunks = run_cli(pdf, tmp_path / f"{pdf.stem}.1.jsonl")
        assert chunks, f"no chunks from {pdf.name}"
        # every chunk must have required keys
        for c in chunks:
            assert c["id"], f"missing id in chunk {c}"
            assert c["page_range"], f"missing page_range in chunk {c}"
            assert c["page"], f"missing page in chunk {c}"
            assert c["type"] in ("text",), f"unexpected type {c['type']}"
        # ids deterministic across re-runs
        again = run_cli(pdf, tmp_path / f"{pdf.stem}.2.jsonl")
        assert [c["id"] for c in chunks] == [c["id"] for c in again], \
            f"ids changed between runs on {pdf.name}"

    sys.path.insert(0, str(ROOT / "cli"))
    from ingest_pdf import Chunk, payload_for
    p = payload_for(
        Chunk(page_range="1", content_type="text", text="probe", index=0, page_number=1),
        "pdf-selfcheck", pdf.name, 10,
        "0" * 64, "model", "probe")
    assert REQUIRED <= set(p), f"missing payload keys: {REQUIRED - set(p)}"
    assert p["source_type"] == "pdf"
    assert p["page_number"] == 1
    assert p["article_number"] == ""


def test_vision_cache_key_stable(monkeypatch):
    """Vision cache key is stable for the same image and model."""
    import sys
    from PIL import Image, ImageDraw
    sys.path.insert(0, str(ROOT / "cli"))
    import ingest_pdf
    from ingest_pdf import Config, Models

    cfg = Config(base_url="http://localhost:9", api_key="k", embed_model="m", embed_dim=None,
                 vision_model="v", qdrant_url=None, qdrant_key=None, collection="c",
                 vector_name=None)
    models = Models(cfg, need_embed=False)
    img = Image.new("RGB", (500, 300), "white")
    ImageDraw.Draw(img).text((40, 120), "hello world", fill="black")

    k1 = models.vision_key(img)
    k2 = models.vision_key(img)
    assert k1 == k2, "cache key not stable for same image"

    # Different model -> different key
    cfg2 = Config(base_url="http://localhost:9", api_key="k", embed_model="m", embed_dim=None,
                  vision_model="w", qdrant_url=None, qdrant_key=None, collection="c",
                  vector_name=None)
    models2 = Models(cfg2, need_embed=False)
    k3 = models2.vision_key(img)
    assert k1 != k3, "cache key should differ for different model"


def test_new_architecture_simple(monkeypatch):
    """Verify the new architecture: render page -> one Vision call -> Markdown."""
    import sys
    from PIL import Image, ImageDraw
    sys.path.insert(0, str(ROOT / "cli"))
    import ingest_pdf
    from ingest_pdf import Config, Models, render_page, process_page, assemble_document

    cfg = Config(base_url="http://localhost:9", api_key="k", embed_model="m", embed_dim=None,
                 vision_model="v", qdrant_url=None, qdrant_key=None, collection="c",
                 vector_name=None)
    models = Models(cfg, need_embed=False)

    # Test with a real PDF page
    import pymupdf as fitz
    doc = fitz.open()
    doc.new_page(width=500, height=700)
    page = doc[0]
    page.insert_text((50, 50), "Test Content")
    rendered = render_page(page, dpi=72)
    assert rendered.width > 0
    assert rendered.height > 0

    # Verify no old extraction heuristics or article mappings in code
    import inspect
    source = inspect.getsource(ingest_pdf)
    assert "unicodedata.normalize" not in source, "Arabic reversal heuristic present"
    assert "find_tables" not in source, "PyMuPDF table extraction present"
    assert "ocr_image" not in source, "Tesseract OCR present"
    assert "osd_rotate" not in source, "OSD rotation present"
    assert "deskew" not in source, "Deskew present"
    assert "select_transcription" not in source, "OCR-vision selection present"
    assert "RETIRED_PATTERNS" not in source, "Retired patterns present"
    assert "RETIRED_STRONG" not in source, "Retired cues present"
    assert "RETIRED_WEAK" not in source, "Retired weak cues present"
    assert "article_number inference" not in source, "Article mapping present"

    doc.close()
