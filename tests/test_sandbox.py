"""Offline unit tests for bm/sandbox.py (issue #221)."""

from __future__ import annotations

import os

from bm.battery import scan_device_health_from_stream
from bm.sandbox import SandboxStore, sandbox_notes, sandbox_readings

NOW = 1_800_000_000


def test_deterministic_same_now():
    a = sandbox_readings(NOW)
    b = sandbox_readings(NOW)
    assert a == b
    assert sandbox_notes(NOW) == sandbox_notes(NOW)


def test_three_scenario_hives_and_health_classification():
    rows = sandbox_readings(NOW)
    hives = {r["hiveId"] for r in rows}
    assert hives == {"hive-healthy", "hive-swarm", "hive-offline"}
    health = {d.device_id: d for d in scan_device_health_from_stream(rows, now_ts=NOW)}
    assert health["dev-offline"].status == "CRITICAL"
    assert health["dev-offline"].needs_attention is True
    assert health["dev-offline"].days_offline > 7
    assert health["dev-healthy"].needs_attention is False


def test_swarm_weight_drop_visible():
    rows = [r for r in sandbox_readings(NOW) if r["hiveId"] == "hive-swarm"]
    weights = [r["m_weight"] for r in sorted(rows, key=lambda r: r["timestamp"]) if r.get("m_weight") is not None]
    assert weights[-1] < weights[0] - 3  # sharp drop


def test_store_interface_no_key_no_network(monkeypatch):
    monkeypatch.delenv("BROODMINDER_API_KEY", raising=False)
    store = SandboxStore(now_ts=NOW)
    assert store.ensure_coverage(["hive-healthy"], 0, NOW)["fetched_windows"] == 0
    rows = list(store.iter_readings("hive-healthy"))
    assert rows and all(r["hiveId"] == "hive-healthy" for r in rows)
    notes = list(store.iter_notes("hive-healthy"))
    assert notes
    topo = store.topology()
    assert topo[0]["apiaryName"] == "Sandbox Apiary"
    assert {h["hiveId"] for h in topo[0]["hives"]} == {"hive-healthy", "hive-swarm", "hive-offline"}
    assert os.environ.get("BROODMINDER_API_KEY") is None
