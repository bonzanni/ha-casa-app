"""#1079 — the silences that must NOT discharge an announcement.

The companion of ``tests/test_chosen_silence_announcements.py`` (the red cases,
which pin the discharge). Every case here ends a synthesized announcement turn
in something that looks like silence, and each must leave the obligation owed:
the counts are acknowledgements through ``Agent._ack_delivery`` and the
notice's own callback, never a status.

Most of these are GREEN before #1079 too — the old silent branch acknowledged
nothing at all — so they are regression pins on the gate's conjuncts, and the
mutation checks in the change's record are what show each reaches its target.
Same harness as the red cases: the real ``handle_message`` → synthesis →
``_process`` with a scripted SDK client, never a patched ``_process``.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from channels import DeliveryOutcome
from output_boundary import (
    Admitted, IntentKind, SILENCE_SENTINEL, TurnScope, strips_to_silence,
)

try:
    from tests.test_chosen_silence_announcements import (
        _Counted, _Factory, _Hook, _TelegramStub, _handle, _live_notice,
        _make_agent, _mk_assistant,
    )
except ImportError:  # pragma: no cover — run from inside tests/
    from test_chosen_silence_announcements import (
        _Counted, _Factory, _Hook, _TelegramStub, _handle, _live_notice,
        _make_agent, _mk_assistant,
    )

pytestmark = [pytest.mark.asyncio]


class _MediaStub(_TelegramStub):
    def __init__(self, send_outcome=DeliveryOutcome.DELIVERED, media=None) -> None:
        super().__init__(send_outcome)
        self.send_media = media if media is not None else AsyncMock()


@pytest.fixture
async def wired(tmp_path):
    """A resident whose channel manager is also the tools' — so a tool send in
    the scripted turn reaches the same stub — plus a real outbox."""
    import plugin_outbox
    import tools

    agent = _make_agent(tmp_path)
    ob = plugin_outbox.init_outbox(str(tmp_path / "plugin-outbox"))

    def _install(stub):
        agent._channel_manager.register(stub)
        tools.init_tools(
            channel_manager=agent._channel_manager, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        )
        return stub

    try:
        yield agent, ob, _install
    finally:
        ob.close()
        plugin_outbox._OUTBOX = None
        await agent.aclose()


def _drop(outbox, name: str, data: bytes) -> str:
    path = os.path.join(outbox._root_realpath, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


PDF = b"%PDF-1.7\n" + b"x" * 100


def _assert_retained(stub, seen, counted, *, final_sends=0):
    # The notice's callback never ran, and the synthesized message still
    # carries it — the obligation is owed at the next boot.
    assert counted.count == 0
    assert len(seen.synth) == 1
    assert seen.synth[0].on_delivery is counted
    assert stub.final_sends() == final_sends


def _silent_after(*steps):
    return _Factory([[*steps, _mk_assistant(SILENCE_SENTINEL)]])


async def _send_message(text: str = "sent-by-tool"):
    import tools
    return await tools.send_message.handler({"message": text,
                                             "channel": "telegram"})


# ---------------------------------------------------------------------------
# The turn itself: a failed turn is not a chosen silence
# ---------------------------------------------------------------------------


async def test_retry_tainted_silence_retains(wired, monkeypatch):
    """A narration turn carries no ``trusted_user_origin``, so #650 never
    reclassifies it: the consumed retry is the only evidence that failed."""
    agent, _ob, install = wired
    stub = install(_TelegramStub())
    counted = _Counted()
    retryable = type("CLIConnectionError", (RuntimeError,), {})
    factory = _Factory([[retryable("upstream reset")],
                        [_mk_assistant(SILENCE_SENTINEL)]])

    _resp, seen = await _handle(agent, _live_notice(counted), factory,
                                monkeypatch)

    assert len(factory.clients) == 2
    assert len(seen.process) == 1
    text, retries = seen.process[0]
    assert text == SILENCE_SENTINEL and len(retries) == 1
    assert stub.turn_finished.await_count == 1
    _assert_retained(stub, seen, counted)


@pytest.mark.parametrize("final", ["", "   \n\t "], ids=["empty", "whitespace"])
async def test_a_silence_with_no_sentinel_retains(wired, monkeypatch, final):
    agent, _ob, install = wired
    stub = install(_TelegramStub())
    counted = _Counted()
    script = [_mk_assistant(final)] if final else []
    factory = _Factory([script])

    _resp, seen = await _handle(agent, _live_notice(counted), factory,
                                monkeypatch)

    assert [r for _t, r in seen.process] == [[]]
    assert strips_to_silence(seen.process[0][0])
    assert SILENCE_SENTINEL not in (seen.process[0][0] or "")
    _assert_retained(stub, seen, counted)


async def test_the_voice_error_line_retains(tmp_path, monkeypatch):
    """The voice error-line arm empties the text with an error kind set, so
    the turn lands on the silent branch — as a failure."""
    agent = _make_agent(tmp_path)
    voice = _TelegramStub()
    voice.name = "voice"
    voice.emit_error_line = AsyncMock(return_value=True)
    agent._channel_manager.register(voice)
    counted = _Counted()
    notice = _live_notice(counted)
    notice.channel = "voice"
    factory = _Factory([[ValueError("scripted non-retryable fault")]])
    try:
        _resp, seen = await _handle(agent, notice, factory, monkeypatch)
    finally:
        await agent.aclose()

    assert voice.emit_error_line.await_count == 1
    assert voice.turn_finished.await_count == 1
    _assert_retained(voice, seen, counted)


async def test_no_channel_retains(wired, monkeypatch):
    agent, _ob, install = wired
    stub = install(_TelegramStub())
    counted = _Counted()
    notice = _live_notice(counted)
    notice.channel = "not-registered"

    _resp, seen = await _handle(agent, notice, _silent_after(), monkeypatch)

    assert seen.process[0] == (SILENCE_SENTINEL, [])
    assert stub.turn_finished.await_count == 0
    _assert_retained(stub, seen, counted)


# ---------------------------------------------------------------------------
# A send the operator may never have seen, then silence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [DeliveryOutcome.NOT_DELIVERED, DeliveryOutcome.UNKNOWN, None,
     RuntimeError("transport down")],
    ids=["not-delivered", "unknown", "off-contract-none", "raises"],
)
async def test_an_unconfirmed_send_message_then_silence_retains(
    wired, monkeypatch, outcome,
):
    agent, _ob, install = wired
    stub = install(_TelegramStub())
    if isinstance(outcome, BaseException):
        stub.send.side_effect = outcome
    else:
        stub.send.return_value = outcome
    counted = _Counted()
    results: list = []

    async def _send():
        results.append(await _send_message())

    _resp, seen = await _handle(agent, _live_notice(counted),
                                _silent_after(_Hook(_send)), monkeypatch)

    assert stub.send.await_count == 1
    assert len(results) == 1
    assert seen.process == [(SILENCE_SENTINEL, [])]
    _assert_retained(stub, seen, counted)


async def test_a_later_delivery_does_not_erase_an_earlier_failure(
    wired, monkeypatch,
):
    agent, _ob, install = wired
    stub = install(_TelegramStub())
    stub.send.side_effect = [DeliveryOutcome.NOT_DELIVERED,
                             DeliveryOutcome.DELIVERED]
    counted = _Counted()

    async def _send():
        await _send_message("first")

    async def _resend():
        await _send_message("second")

    _resp, seen = await _handle(
        agent, _live_notice(counted),
        _silent_after(_Hook(_send), _Hook(_resend)), monkeypatch)

    assert stub.send.await_count == 2
    _assert_retained(stub, seen, counted)


@pytest.mark.parametrize("caption", [None, "your invoice"],
                         ids=["captionless", "captioned"])
@pytest.mark.parametrize(
    "fault", [RuntimeError("channel not started"), NotImplementedError()],
    ids=["unavailable", "unsupported"],
)
async def test_an_undelivered_media_send_then_silence_retains(
    wired, monkeypatch, caption, fault,
):
    agent, ob, install = wired
    stub = install(_MediaStub(media=AsyncMock(side_effect=fault)))
    counted = _Counted()
    results: list = []
    path = _drop(ob, "invoice.pdf", PDF)

    async def _media():
        import tools
        args = {"path": path, "kind": "document"}
        if caption is not None:
            args["caption"] = caption
        results.append(await tools.send_media.handler(args))

    _resp, seen = await _handle(agent, _live_notice(counted),
                                _silent_after(_Hook(_media)), monkeypatch)

    assert stub.send_media.await_count == 1
    assert '"status": "error"' in results[0]["content"][0]["text"]
    _assert_retained(stub, seen, counted)


async def test_a_synchronous_delegates_unconfirmed_send_retains(
    wired, monkeypatch,
):
    """A delegate's scope is built by ``TurnScope.for_child`` from the
    narration's own; model text it commits for the operator and never sees
    delivered keeps the narration's obligation owed."""
    agent, _ob, install = wired
    stub = install(_TelegramStub())
    counted = _Counted()

    async def _child_commits_but_never_delivers():
        import tools
        parent = tools._current_scope(tools._snapshot_origin())
        assert parent is not None
        child = TurnScope.for_child(parent, "")
        committed = child.admit(IntentKind.DISCRETE, "the delegate's report")
        assert isinstance(committed, Admitted)

    _resp, seen = await _handle(
        agent, _live_notice(counted),
        _silent_after(_Hook(_child_commits_but_never_delivers)), monkeypatch)

    assert seen.process == [(SILENCE_SENTINEL, [])]
    _assert_retained(stub, seen, counted)


async def test_an_unposted_keyboard_then_silence_retains(wired, monkeypatch):
    """Model text admitted as a KEYBOARD body — an ``ask_user`` question or an
    authorization challenge that interpolates the model's arguments — whose
    post never confirmed keeps the obligation owed."""
    agent, _ob, install = wired
    stub = install(_TelegramStub())
    counted = _Counted()

    async def _challenge_that_never_posts():
        from output_boundary import resolve_scope
        scope = resolve_scope()
        assert scope is not None
        scope.admit(IntentKind.KEYBOARD, "Allow finance to read the ledger?")

    _resp, seen = await _handle(
        agent, _live_notice(counted),
        _silent_after(_Hook(_challenge_that_never_posts)), monkeypatch)

    _assert_retained(stub, seen, counted)


# ---------------------------------------------------------------------------
# The G-3 recant is not silence: it is delivered, and acknowledged only when
# the transport says so
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome,acks",
    [(DeliveryOutcome.NOT_DELIVERED, 0), (DeliveryOutcome.DELIVERED, 1)],
    ids=["not-delivered", "delivered"],
)
async def test_a_recant_follows_the_transport(wired, monkeypatch, outcome, acks):
    agent, _ob, install = wired
    stub = install(_TelegramStub())
    stub.finalize_response_stream.return_value = outcome
    counted = _Counted()
    factory = _Factory([[_mk_assistant(
        f"{SILENCE_SENTINEL}\nActually — the ledger reconciles.")]])

    _resp, seen = await _handle(agent, _live_notice(counted), factory,
                                monkeypatch)

    assert stub.final_sends() == 1
    assert stub.turn_finished.await_count == 0
    assert counted.count == acks
