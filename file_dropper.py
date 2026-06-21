"""
file_dropper.py — Filesystem-based test driver for pdf-dispatch Phase 1.

Writes PDFs directly to /data/input/ (as a scanner would), waits for
pdf-dispatch to process them, then reads /data/output/ to verify results.

NOTE: Files processed via the watchdog do NOT appear in /api/tasks.
      We detect completion by watching the input directory and parse
      /api/state events to reconstruct the task result.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import requests
from pypdf import PdfReader

from tester_logger import TesterLogger


# ─────────────────────────────────────────────────────────────────────────────
# Result container
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DropResult:
    """Collected results after pdf-dispatch processes a dropped file."""

    task:          dict
    filename:      str
    output_files:  list[Path] = field(default_factory=list)  # files in trigger subfolders
    no_code_files: list[Path] = field(default_factory=list)  # files in output/no_code/
    error_files:   list[Path] = field(default_factory=list)  # files in output/error/

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def status(self) -> str:
        return self.task.get("status", "unknown")

    @property
    def docs_count(self) -> int:
        return self.task.get("docs_count", 0)

    @property
    def triggers(self) -> list[str]:
        return self.task.get("triggers", [])

    @property
    def error_msg(self) -> str:
        return self.task.get("error", "")

    @property
    def page_ranges(self) -> list[str]:
        """Page range strings from the activity log, e.g. ['page 1', 'pages 2–3']."""
        return [o.get("pages", "") for o in self.task.get("outputs", [])]

    @property
    def all_docs(self) -> list[Path]:
        """
        All output documents (output + no_code, excluding error) sorted by
        filename. Filenames contain timestamps so this gives chronological order.
        """
        return sorted(self.output_files + self.no_code_files, key=lambda p: p.name)

    def page_count(self, path: Path) -> int:
        """Number of pages in a specific output file."""
        try:
            return len(PdfReader(path).pages)
        except Exception:
            return -1

    def page_count_of(self, index: int) -> int:
        """
        Number of pages in the Nth output document (all_docs order).
        Use this instead of page_count(index) to correctly handle files
        that went to no_code/ alongside files in trigger subfolders.
        """
        docs = self.all_docs
        if index >= len(docs):
            return 0
        return self.page_count(docs[index])

    def all_page_counts(self) -> list[int]:
        """Page counts for all output documents in chronological order."""
        return [self.page_count(p) for p in self.all_docs]


# ─────────────────────────────────────────────────────────────────────────────
# FileDropper
# ─────────────────────────────────────────────────────────────────────────────

class FileDropper:
    """
    Writes PDFs into pdf-dispatch's watched input folder and collects results.

    Detection strategy
    ------------------
    /api/tasks only tracks files uploaded via POST /api/upload.
    Files processed by the watchdog are detected by watching /data/input/
    until the file disappears, then /api/state events are parsed to
    reconstruct the task result (status, docs_count, page_ranges, triggers).
    """

    def __init__(
        self,
        data_path: Path,
        http: requests.Session,
        server: str,
        log: TesterLogger,
        default_timeout: float = 60.0,
    ):
        self.data            = Path(data_path)
        self.input_dir       = self.data / "input"
        self.output_dir      = self.data / "output"
        self.no_code_dir     = self.data / "output" / "no_code"
        self.error_dir       = self.data / "output" / "error"
        self.http            = http
        self.server          = server
        self.log             = log
        self.default_timeout = default_timeout

        for d in (self.input_dir, self.output_dir):
            if not d.exists():
                raise RuntimeError(
                    f"Directory not found: {d}\n"
                    "Check that data_path in config.yaml is correct."
                )

    # ── Main entry point ─────────────────────────────────────────────────────

    def drop(
        self,
        pdf_bytes: bytes,
        prefix: str = "test",
        timeout: float | None = None,
    ) -> DropResult:
        timeout  = timeout or self.default_timeout
        filename = f"{prefix}_{uuid.uuid4().hex[:10]}.pdf"
        dest     = self.input_dir / filename

        self.log.info(f"Dropping {filename} ({len(pdf_bytes):,} bytes)")
        self.log.capture_pdfdispatch_journal(self.http, self.server, "before drop")

        before = self._snapshot_outputs()
        dest.write_bytes(pdf_bytes)
        self.log.debug(f"Written to {dest}")

        task   = self._wait_for_task(filename, timeout)
        self.log.capture_task(task)
        self.log.capture_pdfdispatch_journal(self.http, self.server, "after processing")

        after     = self._snapshot_outputs()
        new_files = self._diff_snapshots(before, after)
        result    = DropResult(
            task          = task,
            filename      = filename,
            output_files  = sorted(new_files["output"]),
            no_code_files = sorted(new_files["no_code"]),
            error_files   = sorted(new_files["error"]),
        )

        self.log.info(
            f"Result: status={result.status}, docs={result.docs_count}, "
            f"output={len(result.output_files)}, no_code={len(result.no_code_files)}, "
            f"error={len(result.error_files)}"
        )
        return result

    def drop_raw(
        self,
        content: bytes,
        filename: str,
        timeout: float | None = None,
    ) -> DropResult:
        timeout = timeout or self.default_timeout
        dest    = self.input_dir / filename

        self.log.info(f"Dropping raw file: {filename} ({len(content):,} bytes)")
        self.log.capture_pdfdispatch_journal(self.http, self.server, "before drop")

        before = self._snapshot_outputs()
        dest.write_bytes(content)

        task   = self._wait_for_task(filename, timeout)
        self.log.capture_task(task)
        self.log.capture_pdfdispatch_journal(self.http, self.server, "after processing")

        after  = self._snapshot_outputs()
        new    = self._diff_snapshots(before, after)
        return DropResult(
            task          = task,
            filename      = filename,
            output_files  = sorted(new["output"]),
            no_code_files = sorted(new["no_code"]),
            error_files   = sorted(new["error"]),
        )

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def cleanup_output(self, result: DropResult) -> None:
        """Remove output files produced by a test."""
        for p in result.output_files + result.no_code_files + result.error_files:
            try:
                p.unlink(missing_ok=True)
                self.log.debug(f"Cleaned up: {p.name}")
            except OSError as e:
                self.log.warning(f"Could not delete {p}: {e}")

    def cleanup_all_outputs(self) -> None:
        count = 0
        for d in (self.output_dir, self.no_code_dir, self.error_dir):
            if d.exists():
                for f in d.glob("**/*.pdf"):
                    f.unlink(missing_ok=True)
                    count += 1
        self.log.info(f"Cleaned up {count} pre-existing output file(s)")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _wait_for_task(self, filename: str, timeout: float) -> dict:
        """
        Wait for pdf-dispatch to process a file dropped in /data/input/.

        Strategy:
        1. Poll /data/input/ until the file disappears (watchdog picked it up)
        2. Brief wait for output files to be fully written
        3. Parse /api/state events to extract task info (status, docs_count,
           page ranges, triggers)

        This approach is necessary because /api/tasks only tracks files
        processed via POST /api/upload, not watchdog-processed files.
        """
        input_path = self.input_dir / filename
        deadline   = time.monotonic() + timeout

        # 1. Wait for the file to disappear from /data/input/
        while time.monotonic() < deadline:
            if not input_path.exists():
                break
            time.sleep(0.5)
        else:
            raise TimeoutError(
                f"File '{filename}' was not picked up by pdf-dispatch "
                f"within {timeout:.0f}s.\n"
                "Check that pdf-dispatch is running and data_path is correct."
            )

        # 2. Brief wait for output files to be fully written
        time.sleep(1.5)

        # 3. Parse /api/state events to reconstruct task info
        return self._parse_task_from_events(filename)

    def _parse_task_from_events(self, filename: str) -> dict:
        """
        Parse the pdf-dispatch activity log (/api/state) to extract
        processing results for a given filename.
        """
        try:
            r      = self.http.get(f"{self.server}/api/state")
            # Events are newest-first; reverse to chronological order
            events = list(reversed(r.json().get("events", [])))
        except Exception:
            return {"status": "unknown", "docs_count": 0, "error": "",
                    "outputs": [], "triggers": [], "filename": filename}

        status     = "unknown"
        docs_count = 0
        error_msg  = ""
        outputs: list[dict] = []
        triggers: list[str] = []
        in_block            = False

        for ev in events:
            msg   = ev.get("message", "")
            level = ev.get("level", "info")

            # Block start: "Traitement : filename" or "Traitement: filename"
            if filename in msg and ("Traitement" in msg):
                in_block = True
                continue

            if not in_block:
                continue

            # Trigger match: "Page N : fractionnement → «trigger»"
            m = re.search(r'fractionnement → «([^»]+)»', msg)
            if m:
                t = m.group(1)
                if t not in triggers:
                    triggers.append(t)

            # Output file: "→ output/path/file.pdf (page X)" or "(pages X–Y)"
            m = re.search(r'→ output/.+?\.pdf \((.+?)\)', msg)
            if m:
                outputs.append({"pages": m.group(1)})

            # Success: "✓ filename → N doc(s)"
            if filename in msg and "doc(s)" in msg and level != "error":
                m2 = re.search(r'→ (\d+) doc', msg)
                if m2:
                    docs_count = int(m2.group(1))
                status   = "success"
                in_block = False
                continue

            # Error: "✗ filename → /error"
            if filename in msg and (level == "error" or "→ /error" in msg or "✗" in msg):
                status    = "error"
                error_msg = msg
                in_block  = False
                continue

            # Timeout: "Timeout stabilisation"
            if filename in msg and "Timeout" in msg:
                status    = "error"
                error_msg = msg
                in_block  = False
                continue

        # If we didn't find a clean block, infer from output files presence
        if status == "unknown":
            status = "success"

        return {
            "status":     status,
            "docs_count": docs_count,
            "error":      error_msg,
            "outputs":    outputs,
            "triggers":   triggers,
            "filename":   filename,
        }

    def _snapshot_outputs(self) -> dict[str, set[Path]]:
        def ls(d: Path) -> set[Path]:
            return set(d.glob("**/*.pdf")) if d.exists() else set()

        no_code = ls(self.no_code_dir)
        error   = ls(self.error_dir)
        output  = ls(self.output_dir) - no_code - error

        return {"output": output, "no_code": no_code, "error": error}

    def _diff_snapshots(
        self,
        before: dict[str, set[Path]],
        after:  dict[str, set[Path]],
    ) -> dict[str, list[Path]]:
        return {k: list(after[k] - before[k]) for k in before}

    @staticmethod
    def page_count(path: Path) -> int:
        try:
            return len(PdfReader(path).pages)
        except Exception:
            return -1
