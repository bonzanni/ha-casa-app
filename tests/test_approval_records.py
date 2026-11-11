"""The approval record store: one approval per component generation, used by
one attempt.

Every transition is one synchronous call that persists before it returns; a
request supersedes the component's live record and an attempt uses an
approval only under the plugin-tools lock."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import time

import pytest

import approval_records as ar
import tools
from channels.tg_address import TgAddress

KEY = "casa-test/mtg:mtg"
VALUES_A = {"config": {"A": "1"}, "secrets": ["S"]}
VALUES_B = {"config": {"A": "2"}, "secrets": ["S"]}


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    path = tmp_path / "data" / "approval-records.json"
    monkeypatch.setattr(ar, "PATH", str(path))
    # a lock of this test's own, so a held lock never leaks between tests
    monkeypatch.setattr(tools, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    ar.reset()
    yield path
    ar.reset()


async def _request(kind="specialist_install", key=KEY, identity="id-A", values=VALUES_A,
                   **kw):
    async with tools._plugin_tools_guard():
        return ar.request(kind, key, identity, values, **kw)


async def _attempt(origin, *, kind="specialist_install", identity="id-A"):
    async with tools._plugin_tools_guard():
        return ar.attempt(origin, kind=kind, consent_identity=identity)


def _origin(r: ar.ApprovalRecord) -> dict:
    return {"rid": r.rid, "generation": r.generation}


async def _approved(**kw) -> ar.ApprovalRecord:
    r, outcome, _ = await _request(**kw)
    assert outcome in ("new", "superseded_previous")
    assert ar.approve(r.rid) is not None
    return ar.get(r.rid)


def _reload():
    ar.reset()


# -- the one-shot machine ---------------------------------------------------------

async def test_pending_approved_used_is_the_one_way_through():
    r, outcome, prev = await _request(asked="install mtg please")
    assert (outcome, prev, r.state, r.generation) == ("new", None, "pending", 1)
    assert r.values == VALUES_A and r.values_digest == ar.values_digest(VALUES_A)
    assert r.asked == "install mtg please" and r.used_at is None
    a = ar.approve(r.rid)
    assert a is not None and a.state == "approved"
    assert ar.startable(r.rid, r.generation) == "ok"
    assert await _attempt(_origin(r)) is None
    used = ar.get(r.rid)
    assert used.state == "used" and isinstance(used.used_at, float)
    assert ar.startable(r.rid, r.generation) == "used"


async def test_a_second_attempt_is_refused_as_used():
    r = await _approved()
    assert await _attempt(_origin(r)) is None
    refusal = await _attempt(_origin(r))
    assert refusal == {"ok": False, "kind": "approval_used", "detail": ar.USED_DETAIL}
    assert ar.fence(_origin(r), kind="specialist_install", consent_identity="id-A") == refusal


async def test_deny_and_expire_end_a_pending_record():
    r1, _, _ = await _request()
    ar.deny(r1.rid)
    assert ar.get(r1.rid).state == "denied"
    assert ar.approve(r1.rid) is None
    r2, outcome, prev = await _request()
    assert (outcome, prev, r2.generation) == ("new", None, 2)
    ar.expire(r2.rid)
    assert ar.get(r2.rid).state == "expired"
    assert ar.approve(r2.rid) is None


async def test_deny_and_expire_leave_an_approved_record_alone():
    r = await _approved()
    ar.deny(r.rid)
    ar.expire(r.rid)
    assert ar.get(r.rid).state == "approved"


async def test_approve_twice_returns_none_the_second_time():
    r, _, _ = await _request()
    assert ar.approve(r.rid) is not None
    assert ar.approve(r.rid) is None
    assert ar.approve("no-such-rid") is None


# -- generations and supersession ---------------------------------------------------

async def test_each_component_counts_its_own_generations():
    a1, _, _ = await _request(key="x:one")
    b1, _, _ = await _request(key="x:two")
    a2, _, _ = await _request(key="x:one", identity="id-B", values=VALUES_B)
    assert (a1.generation, b1.generation, a2.generation) == (1, 1, 2)
    assert ar.current("x:one").rid == a2.rid
    assert ar.current("x:two").rid == b1.rid
    assert ar.current("x:three") is None


async def test_a_newer_request_supersedes_a_pending_record():
    r1, _, _ = await _request()
    r2, outcome, prev = await _request(identity="id-B", values=VALUES_B)
    assert outcome == "superseded_previous"
    assert prev.rid == r1.rid and prev.state == "superseded"
    assert ar.get(r1.rid).state == "superseded"
    assert ar.approve(r1.rid) is None                    # a tap on the older card
    assert ar.current(KEY).rid == r2.rid


async def test_a_newer_request_supersedes_an_approved_record():
    r1 = await _approved()
    r2, outcome, prev = await _request(identity="id-B", values=VALUES_B)
    assert outcome == "superseded_previous" and prev.rid == r1.rid
    assert ar.startable(r1.rid, r1.generation) == "superseded"
    assert await _attempt(_origin(r1)) == {"ok": False, "kind": "approval_superseded"}
    assert ar.get(r1.rid).state == "superseded"
    assert r2.state == "pending"


async def test_an_equal_pending_request_is_asked_again():
    r1, _, _ = await _request()
    r2, outcome, prev = await _request()
    assert outcome == "superseded_previous" and prev.rid == r1.rid
    assert r2.generation == 2


async def test_a_used_record_stays_used_and_is_no_longer_current():
    r1 = await _approved()
    assert await _attempt(_origin(r1)) is None
    r2, outcome, prev = await _request()                 # the same identity again
    assert (outcome, prev) == ("new", None)
    assert ar.get(r1.rid).state == "used"
    assert ar.current(KEY).rid == r2.rid and r2.generation == 2


async def test_an_equal_approved_identity_is_already_approved_and_nothing_is_written(store):
    r1 = await _approved()
    before = store.read_bytes()
    r, outcome, prev = await _request()
    assert (r.rid, outcome, prev) == (r1.rid, "already_approved", None)
    assert store.read_bytes() == before


async def test_the_run_casa_started_for_the_approval_is_in_progress():
    r1 = await _approved()
    r, outcome, _ = await _request(carrying_rid=r1.rid)
    assert (r.rid, outcome) == (r1.rid, "in_progress")
    r, outcome, _ = await _request(carrying_rid="another-run")
    assert outcome == "already_approved"


async def test_the_same_identity_of_another_kind_is_a_new_request():
    r1 = await _approved(kind="specialist_install")
    r2, outcome, prev = await _request(kind="specialist_upgrade")
    assert outcome == "superseded_previous" and prev.rid == r1.rid
    assert r2.kind == "specialist_upgrade"


async def test_a_changed_artifact_with_equal_values_is_a_new_request():
    # the consent identity covers the artifact: equal values, another root
    r1 = await _approved(identity="id-root-1")
    r2, outcome, prev = await _request(identity="id-root-2")
    assert outcome == "superseded_previous" and prev.rid == r1.rid
    assert r2.values == r1.values and r2.consent_identity == "id-root-2"
    # and after the earlier one was used: simply new
    assert ar.approve(r2.rid) is not None
    assert await _attempt(_origin(r2), identity="id-root-2") is None
    r3, outcome, prev = await _request(identity="id-root-3")
    assert (outcome, prev) == ("new", None)


async def test_asked_is_kept_to_its_bound():
    r, _, _ = await _request(asked="x" * 2000)
    assert r.asked == "x" * ar.ASKED_MAX


async def test_request_freezes_a_copy_of_the_values():
    values = {"config": {"A": "1"}, "secrets": ["S"]}
    r, _, _ = await _request(values=values)
    values["config"]["A"] = "changed"
    assert ar.get(r.rid).values == {"config": {"A": "1"}, "secrets": ["S"]}
    got = ar.approved_values(_origin(r))
    got["config"]["A"] = "changed too"
    assert ar.approved_values(_origin(r)) == {"config": {"A": "1"}, "secrets": ["S"]}


# -- set_card --------------------------------------------------------------------

async def test_set_card_returns_the_record_as_it_now_is():
    addr = TgAddress(111, 701, 9)
    r1, _, _ = await _request()
    got = ar.set_card(r1.rid, addr)
    assert got.state == "pending" and got.card == addr


async def test_set_card_returns_a_superseded_record_as_superseded():
    # a request created its record and released the lock; a newer request
    # superseded it before its card was posted
    r1, _, _ = await _request()
    await _request(identity="id-B", values=VALUES_B)
    got = ar.set_card(r1.rid, TgAddress(111, 701, 10))
    assert got.state == "superseded" and got.card == TgAddress(111, 701, 10)
    _reload()
    assert ar.get(r1.rid).card == TgAddress(111, 701, 10)


# -- start check, fence and attempt refusals ------------------------------------------

async def test_startable_verdicts():
    r1, _, _ = await _request()
    assert ar.startable(r1.rid, r1.generation) == "superseded"      # pending is not startable
    ar.approve(r1.rid)
    assert ar.startable(r1.rid, r1.generation) == "ok"
    assert ar.startable(r1.rid, r1.generation + 1) == "unknown"     # another generation
    assert ar.startable("no-such-rid", 1) == "unknown"
    await _request(identity="id-B", values=VALUES_B)
    assert ar.startable(r1.rid, r1.generation) == "superseded"


@pytest.mark.parametrize("origin", [None, "x", {}, {"rid": "r"}, {"rid": "", "generation": 1},
                                    {"rid": "r", "generation": True},
                                    {"rid": "r", "generation": "1"}])
async def test_a_missing_or_malformed_approval_is_required(origin):
    refusal = await _attempt(origin)
    assert refusal["ok"] is False and refusal["kind"] == "approval_required"
    assert ar.fence(origin, kind="specialist_install", consent_identity="id-A") == refusal
    assert ar.approved_values(origin) is None


async def test_an_unknown_approval_is_required():
    r = await _approved()
    origin = {"rid": r.rid, "generation": r.generation + 5}
    refusal = await _attempt(origin)
    assert refusal == {"ok": False, "kind": "approval_required", "detail": ar.UNREADABLE_DETAIL}
    assert ar.get(r.rid).state == "approved"


async def test_a_pending_approval_cannot_be_attempted():
    r, _, _ = await _request()
    assert await _attempt(_origin(r)) == {"ok": False, "kind": "approval_superseded"}
    assert ar.get(r.rid).state == "pending"


async def test_another_kind_is_refused_at_fence_and_attempt_and_writes_nothing(store):
    r = await _approved(kind="specialist_install")
    before = store.read_bytes()
    want = {"ok": False, "kind": "approval_required", "detail": ar.OTHER_KIND_DETAIL}
    assert ar.fence(_origin(r), kind="specialist_upgrade", consent_identity="id-A") == want
    assert await _attempt(_origin(r), kind="specialist_upgrade") == want
    assert store.read_bytes() == before and ar.get(r.rid).state == "approved"


async def test_another_identity_is_refused_at_fence_and_attempt_and_writes_nothing(store):
    r = await _approved()
    before = store.read_bytes()
    want = {"ok": False, "kind": "approval_required", "detail": ar.OTHER_IDENTITY_DETAIL}
    assert ar.fence(_origin(r), kind="specialist_install", consent_identity="id-X") == want
    assert await _attempt(_origin(r), identity="id-X") == want
    assert store.read_bytes() == before and ar.get(r.rid).state == "approved"


async def test_fence_is_a_read():
    r = await _approved()
    assert ar.fence(_origin(r), kind="specialist_install", consent_identity="id-A") is None
    assert ar.fence(_origin(r), kind="specialist_install", consent_identity="id-A") is None
    assert ar.get(r.rid).state == "approved"


def _write_doc(path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": ar.SCHEMA_VERSION, "records": records}),
                    encoding="utf-8")


def _raw(rid, generation, state="approved", *, key=KEY, values=VALUES_A, updated_at=None,
         used_at=None, identity="id-A"):
    now = time.time()
    return {"rid": rid, "kind": "specialist_install", "component_key": key,
            "consent_identity": identity, "generation": generation, "values": values,
            "values_digest": ar.values_digest(values), "state": state, "card": None,
            "asked": "", "used_at": used_at, "created_at": now,
            "updated_at": now if updated_at is None else updated_at}


async def test_an_approved_record_that_is_not_its_components_current_one_is_refused(store):
    # Only a newer request can make a record non-current, and it supersedes the
    # record in the same write — but the attempt checks currency on its own,
    # so even a store holding two approved generations lets only the newer act.
    _write_doc(store, [_raw("old", 1), _raw("new", 2)])
    _reload()
    assert ar.startable("old", 1) == "superseded"
    assert await _attempt({"rid": "old", "generation": 1}) == {
        "ok": False, "kind": "approval_superseded"}
    assert ar.get("old").state == "approved"
    assert await _attempt({"rid": "new", "generation": 2}) is None


def test_only_the_components_current_pending_record_can_be_approved(store):
    _write_doc(store, [_raw("old", 1, "pending"), _raw("new", 2, "pending")])
    _reload()
    assert ar.approve("old") is None and ar.get("old").state == "pending"
    assert ar.approve("new").state == "approved"


# -- the plugin-tools lock ------------------------------------------------------------

def test_request_refuses_to_run_without_the_plugin_tools_lock(store):
    with pytest.raises(AssertionError):
        ar.request("specialist_install", KEY, "id-A", VALUES_A)
    assert not store.exists() and ar.current(KEY) is None


async def test_attempt_refuses_to_run_without_the_plugin_tools_lock():
    r = await _approved()
    with pytest.raises(AssertionError):
        ar.attempt(_origin(r), kind="specialist_install", consent_identity="id-A")
    assert ar.get(r.rid).state == "approved"


async def test_a_request_waits_for_a_governed_write_holding_the_lock():
    """A governed tool holds the lock from its attempt through its library
    write; a request from elsewhere made meanwhile waits for it, so the newer
    record only exists once the write has completed."""
    r = await _approved()
    events: list[str] = []
    attempted, release = asyncio.Event(), asyncio.Event()

    async def governed():
        async with tools._plugin_tools_guard():
            assert ar.attempt(_origin(r), kind="specialist_install",
                              consent_identity="id-A") is None
            attempted.set()
            await release.wait()                    # the library write, paused
            events.append("write")

    async def newer_request():
        await attempted.wait()
        async with tools._plugin_tools_guard():
            ar.request("specialist_install", KEY, "id-B", VALUES_B)
            events.append("request")

    t1 = asyncio.create_task(governed())
    t2 = asyncio.create_task(newer_request())
    await attempted.wait()
    for _ in range(5):
        await asyncio.sleep(0)
    assert events == [] and ar.current(KEY).rid == r.rid
    release.set()
    await asyncio.gather(t1, t2)
    assert events == ["write", "request"]
    assert ar.get(r.rid).state == "used"


async def test_two_attempts_at_once_use_the_approval_once():
    r = await _approved()

    async def one():
        async with tools._plugin_tools_guard():
            refusal = ar.attempt(_origin(r), kind="specialist_install", consent_identity="id-A")
            await asyncio.sleep(0)
            return refusal

    results = await asyncio.gather(one(), one())
    assert sorted(x is None for x in results) == [False, True]
    assert [x for x in results if x is not None][0]["kind"] == "approval_used"


# -- persistence ----------------------------------------------------------------------

async def test_records_survive_a_reload(store):
    r1 = await _approved(asked="the words")
    ar.set_card(r1.rid, TgAddress(111, 701, 5))
    r2, _, _ = await _request(key="persona:casa/tina", kind="persona_install",
                              identity="p-1", values={})
    _reload()
    got = ar.get(r1.rid)
    assert got == ar.get(r1.rid) and got.state == "approved" and got.asked == "the words"
    assert got.card == TgAddress(111, 701, 5) and got.values == VALUES_A
    assert ar.current("persona:casa/tina").rid == r2.rid
    assert stat.S_IMODE(os.stat(store).st_mode) == 0o600


async def test_a_corrupt_file_is_set_aside_and_the_store_starts_empty(store):
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{not json", encoding="utf-8")
    _reload()
    assert ar.current(KEY) is None
    assert (store.parent / (store.name + ".corrupt")).read_text(encoding="utf-8") == "{not json"
    r, outcome, _ = await _request()
    assert outcome == "new" and r.generation == 1


@pytest.mark.parametrize("mangle", [
    lambda d: d.update(schema_version=2),
    lambda d: d["records"][0].update(state="committed"),
    lambda d: d["records"][0].update(values_digest="sha256:" + "0" * 64),
    lambda d: d["records"][0].update(generation=0),
    lambda d: d["records"][0].update(card=[0, 1, 2]),
    lambda d: d["records"][0].update(used_at=time.time()),        # used_at on an approved one
    lambda d: d["records"].append(dict(d["records"][0])),          # one rid twice
])
async def test_a_malformed_document_is_set_aside_whole(store, mangle):
    r = await _approved()
    doc = json.loads(store.read_text(encoding="utf-8"))
    mangle(doc)
    store.write_text(json.dumps(doc), encoding="utf-8")
    _reload()
    assert ar.get(r.rid) is None and ar.current(KEY) is None
    assert (store.parent / (store.name + ".corrupt")).exists()


async def test_a_failed_write_raises_and_leaves_memory_equal_to_disk(store, monkeypatch):
    r = await _approved()
    before = store.read_bytes()

    def _fail(*a, **k):
        raise OSError("disk full")

    import atomic_io
    with monkeypatch.context() as mp:
        mp.setattr(atomic_io, "atomic_write_json", _fail)
        with pytest.raises(ar.ApprovalStoreError):
            await _attempt(_origin(r))
        with pytest.raises(ar.ApprovalStoreError):
            await _request(identity="id-B", values=VALUES_B)
        with pytest.raises(ar.ApprovalStoreError):
            ar.set_card(r.rid, TgAddress(111, 701, 3))
        assert ar.get(r.rid).state == "approved" and ar.get(r.rid).card is None
        assert ar.current(KEY).rid == r.rid
    assert store.read_bytes() == before
    assert await _attempt(_origin(r)) is None


async def test_old_terminal_records_are_dropped_at_load_but_each_current_one_is_kept(store):
    old = time.time() - ar.PRUNE_AFTER_S - 60
    _write_doc(store, [
        _raw("used-old", 1, "used", updated_at=old, used_at=old),
        _raw("sup-old", 2, "superseded", updated_at=old),
        _raw("cur-old", 3, "expired", updated_at=old),               # the current one
        _raw("other-recent", 1, "denied", key="x:y"),
        _raw("other-pending-old", 1, "pending", key="x:z", updated_at=old),
    ])
    _reload()
    assert ar.get("used-old") is None and ar.get("sup-old") is None
    assert ar.get("cur-old") is not None
    assert ar.get("other-recent") is not None and ar.get("other-pending-old") is not None
    r, _, _ = await _request()
    assert r.generation == 4


async def test_nothing_is_written_until_the_first_transition(store):
    assert ar.current(KEY) is None and ar.get("x") is None
    assert ar.startable("x", 1) == "unknown"
    assert not store.exists()


def test_not_current_carries_its_verdict_and_line():
    for verdict in ("superseded", "used", "unknown"):
        exc = ar.ApprovalNotCurrent(verdict)
        assert exc.verdict == verdict and str(exc) == ar.LINES[verdict]
    assert str(ar.ApprovalNotCurrent("other")) == ar.LINES["unknown"]


def test_values_digest_is_canonical():
    assert ar.values_digest({"config": {"b": "2", "a": "1"}, "secrets": ["S"]}) == \
        ar.values_digest({"secrets": ["S"], "config": {"a": "1", "b": "2"}})
    assert ar.values_digest(VALUES_A) != ar.values_digest(VALUES_B)


def test_the_approval_key_is_reserved():
    import provenance
    assert ar.APPROVAL_KEY == provenance.APPROVAL_KEY == "_approval"
    assert "_approval" in provenance.RESERVED_CONTEXT_KEYS
    assert "_approval" not in provenance.sanitize_external_context({"_approval": {"rid": "x"}})
