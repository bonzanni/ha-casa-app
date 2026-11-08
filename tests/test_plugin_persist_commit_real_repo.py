"""#1436: a plugin mutation commits its own registry write, on a REAL config repo.

Every commit goes through the real ``config_git`` functions on a real
repository, and the mutation runs through the real ``plugin_add`` sequencer
(``_reload_and_verify_targets``) with tests/test_plugin_tools.py's transport
doubles. ``_redirect_commit_tool`` is shared with
tests/test_config_git_commit_reload.py: the commit tool's hard-coded
``/config`` is redirected to the test repository (the adapters assert they
were called with ``/config``), and the schema validator returns no errors —
the schema gate is pinned in test_config_git.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
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


# ---------------------------------------------------------------------------
# #1436: the plugin mutation commits the registry itself
# ---------------------------------------------------------------------------

def _git(repo: _Repo, *args: str) -> str:
    import subprocess
    return subprocess.check_output(
        ["git", "-C", str(repo.root), *args], text=True).strip()


async def test_plugin_mutation_commits_its_registry_write_itself(
        monkeypatch, tmp_path, configurator_origin):
    """#1436: a fully activated mutation leaves /config clean, in one commit
    of its own that holds only the registry, and leaves an unrelated edit
    for the configurator's own commit."""
    import config_git
    import plugin_registry as preg

    repo = _config_repo(tmp_path, ())
    monkeypatch.setattr(config_git, "CONFIG_DIR", str(repo.root))
    monkeypatch.setattr(preg, "REGISTRY_PATH", repo.registry)
    (repo.root / "agents" / "assistant" / "notes.md").write_text(
        "unrelated\n", encoding="utf-8")
    base = _git(repo, "rev-parse", "HEAD")
    tools_mod, _ = _wire_activation(monkeypatch, tmp_path, repo, "probe")
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    assert json.loads(r["content"][0]["text"])["ok"] is True

    subjects = _git(repo, "log", "--format=%s", f"{base}..HEAD").splitlines()
    assert subjects == ["casa: plugin probe registry change"]
    assert _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r",
                "HEAD").splitlines() == ["plugins/registry.json"]
    assert _git(repo, "status", "--porcelain") == (
        "M agents/assistant/notes.md")       # the unrelated edit is not swept
