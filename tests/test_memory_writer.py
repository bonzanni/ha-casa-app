"""The writer's ordinary path: batches through commit, the cadence, the
worker loop and the model port.

A scripted model port drives three batches through commit with exact
dispositions; the cadence queues a job only after quiet or the maximum
wait, at a session's end, and never again for events the writer was already
shown; a correction job runs first. No model is called.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import types
from datetime import datetime, timezone

from memory import corrections, model_port, writer
from memory_writer_support import (
    ScriptedPort, add_each, dispositions, job_rows, new_store, new_writer,
    no_change_all, pending_seqs, reply, rows, run_new, seed,
)


async def test_three_batches_commit_with_exact_dispositions(tmp_path):
    """Batch 1 adds two facts; batch 2 files one as ``no_change`` and defers
    another; batch 3 files the deferred one with a ``replace`` together with
    a new event. Every commit writes exactly one disposition per input event
    under one ``commit_no``."""
    store = new_store(tmp_path)
    e1, e2 = seed(store, "Anna's birthday is 3 May"), seed(store, "I like tea")
    w = new_writer(store, ScriptedPort(add_each))
    assert (await run_new(w))[1] == writer.COMMITTED
    objects = {o["body"]: o["object_id"] for o in rows(
        store, "SELECT * FROM objects")}

    e3, e4 = seed(store, "ok"), seed(store, "actually not tea")
    w.port = ScriptedPort(lambda p: reply({e3: "no_change", e4: "deferred"}))
    assert (await run_new(w))[1] == writer.COMMITTED

    e5 = seed(store, "coffee now")
    w.port = ScriptedPort(lambda p: reply(
        {e4: "patched", e5: "patched"},
        [{"op": "replace", "object_id": objects["I like tea"],
          "body": "coffee (earlier: tea, until today)",
          "source_seqs": [e4, e5]}]))
    assert (await run_new(w))[1] == writer.COMMITTED
    assert [e["seq"] for e in w.port.prompts[0]["events"]] == [e4, e5]

    assert dispositions(store) == [
        (e1, "private", "patched", 1), (e2, "private", "patched", 1),
        (e3, "private", "no_change", 2), (e4, "private", "deferred", 2),
        (e4, "private", "patched", 3), (e5, "private", "patched", 3)]
    assert pending_seqs(store) == []
    tea = rows(store, "SELECT * FROM objects WHERE object_id = ?",
               objects["I like tea"])[0]
    assert (tea["revision"], tea["body"]) == (
        2, "coffee (earlier: tea, until today)")
    assert json.loads(tea["source_seqs"]) == [e2, e4, e5]
    assert [r["commit_no"] for r in rows(
        store, "SELECT commit_no FROM revisions ORDER BY commit_no")] == [
            1, 1, 3]
    assert [j["state"] for j in job_rows(store)] == ["committed"] * 3


async def test_the_prompt_carries_the_events_the_ledger_and_context(tmp_path):
    store = new_store(tmp_path)
    old = seed(store, "earlier words", session="s9")
    await run_new(new_writer(store, ScriptedPort(add_each)))
    new = seed(store, "later words", session="s9")
    port = ScriptedPort(no_change_all)
    await run_new(new_writer(store, port))
    [prompt] = port.prompts
    assert prompt["job"] == "ordinary"
    assert [e["seq"] for e in prompt["events"]] == [new]
    assert [e["seq"] for e in prompt["context"]] == [old]
    assert [o["body"] for o in prompt["ledger"]] == ["earlier words"]
    assert "Keep everything the operator states" in port.systems[0]


async def test_past_the_whole_ledger_limit_a_run_opens_what_it_needs(
        tmp_path, monkeypatch):
    """A ledger over :data:`writer.WHOLE_LEDGER_MAX_TOKENS` is given as
    its index, bodies left out; the run's first answer names what to open
    (at most :data:`writer.MAX_OPENED`), and a second call gives those in
    full from the same snapshot before the run answers."""
    store = new_store(tmp_path)
    for text in ("FIRST-BODY", "SECOND-BODY", "THIRD-BODY"):
        seed(store, text)
    await run_new(new_writer(store, ScriptedPort(add_each)))
    ids = [r["object_id"] for r in rows(
        store, "SELECT object_id FROM objects ORDER BY rowid")]
    monkeypatch.setattr(writer, "WHOLE_LEDGER_MAX_TOKENS", 1)
    monkeypatch.setattr(writer, "MAX_OPENED", 2)
    e = seed(store, "the third one changed")

    def opened(prompt):
        assert [o["body"] for o in prompt["opened"]] == [
            "FIRST-BODY", "SECOND-BODY"]
        return reply({e: "patched"}, [{"op": "replace", "object_id": ids[1],
                                       "body": "NEW-BODY",
                                       "source_seqs": [e]}])

    port = ScriptedPort([json.dumps({"open": [ids[0], "unknown", ids[1],
                                              ids[2]]}), opened])
    _, outcome = await run_new(new_writer(store, port))
    assert outcome == writer.COMMITTED
    first, second = port.prompts
    assert first["ledger_mode"] == "index" and "opened" not in first
    assert all("body" not in o for o in first["ledger"])
    assert len(first["ledger"]) == 3 and first["events"] == second["events"]
    assert dispositions(store, e) == [(e, "private", "patched", 2)]
    assert rows(store, "SELECT body FROM objects WHERE object_id = ?",
                ids[1])[0]["body"] == "NEW-BODY"


async def test_an_index_run_may_answer_at_once(tmp_path, monkeypatch):
    store = new_store(tmp_path)
    seed(store, "kept")
    await run_new(new_writer(store, ScriptedPort(add_each)))
    monkeypatch.setattr(writer, "WHOLE_LEDGER_MAX_TOKENS", 1)
    seed(store, "nothing new")
    port = ScriptedPort([no_change_all])
    assert (await run_new(new_writer(store, port)))[1] == writer.COMMITTED
    assert len(port.prompts) == 1


async def test_an_empty_run_commits_with_no_disposition(tmp_path):
    store = new_store(tmp_path)
    port = ScriptedPort([])
    _, outcome = await run_new(new_writer(store, port))
    assert outcome == writer.COMMITTED and port.prompts == []
    assert dispositions(store) == []


# --- cadence -----------------------------------------------------------------

def _at(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


async def test_cadence_waits_for_quiet_or_the_maximum_wait(tmp_path):
    store = new_store(tmp_path)
    e = seed(store, "hello")
    stored = rows(store, "SELECT recorded_at FROM events WHERE seq = ?",
                  e)[0]["recorded_at"]
    clock = {"now": _at(stored) + 60}
    w = new_writer(store, ScriptedPort(no_change_all),
                   clock=lambda: clock["now"])
    assert w.schedule_due() == []
    clock["now"] = _at(stored) + writer.QUIET_S
    [job] = w.schedule_due()
    assert w.schedule_due() == []           # one queued job, not two
    assert await w.run_job(job) == writer.COMMITTED

    # A deferred event the writer was already shown never re-queues itself.
    f = seed(store, "wait for it")
    w.port = ScriptedPort(lambda p: reply({f: "deferred"}))
    clock["now"] = _at(rows(store, "SELECT recorded_at FROM events WHERE "
                                   "seq = ?", f)[0]["recorded_at"]) + 4000
    [job] = w.schedule_due()
    await w.run_job(job)
    assert pending_seqs(store) == [f]
    clock["now"] += 10 * writer.MAX_WAIT_S
    assert w.schedule_due() == []
    # ... until a session ends.
    assert len(w.schedule_due(session_end=True)) == 1


async def test_maximum_wait_beats_a_conversation_that_never_pauses(tmp_path):
    store = new_store(tmp_path)
    first = seed(store, "one")
    t0 = _at(rows(store, "SELECT recorded_at FROM events WHERE seq = ?",
                  first)[0]["recorded_at"])
    w = new_writer(store, ScriptedPort(no_change_all), clock=lambda: t0 + 3700)
    # A newer event keeps the conversation from going quiet ...
    conn = sqlite3.connect(store.path)
    seed(store, "two")
    conn.execute("UPDATE events SET recorded_at = ? WHERE seq != ?", (
        datetime.fromtimestamp(t0 + 3650, timezone.utc).isoformat(
            timespec="microseconds"), first))
    conn.commit()
    conn.close()
    # ... but the oldest has waited past the maximum.
    assert len(w.schedule_due()) == 1


async def test_a_correction_job_runs_first(tmp_path):
    store = new_store(tmp_path)
    a = seed(store, "one")
    b = seed(store, "two")
    ordinary = writer.enqueue(store, "private", writer.ORDINARY)
    corrections.mark(store, b)
    w = new_writer(store, ScriptedPort(no_change_all))
    job = w.next_job()
    assert job["kind"] == "correction" and job["job_id"] != ordinary
    await w.run_job(job["job_id"])
    assert pending_seqs(store) == []
    assert {d[0] for d in dispositions(store)} == {a, b}


async def test_the_worker_runs_what_a_wake_queues(tmp_path):
    """The background loop recovers, then runs a job as soon as it is
    woken, without waiting for its tick."""
    store = new_store(tmp_path)
    seed(store, "hello")
    writer.enqueue(store, "private", writer.ORDINARY)
    with store.transaction(write=True) as conn:
        conn.execute("UPDATE jobs SET state = 'running', boot_id = 'old'")
    w = new_writer(store, ScriptedPort(no_change_all), boot_id="boot-now")
    task = asyncio.get_running_loop().create_task(w.run_forever())
    try:
        for _ in range(500):
            if pending_seqs(store) == []:
                break
            await asyncio.sleep(0.01)
        assert pending_seqs(store) == []
        seed(store, "another")
        w.session_ended()
        for _ in range(500):
            if pending_seqs(store) == []:
                break
            await asyncio.sleep(0.01)
        assert pending_seqs(store) == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert [j["state"] for j in job_rows(store)] == ["committed"] * 2


async def test_the_module_writer_is_a_no_op_until_configured(monkeypatch):
    monkeypatch.setitem(writer._wiring, "writer", None)
    writer.wake()
    writer.session_ended()
    assert writer.start() is None


# --- the model port ----------------------------------------------------------

def _fake_sdk(monkeypatch, messages, launches):
    import claude_agent_sdk
    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = claude_agent_sdk.ClaudeAgentOptions
    fake.AssistantMessage = claude_agent_sdk.AssistantMessage
    fake.ResultMessage = claude_agent_sdk.ResultMessage

    async def query(*, prompt, options):
        launches.append(options)
        for m in messages:
            yield m

    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


def _result(is_error=False):
    from claude_agent_sdk import ResultMessage
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=is_error, num_turns=1, session_id="s",
                         total_cost_usd=0.25,
                         usage={"input_tokens": 10, "output_tokens": 2})


async def test_the_sdk_port_joins_the_reply_and_reports_cost(monkeypatch):
    from claude_agent_sdk import AssistantMessage, TextBlock
    launches: list = []
    _fake_sdk(monkeypatch, [
        AssistantMessage(content=[TextBlock(text='{"ops": '),
                                  TextBlock(text="[]}")], model="m"),
        _result()], launches)
    got = await model_port.SdkModelPort().complete(system="rules",
                                                   prompt="{}")
    assert got == model_port.ModelReply(
        text='{"ops": []}', usage={"input_tokens": 10, "output_tokens": 2},
        cost_usd=0.25)
    [opts] = launches
    assert (opts.model, opts.effort, opts.system_prompt) == (
        "claude-opus-5-5", "medium", "rules")
    assert opts.tools == [] and opts.allowed_tools == []


async def test_the_sdk_port_raises_on_an_error_result(monkeypatch):
    _fake_sdk(monkeypatch, [_result(is_error=True)], [])
    try:
        await model_port.SdkModelPort().complete(system="s", prompt="p")
    except RuntimeError as exc:
        assert "error" in str(exc)
    else:
        raise AssertionError("an error result must raise")


async def test_the_sdk_port_times_out(monkeypatch):
    class Slow(model_port.SdkModelPort):
        async def _query(self, system, prompt):
            await asyncio.Event().wait()

    try:
        await Slow(timeout_s=0.01).complete(system="s", prompt="p")
    except asyncio.TimeoutError:
        pass
    else:
        raise AssertionError("the call must time out")


def _said(text, session="s1"):
    from memory import capture
    return capture.event(role="assistant", channel="telegram",
                         speaker_kind="operator", origin_kind="operator",
                         text=text, eligibility="write_trusted_turn",
                         origin_tier=None, session_id=session,
                         tier="private", tier_status="provisional")


def _store_now(store, item):
    from memory import capture
    assert capture.append(store, item).outcome == capture.STORED


async def test_the_writer_reads_events_in_the_order_they_were_said(tmp_path):
    """A capture that waited in the spool is stored after one said later;
    the writer's input follows when each was said, not when it was
    stored."""
    store = new_store(tmp_path)
    first, second = _said("question first"), _said("answer second")
    for item in (second, first):
        _store_now(store, item)
    port = ScriptedPort(no_change_all)
    await run_new(new_writer(store, port))
    assert [e["text"] for e in port.prompts[0]["events"]] == [
        "question first", "answer second"]


async def test_a_late_stored_input_is_not_given_later_words_as_context(
        tmp_path):
    """Said at 09:00 but stored after a 10:00 event that is already filed:
    the 10:00 event was said after the input, so it is not context."""
    store = new_store(tmp_path)
    early, later = _said("said at nine"), _said("said at ten")
    _store_now(store, later)
    await run_new(new_writer(store, ScriptedPort(add_each)))
    _store_now(store, early)
    port = ScriptedPort(no_change_all)
    await run_new(new_writer(store, port))
    [prompt] = port.prompts
    assert [e["text"] for e in prompt["events"]] == ["said at nine"]
    assert [e["text"] for e in prompt["context"]] == []


async def test_a_late_stored_input_keeps_the_context_said_before_it(
        tmp_path):
    """The session's earliest input by time is stored after 25 later
    inputs; the question filed before all of them is still its context."""
    store = new_store(tmp_path)
    _store_now(store, _said("which room should be 19 degrees?"))
    await run_new(new_writer(store, ScriptedPort(add_each)))
    reply_item = _said("the nursery")
    later = [_said(f"later {n}") for n in range(25)]
    for item in later:
        _store_now(store, item)
    _store_now(store, reply_item)
    port = ScriptedPort(no_change_all)
    await run_new(new_writer(store, port))
    [prompt] = port.prompts
    assert [e["text"] for e in prompt["events"]][0] == "the nursery"
    assert len(prompt["events"]) == 26
    assert [e["text"] for e in prompt["context"]] == [
        "which room should be 19 degrees?"]


async def test_a_deferred_input_keeps_a_question_stored_after_it(tmp_path):
    """The question waited in the spool and is stored after its answer; the
    question is filed, the answer deferred. On the next batch the answer
    still gets the question as context, though the question's seq is
    higher."""
    store = new_store(tmp_path)
    question, answer = _said("which room?"), _said("the nursery")
    _store_now(store, answer)
    _store_now(store, question)
    q_seq, a_seq = (rows(store, "SELECT seq FROM events WHERE text = ?", t)[0]
                    ["seq"] for t in ("which room?", "the nursery"))
    w = new_writer(store, ScriptedPort(
        lambda p: reply({q_seq: "no_change", a_seq: "deferred"})))
    await run_new(w)
    _store_now(store, _said("elsewhere", session="s2"))
    port = ScriptedPort(no_change_all)
    await run_new(new_writer(store, port))
    [prompt] = port.prompts
    assert sorted(e["text"] for e in prompt["events"]) == [
        "elsewhere", "the nursery"]
    assert [e["text"] for e in prompt["context"]] == ["which room?"]
