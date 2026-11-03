"""Pinning tests for turn-loop/overview/agent-taxonomy invariants (docs corpus).

Each test names the corpus invariant it pins and records, in its docstring, the
red case that was demonstrated: the code edit that made it fail. A pinning test
never shown red proves nothing.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_loader import LoadError, TIER_FILES, _check_file_set
from agent_registry import AgentRegistry


def test_pin_inv_sys_001_materialisation_depends_on_validation():
    """INV-SYS-001: config materialisation depends on config validation, so
    the validating one-shot runs first and its failure stops what depends on
    it. s6 reads exactly this dependency directory.

    Red case demonstrated: deleting the init-validate-config dependency file
    fails this test.
    """
    root = Path(__file__).resolve().parents[1]
    dependencies = (
        root / "casa/rootfs/etc/s6-overlay/s6-rc.d"
        / "init-setup-configs/dependencies.d"
    )
    assert sorted(p.name for p in dependencies.iterdir()) == [
        "init-memory-maintenance", "init-validate-config",
    ]


def test_pin_inv_agent_002_strict_file_set_for_residents_and_specialists(tmp_path):
    """INV-AGENT-002: _check_file_set refuses a missing required, forbidden,
    or unrecognised file for residents and specialists.

    Red case demonstrated: deleting the unknown-files rejection block fails
    the unrecognised-file half of this test.
    """
    for tier in ("resident", "specialist"):
        required = TIER_FILES[tier]["required"]
        agent_dir = tmp_path / f"{tier}-missing"
        agent_dir.mkdir()
        for name in sorted(required)[1:]:
            (agent_dir / name).touch()
        with pytest.raises(LoadError, match="missing required"):
            _check_file_set(str(agent_dir), tier, tier)

        agent_dir = tmp_path / f"{tier}-unknown"
        agent_dir.mkdir()
        for name in required:
            (agent_dir / name).touch()
        (agent_dir / "unrecognised.yaml").touch()
        with pytest.raises(LoadError, match="unknown file"):
            _check_file_set(str(agent_dir), tier, tier)

    agent_dir = tmp_path / "specialist-forbidden"
    agent_dir.mkdir()
    for name in TIER_FILES["specialist"]["required"]:
        (agent_dir / name).touch()
    (agent_dir / "disclosure.yaml").touch()
    with pytest.raises(LoadError, match="forbidden file"):
        _check_file_set(str(agent_dir), "specialist", "finance")


def test_pin_inv_agent_004_registry_performs_no_filesystem_access(monkeypatch):
    """INV-AGENT-004: the registry is an index over already-loaded
    configuration; building and querying it opens no files.

    Red case demonstrated: adding an open() call to AgentRegistry.build fails
    this test.
    """
    def fail_open(*_args, **_kwargs):
        raise AssertionError("registry must not access the filesystem")

    monkeypatch.setattr("builtins.open", fail_open)

    def cfg(name):
        return SimpleNamespace(character=SimpleNamespace(name=name, card=""))

    registry = AgentRegistry.build(
        residents={"assistant": cfg("Ellen")},
        specialists={"finance": cfg("Alex")},
    )
    assert registry.role_to_name("assistant") == "Ellen"
    assert registry.name_to_role("alex") == "finance"
    assert registry.tier_for_role("finance") == "specialist"
