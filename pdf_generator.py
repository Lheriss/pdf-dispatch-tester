"""
pdf_generator.py — Test PDF factory for pdf-dispatch-tester.

Generates PDFs with known content at specific page positions, suitable for
triggering and verifying all pdf-dispatch splitting behaviours.

Usage
-----
from pdf_generator import make_pdf, make_corrupt_pdf, make_non_pdf

# A 4-page PDF: content | QR FK3 | content | content
pdf_bytes = make_pdf([
    {"kind": "content",  "text": "Document A — page 1"},
    {"kind": "qr",       "value": "FK3"},
    {"kind": "content",  "text": "Document B — page 1"},
    {"kind": "content",  "text": "Document B — page 2"},
])

# A corrupted file (not a valid PDF)
bad_bytes = make_corrupt_pdf()

# A non-PDF file with a .pdf extension
fake_bytes = make_non_pdf()

Page specs (list of dicts)
--------------------------
{"kind": "content",  "text": "…", "subtitle": "…"}  — plain text page
{"kind": "qr",       "value": "FK3",  "label": "…"}  — QR code centred on page
{"kind": "code128",  "value": "FK3",  "label": "…"}  — Code128 barcode on page
{"kind": "multi",    "values": ["A","B"], "format": "qr"}  — multiple codes, one per block
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm

# ── Optional base PDF (content pages provided by the user) ───────────────────
_BASE_PDF_BYTES: bytes | None = None


def set_base_pdf(data: bytes | None) -> None:
    """Register a custom base PDF whose pages replace generated content pages."""
    global _BASE_PDF_BYTES
    _BASE_PDF_BYTES = data


def get_base_pdf() -> bytes | None:
    return _BASE_PDF_BYTES

from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = A4


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _qr_png(value: str) -> bytes:
    """Render a QR code to PNG bytes."""
    import segno
    buf = BytesIO()
    segno.make_qr(value).save(buf, kind="png", scale=12, border=4, dark="#000000", light="#ffffff")
    return buf.getvalue()


def _code128_png(value: str) -> bytes:
    """Render a Code128 barcode to PNG bytes."""
    import barcode
    from barcode.writer import ImageWriter
    buf = BytesIO()
    writer = ImageWriter()
    barcode.get("code128", value, writer=writer).write(
        buf,
        options={
            "module_width":  0.6,
            "module_height": 15.0,
            "quiet_zone":    6.5,
            "font_size":     10,
            "text_distance": 5.0,
            "write_text":    True,
        },
    )
    return buf.getvalue()


def _draw_barcode_page(c: canvas.Canvas, png_bytes: bytes, label: str | None) -> None:
    """Draw a barcode/QR image centred on an A4 page."""
    img = Image.open(BytesIO(png_bytes))
    img_w, img_h = img.size
    ratio = img_h / img_w

    # Target width: 60% of page width, capped so it doesn't overflow vertically
    target_w = PAGE_W * 0.60
    target_h = target_w * ratio
    if target_h > PAGE_H * 0.50:
        target_h = PAGE_H * 0.50
        target_w = target_h / ratio

    x = (PAGE_W - target_w) / 2
    y = (PAGE_H - target_h) / 2

    reader = ImageReader(BytesIO(png_bytes))
    c.drawImage(reader, x, y, width=target_w, height=target_h, preserveAspectRatio=True)

    if label:
        c.setFont("Helvetica", 11)
        c.setFillColorRGB(0.5, 0.5, 0.5)
        c.drawCentredString(PAGE_W / 2, y - 20, label)


def _draw_content_page(c: canvas.Canvas, text: str, subtitle: str | None, page_num: int | None) -> None:
    """Draw a plain-text content page."""
    c.setFont("Helvetica-Bold", 18)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.drawCentredString(PAGE_W / 2, PAGE_H * 0.60, text)

    if subtitle:
        c.setFont("Helvetica", 12)
        c.setFillColorRGB(0.4, 0.4, 0.4)
        c.drawCentredString(PAGE_W / 2, PAGE_H * 0.60 - 28, subtitle)

    # Corner label for traceability in manual inspection
    if page_num is not None:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.7, 0.7, 0.7)
        c.drawString(1 * cm, 1 * cm, f"pdf-dispatch-tester · p{page_num}")


def _draw_multi_page(c: canvas.Canvas, values: list[str], fmt: str) -> None:
    """Draw multiple barcodes on a single page (grid layout)."""
    n = len(values)
    cols = 2 if n > 1 else 1
    rows = (n + cols - 1) // cols
    cell_w = PAGE_W / cols
    cell_h = PAGE_H * 0.80 / rows
    y_offset = PAGE_H * 0.10

    for idx, value in enumerate(values):
        col = idx % cols
        row = idx // cols
        png = _qr_png(value) if fmt == "qr" else _code128_png(value)
        img = Image.open(BytesIO(png))
        ratio = img.height / img.width
        img_w = cell_w * 0.7
        img_h = img_w * ratio
        x = col * cell_w + (cell_w - img_w) / 2
        y = y_offset + (rows - 1 - row) * cell_h + (cell_h - img_h) / 2
        c.drawImage(ImageReader(BytesIO(png)), x, y, width=img_w, height=img_h,
                    preserveAspectRatio=True)
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.3, 0.3, 0.3)
        c.drawCentredString(col * cell_w + cell_w / 2, y - 14, value)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def make_pdf(pages: list[dict[str, Any]]) -> bytes:
    """
    Generate a multi-page PDF from a list of page specifications.

    Parameters
    ----------
    pages : list of dicts with the following keys:
        kind      : "content" | "qr" | "code128" | "multi"
        text      : (content) main text displayed on the page
        subtitle  : (content) optional smaller text below
        value     : (qr | code128) barcode value
        label     : (qr | code128) optional caption below the barcode
        values    : (multi) list of barcode values on the same page
        format    : (multi) "qr" or "code128" (default: "qr")

    Returns
    -------
    bytes  PDF file content
    """
    from pypdf import PdfReader, PdfWriter

    # If a custom base PDF has been registered, use its pages as content.
    _base = _BASE_PDF_BYTES
    _base_pages: list | None = None
    _base_idx = 0
    if _base:
        try:
            _base_pages = list(PdfReader(BytesIO(_base)).pages)
        except Exception:
            _base_pages = None

    # When mixing base PDF pages (pypdf) with generated pages (reportlab)
    # we must render each page separately and merge at the end.
    if _base_pages:
        writer = PdfWriter()
        for i, spec in enumerate(pages, start=1):
            kind = spec.get("kind", "content")
            if kind == "content":
                writer.add_page(_base_pages[_base_idx % len(_base_pages)])
                _base_idx += 1
            else:
                # Render barcode / multi page with reportlab
                _buf = BytesIO()
                _c = canvas.Canvas(_buf, pagesize=A4)
                if kind == "qr":
                    _draw_barcode_page(_c, _qr_png(spec["value"]), spec.get("label"))
                elif kind == "code128":
                    _draw_barcode_page(_c, _code128_png(spec["value"]), spec.get("label"))
                elif kind == "multi":
                    _draw_multi_page(_c, spec["values"], spec.get("format", "qr"))
                else:
                    raise ValueError(f"Unknown page kind: {kind!r}")
                _c.showPage(); _c.save(); _buf.seek(0)
                writer.add_page(PdfReader(_buf).pages[0])
        out = BytesIO(); writer.write(out); out.seek(0); return out.read()

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)

    for i, spec in enumerate(pages, start=1):
        kind = spec.get("kind", "content")

        if kind == "content":
            _draw_content_page(c, spec.get("text", f"Page {i}"), spec.get("subtitle"), i)

        elif kind == "qr":
            png = _qr_png(spec["value"])
            _draw_barcode_page(c, png, spec.get("label"))

        elif kind == "code128":
            png = _code128_png(spec["value"])
            _draw_barcode_page(c, png, spec.get("label"))

        elif kind == "multi":
            _draw_multi_page(c, spec["values"], spec.get("format", "qr"))

        else:
            raise ValueError(f"Unknown page kind: {kind!r}")

        c.showPage()

    c.save()
    buf.seek(0)
    return buf.read()


def make_corrupt_pdf() -> bytes:
    """Return bytes that look like a PDF header but contain garbage — triggers error handling."""
    return b"%PDF-1.4\n%%EOF\x00\xff\xfe\x00" + b"\x00" * 128


def make_non_pdf() -> bytes:
    """Return a JPEG file with a .pdf extension — triggers non-PDF error handling."""
    buf = BytesIO()
    img = Image.new("RGB", (200, 100), color=(180, 200, 220))
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Pre-built fixtures used across all test phases
# ─────────────────────────────────────────────────────────────────────────────

def fixture_one_trigger_before(trigger: str = "FK3") -> bytes:
    """4-page PDF: content | trigger | content | content  (before+keep scenario)."""
    return make_pdf([
        {"kind": "content",  "text": "Document 1 — page 1",   "subtitle": f"(before trigger '{trigger}')"},
        {"kind": "qr",       "value": trigger,                 "label": f"Trigger: {trigger}"},
        {"kind": "content",  "text": "Document 2 — page 1"},
        {"kind": "content",  "text": "Document 2 — page 2"},
    ])


def fixture_one_trigger_after(trigger: str = "FK3") -> bytes:
    """4-page PDF: content | content | trigger | content  (after+keep scenario)."""
    return make_pdf([
        {"kind": "content",  "text": "Document 1 — page 1"},
        {"kind": "content",  "text": "Document 1 — page 2"},
        {"kind": "qr",       "value": trigger,                 "label": f"Trigger: {trigger}"},
        {"kind": "content",  "text": "Document 2 — page 1"},
    ])


def fixture_two_triggers(t1: str = "FK3", t2: str = "FK3") -> bytes:
    """6-page PDF: 3 documents separated by 2 triggers."""
    return make_pdf([
        {"kind": "content",  "text": "Document 1 — page 1"},
        {"kind": "qr",       "value": t1},
        {"kind": "content",  "text": "Document 2 — page 1"},
        {"kind": "content",  "text": "Document 2 — page 2"},
        {"kind": "qr",       "value": t2},
        {"kind": "content",  "text": "Document 3 — page 1"},
    ])


def fixture_no_code() -> bytes:
    """3-page PDF with no barcode — should go to no_code/."""
    return make_pdf([
        {"kind": "content", "text": "No barcode page 1"},
        {"kind": "content", "text": "No barcode page 2"},
        {"kind": "content", "text": "No barcode page 3"},
    ])


def fixture_glob(trigger: str = "FK42") -> bytes:
    """1-page PDF with a code that matches a glob pattern (e.g. FK*)."""
    return make_pdf([
        {"kind": "content", "text": "Document before"},
        {"kind": "qr",      "value": trigger, "label": f"Glob test: {trigger}"},
        {"kind": "content", "text": "Document after"},
    ])


def fixture_case_sensitivity(trigger: str = "invoice") -> bytes:
    """PDF with lowercase trigger value for case-sensitivity tests."""
    return make_pdf([
        {"kind": "content", "text": "Document before"},
        {"kind": "qr",      "value": trigger, "label": f"Case test: {trigger}"},
        {"kind": "content", "text": "Document after"},
    ])


def fixture_multi_trigger_same_page(triggers: list[str] | None = None) -> bytes:
    """PDF with two codes on the same page."""
    if triggers is None:
        triggers = ["INVOICE", "COPY"]
    return make_pdf([
        {"kind": "content", "text": "Document before"},
        {"kind": "multi",   "values": triggers, "format": "qr"},
        {"kind": "content", "text": "Document after"},
    ])


# ─────────────────────────────────────────────────────────────────────────────
# Adversarial fixtures (Phase 1d / 1e)
# ─────────────────────────────────────────────────────────────────────────────

def make_truncated_pdf() -> bytes:
    """Valid PDF start then abruptly truncated — should go to error/."""
    full = make_pdf([{"kind": "content", "text": "Truncated"}])
    return full[: len(full) // 3]


def make_zero_bytes() -> bytes:
    """Empty file — should go to error/."""
    return b""


def make_non_pdf_with_pdf_extension() -> bytes:
    """A JPEG file saved with a .pdf extension — should go to error/."""
    return make_non_pdf()


def make_zip_as_pdf() -> bytes:
    """Minimal ZIP header disguised as a PDF — should go to error/."""
    return b"PK\x03\x04" + b"\x00" * 26 + b"fake content"


def make_low_dpi_qr(value: str = "FK3") -> bytes:
    """
    PDF with a very small QR code that may not be detected at standard DPI.
    Useful for testing the boundary of barcode detection.
    """
    import segno
    buf = BytesIO()
    # Tiny QR: scale=1, no border — difficult to detect
    segno.make_qr(value).save(buf, kind="png", scale=1, border=0)
    png = buf.getvalue()

    out = BytesIO()
    c   = canvas.Canvas(out, pagesize=A4)
    # Draw the tiny QR (20x20 px) in a corner
    reader = ImageReader(BytesIO(png))
    c.drawImage(reader, 10, 10, width=20, height=20)
    c.showPage()
    c.save()
    out.seek(0)
    return out.read()


def make_rotated_barcode(value: str = "FK3", angle: float = 45.0) -> bytes:
    """
    PDF with a barcode rotated by `angle` degrees.
    ZXing handles rotation well; pyzbar may struggle.
    """
    import segno
    buf = BytesIO()
    segno.make_qr(value).save(buf, kind="png", scale=10, border=4)
    png = buf.getvalue()

    from PIL import Image as _Image
    img = _Image.open(BytesIO(png)).rotate(angle, expand=True)
    rotated_buf = BytesIO()
    img.save(rotated_buf, format="PNG")

    out = BytesIO()
    c   = canvas.Canvas(out, pagesize=A4)
    reader = ImageReader(rotated_buf)
    c.drawImage(reader, PAGE_W * 0.2, PAGE_H * 0.3,
                width=PAGE_W * 0.6, height=PAGE_W * 0.6,
                preserveAspectRatio=True)
    c.showPage()
    c.save()
    out.seek(0)
    return out.read()


def make_single_page_with_code(value: str = "FK3") -> bytes:
    """Single-page PDF containing only a trigger code — edge case for delete mode."""
    return make_pdf([{"kind": "qr", "value": value}])


def make_code_on_last_page(value: str = "FK3", content_pages: int = 3) -> bytes:
    """PDF where the trigger appears on the last page only."""
    pages = [{"kind": "content", "text": f"Content page {i}"} for i in range(1, content_pages + 1)]
    pages.append({"kind": "qr", "value": value, "label": "Trigger on last page"})
    return make_pdf(pages)


def make_unknown_trigger(value: str = "UNKNOWN_CODE_XYZ") -> bytes:
    """PDF with a valid QR code not in the trigger list — should go to no_code/."""
    return make_pdf([
        {"kind": "content", "text": "Before"},
        {"kind": "qr",      "value": value},
        {"kind": "content", "text": "After"},
    ])
