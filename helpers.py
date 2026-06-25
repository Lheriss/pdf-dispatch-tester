"""
helpers.py — Shared utilities for pdf-dispatch-tester.

Provides convenience wrappers around the pdf-dispatch REST API and
common patterns used across all test phases.
"""

from __future__ import annotations

import time
from typing import Any

import requests


# ─────────────────────────────────────────────────────────────────────────────
# Task polling
# ─────────────────────────────────────────────────────────────────────────────

def poll_task(
    http: requests.Session,
    server: str,
    task_id: str,
    timeout: float = 30.0,
    interval: float = 0.5,
) -> dict:
    """
    Poll GET /api/tasks/<task_id> until the task reaches a terminal state.

    Returns the task dict on success.
    Raises TimeoutError if the task does not complete within `timeout` seconds.

    ConnectionError is caught and retried transparently: pdf-dispatch may be
    restarting after an OOM kill (exit 137) and should recover within a few
    seconds thanks to Docker's restart: unless-stopped policy.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = http.get(f"{server}/api/tasks/{task_id}", timeout=10.0)
            r.raise_for_status()
            task = r.json()["task"]
            if task["status"] in ("success", "error"):
                return task
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout):
            # pdf-dispatch may be restarting or temporarily unresponsive;
            # wait and retry for the duration of the timeout.
            pass
        time.sleep(interval)
    raise TimeoutError(
        f"Task {task_id!r} did not reach a terminal state within {timeout:.0f}s"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Upload helpers
# ─────────────────────────────────────────────────────────────────────────────

def upload_pdf(
    http: requests.Session,
    server: str,
    pdf_bytes: bytes,
    filename: str = "test.pdf",
    **kwargs: Any,
) -> dict:
    """
    POST /api/upload with a single PDF file.
    Extra keyword arguments are passed as form fields (e.g. split_values,
    separator_placement, trigger, subdirs_by_trigger, delete_source).

    Returns the full JSON response body.
    """
    # Convert any non-string values for multipart encoding
    data = {k: (str(v).lower() if isinstance(v, bool) else str(v))
            for k, v in kwargs.items()}
    r = http.post(
        f"{server}/api/upload",
        files={"file": (filename, pdf_bytes, "application/pdf")},
        data=data,
    )
    r.raise_for_status()
    return r.json()


def upload_and_wait(
    http: requests.Session,
    server: str,
    pdf_bytes: bytes,
    filename: str = "test.pdf",
    timeout: float = 30.0,
    **kwargs: Any,
) -> dict:
    """
    Upload a PDF and block until processing completes.

    Returns the completed task dict.
    Raises AssertionError if the upload itself failed (no saved files).
    """
    result = upload_pdf(http, server, pdf_bytes, filename, **kwargs)
    assert result.get("ok"), f"Upload failed: {result}"
    assert result["saved"], f"No files saved: {result}"
    task_id = result["saved"][0]["task_id"]
    return poll_task(http, server, task_id, timeout=timeout)


def upload_non_pdf(
    http: requests.Session,
    server: str,
    content: bytes,
    filename: str = "test.pdf",
) -> dict:
    """Upload arbitrary bytes (used to test error handling for non-PDF files)."""
    r = http.post(
        f"{server}/api/upload",
        files={"file": (filename, content, "application/octet-stream")},
    )
    r.raise_for_status()
    return r.json()


# ─────────────────────────────────────────────────────────────────────────────
# Configuration helpers
# ─────────────────────────────────────────────────────────────────────────────

def set_config(http: requests.Session, server: str, **kwargs: Any) -> dict:
    """Update one or more configuration keys via POST /api/config."""
    r = http.post(f"{server}/api/config", json=kwargs)
    r.raise_for_status()
    return r.json()


def get_config(http: requests.Session, server: str) -> dict:
    """Return the current app_config from GET /api/state."""
    r = http.get(f"{server}/api/state")
    r.raise_for_status()
    return r.json()["app_config"]


def reset_stats(http: requests.Session, server: str) -> None:
    """Reset processing statistics via POST /api/stats/reset."""
    http.post(f"{server}/api/stats/reset").raise_for_status()


def set_triggers(
    http: requests.Session,
    server: str,
    triggers: list[dict] | None = None,
) -> None:
    """
    Set the split_values list.
    Pass None or [] to clear all triggers (permissive mode: every code splits).

    Example:
        set_triggers(http, server, [
            {"value": "FK3",     "page_handling": "keep",   "case_sensitive": True},
            {"value": "INVOICE", "page_handling": "delete", "case_sensitive": False},
        ])
    """
    set_config(http, server, split_values=triggers or [])


# ─────────────────────────────────────────────────────────────────────────────
# Assertion helpers
# ─────────────────────────────────────────────────────────────────────────────

def assert_task_success(task: dict, docs_count: int | None = None) -> None:
    """Assert a task completed successfully with an optional document count."""
    assert task["status"] == "success", (
        f"Expected success, got {task['status']!r}: {task.get('error', '')}"
    )
    if docs_count is not None:
        assert task["docs_count"] == docs_count, (
            f"Expected {docs_count} document(s), got {task['docs_count']}"
        )


def assert_task_error(task: dict) -> None:
    """Assert a task completed with an error status."""
    assert task["status"] == "error", (
        f"Expected error, got {task['status']!r}"
    )


def assert_page_range(task: dict, doc_index: int, expected: str) -> None:
    """
    Assert that output document at `doc_index` has the expected page range string.

    expected examples: "page 1", "pages 2–3", "pages 1–4"
    """
    outputs = task.get("outputs", [])
    assert len(outputs) > doc_index, (
        f"Task has only {len(outputs)} output(s), cannot check index {doc_index}"
    )
    actual = outputs[doc_index].get("pages", "")
    assert actual == expected, (
        f"Output [{doc_index}] page range: expected {expected!r}, got {actual!r}"
    )


def assert_trigger(task: dict, expected: str | list[str]) -> None:
    """Assert that the task has the expected trigger code(s)."""
    if isinstance(expected, str):
        expected = [expected]
    assert sorted(task.get("triggers", [])) == sorted(expected), (
        f"Triggers: expected {expected}, got {task.get('triggers', [])}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Email / Phase 4 helpers
# ─────────────────────────────────────────────────────────────────────────────

import smtplib
from dataclasses import dataclass, field
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


@dataclass
class EmailDropResult:
    """Files that appeared in output/ after an email was processed."""
    output_files:  list[Path] = field(default_factory=list)
    no_code_files: list[Path] = field(default_factory=list)
    error_files:   list[Path] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.error_files:
            return "error"
        if self.output_files or self.no_code_files:
            return "success"
        return "timeout"

    @property
    def all_files(self) -> list[Path]:
        return self.output_files + self.no_code_files + self.error_files


def send_email(
    smtp_host: str,
    smtp_port: int,
    from_addr: str,
    to_addr: str,
    subject: str,
    attachments: list[tuple[bytes, str]],
    body: str = "Test email from pdf-dispatch-tester.",
) -> None:
    """Send one email with PDF attachment(s) via plain SMTP (no auth)."""
    msg = MIMEMultipart()
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    for pdf_bytes, filename in attachments:
        part = MIMEApplication(pdf_bytes, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)
    with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
        s.sendmail(from_addr, to_addr, msg.as_bytes())


def snapshot_output(data_dir: Path) -> set[Path]:
    """Return all PDF files currently present anywhere under output/."""
    out = data_dir / "output"
    return set(out.rglob("*.pdf")) if out.exists() else set()


def wait_for_new_output(
    data_dir: Path,
    before: set[Path],
    timeout: float = 60.0,
    poll: float = 1.0,
) -> EmailDropResult:
    """
    Poll output/ until at least one new PDF appears (compared to *before*).
    Give it up to *timeout* seconds, then return whatever was found.
    """
    import time
    output   = data_dir / "output"
    no_code  = output / "no_code"
    error    = output / "error"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        after = snapshot_output(data_dir)
        new   = after - before
        if new:
            out_files = [f for f in new if error not in f.parents and no_code not in f.parents]
            nc_files  = [f for f in new if no_code in f.parents]
            err_files = [f for f in new if error in f.parents]
            return EmailDropResult(out_files, nc_files, err_files)
        time.sleep(poll)

    return EmailDropResult()   # timeout — nothing appeared
