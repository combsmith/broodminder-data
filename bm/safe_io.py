"""Bounded readers for local extract files (NDJSON, CSV, JSON; plain or gzip).

A small gzip file can expand to gigabytes. Reading it line by line, or
decompressing a whole JSON document, must therefore cap how much is
materialized in memory *before* the line or document is complete.
Exceeding a cap raises :class:`InputLimitError` naming the file, the
limit, and the environment variable that raises it.

Limits (read at call time, so they can be changed per process):

- ``BROODMINDER_MAX_LINE_BYTES``: longest NDJSON/CSV line, newline
  included (default 1 MiB; a reading row is well under 2 KiB).
- ``BROODMINDER_MAX_DOCUMENT_BYTES``: largest decompressed JSON document,
  e.g. one raw 180-day window (default 128 MiB).
"""

from __future__ import annotations

import csv as csv_module
import gzip
import json
import os
from pathlib import Path
from typing import Any, BinaryIO, Iterator

MAX_LINE_BYTES_ENV = "BROODMINDER_MAX_LINE_BYTES"
MAX_DOCUMENT_BYTES_ENV = "BROODMINDER_MAX_DOCUMENT_BYTES"
DEFAULT_MAX_LINE_BYTES = 1 << 20  # 1 MiB
DEFAULT_MAX_DOCUMENT_BYTES = 128 << 20  # 128 MiB


class InputLimitError(ValueError):
    """A local input file exceeded a configured size limit."""


def _env_limit(name: str, default: int) -> int:
    """Read an environment variable as a positive integer byte limit, or return the default."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer number of bytes, got {raw!r}")
    return value


def max_line_bytes() -> int:
    return _env_limit(MAX_LINE_BYTES_ENV, DEFAULT_MAX_LINE_BYTES)


def max_document_bytes() -> int:
    return _env_limit(MAX_DOCUMENT_BYTES_ENV, DEFAULT_MAX_DOCUMENT_BYTES)


def open_binary(path: Path | str) -> BinaryIO:
    """Open ``path`` for binary reading, decompressing when it ends in ``.gz``.

    Validates the path with canonical resolution to prevent traversal attacks,
    ensuring the resolved path is within the current working directory.
    """
    path = Path(path)
    # Canonicalize the path to resolve symlinks and relative components.
    resolved = os.path.realpath(path)
    base_dir = os.path.realpath(os.getcwd())
    # Ensure the resolved path is within the current working directory.
    # The base_dir must end with a separator to prevent partial path traversal.
    if resolved != base_dir and not resolved.startswith(base_dir + os.sep):
        raise ValueError(f"Path traversal not allowed: {path}")

    if path.name.lower().endswith(".gz"):
        return gzip.open(resolved, "rb")  # type: ignore[return-value]
    return open(resolved, "rb")


def iter_bounded_lines(fh: BinaryIO, source: Path | str, limit: int | None = None) -> Iterator[str]:
    """Yield UTF-8 decoded lines (newline kept) from binary ``fh``.

    Each read is capped at ``limit + 1`` bytes, so an over-long line is
    detected without ever buffering more than the limit.
    """
    if limit is None:
        limit = max_line_bytes()
    while True:
        line = fh.readline(limit + 1)
        if not line:
            return
        if len(line) > limit:
            raise InputLimitError(
                f"{source}: line exceeds {limit} bytes "
                f"(set {MAX_LINE_BYTES_ENV} to raise the limit)"
            )
        yield line.decode("utf-8")


class _TrackingLineIterator:
    """Wrapper around a line iterator that tracks cumulative bytes for CSV records."""

    def __init__(self, lines: Iterator[str], source: Path | str, limit: int):
        self.lines = lines
        self.source = source
        self.limit = limit
        self.cumulative_bytes = 0

    def __iter__(self):
        return self

    def __next__(self) -> str:
        line = next(self.lines)
        self.cumulative_bytes += len(line.encode("utf-8"))
        if self.cumulative_bytes > self.limit:
            raise InputLimitError(
                f"{self.source}: record exceeds {self.limit} bytes "
                f"(set {MAX_LINE_BYTES_ENV} to raise the limit)"
            )
        # Return line as-is, but reset cumulative counter on record boundaries.
        # csv.DictReader will signal a new record implicitly; we detect it by
        # checking if we've just seen a complete line from a multiline record.
        return line

    def reset_record(self):
        """Reset cumulative byte counter for next record."""
        self.cumulative_bytes = 0


def iter_csv_records_bounded(
    lines: Iterator[str], source: Path | str, limit: int | None = None
) -> Iterator[dict[str, Any]]:
    """Yield CSV records from bounded lines, enforcing cumulative bytes per record.

    This wraps the line iterator with a tracker that accumulates bytes across
    all lines belonging to a single CSV record (e.g. multiline quoted fields),
    and raises InputLimitError if a record exceeds the limit.
    """
    if limit is None:
        limit = max_line_bytes()
    tracker = _TrackingLineIterator(lines, source, limit)
    reader = csv_module.DictReader(tracker)
    tracker.reset_record()  # Clear header bytes before reading data records
    for record in reader:
        yield record
        tracker.reset_record()


def load_json_document(path: Path | str, limit: int | None = None) -> Any:
    """Parse a JSON (or ``.json.gz``) document, capping decompressed size."""
    if limit is None:
        limit = max_document_bytes()
    with open_binary(path) as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise InputLimitError(
            f"{path}: decompressed document exceeds {limit} bytes "
            f"(set {MAX_DOCUMENT_BYTES_ENV} to raise the limit)"
        )
    return json.loads(data.decode("utf-8"))
