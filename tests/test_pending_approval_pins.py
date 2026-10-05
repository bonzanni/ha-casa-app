"""#1207 under ruling #1252: the turns the withholding must NOT touch, and the
hook builds whose answers it must not change.

Regression pins beside the accepted red cases (``test_pending_approval_output``
and ``test_pending_approval_desk``), through the same real paths: a deny that
leaves no keyboard pending keeps the words; a consume of the same grant, read
at its OWN call's result even when its hook ran before the consumer reached
the denied call's result, releases them (critique r3, R3-1); an attempt that
never made the call is not cut by an abandoned attempt's deny; the words stay
in the turn's record; and the hook's answers are the same constants on every
build, with the record a no-op that cannot raise (F4).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import specialist_desk as sd
from authz_grants import (
    AuthzDeps, ChallengeCoordinator, GrantStore, make_resident_authz_hook,
    _DENY_DELIVERY_FAILED, _DENY_INACTIVE, _DENY_NOT_OPERATOR, _DENY_PENDING,
    _DENY_POSTED,
)
from output_boundary import TurnScope

from test_authz_hook import (
    _FakeChannel, _OriginCtx, _deny_reason, _engagement, _origin,
)
from test_pending_approval_desk import (  # noqa: F401 — the desk harness
    _Scripted, _ask as _desk_ask, _call as _desk_call, _result as _desk_result,
    _text as _desk_text, desk, env,
)
from test_pending_approval_desk import LABEL, OPERATOR as DESK_OPERATOR, _reply
from test_pending_approval_output import (  # noqa: F401 — the resident harness
    ARGS, PROTECTED, TAIL, TOOL, _Factory, _Gate, _ask, _call, _drive, _key,
    _msg, _result, _text, turn,
)

pytestmark = pytest.mark.asyncio


# --- a deny that leaves no keyboard pending keeps the words ----------------------

@pytest.mark.parametrize("deny", ["delivery_failed", "inactive", "not_operator"])
async def test_other_denies_deliver_the_words_after_the_call(turn, monkeypatch, deny):
    agent, channel, hook, _grants = turn
    import verdict_broker
    post_entered, post_gate = asyncio.Event(), asyncio.Event()
    real_post = channel.post_dm_keyboard

    async def _post(**kw):
        if deny == "delivery_failed":
            raise RuntimeError("post boom")
        post_entered.set()
        await post_gate.wait()
        return await real_post(**kw)

    monkeypatch.setattr(channel, "post_dm_keyboard", _post)
    if deny == "not_operator":
        monkeypatch.setattr(channel, "_user_id_is_operator", lambda _uid: False)

    async def _hook_step():
        task = asyncio.ensure_future(_ask(hook, "deny-1"))
        if deny == "inactive":
            await post_entered.wait()
            # /new cancels the chat's authz scope while the post is in flight
            verdict_broker.BROKER.cancel_scope(
                namespace="resident_ask", scope="authz:42", reason="new_session")
        post_gate.set()
        return await task

    gate = _Gate()
    script = [_text("BEFORE"), _call("deny-1"), gate.step(_hook_step),
              _result("deny-1"), _text(TAIL)]
    await _drive(agent, monkeypatch, _Factory([script]), _msg("dm"), gate)

    want = {"delivery_failed": _DENY_DELIVERY_FAILED, "inactive": _DENY_INACTIVE,
            "not_operator": _DENY_NOT_OPERATOR}[deny]
    assert [_deny_reason(a) for a in gate.answers] == [want]
    assert channel.tokens == ["BEFORE", "BEFORE\n\n" + TAIL]
    assert channel.final_texts() == ["BEFORE\n\n" + TAIL]


# --- R3-1: a consume releases the words at its own result, however early its hook ran

async def test_a_same_grant_consume_releases_the_words_at_its_own_result(turn, monkeypatch):
    agent, channel, hook, grants = turn
    gate = _Gate()

    async def _hooks():
        denied = await _ask(hook, "deny-1")
        grants.mint(_key())                    # the operator approved
        consumed = await _ask(hook, "retry-1")  # the retry's hook, ahead of the consumer
        return [denied, consumed]

    script = [_text("BEFORE"), _call("deny-1"), gate.step(_hooks),
              _result("deny-1"), _text("S"), _call("retry-1"),
              _result("retry-1", error=False), _text("Added it.")]
    await _drive(agent, monkeypatch, _Factory([script]), _msg("dm"), gate)

    ((denied, consumed),) = gate.answers
    assert _deny_reason(denied) == _DENY_POSTED and consumed == {}
    assert channel.tokens == ["BEFORE", "BEFORE\n\nS\n\nAdded it."]
    assert channel.final_texts() == ["BEFORE\n\nS\n\nAdded it."]


# --- an abandoned attempt's deny does not cut the attempt that answered -----------

async def test_a_retried_attempt_that_never_made_the_call_is_delivered(turn, monkeypatch):
    agent, channel, hook, _grants = turn
    CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
    gate = _Gate()
    attempt_1 = [_call("deny-1"), gate.step(lambda: _ask(hook, "deny-1")),
                 _result("deny-1"), CLIConnectionError("reset")]
    attempt_2 = [_text("Done.")]
    factory = _Factory([attempt_1, attempt_2])
    await _drive(agent, monkeypatch, factory, _msg("dm"), gate)

    assert len(factory.clients) == 2
    assert channel.tokens == ["Done."]
    assert channel.final_texts() == ["Done."]


# --- the words stay in the turn's own record ---------------------------------------

async def test_withheld_words_stay_in_the_turn_report(turn, monkeypatch):
    agent, channel, hook, _grants = turn
    reports = []
    real_process = agent._process

    async def _process(msg, on_token=None, turn_report=None, **kw):
        text = await real_process(msg, on_token=on_token, turn_report=turn_report, **kw)
        reports.append((text, dict(turn_report or {})))
        return text

    monkeypatch.setattr(agent, "_process", _process)
    gate = _Gate()
    script = [_text("BEFORE"), _call("deny-1"),
              gate.step(lambda: _ask(hook, "deny-1")), _result("deny-1"), _text(TAIL)]
    await _drive(agent, monkeypatch, _Factory([script]), _msg("dm"), gate)

    ((text, report),) = reports
    assert text == "BEFORE\n\n" + TAIL
    assert tuple(report["reply_messages"]) == ("BEFORE", TAIL)
    assert report["approval_cut"] == 1
    assert channel.final_texts() == ["BEFORE"]


# --- the desk: a consume of the same grant releases the specialist's words ---------

async def test_desk_deny_then_consume_then_prose_is_posted_whole(desk):
    async def _consume():
        desk.grants.mint(_desk_key())
        desk.answers.append(await desk.hook(
            {"tool_name": TOOL, "tool_input": dict(ARGS)}, "retry-1", {}))

    from test_pending_approval_desk import _Step
    _Scripted.load(_desk_text("BEFORE"), _desk_call("deny-1"), _desk_ask(desk, "deny-1"),
                   _Step(_consume), _desk_result("deny-1"), _desk_call("retry-1"),
                   _desk_result("retry-1", error=False), _desk_text("Filed it."))
    task = asyncio.create_task(_reply(desk, text="file the March invoice"))
    await asyncio.wait_for(desk.hook_returned.wait(), 10)
    desk.release_result.set()
    await asyncio.wait_for(task, 10)

    assert _deny_reason(desk.answers[0]) == _DENY_POSTED and desk.answers[1] == {}
    assert [str(m) for m, _ in desk.channel.replies] == [LABEL + "\nBEFORE\n\nFiled it."]
    assert sd.prompt_prefix(DESK_OPERATOR) == (
        "(front desk) " + LABEL + " answered your reply (1 page).\n\n")


def _desk_key():
    from authz_grants import GrantKey, canonical_args_hash
    return GrantKey(operator_id=DESK_OPERATOR, chat_id=DESK_OPERATOR,
                    enforcement_role="finance", artifact_id="artifact-1",
                    tool_name=TOOL, args_hash=canonical_args_hash(ARGS),
                    engagement_id="")


# --- F4: the hook's answers on every build, and a record that cannot raise --------

def _hook_for(role, channel, grants):
    coord = ChallengeCoordinator()
    return make_resident_authz_hook(
        role, PROTECTED,
        lambda: AuthzDeps(channel=channel, grants=grants, challenges=coord))


async def _posted_then_pending(hook):
    outs = []
    for call_id in ("c-1", "c-2"):
        outs.append(await hook({"tool_name": TOOL, "tool_input": dict(ARGS)}, call_id, {}))
    return [_deny_reason(o) for o in outs]


@pytest.mark.parametrize("kind", ["specialist", "plugin"])
async def test_engagement_and_plugin_job_answers_are_unchanged_and_never_reach_the_ambient_scope(
        monkeypatch, kind):
    """The specialist/engagement build (tools.py `_build_specialist_options`)
    and the plugin-job build (`_build_plugin_job_options`) both build this hook
    through `make_resident_authz_hook` (TestWiring in test_authz_hook.py). Under
    an engagement the record goes only to the running turn's holder of the
    client whose hook it is (#1207; tests/test_pending_approval_topic.py) —
    here no holder is bound, so it goes nowhere — and never to the ambient
    turn's scope."""
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    ambient = TurnScope(id="t", cid="c", role="finance", display_name="F",
                        channel="telegram", message_type="channel_in")
    rec = _engagement(kind=kind, role_or_type="finance")
    hook = _hook_for("finance", _FakeChannel(), GrantStore())
    with _OriginCtx(_origin(turn_scope=ambient), engagement=rec):
        reasons = await _posted_then_pending(hook)
    assert reasons == [_DENY_POSTED, _DENY_PENDING]
    assert ambient.approvals == {}


async def test_a_record_that_raises_changes_no_answer(monkeypatch):
    """The record is written on the turn's own scope; if writing it fails,
    the hook still answers exactly as before — POSTED, PENDING, and allow on a
    consumed grant — never the internal-error deny."""
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())

    def _boom(*_a, **_k):
        raise RuntimeError("record failed")

    monkeypatch.setattr(TurnScope, "note_approval", _boom)
    scope = TurnScope(id="t", cid="c", role="finance", display_name="F",
                      channel="telegram", message_type="channel_in")
    grants = GrantStore()
    hook = _hook_for("finance", _FakeChannel(), grants)
    with _OriginCtx(_origin(turn_scope=scope)):
        reasons = await _posted_then_pending(hook)
        from test_authz_hook import _expected_key
        grants.mint(_expected_key(dict(ARGS)))
        allowed = await hook({"tool_name": TOOL, "tool_input": dict(ARGS)}, "c-3", {})
    assert reasons == [_DENY_POSTED, _DENY_PENDING]
    assert allowed == {}


# --- a repeated deny of the same grant keeps the EARLIEST cut ----------------------

async def test_a_repeated_deny_of_the_same_grant_keeps_the_first_cut(turn, monkeypatch):
    """The model retries the same call before the operator answered: the
    second answer is PENDING, and the words between the two calls were written
    after the first protected call — withheld like the rest."""
    agent, channel, hook, _grants = turn
    gate = _Gate()

    async def _hooks():
        return [await _ask(hook, "deny-1"), await _ask(hook, "deny-2")]

    script = [_text("BEFORE"), _call("deny-1"), gate.step(_hooks), _result("deny-1"),
              _text("MID"), _call("deny-2"), _result("deny-2"), _text(TAIL)]
    await _drive(agent, monkeypatch, _Factory([script]), _msg("dm"), gate)

    ((first, second),) = gate.answers
    assert [_deny_reason(first), _deny_reason(second)] == [_DENY_POSTED, _DENY_PENDING]
    assert channel.tokens == ["BEFORE"]
    assert channel.final_texts() == ["BEFORE"]
