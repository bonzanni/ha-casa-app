"""A value updated above a lower ledger's clearance leaves that ledger
(INV-MEM-039).

Each event is classified alone, so an update can land in a higher tier than
the fact it replaces: "my locker is ZQXL5" at ``friends``, then "the locker is
ZQXL7 now" at ``family``. The ``friends`` job never sees the update, so without
a withdrawal it keeps ZQXL5 as current and the butler serves it. The
``private`` job sees both and withdraws the stale fact from the lower ledger:
an entry it already holds is retired there, an event still owed to it is
closed there. Nothing of the update is written below. Which fact is stale is
the model's judgement; these tests script it. Real store and writer; no model
is called.
"""
from __future__ import annotations

import json
import logging
import shutil
import time

import pytest

import memory_restore_support as r
import memory_wipe_support as w
from memory import corrections, export, ledgers, view, writer
from memory.model_port import ModelReply
from memory_serving_support import add, fact
from memory_writer_support import (
    new_store, pending_seqs, reply, rows, seed,
)

pytestmark = [pytest.mark.unit]

OLD, NEW = "ZQXL5", "ZQXL7"
OLD_TEXT = f"At the pool, my locker number is {OLD}."
NEW_TEXT = f"The pool locker number is {NEW} now."


@pytest.fixture
def friends(monkeypatch):
    """The butler is served a view: ``friends`` is maintained."""
    monkeypatch.setattr(ledgers, "_served", frozenset({"friends"}))
    assert ledgers.maintained() == ("private", "friends")


async def by_text(text: str) -> str:
    """The live case: the fact at ``friends``, its update at ``family``."""
    if NEW in text:
        return "family"
    if "guests" in text or OLD in text:
        return "friends"
    return "private"


class Model:
    """A scripted writer model. Every job files each of its events as one
    fact. The ``private`` job, when it is given the lower ledgers, also
    withdraws what an event above them replaced: here, anything naming the
    old locker number once an event naming the new one is filed."""

    def __init__(self, extra=None, *, withdraws: bool = True) -> None:
        self.prompts: list[tuple[str, dict]] = []
        self.extra = extra
        self.withdraws = withdraws

    async def complete(self, *, system: str, prompt: str) -> ModelReply:
        body = json.loads(prompt)
        ledger = "private" if "lower" in body or self._private(body) else "?"
        self.prompts.append((ledger, body))
        events = body.get("events", [])
        ops = [{"op": "add", "kind": "entry", "entry_kind": "fact",
                "subject": "locker" if "locker" in e["text"] else "note",
                "body": e["text"], "line": "a fact",
                "source_seqs": [e["seq"]]} for e in events]
        replaced = self.withdraws and any(NEW in e["text"] for e in events)
        for lower in body.get("lower", []) if replaced else []:
            for entry in lower["entries"]:
                if OLD in entry.get("body", ""):
                    ops.append({"op": "withdraw", "ledger": lower["ledger"],
                                "object_id": entry["object_id"]})
            for e in lower["pending"]:
                if OLD in e["text"]:
                    ops.append({"op": "withdraw", "ledger": lower["ledger"],
                                "seq": e["seq"]})
            for e in events:
                if OLD in e["text"] and e.get("tier") == lower["ledger"]:
                    ops.append({"op": "withdraw", "ledger": lower["ledger"],
                                "seq": e["seq"]})
        if self.extra is not None:
            ops += self.extra(body)
        return ModelReply(text=reply({e["seq"]: "patched" for e in events},
                                     ops), usage=None, cost_usd=None)

    @staticmethod
    def _private(body: dict) -> bool:
        return any("tier" in e for e in body.get("events", []))


def new_writer(store, port) -> writer.Writer:
    # Everything is past its quiet period at once.
    return writer.Writer(store, port, classifier=by_text, boot_id="boot-d1",
                         clock=lambda: time.time() + 1e5)


async def drain(w_: writer.Writer, limit: int = 12) -> None:
    for _ in range(limit):
        if not await w_.step():
            return
    raise AssertionError("the writer never ran out of work")


def current(store, ledger: str) -> list[str]:
    return [o["body"] for o in rows(
        store, "SELECT body FROM objects WHERE ledger = ? AND status = "
        "'current' AND kind = 'entry'", ledger)]


def friends_text(store) -> str:
    return json.dumps(
        rows(store, "SELECT * FROM objects WHERE ledger = 'friends'")
        + rows(store, "SELECT * FROM revisions WHERE ledger = 'friends'")
        + rows(store, "SELECT * FROM dispositions WHERE ledger = 'friends'"))


def served(store, ledger: str = "friends") -> str:
    with store.transaction() as conn:
        return view.render(conn, ledger, 10_000).text


def job_of(store, ledger: str) -> list[str]:
    return [j["job_id"] for j in rows(
        store, "SELECT job_id FROM jobs WHERE ledger = ? AND state = "
        "'committed' ORDER BY rowid", ledger)]


# --- the three timings ---------------------------------------------------------

async def test_an_update_above_the_lower_ledger_in_the_same_batch(
        tmp_path, friends):
    """The live case: both said in one conversation, filed in one
    ``private`` job before the ``friends`` job runs."""
    store = new_store(tmp_path)
    old = seed(store, OLD_TEXT)
    seed(store, NEW_TEXT)
    await drain(new_writer(store, Model()))

    assert any(NEW in b for b in current(store, "private"))
    assert not any(OLD in b for b in current(store, "friends"))
    assert OLD not in served(store)
    # The stale event was closed in friends by the private job, and is owed
    # nowhere.
    [private_job] = job_of(store, "private")
    assert rows(store, "SELECT job_id, outcome FROM dispositions WHERE "
                       "seq = ? AND ledger = 'friends'", old) == [
        {"job_id": private_job, "outcome": "no_change"}]
    assert pending_seqs(store, "friends") == []
    assert NEW not in friends_text(store)


async def test_an_update_above_the_lower_ledger_after_it_filed_the_fact(
        tmp_path, friends):
    store = new_store(tmp_path)
    seed(store, OLD_TEXT)
    model = Model()
    w_ = new_writer(store, model)
    await drain(w_)
    [entry] = rows(store, "SELECT * FROM objects WHERE ledger = 'friends'")
    assert OLD in entry["body"] and OLD in served(store)

    seed(store, NEW_TEXT)
    await drain(w_)
    assert not any(OLD in b for b in current(store, "friends"))
    assert OLD not in served(store)
    # A revision of the entry's own state, retired, with nothing of the job's.
    revs = rows(store, "SELECT * FROM revisions WHERE ledger = 'friends' "
                       "AND object_id = ? ORDER BY revision",
                entry["object_id"])
    assert [(x["op"], x["status"]) for x in revs] == [
        ("add", "current"), ("withdraw", "retired")]
    assert revs[1]["job_id"] == job_of(store, "private")[-1]
    assert revs[1]["reason"] is None
    for col in ("body", "subject", "line", "cues", "source_seqs", "topic"):
        assert revs[1][col] == revs[0][col], col
    assert NEW not in friends_text(store)
    # open_memory at friends reports it retired.
    with store.transaction() as conn:
        assert not view.unreachable(conn, "friends")


async def test_an_update_above_the_lower_ledger_while_the_fact_is_still_owed(
        tmp_path, friends):
    """The fact was classified by an earlier ``private`` job; its
    ``friends`` job has not run when the update's ``private`` job does."""
    store = new_store(tmp_path)
    old = seed(store, OLD_TEXT)
    model = Model()
    w_ = new_writer(store, model)
    job = writer.enqueue(store, "private", writer.ORDINARY)
    assert await w_.run_job(job) == writer.COMMITTED
    assert pending_seqs(store, "friends") == [old]

    seed(store, NEW_TEXT)
    job = writer.enqueue(store, "private", writer.ORDINARY)
    assert await w_.run_job(job) == writer.COMMITTED
    lower = model.prompts[-1][1]["lower"]
    assert [e["seq"] for e in lower[0]["pending"]] == [old]
    assert pending_seqs(store, "friends") == []
    await drain(w_)
    assert not any(OLD in b for b in current(store, "friends"))


# --- nothing from above is written below ----------------------------------------

async def test_a_withdrawal_writes_nothing_above_the_lower_ledger(
        tmp_path, friends):
    store = new_store(tmp_path)
    seed(store, OLD_TEXT)
    secret = "CANARY" + NEW

    def stuffed(body):
        ops = []
        for lower in body.get("lower", []):
            new = [e["seq"] for e in body["events"] if NEW in e["text"]]
            for entry in lower["entries"]:
                ops.append({"op": "withdraw", "ledger": "friends",
                            "object_id": entry["object_id"], "body": secret,
                            "subject": secret, "line": secret,
                            "reason": secret, "cues": [secret],
                            "topic": secret, "source_seqs": new,
                            "status": "current", "revision": 99})
        return ops

    # The stuffed withdrawal is the only one, so its payload is what the
    # commit reads.
    w_ = new_writer(store, Model(stuffed, withdraws=False))
    await drain(w_)
    seed(store, NEW_TEXT + " " + secret)
    await drain(w_)
    assert rows(store, "SELECT count(*) AS n FROM revisions WHERE ledger = "
                       "'friends' AND op = 'withdraw'") == [{"n": 1}]
    assert not any(OLD in b for b in current(store, "friends"))
    text = friends_text(store)
    assert secret not in text and NEW not in text
    family = [e["seq"] for e in rows(
        store, "SELECT seq FROM events WHERE tier IN ('family', 'private')")]
    for rev in rows(store, "SELECT source_seqs FROM revisions "
                           "WHERE ledger = 'friends'"):
        assert not set(json.loads(rev["source_seqs"])) & set(family)
    assert not rows(store, "SELECT 1 FROM dispositions WHERE ledger = "
                           "'friends' AND seq IN (%s)" % ",".join(
                               str(s) for s in family))


# --- what code checks, and what it does not -----------------------------------

async def test_a_bad_withdrawal_is_dropped_never_fatal(
        tmp_path, friends, caplog):
    store = new_store(tmp_path)
    _, [private_id] = add(store, [fact("safe", "code 1234")])
    _, [group] = add(store, [{"op": "add", "kind": "group",
                              "line": "the house"}], ledger="friends")
    _, [kept] = add(store, [fact("bins", "Tuesday")], ledger="friends")
    own = seed(store, "the boiler is serviced in May")
    left_out = seed(store, "the car is at the garage")
    stranger = seed(store, "the plumber said hello", tier="family",
                    tier_status="classified")

    def bad(body):
        return [
            {"op": "withdraw", "ledger": "friends", "object_id": private_id},
            {"op": "withdraw", "ledger": "friends", "object_id": group},
            {"op": "withdraw", "ledger": "friends", "object_id": "nope"},
            {"op": "withdraw", "ledger": "private", "object_id": private_id},
            {"op": "withdraw", "ledger": "family", "seq": own},
            {"op": "withdraw", "ledger": "friends", "seq": own},
            {"op": "withdraw", "ledger": "friends", "seq": stranger},
            {"op": "withdraw", "ledger": "friends", "seq": "x"},
            {"op": "withdraw", "ledger": "friends"},
            {"op": "withdraw", "ledger": [], "object_id": kept},
            {"op": "withdraw", "ledger": {}, "seq": own},
            {"op": "withdraw", "ledger": ["friends"], "object_id": kept},
            {"op": "withdraw", "ledger": "friends", "object_id": ["x"]},
            {"op": "withdraw", "ledger": "friends", "seq": [own]},
            {"op": "withdraw", "ledger": "friends", "object_id": kept,
             "source_seqs": [{}]},
            {"op": "withdraw", "ledger": "friends", "seq": left_out,
             "source_seqs": [left_out]},
        ]

    model = Model(bad)

    async def without_left_out(*, system, prompt):
        out = await Model.complete(model, system=system, prompt=prompt)
        data = json.loads(out.text)
        data["ops"] = [o for o in data["ops"] if not (
            isinstance(o, dict) and o.get("op") == "add"
            and o["source_seqs"] == [left_out])]
        data["events"].pop(str(left_out), None)
        return ModelReply(text=json.dumps(data), usage=None, cost_usd=None)

    model_port = type("P", (), {"complete": staticmethod(without_left_out)})
    job = writer.enqueue(store, "private", writer.ORDINARY)
    with caplog.at_level(logging.WARNING, logger="memory.writer"):
        out = await new_writer(store, model_port).run_job(job)
    assert out == writer.COMMITTED
    # Only the well-formed withdrawal of a shown entry applied.
    assert rows(store, "SELECT object_id FROM objects WHERE ledger = "
                       "'friends' AND status = 'retired'") == [
        {"object_id": kept}]
    assert rows(store, "SELECT status FROM objects WHERE object_id = ?",
                private_id) == [{"status": "current"}]
    # No seq withdrawal applied: none named an event owed to friends, and an
    # event above friends (``stranger``) gets no friends row at all.
    assert rows(store, "SELECT seq FROM dispositions "
                       "WHERE ledger = 'friends'") == []
    # A withdrawal's fields never decide an own event's outcome.
    assert left_out in pending_seqs(store, "private")
    assert any("withdrawal" in m for m in caplog.messages)


async def test_a_lower_job_cannot_withdraw(tmp_path, friends):
    store = new_store(tmp_path)
    _, [entry] = add(store, [fact("bins", "Tuesday")], ledger="friends")
    seq = seed(store, "the guests park in bay 12", tier="friends",
               tier_status="classified")

    def script(body):
        return [{"op": "withdraw", "ledger": "friends", "object_id": entry}]

    port = Model(script)
    job = writer.enqueue(store, "friends", writer.ORDINARY)
    assert await new_writer(store, port).run_job(job) == writer.COMMITTED
    assert "lower" not in port.prompts[0][1]
    assert rows(store, "SELECT status FROM objects WHERE object_id = ?",
                entry) == [{"status": "current"}]
    assert pending_seqs(store, "friends") == []
    assert seq not in pending_seqs(store, "friends")


async def test_a_target_changed_since_the_read_makes_the_run_stale(
        tmp_path, friends, monkeypatch):
    store = new_store(tmp_path)
    seed(store, OLD_TEXT)
    w_ = new_writer(store, Model())
    await drain(w_)
    [entry] = rows(store, "SELECT object_id FROM objects WHERE ledger = "
                          "'friends'")
    seed(store, NEW_TEXT)

    def another_writer():
        add(store, [{"op": "add_cue", "object_id": entry["object_id"],
                     "cues": ["pool"]}], ledger="friends")
        monkeypatch.setitem(writer._hooks, "after_model", lambda: None)

    monkeypatch.setitem(writer._hooks, "after_model", another_writer)
    job = writer.enqueue(store, "private", writer.ORDINARY)
    assert await w_.run_job(job) == writer.REQUEUED
    assert any(OLD in b for b in current(store, "friends"))
    # The rerun reads the new revision and withdraws it.
    assert await w_.run_job(job) == writer.COMMITTED
    assert not any(OLD in b for b in current(store, "friends"))


async def test_a_mark_on_a_lower_target_after_the_read_makes_the_run_stale(
        tmp_path, friends, monkeypatch):
    store = new_store(tmp_path)
    old = seed(store, OLD_TEXT)
    w_ = new_writer(store, Model())
    job = writer.enqueue(store, "private", writer.ORDINARY)
    assert await w_.run_job(job) == writer.COMMITTED
    seed(store, NEW_TEXT)

    def marked():
        assert corrections.mark(store, old).marked
        monkeypatch.setitem(writer._hooks, "after_model", lambda: None)

    monkeypatch.setitem(writer._hooks, "after_model", marked)
    job = writer.enqueue(store, "private", writer.ORDINARY)
    assert await w_.run_job(job) == writer.REQUEUED
    assert old in pending_seqs(store, "friends")


async def test_without_a_lower_ledger_the_prompt_is_unchanged(
        tmp_path, monkeypatch):
    monkeypatch.setattr(ledgers, "_served", frozenset())
    store = new_store(tmp_path)
    seed(store, OLD_TEXT)
    port = Model()
    job = writer.enqueue(store, "private", writer.ORDINARY)
    assert await new_writer(store, port).run_job(job) == writer.COMMITTED
    [(_, body)] = port.prompts
    assert "lower" not in body
    assert all("tier" not in e for e in body["events"])


async def test_the_lower_backlog_shown_is_bounded(
        tmp_path, friends, monkeypatch, caplog):
    store = new_store(tmp_path)
    owed = [seed(store, f"guests: note {i} " + "x" * 400) for i in range(6)]
    w_ = new_writer(store, Model())
    job = writer.enqueue(store, "private", writer.ORDINARY)
    assert await w_.run_job(job) == writer.COMMITTED
    assert pending_seqs(store, "friends") == owed
    seed(store, NEW_TEXT)
    monkeypatch.setattr(writer, "WHOLE_LEDGER_MAX_TOKENS", 250)
    port = Model()
    w2 = new_writer(store, port)
    job = writer.enqueue(store, "private", writer.ORDINARY)
    with caplog.at_level(logging.WARNING, logger="memory.writer"):
        assert await w2.run_job(job) == writer.COMMITTED
    shown = [e["seq"] for e in port.prompts[0][1]["lower"][0]["pending"]]
    assert shown == owed[-2:]
    assert any("left out" in m for m in caplog.messages)


# --- export and restore ------------------------------------------------------------

async def test_a_withdrawal_round_trips_through_export_and_restore(
        tmp_path, friends):
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    store = r.store_at(root)
    seed(store, OLD_TEXT)
    w_ = new_writer(store, Model())
    await drain(w_)
    seed(store, NEW_TEXT)
    await drain(w_)
    assert rows(store, "SELECT count(*) AS n FROM revisions WHERE op = "
                       "'withdraw'") == [{"n": 1}]
    path = export.export(store)
    moved = str(tmp_path / "saved")
    shutil.move(path, moved)
    assert r.stage(root, moved) == "staged"
    p = r.run_step(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    r.assert_store_equals_export(store.path, moved)
    after = r.store_at(root)
    assert not any(OLD in b for b in current(after, "friends"))
    assert pending_seqs(after, "friends") == []


async def test_a_repeated_withdrawal_applies_once(tmp_path, friends):
    store = new_store(tmp_path)
    old = seed(store, OLD_TEXT)
    w_ = new_writer(store, Model())
    job = writer.enqueue(store, "private", writer.ORDINARY)
    assert await w_.run_job(job) == writer.COMMITTED
    _, [entry] = add(store, [fact("locker", OLD)], ledger="friends")

    def twice(body):
        one = [{"op": "withdraw", "ledger": "friends", "object_id": entry},
               {"op": "withdraw", "ledger": "friends", "seq": old}]
        return one + one

    seed(store, "the boiler is serviced in May")
    job = writer.enqueue(store, "private", writer.ORDINARY)
    assert await new_writer(store, Model(twice)).run_job(job) == \
        writer.COMMITTED
    assert rows(store, "SELECT count(*) AS n FROM revisions WHERE op = "
                       "'withdraw'") == [{"n": 1}]
    assert rows(store, "SELECT count(*) AS n FROM dispositions WHERE seq = ? "
                       "AND ledger = 'friends'", old) == [{"n": 1}]
