"""
tests/test_05_security.py — Security regression tests.

Verifies that pdf-dispatch resists the two highest-priority adversarial
attack vectors identified in the security audit:

  A. POST /api/config config-poisoning (no key allowlist)
     - email_configs / stats / counter injection → must be rejected (HTTP 400)
     - dirs path-traversal bypass → must be rejected (HTTP 400)
     - Legitimate keys still accepted

  B. password_enc exposure in email config API responses
     - POST /api/email/configs (create) response must not contain password_enc
     - POST /api/email/configs/{id} (update) response must not contain password_enc
     - GET /api/state never exposes password_enc (regression guard)

Marker: (no marker — runs as part of the default test suite)
"""

from __future__ import annotations

import pytest

from helpers import get_config, set_config, set_triggers

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _post_config(http, server, payload: dict) -> tuple[int, dict]:
    r = http.post(f"{server}/api/config", json=payload)
    return r.status_code, r.json()


def _cleanup_email_configs(http, server) -> None:
    state = http.get(f"{server}/api/state").json()
    for ec in state.get("app_config", {}).get("email_configs", []):
        http.delete(f"{server}/api/email/configs/{ec['id']}")


def _make_email_payload(**overrides) -> dict:
    base = {
        "name":        "sec-test-config",
        "enabled":     False,
        "host":        "imap.example.com",
        "port":        993,
        "username":    "user@example.com",
        "password":    "secret-password",
        "folder":      "INBOX",
        "verify_ssl":  True,
        "use_ssl":     True,
        "action":      "read",
        "poll_interval": 5,
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# A. Config poisoning via POST /api/config
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigPoisoning:
    """
    POST /api/config must block keys that have dedicated management endpoints
    or are internal state, to prevent poisoning via the generic config write.
    """

    @pytest.fixture(autouse=True)
    def _restore(self, http, server):
        """Snapshot and restore split_values + separator_placement after each test."""
        snap = get_config(http, server)
        yield
        set_config(http, server,
                   split_values=snap.get("split_values", []),
                   separator_placement=snap.get("separator_placement", "before"))
        _cleanup_email_configs(http, server)

    # ── email_configs injection ───────────────────────────────────────────────

    def test_email_configs_injection_rejected(self, http, server):
        """Injecting email_configs via POST /api/config must return HTTP 400."""
        injected_configs = [{
            "id":       "attacker-injected",
            "name":     "Attacker config",
            "enabled":  True,
            "host":     "attacker.example.com",
            "port":     993,
            "username": "attacker",
            "folder":   "INBOX",
            "action":   "read",
        }]
        code, body = _post_config(http, server, {"email_configs": injected_configs})
        assert code == 400, (
            f"Expected HTTP 400 for email_configs injection, got {code}: {body}"
        )
        assert not body.get("ok"), "Response must have ok=False"

    def test_email_configs_not_written_after_rejection(self, http, server):
        """After a rejected injection, email_configs in state must be unchanged."""
        before = get_config(http, server).get("email_configs", [])
        _post_config(http, server, {"email_configs": [{"id": "injected", "name": "evil"}]})
        after = get_config(http, server).get("email_configs", [])
        assert after == before, (
            "email_configs changed after a rejected POST /api/config"
        )

    # ── stats poisoning ───────────────────────────────────────────────────────

    def test_stats_injection_rejected(self, http, server):
        """Injecting stats via POST /api/config must return HTTP 400."""
        code, body = _post_config(http, server, {
            "stats": {"processed": 0, "errors": 0, "produced": 0}
        })
        assert code == 400, (
            f"Expected HTTP 400 for stats injection, got {code}: {body}"
        )
        assert not body.get("ok")

    def test_stats_not_overwritten_after_rejection(self, http, server):
        """Stats in /api/state must be unchanged after a rejected injection."""
        before = http.get(f"{server}/api/state").json()["stats"]
        _post_config(http, server, {"stats": {"processed": 9999}})
        after = http.get(f"{server}/api/state").json()["stats"]
        assert after["processed"] == before["processed"], (
            f"stats.processed changed from {before['processed']} "
            f"to {after['processed']} after rejected injection"
        )

    # ── counter poisoning ─────────────────────────────────────────────────────

    def test_counter_injection_rejected(self, http, server):
        """The internal file counter must not be settable via POST /api/config."""
        code, body = _post_config(http, server, {"counter": 0})
        assert code == 400, (
            f"Expected HTTP 400 for counter injection, got {code}: {body}"
        )
        assert not body.get("ok")

    # ── dirs path traversal ───────────────────────────────────────────────────

    def test_dirs_path_traversal_rejected(self, http, server):
        """Attempting to redirect 'output' outside DATA_DIR via POST /api/config must fail."""
        code, body = _post_config(http, server, {
            "dirs": {"output": "../../etc"}
        })
        assert code == 400, (
            f"Expected HTTP 400 for dirs path traversal, got {code}: {body}"
        )
        assert not body.get("ok")

    def test_dirs_absolute_path_rejected(self, http, server):
        """Absolute paths in dirs must be rejected by the safe-path validator."""
        # An absolute-looking path gets caught by the forbidden-chars check
        # or the resolve-check when combined with DATA_DIR.
        code, body = _post_config(http, server, {
            "dirs": {"input": "/etc/passwd"}
        })
        # Either 400 (blocked) or the path is sanitised and rejected —
        # either way the route must not accept a clear traversal attempt.
        assert code == 400, (
            f"Expected HTTP 400 for absolute path in dirs, got {code}: {body}"
        )

    def test_dirs_double_dot_segment_rejected(self, http, server):
        """A path containing '..' must be rejected."""
        code, body = _post_config(http, server, {
            "dirs": {"error": "../../../tmp/evil"}
        })
        assert code == 400, (
            f"Expected HTTP 400 for .. in dirs path, got {code}: {body}"
        )

    def test_dirs_unknown_key_rejected(self, http, server):
        """Unknown dir keys (not in the allowed set) must be rejected."""
        code, body = _post_config(http, server, {
            "dirs": {"secret_exfil": "output"}
        })
        assert code == 400, (
            f"Expected HTTP 400 for unknown dirs key, got {code}: {body}"
        )

    # ── Legitimate keys still work ────────────────────────────────────────────

    def test_legitimate_key_still_accepted(self, http, server):
        """
        Blocking dangerous keys must not break legitimate settings.
        Posting 'separator_placement' must still succeed.
        """
        original = get_config(http, server).get("separator_placement", "before")
        new_val  = "after" if original == "before" else "before"
        code, body = _post_config(http, server, {"separator_placement": new_val})
        assert code == 200, f"Legitimate key rejected unexpectedly: {body}"
        assert body.get("ok")
        assert get_config(http, server)["separator_placement"] == new_val

    def test_split_values_still_settable(self, http, server):
        """split_values is a core config key and must remain writable."""
        new_sv = [{"value": "SEC_TEST", "page_handling": "keep", "case_sensitive": True}]
        code, body = _post_config(http, server, {"split_values": new_sv})
        assert code == 200, f"split_values rejected unexpectedly: {body}"
        assert body.get("ok")
        assert any(sv.get("value") == "SEC_TEST"
                   for sv in get_config(http, server).get("split_values", []))

    def test_combined_blocked_and_allowed_rejected(self, http, server):
        """
        A payload mixing a blocked key with a legitimate key must be rejected
        entirely (the valid part must not be applied either).
        """
        original_sep = get_config(http, server).get("separator_placement", "before")
        new_sep = "after" if original_sep == "before" else "before"

        code, body = _post_config(http, server, {
            "separator_placement": new_sep,   # legitimate
            "email_configs":       [],         # blocked
        })
        assert code == 400, (
            f"Mixed payload with blocked key must be rejected entirely, got {code}"
        )
        # The legitimate field must NOT have been applied
        current = get_config(http, server).get("separator_placement", "before")
        assert current == original_sep, (
            f"separator_placement changed from {original_sep!r} to {current!r} "
            "despite the request being rejected"
        )


# ─────────────────────────────────────────────────────────────────────────────
# B. password_enc exposure in email config responses
# ─────────────────────────────────────────────────────────────────────────────

class TestPasswordEncExposure:
    """
    password_enc must never appear in API responses, even when a password
    is stored internally.  Only /api/state → app_config had this guarantee
    before; it must now also hold for the CRUD response bodies.
    """

    @pytest.fixture(autouse=True)
    def _cleanup(self, http, server):
        _cleanup_email_configs(http, server)
        yield
        _cleanup_email_configs(http, server)

    def test_create_response_has_no_password_enc(self, http, server):
        """POST /api/email/configs response must not contain password_enc."""
        r = http.post(f"{server}/api/email/configs",
                      json=_make_email_payload())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok"), body
        assert "password_enc" not in body.get("config", {}), (
            "password_enc must not be returned in the create response"
        )

    def test_create_response_has_no_processed_ids(self, http, server):
        """POST /api/email/configs response must not contain processed_ids."""
        r = http.post(f"{server}/api/email/configs",
                      json=_make_email_payload())
        cfg = r.json().get("config", {})
        assert "processed_ids" not in cfg, (
            "processed_ids (internal list) must not be returned in the create response"
        )

    def test_update_response_has_no_password_enc(self, http, server):
        """POST /api/email/configs/{id} update response must not contain password_enc."""
        create_r = http.post(f"{server}/api/email/configs",
                             json=_make_email_payload())
        assert create_r.status_code == 200
        config_id = create_r.json()["config"]["id"]

        updated_payload = _make_email_payload(name="sec-test-config",
                                              action="delete",
                                              poll_interval=10)
        update_r = http.post(f"{server}/api/email/configs/{config_id}",
                             json={**updated_payload, "id": config_id})
        assert update_r.status_code == 200, update_r.text
        update_body = update_r.json()
        assert update_body.get("ok"), update_body
        assert "password_enc" not in update_body.get("config", {}), (
            "password_enc must not be returned in the update response"
        )

    def test_update_response_has_no_processed_ids(self, http, server):
        """POST /api/email/configs/{id} update response must not contain processed_ids."""
        create_r = http.post(f"{server}/api/email/configs",
                             json=_make_email_payload())
        config_id = create_r.json()["config"]["id"]
        update_r  = http.post(f"{server}/api/email/configs/{config_id}",
                              json={**_make_email_payload(), "id": config_id})
        cfg = update_r.json().get("config", {})
        assert "processed_ids" not in cfg, (
            "processed_ids must not be returned in the update response"
        )

    def test_state_has_no_password_enc(self, http, server):
        """Regression: GET /api/state must never expose password_enc."""
        http.post(f"{server}/api/email/configs",
                  json=_make_email_payload())
        state = http.get(f"{server}/api/state").json()
        for ec in state.get("app_config", {}).get("email_configs", []):
            assert "password_enc" not in ec, (
                f"password_enc found in /api/state email config {ec.get('id')!r}"
            )

    def test_password_not_returned_in_any_field(self, http, server):
        """
        The cleartext password supplied at creation time must not appear
        anywhere in the create or update response body.
        """
        secret = "ultra-secret-imap-password-XYZ"
        create_r = http.post(f"{server}/api/email/configs",
                             json=_make_email_payload(password=secret))
        assert create_r.status_code == 200
        create_body_str = create_r.text
        assert secret not in create_body_str, (
            "Cleartext password found in create response body"
        )
        config_id = create_r.json()["config"]["id"]
        update_r = http.post(f"{server}/api/email/configs/{config_id}",
                             json={**_make_email_payload(password=secret), "id": config_id})
        assert update_r.status_code == 200
        assert secret not in update_r.text, (
            "Cleartext password found in update response body"
        )
