"""Resident-hosted background-job discovery and prompt rendering."""
from __future__ import annotations

import pytest

import plugin_registry
from agent import _render_jobs_block
from background_jobs import find_job_host, startable_jobs

try:
    from tests.plugin_fixtures import entry, mk_artifact, mk_registry
except ImportError:
    from plugin_fixtures import entry, mk_artifact, mk_registry


pytestmark = pytest.mark.unit
_START_JOB = "mcp__casa-framework__start_job"


def _load_jobs(tmp_path, monkeypatch) -> None:
    store = tmp_path / "store"
    plugin = entry("finance", ["resident:assistant", "specialist:finance"])
    mk_artifact(
        store, "finance", plugin["artifact_id"],
        extra_manifest={"casa": {"jobs": [{
            "name": "classify", "skill": "classify", "title": "Classify transactions",
            "summary": "Classify unreviewed transactions in batches",
            "batches": "unlimited", "turnsPerBatch": 30,
        }]}},
        extra_files={"skills/classify/SKILL.md": "# Classify\n"},
    )
    monkeypatch.setattr(plugin_registry, "_snapshot", None)
    plugin_registry.reload_snapshot(
        registry_path=mk_registry(tmp_path, [plugin]), store_root=store)


def test_resident_job_is_listed_only_for_its_host(tmp_path, monkeypatch) -> None:
    _load_jobs(tmp_path, monkeypatch)

    block = _render_jobs_block(
        "assistant", [], None, live_names={"assistant": "Ellen"},
        allowed_tools=[_START_JOB],
    )

    assert block == (
        "<jobs>\n"
        "- finance:classify — Classify transactions: Classify unreviewed "
        "transactions in batches (runs as a plugin job in Ellen's topic)\n"
        "</jobs>"
    )
    assert _render_jobs_block(
        "butler", [], None, live_names={"butler": "Milo"},
        allowed_tools=[_START_JOB],
    ) == ""


def test_own_job_precedes_delegate_and_wins_duplicate(tmp_path, monkeypatch) -> None:
    _load_jobs(tmp_path, monkeypatch)

    jobs = startable_jobs("assistant", ["finance"])
    assert [(host.kind, host.role, host.decl.qualified_name) for host in jobs] == [
        ("resident", "assistant", "finance:classify"),
    ]
    host = find_job_host("finance:classify", "assistant", ["finance"])
    assert host is not None
    assert host.kind == "resident"
    assert host.plugin.manifest["name"] == "finance"


def test_find_job_host_keeps_specialist_host_kind(tmp_path, monkeypatch) -> None:
    _load_jobs(tmp_path, monkeypatch)

    host = find_job_host("finance:classify", "butler", ["finance"])

    assert host is not None
    assert host.kind == "specialist"
    assert host.role == "finance"


def test_start_job_grant_still_controls_resident_block(tmp_path, monkeypatch) -> None:
    _load_jobs(tmp_path, monkeypatch)

    assert _render_jobs_block(
        "assistant", [], None, live_names={"assistant": "Ellen"}, allowed_tools=[],
    ) == ""
