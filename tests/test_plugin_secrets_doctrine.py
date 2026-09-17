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
    "exactly one item matches and every unresolved variable maps to exactly one field role, wire it",
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
    # #1020: the vault-search account is owed for a CREDENTIAL; a plain setting
    # is set from the plugin's documentation, never searched for.
    assert ("A completion that leaves a required credential unwired without saying "
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
    assert "never for a secret value" in text
    assert "the default vault is named in your world state" in text
    assert "a vault name is the installer's choice" not in text


def test_secrets_recipe_distinguishes_unreadable_from_nothing_matched():
    text = _read(_SECRETS)
    assert "that is NOT \"nothing matched\"" in text
    assert "report that vault as unreadable" in text


def test_recipes_call_verify_plugin_state_by_its_real_keyword():
    """diff r2 E3: the tool's schema names `plugin_name`; a recipe example
    with `name` is a call the validator refuses."""
    for path in (_ADD, _UPDATE):
        text = _read(path)
        assert "verify_plugin_state(name)" not in text, path.name
        assert "verify_plugin_state(plugin_name=name)" in text, path.name


# --- #1019 / #1020 / #1021 ------------------------------------------------------

def test_secrets_recipe_names_matched_query_and_asks_by_what_the_operator_recognises():
    """#1019: the result field is `matched_query`, never described as a name,
    and a several-matches question describes items by category and field
    roles — never by the search word, never as items sharing a name."""
    text = _read(_SECRETS)
    assert "each named by the query term it matched" not in text
    assert "# name = the term matched" not in text
    assert "{ matched_query, id, category }" in text
    assert "never its title and not its name" in text
    assert ("Describe each item by its category and the roles of its fields — "
            "what the operator can recognise — and never by its `matched_query`") in text
    assert "never two items with the same name" in text
    assert "ids abc123 and def456 — which one?" not in text


def test_secrets_recipe_searches_the_vault_only_for_a_credential():
    """#1020: a plain setting is set from the plugin's documentation or asked
    by what it means, never mapped to a vault item."""
    text = _read(_SECRETS)
    assert "The vault is searched only for a variable that holds a credential" in text
    assert ("A required variable the plugin documents as a plain setting — a vault "
            "name, a host, a region, an environment — is never mapped to a vault item") in text
    install = _read(_SPEC_INSTALL)
    assert "search only for a credential" in install
    # diff r1 (Astra S1): the install flow may ask a plain setting by meaning;
    # the item-or-field-only rule is scoped to credentials.
    assert "ask the operator for it by what it means" in install
    assert "ask the operator only for an item or field name you could not settle" not in install
    assert ("For a credential, the default vault is named in your world state, so "
            "search it") in install
    # diff r1 (Astra Q4): an undocumented variable is searched as a credential.
    assert ("when they do not say, treat the variable as a credential and search "
            "for it") in text
    add = _read(_ADD)
    assert "leaves a required credential unwired without saying which vault was searched" in add
    assert "a required plain setting is set from the plugin's documentation" in add


def test_a_setup_provided_value_is_the_configurators_to_wire():
    """#1021: nothing wires a casa.setupProvides value on its own; the setup
    tool reports it and the configurator wires it."""
    text = _read(_SECRETS)
    assert "## A value the plugin's setup tool reports (`casa.setupProvides`)" in text
    assert "Nothing wires it on its own" in text
    assert "wiring it is your job" in text
    assert "never report it as something no configurator can do" in text
    install = _read(_SPEC_INSTALL)
    assert "is filled in by the plugin's setup episode" not in install
    assert "A setup-provisioned name is NOT filled in by itself" in install
    assert "wiring it is a configurator's job then" in install
    # diff r1 (Astra S1): the Common-mistakes exclusion no longer covers a
    # setup-provided name once its setup run has reported it.
    assert "they are not the installer's to wire." not in install
    assert "once the plugin's setup run reports its value, wiring it IS a configurator's job" in install


_DEVDOC = (_DEFAULTS / "agents/executors/plugin-developer/doctrine/casa-conventions.md")
_USER_DOCS = _ROOT / "casa/DOCS.md"


def test_the_user_docs_split_credentials_from_plain_settings():
    """#1020 (diff round 4, Terra S1): the shipped user documentation is the
    fourth surface that described every required variable as a 1Password
    reference the operator is asked for."""
    text = _read(_USER_DOCS)
    assert "asks for a 1Password reference (`op://…`) for each" not in text
    assert "A **credential** is searched for in your default vault" in text
    assert ("A **plain setting** — a vault name, a host, a region — is taken from the "
            "plugin's documentation") in text
    assert "is never mapped to a vault item" in text
    assert "the setup run reports what to wire and the configurator wires it" in text


def test_the_plugin_developer_doctrine_splits_credentials_from_plain_settings():
    """#1020 (diff round 3, Terra S1): the plugin author's own shipped guidance
    is a third surface that described every required variable as a 1Password
    reference the user is asked for."""
    text = _read(_DEVDOC)
    assert "the configurator asks the user for a 1P reference" not in text
    assert "a CREDENTIAL is searched for in the default 1Password vault" in text
    assert ("A PLAIN SETTING (a vault name, a host, a region) is set from your plugin's "
            "documentation") in text
    assert "it is never mapped to a vault item" in text
    # #1021 on the same surface: a setup-provided value has a writer.
    assert "nothing fills it in by itself" in text
    assert "the configurator wires it" in text


_SETUP_REPORT_RULE = ("When a setup tool's result names values for the configurator to "
                      "wire, engage the configurator with that report and relay what it "
                      "did; the setup run does not wire them itself.")


def test_the_assistant_hands_a_setup_report_to_the_configurator_on_every_surface():
    """#1021: the resident rule, in the unbound prompt and in the compiled role
    doctrine a persona-bound assistant reads (INV-PERS-001), on every
    projection."""
    from markdown_sections import select_markdown_sections

    assert _SETUP_REPORT_RULE in _read(_SYSTEM)
    doctrine = _DOCTRINE.read_text(encoding="utf-8")
    for surface in ("Text projection", "Voice projection",
                    "Restricted webhook projection"):
        selected = select_markdown_sections(
            doctrine, ("Core doctrine", surface),
            exclude=("Text projection", "Voice projection",
                     "Restricted webhook projection"))
        assert _SETUP_REPORT_RULE in _collapse_ws(selected), surface


def test_secrets_recipe_never_speaks_of_labels():
    """diff r4 G3 / r5 H2: the cut returns ids and roles; a recipe sentence or
    example that routes the configurator through labels or titles reintroduces
    the echo. Every example op:// path is id-shaped."""
    text = _read(_SECRETS)
    assert "field label" not in text
    assert "a label works" not in text
    assert "id-or-title" not in text
    assert "op://Casa/" not in text
    assert "op://<vault>/<item id>/<field id>" in text
