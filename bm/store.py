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
import re
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx

from .client import BroodMinderClient, BroodMinderError, iter_windows
from .safe_io import iter_bounded_lines, load_json_document, open_binary

# A truncated/corrupt gzip window must not crash a read (a sync may be
# writing it concurrently); treat it as absent.
_GZIP_READ_ERRORS = (OSError, EOFError, zlib.error, json.JSONDecodeError)

_HIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Tail-fetch tolerances: do not spend API calls on a tail smaller than
# MIN_TAIL_SECONDS, and do not fetch the most recent SETTLE_SECONDS so
# late-arriving BLE uploads inside a window are not locked out by
# recording that window complete too early.
MIN_TAIL_SECONDS = 3600
SETTLE_SECONDS = 900

# Longest [start, end) span ensure_coverage accepts. Lookback lengths are
# caller-controlled (MCP tool arguments), and each span is split into
# 180-day API windows, so an unbounded span means unbounded work.
MAX_COVERAGE_DAYS_ENV = "BROODMINDER_MAX_COVERAGE_DAYS"
DEFAULT_MAX_COVERAGE_DAYS = 7305  # 20 years: ~41 windows per hive


def max_coverage_days() -> int:
    raw = os.environ.get(MAX_COVERAGE_DAYS_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_COVERAGE_DAYS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise ValueError(f"{MAX_COVERAGE_DAYS_ENV} must be a positive integer, got {raw!r}")
    return value


def _safe_hive_id(hive_id: str | int) -> str:
    """Validate a hive ID before it becomes a path or URL segment."""
    s = str(hive_id)
    if not _HIVE_ID_RE.fullmatch(s):
        raise ValueError(f"invalid hive_id: {s!r}")
    return s


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

    def _manifest_unreadable(self) -> bool:
        """True when a manifest file exists but cannot be parsed."""
        if not self.manifest_path.exists():
            return False
        try:
            json.loads(self.manifest_path.read_text(encoding="utf-8"))
            return False
        except (json.JSONDecodeError, OSError):
            return True

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Unique temp name per process: a fixed name can be clobbered by a
        # concurrent writer between write and replace.
        tmp = self.manifest_path.with_name(f".manifest.json.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp.replace(self.manifest_path)

    def _save_completed(self, manifest: dict[str, Any]) -> None:
        """Persist ``manifest``'s completed windows, merging any records
        written by another process since ``manifest`` was loaded.

        The ``completed`` dict identity is preserved: callers holding a
        reference to it (e.g. the sync loop) must keep writing into the
        live mapping after a save.
        """
        fresh = self._load_manifest()
        completed = manifest.setdefault("completed", {})
        for key, rec in fresh.get("completed", {}).items():
            completed.setdefault(key, rec)
        meta = manifest.setdefault("meta", {})
        for key, value in fresh.get("meta", {}).items():
            meta.setdefault(key, value)
        self._save_manifest(manifest)

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
            entry = meta.setdefault(
                hid, {"apiaryId": None, "apiaryName": None, "hiveName": None}
            )
            # Fill any fields the manifest record lacks from archived rows,
            # so a counts-only sync record cannot orphan a named hive.
            for field in ("apiaryId", "apiaryName", "hiveName"):
                if entry.get(field) is None and row.get(field) is not None:
                    entry[field] = row.get(field)
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
    def _archive_meta(self) -> dict[str, dict[str, Any]]:
        """Per-hive apiary/hive names recovered from flattened archive rows."""
        meta: dict[str, dict[str, Any]] = {}
        for row in self._iter_flat_readings():
            hid = str(row.get("hiveId") or "")
            if not hid:
                continue
            entry = meta.setdefault(
                hid, {"apiaryId": None, "apiaryName": None, "hiveName": None}
            )
            for field in ("apiaryId", "apiaryName", "hiveName"):
                if entry.get(field) is None and row.get(field) is not None:
                    entry[field] = row.get(field)
        return meta

    def _iter_raw_readings(self, hive_id: str | None) -> Iterator[dict[str, Any]]:
        if not self.raw.exists():
            return
        meta = self._hive_meta()
        # Counts-only manifest records (from a live sync) must not blank
        # out metadata the archive already knows for the hive.
        for hid, archived in self._archive_meta().items():
            entry = meta.setdefault(
                hid, {"apiaryId": None, "apiaryName": None, "hiveName": None}
            )
            for field in ("apiaryId", "apiaryName", "hiveName"):
                if entry.get(field) is None and archived.get(field) is not None:
                    entry[field] = archived[field]
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
                    payload = load_json_document(path)
                except _GZIP_READ_ERRORS:
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
        try:
            with open_binary(path) as fh:
                for line in iter_bounded_lines(fh, path):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except _GZIP_READ_ERRORS:
            return

    def iter_readings(
        self, hive_id: str | int | None = None, start: int | None = None, end: int | None = None
    ) -> Iterator[dict[str, Any]]:
        """Stream rows from BOTH raw windows and the flattened archive.

        The two sources are merged and deduplicated on
        (hive, position, device, timestamp): a partially retained raw tree
        must never hide archived history, and vice versa.
        """
        if hive_id is not None:
            hive_id = _safe_hive_id(hive_id)
        seen: dict[str, set[tuple]] = {}
        sources: list[Iterable[dict[str, Any]]] = [self._iter_flat_readings()]
        if self.raw.exists() and any(self.raw.iterdir()):
            sources.append(
                self._iter_raw_readings(str(hive_id) if hive_id is not None else None)
            )
        rows = (row for src in sources for row in src)
        for row in rows:
            # Filter first: dedupe keys are retained only for rows the
            # caller asked for, so a narrow query does not hold keys for
            # the entire archive in memory.
            if hive_id is not None and str(row.get("hiveId") or "") != str(hive_id):
                continue
            ts = row.get("timestamp")
            if start is not None and (ts is None or ts < start):
                continue
            if end is not None and (ts is None or ts >= end):
                continue
            hid = str(row.get("hiveId") or "")
            dk = (row.get("positionID"), row.get("deviceId"), row.get("timestamp"))
            hive_seen = seen.setdefault(hid, set())
            if dk in hive_seen:
                continue
            hive_seen.add(dk)
            yield row

    def iter_notes(self, hive_id: str | int | None = None) -> Iterator[dict[str, Any]]:
        """Stream notes merged from raw windows AND notes.ndjson (deduplicated)."""
        if hive_id is not None:
            hive_id = _safe_hive_id(hive_id)
        seen: set[tuple] = set()
        if self.raw.exists():
            hive_dirs = (
                sorted(self.raw.iterdir()) if hive_id is None else [self.raw / str(hive_id)]
            )
            for hdir in hive_dirs:
                if not hdir.is_dir():
                    continue
                files = sorted(hdir.glob("*.notes.json.gz")) + sorted(hdir.glob("*.notes.json"))
                for path in files:
                    try:
                        payload = load_json_document(path)
                    except _GZIP_READ_ERRORS:
                        continue
                    items = payload if isinstance(payload, list) else (payload or {}).get("notes", [])
                    for note in items or []:
                        note_text = note.get("text") or note.get("note")
                        key = (hdir.name, note.get("id") or (note.get("created"), note.get("description"), note_text))
                        if key in seen:
                            continue
                        seen.add(key)
                        yield {"hiveId": hdir.name, **note}
        notes_path = self.data_dir / "notes.ndjson"
        if notes_path.exists():
            with open_binary(notes_path) as fh:
                for line in iter_bounded_lines(fh, notes_path):
                    if not line.strip():
                        continue
                    try:
                        note = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if hive_id is not None and str(note.get("hiveId") or "") != str(hive_id):
                        continue
                    note_text = note.get("text") or note.get("note")
                    key = (str(note.get("hiveId") or ""), note.get("id") or (note.get("created"), note.get("description"), note_text))
                    if key in seen:
                        continue
                    seen.add(key)
                    yield note

    # -- live tail sync -----------------------------------------------------
    def _locked(self):
        """Exclusive file lock serializing manifest read-modify-write.

        Uses the same lockfile convention as the cron scripts
        (``data/.sync.lock`` flock), scoped to the store directory.
        Falls back to ``msvcrt.locking`` on Windows, and to a no-op lock
        when neither platform module is available.
        """
        import contextlib

        try:
            import fcntl
        except ImportError:  # non-POSIX (e.g. Windows)
            fcntl = None
        try:
            import msvcrt
        except ImportError:  # POSIX
            msvcrt = None

        @contextlib.contextmanager
        def _cm():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.data_dir / ".sync.lock", "a+") as fh:
                if fcntl is not None:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                elif msvcrt is not None:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(fh, fcntl.LOCK_UN)
                    elif msvcrt is not None:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

        return _cm()

    def _completed_intervals(self, manifest: dict[str, Any], hive: str) -> list[tuple[int, int]]:
        intervals: list[tuple[int, int]] = []
        for key in manifest.get("completed", {}):
            parts = key.rsplit("|", 2)
            if len(parts) == 3 and parts[0] == hive:
                try:
                    intervals.append((int(parts[1]), int(parts[2])))
                except ValueError:
                    continue
        return sorted(intervals)

    @staticmethod
    def _gaps(intervals: list[tuple[int, int]], start: int, end: int) -> list[tuple[int, int]]:
        """Parts of [start, end) not in sorted ``intervals`` (at most one
        per interval, plus one), without splitting them into windows."""
        gaps: list[tuple[int, int]] = []
        cur = start
        for s, e in intervals:
            if e <= cur:
                continue
            if s > cur:
                gaps.append((cur, min(s, end)))
            cur = max(cur, e)
            if cur >= end:
                break
        if cur < end:
            gaps.append((cur, end))
        return gaps

    @staticmethod
    def _missing_windows(intervals: list[tuple[int, int]], start: int, end: int) -> list[tuple[int, int]]:
        """Windows covering the parts of [start, end) not in ``intervals``.

        Callers must bound ``end - start`` (see ``ensure_coverage``): the
        result holds one entry per 180-day window of every gap.
        """
        missing: list[tuple[int, int]] = []
        for gs, ge in LocalStore._gaps(intervals, start, end):
            missing.extend(iter_windows(gs, ge))
        return missing

    def _covered_end(self, manifest: dict[str, Any], hive: str, start: int) -> int:
        intervals = self._completed_intervals(manifest, hive)
        return max([start] + [e for _s, e in intervals])

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
        missing intervals (prefix, interior gaps, or tail) per hive and
        persists each window before continuing. Completed windows are
        never re-fetched; failed note fetches are tracked separately
        (``notes_missing``) and retried without refetching readings.
        Failures degrade to ``stale: True`` rather than raising; invalid
        arguments (including a span over ``BROODMINDER_MAX_COVERAGE_DAYS``)
        raise ``ValueError``.
        The whole read-modify-write runs under an exclusive file lock.
        """
        if source not in ("auto", "offline"):
            raise ValueError(f"unknown source {source!r}; use 'auto' or 'offline'")
        hive_list = [_safe_hive_id(h) for h in hive_ids]
        limit_days = max_coverage_days()
        if end - start > limit_days * 86400:
            raise ValueError(
                f"requested interval of {(end - start) // 86400} days exceeds the "
                f"{limit_days} days limit (set {MAX_COVERAGE_DAYS_ENV} to raise it)"
            )
        if source == "offline":
            # Read-only path: never acquire the (write-requiring) lock, so
            # offline queries work on read-only data mounts.
            manifest = self._load_manifest()
            completed = manifest.get("completed", {})
            stale = False
            for h in hive_list:
                # Only "is there any gap?" matters here: no window list.
                if self._gaps(self._completed_intervals(manifest, h), start, end):
                    stale = True
                for key, rec in completed.items():
                    parts = key.rsplit("|", 2)
                    notes_unknown = rec.get("notes_missing") or "notes" not in rec
                    if len(parts) == 3 and parts[0] == h and notes_unknown:
                        stale = True
            return {
                "stale": stale, "fetched_windows": 0,
                "covered_end": {h: self._covered_end(manifest, h, start) for h in hive_list},
                "source": source,
            }
        try:
            with self._locked():
                return self._ensure_coverage_locked(hive_list, start, end, source, client)
        except OSError as exc:
            # Unwritable/read-only store: degrade to stale local data
            # rather than raising out of a tool call.
            _log(f"store: cannot acquire sync lock ({exc}); serving stale local data")
            manifest = self._load_manifest()
            return {
                "stale": True, "fetched_windows": 0,
                "covered_end": {h: self._covered_end(manifest, h, start) for h in hive_list},
                "source": source,
            }

    def _ensure_coverage_locked(
        self, hive_list: list[str], start: int, end: int, source: str, client: Any
    ) -> dict[str, Any]:
        # Settle lag: never fetch the most recent SETTLE_SECONDS, so
        # late-arriving BLE uploads are not locked out of a window that
        # was recorded complete before they arrived.
        fetch_end = end - SETTLE_SECONDS if (end - start) > SETTLE_SECONDS else end
        if self._manifest_unreadable():
            _log("store: manifest exists but is unreadable; serving stale local data")
            return {
                "stale": True, "fetched_windows": 0,
                "covered_end": {h: start for h in hive_list}, "source": source,
            }
        manifest = self._load_manifest()
        completed = manifest.setdefault("completed", {})
        covered_end = {h: self._covered_end(manifest, h, start) for h in hive_list}

        def _notes_missing(hive: str) -> list[tuple[int, int]]:
            out: list[tuple[int, int]] = []
            for key, rec in completed.items():
                parts = key.rsplit("|", 2)
                # A record with no notes-completion info at all (e.g. from
                # `extract_all.py --no-notes`) also needs a notes-only fetch.
                notes_unknown = rec.get("notes_missing") or "notes" not in rec
                if len(parts) == 3 and parts[0] == hive and notes_unknown:
                    try:
                        s, e = int(parts[1]), int(parts[2])
                    except ValueError:
                        continue
                    if s < end and e > start:
                        out.append((s, e))
            return out

        missing = {
            h: self._missing_windows(self._completed_intervals(manifest, h), start, fetch_end)
            for h in hive_list
        }
        # Minimum tail: a lone tail gap smaller than MIN_TAIL_SECONDS (with
        # prior coverage) is not worth API calls yet; it is within tolerance,
        # so it does not count as stale either.
        for h in hive_list:
            wins = missing[h]
            intervals = self._completed_intervals(manifest, h)
            if wins and intervals:
                if fetch_end - wins[0][0] < MIN_TAIL_SECONDS and wins[-1][1] >= fetch_end:
                    missing[h] = []
        if source == "offline":
            stale = any(missing[h] or _notes_missing(h) for h in hive_list)
            return {"stale": stale, "fetched_windows": 0, "covered_end": covered_end, "source": source}
        if not any(missing[h] or _notes_missing(h) for h in hive_list):
            return {"stale": False, "fetched_windows": 0, "covered_end": covered_end, "source": source}

        owns_client = False
        if client is None:
            try:
                client = BroodMinderClient()
                owns_client = True
            except BroodMinderError as exc:
                _log(f"store: no live client ({exc}); serving stale local data")
                return {"stale": True, "fetched_windows": 0, "covered_end": covered_end, "source": source}

        fetched = 0
        stale = False

        def _fetch_notes(hive: str, s: int, e: int, rec: dict[str, Any]) -> bool:
            nonlocal network_down
            try:
                notes = client.hive_notes(hive, s, e, raw=True)
            except Exception as exc:  # tracked separately; readings stay complete
                _log(f"store: notes fetch failed for hive {hive} window {s}-{e}: {exc}")
                rec["notes_missing"] = True
                if isinstance(exc, BroodMinderError) and exc.status in (0, 429):
                    # Transport down or quota exhausted (client already
                    # burned its retries): abort the rest of the batch.
                    network_down = True
                return False
            hive_dir = self.raw / hive
            hive_dir.mkdir(parents=True, exist_ok=True)
            with gzip.open(hive_dir / f"{s}-{e}.notes.json.gz", "wt", encoding="utf-8") as fh:
                json.dump(notes, fh)
            rec["notes"] = (
                len(notes) if isinstance(notes, list)
                else len((notes or {}).get("notes", []) or [])
            )
            rec.pop("notes_missing", None)
            return True

        network_down = False
        try:
            for hive in hive_list:
                if network_down:
                    # A transport failure (status 0) already burned the
                    # client's retries; do not repeat them per hive.
                    stale = True
                    break
                # Retry previously failed notes without refetching readings.
                for s, e in _notes_missing(hive):
                    rec = completed[f"{hive}|{s}|{e}"]
                    if not _fetch_notes(hive, s, e, rec):
                        stale = True
                    self._save_completed(manifest)
                    if network_down:
                        break
                if network_down:
                    stale = True
                    break  # skip readings fetch and remaining hives
                for s, e in missing[hive]:
                    key = f"{hive}|{s}|{e}"
                    if key in completed:
                        continue
                    try:
                        readings = client.hive_readings(hive, s, e)
                    except (BroodMinderError, httpx.HTTPError, ValueError) as exc:
                        _log(f"store: live fetch failed for hive {hive} [{s},{e}): {exc}")
                        stale = True
                        if isinstance(exc, BroodMinderError) and exc.status in (0, 429):
                            network_down = True
                        break
                    hive_dir = self.raw / hive
                    hive_dir.mkdir(parents=True, exist_ok=True)
                    with gzip.open(hive_dir / f"{s}-{e}.readings.json.gz", "wt", encoding="utf-8") as fh:
                        json.dump(readings, fh)
                    rows = 0
                    if isinstance(readings, list):
                        rows = sum(len(p.get("readings", []) or []) for p in readings)
                    rec = {"reading_rows": rows}
                    if not _fetch_notes(hive, s, e, rec):
                        stale = True
                    completed[key] = rec
                    self._save_completed(manifest)
                    fetched += 1
                    if network_down:
                        break
                covered_end[hive] = self._covered_end(manifest, hive, start)
                if self._gaps(self._completed_intervals(manifest, hive), start, fetch_end):
                    stale = True
        finally:
            if owns_client:
                try:
                    client.close()
                except Exception:
                    pass
        return {
            "stale": stale,
            "fetched_windows": fetched,
            "covered_end": covered_end,
            "source": source,
        }
