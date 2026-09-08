"""Tests for the bidirectional name↔role registry."""

from __future__ import annotations

from agent_registry import AgentRegistry, KnownAgent
from config import AgentConfig, CharacterConfig

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


def _cfg(role: str, name: str, card: str = "") -> AgentConfig:
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        character=CharacterConfig(name=name, card=card),
    )


def test_role_to_name_basic():
    reg = AgentRegistry.build(
        residents={"assistant": _cfg("assistant", "Ellen")},
        specialists={"finance": _cfg("finance", "Alex")},
    )
    assert reg.role_to_name("assistant") == "Ellen"
    assert reg.role_to_name("finance") == "Alex"


def test_name_to_role_case_insensitive():
    reg = AgentRegistry.build(
        residents={"butler": _cfg("butler", "Tina")},
        specialists={},
    )
    assert reg.name_to_role("Tina") == "butler"
    assert reg.name_to_role("tina") == "butler"
    assert reg.name_to_role("TINA") == "butler"


def test_name_to_role_unknown_returns_none():
    reg = AgentRegistry.build(residents={}, specialists={})
    assert reg.name_to_role("nobody") is None


def test_role_to_name_unknown_returns_role_itself():
    """Fallback so prompt rendering never blows up if a config is malformed."""
    reg = AgentRegistry.build(residents={}, specialists={})
    assert reg.role_to_name("ghost") == "ghost"


def test_all_known_returns_residents_and_specialists_with_tier():
    reg = AgentRegistry.build(
        residents={"assistant": _cfg("assistant", "Ellen", card="primary")},
        specialists={"finance": _cfg("finance", "Alex", card="money")},
    )
    known = {k.role: k for k in reg.all_known()}
    assert known["assistant"].name == "Ellen"
    assert known["assistant"].tier == "resident"
    assert known["assistant"].card == "primary"
    assert known["finance"].tier == "specialist"


def test_cross_tier_role_collision_resident_wins(caplog):
    """#343: a specialist sharing a resident's role must NOT silently
    overwrite it — boot refuses this outright (_build_role_registry
    raises), and on reload the delegation map keeps the resident, so the
    registry must agree: resident wins, with a warning."""
    import logging

    with caplog.at_level(logging.WARNING, logger="agent_registry"):
        reg = AgentRegistry.build(
            residents={"butler": _cfg("butler", "Tina")},
            specialists={"butler": _cfg("butler", "Impostor")},
        )
    assert reg.role_to_name("butler") == "Tina"
    assert reg.tier_for_role("butler") == "resident"
    assert any("both tiers" in r.getMessage() for r in caplog.records)


def test_cross_tier_collision_tie_break_agrees_across_both_structures(
        caplog, monkeypatch):
    """#441: the two structures a reload rebuilds — the registry mapping and the
    delegation role map — must pick the SAME winner on a post-boot collision.

    The corpus claimed they picked OPPOSITE winners (registry → specialist,
    delegation map → resident), which stopped being true in v0.143.0 when #343
    added the skip-and-warn guard. Pin the agreement so the corrected prose has
    a test behind it rather than a code comment: a reader who believes the two
    diverge would mis-reason about every role-tier question downstream.
    """
    import logging

    import tools as tools_mod

    # `sync_agent_role_map` writes a MODULE GLOBAL. Restore it so this test
    # cannot leak a two-role map into whatever else shares this worker.
    monkeypatch.setattr(tools_mod, "_agent_role_map",
                        dict(tools_mod._agent_role_map))

    residents = {"butler": _cfg("butler", "Tina")}
    specialists = {"butler": _cfg("butler", "Impostor")}

    class _FakeSpecialistRegistry:
        def all_configs(self):
            return specialists

    runtime = type("_RT", (), {
        "role_configs": residents,
        "specialist_registry": _FakeSpecialistRegistry(),
    })()

    with caplog.at_level(logging.WARNING):
        reg = AgentRegistry.build(residents=residents, specialists=specialists)
        tools_mod.sync_agent_role_map(runtime)

    # Same winner in both, and neither raised.
    assert reg.tier_for_role("butler") == "resident"
    assert reg.role_to_name("butler") == "Tina"
    resident_cfg = residents["butler"]
    assert tools_mod._agent_role_map["butler"] is resident_cfg
    # Both warn — the degradation is silent apart from these two lines.
    warned = [r.getMessage() for r in caplog.records if "both tiers" in r.getMessage()]
    assert len(warned) == 2
    assert any("AgentRegistry.build" in m for m in warned)
    assert any("sync_agent_role_map" in m for m in warned)


# ---------------------------------------------------------------------------
# #439 — the tier authority tools reads must not stay a boot snapshot
# ---------------------------------------------------------------------------


def test_sync_adopts_the_runtimes_current_agent_registry(monkeypatch):
    """``tools._agent_registry`` answers ``tier_for_role``, which picks the
    plugin-assignment TARGET (``resident:<role>`` vs ``specialist:<role>``) for
    a delegation's session build (``tools.py`` ``_build_specialist_options`` /
    ``_prelaunch``).

    It used to be captured once by ``init_tools`` and never re-synced, while
    every reload path rebuilds ``runtime.agent_registry`` immediately before
    calling this. A role added after boot is simply absent from the boot
    snapshot, and the ``or "specialist"`` fallback then resolves a RESIDENT's
    plugins against ``specialist:<role>`` — the delegation launches without the
    plugins that role is actually assigned, for the life of the process.
    """
    import tools as tools_mod

    monkeypatch.setattr(tools_mod, "_agent_role_map",
                        dict(tools_mod._agent_role_map))
    boot = AgentRegistry.build(residents={}, specialists={})
    monkeypatch.setattr(tools_mod, "_agent_registry", boot)

    residents = {"butler": _cfg("butler", "Tina")}
    fresh = AgentRegistry.build(residents=residents, specialists={})
    runtime = type("_RT", (), {
        "role_configs": residents,
        "specialist_registry": type("_SR", (), {
            "all_configs": lambda self: {}})(),
        "agent_registry": fresh,
    })()

    assert boot.tier_for_role("butler") is None      # the stale answer
    tools_mod.sync_agent_role_map(runtime)
    assert tools_mod._agent_registry is fresh
    assert tools_mod._agent_registry.tier_for_role("butler") == "resident"


def test_sync_keeps_the_previous_registry_when_the_runtime_carries_none(
    monkeypatch,
):
    """A runtime stand-in without an ``agent_registry`` must not BLANK the
    capture — losing it would send every tier lookup to the ``or "specialist"``
    fallback, which is strictly worse than a stale answer."""
    import tools as tools_mod

    monkeypatch.setattr(tools_mod, "_agent_role_map",
                        dict(tools_mod._agent_role_map))
    boot = AgentRegistry.build(residents={"butler": _cfg("butler", "Tina")},
                               specialists={})
    monkeypatch.setattr(tools_mod, "_agent_registry", boot)

    runtime = type("_RT", (), {
        "role_configs": {},
        "specialist_registry": type("_SR", (), {
            "all_configs": lambda self: {}})(),
    })()
    tools_mod.sync_agent_role_map(runtime)
    assert tools_mod._agent_registry is boot
