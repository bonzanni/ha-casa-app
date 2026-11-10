"""The continuous job session: one writer per job, take-up by the CLI's echo,
the operator-message journal and its transcript reconcile, and recovery after
the job's CLI process dies.

The CLI is modelled by ``FakeCLI``. Its timing is explicit and follows what
was measured on the pinned CLI with ``--replay-user-messages``:

- a written message produces NO frame at write time;
- when the CLI takes a message into a turn it echoes the message once, as a
  ``UserMessage`` carrying the uuid it was written with (the test calls
  ``echo``), and writes the uuid into the session transcript (the test writes
  the transcript line where the scenario says the CLI did);
- a turn ends with one ``ResultMessage``;
- a dead CLI ends the message stream (``die``) and refuses writes.

The real SDK message types are used throughout."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock,
    ToolResultBlock, UserMessage,
)
from claude_agent_sdk._errors import CLIConnectionError

import job_inbox
import tools
from drivers import in_casa_driver as drv

pytestmark = pytest.mark.asyncio

END = object()
BRIEF_UUID = "11111111-1111-4111-8111-111111111111"


class FakeCLI:
    """One CLI process behind one ``ClaudeSDKClient``."""

    instances: list["FakeCLI"] = []
    events: list[str] = []
    on_enter = None                 # called at each __aenter__
    hold_disconnect: asyncio.Event | None = None
    hold_usage: asyncio.Event | None = None
    hold_enter: asyncio.Event | None = None

    def __init__(self, options):
        self.options = options
        self.written: list[dict] = []
        self.frames: asyncio.Queue = asyncio.Queue()
        self.dead = False
        self.fail_next_write = False
        self.gate: asyncio.Event | None = None
        self.writing = 0
        self.max_writing = 0
        self.disconnected = False
        type(self).instances.append(self)

    async def __aenter__(self):
        type(self).events.append(f"enter:{len(type(self).instances) - 1}")
        if type(self).on_enter is not None:
            type(self).on_enter()
        if type(self).hold_enter is not None:
            await type(self).hold_enter.wait()
        return self

    async def query(self, prompt):
        async for line in prompt:
            self.writing += 1
            self.max_writing = max(self.max_writing, self.writing)
            try:
                if self.gate is not None:
                    await self.gate.wait()
                if self.dead or self.fail_next_write:
                    self.fail_next_write = False
                    raise CLIConnectionError("the CLI process exited")
                self.written.append(line)
            finally:
                self.writing -= 1

    async def receive_messages(self):
        while True:
            frame = await self.frames.get()
            if frame is END:
                return
            yield frame

    async def disconnect(self):
        if type(self).hold_disconnect is not None:
            await type(self).hold_disconnect.wait()
        type(self).events.append(f"disconnect:{type(self).instances.index(self)}")
        self.disconnected = True
        self.frames.put_nowait(END)

    async def get_context_usage(self):
        if type(self).hold_usage is not None:
            await type(self).hold_usage.wait()
        return {"maxTokens": 200000, "autoCompactThreshold": 167000,
                "autocompactSource": "env", "isAutoCompactEnabled": True}

    # -- what the CLI does, driven by the test --------------------------------

    def texts(self) -> list[str]:
        return [line["message"]["content"] for line in self.written]

    def uuids(self) -> list[str]:
        return [line["uuid"] for line in self.written]

    def echo(self, index: int) -> None:
        line = self.written[index]
        self.frames.put_nowait(UserMessage(content=line["message"]["content"],
                                           uuid=line["uuid"]))

    def say(self, text: str) -> None:
        self.frames.put_nowait(AssistantMessage(content=[TextBlock(text=text)],
                                                model="claude-opus-5-5"))

    def result(self, subtype: str = "success") -> None:
        self.frames.put_nowait(ResultMessage(
            subtype=subtype, duration_ms=1, duration_api_ms=1,
            is_error=subtype != "success", num_turns=1, session_id="sess-1",
            total_cost_usd=0.01, usage={}, result="done"))

    def die(self) -> None:
        self.dead = True
        self.frames.put_nowait(END)


class Registry:
    def __init__(self):
        self.records: dict[str, SimpleNamespace] = {}
        self.stamps: list[str] = []

    def get(self, eid):
        return self.records.get(eid)

    def begin_turn_delivery(self, eid):
        rec = self.records.get(eid)
        return rec is None or rec.status not in ("completed", "cancelled", "error")

    def stamp(self, eid, ts):
        self.stamps.append(eid)


def _record(eid="e" * 32):
    return SimpleNamespace(
        id=eid, kind="plugin", role_or_type="assistant",
        origin={"job": {"brief_uuid": BRIEF_UUID, "title": "Rows"}},
        status="active", sdk_session_id="sess-1", started_at=0.0,
        context_rebuild_pending=False, plugin_artifacts=())


@pytest.fixture
def env(tmp_path, monkeypatch):
    FakeCLI.instances = []
    FakeCLI.events = []
    FakeCLI.on_enter = None
    FakeCLI.hold_disconnect = None
    FakeCLI.hold_usage = None
    FakeCLI.hold_enter = None
    monkeypatch.setattr(drv, "ClaudeSDKClient", FakeCLI)
    monkeypatch.setattr(job_inbox, "INBOX_ROOT", tmp_path / "job-inbox")
    transcript = tmp_path / "sess-1.jsonl"
    transcript.write_text("")
    monkeypatch.setattr(job_inbox, "transcript_path", lambda cwd, sid: transcript)
    monkeypatch.setattr(tools, "build_engagement_resume_options",
                        lambda rec, sid: ClaudeAgentOptions(resume=sid))
    reg = Registry()
    rec = _record()
    reg.records[rec.id] = rec
    turn_ends: list = []
    lost: list = []

    def make(**kw):
        return drv.InCasaDriver(
            record_lookup=reg.get, begin_turn_delivery=reg.begin_turn_delivery,
            stamp_activity=reg.stamp,
            on_turn_end=lambda r, res, err=None: turn_ends.append(res), **kw)

    return SimpleNamespace(reg=reg, rec=rec, make=make, transcript=transcript,
                           turn_ends=turn_ends, lost=lost)


def transcript_line(path, **obj):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


async def settle(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def until(predicate, timeout: float = 2.0) -> None:
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), timeout)


async def open_job(env, driver):
    await driver.open(env.rec, ClaudeAgentOptions())
    return FakeCLI.instances[-1]


async def test_one_writer_per_job_writes_in_arrival_order(env):
    """Three operator messages and a nudge submitted while a write is held in
    the transport are written one at a time, in arrival order."""
    driver = env.make()
    cli = await open_job(env, driver)
    cli.gate = asyncio.Event()
    driver.submit_operator(env.rec, "first")
    await settle()
    driver.submit_operator(env.rec, "second")
    driver.submit_casa(env.rec, "nudge")
    driver.submit_operator(env.rec, "third")
    await settle()
    assert cli.written == []
    cli.gate.set()
    await until(lambda: len(cli.written) == 4)
    assert cli.texts() == ["first", "second", "nudge", "third"]
    assert cli.max_writing == 1


async def test_a_write_sets_turn_open_before_it_is_awaited(env):
    """The idle sweep must see a write that opens a turn: ``turn_open`` is set
    synchronously before the awaited write."""
    driver = env.make()
    cli = await open_job(env, driver)
    cli.gate = asyncio.Event()
    driver.submit_operator(env.rec, "go")
    await until(lambda: cli.writing == 1)
    assert driver.turn_open(env.rec.id) is True
    cli.gate.set()


async def test_the_journal_is_committed_before_admission(env):
    driver = env.make()
    await open_job(env, driver)
    seen_at_admission = []
    original = env.reg.begin_turn_delivery

    def admission(eid):
        seen_at_admission.append([e["text"] for e in job_inbox.load(eid)])
        return original(eid)

    driver._begin_turn_delivery = admission
    ticket = driver.submit_operator(env.rec, "hello", notice={"chat_id": 1})
    assert seen_at_admission[0] == ["hello"]
    assert driver.inbound_token_held(env.rec.id, ticket)
    assert env.reg.stamps.count(env.rec.id) >= 1


async def test_a_failed_journal_commit_accepts_nothing(env, tmp_path, monkeypatch):
    driver = env.make()
    cli = await open_job(env, driver)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(job_inbox, "INBOX_ROOT", blocker)
    with pytest.raises(job_inbox.JournalError):
        driver.submit_operator(env.rec, "lost?")
    await settle()
    assert driver.inbound_unread_depth(env.rec.id) == 0
    assert cli.written == []


async def test_a_terminal_record_refuses_and_leaves_no_entry(env):
    driver = env.make()
    env.rec.status = "cancelled"
    with pytest.raises(drv.EngagementTerminalError):
        driver.submit_operator(env.rec, "too late")
    assert job_inbox.load(env.rec.id) == []
    assert driver.inbound_unread_depth(env.rec.id) == 0


async def test_the_echo_discharges_exactly_its_ticket(env):
    """Written is not read: the ticket vetoes completion until the CLI echoes
    its uuid. A tool result with another uuid discharges nothing. The journal
    entry is marked at take-up and removed when that turn ends."""
    driver = env.make()
    cli = await open_job(env, driver)
    a = driver.submit_operator(env.rec, "a")
    b = driver.submit_operator(env.rec, "b")
    await until(lambda: len(cli.written) == 2)
    assert driver.inbound_unread_depth(env.rec.id) == 2
    cli.frames.put_nowait(UserMessage(
        content=[ToolResultBlock(tool_use_id="t1", content="ok")],
        uuid="22222222-2222-4222-8222-222222222222"))
    cli.echo(1)
    await until(lambda: driver.inbound_unread_depth(env.rec.id) == 1)
    assert driver.inbound_token_held(env.rec.id, a)
    assert not driver.inbound_token_held(env.rec.id, b)
    entries = {e["uuid"]: e for e in job_inbox.load(env.rec.id)}
    assert entries[b.uuid]["taken_up"] is True and not entries[a.uuid]["taken_up"]
    cli.result()
    await until(lambda: len(env.turn_ends) == 1)
    assert [e["uuid"] for e in job_inbox.load(env.rec.id)] == [a.uuid]
    assert driver.turn_open(env.rec.id) is False


async def test_written_lines_carry_the_ticket_uuid(env):
    driver = env.make()
    cli = await open_job(env, driver)
    ticket = driver.submit_operator(env.rec, "x")
    await until(lambda: cli.written)
    assert cli.written[0]["uuid"] == ticket.uuid
    assert cli.written[0]["type"] == "user"


async def recovering_driver(env, *, restart="Casa restarted. Continue the job.",
                            brief="BRIEF", prefix="(Sent before the job's session restarted:)",
                            fail=False):
    """A driver whose session-lost handler resumes once, as the job's policy
    will, recording ``recovering`` as seen from inside the handler."""
    seen = {}
    holder = {}

    async def on_lost(rec):
        seen["recovering_in_handler"] = holder["d"].recovering(rec.id)
        if fail:
            raise RuntimeError("policy ends the job")
        await holder["d"].recover(rec, restart_line=restart, brief=brief,
                                  redelivery_prefix=prefix)

    holder["d"] = env.make(on_session_lost=on_lost)
    return holder["d"], seen


async def test_recovery_stages_restart_then_redeliveries_ahead_of_the_queue(env):
    """CLI death with a written-but-unread ticket and a queued one: the
    replacement gets the restart line, then the unread message (same uuid,
    prefixed), then the queued message. ``recovering`` is set from the death
    until the restart items are staged, and clear after."""
    driver, seen = await recovering_driver(env)
    cli = await open_job(env, driver)
    transcript_line(env.transcript, type="user", uuid=BRIEF_UUID)   # brief was read
    FakeCLI.on_enter = lambda: seen.setdefault(
        "recovering_at_reopen", driver.recovering(env.rec.id))
    unread = driver.submit_operator(env.rec, "unread")
    await until(lambda: len(cli.written) == 1)
    cli.gate = asyncio.Event()                  # the next write hangs ...
    driver.submit_operator(env.rec, "queued")
    await until(lambda: cli.writing == 1)
    cli.die()                                   # ... and the CLI dies under it
    cli.gate.set()
    await until(lambda: len(FakeCLI.instances) == 2)
    new = FakeCLI.instances[1]
    await until(lambda: len(new.written) == 3)
    assert seen["recovering_in_handler"] is True
    assert seen["recovering_at_reopen"] is True
    assert driver.recovering(env.rec.id) is False
    assert new.texts() == [
        "Casa restarted. Continue the job.",
        "(Sent before the job's session restarted:)\nunread",
        "queued"]
    assert new.uuids()[1] == unread.uuid
    assert driver.inbound_unread_depth(env.rec.id) == 2


async def test_the_dead_session_is_retired_before_its_replacement_opens(env):
    """The dead client's disconnect is slow (modelled by holding it): no
    replacement is opened until it has finished, and the dead session has
    already left the registry."""
    driver, _ = await recovering_driver(env)
    cli = await open_job(env, driver)
    transcript_line(env.transcript, type="user", uuid=BRIEF_UUID)
    FakeCLI.hold_disconnect = asyncio.Event()
    cli.die()
    await settle(50)
    assert len(FakeCLI.instances) == 1 and not driver.is_alive(env.rec)
    assert driver.recovering(env.rec.id) is True
    FakeCLI.hold_disconnect.set()
    await until(lambda: len(FakeCLI.instances) == 2)
    assert FakeCLI.events.index("disconnect:0") < FakeCLI.events.index("enter:1")
    assert driver.is_alive(env.rec)
    assert driver._sessions[env.rec.id].client is FakeCLI.instances[1]


async def test_a_transcript_hit_drops_the_entry_and_the_ticket(env):
    """The CLI took the message up and wrote its transcript line, then died
    before its echo reached Casa: the transcript is authoritative — the entry
    and the in-memory ticket go, nothing is redelivered."""
    driver, _ = await recovering_driver(env)
    cli = await open_job(env, driver)
    transcript_line(env.transcript, type="user", uuid=BRIEF_UUID)
    ticket = driver.submit_operator(env.rec, "read before death")
    await until(lambda: cli.written)
    transcript_line(env.transcript, type="attachment",
                    attachment={"type": "queued_command", "source_uuid": ticket.uuid})
    cli.die()
    await until(lambda: len(FakeCLI.instances) == 2)
    new = FakeCLI.instances[1]
    await until(lambda: new.written)
    await settle()
    assert new.texts() == ["Casa restarted. Continue the job."]
    assert job_inbox.load(env.rec.id) == []
    assert driver.inbound_unread_depth(env.rec.id) == 0


async def test_a_transcript_miss_after_a_casa_restart_redelivers_once(env):
    """Boot: no in-memory ticket, one journaled entry the transcript lacks and
    one it holds. The job gets the restart line and the missing message once."""
    driver, _ = await recovering_driver(env)
    job_inbox.append(env.rec.id, uuid="aaaaaaaa-0000-4000-8000-000000000001",
                     text="missed", notice=None)
    job_inbox.append(env.rec.id, uuid="aaaaaaaa-0000-4000-8000-000000000002",
                     text="read", notice=None)
    transcript_line(env.transcript, type="user", uuid=BRIEF_UUID)
    transcript_line(env.transcript, type="queue-operation", operation="remove",
                    commandUuid="aaaaaaaa-0000-4000-8000-000000000002")
    await driver.recover(env.rec, restart_line="Casa restarted.", brief="BRIEF",
                         redelivery_prefix="(Sent before Casa restarted:)")
    cli = FakeCLI.instances[0]
    await until(lambda: len(cli.written) == 2)
    await settle()
    assert cli.texts() == ["Casa restarted.",
                           "(Sent before Casa restarted:)\nmissed"]
    assert cli.uuids()[1] == "aaaaaaaa-0000-4000-8000-000000000001"
    assert [e["uuid"] for e in job_inbox.load(env.rec.id)] == [
        "aaaaaaaa-0000-4000-8000-000000000001"]


async def test_a_brief_the_transcript_lacks_is_written_instead_of_the_restart_line(env):
    driver, _ = await recovering_driver(env)
    await driver.recover(env.rec, restart_line="Casa restarted.", brief="BRIEF",
                         redelivery_prefix="(Sent before Casa restarted:)")
    cli = FakeCLI.instances[0]
    await until(lambda: cli.written)
    await settle()
    assert cli.texts() == ["BRIEF"] and cli.uuids() == [BRIEF_UUID]


async def test_a_transcript_miss_on_a_terminal_job_is_told_lost_once(env):
    driver = env.make()
    job_inbox.append(env.rec.id, uuid="bbbbbbbb-0000-4000-8000-000000000001",
                     text="never read", notice={"chat_id": 1, "message_id": 7})
    job_inbox.append(env.rec.id, uuid="bbbbbbbb-0000-4000-8000-000000000002",
                     text="read", notice=None)
    transcript_line(env.transcript, type="user",
                    uuid="bbbbbbbb-0000-4000-8000-000000000002")
    env.rec.status = "completed"
    told = []

    async def tell(rec, entry):
        told.append(entry["text"])

    assert await driver.settle_journal_terminal(env.rec, tell) == 1
    assert told == ["never read"]
    assert not job_inbox.path_for(env.rec.id).exists()


async def test_a_failed_write_is_written_once_after_recovery(env):
    """The CLI dies during a write: the ticket stays unwritten at the head of
    the queue and is written exactly once into the replacement."""
    driver, _ = await recovering_driver(env)
    cli = await open_job(env, driver)
    transcript_line(env.transcript, type="user", uuid=BRIEF_UUID)
    cli.fail_next_write = True
    ticket = driver.submit_operator(env.rec, "retry me")
    await until(lambda: len(FakeCLI.instances) == 2)
    cli.die()
    new = FakeCLI.instances[1]
    await until(lambda: len(new.written) == 2)
    await settle()
    assert new.texts() == ["Casa restarted. Continue the job.", "retry me"]
    assert new.uuids().count(ticket.uuid) == 1
    assert cli.written == []


async def test_a_failed_recovery_clears_recovering_and_stops_writing(env):
    driver, seen = await recovering_driver(env, fail=True)
    cli = await open_job(env, driver)
    driver.submit_operator(env.rec, "pending")
    await until(lambda: cli.written)
    cli.die()
    await until(lambda: seen.get("recovering_in_handler") is True)
    await settle(50)
    assert driver.recovering(env.rec.id) is False
    assert len(FakeCLI.instances) == 1
    assert driver.inbound_unread_depth(env.rec.id) == 1


async def test_cancel_is_a_quiet_end(env):
    driver, seen = await recovering_driver(env)
    cli = await open_job(env, driver)
    env.rec.status = "cancelled"
    await driver.cancel(env.rec)
    await settle()
    assert cli.disconnected and not driver.is_alive(env.rec)
    assert "recovering_in_handler" not in seen
    assert len(FakeCLI.instances) == 1


async def test_a_suspended_job_is_resumed_by_its_next_input(env):
    driver = env.make()
    cli = await open_job(env, driver)
    await driver.cancel(env.rec)            # the idle suspension: record live
    assert not driver.is_alive(env.rec)
    driver.submit_operator(env.rec, "wake up")
    await until(lambda: len(FakeCLI.instances) == 2 and FakeCLI.instances[1].written)
    assert FakeCLI.instances[1].options.resume == "sess-1"
    assert FakeCLI.instances[1].texts() == ["wake up"]
    assert cli.written == []


async def test_turn_end_hands_the_result_to_the_handler(env):
    driver = env.make()
    cli = await open_job(env, driver)
    driver.submit_casa(env.rec, "work")
    await until(lambda: cli.written)
    cli.echo(0)
    cli.say("working")
    cli.result("error_max_turns")
    await until(lambda: env.turn_ends)
    assert env.turn_ends[0].subtype == drv.RESULT_SUBTYPE_MAX_TURNS
    assert driver.turn_open(env.rec.id) is False


async def test_context_usage_is_logged_at_open(env, caplog):
    import logging
    caplog.set_level(logging.INFO, logger=drv.__name__)
    driver = env.make()
    await open_job(env, driver)
    assert any("threshold=167000 source=env" in r.getMessage()
               for r in caplog.records)


async def test_a_completion_survives_its_callback_being_cancelled(env, monkeypatch):
    """Retiring a session cancels the client's outstanding tool callbacks. The
    completion's finalization runs in a Casa-owned task, so cancelling the
    emit_completion callback mid-finalization does not cut it."""
    import agent
    driver = env.make()
    monkeypatch.setattr(agent, "active_engagement_driver", driver, raising=False)
    monkeypatch.setattr(tools, "_engagement_registry", None)
    release = asyncio.Event()
    finished = []

    async def finalize(engagement, **kw):
        await release.wait()
        finished.append(kw["outcome"])
        return tools.FinalizeResult.FINALIZED

    monkeypatch.setattr(tools, "_finalize_engagement", finalize)
    token = tools.engagement_var.set(env.rec)
    try:
        callback = asyncio.create_task(tools.emit_completion.handler(
            {"status": "ok", "text": "done"}))
        await settle()
    finally:
        tools.engagement_var.reset(token)
    callback.cancel()
    await settle()
    release.set()
    await until(lambda: finished)
    assert finished == ["completed"]


async def test_transcript_shapes_that_mean_taken_up(tmp_path):
    """The three line shapes the pinned CLI writes at take-up; the queue-time
    ``enqueue`` line carries no uuid and means nothing."""
    path = tmp_path / "t.jsonl"
    for obj in (
        {"type": "queue-operation", "operation": "enqueue"},
        {"type": "user", "uuid": "u-own-turn", "message": {"content": "x"}},
        {"type": "attachment", "attachment": {"type": "queued_command",
                                              "source_uuid": "u-absorbed"}},
        {"type": "queue-operation", "operation": "remove",
         "commandUuid": "u-removed", "reason": "absorbed_mid_turn"},
        {"type": "attachment", "attachment": {"type": "date"}, "uuid": "u-other"},
    ):
        transcript_line(path, **obj)
    with open(path, "a") as fh:
        fh.write("not json\n")
    assert job_inbox.uuids_in_transcript(path) == {
        "u-own-turn", "u-absorbed", "u-removed"}
    assert job_inbox.uuids_in_transcript(tmp_path / "missing.jsonl") == set()


async def test_the_job_cost_is_its_base_plus_the_cli_total_at_each_turn_end(env):
    """INV-BGJOB-020: the CLI reports ``total_cost_usd`` cumulatively for its
    own process (a resumed one starts again at 0), so the job's cost at a turn
    end is the record's cost when this client opened plus that total."""
    env.rec.origin["job"]["cost_usd"] = 1.5
    driver = env.make()
    cli = await open_job(env, driver)
    driver.submit_casa(env.rec, "work")
    await until(lambda: cli.written)
    cli.echo(0)
    cli.result()                        # total_cost_usd=0.01
    await until(lambda: env.turn_ends)
    assert env.rec.origin["job"]["cost_usd"] == pytest.approx(1.51)
    cli.frames.put_nowait(ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="sess-1", total_cost_usd=0.25, usage={}))
    await until(lambda: len(env.turn_ends) == 2)
    assert env.rec.origin["job"]["cost_usd"] == pytest.approx(1.75)


async def _with_runner(env, monkeypatch):
    """The real driver wired to the real job runner, as casa_core wires them."""
    import background_jobs as bj
    reg = env.reg

    async def persist_origin(eid):
        return None
    reg.persist_origin = persist_origin
    reg.terminal_pending = lambda eid: False
    finalized = []

    async def finalize(rec, **kw):
        finalized.append(kw)
        rec.status = kw["outcome"]
    monkeypatch.setattr(tools, "_finalize_engagement", finalize)
    runner = bj.JobRunner(registry=reg)
    driver = drv.InCasaDriver(
        record_lookup=reg.get,
        begin_turn_delivery=reg.begin_turn_delivery, stamp_activity=reg.stamp,
        on_turn_end=runner.turn_ended, on_session_lost=runner.session_lost,
        on_operator_written=bj.note_operator_input)
    runner.driver = driver
    bj.configure(runner)
    return driver, finalized


async def test_a_job_that_stops_twice_is_nudged_then_parked(env, monkeypatch):
    """INV-BGJOB-017 end to end: the brief's turn ends unfinished — one nudge
    is written into the session; the nudge's turn ends unfinished too — the
    job is parked, still live."""
    import background_jobs as bj
    driver, finalized = await _with_runner(env, monkeypatch)
    env.rec.kind = "specialist"
    cli = await open_job(env, driver)
    driver.enqueue_brief(env.rec, "BRIEF")
    await until(lambda: cli.written)
    cli.echo(0)
    cli.result()
    await until(lambda: len(cli.written) == 2)
    assert cli.texts()[1] == bj._NUDGE_ASK
    cli.echo(1)
    cli.result()
    await until(lambda: bj.awaiting(env.rec) == bj.AWAITING_STOPPED)
    await settle()
    assert len(cli.written) == 2 and finalized == []
    # The operator's reply continues it: written, and the job no longer waits.
    driver.submit_operator(env.rec, "carry on")
    await until(lambda: len(cli.written) == 3)
    assert bj.awaiting(env.rec) is None


async def test_a_dead_session_is_resumed_once_then_a_second_death_ends_it(
        env, monkeypatch):
    """INV-BGJOB-018 end to end: the first death resumes the session with the
    restart line; the replacement dying too ends the job error."""
    import background_jobs as bj
    driver, finalized = await _with_runner(env, monkeypatch)
    cli = await open_job(env, driver)
    transcript_line(env.transcript, type="user", uuid=BRIEF_UUID)
    cli.die()
    await until(lambda: len(FakeCLI.instances) == 2)
    new = FakeCLI.instances[1]
    await until(lambda: new.written)
    assert new.texts() == [bj.SESSION_RESTART_LINE]
    assert finalized == []
    new.die()
    await until(lambda: finalized)
    assert finalized[0]["outcome"] == "error"
    assert finalized[0]["text"].startswith("its session ended unexpectedly")
    assert len(FakeCLI.instances) == 2


class _PaintChannel:
    """The channel's end paint, as the real one reads it: the record's posts
    retired with the outcome, time and cost the record holds at that moment."""

    def __init__(self):
        self.painted: list[str] = []

    def retire_job_keyboards(self, rec):
        import background_jobs as bj
        self.painted.append(rec.status + bj.cost_suffix(rec))


async def _completing_job(env, tmp_path, monkeypatch):
    """A real registry and the real finalize funnel around the real driver:
    a job whose brief turn is running, ready to call emit_completion from
    inside it."""
    from unittest.mock import AsyncMock, MagicMock

    import agent
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="rows",
        origin={"role": "assistant", "channel": "telegram", "chat_id": "100",
                "job": {"brief_uuid": BRIEF_UUID, "title": "Rows"}})
    rec.sdk_session_id = "sess-1"
    channel = _PaintChannel()
    manager = MagicMock()
    manager.get = lambda name: channel
    notices: list[str] = []

    async def outcome(bus, *, complete, **kw):
        notices.append(complete.text)

    monkeypatch.setattr(tools, "send_engagement_outcome", outcome)
    tools.init_tools(
        channel_manager=manager, bus=MagicMock(notify=AsyncMock()),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg)
    driver = drv.InCasaDriver(
        record_lookup=reg.get,
        begin_turn_delivery=reg.begin_turn_delivery,
        stamp_activity=lambda eid, ts: None)
    monkeypatch.setattr(agent, "active_engagement_driver", driver, raising=False)
    await driver.open(rec, ClaudeAgentOptions())
    cli = FakeCLI.instances[-1]
    driver.enqueue_brief(rec, "BRIEF")
    await until(lambda: cli.written)
    cli.echo(0)
    await until(lambda: driver.turn_open(rec.id))
    token = tools.engagement_var.set(rec)
    try:
        call = asyncio.create_task(tools.emit_completion.handler(
            {"status": "ok", "text": "done"}))
    finally:
        tools.engagement_var.reset(token)
    return SimpleNamespace(rec=rec, cli=cli, call=call, channel=channel,
                           notices=notices)


async def test_a_one_turn_job_s_end_carries_that_turn_s_cost(
        env, tmp_path, monkeypatch):
    """INV-BGJOB-020: a job that completes inside its only turn is torn down
    only after that turn's ResultMessage, so the end paint and the end notice
    carry the turn's cost. The tool call itself returns at once: the turn
    cannot end before it has."""
    job = await _completing_job(env, tmp_path, monkeypatch)
    payload = json.loads((await asyncio.wait_for(job.call, 2))["content"][0]["text"])
    assert payload["status"] == "acknowledged"
    assert job.rec.status == "completed"
    await settle()
    assert job.channel.painted == [] and job.notices == []
    assert not job.cli.disconnected
    job.cli.result()                        # total_cost_usd=0.01
    await until(lambda: job.channel.painted and job.notices)
    assert job.channel.painted[0].startswith("completed")
    assert job.channel.painted[0].endswith(" · $0.01")
    assert job.notices[0].endswith(" · $0.01")
    assert job.cli.disconnected


async def test_a_completing_turn_that_never_ends_finalizes_after_the_bound(
        env, tmp_path, monkeypatch):
    """The completing turn's end is waited for, never required: past the
    bound the job is torn down and painted with the last cost known (none,
    for a job whose first turn never ended)."""
    monkeypatch.setattr(tools, "_COMPLETION_TURN_END_WAIT_S", 0.05)
    job = await _completing_job(env, tmp_path, monkeypatch)
    await asyncio.wait_for(job.call, 2)
    await until(lambda: job.channel.painted and job.notices)
    assert job.channel.painted[0].startswith("completed")
    assert "$" not in job.channel.painted[0]
    assert "$" not in job.notices[0]
    assert job.cli.disconnected


async def test_a_resume_waits_for_a_suspended_client_to_disconnect(env):
    """The idle suspension takes the client out of the map and disconnects
    it; a message arriving while that disconnect is still running resumes the
    job only after the old client is retired — never two clients at once."""
    driver = env.make()
    await open_job(env, driver)
    FakeCLI.hold_disconnect = asyncio.Event()
    suspension = asyncio.create_task(driver.cancel(env.rec))
    await settle()
    driver.submit_operator(env.rec, "are you there?")
    await settle(50)
    assert [e for e in FakeCLI.events if e.startswith("enter")] == ["enter:0"]
    FakeCLI.hold_disconnect.set()
    await suspension
    await until(lambda: len(FakeCLI.instances) == 2
                and FakeCLI.instances[1].written)
    assert FakeCLI.events.index("disconnect:0") < FakeCLI.events.index("enter:1")
    assert FakeCLI.instances[1].texts() == ["are you there?"]


async def test_two_settlements_of_one_ended_job_tell_a_lost_message_once(env):
    """Boot and a live terminal path settling the same ended job at once:
    one lost-message notice per entry, not one per settler."""
    driver = env.make()
    job_inbox.append(env.rec.id, uuid="cccccccc-0000-4000-8000-000000000001",
                     text="never read", notice=None)
    env.rec.status = "cancelled"
    told = []
    release = asyncio.Event()

    async def slow_tell(rec, entry):
        told.append(entry["text"])
        await release.wait()

    first = asyncio.create_task(driver.settle_journal_terminal(env.rec, slow_tell))
    await until(lambda: told)
    second = asyncio.create_task(driver.settle_journal_terminal(env.rec, slow_tell))
    await settle()
    release.set()
    assert sorted([await first, await second]) == [0, 1]
    assert told == ["never read"]


async def test_a_boot_reconcile_leaves_a_reply_written_to_the_live_session(env):
    """Telegram is up before the boot resume: the operator's reply resumed
    the job and was written into its live session, not yet taken up. The boot
    reconcile that runs next must not redeliver it — one write, one uuid."""
    driver = env.make()
    cli = await open_job(env, driver)
    ticket = driver.submit_operator(env.rec, "the reply")
    await until(lambda: cli.written)
    await driver.recover(env.rec, restart_line="RESTART", brief=None,
                         redelivery_prefix="(Sent before Casa restarted:)")
    await until(lambda: len(cli.written) == 2)
    await settle()
    assert cli.uuids().count(ticket.uuid) == 1
    assert cli.texts() == ["the reply", "RESTART"]
    assert len(FakeCLI.instances) == 1


async def test_a_reply_taken_up_during_the_boot_transcript_read_is_not_redelivered(
        env, monkeypatch):
    """The live session takes the reply up (its echo arrives) while the boot
    reconcile is still reading the transcript, whose snapshot lacks it: the
    reply is still written once."""
    import threading
    driver = env.make()
    cli = await open_job(env, driver)
    ticket = driver.submit_operator(env.rec, "the reply")
    await until(lambda: cli.written)
    entered, gate = threading.Event(), threading.Event()

    def slow_read(rec, sid):
        entered.set()
        gate.wait(2)
        return set()

    monkeypatch.setattr(drv, "_transcript_uuids", slow_read)
    task = asyncio.create_task(driver.recover(
        env.rec, restart_line="RESTART", brief=None,
        redelivery_prefix="(Sent before Casa restarted:)"))
    await until(entered.is_set)
    cli.echo(0)
    await settle()
    gate.set()
    await task
    await until(lambda: len(cli.written) >= 2)
    await settle()
    assert cli.uuids().count(ticket.uuid) == 1
    assert cli.texts() == ["the reply", "RESTART"]


async def test_a_live_boot_reconcile_redelivers_only_messages_from_before_the_restart(env):
    """A journal entry written before Casa restarted is redelivered once,
    marked; a reply this process accepted and wrote is not."""
    driver = env.make()
    before = "dddddddd-0000-4000-8000-000000000001"
    job_inbox.append(env.rec.id, uuid=before, text="sent before", notice=None)
    cli = await open_job(env, driver)
    ticket = driver.submit_operator(env.rec, "sent after")
    await until(lambda: cli.written)
    await driver.recover(env.rec, restart_line="RESTART", brief=None,
                         redelivery_prefix="(Sent before Casa restarted:)")
    await until(lambda: len(cli.written) == 3)
    await settle()
    assert cli.uuids().count(ticket.uuid) == 1
    assert cli.uuids().count(before) == 1
    assert cli.texts()[:2] == ["sent after", "RESTART"]
    assert "sent before" in cli.texts()[2]


async def test_a_session_death_during_the_boot_reconcile_leaves_recovery_to_its_own_owner(
        env, monkeypatch):
    """The live session dies while the boot reconcile reads the transcript:
    the boot path stages nothing and leaves the retirement in place; the
    death's own recovery disconnects the old client before opening the
    replacement and redelivers the written-but-unread reply once."""
    import threading
    holder = {}

    async def on_lost(rec):
        await holder["driver"].recover(
            rec, restart_line="RESTART-LOST", brief=None,
            redelivery_prefix="(Sent before the job's session restarted:)")

    driver = env.make(on_session_lost=on_lost)
    holder["driver"] = driver
    cli = await open_job(env, driver)
    ticket = driver.submit_operator(env.rec, "the reply")
    await until(lambda: cli.written)
    entered, gate = threading.Event(), threading.Event()

    def slow_read(rec, sid):
        entered.set()
        gate.wait(2)
        return set()

    monkeypatch.setattr(drv, "_transcript_uuids", slow_read)
    FakeCLI.hold_disconnect = asyncio.Event()
    boot = asyncio.create_task(driver.recover(
        env.rec, restart_line="RESTART-BOOT", brief=None,
        redelivery_prefix="(Sent before Casa restarted:)"))
    await until(entered.is_set)
    cli.die()
    await settle()
    gate.set()
    await boot
    await settle()
    assert len(FakeCLI.instances) == 1
    FakeCLI.hold_disconnect.set()
    await until(lambda: len(FakeCLI.instances) == 2
                and len(FakeCLI.instances[1].written) == 2)
    await settle()
    replacement = FakeCLI.instances[1]
    assert FakeCLI.events.index("disconnect:0") < FakeCLI.events.index("enter:1")
    assert replacement.texts()[0] == "RESTART-LOST"
    assert replacement.uuids().count(ticket.uuid) == 1
    assert cli.texts() == ["the reply"]


async def test_a_death_while_recovery_waits_for_the_lock_still_retires_first(env):
    """The worker is resuming a suspended job (holding the lifecycle lock)
    when boot recovery queues on that lock; the session the worker opened
    dies and its disconnect is still running when the lock is released.
    Boot's resume opens its client only after that disconnect, and the
    operator's reply is written exactly once."""
    holder = {}

    async def on_lost(rec):
        await holder["driver"].recover(
            rec, restart_line="RESTART-LOST", brief=None,
            redelivery_prefix="(Sent before the job's session restarted:)")

    driver = env.make(on_session_lost=on_lost)
    holder["driver"] = driver
    await open_job(env, driver)
    await driver.cancel(env.rec)                # the idle suspension
    FakeCLI.hold_usage = asyncio.Event()
    ticket = driver.submit_operator(env.rec, "the reply")
    await until(lambda: len(FakeCLI.instances) == 2)
    boot = asyncio.create_task(driver.recover(
        env.rec, restart_line="RESTART-BOOT", brief=None,
        redelivery_prefix="(Sent before Casa restarted:)"))
    await settle()
    FakeCLI.hold_disconnect = asyncio.Event()
    FakeCLI.instances[1].die()
    await settle()
    FakeCLI.hold_usage.set()
    await settle(50)
    assert len(FakeCLI.instances) == 2
    FakeCLI.hold_disconnect.set()
    await boot
    await until(lambda: any(ticket.uuid in c.uuids() for c in FakeCLI.instances))
    await settle(50)
    assert FakeCLI.events.index("disconnect:1") < FakeCLI.events.index("enter:2")
    assert sum(c.uuids().count(ticket.uuid) for c in FakeCLI.instances) == 1


async def test_a_cancel_during_an_unfinished_suspension_keeps_the_barrier(
        env, monkeypatch):
    """The idle suspension is still disconnecting when a reply starts a
    resume and the operator's Cancel ends the job and its lane: the resume's
    client still opens only after the suspended client is disconnected."""
    import threading
    driver = env.make()
    await open_job(env, driver)
    FakeCLI.hold_disconnect = asyncio.Event()
    suspension = asyncio.create_task(driver.cancel(env.rec))
    await settle()
    entered, gate = threading.Event(), threading.Event()

    def slow_options(rec, sid):
        entered.set()
        gate.wait(2)
        return ClaudeAgentOptions(resume=sid)

    monkeypatch.setattr(tools, "build_engagement_resume_options", slow_options)
    driver.submit_operator(env.rec, "the reply")
    await until(entered.is_set)
    env.rec.status = "cancelled"
    await driver.cancel(env.rec)
    gate.set()
    await settle(50)
    assert [e for e in FakeCLI.events if e.startswith("enter")] == ["enter:0"]
    FakeCLI.hold_disconnect.set()
    await suspension
    await settle(50)
    if "enter:1" in FakeCLI.events:
        assert FakeCLI.events.index("disconnect:0") < FakeCLI.events.index("enter:1")


class _Launch:
    """The launcher's ownership, as the registry reports it: enrolled from
    the committed create until the launcher's finally, which wakes the
    job's worker."""

    def __init__(self):
        self.in_flight: set[str] = set()

    def __call__(self, eid):
        return eid in self.in_flight

    async def run(self, driver, rec, *, brief="BRIEF", fail=False):
        self.in_flight.add(rec.id)
        try:
            if fail:
                raise RuntimeError("the client could not start")
            await driver.open(rec, ClaudeAgentOptions())
            await asyncio.sleep(0)          # an await before the brief
            driver.enqueue_brief(rec, brief)
        finally:
            self.in_flight.discard(rec.id)
            driver.wake_worker(rec.id)


def _lost_recorder(holder):
    async def on_lost(rec):
        holder.setdefault("lost", 0)
        holder["lost"] += 1
        await holder["driver"].recover(
            rec, restart_line="RESTART", brief=None, redelivery_prefix="x")
    return on_lost


async def test_a_reply_during_the_launch_waits_for_it_and_follows_the_brief(env):
    """An operator message arrives while the launch's first client is still
    connecting: nothing resumes or recovers, the job stays live, and the
    reply is written after the brief."""
    holder, launch = {}, _Launch()
    driver = env.make(on_session_lost=_lost_recorder(holder),
                      launch_in_flight=launch)
    holder["driver"] = driver
    env.rec.sdk_session_id = None
    FakeCLI.hold_enter = asyncio.Event()
    launch.in_flight.add(env.rec.id)       # enrolled at the committed create
    ticket = driver.submit_operator(env.rec, "the reply")
    task = asyncio.create_task(launch.run(driver, env.rec))
    await until(lambda: FakeCLI.instances)
    await settle(50)
    assert "lost" not in holder and env.rec.status == "active"
    FakeCLI.hold_enter.set()
    await task
    cli = FakeCLI.instances[0]
    await until(lambda: len(cli.written) == 2)
    assert cli.texts() == ["BRIEF", "the reply"]
    assert cli.uuids() == [BRIEF_UUID, ticket.uuid]
    assert len(FakeCLI.instances) == 1 and "lost" not in holder


async def test_a_reply_after_the_client_registers_still_follows_the_brief(env):
    """The client is registered and the launch is still probing it when the
    reply arrives: the brief is still written first, and each item once."""
    launch = _Launch()
    driver = env.make(launch_in_flight=launch)
    env.rec.sdk_session_id = None
    FakeCLI.hold_usage = asyncio.Event()
    task = asyncio.create_task(launch.run(driver, env.rec))
    await until(lambda: driver.is_alive(env.rec))
    ticket = driver.submit_operator(env.rec, "the reply")
    await settle(50)
    cli = FakeCLI.instances[0]
    assert cli.written == []
    FakeCLI.hold_usage.set()
    await task
    await until(lambda: len(cli.written) == 2)
    await settle()
    assert cli.texts() == ["BRIEF", "the reply"]
    assert cli.uuids() == [BRIEF_UUID, ticket.uuid]


async def test_a_launch_that_fails_and_leaves_the_record_live_ends_the_job(env):
    """The launch fails before any client registers and its record stays
    live: when the launcher lets go, the worker resumes, finds nothing to
    resume, and the job reaches the ordinary failed-resume end — the queued
    reply does not wait forever."""
    holder, launch = {}, _Launch()
    driver = env.make(on_session_lost=_lost_recorder(holder),
                      launch_in_flight=launch)
    holder["driver"] = driver
    env.rec.sdk_session_id = None
    launch.in_flight.add(env.rec.id)
    driver.submit_operator(env.rec, "the reply")
    await settle()                          # the worker parks: launch in flight
    with pytest.raises(RuntimeError):
        await launch.run(driver, env.rec, fail=True)
    await until(lambda: holder.get("lost") == 1)
    await settle(50)
    assert FakeCLI.instances == []
    assert driver.recovering(env.rec.id) is False


async def test_a_client_that_dies_while_the_launch_probes_it_has_a_recovery_owner(env):
    """The launch's client dies after registration, before the launch's
    worker exists: its death still reaches the session-lost owner."""
    holder, launch = {}, _Launch()
    driver = env.make(on_session_lost=_lost_recorder(holder),
                      launch_in_flight=launch)
    holder["driver"] = driver
    env.rec.sdk_session_id = None
    FakeCLI.hold_usage = asyncio.Event()
    task = asyncio.create_task(launch.run(driver, env.rec))
    await until(lambda: driver.is_alive(env.rec))
    FakeCLI.instances[0].die()
    await until(lambda: holder.get("lost") == 1)
    FakeCLI.hold_usage.set()
    await task


async def test_a_lost_message_notice_leaves_the_journal_as_soon_as_it_is_told(env):
    """Casa dies after telling the first of two lost messages: the next
    settlement tells only the second."""
    driver = env.make()
    first, second = ("eeeeeeee-0000-4000-8000-000000000001",
                     "eeeeeeee-0000-4000-8000-000000000002")
    job_inbox.append(env.rec.id, uuid=first, text="one", notice=None)
    job_inbox.append(env.rec.id, uuid=second, text="two", notice=None)
    env.rec.status = "cancelled"
    told = []

    class Died(BaseException):
        pass

    async def dies_after_first(rec, entry):
        if told:
            raise Died()
        told.append(entry["text"])

    with pytest.raises(Died):
        await driver.settle_journal_terminal(env.rec, dies_after_first)
    assert [e["uuid"] for e in job_inbox.load(env.rec.id)] == [second]
    again = []

    async def tell(rec, entry):
        again.append(entry["text"])

    assert await driver.settle_journal_terminal(env.rec, tell) == 1
    assert told == ["one"] and again == ["two"]


async def test_a_finished_write_removes_its_own_ticket_not_the_queue_head(env):
    """Something is staged at the head of the queue while a write is in the
    transport (a recovery's restart line, a brief): the finished write
    removes its own ticket, so the staged item is written next and the
    operator's message once."""
    driver = env.make()
    cli = await open_job(env, driver)
    cli.gate = asyncio.Event()
    ticket = driver.submit_operator(env.rec, "the reply")
    await until(lambda: cli.writing == 1)
    driver.submit_casa(env.rec, "STAGED", head=True)
    cli.gate.set()
    await until(lambda: len(cli.written) == 2)
    await settle()
    assert cli.texts() == ["the reply", "STAGED"]
    assert cli.uuids().count(ticket.uuid) == 1
