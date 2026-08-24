"""#678 — the launch-death reporter's remaining count and mutation pins.

Separate from the frozen red case (``test_in_casa_launch_terminal_artifact.py``,
accepted and not to be edited) and from its H2 guard. Everything here exists to
make one specific mutation fail; the mapping is named on each test.

No ``asyncio.sleep`` is patched anywhere: a wedged counterparty is modelled as
an ``asyncio.Event`` that is never set, and the production bound is shortened
instead. Patching ``<module>.asyncio.sleep`` is the shared module attribute and
is forbidden in this repo (it once OOM-killed the whole VM).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from tests.test_in_casa_launch_terminal_artifact import (
        ScriptedCompleteClient, ScriptedCutoffClient, _Probe, _build, _launch,
        _mock_executor_def,
    )
except ImportError:  # pragma: no cover — direct-path collection
    from test_in_casa_launch_terminal_artifact import (
        ScriptedCompleteClient, ScriptedCutoffClient, _Probe, _build, _launch,
        _mock_executor_def,
    )

pytestmark = [pytest.mark.asyncio]

_COUNTERPARTY_LATENCY_S = 0.1
"""#695: the modelled round-trip of the Telegram counterparty the launch-death
reporter posts through.

NOT a wait and not an allowance: no test waits for this to elapse. It is part of
the ARRANGEMENT — a real ``_post_engagement_notice`` is tens of milliseconds, and
pinning it at zero is what let ``TestCancellationHasAnOwner`` observe complete
effects from an incomplete reporter on an idle box. With the counterparty this
slow, the reporter provably has NOT finished when each readiness wait below
begins, so "the wait observes the reporter's completion" is falsifiable on any
machine instead of only on a loaded CI runner: swap either drain back for the
old ``for _ in range(N): await asyncio.sleep(0)`` proxy and the test fails
deterministically, idle. 500 bare yields cost ~0.6 ms (9.7 ms under 24-way
load); this is two orders of magnitude more, and no event-loop yield can advance
a real timer."""


class TextlessCompleteClient(ScriptedCutoffClient):
    """A launch turn that RAN TO ITS END — one ResultMessage — but streamed no
    assistant text and called no emit_completion. Nothing about the engagement
    is visible anywhere: the `no_visible_output` arm."""

    async def receive_response(self):
        from claude_agent_sdk import (
            AssistantMessage, ResultMessage, ToolUseBlock,
        )
        use = ToolUseBlock(id="t1", name="Read", input={"file_path": "/x"})
        type(self).frames_yielded += 1
        yield AssistantMessage(content=[use], model="claude-sonnet-4-6")
        rm = ResultMessage.__new__(ResultMessage)
        for k, v in dict(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="s", total_cost_usd=0.0,
            usage={}, result="", stop_reason=None, parent_tool_use_id=None,
        ).items():
            setattr(rm, k, v)
        type(self).result_messages_yielded += 1
        type(self).frames_yielded += 1
        yield rm


def _record(rec_id: str, **overrides):
    from engagement_registry import EngagementRecord
    base = dict(
        id=rec_id, kind="executor", role_or_type="configurator",
        driver="in_casa", status="active", topic_id=42, started_at=0.0,
        last_user_turn_ts=0.0, last_idle_reminder_ts=0.0, completed_at=None,
        sdk_session_id=None, origin={}, task="t",
    )
    base.update(overrides)
    return EngagementRecord(**base)


async def _payload(envelope):
    return json.loads(envelope["content"][0]["text"])


_FOLLOWUP_MISSING_RESULT = "followup_missing_result"
"""The follow-up observation's value, written out here rather than imported
from ``drivers.in_casa_driver``.

Deliberate, and load-bearing for the red case: importing the production
constant makes the pre-fix run fail with an ImportError, which is a statement
about a symbol rather than about behaviour. Duplicated, the same run fails on
``"" != "followup_missing_result"`` — the observation the driver did not make.
A reworded production constant then also fails a test, which is the second
reason.
"""


def _pop_followup(drv, engagement_id, token):
    """Read the follow-up observation the way ``casa_core`` does — through
    ``getattr``, tolerating a driver that has no such accessor — so a pre-fix
    tree answers "nothing observed" instead of raising ``AttributeError``.

    The production seam itself is pinned separately, in
    ``TestTheProductionSeamIsTheOneUnderTest``; this helper exists only so the
    behavioural red cases fail for the behavioural reason.
    """
    fn = getattr(drv, "followup_turn_incomplete", None)
    return fn(engagement_id, token) if callable(fn) else ""


class TestTheOtherArm:
    async def test_a_complete_but_mute_launch_turn_is_reported(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: drop the `no_visible_output` arm → this fails.

        A ResultMessage arrived, so the turn was not cut off; but nothing was
        posted and nothing was completed, so the operator has a topic with no
        evidence anything happened.
        """
        probe = _Probe()
        engage_executor, registry, channel, _d = _build(
            tmp_path, monkeypatch, probe, TextlessCompleteClient,
        )
        envelope = await _launch(engage_executor)
        payload = await _payload(envelope)

        assert TextlessCompleteClient.result_messages_yielded == 1
        assert probe.finalize_count == 0
        assert payload["status"] == "error", payload
        assert payload["kind"] == "launch_turn_incomplete"
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"
        assert channel._post_engagement_notice.await_count == 1
        assert channel.close_topic.await_count == 1


class TestTheDriverDoesNotAdjudicate:
    async def test_the_observation_ignores_the_records_own_status(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: read the record's status in the driver → this fails.

        A lock-free status read there could observe the uncommitted window of a
        strict terminal transition that a persist failure then ROLLS BACK
        (engagement_registry.py commits the terminal fields in memory before
        awaiting the tombstone write). The driver must therefore observe the
        turn's own facts and nothing else; the registry adjudicates.
        """
        from claude_agent_sdk import ClaudeAgentOptions
        from drivers.in_casa_driver import (
            InCasaDriver, LAUNCH_MISSING_RESULT,
        )
        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient", ScriptedCutoffClient)

        class _Handle:
            async def emit(self, text):
                pass

            async def finalize(self, text):
                pass

        drv = InCasaDriver(topic_stream_factory=lambda t: _Handle())
        rec = _record("a" * 32)
        # The record LOOKS terminal to a lock-free reader — exactly the
        # transient state a rollback erases.
        rec.status = "completed"

        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="s"))

        assert drv.launch_turn_incomplete(rec.id) == LAUNCH_MISSING_RESULT
        # And popping is a pop: a second read cannot report the same death.
        assert drv.launch_turn_incomplete(rec.id) == ""

    async def test_a_ticketed_follow_up_records_its_own_observation(
        self, tmp_path, monkeypatch,
    ):
        """#692/#678 R1 — REWRITTEN (was ``..._records_no_observation``).

        The old test asserted only that a ticketed follow-up turn leaves the
        LAUNCH slot alone, and justified the silence with a sentence that is
        FALSE: "a follow-up turn's failure owner is the Telegram delivery
        task's 'Turn failed' notice (#649)". That notice lives inside
        ``except Exception`` around ``_driver_send_user_turn``
        (channels/telegram.py) and a mid-tool-loop cutoff on a ticketed turn
        RAISES NOTHING, so the owner could never fire. The test therefore
        pinned the defect.

        What is kept: the launch slot must stay untouched (a ticketed write
        could clobber an unread launch observation).
        What is added: the FOLLOW-UP slot is written, keyed by the exact
        admission ticket, and popped exactly once.

        MUTATIONS: write the observation into ``_launch_incomplete`` → the
        launch assertion fails. Key the map by engagement id instead of by
        ticket → ``test_two_crossed_follow_up_turns_each_get_their_own``
        fails. Drop the ``result_msg is None`` guard → the healthy control
        below fails.
        """
        from claude_agent_sdk import ClaudeAgentOptions
        from drivers.in_casa_driver import InCasaDriver
        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient", ScriptedCompleteClient)

        class _Handle:
            async def emit(self, text):
                pass

            async def finalize(self, text):
                pass

        drv = InCasaDriver(topic_stream_factory=lambda t: _Handle())
        rec = _record("b" * 32)
        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="s"))
        assert drv.launch_turn_incomplete(rec.id) == ""

        # Now a FOLLOW-UP turn on the same client that ends without a
        # ResultMessage. Evidence HAS latched (one assistant frame), so this
        # is the mid-tool-loop shape and not the zero-evidence one.
        async def _truncated(self):
            from claude_agent_sdk import AssistantMessage, TextBlock
            yield AssistantMessage(
                content=[TextBlock(text="working")], model="m")

        monkeypatch.setattr(
            type(drv._clients[rec.id]), "receive_response", _truncated)
        token = drv.admit_inbound(rec.id, "and now this")
        await drv.send_user_turn(rec, "and now this", inbound_token=token)

        # The launch slot is untouched — the property the old test pinned.
        # Asserted FIRST, deliberately: it is the assertion carried over from
        # the old test, and putting it ahead of the new accessor keeps it
        # reachable on a tree where that accessor does not exist yet.
        assert drv.launch_turn_incomplete(rec.id) == ""
        # And the follow-up slot carries the observation, once, per ticket.
        assert _pop_followup(drv, rec.id, token) == _FOLLOWUP_MISSING_RESULT
        assert _pop_followup(drv, rec.id, token) == ""

    async def test_a_healthy_follow_up_records_nothing(
        self, tmp_path, monkeypatch,
    ):
        """The CONTROL. A ticketed follow-up turn that ends holding its own
        terminal artifact is a silent success: no observation in either slot.

        MUTATION: drop the ``result_msg is None`` guard (observe every
        ticketed turn) → this fails.
        """
        from claude_agent_sdk import ClaudeAgentOptions
        from drivers.in_casa_driver import InCasaDriver
        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient", ScriptedCompleteClient)

        class _Handle:
            async def emit(self, text):
                pass

            async def finalize(self, text):
                pass

        drv = InCasaDriver(topic_stream_factory=lambda t: _Handle())
        rec = _record("c" * 32)
        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="s"))
        ScriptedCompleteClient.result_messages_yielded = 0
        token = drv.admit_inbound(rec.id, "carry on")
        await drv.send_user_turn(rec, "carry on", inbound_token=token)
        assert ScriptedCompleteClient.result_messages_yielded == 1
        assert _pop_followup(drv, rec.id, token) == ""
        assert drv.launch_turn_incomplete(rec.id) == ""

    async def test_the_follow_up_observation_ignores_the_records_own_status(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: read the record's status in the driver's follow-up
        branch → this fails. Same reason as the launch arm above: a lock-free
        read there can see the uncommitted window of a strict transition that
        a persist failure then rolls back. The driver observes the turn's own
        facts; the delivery owner adjudicates against the SETTLED record.
        """
        from claude_agent_sdk import ClaudeAgentOptions
        from drivers.in_casa_driver import InCasaDriver
        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient", ScriptedCompleteClient)

        class _Handle:
            async def emit(self, text):
                pass

            async def finalize(self, text):
                pass

        drv = InCasaDriver(topic_stream_factory=lambda t: _Handle())
        rec = _record("d" * 32)
        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="s"))

        async def _truncated(self):
            from claude_agent_sdk import AssistantMessage, TextBlock
            yield AssistantMessage(content=[TextBlock(text="x")], model="m")

        monkeypatch.setattr(
            type(drv._clients[rec.id]), "receive_response", _truncated)
        # The record LOOKS terminal to a lock-free reader — the transient
        # state a rollback erases.
        rec.status = "completed"
        token = drv.admit_inbound(rec.id, "still mine")
        await drv.send_user_turn(rec, "still mine", inbound_token=token)
        assert _pop_followup(drv, rec.id, token) == _FOLLOWUP_MISSING_RESULT

    async def test_two_crossed_follow_up_turns_each_get_their_own(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: key the observation map by engagement id instead of by
        admission ticket → this fails.

        The observation is written AFTER ``_deliver_turn`` releases the
        per-engagement turn lock, so turn T1 can write and T2 can then run to
        completion before T1's delivery owner reads. With one slot per
        engagement, T2 overwrites or consumes T1's reason and one notice is
        lost or misattributed. Both reviewers reached this interleaving
        independently in the design round.

        Both turns here are cutoffs, and each owner must pop ITS OWN ticket.
        """
        from claude_agent_sdk import ClaudeAgentOptions
        from drivers.in_casa_driver import InCasaDriver
        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient", ScriptedCompleteClient)

        class _Handle:
            async def emit(self, text):
                pass

            async def finalize(self, text):
                pass

        drv = InCasaDriver(topic_stream_factory=lambda t: _Handle())
        rec = _record("e" * 32)
        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="s"))

        async def _truncated(self):
            from claude_agent_sdk import AssistantMessage, TextBlock
            yield AssistantMessage(content=[TextBlock(text="x")], model="m")

        monkeypatch.setattr(
            type(drv._clients[rec.id]), "receive_response", _truncated)
        t1 = drv.admit_inbound(rec.id, "first")
        t2 = drv.admit_inbound(rec.id, "second")
        # Both turns run to completion before EITHER owner reads — the
        # interleaving the per-engagement slot cannot survive.
        await drv.send_user_turn(rec, "first", inbound_token=t1)
        await drv.send_user_turn(rec, "second", inbound_token=t2)
        assert _pop_followup(drv, rec.id, t1) == _FOLLOWUP_MISSING_RESULT
        assert _pop_followup(drv, rec.id, t2) == _FOLLOWUP_MISSING_RESULT

    async def test_a_launch_turn_writes_nothing_into_the_follow_up_map(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: drop the ``inbound_token is not None`` half of the
        follow-up predicate, so a LAUNCH turn writes the follow-up slot too →
        this fails.

        This mutation SURVIVED the first mutation sweep, which is why the test
        exists. Its harm is bounded — a launch turn has no ticket, so the seam
        reads "" for it and no notice fires — but the two observations have
        different owners on purpose and an entry keyed by no ticket is a leak
        into a map nothing will ever pop except ``cancel()``. The reviewers
        named this mutation explicitly; nothing was killing it.
        """
        from claude_agent_sdk import ClaudeAgentOptions
        from drivers.in_casa_driver import InCasaDriver, LAUNCH_MISSING_RESULT
        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient", ScriptedCutoffClient)

        class _Handle:
            async def emit(self, text):
                pass

            async def finalize(self, text):
                pass

        drv = InCasaDriver(topic_stream_factory=lambda t: _Handle())
        rec = _record("9" * 32)
        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="s"))

        # The launch turn WAS a cutoff — its own slot proves the arrangement
        # reached the observation code at all, so this is not a vacuous pass.
        assert drv._followup_incomplete == {}
        assert drv.launch_turn_incomplete(rec.id) == LAUNCH_MISSING_RESULT

    async def test_cancel_drops_pending_follow_up_observations(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: leave the follow-up bucket behind in ``cancel()`` → this
        fails. Map hygiene, same line as the launch slot's."""
        from claude_agent_sdk import ClaudeAgentOptions
        from drivers.in_casa_driver import InCasaDriver
        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient", ScriptedCompleteClient)

        class _Handle:
            async def emit(self, text):
                pass

            async def finalize(self, text):
                pass

        drv = InCasaDriver(topic_stream_factory=lambda t: _Handle())
        rec = _record("f" * 32)
        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="s"))

        async def _truncated(self):
            from claude_agent_sdk import AssistantMessage, TextBlock
            yield AssistantMessage(content=[TextBlock(text="x")], model="m")

        monkeypatch.setattr(
            type(drv._clients[rec.id]), "receive_response", _truncated)
        token = drv.admit_inbound(rec.id, "doomed")
        await drv.send_user_turn(rec, "doomed", inbound_token=token)
        await drv.cancel(rec)
        assert drv._followup_incomplete == {}


class TestEveryBoundIsRealAndEveryCountIsOne:
    """MUTATIONS: remove the notice timeout / the paint bound / the close bound
    / the driver.cancel bound → the matching case below fails. Each wedged
    counterparty is an Event that is never set; no asyncio.sleep is patched."""

    async def test_a_notice_that_never_returns_still_reaches_the_close(
        self, tmp_path, monkeypatch,
    ):
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_TOPIC_OP_TIMEOUT_S", 0.05)
        probe = _Probe()
        engage_executor, registry, channel, _d = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        wedged = asyncio.Event()

        async def _hang(rec, text):
            probe.events.append("notice")
            await wedged.wait()

        channel._post_engagement_notice = AsyncMock(side_effect=_hang)

        envelope = await asyncio.wait_for(_launch(engage_executor), 15)

        assert (await _payload(envelope))["status"] == "error"
        assert channel._post_engagement_notice.await_count == 1
        assert probe.driver_cancel_calls == 1
        assert channel.close_topic.await_count == 1
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"

    async def test_a_paint_that_never_returns_still_reaches_the_close(
        self, tmp_path, monkeypatch,
    ):
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_TOPIC_OP_TIMEOUT_S", 0.05)
        probe = _Probe()
        engage_executor, _registry, channel, _d = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        wedged = asyncio.Event()

        async def _hang(*, engagement_id, new_state):
            await wedged.wait()

        channel.update_topic_state = AsyncMock(side_effect=_hang)

        envelope = await asyncio.wait_for(_launch(engage_executor), 15)

        assert (await _payload(envelope))["status"] == "error"
        assert channel.update_topic_state.await_count == 1
        assert channel.close_topic.await_count == 1

    async def test_a_close_that_never_returns_still_retires_the_reporter(
        self, tmp_path, monkeypatch,
    ):
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_TOPIC_OP_TIMEOUT_S", 0.05)
        probe = _Probe()
        engage_executor, _registry, channel, _d = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        wedged = asyncio.Event()

        async def _hang(*, thread_id):
            await wedged.wait()

        channel.close_topic = AsyncMock(side_effect=_hang)

        envelope = await asyncio.wait_for(_launch(engage_executor), 15)

        assert (await _payload(envelope))["status"] == "error"
        assert channel.close_topic.await_count == 1
        assert not tools_mod._LAUNCH_DEATH_TASKS

    async def test_a_driver_cancel_that_never_returns_still_tells_the_operator(
        self, tmp_path, monkeypatch,
    ):
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_DRIVER_CANCEL_TIMEOUT_S", 0.05)
        probe = _Probe()
        engage_executor, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        wedged = asyncio.Event()

        async def _hang(engagement):
            probe.driver_cancel_calls += 1
            await wedged.wait()

        monkeypatch.setattr(driver, "cancel", _hang)

        envelope = await asyncio.wait_for(_launch(engage_executor), 15)

        assert (await _payload(envelope))["status"] == "error"
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"
        assert channel._post_engagement_notice.await_count == 1
        assert probe.driver_cancel_calls == 1
        assert channel.close_topic.await_count == 1


class TestTheStrictTransitionIsTheAuthority:
    async def test_a_persist_failure_reports_nothing_and_retires_the_client(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: use `mark_error` instead of the strict primitive, or drop
        the PERSIST_FAILED branch → this fails.

        `mark_error` persists BEST-EFFORT: it swallows a tombstone-write
        failure and still returns True, which would authorize an irreversible
        topic close over a record disk still calls `active`. The strict
        primitive rolls the record back and re-raises, and the reporter then
        posts nothing, paints nothing and closes nothing — an open topic over a
        live record is recoverable, a closed one is not. It DOES retire the
        driver client: with no exception out of the driver, start()'s rollback
        never ran, so `is_alive` would hand the next operator message to an
        ended client instead of resuming from the persisted session id.
        """
        probe = _Probe()
        engage_executor, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )

        real_write = registry._write_tombstone

        def _boom_on_terminal(snapshot):
            # Fail ONLY the terminal write: create() persists strictly too, and
            # failing that would abort the launch before it ever ran a turn.
            if any(row.get("status") == "error" for row in snapshot):
                raise OSError("tombstone write refused")
            return real_write(snapshot)

        monkeypatch.setattr(registry, "_write_tombstone", _boom_on_terminal)

        envelope = await _launch(engage_executor)
        payload = await _payload(envelope)

        assert payload["status"] == "error", payload
        assert payload["kind"] == "launch_turn_incomplete"
        created_id = next(iter(registry._records))
        rec = registry.get(created_id)
        assert rec.status == "active", rec.status      # rolled fully back
        assert rec.completed_at is None
        assert channel._post_engagement_notice.await_count == 0
        assert channel.update_topic_state.await_count == 0
        assert channel.close_topic.await_count == 0
        assert probe.driver_cancel_calls == 1
        assert driver.is_alive(rec) is False

    async def test_a_lost_terminal_race_reports_nothing_and_stays_pending(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: ignore the strict transition's winner bool → this fails.

        The engagement reported itself between the turn's end and the owner's
        read (its own emit_completion, or a concurrent /complete or /cancel).
        That writer owns every side effect; this path must perform ZERO and
        tell the engager exactly what a successful launch is told.
        """
        probe = _Probe()
        engage_executor, registry, channel, _d = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        real_create = registry.create

        async def create_then_terminalize(*a, **kw):
            rec = await real_create(*a, **kw)
            # Stand in for emit_completion winning the flip durably.
            await registry.try_transition_terminal(
                rec.id, "completed", strict=True)
            return rec

        monkeypatch.setattr(registry, "create", create_then_terminalize)

        envelope = await _launch(engage_executor)
        payload = await _payload(envelope)

        assert payload["status"] == "pending", payload
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "completed"
        assert channel._post_engagement_notice.await_count == 0
        assert channel.update_topic_state.await_count == 0
        assert channel.close_topic.await_count == 0


class TestCancellationHasAnOwner:
    async def test_the_reporter_survives_its_launchers_cancellation(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: await the reporter unshielded → this fails.

        The registry deliberately KEEPS a durable terminal state and re-raises
        when a cancellation lands after its write committed, so an inline owner
        could commit the flip and then never post, paint or close — and a
        second, compensating reporter would correctly LOSE the transition and
        do nothing, leaving the durable error with no side-effect owner at all.
        """
        import tools as tools_mod
        probe = _Probe()
        engage_executor, registry, channel, _d = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        entered = asyncio.Event()
        real_notice = channel._post_engagement_notice

        async def _slow_notice(rec, text):
            entered.set()
            await asyncio.sleep(0)
            await asyncio.sleep(_COUNTERPARTY_LATENCY_S)
            await real_notice(rec, text)

        channel._post_engagement_notice = AsyncMock(side_effect=_slow_notice)

        task = asyncio.ensure_future(_launch(engage_executor))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The launcher is gone; the reporter finished the job anyway. AWAIT
        # THE REPORTER, do not proxy it (#695): a bare `asyncio.sleep(0)`
        # re-arms `loop._ready` through `call_soon`, so the loop never blocks
        # and a yield count measures no wall time at all — while the reporter
        # ahead of it must still cross three `asyncio.to_thread` hops carrying
        # six `os.fsync` calls. `_abort_launch_on_cancel` is synchronous, so
        # every reporter is anchored in `_LAUNCH_DEATH_TASKS` before the
        # cancelled launcher re-raises; snapshot the set (its own done-callback
        # mutates it) and await the tasks themselves. Same idiom as the
        # `_ABORT_BG_TASKS` drains in `test_engage_executor_tool.py`. Plain
        # `gather`: `_report_launch_death` promises never to raise, so a raise
        # must red this test rather than become a successful drain.
        pending = list(tools_mod._LAUNCH_DEATH_TASKS)
        if pending:
            await asyncio.gather(*pending)
            # `gather` returns WITHOUT yielding when every child is already
            # done — CPython's bpo-46672 fast path calls `_done_callback`
            # inline for those (`asyncio.tasks.gather`'s `done_futs` loop) —
            # so a reporter's own `_launch_death_done` may still be sitting in
            # `loop._ready`. This yield flushes callbacks that are ALREADY
            # QUEUED, which is deterministic and bounded at one turn; it is not
            # the wall-time proxy this test just stopped using, because nothing
            # is being waited FOR. Measured: without it, the unshielded-await
            # mutation fails here on a finished-but-still-anchored task instead
            # of on the close count it is supposed to catch.
            await asyncio.sleep(0)
        assert not tools_mod._LAUNCH_DEATH_TASKS
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"
        assert channel._post_engagement_notice.await_count == 1
        assert channel.close_topic.await_count == 1

    async def test_a_cancelled_launch_now_posts_before_it_closes(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: drop the reporter from the cancellation compensator →
        this fails.

        Before #678 a cancelled launch flipped the topic to failed and closed
        it having posted NOTHING: the operator saw a ❌ title over an empty
        topic and could not tell whether the task had mutated anything.
        """
        import tools as tools_mod
        probe = _Probe()
        engage_executor, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        entered = asyncio.Event()
        wedged = asyncio.Event()
        real_notice = channel._post_engagement_notice

        async def _slow_notice(rec, text):
            await asyncio.sleep(_COUNTERPARTY_LATENCY_S)
            await real_notice(rec, text)

        channel._post_engagement_notice = AsyncMock(side_effect=_slow_notice)

        async def _hang(engagement, prompt, options, expected_generation=None):
            entered.set()
            await wedged.wait()

        monkeypatch.setattr(driver, "start", _hang)

        task = asyncio.ensure_future(_launch(engage_executor))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Await the reporter itself, not a yield count (#695) — see the
        # sibling above for why 500 bare yields measure nothing. The trailing
        # emptiness assertion is new: the old loop's break condition only
        # IMPLIED that the reporter retired, and an implication inside a
        # best-effort loop is not an assertion.
        pending = list(tools_mod._LAUNCH_DEATH_TASKS)
        if pending:
            await asyncio.gather(*pending)
            # Flush the reporter's ALREADY-QUEUED `_launch_death_done`; see
            # the sibling above for why `gather` can return without yielding.
            await asyncio.sleep(0)
        assert not tools_mod._LAUNCH_DEATH_TASKS

        created_id = next(iter(registry._records))
        rec = registry.get(created_id)
        assert rec.status == "error", rec.status
        assert rec.origin["error_kind"] == "launch_cancelled"
        assert channel._post_engagement_notice.await_count == 1
        assert channel.close_topic.await_count == 1
        assert probe.events.index("notice") < probe.events.index("topic_close")


class TestTheBoundaries:
    async def test_a_healthy_claude_code_launch_is_untouched(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: call `launch_turn_incomplete` outside the in_casa branches
        → this fails.

        Neither `DriverProtocol` nor `ClaudeCodeDriver` defines the method, so
        a bare attribute access would raise AttributeError and the launcher's
        generic `except Exception` would mark a HEALTHY engagement errored and
        abort its topic.
        """
        import agent as agent_mod

        class _ClaudeCodeStub:
            """No `launch_turn_incomplete` — deliberately not a MagicMock,
            which would fabricate the attribute and hide the boundary."""

            def __init__(self):
                self.starts = 0

            async def start(self, rec, prompt=None, options=None,
                            expected_generation=None):
                self.starts += 1

            async def cancel(self, rec):
                pass

        probe = _Probe()
        engage_executor, registry, channel, _d = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
            driver_kind="claude_code",
        )
        stub = _ClaudeCodeStub()
        monkeypatch.setattr(
            agent_mod, "active_claude_code_driver", stub, raising=False)

        envelope = await _launch(engage_executor)
        payload = await _payload(envelope)

        assert stub.starts == 1
        assert payload["status"] == "pending", payload
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "active"
        assert probe.transition_calls == []
        assert channel._post_engagement_notice.await_count == 0
        assert channel.close_topic.await_count == 0

    async def test_inbound_admitted_during_a_dying_launch_is_disclosed(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: drop the terminal hook's inbound snapshot → this fails.

        Telegram admits an inbound ticket SYNCHRONOUSLY before its first await,
        and an expected-non-delivery path discharges it with no telling — so a
        message admitted while the launch turn was ending would vanish with the
        topic, unmentioned. The funnel discloses this shape; a new
        out-of-funnel terminal writer inherits the obligation.
        """
        probe = _Probe()
        engage_executor, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        real_start = driver.start

        async def start_then_admit(rec, prompt=None, options=None,
                                   expected_generation=None):
            await real_start(rec, prompt=prompt, options=options,
                             expected_generation=expected_generation)
            # The operator typed while the turn was ending.
            driver.admit_inbound(rec.id, "wait, also remove the old config")

        monkeypatch.setattr(driver, "start", start_then_admit)

        envelope = await _launch(engage_executor)
        assert (await _payload(envelope))["status"] == "error"

        assert len(probe.notice_texts) == 1
        notice = probe.notice_texts[0]
        assert "had no turn start recorded" in notice, notice
        assert "wait, also remove the old config" in notice, notice
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"


class TextBearingCutoffClient(ScriptedCutoffClient):
    """The production shape in full: the executor narrates, calls tools, gets
    results — and THEN the stream dies. Text WAS posted, so `no_visible_output`
    cannot catch it; only the missing ResultMessage can.

    Found by mutation-checking: the frozen red case's stream carries no text,
    so BOTH arms satisfy it and removing the missing-ResultMessage arm alone
    left it green. This test isolates that arm.
    """

    async def receive_response(self):
        from claude_agent_sdk import (
            AssistantMessage, TextBlock, ToolResultBlock, ToolUseBlock,
            UserMessage,
        )
        type(self).frames_yielded += 1
        yield AssistantMessage(
            content=[TextBlock(text="Removing the weather plugin now.")],
            model="claude-sonnet-4-6")
        use = ToolUseBlock(id="t1", name="plugin_remove", input={"name": "w"})
        type(self).frames_yielded += 1
        yield AssistantMessage(content=[use], model="claude-sonnet-4-6")
        res = ToolResultBlock(tool_use_id="t1", content="removed", is_error=False)
        type(self).frames_yielded += 1
        yield UserMessage(content=[res])
        # EOF mid-tool-loop. Text was posted; no ResultMessage ever arrives.


class TestTheMissingResultArmOnItsOwn:
    async def test_a_cutoff_that_already_posted_text_is_still_reported(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: drop the `missing_result_message` arm → this fails, and
        it is the ONLY test that does.

        This is the reported production shape: the executor said what it was
        doing, ran its tool, and then its turn died. The posted text is not a
        report — the work stopped mid-flight and nobody owns the outcome — so
        the record must not stay `active` behind a dead transport.
        """
        probe = _Probe()
        engage_executor, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, TextBearingCutoffClient,
        )
        envelope = await _launch(engage_executor)
        payload = await _payload(envelope)

        # Text really did reach the topic, so `no_visible_output` cannot fire.
        assert TextBearingCutoffClient.frames_yielded == 3
        assert TextBearingCutoffClient.result_messages_yielded == 0
        assert probe.finalize_count == 1
        assert probe.emit_count >= 1

        assert payload["status"] == "error", payload
        assert payload["kind"] == "launch_turn_incomplete"
        assert "without ResultMessage" in payload["message"]
        created_id = next(iter(registry._records))
        rec = registry.get(created_id)
        assert rec.status == "error"
        assert rec.origin["error_kind"] == "launch_turn_incomplete"
        assert channel._post_engagement_notice.await_count == 1
        assert channel.close_topic.await_count == 1
        assert probe.events.index("notice") < probe.events.index("topic_close")
        assert driver.is_alive(rec) is False


# ---------------------------------------------------------------------------
# #692/#678 R1 red case — the DELIVERY OWNER, end to end
# ---------------------------------------------------------------------------

try:
    from tests.test_in_casa_inbound_admission import (
        _ScriptedClient, _drain, _mk_channel, _mk_driver_rec,
    )
except ImportError:  # pragma: no cover — direct-path collection
    from test_in_casa_inbound_admission import (          # type: ignore
        _ScriptedClient, _drain, _mk_channel, _mk_driver_rec,
    )


def _mk_cutoff_frames():
    """The mid-tool-loop shape: an assistant tool_use and its tool_result,
    then EOF with no ResultMessage. Evidence has latched, so ``evidence_seen``
    cannot distinguish this from a finished turn — which is why the predicate
    is the missing result frame."""
    from claude_agent_sdk import (
        AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage,
    )
    use = ToolUseBlock(id="t-1", name="Read", input={"file_path": "/x"})
    res = ToolResultBlock(tool_use_id="t-1", content="ok", is_error=False)
    return [
        AssistantMessage(content=[use], model="claude-sonnet-4-6"),
        UserMessage(content=[res]),
    ]


def _mk_result():
    from claude_agent_sdk import ResultMessage
    rm = ResultMessage.__new__(ResultMessage)
    for k, v in dict(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="s", total_cost_usd=0.0, usage={}, result="",
        stop_reason=None, parent_tool_use_id=None,
    ).items():
        setattr(rm, k, v)
    return rm


_EXPECTED_CUT_OFF_NOTICE = (
    "This turn did not finish: its stream stopped without ResultMessage, the "
    "frame that marks a turn that ran to its end, so its outcome was never "
    "confirmed. Anything it already posted above may be partial, and work it "
    "started may already have taken effect \u2014 check before asking for it "
    "again."
)
"""The notice's exact sentence, duplicated here ON PURPOSE rather than
imported: a reworded production string must fail a test, and asserting the
WHOLE sentence (never a word of it) is what keeps the assertion from matching
something incidental.

It carries the TERMINAL-ARTIFACT FACT, and that is a requirement rather than a
stylistic choice. Sol specified the notice by the substring "without
ResultMessage"; Terra then observed that whole-sentence equality against a
sentence NOT containing it is weaker than the substring check it replaced — a
notice that omits why the turn is being reported would pass equality while
failing Sol's assertion. Both are asserted below: the whole sentence, and the
fact independently.

RE-SPECIFIED in the third diff round. The earlier sentence said "nothing was
reported back for it" and "send your message again if you want it retried".
Sol reproduced a ticketed cutoff that streamed one text block before EOF: the
text WAS posted, so the first clause was false, and the second invited the
operator to repeat work that had already taken effect. Both clauses are gone.
The regression case for that exact shape is
``test_a_cutoff_that_already_posted_text_does_not_claim_silence``."""


_EXPECTED_TERMINAL_UNCONFIRMED_NOTICE = (
    "This engagement has ended, and Casa could not confirm that its account of "
    "the outcome reached this topic. If a summary appears above, that is it."
)
"""RE-SPECIFIED in the first diff round, by the reviewer who did not accept
this red case, and re-accepted rather than edited on my own authority.

The terminal-and-untold arm used to post ``_EXPECTED_CUT_OFF_NOTICE``. Sol
reproduced why that is false: a Telegram ``TimedOut`` can lose the
acknowledgement of a completion summary the wire ACCEPTED, so the operator can
be looking at the summary while the funnel recorded the telling as unconfirmed
— and "this turn did not finish … anything it already posted above may be
partial" contradicts their screen. The turn did not fail there either; it ended
because it completed the engagement.

RE-SPECIFIED AGAIN at re-acceptance, and the acceptor was right: the first
correction still promised that "the outcome is still in the engagement record
and was delivered to whoever asked for the work". Both halves can be false at
once — the engager notification is best-effort and its failure is caught, and
the record stores the terminal STATE rather than the completion text — so a
notify that raises after an unconfirmed post makes that sentence a second false
reassurance in the same place the first one was removed from.

Written out rather than imported, for the same reason as the sentence below it.
Asserted by whole-sentence equality AND by the clauses that must NOT come back,
which protect different things."""


def _seam(drv):
    """What ``casa_core`` wires: SYNCHRONOUS, ``getattr``-guarded, "" for a
    driver that has no such accessor. Tolerant on purpose, so the PRE-FIX
    failure of the red case below is the SILENCE (zero notices) and not an
    AttributeError from a symbol that does not exist yet."""
    def _read(rec, token):
        if token is None:
            return ""
        fn = getattr(drv, "followup_turn_incomplete", None)
        return fn(rec.id, token) if callable(fn) else ""
    return _read


class TestTheFollowUpTurnHasAFailureOwner:
    """INV-ENG-012 red case. A ticketed follow-up turn cut off mid-tool-loop
    over a LIVE record is answered with exactly ONE bounded notice by the
    delivery TASK — and a healthy one with none.

    Real ``TelegramChannel``, real ``EngagementRegistry`` with an on-disk
    tombstone, real ``InCasaDriver``, and the real
    ``deliver_system_turn`` → tracked ``_deliver_turn_bg`` task → task-end
    ``_schedule_inbound_cleanup`` backstop chain. Terra RETURNED an earlier
    version of this red case that awaited ``_deliver_turn_bg`` directly:
    without the real spawn there is no tracked task and no done-callback
    cleanup task, so "both task sets drained" was vacuous and the ONE-notice
    count did not pin the delivery task rather than wrapper behaviour. It goes
    through the spawning entry point for that reason.

    No ``asyncio.sleep`` is patched anywhere.
    """

    @staticmethod
    def _capture_token(ch, drv):
        """Wrap the admit seam so the test holds the exact ticket
        ``deliver_system_turn`` creates for itself."""
        seen: list[object] = []
        inner = ch._driver_admit_inbound

        def _admit(rec, text):
            tok = inner(rec, text)
            seen.append(tok)
            return tok
        ch._driver_admit_inbound = _admit
        return seen

    @staticmethod
    def _capture_owner(ch, drv):
        """Record WHICH asyncio task reads the observation seam, and how many
        times the task-end cleanup backstop was scheduled.

        Terra RETURNED the previous version of this red case a second time on
        exactly this gap: a mutation that moved the pop and the notice out of
        ``_deliver_turn_bg`` and into ``_schedule_inbound_cleanup`` would still
        produce one notice, an empty second read and two drained task sets, so
        the test could not certify "popped exactly once BY THAT TURN'S DELIVERY
        TASK". Task IDENTITY is the pin that closes it — nothing weaker can,
        because every count is identical under that mutation.
        """
        callers: list[object] = []
        cleanup_calls: list[object] = []
        read = _seam(drv)

        def _observe(rec, token):
            callers.append(asyncio.current_task())
            return read(rec, token)
        ch._driver_turn_incomplete = _observe

        inner_cleanup = ch._schedule_inbound_cleanup

        def _cleanup(rec, token):
            cleanup_calls.append(token)
            return inner_cleanup(rec, token)
        ch._schedule_inbound_cleanup = _cleanup
        return callers, cleanup_calls

    async def test_a_cutoff_follow_up_gets_exactly_one_bounded_notice(
        self, tmp_path, fake_telegram_bot,
    ):
        client = _ScriptedClient(scripts=[_mk_cutoff_frames()])
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        tokens = self._capture_token(ch, drv)
        callers, cleanup_calls = self._capture_owner(ch, drv)

        spawn = asyncio.ensure_future(
            ch.deliver_system_turn(rec, "please finish the plugin work"))
        await spawn
        # deliver_system_turn returns having SPAWNED the delivery task, before
        # that task's first step — so the set holds exactly it, and its
        # identity is recoverable for the ownership assertion below.
        assert len(ch._turn_tasks) == 1
        delivery_task = next(iter(ch._turn_tasks))
        await _drain(ch)
        # bpo-46672: gather() over already-done tasks takes the fast path and
        # does NOT yield, so the tasks' own discard done-callbacks are still
        # QUEUED. One bare yield flushes what is already queued — nothing is
        # awaited for a duration and no sleep is patched.
        await asyncio.sleep(0)

        # The real spawn happened, and the stream really was the cutoff.
        assert len(tokens) == 1
        assert client.query_prompts == ["please finish the plugin work"]

        # EXACTLY one notice, and it is the whole sentence — not a word of it.
        assert ch._post_engagement_notice.await_count == 1
        _notice = ch._post_engagement_notice.await_args.args[1]
        assert _notice == _EXPECTED_CUT_OFF_NOTICE
        # And it is NOT the terminal-unconfirmed sentence: over a LIVE record
        # the turn really was cut off, and the two tellings are not
        # interchangeable (first diff round).
        assert _notice != _EXPECTED_TERMINAL_UNCONFIRMED_NOTICE
        # Sol's own assertion, kept independently of the equality above: the
        # notice must state the terminal-artifact fact, not merely that
        # something went wrong.
        assert "without ResultMessage" in _notice

        # OWNERSHIP. The seam was read exactly once, and the task that read it
        # IS the tracked delivery task — not the task-end cleanup backstop,
        # which did run (it is scheduled unconditionally) and must not be the
        # one that tells the operator. Every count in this test is identical
        # under a mutation that moves the pop into the backstop; only this
        # identity assertion fails under it.
        assert len(callers) == 1
        assert callers[0] is delivery_task
        assert len(cleanup_calls) == 1          # the backstop really ran
        assert cleanup_calls[0] is tokens[0]

        # And the observation was popped, so a second read reports nothing.
        assert _seam(drv)(rec, tokens[0]) == ""

        # Nothing terminal happened. The record is still live, read back from
        # the real registry.
        assert reg.get(rec.id).status == "active"
        assert reg.get(rec.id).completed_at is None
        assert drv.inbound_token_held(rec.id, tokens[0]) is False
        assert list(ch._turn_tasks) == []
        assert list(ch._inbound_cleanup_tasks) == []

    async def test_a_healthy_follow_up_gets_no_notice(
        self, tmp_path, fake_telegram_bot,
    ):
        """The CONTROL: the same delivery path, one ResultMessage, zero
        notices and zero side effects.

        MUTATION: drop the ``result_msg is None`` guard → this fails.
        """
        client = _ScriptedClient(
            scripts=[_mk_cutoff_frames() + [_mk_result()]])
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        tokens = self._capture_token(ch, drv)
        callers, cleanup_calls = self._capture_owner(ch, drv)

        await ch.deliver_system_turn(rec, "carry on")
        await _drain(ch)
        # bpo-46672: gather() over already-done tasks takes the fast path and
        # does NOT yield, so the tasks' own discard done-callbacks are still
        # QUEUED. One bare yield flushes what is already queued — nothing is
        # awaited for a duration and no sleep is patched.
        await asyncio.sleep(0)

        assert len(tokens) == 1
        assert ch._post_engagement_notice.await_count == 0
        # No observation exists to read — which is what makes a healthy
        # follow-up turn a silent success. Asserted on the OBSERVATION and not
        # on the owner's read count, so this control is green both before and
        # after the fix; the ownership pin belongs to the red case above.
        assert _seam(drv)(rec, tokens[0]) == ""
        assert len(cleanup_calls) == 1
        assert reg.get(rec.id).status == "active"
        assert drv.inbound_token_held(rec.id, tokens[0]) is False
        assert list(ch._turn_tasks) == []
        assert list(ch._inbound_cleanup_tasks) == []

    @pytest.mark.parametrize("terminal", ["completed", "cancelled", "error"])
    async def test_a_settled_terminal_record_gets_no_notice(
        self, tmp_path, fake_telegram_bot, terminal,
    ):
        """The healthy in_casa SELF-EMIT case, which is the reason the
        adjudication reads the record at all: ``emit_completion`` inside a
        follow-up turn commits the terminal transition and then
        ``_finalize_engagement_tail`` closes the engagement's own SDK client,
        so the turn can end with no ResultMessage on the HAPPY path. The
        terminal path owns that telling; this one must stay quiet.

        PARAMETERISED OVER EVERY TERMINAL STATUS, and that is the point rather
        than thoroughness for its own sake. A single-value version of this test
        covered only ``completed``, so NARROWING the guard's tuple to
        ``("completed",)`` left the whole changed suite green while a cancelled
        or reaped engagement got a false "this turn ended before it finished"
        notice — a reviewer reproduced exactly that. The suppression is a
        multi-value guard and every arm of it needs its own killing mutation;
        one arm standing in for three is how the previous two rounds' findings
        happened.

        MUTATIONS: remove the suppression entirely, or drop ANY ONE of the
        three statuses from it → the matching parameter fails.
        """
        client = _ScriptedClient(scripts=[_mk_cutoff_frames()])
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._driver_turn_incomplete = _seam(drv)
        self._capture_token(ch, drv)

        # Commit the terminal transition the way the owning path does, from
        # INSIDE the turn, so the SETTLED record is terminal by the time the
        # delivery task adjudicates.
        # R2: the REAL funnel, with a completion post that CONFIRMS. Terminal
        # status alone no longer suppresses, so the arrangement has to make the
        # terminal path genuinely tell the topic rather than assert an
        # internal fact about it — see
        # ``TestTerminalStatusIsNotProofOfATelling`` for the case this control
        # is the counterpart of.
        await _wire_the_real_funnel(ch, reg, post_confirms=True)

        async def _deliver_then_finalize(r, text, *, tg_message_id=None,
                                         inbound_token=None):
            from tools import _finalize_engagement
            await drv.send_user_turn(r, text, inbound_token=inbound_token)
            await _finalize_engagement(
                r, outcome=terminal, text="done", artifacts=[],
                next_steps=[], driver=None)
            return None
        ch._driver_send_user_turn = _deliver_then_finalize

        await ch.deliver_system_turn(rec, "finish and complete")
        await _drain(ch)

        # The arrangement really did reach the state under test.
        assert reg.get(rec.id).status == terminal
        assert ch._post_engagement_notice.await_count == 0

    async def test_a_cutoff_that_already_posted_text_does_not_claim_silence(
        self, tmp_path, fake_telegram_bot,
    ):
        """Sol's round-3 regression case: visible text AND a missing result.

        A cut-off turn can stream assistant text before its stream ends — the
        driver finalizes whatever it accumulated BEFORE recording the
        observation, deliberately, so a partial answer is not thrown away. The
        notice therefore must not say nothing was reported and must not invite
        a blind retry: for non-idempotent work ("I changed the thermostat
        setting") that advice makes the notice itself the harm.

        MUTATIONS: restore either clause of the old wording — "nothing was
        reported back for it" or "send your message again if you want it
        retried" — and the whole-sentence equality below fails.
        """
        from claude_agent_sdk import AssistantMessage, TextBlock
        frames = [AssistantMessage(
            content=[TextBlock(text="I changed the thermostat setting.")],
            model="claude-sonnet-4-6")]
        client = _ScriptedClient(scripts=[frames])
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._driver_turn_incomplete = _seam(drv)

        posted: list[str] = []

        class _Handle:
            async def emit(self, text):
                pass

            async def finalize(self, text):
                posted.append(text)

        drv._topic_stream_factory = lambda tid: _Handle()

        await ch.deliver_system_turn(rec, "turn the heating down")
        await _drain(ch)

        # The turn DID report something — the arrangement is the one under
        # test and not the silent case.
        assert posted == ["I changed the thermostat setting."]
        assert reg.get(rec.id).status == "active"

        # And the notice does not contradict it.
        assert ch._post_engagement_notice.await_count == 1
        notice = ch._post_engagement_notice.await_args.args[1]
        assert notice == _EXPECTED_CUT_OFF_NOTICE
        assert "without ResultMessage" in notice
        assert "nothing was reported" not in notice
        assert "send your message again" not in notice

    async def test_the_notice_is_bounded(self, tmp_path, fake_telegram_bot,
                                         monkeypatch):
        """MUTATION: remove the notice timeout → this test hangs instead of
        passing. The wedged counterparty is an Event that is never set; the
        production bound is shortened rather than any sleep being patched."""
        import channels.telegram as tg_mod
        monkeypatch.setattr(tg_mod, "_TURN_INCOMPLETE_NOTICE_TIMEOUT_S",
                            0.05, raising=False)
        client = _ScriptedClient(scripts=[_mk_cutoff_frames()])
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._driver_turn_incomplete = _seam(drv)

        wedged = asyncio.Event()
        attempts = []

        async def _hang(r, text):
            attempts.append(text)
            await wedged.wait()

        ch._post_engagement_notice = AsyncMock(side_effect=_hang)
        await ch.deliver_system_turn(rec, "x")
        await _drain(ch)
        # bpo-46672: gather() over already-done tasks takes the fast path and
        # does NOT yield, so the tasks' own discard done-callbacks are still
        # QUEUED. One bare yield flushes what is already queued — nothing is
        # awaited for a duration and no sleep is patched.
        await asyncio.sleep(0)
        assert len(attempts) == 1          # one attempt, and it returned
        assert list(ch._turn_tasks) == []


async def _wire_the_real_funnel(ch, reg, *, post_confirms):
    """Point ``tools._finalize_engagement`` at THIS real channel, so the
    terminal path's topic work is the production one and not a stand-in.

    The only things scripted are the COUNTERPARTIES — the two topic senders.
    ``_post_engagement_notice`` stays the channel's own AsyncMock, so the
    cut-off notice is counted separately from the funnel's sends.
    """
    import agent as agent_mod
    from tools import init_tools

    sends: list[str] = []

    async def _rich(thread_id, text, **kw):
        sends.append(text)
        if not post_confirms:
            raise RuntimeError("telegram 500")
        return 4242

    async def _plain(thread_id, text, **kw):
        sends.append(text)
        if not post_confirms:
            raise RuntimeError("telegram 500")
        return 4243

    ch.send_response_to_topic = _rich
    ch.send_to_topic = _plain
    ch.update_topic_state = AsyncMock()
    ch.close_topic = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = ch
    bus = MagicMock()
    bus.notify = AsyncMock()
    agent_mod.active_semantic_memory = None
    init_tools(
        channel_manager=cm, bus=bus, specialist_registry=MagicMock(),
        mcp_registry=MagicMock(), trigger_registry=MagicMock(),
        engagement_registry=reg,
    )
    return sends


class TestTerminalStatusIsNotProofOfATelling:
    """R2 red case (INV-ENG-012 x INV-ENG-013). The seam round found that
    suppressing the follow-up notice on the record's SETTLED TERMINAL STATUS is
    a PROXY for "the terminal path told the topic", and that the proxy is wrong
    in exactly the state this cluster's other arm creates.

    The sequence, assembled here out of REAL parts: a ticketed follow-up turn
    ends the engagement; the record commits terminal; the real finalize
    funnel's completion post fails; its ONE bounded plain disclosure fails too;
    the turn's stream ends with no ``ResultMessage``; the driver records the
    cutoff; and the delivery task reads a terminal record and stays quiet. Net:
    a terminal record, a topic that heard NOTHING, and zero notices — the
    defect this cluster exists to remove, reassembled from its two halves.

    Both cases below go through ``tools._finalize_engagement`` itself, and
    assert only NOTICE COUNTS. Nothing here names the mechanism by which the
    funnel makes its telling known — no accessor, no attribute, no storage
    shape — because the invariant is about the outcome and a red case that
    named the shape would pin the fix instead of the behaviour (Terra returned
    an earlier version of this class for exactly that). The mechanism's own
    mutation pins live in ``TestTheSettledStatusReadIsBoundedAndProduction``,
    where naming it is the point.

    The funnel runs after ``send_user_turn`` returns rather than from inside
    the tool call, which preserves the ordering production guarantees — the
    funnel's topic work completes before ``driver.cancel`` ends the response
    iterator, and therefore before the delivery task can adjudicate.

    MUTATION: suppress on terminal status alone → every parameter of the first
    test fails. MUTATION: always tell → the second test fails.
    """

    @staticmethod
    def _finalizing_delivery(ch, reg, drv, outcome):
        async def _deliver_then_finalize(r, text, *, tg_message_id=None,
                                         inbound_token=None):
            from tools import _finalize_engagement
            await drv.send_user_turn(r, text, inbound_token=inbound_token)
            await _finalize_engagement(
                r, outcome=outcome, text="done", artifacts=[], next_steps=[],
                driver=None)
            return None
        ch._driver_send_user_turn = _deliver_then_finalize

    @pytest.mark.parametrize("terminal", ["completed", "cancelled", "error"])
    async def test_a_terminal_record_that_told_nobody_still_gets_its_notice(
        self, tmp_path, fake_telegram_bot, terminal,
    ):
        """Parameterised over every terminal outcome because the suppression is
        a three-value guard, and one arm standing in for three is how two
        earlier rounds' findings happened."""
        client = _ScriptedClient(scripts=[_mk_cutoff_frames()])
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._driver_turn_incomplete = _seam(drv)
        sends = await _wire_the_real_funnel(ch, reg, post_confirms=False)
        self._finalizing_delivery(ch, reg, drv, terminal)

        await ch.deliver_system_turn(rec, "finish and complete")
        await _drain(ch)

        # The arrangement really did reach the state under test: the funnel ran,
        # its completion post was attempted, and the record is terminal. Both
        # of these hold on a PRE-FIX tree too, deliberately — an arrangement
        # assertion that only becomes true after the fix would mask the red
        # case's own reason.
        assert sends[0] == f"Engagement {terminal}. Summary:\ndone"
        assert reg.get(rec.id).status == terminal
        # ONE notice. The operator is not left holding a terminal record over
        # a topic that heard nothing. THIS is the red case's reason: pre-fix
        # the count is 0.
        assert ch._post_engagement_notice.await_count == 1
        _notice = ch._post_engagement_notice.await_args.args[1]
        # And it is the TERMINAL sentence, not the cut-off one — the
        # re-specification the first diff round produced. The engagement ended
        # because this turn completed it, and a lost acknowledgement is
        # indistinguishable from a failed send from here, so the operator may
        # be looking at the summary right now.
        assert _notice == _EXPECTED_TERMINAL_UNCONFIRMED_NOTICE
        # The two clauses that must never come back on this arm, asserted
        # independently of the equality above: a rewording that kept the
        # sentence's shape and reintroduced either would pass equality against
        # its own new text but not these.
        # THE RULE, after the same finding shape three times: this notice may
        # assert only the settled terminal status and that the telling was not
        # confirmed. Each claim is one a review found the notice could not see,
        # and each is asserted independently of the equality above — a
        # rewording that keeps the sentence's shape passes equality against its
        # own new text and only these catch the content. ONE list, shared with
        # the funnel's own disclosure test: two copies had already diverged
        # when an acceptor checked them.
        from test_finalize_engagement import FORBIDDEN_NOTICE_CLAIMS
        for _forbidden in FORBIDDEN_NOTICE_CLAIMS:
            assert _forbidden not in _notice, _forbidden
        # And the funnel had already made its OWN one bounded attempt and
        # failed it, so this notice is not a second telling of the same thing.
        assert len(sends) == 2

    async def test_a_terminal_record_that_did_tell_gets_no_notice(
        self, tmp_path, fake_telegram_bot,
    ):
        """The CONTROL, and the reason the suppression exists at all: the
        healthy in_casa self-emit completion also ends its turn with no
        ``ResultMessage``, and the terminal path DID tell the topic, so this
        one must stay quiet or every completing follow-up turn gets a spurious
        "did not finish" notice."""
        client = _ScriptedClient(scripts=[_mk_cutoff_frames()])
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._driver_turn_incomplete = _seam(drv)
        sends = await _wire_the_real_funnel(ch, reg, post_confirms=True)
        self._finalizing_delivery(ch, reg, drv, "completed")

        await ch.deliver_system_turn(rec, "finish and complete")
        await _drain(ch)

        assert reg.get(rec.id).status == "completed"
        assert len(sends) == 1                  # the summary, confirmed
        assert ch._post_engagement_notice.await_count == 0


class TestTheSettledStatusReadIsBoundedAndProduction:
    """#678 first-diff-round reviewer findings. Not part of the accepted red
    case; these kill mutations of the settled-status read and of the
    production seam that installs the observation accessor."""

    async def test_a_terminal_record_with_no_receipt_at_all_gets_its_notice(
        self, tmp_path, fake_telegram_bot,
    ):
        """MUTATION: default an ABSENT terminal-telling answer to "told" →
        this fails (0 notices).

        The record is terminal but no terminal telling was ever recorded for
        it — the funnel never ran through this registry, or ran and could not
        record — so whether the topic heard anything is UNKNOWN. Unknown must
        tell: this issue is about silence, so that is the fail-open direction.
        """
        client = _ScriptedClient(scripts=[_mk_cutoff_frames()])
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._driver_turn_incomplete = _seam(drv)

        async def _deliver_then_finalize(r, text, *, tg_message_id=None,
                                         inbound_token=None):
            await drv.send_user_turn(r, text, inbound_token=inbound_token)
            await reg.try_transition_terminal(r.id, "completed", strict=True)
            return None                      # deliberately NO telling recorded
        ch._driver_send_user_turn = _deliver_then_finalize

        await ch.deliver_system_turn(rec, "finish and complete")
        await _drain(ch)

        assert reg.get(rec.id).status == "completed"
        assert ch._post_engagement_notice.await_count == 1

    async def test_a_settled_read_that_raises_still_tells(
        self, tmp_path, fake_telegram_bot, monkeypatch,
    ):
        """MUTATION: let the read's exception SUPPRESS the notice → this fails.
        A read that cannot answer is not an answer of "told"."""
        client = _ScriptedClient(scripts=[_mk_cutoff_frames()])
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._driver_turn_incomplete = _seam(drv)
        reads = []

        async def _boom(engagement_id):
            reads.append(engagement_id)
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(reg, "settled_terminal_state", _boom,
                            raising=False)

        await ch.deliver_system_turn(rec, "please finish")
        await _drain(ch)

        assert reads == [rec.id]              # the read really was reached
        assert ch._post_engagement_notice.await_count == 1

    async def test_a_wedged_registry_read_still_reaches_the_notice(
        self, tmp_path, fake_telegram_bot, monkeypatch,
    ):
        """MUTATION: remove the bound around the settled-status read → this
        test hangs instead of passing. MUTATION: make the timeout path
        SUPPRESS instead of tell → the notice count becomes 0 and this fails.

        Both reviewers found that nothing referenced the settled read or its
        timeout: the only bound under test wedged the NOTICE, not the registry
        read. The read acquires the registry lock, which is held across a
        tombstone write, and it runs on a Telegram delivery task — so an
        unbounded read there is a delivery task that can wait forever and
        never tell the operator anything.

        Fail-open direction is asserted, not just the bound: a read that does
        not return means the record's status is UNKNOWN, and on this path an
        unknown status must produce the notice, because the harm being fixed
        is silence.

        The counterparty is an Event that is never set; the production
        constant is shortened. No asyncio.sleep is patched.
        """
        import channels.telegram as tg_mod
        monkeypatch.setattr(tg_mod, "_SETTLED_STATUS_TIMEOUT_S", 0.05)
        client = _ScriptedClient(scripts=[_mk_cutoff_frames()])
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._driver_turn_incomplete = _seam(drv)

        wedged = asyncio.Event()
        reads = []

        async def _hang(engagement_id):
            reads.append(engagement_id)
            await wedged.wait()
            return ("active", False)

        monkeypatch.setattr(reg, "settled_terminal_state", _hang,
                            raising=False)

        await ch.deliver_system_turn(rec, "please finish")
        await _drain(ch)

        assert reads == [rec.id]                  # the read really was reached
        assert ch._post_engagement_notice.await_count == 1
        assert (ch._post_engagement_notice.await_args.args[1]
                == _EXPECTED_CUT_OFF_NOTICE)

    async def test_the_settled_read_really_waits_on_the_registry_lock(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: drop the ``async with self._lock`` from
        ``EngagementRegistry.settled_terminal_state`` → this fails, twice over
        (the read completes while the transition is still in flight, and it
        returns the transient terminal value instead of what settled).

        The mutation sweep found this gap and it is worth naming: the sibling
        test above monkeypatches ``settled_terminal_state`` wholesale, so it
        pins what the CHANNEL does with the answer and cannot see how the
        registry produced it. Removing the lock left it green. This one calls
        the real accessor against a real transition.

        The arrangement is the window the lock exists for. A strict terminal
        transition commits the record's terminal fields in memory, then awaits
        the tombstone write, then FULL-FIELD restores them if that write fails
        — all while holding the lock. A reader that does not take the lock
        lands inside that window and sees ``completed`` for a transition that
        rolls back live.

        The wedged counterparty is an Event that is never set until the test
        sets it; no ``asyncio.sleep`` is patched and nothing is awaited for a
        duration.
        """
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "in_casa", "t",
            {"role": "assistant", "channel": "telegram"}, topic_id=901)

        inside = asyncio.Event()
        release = asyncio.Event()
        real_write = reg._write_tombstone_locked

        async def _blocking_write(*a, strict: bool = False, **kw):
            if not strict:                       # the create's own write
                return await real_write(*a, strict=strict, **kw)
            inside.set()
            await release.wait()
            # What the real writer records for a write that did NOT settle
            # (engagement_registry.py:739). The strict path's rollback is
            # guarded on this flag, so a double that skipped it would leave the
            # record terminal and the arrangement would not be the one under
            # test.
            reg._last_tombstone_ok = False
            raise RuntimeError("tombstone write failed")

        monkeypatch.setattr(reg, "_write_tombstone_locked", _blocking_write)

        flip = asyncio.ensure_future(
            reg.try_transition_terminal(rec.id, "completed", strict=True))
        await inside.wait()
        # In memory the record ALREADY reads terminal — the transient value a
        # lock-free reader would return.
        assert reg.get(rec.id).status == "completed"

        read = asyncio.ensure_future(reg.settled_terminal_state(rec.id))
        for _ in range(20):                      # queued work only, no duration
            await asyncio.sleep(0)
        # THE PIN: the read has not answered, because it is waiting for the
        # transition to settle.
        assert read.done() is False

        release.set()
        with pytest.raises(RuntimeError):
            await flip
        status, told = await read

        # And what it answers is what SETTLED: the transition rolled back.
        assert (status, told) == ("active", False)
        assert reg.get(rec.id).status == "active"

    async def test_the_settled_read_is_what_suppresses_not_a_lockfree_read(
        self, tmp_path, fake_telegram_bot, monkeypatch,
    ):
        """MUTATION: read ``registry.get(...).status`` instead of
        ``settled_terminal_state`` → this fails.

        The record's in-memory status says ``completed`` — the transient value
        a strict transition commits before its tombstone write, which a
        persist failure then fully rolls back — while the SETTLED answer is
        ``active``. A lock-free reader would suppress the notice the operator
        is owed; the settled reader must not.
        """
        client = _ScriptedClient(scripts=[_mk_cutoff_frames()])
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._driver_turn_incomplete = _seam(drv)

        real = getattr(reg, "settled_terminal_state", None)

        async def _settled(engagement_id):
            # What the lock reveals once the in-flight transition settles: the
            # transition rolled back, so the record is LIVE and no terminal
            # path ever told this topic anything.
            if real is not None:
                await real(engagement_id)
            return ("active", False)

        monkeypatch.setattr(reg, "settled_terminal_state", _settled,
                            raising=False)

        await ch.deliver_system_turn(rec, "please finish")
        # Only NOW does the lock-free view show the transient terminal value,
        # which is what a mutated implementation would read.
        rec.status = "completed"
        await _drain(ch)

        assert reg.get(rec.id).status == "completed"   # the lock-free lie
        assert ch._post_engagement_notice.await_count == 1


class TestTheProductionSeamIsTheOneUnderTest:
    """MUTATIONS of ``casa_core.read_followup_incomplete`` — the seam the
    production wiring installs on the channel. A reviewer observed that every
    other test injects an equivalent closure of its own, so the shipped one
    was covered by nothing at all."""

    async def test_a_claude_code_record_reads_empty(self, tmp_path):
        """MUTATION: drop the claude_code guard → this fails. That driver owns
        its own failure reporting and has no such accessor."""
        import casa_core
        reg, rec, drv = await _mk_driver_rec(tmp_path, _ScriptedClient())
        rec.driver = "claude_code"
        token = drv.admit_inbound(rec.id, "x")
        drv._followup_incomplete.setdefault(rec.id, {})[token] = "would-report"
        assert casa_core.read_followup_incomplete(drv, rec, token) == ""

    async def test_a_lookup_without_the_ticket_never_reads_another_turns(
        self, tmp_path,
    ):
        """MUTATION: make the accessor fall back to "any reason in this
        engagement's bucket" when the exact ticket is not found → this fails.

        This test replaced one that asserted a ``token is None`` guard. The
        generalised mutation sweep showed that guard SURVIVING every arm —
        not because the test was weak but because the guard was redundant: the
        map is keyed by ticket, the writing branch requires a non-None one, so
        a lookup with no ticket returns "" on its own. Third recurrence of the
        "new guard has no killing mutation" shape in this mechanism, so the
        guard was CUT and this test now pins the property that actually holds
        the weight — the accessor is TOTAL over a ticket it does not hold, and
        never substitutes another turn's observation for it.

        The bucket is deliberately NON-empty, which the previous version's
        arrangement was not: with an empty bucket the assertion passed however
        the lookup was implemented.
        """
        import casa_core
        from drivers.in_casa_driver import FOLLOWUP_MISSING_RESULT
        reg, rec, drv = await _mk_driver_rec(tmp_path, _ScriptedClient())
        mine = drv.admit_inbound(rec.id, "mine")
        drv._followup_incomplete.setdefault(
            rec.id, {})[mine] = FOLLOWUP_MISSING_RESULT

        assert casa_core.read_followup_incomplete(drv, rec, None) == ""
        assert casa_core.read_followup_incomplete(drv, rec, object()) == ""
        # And the real ticket still resolves, so the arrangement was live.
        assert casa_core.read_followup_incomplete(drv, rec, mine) == \
            FOLLOWUP_MISSING_RESULT

    async def test_a_driver_without_the_accessor_reads_empty(self, tmp_path):
        """MUTATION: replace the ``getattr`` guard with a direct attribute
        access → this raises AttributeError. The accessor is deliberately NOT
        on DriverProtocol, so a driver that lacks it must read as "nothing to
        report" rather than surface a failure for a healthy turn."""
        import casa_core
        reg, rec, drv = await _mk_driver_rec(tmp_path, _ScriptedClient())

        class _Bare:
            pass

        assert casa_core.read_followup_incomplete(_Bare(), rec, object()) == ""

    async def test_the_real_observation_is_read_through_the_seam(self, tmp_path):
        """The positive case: the shipped seam returns what the real driver
        recorded, and POPS it."""
        import casa_core
        from drivers.in_casa_driver import FOLLOWUP_MISSING_RESULT
        reg, rec, drv = await _mk_driver_rec(
            tmp_path, _ScriptedClient(scripts=[[_mk_assistant_frame()]]))
        token = drv.admit_inbound(rec.id, "cut off")
        await drv.send_user_turn(rec, "cut off", inbound_token=token)
        assert casa_core.read_followup_incomplete(drv, rec, token) == \
            FOLLOWUP_MISSING_RESULT
        assert casa_core.read_followup_incomplete(drv, rec, token) == ""


def _mk_assistant_frame():
    from claude_agent_sdk import AssistantMessage, TextBlock
    return AssistantMessage(content=[TextBlock(text="working")], model="m")
