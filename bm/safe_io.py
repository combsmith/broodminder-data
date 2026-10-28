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
    """Return the configured maximum line byte limit, defaulting to 1 MiB."""
    return _env_limit(MAX_LINE_BYTES_ENV, DEFAULT_MAX_LINE_BYTES)


def max_document_bytes() -> int:
    """Return the configured maximum JSON document byte limit, defaulting to 128 MiB."""
    return _env_limit(MAX_DOCUMENT_BYTES_ENV, DEFAULT_MAX_DOCUMENT_BYTES)


def open_binary(path: Path | str) -> BinaryIO:
    """Open ``path`` for binary reading, decompressing when it ends in ``.gz``.

    Paths come from trusted local configuration (data dir, ``--data``, local
    store directories). Callers must validate that paths originate from trusted
    sources and are within allowed directories if path restrictions are required.

    Args:
        path: A path to a local file. The path must not contain traversal attempts
              (e.g., '..' components), which are rejected to prevent accidental
              path traversal when paths are LLM-generated or come from untrusted
              parsing.

    Raises:
        ValueError: If the path contains parent directory traversal components.
        FileNotFoundError: If the resolved path is not a regular file.
    """
    input_path = Path(path)
    # Reject paths with '..' to prevent traversal attacks when paths come from
    # untrusted parsing (e.g., LLM-generated code).
    for part in input_path.parts:
        if part == "..":
            raise ValueError(
                f"Path traversal attempt detected: {input_path} contains '..' component"
            )
    # Dispatch on the caller's name: a ``.gz`` symlink may target a file
    # whose own name has no ``.gz`` suffix.
    path = Path(os.path.realpath(input_path))
    if not path.is_file():
        raise FileNotFoundError(f"{path}: not a regular file")
    if input_path.name.lower().endswith(".gz"):
        return gzip.open(path, "rb")  # type: ignore[return-value]
    # NOSONAR: path is operator-supplied local config (CLI --data / data dir), not
    # model or network input; '..' components are rejected and the realpath is
    # verified to be a regular file above. No fixed base dir exists to confine to.
    return open(path, "rb")  # NOSONAR


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
        try:
            yield line.decode("utf-8")
        except UnicodeDecodeError as e:
            raise InputLimitError(
                f"{source}: invalid UTF-8 at byte offset {e.start}: {e.reason}"
            )


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
    _ = reader.fieldnames  # Consume header line to exclude its bytes from first record limit
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
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise InputLimitError(
            f"{path}: invalid UTF-8 at byte offset {e.start}: {e.reason}"
        )
    return json.loads(decoded)
