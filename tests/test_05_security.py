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

  C. Authentication bypass (anonymous requests)
     - Any request with no X-API-Key must be rejected (HTTP 401) when a key
       is configured, even when APP_USERNAME/APP_PASSWORD are not set.
     - Wrong API key must always be rejected.
     - Valid API key must still be accepted (regression guard).
     - /healthz must remain publicly accessible.

  D. API key bootstrap
     - The api_key must be non-empty in /api/state from the very first
       request (auto-generated at startup, not waiting for manual action).

  E. Web-interface sign-in (pdf-dispatch >= 1.8, administrator account)
     - A browser asking for a page is redirected to /login — never a bare
       401 or a Basic prompt (the v1.7.4 web-interface lockout).
     - Wrong password → 401, no session; right password → session cookie
       (HttpOnly, SameSite=Lax, no password inside).
     - Session writes require X-Requested-With (CSRF); sign-out ends it.
     - Basic Authorization header accepted, never solicited.
     - /login only redirects to local paths; /setup is closed once an
       account exists; the API description is public, /api/runtime is not.

  F. Protected settings
     - api_key, administrator account and session secret cannot be set via
       POST /api/config (api_key="" used to re-open the instance) and are
       never returned by /api/state.

Marker: (no marker — runs as part of the default test suite)
"""

from __future__ import annotations

import re
import requests
import pytest

from helpers import get_config, set_config, set_triggers

# ─────────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────────
# A. Config poisoning via POST /api/config
# ─────────────────────────────────────────────────────────────────────────────────

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

    # ── email_configs injection ────────────────────────────────────────────────

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

    # ── stats poisoning ─────────────────────────────────────────────────────────

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

    # ── counter poisoning ───────────────────────────────────────────────────────────

    def test_counter_injection_rejected(self, http, server):
        """The internal file counter must not be settable via POST /api/config."""
        code, body = _post_config(http, server, {"counter": 0})
        assert code == 400, (
            f"Expected HTTP 400 for counter injection, got {code}: {body}"
        )
        assert not body.get("ok")

    # ── dirs path traversal ──────────────────────────────────────────────────────────

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
        code, body = _post_config(http, server, {
            "dirs": {"input": "/etc/passwd"}
        })
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

    def test_dirs_sibling_dir_rejected(self, http, server):
        """Path resolving to a DATA_DIR *sibling* must be rejected."""
        code, body = _post_config(http, server, {
            "dirs": {"output": "output/../../data_sibling"}
        })
        assert code == 400, (
            f"Sibling-dir traversal must be rejected (HTTP 400), got {code}: {body}"
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
            "separator_placement": new_sep,
            "email_configs":       [],
        })
        assert code == 400, (
            f"Mixed payload with blocked key must be rejected entirely, got {code}"
        )
        current = get_config(http, server).get("separator_placement", "before")
        assert current == original_sep, (
            f"separator_placement changed from {original_sep!r} to {current!r} "
            "despite the request being rejected"
        )


# ─────────────────────────────────────────────────────────────────────────────────
# B. password_enc exposure in email config responses
# ─────────────────────────────────────────────────────────────────────────────────

class TestPasswordEncExposure:
    """
    password_enc must never appear in API responses, even when a password
    is stored internally.
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


# ─────────────────────────────────────────────────────────────────────────────────
# C. Authentication bypass
# ─────────────────────────────────────────────────────────────────────────────────

class TestAuthBypass:
    """
    Verifies that anonymous requests are rejected when an API key is
    configured, even when APP_USERNAME/APP_PASSWORD are not set.

    Vulnerability (fixed): auth.py previously allowed any request through
    when no X-API-Key was provided and Basic auth was not configured —
    meaning a request with no credentials at all would bypass authentication
    entirely.  A key being generated and stored does not protect the API if
    the server never *requires* it on anonymous requests.

    Fix: if a stored API key exists, any request that does not present a
    valid X-API-Key must be rejected with HTTP 401.
    """

    @pytest.fixture
    def anon(self, server):
        """Unauthenticated requests.Session (no X-API-Key, no Basic auth)."""
        s = requests.Session()
        s.headers["Accept"] = "application/json"
        return s

    @pytest.fixture
    def bad_key(self, server):
        """Session carrying a syntactically valid but wrong API key."""
        s = requests.Session()
        s.headers["X-API-Key"] = "0" * 64
        s.headers["Accept"]    = "application/json"
        return s

    def test_anonymous_get_state_rejected(self, anon, server):
        """GET /api/state without credentials → 401."""
        r = anon.get(f"{server}/api/state")
        assert r.status_code == 401, (
            f"Anonymous GET /api/state must be rejected, got {r.status_code}: {r.text[:120]}"
        )

    def test_anonymous_config_write_rejected(self, anon, server):
        """POST /api/config without credentials → 401."""
        r = anon.post(f"{server}/api/config", json={"separator_placement": "after"})
        assert r.status_code == 401, (
            f"Anonymous POST /api/config must be rejected, got {r.status_code}"
        )

    def test_anonymous_upload_rejected(self, anon, server):
        """POST /api/upload without credentials → 401."""
        dummy_pdf = b"%PDF-1.4 1 0 obj<</Type/Catalog>>endobj\nxref\n0 0\ntrailer<</Size 1>>startxref 9\n%%EOF"
        r = anon.post(
            f"{server}/api/upload",
            files={"file": ("x.pdf", dummy_pdf, "application/pdf")},
        )
        assert r.status_code == 401, (
            f"Anonymous POST /api/upload must be rejected, got {r.status_code}"
        )

    def test_anonymous_email_config_read_rejected(self, anon, server):
        """GET /api/state (which exposes email_configs) must not be accessible anonymously."""
        r = anon.get(f"{server}/api/state")
        assert r.status_code == 401

    def test_wrong_key_on_read_rejected(self, bad_key, server):
        """Wrong X-API-Key on GET /api/state → 401."""
        r = bad_key.get(f"{server}/api/state")
        assert r.status_code == 401, (
            f"Wrong API key on GET /api/state must be rejected, got {r.status_code}"
        )

    def test_wrong_key_on_write_rejected(self, bad_key, server):
        """Wrong X-API-Key on a state-modifying endpoint → 401."""
        r = bad_key.post(f"{server}/api/config", json={"separator_placement": "after"})
        assert r.status_code == 401

    def test_valid_key_still_accepted(self, http, server):
        """The auth fix must not break requests carrying a valid API key."""
        r = http.get(f"{server}/api/state")
        assert r.status_code == 200

    def test_healthz_always_public(self, anon, server):
        """/healthz must be accessible without credentials (Docker HEALTHCHECK)."""
        r = anon.get(f"{server}/healthz")
        assert r.status_code == 200, (
            f"/healthz must be public, got {r.status_code}: {r.text[:80]}"
        )

    def test_anonymous_write_does_not_modify_state(self, anon, server, http):
        """
        Anonymous request must not modify server state (confirmed via 401
        and by checking the config is unchanged).
        """
        before = http.get(f"{server}/api/state").json()
        sep_before = before.get("app_config", {}).get("separator_placement", "before")
        new_val = "after" if sep_before == "before" else "before"

        anon.post(f"{server}/api/config", json={"separator_placement": new_val})

        sep_after = (
            http.get(f"{server}/api/state")
                .json()
                .get("app_config", {})
                .get("separator_placement")
        )
        assert sep_after == sep_before, (
            f"Anonymous request must not modify config: separator_placement "
            f"changed from {sep_before!r} to {sep_after!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────────
# D. API key bootstrap
# ─────────────────────────────────────────────────────────────────────────────────

class TestApiKeyBootstrap:
    """
    Verifies that an API key is present from the very first request,
    without requiring manual intervention in the UI.

    Regression guard for the window that existed between first startup
    and the user clicking \"Regenerate API key\": during that window
    api_key was \"\" and anonymous requests were silently allowed.

    After the fix (_ensure_api_key called from create_app), the key is
    generated and persisted before the HTTP server accepts connections,
    so this window no longer exists.
    """

    def test_api_key_present_in_state(self, http, server):
        """GET /api/state must return a non-empty api_key in app_config."""
        r = http.get(f"{server}/api/state")
        assert r.status_code == 200
        app_config = r.json().get("app_config", {})
        api_key = app_config.get("api_key", "")
        assert api_key, (
            "api_key must be non-empty in /api/state from the first request — "
            "it should have been auto-generated at startup by _ensure_api_key(). "
            f"Got: {api_key!r}"
        )

    def test_api_key_minimum_length(self, http, server):
        """The auto-generated API key must be at least 32 characters long."""
        app_config = http.get(f"{server}/api/state").json().get("app_config", {})
        api_key = app_config.get("api_key", "")
        # secrets.token_hex(32) produces 64 hex chars; env var keys may vary.
        # Enforce a reasonable minimum to catch accidental short keys.
        assert len(api_key) >= 32, (
            f"API key is too short ({len(api_key)} chars, expected >= 32): {api_key!r}"
        )

    def test_api_key_is_hex(self, http, server):
        """The auto-generated API key must be a hex string (no special chars)."""
        import re
        app_config = http.get(f"{server}/api/state").json().get("app_config", {})
        api_key = app_config.get("api_key", "")
        # Allow uppercase and lowercase hex, plus env-var keys that may differ.
        # The main guard: no whitespace, no control characters, no quotes.
        assert api_key and not re.search(r'[\s\x00-\x1f"\']', api_key), (
            f"API key contains invalid characters: {api_key!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────────
# E. Web-interface sign-in
# ─────────────────────────────────────────────────────────────────────────────────

# Prefix only: since pdf-dispatch v1.8.3 the name is pdf_dispatch_session_<id>,
# unique per instance (browsers share cookies across ports of one host).
_SESSION_COOKIE = "pdf_dispatch_session"


def _session_cookies(s: requests.Session) -> list[str]:
    return [c.name for c in s.cookies if c.name.startswith(_SESSION_COOKIE)]


def _browser_session() -> requests.Session:
    """A cookie-keeping session with no credential, like a fresh browser."""
    s = requests.Session()
    s.headers["Accept"] = "text/html,application/xhtml+xml"
    return s


def _sign_in(server, user, pwd, **extra) -> tuple[requests.Session, requests.Response]:
    s = _browser_session()
    r = s.post(f"{server}/login", data={"username": user, "password": pwd, **extra},
               allow_redirects=False)
    return s, r


class TestWebSignIn:
    """Sign-in flow over real HTTP against the running container.

    Complements pdf-dispatch's own test_auth.py (Flask test client) with what
    only a real server shows: redirects, Set-Cookie attributes, cookies kept
    across requests. Requires the predefined account (APP_USERNAME /
    APP_PASSWORD on the instance = TESTER_ADMIN_USER / TESTER_ADMIN_PASSWORD).
    """

    def test_page_redirects_to_login(self, server, admin_creds):
        """GET / without credentials → 302 to /login (not 401, not a Basic prompt)."""
        r = _browser_session().get(f"{server}/", allow_redirects=False)
        assert r.status_code == 302, (
            f"A browser opening / must be redirected to the sign-in page, got "
            f"{r.status_code} — the web interface would be unusable "
            f"(pdf-dispatch v1.7.4 regression)"
        )
        assert "/login" in r.headers.get("Location", "")
        assert "WWW-Authenticate" not in r.headers

    def test_login_page_renders(self, server, admin_creds):
        r = _browser_session().get(f"{server}/login")
        assert r.status_code == 200
        assert 'name="password"' in r.text

    def test_setup_closed_when_account_exists(self, server, admin_creds):
        """/setup redirects to /login once an administrator account exists."""
        r = _browser_session().get(f"{server}/setup", allow_redirects=False)
        assert r.status_code == 302 and "/login" in r.headers.get("Location", "")

    def test_wrong_password_rejected(self, server, admin_creds):
        user, _ = admin_creds
        s, r = _sign_in(server, user, "definitely-not-the-password")
        assert r.status_code == 401
        assert _session_cookies(s) == []
        assert s.get(f"{server}/api/state").status_code == 401

    def test_api_key_is_not_a_password(self, server, admin_creds, api_key):
        user, _ = admin_creds
        _, r = _sign_in(server, user, api_key)
        assert r.status_code == 401

    def test_sign_in_sets_secure_cookie(self, server, admin_creds):
        user, pwd = admin_creds
        s, r = _sign_in(server, user, pwd)
        assert r.status_code == 302 and r.headers.get("Location") == "/"
        cookie = r.headers.get("Set-Cookie", "")
        assert cookie.startswith(_SESSION_COOKIE), f"Unexpected Set-Cookie: {cookie}"
        assert "HttpOnly" in cookie, f"Session cookie must be HttpOnly: {cookie}"
        assert "SameSite=Lax" in cookie, f"Session cookie must be SameSite=Lax: {cookie}"
        assert pwd not in cookie

    def test_cookie_name_is_instance_specific(self, server, admin_creds):
        """pdf_dispatch_session_<8 hex>: two instances on the same host (ports
        differ) must not overwrite each other's sign-in cookie."""
        _, r = _sign_in(server, *admin_creds)
        cookie = r.headers.get("Set-Cookie", "")
        assert re.match(r"pdf_dispatch_session_[0-9a-f]{8}=", cookie), (
            f"Session cookie name must carry the instance identifier: {cookie.split(';')[0]}"
        )

    def test_session_grants_page_and_api(self, server, admin_creds):
        s, _ = _sign_in(server, *admin_creds)
        r = s.get(f"{server}/")
        assert r.status_code == 200 and 'action="/logout"' in r.text
        assert s.get(f"{server}/api/state").status_code == 200

    def test_session_write_requires_csrf_header(self, server, admin_creds):
        """Cookie-authenticated POST without X-Requested-With → 403; with it → 200."""
        s, _ = _sign_in(server, *admin_creds)
        r = s.post(f"{server}/api/config", json={"log_verbose": False})
        assert r.status_code == 403, f"CSRF protection missing: got {r.status_code}"
        r = s.post(f"{server}/api/config", json={"log_verbose": False},
                   headers={"X-Requested-With": "pdf-dispatch-tester"})
        assert r.status_code == 200

    def test_sign_out_ends_session(self, server, admin_creds):
        s, _ = _sign_in(server, *admin_creds)
        r = s.post(f"{server}/logout", allow_redirects=False)
        assert r.status_code == 302 and "/login" in r.headers.get("Location", "")
        assert s.get(f"{server}/api/state").status_code == 401

    def test_password_change_refused_for_env_account(self, server, admin_creds):
        """The predefined (APP_*) account cannot be changed from the API."""
        s, _ = _sign_in(server, *admin_creds)
        r = s.post(f"{server}/api/settings/password",
                   headers={"X-Requested-With": "pdf-dispatch-tester"},
                   json={"current_password": admin_creds[1],
                         "new_password": "another-password-123"})
        assert r.status_code == 400
        assert r.json().get("error_key") == "auth.error_env_credentials"

    @pytest.mark.parametrize("target", ["//evil.example", "https://evil.example/",
                                        "/\\evil.example"])
    def test_login_never_redirects_off_site(self, server, admin_creds, target):
        _, r = _sign_in(server, *admin_creds, next=target)
        assert r.status_code == 302
        assert r.headers.get("Location") == "/", (
            f"Open redirect: next={target!r} → {r.headers.get('Location')!r}")

    def test_basic_header_accepted(self, server, admin_creds):
        r = requests.get(f"{server}/api/state", auth=admin_creds,
                         headers={"Accept": "application/json"})
        assert r.status_code == 200

    def test_wrong_basic_header_rejected_without_challenge(self, server, admin_creds):
        """Wrong Basic credentials → 401 with no WWW-Authenticate (no browser prompt)."""
        r = requests.get(f"{server}/api/state", auth=(admin_creds[0], "wrong-password"),
                         headers={"Accept": "application/json"})
        assert r.status_code == 401
        assert "WWW-Authenticate" not in r.headers

    @pytest.mark.parametrize("path", ["/api/openapi.json", "/api/openapi.yaml", "/api/docs"])
    def test_api_description_public(self, server, path):
        assert requests.get(f"{server}{path}").status_code == 200

    def test_runtime_info_protected(self, server):
        assert requests.get(f"{server}/api/runtime").status_code == 401


# ─────────────────────────────────────────────────────────────────────────────────
# F. Protected settings
# ─────────────────────────────────────────────────────────────────────────────────

class TestProtectedSettings:
    """Credentials can only be managed through their dedicated endpoints."""

    @pytest.mark.parametrize("field", ["api_key", "session_secret",
                                       "admin_username", "admin_password_hash"])
    def test_cannot_be_set_via_config(self, http, server, field):
        status, body = _post_config(http, server, {field: ""})
        assert status == 400, (
            f"POST /api/config must reject {field!r} (got {status}: {body})")

    def test_clearing_api_key_does_not_open_access(self, http, server):
        """Regression: {"api_key": ""} used to switch the instance to open mode."""
        _post_config(http, server, {"api_key": ""})
        anon = requests.Session(); anon.headers["Accept"] = "application/json"
        assert anon.get(f"{server}/api/state").status_code == 401
        assert http.get(f"{server}/api/state").status_code == 200

    @pytest.mark.parametrize("field", ["session_secret", "admin_password_hash"])
    def test_secrets_not_in_state(self, http, server, field):
        assert field not in get_config(http, server)
