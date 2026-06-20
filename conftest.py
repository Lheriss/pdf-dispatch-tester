"""
conftest.py — pytest session fixtures for pdf-dispatch-tester.

Reads configuration from config.yaml (copy config.yaml.example → config.yaml).
Exposes reusable fixtures to all test files.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import requests
import yaml


# ─────────────────────────────────────────────────────────────────────────────
# CLI options
# ─────────────────────────────────────────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        "--config",
        default="config.yaml",
        help="Path to the configuration file (default: config.yaml)",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def cfg(request):
    path = Path(request.config.getoption("--config"))
    if not path.exists():
        pytest.exit(
            f"\n❌ Configuration file not found: {path}\n"
            "   Copy config.yaml.example → config.yaml and fill in your values.\n",
            returncode=1,
        )
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="session")
def server(cfg) -> str:
    return cfg["server"].rstrip("/")


@pytest.fixture(scope="session")
def api_key(cfg) -> str:
    return cfg["api_key"]


@pytest.fixture(scope="session")
def smtp_cfg(cfg) -> dict:
    return cfg.get("smtp", {})


@pytest.fixture(scope="session")
def imap_cfg(cfg) -> dict:
    return cfg.get("imap", {})


@pytest.fixture(scope="session")
def filedrop_path(cfg) -> str | None:
    return cfg.get("filedrop_path") or None


# ─────────────────────────────────────────────────────────────────────────────
# HTTP session
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def http(server, api_key) -> requests.Session:
    """
    Authenticated requests.Session pointing at the test instance.
    All requests automatically include X-API-Key and Accept: application/json.
    """
    s = requests.Session()
    if api_key:
        s.headers["X-API-Key"] = api_key
    s.headers["Accept"] = "application/json"

    # Verify reachability before running any test
    try:
        r = s.get(f"{server}/healthz", timeout=5)
        r.raise_for_status()
    except Exception as exc:
        pytest.exit(
            f"\n❌ Cannot reach pdf-dispatch at {server}\n"
            f"   {exc}\n"
            "   Make sure the test instance is running (see README → Quick start).\n",
            returncode=1,
        )
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Webhook receiver
# ─────────────────────────────────────────────────────────────────────────────

class _WebhookCapture(BaseHTTPRequestHandler):
    """Minimal HTTP server that captures incoming POST requests."""

    received: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        self.received.append({"body": body, "raw": raw, "headers": dict(self.headers)})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass  # suppress HTTP access log


class WebhookServer:
    """Thin wrapper around the capture server with test-friendly helpers."""

    def __init__(self, srv: HTTPServer, url: str):
        self._srv = srv
        self.url  = url

    @property
    def calls(self) -> list[dict]:
        return _WebhookCapture.received

    def clear(self):
        _WebhookCapture.received.clear()

    def wait(self, count: int = 1, timeout: float = 10.0) -> bool:
        """Block until at least `count` webhooks have been received or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.calls) >= count:
                return True
            time.sleep(0.05)
        return False

    def shutdown(self):
        self._srv.shutdown()


@pytest.fixture
def webhook_server(http, server) -> WebhookServer:
    """
    Start a local HTTP webhook receiver on a free port.
    Automatically configures pdf-dispatch to deliver webhooks there.
    Cleans up (disables webhook) after the test.
    """
    _WebhookCapture.received.clear()
    srv  = HTTPServer(("127.0.0.1", 0), _WebhookCapture)
    port = srv.server_address[1]
    url  = f"http://127.0.0.1:{port}"

    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    # Point pdf-dispatch at our receiver
    http.post(f"{server}/api/config", json={
        "webhook_enabled": True,
        "webhook_url":     url,
        "webhook_events":  "all",
        "webhook_secret":  "",
    })

    yield WebhookServer(srv, url)

    # Teardown: disable webhook
    http.post(f"{server}/api/config", json={"webhook_enabled": False, "webhook_url": ""})
    srv.shutdown()
