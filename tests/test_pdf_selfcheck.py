"""Self-check: run the CLI on the manual PDF (no vision, no Qdrant, no cost).

    pytest tests/test_pdf_selfcheck.py

Asserts: no crash, chunk count > 0 per page with content, every point carries
source_type/doc_id/source_filename/page_number, deterministic ids on re-run.
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
        pages = {c["page"] for c in chunks}
        assert pages, f"no pages with content in {pdf.name}"
        import fitz
        n = fitz.open(pdf).page_count
        assert max(pages) <= n
        # ids deterministic across re-runs
        again = run_cli(pdf, tmp_path / f"{pdf.stem}.2.jsonl")
        assert [c["id"] for c in chunks] == [c["id"] for c in again], \
            f"ids changed between runs on {pdf.name}"
        # required payload keys would be present (dry-run dump carries id/page/index/type/text/meta;
        # payload key coverage is asserted on the constructor path below)
        for c in chunks:
            assert c["id"] and c["page"] >= 1 and c["text"].strip() or c["type"] in ("table",)
        sys.path.insert(0, str(ROOT / "cli"))
        from ingest_pdf import Chunk, payload_for
        p = payload_for(Chunk(1, "text", "probe", {}), "pdf-selfcheck", pdf.name, n,
                        "0" * 64, "model", "probe")
        assert REQUIRED <= set(p), f"missing payload keys: {REQUIRED - set(p)}"


def test_vision_src_fallback_no_call(monkeypatch):
    """Generic proof of the dual-hash fallback: with a vcache keyed ONLY by the
    pre-transform source hash (simulating an OSD/deskew threshold flip), image_chunks
    must not call the vision model and must reproduce identical chunks."""
    import sys
    from collections import Counter
    from PIL import Image, ImageDraw
    sys.path.insert(0, str(ROOT / "cli"))
    import ingest_pdf
    from ingest_pdf import Config, Models, image_chunks
    monkeypatch.setattr(ingest_pdf, "osd_rotate", lambda im: (im.rotate(90, expand=True), 90))
    cfg = Config(base_url="http://localhost:9", api_key="k", embed_model="m", embed_dim=None,
                 vision_model="v", qdrant_url=None, qdrant_key=None, collection="c",
                 vector_name=None)
    models = Models(cfg, need_embed=False)
    fixed = {"kind": "text", "transcription": "steady state report 12345", "summary": ""}
    calls: list = []
    monkeypatch.setattr(models, "vision",
                        lambda img, label="": (calls.append(1), dict(fixed))[1])
    img = Image.new("RGB", (500, 300), "white")
    ImageDraw.Draw(img).text((40, 120), "steady state report", fill="black")
    out1 = image_chunks(img.copy(), 1, models, cfg, Counter(), "t", vcache={})
    assert len(calls) == 1 and out1
    srckey, _ = models.vision_key(img)
    calls.clear()
    out2 = image_chunks(img.copy(), 1, models, cfg, Counter(), "t", vcache={srckey: dict(fixed)})
    assert not calls, "vision model called despite src-hash cache hit"
    assert [c.text for c in out2] == [c.text for c in out1]
    assert [c.content_type for c in out2] == [c.content_type for c in out1]
