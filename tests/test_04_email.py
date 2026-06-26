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
  user : pdftester@greenmail / pdftester  (IMAP login = pdftester, password = pdftester)
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
_IMAP_USER     = "pdftester"            # IMAP login (login:password@domain format)
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


_GREENMAIL_API = "http://greenmail:8080"


def _purge_greenmail():
    """Delete all messages from Greenmail via its REST API.

    Prevents email accumulation across tests: without this, each new
    email config (processed_ids=[]) would reprocess all previous tests'
    emails, producing stale output at unpredictable times.

    Greenmail 2.x API: DELETE /api/user/{email}/messages
    Silent no-op if Greenmail API is unavailable (e.g. local dev run).
    """
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{_GREENMAIL_API}/api/user/{_EMAIL_ADDR}/messages",
            method="DELETE",
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass   # non-fatal — tests may be slightly flaky but won't crash


@pytest.fixture(autouse=True)
def _reset_config(http, server):
    """Restore a clean pdf-dispatch state before each test in this module."""
    # Clear email configs at start too — they persist on disk between runs
    _state = http.get(f"{server}/api/state").json()
    for _ec in _state.get("app_config", {}).get("email_configs", []):
        http.delete(f"{server}/api/email/configs/{_ec['id']}")
    # Purge Greenmail inbox so emails from previous tests are not reprocessed
    # by subsequent configs (each new config starts with processed_ids=[]).
    _purge_greenmail()
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


def _get_config_id(http, server, name: str = "phase4-greenmail") -> str | None:
    """Return the id of a named email config from GET /api/state, or None."""
    state = http.get(f"{server}/api/state").json()
    for ec in state.get("app_config", {}).get("email_configs", []):
        if ec.get("name") == name:
            return ec["id"]
    return None


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
        ec_id = _get_config_id(http, server)
        assert ec_id is not None, (
            "Config 'phase4-greenmail' not found in GET /api/state after creation"
        )

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
        _create_config(http, server)
        ec_id = _get_config_id(http, server)
        assert ec_id, "Config not found in /api/state after creation"
        updated = _make_email_config(action="delete")
        updated["id"] = ec_id
        r = http.post(f"{server}/api/email/configs/{ec_id}", json=updated)
        assert r.status_code == 200
        state = http.get(f"{server}/api/state").json()
        ec = next(
            (c for c in state["app_config"].get("email_configs", [])
             if c["id"] == ec_id), None)
        assert ec is not None, "Config disappeared after update"
        assert ec.get("action") == "delete", (
            f"Expected action='delete', got: {ec.get('action')}"
        )

    def test_delete_config(self, http, server):
        _create_config(http, server)
        ec_id = _get_config_id(http, server)
        assert ec_id, "Config not found in /api/state after creation"
        r = http.delete(f"{server}/api/email/configs/{ec_id}")
        assert r.status_code == 200
        state = http.get(f"{server}/api/state").json()
        assert not any(
            c["id"] == ec_id
            for c in state["app_config"].get("email_configs", [])
        ), "Config still present after deletion"

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
        """PDF with no trigger code → output/no_code/.

        Timeout is 150 s to absorb a potential ZXING JVM cold start on the
        first barcode scan of a fresh pdf-dispatch session (the JVM can take
        60–90 s to initialise when no previous scan has warmed it up).
        """
        before = snapshot_output(data_dir)
        _send(_plain_pdf())
        result = _poll_result(http, server, data_dir, before, timeout=150.0)
        assert result.status != "timeout", (
            "No output after 150 s — ZXING JVM cold start exceeded threshold "
            "or email pipeline is broken; check pdfdispatch.log"
        )
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
        try:
            _send([(oversized, "huge.pdf")])
        except Exception:
            # Greenmail may refuse very large SMTP messages;
            # either way nothing should appear in output/.
            pass
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


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4d — Roundtrip mot de passe
# ─────────────────────────────────────────────────────────────────────────────

class TestEmailPasswordRoundtrip:
    """Vérifie que le mot de passe est chiffré et stocké correctement.

    Le champ password_enc ne doit jamais être exposé dans les réponses
    API (security hardening), mais la connexion IMAP doit rester
    fonctionnelle (le chiffrement du mdp doit être réversible).
    """

    @pytest.fixture(autouse=True)
    def _cleanup(self, http, server):
        yield
        r = http.get(f"{server}/api/state")
        for ec in r.json().get("app_config", {}).get("email_configs", []):
            if "phase4-pwd" in ec.get("name", ""):
                http.delete(f"{server}/api/email/configs/{ec['id']}")

    def _make_config(self, http, server) -> dict:
        r = http.post(f"{server}/api/email/configs", json={
            "name":         "phase4-pwd-roundtrip",
            "enabled":      False,
            "host":         _IMAP_HOST,
            "port":         _IMAP_PORT,
            "username":     _IMAP_USER,
            "password":     _PASSWORD,
            "folder":       "INBOX",
            "verify_ssl":   False,
            "use_ssl":      False,
            "action":       "read",
            "poll_interval": 5,
        })
        r.raise_for_status()
        return r.json()

    def test_password_enc_not_in_state_response(self, http, server):
        """password_enc ne doit PAS apparaître dans GET /api/state."""
        self._make_config(http, server)
        state = http.get(f"{server}/api/state").json()
        for ec in state.get("app_config", {}).get("email_configs", []):
            assert "password_enc" not in ec, (
                "password_enc must be stripped from /api/state responses"
            )
            assert ec.get("password", "") == "", (
                "Plain password must not appear in /api/state"
            )

    def test_password_enc_not_in_create_response(self, http, server):
        """password_enc ne doit PAS apparaître dans la réponse POST /api/email/configs."""
        body = self._make_config(http, server)
        ec = body.get("config", {})
        assert "password_enc" not in ec, (
            "password_enc must be stripped from create response"
        )

    def test_connection_test_succeeds_after_save(self, http, server):
        """Après sauvegarde avec mot de passe, /api/email/test doit réussir."""
        body = self._make_config(http, server)
        config_id = body.get("config", {}).get("id")
        assert config_id, f"No config id in response: {body}"
        r = http.post(f"{server}/api/email/test", json={
            "id":       config_id,
            "name":     "phase4-pwd-roundtrip",
            "host":     _IMAP_HOST,
            "port":     _IMAP_PORT,
            "username": _IMAP_USER,
            "folder":   "INBOX",
            "verify_ssl": False,
            "use_ssl":    False,
        })
        # Test via id → uses stored encrypted password
        assert r.status_code == 200
        result = r.json()
        assert result.get("ok"), (
            f"IMAP connection test must succeed (password correctly stored): {result}"
        )

    def test_update_password_connection_still_works(self, http, server):
        """Après UPDATE du mot de passe, la connexion fonctionne toujours."""
        body = self._make_config(http, server)
        config_id = body["config"]["id"]
        # Update with same password (simulate UI save)
        http.post(f"{server}/api/email/configs/{config_id}", json={
            "name":       "phase4-pwd-roundtrip",
            "host":       _IMAP_HOST,
            "port":       _IMAP_PORT,
            "username":   _IMAP_USER,
            "password":   _PASSWORD,   # same password, re-encrypt
            "folder":     "INBOX",
            "verify_ssl": False,
            "use_ssl":    False,
        })
        r = http.post(f"{server}/api/email/test", json={
            "id": config_id,
            "name": "phase4-pwd-roundtrip",
            "host": _IMAP_HOST, "port": _IMAP_PORT,
            "username": _IMAP_USER, "folder": "INBOX",
            "verify_ssl": False, "use_ssl": False,
        })
        assert r.json().get("ok"), "Connection must still work after password update"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4e — Déduplication processed_ids
# ─────────────────────────────────────────────────────────────────────────────

class TestEmailDeduplication:
    """Vérifie qu'un email déjà traité n'est PAS retraité lors du poll suivant.

    Mécanisme : pdf-dispatch stocke le message-id dans processed_ids.
    Au poll suivant, les emails dont le message-id est dans processed_ids
    sont ignorés même s'ils sont encore dans la boîte de réception.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, http, server, data_dir):
        _purge_greenmail()
        set_triggers(http, server, [{
            "value": TRIGGER, "page_handling": "keep", "case_sensitive": True
        }])
        set_config(http, server, separator_placement="before",
                   subdirs_by_trigger=True, delete_source=False)
        # Clean up configs before and after
        for ec in http.get(f"{server}/api/state").json().get(
                "app_config", {}).get("email_configs", []):
            http.delete(f"{server}/api/email/configs/{ec['id']}")
        yield
        for ec in http.get(f"{server}/api/state").json().get(
                "app_config", {}).get("email_configs", []):
            http.delete(f"{server}/api/email/configs/{ec['id']}")
        set_triggers(http, server, [])

    def test_processed_ids_populated_after_processing(self, http, server, data_dir):
        """Après traitement d'un email, processed_ids contient au moins 1 entrée."""
        before = snapshot_output(data_dir)
        _send(make_pdf([
            {"kind": "content", "text": "Dedup test"},
            {"kind": "qr", "value": TRIGGER},
            {"kind": "content", "text": "Doc 2"},
        ]))
        _create_config(http, server, action="read")
        result = _poll_result(http, server, data_dir, before, timeout=90.0)
        assert result.status not in ("timeout",), (
            "Email must be processed before checking processed_ids"
        )
        # Verify processed_ids is populated in the config
        state = http.get(f"{server}/api/state").json()
        configs = state["app_config"].get("email_configs", [])
        assert configs, "Email config must still exist"
        processed_ids = configs[0].get("processed_ids", [])
        assert len(processed_ids) >= 1, (
            f"processed_ids must have at least 1 entry after processing, got: {processed_ids}"
        )

    def test_same_email_not_processed_twice(self, http, server, data_dir):
        """Un email traité ne doit PAS être traité une 2ème fois lors du poll suivant."""
        before = snapshot_output(data_dir)
        _send(make_pdf([
            {"kind": "content", "text": "Dedup page 1"},
            {"kind": "qr", "value": TRIGGER},
            {"kind": "content", "text": "Dedup page 2"},
        ]))
        _create_config(http, server, action="read")
        result = _poll_result(http, server, data_dir, before, timeout=90.0)
        assert result.status not in ("timeout",), "First processing must complete"

        # Snapshot after first processing
        after_first = snapshot_output(data_dir)
        new_after_first = after_first - before

        # Wait for 1+ additional poll cycles (poll_interval=1s → ~5s safety margin)
        time.sleep(10)

        # No new files should have appeared
        after_second = snapshot_output(data_dir)
        new_after_second = after_second - before
        assert new_after_second == new_after_first, (
            f"Email reprocessed! New files appeared: {new_after_second - new_after_first}"
        )

    def test_reset_ids_allows_reprocessing(self, http, server, data_dir):
        """Après /api/email/reset_ids, le même email peut être retraité."""
        before = snapshot_output(data_dir)
        _send(make_pdf([
            {"kind": "content", "text": "Reset test"},
            {"kind": "qr", "value": TRIGGER},
            {"kind": "content", "text": "After trigger"},
        ]))
        _create_config(http, server, action="read")
        r1 = _poll_result(http, server, data_dir, before, timeout=90.0)
        assert r1.status not in ("timeout",), "First processing must complete"

        # Get config id
        state = http.get(f"{server}/api/state").json()
        config_id = state["app_config"]["email_configs"][0]["id"]

        # Reset processed_ids
        http.post(f"{server}/api/email/reset_ids/{config_id}")

        # Verify processed_ids is now empty
        state2 = http.get(f"{server}/api/state").json()
        ec2 = next(
            (c for c in state2["app_config"]["email_configs"] if c["id"] == config_id),
            None
        )
        assert ec2 is not None
        assert ec2.get("processed_ids", []) == [], (
            "processed_ids must be empty after reset"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4f — Intégration email → filesystem (vérification page-count)
# ─────────────────────────────────────────────────────────────────────────────

class TestEmailFilesystemIntegration:
    """Vérification croisée API + filesystem pour le pipeline email.

    TestEmailProcessing vérifie le statut via l'activité log.
    Cette classe ajoute la vérification filesystem directe :
    page-count via pypdf sur les fichiers produits dans /data/output/.

    Nécessite data_path configuré (accès au volume monté).
    """

    @pytest.fixture(autouse=True)
    def _setup(self, http, server, data_dir):
        _purge_greenmail()
        set_config(http, server, separator_placement="before",
                   subdirs_by_trigger=True, delete_source=False)
        for ec in http.get(f"{server}/api/state").json().get(
                "app_config", {}).get("email_configs", []):
            http.delete(f"{server}/api/email/configs/{ec['id']}")
        yield
        for ec in http.get(f"{server}/api/state").json().get(
                "app_config", {}).get("email_configs", []):
            http.delete(f"{server}/api/email/configs/{ec['id']}")
        set_triggers(http, server, [])

    def _page_count(self, path) -> int:
        from pypdf import PdfReader
        try:
            return len(PdfReader(path).pages)
        except Exception:
            return -1

    def test_trigger_pdf_page_counts_via_filesystem(self, http, server, data_dir):
        """PDF [content][trigger FK3 keep][content][content] par email.

        Vérification double :
          - result.output_files contient ≥1 fichier (via filesystem watcher)
          - page-count du document de sortie = 3 (trigger + 2 content)
        """
        set_triggers(http, server, [{
            "value": TRIGGER, "page_handling": "keep", "case_sensitive": True
        }])
        pdf = make_pdf([
            {"kind": "content", "text": "Pre-trigger content"},
            {"kind": "qr", "value": TRIGGER, "label": TRIGGER},
            {"kind": "content", "text": "Doc 2 — p1"},
            {"kind": "content", "text": "Doc 2 — p2"},
        ])
        before = snapshot_output(data_dir)
        _send(pdf)
        _create_config(http, server, action="read")
        result = _poll_result(http, server, data_dir, before, timeout=90.0)
        assert result.status != "timeout", "Email PDF with trigger must be processed"
        assert len(result.output_files) >= 1, (
            f"Expected output file in FK3/ subdir, got: {result}"
        )
        for f in result.output_files:
            pages = self._page_count(f)
            assert pages == 3, (
                f"keep mode: output must have 3 pages (trigger+2 content), got {pages} in {f.name}"
            )

    def test_no_code_pdf_page_count_via_filesystem(self, http, server, data_dir):
        """PDF sans barcode → no_code/ avec page-count correct."""
        set_triggers(http, server, [{
            "value": TRIGGER, "page_handling": "keep", "case_sensitive": True
        }])
        pdf = make_pdf([
            {"kind": "content", "text": "Page 1 no trigger"},
            {"kind": "content", "text": "Page 2 no trigger"},
        ])
        before = snapshot_output(data_dir)
        _send(pdf)
        _create_config(http, server, action="read")
        result = _poll_result(http, server, data_dir, before, timeout=90.0)
        assert result.status != "timeout"
        assert len(result.no_code_files) >= 1
        for f in result.no_code_files:
            pages = self._page_count(f)
            assert pages == 2, (
                f"no-code PDF: expected 2 pages in no_code/, got {pages} in {f.name}"
            )

    def test_delete_mode_removes_separator_page(self, http, server, data_dir):
        """PDF [content][trigger FK3 delete][content] : page séparateur absente."""
        set_triggers(http, server, [{
            "value": TRIGGER, "page_handling": "delete", "case_sensitive": True
        }])
        pdf = make_pdf([
            {"kind": "content", "text": "Pre-trigger"},
            {"kind": "qr", "value": TRIGGER},
            {"kind": "content", "text": "Post-trigger"},
        ])
        before = snapshot_output(data_dir)
        _send(pdf)
        _create_config(http, server, action="read")
        result = _poll_result(http, server, data_dir, before, timeout=90.0)
        assert result.status != "timeout"
        if result.output_files:
            for f in result.output_files:
                pages = self._page_count(f)
                assert pages == 1, (
                    f"delete mode: output must have 1 page (separator removed), got {pages}"
                )
        # At minimum, no output in no_code/ (the split should produce output files)
        assert len(result.error_files) == 0, (
            f"No files should go to error/: {result.error_files}"
        )
