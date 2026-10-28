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

    Rejects paths containing parent directory references to prevent traversal attacks.
    """
    path = Path(path)
    if ".." in path.parts:
        raise ValueError(f"Path traversal not allowed: {path}")
    if path.name.lower().endswith(".gz"):
        return gzip.open(path, "rb")  # type: ignore[return-value]
    return open(path, "rb")


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
