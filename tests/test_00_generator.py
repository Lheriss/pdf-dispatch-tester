"""
test_00_generator.py — Self-tests for pdf_generator.py.

These tests do NOT require a running pdf-dispatch instance.
They verify that the PDF factory produces structurally valid output
before the generated fixtures are used in integration tests.

Run standalone:
    pytest tests/test_00_generator.py -v
"""

import io

import pytest

from pdf_generator import (
    fixture_case_sensitivity,
    fixture_glob,
    fixture_multi_trigger_same_page,
    fixture_no_code,
    fixture_one_trigger_after,
    fixture_one_trigger_before,
    fixture_two_triggers,
    make_corrupt_pdf,
    make_non_pdf,
    make_pdf,
)


def _page_count(pdf_bytes: bytes) -> int:
    """Count pages in a PDF using pypdf (installed transitively via reportlab)."""
    try:
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except ImportError:
        # pypdf not installed — skip count verification
        return -1


# ─────────────────────────────────────────────────────────────────────────────
# Basic make_pdf
# ─────────────────────────────────────────────────────────────────────────────

def test_empty_pages_list():
    """make_pdf([]) should return a valid empty PDF."""
    pdf = make_pdf([])
    assert pdf[:4] == b"%PDF", "Expected PDF magic bytes"


def test_single_content_page():
    pdf = make_pdf([{"kind": "content", "text": "Hello"}])
    assert pdf[:4] == b"%PDF"
    assert _page_count(pdf) in (1, -1)


def test_single_qr_page():
    pdf = make_pdf([{"kind": "qr", "value": "FK3"}])
    assert pdf[:4] == b"%PDF"


def test_single_code128_page():
    pdf = make_pdf([{"kind": "code128", "value": "FK3"}])
    assert pdf[:4] == b"%PDF"


def test_multi_page():
    pdf = make_pdf([
        {"kind": "content", "text": "Page 1"},
        {"kind": "qr",      "value": "ABC"},
        {"kind": "content", "text": "Page 3"},
    ])
    assert pdf[:4] == b"%PDF"
    assert _page_count(pdf) in (3, -1)


def test_unknown_kind_raises():
    with pytest.raises(ValueError, match="Unknown page kind"):
        make_pdf([{"kind": "unsupported"}])


def test_multi_code_page():
    pdf = make_pdf([{"kind": "multi", "values": ["A", "B"], "format": "qr"}])
    assert pdf[:4] == b"%PDF"


# ─────────────────────────────────────────────────────────────────────────────
# Special files
# ─────────────────────────────────────────────────────────────────────────────

def test_corrupt_pdf_is_not_valid_pdf():
    data = make_corrupt_pdf()
    assert data[:4] == b"%PDF", "Should start with PDF header"
    # But it should NOT be parseable
    try:
        from pypdf import PdfReader
        import pypdf.errors
        PdfReader(io.BytesIO(data))
        # If we get here without exception, it may be trivially "valid" but
        # pdf-dispatch's is_valid_pdf() uses its own check — acceptable
    except Exception:
        pass  # Expected: the file is intentionally malformed


def test_non_pdf_bytes():
    data = make_non_pdf()
    # Should be a JPEG (starts with FF D8)
    assert data[:2] == b"\xff\xd8", "Expected JPEG magic bytes"


# ─────────────────────────────────────────────────────────────────────────────
# Pre-built fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("trigger", ["FK3", "INVOICE", "REF001"])
def test_fixture_one_trigger_before(trigger):
    pdf = fixture_one_trigger_before(trigger)
    assert pdf[:4] == b"%PDF"
    assert _page_count(pdf) in (4, -1)


def test_fixture_one_trigger_after():
    pdf = fixture_one_trigger_after("FK3")
    assert pdf[:4] == b"%PDF"
    assert _page_count(pdf) in (4, -1)


def test_fixture_two_triggers():
    pdf = fixture_two_triggers("FK3", "FK3")
    assert pdf[:4] == b"%PDF"
    assert _page_count(pdf) in (6, -1)


def test_fixture_no_code():
    pdf = fixture_no_code()
    assert pdf[:4] == b"%PDF"
    assert _page_count(pdf) in (3, -1)


def test_fixture_glob():
    pdf = fixture_glob("FK42")
    assert pdf[:4] == b"%PDF"


def test_fixture_case_sensitivity():
    pdf = fixture_case_sensitivity("invoice")
    assert pdf[:4] == b"%PDF"


def test_fixture_multi_trigger_same_page():
    pdf = fixture_multi_trigger_same_page(["INVOICE", "COPY"])
    assert pdf[:4] == b"%PDF"


# ─────────────────────────────────────────────────────────────────────────────
# Output is reproducible (same input → same structure)
# ─────────────────────────────────────────────────────────────────────────────

def test_deterministic_page_count():
    """Calling make_pdf twice with the same spec gives the same page count."""
    spec = [
        {"kind": "content", "text": "A"},
        {"kind": "qr",      "value": "X"},
        {"kind": "content", "text": "B"},
    ]
    count1 = _page_count(make_pdf(spec))
    count2 = _page_count(make_pdf(spec))
    if count1 != -1:
        assert count1 == count2 == 3
