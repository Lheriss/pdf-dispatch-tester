"""
test_01_processing.py — Phase 1: Core PDF processing tests.

Tests pdf-dispatch's splitting engine by writing PDFs directly into
/data/input/ and verifying outputs on the filesystem and via the
activity log (GET /api/state).

NOTE on task detection
----------------------
Files processed by the watchdog do NOT appear in GET /api/tasks — that
endpoint only tracks files uploaded via POST /api/upload. The FileDropper
therefore detects completion by watching /data/input/ for the file to
disappear, then parses /api/state events to reconstruct status, docs_count,
page_ranges and triggers.

NOTE on output structure
------------------------
When a document has no trigger code (e.g. the content before a "before"
trigger), it goes to output/no_code/ rather than a trigger subfolder.
r.output_files  = files in trigger subfolders (FK3/, INVOICE/, …)
r.no_code_files = files in output/no_code/
r.all_docs      = output_files + no_code_files, sorted chronologically
r.page_count_of(n) reads the Nth document from all_docs.
"""

import uuid

import pytest

from file_dropper import FileDropper
from helpers import set_config, set_triggers
from pdf_generator import (
    fixture_case_sensitivity,
    make_pdf,
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
    """FileDropper for the entire module."""
    from pathlib import Path
    data_path = cfg.get("data_path", "")
    if not data_path:
        pytest.skip("data_path not configured — filesystem tests require /data access")
    return FileDropper(Path(data_path), http, server, log)


@pytest.fixture(autouse=True)
def _reset_config(http, server):
    """Reset pdf-dispatch to known baseline before each test."""
    set_triggers(http, server, [
        {"value": TRIGGER, "page_handling": "keep", "case_sensitive": True}
    ])
    set_config(http, server,
               separator_placement="before",
               subdirs_by_trigger=True,
               delete_source=False)
    yield


@pytest.fixture(autouse=True)
def _cleanup(dropper, request):
    """
    Collect output files and clean them up ONLY on test pass.
    On failure, files are kept for manual inspection.
    """
    results = []
    yield results
    if hasattr(request.node, "rep_call") and request.node.rep_call.passed:
        for r in results:
            dropper.cleanup_output(r)


# ─────────────────────────────────────────────────────────────────────────────
# 1a — Placement × page_handling
# ─────────────────────────────────────────────────────────────────────────────
# PDF: p1=content | p2=QR FK3 | p3=content | p4=content
# ─────────────────────────────────────────────────────────────────────────────

class TestBeforeKeep:
    """Separator placed BEFORE document, KEPT as its first page."""

    def test_produces_two_documents(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="before_keep")
        _cleanup.append(r)
        assert r.status == "success"
        assert r.docs_count == 2

    def test_first_doc_is_one_page(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="before_keep")
        _cleanup.append(r)
        assert r.page_count_of(0) == 1

    def test_second_doc_includes_trigger_page(self, dropper, http, server, _cleanup, log):
        """Second document = trigger page (1) + 2 content pages = 3 pages."""
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="before_keep")
        _cleanup.append(r)
        assert r.page_count_of(1) == 3

    def test_page_ranges_in_api(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="before_keep")
        _cleanup.append(r)
        assert r.page_ranges[0] == "page 1"
        assert r.page_ranges[1] == "pages 2\u20134"

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
        assert r.page_count_of(0) == 1

    def test_second_doc_excludes_trigger_page(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="before_del")
        _cleanup.append(r)
        assert r.page_count_of(1) == 2

    def test_page_ranges_in_api(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="before")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="before_del")
        _cleanup.append(r)
        assert r.page_ranges[0] == "page 1"
        assert r.page_ranges[1] == "pages 3\u20134"


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
        set_config(http, server, separator_placement="after")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_keep")
        _cleanup.append(r)
        assert r.page_count_of(0) == 3

    def test_second_doc_is_one_page(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="after")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_keep")
        _cleanup.append(r)
        assert r.page_count_of(1) == 1

    def test_page_ranges_in_api(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="after")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_keep")
        _cleanup.append(r)
        assert r.page_ranges[0] == "pages 1\u20133"
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
        set_config(http, server, separator_placement="after")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_del")
        _cleanup.append(r)
        assert r.page_count_of(0) == 2

    def test_second_doc_is_one_page(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="after")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_del")
        _cleanup.append(r)
        assert r.page_count_of(1) == 1

    def test_page_ranges_in_api(self, dropper, http, server, _cleanup, log):
        set_config(http, server, separator_placement="after")
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_del")
        _cleanup.append(r)
        assert r.page_ranges[0] == "pages 1\u20132"
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
        set_triggers(http, server, [{"value": "FK3", "page_handling": "keep"}])
        r = dropper.drop(make_unknown_trigger("NOTINTRIGGERLIST"), prefix="unknown")
        _cleanup.append(r)
        assert len(r.no_code_files) == 1

    def test_glob_star_matches(self, dropper, http, server, _cleanup, log):
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
        set_triggers(http, server, [
            {"value": "INVOICE", "page_handling": "keep", "case_sensitive": False}
        ])
        r = dropper.drop(fixture_case_sensitivity("invoice"), prefix="case_insensitive")
        _cleanup.append(r)
        assert r.docs_count == 2

    def test_case_sensitive_no_match(self, dropper, http, server, _cleanup, log):
        set_triggers(http, server, [
            {"value": "INVOICE", "page_handling": "keep", "case_sensitive": True}
        ])
        r = dropper.drop(fixture_case_sensitivity("invoice"), prefix="case_sensitive")
        _cleanup.append(r)
        assert len(r.no_code_files) == 1

    def test_empty_trigger_list_splits_on_any_code(self, dropper, http, server, _cleanup, log):
        set_triggers(http, server, [])
        r = dropper.drop(fixture_one_trigger_before("ANYTHINGHERE"), prefix="permissive")
        _cleanup.append(r)
        assert r.docs_count == 2


# ─────────────────────────────────────────────────────────────────────────────
# 1c — Multi-trigger sequences
# ─────────────────────────────────────────────────────────────────────────────

class TestMultiTrigger:

    def test_two_triggers_produce_three_documents(self, dropper, http, server, _cleanup, log):
        r = dropper.drop(fixture_two_triggers(TRIGGER, TRIGGER), prefix="two_triggers")
        _cleanup.append(r)
        assert r.docs_count == 3

    def test_two_triggers_page_counts(self, dropper, http, server, _cleanup, log):
        """
        PDF: content(p1) | FK3(p2) | content(p3,p4) | FK3(p5) | content(p6)
        before+keep: doc1=1p, doc2=3p, doc3=2p
        """
        r = dropper.drop(fixture_two_triggers(TRIGGER, TRIGGER), prefix="two_triggers_pages")
        _cleanup.append(r)
        assert r.all_page_counts() == [1, 3, 2]

    def test_two_different_triggers(self, dropper, http, server, _cleanup, log):
        set_triggers(http, server, [
            {"value": "FK3",     "page_handling": "keep"},
            {"value": "INVOICE", "page_handling": "keep"},
        ])
        r = dropper.drop(fixture_two_triggers("FK3", "INVOICE"), prefix="two_diff_triggers")
        _cleanup.append(r)
        assert r.docs_count == 3

    def test_two_codes_on_same_page(self, dropper, http, server, _cleanup, log):
        set_triggers(http, server, [
            {"value": "INVOICE", "page_handling": "keep"},
            {"value": "COPY",    "page_handling": "keep"},
        ])
        r = dropper.drop(fixture_multi_trigger_same_page(["INVOICE", "COPY"]),
                         prefix="same_page_triggers")
        _cleanup.append(r)
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
        """
        Zero-byte file triggers a stabilization timeout (15s) in pdf-dispatch.
        Since fix 262f600d, the file is moved to output/error/ instead of being
        left in input/.  Verified: exactly 1 error file, no output produced.
        """
        filename = f"zero_{uuid.uuid4().hex[:8]}.pdf"
        r = dropper.drop_raw(make_zero_bytes(), filename=filename)
        _cleanup.append(r)
        assert r.status == "error"
        assert len(r.output_files) == 0
        assert len(r.no_code_files) == 0
        assert len(r.error_files) == 1, (
            f"Expected zero-byte file in error/, got {r.error_files}"
        )

    def test_jpeg_with_pdf_extension_goes_to_error(self, dropper, http, server, _cleanup, log):
        filename = f"fake_{uuid.uuid4().hex[:8]}.pdf"
        r = dropper.drop_raw(make_non_pdf_with_pdf_extension(), filename=filename)
        _cleanup.append(r)
        assert len(r.error_files) == 1

    def test_zip_with_pdf_extension_goes_to_error(self, dropper, http, server, _cleanup, log):
        filename = f"zip_{uuid.uuid4().hex[:8]}.pdf"
        r = dropper.drop_raw(make_zip_as_pdf(), filename=filename)
        _cleanup.append(r)
        assert len(r.error_files) == 1

    def test_oversized_pages_via_drop_goes_to_error(self, dropper, http, server, _cleanup, log):
        """
        File-drop path: PDF with 60 pages exceeds MAX_PAGES=50.
        process_file() must detect this BEFORE DPI rendering and move
        the file to output/error/.  Validates that the limit is active
        on the watchdog path, not just on the API upload path.
        """
        pages = [{"kind": "content", "text": f"Page {i}"} for i in range(60)]
        r = dropper.drop(make_pdf(pages), prefix="bigpages")
        _cleanup.append(r)
        assert r.status == "error", (
            f"60-page PDF must be rejected by MAX_PAGES=50 guard "
            f"(got status={r.status!r}, error_files={r.error_files})"
        )
        assert len(r.error_files) == 1, (
            f"Expected 1 file in error/, got {r.error_files}"
        )
        assert len(r.output_files) == 0

    def test_oversized_size_via_drop_goes_to_error(self, dropper, http, server, _cleanup, log):
        """
        File-drop path: file > MAX_UPLOAD_MB=20 must be moved to error/.
        Sends 21 MB (PDF header + null bytes — size matters, not content).
        process_file() checks size after wait_until_stable(), before any
        DPI rendering.  This is the same limit enforced by /api/upload but
        now active for scanner deposits and email attachments too.
        """
        oversized = b"%PDF-1.4\n" + b"\x00" * (21 * 1024 * 1024)
        filename = f"big_{uuid.uuid4().hex[:8]}.pdf"
        r = dropper.drop_raw(oversized, filename=filename)
        _cleanup.append(r)
        assert r.status == "error", (
            f"21 MB file must be rejected by MAX_UPLOAD_MB=20 guard "
            f"(got status={r.status!r})"
        )
        assert len(r.error_files) == 1
        assert len(r.output_files) == 0

    def test_low_dpi_qr_detected_or_no_code(self, dropper, http, server, _cleanup, log):
        r = dropper.drop(make_low_dpi_qr(TRIGGER), prefix="low_dpi")
        _cleanup.append(r)
        log.info(f"Low-DPI QR: status={r.status}, docs={r.docs_count}, "
                 f"no_code={len(r.no_code_files)}")

    def test_rotated_barcode(self, dropper, http, server, _cleanup, log):
        r = dropper.drop(make_rotated_barcode(TRIGGER, angle=45), prefix="rotated")
        _cleanup.append(r)
        log.info(f"Rotated (45°): status={r.status}, docs={r.docs_count}")


# ─────────────────────────────────────────────────────────────────────────────
# 1e — Edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_single_page_with_code_before_keep(self, dropper, http, server, _cleanup, log):
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(make_single_page_with_code(TRIGGER), prefix="single_page_keep")
        _cleanup.append(r)
        assert r.docs_count == 1
        assert r.page_count_of(0) == 1

    def test_single_page_with_code_before_delete(self, dropper, http, server, _cleanup, log):
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(make_single_page_with_code(TRIGGER), prefix="single_page_del")
        _cleanup.append(r)
        assert r.docs_count == 0
        assert len(r.output_files) == 0

    def test_code_on_last_page_before_keep(self, dropper, http, server, _cleanup, log):
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
        r = dropper.drop(make_code_on_last_page(TRIGGER, content_pages=3),
                         prefix="code_last")
        _cleanup.append(r)
        assert r.docs_count == 2
        assert r.page_count_of(0) == 3
        assert r.page_count_of(1) == 1

    def test_code_on_last_page_before_delete(self, dropper, http, server, _cleanup, log):
        set_triggers(http, server, [{"value": TRIGGER, "page_handling": "delete"}])
        r = dropper.drop(make_code_on_last_page(TRIGGER, content_pages=3),
                         prefix="code_last_del")
        _cleanup.append(r)
        assert r.docs_count == 1
        assert r.page_count_of(0) == 3

