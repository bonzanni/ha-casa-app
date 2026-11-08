"""INV-MEM-034: every read path checks each object it returns.

The matrix: every read path × every reader clearance, over one store holding
a canary at every tier — events (and a provisional one), the ``private`` and
``friends`` ledgers with their history, and correction marks. Each path must return exactly the canaries its reader may read:

* the served view and ``open_memory`` (history included) by ledger: the
  highest maintained ledger at or below the reader;
* the correction note by that ledger and by each event's tier;
* search hits and each hit's window neighbours, date browse (its counts
  included) and the engager's search by event tier, before any paging.

An id the reader's ledger does not hold gets the same answer as an id that
does not exist. Reachability is checked per ledger, and a ledger past the
budget even at its coarsest level is served anyway. Real store; no model.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from memory import browse, corrections, ledgers, note, search, view, writer
from memory_serving_support import add, fact
from memory_tools_support import canary, new_store, store_event
from memory_writer_support import ScriptedPort, new_writer, reply

pytestmark = [pytest.mark.unit]

TIERS = ("public", "friends", "family", "private")
WORD = "kettle"


def rank(tier: str) -> int:
    return TIERS.index(tier)


@pytest.fixture
def friends(monkeypatch):
    monkeypatch.setattr(ledgers, "_served", frozenset({"friends"}))


class World:
    """A store with a canary at every tier of every kind of memory."""

    def __init__(self, tmp_path) -> None:
        self.store = new_store(tmp_path)
        today = date.today().isoformat()
        #: event canary -> (tier, provisional)
        self.events: dict[str, tuple[str, bool]] = {}
        self.seqs: dict[str, int] = {}
        for n, tier in enumerate(TIERS):
            c = canary()
            self.seqs[c] = store_event(self.store, f"{WORD} {c}", tier=tier,
                                       day=today, at=f"10:0{n}")
            self.events[c] = (tier, False)
        c = canary()
        self.seqs[c] = store_event(self.store, f"{WORD} {c}", day=today,
                                   at="10:09", classified=False)
        self.events[c] = ("private", True)
        #: object canary -> ledger (current text and earlier text alike)
        self.objects: dict[str, str] = {}
        self.ids: dict[str, list[str]] = {"private": [], "friends": []}
        for ledger in ("private", "friends"):
            then, now = canary(), canary()
            _, [oid] = add(self.store, [fact("thing", then)], ledger=ledger)
            add(self.store, [{"op": "replace", "object_id": oid,
                              "body": now}], ledger=ledger)
            self.objects[then] = self.objects[now] = ledger
            self.ids[ledger].append(oid)

    def readable_events(self, clearance: str) -> set[str]:
        return {c for c, (tier, provisional) in self.events.items()
                if rank(tier) <= rank(clearance)}

    def ledger_objects(self, clearance: str) -> set[str]:
        ledger = view.ledger_for(clearance)
        return {c for c, led in self.objects.items() if led == ledger}

    def all_canaries(self) -> set[str]:
        return {*self.events, *self.objects}


def seen(world: World, text: str) -> set[str]:
    return {c for c in world.all_canaries() if c in text}


@pytest.mark.parametrize("clearance", TIERS)
def test_every_read_path_returns_only_what_the_reader_may_read(
        tmp_path, friends, clearance):
    from tools import _engager_context
    w = World(tmp_path)
    readable = w.readable_events(clearance)
    objects = w.ledger_objects(clearance)
    assert view.ledger_for(clearance) == {
        "public": None, "friends": "friends", "family": "friends",
        "private": "private"}[clearance]

    # The served view: the reader's ledger, current values only.
    served = view.serve(w.store, f"k-{clearance}", clearance=clearance,
                        budget=10_000).view
    shown = seen(w, served.text) if served is not None else set()
    assert shown == {c for c in objects
                     if c in json.dumps(_current(w, clearance))}

    # open_memory, with history: the reader's ledger, every revision.
    ids = [*w.ids["private"], *w.ids["friends"], "o-no-such-id"]
    opened = view.open_objects(w.store, ids, clearance=clearance,
                               with_history=True)
    if view.ledger_for(clearance) is None:
        assert opened is None
    else:
        assert seen(w, json.dumps(opened)) == objects
        own = set(w.ids[view.ledger_for(clearance)])
        for item in opened:
            if item["id"] not in own:
                assert item == {"id": item["id"], "status": "unknown"}

    # Search: hits and every hit's window neighbours.
    found = search.search(w.store, WORD, clearance=clearance)
    assert seen(w, json.dumps(found.hits)) == readable
    assert len(found.hits) == len(readable)

    # Date browse, and its counts.
    page = browse.browse(w.store, clearance=clearance,
                         first_day=date.today(), last_day=date.today())
    assert seen(w, page.text) == readable
    assert page.total == len(readable)

    # The engager's view and search.
    context = _engager_context(w.store, WORD, clearance)
    assert seen(w, context) == readable | {
        c for c in objects if c in json.dumps(_current(w, clearance))}


def _current(w: World, clearance: str) -> list:
    """The current text of the reader's ledger."""
    ledger = view.ledger_for(clearance)
    if ledger is None:
        return []
    with w.store.transaction() as conn:
        return [o.body for o in view.objects_as_of(
            conn, ledger, 10 ** 9)]


@pytest.mark.parametrize("clearance", TIERS)
def test_the_note_owes_only_corrections_the_reader_may_read(
        tmp_path, friends, clearance):
    w = World(tmp_path)
    for seq in w.seqs.values():
        assert corrections.mark(w.store, seq).marked
    text = note.for_turn(w.store, clearance=clearance).text
    ledger = view.ledger_for(clearance)
    if ledger is None:
        assert text == ""
        return
    # Pending in the reader's ledger (classified at or below it, or any
    # for private) and readable at the reader's clearance.
    owed = {c for c, (tier, provisional) in w.events.items()
            if rank(tier) <= rank(ledger)
            and (ledger == "private" or not provisional)
            and rank(tier) <= rank(clearance)}
    assert seen(w, text) == owed


def test_the_note_checks_each_correction_s_own_tier(tmp_path, friends):
    """The note re-reads each correction's tier, so one the reader cannot
    read is never shown, even when its ledger holds a disposition for it
    (a state only a raised tier could leave)."""
    store = new_store(tmp_path)
    secret, shared = canary(), canary()
    above = store_event(store, secret)
    fine = store_event(store, shared, tier="friends")
    for seq in (above, fine):
        assert corrections.mark(store, seq).marked
    with store.transaction(write=True) as conn:
        conn.execute(
            "INSERT INTO dispositions (seq, ledger, job_id, outcome, "
            "commit_no, committed_at) VALUES (?, 'friends', 'j', "
            "'patched', 7, '2026-01-01T00:00:00+00:00')", (above,))
        conn.execute(
            "INSERT INTO dispositions (seq, ledger, job_id, outcome, "
            "commit_no, committed_at) VALUES (?, 'friends', 'j', "
            "'patched', 7, '2026-01-01T00:00:00+00:00')", (fine,))
    with store.transaction() as conn:
        owed = note.corrections(conn, ledger="friends", clearance="friends",
                                base_commit_no=0)
    assert [text for _, text in owed] == [shared]


def test_an_unknown_id_and_an_unreadable_id_get_one_answer(tmp_path, friends):
    w = World(tmp_path)
    [private_id] = w.ids["private"]
    [friends_id] = w.ids["friends"]
    for retire in (True, False):
        out = view.open_objects(
            w.store, [private_id, "o-no-such-id", friends_id],
            clearance="friends", with_history=True)
        assert out[0] == {"id": private_id, "status": "unknown"}
        assert out[1] == {"id": "o-no-such-id", "status": "unknown"}
        assert out[2]["id"] == friends_id and "text" in out[2]
        if retire:
            # Retired in its own ledger, it is still unknown to friends.
            add(w.store, [{"op": "retire", "object_id": private_id}])
    assert view.open_objects(w.store, [private_id], clearance="private")[0][
        "status"] == "retired"


def test_an_object_is_dated_only_from_events_its_ledger_may_read(
        tmp_path, friends):
    """A source id naming an event the ledger cannot read dates nothing.
    A patch never records one (INV-MEM-029), so the row is written as a
    raised tier would leave it: the read check stands on its own."""
    store = new_store(tmp_path)
    private = store_event(store, "a private day", day="2001-01-01")
    shared = store_event(store, "a shared day", tier="friends",
                         day="2002-02-02")
    _, [only_private] = add(store, [fact("a", "x")], ledger="friends")
    _, [both] = add(store, [fact("b", "y")], ledger="friends")
    for oid, cited in ((only_private, [private]), (both, [private, shared])):
        with store.transaction(write=True) as conn:
            for table in ("objects", "revisions"):
                conn.execute(f"UPDATE {table} SET source_seqs = ? WHERE "
                             "ledger = 'friends' AND object_id = ?",
                             (json.dumps(cited), oid))
    _, [mine] = add(store, [fact("c", "z", source_seqs=[private])])
    friends_view = view.open_objects(store, [only_private, both],
                                     clearance="friends")
    assert "date" not in friends_view[0]
    assert friends_view[1]["date"] == "2002-02-02"
    assert view.open_objects(store, [mine], clearance="private")[0][
        "date"] == "2001-01-01"


async def test_reachability_is_checked_per_ledger(tmp_path, friends):
    """A ``friends`` commit that leaves a ``friends`` object unreachable
    fails; the ``private`` ledger's own state neither causes nor hides
    it."""
    store = new_store(tmp_path)
    _, [group] = add(store, [{"op": "add", "kind": "group", "line": "g"}],
                     ledger="friends")
    add(store, [fact("m", "v", parent=group)], ledger="friends")
    _, [pgroup] = add(store, [{"op": "add", "kind": "group", "line": "p"}])
    add(store, [fact("pm", "pv", parent=pgroup)])
    seq = store_event(store, "drop the group", tier="friends")
    port = ScriptedPort(lambda p: reply(
        {seq: "patched"}, [{"op": "retire", "object_id": group,
                            "source_seqs": [seq]}]))
    job = writer.enqueue(store, "friends", writer.ORDINARY)
    assert await new_writer(store, port).run_job(job) == writer.RETRY
    with store.transaction() as conn:
        assert view.unreachable(conn, "friends") == []
        assert view.unreachable(conn, "private") == []
    # The same retire in private leaves friends reachable, private not.
    with store.transaction(write=True) as conn:
        conn.execute("UPDATE objects SET status = 'retired' "
                     "WHERE ledger = 'private' AND object_id = ?", (pgroup,))
        assert view.unreachable(conn, "friends") == []
        assert len(view.unreachable(conn, "private")) == 1


def test_a_lower_ledger_past_its_coarsest_level_is_served(tmp_path, friends):
    store = new_store(tmp_path)
    _, [topic] = add(store, [{"op": "add", "kind": "topic",
                              "line": "the house " * 20}], ledger="friends")
    add(store, [fact(f"thing {n}", "detail " * 10, parent=topic)
                for n in range(30)], ledger="friends")
    served = view.serve(store, "voice:butler:x", clearance="friends",
                        budget=5).view
    assert served is not None and served.ledger == "friends"
    assert served.overflow and view.HEAD_OVERFLOW in served.text
    assert topic in served.text
