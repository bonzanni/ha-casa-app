"""Id-addressed patches (``memory/patches.py``): every operation writes the
object's next revision and a history row; cues are add-only; an operation
can touch only its own ledger; a malformed one is refused; reachability
(``memory.view.unreachable``) follows parents a level up to the top."""
from __future__ import annotations

import json

import pytest

from memory import patches, view
from memory_writer_support import new_store, rows


def _apply(store, ops, ledger="private", commit_no=1):
    with store.transaction(write=True) as conn:
        touched = patches.apply(conn, ledger, ops, job_id="j", commit_no=commit_no,
                                committed_at="2026-11-02T10:00:00+00:00")
        return touched, view.unreachable(conn, ledger)


def _obj(store, object_id, ledger="private"):
    [o] = rows(store, "SELECT * FROM objects WHERE ledger = ? AND "
                      "object_id = ?", ledger, object_id)
    o["cues"] = json.loads(o["cues"])
    o["source_seqs"] = json.loads(o["source_seqs"])
    return o


def _fact(**kw):
    return {"op": "add", "kind": "entry", "entry_kind": "fact", "body": "b",
            **kw}


def test_add_and_replace_write_revisions_and_history(tmp_path):
    store = new_store(tmp_path)
    [oid], lost = _apply(store, [_fact(subject="Anna", cues=["birthday"],
                                       source_seqs=[1])])
    assert lost == []
    _apply(store, [{"op": "replace", "object_id": oid, "body": "new",
                    "cues": ["party"], "source_seqs": [2]}], commit_no=2)
    o = _obj(store, oid)
    assert (o["revision"], o["body"], o["subject"]) == (2, "new", "Anna")
    assert o["cues"] == ["birthday", "party"] and o["source_seqs"] == [1, 2]
    assert [(r["revision"], r["op"], r["commit_no"], r["body"]) for r in rows(
        store, "SELECT * FROM revisions ORDER BY revision")] == [
            (1, "add", 1, "b"), (2, "replace", 2, "new")]


def test_cues_are_add_only(tmp_path):
    store = new_store(tmp_path)
    [oid], _ = _apply(store, [_fact(cues=["a", "b"])])
    _apply(store, [{"op": "add_cue", "object_id": oid, "cues": ["b", "c"]},
                   {"op": "replace", "object_id": oid, "body": "x",
                    "cues": []}])
    assert _obj(store, oid)["cues"] == ["a", "b", "c"]
    for bad in ({"op": "add_cue", "object_id": oid, "cues": []},
                {"op": "replace", "object_id": oid, "cues": ["z"]}):
        with pytest.raises(patches.PatchError):
            _apply(store, [bad])


def test_retire_split_merge_set_line_move(tmp_path):
    store = new_store(tmp_path)
    (a, b, g), _ = _apply(store, [
        _fact(body="a"), _fact(body="b"),
        {"op": "add", "kind": "group", "line": "home"}])
    touched, lost = _apply(store, [
        {"op": "split", "object_id": a, "into": [_fact(body="a1"),
                                                  _fact(body="a2")]},
        {"op": "set_line", "object_id": b, "line": "what is b"},
        {"op": "move", "object_id": b, "parent": g}])
    assert lost == []
    a1, a2 = touched[0], touched[1]
    assert _obj(store, a)["status"] == "retired"
    assert json.loads(_obj(store, a1)["origin"]) == [a]
    assert (_obj(store, b)["line"], _obj(store, b)["parent"]) == (
        "what is b", g)
    touched, _ = _apply(store, [{"op": "merge", "object_ids": [a1, a2],
                                 "into": _fact(body="a again")}])
    m = touched[0]
    assert {_obj(store, a1)["status"], _obj(store, a2)["status"]} == {
        "retired"}
    assert json.loads(_obj(store, m)["origin"]) == [a1, a2]
    _apply(store, [{"op": "retire", "object_id": m}])
    assert _obj(store, m)["status"] == "retired"


def test_refs_name_objects_added_in_the_same_answer(tmp_path):
    store = new_store(tmp_path)
    touched, lost = _apply(store, [
        {"op": "add", "ref": "t", "kind": "topic", "line": "house"},
        {"op": "add", "ref": "g", "kind": "group", "line": "heating",
         "parent": "@t"},
        _fact(parent="@g")])
    assert lost == []
    topic, group, entry = touched
    assert _obj(store, entry)["parent"] == group
    assert _obj(store, group)["parent"] == topic
    with pytest.raises(patches.PatchError):
        _apply(store, [_fact(parent="@nope")])


def test_an_operation_never_reaches_another_ledger(tmp_path):
    store = new_store(tmp_path)
    [oid], _ = _apply(store, [_fact()], ledger="private")
    with pytest.raises(patches.PatchError):
        _apply(store, [{"op": "retire", "object_id": oid}], ledger="friends")
    assert _obj(store, oid)["status"] == "current"


@pytest.mark.parametrize("op", [
    {"op": "rewrite_all"},
    {"op": "add", "kind": "note"},
    {"op": "add", "kind": "entry", "entry_kind": "rumour"},
    {"op": "add", "kind": "group", "entry_kind": "fact"},
    {"op": "add", "kind": "entry", "entry_kind": "fact", "colour": "red"},
    {"op": "add", "kind": "entry", "entry_kind": "fact",
     "errand_state": "open"},
    {"op": "split", "object_id": "o1", "into": []},
    {"op": "merge", "object_ids": ["o1"], "into": {}},
    {"op": "set_line", "object_id": "o1"},
    "not an object",
])
def test_malformed_operations_are_refused(tmp_path, op):
    store = new_store(tmp_path)
    with pytest.raises(patches.PatchError):
        _apply(store, [op])
    assert rows(store, "SELECT * FROM objects") == []


def test_an_errand_opens_and_closes(tmp_path):
    store = new_store(tmp_path)
    [oid], _ = _apply(store, [{"op": "add", "kind": "entry",
                               "entry_kind": "errand", "body": "call Bo"}])
    assert _obj(store, oid)["errand_state"] == "open"
    _apply(store, [{"op": "replace", "object_id": oid, "errand_state": "done"}])
    assert _obj(store, oid)["errand_state"] == "done"


def test_reachability(tmp_path):
    store = new_store(tmp_path)
    (t, g, e), lost = _apply(store, [
        {"op": "add", "ref": "t", "kind": "topic", "line": "house"},
        {"op": "add", "ref": "g", "kind": "group", "line": "heating",
         "parent": "@t"},
        _fact(parent="@g")])
    assert lost == []
    _, lost = _apply(store, [{"op": "retire", "object_id": t}])
    assert lost == sorted([g, e])
    store2 = new_store(tmp_path / "two")
    (g2, e2), _ = _apply(store2, [
        {"op": "add", "ref": "g", "kind": "group", "line": "x"},
        _fact(parent="@g")])
    # An entry is never a parent; a group never sits under an entry.
    _, lost = _apply(store2, [{"op": "move", "object_id": g2,
                               "parent": e2}])
    assert set(lost) == {g2, e2}
