"""Explore before asking: the configurator's plugin doctrine and the
assistant's install-brief rule (design 2026-09-15 §2.D, §2.E).

On the N150 (2026-09-15) the configurator ended an install with three required
secrets unwired and no vault search, because its recipe said to "return the
candidate items and let the operator choose", its brief told it to report what
the operator still needs to provide, and the assistant then invited the
secrets into the chat. These rules live in shipped prose, so these tests ARE
their enforcement: each pins a whole sentence, collapsed on whitespace, the
same shape ``test_assistant_prompts.py`` uses for the liveness prohibition.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULTS = _ROOT / "casa/rootfs/opt/casa/defaults"
_SECRETS = _DEFAULTS / "agents/executors/configurator/doctrine/recipes/plugin/secrets.md"
_ADD = _DEFAULTS / "agents/executors/configurator/doctrine/recipes/plugin/add.md"
_UPDATE = _DEFAULTS / "agents/executors/configurator/doctrine/recipes/plugin/update.md"
_SPEC_INSTALL = _DEFAULTS / "agents/executors/configurator/doctrine/recipes/specialist/install.md"
_SYSTEM = _DEFAULTS / "agents/assistant/prompts/system.md"
_DOCTRINE = _DEFAULTS / "roles/resident/assistant/doctrine.md"


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _read(path: Path) -> str:
    assert path.is_file(), f"doctrine surface moved: {path}"
    return _collapse_ws(path.read_text(encoding="utf-8"))


# --- the configurator: explore, then wire, then ask ------------------------

_SECRETS_RULES = [
    # the result field is where exploration lands
    "`plugin_add` and `plugin_update` already searched the default vault for you",
    # one match, every var maps to one field → wire
    "exactly one item matches and every unresolved variable maps to exactly one field label, wire it",
    # several / unmappable → ask in the topic with what was found
    "ask in the engagement topic, naming what you found",
    # never ask for a value
    "Never ask the operator for a secret value",
    # nothing → report what was searched
    "report that vault",
]


@pytest.mark.parametrize("rule", _SECRETS_RULES)
def test_secrets_recipe_orders_explore_wire_ask(rule):
    assert rule in _read(_SECRETS), rule


def test_secrets_recipe_no_longer_defers_the_choice_to_the_operator_by_default():
    """The sentence that made asking the default is gone."""
    text = _read(_SECRETS)
    assert "let the operator choose by name" not in text
    assert "Return the candidate items and let the operator" not in text


def test_secrets_recipe_names_the_world_state_as_the_vault_source():
    text = _read(_SECRETS)
    assert "named in your world state" in text
    assert "(see `config.yaml`)" not in text


def test_add_recipe_makes_wiring_a_required_stage():
    text = _read(_ADD)
    assert ("A completion that leaves a required variable unwired without saying "
            "which vault was searched, for what, and what was found is a doctrine "
            "violation") in text
    assert "secret_candidates" in text


# --- the assistant: the brief never makes secrets the operator's job ------

_BRIEF_RULE = ("never make an unwired secret the operator's job")
_RELAY_RULE = ("never state a fact the completion did not state")
_NO_SECRETS_IN_CHAT = ("never ask for a secret value in chat")


def test_system_prompt_carries_the_install_brief_rule():
    text = _read(_SYSTEM)
    assert _BRIEF_RULE in text
    assert _NO_SECRETS_IN_CHAT in text


def test_system_prompt_carries_the_relay_adds_nothing_rule():
    assert _RELAY_RULE in _read(_SYSTEM)


def test_rules_reach_a_persona_bound_assistant():
    """A persona-bound resident never reads system.md (INV-PERS-001); the
    compiled role doctrine replaces it. Asserted on the COMPILED text."""
    from markdown_sections import select_markdown_sections

    doctrine = _DOCTRINE.read_text(encoding="utf-8")
    for sentence in (_BRIEF_RULE, _RELAY_RULE, _NO_SECRETS_IN_CHAT):
        assert sentence in _collapse_ws(doctrine), sentence
        for surface in ("Text projection", "Voice projection",
                        "Restricted webhook projection"):
            selected = select_markdown_sections(
                doctrine, ("Core doctrine", surface),
                exclude=("Text projection", "Voice projection",
                         "Restricted webhook projection"))
            assert sentence in _collapse_ws(selected), (sentence, surface)


# --- the other two recipes that wire secrets say the same (diff r1, D6) -----

def test_update_recipe_makes_wiring_a_required_stage():
    text = _read(_UPDATE)
    assert "wiring them is a REQUIRED stage of this update, not a follow-up" in text
    assert "secret_candidates" in text


def test_specialist_install_asks_for_names_never_values():
    text = _read(_SPEC_INSTALL)
    assert "never for a value" in text
    assert "the default vault is named in your world state" in text
    assert "a vault name is the installer's choice" not in text


def test_secrets_recipe_distinguishes_unreadable_from_nothing_matched():
    text = _read(_SECRETS)
    assert "that is NOT \"nothing matched\"" in text
    assert "report that vault as unreadable" in text
