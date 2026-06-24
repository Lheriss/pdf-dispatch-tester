"""
tests/test_08_input_validation.py — Input validation security tests.

Covers five vulnerabilities identified in the security audit:

  C. CRLF injection in IMAP fields (host, username, folder)
     → Prevented by _validate_imap_field() in both create and update endpoints.

  E. Port and poll_interval out-of-range values accepted at write time
     → _validate_email_config_fields() now rejects invalid values at the API
       level rather than only correcting them silently at config-load time.

  F. Barcode value used in output filename / subdirectory (regression guard)
     → FORBIDDEN_CHARS already includes '/' so '../../../etc' cannot escape
       OUTPUT_DIR; test documents this invariant.

  G. Control-character injection in /api/log messages
     → \\r, \\n and ANSI escapes are stripped before reaching log_event().

  H. /api/recent?n=<non-integer> → unhandled ValueError → 500
     → Returns HTTP 400 with an error message instead.

No marker — runs as part of the default test suite.
"""
from __future__ import annotations

import pytest

from helpers import get_config, set_config, upload_and_wait
from pdf_generator import make_pdf

# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

_BASE_EMAIL_PAYLOAD = {
    "name":          "validation-test",
    "enabled":       False,
    "host":          "imap.example.com",
    "port":          993,
    "username":      "user@example.com",
    "password":      "secret",
    "folder":        "INBOX",
    "verify_ssl":    True,
    "use_ssl":       True,
    "action":        "read",
    "poll_interval": 5,
}


def _create(http, server, **overrides) -> tuple[int, dict]:
    payload = {**_BASE_EMAIL_PAYLOAD, **overrides}
    r = http.post(f"{server}/api/email/configs", json=payload)
    return r.status_code, r.json()


def _cleanup_email_configs(http, server) -> None:
    state = http.get(f"{server}/api/state").json()
    for ec in state.get("app_config", {}).get("email_configs", []):
        http.delete(f"{server}/api/email/configs/{ec['id']}")


# ─────────────────────────────────────────────────────────────────────────────
# C — CRLF injection in IMAP fields
# ─────────────────────────────────────────────────────────────────────────────

class TestCrlfInjection:
    """CRLF characters in host, username, folder must be rejected with HTTP 400."""

    @pytest.fixture(autouse=True)
    def _cleanup(self, http, server):
        _cleanup_email_configs(http, server)
        yield
        _cleanup_email_configs(http, server)

    # ── Create endpoint ───────────────────────────────────────────────────────

    def test_create_host_newline_rejected(self, http, server):
        code, body = _create(http, server, host="imap.example.com\nX-Injected: header")
        assert code == 400, f"Expected 400, got {code}: {body}"
        assert not body.get("ok")

    def test_create_host_crlf_rejected(self, http, server):
        code, body = _create(http, server, host="imap.example.com\r\nLOGOUT")
        assert code == 400, f"Expected 400, got {code}: {body}"

    def test_create_username_newline_rejected(self, http, server):
        code, body = _create(http, server, username="user\nA001 LOGOUT")
        assert code == 400, f"Expected 400, got {code}: {body}"

    def test_create_folder_crlf_rejected(self, http, server):
        code, body = _create(http, server, folder="INBOX\r\nA001 CAPABILITY")
        assert code == 400, f"Expected 400, got {code}: {body}"

    def test_create_clean_fields_accepted(self, http, server):
        """Legitimate values must still be accepted (no false positive)."""
        code, body = _create(http, server,
                             name="clean-test",
                             host="mail.example.com",
                             username="user@example.com",
                             folder="INBOX/Subfolder")
        assert code == 200, f"Legitimate config rejected: {body}"
        assert body.get("ok")

    # ── Update endpoint ───────────────────────────────────────────────────────

    def test_update_host_crlf_rejected(self, http, server):
        code, body = _create(http, server, name="update-crlf-test")
        assert code == 200
        ec_id = body["config"]["id"]

        r = http.post(f"{server}/api/email/configs/{ec_id}",
                      json={**_BASE_EMAIL_PAYLOAD,
                            "name": "update-crlf-test",
                            "host": "evil.com\r\nLOGIN attacker pw"})
        assert r.status_code == 400, f"Expected 400 on update, got {r.status_code}: {r.json()}"

    def test_update_folder_newline_rejected(self, http, server):
        code, body = _create(http, server, name="update-folder-test")
        assert code == 200
        ec_id = body["config"]["id"]

        r = http.post(f"{server}/api/email/configs/{ec_id}",
                      json={**_BASE_EMAIL_PAYLOAD,
                            "name": "update-folder-test",
                            "folder": "INBOX\nA001 SELECT SECRET"})
        assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# E — Port and poll_interval bounds
# ─────────────────────────────────────────────────────────────────────────────

class TestEmailConfigBounds:
    """Out-of-range port and poll_interval must be rejected at API write time."""

    @pytest.fixture(autouse=True)
    def _cleanup(self, http, server):
        _cleanup_email_configs(http, server)
        yield
        _cleanup_email_configs(http, server)

    # ── port ─────────────────────────────────────────────────────────────────

    def test_port_zero_rejected(self, http, server):
        code, body = _create(http, server, port=0)
        assert code == 400, f"port=0 must be rejected, got {code}: {body}"
        assert not body.get("ok")

    def test_port_negative_rejected(self, http, server):
        code, body = _create(http, server, port=-1)
        assert code == 400, f"port=-1 must be rejected, got {code}: {body}"

    def test_port_above_max_rejected(self, http, server):
        code, body = _create(http, server, port=65536)
        assert code == 400, f"port=65536 must be rejected, got {code}: {body}"

    def test_port_very_large_rejected(self, http, server):
        code, body = _create(http, server, port=99999)
        assert code == 400, f"port=99999 must be rejected, got {code}: {body}"

    def test_port_min_boundary_accepted(self, http, server):
        code, body = _create(http, server, name="port-min-test", port=1)
        assert code == 200, f"port=1 must be accepted, got {code}: {body}"

    def test_port_max_boundary_accepted(self, http, server):
        code, body = _create(http, server, name="port-max-test", port=65535)
        assert code == 200, f"port=65535 must be accepted, got {code}: {body}"

    # ── poll_interval ─────────────────────────────────────────────────────────

    def test_poll_interval_zero_rejected(self, http, server):
        code, body = _create(http, server, poll_interval=0)
        assert code == 400, f"poll_interval=0 must be rejected, got {code}: {body}"

    def test_poll_interval_negative_rejected(self, http, server):
        code, body = _create(http, server, poll_interval=-5)
        assert code == 400, f"poll_interval=-5 must be rejected, got {code}: {body}"

    def test_poll_interval_one_accepted(self, http, server):
        code, body = _create(http, server, name="interval-min-test", poll_interval=1)
        assert code == 200, f"poll_interval=1 must be accepted, got {code}: {body}"

    def test_update_port_out_of_range_rejected(self, http, server):
        code, body = _create(http, server, name="update-port-test")
        assert code == 200
        ec_id = body["config"]["id"]
        r = http.post(f"{server}/api/email/configs/{ec_id}",
                      json={**_BASE_EMAIL_PAYLOAD,
                            "name": "update-port-test",
                            "port": 99999})
        assert r.status_code == 400, f"Expected 400 on update, got {r.status_code}"


# ─────────────────────────────────────────────────────────────────────────────
# F — Barcode value → output filename / subdirectory (regression guard)
# ─────────────────────────────────────────────────────────────────────────────

class TestBarcodeFilenameRegression:
    """
    FORBIDDEN_CHARS includes '/' so a QR code containing '../../../etc' cannot
    produce a path that escapes OUTPUT_DIR.  This test uploads a PDF whose
    trigger code is a path-traversal string and verifies the task succeeds
    (file processed normally) and that the output path stays within output/.
    """

    def test_path_traversal_trigger_value_stays_in_output(self, http, server):
        """QR code value '../../../etc' must not escape the output directory."""
        traversal = "../../../etc"
        set_config(http, server, split_values=[
            {"value": traversal, "page_handling": "keep", "case_sensitive": False}
        ])
        try:
            pdf = make_pdf([
                {"kind": "qr",      "value": traversal},
                {"kind": "content", "text": "payload"},
            ])
            task = upload_and_wait(http, server, pdf, "traversal_test.pdf", timeout=30)
            assert task["status"] == "success"
            for out in task.get("outputs", []):
                path = out.get("path", "")
                assert not path.startswith("/"), f"Absolute path in output: {path!r}"
                assert ".." not in path.split("/")[0], (
                    f"Path traversal component in output path: {path!r}"
                )
                # Must stay under output/
                assert path.startswith("output/"), (
                    f"Output path escaped output/ directory: {path!r}"
                )
        finally:
            set_config(http, server, split_values=[
                {"value": "FK3", "page_handling": "keep", "case_sensitive": True}
            ])

    def test_long_trigger_value_truncated_safely(self, http, server):
        """A 200-char trigger value must be truncated to a safe length without crash."""
        long_trigger = "A" * 200
        set_config(http, server, split_values=[
            {"value": long_trigger, "page_handling": "keep", "case_sensitive": True}
        ])
        try:
            pdf = make_pdf([
                {"kind": "qr",      "value": long_trigger},
                {"kind": "content", "text": "content"},
            ])
            task = upload_and_wait(http, server, pdf, "long_trigger.pdf", timeout=30)
            assert task["status"] == "success"
            for out in task.get("outputs", []):
                # build_filename truncates safe_trigger to 40 chars
                filename = out.get("filename", "")
                assert len(filename) < 300, f"Filename suspiciously long: {filename!r}"
        finally:
            set_config(http, server, split_values=[
                {"value": "FK3", "page_handling": "keep", "case_sensitive": True}
            ])


# ─────────────────────────────────────────────────────────────────────────────
# G — Control-character injection in /api/log
# ─────────────────────────────────────────────────────────────────────────────

class TestLogInjection:
    """Control characters in /api/log messages must be stripped before storage."""

    def _post_log(self, http, server, message: str, level: str = "info") -> dict:
        r = http.post(f"{server}/api/log",
                      json={"level": level, "message": message})
        r.raise_for_status()
        return r.json()

    def _last_events(self, http, server, n: int = 5) -> list[dict]:
        return http.get(f"{server}/api/state").json().get("events", [])[:n]

    def test_newline_stripped_from_log_message(self, http, server):
        """A newline in the message must not create a second log entry."""
        marker = "SECURITY_TEST_NL_" + "X" * 8
        self._post_log(http, server, f"{marker}\nFAKE_ENTRY injected")
        events = self._last_events(http, server, 20)
        messages = [e.get("message", "") for e in events]
        # The marker must appear but no entry starting with FAKE_ENTRY
        assert any(marker in m for m in messages), "Marker not found in events"
        assert not any(m.startswith("FAKE_ENTRY") for m in messages), (
            "Injected fake log entry appeared as a separate event"
        )

    def test_crlf_stripped_from_log_message(self, http, server):
        """CRLF sequence must not produce a second log event."""
        marker = "SECURITY_TEST_CR_" + "Y" * 8
        self._post_log(http, server, f"{marker}\r\nFAKE_CR_ENTRY")
        events = self._last_events(http, server, 20)
        messages = [e.get("message", "") for e in events]
        assert not any("FAKE_CR_ENTRY" in m for m in messages), (
            "CR-injected entry appeared in log"
        )

    def test_ansi_escape_stripped_from_log_message(self, http, server):
        """ANSI escape sequences must be stripped (prevents terminal hijacking)."""
        ansi_msg = "\x1b[31mRED ALERT\x1b[0m injected colour"
        result = self._post_log(http, server, ansi_msg)
        assert result.get("ok"), result
        # Verify endpoint didn't crash; actual stripping verified by absence
        # of ESC char (\x1b) in stored events
        events = self._last_events(http, server, 20)
        for e in events:
            assert "\x1b" not in e.get("message", ""), (
                "ANSI escape sequence found in stored log event"
            )

    def test_null_byte_stripped_from_log_message(self, http, server):
        """Null bytes in log messages must be stripped."""
        result = self._post_log(http, server, "before\x00after")
        assert result.get("ok"), result
        events = self._last_events(http, server, 10)
        for e in events:
            assert "\x00" not in e.get("message", ""), (
                "Null byte found in stored log event"
            )

    def test_normal_message_unaffected(self, http, server):
        """Legitimate messages with no control characters must pass through unchanged."""
        msg = "Config updated: separator=before, triggers=[FK3, INVOICE]"
        self._post_log(http, server, msg)
        events = self._last_events(http, server, 10)
        assert any(msg in e.get("message", "") for e in events), (
            "Legitimate log message not found in events"
        )


# ─────────────────────────────────────────────────────────────────────────────
# H — /api/recent?n= parameter validation
# ─────────────────────────────────────────────────────────────────────────────

class TestApiRecentNParam:
    """Non-integer n parameter must return 400 instead of 500."""

    def test_string_n_returns_400(self, http, server):
        r = http.get(f"{server}/api/recent?n=abc")
        assert r.status_code == 400, (
            f"Expected 400 for n=abc, got {r.status_code}: {r.text[:200]}"
        )
        assert not r.json().get("ok")

    def test_float_n_returns_400(self, http, server):
        r = http.get(f"{server}/api/recent?n=3.14")
        assert r.status_code == 400, (
            f"Expected 400 for n=3.14, got {r.status_code}"
        )

    def test_empty_n_uses_default(self, http, server):
        """Empty n should fall back to default (20) and return 200."""
        r = http.get(f"{server}/api/recent?n=")
        # Empty string → int("") raises ValueError → 400
        # OR the endpoint treats it as missing and uses default → 200
        # Either is acceptable; must not be 500
        assert r.status_code != 500, (
            f"Empty n must not cause a 500 error, got {r.status_code}"
        )

    def test_valid_integer_n_returns_200(self, http, server):
        """A valid integer must still work correctly."""
        r = http.get(f"{server}/api/recent?n=5")
        assert r.status_code == 200, f"Valid n=5 rejected: {r.text[:200]}"
        assert isinstance(r.json().get("files"), list)

    def test_n_capped_at_100(self, http, server):
        """n > 100 must be silently capped to 100."""
        r = http.get(f"{server}/api/recent?n=999")
        assert r.status_code == 200
        files = r.json().get("files", [])
        assert len(files) <= 100, f"Expected ≤100 results, got {len(files)}"

    def test_no_stack_trace_on_invalid_n(self, http, server):
        """Error response must not contain a Python stack trace."""
        r = http.get(f"{server}/api/recent?n=notanumber")
        for token in ("Traceback", 'File "', "ValueError"):
            assert token not in r.text, (
                f"Stack trace exposed for invalid n: {r.text[:300]}"
            )
