"""INV-TOOL-014: a configuration commit reloads what it changed.

Two layers. The table (``reload.commit_reload_scopes``) is pinned on its own,
path by path. The tool is pinned through the real entry point: the real
``config_git_commit`` handler on a real temporary config repository
(``config_git.commit_config_checked`` and ``config_git.changed_paths``
unpatched, only ``/config`` redirected), the real ``reload.dispatch`` wrapped by
a spy that records each call, its scope and role, and whether the plugin-tools
guard was held while it ran; and, for ``triggers``, ``agent`` and ``policies``,
the real reload handlers against a real resident tree, so the reload's effect
is asserted too (the trigger registry saw the new triggers, the agent was
rebuilt). Handlers replaced by counters (``agents``, ``executors``, ``full``)
pin which reload ran, not what it does — that is tests/test_reload.py's.

No test binds an engagement unless it says so: the reload must not depend on
the caller being one.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import textwrap
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_HEX = "0123456789abcdef0123456789abcdef"


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

@pytest.fixture
def table_runtime(tmp_path):
    """Live residents assistant and butler; a resident directory `newbie` that
    is not live yet; specialists `finance` (installed: a link to its content
    directory) and `legacy` (a plain directory); one executor."""
    agents = tmp_path / "agents"
    for d in ("assistant", "butler", "newbie", "executors/configurator",
              f"specialists/.finance.material-{_HEX}", "specialists/legacy"):
        (agents / d).mkdir(parents=True)
    (agents / "specialists" / "finance").symlink_to(f".finance.material-{_HEX}")
    return types.SimpleNamespace(
        agents_dir=str(agents),
        role_configs={"assistant": object(), "butler": object(), "gone": object()})


_MATERIAL = f"agents/specialists/.finance.material-{_HEX}"

_TABLE = [
    # residents
    (["agents/assistant/triggers.yaml"], [("triggers", "assistant")]),
    (["agents/assistant/prompts/morning.md"], [("triggers", "assistant")]),
    (["agents/assistant/prompts/café.md",
      "agents/assistant/triggers.yaml"], [("triggers", "assistant")]),
    (["agents/assistant/prompts/system.md"], [("agent", "assistant")]),
    (["agents/assistant/character.yaml"], [("agent", "assistant")]),
    (["agents/assistant/delegates.yaml"], [("agent", "assistant")]),
    (["agents/assistant/triggers.yaml",
      "agents/assistant/character.yaml"], [("agent", "assistant")]),
    (["agents/assistant/triggers.yaml",
      "agents/butler/triggers.yaml"], [("triggers", "assistant"),
                                       ("triggers", "butler")]),
    (["agents/assistant/character.yaml",
      "agents/butler/runtime.yaml"], [("agent", "assistant"), ("agent", "butler")]),
    (["agents/newbie/character.yaml"], [("agents", None)]),        # not live yet
    (["agents/gone/character.yaml"], [("agents", None)]),          # directory removed
    # specialists
    (["agents/specialists/finance",
      f"{_MATERIAL}/runtime.yaml"], [("agents", None)]),           # install / upgrade
    (["agents/specialists/finance"], [("agents", None)]),          # relink / uninstall
    ([f"{_MATERIAL}/runtime.yaml"], [("agent", "finance")]),        # edit in place
    (["agents/specialists/legacy/runtime.yaml"], [("agent", "legacy")]),
    (["agents/specialists/dropped/runtime.yaml"], [("agents", None)]),
    # executors
    (["agents/executors/configurator/definition.yaml"], [("executors", None)]),
    (["agents/executors/probe/definition.yaml",
      "agents/executors/probe/prompt.md"], [("executors", None)]),
    (["agents/executors/configurator/prompt.md"], []),
    (["agents/executors/configurator/observer.yaml",
      "agents/executors/configurator/doctrine/reload.md"], []),
    # policies, plugins
    (["policies/disclosure.yaml"], [("policies", None)]),
    (["plugins/registry.json"], []),
    (["plugins/registry.json",
      "agents/assistant/character.yaml"], [("agent", "assistant")]),
    # a resident's staged persona binding: only a restart activates it
    (["bindings/resident-assistant/desired.yaml"], []),
    (["bindings/resident-assistant/desired.yaml",
      "agents/assistant/character.yaml"], [("agent", "assistant")]),
    # full
    (["schema/probe.v1.json"], [("full", None)]),
    ([], [("full", None)]),                                         # paths unreadable
    ([".gitignore"], [("full", None)]),
    (["agents/README.md"], [("full", None)]),
    (["agents/executors/README.md"], [("full", None)]),
    (["agents/specialists/.stray/file"], [("full", None)]),
    # mixed kinds collapse to one full
    (["agents/specialists/finance",
      "agents/assistant/delegates.yaml"], [("full", None)]),
    (["policies/disclosure.yaml",
      "agents/executors/configurator/definition.yaml"], [("full", None)]),
    (["agents/assistant/triggers.yaml",
      "agents/butler/character.yaml"], [("full", None)]),
    (["agents/assistant/character.yaml",
      "schema/probe.v1.json"], [("full", None)]),
]


@pytest.mark.parametrize("paths, expected", _TABLE,
                         ids=[" + ".join(p) or "unreadable" for p, _ in _TABLE])
def test_commit_reload_table(table_runtime, paths, expected):
    import reload as reload_mod
    assert reload_mod.commit_reload_scopes(paths, runtime=table_runtime) == expected


# ---------------------------------------------------------------------------
# The tool, on a real repository
# ---------------------------------------------------------------------------

def _seed_resident(agents_dir: Path, role: str, name: str) -> Path:
    from test_casa_reload_triggers_resident import _seed_resident_with_disclosure
    d = _seed_resident_with_disclosure(agents_dir, role=role)
    _w(d / "character.yaml", f"""\
        schema_version: 1
        name: {name}
        role: {role}
        archetype: assistant
        card: |
          Peer summary.
        prompt: |
          You are {name}.
    """)
    if role == "butler":
        rt = d / "runtime.yaml"
        rt.write_text(rt.read_text(encoding="utf-8").replace(
            "option: primary_agent_model, default: opus",
            "option: voice_agent_model, default: haiku"), encoding="utf-8")
    _write_triggers(d, ["probe-a"])
    return d


def _write_triggers(agent_dir: Path, names: list[str]) -> None:
    body = "".join(
        f"  - name: {n}\n    type: interval\n    channel: telegram\n"
        f"    minutes: 1\n    prompt: \"probe fire\"\n" for n in names)
    (agent_dir / "triggers.yaml").write_text(
        "schema_version: 1\ntriggers:\n" + body, encoding="utf-8")


def _edit_card(agent_dir: Path, text: str) -> None:
    p = agent_dir / "character.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "  Peer summary.", f"  {text}"), encoding="utf-8")


def _git_paths(root: Path, sha: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff-tree", "-z", "--no-commit-id", "--name-only", "-r", sha],
        cwd=root, check=True, capture_output=True).stdout
    return sorted(p.decode("utf-8") for p in out.split(b"\0") if p)


def _decode(result) -> dict:
    return json.loads(result["content"][0]["text"])


class _World:
    def __init__(self, root: Path, runtime):
        self.root = root
        self.runtime = runtime
        self.registrations: list[tuple[str, list[str]]] = []
        self.constructed: list[str] = []
        self.counted: list[str] = []
        # (scope, role, plugin-tools guard held by the dispatching task)
        self.dispatched: list[tuple[str, "str | None", bool]] = []
        self.health = AsyncMock()

    def agent_dir(self, role: str) -> Path:
        return self.root / "agents" / role

    async def commit(self, expected: list[str], message: str = "edit") -> dict:
        import tools as tools_mod
        r = _decode(await tools_mod.config_git_commit.handler({"message": message}))
        assert r["sha"], r
        assert _git_paths(self.root, r["sha"]) == sorted(expected)
        return r

    def count_handler(self, monkeypatch, scope: str, *, note=None) -> None:
        import reload as reload_mod

        async def counted(runtime, *, role=None, include_env=False):
            self.counted.append(f"{scope}:{role}")
            if note:
                reload_mod._note_failure(*note)
            return [f"counted_{scope}"]
        monkeypatch.setitem(reload_mod._HANDLERS, scope, counted)

    def health_scopes(self) -> list[str]:
        return [c.args[0] for c in self.health.await_args_list]


@pytest.fixture
def configurator_origin():
    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        yield
    finally:
        agent_mod.origin_var.reset(tok)


@pytest.fixture
def world(tmp_path, monkeypatch, configurator_origin):
    import agent as agent_mod
    import agent_loader
    import config_git
    import policies as policies_module
    import reload as reload_mod
    import tools as tools_mod
    from test_casa_reload_triggers_resident import _runtime_with, _seed_policies
    from test_plugin_persist_commit_real_repo import _redirect_commit_tool

    assert tools_mod.engagement_var.get(None) is None

    root = tmp_path / "config"
    root.mkdir()
    config_git.init_repo(str(root))
    _seed_policies(root)
    for role, name in (("assistant", "Ellen"), ("butler", "Tina")):
        _seed_resident(root / "agents", role, name)
    registry_json = root / "plugins" / "registry.json"
    registry_json.parent.mkdir(parents=True)
    registry_json.write_text(json.dumps(
        {"schema_version": 1, "seeded_defaults": [], "plugins": []}), encoding="utf-8")

    trig = MagicMock()
    runtime = _runtime_with(root, trigger_registry=trig)
    runtime.specialist_registry.all_configs = lambda: {}
    w = _World(root, runtime)
    trig.reregister_for.side_effect = (
        lambda role, triggers, channels, **kw: w.registrations.append(
            (role, [t.name for t in triggers])))

    lib = policies_module.load_policies(str(root / "policies" / "disclosure.yaml"))
    for role in ("assistant", "butler"):
        runtime.role_configs[role] = agent_loader.load_agent_from_dir(
            str(root / "agents" / role), policies=lib)
    assert config_git.commit_config(str(root), "fixture baseline")

    def construct(*, cfg, runtime, agent_registry=None):
        w.constructed.append(cfg.role)
        a = MagicMock(name=f"agent:{cfg.role}")
        a.aclose = AsyncMock()
        a.plugin_binding_snapshot = None
        a._get_plugin_resolution = AsyncMock()
        return a
    monkeypatch.setattr(reload_mod, "_construct_agent", construct)
    monkeypatch.setattr(reload_mod, "_start_bus_loop", lambda runtime, role: None)
    monkeypatch.setattr(reload_mod, "_schedule_agent_close",
                        lambda old, *, runtime=None, role=None: None)
    monkeypatch.setattr(reload_mod, "_refresh_role_map",
                        lambda runtime, *, context: [])
    import callback_reconcile
    import event_reconcile
    import trigger_reconcile
    monkeypatch.setattr(trigger_reconcile, "reconcile_from_runtime", AsyncMock())
    monkeypatch.setattr(callback_reconcile, "reconcile_from_runtime", AsyncMock())
    monkeypatch.setattr(event_reconcile, "reconcile_plugin_events", AsyncMock())
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health_after_reload", w.health)
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health_guarded", AsyncMock())
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)

    real_dispatch = reload_mod.dispatch

    async def spy(scope, *, runtime, role=None, include_env=False):
        w.dispatched.append((scope, role, tools_mod._PLUGIN_TOOLS_LOCK_OWNER
                             is asyncio.current_task()))
        return await real_dispatch(scope, runtime=runtime, role=role,
                                   include_env=include_env)
    monkeypatch.setattr(reload_mod, "dispatch", spy)
    _redirect_commit_tool(monkeypatch, types.SimpleNamespace(root=root))
    return w


def _scopes(w: _World) -> list[tuple[str, "str | None"]]:
    return [(s, r) for s, r, _ in w.dispatched]


async def test_trigger_edit_reloads_the_roles_triggers(world):
    _write_triggers(world.agent_dir("assistant"), ["probe-a", "probe-b"])
    r = await world.commit(["agents/assistant/triggers.yaml"])

    assert _scopes(world) == [("triggers", "assistant")]
    assert world.registrations == [("assistant", ["probe-a", "probe-b"])]
    assert world.constructed == []
    assert [(e["status"], e["scope"], e["role"]) for e in r["reloads"]] == [
        ("ok", "triggers", "assistant")]
    assert "warning" not in r
    assert world.health_scopes() == ["triggers"]


async def test_character_edit_rebuilds_that_resident(world):
    _edit_card(world.agent_dir("assistant"), "Edited summary.")
    r = await world.commit(["agents/assistant/character.yaml"])

    assert _scopes(world) == [("agent", "assistant")]
    assert world.constructed == ["assistant"]
    assert world.runtime.role_configs["assistant"].character.card.strip() == "Edited summary."
    assert [(e["status"], e["scope"]) for e in r["reloads"]] == [("ok", "agent")]
    assert world.health_scopes() == ["agent"]


async def test_two_commits_reload_twice(world):
    _write_triggers(world.agent_dir("butler"), ["probe-a", "probe-c"])
    await world.commit(["agents/butler/triggers.yaml"])
    _edit_card(world.agent_dir("butler"), "Second edit.")
    await world.commit(["agents/butler/character.yaml"])

    assert _scopes(world) == [("triggers", "butler"), ("agent", "butler")]
    assert world.constructed == ["butler"]


async def test_policies_edit_reloads_policies(world):
    p = world.root / "policies" / "disclosure.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("private.", "kept private."),
                 encoding="utf-8")
    r = await world.commit(["policies/disclosure.yaml"])

    assert _scopes(world) == [("policies", None)]
    assert "reload_policy_lib" in r["reloads"][0]["actions"]
    assert sorted(world.constructed) == ["assistant", "butler"]


@pytest.mark.parametrize("scope, path, fenced", [
    ("executors", "agents/executors/probe/definition.yaml", True),
    ("full", "schema/probe.v1.json", True),
    ("agents", "agents/newbie/character.yaml", False),     # a resident not live yet
])
async def test_fleet_scopes_run_through_the_entry_point_fence(
        world, monkeypatch, scope, path, fenced):
    """The commit's reload takes the plugin-tools guard exactly where the
    casa_reload tool does (INV-CFG-011), and refreshes plugin health after."""
    world.count_handler(monkeypatch, scope)
    _w(world.root / path, "probe: 1\n")
    r = await world.commit([path])

    assert world.dispatched == [(scope, None, fenced)]
    assert world.counted == [f"{scope}:None"]
    assert r["reloads"][0]["status"] == "ok"
    assert world.health_scopes() == [scope]


async def test_mixed_commit_reloads_full_once(world, monkeypatch):
    world.count_handler(monkeypatch, "full")
    _edit_card(world.agent_dir("assistant"), "Edited summary.")
    p = world.root / "policies" / "disclosure.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("private.", "kept private."),
                 encoding="utf-8")
    await world.commit(["agents/assistant/character.yaml", "policies/disclosure.yaml"])
    assert world.dispatched == [("full", None, True)]
    assert world.counted == ["full:None"]


async def test_plugins_only_commit_reloads_nothing(world):
    raw = json.loads((world.root / "plugins" / "registry.json").read_text())
    raw["seeded_defaults"] = ["hand-edited"]
    (world.root / "plugins" / "registry.json").write_text(json.dumps(raw))
    r = await world.commit(["plugins/registry.json"])
    assert (world.dispatched, r["reloads"]) == ([], [])
    assert "warning" not in r


async def test_executor_prompt_edit_reloads_nothing(world):
    _w(world.root / "agents" / "executors" / "probe" / "prompt.md", "hello\n")
    r = await world.commit(["agents/executors/probe/prompt.md"])
    assert (world.dispatched, r["reloads"]) == ([], [])


async def test_unreadable_paths_reload_full(world, monkeypatch):
    import config_git
    monkeypatch.setattr(config_git, "changed_paths", lambda config_dir, sha: [])
    world.count_handler(monkeypatch, "full")
    _edit_card(world.agent_dir("assistant"), "Edited summary.")
    await world.commit(["agents/assistant/character.yaml"])
    assert world.dispatched == [("full", None, True)]


async def test_empty_commit_reloads_nothing(world):
    import tools as tools_mod
    r = _decode(await tools_mod.config_git_commit.handler({"message": "nothing"}))
    assert r["sha"] == "" and "warning" in r and "reloads" not in r
    assert world.dispatched == []


async def test_a_failed_reload_is_reported_and_the_commit_stands(world, monkeypatch):
    import reload as reload_mod

    def broken(*, cfg, runtime, agent_registry=None):
        raise RuntimeError("cannot build")
    monkeypatch.setattr(reload_mod, "_construct_agent", broken)
    _edit_card(world.agent_dir("assistant"), "Edited summary.")
    r = await world.commit(["agents/assistant/character.yaml"])

    assert _scopes(world) == [("agent", "assistant")]
    assert [(e["status"], e["kind"]) for e in r["reloads"]] == [
        ("error", "construct_failed")]
    assert "not live" in r["warning"]
    assert world.health_scopes() == ["agent"]     # run, and it skips a failed reload


async def test_a_reported_subordinate_failure_is_surfaced(world, monkeypatch):
    world.count_handler(monkeypatch, "executors", note=("probe", "probe_load_failed"))
    _w(world.root / "agents" / "executors" / "probe" / "definition.yaml", "enabled: 1\n")
    r = await world.commit(["agents/executors/probe/definition.yaml"])
    assert r["reloads"][0]["status"] == "ok"
    assert r["reloads"][0]["failures"] == [{"unit": "probe", "detail": "probe_load_failed"}]
    assert "not live" in r["warning"]


async def test_inside_an_engagement_it_reloads_the_same_way(world):
    """The engagement binding changes nothing: the same reload runs, and
    nothing is left for completion to do."""
    import tools as tools_mod
    tok = tools_mod.engagement_var.set(types.SimpleNamespace(id="e" * 32))
    try:
        _write_triggers(world.agent_dir("assistant"), ["probe-z"])
        await world.commit(["agents/assistant/triggers.yaml"])
    finally:
        tools_mod.engagement_var.reset(tok)
    assert _scopes(world) == [("triggers", "assistant")]
    assert world.registrations == [("assistant", ["probe-z"])]
