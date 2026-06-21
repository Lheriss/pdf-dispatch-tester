"""
conftest.py — pytest session fixtures for pdf-dispatch-tester.

Reads configuration from config.yaml (copy config.yaml.example → config.yaml).
Exposes reusable fixtures to all test files.
All HTTP traffic and pdf-dispatch journal entries are automatically logged.
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

from tester_logger import TesterLogger


# ─────────────────────────────────────────────────────────────────────────────
# CLI options
# ─────────────────────────────────────────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption("--config", default="config.yaml",
                     help="Path to the configuration file (default: config.yaml)")


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
# Logger (session-scoped — one log directory per pytest run)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def log() -> TesterLogger:
    logger = TesterLogger(log_dir=Path("logs"))
    yield logger
    logger.close()


# ─────────────────────────────────────────────────────────────────────────────
# HTTP session
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def http(server, api_key, log) -> requests.Session:
    """
    Authenticated requests.Session pointing at the test instance.
    All HTTP traffic is automatically logged to logs/<run>/http_traffic.jsonl.
    """
    s = requests.Session()
    if api_key:
        s.headers["X-API-Key"] = api_key
    s.headers["Accept"] = "application/json"

    # Attach logging hooks BEFORE the connectivity check
    log.hook_session(s)

    try:
        r = s.get(f"{server}/healthz", timeout=5)
        r.raise_for_status()
        log.info(f"✓ Connected to pdf-dispatch at {server}")
    except Exception as exc:
        pytest.exit(
            f"\n❌ Cannot reach pdf-dispatch at {server}\n"
            f"   {exc}\n"
            "   Make sure the test instance is running (see README → Quick start).\n",
            returncode=1,
        )
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Automatic per-test logging via pytest hooks
# ─────────────────────────────────────────────────────────────────────────────

def pytest_runtest_setup(item):
    """Log the start of each test (accesses the session-scoped log fixture)."""
    try:
        logger = item.session._store.get("tester_log", None)
        if logger:
            logger.begin_test(item.nodeid)
    except Exception:
        pass


def pytest_runtest_logreport(report):
    """Log test outcome after the call phase."""
    if report.when != "call":
        return
    try:
        logger = report.fspath  # will fail gracefully if not available
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _auto_log(request, log):
    """
    Auto-use fixture: logs test start/end for every test automatically.
    Tests may also call log.info(), log.capture_pdfdispatch_journal(), etc.
    """
    log.begin_test(request.node.nodeid)
    yield
    # Outcome is available after yield
    outcome = "UNKNOWN"
    if hasattr(request.node, "rep_call"):
        rep = request.node.rep_call
        outcome = "PASS" if rep.passed else ("FAIL" if rep.failed else "ERROR")
    log.end_test(outcome)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Attach the call-phase report to the item so _auto_log can read it."""
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call":
        item.rep_call = rep


# ─────────────────────────────────────────────────────────────────────────────
# Webhook receiver
# ─────────────────────────────────────────────────────────────────────────────

class _WebhookCapture(BaseHTTPRequestHandler):
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
        pass


class WebhookServer:
    def __init__(self, srv: HTTPServer, url: str):
        self._srv = srv
        self.url  = url

    @property
    def calls(self) -> list[dict]:
        return _WebhookCapture.received

    def clear(self):
        _WebhookCapture.received.clear()

    def wait(self, count: int = 1, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.calls) >= count:
                return True
            time.sleep(0.05)
        return False

    def shutdown(self):
        self._srv.shutdown()


@pytest.fixture
def webhook_server(cfg, http, server, log) -> WebhookServer:
    """
    Local webhook receiver. Configures pdf-dispatch to deliver to it,
    and restores the previous webhook configuration after the test.

    Uses webhook_host and webhook_port from config.yaml (written by
    entrypoint.sh from the auto-detected Docker gateway IP).
    """
    _WebhookCapture.received.clear()
    wh_host = str(cfg.get("webhook_host", "127.0.0.1"))
    wh_port = int(cfg.get("webhook_port", 5882))
    srv  = HTTPServer(("0.0.0.0", wh_port), _WebhookCapture)
    port = srv.server_address[1]
    url  = f"http://{wh_host}:{port}"

    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    http.post(f"{server}/api/config", json={
        "webhook_enabled": True,
        "webhook_url":     url,
        "webhook_events":  "all",
        "webhook_secret":  "",
    })
    log.info(f"Webhook receiver started on {url}")

    yield WebhookServer(srv, url)

    http.post(f"{server}/api/config", json={"webhook_enabled": False, "webhook_url": ""})
    srv.shutdown()


# ─────────────────────────────────────────────────────────────────────────────
# Session-start cleanup — restore /data to baseline state
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def _clean_data_on_start(request, log):
    """
    Remove all output files left from previous test sessions before any test runs.
    Keeps the standard directory structure (error/, no_code/, processed/).
    Custom trigger subdirectories (FK3/, INVOICE/, etc.) are deleted entirely.

    Reads config.yaml directly (without depending on the cfg fixture) so this
    fixture is safe to run even when no config.yaml exists (e.g. in CI for
    Phase 0 self-tests that don't need a server or data directory).
    """
    import shutil
    config_path = Path(request.config.getoption("--config", default="config.yaml"))
    if not config_path.exists():
        log.debug("No config.yaml found — skipping data cleanup")
        return
    try:
        with open(config_path, encoding="utf-8") as f:
            _cfg = yaml.safe_load(f)
        data_path = (_cfg or {}).get("data_path", "")
    except Exception as e:
        log.warning(f"Could not read config for cleanup: {e}")
        return
    if not data_path:
        return

    data       = Path(data_path)
    output_dir = data / "output"
    input_dir  = data / "input"
    standard   = {"error", "no_code", "processed"}
    count      = 0

    log.info("=== Cleaning /data from previous sessions ===")

    if output_dir.exists():
        for item in list(output_dir.iterdir()):
            if item.is_dir():
                if item.name in standard:
                    # Clear files inside standard dirs, keep the dir
                    for f in item.rglob("*"):
                        if f.is_file():
                            f.unlink(missing_ok=True)
                            count += 1
                else:
                    # Remove custom trigger subdirs entirely
                    shutil.rmtree(item, ignore_errors=True)
                    count += 1
            elif item.is_file():
                item.unlink(missing_ok=True)
                count += 1

    # Clear any stale input files (e.g. from a crashed previous run)
    if input_dir.exists():
        for f in input_dir.glob("*.pdf"):
            f.unlink(missing_ok=True)
            count += 1

    log.info(f"Cleanup done: {count} item(s) removed. /data is in baseline state.")
