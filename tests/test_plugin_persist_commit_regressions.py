"""#1085 regressions around INV-TOOL-011, on the same real config repo as the
red cases in test_plugin_persist_commit_real_repo.py (whose helpers this
reuses): the exemption must stay confined to plugins-only persists."""
from __future__ import annotations

import json

import pytest

from test_plugin_persist_commit_real_repo import (  # noqa: F401 — fixtures
    _activate, _commit, _config_repo, _redirect_commit_tool, _state,
    configurator_origin, engagement,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("edit", ["through_symlink", "content_dir_file", "new_file"])
async def test_a_hand_edit_under_agents_specialists_still_arms(
        monkeypatch, tmp_path, configurator_origin, engagement, edit):
    """C-O1: skipping the unchanged re-materialisation must not hide a
    configurator's hand edit of a specialist's operational files from the
    guard — with the credit present, a plugins+agents persist arms."""
    import os
    repo = _config_repo(tmp_path, ("alpha",))
    real_changed = _redirect_commit_tool(monkeypatch, repo)
    await _activate(monkeypatch, tmp_path, repo, "probe", "specialist:alpha", engagement)
    link = repo.agents_specialists_dir / "alpha"
    content = repo.agents_specialists_dir / os.readlink(link)
    if edit == "through_symlink":
        (link / "runtime.yaml").write_text(
            (link / "runtime.yaml").read_text() + "# hand\n", encoding="utf-8")
    elif edit == "content_dir_file":
        (content / "voice.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    else:
        (repo.root / "agents" / "assistant" / "extra.md").write_text("x\n", encoding="utf-8")
    sha = await _commit("persist plugin + hand edit")
    paths = real_changed(str(repo.root), sha)
    assert (sum(p.startswith("agents/") for p in paths) >= 1, *_state(engagement)) == (True, 1, 0)


async def test_an_exempt_persist_never_clears_an_existing_obligation(
        monkeypatch, tmp_path, configurator_origin, engagement):
    import tools as tools_mod
    repo = _config_repo(tmp_path, ())
    _redirect_commit_tool(monkeypatch, repo)
    tools_mod._ENGAGEMENTS_PENDING_RELOAD.add(engagement.id)   # an earlier arming commit
    await _activate(monkeypatch, tmp_path, repo, "probe", "resident:assistant", engagement)
    await _commit("persist plugin probe")
    assert _state(engagement) == (1, 0)


async def test_an_empty_commit_keeps_the_credit(
        monkeypatch, tmp_path, configurator_origin, engagement):
    """C-O3: an empty commit changes nothing — the credit survives it and the
    later persist is still exempt."""
    from tools import config_git_commit
    repo = _config_repo(tmp_path, ())
    _redirect_commit_tool(monkeypatch, repo)
    await _activate(monkeypatch, tmp_path, repo, "probe", "resident:assistant", engagement)
    import config_git
    assert config_git.commit_config(str(repo.root), "persist out of band")
    r = await config_git_commit.handler({"message": "nothing left"})
    assert json.loads(r["content"][0]["text"])["sha"] == ""
    assert _state(engagement) == (0, 1)
