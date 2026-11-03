"""``open_memory``: entries of the session's own ledger by id, at their
latest committed revision, marked when they changed after the session's
view. An id outside that ledger gets exactly the answer an id that exists
nowhere gets."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent as agent_mod
import tools
from memory import sessions
from memory_serving_support import add, fact
from memory_tools_support import (  # noqa: F401 — fixtures
    canary, mem, payload, turn)
from session_registry import build_scoped_session_key

pytestmark = [pytest.mark.asyncio]

CHAT = "4242"
KEY = build_scoped_session_key("telegram", "assistant", CHAT)


async def _open(**args) -> dict:
    return payload(await tools.open_memory.handler(args))


def _bind(store, monkeypatch, base: int, *, sid: str = "sid-1",
          running: str = "sid-1") -> None:
    """Record this chat's session view at *base*, bound to SDK session
    *sid*; the session registry says the chat runs *running*."""
    with store.transaction(write=True) as conn:
        sessions.put(conn, sessions.SessionView(
            session_key=KEY, ledger="private", base_commit_no=base,
            clearance="private", budget=1000, renderer_version="1",
            block_hash="sha256:x", sdk_session_id=sid))
    monkeypatch.setattr(agent_mod, "active_session_registry", SimpleNamespace(
        get=lambda key: {"sdk_session_id": running} if key == KEY else None),
        raising=False)


async def test_an_entry_opens_in_full_and_a_group_as_its_lines(mem, turn):
    _, (group,) = add(mem, [{"op": "add", "kind": "group",
                             "line": "the car"}])
    _, (tyres, mot) = add(mem, [
        fact("tyres", "winter tyres fitted", parent=group, line="tyre type"),
        fact("MOT", "due in March", parent=group, line="MOT date")])
    turn("private")
    out = await _open(ids=[tyres, group])
    entry, opened_group = out["entries"]
    assert entry == {"id": tyres, "kind": "entry",
                     "text": "tyres: winter tyres fitted"}
    assert opened_group["label"] == "the car"
    assert sorted(opened_group["members"]) == sorted(
        [f"{tyres} tyre type", f"{mot} MOT date"])


async def test_an_id_of_another_ledger_answers_as_unknown(mem, turn):
    secret = canary()
    _, (friends_id,) = add(mem, [fact("gate", secret)], ledger="friends")
    turn("private")
    out = await _open(ids=[friends_id, "o0000000000000000"])
    assert out["entries"] == [{"id": friends_id, "status": "unknown"},
                              {"id": "o0000000000000000", "status": "unknown"}]
    assert secret not in str(out)


async def test_a_clearance_with_no_ledger_opens_nothing(mem, turn):
    secret = canary()
    _, (entry,) = add(mem, [fact("gate", secret)])
    turn("friends")
    out = await _open(ids=[entry])
    assert out["entries"] == [] and secret not in str(out)
    assert "search_memory" in out["message"]


async def test_a_change_after_the_view_is_marked(mem, turn, monkeypatch):
    base, (kept, changed) = add(mem, [fact("bins", "tuesday"),
                                      fact("milk", "oat")])
    add(mem, [{"op": "replace", "object_id": changed,
               "body": "soy; earlier: oat"}])
    _, (new,) = add(mem, [fact("dentist", "friday")])
    _bind(mem, monkeypatch, base)
    turn("private")
    out = {e["id"]: e for e in (await _open(ids=[kept, changed, new]))[
        "entries"]}
    assert "changed_since_your_memory_above" not in out[kept]
    assert out[changed]["changed_since_your_memory_above"] is True
    assert out[changed]["text"] == "milk: soy; earlier: oat"
    assert out[new]["changed_since_your_memory_above"] is True


async def test_a_view_of_another_session_marks_nothing(mem, turn,
                                                      monkeypatch):
    """The base is read only from a record bound to the SDK session the
    chat runs now."""
    base, (changed,) = add(mem, [fact("milk", "oat")])
    add(mem, [{"op": "replace", "object_id": changed, "body": "soy"}])
    _bind(mem, monkeypatch, base, sid="sid-old", running="sid-new")
    turn("private")
    (entry,) = (await _open(ids=[changed]))["entries"]
    assert "changed_since_your_memory_above" not in entry


async def test_history_and_retirement(mem, turn):
    _, (entry,) = add(mem, [fact("milk", "oat")])
    add(mem, [{"op": "replace", "object_id": entry, "body": "soy"}])
    add(mem, [{"op": "retire", "object_id": entry}])
    turn("private")
    (opened,) = (await _open(ids=[entry], with_history=True))["entries"]
    assert opened["status"] == "retired"
    assert [h["text"] for h in opened["history"]] == ["milk: oat",
                                                      "milk: soy"]
    assert [h["then"] for h in opened["history"]] == ["replace", "retire"]
    (plain,) = (await _open(ids=[entry]))["entries"]
    assert "history" not in plain


async def test_a_malformed_call_names_the_right_tool(mem, turn):
    turn("private")
    out = await _open(ids=[])
    assert out["status"] == "error" and "search_memory" in out["message"]
    out = await _open(ids=[f"o{n:016d}" for n in range(21)])
    assert out["status"] == "error" and out["kind"] == "too_many_ids"


async def test_an_unavailable_store_is_never_unknown(mem, turn):
    _, (entry,) = add(mem, [fact("milk", "oat")])
    turn("private")
    mem._unavailable_reason = "a simulated outage"
    try:
        out = await _open(ids=[entry])
    finally:
        mem._unavailable_reason = None
    assert out["status"] == "unavailable" and "entries" not in out
