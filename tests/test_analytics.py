"""Offline unit tests for bm/analytics.py (issue #219)."""

from __future__ import annotations

import pytest

from bm.analytics import derive_notes_window, latest_status, trends

BASE = 1_700_000_000


def row(hive="h1", device="d1", pos="p1", ts=BASE, **metrics):
    r = {
        "hiveId": hive,
        "deviceId": device,
        "positionID": pos,
        "timestamp": ts,
        "batteryLevel": metrics.pop("batteryLevel", None),
        "chargeRemaining": metrics.pop("chargeRemaining", None),
    }
    r.update({f"m_{k}": v for k, v in metrics.items()})
    return r


# -- latest_status -------------------------------------------------------


def test_latest_status_picks_newest_per_device():
    rows = [
        row(ts=BASE, temperature=90.0),
        row(ts=BASE + 3600, temperature=95.0),
        row(device="d2", ts=BASE + 100, temperature=80.0),
        row(hive="other", device="d9", ts=BASE + 9999, temperature=1.0),
    ]
    out = latest_status(iter(rows), "h1")
    assert out["hive_id"] == "h1"
    by_dev = {d["device_id"]: d for d in out["devices"]}
    assert set(by_dev) == {"d1", "d2"}
    assert by_dev["d1"]["timestamp"] == BASE + 3600
    assert by_dev["d1"]["metrics"] == {"temperature": 95.0}
    assert by_dev["d1"]["position_id"] == "p1"


def test_latest_status_coalesces_battery_fields():
    rows = [
        row(ts=BASE, batteryLevel=None, chargeRemaining=77),
        row(device="d2", ts=BASE, batteryLevel=42, chargeRemaining=None),
    ]
    out = latest_status(rows, "h1")
    by_dev = {d["device_id"]: d for d in out["devices"]}
    assert by_dev["d1"]["battery_percent"] == 77
    assert by_dev["d2"]["battery_percent"] == 42


def test_latest_status_empty_and_missing_ids():
    assert latest_status([], "h1")["devices"] == []
    rows = [{"hiveId": "h1", "timestamp": BASE}, row(device="", ts=BASE)]
    assert latest_status(rows, "h1")["devices"] == []


def test_latest_status_no_invented_metric_labels():
    out = latest_status([row(temperature=95.0)], "h1")
    dev = out["devices"][0]
    assert set(dev["metrics"]) == {"temperature"}
    assert "brood" not in str(out).lower()
    assert "ambient" not in str(out).lower()


# -- trends ---------------------------------------------------------------


def test_trends_buckets_min_max_mean_count():
    rows = [
        row(ts=BASE, temperature=90.0),
        row(ts=BASE + 1800, temperature=94.0),
        row(ts=BASE + 3600, temperature=100.0),
    ]
    out = trends(rows, "h1", "temperature", BASE, BASE + 7200, bucket="1h")
    assert out["bucket_seconds"] == 3600
    assert len(out["buckets"]) == 2
    b0, b1 = out["buckets"]
    assert (b0["min"], b0["max"], b0["mean"], b0["count"]) == (90.0, 94.0, 92.0, 2)
    assert (b1["min"], b1["max"], b1["mean"], b1["count"]) == (100.0, 100.0, 100.0, 1)
    assert out["delta"] == 8.0
    assert out["truncated"] is False


def test_trends_ignores_nulls_outside_window_and_prefixed_metric():
    rows = [
        row(ts=BASE - 10, temperature=1.0),
        row(ts=BASE, temperature=None),
        row(ts=BASE + 10, temperature=50.0),
        row(hive="other", ts=BASE + 10, temperature=999.0),
    ]
    out = trends(rows, "h1", "m_temperature", BASE, BASE + 3600, bucket="1h")
    assert len(out["buckets"]) == 1
    assert out["buckets"][0]["count"] == 1
    assert out["buckets"][0]["mean"] == 50.0


def test_trends_empty_delta_none_and_bad_bucket():
    out = trends([], "h1", "weight", BASE, BASE + 3600, bucket="6h")
    assert out["buckets"] == []
    assert out["delta"] is None
    with pytest.raises(ValueError):
        trends([], "h1", "weight", BASE, BASE + 3600, bucket="bogus")


def test_trends_bucket_edges_and_cap():
    # Exactly on a bucket boundary belongs to that bucket.
    rows = [row(ts=BASE + 86400 * i, weight=float(i)) for i in range(3)]
    out = trends(rows, "h1", "weight", BASE, BASE + 86400 * 3, bucket="1d")
    assert [b["count"] for b in out["buckets"]] == [1, 1, 1]
    assert out["delta"] == 2.0

    many = [row(ts=BASE + 3600 * i, weight=1.0) for i in range(600)]
    capped = trends(many, "h1", "weight", BASE, BASE + 3600 * 600, bucket="1h")
    assert len(capped["buckets"]) == 500
    assert capped["truncated"] is True
    # Truncation keeps the NEWEST buckets (trend ends at "now").
    assert capped["buckets"][-1]["start"] == (BASE + 3600 * 599) // 3600 * 3600
    assert capped["buckets"][0]["start"] == (BASE + 3600 * 100) // 3600 * 3600


# -- notes window ----------------------------------------------------------


def test_derive_notes_window():
    start, end = derive_notes_window(30, BASE)
    assert (start, end) == (BASE - 30 * 86400, BASE)
    with pytest.raises(ValueError):
        derive_notes_window(0, BASE)
