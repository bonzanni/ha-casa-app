"""Regression guards on butler/prompts/system.md content (B-2, v0.69.5).

Plain string-match assertions on the bundled butler (voice) system prompt,
mirroring test_assistant_prompts.py. The wording is load-bearing for
HA-tool-routing behaviour: a delegated "toggle office light" once looped on
GetLiveContext without acting (2026-07-11 ~23:50Z) because the prompt framed
GetLiveContext as a prerequisite for action and gave no anti-loop guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _butler_md_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / (
        "casa/rootfs/opt/casa/defaults/agents/butler/prompts/system.md"
    )


def _resident_ha_doctrine_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / (
        "casa/rootfs/opt/casa/defaults/"
        "configurator/doctrine/recipes/resident/grant_ha_tools.md"
    )


@pytest.fixture(scope="module")
def butler_md_text() -> str:
    # Collapse whitespace so anchor phrases match regardless of markdown
    # line-wrapping (prose guards must not be brittle to re-flowing).
    import re
    raw = _butler_md_path().read_text(encoding="utf-8")
    return re.sub(r"\s+", " ", raw)


def test_butler_prompt_describes_normalized_state_filter(butler_md_text):
    text = butler_md_text.lower()
    assert "optional domain filter" in text
    assert "forwarded to home assistant" not in text
    assert "at most once per turn" in text


def test_resident_ha_doctrine_matches_normalized_state_filter_contract():
    text = _resident_ha_doctrine_path().read_text(encoding="utf-8").lower()
    assert "optional domain filter" in text
    assert "local" in text
    assert "upstream" in text and "{}" in text
    assert "at most once per turn" in text


def test_butler_prompt_has_getlivecontext_anti_loop_rule(butler_md_text):
    """B-2: an explicit 'at most once per turn' bound on GetLiveContext is
    the load-bearing anti-loop guard. This is the regression this test
    catches if the wording is reverted."""
    text = butler_md_text.lower()
    assert "at most once per turn" in text or "at most one" in text, (
        "butler prompt must bound GetLiveContext to one call per turn"
    )
    # It must forbid re-querying rather than acting.
    assert "never call `getlivecontext` a second time" in text or (
        "do not re-query" in text
    ), "butler prompt must forbid a second GetLiveContext in the same turn"


def test_butler_prompt_says_act_directly_for_actions(butler_md_text):
    """B-2: actions call the action tool directly; GetLiveContext is not a
    prerequisite. Assist resolves entities by name."""
    text = butler_md_text.lower()
    assert "act directly" in text, (
        "butler prompt must tell the model to act directly on action intents"
    )
    # GetLiveContext explicitly framed as a READ tool, not an action precursor.
    assert "read" in text and "getlivecontext" in text


def test_butler_prompt_covers_toggle(butler_md_text):
    """B-2: 'toggle' (the exact verb Ellen delegated) must be in the intent
    guidance — its absence from the action table was part of why the model
    fell back to surveying instead of acting."""
    assert "toggle" in butler_md_text.lower()


def test_butler_runtime_disallows_subagent_spawn():
    """Q-1 (v0.69.8): butler must not spawn sub-agents — Agent/Task in the
    bundled runtime.yaml disallowed list (config-enforced; butler also can't
    Write to change it)."""
    import yaml

    root = Path(__file__).resolve().parent.parent
    rt = root / "casa/rootfs/opt/casa/defaults/agents/butler/runtime.yaml"
    data = yaml.safe_load(rt.read_text(encoding="utf-8"))
    disallowed = data["tools"]["disallowed"]
    assert "Agent" in disallowed and "Task" in disallowed
    # existing restrictions preserved
    assert {"Bash", "Write", "Edit"} <= set(disallowed)
