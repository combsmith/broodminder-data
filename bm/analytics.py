"""Pure telemetry aggregation over flattened reading rows.

Shared by the MCP server (and any future CLI) so status/trend logic lives in
exactly one place. Operates on rows as produced by ``scripts/flatten.py``:
identity fields plus ``m_<metric>`` keys. No I/O, no network.
"""

from __future__ import annotations

from typing import Any, Iterable

from .battery import extract_battery

BUCKET_SECONDS = {"1h": 3600, "6h": 6 * 3600, "1d": 24 * 3600}
MAX_BUCKETS = 500


def _valid_ts(row: dict[str, Any]) -> int | None:
    try:
        ts = row.get("timestamp")
        return int(ts) if ts is not None else None
    except (TypeError, ValueError):
        return None


def _metrics(row: dict[str, Any]) -> dict[str, Any]:
    return {k[2:]: v for k, v in row.items() if k.startswith("m_") and len(k) > 2}


def latest_status(rows: Iterable[dict[str, Any]], hive_id: str | int) -> dict[str, Any]:
    """Latest reading per device in a hive, streamed (never materializes rows).

    Metrics keep their real names; no brood/ambient labels are invented, as
    positionID is a slot type with no reliable semantic mapping.
    """
    wanted = str(hive_id)
    latest: dict[str, dict[str, Any]] = {}
    latest_ts: dict[str, int] = {}
    for row in rows:
        if str(row.get("hiveId") or "") != wanted:
            continue
        device_id = str(row.get("deviceId") or "").strip()
        ts = _valid_ts(row)
        if not device_id or ts is None:
            continue
        if device_id not in latest or ts >= latest_ts[device_id]:
            latest[device_id] = row
            latest_ts[device_id] = ts

    devices = []
    for device_id in sorted(latest):
        row = latest[device_id]
        ts = latest_ts[device_id]
        devices.append(
            {
                "device_id": device_id,
                "position_id": row.get("positionID"),
                "timestamp": ts,
                "datetime": row.get("datetime"),
                "battery_percent": extract_battery(row),
                "metrics": _metrics(row),
            }
        )
    return {"hive_id": wanted, "devices": devices}


def _bucket_size(bucket: str | int) -> int:
    if isinstance(bucket, int) and bucket > 0:
        return bucket
    if isinstance(bucket, str) and bucket in BUCKET_SECONDS:
        return BUCKET_SECONDS[bucket]
    raise ValueError(f"unknown bucket {bucket!r}; use one of {sorted(BUCKET_SECONDS)}")


def trends(
    rows: Iterable[dict[str, Any]],
    hive_id: str | int,
    metric: str,
    start: int,
    end: int,
    bucket: str | int = "1d",
) -> dict[str, Any]:
    """Bucketed min/max/mean/count for one metric in [start, end)."""
    size = _bucket_size(bucket)
    name = metric[2:] if metric.startswith("m_") else metric
    key = f"m_{name}"
    wanted = str(hive_id)

    agg: dict[int, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("hiveId") or "") != wanted:
            continue
        ts = _valid_ts(row)
        if ts is None or not (start <= ts < end):
            continue
        value = row.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            val = float(value)
        except (TypeError, ValueError):
            continue
        b = (ts // size) * size
        slot = agg.setdefault(b, {"min": val, "max": val, "sum": 0.0, "count": 0})
        slot["min"] = min(slot["min"], val)
        slot["max"] = max(slot["max"], val)
        slot["sum"] += val
        slot["count"] += 1

    ordered = sorted(agg)
    truncated = len(ordered) > MAX_BUCKETS
    ordered = ordered[:MAX_BUCKETS]
    buckets = [
        {
            "start": b,
            "end": b + size,
            "min": agg[b]["min"],
            "max": agg[b]["max"],
            "mean": agg[b]["sum"] / agg[b]["count"],
            "count": agg[b]["count"],
        }
        for b in ordered
    ]
    delta = None
    if buckets:
        delta = buckets[-1]["mean"] - buckets[0]["mean"]
    return {
        "hive_id": wanted,
        "metric": name,
        "bucket_seconds": size,
        "buckets": buckets,
        "delta": delta,
        "truncated": truncated,
    }


def derive_notes_window(days: int, now_ts: int) -> tuple[int, int]:
    """Return (start, end) epoch window covering the last `days` days."""
    if days < 1:
        raise ValueError("days must be >= 1")
    return now_ts - days * 86400, now_ts
