"""query_engager reads the engager's memory — its view of the ledger the
engagement's clearance reads, and a search of the saved conversation — at
the read clearance of the turn that created the engagement (#336), and keeps
its three outcomes: ok, unknown (not proof of absence) and unavailable."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from memory import capture
from memory_serving_support import add, fact
from memory_tools_support import canary, new_store, store_event

pytestmark = [pytest.mark.unit]


async def _setup(reg, monkeypatch, tmp_path, *,
                 synth_answer="Yes, Lantern paid in March."):
    from tools import init_tools

    bus = MagicMock()

    async def _notify(*a, **k):
        return None

    bus.notify = _notify
    init_tools(
        channel_manager=MagicMock(), bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    store = new_store(tmp_path / "mem")
    monkeypatch.setitem(capture._wiring, "store", store)
    contexts: list[str] = []

    async def _fake_synth(question, context, max_tokens):
        contexts.append(context)
        return synth_answer

    monkeypatch.setattr("tools._synthesize_answer", _fake_synth)
    return store, contexts


async def _engagement(tmp_path, **markers):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="t", origin={"role": "assistant", "channel": "telegram",
                          "chat_id": "c1", **markers},
    )
    return reg, rec


async def _ask(rec, question: str) -> dict:
    from tools import engagement_var, query_engager
    token = engagement_var.set(rec)
    try:
        res = await query_engager.handler(
            {"question": question, "max_tokens": 500})
    finally:
        engagement_var.reset(token)
    return json.loads(res["content"][0]["text"])


async def test_the_synthesizer_reads_the_view_and_the_search(tmp_path,
                                                             monkeypatch):
    reg, rec = await _engagement(tmp_path)
    store, contexts = await _setup(reg, monkeypatch, tmp_path)
    add(store, [fact("Lantern", "paid in March", topic="Clients")])
    store_event(store, "Lantern's invoice was paid by bank transfer")
    store_event(store, "the bins go out on tuesday")

    payload = await _ask(rec, "when did Lantern pay?")

    assert payload == {"status": "ok", "text": "Yes, Lantern paid in March."}
    (context,) = contexts
    assert "Lantern: paid in March" in context
    assert "Lantern's invoice was paid by bank transfer" in context
    assert "bins" not in context


async def test_nothing_readable_is_unknown_not_absence(tmp_path, monkeypatch):
    reg, rec = await _engagement(tmp_path)
    store, contexts = await _setup(reg, monkeypatch, tmp_path)

    payload = await _ask(rec, "when did Lantern pay?")

    assert payload["status"] == "unknown" and contexts == []
    assert "NOT proof of absence" in payload["message"]


async def test_an_unreadable_store_is_unavailable_not_unknown(tmp_path,
                                                              monkeypatch):
    """Three-outcome contract: engager memory that could NOT be checked is
    'unavailable', distinct from a genuine 'unknown'."""
    from tools import query_engager
    reg, rec = await _engagement(tmp_path)
    store, contexts = await _setup(reg, monkeypatch, tmp_path)
    store._unavailable_reason = "a simulated outage"

    payload = await _ask(rec, "x")

    assert payload["status"] == "unavailable" and contexts == []
    assert "could not be checked" in payload["message"]
    for status in ("status=ok", "status=unknown", "status=unavailable"):
        assert status in query_engager.description


async def test_non_operator_engagement_queries_at_public_only(tmp_path,
                                                              monkeypatch):
    """#336: the engager-side context is read on behalf of THIS engagement,
    at the clearance of the turn that created it. Red case: reading the
    ambient channel's clearance instead makes the private ledger and the
    private conversation readable to a non-operator's engagement."""
    reg, rec = await _engagement(tmp_path, _origin_route="telegram",
                                 _origin_clearance="public")
    store, contexts = await _setup(reg, monkeypatch, tmp_path)
    secret = canary()
    add(store, [fact("alarm", f"the alarm code is {secret}")])
    store_event(store, f"the alarm code is {secret}", tier="private")
    store_event(store, "the alarm is tested on mondays", tier="public")

    await _ask(rec, "what is the alarm code?")

    (context,) = contexts
    assert "the alarm is tested on mondays" in context
    assert secret not in context


def test_the_description_never_claims_memory_holds_nothing():
    from tools import query_engager
    assert "holds nothing relevant" not in query_engager.description
    assert "NOT proof of absence" in query_engager.description
