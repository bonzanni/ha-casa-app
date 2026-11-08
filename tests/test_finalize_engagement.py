"""Tests for the shared _finalize_engagement helper in tools.py."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit


def _driver_double():
    """A driver double whose ``cancel`` is awaitable, like ``InCasaDriver``'s."""
    d = MagicMock()
    d.cancel = AsyncMock()
    return d


class TestFinalizeEngagement:
    async def test_happy_path_closes_topic_and_notifies_ellen(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t",
            origin={"role": "assistant", "channel": "telegram", "chat_id": "12345"},
            topic_id=42,
        )

        telegram = MagicMock()
        telegram.send_to_topic = AsyncMock()
        telegram.send_response_to_topic = AsyncMock()
        telegram.close_topic = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = telegram
        bus = MagicMock()
        bus.notify = AsyncMock()

        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        driver = _driver_double()

        await _finalize_engagement(
            rec, outcome="completed", text="summary",
            next_steps=[], driver=driver,
        )

        # Topic closed + icon flipped
        telegram.close_topic.assert_awaited_once_with(thread_id=42)
        # Completion message posted in topic
        telegram.send_response_to_topic.assert_awaited()  # v0.109.0 rich summary
        # NOTIFICATION sent to Ellen
        bus.notify.assert_awaited_once()
        # Driver cancelled
        driver.cancel.assert_awaited_once_with(rec)
        # Record status is completed
        assert rec.status == "completed"
        assert rec.completed_at is not None

    async def test_cancel_outcome_uses_cancel_path(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )

        telegram = MagicMock()
        telegram.send_to_topic = AsyncMock()
        telegram.send_response_to_topic = AsyncMock()
        telegram.close_topic = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = telegram
        bus = MagicMock()
        bus.notify = AsyncMock()

        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        driver = _driver_double()

        await _finalize_engagement(
            rec, outcome="cancelled", text="user cancelled",
            next_steps=[], driver=driver,
        )
        assert rec.status == "cancelled"
        driver.cancel.assert_awaited_once_with(rec)


class TestFinalizeU3Transition:
    """E-12 (v0.37.0) Task 23: terminal-state U3 flip from _finalize_engagement."""

    async def _make_setup(self, outcome, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="executor", role_or_type="configurator",
            driver="in_casa", task="t",
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "12345"},
            topic_id=42,
        )

        telegram = MagicMock()
        telegram.send_to_topic = AsyncMock()
        telegram.send_response_to_topic = AsyncMock()
        telegram.close_topic = AsyncMock()
        telegram.update_topic_state = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = telegram
        bus = MagicMock()
        bus.notify = AsyncMock()

        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        driver = _driver_double()

        await _finalize_engagement(
            rec, outcome=outcome, text="x",
            next_steps=[], driver=driver,
        )
        return telegram, rec

    async def test_completed_flips_topic_to_completed(self, tmp_path):
        telegram, rec = await self._make_setup("completed", tmp_path)
        telegram.update_topic_state.assert_awaited_once_with(
            engagement_id=rec.id, new_state="completed",
        )

    async def test_cancelled_flips_topic_to_cancelled(self, tmp_path):
        telegram, rec = await self._make_setup("cancelled", tmp_path)
        telegram.update_topic_state.assert_awaited_once_with(
            engagement_id=rec.id, new_state="cancelled",
        )

    async def test_error_flips_topic_to_failed(self, tmp_path):
        telegram, rec = await self._make_setup("error", tmp_path)
        telegram.update_topic_state.assert_awaited_once_with(
            engagement_id=rec.id, new_state="failed",
        )

    async def test_state_update_failure_does_not_block_close(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="executor", role_or_type="configurator",
            driver="in_casa", task="t",
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "12345"},
            topic_id=42,
        )

        telegram = MagicMock()
        telegram.send_to_topic = AsyncMock()
        telegram.send_response_to_topic = AsyncMock()
        telegram.close_topic = AsyncMock()
        telegram.update_topic_state = AsyncMock(
            side_effect=RuntimeError("telegram down"),
        )
        cm = MagicMock()
        cm.get.return_value = telegram

        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        await _finalize_engagement(
            rec, outcome="completed", text="x",
            next_steps=[], driver=None,
        )
        # Close still happened despite the state-update failure.
        telegram.close_topic.assert_awaited_once()
        assert rec.status == "completed"


# --- G4 D5 (v0.96.0): typed finalize result --------------------------------


def test_finalize_result_enum_exists():
    from tools import FinalizeResult
    assert {r.name for r in FinalizeResult} >= {
        "FINALIZED", "ALREADY_TERMINAL", "PRECONDITION_FAILED",
        "PERSIST_FAILED"}
    # Truthiness contract preserved for existing boolean callers: only a
    # won finalize is truthy.
    assert bool(FinalizeResult.FINALIZED) is True
    assert bool(FinalizeResult.ALREADY_TERMINAL) is False
    assert bool(FinalizeResult.PERSIST_FAILED) is False
    assert bool(FinalizeResult.PRECONDITION_FAILED) is False


def test_terminal_hook_abort_raises_and_leaves_live():
    """G4 D2: the registry evaluates the terminal hook INSIDE the mutation
    critical section; an abort leaves the record live."""
    import asyncio
    from engagement_registry import (
        EngagementRegistry, TerminalPreconditionFailed)

    async def run(tmp_path="/tmp"):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            reg = EngagementRegistry(tombstone_path=td + "/e.json")
            rec = await reg.create(
                kind="executor", role_or_type="configurator",
                driver="in_casa", task="t",
                origin={"role": "assistant"}, topic_id=1)
            calls = []
            def hook():
                calls.append(1)
                return "unread_inbound depth=1"
            try:
                await reg.try_transition_terminal(
                    rec.id, "completed", strict=True, terminal_hook=hook)
            except TerminalPreconditionFailed as exc:
                assert "unread_inbound" in str(exc)
            else:
                raise AssertionError("expected TerminalPreconditionFailed")
            assert calls == [1]
            assert rec.status not in ("completed", "cancelled", "error")
            # hook returning None proceeds
            won = await reg.try_transition_terminal(
                rec.id, "completed", strict=True, terminal_hook=lambda: None)
            assert won is True
            assert rec.status == "completed"
    asyncio.get_event_loop().run_until_complete(run()) if False else None
    asyncio.run(run())


# ---------------------------------------------------------------------------
# #678 R2(i) red case — INV-ENG-013: the paint and the close wait for a
# CONFIRMED completion post
# ---------------------------------------------------------------------------


# THE RULE, and where it is actually enforced.
#
# An unconfirmed-completion notice may assert ONLY what its own site observed:
# the settled terminal status (durable — the strict transition commits memory
# and disk together or neither), that the telling was not confirmed, and, for
# the funnel, that the MARK is being withheld, which is the decision that very
# branch is taking. Three reviews found three violations of it — "this turn did
# not finish … may be partial", "the outcome was delivered to the engager and is
# in the engagement record", "this topic is closed … deleted at its retention
# deadline" — and the class of claim was cut from both notices rather than
# reworded a fourth time.
#
# A LEXICAL BLACKLIST OF FORBIDDEN PHRASES STOOD HERE AND IS CUT. It was
# widened twice and bypassed three times by the acceptor, each time with the
# same false claims in a new spelling: a diverged second copy of the list, then
# "Did Not Finish; May Be Partial" past a case-sensitive match, then
# "Did-Not-Finish; results May-Be-Partial" past a case-folded and
# whitespace-collapsed one. Stripping hyphens next merely restarts the
# enumeration, and the acceptor's own verdict was to cut rather than widen
# again. A guard that can always be spelled around is worse than no guard,
# because it reads as assurance.
#
# What remains is honest about which control does what. The WHOLE-SENTENCE
# EQUALITY assertions below are the mechanical guard, and they are the one that
# works: any reword of a shipped notice fails a test, so no notice changes
# silently. What no test can do is stop an author who edits the production
# string and its expectation together — that is a judgment about whether a
# sentence is true, and it belongs to review. So the rule is written at both
# production sites, where the person making that edit reads it, and in
# `architecture/engagement-terminal-telling.md`, where the reviewer does.


_EXPECTED_DISCLOSURE = (
    "\u26a0\ufe0f This engagement is recorded as {outcome}, but its completion "
    "summary could not be confirmed as posted here. The topic is left UNMARKED "
    "on purpose, so the failure stays visible."
)
"""The disclosure's exact sentence, duplicated here rather than imported so a
reworded production string fails a test. "could not be CONFIRMED as posted",
never "could not be posted": a Telegram ``TimedOut`` can lose the
acknowledgement of a message the wire accepted (SEAM-2).

R2: it no longer says the topic is left OPEN, because it no longer is. The
seam round established that a deliberately-open terminal topic still ACCEPTS
messages while its retention ledger entry — appended unconditionally, before
this block ever runs — later deletes the topic and everything in it, so a note
typed there is lost. The close is therefore unconditional again and only the
outcome MARK is withheld, which carries the same failure signal; what the
notice now warns about is the retention deletion, which is true of every
terminal topic and is the thing the operator can still act on."""


async def _build_finalize(tmp_path, *, rich_returns=None, rich_raises=None):
    """Real registry + on-disk tombstone; a channel double whose senders are
    genuinely awaitable (a bare MagicMock fabricates every attribute and then
    raises TypeError when awaited, which is how eight tests in this repo came
    to assert the pre-fix paint-and-close over a topic that got nothing)."""
    from engagement_registry import EngagementRegistry
    from tools import init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="t",
        origin={"role": "assistant", "channel": "telegram", "chat_id": "1"},
        topic_id=42,
    )

    events: list[str] = []
    telegram = MagicMock()

    async def _rich(thread_id, text, **kw):
        events.append("rich")
        if rich_raises is not None:
            raise rich_raises
        return rich_returns

    async def _plain(thread_id, text, **kw):
        events.append("plain")
        return 9001

    async def _paint(*, engagement_id, new_state):
        events.append(f"paint:{new_state}")

    async def _close(*, thread_id):
        events.append("close")

    telegram.send_response_to_topic = AsyncMock(side_effect=_rich)
    telegram.send_to_topic = AsyncMock(side_effect=_plain)
    telegram.update_topic_state = AsyncMock(side_effect=_paint)
    telegram.close_topic = AsyncMock(side_effect=_close)
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()

    init_tools(
        channel_manager=cm, bus=bus, specialist_registry=MagicMock(),
        mcp_registry=MagicMock(), trigger_registry=MagicMock(),
        engagement_registry=reg,
    )

    driver = _driver_double()
    return reg, rec, telegram, bus, driver, events


class TestFinalizeCompletionConfirmation:
    """INV-ENG-013: the funnel paints the topic's terminal state and closes it
    only after the completion post is CONFIRMED by the wire. An unconfirmed
    post leaves the topic UNMARKED after one bounded plain disclosure, and
    the post-topic tail runs either way. R2: the CLOSE is unconditional and
    bounded — only the outcome mark is withheld — because a deliberately
    open terminal topic still accepts messages that its already-appended
    retention entry later deletes.

    Pre-fix this is false: the direct send's exception is swallowed and the
    paint and close proceed regardless.
    """

    async def test_a_raising_direct_post_leaves_the_topic_unmarked(self, tmp_path):
        """MUTATION: make the paint-and-close unconditional → this fails
        (paint and close each become 1). MUTATION: set the confirmation flag
        before the await → this fails the same way."""
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))

        won = await _finalize_engagement(
            rec, outcome="completed", text="done",
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert reg.get(rec.id).status == "completed"     # the work IS done
        assert telegram.send_response_to_topic.await_count == 1
        # ONE bounded plain disclosure, and it is the whole sentence.
        assert telegram.send_to_topic.await_count == 1
        # Whole-sentence equality, which is what stops a SILENT reword. The
        # rule about what this sentence may claim is stated at the production
        # site; see the note at the top of this section for why it is not
        # asserted as a phrase blacklist here.
        assert (telegram.send_to_topic.await_args.args[1]
                == _EXPECTED_DISCLOSURE.format(outcome="completed"))
        # The topic is left UNPAINTED — the defect was painting ✅ over a
        # topic that heard nothing. It is still CLOSED, like every
        # terminal topic (R2).
        assert telegram.update_topic_state.await_count == 0
        assert telegram.close_topic.await_count == 1
        assert "paint:completed" not in events
        assert "close" in events
        # The post-topic tail is untouched.
        assert driver.cancel.await_count == 1
        assert bus.notify.await_count == 1

    async def test_a_confirmed_direct_post_paints_and_closes_once(
        self, tmp_path,
    ):
        """The CONTROL: a rich send that returns a message id is
        confirmation."""
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_returns=4242)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done",
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert telegram.send_response_to_topic.await_count == 1
        assert telegram.send_to_topic.await_count == 0
        assert telegram.update_topic_state.await_count == 1
        assert telegram.close_topic.await_count == 1
        assert events == ["rich", "paint:completed", "close"]

    async def test_a_wedged_disclosure_is_bounded(self, tmp_path, monkeypatch):
        """MUTATION: remove the disclosure's bound → this hangs. The wedged
        counterparty is an Event never set; the production constant is
        shortened. No asyncio.sleep is patched."""
        import asyncio as _aio
        import tools as tools_mod
        from tools import FinalizeResult, _finalize_engagement

        monkeypatch.setattr(tools_mod, "_TOPIC_OP_TIMEOUT_S", 0.05)
        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))
        wedged = _aio.Event()
        attempts = []

        async def _hang(thread_id, text, **kw):
            attempts.append(text)
            await wedged.wait()

        telegram.send_to_topic = AsyncMock(side_effect=_hang)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done",
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert len(attempts) == 1
        assert telegram.update_topic_state.await_count == 0
        assert telegram.close_topic.await_count == 1
        assert bus.notify.await_count == 1

    async def test_an_unconfirmed_cancel_also_stays_unmarked(self, tmp_path):
        """The rule is uniform across outcomes: a 🛑 over a topic that never
        heard why is the same lie as a ✅.

        MUTATION: scope the confirmation gate to ``outcome == "completed"``
        → this fails.
        """
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))

        won = await _finalize_engagement(
            rec, outcome="cancelled", text="stopped",
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert reg.get(rec.id).status == "cancelled"
        assert telegram.update_topic_state.await_count == 0
        assert telegram.close_topic.await_count == 1
        assert telegram.send_to_topic.await_count == 1

    async def test_a_wedged_close_cannot_strand_the_tail(
        self, tmp_path, monkeypatch,
    ):
        """R2 seam finding (Sol), H3. Making the close unconditional put a
        BARE, unbounded await on the untold path: this funnel's close has no
        caller-side bound, while the same module's abort path already wraps the
        identical call in ``asyncio.wait_for(..., _TOPIC_OP_TIMEOUT_S)``.

        A close that never returns would strand everything behind it — the
        driver teardown, the bus notification, the retains — over a record that
        is ALREADY durably terminal, and, because ``driver.cancel`` is what
        ends the response iterator, it would also strand the follow-up turn
        that is waiting to adjudicate its own cutoff. F1's fix disabled by
        F3's fix.

        MUTATION: remove the bound around the close → this test HANGS instead
        of passing. A "the close raises" pin does NOT kill that mutation and is
        not a substitute for this one.

        The wedged counterparty is an Event that is never set and the
        production constant is shortened; no ``asyncio.sleep`` is patched.
        """
        import asyncio as _aio
        import tools as tools_mod
        from tools import FinalizeResult, _finalize_engagement

        monkeypatch.setattr(tools_mod, "_TOPIC_OP_TIMEOUT_S", 0.05)
        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))
        wedged = _aio.Event()
        closes = []

        async def _hang(*, thread_id):
            closes.append(thread_id)
            await wedged.wait()
        telegram.close_topic = AsyncMock(side_effect=_hang)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done",
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert closes == [42]                    # attempted exactly once
        assert telegram.send_to_topic.await_count == 1   # the disclosure
        # Everything the wedge would otherwise have stranded.
        assert driver.cancel.await_count == 1
        assert bus.notify.await_count == 1


class TestTheFunnelRecordsWhetherItToldTheTopic:
    """R2 mutation pins — NOT part of the accepted red case. The funnel must
    record, for the follow-up adjudicator to read, whether ITS telling reached
    the topic.

    These assert the recorded fact directly, which means they name the
    mechanism's storage. That is deliberate HERE and would be wrong in the red
    case: Terra returned an earlier version of this cluster's red case for
    pinning this representation, and the behavioural version of the same
    property now lives end to end in
    ``tests/test_launch_death_reporter.py::TestTerminalStatusIsNotProofOfATelling``,
    which names nothing. What these add on top is a killing mutation for each
    ARM of the recording rule, which an end-to-end count cannot separate.

    Terminal STATUS is not that fact. A ticketed follow-up turn that calls
    ``emit_completion`` ends with no ``ResultMessage`` on the happy path AND on
    the failure path, and the delivery task can only tell those apart by
    knowing whether the terminal path actually said anything into the topic.
    The receipt is that knowledge, and it is written here.

    MUTATIONS: never write the receipt → the terminal-untold red case in
    ``tests/test_launch_death_reporter.py`` cannot distinguish the states.
    Write it as confirmed whatever happened → the untold parameter there
    fails. Write it AFTER the close → the wedged-close case below fails.
    """

    @staticmethod
    def _receipts(reg):
        """The recorded telling, read without asserting an implementation:
        ``{engagement_id: confirmed}``."""
        return dict(getattr(reg, "_terminal_telling", {}) or {})

    async def test_a_confirmed_post_records_the_telling_as_confirmed(
        self, tmp_path,
    ):
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_returns=4242)
        won = await _finalize_engagement(
            rec, outcome="completed", text="done",
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert self._receipts(reg) == {rec.id: True}

    async def test_a_failed_post_and_a_confirmed_disclosure_still_told(
        self, tmp_path,
    ):
        """The disclosure IS a telling. If it landed, the topic heard why, and
        the follow-up owner must stay quiet.

        MUTATION: record the receipt from the summary alone, ignoring the
        disclosure → this fails.
        """
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))
        won = await _finalize_engagement(
            rec, outcome="completed", text="done",
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert telegram.send_to_topic.await_count == 1   # the disclosure
        assert self._receipts(reg) == {rec.id: True}

    async def test_nothing_confirmed_records_the_telling_as_UNTOLD(
        self, tmp_path,
    ):
        """The state the seam round found: the summary failed AND the
        disclosure failed, so the topic heard nothing at all."""
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))

        async def _plain_fails(thread_id, text, **kw):
            events.append("plain")
            raise RuntimeError("telegram 500 again")
        telegram.send_to_topic = AsyncMock(side_effect=_plain_fails)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done",
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert telegram.send_to_topic.await_count == 1
        assert self._receipts(reg) == {rec.id: False}
        assert telegram.update_topic_state.await_count == 0
        assert telegram.close_topic.await_count == 1

    async def test_a_disclosure_returning_none_is_not_a_telling(
        self, tmp_path,
    ):
        """MUTATION: treat the disclosure send RETURNING as confirmation → this
        fails. The same third possibility the summary path already pins: a
        sender that returns normally having sent nothing."""
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))

        async def _plain_none(thread_id, text, **kw):
            events.append("plain")
            return None
        telegram.send_to_topic = AsyncMock(side_effect=_plain_none)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done",
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert self._receipts(reg) == {rec.id: False}

class TestFinalizeConfirmationMutationPins:
    """#678 first-diff-round reviewer findings. These are NOT part of the
    accepted red case; they exist so specific mutations of the confirmation
    gate cannot pass. Each names the mutation it kills."""

    async def test_a_direct_sender_returning_none_is_not_confirmation(
        self, tmp_path,
    ):
        """MUTATION: accept a ``None`` return from the direct sender as
        confirmation (e.g. ``completion_post_confirmed = True`` after the
        await, or ``_mid is not None`` weakened to a bare truthiness test on a
        call that returned) → this fails.

        Both reviewers found the same gap: every direct-send case in the
        accepted red case either RAISES or returns ``4242``, so nothing pinned
        the third possibility — a sender that returns normally having sent
        nothing. A channel implementation is free to do that.
        """
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_returns=None)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done",
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert telegram.send_response_to_topic.await_count == 1
        assert telegram.send_to_topic.await_count == 1          # disclosure
        assert telegram.update_topic_state.await_count == 0
        assert telegram.close_topic.await_count == 1
        assert driver.cancel.await_count == 1
        assert bus.notify.await_count == 1

    async def test_an_error_outcome_also_withholds_the_mark_and_the_close(
        self, tmp_path,
    ):
        """MUTATION: scope the confirmation gate to
        ``outcome in ("completed", "cancelled")`` → this fails.

        The accepted red case covers ``completed`` and ``cancelled``. The
        third terminal outcome takes the same branch and is asserted here
        separately, because a gate that silently excluded one outcome would
        leave that outcome painting a ⚠️ over a topic that never heard why.
        """
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))

        won = await _finalize_engagement(
            rec, outcome="error", text="broke",
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert reg.get(rec.id).status == "error"
        assert telegram.update_topic_state.await_count == 0
        assert telegram.close_topic.await_count == 1
        assert telegram.send_to_topic.await_count == 1
        assert bus.notify.await_count == 1


# ---------------------------------------------------------------------------
# #1323 red case: every pointer to the terminal-notice rule names a document
# that carries it
# ---------------------------------------------------------------------------
#
# The rule (a reword of a notice together with its expectation is a judgment for
# review; the phrase blacklist was cut) is stated in
# docs/architecture/engagement-terminal-telling.md. Five sites point a reader at
# it — two production docstrings and three test-side notes. Each is read as
# SOURCE TEXT (attribute docstrings are not reachable as ``__doc__``), must hold
# exactly one corpus pointer, and the document it names must exist and carry the
# rule, identified by two of the rule paragraph's own phrases.

# A pointer is the WHOLE path-shaped token that mentions ``architecture/`` (so a
# ``docs/archive/architecture/…`` prefix is seen, not silently trimmed), and it
# is well formed only in one of the two spellings the sites use.
_RULE_POINTER_TOKEN = re.compile(r"[A-Za-z0-9_./-]*architecture/[A-Za-z0-9_./-]*")
_RULE_POINTER_FORM = re.compile(r"(?:docs/)?architecture/[A-Za-z0-9_.-]+")
_RULE_LEAD_PHRASES = (
    "A blacklist of forbidden phrases was",
    "bypassed three times",
)


def _repo_root():
    return Path(__file__).resolve().parent.parent


def _module_tree(rel):
    src = (_repo_root() / rel).read_text(encoding="utf-8")
    return src, ast.parse(src)


def _assignment(tree, name):
    hits = [
        (i, node) for i, node in enumerate(tree.body)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    ]
    assert len(hits) == 1, f"{name}: {len(hits)} module-level assignments"
    return hits[0]


def _attribute_docstring(rel, name):
    _src, tree = _module_tree(rel)
    i, _node = _assignment(tree, name)
    nxt = tree.body[i + 1]
    assert isinstance(nxt, ast.Expr) and isinstance(nxt.value, ast.Constant) \
        and isinstance(nxt.value.value, str), f"{name} has no attribute docstring"
    return nxt.value.value


def _comment_block_above(rel, name):
    src, tree = _module_tree(rel)
    _i, node = _assignment(tree, name)
    lines = src.splitlines()
    block = []
    j = node.lineno - 2
    while j >= 0 and (not lines[j].strip() or lines[j].lstrip().startswith("#")):
        block.append(lines[j])
        j -= 1
    assert any(ln.lstrip().startswith("#") for ln in block), \
        f"no comment block above {name}"
    return "\n".join(reversed(block))


def _comment_block_above_assert(rel, cls, meth, assertion):
    src, tree = _module_tree(rel)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls]
    assert len(classes) == 1, f"{cls}: {len(classes)} classes"
    fns = [
        n for n in classes[0].body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == meth
    ]
    assert len(fns) == 1, f"{cls}.{meth}: {len(fns)} definitions"
    lines = src.splitlines()[fns[0].lineno - 1:fns[0].end_lineno]
    at = [i for i, ln in enumerate(lines) if ln.strip() == assertion]
    assert len(at) == 1, f"{cls}.{meth}: {len(at)} lines {assertion!r}"
    block = []
    j = at[0] - 1
    while j >= 0 and lines[j].lstrip().startswith("#"):
        block.append(lines[j])
        j -= 1
    assert block, f"no comment block above {assertion!r}"
    return "\n".join(reversed(block))


def _rule_pointer_sites():
    return {
        "tools.py _DISCLOSURE_UNCONFIRMED": _attribute_docstring(
            "casa/rootfs/opt/casa/tools.py", "_DISCLOSURE_UNCONFIRMED"),
        "channels/telegram.py _TURN_ENDED_UNCONFIRMED_NOTICE": _attribute_docstring(
            "casa/rootfs/opt/casa/channels/telegram.py",
            "_TURN_ENDED_UNCONFIRMED_NOTICE"),
        "test_finalize_engagement.py _EXPECTED_DISCLOSURE": _comment_block_above(
            "tests/test_finalize_engagement.py", "_EXPECTED_DISCLOSURE"),
        "test_launch_death_reporter.py _EXPECTED_TERMINAL_UNCONFIRMED_NOTICE":
            _attribute_docstring(
                "tests/test_launch_death_reporter.py",
                "_EXPECTED_TERMINAL_UNCONFIRMED_NOTICE"),
        "test_launch_death_reporter.py TestTerminalStatusIsNotProofOfATelling":
            _comment_block_above_assert(
                "tests/test_launch_death_reporter.py",
                "TestTerminalStatusIsNotProofOfATelling",
                "test_a_terminal_record_that_told_nobody_still_gets_its_notice",
                "assert _notice == _EXPECTED_TERMINAL_UNCONFIRMED_NOTICE"),
    }


def test_every_terminal_notice_rule_pointer_names_a_document_carrying_the_rule():
    docs = _repo_root() / "docs"
    sites = _rule_pointer_sites()
    assert len(sites) == 5
    problems = []
    for site, text in sites.items():
        pointers = [t.rstrip(".") for t in _RULE_POINTER_TOKEN.findall(text)]
        if len(pointers) != 1:
            problems.append(f"{site}: {len(pointers)} pointers {pointers}")
            continue
        if not _RULE_POINTER_FORM.fullmatch(pointers[0]):
            problems.append(f"{site}: {pointers[0]} is not a docs/ corpus pointer")
            continue
        target = docs / pointers[0].removeprefix("docs/")
        if not target.is_file():
            problems.append(f"{site}: {pointers[0]} does not resolve under docs/")
            continue
        body = " ".join(target.read_text(encoding="utf-8").split())
        missing = [p for p in _RULE_LEAD_PHRASES if p not in body]
        if missing:
            problems.append(f"{site}: {pointers[0]} does not carry {missing}")
    assert problems == []


def test_the_finalization_citations_that_are_not_about_the_rule_stay():
    """Regression, green at base (not a red case): the #632 suite's comment
    cites engagement-finalization.md correctly for INV-ENG-010 and must not
    be swept up in a retarget of the rule pointers."""
    _src, tree = _module_tree("tests/test_emit_completion_tool.py")
    classes = [
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "TestEmitCompletionCancellation"
    ]
    assert len(classes) == 1
    assert "docs/architecture/engagement-finalization.md" in ast.get_docstring(classes[0])
