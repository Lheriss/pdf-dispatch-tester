"""
tests/test_03_webhook.py — Phase 3: Outbound webhook tests.

Tests the outbound webhook: payload schema & field values, HMAC-SHA256 signing,
event-type filtering (all / success / error), and delivery behaviour
(disabled, empty URL, retry on 5xx).

The ``webhook_server`` fixture (conftest.py) provides a local HTTP receiver and
auto-configures pdf-dispatch to deliver to it; it is re-created for every test.

Marker: @pytest.mark.webhook
"""
from __future__ import annotations

import hmac
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from helpers import (
    assert_task_success,
    get_config,
    poll_task,
    set_config,
    set_triggers,
    upload_and_wait,
    upload_pdf,
)
from pdf_generator import make_pdf

pytestmark = pytest.mark.webhook


# ─────────────────────────────────────────────────────────────────────────────
# PDF / payload builders
# ─────────────────────────────────────────────────────────────────────────────

_TRIGGER   = "FK3"
_TRIGGERS  = [{"value": _TRIGGER, "page_handling": "keep", "case_sensitive": True}]


def _pdf_trigger() -> bytes:
    """Two-document PDF separated by a FK3 QR code."""
    return make_pdf([
        {"kind": "content", "text": "Doc 1"},
        {"kind": "qr",      "value": _TRIGGER},
        {"kind": "content", "text": "Doc 2"},
    ])


def _pdf_plain() -> bytes:
    """Plain PDF with no trigger codes — goes to no_code/ on success."""
    return make_pdf([{"kind": "content", "text": f"Page {i}"} for i in range(2)])


def _corrupt_pdf() -> bytes:
    """Bytes that are not a valid PDF → processing ends in status=error."""
    return b"not-a-pdf\x00" * 8


# ─────────────────────────────────────────────────────────────────────────────
# Test-local helpers
# ─────────────────────────────────────────────────────────────────────────────

def _upload_wait_any(http, server, pdf_bytes, filename="test.pdf", timeout=30) -> dict:
    """Upload a PDF and block until the task reaches any terminal state (success *or* error)."""
    r = upload_pdf(http, server, pdf_bytes, filename)
    assert r.get("ok"),    f"Upload rejected: {r}"
    assert r.get("saved"), f"Upload returned no saved file: {r}"
    return poll_task(http, server, r["saved"][0]["task_id"], timeout=timeout)


def _wait_event(ws, status: str, timeout: float = 12.0) -> dict | None:
    """Return the first captured webhook call whose body.status matches, or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for call in ws.calls:
            if call["body"].get("status") == status:
                return call
        time.sleep(0.1)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 1. TestWebhookPayloadStructure
#    Verify the "fat event" schema sent by pdf-dispatch.
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookPayloadStructure:
    """
    The webhook body must contain all documented fields with the correct types.
    Full schema (fat event — receiver needs no follow-up call):
      event        str  — always "file.processed"
      timestamp    str  — ISO 8601, second precision
      source_file  str  — basename only
      status       str  — "success" | "error"
      triggers     list — detected trigger code values
      documents    list — [{filename, path}] for each output PDF
      docs_count   int  — number of output documents
      error        str  — empty on success, non-empty on error
    """

    @pytest.fixture(autouse=True)
    def _triggers(self, http, server):
        original = get_config(http, server).get("split_values", [])
        set_triggers(http, server, _TRIGGERS)
        yield
        set_config(http, server, split_values=original)

    # ── Schema ────────────────────────────────────────────────────────────────

    def test_all_mandatory_fields_present(self, http, server, webhook_server):
        """All 8 payload fields must be present after a successful upload."""
        upload_and_wait(http, server, _pdf_trigger(), timeout=30)
        assert webhook_server.wait(1, timeout=12), "No webhook received within 12 s"
        body = webhook_server.calls[0]["body"]
        for field in ("event", "timestamp", "source_file", "status",
                      "triggers", "documents", "docs_count", "error"):
            assert field in body, f"Missing field in webhook body: {field!r}"

    def test_field_types(self, http, server, webhook_server):
        """Each payload field must carry the documented Python type."""
        upload_and_wait(http, server, _pdf_trigger(), timeout=30)
        assert webhook_server.wait(1, timeout=12)
        b = webhook_server.calls[0]["body"]
        assert isinstance(b["event"],       str),  "event must be str"
        assert isinstance(b["timestamp"],   str),  "timestamp must be str"
        assert isinstance(b["source_file"], str),  "source_file must be str"
        assert isinstance(b["status"],      str),  "status must be str"
        assert isinstance(b["triggers"],    list), "triggers must be list"
        assert isinstance(b["documents"],   list), "documents must be list"
        assert isinstance(b["docs_count"],  int),  "docs_count must be int"
        assert isinstance(b["error"],       str),  "error must be str"

    # ── Values on success ─────────────────────────────────────────────────────

    def test_event_name(self, http, server, webhook_server):
        """event must always be the literal string 'file.processed'."""
        upload_and_wait(http, server, _pdf_trigger(), timeout=30)
        assert webhook_server.wait(1, timeout=12)
        assert webhook_server.calls[0]["body"]["event"] == "file.processed"

    def test_success_status_and_counts(self, http, server, webhook_server):
        """status=success, docs_count=2, trigger list contains FK3, error is empty."""
        upload_and_wait(http, server, _pdf_trigger(), "split.pdf", timeout=30)
        assert webhook_server.wait(1, timeout=12)
        b = webhook_server.calls[0]["body"]
        assert b["status"]    == "success"
        assert b["docs_count"] == 2
        assert _TRIGGER in b["triggers"], f"{_TRIGGER!r} not in triggers {b['triggers']}"
        assert b["error"] == ""

    def test_source_file_is_basename(self, http, server, webhook_server):
        """source_file must be a plain filename — no directory separators."""
        upload_and_wait(http, server, _pdf_trigger(), "myfile.pdf", timeout=30)
        assert webhook_server.wait(1, timeout=12)
        sf = webhook_server.calls[0]["body"]["source_file"]
        assert sf == "myfile.pdf"
        assert "/" not in sf and "\\" not in sf, f"source_file contains path: {sf!r}"

    def test_timestamp_iso8601_seconds(self, http, server, webhook_server):
        """timestamp must match YYYY-MM-DDTHH:MM:SS (ISO 8601, second precision)."""
        upload_and_wait(http, server, _pdf_trigger(), timeout=30)
        assert webhook_server.wait(1, timeout=12)
        ts = webhook_server.calls[0]["body"]["timestamp"]
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", ts), (
            f"timestamp {ts!r} does not match ISO 8601"
        )

    def test_documents_list_on_success(self, http, server, webhook_server):
        """documents list must be non-empty; each entry needs filename and path fields."""
        upload_and_wait(http, server, _pdf_trigger(), timeout=30)
        assert webhook_server.wait(1, timeout=12)
        docs = webhook_server.calls[0]["body"]["documents"]
        assert len(docs) > 0, "documents list is empty on success"
        for doc in docs:
            assert "filename" in doc, f"document entry missing 'filename': {doc}"
            assert "path"     in doc, f"document entry missing 'path': {doc}"
            assert doc["filename"].lower().endswith(".pdf"), (
                f"document filename not a PDF: {doc['filename']!r}"
            )

    def test_no_code_pdf_success_zero_docs(self, http, server, webhook_server):
        """A PDF with no trigger codes → success, docs_count=0 (no_code path)."""
        set_triggers(http, server, [])   # clear → every code triggers, plain PDF → no_code
        upload_and_wait(http, server, _pdf_plain(), timeout=30)
        set_triggers(http, server, _TRIGGERS)
        assert webhook_server.wait(1, timeout=12)
        b = webhook_server.calls[0]["body"]
        assert b["status"]    == "success"
        assert b["docs_count"] == 0

    # ── Error payload ─────────────────────────────────────────────────────────

    def test_error_payload_structure(self, http, server, webhook_server):
        """Corrupt file → status=error, non-empty error field, docs_count=0."""
        task = _upload_wait_any(http, server, _corrupt_pdf(), "corrupt.pdf")
        assert task["status"] == "error", "Corrupt PDF task expected error status"
        call = _wait_event(webhook_server, "error", timeout=15)
        assert call is not None, "No error webhook received within 15 s"
        b = call["body"]
        assert b["event"]      == "file.processed"
        assert b["status"]     == "error"
        assert b["error"]      != "", "error field must not be empty on status=error"
        assert b["docs_count"] == 0,  "docs_count must be 0 on error"


# ─────────────────────────────────────────────────────────────────────────────
# 2. TestWebhookHmac
#    X-Signature header: absent without a secret; HMAC-SHA256 when secret is set.
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookHmac:
    """
    Signing behaviour:
      - No secret → X-Signature header absent.
      - Secret set → X-Signature: sha256=<hex> present and verifiable.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, http, server):
        original = get_config(http, server).get("split_values", [])
        set_triggers(http, server, _TRIGGERS)
        yield
        # Reset secret after each test (webhook_server fixture resets it too,
        # but being explicit avoids cross-test contamination)
        set_config(http, server, split_values=original, webhook_secret="")

    def test_no_secret_no_signature_header(self, http, server, webhook_server):
        """When webhook_secret is empty, X-Signature must NOT appear in the headers."""
        # webhook_server fixture already sets webhook_secret=""
        upload_and_wait(http, server, _pdf_trigger(), timeout=30)
        assert webhook_server.wait(1, timeout=12)
        header_keys = {k.lower() for k in webhook_server.calls[0]["headers"]}
        assert "x-signature" not in header_keys, (
            "X-Signature header present despite no webhook_secret"
        )

    def test_with_secret_signature_header_present(self, http, server, webhook_server):
        """When webhook_secret is set, X-Signature must appear with prefix 'sha256='."""
        set_config(http, server, webhook_secret="my-integration-secret")
        upload_and_wait(http, server, _pdf_trigger(), timeout=30)
        assert webhook_server.wait(1, timeout=12)
        headers_lower = {k.lower(): v for k, v in webhook_server.calls[0]["headers"].items()}
        assert "x-signature" in headers_lower, (
            "X-Signature header absent despite webhook_secret being set"
        )
        sig = headers_lower["x-signature"]
        assert sig.startswith("sha256="), (
            f"Unexpected X-Signature format (expected 'sha256=…'): {sig!r}"
        )

    def test_hmac_sha256_recomputation_matches(self, http, server, webhook_server):
        """
        The receiver can independently compute HMAC-SHA256 over the raw body
        and compare it to X-Signature — the digests must match exactly.
        """
        secret = "phase-3-hmac-test-key-42"
        set_config(http, server, webhook_secret=secret)
        upload_and_wait(http, server, _pdf_trigger(), timeout=30)
        assert webhook_server.wait(1, timeout=12)
        call      = webhook_server.calls[0]
        raw_body  = call["raw"]
        headers   = {k.lower(): v for k, v in call["headers"].items()}
        sig_hdr   = headers.get("x-signature", "")
        assert sig_hdr.startswith("sha256="), f"Malformed X-Signature: {sig_hdr!r}"
        received  = sig_hdr[len("sha256="):]
        expected  = hmac.new(
            secret.encode("utf-8"), raw_body, "sha256"
        ).hexdigest()
        assert hmac.compare_digest(received, expected), (
            f"HMAC mismatch:\n  header:   {received}\n  computed: {expected}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. TestWebhookFilter
#    webhook_events = "all" | "success" | "error"
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookFilter:
    """
    pdf-dispatch only delivers webhook calls whose event type matches
    the configured webhook_events value.
    """

    @pytest.fixture(autouse=True)
    def _triggers(self, http, server):
        original = get_config(http, server).get("split_values", [])
        set_triggers(http, server, _TRIGGERS)
        yield
        set_config(http, server, split_values=original)

    # ── "all" ─────────────────────────────────────────────────────────────────

    def test_all_receives_success_events(self, http, server, webhook_server):
        """webhook_events=all → success events are delivered."""
        set_config(http, server, webhook_events="all")
        upload_and_wait(http, server, _pdf_trigger(), timeout=30)
        assert webhook_server.wait(1, timeout=12), "No webhook received"
        assert any(c["body"]["status"] == "success" for c in webhook_server.calls)

    def test_all_receives_error_events(self, http, server, webhook_server):
        """webhook_events=all → error events are also delivered."""
        set_config(http, server, webhook_events="all")
        task = _upload_wait_any(http, server, _corrupt_pdf())
        assert task["status"] == "error"
        call = _wait_event(webhook_server, "error", timeout=15)
        assert call is not None, "error webhook not received under webhook_events=all"

    # ── "success" ─────────────────────────────────────────────────────────────

    def test_success_filter_delivers_success(self, http, server, webhook_server):
        """webhook_events=success → success events are delivered."""
        set_config(http, server, webhook_events="success")
        upload_and_wait(http, server, _pdf_trigger(), timeout=30)
        assert webhook_server.wait(1, timeout=12), "success webhook not received"

    def test_success_filter_suppresses_error(self, http, server, webhook_server):
        """webhook_events=success → error events must NOT be delivered."""
        set_config(http, server, webhook_events="success")
        task = _upload_wait_any(http, server, _corrupt_pdf())
        assert task["status"] == "error"
        # Allow enough time for any (unwanted) delivery to arrive
        time.sleep(4)
        error_calls = [c for c in webhook_server.calls
                       if c["body"].get("status") == "error"]
        assert error_calls == [], (
            f"error webhook received despite webhook_events=success: {error_calls}"
        )

    # ── "error" ───────────────────────────────────────────────────────────────

    def test_error_filter_delivers_error(self, http, server, webhook_server):
        """webhook_events=error → error events are delivered."""
        set_config(http, server, webhook_events="error")
        task = _upload_wait_any(http, server, _corrupt_pdf())
        assert task["status"] == "error"
        call = _wait_event(webhook_server, "error", timeout=15)
        assert call is not None, "error webhook not received under webhook_events=error"

    def test_error_filter_suppresses_success(self, http, server, webhook_server):
        """webhook_events=error → success events must NOT be delivered."""
        set_config(http, server, webhook_events="error")
        upload_and_wait(http, server, _pdf_trigger(), timeout=30)
        time.sleep(4)
        success_calls = [c for c in webhook_server.calls
                         if c["body"].get("status") == "success"]
        assert success_calls == [], (
            f"success webhook received despite webhook_events=error: {success_calls}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. TestWebhookDelivery
#    Webhook must not fire when disabled or URL is empty.
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookDelivery:
    """Sanity checks on enabled/URL guards."""

    @pytest.fixture(autouse=True)
    def _restore(self, http, server):
        original = get_config(http, server).get("split_values", [])
        set_triggers(http, server, _TRIGGERS)
        yield
        set_config(http, server,
                   split_values=original,
                   webhook_enabled=False,
                   webhook_url="",
                   webhook_secret="")

    def test_disabled_no_delivery(self, http, server, webhook_server):
        """webhook_enabled=False → no HTTP call is ever made, even when URL is set."""
        # Override the fixture's enabled=True
        set_config(http, server, webhook_enabled=False)
        upload_and_wait(http, server, _pdf_trigger(), timeout=30)
        time.sleep(3)
        assert webhook_server.calls == [], (
            f"Webhook fired despite webhook_enabled=False: {webhook_server.calls}"
        )

    def test_empty_url_no_delivery(self, http, server, webhook_server):
        """webhook_url='' → no delivery even if webhook_enabled=True."""
        # Keep enabled=True but clear the URL
        set_config(http, server, webhook_url="")
        upload_and_wait(http, server, _pdf_trigger(), timeout=30)
        time.sleep(3)
        assert webhook_server.calls == [], (
            f"Webhook fired despite empty webhook_url: {webhook_server.calls}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. TestWebhookRetry
#    pdf-dispatch retries delivery on non-2xx responses (up to 3 attempts).
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
class TestWebhookRetry:
    """
    Delivery retry: if the endpoint returns 5xx, pdf-dispatch retries
    with exponential backoff before giving up.
    The implementation uses up to 3 attempts (delays: 0 s, 1 s, 2 s).
    """

    @pytest.fixture(autouse=True)
    def _setup(self, http, server):
        original = get_config(http, server).get("split_values", [])
        set_triggers(http, server, _TRIGGERS)
        yield
        set_config(http, server,
                   split_values=original,
                   webhook_enabled=False,
                   webhook_url="",
                   webhook_secret="")

    def test_retry_on_5xx_eventually_delivers(self, http, server):
        """
        When the receiver returns 503 on the first call, pdf-dispatch retries
        and eventually delivers a 200 response.
        Both a 503 and a 200 must be observed on the receiver side.
        """
        response_codes: list[int] = []
        success_bodies: list[dict] = []
        fail_remaining = [1]   # fail the first N calls

        class _FailFirstHandler(BaseHTTPRequestHandler):
            def do_POST(self):                          # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                raw    = self.rfile.read(length)
                if fail_remaining[0] > 0:
                    fail_remaining[0] -= 1
                    response_codes.append(503)
                    self.send_response(503)
                    self.end_headers()
                else:
                    try:
                        body = json.loads(raw)
                    except Exception:
                        body = {}
                    response_codes.append(200)
                    success_bodies.append(body)
                    self.send_response(200)
                    self.end_headers()

            def log_message(self, *args):               # silence stdlib logging
                pass

        srv    = HTTPServer(("0.0.0.0", 0), _FailFirstHandler)
        port   = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            set_config(http, server,
                       webhook_enabled=True,
                       webhook_url=f"http://localhost:{port}",
                       webhook_events="all",
                       webhook_secret="")
            upload_and_wait(http, server, _pdf_trigger(), timeout=30)

            # Wait for the retry cycle: first 503 (immediate) + retry after ≤2 s
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and not success_bodies:
                time.sleep(0.2)

            assert 503 in response_codes, (
                f"Expected at least one 503 response; got: {response_codes}"
            )
            assert success_bodies, (
                f"Webhook never delivered after retry; response codes: {response_codes}"
            )
            assert success_bodies[0]["status"] == "success"
        finally:
            srv.shutdown()
