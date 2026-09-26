#!/usr/bin/env python3
"""Generate HELD-OUT hostile PDFs for generality testing (S3.5 review).

These must differ from the dev fixture (cli/make_challenge_pdf.py) in STRUCTURE, not
just text: different page sizes, fonts, colours, row/column counts, header depths,
span placements, rotations, languages-as-layers, scan qualities and diagram styles.

    python cli/make_heldout_pdfs.py [--seed 11,22,33] [--out-dir challenge-heldout]

Each PDF ships with a ground-truth JSON written by the GENERATOR (never by tool output):
tables as header->value grids, OCR reference strings, diagram fact lists
(nodes, decision nodes with branch labels, directed edges with labels).

Frozen final-report seeds: 11, 22, 33. Iterate with other seeds; regenerate to avoid
tuning on held-out truth.
"""
import argparse
import io
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_challenge_pdf import ar, font  # noqa: E402  (RTL shaping helpers + DejaVu paths)

from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import LETTER, A5, landscape  # noqa: E402
from reportlab.lib.utils import ImageReader  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402
from reportlab.platypus import Table, TableStyle  # noqa: E402


def _font(name: str, size: int, bold: bool = False):
    base = {"helvetica": "Helvetica", "times": "Times-Roman", "courier": "Courier"}[name]
    if bold:
        base = {"helvetica": "Helvetica-Bold", "times": "Times-Bold", "courier": "Courier-Bold"}[name]
    return (base, size)


def _noise(img: Image.Image, n: int = 4000) -> Image.Image:
    px = img.load()
    for _ in range(n):
        px[random.randrange(img.width), random.randrange(img.height)] = (140, 140, 140)
    return img


def _scan_image(lines, size=(1300, 800), dpi_note=120, skew_deg: float = 0.0,
                dark: bool = False) -> Image.Image:
    """Image-only 'scan': text drawn into pixels, optional skew + noise + low fidelity."""
    bg, fg = ("#1a2433", "#e8eef7") if dark else ("white", "black")
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    y = 40
    for text, is_ar, sz in lines:
        f = font(sz, True)
        s = ar(text) if is_ar else text
        kw = {"direction": "rtl", "language": "ar"} if (is_ar and _RAQM()) else {}
        w = d.textlength(s, font=f, **kw)
        x = size[0] - 50 - w if is_ar else 50
        d.text((x, y), s, font=f, fill=fg, **kw)
        y += int(sz * 1.9)
    img = _noise(img)
    if skew_deg:
        img = img.rotate(skew_deg, fillcolor=bg, resample=Image.BICUBIC)
    # low-resolution feel: downscale then upscale
    small = img.resize((size[0] * dpi_note // 200, size[1] * dpi_note // 200), Image.BILINEAR)
    return small.resize(size, Image.BILINEAR).filter(ImageFilter.GaussianBlur(0.4))


def _RAQM():
    from PIL import features
    return features.check("raqm")


def _flow_diagram(style: str, seed: int, dark: bool = False) -> tuple[Image.Image, dict]:
    """Draw one of several diagram styles; return (image, facts). Facts use the same
    generic shape everywhere: nodes, decisions with branches, labelled edges."""
    rnd = random.Random(seed)
    bg, fg, box, hi = ("#141b26", "#dfe7f2", "#24344d", "#f2c94c") if dark else ("white", "black", "#e8f0fe", "#a00000")
    img = Image.new("RGB", (1400, 560), bg)
    d = ImageDraw.Draw(img)
    f = font(26, True)

    def _box(xy, text, fill=None):
        d.rounded_rectangle(xy, 14, fill=fill or box, outline=fg, width=3)
        w = d.textlength(text, font=f)
        d.text(((xy[0] + xy[2]) / 2 - w / 2, (xy[1] + xy[3]) / 2 - 15), text, font=f, fill=fg if not dark else "#ffffff")

    def _arrow(a, b, label=None):
        d.line([a, b], fill=fg, width=4)
        import math
        ang = math.atan2(b[1] - a[1], b[0] - a[0])
        for da in (2.6, -2.6):
            d.line([b, (b[0] + 18 * math.cos(ang + da), b[1] + 18 * math.sin(ang + da))], fill=fg, width=4)
        if label:
            d.text(((a[0] + b[0]) / 2 + 8, (a[1] + b[1]) / 2 - 30), label, font=font(22), fill=hi)

    if style == "network":
        labels = ["Sensor", "Gateway", "Auth check", "Ingest svc", "Hot store", "Cold archive"]
        pos = [(40, 220, 240, 300), (330, 220, 560, 300), (650, 80, 900, 160),
               (650, 330, 900, 410), (990, 330, 1240, 410), (990, 80, 1360, 160)]
        for xy, t in zip(pos, labels):
            _box(xy, t)
        edges = [("Sensor", "Gateway", "mqtt"), ("Gateway", "Auth check", "token"),
                 ("Gateway", "Ingest svc", "https"), ("Ingest svc", "Hot store", "write"),
                 ("Hot store", "Cold archive", "rollup")]
        _arrow((240, 260), (330, 260), "mqtt")
        _arrow((560, 240), (650, 130), "token")
        _arrow((560, 280), (650, 360), "https")
        _arrow((900, 370), (990, 370), "write")
        _arrow((1115, 330), (1175, 160), "rollup")
        facts = {"nodes": labels, "decisions": [], "edges": [[a, b, l] for a, b, l in edges]}
    elif style == "swimlane":
        lanes = ["Intake", "Triage", "Fix"]
        steps = [["Ticket filed", "Auto-tag"], ["Reproduce", "Bisect"], ["Patch", "Verify"]]
        facts = {"nodes": [], "decisions": [], "edges": []}
        for li, lane in enumerate(lanes):
            y0 = 60 + li * 160
            d.text((20, y0 + 5), lane, font=font(24, True), fill=fg)
            x = 220
            prev = None
            for s in steps[li]:
                _box((x, y0, x + 200, y0 + 70), s)
                facts["nodes"].append(s)
                if prev:
                    _arrow((x - 140 + 200, y0 + 35), (x, y0 + 35))
                    facts["edges"].append([prev, s, ""])
                prev = s
                x += 260
    elif style == "orgchart":
        facts = {"nodes": ["CTO", "Platform lead", "Data lead", "SRE on-call", "Backend dev", "Analyst"],
                 "decisions": [],
                 "edges": [["CTO", "Platform lead", ""], ["CTO", "Data lead", ""],
                           ["Platform lead", "SRE on-call", ""], ["Platform lead", "Backend dev", ""],
                           ["Data lead", "Analyst", ""]]}
        _box((600, 40, 800, 110), "CTO")
        _box((330, 200, 560, 270), "Platform lead")
        _box((840, 200, 1070, 270), "Data lead")
        _box((180, 380, 420, 450), "SRE on-call")
        _box((480, 380, 700, 450), "Backend dev")
        _box((840, 380, 1070, 450), "Analyst")
        for a, b in [((700, 110), (445, 200)), ((700, 110), (955, 200)), ((445, 270), (300, 380)),
                     ((445, 270), (590, 380)), ((955, 270), (955, 380))]:
            _arrow(a, b)
    elif style == "barchart":
        d.text((30, 20), "Queue depth by week", font=font(30, True), fill=fg)
        d.line([(80, 80), (80, 480)], fill=fg, width=3)
        d.line([(80, 480), (1330, 480)], fill=fg, width=3)
        vals = [rnd.randint(2, 9) for _ in range(6)]
        facts = {"nodes": [f"W{i + 1}" for i in range(6)] + [str(v) for v in vals],
                 "decisions": [], "edges": []}
        for i, v in enumerate(vals):
            x0 = 140 + i * 190
            h = v * 40
            d.rectangle([(x0, 480 - h), (x0 + 120, 480)], fill="#2f80ed" if not dark else "#5aa2ff", outline=fg, width=2)
            d.text((x0 + 20, 490), f"W{i + 1}", font=font(24, True), fill=fg)
            d.text((x0 + 40, 480 - h - 40), str(v), font=font(24, True), fill=fg)
    else:  # decision-tree flowchart
        facts = {"nodes": ["Start", "Load > 80%?", "Shed low-pri", "Page owner", "Drain pool", "Done"],
                 "decisions": [{"node": "Load > 80%?", "branches": {"yes": "Shed low-pri", "no": "Page owner"}}],
                 "edges": [["Start", "Load > 80%?", ""], ["Load > 80%?", "Shed low-pri", "yes"],
                           ["Load > 80%?", "Page owner", "no"], ["Shed low-pri", "Drain pool", ""],
                           ["Page owner", "Drain pool", ""], ["Drain pool", "Done", ""]]}
        _box((80, 220, 280, 300), "Start")
        x0, y0, x1, y1 = (420, 180, 720, 340)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        d.polygon([(cx, y0), (x1, cy), (cx, y1), (x0, cy)], fill="#fff4d6" if not dark else "#4d3d17", outline=fg)
        d.text((cx - 110, cy - 15), "Load > 80%?", font=f, fill=fg if not dark else "#ffffff")
        _box((850, 90, 1120, 170), "Shed low-pri")
        _box((850, 350, 1120, 430), "Page owner")
        _box((150, 430, 420, 510), "Drain pool")
        _box((1150, 220, 1330, 300), "Done")
        _arrow((280, 260), (420, 260))
        _arrow((720, 230), (850, 130), "yes")
        _arrow((720, 290), (850, 390), "no")
        _arrow((285, 430), (150, 470))
        _arrow((985, 430), (985, 470) if False else (700, 470))
        _arrow((420, 470), (1150, 260))
    return img, {"style": style, "dark": dark, **facts}


def _styled_table(rnd: random.Random, kind: str):
    """Return (data, style_cmds, truth_headers, truth_rows, note). Randomised dimensions."""
    if kind == "deep-header":
        ncols = rnd.randint(3, 5)
        top = (["Group"] * (ncols - 1) + ["Lead"])[:ncols]
        subs = [f"M{i + 1}" for i in range(ncols - 1)] + ["Name"]
        nrows = rnd.randint(3, 6)
        truth_rows = [[f"svc-{r}", *[str(rnd.randint(1, 99)) for _ in range(ncols - 2)], f"owner-{r}"]
                      for r in range(nrows)]
        data = [["Service", *top], ["", *subs], *truth_rows]
        truth_headers = ["Service"] + [f"{a} > {b}" for a, b in zip(top, subs)]
        style = [("GRID", (0, 0), (-1, -1), 0.6, colors.black),
                 ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor("#dbe7f5")),
                 ("FONTSIZE", (0, 0), (-1, -1), 9)]
        if ncols > 2:
            style.append(("SPAN", (1, 0), (ncols - 1, 0)))
        return data, style, truth_headers, truth_rows, "header-depth-2 colspan"
    if kind == "rowspan":
        nrows = rnd.randint(4, 6)
        groups = ["net", "db"] if nrows <= 5 else ["net", "db", "app"]
        per = nrows // len(groups)
        data = [["Area", "Check", "Cmd"]]
        truth_rows = []
        r = 0
        for g in groups:
            for k in range(per):
                data.append([g if k == 0 else "", f"check-{r}", f"run-{r}"])
                truth_rows.append([g, f"check-{r}", f"run-{r}"])
                r += 1
        style = [("GRID", (0, 0), (-1, -1), 0.6, colors.black),
                 ("SPAN", (0, 1), (0, per)), ("SPAN", (0, per + 1), (0, 2 * per))]
        if len(groups) > 2:
            style.append(("SPAN", (0, 2 * per + 1), (0, 3 * per)))
        return data, style, ["Area", "Check", "Cmd"], truth_rows, "rowspan first column"
    if kind == "nested":
        inner = Table([["K", "V"], ["retries", "3"], ["timeout", "30s"]], colWidths=[60, 60])
        inner.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                   ("FONTSIZE", (0, 0), (-1, -1), 7)]))
        data = [["Host", "Net", "Limits"], ["web-1", "10.0.0.1", inner], ["web-2", "10.0.0.2", "cpu: 2"]]
        return data, [("GRID", (0, 0), (-1, -1), 0.7, colors.black)], ["Host", "Net", "Limits"], \
            [["web-1", "10.0.0.1", "K / V; retries / 3; timeout / 30s"], ["web-2", "10.0.0.2", "cpu: 2"]], \
            "table nested in a cell"
    # borderless: styled fills, no rules
    ncols, nrows = rnd.randint(3, 4), rnd.randint(3, 5)
    data = [[f"H{j + 1}" for j in range(ncols)]]
    truth_rows = []
    for r in range(nrows):
        row = [f"v{r}-{j}" for j in range(ncols)]
        data.append(row)
        truth_rows.append(row)
    style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef3fa")),
             ("BACKGROUND", (0, 1), (-1, -1), colors.whitesmoke),
             ("FONTSIZE", (0, 0), (-1, -1), 9)]
    return data, style, [f"H{j + 1}" for j in range(ncols)], truth_rows, "borderless fills"


def build_pdf_a(out: Path, seed: int) -> dict:
    rnd = random.Random(seed)
    truth = {"seed": seed, "tables": [], "ocr": [], "diagrams": []}
    c = canvas.Canvas(str(out), pagesize=LETTER)
    W, H = LETTER
    fname = _font("helvetica", 13)

    c.setFont(*_font("helvetica", 18, True))
    c.drawString(50, H - 60, f"Held-out A — seed {seed} (Letter, mixed structures)")
    data, style, th, tr, note = _styled_table(rnd, "deep-header")
    t = Table(data, repeatRows=0)
    t.setStyle(TableStyle(style))
    t.wrapOn(c, W, H)
    t.drawOn(c, 50, H - 60 - 40 - len(data) * 20)
    truth["tables"].append({"page": 1, "kind": note, "headers": th, "rows": tr})
    c.showPage()

    # two-page table with repeated header
    hdr = ["ID", "Summary", "Owner"]
    rows = [[f"INC-{1000 + i}", f"event number {i} triaged", f"owner-{i % 3}"] for i in range(28)]
    for half, page in ((rows[:14], 2), (rows[14:], 3)):
        c.setFont(*fname)
        c.drawString(50, H - 60, f"Register (continued) — page {page}")
        t = Table([hdr, *half], repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.black),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
        t.wrapOn(c, W, H)
        t.drawOn(c, 50, H - 120 - len(half) * 18)
        c.showPage()
    truth["tables"].append({"page": "2-3", "kind": "split across pages, repeated header",
                             "headers": hdr, "rows": rows})

    # Arabic text layer + Arabic table (RTL)
    c.setFont(*_font("times", 14, True))
    c.drawString(50, H - 60, "Arabic text layer and RTL table")
    c.setFont(*_font("times", 12))
    c.drawString(50, H - 90, "Duplicate handling differs per queue. Escalate P1 immediately.")
    truth["ocr"].append({"page": 4, "kind": "native-text-layer",
                          "strings": ["Duplicate handling differs per queue. Escalate P1 immediately."]})
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import os as _os
    _dej = _os.getenv("FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    try:
        pdfmetrics.registerFont(TTFont("DejaVuAr", _dej))
        arfont = "DejaVuAr"
    except Exception:
        arfont = "Helvetica"
    data = [["\u0627\u0644\u062e\u062f\u0645\u0629", "\u0627\u0644\u0645\u0627\u0644\u0643"],
            ["vpn", "L. Haddad"], ["wifi", "L. Haddad"]]
    t = Table(data, style=[("FONTNAME", (0, 0), (-1, -1), arfont)])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.black)]))
    t.wrapOn(c, W, H)
    t.drawOn(c, 50, H - 220)
    truth["tables"].append({"page": 4, "kind": "arabic RTL table",
                             "headers": ["\u0627\u0644\u062e\u062f\u0645\u0629", "\u0627\u0644\u0645\u0627\u0644\u0643"],
                             "rows": [["vpn", "L. Haddad"], ["wifi", "L. Haddad"]]})
    c.showPage()

    # dark network diagram + bar chart
    pg = 5
    for style, dark in (("network", True), ("barchart", False)):
        img, facts = _flow_diagram(style, seed * 7 + len(truth["diagrams"]), dark=dark)
        c.drawImage(ImageReader(img), 40, H / 2 - 60, width=W - 80, height=(W - 80) * img.height / img.width)
        truth["diagrams"].append({"page": pg, **facts})
        pg += 1
        c.showPage()
    c.save()
    return truth


def build_pdf_b(out: Path, seed: int) -> dict:
    rnd = random.Random(seed)
    truth = {"seed": seed, "tables": [], "ocr": [], "diagrams": []}
    pagesize = landscape(LETTER)
    W, H = pagesize
    c = canvas.Canvas(str(out), pagesize=pagesize)
    c.setFont(*_font("courier", 16, True))
    c.drawString(50, H - 60, f"Held-out B — seed {seed} (landscape, spans everywhere)")

    data, style, th, tr, note = _styled_table(rnd, "rowspan")
    t = Table(data)
    t.setStyle(TableStyle(style))
    t.wrapOn(c, W, H)
    t.drawOn(c, 50, H - 260)
    truth["tables"].append({"page": 1, "kind": note, "headers": th, "rows": tr})
    data2, style2, th2, tr2, note2 = _styled_table(rnd, "nested")
    t2 = Table(data2)
    t2.setStyle(TableStyle(style2))
    t2.wrapOn(c, W, H)
    t2.drawOn(c, 420, H - 260)
    truth["tables"].append({"page": 1, "kind": note2, "headers": th2, "rows": tr2})
    c.showPage()

    # skewed low-res mixed Arabic/English scan
    lines = [("Shift notes - batch 9", False, 38),
             ("\u0645\u0644\u0627\u062d\u0638\u0627\u062a \u0627\u0644\u0648\u0631\u062f\u064a\u0629 \u0645\u0639 \u062a\u062d\u062f\u064a\u062b \u0627\u0644\u0646\u0638\u0627\u0645", True, 36),
             ("Ticket INC-4410 \u062a\u0645 \u0625\u063a\u0644\u0627\u0642\u0647 solved at 09:41", False, 32)]
    img = _scan_image(lines, skew_deg=rnd.choice([2.5, -3.0, 4.0]), dpi_note=120)
    c.drawImage(ImageReader(img), 40, H - 480, width=W - 80, height=(W - 80) * img.height / img.width)
    truth["ocr"].append({"page": 2, "kind": "image-only scan, skewed 2-4deg, 120dpi, mixed AR/EN line",
                          "strings": ["Shift notes - batch 9",
                                      "\u0645\u0644\u0627\u062d\u0638\u0627\u062a \u0627\u0644\u0648\u0631\u062f\u064a\u0629 \u0645\u0639 \u062a\u062d\u062f\u064a\u062b \u0627\u0644\u0646\u0638\u0627\u0645",
                                      "Ticket INC-4410", "\u062a\u0645 \u0625\u063a\u0644\u0627\u0642\u0647", "solved at 09:41"]})
    c.showPage()

    for style in ("swimlane", "decision-tree"):
        img, facts = _flow_diagram(style, seed * 13 + len(truth["diagrams"]))
        c.drawImage(ImageReader(img), 40, H - 400, width=W - 80, height=(W - 80) * img.height / img.width)
        truth["diagrams"].append({"page": 3 + (1 if style == "decision-tree" else 0), **facts})
        c.showPage()
    c.save()

    # real /Rotate 270 on page 1 content: bake rotation into a copy
    import pymupdf
    d = pymupdf.open(str(out))
    d[0].set_rotation(270)
    d.save(str(out) + ".tmp", garbage=3, deflate=True)
    d.close()
    os.replace(str(out) + ".tmp", str(out))
    truth["rotated_pages"] = [{"page": 1, "rotate": 270}]
    return truth


def build_pdf_c(out: Path, seed: int) -> dict:
    rnd = random.Random(seed)
    truth = {"seed": seed, "tables": [], "ocr": [], "diagrams": []}
    c = canvas.Canvas(str(out), pagesize=A5)
    W, H = A5
    c.setFont(*_font("times", 15, True))
    c.drawString(40, H - 50, f"Held-out C — seed {seed} (A5, image-only + vector)")

    # whole-page single image, no text layer
    lines = [("Nightly reconciliation", False, 34),
             ("\u062a\u0633\u0648\u064a\u0629 \u0644\u064a\u0644\u064a\u0629 \u0644\u0644\u0645\u062f\u0641\u0648\u0639\u0627\u062a", True, 34),
             ("Batch 7 closed with 0 errors.", False, 30)]
    img = _scan_image(lines, size=(1100, 1300), dpi_note=150)
    c.drawImage(ImageReader(img), 30, 60, width=W - 60, height=H - 140)
    truth["ocr"].append({"page": 1, "kind": "whole-page single image, no text layer",
                          "strings": ["Nightly reconciliation",
                                      "\u062a\u0633\u0648\u064a\u0629 \u0644\u064a\u0644\u064a\u0629 \u0644\u0644\u0645\u062f\u0641\u0648\u0639\u0627\u062a",
                                      "Batch 7 closed with 0 errors."]})
    c.showPage()

    # borderless table + Arabic image-only table side content
    data, style, th, tr, note = _styled_table(rnd, "borderless")
    t = Table(data)
    t.setStyle(TableStyle(style))
    t.wrapOn(c, W, H)
    t.drawOn(c, 40, H - 60 - len(data) * 22)
    truth["tables"].append({"page": 2, "kind": note, "headers": th, "rows": tr})
    c.showPage()

    # vector-drawn org chart (no raster image, many drawing ops)
    c.setFont(*_font("helvetica", 12, True))
    c.drawString(40, H - 40, "On-call rotation (vector)")
    names = ["Captain", "Deputy", "Comms", "Scribe"]
    for i, n in enumerate(names):
        x = 40 + (i % 2) * 200
        y = H - 140 - (i // 2) * 120
        c.roundRect(x, y, 160, 44, 6, fill=0)
        c.drawCentredString(x + 80, y + 17, n)
        if i:
            c.line(x - 40 + 160, y + 60, x, y + 44)
    for j in range(30):
        c.line(40, 90 + j * 4, 380, 90 + j * 4)
    truth["diagrams"].append({"page": 3, "style": "org-chart-vector",
                               "nodes": names, "decisions": [],
                               "edges": [["Captain", "Deputy", ""], ["Captain", "Comms", ""],
                                         ["Deputy", "Scribe", ""]]})
    c.showPage()
    c.save()
    return truth


BUILDERS = {"a": build_pdf_a, "b": build_pdf_b, "c": build_pdf_c}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="11,22,33")
    ap.add_argument("--out-dir", default="challenge-heldout")
    a = ap.parse_args()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    seeds = [int(s) for s in a.seeds.split(",")]
    manifest = {}
    for i, (key, fn) in enumerate(BUILDERS.items()):
        seed = seeds[i % len(seeds)]
        pdf = out / f"heldout_{key}_s{seed}.pdf"
        truth = fn(pdf, seed)
        (out / f"heldout_{key}_s{seed}.truth.json").write_text(
            json.dumps(truth, ensure_ascii=False, indent=1))
        manifest[pdf.name] = {"seed": seed, "truth": pdf.with_suffix("").name + ".truth.json"}
        print("wrote", pdf, f"({truth['tables'] and len(truth['tables'])} tables, "
              f"{len(truth['ocr'])} ocr pages, {len(truth['diagrams'])} diagrams)")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
