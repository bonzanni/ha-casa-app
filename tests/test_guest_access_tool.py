"""The ``guest_access`` tool: the operator, inside a group, lets the specialist
they summoned there answer everyone in it. The group comes from the turn's
server-stamped origin, never from the model's arguments."""
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
    assert schema["properties"]["action"]["enum"] == ["allow", "revoke", "status"]
    assert schema["required"] == ["action"]
