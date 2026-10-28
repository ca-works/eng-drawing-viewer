"""Generate a realistic test corpus of engineering-style PDFs.

Run directly to create a browsable sample folder:
    python tests/corpus.py /path/to/output
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pymupdf

SIZES = {
    "A0": (3370.4, 2383.9),
    "A1": (2383.9, 1683.8),
    "A3": (1190.6, 841.9),
    "A4P": (595.3, 841.9),
}


def _title_block(page: pymupdf.Page, title: str, number: str, rev: str):
    r = page.rect
    w, h = r.width, r.height
    m = 20
    page.draw_rect(pymupdf.Rect(m, m, w - m, h - m), width=1.2)
    page.draw_rect(pymupdf.Rect(m + 8, m + 8, w - m - 8, h - m - 8), width=0.35)
    bw, bh = min(520, w * 0.42), 110
    box = pymupdf.Rect(w - m - 8 - bw, h - m - 8 - bh, w - m - 8, h - m - 8)
    page.draw_rect(box, width=0.7)
    for i in range(1, 4):
        y = box.y0 + i * bh / 4
        page.draw_line((box.x0, y), (box.x1, y), width=0.25)
    page.draw_line((box.x0 + bw * 0.6, box.y0), (box.x0 + bw * 0.6, box.y1), width=0.25)
    page.insert_text((box.x0 + 6, box.y0 + 19), title, fontsize=12)
    page.insert_text((box.x0 + 6, box.y0 + 46), f"DWG NO. {number}", fontsize=9)
    page.insert_text((box.x0 + bw * 0.6 + 6, box.y0 + 46), f"REV {rev}", fontsize=9)
    page.insert_text((box.x0 + 6, box.y0 + 73), "TOLERANCES UNLESS NOTED: ±0.1  ANGLES ±0.5°", fontsize=5.5)
    page.insert_text((box.x0 + 6, box.y0 + 100), "MATERIAL: AlMgSi1 F31  —  SCALE 1:2  —  SHEET 1/1", fontsize=5.5)
    # zone markers
    for i in range(8):
        x = m + 8 + (w - 2 * m - 16) * (i + 0.5) / 8
        page.insert_text((x - 2, m + 6.5), str(i + 1), fontsize=5)


def _dimension(page, a, b, offset, text):
    ax, ay = a
    bx, by = b
    page.draw_line((ax, ay), (ax, ay - offset - 4), width=0.18)
    page.draw_line((bx, by), (bx, by - offset - 4), width=0.18)
    y = ay - offset
    page.draw_line((ax, y), (bx, y), width=0.18)
    for x, d in ((ax, 1), (bx, -1)):
        page.draw_polyline([(x + d * 6, y - 1.6), (x, y), (x + d * 6, y + 1.6)], width=0.18, closePath=True, fill=(0, 0, 0))
    page.insert_text(((ax + bx) / 2 - len(text) * 1.1, y - 2), text, fontsize=3.2)


def engineering_page(page: pymupdf.Page, title: str, number: str, rev: str = "B", seed: int = 0):
    _title_block(page, title, number, rev)
    r = page.rect
    cx, cy = r.width * 0.38, r.height * 0.45
    scale = r.width / 2384
    # Part outline
    page.draw_rect(pymupdf.Rect(cx - 380 * scale, cy - 220 * scale, cx + 380 * scale, cy + 220 * scale), width=0.7)
    # Holes with centre marks
    for i in range(6):
        hx = cx - 300 * scale + i * 120 * scale
        page.draw_circle((hx, cy), 26 * scale, width=0.5)
        page.draw_line((hx - 34 * scale, cy), (hx + 34 * scale, cy), width=0.12, dashes="[6 2 1 2] 0")
        page.draw_line((hx, cy - 34 * scale), (hx, cy + 34 * scale), width=0.12, dashes="[6 2 1 2] 0")
        page.insert_text((hx - 10 * scale, cy - 40 * scale), f"Ø{8 + i}H7", fontsize=2.6)
    # Hatched section with true hairlines (zero width = thinnest device line)
    hatch = pymupdf.Rect(cx - 380 * scale, cy + 260 * scale, cx + 120 * scale, cy + 400 * scale)
    page.draw_rect(hatch, width=0.5)
    shape = page.new_shape()
    step = 7 * scale
    W, H = hatch.width, hatch.height
    k = -H
    while k < W:
        # 45° line starting at (x0 + k, y1), clipped to the rectangle.
        t0, t1 = max(0.0, -k), min(H, W - k)
        if t1 > t0:
            shape.draw_line((hatch.x0 + k + t0, hatch.y1 - t0), (hatch.x0 + k + t1, hatch.y1 - t1))
        k += step
    shape.finish(width=0.0, color=(0, 0, 0))
    shape.commit()
    # Very thin (0.1 pt) construction grid
    for i in range(30):
        x = cx + 450 * scale + i * 14 * scale
        page.draw_line((x, cy - 200 * scale), (x, cy + 200 * scale), width=0.1, color=(0.2, 0.2, 0.2))
    # Dimensions and tiny notes
    _dimension(page, (cx - 380 * scale, cy - 220 * scale), (cx + 380 * scale, cy - 220 * scale), 30 * scale, f"{760 * 0.5:.1f}")
    _dimension(page, (cx - 300 * scale, cy - 220 * scale), (cx - 180 * scale, cy - 220 * scale), 14 * scale, "60.0 ±0.05")
    notes = [
        "1. BREAK ALL SHARP EDGES 0.2 x 45°.",
        "2. SURFACE ROUGHNESS Ra 1.6 UNLESS OTHERWISE SPECIFIED.",
        "3. THREAD M12x1.5-6H, DEPTH 18 MIN.",
        "4. ANODISE CLEAR 10–15 µm AFTER MACHINING.",
    ]
    for i, n in enumerate(notes):
        page.insert_text((40, r.height * 0.70 + i * 8), n, fontsize=4.2)
    # Fine radial pattern to stress anti-aliasing
    for a in range(0, 360, 4):
        ang = math.radians(a + seed)
        x2 = cx + 520 * scale + math.cos(ang) * 90 * scale
        y2 = cy + 320 * scale + math.sin(ang) * 90 * scale
        page.draw_line((cx + 520 * scale, cy + 320 * scale), (x2, y2), width=0.08)


def make_pdf(path: Path, size: str = "A1", pages: int = 1, title: str = "BRACKET ASSEMBLY", rotate: int = 0):
    doc = pymupdf.open()
    w, h = SIZES[size]
    for i in range(pages):
        page = doc.new_page(width=w, height=h)
        engineering_page(page, f"{title} — SHEET {i + 1}", f"ENG-{1000 + i}", seed=i * 7)
        if rotate:
            page.set_rotation(rotate)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


def make_corpus(root: Path) -> dict[str, Path]:
    root = Path(root)
    out: dict[str, Path] = {}
    out["a1"] = make_pdf(root / "Project X" / "Drawing 1.pdf", "A1")
    out["a1_2"] = make_pdf(root / "Project X" / "Drawing 2.pdf", "A1", title="HOUSING")
    out["a1_10"] = make_pdf(root / "Project X" / "Drawing 10.pdf", "A1", title="COVER PLATE")
    out["multi"] = make_pdf(root / "Project X" / "Sub Assemblies" / "Gearbox set (5 sheets).pdf", "A3", pages=5, title="GEARBOX")
    out["rotated"] = make_pdf(root / "Project X" / "Sub Assemblies" / "Rotated 90.pdf", "A3", rotate=90, title="SHAFT")
    out["a4"] = make_pdf(root / "Specs" / "a4 portrait spec.pdf", "A4P", title="SPEC SHEET")
    out["a0"] = make_pdf(root / "Specs" / "Large A0 layout.pdf", "A0", title="FACTORY LAYOUT")
    deep = root / "Deep" / "L1" / "L2" / "L3" / "L4" / "L5" / "L6"
    out["deep"] = make_pdf(deep / "Deep nested drawing.pdf", "A3", title="DEEP PART")
    out["unicode"] = make_pdf(root / "Ünïcødé & Symbols" / "Çhâssis — rév B (final) #12 [ä].pdf", "A3", title="CHASSIS")
    long_name = "Very long filename for an engineering drawing that keeps going and going to test eliding in the sidebar rev C.pdf"
    out["long"] = make_pdf(root / "Ünïcødé & Symbols" / long_name, "A3", title="LONG NAME")
    out["upper"] = make_pdf(root / "Specs" / "UPPERCASE.PDF", "A4P", title="UPPER EXT")
    # Problem files
    bad = root / "Problems"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "empty.pdf").write_bytes(b"")
    (bad / "not really a pdf.pdf").write_text("This is plain text pretending to be a PDF.\n")
    src = make_pdf(bad / "_tmp.pdf", "A4P", title="TRUNCATED")
    data = src.read_bytes()
    (bad / "truncated.pdf").write_bytes(data[: len(data) // 2])
    src.unlink()
    enc = pymupdf.open()
    p = enc.new_page()
    p.insert_text((72, 72), "secret", fontsize=20)
    enc.save(bad / "password protected.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    enc.close()
    (root / "Project X" / "._Drawing 1.pdf").write_bytes(b"\x00\x05\x16\x07AppleDouble")
    (root / ".hidden").mkdir(exist_ok=True)
    make_pdf(root / ".hidden" / "should not appear.pdf", "A4P")
    (root / "Specs" / "notes.txt").write_text("not a pdf")
    out["root"] = root
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_drawings")
    paths = make_corpus(target)
    print(f"Created sample drawings in {target.resolve()}")
