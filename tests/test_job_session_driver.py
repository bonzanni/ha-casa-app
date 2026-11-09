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
        id=eid, kind="plugin", role_or_type="assistant", topic_id=None,
        origin={"job": {"brief_uuid": BRIEF_UUID, "title": "Rows"}},
        status="active", sdk_session_id="sess-1", started_at=0.0,
        context_rebuild_pending=False, plugin_artifacts=())


@pytest.fixture
def env(tmp_path, monkeypatch):
    FakeCLI.instances = []
    FakeCLI.events = []
    FakeCLI.on_enter = None
    FakeCLI.hold_disconnect = None
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
            topic_stream_factory=lambda topic: None,
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
        topic_stream_factory=lambda topic: None, record_lookup=reg.get,
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
