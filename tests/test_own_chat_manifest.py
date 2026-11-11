"""A specialist's ``own_chat`` declaration and ``session.long_runs``, from
role.yaml through the materialized runtime.yaml to the loaded AgentConfig.

``own_chat`` is optional and only a specialist may carry it; a resident or an
executor declaring it is refused at load. ``session.long_runs`` is the only
name for a role whose requests are each one long conversation: a role still
carrying the retired ``session.desk_context`` key fails validation.
"""
from __future__ import annotations

import copy
from pathlib import Path

import jsonschema
import pytest
import yaml

import agent_loader
import role_artifact
import role_slot
from config import OwnChatConfig, SessionConfig
from persona_pack import PersonaManifest, PersonaPack
from specialist_materialize import (
    _render_specialist_operational_files,
    materialize_specialist_operational_files,
    reconcile_specialist_roles_overlay,
)

OWN_CHAT = {"short_description": "Keeps the household's finances",
            "description": "Ask me about accounts, transactions and the quarterly close."}

_ROLE = {
    "api_version": "casa.role/v1", "id": "specialist:finance", "kind": "specialist",
    "slot": "finance", "mission": "Retrieve and explain household financial records.",
    "enabled": False, "model": {"source": "fixed", "value": "sonnet"},
    "tools": {"allowed": ["Read"], "disallowed": [], "permission_mode": "acceptEdits",
              "max_turns": 10, "skills": "all", "voice_guard": "none"},
    "mcp_servers": ["casa-framework"], "channels": [],
    "memory": {"token_budget": 4000, "read_strategy": "per_turn"},
    "session": {"strategy": "ephemeral", "idle_timeout_seconds": 900},
    "disclosure": {"policy": "delegated", "overrides": {}},
    "delegates": [], "executors": [], "triggers": [], "hooks": {"pre_tool_use": []},
    "tts": {"tag_dialect": "none", "error_phrases": {}},
    "response": {"text": {"register": "precise"}, "voice": {"register": "spoken"},
                 "restricted_webhook": {"register": "plain"}},
    "persona": {"policy": "optional-but-bound", "compatibility": ["casa/alex@>=0.1.0 <1.0.0"]},
    "requires": {"plugins": [], "tools": []},
    "doctrine_file": "doctrine.md",
}

_DOCTRINE = """\
# Core doctrine

Answer only finance-scoped delegations and return a precise result.

## Text projection

Concise prose.

## Voice projection

Lead with the result.

## Restricted webhook projection

Expose nothing.
"""


def _persona() -> PersonaPack:
    return PersonaPack(
        persona_id="casa/alex", version="0.1.0", trait_schema_version=1,
        identity={"display_name": "Alex", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="advisor",
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        quirks=(),
        markdown="# Core\n\nHandles household finances.\n\n## Negative space\n\nNo guesses.\n",
        examples=(), manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )


def _role_dir(root: Path, role: dict) -> Path:
    kind, slot = role["kind"], role["slot"]
    d = root / "image-roles" / kind / slot
    d.mkdir(parents=True)
    (d / "role.yaml").write_text(yaml.safe_dump(role, sort_keys=False), encoding="utf-8")
    (d / "doctrine.md").write_text(_DOCTRINE, encoding="utf-8")
    return d


def _role(**changes) -> dict:
    role = copy.deepcopy(_ROLE)
    for key, value in changes.items():
        if value is None:
            role.pop(key, None)
        else:
            role[key] = value
    return role


def _load(tmp_path: Path, role: dict):
    """role.yaml -> materialized operational files -> the real loader."""
    source = role_artifact.load_role_artifact(_role_dir(tmp_path, role))
    slot = role_slot.materialize_role(source=source, options={})
    agents = tmp_path / "agents-specialists"
    materialize_specialist_operational_files(
        agents_specialists_dir=agents, slug="finance", role=slot, persona=_persona())
    overlay = tmp_path / "overlay"

    class _NoInstalled:
        def installed_component_role_dirs(self):
            return {}

    reconcile_specialist_roles_overlay(
        installed_index=_NoInstalled(), overlay_root=overlay,
        image_roles_dir=str(tmp_path / "image-roles"))
    return agent_loader.load_agent_from_dir(
        str(agents / "finance"), policies=None, roles_dir=str(overlay))


def test_a_specialists_own_chat_block_reaches_the_agent_config(tmp_path: Path) -> None:
    cfg = _load(tmp_path, _role(own_chat=OWN_CHAT))
    assert cfg.own_chat == OwnChatConfig(**OWN_CHAT)


def test_a_specialist_without_own_chat_loads_with_none(tmp_path: Path) -> None:
    cfg = _load(tmp_path, _role())
    assert cfg.own_chat is None
    assert cfg.session.long_runs is False


def test_without_own_chat_the_materialized_runtime_has_no_such_key(tmp_path: Path) -> None:
    source = role_artifact.load_role_artifact(_role_dir(tmp_path, _role()))
    slot = role_slot.materialize_role(source=source, options={})
    runtime = yaml.safe_load(_render_specialist_operational_files(
        slug="finance", role=slot, persona=_persona())["runtime.yaml"])
    assert "own_chat" not in runtime


@pytest.mark.parametrize("kind", ["resident", "executor"])
def test_a_non_specialist_role_declaring_own_chat_is_refused(tmp_path: Path, kind) -> None:
    role = _role(own_chat=OWN_CHAT, kind=kind, id=f"{kind}:finance")
    with pytest.raises(ValueError, match="role schema validation failed"):
        role_artifact.load_role_artifact(_role_dir(tmp_path, role))
    # The same role without the block is admitted, so the refusal is the block's.
    ok = _role(kind=kind, id=f"{kind}:finance")
    role_artifact.load_role_artifact(_role_dir(tmp_path / "ok", ok))


@pytest.mark.parametrize("kind", ["resident", "executor"])
def test_a_non_specialist_runtime_declaring_own_chat_is_refused(kind) -> None:
    runtime = {"schema_version": 1, "kind": kind,
               "model": {"source": "fixed", "value": "sonnet"}}
    agent_loader._validate(runtime, "runtime", "runtime.yaml")
    with pytest.raises(agent_loader.LoadError, match="runtime.yaml: schema violation"):
        agent_loader._validate({**runtime, "own_chat": OWN_CHAT}, "runtime", "runtime.yaml")


@pytest.mark.parametrize("own_chat", [
    {"short_description": "x"},
    {"short_description": "", "description": "y"},
    {"short_description": "x" * 121, "description": "y"},
    {"short_description": "x", "description": "y" * 513},
    {**OWN_CHAT, "avatar": "avatar.jpg"},
])
def test_a_malformed_own_chat_block_is_refused(tmp_path: Path, own_chat) -> None:
    with pytest.raises(ValueError, match="role schema validation failed"):
        role_artifact.load_role_artifact(_role_dir(tmp_path, _role(own_chat=own_chat)))


def test_a_role_carrying_desk_context_is_refused(tmp_path: Path) -> None:
    session = {"strategy": "ephemeral", "idle_timeout_seconds": 0, "desk_context": False}
    with pytest.raises(ValueError, match="role schema validation failed"):
        role_artifact.load_role_artifact(_role_dir(tmp_path, _role(session=session)))
    runtime = {"schema_version": 1, "kind": "specialist",
               "model": {"source": "fixed", "value": "sonnet"},
               "session": {"strategy": "ephemeral", "desk_context": False}}
    with pytest.raises(agent_loader.LoadError):
        agent_loader._validate(runtime, "runtime", "runtime.yaml")


def test_long_runs_reaches_the_session_config(tmp_path: Path) -> None:
    session = {"strategy": "ephemeral", "idle_timeout_seconds": 0, "long_runs": True}
    cfg = _load(tmp_path, _role(session=session))
    assert cfg.session.long_runs is True
    assert cfg.session.idle_timeout == 0


def test_long_runs_is_off_unless_declared() -> None:
    assert SessionConfig().long_runs is False
    assert not hasattr(SessionConfig(), "desk_context")
