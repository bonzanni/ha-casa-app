"""Agent-home provisioning (unified plugin architecture v0.71.0): settings.json
is created for hooks + user edits, but NO enabledPlugins is seeded — plugin
assignment is the registry's job. Pre-existing user data is never deleted."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_provision_creates_settings_without_enabled_plugins(tmp_path: Path) -> None:
    from agent_home import provision_agent_home

    home_root = tmp_path / "agent-home"
    provision_agent_home(role="ellen", home_root=home_root, defaults_root=tmp_path)
    settings = json.loads(
        (home_root / "ellen" / ".claude" / "settings.json").read_text())
    assert settings == {}
    assert "enabledPlugins" not in settings


def test_provision_preserves_user_enabledplugins_verbatim(tmp_path: Path) -> None:
    """A stale enabledPlugins key from an older deploy is user data — never
    deleted or mutated (nothing reads it anymore)."""
    from agent_home import provision_agent_home

    home_root = tmp_path / "agent-home"
    settings_path = home_root / "ellen" / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "enabledPlugins": {"user-custom@casa-plugins": True},
        "hooks": {"PreToolUse": []},
    }), encoding="utf-8")

    provision_agent_home(role="ellen", home_root=home_root, defaults_root=tmp_path)
    settings = json.loads(settings_path.read_text())
    assert settings["enabledPlugins"] == {"user-custom@casa-plugins": True}
    assert settings["hooks"] == {"PreToolUse": []}


@pytest.mark.parametrize("payload", ["null", "[]", '"x"', "42"])
def test_provision_recreates_non_object_settings(tmp_path: Path, payload: str,
                                                 caplog) -> None:
    """Valid-but-non-object JSON self-heals to an empty object (no enabledPlugins
    seeding), not an AttributeError."""
    import logging
    from agent_home import provision_agent_home

    home_root = tmp_path / "agent-home"
    settings_path = home_root / "ellen" / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(payload, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent_home"):
        provision_agent_home(role="ellen", home_root=home_root,
                             defaults_root=tmp_path)
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings == {}
    assert any("not a JSON object" in r.message for r in caplog.records)


def test_provision_preserves_unparseable_settings(tmp_path: Path, caplog) -> None:
    """#355: a settings.json that does not PARSE may be the truncated remains
    of real user settings (a crash mid-rewrite, an interrupted edit) —
    provisioning must leave the bytes in place for repair, never replace
    them with {}."""
    import logging
    from agent_home import provision_agent_home

    home_root = tmp_path / "agent-home"
    settings_path = home_root / "ellen" / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt = '{"hooks": {"PreToolUse": ['   # truncated mid-write
    settings_path.write_text(corrupt, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent_home"):
        provision_agent_home(role="ellen", home_root=home_root,
                             defaults_root=tmp_path)
    assert settings_path.read_text(encoding="utf-8") == corrupt
    assert any("preserving" in r.message for r in caplog.records)


def test_provision_write_replaces_atomically(tmp_path: Path,
                                             monkeypatch) -> None:
    """#355: the rewrite must go through a same-dir temp file + os.replace so
    a crash mid-write can never leave a truncated settings.json behind."""
    from agent_home import provision_agent_home

    home_root = tmp_path / "agent-home"
    settings_path = home_root / "ellen" / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    original = '{"hooks": {}}'
    settings_path.write_text(original, encoding="utf-8")

    def _boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("agent_home.os.replace", _boom)
    with pytest.raises(OSError):
        provision_agent_home(role="ellen", home_root=home_root,
                             defaults_root=tmp_path)
    # The prior valid content is untouched and no temp litter remains.
    assert settings_path.read_text(encoding="utf-8") == original
    assert [p.name for p in settings_path.parent.iterdir()] == ["settings.json"]


def test_provision_syncs_file_before_replace_and_directory_after(
        tmp_path: Path, monkeypatch) -> None:
    """#1689: the temp file's data reaches disk before the replace and the
    rename itself is made durable by a directory fsync after it."""
    import os
    import stat
    from agent_home import provision_agent_home

    home_root = tmp_path / "agent-home"
    claude_dir = home_root / "ellen" / ".claude"
    events: list = []
    real_fsync, real_replace = os.fsync, os.replace

    def _fsync(fd):
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            events.append(("fsync-dir", os.readlink(f"/proc/self/fd/{fd}")))
        else:
            events.append(("fsync-file",))
        real_fsync(fd)

    def _replace(src, dst):
        events.append(("replace", os.path.basename(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(os, "replace", _replace)
    provision_agent_home(role="ellen", home_root=home_root,
                         defaults_root=tmp_path)
    monkeypatch.undo()

    assert events == [("fsync-file",), ("replace", "settings.json"),
                      ("fsync-dir", str(claude_dir))]


def test_provision_preserves_existing_file_mode(tmp_path: Path) -> None:
    """Terra r1 (#355): replacing via a fresh temp file must not widen an
    operator-tightened mode (0600 settings must stay 0600)."""
    import os
    import stat
    from agent_home import provision_agent_home

    home_root = tmp_path / "agent-home"
    settings_path = home_root / "ellen" / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text('{"hooks": {}}', encoding="utf-8")
    os.chmod(settings_path, 0o600)

    provision_agent_home(role="ellen", home_root=home_root,
                         defaults_root=tmp_path)
    assert stat.S_IMODE(os.stat(settings_path).st_mode) == 0o600


def test_provision_serialization_failure_leaves_no_temp_litter(
        tmp_path: Path, monkeypatch) -> None:
    """Terra r1 (#355): a failure while WRITING the temp file (not only at
    replace time) must clean the temp up and leave the original intact."""
    from agent_home import provision_agent_home

    home_root = tmp_path / "agent-home"
    settings_path = home_root / "ellen" / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    original = '{"hooks": {}}'
    settings_path.write_text(original, encoding="utf-8")

    def _boom(*a, **kw):
        raise OSError("simulated serialization failure")

    monkeypatch.setattr("agent_home.json.dumps", _boom)
    with pytest.raises(OSError):
        provision_agent_home(role="ellen", home_root=home_root,
                             defaults_root=tmp_path)
    assert settings_path.read_text(encoding="utf-8") == original
    assert [p.name for p in settings_path.parent.iterdir()] == ["settings.json"]


def test_provision_all_homes_includes_residents_and_specialists(tmp_path: Path) -> None:
    """E-4 regression: residents AND specialists must each get an agent-home."""
    from agent_home import provision_all_homes

    home_root = tmp_path / "agent-home"
    provision_all_homes(
        role_configs={"assistant": object(), "butler": object()},
        specialist_configs={"finance": object()},
        home_root=home_root, defaults_root=tmp_path)
    for role in ("assistant", "butler", "finance"):
        settings = home_root / role / ".claude" / "settings.json"
        assert settings.is_file(), f"missing {role} settings.json"
        assert json.loads(settings.read_text(encoding="utf-8")) == {}


def test_provision_all_homes_excludes_executors(tmp_path: Path) -> None:
    from agent_home import provision_all_homes

    home_root = tmp_path / "agent-home"
    provision_all_homes(role_configs={"assistant": object()},
                        specialist_configs={}, home_root=home_root,
                        defaults_root=tmp_path)
    assert (home_root / "assistant").is_dir()
    assert not (home_root / "configurator").exists()
    assert not (home_root / "finance").exists()


def test_provision_all_homes_continues_on_individual_failure(
        tmp_path: Path, caplog) -> None:
    """One role failing its provisioning step must not block the others
    (per-role try/except inside provision_all_homes)."""
    import logging
    from agent_home import provision_all_homes

    home_root = tmp_path / "agent-home"
    # Make butler's agent-home path a FILE so .claude/ can't be created.
    (home_root).mkdir(parents=True, exist_ok=True)
    (home_root / "butler").write_text("not a dir", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent_home"):
        provision_all_homes(
            role_configs={"assistant": object(), "butler": object()},
            specialist_configs={}, home_root=home_root, defaults_root=tmp_path)

    assert (home_root / "assistant" / ".claude" / "settings.json").is_file()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("butler" in r.message for r in warnings), (
        f"expected a warning naming butler, got: {[r.message for r in warnings]}")
