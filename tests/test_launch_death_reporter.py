"""#678 — the launch-death reporter's count and mutation pins.

A launch that ends after its record exists without a running job — cancelled
while its session opens, or caught by Casa's stop — is reported by ONE owner,
``tools._report_launch_death``: one strict terminal transition, then one
bounded notice while the topic is still open, the bounded driver teardown, and
the topic close. Everything here exists to make one specific mutation of that
owner fail; the mapping is named on each test. (A launch turn that is cut off
once the session runs is a session loss, owned by session recovery, and is
pinned with the job session.)

No ``asyncio.sleep`` is patched anywhere: a wedged counterparty is modelled as
an ``asyncio.Event`` that is never set, and the production bound is shortened
instead. Patching ``<module>.asyncio.sleep`` is the shared module attribute and
is forbidden in this repo (it once OOM-killed the whole VM).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

try:
    from tests.test_in_casa_launch_terminal_artifact import (
        ScriptedCompleteClient, ScriptedCutoffClient, _Probe, _build, _launch,
    )
except ImportError:  # pragma: no cover — direct-path collection
    from test_in_casa_launch_terminal_artifact import (
        ScriptedCompleteClient, ScriptedCutoffClient, _Probe, _build, _launch,
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
machine instead of only on a loaded CI runner."""


async def _drain_owner():
    """Await every anchored launch-death reporter (and its telling) before
    asserting what it left behind."""
    import tools as tools_mod
    await tools_mod.drain_launch_death_reports()
    await asyncio.sleep(0)


async def _drain_reporters():
    """Await the reporters themselves, not a yield count (#695): snapshot the
    anchored set (its own done-callback mutates it) and gather it. Plain
    ``gather``: ``_report_launch_death`` promises never to raise, so a raise
    must red the test rather than become a successful drain."""
    import tools as tools_mod
    pending = list(tools_mod._LAUNCH_DEATH_TASKS)
    if pending:
        await asyncio.gather(*pending)
        # ``gather`` returns WITHOUT yielding when every child is already done
        # (CPython's bpo-46672 fast path), so a reporter's own done callback
        # may still be queued; one yield flushes what is ALREADY queued.
        await asyncio.sleep(0)


async def _cancel_while_opening(launcher, driver, monkeypatch, *, during=None):
    """Run one launch whose session open never returns, and cancel the
    launcher there — after its record and topic exist. ``during(rec)`` runs
    while the open hangs. Returns once the cancelled launcher re-raised."""
    entered = asyncio.Event()
    wedged = asyncio.Event()
    seen: list = []

    async def _hang(engagement, options=None):
        seen.append(engagement)
        entered.set()
        await wedged.wait()

    monkeypatch.setattr(driver, "open", _hang)
    task = asyncio.ensure_future(_launch(launcher))
    await asyncio.wait_for(entered.wait(), 5)
    if during is not None:
        during(seen[0])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return seen[0]


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
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        wedged = asyncio.Event()

        async def _hang(rec, text):
            probe.events.append("notice")
            await wedged.wait()

        channel._post_engagement_notice = AsyncMock(side_effect=_hang)

        await _cancel_while_opening(launcher, driver, monkeypatch)
        await asyncio.wait_for(_drain_owner(), 15)

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
        launcher, _registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        wedged = asyncio.Event()

        async def _hang(*, engagement_id, new_state):
            await wedged.wait()

        channel.update_topic_state = AsyncMock(side_effect=_hang)

        await _cancel_while_opening(launcher, driver, monkeypatch)
        await asyncio.wait_for(_drain_owner(), 15)

        assert channel.update_topic_state.await_count == 1
        assert channel.close_topic.await_count == 1

    async def test_a_close_that_never_returns_still_retires_the_reporter(
        self, tmp_path, monkeypatch,
    ):
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_TOPIC_OP_TIMEOUT_S", 0.05)
        probe = _Probe()
        launcher, _registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        wedged = asyncio.Event()

        async def _hang(*, thread_id):
            await wedged.wait()

        channel.close_topic = AsyncMock(side_effect=_hang)

        await _cancel_while_opening(launcher, driver, monkeypatch)
        await asyncio.wait_for(_drain_owner(), 15)

        assert channel.close_topic.await_count == 1
        assert not tools_mod._LAUNCH_DEATH_TASKS

    async def test_a_driver_cancel_that_never_returns_still_tells_the_operator(
        self, tmp_path, monkeypatch,
    ):
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_DRIVER_CANCEL_TIMEOUT_S", 0.05)
        probe = _Probe()
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        wedged = asyncio.Event()

        async def _hang(engagement):
            probe.driver_cancel_calls += 1
            await wedged.wait()

        monkeypatch.setattr(driver, "cancel", _hang)

        await _cancel_while_opening(launcher, driver, monkeypatch)
        await asyncio.wait_for(_drain_owner(), 15)

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
        paints nothing and closes nothing — an open topic over a live record
        is recoverable, a closed one is not. It DOES retire the driver's
        session, so `is_alive` never hands the next message to a client the
        launch abandoned.
        """
        probe = _Probe()
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )

        real_write = registry._write_tombstone

        def _boom_on_terminal(snapshot):
            # Fail ONLY the terminal write: create() persists strictly too, and
            # failing that would abort the launch before its session opened.
            if any(row.get("status") == "error" for row in snapshot):
                raise OSError("tombstone write refused")
            return real_write(snapshot)

        monkeypatch.setattr(registry, "_write_tombstone", _boom_on_terminal)

        await _cancel_while_opening(launcher, driver, monkeypatch)
        await _drain_owner()

        created_id = next(iter(registry._records))
        rec = registry.get(created_id)
        assert rec.status == "active", rec.status      # rolled fully back
        assert rec.completed_at is None
        # The reporter paints and closes nothing over a live record; its owner
        # posts one bounded notice saying the outcome could not be recorded —
        # that is the one notice.
        assert channel._post_engagement_notice.await_count == 1
        assert "could not be recorded" in probe.notice_texts[0]
        assert channel.update_topic_state.await_count == 0
        assert channel.close_topic.await_count == 0
        assert probe.driver_cancel_calls == 1
        assert driver.is_alive(rec) is False

    async def test_a_lost_terminal_race_reports_nothing_and_stays_pending(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: ignore the strict transition's winner bool → this fails.

        The engagement reported itself before the reporter's transition (its
        own emit_completion, or a concurrent /complete or /cancel). That writer
        owns every side effect; the reporter performs ZERO and leaves the
        winner's outcome standing.
        """
        probe = _Probe()
        launcher, registry, channel, driver = _build(
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

        await _cancel_while_opening(launcher, driver, monkeypatch)
        await _drain_owner()

        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "completed"
        assert len(probe.transition_calls) == 2     # the winner's, then the loser's
        assert channel._post_engagement_notice.await_count == 0
        assert channel.update_topic_state.await_count == 0
        assert channel.close_topic.await_count == 0


class TestCancellationHasAnOwner:
    async def test_the_reporter_survives_its_launchers_cancellation(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: await the reporter unshielded → this fails.

        Casa's stop latched while the session opened, so the launcher reports
        the death inline and then returns its envelope. The registry
        deliberately KEEPS a durable terminal state and re-raises when a
        cancellation lands after its write committed, so a launcher cancelled
        inside that report could commit the flip and then never post, paint
        or close — and a second, compensating reporter would correctly LOSE
        the transition and do nothing, leaving the durable error with no
        side-effect owner at all.
        """
        import tools as tools_mod
        probe = _Probe()
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCompleteClient,
        )
        entered = asyncio.Event()
        real_notice = channel._post_engagement_notice

        async def _slow_notice(rec, text):
            entered.set()
            await asyncio.sleep(0)
            await asyncio.sleep(_COUNTERPARTY_LATENCY_S)
            await real_notice(rec, text)

        channel._post_engagement_notice = AsyncMock(side_effect=_slow_notice)
        real_open = driver.open

        async def _open_then_the_stop_latches(engagement, options=None):
            await real_open(engagement, options=options)
            monkeypatch.setattr(registry, "launch_shutdown_active", lambda: True)

        monkeypatch.setattr(driver, "open", _open_then_the_stop_latches)

        task = asyncio.ensure_future(_launch(launcher))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The launcher is gone; the reporter finished the job anyway.
        await _drain_reporters()
        assert not tools_mod._LAUNCH_DEATH_TASKS
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"
        assert registry.get(created_id).origin["error_kind"] == "launch_cancelled"
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
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        real_notice = channel._post_engagement_notice

        async def _slow_notice(rec, text):
            await asyncio.sleep(_COUNTERPARTY_LATENCY_S)
            await real_notice(rec, text)

        channel._post_engagement_notice = AsyncMock(side_effect=_slow_notice)

        # The launcher awaits `open()` inline; a launch cancelled while its
        # session is opening is cancelled there.
        await _cancel_while_opening(launcher, driver, monkeypatch)

        await _drain_reporters()
        assert not tools_mod._LAUNCH_DEATH_TASKS

        created_id = next(iter(registry._records))
        rec = registry.get(created_id)
        assert rec.status == "error", rec.status
        assert rec.origin["error_kind"] == "launch_cancelled"
        assert channel._post_engagement_notice.await_count == 1
        assert channel.close_topic.await_count == 1
        assert probe.events.index("notice") < probe.events.index("topic_close")


class TestTheBoundaries:
    async def test_inbound_admitted_during_a_dying_launch_is_not_listed(
        self, tmp_path, monkeypatch,
    ):
        """A message the operator sent while the launch was dying is not
        listed in the death notice: the notice names the launch's fault only.
        What the message is owed is the journal's to tell."""
        probe = _Probe()
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )

        await _cancel_while_opening(
            launcher, driver, monkeypatch,
            during=lambda rec: driver.submit_operator(
                rec, "wait, also remove the old config", tg_message_id=7))
        await _drain_owner()

        assert len(probe.notice_texts) == 1
        notice = probe.notice_texts[0]
        assert "wait, also remove the old config" not in notice, notice
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"

    async def test_a_message_accepted_during_a_dying_launch_is_told_lost_once(
        self, tmp_path, monkeypatch,
    ):
        import job_inbox
        probe = _Probe()
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        channel.tell_journal_lost = AsyncMock()

        rec = await _cancel_while_opening(
            launcher, driver, monkeypatch,
            during=lambda rec: driver.submit_operator(
                rec, "wait, also remove the old config", tg_message_id=7))
        await _drain_owner()

        assert registry.get(rec.id).status == "error"
        assert channel.tell_journal_lost.await_count == 1
        assert job_inbox.load(rec.id) == []
