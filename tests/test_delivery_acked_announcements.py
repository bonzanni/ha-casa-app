"""#701 — an announcement Casa owes is durable until it is DELIVERED.

Two markers, one rule. `orphan_notification_pending` is written when boot
recovery converts a live row; `terminal_notification_pending` is written by the
terminal that a delegation reached while the process was still up but whose
announcement never got out. Neither is cleared by the bus accepting a message —
only by the consuming resident reporting that its turn reached the transport.

The companion seam, where a channel outcome becomes an acknowledgement, is
pinned in `tests/test_agent_process.py::TestDeliveryAcknowledgedAnnouncements`.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from job_registry import (
    DeliveryState,
    ExecutionState,
    JobFailure,
    JobRegistry,
    VoiceJob,
)
from personality_types import SpeakerProvenance

pytestmark = [pytest.mark.asyncio]

SYSTEM_SPEAKER = SpeakerProvenance(speaker_kind="system")


def make_job(**changes):
    base = VoiceJob(
        id="job-1", parent_job_id=None,
        creating_speaker=SYSTEM_SPEAKER, executing_speaker=SYSTEM_SPEAKER,
        creating_role="concierge", specialist_role="finance",
        specialist_display_name="Finance",
        creator_peer="telegram", creator_user_id=None,
        scope_id="chat-1", origin_route_id="route-1",
        origin_device_id=None,
        task="what is my bank balance", context="",
        created_at=100.0, started_at=101.0, terminal_at=None,
        expires_at=None, execution_state=ExecutionState.RUNNING,
        delivery_state=DeliveryState.NONE,
        result=None, failure=None, awaiting_input=False,
        continuable_until=None, delivery_sequence=0,
        delivery_attempt_id=None, lease_until=None,
        cancel_pending=False,
    )
    return replace(base, **changes)


async def _registry(tmp_path, *jobs, now=200.0, retry_interval=5.0):
    registry = JobRegistry(
        tmp_path / "jobs.json", clock=lambda: now,
        reconciliation_retry_interval=retry_interval,
    )
    await registry.load()
    for job in jobs:
        await registry.create(job)
    return registry


# ---------------------------------------------------------------------------
# The codec: additive, and fail-closed for a row written before the field
# ---------------------------------------------------------------------------


async def test_a_row_without_the_terminal_marker_decodes_as_owing_nothing(
    tmp_path,
):
    registry = await _registry(tmp_path, make_job())
    path = tmp_path / "jobs.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert "terminal_notification_pending" in snapshot[0]
    snapshot[0].pop("terminal_notification_pending")
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    reloaded = JobRegistry(path)
    await reloaded.load()
    assert reloaded.get("job-1").terminal_notification_pending is False
    # ...and a legacy row is therefore not announced at boot.
    assert await reloaded.recover_after_restart() != []      # it is still LIVE
    assert reloaded.get("job-1").terminal_notification_pending is False


async def test_an_armed_terminal_survives_the_snapshot(tmp_path):
    registry = await _registry(tmp_path, make_job())
    await registry.finish_compat(
        "job-1", "the balance is 12", announce_creator=True)
    assert registry.get("job-1").terminal_notification_pending is True

    reloaded = JobRegistry(tmp_path / "jobs.json")
    await reloaded.load()
    row = reloaded.get("job-1")
    assert row.execution_state is ExecutionState.SUCCEEDED
    assert row.terminal_notification_pending is True
    # #688 (was: "the answer text itself is NOT what survives"). It is now
    # exactly what survives, because it is what the owed announcement has to
    # carry — and it survives in the SAME snapshot as the marker, so a boot can
    # never find one without the other.
    assert row.result == "the balance is 12"
    assert row.result_available is True


# ---------------------------------------------------------------------------
# Arming: only a terminal that actually owes a notice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "announce,creator,expected",
    [
        (True, "telegram", True),
        (False, "telegram", False),      # sync: the answer went back in-band
        (True, "voice", False),          # not a channel this notice reaches
        (False, "voice", False),
    ],
)
async def test_only_an_announcing_telegram_terminal_is_armed(
    tmp_path, announce, creator, expected,
):
    registry = await _registry(tmp_path, make_job(creator_peer=creator))
    await registry.finish_compat("job-1", "", announce_creator=announce)
    assert registry.get("job-1").terminal_notification_pending is expected


async def test_a_failed_terminal_is_armed_and_keeps_its_typed_kind(tmp_path):
    registry = await _registry(tmp_path, make_job())
    await registry.fail_compat(
        "job-1", JobFailure("rate_limit", "too many"), announce_creator=True)
    row = registry.get("job-1")
    assert row.execution_state is ExecutionState.FAILED
    assert row.terminal_notification_pending is True
    assert row.failure.kind == "rate_limit"


@pytest.mark.parametrize("arm", ["finish", "fail"])
async def test_a_creator_cancelled_terminal_owes_nothing(tmp_path, arm):
    """The creator withdrew the question; they are not told it was answered."""
    registry = await _registry(tmp_path, make_job(cancel_pending=True))
    if arm == "finish":
        await registry.finish_compat("job-1", "", announce_creator=True)
    else:
        await registry.fail_compat(
            "job-1", JobFailure("boom", "late"), announce_creator=True)
    row = registry.get("job-1")
    assert row.execution_state is ExecutionState.CANCELLED
    assert row.terminal_notification_pending is False

    reloaded = JobRegistry(tmp_path / "jobs.json")
    await reloaded.load()
    assert await reloaded.recover_after_restart() == []


async def test_the_two_markers_are_never_both_owed(tmp_path):
    """The orphan conversion only ever runs on a LIVE row, so a row cannot
    carry both — one announcement per boot, not two."""
    registry = await _registry(tmp_path, make_job())
    await registry.finish_compat("job-1", "", announce_creator=True)

    reloaded = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await reloaded.load()
    owed = await reloaded.recover_after_restart()
    assert [job.id for job in owed] == ["job-1"]
    row = reloaded.get("job-1")
    assert row.terminal_notification_pending is True
    assert row.orphan_notification_pending is False
    assert row.execution_state is ExecutionState.SUCCEEDED


# ---------------------------------------------------------------------------
# Boot replay: two shapes, both truthful, neither carrying an answer
# ---------------------------------------------------------------------------


class _BusProbe:
    queues = {"concierge": object()}

    def __init__(self):
        self.sent = []

    async def notify(self, message):
        self.sent.append(message)


async def _replay(registry):
    from casa_core import _notify_recovered_delegations
    owed = await registry.recover_after_restart()
    bus = _BusProbe()
    await _notify_recovered_delegations(
        owed, registry, bus, assistant_role="concierge",
    )
    return bus


async def _strip_retained_answer(path):
    """Rewrite the snapshot as a row written BEFORE `result_available` existed.

    #688: this is what "a success that retained no answer" now means. The field
    defaults False, so a legacy row replays exactly as every row did before the
    feature — the fail-closed direction.
    """
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert "result_available" in snapshot[0]
    snapshot[0].pop("result_available")
    snapshot[0]["result"] = ""
    path.write_text(json.dumps(snapshot), encoding="utf-8")


async def test_a_success_lost_in_a_stop_is_announced_without_an_answer(
    tmp_path,
):
    registry = await _registry(tmp_path, make_job())
    await registry.finish_compat("job-1", "", announce_creator=True)
    await _strip_retained_answer(tmp_path / "jobs.json")

    reloaded = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await reloaded.load()
    bus = await _replay(reloaded)

    assert len(bus.sent) == 1
    complete = bus.sent[0].content
    # The execution state is reported truthfully: it really did succeed.
    assert complete.status == "ok"
    assert complete.result_available is False
    assert complete.text == ""
    # ...and no failure kind is invented for it.
    assert complete.kind == ""
    assert complete.origin["user_text"] == "what is my bank balance"


async def test_a_failure_lost_in_a_stop_replays_its_own_kind(tmp_path):
    registry = await _registry(tmp_path, make_job())
    await registry.fail_compat(
        "job-1", JobFailure("rate_limit", "too many"), announce_creator=True)

    reloaded = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await reloaded.load()
    bus = await _replay(reloaded)

    assert len(bus.sent) == 1
    complete = bus.sent[0].content
    assert complete.status == "error"
    assert complete.kind == "rate_limit"
    assert complete.message == "too many"


async def test_the_replayed_success_is_narrated_as_a_lost_answer(tmp_path):
    """The synthesized resident turn must never present the empty stored
    result as the specialist's answer.

    #688: the row this replays is one that retained NO answer — a legacy row.
    A row that did retain one is narrated by
    `test_answered_terminal_replays_the_retained_answer_after_restart`.
    """
    registry = await _registry(tmp_path, make_job())
    await registry.finish_compat("job-1", "", announce_creator=True)
    await _strip_retained_answer(tmp_path / "jobs.json")
    reloaded = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await reloaded.load()
    bus = await _replay(reloaded)

    from unittest.mock import Mock
    from agent import Agent
    synthesized = Agent._synthesize_delegation_turn(Mock(), bus.sent[0])
    body = synthesized.content
    assert "Result text" not in body
    # #766 reworded this branch: it now has a SECOND producer (a replayed
    # engagement outcome, which may have finished long before the restart and
    # whose summary may well have been retained elsewhere), so the prose is
    # timing- and storage-neutral. What it must still refuse to do is present
    # the empty stored result as the answer, and it must still say plainly that
    # the answer is not here.
    assert "does not carry its answer" in body
    assert "not the result text" in body
    assert "what is my bank balance" in body
    # The obligation rides the synthesis, or the delivery could never clear it.
    assert synthesized.on_delivery is bus.sent[0].on_delivery
    assert synthesized.on_delivery is not None


async def test_delivery_clears_it_and_the_next_boot_owes_nothing(tmp_path):
    registry = await _registry(tmp_path, make_job())
    await registry.finish_compat("job-1", "", announce_creator=True)
    reloaded = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await reloaded.load()
    bus = await _replay(reloaded)

    # Enqueue on its own settles nothing.
    assert reloaded.get("job-1").terminal_notification_pending is True

    await bus.sent[0].on_delivery()
    assert reloaded.get("job-1").terminal_notification_pending is False

    third = JobRegistry(tmp_path / "jobs.json", clock=lambda: 400.0)
    await third.load()
    assert await third.recover_after_restart() == []


async def test_an_undelivered_announcement_is_owed_at_every_boot(tmp_path):
    registry = await _registry(tmp_path, make_job())
    await registry.fail_compat(
        "job-1", JobFailure("rate_limit", "too many"), announce_creator=True)

    for boot in range(3):
        reloaded = JobRegistry(
            tmp_path / "jobs.json", clock=lambda: 300.0 + boot)
        await reloaded.load()
        bus = await _replay(reloaded)
        assert len(bus.sent) == 1, f"boot {boot} announced nothing"

    # The last one is delivered, and it stops.
    await bus.sent[0].on_delivery()
    final = JobRegistry(tmp_path / "jobs.json", clock=lambda: 400.0)
    await final.load()
    assert await final.recover_after_restart() == []


# ---------------------------------------------------------------------------
# The live path: persist, then announce, then acknowledge on delivery
# ---------------------------------------------------------------------------


class _FakeDelegatedOutput:
    def __init__(self, text="the answer", run_aborted=False, run_subtype=None):
        self.text = text
        self.run_aborted = run_aborted
        self.run_subtype = run_subtype or "error_max_turns"
        self.answer_incomplete = False


async def _drive_completion_callback(monkeypatch, outcome, *, cancelled_row=False):
    """Run `_attach_completion_callback` over one finished task and report
    what was written durably and what was announced."""
    import tools
    from specialist_registry import DelegationRecord

    writes = []
    notified = []
    acks = []

    class _JobRegistry:
        async def finish_compat(self, did, result="", *, announce_creator=False):
            writes.append(("finish", did, announce_creator, result))
            return make_job(
                id=did,
                execution_state=(
                    ExecutionState.CANCELLED if cancelled_row
                    else ExecutionState.SUCCEEDED),
            )

        async def fail_compat(self, did, failure, *, announce_creator=False):
            writes.append(("fail", did, announce_creator, failure.kind))
            return make_job(
                id=did,
                execution_state=(
                    ExecutionState.CANCELLED if cancelled_row
                    else ExecutionState.FAILED),
                failure=failure,
            )

        async def ack_terminal_notification(self, did):
            acks.append(did)

        def schedule_failure_reconciliation(self, did, failure=None, **kw):
            raise AssertionError("no write failed")

        def schedule_completion_reconciliation(self, did, **kw):
            raise AssertionError("no write failed")

    class _Registry:
        job_registry = _JobRegistry()

        async def complete_delegation(
            self, did, result="", *, announce_creator=False,
        ):
            return await self.job_registry.finish_compat(
                did, result, announce_creator=announce_creator)

        async def cancel_delegation(self, did):
            writes.append(("cancel", did, False))

    class _Bus:
        async def notify(self, message):
            notified.append(message)

    monkeypatch.setattr(tools, "_specialist_registry", _Registry())
    monkeypatch.setattr(tools, "_bus", _Bus())

    async def _run():
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    record = DelegationRecord(
        id="d-1", agent="finance", started_at=0.0,
        origin={"role": "concierge", "channel": "telegram", "chat_id": "c-1"},
    )
    task = asyncio.create_task(_run())
    await asyncio.gather(task, return_exceptions=True)
    tools._attach_completion_callback(task, record)
    for _ in range(20):
        await asyncio.sleep(0)
    return writes, notified, acks


@pytest.mark.parametrize(
    "outcome,expected_write",
    [
        (_FakeDelegatedOutput(), ("finish", "d-1", True)),
        (_FakeDelegatedOutput(run_aborted=True),
         ("fail", "d-1", True, "specialist_turn_limit")),
        (RuntimeError("boom"), ("fail", "d-1", True, "unknown")),
    ],
    ids=["ok", "cli-abort", "exception"],
)
async def test_each_terminal_arm_arms_the_obligation_before_announcing(
    monkeypatch, outcome, expected_write,
):
    writes, notified, acks = await _drive_completion_callback(
        monkeypatch, outcome)

    assert len(writes) == 1
    assert writes[0][:3] == expected_write[:3]
    assert len(notified) == 1
    # The notice carries the acknowledgement, and nothing has acknowledged yet:
    # enqueue is not delivery.
    assert notified[0].on_delivery is not None
    assert acks == []

    await notified[0].on_delivery()
    assert acks == ["d-1"]


@pytest.mark.parametrize(
    "outcome",
    [_FakeDelegatedOutput(), _FakeDelegatedOutput(run_aborted=True),
     RuntimeError("boom")],
    ids=["ok", "cli-abort", "exception"],
)
async def test_a_creator_cancelled_settlement_is_not_announced(
    monkeypatch, outcome,
):
    """The durable row is the authority: a creator who cancelled while the
    specialist was finishing is not told it completed."""
    writes, notified, acks = await _drive_completion_callback(
        monkeypatch, outcome, cancelled_row=True)

    assert len(writes) == 1
    assert notified == []
    assert acks == []


async def test_a_failed_terminal_write_still_announces_and_still_retries(
    monkeypatch,
):
    """The caller must be told either way, and the obligation rides the retry.

    Two orderings are possible afterwards and both are safe. Retry-then-ack
    leaves the marker clear. Ack-then-retry arms it again, so the next boot
    announces once more — the at-least-once duplicate this design accepts,
    because told twice beats never told.
    """
    import tools
    from specialist_registry import DelegationRecord

    scheduled = []
    notified = []

    class _JobRegistry:
        async def fail_compat(self, did, failure, *, announce_creator=False):
            raise OSError("disk hiccup")

        def schedule_failure_reconciliation(
            self, did, failure=None, *, announce_creator=False,
        ):
            scheduled.append((did, failure.kind, announce_creator))

        async def ack_terminal_notification(self, did):
            return None

    class _Registry:
        job_registry = _JobRegistry()

        async def cancel_delegation(self, did):
            pass

    class _Bus:
        async def notify(self, message):
            notified.append(message)

    monkeypatch.setattr(tools, "_specialist_registry", _Registry())
    monkeypatch.setattr(tools, "_bus", _Bus())

    async def _run():
        raise RuntimeError("boom")

    record = DelegationRecord(
        id="d-1", agent="finance", started_at=0.0,
        origin={"role": "concierge", "channel": "telegram", "chat_id": "c-1"},
    )
    task = asyncio.create_task(_run())
    await asyncio.gather(task, return_exceptions=True)
    tools._attach_completion_callback(task, record)
    for _ in range(20):
        await asyncio.sleep(0)

    assert [(did, announce) for did, _, announce in scheduled] == [("d-1", True)]
    assert len(notified) == 1


async def test_the_reconciliation_retry_lands_the_obligation(tmp_path):
    """A retry that lands the terminal must land what the terminal owes."""
    registry = await _registry(tmp_path, make_job(), retry_interval=0.01)
    registry.schedule_completion_reconciliation("job-1", announce_creator=True)
    await registry.wait_for_reconciliation("job-1")
    row = registry.get("job-1")
    assert row.execution_state is ExecutionState.SUCCEEDED
    assert row.terminal_notification_pending is True


# ---------------------------------------------------------------------------
# #688 / INV-JOB-015 — a delegated answer is retained exactly while it is owed
#
# Red case, specified by the design round's reviewer. The answer that was in
# hand when a delegation completed must survive the restart that interrupts its
# delivery, and must stop being retained once a delivery has been acknowledged.
# ---------------------------------------------------------------------------

# A sentinel that appears in no fixture field, so a byte count over the snapshot
# is a count of the ANSWER and of nothing else.
_ANSWER_688 = "ZQX-answer-688-sentinel"


async def test_answered_terminal_replays_the_retained_answer_after_restart(
    tmp_path,
):
    """The ruling's recovery arm: an answer completed, the process stopped
    between the terminal write and delivery, and the boot replay carries the
    ANSWER rather than a rerun offer."""
    registry = await _registry(tmp_path, make_job())
    await registry.finish_compat("job-1", _ANSWER_688, announce_creator=True)

    reloaded = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await reloaded.load()
    owed = await reloaded.recover_after_restart()
    assert len(owed) == 1
    bus = _BusProbe()
    from casa_core import _notify_recovered_delegations
    await _notify_recovered_delegations(
        owed, reloaded, bus, assistant_role="concierge",
    )

    assert len(bus.sent) == 1
    complete = bus.sent[0].content
    assert complete.status == "ok"
    assert complete.text == _ANSWER_688
    assert complete.result_available is True

    from unittest.mock import Mock
    from agent import Agent
    body = Agent._synthesize_delegation_turn(Mock(), bus.sent[0]).content
    assert f"Result text from finance:\n{_ANSWER_688}" in body
    assert "offer to run it again" not in body


async def test_empty_answer_is_available_and_replays_as_an_empty_answer(
    tmp_path,
):
    """A specialist that legitimately answered with nothing is not the same as
    a row that retained nothing: availability is a persisted fact, never
    `bool(row.result)`."""
    registry = await _registry(tmp_path, make_job())
    await registry.finish_compat("job-1", "", announce_creator=True)

    reloaded = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await reloaded.load()
    owed = await reloaded.recover_after_restart()
    bus = _BusProbe()
    from casa_core import _notify_recovered_delegations
    await _notify_recovered_delegations(
        owed, reloaded, bus, assistant_role="concierge",
    )

    assert len(bus.sent) == 1
    complete = bus.sent[0].content
    assert complete.text == ""
    assert complete.result_available is True

    from unittest.mock import Mock
    from agent import Agent
    body = Agent._synthesize_delegation_turn(Mock(), bus.sent[0]).content
    assert "Result text from" in body
    assert "does not carry its answer" not in body


async def test_ack_drops_answer_bytes_but_unacknowledged_row_keeps_them(
    tmp_path,
):
    """The drop rule, measured in the file's BYTES: an acknowledged delivery
    removes the answer, an unacknowledged one keeps it."""
    import stat as _stat

    registry = await _registry(tmp_path, make_job())
    await registry.finish_compat("job-1", _ANSWER_688, announce_creator=True)

    path = tmp_path / "jobs.json"
    assert _stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes().count(_ANSWER_688.encode()) == 1

    reloaded = JobRegistry(path, clock=lambda: 300.0)
    await reloaded.load()
    owed = await reloaded.recover_after_restart()
    bus = _BusProbe()
    from casa_core import _notify_recovered_delegations
    await _notify_recovered_delegations(
        owed, reloaded, bus, assistant_role="concierge",
    )
    assert len(bus.sent) == 1
    # Enqueued, not delivered: the answer is still owed and still on disk.
    assert path.read_bytes().count(_ANSWER_688.encode()) == 1

    await bus.sent[0].on_delivery()
    assert path.read_bytes().count(_ANSWER_688.encode()) == 0
    row = reloaded.get("job-1")
    assert row.terminal_notification_pending is False
    assert row.result == ""
    assert row.result_available is False


# ---------------------------------------------------------------------------
# #926 / INV-JOB-016 — a retained answer replayed at boot is told apart from a
# live completion by exactly one statement
#
# Red case, specified by astra (redcase-specify, run 2026-09-10 cluster B).
# The two prompts a resident receives — the live announcement of a completion,
# and the boot replay of the same retained answer after a restart interrupted
# its delivery — must differ in exactly a post-restart re-announcement
# statement, and in nothing else. At the base they are byte-identical.
# ---------------------------------------------------------------------------

_ANSWER_926 = "ZQX-answer-926-sentinel"

# Test-owned literal: the expected wording is NOT derived from the production
# constant, so a drift in either is a failure here rather than a silent match.
_REANNOUNCEMENT_926 = (
    "This is a post-restart re-announcement. The delegation finished before a "
    "Casa restart, and full delivery of its result to the user has NOT been "
    "confirmed: they may have seen nothing, a partial draft, or the whole "
    "answer. The complete result text below is still owed to them — relay "
    "all of it, even if it appears in your conversation history. Do not "
    "dismiss it as a duplicate, summarise it, or supply only an ending; keep "
    "only your own framing concise.\n\n"
)
_HEAD_926 = (
    "[System notification: your delegation to finance "
    "(id job-1) has returned with status=ok]\n\n"
)
_RESULT_926 = "Result text from finance:\n"
_TAIL_926 = (
    "\nThe original user question was: what is my bank balance\n\n"
    "Reply to the user via their original channel. Be concise.\n"
)


@pytest.mark.parametrize(
    "answer", [_ANSWER_926, ""], ids=["nonempty", "empty"],
)
async def test_retained_answer_replay_adds_only_the_restart_reannouncement(
    tmp_path, monkeypatch, answer,
):
    """INV-JOB-016: the boot replay of a retained answer and the live
    announcement of the same completion differ in exactly the re-announcement
    statement; the replay carries it once, the live prompt never."""
    from unittest.mock import Mock
    from agent import Agent
    from casa_core import _notify_recovered_delegations
    from specialist_registry import DelegationRecord

    row = make_job()

    # LIVE arm: the real producer, over a record whose every non-marker input
    # matches the durable row the replay arm will announce from.
    record = DelegationRecord(
        id=row.id,
        agent=row.specialist_role,
        started_at=row.started_at,
        origin={
            "role": "concierge",
            "channel": "telegram",
            "chat_id": "chat-1",
            "cid": "route-1",
            "user_text": row.task,
        },
    )
    terminal, retries, notified = await _drive_answer_carrying_callback(
        monkeypatch, answer, record=record)
    assert terminal == [(row.id, answer, True)]
    assert retries == [(row.id, answer, True)]
    assert len(notified) == 1
    live_notice = notified[0]

    # REPLAY arm: the durable row, reloaded from disk, recovered, announced.
    registry = await _registry(tmp_path, row)
    await registry.finish_compat(row.id, answer, announce_creator=True)
    reloaded = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await reloaded.load()
    owed = await reloaded.recover_after_restart()
    assert [j.id for j in owed] == ["job-1"]
    bus = _BusProbe()
    await _notify_recovered_delegations(
        owed, reloaded, bus, assistant_role="concierge",
    )
    assert len(bus.sent) == 1
    replay_notice = bus.sent[0]

    # Existing-contract preconditions: the two payloads agree on everything a
    # prompt is rendered from.
    for notice in (live_notice, replay_notice):
        assert notice.content.delegation_id == "job-1"
        assert notice.content.agent == "finance"
        assert notice.content.origin["user_text"] == row.task
        assert notice.content.text == answer
        assert notice.content.status == "ok"
        assert notice.content.result_available is True

    live_body = Agent._synthesize_delegation_turn(Mock(), live_notice).content
    replay_msg = Agent._synthesize_delegation_turn(Mock(), replay_notice)
    replay_body = replay_msg.content

    # THE PIN. At the base the first line fails: the bodies are byte-identical.
    assert replay_body != live_body
    assert live_body.count(_REANNOUNCEMENT_926) == 0
    assert replay_body.count(_REANNOUNCEMENT_926) == 1
    assert replay_body.replace(_REANNOUNCEMENT_926, "", 1) == live_body

    # Placement, and the live template preserved byte-for-byte.
    assert live_body == _HEAD_926 + _RESULT_926 + answer + "\n" + _TAIL_926
    assert replay_body == (
        _HEAD_926 + _REANNOUNCEMENT_926 + _RESULT_926 + answer + "\n" + _TAIL_926
    )

    # Structural answer checks — the empty slot is pinned by partition, never
    # by `str.count("")`.
    for body in (live_body, replay_body):
        assert body.count(_RESULT_926) == 1
        assert body.partition(_RESULT_926)[2] == answer + "\n" + _TAIL_926
        if answer:
            assert body.count(answer) == 1
        else:
            assert body.partition(_RESULT_926)[2] == (
                "\n\nThe original user question was: what is my bank balance\n\n"
                "Reply to the user via their original channel. Be concise.\n"
            )

    # The marker: set by the boot replay, never by the live producer.
    assert live_notice.content.replayed_after_restart is False
    assert replay_notice.content.replayed_after_restart is True
    assert replay_body.count("offer to run it again") == 0

    # Acknowledgement regression (INV-JOB-010/015 unchanged): the obligation
    # rides synthesis, and delivery clears marker and answer in one snapshot.
    assert live_notice.on_delivery is not None
    assert replay_notice.on_delivery is not None
    live_msg = Agent._synthesize_delegation_turn(Mock(), live_notice)
    assert live_msg.on_delivery is live_notice.on_delivery
    assert replay_msg.on_delivery is replay_notice.on_delivery

    path = tmp_path / "jobs.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert [r["id"] for r in rows] == ["job-1"]
    assert rows[0]["result"] == answer
    assert rows[0]["result_available"] is True
    assert rows[0]["terminal_notification_pending"] is True
    if answer:
        assert path.read_bytes().count(answer.encode()) == 1

    await replay_msg.on_delivery()

    rows = json.loads(path.read_text(encoding="utf-8"))
    assert [r["id"] for r in rows] == ["job-1"]
    assert rows[0]["result"] == ""
    assert rows[0]["result_available"] is False
    assert rows[0]["terminal_notification_pending"] is False
    if answer:
        assert path.read_bytes().count(answer.encode()) == 0

    again = JobRegistry(path, clock=lambda: 400.0)
    await again.load()
    assert await again.recover_after_restart() == []


async def _drive_answer_carrying_callback(monkeypatch, raw_text, *, record=None):
    """`_attach_completion_callback` over one successful task, with fakes
    permissive enough to record what the production caller actually forwards.

    The terminal write RAISES, so the registry-owned reconciliation is
    scheduled and its arguments are recorded too — the seam where one failed
    write can silently lose the answer after the live notice has gone out.
    """
    import tools
    from specialist_registry import DelegationRecord

    terminal = []
    retries = []
    notified = []

    class _JobRegistry:
        def schedule_completion_reconciliation(
            self, did, result="", *, announce_creator=False,
        ):
            retries.append((did, result, announce_creator))

        def schedule_failure_reconciliation(self, did, failure=None, **kw):
            raise AssertionError("the ok arm does not fail")

        async def ack_terminal_notification(self, did):
            return None

    class _Registry:
        job_registry = _JobRegistry()

        async def complete_delegation(
            self, did, result="", *, announce_creator=False,
        ):
            terminal.append((did, result, announce_creator))
            raise OSError("terminal snapshot write failed")

        async def cancel_delegation(self, did):
            raise AssertionError("not cancelled")

    class _Bus:
        async def notify(self, message):
            notified.append(message)

    monkeypatch.setattr(tools, "_specialist_registry", _Registry())
    monkeypatch.setattr(tools, "_bus", _Bus())

    async def _run():
        return _FakeDelegatedOutput(text=raw_text)

    if record is None:
        record = DelegationRecord(
            id="d-1", agent="finance", started_at=0.0,
            origin={"role": "concierge", "channel": "telegram", "chat_id": "c-1"},
        )
    task = asyncio.create_task(_run())
    await asyncio.gather(task, return_exceptions=True)
    tools._attach_completion_callback(task, record)
    for _ in range(20):
        await asyncio.sleep(0)
    return terminal, retries, notified


async def test_completion_callback_forwards_the_bounded_answer_to_terminal_and_retry(
    monkeypatch,
):
    """The production caller forwards the BOUNDED answer — the same text the
    live notice carries — to the terminal write and to the retry that lands the
    obligation when that write fails."""
    import specialist_limits

    raw = "A" * (specialist_limits._MAX_OUTPUT_CHARS + 500)
    bounded, truncated = specialist_limits.truncate_output(raw)
    assert truncated is True

    terminal, retries, notified = await _drive_answer_carrying_callback(
        monkeypatch, raw)

    assert len(terminal) == 1
    assert terminal[0] == ("d-1", bounded, True)
    assert len(retries) == 1
    assert retries[0] == ("d-1", bounded, True)
    assert len(notified) == 1
    assert notified[0].content.text == bounded
