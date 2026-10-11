"""Thin, reusable client for the BroodMinder External User API.

Deliberately dependency-light and transport-clean so it can later be lifted
into an MCP server, a broodly ingestion job, or a Go port. The only stateful
concession to the alpha API is a call counter + 429 backoff, because the key is
capped at 1000 calls/day.

API surface (per https://external-api.mybroodminder.com/user/docs):
    GET /user/metadata/apiaries
    GET /user/hive/{id}/readings?start=&end=
    GET /user/hive/{id}/notes?start=&end=
    GET /user/device/{id}/readings?start=&end=

Auth: header `X-Api-Key: <key>`. Time params are unix epoch seconds; the
readings/notes endpoints are limited to a 6-month span per request.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # python-dotenv optional at runtime
    pass

# The docs say "limited to 6 months per request". Use a conservative window
# (slightly under 6 * 30 days) so we never trip a strict boundary check.
MAX_WINDOW_SECONDS = 180 * 24 * 60 * 60  # ~180 days


class BroodMinderError(RuntimeError):
    """Any non-2xx response that isn't a rate limit."""

    def __init__(self, status: int, method: str, url: str, body: str):
        self.status = status
        self.method = method
        self.url = url
        self.body = body
        super().__init__(f"{method} {url} -> {status}: {body[:300]}")


class RateLimited(BroodMinderError):
    """429 Too Many Requests."""


@dataclass
class BroodMinderClient:
    api_key: str | None = None
    base_url: str | None = None
    timeout: float = 30.0
    max_retries: int = 3
    # Observability: track usage against the 1000/day budget.
    call_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.api_key = self.api_key or os.environ.get("BROODMINDER_API_KEY")
        self.base_url = (
            self.base_url
            or os.environ.get("BROODMINDER_BASE_URL")
            or "https://external-api.mybroodminder.com"
        ).rstrip("/")
        if not self.api_key:
            raise BroodMinderError(0, "INIT", self.base_url, "BROODMINDER_API_KEY not set")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
        )

    # -- context manager -------------------------------------------------
    def __enter__(self) -> "BroodMinderClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- low-level -------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET with 429-aware retry. Returns the raw response (callers decide
        whether to .json()), so contract tests can assert on status/headers."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.call_count += 1
            resp = self._client.get(path, params=params)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                last_exc = RateLimited(429, "GET", str(resp.url), resp.text)
                if attempt < self.max_retries:
                    time.sleep(min(retry_after, 30))
                    continue
                raise last_exc
            return resp
        raise last_exc  # pragma: no cover

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET and return parsed JSON, raising BroodMinderError on non-2xx."""
        resp = self._get(path, params)
        if not resp.is_success:
            raise BroodMinderError(resp.status_code, "GET", str(resp.url), resp.text)
        return resp.json()

    # -- endpoints -------------------------------------------------------
    def apiaries(self) -> Any:
        """GET /user/metadata/apiaries — all active apiaries + nested hives."""
        return self.get_json("/user/metadata/apiaries")

    def hive_readings(self, hive_id: str | int, start: int, end: int) -> Any:
        """GET /user/hive/{id}/readings — sensor readings grouped by position."""
        return self.get_json(
            f"/user/hive/{hive_id}/readings", {"start": int(start), "end": int(end)}
        )

    def hive_notes(self, hive_id: str | int, start: int, end: int) -> list[dict[str, Any]]:
        """GET /user/hive/{id}/notes — hive notes in window.

        Normalizes the response to always return a list of note dictionaries,
        handling both the empty bare array ``[]`` and the populated object
        wrapper ``{"notes": [...]}`` returned by the API.
        """
        payload = self.get_json(
            f"/user/hive/{hive_id}/notes", {"start": int(start), "end": int(end)}
        )
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return payload.get("notes", []) or []
        return []

    def device_readings(self, device_id: str | int, start: int, end: int) -> Any:
        """GET /user/device/{id}/readings — single-device sensor readings."""
        return self.get_json(
            f"/user/device/{device_id}/readings", {"start": int(start), "end": int(end)}
        )

    # -- raw variants (for contract tests that need status/headers) ------
    def raw(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return self._get(path, params)


# -- time helpers --------------------------------------------------------
def now_epoch() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def to_epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def iter_windows(start: int, end: int, window: int = MAX_WINDOW_SECONDS) -> Iterator[tuple[int, int]]:
    """Yield [s, e) chunks no larger than `window`, covering [start, end].

    Used to walk an arbitrarily long history past the API's 6-month per-request
    cap. Windows are half-open back-to-back so readings are neither dropped nor
    double-counted across the seam.
    """
    if end <= start:
        return
    cur = start
    while cur < end:
        nxt = min(cur + window, end)
        yield cur, nxt
        cur = nxt
