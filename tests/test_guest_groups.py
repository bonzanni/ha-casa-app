"""The guest-group allowance store."""
import json
from types import SimpleNamespace

import guest_groups
from guest_groups import ChatRef, GuestStore

GOOD_ROOT = "casa/mtg@0.9.1#sha256:" + "a" * 64


def test_allow_then_allowed_for_the_same_component(tmp_path, monkeypatch):
    monkeypatch.setattr(guest_groups, "component_of", lambda role: "casa/mtg")
    s = GuestStore(tmp_path / "g.json")
    s.allow("mtg", ChatRef(-5, "Test Group", "group"), "casa/mtg")
    assert s.allowed("mtg", -5, "casa/mtg") is True
    assert s.allowed("mtg", -5, "casa/other") is False
    assert s.allowed("mtg", -5, None) is False
    assert s.allowed("mtg", -6, "casa/mtg") is False
    assert s.status("mtg", -5) == "allowed" and s.status("mtg", -6) == "none"


def test_a_previous_specialists_entry_is_inactive_but_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(guest_groups, "component_of", lambda role: "casa/new")
    s = GuestStore(tmp_path / "g.json")
    s.allow("mtg", ChatRef(-5, "G", "group"), "casa/old")
    assert s.allowed("mtg", -5, "casa/new") is False
    assert s.status("mtg", -5) == "previous_specialist"
    assert [e.chat.id for e in s.entries("mtg")] == [-5]
    assert s.entries("other") == []


def test_revoke_removes_and_reports(tmp_path):
    s = GuestStore(tmp_path / "g.json")
    s.allow("mtg", ChatRef(-5, "G", "group"), "casa/mtg")
    assert s.revoke("mtg", -5) is True
    assert s.revoke("mtg", -5) is False
    assert s.entries() == []


async def test_save_then_reload_round_trips(tmp_path):
    path = tmp_path / "g.json"
    s = GuestStore(path)
    s.allow("mtg", ChatRef(-5, "G", "supergroup"), "casa/mtg")
    s.allow("fin", ChatRef(-7, "H", "group"), "casa/fin")
    await s.save()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert json.loads(path.read_text())["mtg"]["-5"]["type"] == "supergroup"
    assert GuestStore(path).entries() == s.entries()
    assert len(s.entries()) == 2


def test_a_malformed_file_is_moved_aside(tmp_path):
    path = tmp_path / "g.json"
    path.write_text('{"mtg": {"x": {"title": "t"}}}')
    s = GuestStore(path)
    assert s.entries() == []
    assert (tmp_path / "g.json.corrupt").exists() and not path.exists()
    path.write_text("not json")
    GuestStore(path)
    assert (tmp_path / "g.json.corrupt").exists()


def test_component_of_parses_the_encoded_root(monkeypatch):
    def fake(instance):
        monkeypatch.setattr(guest_groups.specialist_registry,
                            "get_installed_instance", lambda role: instance)

    fake(SimpleNamespace(active=SimpleNamespace(root=GOOD_ROOT)))
    assert guest_groups.component_of("mtg") == "casa/mtg"
    fake(None)
    assert guest_groups.component_of("mtg") is None
    fake(SimpleNamespace(active=None))
    assert guest_groups.component_of("mtg") is None
    fake(SimpleNamespace(active=SimpleNamespace(root="garbage")))
    assert guest_groups.component_of("mtg") is None


def test_configure_points_the_singleton_at_a_root(tmp_path):
    guest_groups.configure(tmp_path)
    try:
        assert guest_groups.store().path == tmp_path / "agent-voices-guests.json"
        assert guest_groups.store() is guest_groups.store()
    finally:
        guest_groups.configure(None)
