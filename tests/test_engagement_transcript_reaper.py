"""#1162: a finished in_casa engagement's CLI transcripts are deleted.

Casa owns deleting the transcripts of the SDK sessions it runs — the CLI's
own ``cleanupPeriodDays`` sweep never fires under Casa's SDK invocation
(``session_sweeper.py``) — but before #1162 only resident sessions were ever
reaped. Every path here is seeded through the SDK's public cwd → project-key
mapping under a tmp ``HOME``, never re-typed, so a naming drift cannot leave
these tests green while nothing is deleted.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk import project_key_for_directory

from engagement_registry import EngagementRegistry


def _projects(home: Path) -> Path:
    return home / ".claude" / "projects"


def _project_dir(home: Path, cwd: str) -> Path:
    return _projects(home) / project_key_for_directory(cwd)


def _seed_session(project: Path, sid: str, *, body: str = '{"type":"user"}\n',
                  sibling: bool = True) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{sid}.jsonl").write_text(body, encoding="utf-8")
    if sibling:
        results = project / sid / "tool-results"
        results.mkdir(parents=True, exist_ok=True)
        (results / "result.txt").write_text("payload", encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return h


def _specialists(monkeypatch, **cfgs) -> None:
    """Install a specialist registry stub on ``tools`` — the lookup the
    launch and resume builders use."""
    import tools

    class _Reg:
        def get(self, role):
            return cfgs.get(role)

    monkeypatch.setattr(tools, "_specialist_registry", _Reg())


async def _run_periodic_passes(registry, tmp_path: Path, monkeypatch) -> None:
    """The periodic passes casa_core.main schedules over terminal engagement
    artifacts: the workspace sweep, then — once it exists — the transcript
    reaper. At the base the reaper module is absent and only the sweep runs."""
    from drivers import workspace

    engagements_root = tmp_path / "engagements"
    engagements_root.mkdir(exist_ok=True)
    monkeypatch.setattr(workspace, "CONTROL_ROOT", str(tmp_path / "ctl"))
    await workspace._sweep_workspaces(
        engagements_root=str(engagements_root), log_root=str(tmp_path / "logs"))
    if importlib.util.find_spec("engagement_transcript_reaper") is not None:
        reaper = importlib.import_module("engagement_transcript_reaper")
        await reaper.reap_engagement_transcripts(registry)


async def _terminal(registry, rec_id: str, outcome: str) -> None:
    if outcome == "completed":
        await registry.mark_completed(rec_id, time.time())
    elif outcome == "cancelled":
        await registry.mark_cancelled(rec_id)
    else:
        await registry.mark_error(rec_id, "test", "boom")


@pytest.mark.parametrize("outcome", ["completed", "cancelled", "error"])
@pytest.mark.parametrize("specialist_cwd", ["", "/config/custom-home/researcher"])
async def test_terminal_sdk_transcripts_are_reaped(
        home, tmp_path, monkeypatch, outcome, specialist_cwd):
    """Red case (#1162). A terminal plugin job's whole project dir, and every
    session a terminal specialist-hosted job names, are gone after the
    periodic passes — at the base all five targets survive."""
    tombstone = tmp_path / "engagements.json"
    registry = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    _specialists(monkeypatch, researcher=SimpleNamespace(
        role="researcher", cwd=specialist_cwd))

    p, s1, s2 = (str(uuid.uuid4()) for _ in range(3))
    plugin = await registry.create(
        kind="plugin", role_or_type="researcher", driver="in_casa",
        task="t", origin={}, topic_id=None)
    job = await registry.create(
        kind="specialist", role_or_type="researcher", driver="in_casa",
        task="t", origin={"job": {"sids": [s1, s2]}}, topic_id=None)
    await registry.persist_session_id(plugin.id, p)
    await registry.persist_session_id(job.id, s1)
    await _terminal(registry, plugin.id, outcome)
    await _terminal(registry, job.id, outcome)

    rows = {r["id"]: r for r in json.loads(tombstone.read_text())}
    assert {rows[plugin.id]["status"], rows[job.id]["status"]} == {outcome}
    assert (rows[plugin.id]["sdk_session_id"], rows[job.id]["sdk_session_id"]) == (p, s1)
    assert rows[job.id]["origin"]["job"]["sids"] == [s1, s2]
    for rid in (plugin.id, job.id):
        assert time.time() - rows[rid]["completed_at"] < 30 * 86400

    plugin_project = _project_dir(
        home, f"/data/engagements/{plugin.id}/plugin-job")
    specialist_project = _project_dir(
        home, specialist_cwd or "/config/agent-home/researcher")
    _seed_session(plugin_project, p)
    _seed_session(plugin_project, str(uuid.uuid4()), body="", sibling=False)
    _seed_session(plugin_project, str(uuid.uuid4()), sibling=False)
    _seed_session(specialist_project, s1)
    _seed_session(specialist_project, s2)
    seeded = [f for f in (*plugin_project.rglob("*"),
                          *specialist_project.rglob("*")) if f.is_file()]
    assert len(seeded) == 8
    (tmp_path / "engagements" / plugin.id / "plugin-job").mkdir(parents=True)

    await _run_periodic_passes(registry, tmp_path, monkeypatch)

    survivors = [
        path for path in (
            plugin_project,
            specialist_project / f"{s1}.jsonl",
            specialist_project / s1,
            specialist_project / f"{s2}.jsonl",
            specialist_project / s2,
        ) if path.exists()
    ]
    assert len(survivors) == 0, survivors
