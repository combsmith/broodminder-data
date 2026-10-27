"""Deterministic synthetic dataset for MCP demos and tests (issue #221).

Three scenario hives — healthy, weight-drop (swarm-shaped), and
low-battery/stale — in flattened-row shape. Anchored to an injected
``now_ts`` so battery/staleness classification is stable at any wall-clock
time. Synthetic IDs only; no real hive data. No network, no API key.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

DAY = 86400
APIARY = {"apiaryId": "apiary-sandbox", "apiaryName": "Sandbox Apiary"}
HIVES = {
    "hive-healthy": ("Healthy Hive", "dev-healthy"),
    "hive-swarm": ("Swarm Hive", "dev-swarm"),
    "hive-offline": ("Offline Hive", "dev-offline"),
}


def _row(hive: str, device: str, ts: int, battery: int, **metrics: Any) -> dict[str, Any]:
    hive_name, _ = HIVES[hive]
    return {
        **APIARY,
        "hiveId": hive,
        "hiveName": hive_name,
        "positionID": "pos-1",
        "deviceId": device,
        "timestamp": ts,
        "datetime": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "batteryLevel": battery,
        "chargeRemaining": None,
        **{f"m_{k}": v for k, v in metrics.items()},
    }


def sandbox_readings(now_ts: int) -> list[dict[str, Any]]:
    """Flattened synthetic rows anchored to ``now_ts`` (deterministic)."""
    rows: list[dict[str, Any]] = []
    # Healthy: hourly for 24h, stable brood temp, gently rising weight.
    for h in range(24):
        ts = now_ts - (23 - h) * 3600
        rows.append(_row("hive-healthy", "dev-healthy", ts, 92,
                         temperature=95.0, humidity=55.0, weight=60.0 + h * 0.05))
    # Swarm-shaped: daily for 7d, then a sharp weight drop.
    weights = [80.0, 80.5, 81.0, 81.4, 81.8, 82.0, 82.2, 76.5]
    for d, w in enumerate(weights):
        ts = now_ts - (len(weights) - 1 - d) * DAY
        rows.append(_row("hive-swarm", "dev-swarm", ts, 85,
                         temperature=95.5, weight=w))
    # Offline: last seen 10 days ago, critical battery.
    for d in range(3):
        ts = now_ts - (12 - d) * DAY
        rows.append(_row("hive-offline", "dev-offline", ts, 18,
                         temperature=70.0 - d, weight=55.0))
    rows.sort(key=lambda r: (r["hiveId"], r["timestamp"]))
    return rows


def sandbox_notes(now_ts: int) -> list[dict[str, Any]]:
    """Synthetic inspection notes anchored to ``now_ts``."""
    raw = [
        ("hive-healthy", "note-sandbox-1", now_ts - DAY, "Inspected; brood pattern solid."),
        ("hive-swarm", "note-sandbox-2", now_ts - 2 * DAY, "Queen cells spotted on frame 6."),
        ("hive-offline", "note-sandbox-3", now_ts - 11 * DAY, "Sensor battery low at last visit."),
    ]
    return [
        {
            "hiveId": hive,
            "hiveName": HIVES[hive][0],
            "id": nid,
            "timestamp": ts,
            "created": ts,
            "description": desc,
        }
        for hive, nid, ts, desc in raw
    ]


class SandboxStore:
    """Store-interface swap over the synthetic dataset (no I/O)."""

    def __init__(self, now_ts: int):
        self.now_ts = int(now_ts)
        self._rows = sandbox_readings(self.now_ts)
        self._notes = sandbox_notes(self.now_ts)

    def topology(self) -> list[dict[str, Any]]:
        hives = [
            {"hiveId": hid, "hiveName": name, "devices": [dev]}
            for hid, (name, dev) in HIVES.items()
        ]
        return [{**APIARY, "hives": hives}]

    def coverage(self) -> dict[str, dict[str, Any]]:
        cov: dict[str, dict[str, Any]] = {}
        for r in self._rows:
            slot = cov.setdefault(
                r["hiveId"],
                {"covered_start": r["timestamp"], "covered_end": r["timestamp"],
                 "windows": 1, "rows": 0},
            )
            slot["covered_start"] = min(slot["covered_start"], r["timestamp"])
            slot["covered_end"] = max(slot["covered_end"], r["timestamp"])
            slot["rows"] += 1
        return cov

    def iter_readings(
        self, hive_id: str | int | None = None, start: int | None = None, end: int | None = None
    ) -> Iterator[dict[str, Any]]:
        for r in self._rows:
            if hive_id is not None and r["hiveId"] != str(hive_id):
                continue
            if start is not None and r["timestamp"] < start:
                continue
            if end is not None and r["timestamp"] >= end:
                continue
            yield dict(r)

    def iter_notes(self, hive_id: str | int | None = None) -> Iterator[dict[str, Any]]:
        for n in self._notes:
            if hive_id is not None and n["hiveId"] != str(hive_id):
                continue
            yield dict(n)

    def ensure_coverage(
        self, hive_ids: Iterable[str | int], start: int, end: int,
        source: str = "auto", client: Any = None,
    ) -> dict[str, Any]:
        cov = self.coverage()
        return {
            "stale": False,
            "fetched_windows": 0,
            "covered_end": {str(h): cov.get(str(h), {}).get("covered_end", end) for h in hive_ids},
            "source": "sandbox",
        }
