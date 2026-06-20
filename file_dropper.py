"""
file_dropper.py — Filesystem-based test driver for pdf-dispatch Phase 1.

Writes PDFs directly to /data/input/ (as a scanner would), waits for
pdf-dispatch to process them, then reads /data/output/ to verify results.

Both the tester and pdf-dispatch mount the same host directory, so no
network transfer is needed for file delivery or output inspection.
"""

from __future__ import annotations

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

    task:          dict                    # full API task dict
    filename:      str                     # original dropped filename
    output_files:  list[Path] = field(default_factory=list)  # files in output/ (split docs)
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
        """Page range string from each task output, e.g. ['page 1', 'pages 2–3']."""
        return [o.get("pages", "") for o in self.task.get("outputs", [])]

    def page_count(self, index: int) -> int:
        """Number of pages in output file at `index`, read directly from the PDF."""
        if index >= len(self.output_files):
            return 0
        return len(PdfReader(self.output_files[index]).pages)

    def all_page_counts(self) -> list[int]:
        """Page counts for all output files."""
        return [self.page_count(i) for i in range(len(self.output_files))]


# ─────────────────────────────────────────────────────────────────────────────
# FileDropper
# ─────────────────────────────────────────────────────────────────────────────

class FileDropper:
    """
    Writes PDFs into pdf-dispatch's watched input folder and collects results.

    Parameters
    ----------
    data_path : Path
        Path to the pdf-dispatch /data directory, accessible from the tester.
    http : requests.Session
        Authenticated session for API calls (task polling, config).
    server : str
        Base URL of the pdf-dispatch test instance.
    log : TesterLogger
    default_timeout : float
        Seconds to wait for a task to complete (default 30s).
    """

    def __init__(
        self,
        data_path: Path,
        http: requests.Session,
        server: str,
        log: TesterLogger,
        default_timeout: float = 30.0,
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

        # Verify directories exist
        for d in (self.input_dir, self.output_dir):
            if not d.exists():
                raise RuntimeError(
                    f"Directory not found: {d}\n"
                    "Check that data_path in config.yaml is correct and "
                    "that /data is mounted in the tester container."
                )

    # ── Main entry point ─────────────────────────────────────────────────────

    def drop(
        self,
        pdf_bytes: bytes,
        prefix: str = "test",
        timeout: float | None = None,
    ) -> DropResult:
        """
        Write `pdf_bytes` to the watched input folder and wait for processing.

        Returns a DropResult with the task dict and paths of all output files.
        """
        timeout  = timeout or self.default_timeout
        filename = f"{prefix}_{uuid.uuid4().hex[:10]}.pdf"
        dest     = self.input_dir / filename

        self.log.info(f"Dropping {filename} ({len(pdf_bytes):,} bytes)")
        self.log.capture_pdfdispatch_journal(self.http, self.server, "before drop")

        # Snapshot existing output files before the drop
        before = self._snapshot_outputs()

        # Write the file — watchdog picks it up within FILE_STABLE_INTERVAL seconds
        dest.write_bytes(pdf_bytes)
        self.log.debug(f"Written to {dest}")

        # Poll the API until the task appears and completes
        task = self._wait_for_task(filename, timeout)
        self.log.capture_task(task)
        self.log.capture_pdfdispatch_journal(self.http, self.server, "after processing")

        # Collect new output files
        after        = self._snapshot_outputs()
        new_files    = self._diff_snapshots(before, after)
        result       = DropResult(
            task          = task,
            filename      = filename,
            output_files  = sorted(new_files["output"]),
            no_code_files = sorted(new_files["no_code"]),
            error_files   = sorted(new_files["error"]),
        )

        self.log.info(
            f"Result: status={result.status}, docs={result.docs_count}, "
            f"output_files={len(result.output_files)}, "
            f"no_code={len(result.no_code_files)}, "
            f"error={len(result.error_files)}"
        )
        if result.output_files:
            self.log.debug("Output files:")
            for p in result.output_files:
                self.log.debug(f"  {p.relative_to(self.data)}  ({self.page_count(p)} pages)")

        return result

    def drop_raw(
        self,
        content: bytes,
        filename: str,
        timeout: float | None = None,
    ) -> DropResult:
        """
        Write arbitrary bytes with an explicit filename.
        Used for adversarial tests (non-PDF, corrupted, etc.).
        """
        timeout = timeout or self.default_timeout
        dest    = self.input_dir / filename

        self.log.info(f"Dropping raw file: {filename} ({len(content):,} bytes)")
        self.log.capture_pdfdispatch_journal(self.http, self.server, "before drop")

        before = self._snapshot_outputs()
        dest.write_bytes(content)

        task   = self._wait_for_task(filename, timeout)
        self.log.capture_task(task)
        self.log.capture_pdfdispatch_journal(self.http, self.server, "after processing")

        after    = self._snapshot_outputs()
        new      = self._diff_snapshots(before, after)
        return DropResult(
            task          = task,
            filename      = filename,
            output_files  = sorted(new["output"]),
            no_code_files = sorted(new["no_code"]),
            error_files   = sorted(new["error"]),
        )

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def cleanup_output(self, result: DropResult) -> None:
        """Remove output files produced by a specific test (for inter-test isolation)."""
        for p in result.output_files + result.no_code_files + result.error_files:
            try:
                p.unlink(missing_ok=True)
                self.log.debug(f"Cleaned up: {p.name}")
            except OSError as e:
                self.log.warning(f"Could not delete {p}: {e}")

    def cleanup_all_outputs(self) -> None:
        """Remove ALL files from output directories. Use at test session start."""
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
        Poll /api/tasks until a task for `filename` reaches a terminal state.
        Raises TimeoutError if not found within `timeout` seconds.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = self.http.get(f"{self.server}/api/tasks?n=50")
            for task in r.json().get("tasks", []):
                if task.get("filename") == filename:
                    if task["status"] in ("success", "error"):
                        return task
            time.sleep(0.5)
        raise TimeoutError(
            f"Task for '{filename}' not found or not completed within {timeout:.0f}s.\n"
            "Check that pdf-dispatch is running and the data_path is correct."
        )

    def _snapshot_outputs(self) -> dict[str, set[Path]]:
        """Return sets of existing PDF paths in each output directory."""
        def ls(d: Path) -> set[Path]:
            return set(d.glob("**/*.pdf")) if d.exists() else set()

        return {
            "output":  ls(self.output_dir) - ls(self.no_code_dir) - ls(self.error_dir),
            "no_code": ls(self.no_code_dir),
            "error":   ls(self.error_dir),
        }

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
