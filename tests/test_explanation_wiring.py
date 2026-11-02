"""GH #199: the per-turn explanation writer is wired into Agent._process.

Task 14 shipped ExplanationStore + the /admin/explain route + `casactl explain`,
but nothing called ``record()`` — every correlation id 404'd. These tests drive
the REAL production turn path (Agent._process via the SDK-client pool, with the
CLI subprocess stubbed the way sibling agent-turn tests do) and prove:

* a record lands under the turn's ACTUAL correlation id (the one an operator
  reads from logs / cid_var), so ``casactl explain <cid>`` round-trips to a 200;
* the record honestly captures the resolved model, the selected prompt
  projection, the prompt/binding digests, and the served memory view with
  its ledger and base;
* an explanation-store failure never fails or delays the user's reply.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import agent as agent_mod
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from explanation_store import ExplanationStore
from log_cid import cid_var
from mcp_registry import McpServerRegistry
from personality_binding import BindingRecord
from personality_types import SpeakerProvenance
from prompt_compiler import CompiledProjection, CompiledPromptBundle
from session_registry import SessionRegistry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.unit


class _FakeClient:
    """Minimal stand-in for the CLI subprocess client (mirrors the sibling
    agent-turn tests): opens, accepts a query, yields no messages, returns a
    fixed session id."""

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, text):
        return None

    async def receive_response(self):
        return
        yield  # pragma: no cover

    @property
    def session_id(self):
        return "sid-explain"


def _binding() -> BindingRecord:
    return BindingRecord(
        stable_agent_id="resident:assistant", role_checksum="rc-assistant",
        mode="component-default", persona_id="ellen", persona_version="3",
        persona_checksum="pc-ellen", compiler_schema_version="v1",
        dependency_digests=("dep-a", "dep-b"), effective_config_digest="ecd-ellen",
        binding_digest="bd-ellen",
    )


def _bundle() -> CompiledPromptBundle:
    text = CompiledProjection(
        system_prompt="TEXT PROJECTION — Ellen persona prose",
        digest="digest-text", estimated_tokens=321,
    )
    voice = CompiledProjection(
        system_prompt="VOICE PROJECTION", digest="digest-voice", estimated_tokens=111,
    )
    restricted = CompiledProjection(
        system_prompt="RESTRICTED PROJECTION", digest="digest-restricted",
        estimated_tokens=42,
    )
    return CompiledPromptBundle(
        role_id="resident:assistant", resolved_model="claude-sonnet-4-6",
        text=text, voice=voice, restricted_webhook=restricted,
        binding_digest="bd-ellen",
    )


def _resident_config(semantic_hint="You are Ellen (legacy).") -> AgentConfig:
    binding = _binding()
    return AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT,
        role="assistant",
        model="claude-sonnet-4-6",
        system_prompt=semantic_hint,
        character=CharacterConfig(name="Ellen"),
        tools=ToolsConfig(allowed=[]),
        memory=MemoryConfig(token_budget=1000),
        role_id="resident:assistant",
        kind="resident",
        role_checksum="rc-assistant",
        binding=binding,
        compiled_prompt_bundle=_bundle(),
        binding_digest="bd-ellen",
    )


def _make_agent(tmp_path) -> agent_mod.Agent:
    return agent_mod.Agent(
        config=_resident_config(),
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
    )


def _admin_app(store: ExplanationStore) -> web.Application:
    from personality_admin_handlers import register_personality_admin_routes

    runtime = SimpleNamespace(
        explanation_store=store, persona_packs={}, compiled_prompt_bundles={},
        role_slots={}, bindings={},
    )
    app = web.Application()
    register_personality_admin_routes(app, runtime=runtime)
    return app


def _drive_turn(agent, monkeypatch, store, *, cid):
    """Run one telegram turn through the production _process path with the CLI
    stubbed and the module-global runtime pointed at ``store``."""
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        SimpleNamespace(explanation_store=store), raising=False,
    )
    token = cid_var.set(cid)
    try:
        async def run():
            with patch("sdk_client_pool._default_make_client", _FakeClient):
                msg = BusMessage(
                    type=MessageType.REQUEST, source="telegram",
                    target="assistant", content="what did we decide?",
                    channel="telegram", context={"chat_id": "42"},
                )
                await agent._process(msg, on_token=None)

        asyncio.run(run())
    finally:
        cid_var.reset(token)


def test_turn_records_explanation_roundtrips_via_admin_route(tmp_path, monkeypatch):
    """The record lands under the turn's real cid → GET /admin/explain returns
    200 with the honest projection/model/binding, sensitive fields stripped."""
    store = ExplanationStore(tmp_path / "explanations")
    agent = _make_agent(tmp_path)

    cid = "c-explain-roundtrip-1"
    _drive_turn(agent, monkeypatch, store, cid=cid)

    # The store now holds the record (proves record() was called on the turn).
    raw = store.get(cid, show_sensitive=False)
    assert raw["role_id"] == "resident:assistant"
    assert raw["resolved_model"] == "claude-sonnet-4-6"
    assert raw["projection"] == "text"
    assert raw["static_prompt_digest"] == "digest-text"
    assert raw["static_prompt_estimated_tokens"] == 321
    assert raw["binding_digest"] == "bd-ellen"
    assert raw["persona_ref"] == "ellen@3"
    assert raw["dependency_digests"] == ["dep-a", "dep-b"]
    # Sensitive prose fields stripped by default (tier-token gating is #202).
    assert "system_prompt" not in raw
    assert "memory_text" not in raw

    async def check_route():
        app = _admin_app(store)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/admin/explain", json={"correlation_id": cid},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["projection"] == "text"
            assert body["role_id"] == "resident:assistant"

    asyncio.run(check_route())


def test_turn_records_the_served_view_its_ledger_and_base(tmp_path, monkeypatch):
    """The explanation record names the ledger the session reads
    and the commit its frozen view was rendered at, and keeps the served view
    as the sensitive memory prose (tiers gated behind show_sensitive)."""
    from memory_serving_support import add, fact, ledger_store, wire
    memory = ledger_store(tmp_path, [fact("release", "we decided to ship Friday")])
    commit_no, _ = add(memory, [fact("venue", "the office")])
    wire(monkeypatch, memory)
    store = ExplanationStore(tmp_path / "explanations")
    agent = _make_agent(tmp_path)

    cid = "c-explain-memory-1"
    _drive_turn(agent, monkeypatch, store, cid=cid)

    full = store.get(cid, show_sensitive=True)
    assert full["memory_tiers"] == ["private"]
    assert full["memory_ledger"] == "private"
    assert full["memory_base_commit_no"] == commit_no
    assert full["memory_text"].startswith("<memory>")
    assert "decided to ship Friday" in full["memory_text"]
    assert "decided to ship Friday" in full["system_prompt"]
    default = store.get(cid, show_sensitive=False)
    assert "memory_text" not in default and "memory_tiers" not in default
    assert default["memory_ledger"] == "private"


def test_projection_name_matches_build_options_selection():
    """The recorded projection name must track prompt_compiler.projection_for's
    selection (Mg): webhook_trigger → restricted_webhook, voice → voice, else
    text. The untrusted-webhook short-circuit in _build_options records
    "restricted_webhook" directly and never routes through this function, so —
    matching projection_for — a webhook turn with a missing/unknown route
    resolves to text here, not restricted."""
    from agent import _projection_name_for_turn

    assert _projection_name_for_turn("webhook", "webhook_trigger") == "restricted_webhook"
    # projection_for falls back to the text surface for a missing/unknown
    # webhook route — this function mirrors that exactly.
    assert _projection_name_for_turn("webhook", None) == "text"
    # An explicit server-stamped invoke webhook is a trusted origin → text.
    assert _projection_name_for_turn("webhook", "invoke") == "text"
    assert _projection_name_for_turn("voice", None) == "voice"
    assert _projection_name_for_turn("telegram", None) == "text"
    # A voice channel carrying a webhook_trigger route still takes restricted,
    # exactly as projection_for does (route wins over channel).
    assert _projection_name_for_turn("voice", "webhook_trigger") == "restricted_webhook"


def test_explanation_store_failure_never_fails_the_turn(tmp_path, monkeypatch):
    """A store that raises on record() must not propagate into the user turn."""

    class _ExplodingStore:
        def record(self, record):
            raise RuntimeError("disk full")

    agent = _make_agent(tmp_path)

    monkeypatch.setattr(
        agent_mod, "active_runtime",
        SimpleNamespace(explanation_store=_ExplodingStore()), raising=False,
    )
    token = cid_var.set("c-explain-explode-1")
    try:
        async def run():
            with patch("sdk_client_pool._default_make_client", _FakeClient):
                msg = BusMessage(
                    type=MessageType.REQUEST, source="telegram",
                    target="assistant", content="hi",
                    channel="telegram", context={"chat_id": "7"},
                )
                # Must complete without raising despite the store fault.
                return await agent._process(msg, on_token=None)

        # No exception escapes.
        asyncio.run(run())
    finally:
        cid_var.reset(token)


def test_explanation_record_runs_off_the_event_loop(tmp_path, monkeypatch):
    """F7 (whole-branch review): the ExplanationStore write+prune must run via
    ``asyncio.to_thread`` — off the event loop — and a fault inside that thread
    must STILL never propagate into the user turn. Captures the thread ident
    ``record()`` runs on and proves (a) it differs from the loop thread and
    (b) even a RuntimeError raised in the worker thread is swallowed."""
    import threading

    loop_ident = threading.get_ident()
    seen: dict = {}

    class _ThreadCheckingStore:
        def record(self, record):
            seen["ident"] = threading.get_ident()
            # A fault inside the worker thread re-raises into _process's
            # fail-safe try/except — it must NOT fail the turn.
            raise RuntimeError("boom in worker thread")

    agent = _make_agent(tmp_path)

    monkeypatch.setattr(
        agent_mod, "active_runtime",
        SimpleNamespace(explanation_store=_ThreadCheckingStore()), raising=False,
    )
    token = cid_var.set("c-explain-offloop-1")
    try:
        async def run():
            with patch("sdk_client_pool._default_make_client", _FakeClient):
                msg = BusMessage(
                    type=MessageType.REQUEST, source="telegram",
                    target="assistant", content="hi",
                    channel="telegram", context={"chat_id": "7"},
                )
                return await agent._process(msg, on_token=None)

        # Completes without raising despite the in-thread fault.
        asyncio.run(run())
    finally:
        cid_var.reset(token)

    assert seen.get("ident") is not None, "store.record was never invoked"
    assert seen["ident"] != loop_ident, "store.record ran on the event-loop thread, not a worker"
