"""#1085, INV-TOOL-011: a plugin mutation's persist commit, measured on a REAL config repo.

The G-2 guard's plugins-only exemption (``tools.config_git_commit``) was pinned
only with a patched ``config_git.changed_paths`` (tests/test_config_git_commit_tool.py),
so it never saw what a real persist commit carries. Here every commit goes
through the real ``config_git.commit_config_checked`` and the real
``config_git.changed_paths`` on a real repository, the specialists are really
installed (``commit_specialist_install``) and really re-materialised
(``specialist_materialize.current_specialist_roles_dir`` — the one function
boot and every specialist-tier reload route through), and the pre-activation
credit is earned through the real ``plugin_add`` sequencer
(``_reload_and_verify_targets``).

Two thin adapters only: the tool's hard-coded ``/config`` is redirected to the
test repository (the adapters assert they were called with ``/config``), and the
schema validator returns no errors — the fixture repository has no resident
set, and the validator runs on the already-staged index, so it cannot change
which paths a commit carries. The schema gate is pinned in test_config_git.py.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_DISCLOSURE = (Path(__file__).resolve().parent.parent
               / "casa" / "rootfs" / "opt" / "casa" / "defaults" / "policies"
               / "disclosure.yaml")


class _Repo:
    def __init__(self, root: Path, slugs: tuple[str, ...]):
        self.root = root
        self.slugs = slugs
        self.specialists_dir = root / "specialists"
        self.agents_specialists_dir = root / "agents" / "specialists"
        self.registry = root / "plugins" / "registry.json"

    def reconcile(self) -> None:
        import specialist_materialize
        specialist_materialize.current_specialist_roles_dir(
            specialists_dir=self.specialists_dir,
            agents_specialists_dir=self.agents_specialists_dir)

    def targets(self) -> dict[str, str]:
        return {s: os.readlink(self.agents_specialists_dir / s) for s in self.slugs}


def _config_repo(tmp_path: Path, slugs: tuple[str, ...]) -> _Repo:
    import config_git
    from specialist_install import commit_specialist_install
    from test_specialist_lifecycle_matrix import _approved_inspection

    root = tmp_path / "config"
    root.mkdir()
    config_git.init_repo(str(root))
    repo = _Repo(root, slugs)
    (root / "policies").mkdir()
    shutil.copyfile(_DISCLOSURE, root / "policies" / "disclosure.yaml")
    (root / "agents" / "assistant").mkdir(parents=True)
    (root / "agents" / "assistant" / "notes.md").write_text("v1\n", encoding="utf-8")
    for slug in slugs:
        inspection, acks = _approved_inspection(tmp_path, slug=slug)
        instance = commit_specialist_install(
            inspection=inspection, config={}, secret_names_provided=frozenset(),
            acks=acks, specialists_dir=repo.specialists_dir,
            agents_specialists_dir=repo.agents_specialists_dir)
        assert instance.state == "active"
    repo.registry.parent.mkdir(parents=True, exist_ok=True)
    repo.registry.write_text(json.dumps(
        {"schema_version": 1, "seeded_defaults": [], "plugins": []}), encoding="utf-8")
    assert config_git.commit_config(str(root), "fixture baseline")
    return repo


@pytest.fixture
def configurator_origin():
    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        yield
    finally:
        agent_mod.origin_var.reset(tok)


@pytest.fixture
def engagement():
    import tools as tools_mod
    eng = types.SimpleNamespace(id="5" * 32)
    tools_mod._ENGAGEMENTS_PENDING_RELOAD.discard(eng.id)
    tools_mod._ENGAGEMENTS_PREACTIVATED.discard(eng.id)
    tok = tools_mod.engagement_var.set(eng)
    try:
        yield eng
    finally:
        tools_mod.engagement_var.reset(tok)
        tools_mod._ENGAGEMENTS_PENDING_RELOAD.discard(eng.id)
        tools_mod._ENGAGEMENTS_PREACTIVATED.discard(eng.id)


def _state(eng) -> tuple[int, int]:
    import tools as tools_mod
    return (int(eng.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD),
            int(eng.id in tools_mod._ENGAGEMENTS_PREACTIVATED))


def _redirect_commit_tool(monkeypatch, repo: _Repo, *, changed_paths_dir=None):
    import agent_loader
    import config_git

    real_checked = config_git.commit_config_checked
    real_changed = config_git.changed_paths

    def checked(config_dir, message, validate):
        assert config_dir == "/config"
        return real_checked(str(repo.root), message, validate)

    def changed(config_dir, sha):
        assert config_dir == "/config"
        return real_changed(str(changed_paths_dir or repo.root), sha)

    monkeypatch.setattr(config_git, "commit_config_checked", checked)
    monkeypatch.setattr(config_git, "changed_paths", changed)
    monkeypatch.setattr(agent_loader, "validate_config_repo", lambda d: [])
    return real_changed


def _wire_activation(monkeypatch, tmp_path: Path, repo: _Repo, name: str):
    """The real plugin_add sequencer, with test_plugin_tools' transport
    doubles, except: the registry save lands in the real config repo, and a
    specialist-target reload runs the real re-materialisation."""
    import plugin_registry as preg
    import reload as reload_mod
    from test_plugin_tools import _State, _pr, _wire

    st = _State()
    st.raw = json.loads(repo.registry.read_text(encoding="utf-8"))
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name=name))
    wired_save = preg.save_registry

    def save(data, path=None):
        wired_save(data, path)
        repo.registry.write_text(json.dumps(st.raw, indent=2), encoding="utf-8")

    async def dispatch(scope, *, runtime, role=None):
        st.log.append(f"dispatch:{role}")
        if role in repo.slugs:
            await asyncio.to_thread(repo.reconcile)
        return {"status": "ok"}

    monkeypatch.setattr(preg, "save_registry", save)
    monkeypatch.setattr(reload_mod, "dispatch", dispatch)
    return tools_mod, st


async def _activate(monkeypatch, tmp_path, repo, name, target, eng) -> list[str]:
    tools_mod, st = _wire_activation(monkeypatch, tmp_path, repo, name)
    r = await tools_mod.plugin_add.handler({
        "name": name, "repo": "o/r", "ref": "v1", "targets": [target]})
    assert json.loads(r["content"][0]["text"])["ok"] is True
    assert _state(eng)[1] == 1         # the credit was earned by the real sequencer
    return st.log


async def _commit(message: str) -> str:
    from tools import config_git_commit
    r = await config_git_commit.handler({"message": message})
    sha = json.loads(r["content"][0]["text"])["sha"]
    assert sha
    return sha


async def test_plugin_persist_with_two_active_specialists_does_not_arm(
        monkeypatch, tmp_path, configurator_origin, engagement):
    """I-1a. A specialist-target mutation's own reload re-materialises EVERY
    active specialist; unchanged inputs must leave no derived-file change, so
    the persist commit is plugins-only and arms nothing."""
    repo = _config_repo(tmp_path, ("alpha", "beta"))
    real_changed = _redirect_commit_tool(monkeypatch, repo)
    before = repo.targets()

    log = await _activate(monkeypatch, tmp_path, repo, "probe", "specialist:alpha",
                          engagement)
    assert log.count("dispatch:alpha") == 1
    sha = await _commit("persist plugin probe")

    paths = real_changed(str(repo.root), sha)
    changed_targets = sum(repo.targets()[s] != before[s] for s in repo.slugs)
    assert (len(paths), changed_targets, *_state(engagement)) == (1, 0, 0, 0), paths
    assert paths == ["plugins/registry.json"]


async def test_resident_plugin_persist_after_specialist_reconcile_does_not_arm(
        monkeypatch, tmp_path, configurator_origin, engagement):
    """I-1b. Boot reconciles every active specialist AFTER its snapshot
    commit, and every specialist-tier reload does the same. A later
    resident-only mutation's persist commit must not sweep that in — twice
    in a row, so a churn the forced reload would re-create is caught too."""
    repo = _config_repo(tmp_path, ("alpha",))
    real_changed = _redirect_commit_tool(monkeypatch, repo)
    trace = []
    for i in (1, 2):
        repo.reconcile()                       # boot / earlier reload
        await _activate(monkeypatch, tmp_path, repo, f"probe{i}",
                        "resident:assistant", engagement)
        sha = await _commit(f"persist plugin probe{i}")
        paths = real_changed(str(repo.root), sha)
        trace.append((len(paths), sum(p.startswith("agents/") for p in paths),
                      *_state(engagement)))
    assert trace == [(1, 0, 0, 0), (1, 0, 0, 0)]


async def test_mixed_commit_consumes_credit_before_later_plugin_persist(
        monkeypatch, tmp_path, configurator_origin, engagement):
    """I-2. An arming commit spends the credit: after a reload drains the
    obligation, a later plugins-only commit with NO new activation arms."""
    import tools as tools_mod

    repo = _config_repo(tmp_path, ())
    real_changed = _redirect_commit_tool(monkeypatch, repo)
    await _activate(monkeypatch, tmp_path, repo, "probe", "resident:assistant", engagement)

    (repo.root / "agents" / "assistant" / "notes.md").write_text("v2\n", encoding="utf-8")
    first = await _commit("plugin + agent edit")
    trace = [(len(real_changed(str(repo.root), first)), *_state(engagement))]
    tools_mod._ENGAGEMENTS_PENDING_RELOAD.discard(engagement.id)   # a reload drained it

    raw = json.loads(repo.registry.read_text(encoding="utf-8"))
    raw["seeded_defaults"] = ["hand-edited"]
    repo.registry.write_text(json.dumps(raw), encoding="utf-8")
    second = await _commit("registry hand edit")
    trace.append((len(real_changed(str(repo.root), second)), *_state(engagement)))
    assert trace == [(2, 1, 0), (1, 1, 0)]


async def test_changed_paths_failure_arms_and_consumes_credit(
        monkeypatch, tmp_path, configurator_origin, engagement):
    """I-2, fail-safe arm: a changed-paths git failure arms AND spends the
    credit."""
    repo = _config_repo(tmp_path, ())
    _redirect_commit_tool(monkeypatch, repo, changed_paths_dir=tmp_path / "not-a-repo")
    (tmp_path / "not-a-repo").mkdir()
    await _activate(monkeypatch, tmp_path, repo, "probe", "resident:assistant", engagement)
    await _commit("persist plugin probe")
    assert _state(engagement) == (1, 0)
