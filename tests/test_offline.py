"""Fast, deterministic offline unit tests (no network calls, run in CI without API key)."""

from __future__ import annotations

import csv
import gzip
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bm.client import BroodMinderClient, BroodMinderError, iter_windows, now_epoch, to_epoch
from scripts.extract_all import (
    count_notes,
    count_reading_rows,
    get_hive_resume_start,
    get_manifest_max_end,
    parse_date,
)
from scripts.flatten import KNOWN_METRIC_KEYS


def test_iter_windows_basic():
    # Exactly 2 windows of 100 seconds
    windows = list(iter_windows(1000, 1200, window=100))
    assert windows == [(1000, 1100), (1100, 1200)]


def test_iter_windows_partial_remainder():
    # 250 seconds -> 2 full windows of 100, one remainder of 50
    windows = list(iter_windows(1000, 1250, window=100))
    assert windows == [(1000, 1100), (1100, 1200), (1200, 1250)]


def test_iter_windows_empty():
    assert list(iter_windows(1000, 1000, window=100)) == []
    assert list(iter_windows(1200, 1000, window=100)) == []


def test_to_epoch_and_now_epoch():
    now = now_epoch()
    assert isinstance(now, int)
    assert now > 1_600_000_000

    dt_naive = datetime(2026, 7, 1, 12, 0, 0)
    dt_aware = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert to_epoch(dt_naive) == to_epoch(dt_aware)


def test_parse_date():
    ts = parse_date("2026-07-01")
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert dt.year == 2026
    assert dt.month == 7
    assert dt.day == 1
    assert dt.hour == 0
    assert dt.minute == 0
    assert dt.second == 0


def test_get_manifest_max_end():
    assert get_manifest_max_end({}) is None
    assert get_manifest_max_end({"completed": {}}) is None

    manifest = {
        "completed": {
            "hive1|1000|2000": {"reading_rows": 10},
            "hive1|2000|3500": {"reading_rows": 20},
            "hive2|1000|2500": {"reading_rows": 5},
            "malformed_key": {"reading_rows": 0},
        }
    }
    assert get_manifest_max_end(manifest) == 3500


def test_get_hive_resume_start():
    manifest = {
        "completed": {
            "hive1|1000|2000": {"reading_rows": 10},
            "hive1|2000|3500": {"reading_rows": 20},
            "hive2|1000|2500": {"reading_rows": 5},
            "hive|with|pipe|1000|4200": {"reading_rows": 7},
            "malformed_key": {"reading_rows": 0},
        }
    }
    # Hive with multiple windows resumes from its own latest window end
    assert get_hive_resume_start(manifest, "hive1", default_start=500) == 3500
    assert get_hive_resume_start(manifest, "hive2", default_start=500) == 2500
    # Hive with pipe characters in hiveId handled properly via rsplit
    assert get_hive_resume_start(manifest, "hive|with|pipe", default_start=500) == 4200
    # Hive with no prior windows falls back to default_start
    assert get_hive_resume_start(manifest, "hive3_unknown", default_start=500) == 500
    assert get_hive_resume_start({}, "hive1", default_start=500) == 500


def test_count_reading_rows():
    assert count_reading_rows(None) == 0
    assert count_reading_rows([]) == 0
    payload = [
        {"positionID": "p1", "readings": [{"deviceId": "d1"}, {"deviceId": "d1"}]},
        {"positionID": "p2", "readings": [{"deviceId": "d2"}]},
        {"positionID": "p3", "readings": []},
    ]
    assert count_reading_rows(payload) == 3


def test_count_notes():
    assert count_notes(None) == 0
    assert count_notes([]) == 0
    assert count_notes([{"id": "n1"}, {"id": "n2"}]) == 2
    assert count_notes({"notes": [{"id": "n1"}]}) == 1


def test_known_metric_keys_stability():
    expected = {"audio", "humidity", "radar", "swarmState", "temperature", "weight"}
    assert expected.issubset(KNOWN_METRIC_KEYS)


def test_client_init_requires_key(monkeypatch):
    """Verify BroodMinderClient raises BroodMinderError when BROODMINDER_API_KEY is not set."""
    monkeypatch.delenv("BROODMINDER_API_KEY", raising=False)
    with pytest.raises(BroodMinderError) as exc_info:
        BroodMinderClient(api_key=None)
    assert "BROODMINDER_API_KEY not set" in str(exc_info.value)


def test_client_hive_notes_normalization(monkeypatch):
    """Verify BroodMinderClient.hive_notes normalizes payloads and raw option preserves wire envelope."""
    client = BroodMinderClient(api_key="test-key")
    sample_notes = [
        {
            "id": "note-1",
            "hiveId": "h1",
            "description": "Added winter feed block",
            "tags": [{"id": "tag-1", "name": "Feeding"}],
            "timestamp": 1700000000,
            "created": 1700000050,
        }
    ]

    # Case 1: Bare array returned by API (e.g. empty or legacy)
    monkeypatch.setattr(client, "get_json", lambda path, params: sample_notes)
    assert client.hive_notes("h1", 1000, 2000) == sample_notes

    # Case 2: Dict wrapper {"notes": [...]} returned when notes exist
    monkeypatch.setattr(client, "get_json", lambda path, params: {"notes": sample_notes})
    assert client.hive_notes("h1", 1000, 2000) == sample_notes

    # Case 3: Empty bare list returned when no notes exist
    monkeypatch.setattr(client, "get_json", lambda path, params: [])
    assert client.hive_notes("h1", 1000, 2000) == []

    # Case 4: Dict with empty notes list
    monkeypatch.setattr(client, "get_json", lambda path, params: {"notes": []})
    assert client.hive_notes("h1", 1000, 2000) == []

    # Case 5: None or unexpected payload falls back safely to empty list
    monkeypatch.setattr(client, "get_json", lambda path, params: None)
    assert client.hive_notes("h1", 1000, 2000) == []

    # Case 6: raw=True preserves the wire envelope losslessly
    monkeypatch.setattr(client, "get_json", lambda path, params: {"notes": sample_notes})
    assert client.hive_notes("h1", 1000, 2000, raw=True) == {"notes": sample_notes}

    # Case 7: hive_notes_raw convenience alias returns exact wire payload
    assert client.hive_notes_raw("h1", 1000, 2000) == {"notes": sample_notes}


def test_flatten_merge_deduplication(tmp_path, monkeypatch):
    import scripts.flatten as fl

    out_dir = tmp_path / "extract"
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True)

    monkeypatch.setattr(fl, "OUT", out_dir)
    monkeypatch.setattr(fl, "RAW", raw_dir)

    # 1. Create a prior baseline readings.ndjson.gz with a custom metric
    prior_rows = [
        {
            "apiaryId": "ap1", "apiaryName": "Apiary 1",
            "hiveId": "h1", "hiveName": "Hive 1",
            "positionID": "pos1", "deviceId": "dev1",
            "timestamp": 1000, "datetime": "2026-07-01T00:00:00+00:00",
            "batteryLevel": 90, "chargeRemaining": None,
            "m_temperature": 75.0, "m_humidity": 50.0, "m_custom_sensor": 42.0,
        },
        {
            "apiaryId": "ap1", "apiaryName": "Apiary 1",
            "hiveId": "h1", "hiveName": "Hive 1",
            "positionID": "pos1", "deviceId": "dev1",
            "timestamp": 1100, "datetime": "2026-07-01T01:00:00+00:00",
            "batteryLevel": 89, "chargeRemaining": None,
            "m_temperature": 76.0, "m_humidity": 51.0,
        },
    ]
    with gzip.open(out_dir / "readings.ndjson.gz", "wt", encoding="utf-8") as fh:
        for r in prior_rows:
            fh.write(json.dumps(r) + "\n")

    # 2. Create prior notes.ndjson
    with (out_dir / "notes.ndjson").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"hiveId": "h1", "hiveName": "Hive 1", "id": "note1", "description": "old note"}) + "\n")

    # 3. Create raw files: 1 duplicate row (ts=1100), 1 new row (ts=1200)
    h1_dir = raw_dir / "h1"
    h1_dir.mkdir()
    raw_readings = [
        {
            "positionID": "pos1",
            "readings": [
                {"deviceId": "dev1", "timestamp": 1100, "batteryLevel": 89, "chargeRemaining": None,
                 "readings": {"temperature": 76.0, "humidity": 51.0}},
                {"deviceId": "dev1", "timestamp": 1200, "batteryLevel": 88, "chargeRemaining": None,
                 "readings": {"temperature": 77.0, "humidity": 52.0}},
            ],
        }
    ]
    with gzip.open(h1_dir / "1100-1300.readings.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(raw_readings, fh)

    manifest = {
        "completed": {
            "h1|1100|1300": {"apiaryName": "Apiary 1", "apiaryId": "ap1", "hiveName": "Hive 1"}
        }
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest))

    # Run flatten with --merge
    monkeypatch.setattr("sys.argv", ["flatten.py", "--merge"])
    exit_code = fl.main()
    assert exit_code == 0

    # Verify merged readings
    merged_rows = []
    with gzip.open(out_dir / "readings.ndjson.gz", "rt", encoding="utf-8") as fh:
        for line in fh:
            merged_rows.append(json.loads(line))

    # Total should be 3 (ts=1000, 1100, 1200; duplicate 1100 discarded)
    assert len(merged_rows) == 3
    timestamps = [r["timestamp"] for r in merged_rows]
    assert timestamps == [1000, 1100, 1200]

    # Verify CSV has full header with KNOWN_METRIC_KEYS and the discovered archive metric
    with gzip.open(out_dir / "readings.csv.gz", "rt", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        for expected_col in ["m_audio", "m_humidity", "m_radar", "m_swarmState", "m_temperature", "m_weight"]:
            assert expected_col in header
        # Discovered custom metric from existing archive is preserved in header
        assert "m_custom_sensor" in header

    # Verify coverage.json has min_ts=1000, max_ts=1200, rows=3
    cov = json.loads((out_dir / "coverage.json").read_text())
    assert "h1" in cov
    assert cov["h1"]["rows"] == 3
    assert cov["h1"]["earliest"] == "1970-01-01T00:16:40+00:00"
    assert cov["h1"]["latest"] == "1970-01-01T00:20:00+00:00"

    # Verify no temporary files remain
    tmp_files = list(out_dir.glob("*.tmp*"))
    assert tmp_files == []


def test_flatten_fresh_atomic_build(tmp_path, monkeypatch):
    import scripts.flatten as fl

    out_dir = tmp_path / "extract"
    raw_dir = out_dir / "raw"
    h1_dir = raw_dir / "h1"
    h1_dir.mkdir(parents=True)

    monkeypatch.setattr(fl, "OUT", out_dir)
    monkeypatch.setattr(fl, "RAW", raw_dir)

    raw_readings = [
        {
            "positionID": "pos1",
            "readings": [
                {"deviceId": "dev1", "timestamp": 2000, "readings": {"temperature": 70.0}},
                {"deviceId": "dev1", "timestamp": 2000, "readings": {"temperature": 70.0}},  # dup
                {"deviceId": "dev1", "timestamp": 2100, "readings": {"temperature": 71.0}},
            ],
        }
    ]
    with gzip.open(h1_dir / "2000-2200.readings.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(raw_readings, fh)

    raw_notes = [{"id": "n1", "description": "note 1"}]
    with gzip.open(h1_dir / "2000-2200.notes.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(raw_notes, fh)

    manifest = {"completed": {"h1|2000|2200": {"apiaryName": "Ap1", "hiveName": "H1"}}}
    (out_dir / "manifest.json").write_text(json.dumps(manifest))

    # Fresh run (no --merge)
    monkeypatch.setattr("sys.argv", ["flatten.py"])
    assert fl.main() == 0

    assert (out_dir / "readings.ndjson.gz").exists()
    assert (out_dir / "readings.csv.gz").exists()
    assert (out_dir / "notes.ndjson").exists()
    assert (out_dir / "coverage.json").exists()

    # Deduplicated 2 rows from 3
    with gzip.open(out_dir / "readings.ndjson.gz", "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    assert len(rows) == 2

    # No leftover temporary files
    assert list(out_dir.glob("*.tmp*")) == []


def test_flatten_atomic_interruption_preserves_existing_targets(tmp_path, monkeypatch):
    import scripts.flatten as fl

    out_dir = tmp_path / "extract"
    raw_dir = out_dir / "raw"
    h1_dir = raw_dir / "h1"
    h1_dir.mkdir(parents=True)

    monkeypatch.setattr(fl, "OUT", out_dir)
    monkeypatch.setattr(fl, "RAW", raw_dir)

    # Pre-create baseline valid targets
    orig_readings = [{"hiveId": "h1", "timestamp": 100, "datetime": "1970-01-01T00:01:40+00:00"}]
    with gzip.open(out_dir / "readings.ndjson.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(orig_readings[0]) + "\n")
    orig_notes = [{"hiveId": "h1", "id": "orig_note"}]
    (out_dir / "notes.ndjson").write_text(json.dumps(orig_notes[0]) + "\n")

    raw_readings = [
        {
            "positionID": "pos1",
            "readings": [
                {"deviceId": "dev1", "timestamp": 2000, "readings": {"temperature": 70.0}},
            ],
        }
    ]
    with gzip.open(h1_dir / "2000-2200.readings.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(raw_readings, fh)

    manifest = {"completed": {"h1|2000|2200": {"apiaryName": "Ap1", "hiveName": "H1"}}}
    (out_dir / "manifest.json").write_text(json.dumps(manifest))

    # Simulate crash / exception during json.dumps in reading loop
    original_dumps = json.dumps
    def failing_dumps(obj, *args, **kwargs):
        if isinstance(obj, dict) and obj.get("timestamp") == 2000:
            raise RuntimeError("Simulated crash during write")
        return original_dumps(obj, *args, **kwargs)

    monkeypatch.setattr(json, "dumps", failing_dumps)
    monkeypatch.setattr("sys.argv", ["flatten.py"])

    with pytest.raises(RuntimeError, match="Simulated crash"):
        fl.main()

    # Pre-existing target files MUST survive uncorrupted
    with gzip.open(out_dir / "readings.ndjson.gz", "rt", encoding="utf-8") as fh:
        surviving = [json.loads(line) for line in fh]
    assert surviving == orig_readings
    assert (out_dir / "notes.ndjson").read_text().strip() == json.dumps(orig_notes[0])

    # No leftover temporary files
    assert list(out_dir.glob("*.tmp*")) == []


def test_extract_all_catchup_roster_check(tmp_path, monkeypatch, capsys):
    """Verify extract_all catchup correctly inspects manifest rosters without redundant fetches."""
    import scripts.extract_all as ex

    out_dir = tmp_path / "extract"
    out_dir.mkdir()
    end_ts = 1782864000  # 2026-07-01 00:00:00 UTC
    manifest = {
        "completed": {
            f"h1|1000|{end_ts}": {"reading_rows": 10},
            f"h2|1000|{end_ts}": {"reading_rows": 20},
        }
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest))

    calls = {"readings": 0, "apiaries": 0}

    class MockClient:
        def __init__(self):
            """Initialize mock client."""
            self.call_count = 0

        def __enter__(self):
            """Enter context manager."""
            return self

        def __exit__(self, *args):
            """Exit context manager."""
            pass

        def apiaries(self):
            """Return mock apiary topology."""
            calls["apiaries"] += 1
            return [{"apiaryId": "a1", "name": "Apiary 1",
                     "hives": [{"hiveId": "h1", "name": "H1"}, {"hiveId": "h2", "name": "H2"}]}]

        def hive_readings(self, hid, s, e):
            """Return mock readings."""
            calls["readings"] += 1
            return []

        def hive_notes(self, hid, s, e, *args, **kwargs):
            """Return mock notes."""
            return []

    monkeypatch.setattr(ex, "BroodMinderClient", MockClient)
    monkeypatch.setattr("sys.argv", [
        "extract_all.py", "--catchup", "--end", "2026-07-01", "--out", str(out_dir)
    ])

    # Case 1: All discovered hives are caught up -> 1 discovery call, 0 extraction calls
    assert ex.main() == 0
    captured = capsys.readouterr()
    assert "already up to date through 2026-07-01 across all 2 hives" in captured.out
    assert calls["apiaries"] == 1
    assert calls["readings"] == 0

    # Case 2: New hive added to account -> not up to date, extracts the new hive
    class MockClientWithNewHive(MockClient):
        def apiaries(self):
            """Return mock apiaries with an additional hive."""
            calls["apiaries"] += 1
            return [{"apiaryId": "a1", "name": "Apiary 1",
                     "hives": [{"hiveId": "h1", "name": "H1"},
                               {"hiveId": "h2", "name": "H2"},
                               {"hiveId": "h3_new", "name": "H3"}]}]

    monkeypatch.setattr(ex, "BroodMinderClient", MockClientWithNewHive)
    assert ex.main() == 0
    captured = capsys.readouterr()
    assert "already up to date across all" not in captured.out
    assert calls["readings"] > 0



def test_flatten_merge_completed_only_skips_unmanifested_hives(tmp_path, monkeypatch):
    import scripts.flatten as fl

    out_dir = tmp_path / "extract"
    raw_dir = out_dir / "raw"
    for hid in ("h1", "h2"):
        (raw_dir / hid).mkdir(parents=True)
        with gzip.open(raw_dir / hid / "1-2.readings.json.gz", "wt", encoding="utf-8") as fh:
            json.dump([{"positionID": "p", "readings": [
                {"deviceId": "d", "timestamp": 1, "readings": {"temperature": 70.0}}]}], fh)
        (raw_dir / hid / "1-2.notes.json").write_text(json.dumps([{"id": f"n-{hid}"}]))
    (out_dir / "manifest.json").write_text(json.dumps(
        {"completed": {"h1|1|2": {"apiaryName": "A", "apiaryId": "a1", "hiveName": "H1"}}}))
    monkeypatch.setattr(fl, "OUT", out_dir)
    monkeypatch.setattr(fl, "RAW", raw_dir)

    monkeypatch.setattr("sys.argv", ["flatten.py", "--merge", "--completed-only", "--no-csv"])
    assert fl.main() == 0

    with gzip.open(out_dir / "readings.ndjson.gz", "rt", encoding="utf-8") as fh:
        assert [json.loads(line)["hiveId"] for line in fh] == ["h1"]
    notes = [json.loads(line)["hiveId"] for line in (out_dir / "notes.ndjson").read_text().splitlines()]
    assert notes == ["h1"]


def test_flatten_completed_only_skips_incomplete_windows_of_known_hive(tmp_path, monkeypatch):
    import scripts.flatten as fl

    out_dir = tmp_path / "extract"
    hdir = out_dir / "raw" / "h1"
    hdir.mkdir(parents=True)
    with gzip.open(hdir / "1-2.readings.json.gz", "wt", encoding="utf-8") as fh:
        json.dump([{"positionID": "p", "readings": [
            {"deviceId": "d", "timestamp": 1, "readings": {"temperature": 70.0}}]}], fh)
    (hdir / "2-3.readings.json.gz").write_bytes(b"truncated")  # failed mid-write, not in manifest
    (hdir / "2-3.notes.json").write_text("{")
    (out_dir / "manifest.json").write_text(json.dumps(
        {"completed": {"h1|1|2": {"apiaryName": "A", "apiaryId": "a1", "hiveName": "H1"}}}))
    monkeypatch.setattr(fl, "OUT", out_dir)
    monkeypatch.setattr(fl, "RAW", out_dir / "raw")

    monkeypatch.setattr("sys.argv", ["flatten.py", "--merge", "--completed-only"])
    assert fl.main() == 0
    with gzip.open(out_dir / "readings.ndjson.gz", "rt", encoding="utf-8") as fh:
        assert [json.loads(line)["timestamp"] for line in fh] == [1]


def test_flatten_completed_only_merge_handles_pipe_in_hive_id(tmp_path, monkeypatch):
    import scripts.flatten as fl

    hid = "hive|with|pipe"
    out_dir = tmp_path / "extract"
    hdir = out_dir / "raw" / hid
    hdir.mkdir(parents=True)
    for stem, ts in (("1-2", 1), ("2-3", 2)):
        with gzip.open(hdir / f"{stem}.readings.json.gz", "wt", encoding="utf-8") as fh:
            json.dump([{"positionID": "p", "readings": [
                {"deviceId": "d", "timestamp": ts, "readings": {"temperature": 70.0}}]}], fh)
        (hdir / f"{stem}.notes.json").write_text(json.dumps([{"id": f"n-{stem}"}]))
    # only the 1-2 window is completed; 2-3 is on disk but absent from the manifest
    (out_dir / "manifest.json").write_text(json.dumps(
        {"completed": {f"{hid}|1|2": {"apiaryName": "A", "apiaryId": "a1", "hiveName": "H1"}}}))
    monkeypatch.setattr(fl, "OUT", out_dir)
    monkeypatch.setattr(fl, "RAW", out_dir / "raw")

    monkeypatch.setattr("sys.argv", ["flatten.py", "--merge", "--completed-only", "--no-csv"])
    assert fl.main() == 0

    with gzip.open(out_dir / "readings.ndjson.gz", "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    assert [(r["hiveId"], r["timestamp"]) for r in rows] == [(hid, 1)]
    notes = [json.loads(line) for line in (out_dir / "notes.ndjson").read_text().splitlines()]
    assert [(n["hiveId"], n["id"]) for n in notes] == [(hid, "n-1-2")]


def test_flatten_merge_completed_only_filters_archived_incomplete_windows(tmp_path, monkeypatch):
    """--merge --completed-only removes previously archived incomplete rows/notes
    while preserving completed windows whose raw files were purged, and allows
    later completed windows to publish with full metadata."""
    import scripts.flatten as fl

    out_dir = tmp_path / "extract"
    raw_dir = out_dir / "raw"
    h1_dir = raw_dir / "h1"
    hnew_dir = raw_dir / "h_new"
    h1_dir.mkdir(parents=True)
    hnew_dir.mkdir(parents=True)

    # Seed existing archive containing:
    # 1) h1 ts=50 from completed window 0|100 whose raw file was already purged
    # 2) h1 ts=150 from incomplete window 100-200 (archived by an earlier unfiltered merge)
    # 3) h_new ts=120 from unmanifested hive (archived earlier with null metadata)
    prior_readings = [
        {
            "apiaryId": "a1", "apiaryName": "Yard", "hiveId": "h1", "hiveName": "Hive 1",
            "positionID": "p1", "deviceId": "d1", "timestamp": 50,
            "datetime": "1970-01-01T00:00:50+00:00", "batteryLevel": 95, "chargeRemaining": None,
            "m_temperature": 74.0,
        },
        {
            "apiaryId": "a1", "apiaryName": "Yard", "hiveId": "h1", "hiveName": "Hive 1",
            "positionID": "p1", "deviceId": "d1", "timestamp": 150,
            "datetime": "1970-01-01T00:02:30+00:00", "batteryLevel": 94, "chargeRemaining": None,
            "m_temperature": 75.0, "m_stale_partial": 99.0,
        },
        {
            "apiaryId": None, "apiaryName": None, "hiveId": "h_new", "hiveName": None,
            "positionID": "p1", "deviceId": "d2", "timestamp": 120,
            "datetime": "1970-01-01T00:02:00+00:00", "batteryLevel": 90, "chargeRemaining": None,
            "m_temperature": 72.0,
        },
    ]
    with gzip.open(out_dir / "readings.ndjson.gz", "wt", encoding="utf-8") as fh:
        for row in prior_readings:
            fh.write(json.dumps(row) + "\n")

    prior_notes = [
        {"hiveId": "h1", "hiveName": "Hive 1", "id": "n-purged-ok", "description": "historical"},
        {"hiveId": "h1", "hiveName": "Hive 1", "id": "n-inc-ts", "timestamp": 150, "description": "partial"},
        {"hiveId": "h1", "hiveName": "Hive 1", "id": "n-inc-notime", "description": "partial no ts"},
        {"hiveId": "h_new", "hiveName": None, "id": "n-new", "timestamp": 120, "description": "unmanifested hive"},
    ]
    (out_dir / "notes.ndjson").write_text("\n".join(json.dumps(n) for n in prior_notes) + "\n")

    # Unmanifested raw files currently on disk for 100-200 and h_new
    with gzip.open(h1_dir / "100-200.readings.json.gz", "wt", encoding="utf-8") as fh:
        json.dump([{"positionID": "p1", "readings": [
            {"deviceId": "d1", "timestamp": 150, "batteryLevel": 94, "readings": {"temperature": 75.5}}]}], fh)
    (h1_dir / "100-200.notes.json").write_text(json.dumps([
        {"id": "n-inc-ts", "timestamp": 150, "description": "partial"},
        {"id": "n-inc-notime", "description": "partial no ts"},
    ]))
    with gzip.open(hnew_dir / "100-200.readings.json.gz", "wt", encoding="utf-8") as fh:
        json.dump([{"positionID": "p1", "readings": [
            {"deviceId": "d2", "timestamp": 120, "batteryLevel": 90, "readings": {"temperature": 72.0}}]}], fh)
    (hnew_dir / "100-200.notes.json").write_text(json.dumps([
        {"id": "n-new", "timestamp": 120, "description": "unmanifested hive"},
    ]))

    # Pass 1: only h1|0|100 is completed in manifest.json
    (out_dir / "manifest.json").write_text(json.dumps({
        "completed": {"h1|0|100": {"apiaryName": "Yard", "apiaryId": "a1", "hiveName": "Hive 1"}}
    }))
    monkeypatch.setattr(fl, "OUT", out_dir)
    monkeypatch.setattr(fl, "RAW", raw_dir)
    monkeypatch.setattr("sys.argv", ["flatten.py", "--merge", "--completed-only"])
    assert fl.main() == 0

    with gzip.open(out_dir / "readings.ndjson.gz", "rt", encoding="utf-8") as fh:
        pass1_rows = [json.loads(line) for line in fh]
    assert [(r["hiveId"], r["timestamp"]) for r in pass1_rows] == [("h1", 50)]
    with gzip.open(out_dir / "readings.csv.gz", "rt", encoding="utf-8") as fh:
        csv_header = next(csv.reader(fh))
    assert "m_stale_partial" not in csv_header
    pass1_notes = [json.loads(line) for line in (out_dir / "notes.ndjson").read_text().splitlines()]
    assert [n["id"] for n in pass1_notes] == ["n-purged-ok"]

    # Pass 2: 100-200 completes for both hives (and simulate a pre-existing null-meta archived row)
    with gzip.open(out_dir / "readings.ndjson.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(pass1_rows[0]) + "\n")
        fh.write(json.dumps(prior_readings[2]) + "\n")  # h_new with null metadata
    (out_dir / "notes.ndjson").write_text(
        json.dumps(pass1_notes[0]) + "\n" + json.dumps(prior_notes[3]) + "\n"
    )
    (out_dir / "manifest.json").write_text(json.dumps({
        "completed": {
            "h1|0|100": {"apiaryName": "Yard", "apiaryId": "a1", "hiveName": "Hive 1"},
            "h1|100|200": {"apiaryName": "Yard", "apiaryId": "a1", "hiveName": "Hive 1"},
            "h_new|100|200": {"apiaryName": "Yard", "apiaryId": "a1", "hiveName": "Hive New"},
        }
    }))
    monkeypatch.setattr("sys.argv", ["flatten.py", "--merge", "--completed-only", "--no-csv"])
    assert fl.main() == 0

    with gzip.open(out_dir / "readings.ndjson.gz", "rt", encoding="utf-8") as fh:
        pass2_list = [json.loads(line) for line in fh]
    pass2_rows = {(r["hiveId"], r["timestamp"]): r for r in pass2_list}
    assert len(pass2_list) == len(pass2_rows), "duplicate readings in merged output"
    assert set(pass2_rows) == {("h1", 50), ("h1", 150), ("h_new", 120)}
    assert pass2_rows[("h1", 150)]["m_temperature"] == 75.5
    assert pass2_rows[("h_new", 120)]["hiveName"] == "Hive New"
    assert pass2_rows[("h_new", 120)]["apiaryId"] == "a1"
    pass2_notes = {
        n["id"]: n for n in (json.loads(line) for line in (out_dir / "notes.ndjson").read_text().splitlines())
    }
    assert set(pass2_notes) == {"n-purged-ok", "n-inc-ts", "n-inc-notime", "n-new"}
    assert pass2_notes["n-new"]["hiveName"] == "Hive New"


def test_extract_all_partial_rate_limit_to_flatten_completed_only_e2e(tmp_path, monkeypatch):
    """End-to-end contract test: extract_all writes raw files and manifest keys
    in the exact format flatten.py --completed-only filters."""
    import scripts.extract_all as ex
    import scripts.flatten as fl
    from bm.client import RateLimited

    out_dir = tmp_path / "extract"
    rate_limit_h2 = {"active": True}

    class PartialClient:
        def __init__(self):
            """Initialize partial mock client."""
            self.call_count = 0

        def __enter__(self):
            """Enter context manager."""
            return self

        def __exit__(self, *args):
            """Exit context manager."""
            pass

        def apiaries(self):
            """Return two hives in one apiary."""
            self.call_count += 1
            return [{"apiaryId": "a1", "name": "North", "hives": [
                {"hiveId": "h1", "name": "Hive 1"},
                {"hiveId": "h2", "name": "Hive 2"},
            ]}]

        def hive_readings(self, hid, s, e):
            """Return one reading inside [s, e)."""
            self.call_count += 1
            return [{"positionID": "p1", "readings": [
                {"deviceId": f"dev-{hid}", "timestamp": s + 60, "readings": {"temperature": 88.0}}
            ]}]

        def hive_notes(self, hid, s, e, **_kwargs):
            """Rate-limit h2 on first pass after its readings file is already written."""
            self.call_count += 1
            if hid == "h2" and rate_limit_h2["active"]:
                raise RateLimited(429, "GET", f"/user/hive/{hid}/notes", "daily limit exceeded")
            return {"notes": [{"id": f"note-{hid}", "timestamp": s + 60, "description": "ok"}]}

    monkeypatch.setattr(ex, "BroodMinderClient", PartialClient)
    monkeypatch.setattr(fl, "OUT", out_dir)
    monkeypatch.setattr(fl, "RAW", out_dir / "raw")

    # Run 1: h1 completes, h2 writes readings.json.gz then hits 429 on notes (extract exits 0)
    monkeypatch.setattr("sys.argv", [
        "extract_all.py", "--start", "2026-01-01", "--end", "2026-01-02", "--out", str(out_dir)
    ])
    assert ex.main() == 0
    assert any((out_dir / "raw" / "h2").glob("*.readings.json.gz"))

    monkeypatch.setattr("sys.argv", ["flatten.py", "--merge", "--completed-only", "--no-csv"])
    assert fl.main() == 0
    with gzip.open(out_dir / "readings.ndjson.gz", "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    assert [r["hiveId"] for r in rows] == ["h1"]
    assert rows[0]["hiveName"] == "Hive 1"

    # Run 2: rate limit clears; catchup finishes h2 and flatten publishes both hives
    rate_limit_h2["active"] = False
    monkeypatch.setattr("sys.argv", [
        "extract_all.py", "--catchup", "--start", "2026-01-01", "--end", "2026-01-02", "--out", str(out_dir)
    ])
    assert ex.main() == 0
    monkeypatch.setattr("sys.argv", ["flatten.py", "--merge", "--completed-only", "--no-csv"])
    assert fl.main() == 0
    with gzip.open(out_dir / "readings.ndjson.gz", "rt", encoding="utf-8") as fh:
        rows2 = [json.loads(line) for line in fh]
    assert sorted((r["hiveId"], r["hiveName"]) for r in rows2) == [("h1", "Hive 1"), ("h2", "Hive 2")]


def test_cron_sync_exit_code_precedence_and_manifest_guard(tmp_path):
    """Verify cron_sync.sh skips flatten when manifest.json is absent (preserving
    extract_rc) and logs both exit codes when both extract and flatten fail."""
    import os
    import shutil
    import stat
    import subprocess

    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    venv_bin = repo / ".venv" / "bin"
    fake_bin = tmp_path / "bin"
    scripts_dir.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    fake_bin.mkdir(parents=True)

    cron_src = Path(__file__).resolve().parent.parent / "scripts" / "cron_sync.sh"
    cron_dst = scripts_dir / "cron_sync.sh"
    shutil.copyfile(cron_src, cron_dst)
    cron_dst.chmod(cron_dst.stat().st_mode | stat.S_IEXEC)

    # Portable flock shim so the test runs identically on macOS and Linux
    flock_shim = fake_bin / "flock"
    flock_shim.write_text("#!/usr/bin/env bash\nexit 0\n")
    flock_shim.chmod(0o755)

    calls_log = tmp_path / "calls.log"
    py_shim = venv_bin / "python"
    py_shim.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{calls_log}"\n'
        'if [[ "$1" == "scripts/extract_all.py" ]]; then\n'
        '  if [[ "${MOCK_WRITE_MANIFEST:-0}" == "1" ]]; then\n'
        '    mkdir -p data/extract\n'
        '    echo \'{"completed":{}}\' > data/extract/manifest.json\n'
        '  fi\n'
        '  exit "${MOCK_EXTRACT_RC:-0}"\n'
        'fi\n'
        'exit "${MOCK_FLATTEN_RC:-0}"\n'
    )
    py_shim.chmod(0o755)

    base_env = {**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
    log_file = repo / "data" / "cron_sync.log"

    # 1) Extract fails before manifest.json exists -> flatten skipped, exits with extract_rc (2)
    res = subprocess.run(
        ["bash", str(cron_dst)],
        env={**base_env, "MOCK_EXTRACT_RC": "2", "MOCK_FLATTEN_RC": "1"},
        capture_output=True, text=True,
    )
    assert res.returncode == 2
    assert calls_log.read_text().splitlines() == ["scripts/extract_all.py --catchup"]
    assert "failed during extract" in log_file.read_text()

    # 2) Extract exits 0 before any manifest.json exists -> flatten skipped cleanly, exits 0
    calls_log.unlink()
    log_file.unlink()
    res = subprocess.run(
        ["bash", str(cron_dst)],
        env={**base_env, "MOCK_EXTRACT_RC": "0", "MOCK_FLATTEN_RC": "1"},
        capture_output=True, text=True,
    )
    assert res.returncode == 0
    assert calls_log.read_text().splitlines() == ["scripts/extract_all.py --catchup"]

    # 3) Partial extract fails (2) AFTER saving manifest, and flatten ALSO fails (1) -> logs both, exits 2
    calls_log.unlink()
    log_file.unlink()
    res = subprocess.run(
        ["bash", str(cron_dst)],
        env={**base_env, "MOCK_WRITE_MANIFEST": "1", "MOCK_EXTRACT_RC": "2", "MOCK_FLATTEN_RC": "1"},
        capture_output=True, text=True,
    )
    assert res.returncode == 2
    assert calls_log.read_text().splitlines() == [
        "scripts/extract_all.py --catchup",
        "scripts/flatten.py --merge --completed-only",
    ]
    log_text = log_file.read_text()
    assert "extract (exit 2)" in log_text and "flatten (exit 1)" in log_text

