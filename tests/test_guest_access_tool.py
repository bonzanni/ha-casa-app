"""The ``guest_access`` tool: the operator, inside a group, lets the specialist
they summoned there answer everyone in it. The group comes from the turn's
server-stamped origin, never from the model's arguments."""
import asyncio
import json

import pytest

import agent as agent_mod
import guest_groups
import tools

GROUP = {"id": -5, "title": "Game Night", "type": "group"}


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def store(tmp_path, monkeypatch):
    guest_groups.configure(tmp_path)
    monkeypatch.setattr(guest_groups, "component_of", lambda role: "casa/mtg")
    yield guest_groups.store()
    guest_groups.configure(None)


# The origin the next call runs under; bound inside the call's own context,
# as a turn binds it, since a fixture runs in a different one.
_ORIGIN: dict = {}


@pytest.fixture
def set_origin():
    def _set(origin: dict | None) -> None:
        _ORIGIN["value"] = origin

    _set(None)
    yield _set
    _ORIGIN.clear()


def _group_origin(**extra) -> dict:
    origin = {"role": "mtg", "execution_role": "mtg", "channel": "telegram",
              "chat_id": "-5", "_operator_turn": True, "_guest_chat": dict(GROUP)}
    origin.update(extra)
    return origin


@pytest.fixture
def origin_with_guest_chat(set_origin):
    set_origin(_group_origin())


@pytest.fixture
def origin_dm(set_origin):
    set_origin({"role": "mtg", "execution_role": "mtg", "channel": "telegram",
                "chat_id": "7", "_operator_turn": True})


@pytest.fixture
def origin_guest_no_operator(set_origin):
    origin = _group_origin()
    del origin["_operator_turn"]
    set_origin(origin)


async def _call(args: dict) -> dict:
    token = agent_mod.origin_var.set(_ORIGIN.get("value"))
    try:
        return _payload(await tools.guest_access.handler(args))
    finally:
        agent_mod.origin_var.reset(token)


async def test_allow_uses_the_origins_group_and_ignores_any_argument(
        store, origin_with_guest_chat):
    out = await _call({"action": "allow", "chat_id": -999, "title": "Other"})
    assert out == {"status": "ok", "action": "allow", "group": "Game Night",
                   "state": "allowed"}
    assert [(e.role, e.chat.id) for e in store.entries()] == [("mtg", -5)]


async def test_allow_records_the_installed_component(store, origin_with_guest_chat, tmp_path):
    await _call({"action": "allow"})
    entry = store.entries("mtg")[0]
    assert entry.component_id == "casa/mtg"
    assert entry.chat == guest_groups.ChatRef(-5, "Game Night", "group")
    # saved, not only held in memory
    on_disk = guest_groups.GuestStore(tmp_path / "agent-voices-guests.json")
    assert on_disk.allowed("mtg", -5, "casa/mtg")


async def test_allow_refuses_when_the_specialist_has_no_component(
        store, origin_with_guest_chat, monkeypatch):
    monkeypatch.setattr(guest_groups, "component_of", lambda role: None)
    out = await _call({"action": "allow"})
    assert out["status"] == "error" and out["kind"] == "no_component"
    assert out["message"] == "That specialist isn't fully installed yet."
    assert store.entries() == []


async def test_revoke_and_status(store, origin_with_guest_chat, tmp_path):
    assert (await _call({"action": "status"}))["state"] == "none"
    await _call({"action": "allow"})
    assert await _call({"action": "status"}) == {
        "status": "ok", "action": "status", "group": "Game Night", "state": "allowed"}
    assert await _call({"action": "revoke"}) == {
        "status": "ok", "action": "revoke", "group": "Game Night", "state": "none"}
    assert store.entries() == []
    assert guest_groups.GuestStore(tmp_path / "agent-voices-guests.json").entries() == []
    # revoking again is still fine
    assert (await _call({"action": "revoke"}))["state"] == "none"


async def test_status_names_an_entry_made_for_a_previous_specialist(
        store, origin_with_guest_chat, monkeypatch):
    store.allow("mtg", guest_groups.ChatRef(-5, "Game Night", "group"), "casa/old")
    assert (await _call({"action": "status"}))["state"] == "previous_specialist"


async def test_without_a_guest_chat_in_the_origin_it_refuses(store, origin_dm):
    for action in ("allow", "revoke", "status"):
        out = await _call({"action": action})
        assert out == {"status": "error", "kind": "not_in_group",
                       "message": "I can only do that from inside the group concerned."}
    assert store.entries() == []


async def test_without_any_origin_it_refuses(store):
    out = await _call({"action": "allow"})
    assert out["kind"] == "not_in_group" and store.entries() == []


async def test_without_the_operator_marker_it_refuses(store, origin_guest_no_operator):
    out = await _call({"action": "allow"})
    assert out["status"] == "error" and out["kind"] == "not_operator"
    assert out["message"] == "I can only do that from inside the group concerned."
    assert store.entries() == []


async def test_a_delegated_run_cannot_allow_a_group_for_itself(store, set_origin):
    """A run handed off from the summoned specialist copies its origin; the
    group belongs to the specialist that was summoned, not to the delegate."""
    set_origin(_group_origin(execution_role="browser"))
    out = await _call({"action": "allow"})
    assert out["kind"] == "not_in_group" and store.entries() == []


@pytest.mark.parametrize("chat_type", ["private", "channel", "sender", ""])
async def test_only_groups_and_supergroups_can_be_allowed(chat_type, store, set_origin):
    set_origin(_group_origin(_guest_chat={**GROUP, "type": chat_type}))
    out = await _call({"action": "allow"})
    assert out == {"status": "error", "kind": "not_a_group",
                   "message": "I answer others in groups only."}
    assert store.entries() == []


async def test_a_supergroup_can_be_allowed(store, set_origin):
    set_origin(_group_origin(_guest_chat={**GROUP, "type": "supergroup"}))
    assert (await _call({"action": "allow"}))["state"] == "allowed"


async def test_an_unknown_action_is_refused(store, origin_with_guest_chat):
    out = await _call({"action": "delete"})
    assert out["status"] == "error" and store.entries() == []


def test_the_tool_is_registered_with_its_schema():
    assert tools.guest_access in tools.CASA_TOOLS
    schema = tools.guest_access.input_schema
    assert schema["properties"]["action"]["enum"] == ["allow", "revoke", "status", "list"]
    assert schema["properties"]["role"] == {"type": "string"}
    assert schema["properties"]["chat_id"] == {"type": "integer"}
    assert schema["required"] == ["action"]


# -- Ellen's face: list and revoke from the resident's own chat ---------------

class _Registry:
    def tier_for_role(self, role):
        return "resident" if role == "assistant" else "specialist"


def _resident_origin(**extra) -> dict:
    origin = {"role": "assistant", "execution_role": "assistant",
              "channel": "telegram", "chat_id": "7", "_operator_turn": True}
    origin.update(extra)
    return origin


@pytest.fixture
def resident_registry(monkeypatch):
    monkeypatch.setattr(tools, "_agent_registry", _Registry())


@pytest.fixture
def origin_resident_operator(set_origin, resident_registry):
    set_origin(_resident_origin())


class _FakeBot:
    def __init__(self, supports):
        self._supports = supports
        self.calls = 0

    async def get_me(self):
        self.calls += 1
        if isinstance(self._supports, Exception):
            raise self._supports
        return type("User", (), {"supports_guest_queries": self._supports})()


def _endpoint(bot):
    app = type("App", (), {"bot": bot})()
    return type("Endpoint", (), {"_app": app})()


@pytest.fixture
def endpoints(monkeypatch):
    """role -> endpoint for the hint; a role absent here has no endpoint."""
    import own_voice
    bound: dict = {}
    monkeypatch.setattr(own_voice, "endpoint_for", lambda role: bound.get(role))
    return bound


@pytest.fixture
def two_roles(store, monkeypatch):
    monkeypatch.setattr(guest_groups, "component_of",
                        lambda role: {"mtg": "casa/mtg", "finance": "casa/fin"}.get(role))
    monkeypatch.setattr(tools, "_display_name_for_role",
                        lambda role: {"mtg": "Merlin", "finance": "Alex"}.get(role, role))
    store.allow("mtg", guest_groups.ChatRef(-5, "Game Night", "group"), "casa/mtg")
    store.allow("mtg", guest_groups.ChatRef(-6, "Draft Club", "supergroup"), "casa/old")
    store.allow("finance", guest_groups.ChatRef(-7, "Budget", "group"), "casa/fin")
    return store


async def test_list_from_ellens_chat_shows_every_role_and_group(
        two_roles, origin_resident_operator, endpoints):
    mtg_bot, fin_bot = _FakeBot(True), _FakeBot(False)
    endpoints["mtg"] = _endpoint(mtg_bot)
    endpoints["finance"] = _endpoint(fin_bot)
    out = await _call({"action": "list"})
    assert out["status"] == "ok" and out["action"] == "list"
    rows = {(g["role"], g["chat_id"]): g for g in out["groups"]}
    assert set(rows) == {("mtg", -5), ("mtg", -6), ("finance", -7)}
    first = rows[("mtg", -5)]
    assert first == {"role": "mtg", "specialist": "Merlin", "chat_id": -5,
                     "title": "Game Night", "type": "group",
                     "allowed_at": first["allowed_at"], "state": "allowed",
                     "telegram_reports_guest_mode": True}
    assert rows[("mtg", -6)]["state"] == "previous_specialist"
    assert rows[("mtg", -6)]["type"] == "supergroup"
    assert rows[("mtg", -6)]["telegram_reports_guest_mode"] is True
    assert rows[("finance", -7)]["specialist"] == "Alex"
    assert rows[("finance", -7)]["telegram_reports_guest_mode"] is False
    # the hint is asked once per role, not once per group
    assert mtg_bot.calls == 1 and fin_bot.calls == 1


async def test_the_hints_of_different_roles_are_asked_side_by_side(
        two_roles, origin_resident_operator, endpoints):
    """Each bot answers only once every bot has been asked: asked one after
    another, the first would wait out its bound and read ``unknown``."""
    asked: list[str] = []
    everyone = asyncio.Event()

    class _Waiting(_FakeBot):
        def __init__(self, role, supports):
            super().__init__(supports)
            self.role = role

        async def get_me(self):
            asked.append(self.role)
            if len(asked) == 2:
                everyone.set()
            await asyncio.wait_for(everyone.wait(), 1.0)
            return await super().get_me()

    endpoints["mtg"] = _endpoint(_Waiting("mtg", True))
    endpoints["finance"] = _endpoint(_Waiting("finance", False))
    out = await _call({"action": "list"})
    hints = {g["role"]: g["telegram_reports_guest_mode"] for g in out["groups"]}
    assert hints == {"mtg": True, "finance": False}
    assert sorted(asked) == ["finance", "mtg"]


async def test_the_hint_is_unknown_without_an_endpoint_or_on_failure(
        two_roles, origin_resident_operator, endpoints):
    endpoints["mtg"] = _endpoint(_FakeBot(RuntimeError("network")))
    endpoints["finance"] = type("Endpoint", (), {"_app": None})()
    out = await _call({"action": "list"})
    assert {g["telegram_reports_guest_mode"] for g in out["groups"]} == {"unknown"}
    endpoints.clear()
    out = await _call({"action": "list"})
    assert {g["telegram_reports_guest_mode"] for g in out["groups"]} == {"unknown"}


async def test_list_is_empty_when_nothing_is_allowed(store, origin_resident_operator,
                                                     endpoints):
    assert await _call({"action": "list"}) == {
        "status": "ok", "action": "list", "groups": []}


async def test_revoke_from_ellens_chat_needs_role_and_chat_id(
        two_roles, origin_resident_operator, tmp_path):
    bad = {"status": "error", "kind": "bad_arguments",
           "message": "Name the specialist and the group as the list shows them."}
    for args in ({"action": "revoke"}, {"action": "revoke", "role": "mtg"},
                 {"action": "revoke", "chat_id": -5},
                 {"action": "revoke", "role": "mtg", "chat_id": "-5"},
                 {"action": "revoke", "role": "mtg", "chat_id": True}):
        assert await _call(args) == bad
    assert len(two_roles.entries()) == 3
    assert await _call({"action": "revoke", "role": "mtg", "chat_id": -5}) == {
        "status": "ok", "action": "revoke", "removed": True}
    on_disk = guest_groups.GuestStore(tmp_path / "agent-voices-guests.json")
    assert {(e.role, e.chat.id) for e in on_disk.entries()} == {
        ("mtg", -6), ("finance", -7)}
    assert await _call({"action": "revoke", "role": "mtg", "chat_id": -5}) == {
        "status": "ok", "action": "revoke", "removed": False}


async def test_allow_and_status_are_refused_from_ellens_chat(
        store, origin_resident_operator):
    for action in ("allow", "status"):
        out = await _call({"action": action, "role": "mtg", "chat_id": -5})
        assert out == {"status": "error", "kind": "not_in_group",
                       "message": "I can only do that from inside the group concerned."}
    assert store.entries() == []


async def test_list_works_in_the_group_face_too(two_roles, origin_with_guest_chat,
                                                endpoints):
    out = await _call({"action": "list"})
    assert out["status"] == "ok" and out["action"] == "list"
    assert {(g["role"], g["chat_id"]) for g in out["groups"]} == {
        ("mtg", -5), ("mtg", -6), ("finance", -7)}


async def test_a_specialists_dm_turn_cannot_list_or_revoke(
        two_roles, origin_dm, resident_registry):
    for args in ({"action": "list"}, {"action": "revoke", "role": "mtg", "chat_id": -5}):
        out = await _call(args)
        assert out["kind"] == "not_in_group"
    assert len(two_roles.entries()) == 3


async def test_without_a_registry_no_turn_is_the_resident(two_roles, set_origin,
                                                          monkeypatch):
    monkeypatch.setattr(tools, "_agent_registry", None)
    set_origin(_resident_origin())
    assert (await _call({"action": "list"}))["kind"] == "not_in_group"


async def test_a_delegated_run_from_ellens_chat_is_refused(two_roles, set_origin,
                                                           resident_registry):
    set_origin(_resident_origin(execution_role="browser"))
    out = await _call({"action": "revoke", "role": "mtg", "chat_id": -5})
    assert out["kind"] == "not_in_group" and len(two_roles.entries()) == 3


async def test_a_non_operator_resident_turn_is_refused(two_roles, set_origin,
                                                       resident_registry):
    origin = _resident_origin()
    del origin["_operator_turn"]
    set_origin(origin)
    for args in ({"action": "list"}, {"action": "revoke", "role": "mtg", "chat_id": -5}):
        out = await _call(args)
        assert out["status"] == "error" and out["kind"] == "not_operator"
    assert len(two_roles.entries()) == 3
