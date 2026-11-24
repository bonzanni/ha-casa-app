"""The resident's contact tools: find, refresh and set marks. Each guard
(clearance, direct operator chat, store) refuses on its own. Invented people
and numbers only."""
from __future__ import annotations

import json
import pathlib
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from contacts import service
from contacts import store as cstore
from contacts.store import ContactsStore, Person
from contacts.sync import Result

CONTACT_TOOLS = ("find_contact", "refresh_contacts", "set_contact_marks")
GRANTS = {f"mcp__casa-framework__{name}" for name in CONTACT_TOOLS}

NOT_PRIVATE = "Contacts are only available in the operator's own chat."
NOT_DIRECT = "Marks can only be changed in a direct chat with the operator."
NOT_SET_UP = ('Contacts are not set up: add a vault item named '
              '"iCloud Contacts" with fields username and password.')

DEFAULTS = pathlib.Path("casa/rootfs/opt/casa/defaults")


@pytest.fixture
def opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ContactsStore:
    store = ContactsStore(str(tmp_path / "contacts"))
    assert store.open() is True, store.unavailable_reason
    monkeypatch.setattr(service, "_store", store)
    monkeypatch.setenv("CONTACTS_DEFAULT_REGION", "NL")
    return store


def _person(uid: str, name: str, *numbers: str, org: str = "",
            groups: tuple[str, ...] = ()) -> Person:
    return Person(source="icloud", uid=uid, name=name,
                  first_name=name.split()[0], org=org, note="",
                  numbers=tuple(numbers), groups=groups, related=())


def _fill(store: ContactsStore, people: list[Person], marks=()) -> None:
    with store.transaction(write=True) as conn:
        cstore.replace_source(conn, "icloud", people)
        for uid, field, value, origin in marks:
            cstore.set_mark(conn, "icloud", uid, field, value, origin)


def _rows(store: ContactsStore) -> list[tuple]:
    with store.transaction() as conn:
        return conn.execute(
            "SELECT uid, field, origin, value FROM marks "
            "ORDER BY uid, field, origin").fetchall()


def _marks(store: ContactsStore, uid: str) -> cstore.Marks:
    with store.transaction() as conn:
        return cstore.marks_for(conn, "icloud", uid)


@contextmanager
def _turn(clearance: str = "private", *, engagement=None,
          drop: tuple[str, ...] = (), **overrides):
    """A turn in the operator's direct Telegram chat, stamped *clearance*;
    *drop* removes keys from that origin."""
    import agent as agent_mod
    import tools as tools_mod
    origin = {
        "role": "assistant", "execution_role": "assistant",
        "channel": "telegram", "source": "telegram",
        "message_type": "channel_in", "chat_id": "500", "user_id": 500,
        "cid": "c1", "_origin_route": "telegram",
        "_origin_clearance": clearance,
    }
    origin.update(overrides)
    for key in drop:
        del origin[key]
    tok = agent_mod.origin_var.set(origin)
    eng_tok = tools_mod.engagement_var.set(engagement)
    try:
        yield
    finally:
        tools_mod.engagement_var.reset(eng_tok)
        agent_mod.origin_var.reset(tok)


async def _call(name: str, args: dict) -> dict:
    import tools as tools_mod
    res = await getattr(tools_mod, name).handler(args)
    return json.loads(res["content"][0]["text"])


async def test_find_returns_marks_and_caps_at_ten(
        opened: ContactsStore) -> None:
    people = [_person(f"U{i:02d}", f"Sample Person {i:02d}",
                      f"+312011100{i:02d}") for i in range(12)]
    people.append(_person("W", "Wanda Example", "+31201119999",
                          org="Example Plumbing", groups=("Priority",)))
    _fill(opened, people, [
        ("U07", "priority", "1", "group"),
        ("U03", "relation", "neighbour", "operator"),
        ("U03", "language", "it", "note"),
        ("W", "priority", "1", "group"),
        ("W", "priority", "0", "operator"),
    ])
    with _turn():
        got = await _call("find_contact", {"query": "sample person"})
        assert got["status"] == "ok"
        assert len(got["contacts"]) == 10
        assert got["more"] is True
        first = got["contacts"][0]
        assert (first["name"], first["priority"], first["priority_origin"]) \
            == ("Sample Person 07", True, "group")
        u03 = next(c for c in got["contacts"]
                   if c["name"] == "Sample Person 03")
        assert u03 == {"name": "Sample Person 03",
                       "numbers": ["+31201110003"], "org": "",
                       "groups": [], "priority": False,
                       "priority_origin": None, "relation": "neighbour",
                       "language": "it"}

        by_org = await _call("find_contact", {"query": "plumbing"})
        assert by_org["more"] is False
        assert [(c["name"], c["org"], c["groups"], c["priority"],
                 c["priority_origin"]) for c in by_org["contacts"]] == [
            ("Wanda Example", "Example Plumbing", ["Priority"], False,
             "operator")]

        # A number written without "+" reads in the default region.
        by_number = await _call("find_contact", {"query": "020 111 9999"})
        assert [c["name"] for c in by_number["contacts"]] == ["Wanda Example"]


async def test_tools_refuse_below_private_clearance(
        opened: ContactsStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _fill(opened, [_person("A", "Marco Example", "+31201110001")])
    refreshed: list[str] = []

    async def refresh(*, reason: str):
        refreshed.append(reason)
        return Result(ok=True, added=1, removed=0, kept=0, skipped_numbers=0,
                      note_changed=(), error="", tell_operator=False)

    monkeypatch.setattr(service, "refresh", refresh)
    calls = [("find_contact", {"query": "marco"}),
             ("refresh_contacts", {}),
             ("set_contact_marks", {"contact": "marco", "relation": "brother",
                                    "priority": True})]
    for clearance in ("family", "public"):
        with _turn(clearance):
            for name, args in calls:
                got = await _call(name, args)
                assert got == {"status": "error", "message": NOT_PRIVATE}, \
                    (clearance, name, got)
    # Origins that never read at private, whatever else they carry.
    origins = [
        ("telegram, no clearance stamp", {}, ("_origin_clearance",)),
        ("guest chat", {"_origin_route": "telegram_guest",
                        "channel": "telegram_guest"}, ()),
        ("voice, no route", {"channel": "voice"},
         ("_origin_route", "_origin_clearance")),
        ("webhook trigger claiming private",
         {"_origin_route": "webhook_trigger"}, ()),
    ]
    for label, overrides, drop in origins:
        with _turn(drop=drop, **overrides):
            for name, args in calls:
                got = await _call(name, args)
                assert got == {"status": "error", "message": NOT_PRIVATE}, \
                    (label, name, got)
    assert refreshed == []
    assert _rows(opened) == []


async def test_set_marks_refused_outside_direct_operator_chat(
        opened: ContactsStore) -> None:
    _fill(opened, [_person("A", "Marco Example", "+31201110001")])
    engagement = MagicMock()
    engagement.context_rebuild_pending = False
    engagement.kind = "executor"
    engagement.origin = None
    turns = [
        {"execution_role": "butler"},          # a delegated run
        {"synthetic": "scheduled"},            # not the operator's words
        {"message_type": "scheduled"},
        {"engagement": engagement},
    ]
    for overrides in turns:
        with _turn(**overrides):
            got = await _call("set_contact_marks",
                              {"contact": "marco", "relation": "brother",
                               "priority": False})
            assert got == {"status": "error", "message": NOT_DIRECT}, \
                (overrides, got)
            # Reading stays possible at private clearance.
            found = await _call("find_contact", {"query": "marco"})
            assert found["status"] == "ok", (overrides, found)
    assert _rows(opened) == []
    # A tapped button in the operator's chat is still his direct chat.
    with _turn(synthetic="button"):
        got = await _call("set_contact_marks",
                          {"contact": "marco", "relation": "brother"})
    assert got["status"] == "ok"


async def test_set_relation_and_language_apply_at_once_as_operator(
        opened: ContactsStore) -> None:
    _fill(opened, [_person("A", "Marco Example", "+31201110001")],
          [("A", "language", "it", "note")])
    with _turn():
        got = await _call("set_contact_marks",
                          {"contact": "marco", "relation": " sister ",
                           "language": "NL", "priority": None})
        assert got == {"status": "ok", "contact": "Marco Example",
                       "applied": {"relation": "sister", "language": "nl"}}
        assert _rows(opened) == [("A", "language", "note", "it"),
                                 ("A", "language", "operator", "nl"),
                                 ("A", "relation", "operator", "sister")]

        # A bad language rejects the whole call: nothing changes.
        for bad in ("dutch", "n", "1l", "nè"):
            got = await _call("set_contact_marks",
                              {"contact": "marco", "relation": "",
                               "language": bad})
            assert got["status"] == "error", bad
            assert _rows(opened) == [("A", "language", "note", "it"),
                                     ("A", "language", "operator", "nl"),
                                     ("A", "relation", "operator", "sister")]

        # "" clears the operator's own mark; the note's reading shows again.
        got = await _call("set_contact_marks",
                          {"contact": "Marco", "relation": "",
                           "language": ""})
        assert got == {"status": "ok", "contact": "Marco Example",
                       "applied": {"relation": "", "language": ""}}
        assert _rows(opened) == [("A", "language", "note", "it")]
        assert _marks(opened, "A") == cstore.Marks(
            priority=False, priority_origin=None, relation="", language="it")


async def test_priority_false_applies_at_once(opened: ContactsStore) -> None:
    _fill(opened, [_person("A", "Marco Example", "+31201110001",
                           groups=("Priority",))],
          [("A", "priority", "1", "group")])
    with _turn():
        # Already priority (the group): no card, the rest applies. Asking the
        # operator on a card is pinned in test_contacts_priority_card.py.
        got = await _call("set_contact_marks",
                          {"contact": "marco", "priority": True,
                           "relation": "brother"})
        assert got == {"status": "ok", "contact": "Marco Example",
                       "applied": {"relation": "brother",
                                   "priority": "already"}}
        assert _rows(opened) == [("A", "priority", "group", "1"),
                                 ("A", "relation", "operator", "brother")]

        got = await _call("set_contact_marks",
                          {"contact": "marco", "priority": False})
    assert got == {"status": "ok", "contact": "Marco Example",
                   "applied": {"priority": False}}
    assert _marks(opened, "A") == cstore.Marks(
        priority=False, priority_origin="operator", relation="brother",
        language="")
    assert ("A", "priority", "operator", "0") in _rows(opened)


async def test_ambiguous_contact_changes_nothing_and_lists_candidates(
        opened: ContactsStore) -> None:
    _fill(opened, [_person("A", "Marco Example", "+31201110001",
                           "+31201110009"),
                   _person("B", "Marco Sample", "+39201110002"),
                   _person("C", "Zoe Example", "+31201110003")])
    with _turn():
        got = await _call("set_contact_marks",
                          {"contact": "marco", "relation": "friend",
                           "language": "it", "priority": False})
        assert got["status"] == "error"
        assert "Marco Example (+31201110001)" in got["message"]
        assert "Marco Sample (+39201110002)" in got["message"]
        assert "Zoe" not in got["message"]

        got = await _call("set_contact_marks",
                          {"contact": "nobody here", "relation": "friend"})
        assert got["status"] == "error"
        assert got["message"].startswith("No contact matches")
    assert _rows(opened) == []


async def test_refresh_reports_counts_and_not_set_up(
        opened: ContactsStore, monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[Result | None] = [
        Result(ok=True, added=3, removed=1, kept=40, skipped_numbers=2,
               note_changed=("A",), error="", tell_operator=False),
        None,
        Result(ok=False, added=0, removed=0, kept=0, skipped_numbers=0,
               note_changed=(), error="iCloud refused the sign-in",
               tell_operator=False),
    ]
    reasons: list[str] = []

    async def refresh(*, reason: str):
        reasons.append(reason)
        return answers.pop(0)

    monkeypatch.setattr(service, "refresh", refresh)
    with _turn():
        assert await _call("refresh_contacts", {}) == {
            "status": "ok", "added": 3, "removed": 1, "kept": 40,
            "skipped_numbers": 2}
        assert await _call("refresh_contacts", {}) == {
            "status": "unavailable", "message": NOT_SET_UP}
        assert await _call("refresh_contacts", {}) == {
            "status": "error", "message": "iCloud refused the sign-in"}
    assert len(reasons) == 3


async def test_unavailable_store_is_reported_by_all_three(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = ContactsStore(str(tmp_path / "never-opened"))
    monkeypatch.setattr(service, "_store", broken)
    refreshed: list[str] = []

    async def refresh(*, reason: str):
        refreshed.append(reason)
        return None

    monkeypatch.setattr(service, "refresh", refresh)
    with _turn():
        for name, args in [
                ("find_contact", {"query": "marco"}),
                ("refresh_contacts", {}),
                ("set_contact_marks", {"contact": "marco",
                                       "relation": "brother"})]:
            got = await _call(name, args)
            assert got["status"] == "unavailable", (name, got)
            assert got["message"] != NOT_SET_UP, name
    assert refreshed == []


def _allowed(path: pathlib.Path) -> set[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return set((data.get("tools") or {}).get("allowed") or [])


def test_only_the_assistant_is_granted_the_contact_tools() -> None:
    granted = {
        DEFAULTS / "agents" / "assistant" / "runtime.yaml",
        DEFAULTS / "roles" / "resident" / "assistant" / "role.yaml",
    }
    configs = sorted({*DEFAULTS.rglob("runtime.yaml"),
                      *DEFAULTS.rglob("role.yaml")})
    assert granted <= set(configs)
    for path in configs:
        held = _allowed(path) & GRANTS
        if path in granted:
            assert held == GRANTS, path
        else:
            assert held == set(), path


def test_contact_tools_not_in_specialist_allowlist_or_restricted_webhook(
        ) -> None:
    import agent as agent_mod
    import tools as tools_mod
    from specialist_component import SPECIALIST_CASA_TOOL_ALLOWLIST

    assert set(CONTACT_TOOLS) <= {t.name for t in tools_mod.CASA_TOOLS}
    for name in CONTACT_TOOLS:
        assert name not in SPECIALIST_CASA_TOOL_ALLOWLIST
        assert f"mcp__casa-framework__{name}" not in \
            agent_mod._RESTRICTED_WEBHOOK_TOOLS
        assert name not in agent_mod._RESTRICTED_WEBHOOK_TOOLS
