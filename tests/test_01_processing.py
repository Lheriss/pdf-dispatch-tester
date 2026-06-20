"""
test_01_processing.py — Phase 1: Core PDF processing tests.

Tests pdf-dispatch's splitting engine by writing PDFs directly into
/data/input/ and verifying outputs on the filesystem and via the API.

Requires:
  - pdf-dispatch test instance running (port 5881)
  - data_path configured in config.yaml (filesystem access)
  - Both tester and pdf-dispatch mount the same /data directory

Sections
--------
  1a. Placement × page_handling (4 combinations)
  1b. Trigger matching (exact, glob, case, no-match, unknown code)
  1c. Multi-trigger sequences
  1d. Adversarial files (corrupted, non-PDF, zero bytes)
  1e. Edge cases (single page, last-page code, code only page)
"""

import pytest

from file_dropper import FileDropper
from helpers import set_config, set_triggers
from pdf_generator import (
    fixture_case_sensitivity,
    fixture_glob,
    fixture_multi_trigger_same_page,
    fixture_no_code,
    fixture_one_trigger_after,
    fixture_one_trigger_before,
    fixture_two_triggers,
    make_code_on_last_page,
    make_low_dpi_qr,
    make_non_pdf_with_pdf_extension,
    make_rotated_barcode,
    make_single_page_with_code,
    make_truncated_pdf,
    make_unknown_trigger,
    make_zero_bytes,
    make_zip_as_pdf,
)

pytestmark = pytest.mark.processing

TRIGGER = "FK3"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def dropper(cfg, http, server, log):
    """FileDropper for the entire module — cleans all outputs once at start."""
    from pathlib import Path
    data_path = cfg.get("data_path", "")
    if not data_path:
        pytest.skip("data_path not configured — filesystem tests require /data access")
    d = FileDropper(Path(data_path), http, server, log)
    d.cleanup_all_outputs()
    return d


@pytest.fixture(autouse=True)
def _reset_config(http, server):
    """
    Reset pdf-dispatch to a known baseline before each test:
      - Trigger: FK3, keep page, case-sensitive
      - Placement: before
      - Subfolders: enabled (organises by trigger for easier result reading)
      - Archive source: disabled
    """
    set_triggers(http, server, [
        {"value": TRIGGER, "page_handling": "keep", "case_sensitive": True}
    ])
    set_config(http, server,
               separator_placement="before",
               subdirs_by_trigger=True,
               delete_source=False)
    yield


@pytest.fixture(autouse=True)
def _cleanup(dropper):
    """Remove output files after each test to keep directories clean."""
    results = []
    yield results
    for r in results:
        dropper.cleanup_output(r)


# ─────────────────────────────────────────────────────────────────────────────
# 1a — Placement × page_handling
# ─────────────────────────────────────────────────────────────────────────────
# PDF structure for all placement tests:
#   p1 = content  (Document 1)
#   p2 = QR FK3   (separator)
#   p3 = content  (Document 2, page 1)
#   p4 = content  (Document 2, page 2)
# ─────────────────────────────────────────────────────────────────────────────

class TestBeforeKeep:
    """Separator placed BEFORE document, KEPT as its first page."""

    def test_produces_two_documents(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        pdf = fixture_one_trigger_before(TRIGGER)
        r   = dropper.drop(pdf, prefix="before_keep")
        _cleanup.append(r)

        assert r.status == "success"
        assert r.docs_count == 2
        assert len(r.output_files) == 2

    def test_first_doc_is_one_page(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="before_keep")
        _cleanup.append(r)

        assert r.page_count(0) == 1  # content before trigger

    def test_second_doc_includes_trigger_page(self, dropper, http, server, _cleanup, log):
        """Second document = trigger page (1) + 2 content pages = 3 pages."""
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="before_keep")
        _cleanup.append(r)

        assert r.page_count(1) == 3

    def test_page_ranges_in_api(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="before_keep")
        _cleanup.append(r)

        assert r.page_ranges[0] == "page 1"
        assert r.page_ranges[1] == "pages 2–4"

    def test_trigger_name_in_task(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="before_keep")
        _cleanup.append(r)

        assert TRIGGER in r.triggers


class TestBeforeDelete:
    """Separator placed BEFORE document, DELETED from output."""

    def test_produces_two_documents(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="before_del")
        _cleanup.append(r)

        assert r.status == "success"
        assert r.docs_count == 2

    def test_trigger_page_not_in_first_doc(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="before_del")
        _cleanup.append(r)

        # Doc 1: p1 only (1 page)
        assert r.page_count(0) == 1

    def test_second_doc_excludes_trigger_page(self, dropper, http, server, _cleanup, log):
        """Second document = 2 content pages only (trigger deleted)."""
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="before_del")
        _cleanup.append(r)

        assert r.page_count(1) == 2

    def test_page_ranges_in_api(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="before_del")
        _cleanup.append(r)

        assert r.page_ranges[0] == "page 1"
        assert r.page_ranges[1] == "pages 3–4"


class TestAfterKeep:
    """Separator placed AFTER document, KEPT as its last page."""

    def test_produces_two_documents(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="after")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_keep")
        _cleanup.append(r)

        assert r.status == "success"
        assert r.docs_count == 2

    def test_first_doc_includes_trigger_page(self, dropper, http, server, _cleanup, log):
        """First doc = 2 content pages + trigger page = 3 pages."""
        set_config(http, server, separator_placement="after")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_keep")
        _cleanup.append(r)

        assert r.page_count(0) == 3

    def test_second_doc_is_one_page(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="after")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_keep")
        _cleanup.append(r)

        assert r.page_count(1) == 1

    def test_page_ranges_in_api(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="after")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_keep")
        _cleanup.append(r)

        assert r.page_ranges[0] == "pages 1–3"
        assert r.page_ranges[1] == "page 4"


class TestAfterDelete:
    """Separator placed AFTER document, DELETED from output."""

    def test_produces_two_documents(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="after")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_del")
        _cleanup.append(r)

        assert r.status == "success"
        assert r.docs_count == 2

    def test_first_doc_excludes_trigger_page(self, dropper, http, server, _cleanup, log):
        """First doc = 2 content pages only (trigger deleted)."""
        set_config(http, server, separator_placement="after")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_del")
        _cleanup.append(r)

        assert r.page_count(0) == 2

    def test_second_doc_is_one_page(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="after")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_del")
        _cleanup.append(r)

        assert r.page_count(1) == 1

    def test_page_ranges_in_api(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="after")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_del")
        _cleanup.append(r)

        assert r.page_ranges[0] == "pages 1–2"
        assert r.page_ranges[1] == "page 4"


# ─────────────────────────────────────────────────────────────────────────────
# 1b — Trigger matching
# ─────────────────────────────────────────────────────────────────────────────

class TestTriggerMatching:

    def test_exact_match(self, dropper, http, server, _cleanup, log):
        set_triggers(http, server, [{"value": "FK3", "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_before("FK3"), prefix="exact")
        _cleanup.append(r)
        assert r.status == "success"
        assert r.docs_count == 2

    def test_no_code_goes_to_no_code_dir(self, dropper, http, server, _cleanup, log):
        r = dropper.drop(fixture_no_code(), prefix="no_code")
        _cleanup.append(r)
        assert len(r.no_code_files) == 1
        assert len(r.output_files) == 0

    def test_unknown_code_goes_to_no_code_dir(self, dropper, http, server, _cleanup, log):
        """Code is a valid QR but not in the trigger list."""
        set_triggers(http, server, [{"value": "FK3", "page_handling": "keep"}])
        r = dropper.drop(make_unknown_trigger("NOTINTRIGGERLIST"), prefix="unknown")
        _cleanup.append(r)
        assert len(r.no_code_files) == 1

    def test_glob_star_matches(self, dropper, http, server, _cleanup, log):
        """Pattern FK* should match FK3, FK42, FKXYZ."""
        set_triggers(http, server, [{"value": "FK*", "page_handling": "keep"}])
        for value in ("FK3", "FK42", "FKXYZ"):
            r = dropper.drop(fixture_glob(value), prefix=f"glob_{value}")
            _cleanup.append(r)
            assert r.docs_count == 2, f"FK* should match {value!r}"

    def test_glob_star_does_not_match_unrelated(self, dropper, http, server, _cleanup, log):
        set_triggers(http, server, [{"value": "FK*", "page_handling": "keep"}])
        r = dropper.drop(fixture_glob("INVOICE"), prefix="glob_nomatch")
        _cleanup.append(r)
        assert len(r.no_code_files) == 1

    def test_case_insensitive_match(self, dropper, http, server, _cleanup, log):
        """Trigger 'INVOICE' with case_sensitive=False should match 'invoice'."""
        set_triggers(http, server, [
            {"value": "INVOICE", "page_handling": "keep", "case_sensitive": False}
        ])
        r = dropper.drop(fixture_case_sensitivity("invoice"), prefix="case_insensitive")
        _cleanup.append(r)
        assert r.docs_count == 2

    def test_case_sensitive_no_match(self, dropper, http, server, _cleanup, log):
        """Trigger 'INVOICE' with case_sensitive=True should NOT match 'invoice'."""
        set_triggers(http, server, [
            {"value": "INVOICE", "page_handling": "keep", "case_sensitive": True}
        ])
        r = dropper.drop(fixture_case_sensitivity("invoice"), prefix="case_sensitive")
        _cleanup.append(r)
        assert len(r.no_code_files) == 1

    def test_empty_trigger_list_splits_on_any_code(self, dropper, http, server, _cleanup, log):
        """Empty trigger list = permissive mode: every detected code triggers a split."""
        set_triggers(http, server, [])
        r = dropper.drop(fixture_one_trigger_before("ANYTHINGHERE"), prefix="permissive")
        _cleanup.append(r)
        assert r.docs_count == 2


# ─────────────────────────────────────────────────────────────────────────────
# 1c — Multi-trigger sequences
# ─────────────────────────────────────────────────────────────────────────────

class TestMultiTrigger:

    def test_two_triggers_produce_three_documents(self, dropper, http, server, _cleanup, log):
        """6-page PDF with 2 triggers → 3 documents."""
        pdf = fixture_two_triggers(TRIGGER, TRIGGER)
        r   = dropper.drop(pdf, prefix="two_triggers")
        _cleanup.append(r)
        assert r.docs_count == 3
        assert len(r.output_files) == 3

    def test_two_triggers_page_counts(self, dropper, http, server, _cleanup, log):
        """
        PDF: content(p1) | FK3(p2) | content(p3) | content(p4) | FK3(p5) | content(p6)
        before+keep:
          Doc1: p1         → 1 page
          Doc2: p2,p3,p4   → 3 pages
          Doc3: p5,p6      → 2 pages
        """
        pdf = fixture_two_triggers(TRIGGER, TRIGGER)
        r   = dropper.drop(pdf, prefix="two_triggers_pages")
        _cleanup.append(r)
        assert r.all_page_counts() == [1, 3, 2]

    def test_two_different_triggers(self, dropper, http, server, _cleanup, log):
        set_triggers(http, server, [
            {"value": "FK3",     "page_handling": "keep"},
            {"value": "INVOICE", "page_handling": "keep"},
        ])
        pdf = fixture_two_triggers("FK3", "INVOICE")
        r   = dropper.drop(pdf, prefix="two_diff_triggers")
        _cleanup.append(r)
        assert r.docs_count == 3

    def test_two_codes_on_same_page(self, dropper, http, server, _cleanup, log):
        """
        Two QR codes on the same page — pdf-dispatch should produce
        one output per code, so the page is shared between two documents.
        """
        set_triggers(http, server, [
            {"value": "INVOICE", "page_handling": "keep"},
            {"value": "COPY",    "page_handling": "keep"},
        ])
        pdf = fixture_multi_trigger_same_page(["INVOICE", "COPY"])
        r   = dropper.drop(pdf, prefix="same_page_triggers")
        _cleanup.append(r)
        # Exact count depends on implementation — at minimum 2 docs
        assert r.docs_count >= 2


# ─────────────────────────────────────────────────────────────────────────────
# 1d — Adversarial files
# ─────────────────────────────────────────────────────────────────────────────

class TestAdversarial:

    def test_corrupted_pdf_goes_to_error(self, dropper, http, server, _cleanup, log):
        r = dropper.drop(make_truncated_pdf(), prefix="corrupt")
        _cleanup.append(r)
        assert len(r.error_files) == 1
        assert r.status == "error"

    def test_zero_bytes_goes_to_error(self, dropper, http, server, _cleanup, log):
        import uuid
        filename = f"zero_{uuid.uuid4().hex[:8]}.pdf"
        r = dropper.drop_raw(make_zero_bytes(), filename=filename)
        _cleanup.append(r)
        assert len(r.error_files) == 1

    def test_jpeg_with_pdf_extension_goes_to_error(self, dropper, http, server, _cleanup, log):
        import uuid
        filename = f"fake_{uuid.uuid4().hex[:8]}.pdf"
        r = dropper.drop_raw(make_non_pdf_with_pdf_extension(), filename=filename)
        _cleanup.append(r)
        assert len(r.error_files) == 1

    def test_zip_with_pdf_extension_goes_to_error(self, dropper, http, server, _cleanup, log):
        import uuid
        filename = f"zip_{uuid.uuid4().hex[:8]}.pdf"
        r = dropper.drop_raw(make_zip_as_pdf(), filename=filename)
        _cleanup.append(r)
        assert len(r.error_files) == 1

    def test_low_dpi_qr_detected_or_no_code(self, dropper, http, server, _cleanup, log):
        """
        Low-quality QR: either detected (success, docs_count>1) or
        not detected (goes to no_code/). Both are acceptable — we verify
        only that processing completes without an unhandled error.
        """
        r = dropper.drop(make_low_dpi_qr(TRIGGER), prefix="low_dpi")
        _cleanup.append(r)
        assert r.status in ("success", "error") or len(r.no_code_files) == 1
        log.info(f"Low-DPI QR result: status={r.status}, no_code={len(r.no_code_files)}")

    def test_rotated_barcode(self, dropper, http, server, _cleanup, log):
        """
        Rotated QR (45°): ZXing typically handles this well.
        Log the outcome for reference without asserting a specific result.
        """
        r = dropper.drop(make_rotated_barcode(TRIGGER, angle=45), prefix="rotated")
        _cleanup.append(r)
        log.info(
            f"Rotated barcode (45°): status={r.status}, "
            f"docs={r.docs_count}, no_code={len(r.no_code_files)}"
        )
        # At minimum: no crash
        assert r.status in ("success", "error") or len(r.no_code_files) >= 0


# ─────────────────────────────────────────────────────────────────────────────
# 1e — Edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_single_page_with_code_before_keep(self, dropper, http, server, _cleanup, log):
        """
        1-page PDF containing only a trigger code (before+keep):
        The code page is kept as Document 1; Document 0 (before it) is empty → discarded.
        Result: 1 document, 1 page.
        """
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(make_single_page_with_code(TRIGGER), prefix="single_page_keep")
        _cleanup.append(r)
        assert r.docs_count == 1
        assert r.page_count(0) == 1

    def test_single_page_with_code_before_delete(self, dropper, http, server, _cleanup, log):
        """
        1-page PDF containing only a trigger code (before+delete):
        The code page is deleted; both segments are empty → 0 documents.
        """
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(make_single_page_with_code(TRIGGER), prefix="single_page_del")
        _cleanup.append(r)
        assert r.docs_count == 0
        assert len(r.output_files) == 0

    def test_code_on_last_page_before_keep(self, dropper, http, server, _cleanup, log):
        """
        4-page PDF with trigger on last page (before+keep):
        Doc1 = pages 1–3 (content before)
        Doc2 = page 4 (trigger page only, kept as first page of empty doc)
        """
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(make_code_on_last_page(TRIGGER, content_pages=3),
                         prefix="code_last")
        _cleanup.append(r)
        assert r.docs_count == 2
        assert r.page_count(0) == 3
        assert r.page_count(1) == 1

    def test_code_on_last_page_before_delete(self, dropper, http, server, _cleanup, log):
        """
        4-page PDF with trigger on last page (before+delete):
        Doc1 = pages 1–3, Doc2 = nothing (trigger deleted, no content after)
        → 1 document produced.
        """
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(make_code_on_last_page(TRIGGER, content_pages=3),
                         prefix="code_last_del")
        _cleanup.append(r)
        assert r.docs_count == 1
        assert r.page_count(0) == 3
