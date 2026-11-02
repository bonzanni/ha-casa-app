"""M6 (INV-MEM-029): a lower ledger is generated only from inputs readable at
its clearance.

A ``private`` canary sits in an event, in the ``private`` ledger and in that
ledger's history; a provisional event (its classification fell back) carries
another. The real writer files both maintained ledgers, ``private`` first,
through a model port that records every prompt, while ``assemble_input`` is
spied on: every prompt the port is given must be one rendered from an input
that function built for the running job, so a second assembly path fails
these tests. No canary may appear in any ``friends`` prompt, object or
revision. Real store; no model is called.
"""
from __future__ import annotations

import json
import secrets
import time

import pytest

from memory import ledgers, patches, writer
from memory.model_port import ModelReply
from memory_serving_support import add, fact
from memory_writer_support import (
    add_each, new_store, pending_seqs, reply, rows, seed,
)

pytestmark = [pytest.mark.unit]

FRIENDS_MARK = "fm" + secrets.token_hex(4)
FALLBACK_MARK = "fb" + secrets.token_hex(4)


def canary() -> str:
    return secrets.token_hex(8)


@pytest.fixture
def friends(monkeypatch):
    """A ``friends`` reader is served a view: the ledger is maintained."""
    monkeypatch.setattr(ledgers, "_served", frozenset({"friends"}))
    assert ledgers.maintained() == ("private", "friends")


async def classify_by_text(text: str) -> str | None:
    """``friends`` for a text carrying the friends mark, a fallback for one
    carrying the fallback mark, ``private`` otherwise."""
    if FRIENDS_MARK in text:
        return "friends"
    if FALLBACK_MARK in text:
        return None
    return "private"


class Spy:
    """Records every input ``assemble_input`` builds and every prompt the
    model port is given, with the input of the job that was running."""

    def __init__(self, monkeypatch, script) -> None:
        self.inputs: list[writer.Input] = []
        self.calls: list[tuple[writer.Input, str]] = []
        self.script = script
        real = writer.assemble_input

        def assemble(*args, **kwargs):
            inp = real(*args, **kwargs)
            self.inputs.append(inp)
            return inp

        monkeypatch.setattr(writer, "assemble_input", assemble)

    async def complete(self, *, system: str, prompt: str) -> ModelReply:
        assert self.inputs, "the model was called before any input was built"
        inp = self.inputs[-1]
        self.calls.append((inp, prompt))
        text = self.script(inp, json.loads(prompt))
        return ModelReply(text=text, usage=None, cost_usd=None)

    def prompts(self, ledger: str) -> list[str]:
        return [p for inp, p in self.calls if inp.ledger == ledger]

    def assert_every_prompt_was_assembled(self) -> None:
        """Every prompt is a rendering of the running job's assembled input,
        with any entries it opened taken from that same input."""
        for inp, prompt in self.calls:
            got = json.loads(prompt)
            built = json.loads(writer.render_prompt(inp))
            opened = got.pop("opened", None)
            built.pop("opened", None)
            assert got == built, (
                f"a {inp.ledger} prompt was not built by assemble_input")
            own = [writer._object_view(o) for o in inp.objects]
            assert all(o in own for o in opened or []), (
                f"a {inp.ledger} prompt opened an object it was not given")


def by_ledger(inp: writer.Input, prompt: dict) -> str:
    return add_each(prompt)


def ledger_text(store, ledger: str) -> str:
    return json.dumps(
        rows(store, "SELECT * FROM objects WHERE ledger = ?", ledger)
        + rows(store, "SELECT * FROM revisions WHERE ledger = ?", ledger))


async def drain(w: writer.Writer, limit: int = 12) -> None:
    for _ in range(limit):
        if not await w.step():
            return
    raise AssertionError("the writer never ran out of work")


def new_writer(store, port) -> writer.Writer:
    # Everything is past its quiet period at once.
    return writer.Writer(store, port, classifier=classify_by_text,
                         boot_id="boot-m6", clock=lambda: time.time() + 1e5)


async def test_a_private_canary_never_reaches_a_lower_ledger(
        tmp_path, monkeypatch, friends):
    store = new_store(tmp_path)
    in_ledger, in_history = canary(), canary()
    in_event, provisional = canary(), canary()
    _, [safe] = add(store, [fact("safe", f"code {in_history}")])
    add(store, [{"op": "replace", "object_id": safe,
                 "body": f"code {in_ledger}"}])
    spy = Spy(monkeypatch, by_ledger)
    w = new_writer(store, spy)

    # Round 1: a friends-tier fact, filed in both ledgers.
    f0 = seed(store, f"the bins go out on Tuesday {FRIENDS_MARK}")
    await drain(w)
    # Round 2, same conversation: a private event before a friends one, and
    # an event whose classification falls back.
    seed(store, f"my bank PIN is {in_event}")
    f1 = seed(store, f"the plumber comes Friday {FRIENDS_MARK}")
    shy = seed(store, f"{provisional} {FALLBACK_MARK}")
    await drain(w)

    spy.assert_every_prompt_was_assembled()
    assert [inp.ledger for inp in spy.inputs] == [
        "private", "friends", "private", "friends"]
    friends_prompts = spy.prompts("friends")
    assert len(friends_prompts) == 2
    second = json.loads(friends_prompts[1])
    assert [e["seq"] for e in second["events"]] == [f1]
    assert [e["seq"] for e in second["context"]] == [f0]
    for secret in (in_ledger, in_history, in_event, provisional):
        assert not any(secret in p for p in friends_prompts), secret
        assert secret not in ledger_text(store, "friends"), secret
    # Every friends object and revision is a friends fact.
    assert "Tuesday" in ledger_text(store, "friends")
    assert "Friday" in ledger_text(store, "friends")
    # The provisional event was filed in private only, and is owed nowhere.
    assert pending_seqs(store, "friends") == []
    assert [r["ledger"] for r in rows(
        store, "SELECT ledger FROM dispositions WHERE seq = ?", shy)] == [
            "private"]
    # The private job did see what it may read.
    assert in_event in spy.prompts("private")[1]
    assert in_ledger in spy.prompts("private")[1]


async def test_in_index_mode_a_lower_job_opens_only_its_own_ledger(
        tmp_path, monkeypatch, friends):
    store = new_store(tmp_path)
    secret = canary()
    _, [private_id] = add(store, [fact("safe", f"code {secret}")])
    _, [friends_id] = add(store, [fact("bins", "Tuesday")], ledger="friends")
    seed(store, f"the bins move to Wednesday {FRIENDS_MARK}",
         tier="friends", tier_status="classified")
    monkeypatch.setattr(writer, "WHOLE_LEDGER_MAX_TOKENS", 0)

    def script(inp, prompt):
        if "opened" not in prompt:
            return json.dumps({"open": [private_id, friends_id]})
        return reply({e["seq"]: "no_change" for e in prompt["events"]})

    spy = Spy(monkeypatch, script)
    job = writer.enqueue(store, "friends", writer.ORDINARY)
    assert await new_writer(store, spy).run_job(job) == writer.COMMITTED
    spy.assert_every_prompt_was_assembled()
    first, second = spy.prompts("friends")
    assert private_id not in first and secret not in first
    opened = json.loads(second)["opened"]
    assert [o["object_id"] for o in opened] == [friends_id]
    assert secret not in second


async def test_a_lower_job_cannot_write_an_object_of_another_ledger(
        tmp_path, monkeypatch, friends):
    """A ``friends`` answer that names a ``private`` object fails the run,
    and nothing of either ledger changes."""
    store = new_store(tmp_path)
    _, [private_id] = add(store, [fact("safe", "code 1234")])
    seq = seed(store, f"the safe is open {FRIENDS_MARK}", tier="friends",
               tier_status="classified")
    before = ledger_text(store, "private")

    def script(inp, prompt):
        return reply({seq: "patched"}, [
            {"op": "replace", "object_id": private_id, "body": "open",
             "source_seqs": [seq]}])

    job = writer.enqueue(store, "friends", writer.ORDINARY)
    out = await new_writer(store, Spy(monkeypatch, script)).run_job(job)
    assert out == writer.RETRY
    assert ledger_text(store, "private") == before
    assert ledger_text(store, "friends") == "[]"
    assert pending_seqs(store, "friends") == [seq]
    with store.transaction() as conn:
        with pytest.raises(patches.PatchError):
            patches.apply(conn, "friends", [{"op": "retire",
                                             "object_id": private_id}],
                          job_id="j", commit_no=9, committed_at="t")


def test_the_private_job_runs_before_a_lower_one(tmp_path, friends):
    """Queue order does not decide: ``private`` first, after corrections."""
    store = new_store(tmp_path)
    lower = writer.enqueue(store, "friends", writer.ORDINARY)
    first = writer.enqueue(store, "private", writer.ORDINARY)
    w = new_writer(store, None)
    assert w.next_job()["job_id"] == first
    fix_lower = writer.enqueue(store, "friends", writer.CORRECTION)
    fix_first = writer.enqueue(store, "private", writer.CORRECTION)
    assert w.next_job()["job_id"] == fix_first
    with store.transaction(write=True) as conn:
        conn.execute("UPDATE jobs SET state = 'committed' WHERE job_id IN "
                     "(?, ?)", (fix_first, first))
    assert w.next_job()["job_id"] == fix_lower
    with store.transaction(write=True) as conn:
        conn.execute("UPDATE jobs SET state = 'committed' WHERE job_id = ?",
                     (fix_lower,))
    assert w.next_job()["job_id"] == lower


def test_a_lower_ledger_is_maintained_only_while_a_reader_is_served_it(
        monkeypatch):
    monkeypatch.setattr(ledgers, "_served", frozenset())
    assert ledgers.maintained() == ("private",)
    assert ledgers.configure([("private", 50_000), ("friends", 0)]) == (
        "private",)
    assert ledgers.configure([("private", 0), ("friends", 10_000)]) == (
        "private", "friends")
    # No reader is served below friends in Casa: family and public get no
    # ledger of their own.
    assert ledgers.configure([("family", 10_000), ("public", 10_000)]) == (
        "private",)


async def test_an_unclassified_event_reaches_no_lower_ledger_whatever_its_tier(
        tmp_path, monkeypatch, friends):
    """Only a classified event is eligible below ``private``: an event left
    provisional is never given to a lower job, even one stored at a lower
    tier."""
    store = new_store(tmp_path)
    secret = canary()
    shy = seed(store, f"{secret} {FRIENDS_MARK}", tier="friends",
               tier_status="provisional")
    seen = seed(store, f"the bins go out on Tuesday {FRIENDS_MARK}",
                tier="friends", tier_status="classified")
    assert pending_seqs(store, "friends") == [seen]
    spy = Spy(monkeypatch, by_ledger)
    job = writer.enqueue(store, "friends", writer.ORDINARY)
    assert await new_writer(store, spy).run_job(job) == writer.COMMITTED
    [prompt] = spy.prompts("friends")
    assert secret not in prompt and secret not in ledger_text(
        store, "friends")
    assert [r["seq"] for r in rows(
        store, "SELECT seq FROM dispositions WHERE ledger = 'friends'")] == [
            seen]
    assert shy in pending_seqs(store, "private")


def _sources(store, ledger: str, object_id: str) -> tuple[list, list]:
    """An object's current sources, and those of each of its revisions."""
    [obj] = rows(store, "SELECT source_seqs FROM objects WHERE ledger = ? "
                        "AND object_id = ?", ledger, object_id)
    revs = rows(store, "SELECT source_seqs FROM revisions WHERE ledger = ? "
                       "AND object_id = ? ORDER BY revision", ledger,
                object_id)
    return (json.loads(obj["source_seqs"]),
            [json.loads(r["source_seqs"]) for r in revs])


async def _file_friends(store, monkeypatch, ops_for) -> str:
    """Run one friends job whose answer is ``ops_for(input seqs)``."""
    def script(inp, prompt):
        seqs = [e["seq"] for e in prompt["events"]]
        return reply({s: "patched" for s in seqs}, ops_for(seqs))

    job = writer.enqueue(store, "friends", writer.ORDINARY)
    return await new_writer(store, Spy(monkeypatch, script)).run_job(job)


async def test_a_lower_ledger_never_records_a_private_event_as_a_source(
        tmp_path, monkeypatch, friends):
    """A friends answer that cites an event it was never given (a private
    one) still files, and the private source is dropped from the object and
    its revision."""
    store = new_store(tmp_path)
    private = seed(store, f"my PIN is {canary()}", tier="private",
                   tier_status="classified")
    shared = seed(store, f"bins on Tuesday {FRIENDS_MARK}", tier="friends",
                  tier_status="classified")
    out = await _file_friends(store, monkeypatch, lambda seqs: [
        {"op": "add", "kind": "entry", "entry_kind": "fact",
         "subject": "bins", "body": "Tuesday", "line": "bin day",
         "source_seqs": [private]}])
    assert out == writer.COMMITTED
    [oid] = [r["object_id"] for r in rows(
        store, "SELECT object_id FROM objects WHERE ledger = 'friends'")]
    assert _sources(store, "friends", oid) == ([], [[]])
    assert pending_seqs(store, "friends") == []
    assert shared not in pending_seqs(store, "friends")


async def test_only_the_readable_sources_of_a_lower_ledger_patch_are_kept(
        tmp_path, monkeypatch, friends, caplog):
    store = new_store(tmp_path)
    private = seed(store, f"my PIN is {canary()}", tier="private",
                   tier_status="classified")
    shared = seed(store, f"bins on Tuesday {FRIENDS_MARK}", tier="friends",
                  tier_status="classified")
    shy = seed(store, f"{canary()} {FRIENDS_MARK}", tier="friends",
               tier_status="provisional")
    _, [oid] = add(store, [fact("bins", "Monday")], ledger="friends")
    future = shy + 1000   # a seq a later private event could take
    caplog.set_level("WARNING", logger="memory.patches")
    out = await _file_friends(store, monkeypatch, lambda seqs: [
        {"op": "add", "kind": "entry", "entry_kind": "fact",
         "subject": "plumber", "body": "Friday", "line": "visits",
         "source_seqs": [private, shared, future, "x"]},
        {"op": "replace", "object_id": oid, "body": "Tuesday",
         "source_seqs": [shy, shared, private]}])
    assert out == writer.COMMITTED
    new = [r["object_id"] for r in rows(
        store, "SELECT object_id FROM objects WHERE ledger = 'friends' "
               "AND object_id != ?", oid)]
    assert _sources(store, "friends", new[0]) == ([shared], [[shared]])
    assert _sources(store, "friends", oid) == ([shared], [[], [shared]])
    lines = [r.getMessage() for r in caplog.records
             if r.name == "memory.patches"]
    assert len(lines) == 1 and "5 source ref(s)" in lines[0] \
        and "2 object(s)" in lines[0], lines


async def test_the_private_ledger_keeps_every_source_it_cites(
        tmp_path, monkeypatch, friends):
    store = new_store(tmp_path)
    private = seed(store, "my PIN is 1234")
    shared = seed(store, f"bins on Tuesday {FRIENDS_MARK}", tier="friends",
                  tier_status="classified")
    shy = seed(store, "left provisional")

    def script(inp, prompt):
        return reply({e["seq"]: "patched" for e in prompt["events"]}, [
            {"op": "add", "kind": "entry", "entry_kind": "fact",
             "subject": "s", "body": "b", "line": "l",
             "source_seqs": [private, shared, shy, shy + 1000]}])

    job = writer.enqueue(store, "private", writer.ORDINARY)
    out = await new_writer(store, Spy(monkeypatch, script)).run_job(job)
    assert out == writer.COMMITTED
    [oid] = [r["object_id"] for r in rows(
        store, "SELECT object_id FROM objects WHERE ledger = 'private'")]
    cited = [private, shared, shy, shy + 1000]
    assert _sources(store, "private", oid) == (cited, [cited])
