"""Unit tests for the pure helpers extracted from the script `main()` functions.

These lock the behavior of the logic pulled out of `discover.main`,
`extract_all.main`, and `flatten.main` when their cognitive complexity was
reduced (SonarCloud python:S3776). They exercise the extracted helpers directly
— no API calls — so the refactor is provably behavior-preserving.
"""

from __future__ import annotations

import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import discover  # noqa: E402
import extract_all  # noqa: E402
import flatten  # noqa: E402
from bm.client import BroodMinderError  # noqa: E402


# ---------------------------------------------------------------------------
# discover.find_sample_ids
# ---------------------------------------------------------------------------
def test_find_sample_ids_from_list():
    apiaries = [
        {"hives": [
            {"hiveId": "H1", "devices": [{"deviceId": "D1"}, {"deviceId": "D2"}]},
            {"hiveId": "H2", "devices": [{"deviceId": "D3"}]},
        ]},
    ]
    assert discover.find_sample_ids(apiaries) == ("H1", "D1")


def test_find_sample_ids_dict_wrapper():
    apiaries = {"apiaries": [{"hives": [{"hiveId": "H1", "devices": [{"deviceId": "D1"}]}]}]}
    assert discover.find_sample_ids(apiaries) == ("H1", "D1")


def test_find_sample_ids_alternate_keys():
    # schemas vary: id / deviceID / positions instead of hiveId / deviceId / devices
    apiaries = [{"hives": [{"id": "H9", "positions": [{"deviceID": "D9"}]}]}]
    assert discover.find_sample_ids(apiaries) == ("H9", "D9")


def test_find_sample_ids_hive_without_devices():
    apiaries = [{"hives": [{"hiveId": "H1"}]}]
    assert discover.find_sample_ids(apiaries) == ("H1", None)


def test_find_sample_ids_scalar_payload_is_empty():
    assert discover.find_sample_ids("oops") == (None, None)
    assert discover.find_sample_ids(5) == (None, None)


def test_stream_existing_readings_skips_blank_lines(tmp_path):
    target = tmp_path / "readings.ndjson.gz"
    row = {"hiveId": "H1", "positionID": "p", "deviceId": "d", "timestamp": 1}
    with gzip.open(target, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n\n   \n")
    sink = MagicMock()
    seen = defaultdict(set)
    flatten._stream_existing_readings(sink, target, seen)
    assert sink.emit.call_count == 1
    assert ("p", "d", 1) in seen["H1"]


def test_find_sample_ids_empty():
    assert discover.find_sample_ids([]) == (None, None)
    assert discover.find_sample_ids({"apiaries": []}) == (None, None)
    assert discover.find_sample_ids(None) == (None, None)


# ---------------------------------------------------------------------------
# extract_all.select_apiaries
# ---------------------------------------------------------------------------
_APIARIES = [
    {"apiaryId": "A1", "name": "North Yard", "hives": []},
    {"apiaryId": "A2", "name": "South Yard", "hives": []},
]


def test_select_apiaries_no_filter_returns_all():
    assert extract_all.select_apiaries(_APIARIES, []) == _APIARIES


def test_select_apiaries_by_name_case_insensitive():
    got = extract_all.select_apiaries(_APIARIES, ["north yard"])
    assert [a["apiaryId"] for a in got] == ["A1"]


def test_select_apiaries_by_id_exact():
    got = extract_all.select_apiaries(_APIARIES, ["A2"])
    assert [a["apiaryId"] for a in got] == ["A2"]


def test_select_apiaries_no_match_is_empty():
    assert extract_all.select_apiaries(_APIARIES, ["nope"]) == []


def test_select_apiaries_normalizes_wrapped_dict():
    wrapped = {"apiaries": _APIARIES}
    assert extract_all.select_apiaries(wrapped, []) == _APIARIES
    got = extract_all.select_apiaries(wrapped, ["north yard"])
    assert [a["apiaryId"] for a in got] == ["A1"]


# ---------------------------------------------------------------------------
# flatten.build_row
# ---------------------------------------------------------------------------
def test_build_row_full():
    meta = {"apiaryId": "A1", "apiaryName": "North", "hiveName": "Hive-1"}
    reading = {
        "deviceId": "D1",
        "timestamp": 1_700_000_000,
        "batteryLevel": 90,
        "chargeRemaining": 12,
        "readings": {"temperature": 34.5, "humidity": 55},
    }
    row = flatten.build_row("H1", meta, "P1", reading)
    assert row["apiaryId"] == "A1"
    assert row["apiaryName"] == "North"
    assert row["hiveId"] == "H1"
    assert row["hiveName"] == "Hive-1"
    assert row["positionID"] == "P1"
    assert row["deviceId"] == "D1"
    assert row["timestamp"] == 1_700_000_000
    assert row["datetime"] == "2023-11-14T22:13:20+00:00"
    assert row["batteryLevel"] == 90
    assert row["chargeRemaining"] == 12
    assert row["m_temperature"] == 34.5
    assert row["m_humidity"] == 55


def test_build_row_null_timestamp_gives_null_datetime():
    row = flatten.build_row("H1", {}, "P1", {"deviceId": "D1", "timestamp": None})
    assert row["timestamp"] is None
    assert row["datetime"] is None


def test_build_row_zero_timestamp_gives_epoch_datetime():
    row = flatten.build_row("H1", {}, "P1", {"deviceId": "D1", "timestamp": 0})
    assert row["timestamp"] == 0
    assert row["datetime"] == "1970-01-01T00:00:00+00:00"


def test_build_row_missing_metrics_has_no_metric_columns():
    row = flatten.build_row("H1", {}, "P1", {"deviceId": "D1", "timestamp": 1})
    assert not any(k.startswith("m_") for k in row)


# ---------------------------------------------------------------------------
# flatten.accumulate_coverage
# ---------------------------------------------------------------------------
def test_accumulate_coverage_zero_timestamp():
    from collections import defaultdict
    coverage = defaultdict(lambda: {"rows": 0, "min_ts": None, "max_ts": None,
                                    "devices": set(), "positions": set()})
    row = {"hiveId": "H1", "deviceId": "D1", "positionID": "P1", "timestamp": 0}
    flatten.accumulate_coverage(coverage, row)
    assert coverage["H1"]["min_ts"] == 0
    assert coverage["H1"]["max_ts"] == 0
    assert coverage["H1"]["rows"] == 1


# ---------------------------------------------------------------------------
# flatten.build_coverage
# ---------------------------------------------------------------------------
def test_build_coverage_zero_timestamp():
    coverage = {
        "H1": {"rows": 1, "min_ts": 0, "max_ts": 0, "devices": {"D1"}, "positions": {"P1"}}
    }
    meta = {"H1": {"apiaryName": "Apiary1", "hiveName": "Hive1"}}
    out = flatten.build_coverage(coverage, meta)
    assert out["H1"]["earliest"] == "1970-01-01T00:00:00+00:00"
    assert out["H1"]["latest"] == "1970-01-01T00:00:00+00:00"


def test_build_coverage_null_timestamps():
    coverage = {
        "H1": {"rows": 0, "min_ts": None, "max_ts": None, "devices": set(), "positions": set()}
    }
    out = flatten.build_coverage(coverage, {})
    assert out["H1"]["earliest"] is None
    assert out["H1"]["latest"] is None


# ---------------------------------------------------------------------------
# flatten.load_json
# ---------------------------------------------------------------------------
def test_load_json_plain(tmp_path):
    f = tmp_path / "data.json"
    f.write_text('{"key": "val"}', encoding="utf-8")
    assert flatten.load_json(f) == {"key": "val"}


def test_load_json_gz(tmp_path):
    f = tmp_path / "data.json.gz"
    with gzip.open(f, "wt", encoding="utf-8") as fh:
        fh.write('{"key": "val"}')
    assert flatten.load_json(f) == {"key": "val"}


# ---------------------------------------------------------------------------
# flatten.hive_meta
# ---------------------------------------------------------------------------
def test_hive_meta_extracts_fields():
    manifest = {"completed": {
        "H1|0|1": {"apiaryName": "North", "apiaryId": "A1", "hiveName": "Hive-1"},
        "H1|1|2": {"apiaryName": "North", "apiaryId": "A1", "hiveName": "Hive-1"},
        "H2|0|1": {"apiaryName": "South", "apiaryId": "A2", "hiveName": "Hive-2"},
    }}
    meta = flatten.hive_meta(manifest)
    assert meta["H1"]["apiaryName"] == "North"
    assert meta["H2"]["hiveName"] == "Hive-2"
    assert len(meta) == 2


def test_hive_meta_empty():
    assert flatten.hive_meta({}) == {}


# ---------------------------------------------------------------------------
# flatten.iter_reading_files / iter_note_files
# ---------------------------------------------------------------------------
def test_iter_reading_files(tmp_path):
    (tmp_path / "a.readings.json").write_text("[]", encoding="utf-8")
    (tmp_path / "b.readings.json.gz").write_bytes(b"")
    (tmp_path / "other.json").write_text("", encoding="utf-8")
    names = [f.name for f in flatten.iter_reading_files(tmp_path)]
    assert "a.readings.json" in names
    assert "b.readings.json.gz" in names
    assert "other.json" not in names


def test_iter_note_files(tmp_path):
    (tmp_path / "a.notes.json").write_text("[]", encoding="utf-8")
    (tmp_path / "b.notes.json.gz").write_bytes(b"")
    (tmp_path / "other.json").write_text("", encoding="utf-8")
    names = [f.name for f in flatten.iter_note_files(tmp_path)]
    assert "a.notes.json" in names
    assert "b.notes.json.gz" in names
    assert "other.json" not in names


# ---------------------------------------------------------------------------
# flatten.iter_readings
# ---------------------------------------------------------------------------
def test_iter_readings(tmp_path):
    data = [{"positionID": "P1", "readings": [{"deviceId": "D1", "timestamp": 1}]}]
    f = tmp_path / "w.readings.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    pairs = list(flatten.iter_readings(tmp_path))
    assert pairs == [("P1", {"deviceId": "D1", "timestamp": 1})]


# ---------------------------------------------------------------------------
# flatten.discover_metric_keys
# ---------------------------------------------------------------------------
def test_discover_metric_keys(tmp_path):
    (tmp_path / "skip.txt").write_text("not a dir", encoding="utf-8")  # exercises continue branch
    hdir = tmp_path / "H1"
    hdir.mkdir()
    data = [{"positionID": "P1", "readings": [{"readings": {"temp": 35, "hum": 60}}]}]
    (hdir / "w.readings.json").write_text(json.dumps(data), encoding="utf-8")
    keys = flatten.discover_metric_keys(tmp_path)
    assert keys == {"temp", "hum"}


# ---------------------------------------------------------------------------
# flatten.iter_hive_rows
# ---------------------------------------------------------------------------
def test_iter_hive_rows_deduplicates(tmp_path):
    hdir = tmp_path / "H1"
    hdir.mkdir()
    row = {"positionID": "P1", "readings": [
        {"deviceId": "D1", "timestamp": 1},
        {"deviceId": "D1", "timestamp": 1},  # duplicate
    ]}
    (hdir / "w.readings.json").write_text(json.dumps([row]), encoding="utf-8")
    rows = list(flatten.iter_hive_rows(hdir, {}))
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# flatten.write_notes
# ---------------------------------------------------------------------------
def test_write_notes(tmp_path):
    hdir = tmp_path / "H1"
    hdir.mkdir()
    notes_data = [{"text": "a note", "date": "2024-01-01"}]
    (hdir / "w.notes.json").write_text(json.dumps(notes_data), encoding="utf-8")
    out_file = tmp_path / "notes.ndjson"
    n = flatten.write_notes(out_file, {"H1": {"hiveName": "Hive-1"}}, raw=tmp_path)
    assert n == 1
    line = json.loads(out_file.read_text(encoding="utf-8").strip())
    assert line["hiveId"] == "H1"
    assert line["text"] == "a note"


# ---------------------------------------------------------------------------
# extract_all.count_reading_rows / count_notes
# ---------------------------------------------------------------------------
def test_count_reading_rows_list():
    payload = [{"readings": [1, 2, 3]}, {"readings": [4]}]
    assert extract_all.count_reading_rows(payload) == 4


def test_count_reading_rows_not_list():
    assert extract_all.count_reading_rows(None) == 0
    assert extract_all.count_reading_rows({}) == 0


def test_count_notes_list():
    assert extract_all.count_notes([1, 2, 3]) == 3


def test_count_notes_dict():
    assert extract_all.count_notes({"notes": [1, 2]}) == 2


def test_count_notes_other():
    assert extract_all.count_notes(None) == 0


# ---------------------------------------------------------------------------
# extract_all.parse_date
# ---------------------------------------------------------------------------
def test_parse_date():
    assert extract_all.parse_date("2022-01-01") == 1640995200


# ---------------------------------------------------------------------------
# extract_all.load_manifest
# ---------------------------------------------------------------------------
def test_load_manifest_existing(tmp_path):
    data = {"completed": {"k": "v"}, "meta": {}}
    (tmp_path / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    assert extract_all.load_manifest(tmp_path / "manifest.json") == data


def test_load_manifest_missing(tmp_path):
    result = extract_all.load_manifest(tmp_path / "no_manifest.json")
    assert result == {"completed": {}, "meta": {}}


# ---------------------------------------------------------------------------
# extract_all.write_gz
# ---------------------------------------------------------------------------
def test_write_gz_roundtrip(tmp_path):
    obj = {"a": [1, 2, 3]}
    path = tmp_path / "out.json.gz"
    extract_all.write_gz(path, obj)
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        assert json.load(fh) == obj


# ---------------------------------------------------------------------------
# extract_all._empty_run
# ---------------------------------------------------------------------------
def test_empty_run_off():
    count, stop = extract_all._empty_run(5, 1, 0)
    assert count == 5
    assert stop is False


def test_empty_run_increment():
    count, stop = extract_all._empty_run(1, 0, 3)
    assert count == 2
    assert stop is False


def test_empty_run_reaches_limit():
    count, stop = extract_all._empty_run(2, 0, 3)
    assert count == 3
    assert stop is True


def test_empty_run_reset_on_data():
    count, stop = extract_all._empty_run(5, 1, 3)
    assert count == 0
    assert stop is False


# ---------------------------------------------------------------------------
# extract_all._log_window
# ---------------------------------------------------------------------------
def test_log_window_prints_when_data(capsys):
    a = {"name": "Yard"}
    h = {"name": "Hive-1"}
    rec = {"reading_rows": 5, "notes": 2}
    extract_all._log_window(a, h, 0, 86400, rec)
    out = capsys.readouterr().out
    assert "Hive-1" in out
    assert "rows=5" in out


def test_log_window_silent_when_empty(capsys):
    a = {"name": "Yard"}
    h = {"name": "Hive-1"}
    rec = {"reading_rows": 0}
    extract_all._log_window(a, h, 0, 86400, rec)
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# discover._sample
# ---------------------------------------------------------------------------
def test_sample_success(capsys):
    out = {}
    discover._sample(out, "label", lambda: {"x": 1}, "ok", "err", "msg", 100)
    assert out["ok"] == {"x": 1}
    assert "err" not in out


def test_sample_error(capsys):
    out = {}

    def bad_fetch():
        raise BroodMinderError(404, "GET", "/x", "not found")

    discover._sample(out, "label", bad_fetch, "ok", "err", "msg", 100)
    assert "ok" not in out
    assert "err" in out
    assert "not found" in out["err"]


# ---------------------------------------------------------------------------
# extract_all.fetch_window
# ---------------------------------------------------------------------------
def _make_args(no_notes=False):
    args = MagicMock()
    args.no_notes = no_notes
    return args


def test_fetch_window_with_notes(tmp_path):
    bm = MagicMock()
    bm.hive_readings.return_value = [{"readings": [1, 2]}]
    bm.hive_notes.return_value = [{"note": "hi"}]
    a = {"apiaryId": "A1", "name": "Yard"}
    h = {"name": "Hive-1"}
    rec = extract_all.fetch_window(bm, a, h, "H1", 0, 86400, tmp_path, _make_args())
    assert rec["reading_rows"] == 2
    assert rec["notes"] == 1
    assert (tmp_path / "0-86400.readings.json.gz").exists()
    assert (tmp_path / "0-86400.notes.json.gz").exists()


def test_fetch_window_no_notes(tmp_path):
    bm = MagicMock()
    bm.hive_readings.return_value = []
    a = {"apiaryId": "A1", "name": "Yard"}
    h = {"name": "Hive-1"}
    rec = extract_all.fetch_window(bm, a, h, "H1", 0, 86400, tmp_path, _make_args(no_notes=True))
    assert rec["reading_rows"] == 0
    assert "notes" not in rec


# ---------------------------------------------------------------------------
# extract_all.process_hive
# ---------------------------------------------------------------------------
def test_process_hive_skips_completed(tmp_path):
    bm = MagicMock()
    bm.call_count = 0
    a = {"apiaryId": "A1", "name": "Yard"}
    h = {"hiveId": "H1", "name": "Hive-1"}
    wins = [(0, 86400)]
    completed = {"H1|0|86400": {"reading_rows": 5}}
    args = MagicMock()
    args.stop_after_empty = 0
    args.max_calls = 900
    extract_all.process_hive(bm, a, h, wins, args, tmp_path, completed, lambda: None)
    bm.hive_readings.assert_not_called()


def test_process_hive_stops_at_budget(tmp_path):
    bm = MagicMock()
    bm.call_count = 900
    bm.hive_readings.return_value = []
    a = {"apiaryId": "A1", "name": "Yard"}
    h = {"hiveId": "H1", "name": "Hive-1"}
    wins = [(0, 86400)]
    completed = {}
    args = MagicMock()
    args.stop_after_empty = 0
    args.max_calls = 900
    args.no_notes = True
    with pytest.raises(extract_all._BudgetExhausted):
        extract_all.process_hive(bm, a, h, wins, args, tmp_path, completed, lambda: None)


def test_process_hive_early_exit_completed_stop(tmp_path):
    bm = MagicMock()
    bm.call_count = 0
    a = {"apiaryId": "A1", "name": "Yard"}
    h = {"hiveId": "H1", "name": "Hive-1"}
    # Two windows both completed with 0 rows; stop_after_empty=1 triggers break
    wins = [(0, 86400), (86400, 172800)]
    completed = {"H1|0|86400": {"reading_rows": 0}, "H1|86400|172800": {"reading_rows": 0}}
    args = MagicMock()
    args.stop_after_empty = 1
    args.max_calls = 900
    # Should NOT raise; just returns early after the break
    extract_all.process_hive(bm, a, h, wins, args, tmp_path, completed, lambda: None)
    bm.hive_readings.assert_not_called()


def test_process_hive_fetches_and_records(tmp_path):
    bm = MagicMock()
    bm.call_count = 0
    bm.hive_readings.return_value = [{"readings": [1, 2]}]
    bm.hive_notes.return_value = []
    a = {"apiaryId": "A1", "name": "Yard"}
    h = {"hiveId": "H1", "name": "Hive-1"}
    wins = [(0, 86400)]
    completed = {}
    args = MagicMock()
    args.stop_after_empty = 0
    args.max_calls = 900
    args.no_notes = False
    extract_all.process_hive(bm, a, h, wins, args, tmp_path, completed, lambda: None)
    assert "H1|0|86400" in completed
    assert completed["H1|0|86400"]["reading_rows"] == 2


def test_process_hive_calls_save_manifest_every_25(tmp_path):
    bm = MagicMock()
    bm.call_count = 0
    bm.hive_readings.return_value = []
    a = {"apiaryId": "A1", "name": "Yard"}
    h = {"hiveId": "H1", "name": "Hive-1"}
    wins = [(i, i + 1) for i in range(1)]
    # Pre-fill completed with 24 entries so adding one more triggers the % 25 == 0 branch
    completed = {f"OTHER|{i}|{i+1}": {"reading_rows": 1} for i in range(24)}
    args = MagicMock()
    args.stop_after_empty = 0
    args.max_calls = 900
    args.no_notes = True
    save_calls = []
    extract_all.process_hive(bm, a, h, wins, args, tmp_path, completed, lambda: save_calls.append(1))
    assert len(save_calls) == 1  # triggered at index 25


def test_process_hive_stop_after_empty_fetch(tmp_path):
    bm = MagicMock()
    bm.call_count = 0
    bm.hive_readings.return_value = []  # 0 rows => empty
    a = {"apiaryId": "A1", "name": "Yard"}
    h = {"hiveId": "H1", "name": "Hive-1"}
    wins = [(0, 86400), (86400, 172800)]
    completed = {}
    args = MagicMock()
    args.stop_after_empty = 1
    args.max_calls = 900
    args.no_notes = True
    extract_all.process_hive(bm, a, h, wins, args, tmp_path, completed, lambda: None)
    # After the first fetch with 0 rows, empties=1 >= limit=1 → break; second window never fetched
    assert bm.hive_readings.call_count == 1


# ---------------------------------------------------------------------------
# S3776 helper extractions (cognitive-complexity refactor, #192)
# ---------------------------------------------------------------------------
def test_iter_hives_normalizes_payload_shapes():
    hive = {"hiveId": "H1"}
    assert list(discover._iter_hives([{"hives": [hive]}])) == [hive]
    assert list(discover._iter_hives({"apiaries": [{"hives": [hive]}]})) == [hive]
    assert list(discover._iter_hives([{"name": "no hives"}])) == []
    assert list(discover._iter_hives(None)) == []


def test_first_device_id_skips_devices_without_id():
    hv = {"devices": [{"name": "unlabelled"}, {"id": "D2"}, {"deviceId": "D3"}]}
    assert discover._first_device_id(hv) == "D2"
    assert discover._first_device_id({"hiveId": "H1"}) is None


def test_find_sample_ids_device_from_later_hive():
    # hive_id comes from the first hive; device_id from the first hive that has one
    apiaries = [{"hives": [{"hiveId": "H1"}, {"hiveId": "H2", "devices": [{"deviceId": "D2"}]}]}]
    assert discover.find_sample_ids(apiaries) == ("H1", "D2")


def test_note_key_prefers_id_then_created_description():
    assert flatten._note_key("H1", {"id": "N1", "created": 1}) == ("H1", "N1")
    assert flatten._note_key("H1", {"created": 1, "description": "d"}) == ("H1", (1, "d", None))
    assert flatten._note_key("H1", {"created": 1, "description": "d", "text": "content"}) == ("H1", (1, "d", "content"))


def test_write_notes_merge_keeps_existing_and_dedupes(tmp_path):
    existing = tmp_path / "notes.ndjson"
    existing.write_text(json.dumps({"hiveId": "H1", "id": "N1"}) + "\n\n")
    raw = tmp_path / "raw"
    (raw / "H1").mkdir(parents=True)
    (raw / "H1" / "0-1.notes.json").write_text(json.dumps([{"id": "N1"}, {"id": "N2"}]))
    out = tmp_path / "out.ndjson"
    n = flatten.write_notes(out, {"H1": {"hiveName": "Hive"}}, raw=raw, merge=True, existing_target=existing)
    assert n == 2
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["id"] for r in rows] == ["N1", "N2"]
    assert rows[1]["hiveName"] == "Hive"


def test_has_content(tmp_path):
    p = tmp_path / "f"
    assert not flatten._has_content(p)
    p.write_text("")
    assert not flatten._has_content(p)
    p.write_text("x")
    assert flatten._has_content(p)


def test_existing_metric_keys(tmp_path):
    p = tmp_path / "readings.ndjson.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"hiveId": "H1", "m_weight": 1, "m_": 2}) + "\n\n")
        fh.write(json.dumps({"m_lux": 3}) + "\n")
    assert flatten._existing_metric_keys(p) == {"weight", "lux"}


def test_resolve_end_explicit_and_snapped(monkeypatch):
    args = MagicMock(end="2024-01-02")
    assert extract_all._resolve_end(args) == extract_all.parse_date("2024-01-02")
    monkeypatch.setattr(extract_all, "now_epoch", lambda: 86400 * 10 + 1234)
    assert extract_all._resolve_end(MagicMock(end=None)) == 86400 * 10


def test_all_caught_up():
    manifest = {"completed": {"H1|0|100": {}, "H2|0|50": {}}}
    hives = [({}, {"hiveId": "H1"}), ({}, {"hiveId": "H2"})]
    assert extract_all._all_caught_up(manifest, hives, 0, 50)
    assert not extract_all._all_caught_up(manifest, hives, 0, 100)
    assert not extract_all._all_caught_up(manifest, [], 0, 50)


def test_hive_meta_preserves_pipe_in_hive_id():
    manifest = {"completed": {
        "hive|with|pipe|1000|4200": {"apiaryName": "N", "apiaryId": "A", "hiveName": "H"},
    }}
    meta = flatten.hive_meta(manifest)
    assert list(meta) == ["hive|with|pipe"]


def test_flatten_completed_only_requires_manifest(tmp_path, monkeypatch, capsys):
    out = tmp_path / "extract"
    (out / "raw").mkdir(parents=True)
    existing = out / "notes.ndjson"
    existing.write_text('{"hiveId": "h1"}\n')
    monkeypatch.setattr(flatten, "OUT", out)
    monkeypatch.setattr(flatten, "RAW", out / "raw")
    monkeypatch.setattr(sys, "argv", ["flatten.py", "--completed-only"])
    assert flatten.main() == 1
    assert "manifest.json" in capsys.readouterr().err
    assert existing.read_text() == '{"hiveId": "h1"}\n'
    assert not (out / "readings.ndjson.gz").exists()


def test_manifest_helpers_skip_malformed_keys_consistently():
    manifest = {
        "completed": {
            "malformed_key": {"apiaryName": "Bad", "apiaryId": "B1", "hiveName": "Bad1"},
            "two|parts": {"apiaryName": "Bad", "apiaryId": "B2", "hiveName": "Bad2"},
            "|10|20": {"apiaryName": "Bad", "apiaryId": "B3", "hiveName": "EmptyHive"},
            "h1|bad|20": {"apiaryName": "Bad", "apiaryId": "B4", "hiveName": "BadTs"},
            "h1|20|20": {"apiaryName": "Bad", "apiaryId": "B5", "hiveName": "ZeroLen"},
            "h1|30|10": {"apiaryName": "Bad", "apiaryId": "B6", "hiveName": "Inverted"},
            "h1|10|20": {"reading_rows": 5},
            "h1|15|25": {"reading_rows": 3},
            "h1|25|30": {"apiaryName": "Yard", "apiaryId": "A1", "hiveName": "Hive-1"},
            "h1|50|60": {"reading_rows": 2},
        }
    }
    meta = flatten.hive_meta(manifest)
    assert list(meta) == ["h1"]
    assert meta["h1"] == {"apiaryName": "Yard", "apiaryId": "A1", "hiveName": "Hive-1"}
    assert flatten.hive_windows(manifest) == {"h1": {"10-20", "15-25", "25-30", "50-60"}}
    assert flatten.hive_intervals(manifest) == {"h1": [(10, 30), (50, 60)]}
    spans = flatten.hive_intervals(manifest)["h1"]
    assert flatten._ts_in_intervals(10, spans)
    assert flatten._ts_in_intervals(22, spans)
    assert not flatten._ts_in_intervals(30, spans)
    assert not flatten._ts_in_intervals(40, spans)
    assert flatten._ts_in_intervals(55, spans)
    assert not flatten._ts_in_intervals(float("nan"), spans)
    assert not flatten._ts_in_intervals(15, [])


def test_flatten_completed_only_handles_gz_notes(tmp_path, monkeypatch):
    out = tmp_path / "extract"
    hdir = out / "raw" / "h1"
    hdir.mkdir(parents=True)
    with gzip.open(hdir / "10-20.readings.json.gz", "wt", encoding="utf-8") as fh:
        json.dump([{"positionID": "p", "readings": [
            {"deviceId": "d", "timestamp": 15, "readings": {"temperature": 80.0}}]}], fh)
    with gzip.open(hdir / "10-20.notes.json.gz", "wt", encoding="utf-8") as fh:
        json.dump({"notes": [{"id": "n-gz", "timestamp": 15, "description": "ok"}]}, fh)
    # Truncated unmanifested .notes.json.gz must be ignored under --completed-only
    (hdir / "20-30.notes.json.gz").write_bytes(b"\x1f\x8bcorrupt")
    (out / "manifest.json").write_text(json.dumps({
        "completed": {"h1|10|20": {"apiaryName": "Yard", "apiaryId": "A1", "hiveName": "H1"}}
    }))
    monkeypatch.setattr(flatten, "OUT", out)
    monkeypatch.setattr(flatten, "RAW", out / "raw")
    monkeypatch.setattr(sys, "argv", ["flatten.py", "--merge", "--completed-only", "--no-csv"])
    assert flatten.main() == 0
    notes = [json.loads(line) for line in (out / "notes.ndjson").read_text().splitlines()]
    assert notes == [{"hiveId": "h1", "hiveName": "H1", "id": "n-gz", "timestamp": 15, "description": "ok"}]


def test_flatten_merge_completed_only_without_raw_dir_and_non_numeric_ts(tmp_path, monkeypatch):
    out = tmp_path / "extract"
    out.mkdir(parents=True)
    with gzip.open(out / "readings.ndjson.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "hiveId": "h1", "positionID": "p1", "deviceId": "d1",
            "timestamp": 15, "m_temperature": 80.0,
        }) + "\n")
        fh.write(json.dumps({
            "hiveId": "h1", "positionID": "p1", "deviceId": "d1",
            "timestamp": True, "m_temperature": 81.0,
        }) + "\n")
        fh.write(json.dumps({
            "hiveId": "unknown", "positionID": "p1", "deviceId": "d1",
            "timestamp": 15, "m_temperature": 82.0,
        }) + "\n")
    (out / "notes.ndjson").write_text(
        json.dumps({"hiveId": "h1", "id": "n1", "timestamp": 15}) + "\n"
        + json.dumps({"hiveId": "h1", "id": "n2", "timestamp": "bad"}) + "\n"
    )
    (out / "manifest.json").write_text(json.dumps({
        "completed": {"h1|10|20": {"apiaryName": "Yard", "apiaryId": "A1", "hiveName": "H1"}}
    }))
    monkeypatch.setattr(flatten, "OUT", out)
    monkeypatch.setattr(flatten, "RAW", out / "raw")  # does not exist (purged)
    monkeypatch.setattr(sys, "argv", ["flatten.py", "--merge", "--completed-only", "--no-csv"])
    assert flatten.main() == 0
    with gzip.open(out / "readings.ndjson.gz", "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    assert len(rows) == 1 and rows[0]["hiveName"] == "H1"
    notes = [json.loads(line) for line in (out / "notes.ndjson").read_text().splitlines()]
    assert [n["id"] for n in notes] == ["n1"]
    assert notes[0]["hiveName"] == "H1"


def test_flatten_merge_completed_only_preserves_purged_timestamped_note_and_filters_created_only(
    tmp_path, monkeypatch
):
    out = tmp_path / "extract"
    hdir = out / "raw" / "h1"
    hdir.mkdir(parents=True)
    # Completed 10-20 raw files were purged; corrupt UTF-8 in unmanifested raw file must not abort
    (hdir / "25-35.notes.json").write_bytes(b"\xff\xfeinvalid-utf8")
    # Overlapping unmanifested 10-30 raw notes remain on disk after interrupted run
    (hdir / "10-30.notes.json").write_text(json.dumps({
        "notes": [
            {"id": "n-completed", "timestamp": 15, "description": "keep me"},
            {"id": "n-incomplete", "timestamp": 25, "description": "drop me"},
            {"id": "n-notime-unmanifested", "description": "drop timeless unmanifested"},
        ]
    }))
    (out / "notes.ndjson").write_text(
        json.dumps({"hiveId": "h1", "id": "n-completed", "timestamp": 15}) + "\n"
        + json.dumps({"hiveId": "h1", "id": "n-incomplete", "timestamp": 25}) + "\n"
        + json.dumps({"hiveId": "h1", "id": "n-created-ok", "created": 18}) + "\n"
        + json.dumps({"hiveId": "h1", "id": "n-created-outside", "created": 28}) + "\n"
        + json.dumps({"hiveId": "h1", "id": "n-notime-unmanifested", "description": "drop timeless unmanifested"}) + "\n"
        + json.dumps({"hiveId": "h1", "id": "n-notime-purged", "description": "keep timeless archived"}) + "\n"
    )
    (out / "manifest.json").write_text(json.dumps({
        "completed": {"h1|10|20": {"apiaryName": "Yard", "apiaryId": "A1", "hiveName": "H1"}}
    }))
    monkeypatch.setattr(flatten, "OUT", out)
    monkeypatch.setattr(flatten, "RAW", out / "raw")
    monkeypatch.setattr(sys, "argv", ["flatten.py", "--merge", "--completed-only", "--no-csv"])
    assert flatten.main() == 0
    notes = [json.loads(line) for line in (out / "notes.ndjson").read_text().splitlines()]
    assert [n["id"] for n in notes] == ["n-completed", "n-created-ok", "n-notime-purged"]
    assert all(n["hiveName"] == "H1" for n in notes)


def test_flatten_merge_completed_only_preserves_non_clobbering_bak_and_logs_pruned_counts(
    tmp_path, monkeypatch, capsys
):
    out = tmp_path / "extract"
    out.mkdir(parents=True)
    orig_rows = [
        {"hiveId": "h1", "positionID": "p1", "deviceId": "d1", "timestamp": 15, "m_temperature": 80.0},
        {"hiveId": "h1", "positionID": "p1", "deviceId": "d1", "timestamp": 25, "m_temperature": 81.0},
        {"hiveId": "h2", "positionID": "p1", "deviceId": "d2", "timestamp": 15, "m_temperature": 82.0},
    ]
    with gzip.open(out / "readings.ndjson.gz", "wt", encoding="utf-8") as fh:
        for r in orig_rows:
            fh.write(json.dumps(r) + "\n")
    with gzip.open(out / "readings.csv.gz", "wt", encoding="utf-8") as fh:
        fh.write("legacy-csv-sentinel\n")
    orig_notes_text = (
        json.dumps({"hiveId": "h1", "id": "n-keep", "timestamp": 15}) + "\n"
        + json.dumps({"hiveId": "h1", "id": "n-drop", "timestamp": 25}) + "\n"
    )
    (out / "notes.ndjson").write_text(orig_notes_text)
    (out / "coverage.json").write_text('{"legacy": true}\n')
    (out / "manifest.json").write_text(json.dumps({
        "completed": {"h1|10|20": {"apiaryName": "Yard", "apiaryId": "A1", "hiveName": "H1"}}
    }))

    monkeypatch.setattr(flatten, "OUT", out)
    monkeypatch.setattr(flatten, "RAW", out / "raw")
    monkeypatch.setattr(sys, "argv", ["flatten.py", "--merge", "--completed-only"])
    assert flatten.main() == 0
    stdout1 = capsys.readouterr().out
    assert "pruned archive: 2 readings, 1 notes (.bak preserved)" in stdout1

    with gzip.open(out / "readings.ndjson.gz.bak", "rt", encoding="utf-8") as fh:
        bak_rows = [json.loads(line) for line in fh]
    assert bak_rows == orig_rows
    with gzip.open(out / "readings.csv.gz.bak", "rt", encoding="utf-8") as fh:
        assert fh.read() == "legacy-csv-sentinel\n"
    assert (out / "notes.ndjson.bak").read_text() == orig_notes_text
    assert (out / "coverage.json.bak").read_text() == '{"legacy": true}\n'

    # Subsequent prune must NOT overwrite existing .bak files; steady-state run logs no prune
    assert flatten.main() == 0
    stdout2 = capsys.readouterr().out
    assert "pruned archive:" not in stdout2
    with gzip.open(out / "readings.ndjson.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(orig_rows[0]) + "\n")
        fh.write(json.dumps(orig_rows[1]) + "\n")
    assert flatten.main() == 0
    with gzip.open(out / "readings.ndjson.gz.bak", "rt", encoding="utf-8") as fh:
        assert [json.loads(line) for line in fh] == orig_rows
    assert (out / "coverage.json.bak").read_text() == '{"legacy": true}\n'


def test_flatten_completed_only_filters_raw_rows_and_notes_by_interval(tmp_path, monkeypatch):
    out = tmp_path / "extract"
    hdir = out / "raw" / "h1"
    hdir.mkdir(parents=True)
    (hdir / "10-20.readings.json").write_text(json.dumps([{"positionID": "p", "readings": [
        {"deviceId": "d", "timestamp": 15, "readings": {"temperature": 80.0}},
        {"deviceId": "d", "timestamp": 20, "readings": {"temperature": 81.0}},  # == end, excluded
    ]}]))
    (hdir / "10-20.notes.json").write_text(json.dumps({"notes": [
        {"id": "in", "created": 12},
        {"id": "before-start", "created": 5},
        {"id": "bad-ts", "timestamp": "x"},
    ]}))
    (out / "manifest.json").write_text(json.dumps({
        "completed": {"h1|10|20": {"apiaryName": "Yard", "apiaryId": "A1", "hiveName": "H1"}}
    }))
    monkeypatch.setattr(flatten, "OUT", out)
    monkeypatch.setattr(flatten, "RAW", out / "raw")
    monkeypatch.setattr(sys, "argv", ["flatten.py", "--merge", "--completed-only", "--no-csv"])
    assert flatten.main() == 0
    with gzip.open(out / "readings.ndjson.gz", "rt", encoding="utf-8") as fh:
        assert [json.loads(line)["timestamp"] for line in fh] == [15]
    notes = [json.loads(line) for line in (out / "notes.ndjson").read_text().splitlines()]
    assert [n["id"] for n in notes] == ["in"]


def test_preserve_prune_backup_keeps_each_prune_under_unique_name(tmp_path):
    target = tmp_path / "notes.ndjson"
    target.write_text("v1\n")
    flatten._preserve_prune_backup(target, 1)
    target.write_text("v2\n")
    flatten._preserve_prune_backup(target, 1)
    assert (tmp_path / "notes.ndjson.bak").read_text() == "v1\n"
    extra = [p for p in tmp_path.glob("notes.ndjson.bak.*")]
    assert len(extra) == 1 and extra[0].read_text() == "v2\n"
