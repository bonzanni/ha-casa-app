"""§3.8: an engagement's plugin_artifacts binding is immutable and survives
every serialization layer (tombstone write + reload)."""
from __future__ import annotations

import time

import pytest

from engagement_registry import EngagementRegistry

pytestmark = pytest.mark.unit

_ARTIFACTS = [
    {"name": "superpowers", "artifact_id": "a" * 64,
     "path": "/config/plugins/store/superpowers/" + "a" * 64},
    {"name": "context7", "artifact_id": "b" * 64,
     "path": "/config/plugins/store/context7/" + "b" * 64},
]


async def test_create_persists_plugin_artifacts_roundtrip(tmp_path):
    path = str(tmp_path / "e.json")
    reg = EngagementRegistry(tombstone_path=path)
    rec = await reg.create(
        "executor", "configurator", "in_casa", "task", {},
        plugin_artifacts=_ARTIFACTS)
    assert rec.plugin_artifacts == tuple(_ARTIFACTS)
    # Reload from disk — the field survives serialization.
    reg2 = EngagementRegistry(tombstone_path=path)
    await reg2.load()
    reloaded = reg2.get(rec.id)
    assert reloaded is not None
    assert list(reloaded.plugin_artifacts) == _ARTIFACTS


async def test_every_mutator_preserves_plugin_artifacts(tmp_path):
    path = str(tmp_path / "e.json")
    reg = EngagementRegistry(tombstone_path=path)
    rec = await reg.create(
        "executor", "configurator", "in_casa", "task", {},
        plugin_artifacts=_ARTIFACTS)
    await reg.mark_idle(rec.id)
    await reg.persist_session_id(rec.id, "sess-xyz")
    await reg.mark_completed(rec.id, completed_at=time.time())
    reg2 = EngagementRegistry(tombstone_path=path)
    await reg2.load()
    reloaded = reg2.get(rec.id)
    assert list(reloaded.plugin_artifacts) == _ARTIFACTS

