"""
test_04_email.py — Phase 4: Email (IMAP) input path.

Tests the full email pipeline:
  sender → Greenmail (SMTP) → pdf-dispatch polls Greenmail (IMAPS) → /data/input/
  → watchdog → process_file() → /data/output/

Timing strategy: creating a fresh email config sets last_poll=0, so
pdf-dispatch's background poller (wake cycle: 30 s) runs the first
IMAP scan within ≤30 s.  No manual trigger endpoint needed.

Greenmail connection (defined in docker-compose.test.yml):
  SMTP : greenmail:3025  (plain, no auth)
  IMAPS: greenmail:3993  (SSL, self-signed → verify_ssl=False)
  user : pdftester@test.local / pdftester
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import requests

from helpers import (
    EmailDropResult,
    get_config,
    send_email,
    set_config,
    set_triggers,
    snapshot_output,
    wait_for_new_output,
    upload_and_wait,
)
from pdf_generator import make_pdf

# ── Shared constants ──────────────────────────────────────────────────────────

TRIGGER        = "FK3"          # same as Phase 1 / Phase 2
_SMTP_HOST     = "greenmail"
_SMTP_PORT     = 3025
_IMAP_HOST     = "greenmail"
_IMAP_PORT     = 3143  # plain IMAP (Greenmail; pdf-dispatch uses use_ssl=False)
_IMAP_USER     = "pdftester"         # IMAP login (local-part only in Greenmail)
_EMAIL_ADDR    = "pdftester@greenmail"  # SMTP To: address
_PASSWORD      = "pdftester"
_FROM          = "sender@greenmail"
_OTHER_SENDER  = "other@external.com"

# ── Module-level fixtures ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def data_dir(cfg) -> Path:
    p = cfg.get("data_path", "")
    if not p:
        pytest.skip("data_path not configured — Phase 4 requires /data access")
    return Path(p)


@pytest.fixture(autouse=True)
def _reset_config(http, server):
    """Restore a clean pdf-dispatch state before each test in this module."""
    # Clear email configs at start too — they persist on disk between runs
    _state = http.get(f"{server}/api/state").json()
    for _ec in _state.get("app_config", {}).get("email_configs", []):
        http.delete(f"{server}/api/email/configs/{_ec['id']}")
    set_triggers(http, server, [{"value": TRIGGER, "page_handling": "keep"}])
    set_config(http, server,
               separator_placement="before",
               delete_source=False,
               archive_source=False)
    yield
    # cleanup email configs
    r = http.get(f"{server}/api/state")
    for ec in r.json().get("app_config", {}).get("email_configs", []):
        http.delete(f"{server}/api/email/configs/{ec['id']}")


# ── helpers ───────────────────────────────────────────────────────────────────

def _plain_pdf() -> bytes:
    return make_pdf([{"kind": "content", "text": "Email test page"}])


def _triggered_pdf() -> bytes:
    return make_pdf([
        {"kind": "qr",      "value": TRIGGER},
        {"kind": "content", "text": "Document body"},
    ])


def _make_email_config(
    action: str = "read",
    default_trigger: str | None = None,
    filter_from: str = "",
    filter_subject: str = "",
) -> dict:
    payload: dict = {
        "name":          "phase4-greenmail",
        "enabled":       True,
        "host":          _IMAP_HOST,
        "port":          _IMAP_PORT,
        "username":      _IMAP_USER,
        "password":      _PASSWORD,
        "folder":        "INBOX",
        "verify_ssl":    False,
        "use_ssl":       False,  # plain IMAP — Greenmail in Docker
        "action":        action,
        "poll_interval": 1,
    }
    if default_trigger:
        payload["default_trigger"] = default_trigger
    if filter_from:
        payload["filter_from"]    = filter_from
    if filter_subject:
        payload["filter_subject"] = filter_subject
    return payload


def _send(attachments, subject="PDF test", from_addr=_FROM):
    if isinstance(attachments, bytes):
        attachments = [(attachments, "test.pdf")]
    send_email(_SMTP_HOST, _SMTP_PORT, from_addr, _EMAIL_ADDR, subject, attachments)


def _create_config(http, server, **kwargs) -> dict:
    """POST /api/email/configs and return response."""
    r = http.post(f"{server}/api/email/configs",
                  json=_make_email_config(**kwargs))
    r.raise_for_status()
    return r.json()


def _poll_result(http, server, data_dir, before, timeout=60.0) -> EmailDropResult:
    """Create fresh email config (triggers immediate poll) then wait for output."""
    _create_config(http, server)
    return wait_for_new_output(data_dir, before, timeout=timeout)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4a — Email configuration API  (no Greenmail connection needed)
# ─────────────────────────────────────────────────────────────────────────────

class TestEmailConfigAPI:
    """Verify /api/email/configs CRUD operations."""

    def test_create_config(self, http, server):
        r = http.post(f"{server}/api/email/configs",
                      json=_make_email_config())
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok"), f"Expected ok=True, got: {body}"
        configs = body.get("email_configs", [])
        # find our config by name (list may contain other configs)
        ec = next((c for c in configs if c.get("name") == "phase4-greenmail"), None)
        assert ec is not None, f"Created config not found in response: {configs}"
        assert ec["host"]     == _IMAP_HOST
        assert ec["username"] == _IMAP_USER
        assert ec["action"]   == "read"
        assert "id" in ec

    def test_config_appears_in_state(self, http, server):
        _create_config(http, server)
        state = http.get(f"{server}/api/state").json()
        configs = state["app_config"].get("email_configs", [])
        assert len(configs) == 1
        assert configs[0]["host"] == _IMAP_HOST

    def test_duplicate_name_rejected(self, http, server):
        _create_config(http, server)
        r = http.post(f"{server}/api/email/configs",
                      json=_make_email_config())   # same name
        assert r.status_code == 400
        assert "errors" in r.json() or not r.json().get("ok")

    def test_update_config(self, http, server):
        body = _create_config(http, server)
        configs = body.get("email_configs", [])
        ec = next((c for c in configs if c.get("name") == "phase4-greenmail"), None)
        assert ec, f"Config not in response: {configs}"
        ec_id = ec["id"]
        updated = _make_email_config(action="delete")
        updated["id"] = ec_id
        r = http.post(f"{server}/api/email/configs/{ec_id}", json=updated)
        assert r.status_code == 200
        configs = r.json().get("email_configs", [])
        assert configs[0]["action"] == "delete"

    def test_delete_config(self, http, server):
        body = _create_config(http, server)
        configs = body.get("email_configs", [])
        ec = next((c for c in configs if c.get("name") == "phase4-greenmail"), None)
        assert ec, f"Config not in response: {configs}"
        ec_id = ec["id"]
        r = http.delete(f"{server}/api/email/configs/{ec_id}")
        assert r.status_code == 200
        state = http.get(f"{server}/api/state").json()
        assert state["app_config"].get("email_configs", []) == []

    def test_connection_test_endpoint(self, http, server):
        """POST /api/email/test — verify pdf-dispatch can reach Greenmail."""
        payload = {
            "name":       "test-conn",
            "host":       _IMAP_HOST,
            "port":       _IMAP_PORT,
            "username":   _IMAP_USER,
            "password":   _PASSWORD,
            "folder":     "INBOX",
            "verify_ssl": False,
            "use_ssl":    False,
        }
        r = http.post(f"{server}/api/email/test", json=payload)
        assert r.status_code == 200, (
            f"IMAP connection test failed — is Greenmail running? "
            f"status={r.status_code} body={r.text[:200]}"
        )
        assert r.json().get("ok"), r.json()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4b — Full email processing pipeline
# ─────────────────────────────────────────────────────────────────────────────

class TestEmailProcessing:
    """
    End-to-end tests: email → Greenmail → IMAP poll → process_file() → output/.
    Each test sends via SMTP then creates a fresh email config so that
    pdf-dispatch polls within ≤30 s.
    """

    def test_plain_pdf_goes_to_no_code(self, http, server, data_dir):
        """PDF with no trigger code → output/no_code/."""
        before = snapshot_output(data_dir)
        _send(_plain_pdf())
        result = _poll_result(http, server, data_dir, before)
        assert result.status != "timeout", "No output after 60 s — check Greenmail is running"
        assert len(result.no_code_files) >= 1
        assert len(result.output_files) == 0

    def test_pdf_with_qr_trigger_goes_to_output(self, http, server, data_dir):
        """PDF containing the FK3 QR trigger → split and placed in output/."""
        before = snapshot_output(data_dir)
        _send(_triggered_pdf())
        result = _poll_result(http, server, data_dir, before)
        assert result.status != "timeout", "No output after 60 s"
        assert len(result.output_files) >= 1, (
            f"Expected at least 1 split document, got {result}"
        )

    def test_default_trigger_applied(self, http, server, data_dir):
        """PDF with no QR code + default_trigger configured → split as if triggered."""
        before = snapshot_output(data_dir)
        _send(_plain_pdf())
        _create_config(http, server, default_trigger=TRIGGER)
        result = wait_for_new_output(data_dir, before, timeout=60.0)
        assert result.status != "timeout", "No output after 60 s"
        # With default_trigger the file is split and placed in output/ (not no_code/)
        assert len(result.output_files) >= 1, (
            "PDF with default_trigger should be split, not sent to no_code"
        )

    def test_filter_from_accepts_matching_sender(self, http, server, data_dir):
        """filter_from='sender' — email from _FROM is processed."""
        before = snapshot_output(data_dir)
        _send(_plain_pdf(), from_addr=_FROM)
        _create_config(http, server, filter_from=_FROM.split("@")[0])
        result = wait_for_new_output(data_dir, before, timeout=60.0)
        assert result.status != "timeout", "Email matching filter_from was not processed"

    def test_filter_from_skips_other_sender(self, http, server, data_dir):
        """filter_from set but sender doesn't match → email not processed."""
        before = snapshot_output(data_dir)
        _send(_plain_pdf(), from_addr=_OTHER_SENDER)
        _create_config(http, server, filter_from="only-this-sender")
        # Poll will run within 30 s but must skip the email.
        # Wait 40 s then assert output is unchanged.
        time.sleep(40)
        after = snapshot_output(data_dir)
        new = after - before
        assert len(new) == 0, (
            f"Email from non-matching sender should have been filtered out, "
            f"but {len(new)} new file(s) appeared: {new}"
        )

    def test_filter_subject_skips_other_subject(self, http, server, data_dir):
        """filter_subject set but subject doesn't match → email not processed."""
        before = snapshot_output(data_dir)
        _send(_plain_pdf(), subject="Unrelated subject")
        _create_config(http, server, filter_subject="EXPECTED_KEYWORD")
        time.sleep(40)
        after = snapshot_output(data_dir)
        assert (after - before) == set(), (
            "Email with non-matching subject should not produce output"
        )

    def test_multiple_attachments_in_one_email(self, http, server, data_dir):
        """One email with 3 PDF attachments → 3 separate files processed."""
        pdfs = [
            (_plain_pdf(), f"doc_{i}.pdf")
            for i in range(3)
        ]
        before = snapshot_output(data_dir)
        _send(pdfs)
        result = _poll_result(http, server, data_dir, before, timeout=90.0)
        assert result.status != "timeout", "No output after 90 s"
        total = len(result.all_files)
        assert total == 3, (
            f"Expected 3 output files (one per attachment), got {total}: {result.all_files}"
        )

    def test_already_processed_email_not_reprocessed(self, http, server, data_dir):
        """Second poll of the same inbox must not produce duplicate output."""
        before = snapshot_output(data_dir)
        _send(_plain_pdf())
        result = _poll_result(http, server, data_dir, before, timeout=60.0)
        assert result.status != "timeout"
        first_count = len(result.all_files)

        # Delete and recreate config → fresh poll of same inbox
        r = http.get(f"{server}/api/state")
        for ec in r.json()["app_config"].get("email_configs", []):
            http.delete(f"{server}/api/email/configs/{ec['id']}")

        before2 = snapshot_output(data_dir)
        _create_config(http, server)
        time.sleep(40)   # let the poll run
        after2 = snapshot_output(data_dir)
        new2 = after2 - before2
        assert len(new2) == 0, (
            f"Already-processed email should not be re-processed "
            f"(got {len(new2)} new file(s))"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4c — Resource limits on email path
# ─────────────────────────────────────────────────────────────────────────────

class TestEmailLimits:
    """
    Verify that MAX_UPLOAD_MB and MAX_PAGES are enforced on email attachments.

    MAX_UPLOAD_MB=20 and MAX_PAGES=50 are set in docker-compose.test.yml.
    """

    def test_oversized_attachment_skipped(self, http, server, data_dir):
        """
        Attachment > MAX_UPLOAD_MB=20 is rejected inside _imap_process()
        BEFORE writing to /data/input/ — no file reaches the watchdog.
        Expect: nothing in output/ (timeout is normal here).
        """
        # 21 MB fake PDF (header + null bytes)
        oversized = b"%PDF-1.4\n" + b"\x00" * (21 * 1024 * 1024)
        before = snapshot_output(data_dir)
        _send([(oversized, "huge.pdf")])
        _create_config(http, server)
        # Wait longer than the poll cycle; nothing should appear
        time.sleep(40)
        after = snapshot_output(data_dir)
        assert (after - before) == set(), (
            "Oversized attachment must be skipped by _imap_process() "
            "before writing to /data/input/"
        )

    def test_oversized_pages_goes_to_error(self, http, server, data_dir):
        """
        PDF with 60 pages exceeds MAX_PAGES=50.  _imap_process() writes it
        to /data/input/; process_file() detects the page count BEFORE DPI
        rendering and moves it to output/error/.
        """
        pages  = [{"kind": "content", "text": f"Page {i}"} for i in range(60)]
        big    = make_pdf(pages)
        before = snapshot_output(data_dir)
        _send([(big, "bigpages.pdf")])
        result = _poll_result(http, server, data_dir, before, timeout=90.0)
        assert result.status != "timeout", (
            "60-page PDF should reach output/error/ — check process_file() guard"
        )
        assert len(result.error_files) >= 1, (
            f"Expected 1 file in error/, got {result}"
        )
        assert len(result.output_files) == 0
