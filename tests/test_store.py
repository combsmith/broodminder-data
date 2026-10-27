"""Offline unit tests for bm/store.py (issue #220)."""

from __future__ import annotations

import gzip
import json

from bm.client import RateLimited
from bm.store import LocalStore

BASE = 1_700_000_000
DAY = 86400


class FakeClient:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
        self.call_count = 0

    def hive_readings(self, hive_id, start, end):
        self.calls += 1
        self.call_count += 1
        if self.fail:
            raise RateLimited(429, "GET", "http://x", "daily limit exceeded")
        return [{
            "positionID": "p1",
            "readings": [{
                "deviceId": "d1", "timestamp": start + 10,
                "batteryLevel": 90, "chargeRemaining": None,
                "readings": {"temperature": 95.0},
            }],
        }]

    def hive_notes(self, hive_id, start, end, raw=True):
        self.calls += 1
        self.call_count += 1
        return {"notes": [{"id": "n1", "description": "inspected"}]}


def seed_store(tmp_path, hive="h1", covered_end=BASE + DAY):
    store = LocalStore(tmp_path)
    manifest = {
        "completed": {
            f"{hive}|{BASE}|{covered_end}": {"reading_rows": 1, "notes": 0,
                                             "apiaryName": "A1", "hiveName": "Hive One"}
        },
        "meta": {},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    hdir = tmp_path / "raw" / hive
    hdir.mkdir(parents=True)
    payload = [{
        "positionID": "p1",
        "readings": [{
            "deviceId": "d1", "timestamp": BASE + 10,
            "batteryLevel": 90, "chargeRemaining": None,
            "readings": {"temperature": 95.0},
        }],
    }]
    with gzip.open(hdir / f"{BASE}-{covered_end}.readings.json.gz", "wt") as fh:
        json.dump(payload, fh)
    return store


def test_coverage_and_iter_readings_from_raw(tmp_path):
    store = seed_store(tmp_path)
    cov = store.coverage()
    assert cov["h1"]["covered_end"] == BASE + DAY
    assert cov["h1"]["covered_start"] == BASE
    rows = list(store.iter_readings("h1", BASE, BASE + DAY))
    assert len(rows) == 1
    assert rows[0]["m_temperature"] == 95.0
    assert rows[0]["hiveName"] == "Hive One"


def test_topology_from_local_data(tmp_path):
    store = seed_store(tmp_path)
    topo = store.topology()
    assert topo[0]["apiaryName"] == "A1"
    assert topo[0]["hives"][0]["hiveId"] == "h1"
    assert topo[0]["hives"][0]["devices"] == ["d1"]


def test_offline_source_never_calls_client(tmp_path):
    store = seed_store(tmp_path)
    fake = FakeClient()
    report = store.ensure_coverage(["h1"], BASE, BASE + DAY, source="offline", client=fake)
    assert fake.calls == 0
    assert report["fetched_windows"] == 0
    assert report["stale"] is False


def test_auto_fetches_only_missing_suffix_then_zero(tmp_path, capsys):
    store = seed_store(tmp_path)
    fake = FakeClient()
    r1 = store.ensure_coverage(["h1"], BASE, BASE + 2 * DAY, source="auto", client=fake)
    assert r1["fetched_windows"] == 1
    calls_after = fake.calls
    assert calls_after > 0
    # new data visible offline afterwards
    rows = list(store.iter_readings("h1", BASE, BASE + 2 * DAY))
    assert len(rows) >= 2
    r2 = store.ensure_coverage(["h1"], BASE, BASE + 2 * DAY, source="auto", client=fake)
    assert r2["fetched_windows"] == 0
    assert fake.calls == calls_after  # never re-fetch
    assert capsys.readouterr().out == ""  # stdout purity


def test_rate_limited_returns_stale_not_raise(tmp_path):
    store = seed_store(tmp_path)
    fake = FakeClient(fail=True)
    report = store.ensure_coverage(["h1"], BASE, BASE + 2 * DAY, source="auto", client=fake)
    assert report["stale"] is True
    assert report["fetched_windows"] == 0


class NotesFailClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.notes_calls = 0

    def hive_notes(self, hive_id, start, end, raw=True):
        self.calls += 1
        self.notes_calls += 1
        raise RuntimeError("notes boom")


def test_merge_flat_archive_with_raw(tmp_path):
    store = seed_store(tmp_path)
    (tmp_path / "readings.ndjson").write_text(
        json.dumps({"hiveId": "h-other", "deviceId": "d9", "positionID": "p",
                    "timestamp": BASE + 5, "m_weight": 42.0}) + "\n"
        + json.dumps({"hiveId": "h1", "deviceId": "d1", "positionID": "p1",
                      "timestamp": BASE + 10, "m_temperature": 95.0}) + "\n")
    rows = list(store.iter_readings())
    keys = {(r["hiveId"], r["deviceId"], r["timestamp"]) for r in rows}
    assert ("h-other", "d9", BASE + 5) in keys  # archived hive not hidden by raw/
    assert len(rows) == len(keys) == 2  # overlapping row deduplicated


def test_prefix_gap_is_fetched(tmp_path):
    store = seed_store(tmp_path)
    fake = FakeClient()
    report = store.ensure_coverage(["h1"], BASE - DAY, BASE + DAY, source="auto", client=fake)
    assert report["fetched_windows"] == 1  # only the missing prefix window
    rows = list(store.iter_readings("h1", BASE - DAY, BASE + DAY))
    assert any(r["timestamp"] == BASE - DAY + 10 for r in rows)


def test_notes_failure_tracked_and_retried_without_refetch(tmp_path):
    store = LocalStore(tmp_path)
    failing = NotesFailClient()
    r1 = store.ensure_coverage(["h1"], BASE, BASE + DAY, source="auto", client=failing)
    assert r1["fetched_windows"] == 1
    assert r1["stale"] is True
    ok = FakeClient()
    readings_before = ok.calls
    r2 = store.ensure_coverage(["h1"], BASE, BASE + DAY, source="auto", client=ok)
    assert r2["fetched_windows"] == 0  # readings not refetched
    assert r2["stale"] is False
    assert list(store.iter_notes("h1"))  # notes now present


def test_transport_error_wrapped_as_broodminder_error():
    import httpx

    from bm.client import BroodMinderClient, BroodMinderError

    def _boom(request):
        raise httpx.ConnectError("down", request=request)

    client = BroodMinderClient(api_key="test-key", max_retries=0)
    client._client = httpx.Client(base_url="https://example.invalid",
                                  transport=httpx.MockTransport(_boom))
    try:
        import pytest

        with pytest.raises(BroodMinderError):
            client.hive_readings("h1", BASE, BASE + DAY)
    finally:
        client.close()


def test_invalid_hive_id_rejected(tmp_path):
    import pytest

    store = seed_store(tmp_path)
    with pytest.raises(ValueError):
        list(store.iter_readings("../escape"))
    with pytest.raises(ValueError):
        store.ensure_coverage(["../../etc"], BASE, BASE + DAY, source="offline")


def test_corrupt_manifest_returns_stale_without_overwrite(tmp_path):
    store = seed_store(tmp_path)
    (tmp_path / "manifest.json").write_text("{not json")
    fake = FakeClient()
    report = store.ensure_coverage(["h1"], BASE, BASE + 2 * DAY, source="auto", client=fake)
    assert report["stale"] is True
    assert fake.calls == 0
    assert (tmp_path / "manifest.json").read_text() == "{not json"  # not overwritten


def test_corrupt_gzip_window_skipped(tmp_path):
    store = seed_store(tmp_path)
    bad = tmp_path / "raw" / "h1" / "1-2.readings.json.gz"
    bad.write_bytes(b"not a gzip stream")
    rows = list(store.iter_readings("h1", BASE, BASE + DAY))
    assert len(rows) == 1  # good window still readable


def test_small_tail_not_fetched(tmp_path):
    store = seed_store(tmp_path)
    fake = FakeClient()
    # Missing tail is 100s (< MIN_TAIL_SECONDS) beyond settled coverage.
    report = store.ensure_coverage(
        ["h1"], BASE, BASE + DAY + 100 + 900, source="auto", client=fake)
    assert report["fetched_windows"] == 0
    assert fake.calls == 0


def test_multi_hive_sync_persists_all_windows(tmp_path):
    store = LocalStore(tmp_path)
    fake = FakeClient()
    r1 = store.ensure_coverage(["h1", "h2"], BASE, BASE + DAY, source="auto", client=fake)
    assert r1["fetched_windows"] == 2
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert any(k.startswith("h1|") for k in manifest["completed"])
    assert any(k.startswith("h2|") for k in manifest["completed"])
    calls = fake.calls
    r2 = store.ensure_coverage(["h1", "h2"], BASE, BASE + DAY, source="auto", client=fake)
    assert r2["fetched_windows"] == 0 and fake.calls == calls


def test_topology_fills_metadata_from_archive(tmp_path):
    store = LocalStore(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"completed": {f"h9|{BASE}|{BASE + DAY}": {"reading_rows": 1}}, "meta": {}}))
    (tmp_path / "readings.ndjson").write_text(json.dumps(
        {"hiveId": "h9", "hiveName": "Named Hive", "apiaryId": "a9",
         "apiaryName": "Named Apiary", "deviceId": "d9", "positionID": "p",
         "timestamp": BASE, "m_weight": 1.0}) + "\n")
    topo = store.topology()
    assert topo[0]["apiaryName"] == "Named Apiary"
    assert topo[0]["hives"][0]["hiveName"] == "Named Hive"


def test_offline_needs_no_writable_store(tmp_path):
    missing = tmp_path / "nope"
    store = LocalStore(missing)
    report = store.ensure_coverage(["h1"], BASE, BASE + DAY, source="offline")
    assert report["stale"] is True
    assert not missing.exists()  # no lock file / directory created


def test_raw_rows_inherit_archive_metadata(tmp_path):
    store = LocalStore(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"completed": {f"h9|{BASE}|{BASE + DAY}": {"reading_rows": 1}}, "meta": {}}))
    (tmp_path / "readings.ndjson").write_text(json.dumps(
        {"hiveId": "h9", "apiaryName": "Named Apiary", "deviceId": "d9",
         "positionID": "p", "timestamp": BASE, "m_weight": 1.0}) + "\n")
    hdir = tmp_path / "raw" / "h9"
    hdir.mkdir(parents=True)
    payload = [{"positionID": "p", "readings": [{
        "deviceId": "d9", "timestamp": BASE + 50, "batteryLevel": 90,
        "chargeRemaining": None, "readings": {"weight": 2.0}}]}]
    with gzip.open(hdir / f"{BASE}-{BASE + DAY}.readings.json.gz", "wt") as fh:
        json.dump(payload, fh)
    raw_row = [r for r in store.iter_readings("h9") if r["timestamp"] == BASE + 50][0]
    assert raw_row["apiaryName"] == "Named Apiary"


def test_auto_on_unwritable_store_returns_stale(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")  # data_dir path is a file: mkdir/lock raises OSError
    store = LocalStore(blocker)
    report = store.ensure_coverage(["h1"], BASE, BASE + DAY, source="auto", client=FakeClient())
    assert report["stale"] is True
    assert report["fetched_windows"] == 0


def test_transport_failure_aborts_remaining_hives(tmp_path):
    from bm.client import BroodMinderError

    class DownClient(FakeClient):
        def hive_readings(self, hive_id, start, end):
            self.calls += 1
            raise BroodMinderError(0, "GET", "http://x", "transport error: down")

    store = LocalStore(tmp_path)
    down = DownClient()
    report = store.ensure_coverage(["h1", "h2", "h3"], BASE, BASE + DAY,
                                   source="auto", client=down)
    assert report["stale"] is True
    assert down.calls == 1  # retries already burned by the client; stop here


def test_legacy_no_notes_record_gets_notes_only_fetch(tmp_path):
    # Manifest written by extract_all.py --no-notes: no notes info at all.
    store = LocalStore(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"completed": {f"h1|{BASE}|{BASE + DAY}": {"reading_rows": 3}}, "meta": {}}))
    fake = FakeClient()
    report = store.ensure_coverage(["h1"], BASE, BASE + DAY, source="auto", client=fake)
    assert report["fetched_windows"] == 0  # readings not refetched
    assert report["stale"] is False
    assert list(store.iter_notes("h1"))  # notes now fetched


def test_rate_limit_aborts_remaining_hives(tmp_path):
    store = LocalStore(tmp_path)
    fake = FakeClient(fail=True)  # hive_readings raises RateLimited
    report = store.ensure_coverage(["h1", "h2", "h3"], BASE, BASE + DAY,
                                   source="auto", client=fake)
    assert report["stale"] is True
    assert fake.calls == 1  # batch aborted after first exhausted 429


def test_notes_from_raw_and_flattened_fallback(tmp_path):
    store = seed_store(tmp_path)
    fake = FakeClient()
    store.ensure_coverage(["h1"], BASE, BASE + 2 * DAY, source="auto", client=fake)
    notes = list(store.iter_notes("h1"))
    assert notes and notes[0]["description"] == "inspected"

    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "readings.ndjson").write_text(json.dumps(
        {"hiveId": "h9", "deviceId": "d9", "positionID": "p", "timestamp": BASE,
         "m_weight": 42.0}) + "\n")
    rows = list(LocalStore(flat).iter_readings("h9"))
    assert rows[0]["m_weight"] == 42.0
