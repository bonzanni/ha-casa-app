# tests/test_delegated_memory.py
"""Delegated memory bridges an originating context to the shared casa bank."""
import pytest

import delegated_memory
from personality_types import RetainedTurn, SpeakerProvenance

pytestmark = [pytest.mark.unit]


def _user(peer: str = "nicola") -> SpeakerProvenance:
    return SpeakerProvenance(speaker_kind="user", user_peer=peer)


def _resident(slot: str = "finance") -> SpeakerProvenance:
    return SpeakerProvenance(
        speaker_kind="resident", role_id=f"resident:{slot}", persona_id=f"casa/{slot}",
        persona_version="0.1.0", display_name=slot.capitalize(),
        binding_digest="sha256:" + "a" * 64,
    )


def _hit(text="prior fact", provenance=None):
    from personality_types import RecallHit
    return RecallHit(
        text=text, memory_type="world", sensitivity="friends",
        application_tags=(), provenance=provenance, backend_id="b1",
        document_id=None, chunk_id=None, source_fact_ids=None, metadata=None,
        context=None, score=None,
    )


class _Sem:
    def __init__(self, recall_ret="prior fact"):
        self.recall_calls = []
        self.retain_calls = []
        self._recall_ret = recall_ret

    async def recall_items(self, bank, query, *, tags, max_tokens, clearance,
                           types=("world", "experience", "observation"),
                           tags_match="any", budget="mid"):
        self.recall_calls.append({"bank": bank, "query": query, "tags": sorted(tags),
                                  "budget": budget, "clearance": clearance})
        return (_hit(self._recall_ret),) if self._recall_ret else ()

    async def retain(self, bank, items, *, async_=True):
        self.retain_calls.append({"bank": bank, "items": items})


async def test_delegated_recall_uses_inherited_clearance():
    sem = _Sem()
    out = await delegated_memory.delegated_recall(
        sem, query="build the invoice", origin_channel="telegram", max_tokens=2000,
    )
    assert "prior fact" in out       # rendered (attributed) digest
    c = sem.recall_calls[0]
    assert c["bank"] == "casa"
    assert c["clearance"] == "private"
    assert c["tags"] == ["family", "friends", "private", "public"]   # telegram → private clearance


async def test_delegated_recall_voice_is_friends():
    sem = _Sem()
    await delegated_memory.delegated_recall(
        sem, query="q", origin_channel="voice", max_tokens=500, budget="low",
    )
    assert sem.recall_calls[0]["tags"] == ["friends", "public"]
    assert sem.recall_calls[0]["budget"] == "low"   # explicit override still wins


async def test_delegated_recall_defaults_to_mid_budget():
    # The D-3 low-budget default (v0.68.1) was reverted in v0.69.4 once the
    # hindsight-side rerank latency was fixed: mid → 300 candidates gives
    # materially better recall quality and no longer risks the 20s client
    # timeout. Explicit budget= (e.g. voice) still overrides.
    sem = _Sem()
    await delegated_memory.delegated_recall(
        sem, query="q", origin_channel="telegram", max_tokens=2000,
    )
    assert sem.recall_calls[0]["budget"] == "mid"


async def test_delegated_recall_propagates_unavailable():
    """Three-outcome contract: backend unavailability is NOT collapsed to ''
    (which callers could not tell from a genuine zero-hit recall)."""
    from semantic_memory import RecallUnavailable

    class _Down:
        async def recall_items(self, *a, **k): raise RecallUnavailable("http_504")
    with pytest.raises(RecallUnavailable):
        await delegated_memory.delegated_recall(
            _Down(), query="q", origin_channel="telegram", max_tokens=10,
        )


async def test_delegated_recall_wraps_unexpected_errors_as_unavailable():
    """Any unexpected backend error still surfaces as UNAVAILABLE (typed),
    never as a raw exception and never as a fake zero-hit ''."""
    from semantic_memory import RecallUnavailable

    class _Boom:
        async def recall_items(self, *a, **k): raise RuntimeError("x")
    with pytest.raises(RecallUnavailable):
        await delegated_memory.delegated_recall(
            _Boom(), query="q", origin_channel="telegram", max_tokens=10,
        )


async def test_delegated_recall_empty_query_raises_instead_of_faking_zero_hits():
    """#201: a blank query is an invalid call, not a zero-hit search.

    This test previously asserted the opposite — that a blank query returns
    `""`, the value reserved for a genuine zero-hit search. That is what made
    the bug look deliberate: `query_engager` accepted a blank question, got
    `""`, and reported `status="unknown"`, which its own tool description
    defines as "the memory was searched and holds nothing relevant". No search
    had run. Still no backend call, but the caller can no longer mistake the
    result for an answer about what Casa remembers.
    """
    sem = _Sem()
    with pytest.raises(ValueError, match="non-blank query"):
        await delegated_memory.delegated_recall(
            sem, query="   ", origin_channel="telegram", max_tokens=10,
        )
    assert sem.recall_calls == []


async def test_retain_delegated_classifies_each_item(monkeypatch):
    async def fake_classify(text): return "private" if "salary" in text else "friends"
    monkeypatch.setattr(delegated_memory, "classify_tier", fake_classify)
    sem = _Sem()
    await delegated_memory.retain_delegated(
        sem, origin_channel="telegram",
        turns=[
            RetainedTurn("what is my salary", _user()),
            RetainedTurn("your salary is 5000", _resident()),
        ],
    )
    items = sem.retain_calls[0]["items"]
    assert sem.retain_calls[0]["bank"] == "casa"
    # Task 10: each item carries exactly one tier tag + one reserved provenance tag.
    assert [i["tags"][0] for i in items] == ["private", "private"]
    assert all(
        sum(1 for t in i["tags"] if t.startswith("casa-source-")) == 1 for i in items
    )
    # Content-addressed ids: user turn keyed on user_peer, agent turn on persona
    # identity — distinct, and each namespaced by kind.
    doc_ids = [i["document_id"] for i in items]
    assert doc_ids[0].startswith("m-") and not doc_ids[0].startswith("m-a-")
    assert doc_ids[1].startswith("m-a-")
    assert len(set(doc_ids)) == 2
    # Provenance survives into metadata for reconstruction.
    assert "casa_source_v1" in items[0]["metadata"]


async def test_retain_delegated_voice_writes_nothing():
    """Pins INV-MEM-005. Red case demonstrated: adding "voice" to
    channel_policy._WRITABLE_CHANNELS fails this test."""
    sem = _Sem()
    await delegated_memory.retain_delegated(
        sem, origin_channel="voice",
        turns=[RetainedTurn("anything", _resident("house"))],
    )
    assert sem.retain_calls == []   # voice = recall-only (write-trust)


async def test_retain_delegated_skips_blank_turns(monkeypatch):
    async def fake_classify(text): return "friends"
    monkeypatch.setattr(delegated_memory, "classify_tier", fake_classify)
    sem = _Sem()
    await delegated_memory.retain_delegated(
        sem, origin_channel="telegram",
        turns=[RetainedTurn("   ", _user()), RetainedTurn("real", _resident())],
    )
    items = sem.retain_calls[0]["items"]
    assert [i["content"] for i in items] == ["real"]   # blank turn dropped


async def test_run_delegated_agent_reads_the_callers_real_provenance_off_origin_var() -> None:
    """Regression: before this fix, origin_var never carried speaker_provenance at
    all, so a delegated turn's caller_provenance was always None — this proves
    _run_delegated_agent's parent.get("speaker_provenance") sees the real value
    Agent._process now sets."""
    import agent as agent_mod

    caller = SpeakerProvenance(
        speaker_kind="resident", role_id="resident:butler", persona_id="casa/tina",
        persona_version="0.1.0", display_name="Tina", binding_digest="sha256:" + "1" * 64,
    )
    token = agent_mod.origin_var.set({
        "role": "butler", "channel": "telegram", "execution_role": "butler",
        "speaker_provenance": caller,
    })
    try:
        import tools

        snapshot = tools._snapshot_origin()
        assert snapshot["speaker_provenance"] == caller
    finally:
        agent_mod.origin_var.reset(token)


async def test_delegated_recall_renders_attribution_and_never_leaks_raw_tags():
    """Step 8: a delegated recall of an attributed hit renders the source but
    never leaks the reserved provenance tag or bare tier tokens."""
    class _AttrSem(_Sem):
        async def recall_items(self, bank, query, *, tags, max_tokens, clearance,
                               types=("world", "experience", "observation"),
                               tags_match="any", budget="mid"):
            self.recall_calls.append({"bank": bank, "query": query,
                                      "tags": sorted(tags), "budget": budget,
                                      "clearance": clearance})
            return (_hit("the invoice was paid", _resident("finance")),)

    out = await delegated_memory.delegated_recall(
        _AttrSem(), query="invoice?", origin_channel="telegram", max_tokens=2000,
    )
    assert "Finance previously said" in out
    assert "[source: resident:finance" in out
    assert "casa-source-" not in out
    for token in ("public", "friends", "family", "private"):
        assert token not in out


async def test_delegated_recall_hit_filter_applied_between_recall_and_render():
    """#215: an optional hit_filter scopes hits client-side BEFORE rendering
    (the executor-archive epoch filter's seam); the with_stats count reflects
    the FILTERED set so callers never overclaim."""
    sem = _Sem()
    out, n = await delegated_memory.delegated_recall(
        sem, query="build the invoice", origin_channel="telegram",
        max_tokens=2000, hit_filter=lambda hits: (), with_stats=True,
    )
    assert out == "" and n == 0

    out2 = await delegated_memory.delegated_recall(
        sem, query="build the invoice", origin_channel="telegram",
        max_tokens=2000, hit_filter=lambda hits: hits,
    )
    assert "prior fact" in out2
