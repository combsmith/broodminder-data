"""FastMCP server exposing BroodMinder hive data to AI agents.

Offline-first: tools read the local extract store (``bm.store.LocalStore``);
with ``source="auto"`` a live fetch fills only the missing tail and is
persisted, so the same data is never fetched twice (see ``bm.store``).
Use ``--sandbox`` (or ``BROODMINDER_SANDBOX=1``) for the deterministic
synthetic dataset with no API key or network.

Run: ``broodminder-mcp`` or ``python -m bm.mcp_server [--sandbox]``.
Stdout is reserved for JSON-RPC; all diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from . import analytics
from .battery import scan_device_health_from_stream
from .sandbox import SandboxStore
from .store import LocalStore


def _now(now_ts: int | None) -> int:
    return int(now_ts if now_ts is not None else time.time())


def _is_sandbox(store: Any) -> bool:
    return isinstance(store, SandboxStore)


def _prepare(store: Any, hive_ids: list[str], start: int, end: int, source: str,
             now_ts: int | None = None) -> dict[str, Any]:
    """Ensure local coverage; return a provenance dict."""
    if _is_sandbox(store):
        return {"source": "sandbox", "stale": False, "fetched_windows": 0}
    report = store.ensure_coverage(hive_ids, start, end, source=source)
    return {
        "source": "synced" if report.get("fetched_windows") else "offline",
        "stale": bool(report.get("stale")),
        "fetched_windows": int(report.get("fetched_windows", 0)),
    }


def _topology(store: Any) -> list[dict[str, Any]]:
    topo_fn = getattr(store, "topology", None)
    if callable(topo_fn):
        return topo_fn()
    # Fallback: derive from coverage only (no names/devices known).
    return [
        {"apiaryId": None, "apiaryName": None,
         "hives": [{"hiveId": h, "hiveName": None, "devices": []}]}
        for h in store.coverage()
    ]


def _known_hives(store: Any) -> set[str]:
    hives: set[str] = set()
    for apiary in _topology(store):
        for hive in apiary.get("hives", []) or []:
            if hive.get("hiveId") is not None:
                hives.add(str(hive["hiveId"]))
    return hives


def apiary_summary(store: Any, source: str = "auto", now_ts: int | None = None) -> dict[str, Any]:
    now = _now(now_ts)
    apiaries_out = []
    all_hives: list[str] = []
    for apiary in _topology(store):
        hives_out = []
        devices: set[str] = set()
        for hive in apiary.get("hives", []) or []:
            hid = str(hive.get("hiveId"))
            all_hives.append(hid)
            devs = [str(d) for d in (hive.get("devices") or [])]
            devices.update(devs)
            hives_out.append({"hive_id": hid, "hive_name": hive.get("hiveName"),
                              "device_count": len(devs)})
        apiaries_out.append({
            "apiary_id": apiary.get("apiaryId"),
            "apiary_name": apiary.get("apiaryName"),
            "hive_count": len(hives_out),
            "device_count": len(devices),
            "hives": hives_out,
        })
    provenance = (
        {"source": "sandbox", "stale": False, "fetched_windows": 0}
        if _is_sandbox(store)
        else {"source": source, "stale": False, "fetched_windows": 0}
    )
    return {"apiaries": apiaries_out, "provenance": provenance}


def hive_status(store: Any, hive_id: str, source: str = "auto",
                now_ts: int | None = None, days: int = 30) -> dict[str, Any]:
    now = _now(now_ts)
    provenance = _prepare(store, [str(hive_id)], now - days * 86400, now, source, now_ts)
    result = analytics.latest_status(store.iter_readings(hive_id), hive_id)
    result["provenance"] = provenance
    return result


def telemetry_trends(store: Any, hive_id: str, metric: str = "temperature", days: int = 7,
                     bucket: str = "1d", source: str = "auto",
                     now_ts: int | None = None) -> dict[str, Any]:
    now = _now(now_ts)
    start = now - days * 86400
    provenance = _prepare(store, [str(hive_id)], start, now, source, now_ts)
    # +1s: the exclusive window end must include a reading timestamped `now`.
    result = analytics.trends(store.iter_readings(hive_id, start, now + 1), hive_id,
                              metric, start, now + 1, bucket=bucket)
    result["provenance"] = provenance
    return result


def hive_notes(store: Any, hive_id: str, days: int = 30, limit: int = 20,
               source: str = "auto", now_ts: int | None = None) -> dict[str, Any]:
    now = _now(now_ts)
    start, end = analytics.derive_notes_window(days, now)
    provenance = _prepare(store, [str(hive_id)], start, end, source, now_ts)
    notes = [n for n in store.iter_notes(hive_id)
             if (n.get("timestamp") or n.get("created") or 0) >= start]
    notes.sort(key=lambda n: (n.get("timestamp") or n.get("created") or 0), reverse=True)
    return {"hive_id": str(hive_id), "notes": notes[: max(1, limit)],
            "provenance": provenance}


def _hives_for_apiary(store: Any, apiary: str | None) -> list[str]:
    """Hive IDs in the named apiary (matched by ID or name), or all hives."""
    all_hives: list[str] = []
    for entry in _topology(store):
        in_apiary = (
            not apiary
            or str(entry.get("apiaryId") or "") == str(apiary)
            or str(entry.get("apiaryName") or "") == str(apiary)
        )
        for hive in entry.get("hives", []) or []:
            if hive.get("hiveId") is not None and (in_apiary or not apiary):
                all_hives.append(str(hive["hiveId"]))
    return all_hives


def device_health(store: Any, apiary: str | None = None, threshold: int = 80,
                  stale_days: int = 7, source: str = "auto",
                  now_ts: int | None = None) -> dict[str, Any]:
    now = _now(now_ts)
    if _is_sandbox(store):
        provenance = {"source": "sandbox", "stale": False, "fetched_windows": 0}
    else:
        # Refresh coverage for known hives first so classification is not
        # silently based on stale local data.
        hives = _hives_for_apiary(store, apiary)
        cov = store.coverage() if hasattr(store, "coverage") else {}
        start = min(
            [cov.get(h, {}).get("covered_start", now - 30 * 86400) for h in hives]
            or [now - 30 * 86400]
        )
        provenance = _prepare(store, hives, start, now, source, now_ts) if hives else {
            "source": source, "stale": False, "fetched_windows": 0}
    devices = scan_device_health_from_stream(
        store.iter_readings(), now_ts=now, threshold=threshold,
        stale_days=stale_days, apiary=apiary,
    )
    return {"devices": [d.to_dict() for d in devices], "provenance": provenance}


def _resolve_store(data_dir: str | None, sandbox: bool, now_ts: int | None) -> Any:
    if sandbox or os.environ.get("BROODMINDER_SANDBOX") == "1":
        return SandboxStore(now_ts=_now(now_ts))
    return LocalStore(data_dir)


def build_server(store: Any = None, now_ts: int | None = None):
    """Build the FastMCP server bound to ``store`` (defaults to env-resolved)."""
    from mcp.server.fastmcp import FastMCP

    if store is None:
        store = _resolve_store(None, False, now_ts)
    mcp = FastMCP("broodminder")

    @mcp.tool()
    def get_apiary_summary(source: str = "auto") -> dict[str, Any]:
        """High-level inventory of apiaries, hives, and device counts."""
        return apiary_summary(store, source=source, now_ts=now_ts)

    @mcp.tool()
    def get_hive_status(hive_id: str, source: str = "auto") -> dict[str, Any]:
        """Latest sensor readings per device for one hive."""
        return hive_status(store, hive_id, source=source, now_ts=now_ts)

    @mcp.tool()
    def get_telemetry_trends(hive_id: str, metric: str = "temperature",
                             days: int = 7, bucket: str = "1d",
                             source: str = "auto") -> dict[str, Any]:
        """Bucketed min/max/mean/delta trend for one metric over a lookback."""
        return telemetry_trends(store, hive_id, metric=metric, days=days,
                                bucket=bucket, source=source, now_ts=now_ts)

    @mcp.tool()
    def get_hive_notes(hive_id: str, days: int = 30, limit: int = 20,
                       source: str = "auto") -> dict[str, Any]:
        """Recent beekeeper inspection notes for one hive."""
        return hive_notes(store, hive_id, days=days, limit=limit,
                          source=source, now_ts=now_ts)

    @mcp.tool()
    def get_device_health(apiary: str | None = None, threshold: int = 80,
                          stale_days: int = 7, source: str = "auto") -> dict[str, Any]:
        """Battery levels and reporting freshness across devices."""
        return device_health(store, apiary=apiary, threshold=threshold,
                             stale_days=stale_days, source=source, now_ts=now_ts)

    # -- resources (phase 2; delegate to the same logic as tools) -----------
    @mcp.resource("broodminder://apiaries/list", mime_type="application/json")
    def apiaries_resource() -> str:
        return json.dumps(apiary_summary(store, now_ts=now_ts))

    @mcp.resource("broodminder://hives/{hive_id}/status", mime_type="application/json")
    def hive_status_resource(hive_id: str) -> str:
        if hive_id not in _known_hives(store):
            raise ValueError(f"unknown hive_id: {hive_id}")
        return json.dumps(hive_status(store, hive_id, source="offline", now_ts=now_ts))

    @mcp.resource("broodminder://devices/health", mime_type="application/json")
    def devices_health_resource() -> str:
        return json.dumps(device_health(store, now_ts=now_ts))

    # -- prompts (phase 3; two only) ----------------------------------------
    @mcp.prompt()
    def inspection_prep(apiary_id: str = "", lookback_days: int = 7) -> str:
        """Pre-inspection briefing: weight loss, temperature anomalies, notes."""
        health = device_health(store, apiary=apiary_id or None, now_ts=now_ts)
        lines = ["# Inspection prep", ""]
        attention = [d for d in health["devices"] if d.get("needs_attention")]
        if attention:
            lines.append("## Devices needing attention")
            for d in attention:
                lines.append(f"- {d['device_id']} ({d.get('hive_name') or d.get('hive_id')}): "
                             f"{'; '.join(d.get('reasons') or []) or d['status']}")
            lines.append("")
        lines.append("## Hive status")
        for hid in sorted(_hives_for_apiary(store, apiary_id or None)):
            status = hive_status(store, hid, source="offline", now_ts=now_ts)
            for dev in status["devices"]:
                metrics = dev.get("metrics") or {}
                parts = [f"{k}={v}" for k, v in sorted(metrics.items())]
                lines.append(f"- {hid} / {dev['device_id']}: " + (", ".join(parts) or "no metrics"))
            trend = telemetry_trends(store, hid, metric="weight", days=lookback_days,
                                     bucket="1d", source="offline", now_ts=now_ts)
            if trend["buckets"]:
                lines.append(f"  weight delta ({lookback_days}d): {trend['delta']}")
            notes = hive_notes(store, hid, days=max(lookback_days, 30), limit=3,
                               source="offline", now_ts=now_ts)
            for n in notes["notes"]:
                if n.get("description"):
                    lines.append(f"  note: {n['description']}")
        return "\n".join(lines)

    @mcp.prompt()
    def device_health_audit() -> str:
        """Battery levels, sync freshness, and replacement recommendations."""
        health = device_health(store, now_ts=now_ts)
        lines = ["# Device health audit", ""]
        for d in health["devices"]:
            batt = d.get("battery_percent")
            batt_txt = f"{batt}%" if batt is not None else "unknown"
            action = "replace/check battery" if d.get("needs_attention") else "ok"
            lines.append(f"- {d['device_id']}: status={d['status']} battery={batt_txt} "
                         f"days_offline={d.get('days_offline')} -> {action}")
        return "\n".join(lines)

    return mcp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BroodMinder MCP server (stdio).")
    parser.add_argument("--sandbox", action="store_true",
                        help="serve the deterministic synthetic dataset (no API key)")
    parser.add_argument("--data-dir", default=None,
                        help="local extract store directory (default: data/extract)")
    args = parser.parse_args(argv)
    store = _resolve_store(args.data_dir, args.sandbox, None)
    server = build_server(store=store)
    print("broodminder MCP server starting (stdio)", file=sys.stderr)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
