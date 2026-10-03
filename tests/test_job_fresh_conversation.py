"""A job declared ``session: "fresh"``: every turn after its launch starts a
fresh conversation that begins with the job brief (INV-BGJOB-005).

Drives the real ``InCasaDriver._deliver_turn``, registry and Telegram owners
through the batch-loop harness, with a fake SDK client whose ``/clear`` emits
what the pinned CLI was observed to emit live: a ``ConversationResetMessage``
carrying the OLD session id, an ``init`` ``SystemMessage`` carrying a NEW one,
then a ``ResultMessage(subtype="success", num_turns=0)``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions, ConversationResetMessage, ResultMessage, SystemMessage,
)

import background_jobs as jobs
import tools
from drivers.in_casa_driver import EngagementTerminalError
from engagement_registry import JOB_SIDS_KEY, EngagementRegistry
from plugin_store import StoreError, manifest_jobs

try:
    from tests.test_background_jobs_loop import Client, harness, result, text_frame  # noqa: F401
    from tests.test_start_job import DECL, runtime  # noqa: F401
    from tests.test_start_job import Client as LaunchClient
except ImportError:
    from test_background_jobs_loop import Client, harness, result, text_frame  # noqa: F401
    from test_start_job import DECL, runtime  # noqa: F401
    from test_start_job import Client as LaunchClient

pytestmark = [pytest.mark.unit]   # asyncio_mode = auto (pytest.ini)

RESET = "/clear"
TASK = "Classify the SECRET-TASK ledger rows"
# Byte-for-byte: CRLF, leading/trailing blanks, braces, a non-ASCII dash, a
# backslash sequence and a trailing newline must all survive into the brief.
CONTEXT = "  rows 1–9 only\r\n{account}: $x \\n keep\t tabs \n"


def init(sid):
    return SystemMessage("init", {"session_id": sid, "tools": ["Skill"]})


def reset_frame(old):
    return ConversationResetMessage(new_conversation_id="conv", uuid="u", session_id=old)


def clear_result(sid, *, is_error=False):
    return ResultMessage("success", 1, 0, is_error, 0, sid)


class FreshClient(Client):
    """The harness client plus the live-observed ``/clear`` sequence, and
    session ids that move only when the conversation is reset."""

    def __init__(self):
        super().__init__()
        self.sid = "launch"
        self.clears = 0
        self.opens = 0
        self.closes = 0
        self.clear_scripts = []   # optional overrides: (old, new) -> frames
        self.fail_prompt = None   # (prompt, exc): that query raises
        self.next_sid = lambda n: f"s{n}"   # the sid the n-th reset produces

    async def __aenter__(self):
        self.opens += 1
        return await super().__aenter__()

    async def close(self):
        self.closes += 1
        await super().close()

    async def query(self, prompt):
        if prompt == RESET:
            assert not self.closed
            self.prompts.append(prompt)
            self.clears += 1
            old, self.sid = self.sid, self.next_sid(self.clears)
            if self.clear_scripts:
                self.current = self.clear_scripts.pop(0)(old, self.sid)
            else:
                self.current = [reset_frame(old), init(self.sid), clear_result(self.sid)]
            return
        if self.fail_prompt is not None and prompt == self.fail_prompt[0]:
            self.prompts.append(prompt)
            raise self.fail_prompt[1]
        await super().query(prompt)
        self.current = [init(self.sid)] + [
            dataclasses.replace(item, session_id=self.sid)
            if isinstance(item, ResultMessage) else item
            for item in self.current]


def ledger(origin):
    """The fresh job's sid ledger where the transcript reaper reads it (#1162)."""
    return origin["job"][JOB_SIDS_KEY]


def no_ledger(origin):
    job = origin.get("job")
    return JOB_SIDS_KEY not in origin and not (
        isinstance(job, dict) and JOB_SIDS_KEY in job)


def queries(h):
    """Every prompt after the launch prompt, /clear included."""
    return h.client.prompts[1:]


def fresh_setup(h):
    h.rec.task = TASK
    h.rec.origin["job"]["session"] = "fresh"
    h.rec.origin["job"]["brief_context"] = CONTEXT
    h.client = FreshClient()


@pytest.fixture
def fresh(harness):
    fresh_setup(harness)
    return harness


def spy_accepts(h):
    calls = []
    real = h.driver._accept_inbound

    def accept(eid, token):
        calls.append(token)
        real(eid, token)
    h.driver._accept_inbound = accept
    return calls


def spy_finalize(monkeypatch):
    outcomes = []
    real = tools._finalize_engagement

    async def finalize(rec, *, outcome, **kw):
        outcomes.append((outcome, kw.get("text")))
        return await real(rec, outcome=outcome, **kw)
    monkeypatch.setattr(tools, "_finalize_engagement", finalize)
    return outcomes


def spy_send_errors(h):
    errors = []
    real = h.channel._driver_send_user_turn

    async def send(r, text, **kw):
        try:
            return await real(r, text, **kw)
        except BaseException as exc:
            errors.append(exc)
            raise
    h.channel._driver_send_user_turn = send
    return errors


# -- red case 1 -----------------------------------------------------------

async def test_each_batch_resets_once_then_starts_with_the_brief(fresh, tmp_path):
    h = fresh
    persisted = []
    real_persist = h.driver._persist_session_id

    async def persist(eid, sid):
        persisted.append(sid)
        await real_persist(eid, sid)
    h.driver._persist_session_id = persist
    h.client.scripts = [[text_frame("Batch work"), h.report, result()],
                        [h.complete, result()]]
    await h.start()
    await h.drain()
    q = queries(h)
    # Exactly one reset before each batch's query, and nothing else.
    assert q.count(RESET) == 2
    assert [q[0], q[2]] == [RESET, RESET]
    assert len(q) == 4
    brief = jobs.job_brief(h.rec)
    assert q[1] == f"{brief}\n\n{jobs.batch_prompt(1, 'Process rows')}"
    assert q[3] == f"{brief}\n\n{jobs.batch_prompt(2, 'Process rows')}"
    # The launch prompt itself gets neither a reset nor a brief.
    assert h.client.prompts[0] == "Acknowledge the job"
    assert persisted == ["launch", "s1", "s2"]
    assert ledger(h.rec.origin) == ["launch", "s1", "s2"]
    loaded = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"), bus=None)
    await loaded.load()
    assert ledger(loaded.get(h.rec.id).origin) == ["launch", "s1", "s2"]
    h.assert_terminal("completed", "All rows handled")


# -- red case 2 -----------------------------------------------------------

async def test_operator_message_waits_then_runs_fresh_on_the_same_client(
        fresh, monkeypatch):
    h = fresh
    finals = spy_finalize(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    at_operator_turn = []

    async def hold():
        entered.set()
        await release.wait()

    async def snapshot():
        at_operator_turn.append((h.client.opens, h.client.closes, list(finals)))
    h.client.scripts = [[text_frame("Working"), hold, h.report, result()],
                        [snapshot, text_frame("Answer"), result()],
                        [h.complete, result()]]
    await h.start()
    await asyncio.wait_for(entered.wait(), 5)
    await h.operator("Correction")
    for _ in range(20):
        await asyncio.sleep(0)
    # The message waits on the turn lock: no reset was sent under the batch.
    assert queries(h).count(RESET) == 1
    release.set()
    await h.drain()
    brief = jobs.job_brief(h.rec)
    assert queries(h) == [
        RESET, f"{brief}\n\n{jobs.batch_prompt(1, 'Process rows')}",
        RESET, f"{brief}\n\nCorrection",
        RESET, f"{brief}\n\n{jobs.batch_prompt(2, 'Process rows')}"]
    # Same client throughout: one open (the launch's), no close, no finalize
    # while the operator's turn ran.
    assert at_operator_turn == [(1, 0, [])]
    assert "Answer" in h.topic()
    h.assert_terminal("completed", "All rows handled")
    assert [o for o, _ in finals] == ["completed"]


# -- red case 3 -----------------------------------------------------------

async def test_brief_context_is_the_launch_context_verbatim_across_batches_and_restart(
        fresh, tmp_path):
    h = fresh
    stalled = [text_frame("Working"), h.report, result()]
    h.client.scripts = [list(stalled), list(stalled)]
    h.rec.origin["job"]["batches"] = 2
    await h.start()
    await h.drain()
    q = [p for p in queries(h) if p != RESET]
    assert len(q) == 2
    for prompt in q:
        assert prompt.count(CONTEXT) == 1
        assert f"Context:\n{CONTEXT}\n" in prompt
        assert TASK in prompt
    # A restart: a new registry loads the record, the client is resumed, and
    # the first turn after it is fresh and carries the same context bytes.
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"), bus=None)
    await reg.load()
    assert reg.get(h.rec.id).origin["job"]["brief_context"] == CONTEXT


async def test_restart_turn_is_fresh_and_carries_the_launch_context(
        fresh, monkeypatch, tmp_path):
    from casa_core import _resume_background_jobs
    h = fresh
    h.rec.origin["job"].update(started=1, reported=True, last_summary="First batch")
    await h.reg.persist_origin(h.rec.id)
    await h.reg.persist_session_id(h.rec.id, "launch")
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"), bus=None)
    await reg.load()
    h.rec.permit.release()
    h.reg, h.rec = reg, reg.get(h.rec.id)
    h.channel._engagement_registry = reg
    monkeypatch.setattr(tools, "_engagement_registry", reg)
    h.driver._record_lookup = reg.get
    h.driver._begin_turn_delivery = reg.begin_turn_delivery
    h.driver._persist_session_id = reg.persist_session_id
    monkeypatch.setattr(tools, "build_engagement_resume_options",
                        lambda rec, sid: ClaudeAgentOptions(resume=sid))
    h.client.scripts = [[h.complete, result()]]
    await _resume_background_jobs(reg, h.channel)
    await h.drain()
    assert h.client.prompts[0] == RESET
    assert h.client.prompts.count(RESET) == 1
    prompt = h.client.prompts[1]
    assert prompt == f"{jobs.job_brief(h.rec)}\n\n{jobs.batch_prompt(2, 'Process rows')}"
    assert f"Context:\n{CONTEXT}\n" in prompt
    h.assert_terminal("completed", "All rows handled")


@pytest.mark.parametrize("session", ["fresh", "resume"])
async def test_start_job_records_brief_context_only_for_a_fresh_job(runtime, monkeypatch, session):
    import agent
    decl = dataclasses.replace(DECL, session=session)
    host = jobs.JobHost('specialist', 'finance', decl, SimpleNamespace(name='ledger'))
    monkeypatch.setattr(jobs, 'find_job_host', lambda name, caller, roles:
                        host if name == decl.qualified_name and 'finance' in roles else None)
    token = agent.origin_var.set(dict(role='assistant', execution_role='assistant',
                                    channel='telegram', chat_id='1', cid='test',
                                    user_text='classify', _operator_turn=True))
    try:
        envelope = await tools.start_job.handler(
            {"job": decl.qualified_name, "task": TASK, "context": CONTEXT})
    finally:
        agent.origin_var.reset(token)
    reply = json.loads(envelope['content'][0]['text'])
    assert reply['status'] == 'pending', reply
    await tools.drain_launch_turns()
    rec = runtime.registry.get(reply['engagement_id'])
    assert rec.origin['job']['session'] == session
    if session == "fresh":
        assert rec.origin['job']['brief_context'] == CONTEXT
    else:
        assert 'brief_context' not in rec.origin['job']
    loaded = EngagementRegistry(tombstone_path=runtime.registry._tombstone_path, bus=None)
    await loaded.load()
    assert loaded.get(rec.id).origin['job'] == rec.origin['job']


# -- red case 4 -----------------------------------------------------------

async def _rebuild(h):
    """The #369 rebuild as ``_resume_and_ready`` runs it: tear down, open a
    fresh floor client, clear the flag — no turn reaches a session while the
    flag is pending (#1166)."""
    await h.driver.invalidate_session(h.rec)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tools, "build_engagement_resume_options",
                   lambda rec, sid: ClaudeAgentOptions())
        await h.driver.open_fresh(h.rec)
    await h.reg.clear_context_rebuild_pending(h.rec.id)


async def test_clearance_downgrade_withholds_task_and_context_from_the_brief(fresh):
    h = fresh
    h.rec.origin["_origin_clearance"] = "private"
    h.client.scripts = [[text_frame("Starting the job"), result()]]
    await h.driver.start(h.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())
    assert await h.reg.lower_origin_clearance(h.rec.id, "public")
    assert "brief_context" not in h.rec.origin["job"]
    await _rebuild(h)
    h.client.scripts = [[text_frame("ok"), result()]]
    await h.driver.send_user_turn(h.rec, jobs.batch_prompt(1, "Process rows"))
    assert queries(h).count(RESET) == 1
    prompt = queries(h)[-1]
    assert "SECRET-TASK" not in prompt
    assert CONTEXT not in prompt and CONTEXT.strip() not in prompt
    assert "rows 1–9" not in prompt
    assert "withheld" in prompt
    assert prompt.startswith(jobs.job_brief(h.rec))


# -- red case 5 -----------------------------------------------------------

def _failure_counts(h, finals):
    return {
        "turn_failed_notices": h.topic().count("Turn failed"),
        "finalizations": len(finals),
        "status": h.rec.status,
        "topics_closed": len(h.bot.closed),
        "permits_held": h.limiter.in_flight,
        "turn_owners": jobs.turn_owners(h.rec.id),
    }


async def _run_one_failing_batch(h, monkeypatch, *, fresh_mode, failure):
    finals = spy_finalize(monkeypatch)
    if fresh_mode:
        fresh_setup(h)
    else:
        h.client = FreshClient()
    accepts = spy_accepts(h)
    if failure == "no_reset":
        h.client.clear_scripts = [lambda old, new: [init(new), clear_result(new)]]
    elif failure == "raises":
        h.client.clear_scripts = [lambda old, new: [RuntimeError("clear broke")]]
    elif failure == "is_error":
        h.client.clear_scripts = [
            lambda old, new: [reset_frame(old), init(new), clear_result(new, is_error=True)]]
    elif failure == "query_raises":
        h.client.fail_prompt = (jobs.batch_prompt(1, "Process rows"),
                                RuntimeError("transport broke"))
    # The launch turn is scripted; only batch 1 fails.
    h.client.scripts.insert(0, [text_frame("Starting the job"), result()])
    await h.driver.start(h.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())
    accepts.clear()
    await jobs.job_after_turn(h.rec, h.channel)
    await h.drain()
    return finals, accepts


@pytest.mark.parametrize("failure", ["no_reset", "raises", "is_error"])
async def test_an_unconfirmed_reset_fails_the_turn_before_acceptance(
        harness, monkeypatch, failure):
    h = harness
    finals, accepts = await _run_one_failing_batch(
        h, monkeypatch, fresh_mode=True, failure=failure)
    # 0 prompt queries: the only thing sent after the launch is the reset.
    assert queries(h) == [RESET]
    assert accepts == []
    assert finals == [("error", "a batch failed: ConversationResetError")]
    counts = _failure_counts(h, finals)
    assert counts == {"turn_failed_notices": 1, "finalizations": 1, "status": "error",
                      "topics_closed": 1, "permits_held": 0, "turn_owners": 0}


async def test_a_failed_query_today_has_the_same_failure_counts(harness, monkeypatch):
    """The baseline red case 5 compares against: a resume-mode job whose
    batch query raises takes today's path with these counts."""
    h = harness
    finals, accepts = await _run_one_failing_batch(
        h, monkeypatch, fresh_mode=False, failure="query_raises")
    assert queries(h) == [jobs.batch_prompt(1, "Process rows")]
    assert len(accepts) == 1
    assert finals == [("error", "a batch failed: RuntimeError")]
    assert _failure_counts(h, finals) == {
        "turn_failed_notices": 1, "finalizations": 1, "status": "error",
        "topics_closed": 1, "permits_held": 0, "turn_owners": 0}


# -- red case 6 -----------------------------------------------------------

@pytest.mark.parametrize("legacy", [False, True])
async def test_a_resume_mode_job_never_resets(harness, legacy):
    h = harness
    h.client = FreshClient()
    if legacy:
        # A record written before `session` existed reads as resume.
        h.rec.origin["job"].pop("session", None)
    h.client.scripts = [[text_frame("Batch work"), h.report, result()],
                        [text_frame("Answer"), result()], [h.complete, result()]]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()
    h.client.scripts[0].insert(1, hold)
    await h.start()
    await asyncio.wait_for(entered.wait(), 5)
    await h.operator("Correction")
    release.set()
    await h.drain()
    assert h.client.clears == 0
    assert queries(h) == [jobs.batch_prompt(1, "Process rows"), "Correction",
                          jobs.batch_prompt(2, "Process rows")]
    assert no_ledger(h.rec.origin)
    h.assert_terminal("completed", "All rows handled")


async def test_a_non_job_engagement_never_resets(harness):
    h = harness
    h.client = FreshClient()
    h.rec.origin.pop("job")
    h.client.scripts = [[text_frame("Hello"), result()], [text_frame("Answer"), result()]]
    await h.driver.start(h.rec, prompt="Engage", options=ClaudeAgentOptions())
    await h.driver.send_user_turn(h.rec, "Operator text")
    assert h.client.clears == 0
    assert h.client.prompts == ["Engage", "Operator text"]
    assert no_ledger(h.rec.origin)


# -- the #690 fence still comes first ------------------------------------

async def test_a_terminal_fresh_job_is_refused_before_any_reset(fresh):
    h = fresh
    h.client.scripts = [[text_frame("Starting the job"), result()]]
    await h.driver.start(h.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())
    await h.reg.mark_cancelled(h.rec.id)
    with pytest.raises(EngagementTerminalError):
        await h.driver.send_user_turn(h.rec, jobs.batch_prompt(1, "Process rows"))
    assert h.client.clears == 0
    assert queries(h) == []


# -- red case 8a ----------------------------------------------------------

@pytest.mark.parametrize("where", ["reset_drain", "sid_persist"])
async def test_cancel_committed_during_the_reset_refuses_the_turn(
        fresh, monkeypatch, where):
    h = fresh
    finals = spy_finalize(monkeypatch)
    errors = spy_send_errors(h)
    h.client.scripts = [[text_frame("Starting the job"), result()]]
    await h.driver.start(h.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())
    accepts = spy_accepts(h)

    async def cancel():
        await h.channel._finalize_cancel(h.rec, reason="operator")
    if where == "reset_drain":
        h.client.clear_scripts = [
            lambda old, new: [reset_frame(old), init(new), cancel, clear_result(new)]]
    else:
        real_persist = h.driver._persist_session_id

        async def persist(eid, sid):
            if sid == "s1":
                await cancel()
            await real_persist(eid, sid)
        h.driver._persist_session_id = persist
    await jobs.job_after_turn(h.rec, h.channel)
    await h.drain()
    assert queries(h) == [RESET]
    assert accepts == []
    assert [type(e) for e in errors] == [EngagementTerminalError]
    assert h.rec.status == "cancelled"
    assert [o for o, _ in finals] == ["cancelled"]
    assert "Turn failed" not in h.topic()


# -- red case 7 -----------------------------------------------------------

def _manifest(**fields):
    job = {"name": "classify", "skill": "classify", "title": "Classify",
           "batches": "unlimited", **fields}
    return {"name": "finance", "casa": {"jobs": [job]}}


@pytest.mark.parametrize("value", ["other", "", None, True, "FRESH"])
def test_an_unknown_session_value_is_jobs_invalid(value):
    with pytest.raises(StoreError) as exc:
        manifest_jobs(_manifest(session=value))
    assert exc.value.reason_code == "jobs_invalid"
    assert "session" in str(exc.value)


@pytest.mark.parametrize("value", ["fresh", "resume", None])
def test_session_flows_from_declaration_to_record(monkeypatch, value):
    import plugin_registry
    manifest = _manifest() if value is None else _manifest(session=value)
    assert manifest_jobs(manifest)[0].get("session") == value
    monkeypatch.setattr(plugin_registry, "resolve_for", lambda scope: SimpleNamespace(
        plugins=[SimpleNamespace(manifest=manifest)]))
    decl, _plugin = jobs.jobs_for_target("specialist:finance")["finance:classify"]
    assert decl.session == (value or "resume")
    assert jobs.initial_job_state(decl)["session"] == (value or "resume")


def test_a_legacy_record_without_session_is_resume():
    rec = SimpleNamespace(origin={"job": {"name": "a:b", "title": "T"}})
    assert jobs.job_session(rec) == "resume"
    assert not jobs.is_fresh_job(rec)
    assert not jobs.is_fresh_job(SimpleNamespace(origin={}))


# -- the sid ledger across a #369 clearance rebuild ----------------------

def _ledger_on_disk(h, tmp_path):
    raw = json.loads((tmp_path / "jobs.json").read_text())
    rows = raw if isinstance(raw, list) else raw.get("records", raw)
    row = next(r for r in rows if r["id"] == h.rec.id)
    return (row["origin"].get("job") or {}).get(JOB_SIDS_KEY)


async def test_rebuild_sid_is_ledgered_from_the_reset_message(fresh, monkeypatch, tmp_path):
    """A #369 rebuild opens a fresh client whose own sid is announced by no
    ``init`` before its first ``/clear``: the reset message's (outgoing)
    ``session_id`` is the only frame that carries it, so the ledger must take
    it from there, in the order the client reported it."""
    from claude_agent_sdk.types import ConversationResetMessage as SdkReset
    from claude_agent_sdk.types import ResultMessage as SdkResult
    from claude_agent_sdk.types import SystemMessage as SdkSystem
    h = fresh
    h.client.scripts = [[text_frame("Starting the job"), result()]]
    await h.driver.start(h.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())
    assert ledger(h.rec.origin) == ["launch"]
    # The clearance-downgrade teardown and the fresh floor client (#369).
    await h.driver.invalidate_session(h.rec)
    monkeypatch.setattr(tools, "build_engagement_resume_options",
                        lambda rec, sid: ClaudeAgentOptions())
    await h.driver.open_fresh(h.rec)
    h.client.sid = "rebuild"   # the fresh CLI's own session, not yet reported
    h.client.clear_scripts = [lambda old, new: [
        SdkReset(new_conversation_id="conv", uuid="u", session_id=old),
        SdkSystem("init", {"session_id": new, "tools": ["Skill"]}),
        SdkResult("success", 1, 0, False, 0, new)]]
    h.client.scripts = [[text_frame("ok"), result()]]
    await h.driver.send_user_turn(h.rec, jobs.batch_prompt(1, "Process rows"))
    assert h.client.prompts.count(RESET) == 1
    assert ledger(h.rec.origin) == ["launch", "rebuild", "s1"]
    assert _ledger_on_disk(h, tmp_path) == ["launch", "rebuild", "s1"]


@pytest.mark.parametrize("kind", ["resume_job", "legacy_job", "non_job"])
async def test_an_operator_clear_on_a_non_fresh_engagement_records_only_the_new_sid(
        harness, tmp_path, kind):
    """An operator may type ``/clear`` into any topic; the CLI then emits the
    reset sequence inside an ordinary turn. Only a fresh job reads the
    outgoing sid off the reset frame — every other engagement keeps today's
    behaviour: one durable write, of the new sid, and the outgoing sid
    nowhere."""
    from claude_agent_sdk.types import ConversationResetMessage as SdkReset
    from claude_agent_sdk.types import ResultMessage as SdkResult
    from claude_agent_sdk.types import SystemMessage as SdkSystem
    h = harness
    h.client = FreshClient()
    if kind == "legacy_job":
        h.rec.origin["job"].pop("session", None)
    elif kind == "non_job":
        h.rec.origin.pop("job")
    h.client.scripts = [[text_frame("Hello"), result()]]
    await h.driver.start(h.rec, prompt="Engage", options=ClaudeAgentOptions())
    assert h.rec.sdk_session_id == "launch"
    # The topic's client is now on a session the record has not seen yet
    # (a rebuilt one); the operator's /clear is relayed verbatim.
    h.client.sid = "rebuild"
    writes = []
    real_persist = h.driver._persist_session_id

    async def persist(eid, sid):
        writes.append(sid)
        await real_persist(eid, sid)
    h.driver._persist_session_id = persist
    h.client.clear_scripts = [lambda old, new: [
        SdkReset(new_conversation_id="conv", uuid="u", session_id=old),
        SdkSystem("init", {"session_id": new, "tools": ["Skill"]}),
        SdkResult("success", 1, 0, False, 0, new)]]
    await h.driver.send_user_turn(h.rec, RESET)
    assert h.client.prompts == ["Engage", RESET]
    assert writes == ["s1"]
    assert h.rec.sdk_session_id == "s1"
    assert h.driver.get_session_id(h.rec) == "s1"
    assert no_ledger(h.rec.origin)
    on_disk = (tmp_path / "jobs.json").read_text()
    assert '"rebuild"' not in on_disk and '"s1"' in on_disk


# -- the job id: the launch turn and every fresh turn name the engagement --

def _job_id_lines(text, rec):
    return [line for line in text.splitlines() if line.startswith("Job id:")], f"Job id: {rec.id}"


@pytest.mark.parametrize("session", ["fresh", "resume"])
async def test_the_launch_prompt_names_the_job_id_once(runtime, monkeypatch, session):
    """The launch prompt — for either mode — carries exactly one
    ``Job id: <engagement id>`` line, right after its first line: a plugin
    claims work by that id and matches the job-end notice's delegation id to
    it."""
    import agent
    decl = dataclasses.replace(DECL, session=session)
    host = jobs.JobHost('specialist', 'finance', decl, SimpleNamespace(name='ledger'))
    monkeypatch.setattr(jobs, 'find_job_host', lambda name, caller, roles:
                        host if name == decl.qualified_name and 'finance' in roles else None)
    token = agent.origin_var.set(dict(role='assistant', execution_role='assistant',
                                    channel='telegram', chat_id='1', cid='test',
                                    user_text='classify', _operator_turn=True))
    try:
        envelope = await tools.start_job.handler(
            {"job": decl.qualified_name, "task": TASK, "context": CONTEXT})
    finally:
        agent.origin_var.reset(token)
    reply = json.loads(envelope['content'][0]['text'])
    assert reply['status'] == 'pending', reply
    await tools.drain_launch_turns()
    rec = runtime.registry.get(reply['engagement_id'])
    launch = LaunchClient.instances[0].prompts[0]
    lines, expected = _job_id_lines(launch, rec)
    assert lines == [expected]
    assert launch.splitlines()[1] == expected
    assert launch.splitlines()[0].startswith('You are starting the background job')


async def test_every_fresh_turn_names_the_job_id_once(fresh):
    h = fresh
    h.client.scripts = [[text_frame("Batch work"), h.report, result()],
                        [h.complete, result()]]
    await h.start()
    await h.drain()
    turns = [q for q in queries(h) if q != RESET]
    assert len(turns) == 2
    for prompt in turns:
        lines, expected = _job_id_lines(prompt, h.rec)
        assert lines == [expected]


async def test_a_clearance_downgrade_keeps_the_job_id_in_the_brief(fresh):
    h = fresh
    h.rec.origin["_origin_clearance"] = "private"
    h.client.scripts = [[text_frame("Starting the job"), result()]]
    await h.driver.start(h.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())
    assert await h.reg.lower_origin_clearance(h.rec.id, "public")
    await _rebuild(h)
    h.client.scripts = [[text_frame("ok"), result()]]
    await h.driver.send_user_turn(h.rec, jobs.batch_prompt(1, "Process rows"))
    prompt = queries(h)[-1]
    lines, expected = _job_id_lines(prompt, h.rec)
    assert lines == [expected]
    assert "SECRET-TASK" not in prompt


async def test_a_clearance_downgrade_keeps_the_sid_ledger(fresh, tmp_path):
    """The downgrade withholds the launch materials and drops the resume
    pointer, but the sid ledger — what names the transcripts on disk —
    survives every step of it, in memory and on disk."""
    h = fresh
    h.rec.origin["_origin_clearance"] = "private"
    h.client.scripts = [[text_frame("Starting the job"), result()],
                        [text_frame("Batch work"), result()]]
    await h.driver.start(h.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())
    await h.driver.send_user_turn(h.rec, jobs.batch_prompt(1, "Process rows"))
    expected = ["launch", "s1"]
    assert ledger(h.rec.origin) == expected
    assert _ledger_on_disk(h, tmp_path) == expected
    assert await h.reg.lower_origin_clearance(h.rec.id, "public")
    assert ledger(h.rec.origin) == expected
    assert _ledger_on_disk(h, tmp_path) == expected
    await h.driver.invalidate_session(h.rec)
    await h.reg.clear_session_id(h.rec.id)
    assert h.rec.sdk_session_id is None
    assert ledger(h.rec.origin) == expected
    assert _ledger_on_disk(h, tmp_path) == expected
    loaded = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"), bus=None)
    await loaded.load()
    assert ledger(loaded.get(h.rec.id).origin) == expected


# -- coupling with the transcript reaper (#1162) --------------------------
#
# S1 writes the ledger, the reaper reads it. These drive S1's real /clear path
# to terminal and then the reaper's real entry point, with transcripts seeded
# under a tmp HOME through the SDK's own cwd -> project-key mapping.

def _uuids(n):
    import uuid
    return [str(uuid.uuid4()) for _ in range(n)]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return h


def _project(home, cwd):
    from claude_agent_sdk import project_key_for_directory
    return home / ".claude" / "projects" / project_key_for_directory(cwd)


def _seed(project, sid):
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{sid}.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    results = project / sid / "tool-results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "result.txt").write_text("payload", encoding="utf-8")


def _snapshot(project):
    return {p.relative_to(project): p.read_bytes()
            for p in project.rglob("*") if p.is_file()}


def _worker_cwd(monkeypatch):
    """The harness specialist ("worker") as the reaper resolves it."""
    cfg = SimpleNamespace(role="worker", cwd="")

    class _Reg:
        def get(self, role):
            return cfg if role == "worker" else None
    monkeypatch.setattr(tools, "_specialist_registry", _Reg())
    return tools.specialist_cwd(cfg)


async def _reap(h):
    import engagement_transcript_reaper
    return await engagement_transcript_reaper.reap_engagement_transcripts(h.reg)


async def test_reaper_deletes_every_fresh_batch_transcript_and_nothing_else(
        fresh, home, monkeypatch):
    """Design red case 8: a specialist-hosted fresh job runs its launch and two
    batches through the real reset path to terminal; the reaper then deletes
    every session the job's client served — the EARLIER batches' included —
    and no other file in the shared project dir."""
    h = fresh
    launch, b1, b2, unrelated = _uuids(4)
    h.client.sid = launch
    h.client.next_sid = {1: b1, 2: b2}.__getitem__
    h.client.scripts = [[text_frame("Batch work"), h.report, result()],
                        [h.complete, result()]]
    await h.start()
    await h.drain()
    assert h.rec.status == "completed"
    assert queries(h).count(RESET) == 2
    assert ledger(h.rec.origin) == [launch, b1, b2]
    assert h.rec.sdk_session_id == b2

    project = _project(home, _worker_cwd(monkeypatch))
    for sid in (launch, b1, b2, unrelated):
        _seed(project, sid)
    (project / "memory.md").write_text("another session's notes", encoding="utf-8")
    keep = {k: v for k, v in _snapshot(project).items()
            if k.parts[0] in (f"{unrelated}.jsonl", unrelated, "memory.md")}
    assert len(keep) == 3

    counts = await _reap(h)

    for sid in (launch, b1, b2):
        assert not (project / f"{sid}.jsonl").exists(), sid
        assert not (project / sid).exists(), sid
    assert _snapshot(project) == keep
    assert counts["deleted"] == 6 and counts["errors"] == 0


async def test_a_downgraded_fresh_job_keeps_its_pre_downgrade_sids_for_the_reaper(
        fresh, home, monkeypatch):
    """Design red case 8, second half: a #369 clearance downgrade drops the
    resume pointer, but the ledger keeps the pre-downgrade sids — and the
    rebuilt client's — so the reaper deletes all of them at terminal."""
    h = fresh
    launch, b1, rebuilt, b2, unrelated = _uuids(5)
    h.rec.origin["_origin_clearance"] = "private"
    h.client.sid = launch
    h.client.next_sid = {1: b1, 2: b2}.__getitem__
    h.client.scripts = [[text_frame("Starting the job"), result()],
                        [text_frame("Batch work"), result()]]
    await h.driver.start(h.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())
    await h.driver.send_user_turn(h.rec, jobs.batch_prompt(1, "Process rows"))
    assert await h.reg.lower_origin_clearance(h.rec.id, "public")
    await h.driver.invalidate_session(h.rec)
    await h.reg.clear_session_id(h.rec.id)
    assert h.rec.sdk_session_id is None
    assert ledger(h.rec.origin) == [launch, b1]
    monkeypatch.setattr(tools, "build_engagement_resume_options",
                        lambda rec, sid: ClaudeAgentOptions())
    await h.driver.open_fresh(h.rec)
    await h.reg.clear_context_rebuild_pending(h.rec.id)
    h.client.sid = rebuilt
    h.client.scripts = [[text_frame("ok"), result()]]
    await h.driver.send_user_turn(h.rec, jobs.batch_prompt(2, "Process rows"))
    assert ledger(h.rec.origin) == [launch, b1, rebuilt, b2]
    await h.reg.mark_cancelled(h.rec.id)

    project = _project(home, _worker_cwd(monkeypatch))
    for sid in (launch, b1, rebuilt, b2, unrelated):
        _seed(project, sid)
    keep = _snapshot(project)
    keep = {k: v for k, v in keep.items() if k.parts[0] in (f"{unrelated}.jsonl", unrelated)}

    await _reap(h)

    for sid in (launch, b1, rebuilt, b2):
        assert not (project / f"{sid}.jsonl").exists(), sid
    assert _snapshot(project) == keep


@pytest.mark.parametrize("harness", ["plugin"], indirect=True)
async def test_reaper_removes_a_fresh_plugin_jobs_own_project_dir(
        fresh, home, monkeypatch):
    """A resident-hosted (kind="plugin") fresh job runs in a per-engagement
    cwd: the reaper removes that whole project dir, and no other."""
    h = fresh
    assert h.rec.kind == "plugin"
    launch, b1, other = _uuids(3)
    h.client.sid = launch
    h.client.next_sid = {1: b1}.__getitem__
    h.client.scripts = [[h.complete, result()]]
    await h.start()
    await h.drain()
    assert h.rec.status == "completed"
    assert ledger(h.rec.origin) == [launch, b1]

    own = _project(home, str(tools.plugin_job_cwd(h.rec.id)))
    for sid in (launch, b1):
        _seed(own, sid)
    neighbour = _project(home, _worker_cwd(monkeypatch))
    _seed(neighbour, other)
    before = _snapshot(neighbour)

    await _reap(h)

    assert not own.exists()
    assert _snapshot(neighbour) == before
