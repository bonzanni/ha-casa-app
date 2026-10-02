"""S3 §6: what the resident's conversation sees of a specialist's post — one
Casa-authored, BODY-FREE echo line per proven delivery, carried by the vehicle
the resident already receives (a sync delegation's returned text, a finished
engagement's or job's terminal notice). The echo carries only Casa-derived
metadata — the label, the kind, the page count or media kind — never the
body, the caption or the file name.

Mechanism: on proven delivery the broker APPENDS one event to a ledger keyed
by the call's owner (the engagement id the identity carries, or the sync
delegation's id the child origin carries as advisory identity metadata); the
composer drains the owner's list atomically, so a post is echoed once.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import result_broker as rb
import tools as tools_mod
from authz_grants import GrantIdentity, resolve_grant_identity
from channels import DeliveryOutcome
from test_operator_message_delivery import (
    BODY, LABEL, REPORT, SLOT, _Manager, _Recorder, _identity, _map, _open,
    _post, _replacement, _store,
)
from test_result_broker import _Origin


@pytest.fixture
def names(monkeypatch):
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "finance": SimpleNamespace(character=SimpleNamespace(name="Finance"))})


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(tools_mod, "_channel_manager", _Manager(rec), raising=False)
    return rec


@pytest.fixture(autouse=True)
def fresh_ledger(monkeypatch):
    ledger = rb.PostLedger()
    monkeypatch.setattr(rb, "POSTS", ledger)
    return ledger


def _event(**over) -> rb.PostEvent:
    base = dict(tool_use_id="call-1", plugin="probe", slot=SLOT, label=LABEL,
                pages=1, media_kind=None)
    base.update(over)
    return rb.PostEvent(**base)


# --- the ledger ---------------------------------------------------------------

def test_events_append_in_order_and_drain_once_atomically():
    ledger = rb.PostLedger()
    ledger.record("eng-1", _event(tool_use_id="a"))
    ledger.record("eng-1", _event(tool_use_id="b", pages=3))
    ledger.record("eng-2", _event(tool_use_id="c"))
    assert ledger.drain("eng-1") == [_event(tool_use_id="a"), _event(tool_use_id="b", pages=3)]
    assert ledger.drain("eng-1") == []                       # once
    assert ledger.drain("eng-2") == [_event(tool_use_id="c")]
    assert ledger.drain("nobody") == [] and ledger.drain("") == []


def test_two_tools_delivering_the_same_slot_name_are_two_events():
    """A list, never a set: the contract forbids a duplicate slot only within
    one tool, and two proven posts are two echo lines."""
    ledger = rb.PostLedger()
    ledger.record("eng-1", _event(tool_use_id="a"))
    ledger.record("eng-1", _event(tool_use_id="b"))
    assert len(ledger.drain("eng-1")) == 2


def test_the_ledger_is_bounded_by_owner_count():
    ledger = rb.PostLedger(max_owners=2)
    for owner in ("o1", "o2", "o3"):
        ledger.record(owner, _event())
    assert ledger.drain("o1") == []                          # evicted, oldest first
    assert len(ledger.drain("o2")) == 1 and len(ledger.drain("o3")) == 1


# --- the lines -----------------------------------------------------------------

def test_echo_lines_are_body_free_and_bounded():
    lines = rb.echo_lines([
        _event(pages=2), _event(pages=1),
        _event(pages=None, media_kind="document"),
        _event(pages=None, media_kind="photo"),
        _event(pages=None, media_kind="audio"),
    ])
    assert lines == [
        "📊 Finance posted to your chat (2 pages).",
        "📊 Finance posted to your chat (1 page).",
        "📊 Finance posted a document to your chat.",
        "📊 Finance posted a photo to your chat.",
        "📊 Finance posted an audio file to your chat.",
    ]
    assert rb.echo_lines([_event(pages=None, media_kind=k) for k in ("voice", "zip", "text")]) == [
        "📊 Finance posted a voice message to your chat.",
        "📊 Finance posted a zip archive to your chat.",
        "📊 Finance posted a text file to your chat.",
    ]
    assert rb.echo_lines([]) == []


def test_echo_lines_are_at_most_five_then_a_count_and_each_within_the_bound():
    lines = rb.echo_lines([_event(tool_use_id=str(i), pages=i + 1) for i in range(7)])
    assert len(lines) == 6
    assert lines[:5] == [f"📊 Finance posted to your chat ({i + 1} page{'s' if i else ''})."
                         for i in range(5)]
    assert lines[5] == "…and 2 more."
    long_label = "📊 " + "N" * 300
    (line,) = rb.echo_lines([_event(label=long_label, pages=1)])
    assert len(line) <= rb.ECHO_LINE_MAX == 120
    assert line.endswith(" posted to your chat (1 page).")
    assert rb.ECHO_MAX_LINES == 5


# --- recorded by the hook, keyed by the owner -----------------------------------

@pytest.mark.asyncio
async def test_a_proven_message_delivery_records_one_event_keyed_by_the_engagement(recorder, names, fresh_ledger):
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store, identity=_identity(engagement_id="eng-1"))
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=BODY)
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert json.loads(_replacement(out))["casa_delivery"]["status"] == "delivered"
    events = fresh_ledger.drain("eng-1")
    assert events == [rb.PostEvent(tool_use_id="call-1", plugin="probe", slot=SLOT,
                                   label=LABEL, pages=1, media_kind=None)]
    assert "SUMMARY-9c1d" not in repr(events)
    assert fresh_ledger.drain("eng-1") == []


@pytest.mark.asyncio
async def test_a_withheld_delivery_records_nothing(recorder, names, fresh_ledger):
    recorder.outcome = DeliveryOutcome.NOT_DELIVERED
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store, identity=_identity(engagement_id="eng-1"))
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=BODY)
    await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert fresh_ledger.drain("eng-1") == []


@pytest.mark.asyncio
async def test_a_sync_delegations_identity_carries_the_delegation_id_as_advisory_owner(recorder, names, fresh_ledger):
    delegated = {"role": "assistant", "execution_role": "finance", "channel": "telegram",
                 "chat_id": 42, "user_id": 100, "message_type": "channel_in",
                 "source": "telegram", "_delegation_id": "d-1"}
    with _Origin(delegated):
        identity, why = resolve_grant_identity("finance", artifact_id="5" * 64)
    assert why is None and identity.delegation_id == "d-1"
    # advisory: takes no part in equality, so a reference minted on this turn
    # compares equal to the identity derived later without it
    assert identity == GrantIdentity(operator_id=100, chat_id=42, enforcement_role="finance",
                                     artifact_id="5" * 64, engagement_id="")
    with _Origin({**delegated, "_delegation_id": 7}):
        identity2, _ = resolve_grant_identity("finance", artifact_id="5" * 64)
    assert identity2.delegation_id == ""                     # only a string is one
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store, identity=identity)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=BODY)
    await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert [e.tool_use_id for e in fresh_ledger.drain("d-1")] == ["call-1"]


# --- the vehicles ----------------------------------------------------------------

def test_the_helper_appends_the_drained_lines_after_the_text_or_leaves_it(fresh_ledger):
    assert tools_mod._with_post_echo("answer", "none") == "answer"
    fresh_ledger.record("d-1", _event(pages=2))
    fresh_ledger.record("d-1", _event(tool_use_id="call-2", pages=None, media_kind="text"))
    assert tools_mod._with_post_echo("answer", "d-1") == (
        "answer\n\n📊 Finance posted to your chat (2 pages).\n"
        "📊 Finance posted a text file to your chat.")
    assert tools_mod._with_post_echo("answer", "d-1") == "answer"     # drained
    assert tools_mod._with_post_echo("", "none") == ""


@pytest.mark.asyncio
async def test_a_sync_delegations_returned_text_carries_the_echo_after_the_answer(tmp_path, monkeypatch, fresh_ledger):
    from test_delegate_to_agent import (
        ChannelManager, MessageBus, SpecialistRegistry, _caller_cfg, _origin,
        _seed_specialist_dir, _use_synthetic_roles_dir, _with_origin,
    )
    from tools import delegate_to_agent, init_tools
    specialists = tmp_path / "ex"
    specialists.mkdir()
    _seed_specialist_dir(specialists, "finance", enabled=True)
    _use_synthetic_roles_dir(monkeypatch, tmp_path, "finance")
    reg = SpecialistRegistry(str(specialists), tombstone_path=str(tmp_path / "del.json"))
    reg.load()
    init_tools(ChannelManager(), MessageBus(), reg,
               agent_role_map={"assistant": _caller_cfg(delegates=("finance",))})

    async def _fake_bounded(cfg, task_text, context_text, resolution=None,
                            output_format=None):
        # the plugin's proven post, recorded under the delegation this run is
        owner = tools_mod._delegation_quota_key.get()
        assert owner
        fresh_ledger.record(owner, _event(pages=2))
        return tools_mod.DelegatedOutput(text="invoice drafted")

    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", _fake_bounded)
    result = await _with_origin(
        delegate_to_agent.handler({"agent": "finance", "task": "draft invoice",
                                   "context": "", "mode": "sync"}),
        _origin())
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "ok"
    assert payload["text"] == "invoice drafted\n\n📊 Finance posted to your chat (2 pages)."
    assert fresh_ledger.drain(payload["delegation_id"]) == []


@pytest.mark.asyncio
async def test_a_finished_engagements_notice_carries_the_echo_after_its_text(tmp_path, fresh_ledger):
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="t", origin={"role": "assistant", "channel": "telegram", "chat_id": 42},
        topic_id=42)
    bus = MagicMock()
    bus.notify = AsyncMock()
    init_tools(channel_manager=None, bus=bus, specialist_registry=MagicMock(),
               mcp_registry=MagicMock(), trigger_registry=MagicMock(),
               engagement_registry=reg)
    fresh_ledger.record(rec.id, _event(pages=None, media_kind="document"))
    await _finalize_engagement(rec, outcome="completed", text="all good",
                               artifacts=[], next_steps=[], driver=None)
    assert bus.notify.await_count == 1
    complete = bus.notify.await_args.args[0].content
    assert complete.text == "all good\n\n📊 Finance posted a document to your chat."
    assert fresh_ledger.drain(rec.id) == []


@pytest.mark.asyncio
async def test_a_completed_async_delegations_notice_carries_the_echo(tmp_path, fresh_ledger):
    """The degraded-sync and async arms announce through the completion
    callback's notice — the same vehicle, the same line."""
    import asyncio
    from specialist_registry import DelegationComplete, DelegationRecord
    from test_delegate_to_agent import ChannelManager, MessageBus, SpecialistRegistry, _origin
    reg = SpecialistRegistry(str(tmp_path / "ex"), tombstone_path=str(tmp_path / "del.json"))
    bus = MessageBus()
    bus.register("assistant", None)
    tools_mod.init_tools(ChannelManager(), bus, reg)
    record = DelegationRecord(id="delegation-echo", agent="finance",
                              started_at=asyncio.get_running_loop().time(),
                              origin=_origin())
    await reg.register_delegation(record)
    fresh_ledger.record("delegation-echo", _event(pages=3))

    async def _done():
        return tools_mod.DelegatedOutput(text="done")

    task = asyncio.create_task(_done())
    tools_mod._attach_completion_callback(task, record)
    await task
    _priority, _sequence, message = await asyncio.wait_for(bus.queues["assistant"].get(), 5)
    assert isinstance(message.content, DelegationComplete)
    assert message.content.text == "done\n\n📊 Finance posted to your chat (3 pages)."
    assert fresh_ledger.drain("delegation-echo") == []


# --- a post is echoed per PROVEN delivery, whatever became of the work -----------

def _sync_setup(tmp_path, monkeypatch):
    from test_delegate_to_agent import (
        ChannelManager, MessageBus, SpecialistRegistry, _caller_cfg,
        _seed_specialist_dir, _use_synthetic_roles_dir,
    )
    from tools import init_tools
    specialists = tmp_path / "ex"
    specialists.mkdir()
    _seed_specialist_dir(specialists, "finance", enabled=True)
    _use_synthetic_roles_dir(monkeypatch, tmp_path, "finance")
    reg = SpecialistRegistry(str(specialists), tombstone_path=str(tmp_path / "del.json"))
    reg.load()
    init_tools(ChannelManager(), MessageBus(), reg,
               agent_role_map={"assistant": _caller_cfg(delegates=("finance",))})


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["raises", "aborted"])
async def test_a_sync_delegation_that_fails_after_a_proven_post_still_echoes_it(tmp_path, monkeypatch, fresh_ledger, ending):
    from test_delegate_to_agent import _origin, _with_origin
    from tools import delegate_to_agent
    _sync_setup(tmp_path, monkeypatch)

    async def _fake_bounded(cfg, task_text, context_text, resolution=None,
                            output_format=None):
        fresh_ledger.record(tools_mod._delegation_quota_key.get(), _event(pages=2))
        if ending == "raises":
            raise RuntimeError("the specialist crashed after posting")
        return tools_mod.DelegatedOutput(text="", run_subtype="error_max_turns",
                                         result_message_seen=True)

    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", _fake_bounded)
    result = await _with_origin(
        delegate_to_agent.handler({"agent": "finance", "task": "draft invoice",
                                   "context": "", "mode": "sync"}),
        _origin())
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["message"].endswith("\n\n📊 Finance posted to your chat (2 pages).")
    assert fresh_ledger.drain(payload["delegation_id"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["raises", "aborted"])
async def test_a_completion_notice_for_failed_work_still_echoes_a_proven_post(tmp_path, fresh_ledger, ending):
    import asyncio
    from specialist_registry import DelegationComplete, DelegationRecord
    from test_delegate_to_agent import ChannelManager, MessageBus, SpecialistRegistry, _origin
    reg = SpecialistRegistry(str(tmp_path / "ex"), tombstone_path=str(tmp_path / "del.json"))
    bus = MessageBus()
    bus.register("assistant", None)
    tools_mod.init_tools(ChannelManager(), bus, reg)
    record = DelegationRecord(id="delegation-fail", agent="finance",
                              started_at=asyncio.get_running_loop().time(),
                              origin=_origin())
    await reg.register_delegation(record)
    fresh_ledger.record("delegation-fail", _event(pages=None, media_kind="text"))

    async def _done():
        if ending == "raises":
            raise RuntimeError("crashed after posting")
        return tools_mod.DelegatedOutput(text="", run_subtype="error_max_turns",
                                         result_message_seen=True)

    task = asyncio.create_task(_done())
    tools_mod._attach_completion_callback(task, record)
    try:
        await task
    except RuntimeError:
        pass
    _priority, _sequence, message = await asyncio.wait_for(bus.queues["assistant"].get(), 5)
    assert isinstance(message.content, DelegationComplete)
    assert message.content.status == "error"
    assert message.content.message.endswith("\n\n📊 Finance posted a text file to your chat.")
    assert fresh_ledger.drain("delegation-fail") == []
