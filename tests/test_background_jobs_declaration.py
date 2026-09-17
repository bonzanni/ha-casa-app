"""Background-job declaration, discovery, and resident prompt coverage."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import plugin_registry
from agent import _render_jobs_block
from agent_registry import AgentRegistry
from background_jobs import find_job_host
from config import AgentConfig, CharacterConfig, DelegateEntry, MemoryConfig, ToolsConfig
from plugin_store import StoreError, validate_manifest

try:
    from tests.plugin_fixtures import entry, mk_artifact, mk_registry
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from plugin_fixtures import entry, mk_artifact, mk_registry
    from role_artifact_stub import STUB_ROLE_ARTIFACT


pytestmark = pytest.mark.unit

_START_JOB = "mcp__casa-framework__start_job"


def _job(**overrides):
    job = {
        "name": "classify",
        "skill": "classify",
        "title": "Classify transactions",
        "summary": "Classify unreviewed transactions in batches",
        "batches": "unlimited",
        "turnsPerBatch": 30,
    }
    job.update(overrides)
    return job


def _manifest_tree(tmp_path: Path, job: dict) -> Path:
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / "skills" / "classify").mkdir(parents=True)
    (root / "skills" / "classify" / "SKILL.md").write_text(
        "# Skill\n", encoding="utf-8")
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "finance", "version": "1.0.0", "casa": {"jobs": [job]},
    }), encoding="utf-8")
    return root


@pytest.mark.parametrize("job", [
    _job(batches=True),
    _job(extra="nope"),
    _job(skill=None),
])
def test_validate_manifest_refuses_invalid_job_declarations(tmp_path, job):
    with pytest.raises(StoreError) as exc:
        validate_manifest(_manifest_tree(tmp_path, job), "finance")
    assert exc.value.reason_code == "jobs_invalid"


def test_validate_manifest_refuses_job_without_skill_file(tmp_path):
    with pytest.raises(StoreError) as exc:
        validate_manifest(_manifest_tree(tmp_path, _job(skill="missing")), "finance")
    assert exc.value.reason_code == "jobs_invalid"


def _load_jobs(tmp_path, monkeypatch):
    store = tmp_path / "store"
    plugin = entry("finance", ["specialist:finance", "specialist:hidden"])
    mk_artifact(
        store, "finance", plugin["artifact_id"],
        extra_manifest={"casa": {"jobs": [_job()]}},
        extra_files={"skills/classify/SKILL.md": "# Classify\n"},
    )
    monkeypatch.setattr(plugin_registry, "_snapshot", None)
    plugin_registry.reload_snapshot(
        registry_path=mk_registry(tmp_path, [plugin]), store_root=store)


def _cfg(role: str, name: str, *, delegates=(), allowed=()):
    return AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name=name),
        tools=ToolsConfig(allowed=list(allowed), permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=0, read_strategy="per_turn"),
        delegates=list(delegates),
    )


def _agent_registry(assistant):
    return AgentRegistry.build(
        residents={"assistant": assistant},
        specialists={
            "finance": _cfg("finance", "Alex"),
            "hidden": _cfg("hidden", "Hannah"),
        },
    )


def test_delegatable_job_renders_and_non_delegate_job_does_not(tmp_path, monkeypatch):
    _load_jobs(tmp_path, monkeypatch)
    assistant = _cfg(
        "assistant", "Ellen",
        delegates=[DelegateEntry(agent="finance", purpose="Money.",
                                 when="Asked about money.")],
    )
    block = _render_jobs_block(
        assistant.delegates, _agent_registry(assistant), allowed_tools=[_START_JOB])
    assert block == (
        "<jobs>\n"
        "- finance:classify — Classify transactions: Classify unreviewed "
        "transactions in batches (runs in Alex's topic)\n"
        "</jobs>"
    )
    assert "Hannah" not in block


def test_find_job_host_uses_delegate_order(tmp_path, monkeypatch):
    _load_jobs(tmp_path, monkeypatch)
    host = find_job_host("finance:classify", ["hidden", "finance"])
    assert host is not None
    assert host[0] == "hidden"


def test_resident_without_start_job_does_not_get_jobs_block(tmp_path, monkeypatch):
    _load_jobs(tmp_path, monkeypatch)
    assistant = _cfg(
        "assistant", "Ellen",
        delegates=[DelegateEntry(agent="finance", purpose="Money.",
                                 when="Asked about money.")],
        allowed=[],
    )
    assert _render_jobs_block(
        assistant.delegates, _agent_registry(assistant), allowed_tools=[]
    ) == ""
