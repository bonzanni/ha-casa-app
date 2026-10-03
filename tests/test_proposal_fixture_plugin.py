"""The PLAY fixture plugin for stored-call buttons (S5, plan §1.10): its
manifest validates and declares the three tools (`offer` and `more` deliver
`operator_proposal`; `apply` is safe), its server speaks stdio JSON-RPC,
deposits its proposals through the REAL broker deposit route and records what
`apply` received into the plugin data directory, and `more` pages twice before
the contract's no-post shape. `offer_hang` deposits a one-button proposal naming
`hang`, the stored call that never returns (never called here).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from aiohttp import web

import result_broker as rb
from plugin_grants import result_contract_map
from plugin_registry import ResolutionResult, ResolvedPlugin
from plugin_store import (
    manifest_result_contract, mcp_command_verdicts, mcp_servers_map, reserved_env_violations,
    validate_manifest,
)
from test_proposal_slot import _identity

ROOT = Path(__file__).resolve().parents[1] / "test-local" / "fixtures" / "proposal-plugin"
NAME = "proposal-fixture"


def _resolution():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    return ResolutionResult(registry_valid=True, plugins=[ResolvedPlugin(
        name=NAME, artifact_id="f" * 64, path=str(ROOT), version=manifest["version"],
        manifest=manifest, manifest_name=NAME)])


def test_the_manifest_validates_and_declares_offer_apply_and_more():
    manifest = validate_manifest(ROOT, NAME)
    contract = manifest_result_contract(manifest)
    tools = contract["tools"]
    assert set(tools) == {"offer", "apply", "more", "offer_hang", "hang"}
    for name in ("offer", "more", "offer_hang"):
        assert tools[name]["result"] == "capability"
        assert tools[name]["provides"] == ["proposal"]
        assert tools[name]["delivers"] == {"proposal": "operator_proposal"}
        assert tools[name]["consumes"] == {}
    for name in ("apply", "hang"):
        assert tools[name]["result"] == "safe" and tools[name]["consumes"] == {}
    servers = mcp_servers_map(ROOT / ".mcp.json")
    assert list(servers) == ["fixture"] and servers["fixture"]["command"] == "python3"
    assert reserved_env_violations(ROOT / ".mcp.json") == []
    assert all(row["status"] in ("ok", "unchecked")
               for row in mcp_command_verdicts(ROOT / ".mcp.json", ROOT)), \
        mcp_command_verdicts(ROOT / ".mcp.json", ROOT)
    # the contract map Casa builds from it admits the buttons' targets
    cmap = result_contract_map(_resolution())
    seg = next(iter(cmap.plugins))
    assert cmap.plugins[seg].adopted
    apply = cmap.tools[f"mcp__plugin_{seg}_fixture__apply"]
    assert apply.kind == "safe" and apply.transport == "stdio" and apply.servers == ("fixture",)


class _Server:
    def __init__(self, proc):
        self.proc, self._id = proc, 0

    async def call(self, method, params=None):
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            req["params"] = params
        self.proc.stdin.write((json.dumps(req) + "\n").encode())
        await self.proc.stdin.drain()
        line = await asyncio.wait_for(self.proc.stdout.readline(), 10)
        res = json.loads(line)
        assert res["id"] == self._id and "error" not in res, res
        return res["result"]

    async def tool(self, name, arguments):
        res = await self.call("tools/call", {"name": name, "arguments": arguments})
        return json.loads(res["content"][0]["text"])


@pytest.fixture
async def broker(tmp_path):
    store = rb.ReferenceStore(now=lambda: 1000.0)
    app = web.Application()
    app.router.add_post("/internal/broker/deposit", rb.build_broker_deposit_handler(store))
    runner = web.AppRunner(app)
    await runner.setup()
    sock = tmp_path / "broker.sock"
    site = web.UnixSite(runner, str(sock))
    await site.start()
    try:
        yield store, sock
    finally:
        await runner.cleanup()


def _open(store, cmap, tool, call_id):
    seg = next(iter(cmap.plugins))
    runtime = f"mcp__plugin_{seg}_fixture__{tool}"
    entry = cmap.tools[runtime]
    store.open_call(client_id="c1", artifact_id=entry.artifact_id, tool_name=runtime,
                    tool_use_id=call_id, identity=_identity(artifact_id=entry.artifact_id),
                    provides=entry.provides, delivers=dict(entry.delivers),
                    contract_map=cmap, protected={}, entry=entry)


async def test_the_server_offers_applies_and_pages_through_the_real_deposit_route(broker, tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "POST_MAP", rb.PostMap())
    store, sock = broker
    cmap = result_contract_map(_resolution())
    data = tmp_path / "plugin-data"
    data.mkdir()
    env = {**os.environ, "CASA_BROKER_CLIENT": "c1", "CASA_BROKER_SOCKET": str(sock),
           "CLAUDE_PLUGIN_DATA": str(data), "CLAUDE_PLUGIN_ROOT": str(ROOT)}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "server.py"), env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    srv = _Server(proc)
    try:
        init = await srv.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        assert "tools" in init["capabilities"]
        listed = await srv.call("tools/list")
        assert {t["name"] for t in listed["tools"]} == {"offer", "apply", "more", "offer_hang", "hang"}
        # offer: a proposal deposited through the broker, validated by the deposit rule
        _open(store, cmap, "offer", "call-offer")
        out = await srv.tool("offer", {})
        ref = out["proposal"]
        assert rb.is_reference(ref)
        proposal = store._refs[ref].proposal
        assert [b["label"] for b in proposal["buttons"]] == ["Yes", "No", "More"]
        assert [b["call"]["wire_name"] for b in proposal["buttons"]] == ["apply", "apply", "more"]
        assert proposal["buttons"][2]["call"]["proposal"] is True
        yes = proposal["buttons"][0]["call"]["arguments"]
        # apply: records what it received and answers with a receipt the operator can read
        receipt = await srv.tool("apply", yes)
        assert receipt["applied"] is True and "yes" in receipt["receipt"]
        rows = [json.loads(l) for l in (data / "applied.jsonl").read_text().splitlines()]
        assert rows == [yes]
        # the plugin's own revision guard refuses without raising: a receipt too
        stale = await srv.tool("apply", {**yes, "expected_revision": yes["expected_revision"] + 1})
        assert stale["applied"] is False and "revision" in stale["receipt"]
        # more: page 2 is another proposal; page 3 is the contract's no-post shape
        more_args = proposal["buttons"][2]["call"]["arguments"]
        store.close_call("c1", "call-offer")             # the result hook closes a call at PostToolUse
        _open(store, cmap, "more", "call-more")
        page2 = await srv.tool("more", more_args)
        assert rb.is_reference(page2["proposal"])
        p2 = store._refs[page2["proposal"]].proposal
        assert p2["buttons"][-1]["call"]["arguments"]["page"] == more_args["page"] + 1
        last = await srv.tool("more", p2["buttons"][-1]["call"]["arguments"])
        assert last == {"proposal": None, "note": "no more entries"}
        # offer_hang: one button naming hang, admitted by the same deposit rule
        store.close_call("c1", "call-more")
        _open(store, cmap, "offer_hang", "call-hang")
        hang = await srv.tool("offer_hang", {})
        hp = store._refs[hang["proposal"]].proposal
        assert [b["label"] for b in hp["buttons"]] == ["Hang"]
        assert hp["buttons"][0]["call"]["wire_name"] == "hang"
        assert hp["buttons"][0]["call"]["arguments"] == {"render_id": "r-1"}
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
