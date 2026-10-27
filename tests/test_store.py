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
