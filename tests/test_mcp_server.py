"""Offline protocol + logic tests for bm/mcp_server.py (issues #222-#224)."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from bm.mcp_server import build_server, device_health, hive_status, telemetry_trends  # noqa: E402
from bm.sandbox import SandboxStore  # noqa: E402

NOW = 1_800_000_000
TOOLS = {"get_apiary_summary", "get_hive_status", "get_telemetry_trends",
         "get_hive_notes", "get_device_health"}


def _server():
    return build_server(store=SandboxStore(now_ts=NOW), now_ts=NOW)


def _text(result) -> str:
    parts = []
    items = getattr(result, "content", None) or getattr(result, "contents", []) or []
    for item in items:
        parts.append(getattr(item, "text", "") or "")
    if getattr(result, "messages", None):
        for msg in result.messages:
            content = msg.content
            parts.append(getattr(content, "text", "") or "")
    return "\n".join(parts)


def _call_json(result):
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(_text(result))


def test_tool_logic_direct_sandbox():
    store = SandboxStore(now_ts=NOW)
    status = hive_status(store, "hive-swarm", now_ts=NOW)
    assert status["devices"][0]["metrics"]["weight"] == 76.5
    assert status["provenance"]["source"] == "sandbox"
    trends = telemetry_trends(store, "hive-swarm", "weight", days=10, bucket="1d", now_ts=NOW)
    assert trends["delta"] < -3
    health = device_health(store, now_ts=NOW)
    by_id = {d["device_id"]: d for d in health["devices"]}
    assert by_id["dev-offline"]["status"] == "CRITICAL"
    assert health["provenance"]["source"] == "sandbox"


def test_protocol_tools_resources_prompts():
    async def run():
        server = _server()
        async with create_connected_server_and_client_session(server) as session:
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == TOOLS

            summary = _call_json(await session.call_tool("get_apiary_summary", {}))
            assert summary["apiaries"][0]["apiary_name"] == "Sandbox Apiary"

            notes = _call_json(await session.call_tool(
                "get_hive_notes", {"hive_id": "hive-swarm", "days": 30}))
            assert any("Queen cells" in n.get("description", "") for n in notes["notes"])

            health = _call_json(await session.call_tool("get_device_health", {}))
            assert any(d["device_id"] == "dev-offline" for d in health["devices"])

            resources = await session.list_resources()
            uris = {str(r.uri) for r in resources.resources}
            assert "broodminder://apiaries/list" in uris
            assert "broodminder://devices/health" in uris
            templates = await session.list_resource_templates()
            assert any("hives/{hive_id}/status" in str(t.uriTemplate)
                       for t in templates.resourceTemplates)

            read = await session.read_resource("broodminder://devices/health")
            assert "dev-offline" in _text(read)

            prompts = await session.list_prompts()
            assert {p.name for p in prompts.prompts} == {
                "inspection_prep", "device_health_audit"}
            prep = await session.get_prompt("inspection_prep", {"apiary_id": "apiary-sandbox"})
            assert "Swarm Hive" in _text(prep) or "hive-swarm" in _text(prep)
            audit = await session.get_prompt("device_health_audit", {})
            assert "dev-offline" in _text(audit)

    asyncio.run(run())


def test_readme_and_packaging_references_exist():
    """Guard for #217: modules/methods/entry points named in README must exist."""
    import re
    import tomllib
    from pathlib import Path

    from bm.client import BroodMinderClient

    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    for mod in set(re.findall(r"bm\.([a-z_]+)", readme)):
        assert (root / "bm" / f"{mod}.py").exists(), f"README names missing bm.{mod}"
    for meth in set(re.findall(r"client\.([a-z_]+)\(", readme)):
        assert hasattr(BroodMinderClient, meth), f"README names missing client.{meth}()"
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["scripts"]["broodminder-mcp"] == "bm.mcp_server:main"


def test_main_entrypoint_and_stdout_guard(capsys):
    from bm.mcp_server import main

    assert callable(main)
    server = _server()
    assert server.name == "broodminder"
    assert capsys.readouterr().out == ""
