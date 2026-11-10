"""The in_casa driver's inbound accounting at the Telegram ingress, and the
completion gate it feeds.

An operator message to an engagement topic is admitted as a text-bearing
ticket SYNCHRONOUSLY by the driver's seam entry, and stays in the ledger until
the job's session takes it up; a recognized command takes nothing. A system
continuation instead holds an ingress RESERVATION (the #663 lease) until its
text is admitted. Both populations veto a successful completion
(INV-ENG-003).

REAL registry over a tmp tombstone + REAL ``InCasaDriver`` + the REAL Telegram
handler (fake bot / fake update). Event-driven coordination only; never patches
``<module>.asyncio.sleep`` (the shared attribute — the memory-cage rule)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# fakes / helpers
# ---------------------------------------------------------------------------


def _mk_update(*, chat_id, text, thread_id=None, user_id=77):
    u = MagicMock()
    u.message = MagicMock()
    u.message.chat = MagicMock()
    u.message.chat.id = chat_id
    u.message.text = text
    u.message.message_thread_id = thread_id
    u.message.from_user = MagicMock(id=user_id)
    u.message.message_id = 999
    return u


try:
    from tests.test_job_session_driver import until
    from tests.turn_scripted_session import TurnScriptedSession, attach_session
except ImportError:  # pragma: no cover — direct-path collection
    from test_job_session_driver import until
    from turn_scripted_session import TurnScriptedSession, attach_session


def _assistant(text):
    from claude_agent_sdk import AssistantMessage, TextBlock
    try:
        return AssistantMessage(content=[TextBlock(text=text)], model="m")
    except TypeError:
        m = AssistantMessage.__new__(AssistantMessage)
        m.content = [TextBlock(text=text)]
        return m


def _result():
    from claude_agent_sdk import ResultMessage
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id="sid")


class _OneShotClient(TurnScriptedSession):
    """Fake job session: every turn answers one line immediately. ``gate``,
    when set, holds each write in the transport until it is set."""

    def __init__(self):
        self.query_prompts: list[str] = []
        self.gate: asyncio.Event | None = None

    async def record_query(self, prompt: str) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.query_prompts.append(prompt)

    async def receive_response(self):
        yield _assistant("ok")
        yield _result()

    async def close(self) -> None:
        pass


async def _settled(drv, rec) -> None:
    await until(lambda: drv.inbound_unread_depth(rec.id) == 0
                and not drv.turn_open(rec.id))


async def _handler_ctx(tmp_path, fake_telegram_bot):
    """A channel wired to a REAL in_casa driver + registry, as casa_core
    wires them, with a live fake session."""
    from channels.telegram import TelegramChannel
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create(
        "executor", "configurator", "in_casa", "t",
        {"user_id": 77}, topic_id=555)
    client = _OneShotClient()
    ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                         engagement_supergroup_id=-1001)
    drv = InCasaDriver(record_lookup=reg.get,
                       begin_turn_delivery=reg.begin_turn_delivery,
                       lifecycle_lock=ch.engagement_lifecycle_lock)
    attach_session(drv, rec, client)
    ch._engagement_registry = reg
    ch._engagement_driver = drv
    ch._post_engagement_notice = AsyncMock()
    return ch, reg, rec, drv, client


class TestInboundIngressReservation:
    """The operator message's admission ticket is visible BEFORE the handler's
    first await and is settled on every path — delivery, command
    classification, and cancellation while suspended. It is a ticket, never
    also a reservation: one message must not count twice in the veto."""

    async def test_reservation_visible_at_first_await(
        self, tmp_path, fake_telegram_bot,
    ):
        """From the moment the handler returns until the session takes the
        message up, a completion racing it already sees the message."""
        ch, reg, rec, drv, client = await _handler_ctx(
            tmp_path, fake_telegram_bot)
        client.gate = asyncio.Event()           # the write hangs in transport

        await asyncio.wait_for(ch.deliver_operator_reply(
            rec, "please reconsider the design", chat_id=100, message_id=31), 5)
        # A completion racing this already sees the message.
        assert drv.inbound_unread_depth(rec.id) == 1
        assert drv.inbound_unread_texts(rec.id) == [
            "please reconsider the design"]
        assert drv.inbound_reservations(rec.id) == 0
        client.gate.set()
        await _settled(drv, rec)

        # Delivered → the ticket is gone.
        assert client.query_prompts == ["please reconsider the design"]
        assert drv.inbound_unread_depth(rec.id) == 0
        assert drv.inbound_unread_texts(rec.id) == []
        await drv.cancel(rec)

    async def test_a_message_in_the_log_group_takes_nothing(
        self, tmp_path, fake_telegram_bot,
    ):
        """A message or a command written in the log group — even in the
        thread an older version used as this job's topic — takes no ticket
        and no reservation and never reaches the client."""
        ch, reg, rec, drv, client = await _handler_ctx(
            tmp_path, fake_telegram_bot)
        for text in ("/help", "please reconsider"):
            await ch.handle_update(_mk_update(chat_id=-1001, text=text,
                                              thread_id=555, user_id=77))
        await asyncio.sleep(0.01)
        assert client.query_prompts == []
        assert (drv.inbound_unread_depth(rec.id),
                drv.inbound_reservations(rec.id)) == (0, 0)
        ch._post_engagement_notice.assert_not_awaited()
        await drv.cancel(rec)


# ===========================================================================
# #649 — the in_casa completion inbound gate (INV-ENG-003): a SUCCESSFUL
# emit_completion is refused (kind=unread_inbound, retryable) while an operator
# message is admitted, or written into the job's session and not yet taken up
# — at the handler's pre-check, and inside the terminal transition for one
# admitted after that pre-check.
# ===========================================================================


class _InCasaRedCaseClient(TurnScriptedSession):
    """Fake job session. Its first turn streams until ``release_first``; a
    message written meanwhile is queued by the CLI and taken up (echoed) only
    once that turn ends."""

    def __init__(self):
        self.query_prompts: list[str] = []
        self.close_calls = 0
        self.first_streaming = asyncio.Event()
        self.release_first = asyncio.Event()
        self.turns = 0

    async def record_query(self, prompt: str) -> None:
        self.query_prompts.append(prompt)

    async def receive_response(self):
        self.turns += 1
        if self.turns == 1:
            self.first_streaming.set()
            await self.release_first.wait()
            yield _assistant("first turn text")
        else:
            yield _assistant("second turn text")
        yield _result()

    async def close(self) -> None:
        self.close_calls += 1
        self.release_first.set()


class _FakeStreamHandle:
    def __init__(self):
        self.emits: list[str] = []
        self.finals: list[str] = []

    async def emit(self, text):
        self.emits.append(text)

    async def finalize(self, text):
        self.finals.append(text)


class TestInCasaCompletionInboundGate:
    async def _ctx(self, tmp_path, fake_telegram_bot):
        import agent as agent_mod
        from channels.telegram import TelegramChannel
        from drivers.in_casa_driver import InCasaDriver
        from engagement_registry import EngagementRegistry
        from tools import init_tools

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            "executor", "configurator", "in_casa", "t",
            {"role": "assistant", "channel": "telegram", "user_id": 77},
            topic_id=555)

        client = _InCasaRedCaseClient()
        drv = InCasaDriver(record_lookup=reg.get,
                           begin_turn_delivery=reg.begin_turn_delivery)
        attach_session(drv, rec, client)

        tch = MagicMock()
        tch.send_to_topic = AsyncMock()
        tch.send_response_to_topic = AsyncMock()
        tch.close_topic = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = tch
        bus = MagicMock()
        bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        agent_mod.active_engagement_driver = drv

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = reg
        ch._engagement_driver = drv
        ch._post_engagement_notice = AsyncMock()
        return ch, reg, rec, drv, client, tch

    async def _emit_ok(self, rec):
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler({
                "text": "done", "next_steps": [],
                "status": "ok"})
        finally:
            engagement_var.reset(token)
        return json.loads(res["content"][0]["text"])

    async def test_queued_in_casa_turn_vetoes_completion_before_client_handoff(
            self, tmp_path, fake_telegram_bot):
        """An operator message accepted while the job's turn streams — written
        into the session but not yet taken up — refuses a successful
        completion at the handler's pre-check (the terminal transition is
        never entered); once the session takes it up and that turn ends, the
        veto clears."""
        import agent as agent_mod
        ch, reg, rec, drv, client, tch = await self._ctx(
            tmp_path, fake_telegram_bot)
        transitions: list = []
        real_transition = reg.try_transition_terminal

        async def _counting_transition(*args, **kwargs):
            transitions.append(args)
            return await real_transition(*args, **kwargs)
        reg.try_transition_terminal = _counting_transition
        try:
            drv.submit_casa(rec, "first")
            await asyncio.wait_for(client.first_streaming.wait(), 5)

            assert await ch.deliver_operator_reply(
                rec, "operator message", chat_id=100, message_id=31)
            assert drv.inbound_unread_texts(rec.id) == ["operator message"]

            payload = await self._emit_ok(rec)
            assert payload["status"] == "error"
            assert payload["kind"] == "unread_inbound"
            assert payload.get("retryable") is True
            assert transitions == []          # the handler pre-check refused
            assert reg.get(rec.id).status == "active"
            assert client.close_calls == 0

            client.release_first.set()
            await until(lambda: client.turns == 2 and not drv.turn_open(rec.id))
            # The operator message was taken up after the refusal.
            assert client.query_prompts == ["first", "operator message"]
            assert drv.inbound_unread_depth(rec.id) == 0
            assert client.close_calls == 0

            # The veto is CLEARABLE: with nothing unread, completion succeeds.
            payload2 = await self._emit_ok(rec)
            assert payload2["status"] == "acknowledged"
            assert reg.get(rec.id).status == "completed"
        finally:
            agent_mod.active_engagement_driver = None
            await drv.cancel(rec)

    async def test_in_casa_turn_admitted_during_finalize_is_vetoed_by_terminal_hook(
            self, tmp_path, fake_telegram_bot):
        import agent as agent_mod
        ch, reg, rec, drv, client, tch = await self._ctx(
            tmp_path, fake_telegram_bot)
        try:
            drv.submit_casa(rec, "first")
            await asyncio.wait_for(client.first_streaming.wait(), 5)

            # Event-controlled seam at the entry of the registry's terminal
            # transition, which runs AFTER emit_completion's handler pre-check
            # and BEFORE try_transition_terminal invokes its terminal hook:
            # the operator update is admitted exactly in that window.
            real_transition = reg.try_transition_terminal
            depth_at_entry: list = []

            async def _admitting_transition(*args, **kwargs):
                depth_at_entry.append(drv.inbound_unread_depth(rec.id))
                await ch.deliver_operator_reply(
                    rec, "operator message", chat_id=100, message_id=31)
                return await real_transition(*args, **kwargs)
            reg.try_transition_terminal = _admitting_transition

            payload = await self._emit_ok(rec)
            assert payload["status"] == "error"
            assert payload["kind"] == "unread_inbound"
            # The pre-check read nothing unread: the veto fired inside the
            # transition, from its terminal hook.
            assert depth_at_entry == [0]
            assert reg.get(rec.id).status == "active"
            assert client.close_calls == 0
            assert drv.inbound_unread_texts(rec.id) == ["operator message"]
        finally:
            agent_mod.active_engagement_driver = None
            client.release_first.set()
            await drv.cancel(rec)


class TestReservationKindThreading:
    """#664: a reservation's command classification travels with it from
    reserve to release. A command reservation vetoes a racing completion but
    is never disclosed as a lost message; a dropped kind at release would
    leave a phantom command counter that later hides one real message."""

    async def test_a_recognized_command_reserves_and_releases_as_command(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, client = await _handler_ctx(
            tmp_path, fake_telegram_bot)
        drv.reserve_inbound(rec.id, command=True)
        assert drv.inbound_reservations(rec.id) == 1          # vetoes
        assert drv.inbound_message_reservations(rec.id) == 0  # not disclosed
        drv.release_inbound_reservation(rec.id, command=True)
        # nothing lingers on any counter after the command
        assert drv.inbound_reservations(rec.id) == 0
        assert drv.inbound_message_reservations(rec.id) == 0
        # ...and a subsequent ordinary reservation projects as exactly one
        # lost-disclosable message (a phantom command counter would hide it)
        drv.reserve_inbound(rec.id)
        assert drv.inbound_message_reservations(rec.id) == 1
        drv.release_inbound_reservation(rec.id)
        assert drv.inbound_reservations(rec.id) == 0

    async def test_an_ordinary_message_reserves_and_releases_as_message(
        self, tmp_path, fake_telegram_bot,
    ):
        """The production reserver — the #663 continuation lease — reserves
        as a message, and its idempotent double release returns every
        counter to zero."""
        ch, reg, rec, drv, client = await _handler_ctx(
            tmp_path, fake_telegram_bot)
        lease = ch.engagement_inbound_reservation(rec.id)
        assert lease.take() is True
        assert drv.inbound_reservations(rec.id) == 1
        assert drv.inbound_message_reservations(rec.id) == 1
        lease.release()
        lease.release()                       # second release is a no-op
        assert drv.inbound_reservations(rec.id) == 0
        assert drv.inbound_message_reservations(rec.id) == 0
