"""The in_casa driver's inbound accounting at the Telegram ingress, and the
completion gate it feeds.

An operator message to an engagement topic is admitted as a text-bearing
ticket SYNCHRONOUSLY, before the handler's first await, and every path settles
it — delivery, command classification, cancellation while suspended, and a
delivery task cancelled before its first step. A system continuation instead
holds an ingress RESERVATION (the #663 lease) until its text is admitted. Both
populations veto a successful completion (INV-ENG-003).

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


async def _drain_turns(ch) -> None:
    tasks = list(getattr(ch, "_turn_tasks", ()) or ())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _drain_all(ch) -> None:
    """Drain the delivery tasks AND the task-end cleanup tasks their
    done-callbacks create (one loop iteration later — hence the bare yield;
    nothing waits for a duration and no ``asyncio.sleep`` is patched)."""
    for _ in range(20):
        tasks = [t for s in ("_turn_tasks", "_inbound_cleanup_tasks")
                 for t in list(getattr(ch, s, ()) or ())]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
    raise AssertionError("tracked tasks did not drain in 20 passes")


def _assistant(text):
    from claude_agent_sdk import AssistantMessage, TextBlock
    try:
        return AssistantMessage(content=[TextBlock(text=text)], model="m")
    except TypeError:
        m = AssistantMessage.__new__(AssistantMessage)
        m.content = [TextBlock(text=text)]
        return m


class _OneShotClient:
    """Fake ClaudeSDKClient: every turn answers one line immediately."""

    def __init__(self):
        self.query_prompts: list[str] = []

    async def query(self, prompt: str) -> None:
        self.query_prompts.append(prompt)

    def receive_response(self):
        async def _gen():
            yield _assistant("ok")
        return _gen()

    async def close(self) -> None:
        pass


async def _handler_ctx(tmp_path, fake_telegram_bot):
    """A channel wired to a REAL in_casa driver + registry, with the
    admission-ticket seams mirroring casa_core's wiring."""
    from channels.telegram import TelegramChannel
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "in_casa", "t",
        {"user_id": 77}, topic_id=555)
    client = _OneShotClient()
    drv = InCasaDriver(topic_stream_factory=lambda tid: _FakeStreamHandle())
    drv._clients[rec.id] = client
    drv._ctx_stack[rec.id] = client
    drv._locks[rec.id] = asyncio.Lock()

    ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                         engagement_supergroup_id=-1001)
    ch._engagement_registry = reg
    ch._engagement_driver = drv
    ch._observer = MagicMock()
    ch._post_engagement_notice = AsyncMock()
    ch._driver_admit_inbound = lambda r, text: drv.admit_inbound(r.id, text)
    ch._driver_discharge_inbound = (
        lambda r, tok: drv.discharge_inbound(r.id, tok))
    ch._driver_inbound_held = (
        lambda r, tok: drv.inbound_token_held(r.id, tok))

    async def _send_user_turn(r, text, *, tg_message_id=None,
                              inbound_token=None):
        await drv.send_user_turn(r, text, inbound_token=inbound_token)
        return None
    ch._driver_send_user_turn = _send_user_turn
    return ch, reg, rec, drv, client


def _block_first_await(reg):
    """Block the handler's FIRST await (the clearance clamp) until released."""
    entered = asyncio.Event()
    gate = asyncio.Event()
    real_lower = reg.lower_origin_clearance

    async def blocking_lower(eng_id, clearance):
        entered.set()
        await gate.wait()
        return await real_lower(eng_id, clearance)
    reg.lower_origin_clearance = blocking_lower
    return entered, gate


class TestInboundIngressReservation:
    """The operator message's admission ticket is visible BEFORE the handler's
    first await and is settled on every path — delivery, command
    classification, and cancellation while suspended. It is a ticket, never
    also a reservation: one message must not count twice in the veto."""

    async def test_reservation_visible_at_first_await(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, client = await _handler_ctx(
            tmp_path, fake_telegram_bot)
        entered, gate = _block_first_await(reg)

        u = _mk_update(chat_id=-1001, text="please reconsider the design",
                       thread_id=555, user_id=77)
        task = asyncio.create_task(ch.handle_update(u))
        await asyncio.wait_for(entered.wait(), 5)
        # A completion racing this suspension already sees the message.
        assert drv.inbound_unread_depth(rec.id) == 1
        assert drv.inbound_unread_texts(rec.id) == [
            "please reconsider the design"]
        assert drv.inbound_reservations(rec.id) == 0
        gate.set()
        await task
        await _drain_all(ch)

        # Delivered → the ticket is gone.
        assert client.query_prompts == ["please reconsider the design"]
        assert drv.inbound_unread_depth(rec.id) == 0
        assert drv.inbound_unread_texts(rec.id) == []

    async def test_a_recognized_command_takes_nothing(
        self, tmp_path, fake_telegram_bot,
    ):
        """A recognized command is consumed by the handler itself: no ticket
        and no reservation at the first await, nothing left after, and it
        never reaches the client."""
        ch, reg, rec, drv, client = await _handler_ctx(
            tmp_path, fake_telegram_bot)
        entered, gate = _block_first_await(reg)

        u = _mk_update(chat_id=-1001, text="/silent", thread_id=555,
                       user_id=77)
        task = asyncio.create_task(ch.handle_update(u))
        await asyncio.wait_for(entered.wait(), 5)
        assert (drv.inbound_unread_depth(rec.id),
                drv.inbound_reservations(rec.id)) == (0, 0)
        gate.set()
        await task
        await _drain_all(ch)
        ch._observer.silence.assert_called_once_with(rec.id)
        assert client.query_prompts == []
        assert (drv.inbound_unread_depth(rec.id),
                drv.inbound_reservations(rec.id)) == (0, 0)

    async def test_cancellation_while_suspended_releases(
        self, tmp_path, fake_telegram_bot,
    ):
        """Handler cancelled while blocked on its first await — the ticket is
        told (one bounded notice) and then discharged, never leaked."""
        ch, reg, rec, drv, client = await _handler_ctx(
            tmp_path, fake_telegram_bot)
        entered, _gate = _block_first_await(reg)

        u = _mk_update(chat_id=-1001, text="hello there", thread_id=555,
                       user_id=77)
        task = asyncio.create_task(ch.handle_update(u))
        await asyncio.wait_for(entered.wait(), 5)
        assert drv.inbound_unread_depth(rec.id) == 1   # held while suspended
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert drv.inbound_unread_depth(rec.id) == 0   # discharged on cancel
        notices = [c.args[1] for c in
                   ch._post_engagement_notice.await_args_list]
        assert sum("could not be delivered" in n for n in notices) == 1
        assert client.query_prompts == []


# ===========================================================================
# 10. RED CASE (#649) — in_casa completion inbound gate
#     Specified by Terra (drive run 2026-08-18-b, rounds-649/redcase-spec),
#     accepted by Sol. FROZEN once accepted — do not modify; re-specify +
#     re-accept instead. Invariant: INV-ENG-003 (engagement-completion-gate.md) extended
#     to the in_casa driver — a SUCCESSFUL emit_completion is refused
#     (kind=unread_inbound, retryable) while an inbound turn has been admitted
#     for delivery but not yet handed to the engagement's ClaudeSDKClient,
#     including a turn queued behind the driver's per-turn lock
#     (in_casa_driver.py:411) while an earlier turn streams. At 3dce522d the
#     gate hasattr-no-ops (tools.py:8563; InCasaDriver implements no inbound
#     accessors) and the queued turn dies on the closed client, dropped at
#     INFO (telegram.py:1877-1889; dossier 649 Part B, measured).
# ===========================================================================


class _InCasaRedCaseClient:
    """Fake ClaudeSDKClient. First turn streams (holds the per-turn lock)
    until ``release_first``; ``close()`` marks closed and releases the first
    stream; a later ``query()`` on a closed client raises — the measured
    pre-fix loss mode (dossier 649 claim 5, interleaving (c))."""

    def __init__(self):
        self.query_prompts: list[str] = []
        self.close_calls = 0
        self.closed = False
        self.first_streaming = asyncio.Event()
        self.release_first = asyncio.Event()

    async def query(self, prompt: str) -> None:
        if self.closed:
            raise RuntimeError("client closed")
        self.query_prompts.append(prompt)

    def receive_response(self):
        from claude_agent_sdk import AssistantMessage, TextBlock
        client = self

        def _assistant(text):
            try:
                return AssistantMessage(content=[TextBlock(text=text)],
                                        model="m")
            except TypeError:
                m = AssistantMessage.__new__(AssistantMessage)
                m.content = [TextBlock(text=text)]
                return m

        first = len(client.query_prompts) == 1

        async def _gen():
            if first:
                client.first_streaming.set()
                await client.release_first.wait()
                yield _assistant("first turn text")
            else:
                yield _assistant("second turn text")
        return _gen()

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self.release_first.set()


class _ProbeLock(asyncio.Lock):
    """Per-engagement driver lock that flags when a SECOND turn queues."""

    def __init__(self):
        super().__init__()
        self.second_waiting = asyncio.Event()

    async def __aenter__(self):
        if self.locked():
            self.second_waiting.set()
        await self.acquire()
        return None


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
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "in_casa", "t",
            {"role": "assistant", "channel": "telegram", "user_id": 77},
            topic_id=555)

        client = _InCasaRedCaseClient()
        drv = InCasaDriver(topic_stream_factory=lambda tid: _FakeStreamHandle())
        lock = _ProbeLock()
        drv._clients[rec.id] = client
        drv._ctx_stack[rec.id] = client
        drv._locks[rec.id] = lock

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
        ch._observer = MagicMock()

        async def _send_user_turn(r, text, *, tg_message_id=None):
            # Mirrors casa_core's in_casa branch (casa_core.py:4386-4389).
            await drv.send_user_turn(r, text)
            return None
        ch._driver_send_user_turn = _send_user_turn
        return ch, reg, rec, drv, client, lock, tch

    async def _emit_ok(self, rec):
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler({
                "text": "done", "artifacts": [], "next_steps": [],
                "status": "ok"})
        finally:
            engagement_var.reset(token)
        return json.loads(res["content"][0]["text"])

    async def test_queued_in_casa_turn_vetoes_completion_before_client_handoff(
            self, tmp_path, fake_telegram_bot):
        import agent as agent_mod
        ch, reg, rec, drv, client, lock, tch = await self._ctx(
            tmp_path, fake_telegram_bot)
        try:
            first = asyncio.create_task(drv.send_user_turn(rec, "first"))
            await asyncio.wait_for(client.first_streaming.wait(), 5)

            u = _mk_update(chat_id=-1001, text="operator message",
                           thread_id=555, user_id=77)
            await ch.handle_update(u)
            await asyncio.wait_for(lock.second_waiting.wait(), 5)
            # Admitted but NOT handed to the client.
            assert client.query_prompts == ["first"]

            payload = await self._emit_ok(rec)
            assert payload["status"] == "error"
            assert payload["kind"] == "unread_inbound"
            assert payload.get("retryable") is True
            # Sol (redcase acceptance r1): pin the HANDLER PRE-CHECK layer
            # specifically — its refusal copy ("waiting unread",
            # tools.py:8615-8622) differs from the terminal hook's
            # ("arrived while completing", tools.py:8669-8675). A hook-only
            # implementation must fail here.
            assert "waiting unread" in payload["message"]
            assert reg.get(rec.id).status == "active"
            assert client.close_calls == 0

            client.release_first.set()
            await asyncio.wait_for(first, 5)
            await _drain_turns(ch)
            # The queued operator turn DELIVERED after the refusal pumped it.
            assert client.query_prompts == ["first", "operator message"]
            assert client.close_calls == 0

            # The veto is CLEARABLE: with nothing queued, completion succeeds.
            payload2 = await self._emit_ok(rec)
            assert payload2["status"] == "acknowledged"
            assert reg.get(rec.id).status == "completed"
        finally:
            agent_mod.active_engagement_driver = None

    async def test_in_casa_turn_admitted_during_finalize_is_vetoed_by_terminal_hook(
            self, tmp_path, fake_telegram_bot):
        import agent as agent_mod
        ch, reg, rec, drv, client, lock, tch = await self._ctx(
            tmp_path, fake_telegram_bot)
        try:
            first = asyncio.create_task(drv.send_user_turn(rec, "first"))
            await asyncio.wait_for(client.first_streaming.wait(), 5)

            # Event-controlled seam at the entry of the registry's terminal
            # transition, which runs AFTER emit_completion's handler pre-check
            # and BEFORE try_transition_terminal invokes its terminal hook:
            # the operator update is admitted exactly in that window. (The
            # seam used to be the funnel's spool drain, which is gone.)
            real_transition = reg.try_transition_terminal

            async def _admitting_transition(*args, **kwargs):
                u = _mk_update(chat_id=-1001, text="operator message",
                               thread_id=555, user_id=77)
                await ch.handle_update(u)
                await asyncio.wait_for(lock.second_waiting.wait(), 5)
                return await real_transition(*args, **kwargs)
            reg.try_transition_terminal = _admitting_transition

            payload = await self._emit_ok(rec)
            assert payload["status"] == "error"
            assert payload["kind"] == "unread_inbound"
            # Sol (redcase acceptance r1): pin the TERMINAL-HOOK layer — this
            # refusal must be the hook's PRECONDITION_FAILED copy ("arrived
            # while completing", tools.py:8669-8675), proving the veto fired
            # inside the transition, not at the handler pre-check (which read
            # zero before the admission).
            assert "arrived while completing" in payload["message"]
            assert reg.get(rec.id).status == "active"
            assert client.close_calls == 0
            assert client.query_prompts == ["first"]
        finally:
            agent_mod.active_engagement_driver = None
            client.release_first.set()
            await asyncio.wait_for(first, 5)
            await _drain_turns(ch)


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


class TestIngressReservationTaskOwnership:
    """A delivery task cancelled BEFORE its first coroutine step never runs
    ``_deliver_turn_bg``, so none of its own settle paths run either. It must
    FAIL TOWARD DISCLOSURE: the task-end backstop tells the operator while the
    ticket is still ledgered (so a racing terminal could still quote the
    words), and only then discharges it.
    """

    async def test_cancel_before_first_coroutine_step_retains_the_disclosure(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, client = await _handler_ctx(
            tmp_path, fake_telegram_bot)
        held_at_notice: list[list[str]] = []

        async def _notice(r, text):
            held_at_notice.append(drv.inbound_unread_texts(rec.id))
        ch._post_engagement_notice = AsyncMock(side_effect=_notice)

        class _CancelOnAdd(set):
            """`self._turn_tasks.add(task)` runs synchronously immediately
            after `create_task`, before any yield — so cancelling here is
            cancellation BEFORE the coroutine's first step, with the real
            `_deliver_turn_bg` coroutine and the real done callbacks."""
            def add(self, t):
                t.cancel()
                super().add(t)

        ch._turn_tasks = _CancelOnAdd()

        u = _mk_update(chat_id=-1001, text="hello", thread_id=555, user_id=77)
        await ch.handle_update(u)
        await _drain_all(ch)

        notices = [c.args[1] for c in
                   ch._post_engagement_notice.await_args_list]
        assert sum("could not be delivered" in n for n in notices) == 1
        assert held_at_notice == [["hello"]]
        assert drv.inbound_unread_depth(rec.id) == 0
        assert client.query_prompts == []
