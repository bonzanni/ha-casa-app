"""Unit coverage for ExecutorMemoryConfig + ExecutorDefinition.memory parsing (M4)."""

from __future__ import annotations

import json
import os

import jsonschema
import pytest

from config import ExecutorDefinition, ExecutorMemoryConfig

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


_SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "casa", "rootfs", "opt", "casa",
    "defaults", "schema", "executor.v1.json",
)


def _load_schema() -> dict:
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _base_defn() -> dict:
    return {
        "schema_version": 1,
        "type": "configurator",
        "description": "Test executor with at least twenty characters.",
        "model": "sonnet",
        "driver": "in_casa",
    }


def test_default_memory_config_disabled():
    cfg = ExecutorMemoryConfig()
    assert cfg.enabled is False
    assert cfg.token_budget == 2000


def test_executor_definition_default_memory_field():
    defn = ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
        type="x", description="x" * 20, model="sonnet", driver="in_casa",
    )
    assert isinstance(defn.memory, ExecutorMemoryConfig)
    assert defn.memory.enabled is False
    assert defn.memory.token_budget == 2000


def test_schema_accepts_definition_without_memory_block():
    """Backward compat: definitions without memory: are valid."""
    jsonschema.validate(_base_defn(), _load_schema())


def test_schema_accepts_memory_disabled():
    d = _base_defn() | {"memory": {"enabled": False}}
    jsonschema.validate(d, _load_schema())


def test_schema_accepts_memory_enabled_with_budget():
    d = _base_defn() | {"memory": {"enabled": True, "token_budget": 2000}}
    jsonschema.validate(d, _load_schema())


def test_schema_rejects_memory_missing_enabled():
    d = _base_defn() | {"memory": {"token_budget": 2000}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(d, _load_schema())


def test_schema_rejects_memory_unknown_property():
    d = _base_defn() | {"memory": {"enabled": True, "huh": 1}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(d, _load_schema())


def test_schema_rejects_token_budget_below_minimum():
    d = _base_defn() | {"memory": {"enabled": True, "token_budget": 50}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(d, _load_schema())


def test_load_all_executors_parses_memory_block(tmp_path):
    """End-to-end: defintion.yaml memory block lands on ExecutorDefinition."""
    from agent_loader import load_all_executors

    base = tmp_path
    exec_dir = base / "executors" / "configurator"
    exec_dir.mkdir(parents=True)
    (exec_dir / "definition.yaml").write_text(
        "schema_version: 1\n"
        "type: configurator\n"
        "description: Configure Casa via the configurator executor.\n"
        "model: claude-sonnet-5\n"
        "driver: in_casa\n"
        "memory:\n"
        "  enabled: true\n"
        "  token_budget: 1500\n",
        encoding="utf-8",
    )
    (exec_dir / "prompt.md").write_text("hello {task}", encoding="utf-8")

    out, _failed = load_all_executors(str(base))
    assert "configurator" in out
    assert out["configurator"].memory.enabled is True
    assert out["configurator"].memory.token_budget == 1500


def test_load_all_executors_defaults_memory_when_block_absent(tmp_path):
    from agent_loader import load_all_executors
    try:
        from tests.test_load_all_executors import _seed_executor_role_artifact
    except ImportError:
        from test_load_all_executors import _seed_executor_role_artifact

    base = tmp_path
    exec_dir = base / "executors" / "smoke"
    exec_dir.mkdir(parents=True)
    (exec_dir / "definition.yaml").write_text(
        "schema_version: 1\n"
        "type: smoke\n"
        "description: Smoke-test executor with no memory configuration here.\n"
        "model: sonnet\n"
        "driver: in_casa\n",
        encoding="utf-8",
    )
    (exec_dir / "prompt.md").write_text("hello {task}", encoding="utf-8")
    roles_dir = str(base / "roles")
    _seed_executor_role_artifact(roles_dir, "smoke")

    out, _failed = load_all_executors(str(base), roles_dir=roles_dir)
    assert out["smoke"].memory.enabled is False
    assert out["smoke"].memory.token_budget == 2000
