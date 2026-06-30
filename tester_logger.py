"""
tester_logger.py — Structured logging for pdf-dispatch-tester.

Creates a timestamped log directory for each test run containing:

  session.log          — human-readable chronological log of everything
  http_traffic.jsonl   — one JSON object per HTTP call (request + response)
  pdfdispatch.log      — pdf-dispatch's own activity journal, fetched at key points

Design goals
------------
- Every outgoing HTTP request and its response are logged in full.
- Every test PDF generated is logged with its full page spec.
- pdf-dispatch's own activity log is captured before and after each
  test so that processing events are preserved alongside test assertions.
- On test failure the log contains everything needed to diagnose whether
  the bug is in pdf-dispatch or in the tester itself.

Usage (automatic via conftest.py)
----------------------------------
The TesterLogger is instantiated once per test session in conftest.py
and injected as a fixture. Individual tests call:

    log.info("Uploading 4-page PDF with FK3 trigger")
    log.capture_pdfdispatch_journal(http, server, label="before upload")
    task = upload_and_wait(...)
    log.capture_pdfdispatch_journal(http, server, label="after processing")
    log.test_result("PASS", {"docs_count": task["docs_count"]})
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

_DIVIDER = "─" * 72


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _truncate(obj: Any, max_len: int = 400) -> Any:
    """Truncate long strings / bytes in a dict/list/scalar for readable logging."""
    if isinstance(obj, bytes):
        return f"<bytes len={len(obj)}>"
    if isinstance(obj, str) and len(obj) > max_len:
        return obj[:max_len] + f"… [+{len(obj) - max_len} chars]"
    if isinstance(obj, dict):
        return {k: _truncate(v, max_len) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) > 20:
            return [_truncate(x, max_len) for x in obj[:20]] + [f"… [{len(obj) - 20} more]"]
        return [_truncate(x, max_len) for x in obj]
    return obj


# ─────────────────────────────────────────────────────────────────────────────
# TesterLogger
# ─────────────────────────────────────────────────────────────────────────────

class TesterLogger:
    """
    Central logger for a pdf-dispatch-tester session.

    Parameters
    ----------
    log_dir : Path
        Root directory for log output.  A timestamped sub-directory is
        created automatically (e.g. logs/2026-06-20_19-55-34/).
    level : int
        Python logging level for the session.log file (default: DEBUG).
    """

    def __init__(self, log_dir: Path = Path("logs"), level: int = logging.DEBUG):
        run_id   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.run_dir = log_dir / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.run_id  = run_id
        self._http_log   = open(self.run_dir / "http_traffic.jsonl", "w", encoding="utf-8")
        self._pdfd_log   = open(self.run_dir / "pdfdispatch.log",    "w", encoding="utf-8")
        self._pdfd_cursor = 0  # chronological index of next unseen event

        # Human-readable session log
        self._log = logging.getLogger(f"tester.{run_id}")
        self._log.setLevel(level)
        # propagate=True: lets pytest's --log-cli mechanism (enabled in
        # web_runner.py for WEB_MODE) mirror every log.info()/error() call
        # into the live SSE journal shown in the web UI, in addition to the
        # file/console handlers below. Root has no handlers of its own, so
        # propagation is a no-op outside of pytest's live-log capture.
        self._log.propagate = True

        fmt = logging.Formatter("%(asctime)s.%(msecs)03d  %(levelname)-7s  %(message)s",
                                datefmt="%H:%M:%S")

        # File handler
        fh = logging.FileHandler(self.run_dir / "session.log", encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(fmt)
        self._log.addHandler(fh)

        # Console handler (INFO and above)
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        self._log.addHandler(ch)

        self._log.info(_DIVIDER)
        self._log.info(f"pdf-dispatch-tester  run={run_id}")
        self._log.info(f"Log directory: {self.run_dir}")
        self._log.info(_DIVIDER)

        self._current_test: str | None = None
        self._test_start:   float      = 0.0

    # ── Test lifecycle ────────────────────────────────────────────────────────

    def begin_test(self, test_name: str) -> None:
        """Call at the start of each test (done automatically via conftest hook)."""
        self._current_test = test_name
        self._test_start   = time.monotonic()
        self._log.info("")
        self._log.info(_DIVIDER)
        self._log.info(f"TEST  {test_name}")
        self._log.info(_DIVIDER)

    def end_test(self, outcome: str, detail: dict | None = None) -> None:
        """Call at the end of each test with 'PASS', 'FAIL', or 'ERROR'."""
        elapsed = time.monotonic() - self._test_start
        msg     = f"→ {outcome}  ({elapsed:.2f}s)"
        if detail:
            msg += f"  {json.dumps(_truncate(detail))}"
        if outcome == "PASS":
            self._log.info(msg)
        else:
            self._log.error(msg)

    # ── General logging ───────────────────────────────────────────────────────

    def info(self, message: str) -> None:
        self._log.info(message)

    def debug(self, message: str) -> None:
        self._log.debug(message)

    def warning(self, message: str) -> None:
        self._log.warning(message)

    def error(self, message: str) -> None:
        self._log.error(message)

    def pdf_generated(self, spec: list[dict], filename: str) -> None:
        """Log the page spec of a generated test PDF."""
        self._log.debug(f"PDF generated: {filename}")
        for i, page in enumerate(spec, 1):
            self._log.debug(f"  p{i:02d}  {page}")

    # ── HTTP traffic ──────────────────────────────────────────────────────────

    def hook_session(self, session: requests.Session) -> None:
        """
        Attach response hooks to a requests.Session so every HTTP call is
        automatically logged to session.log and http_traffic.jsonl.
        """
        session.hooks["response"].append(self._on_response)

    def _on_response(self, response: requests.Response, *args, **kwargs) -> None:
        req = response.request

        # Extract request body
        try:
            try:
                req_body = json.loads(req.body) if req.body else None
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                req_body = f"<binary {len(req.body)} bytes>" if req.body else None
        except (json.JSONDecodeError, TypeError):
            req_body = str(req.body)[:300] if req.body else None

        # Extract response body
        try:
            resp_body = response.json()
        except Exception:
            resp_body = response.text[:500] if response.text else None

        elapsed_ms = int(response.elapsed.total_seconds() * 1000)

        record = {
            "ts":          _ts(),
            "test":        self._current_test,
            "method":      req.method,
            "url":         req.url,
            "req_headers": dict(req.headers),
            "req_body":    _truncate(req_body),
            "status":      response.status_code,
            "elapsed_ms":  elapsed_ms,
            "resp_body":   _truncate(resp_body),
        }

        # JSONL
        self._http_log.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._http_log.flush()

        # session.log (condensed)
        ok = "✓" if response.ok else "✗"
        self._log.debug(
            f"{ok} {req.method:6} {req.url}  →  {response.status_code}  ({elapsed_ms}ms)"
        )
        if not response.ok:
            self._log.warning(f"   Response body: {_truncate(resp_body)}")
        elif resp_body and isinstance(resp_body, dict):
            # Log key fields on success
            interesting = {k: resp_body[k] for k in
                           ("ok", "status", "docs_count", "error", "saved", "task")
                           if k in resp_body}
            if interesting:
                self._log.debug(f"   ↳ {_truncate(interesting)}")

    # ── pdf-dispatch activity journal ─────────────────────────────────────────

    def capture_pdfdispatch_journal(
        self,
        http: requests.Session,
        server: str,
        label: str = "",
    ) -> list[dict]:
        """
        Fetch new events from pdf-dispatch's activity log (/api/state) and
        append ONLY the events not yet written to pdfdispatch.log.

        A cursor tracks the chronological position of the last written event,
        so each event appears exactly once across the session — eliminating
        the duplicate startup-message noise from repeated captures.
        """
        try:
            r = http.get(f"{server}/api/state")
            # events from API are newest-first; reverse to chronological order
            all_events = list(reversed(r.json().get("events", [])))
        except Exception as exc:
            self._log.warning(f"Could not fetch pdf-dispatch journal: {exc}")
            return []

        new_events = all_events[self._pdfd_cursor:]
        self._pdfd_cursor = len(all_events)

        if not new_events:
            return []

        if label:
            self._pdfd_log.write(f"\n── {label} ──\n")

        for ev in new_events:
            level = ev.get("level", "info").upper()
            ts    = ev.get("ts", "")
            msg   = ev.get("message", "")
            self._pdfd_log.write(f"{ts}  {level:<7}  {msg}\n")

        self._pdfd_log.flush()
        self._log.debug(
            f"  ↳ pdf-dispatch journal: {len(new_events)} new event(s)"
            + (f" — {label}" if label else "")
        )
        return new_events

    def capture_task(self, task: dict) -> None:
        """Log a completed task dict in full detail."""
        self._log.debug(f"Task detail: {json.dumps(_truncate(task), indent=2, ensure_ascii=False)}")

    # ── Finalise ──────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Flush and close log files. Called at end of session."""
        self._log.info("")
        self._log.info(_DIVIDER)
        self._log.info(f"Session ended — logs in {self.run_dir}")
        self._log.info(_DIVIDER)
        self._http_log.close()
        self._pdfd_log.close()
        for handler in self._log.handlers[:]:
            handler.close()
            self._log.removeHandler(handler)

    def summary_path(self) -> Path:
        return self.run_dir / "session.log"
