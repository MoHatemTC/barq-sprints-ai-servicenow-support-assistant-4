#!/usr/bin/env python3
"""Builds challenge/barq_challenge.pdf - one hostile document that exercises all four challenges.

  p1  nested + intersecting table   (colspan header, rowspan first column, table nested inside a cell)
  p2  Arabic + English text as image only (no text layer), shaped RTL, light scan noise
  p3  page stored with /Rotate 90   (landscape "schematic" table)
  p4  content drawn rotated 90 deg counter-clockwise inside an upright page (baked-in rotation)
  p5  raster infographics: escalation flowchart with a decision + network diagram
  p6  vector-drawn flow diagram (no raster image, few text labels)
  p7  scanned-style page: Arabic image rotated 180 deg (no text layer)

Usage: python cli/make_challenge_pdf.py [out.pdf]
"""
import io
import os
import random
import sys

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle

FONT = os.getenv("FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_B = FONT.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
W, H = A4
random.seed(7)


from PIL import features

RAQM = features.check("raqm")  # libraqm shapes + reorders RTL itself; without it we must do it by hand


def ar(s: str) -> str:
    """Return the string to hand to PIL. With raqm, pass logical text; without, pre-shape + bidi."""
    return s if RAQM else get_display(arabic_reshaper.reshape(s))


def font(sz, bold=False):
    return ImageFont.truetype(FONT_B if bold and os.path.exists(FONT_B) else FONT, sz)


def draw_rtl_block(lines, size=(1400, 900)) -> Image.Image:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    y = 40
    for text, is_ar, sz in lines:
        f = font(sz, True)
        s = ar(text) if is_ar else text
        kw = {"direction": "rtl", "language": "ar"} if (is_ar and RAQM) else {}
        w = d.textlength(s, font=f, **kw)
        x = size[0] - 60 - w if is_ar else 60  # Arabic right-aligned
        d.text((x, y), s, font=f, fill="black", **kw)
        y += int(sz * 1.9)
    # scan noise
    px = img.load()
    for _ in range(9000):
        px[random.randrange(size[0]), random.randrange(size[1])] = (150, 150, 150)
    return img.rotate(0.6, fillcolor="white", resample=Image.BICUBIC).filter(ImageFilter.GaussianBlur(0.5))


def box(d, xy, text, fill="#e8f0fe", f=None, shape="rect"):
    f = f or font(26, True)
    x0, y0, x1, y1 = xy
    if shape == "diamond":
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        d.polygon([(cx, y0), (x1, cy), (cx, y1), (x0, cy)], fill="#fff4d6", outline="black")
    else:
        d.rounded_rectangle(xy, 14, fill=fill, outline="black", width=3)
    w = d.textlength(text, font=f)
    d.text(((x0 + x1) / 2 - w / 2, (y0 + y1) / 2 - 15), text, font=f, fill="black")


def arrow(d, a, b, label=None):
    d.line([a, b], fill="black", width=4)
    import math
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    for da in (2.6, -2.6):
        d.line([b, (b[0] + 18 * math.cos(ang + da), b[1] + 18 * math.sin(ang + da))], fill="black", width=4)
    if label:
        d.text(((a[0] + b[0]) / 2 + 8, (a[1] + b[1]) / 2 - 30), label, font=font(22), fill="#a00000")


def flowchart() -> Image.Image:
    img = Image.new("RGB", (1500, 620), "white")
    d = ImageDraw.Draw(img)
    d.text((30, 15), "Incident escalation flow", font=font(34, True), fill="black")
    box(d, (30, 260, 290, 360), "Incident raised")
    box(d, (360, 260, 640, 360), "Tier 1 applies KB")
    box(d, (710, 230, 1010, 390), "Resolved in 1h?", shape="diamond")
    box(d, (1120, 100, 1470, 190), "Close + KB number")
    box(d, (1120, 400, 1470, 490), "Escalate to resolver")
    box(d, (720, 500, 1060, 590), "Rejected twice?", f=font(24, True))
    box(d, (1120, 520, 1470, 600), "Service owner", f=font(24, True))
    arrow(d, (290, 310), (360, 310))
    arrow(d, (640, 310), (710, 310))
    arrow(d, (1010, 310), (1120, 145), "YES")
    arrow(d, (1010, 330), (1120, 440), "NO")
    arrow(d, (1120, 470), (1060, 530))
    arrow(d, (1060, 545), (1120, 555), "yes")
    return img


def network() -> Image.Image:
    img = Image.new("RGB", (1500, 520), "white")
    d = ImageDraw.Draw(img)
    d.text((30, 15), "Order path (network diagram)", font=font(34, True), fill="black")
    box(d, (40, 200, 260, 290), "Laptop", "#e6f4ea")
    box(d, (360, 200, 620, 290), "VPN gateway", "#e6f4ea")
    box(d, (720, 90, 1000, 180), "SAP message srv", "#fce8e6", font(24, True))
    box(d, (720, 320, 1000, 410), "Order service", "#fce8e6", font(24, True))
    box(d, (1150, 320, 1440, 410), "Orders DB (pool)", "#fce8e6", font(24, True))
    arrow(d, (260, 245), (360, 245), "TLS")
    arrow(d, (620, 225), (720, 145), "3200")
    arrow(d, (620, 265), (720, 355), "443")
    arrow(d, (1000, 365), (1150, 365), "JDBC")
    return img


def build(out):
    c = canvas.Canvas(out, pagesize=A4)
    c.setTitle("BARQ hostile challenge document")

    # ---- p1: nested + intersecting tables
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, H - 60, "1. SLA matrix and escalation ownership (nested / merged cells)")
    inner = Table([["Step", "Owner"], ["Notify", "IAM lead"], ["Approve", "Change mgr"]], colWidths=[50, 65])
    inner.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 7)]))
    data = [
        ["Priority", "Targets", "", "Escalation", ""],
        ["", "Response", "Resolution", "After", "Route"],
        ["P1", "15 minutes", "4 hours", "15 min", "Bridge + service owner"],
        ["P2", "30 minutes", "8 hours", "1 hour", "Resolver group"],
        ["Access", "4 working hours", "3 working days", "MFA reset:", inner],
        ["", "P4: 1 working day", "5 working days", "3 days", "Convert to request"],
    ]
    t = Table(data, colWidths=[60, 95, 95, 80, 150], rowHeights=[22, 22, 24, 24, 60, 24])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.8, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("SPAN", (1, 0), (2, 0)), ("SPAN", (3, 0), (4, 0)), ("SPAN", (0, 0), (0, 1)),
        ("SPAN", (0, 4), (0, 5)), ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor("#dbe7f5")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, 1), "CENTER")]))
    t.wrapOn(c, W, H)
    t.drawOn(c, 50, H - 320)
    c.setFont("Helvetica", 10)
    c.drawString(50, H - 350, "Read carefully: 'After' under Escalation means unresolved time before escalating.")
    c.showPage()

    # ---- p2: Arabic + English in an image only
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, H - 50, "2. Scanned desk card (Arabic / English)")
    img = draw_rtl_block([
        ("Escalation and acceptance card - issue 4", False, 40),
        ("بطاقة التصعيد والقبول - الإصدار الرابع", True, 44),
        ("إذا لم تتم معالجة الحادث خلال ساعة يتم تصعيده إلى مجموعة الحل.", True, 34),
        ("إعادة تعيين المصادقة متعددة العوامل تتطلب موافقة (RITM0010877).", True, 34),
        ("رفض التصعيد مرتين يعني رفعه إلى مالك الخدمة، وليس إلى مكتب الخدمة.", True, 34),
        ("Two rejections go to the service owner: KB0010 v2, CHG0030455.", False, 32),
    ])
    c.drawImage(ImageReader(img), 40, H - 500, width=W - 80, height=(W - 80) * img.height / img.width)
    c.showPage()

    # ---- p3: /Rotate 90 landscape table
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, H - 60, "3. Rotated page (/Rotate 90): wireless coverage schematic")
    rows = [["Floor", "AP", "Channel", "Roaming drops / day"], ["3", "AP-3A", "36", "12"], ["3", "AP-3B", "44", "19"],
            ["4", "AP-4A", "149", "23"], ["4", "AP-4B", "157", "8"]]
    t3 = Table(rows, colWidths=[60, 80, 80, 140])
    t3.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.8, colors.black), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    t3.wrapOn(c, W, H)
    t3.drawOn(c, 50, H - 220)
    c.setFont("Helvetica", 11)
    c.drawString(50, H - 250, "PRB0040021: drops occur only while roaming between floors 3 and 4 (CHG0030588).")
    c.showPage()

    # ---- p4: baked-in rotation
    c.saveState()
    c.translate(60, 60)
    c.rotate(90)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(0, -20, "4. Content rotated inside the page (schematic sideways)")
    c.setFont("Helvetica", 12)
    for i, s in enumerate(["Print spooler restart clears stuck queues (CHG0030401).",
                           "If the queue stalls again within an hour, escalate to Print Services.",
                           "Mechanical noise on the paper feed is a Facilities matter, not KB0004."]):
        c.drawString(0, -60 - i * 20, s)
    c.restoreState()
    c.showPage()

    # ---- p5: raster diagrams
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, H - 50, "5. Infographics")
    for k, im in enumerate((flowchart(), network())):
        h = (W - 80) * im.height / im.width
        c.drawImage(ImageReader(im), 40, H - 100 - h - k * 330, width=W - 80, height=h)
    c.showPage()

    # ---- p6: vector diagram
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, H - 50, "6. Vector flow (no raster image)")
    labels = ["Alert", "P1?", "Bridge", "Owner paged", "Change approved", "Drain"]
    x, y = 50, H - 200
    pos = []
    for i, lab in enumerate(labels):
        bx = x + (i % 3) * 170
        by = y - (i // 3) * 140
        c.setFillColor(colors.HexColor("#e8f0fe"))
        c.roundRect(bx, by, 130, 50, 8, fill=1)
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 11)
        c.drawCentredString(bx + 65, by + 20, lab)
        pos.append((bx, by))
    for i in range(len(pos) - 1):
        (ax, ay), (bx, by) = pos[i], pos[i + 1]
        if i == 2:
            c.line(ax + 65, ay, ax + 65, by + 50 + 30)
            c.line(ax + 65, by + 80, bx + 65, by + 80)
            c.line(bx + 65, by + 80, bx + 65, by + 50)
        else:
            c.line(ax + 130, ay + 25, bx, by + 25)
            c.line(bx - 8, by + 30, bx, by + 25)
            c.line(bx - 8, by + 20, bx, by + 25)
    for j in range(8):  # extra drawing ops so the page is clearly graphic
        c.setStrokeColor(colors.lightgrey)
        c.line(50, 120 + j * 6, 500, 120 + j * 6)
    c.setStrokeColor(colors.black)
    c.showPage()

    # ---- p7: Arabic scan rotated 180
    img = draw_rtl_block([
        ("بطاقة الاتصال الخارجي", True, 46),
        ("للحوادث من الفئة الأولى يتم استدعاء مالك الخدمة فورا وفتح جسر الطوارئ.", True, 34),
        ("P1: bridge opens immediately, service owner paged.", False, 32),
    ], size=(1400, 500)).rotate(180)
    c.drawImage(ImageReader(img), 40, H / 2, width=W - 80, height=(W - 80) * img.height / img.width)
    c.showPage()
    c.save()
    # p3 gets a real /Rotate 90 entry: content stays upright in the content stream, viewer shows it sideways
    import pymupdf
    d = pymupdf.open(out)
    d[2].set_rotation(90)
    d.saveIncr() if False else d.save(out + ".tmp", garbage=3, deflate=True)
    d.close()
    os.replace(out + ".tmp", out)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "challenge", "barq_challenge.pdf")
    build(out)
    print("wrote", os.path.abspath(out))
