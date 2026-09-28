"""#1079 — the discharge side of every arm of the chosen-silence gate.

``tests/test_chosen_silence_announcements.py`` holds the accepted red cases and
``tests/test_chosen_silence_retains.py`` the retain direction; this module pins
the remaining DISCHARGE arms (each commitment kind confirmed, then silence), the
channel-without-teardown case, exactly-once on the synthesized message, and the
admission facts the gate reads — ``Admitted.chosen_silence`` and the
``TurnScope.operator_sends`` record — at unit level.
"""
from __future__ import annotations

import inspect

import pytest

from output_boundary import (
    IntentKind, SILENCE_SENTINEL, TurnScope, casa_text,
)

try:
    from tests.test_chosen_silence_announcements import (
        _Counted, _Factory, _Hook, _TelegramStub, _handle, _live_notice,
        _mk_assistant,
    )
    from tests.test_chosen_silence_retains import (
        PDF, _MediaStub, _drop, _silent_after, wired,  # noqa: F401 — fixture
    )
except ImportError:  # pragma: no cover — run from inside tests/
    from test_chosen_silence_announcements import (
        _Counted, _Factory, _Hook, _TelegramStub, _handle, _live_notice,
        _mk_assistant,
    )
    from test_chosen_silence_retains import (
        PDF, _MediaStub, _drop, _silent_after, wired,  # noqa: F401 — fixture
    )

# asyncio_mode = auto (pytest.ini): the sync unit tests below carry no mark.


def _discharged_once(seen, counted):
    assert counted.count == 1
    assert len(seen.synth) == 1
    assert [(m is seen.synth[0], e) for m, e in seen.acks] == [(True, None)]
    assert seen.synth[0].on_delivery is None


# ---------------------------------------------------------------------------
# Admission: the facts the gate reads
# ---------------------------------------------------------------------------


def _scope() -> TurnScope:
    return TurnScope(id="t", cid="c", role="assistant", display_name="Test",
                     channel="telegram", message_type="request")


@pytest.mark.parametrize("text,chosen", [
    (SILENCE_SENTINEL, True),
    (f"  {SILENCE_SENTINEL}\n{SILENCE_SENTINEL}  ", True),
    ("   \n\t", False),
])
def test_a_suppressed_final_reply_says_whether_the_silence_was_chosen(
    text, chosen,
):
    admitted = _scope().admit(IntentKind.FINAL_REPLY, text)
    assert admitted.suppressed is True
    assert admitted.chosen_silence is chosen
    assert str(admitted) == ""


def test_a_recant_is_not_suppressed_and_not_a_chosen_silence():
    admitted = _scope().admit(IntentKind.FINAL_REPLY,
                              f"{SILENCE_SENTINEL} actually, here it is")
    assert admitted.suppressed is False
    assert admitted.chosen_silence is False


@pytest.mark.parametrize("kind", [IntentKind.DISCRETE, IntentKind.CAPTION,
                                  IntentKind.KEYBOARD])
def test_each_operator_bound_admission_opens_one_undelivered_record(kind):
    scope = _scope()
    assert scope.operator_sends_delivered is True       # nothing committed
    admitted = scope.admit(kind, "for the operator")
    assert len(scope.operator_sends) == 1
    assert scope.operator_sends_delivered is False
    # A re-rendered body is the same commitment.
    admitted.with_text("shorter").mark_delivered()
    assert len(scope.operator_sends) == 1
    assert scope.operator_sends_delivered is True


@pytest.mark.parametrize("kind", [IntentKind.FINAL_REPLY, IntentKind.STORED,
                                  IntentKind.STREAM_UPDATE])
def test_other_admissions_open_no_record(kind):
    scope = _scope()
    scope.admit(kind, "not a discrete commitment")
    assert scope.operator_sends == []


def test_casa_text_carries_no_commitment():
    text = casa_text("Casa's own card")
    assert text.send is None
    text.mark_delivered()        # a no-op, never an error


def test_a_delegates_commitments_land_on_the_launching_scope():
    parent = _scope()
    child = TurnScope.for_child(parent, "a launch note")
    child.admit(IntentKind.DISCRETE, "the delegate's report")
    assert len(parent.operator_sends) == 1
    assert parent.operator_sends_delivered is False


def test_the_gate_reads_admission_not_the_text():
    """INV-OUT-006's rule, extended: the silence decision is admission's —
    ``handle_message`` never inspects the sentinel itself."""
    import agent
    source = inspect.getsource(agent.Agent.handle_message)
    assert "SILENCE_SENTINEL" not in source
    assert "<silent/>\" in" not in source
    assert "admitted.chosen_silence" in source


# ---------------------------------------------------------------------------
# Each commitment kind, confirmed, then silence: discharged once
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caption", [None, "your invoice"],
                         ids=["captionless", "captioned"])
async def test_a_delivered_media_send_then_silence_discharges(
    wired, monkeypatch, caption,
):
    agent, ob, install = wired
    stub = install(_MediaStub())
    counted = _Counted()
    path = _drop(ob, "invoice.pdf", PDF)

    async def _media():
        import tools
        args = {"path": path, "kind": "document"}
        if caption is not None:
            args["caption"] = caption
        await tools.send_media.handler(args)

    _resp, seen = await _handle(agent, _live_notice(counted),
                                _silent_after(_Hook(_media)), monkeypatch)

    assert stub.send_media.await_count == 1
    _discharged_once(seen, counted)


async def test_a_delivered_media_send_survives_a_claim_cleanup_failure(
    wired, monkeypatch,
):
    agent, ob, install = wired
    stub = install(_MediaStub())
    counted = _Counted()
    path = _drop(ob, "invoice.pdf", PDF)
    monkeypatch.setattr(ob, "remove_claim",
                        lambda _claim: (_ for _ in ()).throw(OSError("busy")))

    async def _media():
        import tools
        await tools.send_media.handler({"path": path, "kind": "document"})

    _resp, seen = await _handle(agent, _live_notice(counted),
                                _silent_after(_Hook(_media)), monkeypatch)

    assert stub.send_media.await_count == 1
    _discharged_once(seen, counted)


async def test_a_delegates_delivered_send_then_silence_discharges(
    wired, monkeypatch,
):
    agent, _ob, install = wired
    install(_TelegramStub())
    counted = _Counted()

    async def _child_delivers():
        import tools
        parent = tools._current_scope(tools._snapshot_origin())
        child = TurnScope.for_child(parent, "")
        child.admit(IntentKind.DISCRETE, "the delegate's report").mark_delivered()

    _resp, seen = await _handle(agent, _live_notice(counted),
                                _silent_after(_Hook(_child_delivers)),
                                monkeypatch)

    _discharged_once(seen, counted)


@pytest.mark.parametrize("post,delivered", [
    ({"post_result": 55}, True),
    ({"post_result": None}, False),
    ({"post_raises": True}, False),
], ids=["posted", "post-none", "post-raises"])
async def test_an_authorization_challenge_commits_its_body_to_the_turn(
    monkeypatch, post, delivered,
):
    """The real challenge coordinator admits its body — which interpolates
    the model's own tool arguments — as KEYBOARD text on the raising turn's
    scope, and only the broker's own "posted" test confirms it."""
    import agent as agent_mod
    try:
        from tests.test_authz_grants import _create, _fresh_env
    except ImportError:  # pragma: no cover
        from test_authz_grants import _create, _fresh_env

    scope = _scope()
    token = agent_mod.origin_var.set({"turn_scope": scope})
    try:
        _broker, coord, channel = _fresh_env(monkeypatch)
        for attr, value in post.items():
            setattr(channel, attr, value)
        _key, handle = _create(coord, channel)
        await handle.settled_post()
    finally:
        agent_mod.origin_var.reset(token)

    assert len(channel.posts) == 1
    assert [r.delivered for r in scope.operator_sends] == [delivered]
    assert scope.operator_sends_delivered is delivered


# ---------------------------------------------------------------------------
# The branch itself
# ---------------------------------------------------------------------------


async def test_a_channel_without_the_teardown_hook_still_discharges(
    wired, monkeypatch,
):
    agent, _ob, install = wired
    stub = _TelegramStub()
    del stub.turn_finished
    install(stub)
    counted = _Counted()

    _resp, seen = await _handle(agent, _live_notice(counted),
                                _silent_after(), monkeypatch)

    _discharged_once(seen, counted)


async def test_a_raising_teardown_still_discharges(wired, monkeypatch):
    agent, _ob, install = wired
    stub = install(_TelegramStub())
    stub.turn_finished.side_effect = RuntimeError("typing loop gone")
    counted = _Counted()

    _resp, seen = await _handle(agent, _live_notice(counted),
                                _silent_after(), monkeypatch)

    assert stub.turn_finished.await_count == 1
    _discharged_once(seen, counted)


async def test_the_silent_discharge_fires_at_most_once(wired, monkeypatch):
    agent, _ob, install = wired
    install(_TelegramStub())
    counted = _Counted()

    _resp, seen = await _handle(agent, _live_notice(counted),
                                _silent_after(), monkeypatch)
    await agent._ack_delivery(seen.synth[0])        # the same message again

    assert counted.count == 1


async def test_repeated_sentinels_are_one_chosen_silence(wired, monkeypatch):
    agent, _ob, install = wired
    stub = install(_TelegramStub())
    counted = _Counted()
    factory = _Factory([[_mk_assistant(
        f"{SILENCE_SENTINEL}\n\n  {SILENCE_SENTINEL} ")]])

    _resp, seen = await _handle(agent, _live_notice(counted), factory,
                                monkeypatch)

    assert stub.final_sends() == 0
    _discharged_once(seen, counted)


async def test_a_cancel_during_teardown_retains(wired, monkeypatch):
    import asyncio
    agent, _ob, install = wired
    stub = install(_TelegramStub())
    stub.turn_finished.side_effect = asyncio.CancelledError()
    counted = _Counted()

    with pytest.raises(asyncio.CancelledError):
        await _handle(agent, _live_notice(counted), _silent_after(),
                      monkeypatch)

    assert counted.count == 0
