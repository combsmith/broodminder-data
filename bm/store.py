"""Offline-first local store over extracted BroodMinder data.

Reads the local extract tree (manifest + raw windows, or flattened NDJSON)
first. A live sync only fills the missing tail per hive — windows already
recorded complete in ``manifest.json`` are never re-fetched — then answers
are served from the local data. All diagnostics go to stderr; library code
never prints to stdout (an MCP stdio server owns stdout for JSON-RPC).
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .client import BroodMinderClient, BroodMinderError, iter_windows


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def resolve_data_dir(data_dir: Path | str | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    env = os.environ.get("BROODMINDER_DATA_DIR")
    if env:
        return Path(env)
    return Path("data") / "extract"


class LocalStore:
    """Local extract store rooted at ``data_dir``."""

    def __init__(self, data_dir: Path | str | None = None):
        self.data_dir = resolve_data_dir(data_dir)
        self.raw = self.data_dir / "raw"
        self.manifest_path = self.data_dir / "manifest.json"

    # -- manifest ---------------------------------------------------------
    def _load_manifest(self) -> dict[str, Any]:
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {"completed": {}, "meta": {}}
        return {"completed": {}, "meta": {}}

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp.replace(self.manifest_path)

    def coverage(self) -> dict[str, dict[str, Any]]:
        """Per-hive covered window derived from the manifest."""
        cov: dict[str, dict[str, Any]] = {}
        for key, rec in self._load_manifest().get("completed", {}).items():
            parts = key.rsplit("|", 2)
            if len(parts) != 3:
                continue
            hive, s, e = parts
            try:
                start, end = int(s), int(e)
            except ValueError:
                continue
            slot = cov.setdefault(
                hive, {"covered_start": start, "covered_end": end, "windows": 0, "rows": 0}
            )
            slot["covered_start"] = min(slot["covered_start"], start)
            slot["covered_end"] = max(slot["covered_end"], end)
            slot["windows"] += 1
            slot["rows"] += int(rec.get("reading_rows", 0) or 0)
        return cov

    def topology(self) -> list[dict[str, Any]]:
        """Apiary -> hive -> devices topology derived from local data."""
        meta = self._hive_meta()
        devices: dict[str, set[str]] = {}
        hive_names: dict[str, str | None] = {}
        for row in self.iter_readings():
            hid = str(row.get("hiveId") or "")
            if not hid:
                continue
            hive_names.setdefault(hid, row.get("hiveName"))
            if row.get("deviceId"):
                devices.setdefault(hid, set()).add(str(row["deviceId"]))
            if hid not in meta:
                meta[hid] = {
                    "apiaryId": row.get("apiaryId"),
                    "apiaryName": row.get("apiaryName"),
                    "hiveName": row.get("hiveName"),
                }
        apiaries: dict[str, dict[str, Any]] = {}
        for hid, m in meta.items():
            key = str(m.get("apiaryId") or m.get("apiaryName") or "")
            slot = apiaries.setdefault(
                key,
                {"apiaryId": m.get("apiaryId"), "apiaryName": m.get("apiaryName"), "hives": []},
            )
            slot["hives"].append(
                {
                    "hiveId": hid,
                    "hiveName": m.get("hiveName") or hive_names.get(hid),
                    "devices": sorted(devices.get(hid, set())),
                }
            )
        return list(apiaries.values())

    def _hive_meta(self) -> dict[str, dict[str, Any]]:
        meta: dict[str, dict[str, Any]] = {}
        for key, rec in self._load_manifest().get("completed", {}).items():
            hive = key.split("|", 1)[0]
            meta.setdefault(
                hive,
                {
                    "apiaryId": rec.get("apiaryId"),
                    "apiaryName": rec.get("apiaryName"),
                    "hiveName": rec.get("hiveName"),
                },
            )
        return meta

    # -- reading rows -------------------------------------------------------
    def _iter_raw_readings(self, hive_id: str | None) -> Iterator[dict[str, Any]]:
        if not self.raw.exists():
            return
        meta = self._hive_meta()
        hive_dirs = sorted(self.raw.iterdir()) if hive_id is None else [self.raw / str(hive_id)]
        for hdir in hive_dirs:
            if not hdir.is_dir():
                continue
            hid = hdir.name
            m = meta.get(hid, {})
            seen: set[tuple] = set()
            files = sorted(hdir.glob("*.readings.json.gz")) + sorted(hdir.glob("*.readings.json"))
            for path in files:
                try:
                    if path.suffix == ".gz":
                        with gzip.open(path, "rt", encoding="utf-8") as fh:
                            payload = json.load(fh)
                    else:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                for pos in payload or []:
                    pid = pos.get("positionID")
                    for r in pos.get("readings", []) or []:
                        dk = (pid, r.get("deviceId"), r.get("timestamp"))
                        if dk in seen:
                            continue
                        seen.add(dk)
                        ts = r.get("timestamp")
                        metrics = r.get("readings") or {}
                        yield {
                            "apiaryId": m.get("apiaryId"),
                            "apiaryName": m.get("apiaryName"),
                            "hiveId": hid,
                            "hiveName": m.get("hiveName"),
                            "positionID": pid,
                            "deviceId": r.get("deviceId"),
                            "timestamp": ts,
                            "datetime": (
                                datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                                if ts is not None else None
                            ),
                            "batteryLevel": r.get("batteryLevel"),
                            "chargeRemaining": r.get("chargeRemaining"),
                            **{f"m_{k}": v for k, v in metrics.items()},
                        }

    def _flat_readings_path(self) -> Path | None:
        for name in ("readings.ndjson.gz", "readings.ndjson"):
            p = self.data_dir / name
            if p.exists() and p.stat().st_size > 0:
                return p
        return None

    def _iter_flat_readings(self) -> Iterator[dict[str, Any]]:
        path = self._flat_readings_path()
        if path is None:
            return
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def iter_readings(
        self, hive_id: str | int | None = None, start: int | None = None, end: int | None = None
    ) -> Iterator[dict[str, Any]]:
        """Stream flattened rows, optionally filtered by hive and [start, end)."""
        if self.raw.exists() and any(self.raw.iterdir()):
            rows: Iterable[dict[str, Any]] = self._iter_raw_readings(
                str(hive_id) if hive_id is not None else None
            )
        else:
            rows = self._iter_flat_readings()
        for row in rows:
            if hive_id is not None and str(row.get("hiveId") or "") != str(hive_id):
                continue
            ts = row.get("timestamp")
            if start is not None and (ts is None or ts < start):
                continue
            if end is not None and (ts is None or ts >= end):
                continue
            yield row

    def iter_notes(self, hive_id: str | int | None = None) -> Iterator[dict[str, Any]]:
        """Stream notes from raw windows (deduplicated), else notes.ndjson."""
        seen: set[tuple] = set()
        found_raw = False
        if self.raw.exists():
            hive_dirs = (
                sorted(self.raw.iterdir()) if hive_id is None else [self.raw / str(hive_id)]
            )
            for hdir in hive_dirs:
                if not hdir.is_dir():
                    continue
                files = sorted(hdir.glob("*.notes.json.gz")) + sorted(hdir.glob("*.notes.json"))
                for path in files:
                    found_raw = True
                    try:
                        if path.suffix == ".gz":
                            with gzip.open(path, "rt", encoding="utf-8") as fh:
                                payload = json.load(fh)
                        else:
                            payload = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    items = payload if isinstance(payload, list) else (payload or {}).get("notes", [])
                    for note in items or []:
                        key = (hdir.name, note.get("id") or (note.get("created"), note.get("description")))
                        if key in seen:
                            continue
                        seen.add(key)
                        yield {"hiveId": hdir.name, **note}
        if found_raw:
            return
        notes_path = self.data_dir / "notes.ndjson"
        if notes_path.exists():
            for line in notes_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    note = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if hive_id is not None and str(note.get("hiveId") or "") != str(hive_id):
                    continue
                yield note

    # -- live tail sync -----------------------------------------------------
    def ensure_coverage(
        self,
        hive_ids: Iterable[str | int],
        start: int,
        end: int,
        source: str = "auto",
        client: Any = None,
    ) -> dict[str, Any]:
        """Ensure the local store covers [start, end) for each hive.

        ``offline`` never touches the network. ``auto`` fetches only the
        missing suffix per hive and persists each window before continuing.
        Failures degrade to ``stale: True`` rather than raising.
        """
        cov = self.coverage()
        covered_end = {str(h): cov.get(str(h), {}).get("covered_end", start) for h in hive_ids}
        # Recompute per requested hive (coverage may be absent).
        hive_list = [str(h) for h in hive_ids]
        covered_end = {
            h: max(start, cov.get(h, {}).get("covered_end", start)) for h in hive_list
        }
        if all(covered_end[h] >= end for h in hive_list):
            return {"stale": False, "fetched_windows": 0, "covered_end": covered_end, "source": source}
        if source == "offline":
            return {"stale": True, "fetched_windows": 0, "covered_end": covered_end, "source": source}
        if source != "auto":
            raise ValueError(f"unknown source {source!r}; use 'auto' or 'offline'")

        owns_client = False
        if client is None:
            try:
                client = BroodMinderClient()
                owns_client = True
            except BroodMinderError as exc:
                _log(f"store: no live client ({exc}); serving stale local data")
                return {"stale": True, "fetched_windows": 0, "covered_end": covered_end, "source": source}

        manifest = self._load_manifest()
        completed = manifest.setdefault("completed", {})
        fetched = 0
        stale = False
        try:
            for hive in hive_list:
                resume = covered_end[hive]
                for s, e in iter_windows(resume, end):
                    key = f"{hive}|{s}|{e}"
                    if key in completed:
                        continue
                    try:
                        readings = client.hive_readings(hive, s, e)
                    except BroodMinderError as exc:
                        _log(f"store: live fetch failed for hive {hive} [{s},{e}): {exc}")
                        stale = True
                        break
                    hive_dir = self.raw / hive
                    hive_dir.mkdir(parents=True, exist_ok=True)
                    with gzip.open(hive_dir / f"{s}-{e}.readings.json.gz", "wt", encoding="utf-8") as fh:
                        json.dump(readings, fh)
                    rows = 0
                    if isinstance(readings, list):
                        rows = sum(len(p.get("readings", []) or []) for p in readings)
                    rec: dict[str, Any] = {"reading_rows": rows}
                    try:
                        notes = client.hive_notes(hive, s, e, raw=True)
                        with gzip.open(hive_dir / f"{s}-{e}.notes.json.gz", "wt", encoding="utf-8") as fh:
                            json.dump(notes, fh)
                        rec["notes"] = (
                            len(notes) if isinstance(notes, list)
                            else len((notes or {}).get("notes", []) or [])
                        )
                    except Exception as exc:  # notes are best-effort; readings are primary
                        _log(f"store: notes fetch failed for hive {hive}: {exc}")
                    completed[key] = rec
                    self._save_manifest(manifest)
                    fetched += 1
                    covered_end[hive] = e
                if stale:
                    break
        finally:
            if owns_client:
                try:
                    client.close()
                except Exception:
                    pass
        if any(covered_end[h] < end for h in hive_list):
            stale = True
        return {
            "stale": stale,
            "fetched_windows": fetched,
            "covered_end": covered_end,
            "source": source,
        }
