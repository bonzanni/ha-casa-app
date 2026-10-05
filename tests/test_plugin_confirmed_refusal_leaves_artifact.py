"""#1145 — what a CONFIRMED plugin_add / plugin_update refused after the warning leaves.

The warning (INV-TOOL-013) comes before the network phase, so a confirmed call can still
be refused by a check that runs after `plugin_store.publish` — here the tag/version check.
That refusal leaves the registry unchanged and activates nothing, but the artifact it
fetched has already been published into the store (atomic rename + fsync), so it stays
there. These tests pin both halves, so the user docs and the release text say exactly
that and never "nothing changed".

They drive the REAL handlers, a REAL `EngagementRegistry`, and the REAL
`plugin_store.publish` into a temporary store; only the git fetch is replaced (it writes
a minimal plugin tree whose manifest declares version 1.2.0, so ref `v9.9.9` mismatches).
"""
from __future__ import annotations

import functools
import json

import plugin_store
import pytest

from test_plugin_open_conversations import _call, _open, _targets, plug, reg  # noqa: F401

pytestmark = pytest.mark.asyncio


_REAL_PUBLISH = plugin_store.publish     # captured at import, before any test patches it


@pytest.fixture
def real_store(plug, monkeypatch, tmp_path):
    store, staging = tmp_path / "real-store", tmp_path / "real-staging"
    store.mkdir()

    def fake_fetch(repo, commit, subdir, dest):
        (dest / ".claude-plugin").mkdir(parents=True)
        (dest / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "probe", "version": "1.2.0"}), encoding="utf-8")

    monkeypatch.setattr(plugin_store, "fetch_commit_tree", fake_fetch)
    real_publish = functools.partial(_REAL_PUBLISH, store_root=store, staging_root=staging)
    monkeypatch.setattr(plug.tm.plugin_store, "publish", real_publish)
    return store


def _artifacts(store) -> list:
    root = store / "probe"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


async def test_confirmed_update_refused_after_publish_leaves_artifact(reg, plug, real_store):
    _targets(plug, "specialist:fin")
    f1 = await _open(reg, "fin")
    before = plug.snapshot()
    out = await _call(plug, "plugin_update", name="probe", new_ref="v9.9.9",
                      acknowledged_conversations=[f1.id])
    assert out.get("ok") is False and out.get("kind") == "tag_version_mismatch", out
    assert out.get("activation_committed") is False, out
    assert plug.snapshot() == before                       # registry unchanged
    assert (plug.count("save"), len(plug.invalidations)) == (0, 0)
    assert not any(s.startswith("dispatch:") for s in plug.st.log), plug.st.log
    assert len(_artifacts(real_store)) == 1                # the fetched artifact stays


async def test_confirmed_add_refused_after_publish_leaves_artifact(reg, plug, real_store):
    plug.st.raw["plugins"] = []                           # probe not registered yet
    f1 = await _open(reg, "fin")
    before = plug.snapshot()
    out = await _call(plug, "plugin_add", name="probe", repo="o/r", ref="v9.9.9",
                      targets=["specialist:fin"], acknowledged_conversations=[f1.id])
    assert out.get("ok") is False and out.get("kind") == "tag_version_mismatch", out
    assert out.get("activation_committed") is False, out
    assert plug.snapshot() == before                       # registry unchanged
    assert plug.count("save") == 0
    assert not any(s.startswith("dispatch:") for s in plug.st.log), plug.st.log
    assert len(_artifacts(real_store)) == 1                # the fetched artifact stays

