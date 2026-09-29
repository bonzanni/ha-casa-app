"""#1086 / #1089: which reload discharges the G-2 reload obligation.

A non-empty ``config_git_commit`` inside an engagement arms the obligation, and
``emit_completion`` force-reloads (``scope="full"``) an engagement that still
holds it. These cases pin which reload DISCHARGES it, through the real entry
points: the real ``config_git_commit`` handler on a real temporary config
repository (``config_git.commit_config_checked`` and the path reader unpatched,
only ``/config`` redirected), ``tools.engagement_var`` bound, the configurator
caller role, the real ``casa_reload`` / ``casa_reload_triggers`` /
``plugin_assign`` handlers, the real ``reload.dispatch`` and, for ``triggers``,
``agent``, ``plugin_env`` and ``policies``, the real reload handlers against a
real resident tree. Only leaves that neither read nor write the obligation are
stubbed: agent construction and the bus loop, plugin-health publication, the
plugin-trigger/callback/event reconcilers, and the plugin-tool transport
doubles of tests/test_plugin_tools.py. Where a handler body is replaced by a
counting stub (``full``, ``agents``, ``executors``, ``config_sync`` and the
specialist-tier ``agent`` control), the test pins discharge classification
only, and ``test_reload_obligation_has_only_authorized_mutation_sites`` is what
shows no replaced body could have mutated the obligation.

The first assertion on the obligation is always membership
(``eng.id in tools._ENGAGEMENTS_PENDING_RELOAD``), which is expressible at the
pre-change base too, so a negative arm fails there for the intended reason (the
id was discharged), never for a missing API.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import textwrap
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_CODE_ROOT = Path(__file__).resolve().parent.parent / "casa" / "rootfs" / "opt" / "casa"


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


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


def _write_triggers(agent_dir: Path, names: list[str], prompt: str = "probe fire") -> None:
    body = "".join(
        f"  - name: {n}\n    type: interval\n    channel: telegram\n"
        f"    minutes: 1\n    prompt: \"{prompt}\"\n" for n in names)
    (agent_dir / "triggers.yaml").write_text(
        "schema_version: 1\ntriggers:\n" + body, encoding="utf-8")


def _edit_card(agent_dir: Path, text: str) -> None:
    p = agent_dir / "character.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "  Peer summary.", f"  {text}"), encoding="utf-8")


def _git_paths(root: Path, sha: str) -> list[str]:
    """An independent NUL-separated read of the paths a commit changed."""
    out = subprocess.run(
        ["git", "diff-tree", "-z", "--no-commit-id", "--name-only", "-r", sha],
        cwd=root, check=True, capture_output=True).stdout
    return sorted(p.decode("utf-8") for p in out.split(b"\0") if p)


def _git_head(root: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


def _decode(result) -> dict:
    return json.loads(result["content"][0]["text"])


class _World:
    """A real config repo + a runtime whose reload handlers run for real."""

    def __init__(self, root: Path, runtime, registry: MagicMock):
        self.root = root
        self.runtime = runtime
        self.registry = registry          # trigger registry double: records reregister_for
        self.registrations: list[tuple[str, list[str]]] = []
        self.constructed: list[str] = []
        self.counted: list[str] = []      # counting handler stubs' calls

    def agent_dir(self, role: str) -> Path:
        return self.root / "agents" / role

    async def commit(self, eng, expected: list[str], message: str = "edit") -> str:
        import tools as tools_mod
        r = await tools_mod.config_git_commit.handler({"message": message})
        sha = _decode(r)["sha"]
        assert sha
        assert sha == _git_head(self.root)
        assert _git_paths(self.root, sha) == sorted(expected)
        assert eng.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD
        return sha

    def count_handler(self, monkeypatch, scope: str) -> None:
        """Replace one scope's handler body with a counter (classification
        pins only — see the module docstring)."""
        import reload as reload_mod

        async def counted(runtime, *, role=None, include_env=False):
            self.counted.append(f"{scope}:{role}")
            return [f"counted_{scope}"]
        monkeypatch.setitem(reload_mod._HANDLERS, scope, counted)


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
    eng = types.SimpleNamespace(id="d" * 32)
    tools_mod._ENGAGEMENTS_PENDING_RELOAD.discard(eng.id)
    tools_mod._ENGAGEMENTS_PREACTIVATED.discard(eng.id)
    tok = tools_mod.engagement_var.set(eng)
    try:
        yield eng
    finally:
        tools_mod.engagement_var.reset(tok)
        tools_mod._ENGAGEMENTS_PENDING_RELOAD.discard(eng.id)
        tools_mod._ENGAGEMENTS_PREACTIVATED.discard(eng.id)


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
    w = _World(root, runtime, trig)
    trig.reregister_for.side_effect = (
        lambda role, triggers, channels, **kw: w.registrations.append(
            (role, [t.name for t in triggers])))

    # Boot: load each resident once (a first load may commit its binding),
    # so later reloads are reloads of LIVE residents; then the baseline.
    lib = policies_module.load_policies(str(root / "policies" / "disclosure.yaml"))
    for role in ("assistant", "butler"):
        runtime.role_configs[role] = agent_loader.load_agent_from_dir(
            str(root / "agents" / role), policies=lib)
    assert config_git.commit_config(str(root), "fixture baseline")
    assert subprocess.run(["git", "status", "--porcelain"], cwd=root,
                          capture_output=True, text=True).stdout == ""

    # Leaves only — none reads or writes the obligation.
    def construct(*, cfg, runtime, agent_registry=None):
        w.constructed.append(cfg.role)
        a = MagicMock(name=f"agent:{cfg.role}")
        a.aclose = AsyncMock()
        a.plugin_binding_snapshot = None          # an agent that resolves lazily
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
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health_after_reload", AsyncMock())
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health_guarded", AsyncMock())
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)
    _redirect_commit_tool(monkeypatch, types.SimpleNamespace(root=root))
    return w


def _pending(eng) -> "frozenset[str] | None":
    """The outstanding committed paths (the post-change API); asserted only
    AFTER the base-expressible membership assertion."""
    import tools as tools_mod
    return tools_mod._ENGAGEMENTS_PENDING_RELOAD.pending(eng.id)


@pytest.fixture
def plugin_env_value(monkeypatch):
    """A real plugin_env reload with one literal entry and no op:// lookup."""
    import plugin_env_conf
    import reload as reload_mod
    monkeypatch.setattr(plugin_env_conf, "read_entries",
                        lambda *a, **kw: {"CASA_D1086_PROBE": "on"})
    monkeypatch.setattr(reload_mod, "_PLUGIN_ENV_LAST_KEYS", set())
    monkeypatch.delenv("CASA_D1086_PROBE", raising=False)
    yield "CASA_D1086_PROBE"
    os.environ.pop("CASA_D1086_PROBE", None)


# --- R1 ---------------------------------------------------------------------

async def test_plugin_env_keeps_character_obligation(
        world, engagement, plugin_env_value):
    """R1 (#1086): a successful plugin_env reload covers no committed path;
    the character edit it did not activate stays owed."""
    import tools as tools_mod
    _edit_card(world.agent_dir("assistant"), "Edited summary.")
    await world.commit(engagement, ["agents/assistant/character.yaml"])

    r = _decode(await tools_mod.casa_reload.handler({"scope": "plugin_env"}))
    assert (r["status"], r["scope"], r["actions"][0]) == ("ok", "plugin_env", "set_1_vars")
    assert os.environ[plugin_env_value] == "on"

    assert engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD
    assert _pending(engagement) == {"agents/assistant/character.yaml"}


async def test_agent_reload_discharges_character_obligation(world, engagement):
    """R1's positive control: the doctrine's reload for a character edit."""
    import tools as tools_mod
    _edit_card(world.agent_dir("assistant"), "Edited summary.")
    await world.commit(engagement, ["agents/assistant/character.yaml"])

    r = _decode(await tools_mod.casa_reload.handler(
        {"scope": "agent", "role": "assistant"}))
    assert (r["status"], r["role"]) == ("ok", "assistant")
    assert {"load_config", "construct_agent", "reregister_bus"} <= set(r["actions"])
    assert world.constructed == ["assistant"]
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


# --- R2 / R3 and the #1089 pin ----------------------------------------------

async def test_reload_triggers_alias_keeps_character_from_mixed_commit(world, engagement):
    """R2 (#1086): casa_reload_triggers re-registers the role's triggers — and
    covers only its trigger inputs; the character edit committed with them
    stays owed."""
    import tools as tools_mod
    d = world.agent_dir("assistant")
    _write_triggers(d, ["probe-a", "probe-b"])
    _edit_card(d, "Edited summary.")
    await world.commit(engagement, ["agents/assistant/character.yaml",
                                    "agents/assistant/triggers.yaml"])

    r = _decode(await tools_mod.casa_reload_triggers.handler({"role": "resident:assistant"}))
    assert (r["status"], r["role"]) == ("ok", "assistant")
    assert r["actions"].count("reregister_triggers") == 1
    assert world.registrations == [("assistant", ["probe-a", "probe-b"])]

    assert engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD
    assert _pending(engagement) == {"agents/assistant/character.yaml"}


async def test_reload_triggers_alias_discharges_trigger_only_commit(world, engagement):
    """#1089: casa_reload_triggers DOES discharge the obligation — for a commit
    confined to the role's trigger inputs (R2's positive control)."""
    import tools as tools_mod
    _write_triggers(world.agent_dir("assistant"), ["probe-a", "probe-b"])
    await world.commit(engagement, ["agents/assistant/triggers.yaml"])

    r = _decode(await tools_mod.casa_reload_triggers.handler({"role": "assistant"}))
    assert (r["status"], r["role"]) == ("ok", "assistant")
    assert r["actions"].count("reregister_triggers") == 1
    assert world.registrations == [("assistant", ["probe-a", "probe-b"])]
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


async def test_reload_triggers_discharges_trigger_prompt_edits(world, engagement):
    """A prompts/<trigger>.md edit is a trigger input (doctrine: `triggers`),
    including a non-ASCII file name the git path reader would C-quote."""
    import tools as tools_mod
    d = world.agent_dir("assistant")
    (d / "prompts").mkdir(exist_ok=True)
    (d / "prompts" / "morning.md").write_text("Good morning.\n", encoding="utf-8")
    (d / "prompts" / "café.md").write_text("Coffee time.\n", encoding="utf-8")
    await world.commit(engagement, ["agents/assistant/prompts/café.md",
                                    "agents/assistant/prompts/morning.md"])

    r = _decode(await tools_mod.casa_reload_triggers.handler({"role": "assistant"}))
    assert r["status"] == "ok" and r["actions"].count("reregister_triggers") == 1
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


async def test_explicit_triggers_keeps_character_obligation(world, engagement):
    """R3 (#1086): casa_reload(scope="triggers") after a character-only commit
    re-registers triggers and leaves the character edit owed."""
    import tools as tools_mod
    _edit_card(world.agent_dir("assistant"), "Edited summary.")
    await world.commit(engagement, ["agents/assistant/character.yaml"])

    r = _decode(await tools_mod.casa_reload.handler(
        {"scope": "triggers", "role": "assistant"}))
    assert (r["status"], r["scope"], r["role"]) == ("ok", "triggers", "assistant")
    assert r["actions"].count("reregister_triggers") == 1
    assert world.registrations == [("assistant", ["probe-a"])]

    assert engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD
    assert _pending(engagement) == {"agents/assistant/character.yaml"}


async def test_explicit_triggers_discharges_trigger_only_commit(world, engagement):
    """R3's positive control."""
    import tools as tools_mod
    _write_triggers(world.agent_dir("assistant"), ["probe-a"], prompt="edited fire")
    await world.commit(engagement, ["agents/assistant/triggers.yaml"])

    r = _decode(await tools_mod.casa_reload.handler(
        {"scope": "triggers", "role": "assistant"}))
    assert r["status"] == "ok" and r["actions"].count("reregister_triggers") == 1
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


async def test_partial_reloads_accumulate_to_discharge(world, engagement):
    """Two commits; each reload removes only what it covers, and together the
    doctrine's two reloads discharge both."""
    import tools as tools_mod
    _write_triggers(world.agent_dir("assistant"), ["probe-a", "probe-b"])
    await world.commit(engagement, ["agents/assistant/triggers.yaml"])
    _edit_card(world.agent_dir("butler"), "Edited summary.")
    await world.commit(engagement, ["agents/butler/character.yaml"])

    r = _decode(await tools_mod.casa_reload_triggers.handler({"role": "assistant"}))
    assert r["status"] == "ok"
    assert engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD
    assert _pending(engagement) == {"agents/butler/character.yaml"}

    r = _decode(await tools_mod.casa_reload.handler({"scope": "agent", "role": "butler"}))
    assert r["status"] == "ok" and world.constructed == ["butler"]
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


# --- R5: a failed path read ---------------------------------------------------

import config_git as _config_git_mod  # noqa: E402

_REAL_CHANGED_PATHS = _config_git_mod.changed_paths


async def test_unknown_obligation_survives_narrow_reload(
        world, engagement, plugin_env_value, monkeypatch, tmp_path):
    """R5 (#1086): when the commit's paths cannot be read, only `full` (or a
    supervised restart) can know it covered them; a narrow reload does not."""
    import config_git
    import tools as tools_mod
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    reads: list[str] = []

    def changed(config_dir, sha):
        assert config_dir == "/config"
        reads.append(sha)
        return _REAL_CHANGED_PATHS(str(not_a_repo), sha)   # a real git failure
    monkeypatch.setattr(config_git, "changed_paths", changed)
    # The credit makes the base read the paths too, so both sides take the
    # same failed read (INV-TOOL-011: a failed read arms and spends it).
    tools_mod._ENGAGEMENTS_PREACTIVATED.add(engagement.id)

    _edit_card(world.agent_dir("assistant"), "Edited summary.")
    sha = await world.commit(engagement, ["agents/assistant/character.yaml"])
    assert reads == [sha]
    assert engagement.id not in tools_mod._ENGAGEMENTS_PREACTIVATED

    r = _decode(await tools_mod.casa_reload.handler({"scope": "plugin_env"}))
    assert (r["status"], r["actions"][0]) == ("ok", "set_1_vars")
    assert engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD
    assert _pending(engagement) == {tools_mod.RELOAD_PATHS_UNKNOWN}

    r = _decode(await tools_mod.casa_reload.handler({"scope": "agent", "role": "assistant"}))
    assert r["status"] == "ok"
    assert engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD

    world.count_handler(monkeypatch, "full")
    r = _decode(await tools_mod.casa_reload.handler({"scope": "full"}))
    assert (r["status"], world.counted) == ("ok", ["full:None"])
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


# --- R6: a commit that lands while a reload is running ------------------------

@pytest.mark.parametrize("later", ["new_prompt", "same_triggers_file"])
async def test_commit_during_reload_survives(world, engagement, monkeypatch, later):
    """R6 (#1086): a reload discharges only what was committed before it
    began; a commit landing mid-reload (new path, or the same path again)
    stays owed. Ordered by events, never by sleeps."""
    import reload as reload_mod
    import tools as tools_mod
    d = world.agent_dir("assistant")
    _write_triggers(d, ["probe-a", "probe-b"])
    first = await world.commit(engagement, ["agents/assistant/triggers.yaml"])

    real = reload_mod._HANDLERS["triggers"]
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def gated(runtime, *, role=None):
        calls.append(role)
        entered.set()
        await asyncio.wait_for(release.wait(), 30)
        return await real(runtime, role=role)
    monkeypatch.setitem(reload_mod._HANDLERS, "triggers", gated)

    task = asyncio.create_task(tools_mod.casa_reload.handler(
        {"scope": "triggers", "role": "assistant"}))
    try:
        await asyncio.wait_for(entered.wait(), 30)
        if later == "new_prompt":
            (d / "prompts").mkdir(exist_ok=True)
            (d / "prompts" / "later.md").write_text("Later.\n", encoding="utf-8")
            expected_later = "agents/assistant/prompts/later.md"
        else:
            _write_triggers(d, ["probe-a", "probe-b", "probe-c"])
            expected_later = "agents/assistant/triggers.yaml"
        second = await world.commit(engagement, [expected_later])
        assert second != first
        release.set()
        r = _decode(await asyncio.wait_for(task, 30))
    finally:
        release.set()
        if not task.done():
            task.cancel()
    assert (r["status"], calls) == ("ok", ["assistant"])
    assert r["actions"].count("reregister_triggers") == 1

    assert engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD
    assert _pending(engagement) == {expected_later}

    r = _decode(await tools_mod.casa_reload.handler(
        {"scope": "triggers", "role": "assistant"}))
    assert r["status"] == "ok"
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


# --- R8: the plugin registry ----------------------------------------------------

def _edit_registry(world) -> None:
    p = world.root / "plugins" / "registry.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["seeded_defaults"] = ["edited"]
    p.write_text(json.dumps(raw), encoding="utf-8")


@pytest.mark.parametrize("scope", ["plugin_env", "policies"])
async def test_registry_commit_survives_unrelated_scope(
        world, engagement, plugin_env_value, scope):
    """R8 (#1086): the registry is covered by the reloads the doctrine names
    for a plugin change (`agent` for the target role, `agents` for a
    specialist bundle, `full`) — not by plugin_env or policies."""
    import tools as tools_mod
    _edit_registry(world)
    await world.commit(engagement, ["plugins/registry.json"])

    r = _decode(await tools_mod.casa_reload.handler({"scope": scope}))
    assert (r["status"], r["scope"]) == ("ok", scope)
    if scope == "plugin_env":
        assert r["actions"][0] == "set_1_vars"
    else:
        assert "reload_policy_lib" in r["actions"]
        assert "cascaded_to_2_roles" in r["actions"]
        assert sorted(world.constructed) == ["assistant", "butler"]

    assert engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD
    assert _pending(engagement) == {"plugins/registry.json"}


async def test_registry_commit_discharged_by_target_agent_reload(world, engagement):
    """R8's positive control, and the doctrine's install-origin secrets order
    (`plugin_env`, then `agent` for the plugin's target role)."""
    import tools as tools_mod
    _edit_registry(world)
    _edit_card(world.agent_dir("assistant"), "Edited summary.")
    await world.commit(engagement, ["agents/assistant/character.yaml",
                                    "plugins/registry.json"])
    r = _decode(await tools_mod.casa_reload.handler({"scope": "agent", "role": "assistant"}))
    assert r["status"] == "ok" and world.constructed == ["assistant"]
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


# --- R4: a tool's own reload -----------------------------------------------------

async def test_plugin_assign_internal_agent_reload_keeps_other_role_commit(
        world, engagement, monkeypatch, tmp_path):
    """R4 (#1086): plugin_assign reloads its target through the real
    sequencer (`_reload_and_verify_targets` -> `reload.dispatch("agent")` ->
    `reload_agent`); that reload is the tool's own and discharges nothing, so
    an earlier commit on ANOTHER role stays owed. Only the plugin transport is
    doubled (tests/test_plugin_tools.py); dispatch and reload_agent are real."""
    import agent as agent_mod
    import reload as reload_mod
    import tools as tools_mod
    from test_plugin_tools import _State, _pr, _registered, _wire

    _edit_card(world.agent_dir("butler"), "Edited summary.")
    await world.commit(engagement, ["agents/butler/character.yaml"])

    real_dispatch = reload_mod.dispatch
    st = _State()
    _registered(st, targets=[])
    _wire(monkeypatch, tmp_path, st, publish=_pr())
    monkeypatch.setattr(reload_mod, "dispatch", real_dispatch)
    monkeypatch.setattr(agent_mod, "active_runtime", world.runtime, raising=False)
    internal: list[tuple[str, str]] = []

    async def observed(scope, *, runtime, role=None, include_env=False):
        res = await real_dispatch(scope, runtime=runtime, role=role, include_env=include_env)
        internal.append((f"{scope}:{role}", res.get("status")))
        return res
    monkeypatch.setattr(reload_mod, "dispatch", observed)

    r = _decode(await tools_mod.plugin_assign.handler(
        {"name": "probe", "target": "resident:assistant"}))
    assert r["ok"] is True, json.dumps(r)[:3000]
    assert st.raw["plugins"][0]["targets"] == ["resident:assistant"]
    assert (st.log.count("save"), st.log.count("reload_snapshot")) == (1, 1)
    assert internal == [("agent:assistant", "ok")]
    assert world.constructed == ["assistant"]

    assert engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD
    assert _pending(engagement) == {"agents/butler/character.yaml"}

    r = _decode(await tools_mod.casa_reload.handler({"scope": "agent", "role": "butler"}))
    assert r["status"] == "ok" and world.constructed == ["assistant", "butler"]
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


# --- the single-mutator pin -----------------------------------------------------

_OBLIGATION = "_ENGAGEMENTS_PENDING_RELOAD"

# (owning function, operation) -> count, for every production use of the
# obligation outside its own class. Reads are membership tests and `pending`.
_AUTHORIZED = {
    ("<module>", "declaration"): 1,
    ("config_git_commit", "arm"): 1,
    ("casa_reload", "discharge"): 1,
    ("casa_reload_triggers", "discharge"): 1,
    ("casa_restart_supervised", "discard"): 1,
    ("_finalize_engagement_tail", "discard"): 1,
    ("emit_completion", "discard"): 2,
    ("emit_completion", "contains"): 1,
    ("emit_completion", "pending"): 1,
}


def _obligation_uses(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    def owner(node) -> str:
        cur = parents.get(id(node))
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return cur.name
            cur = parents.get(id(cur))
        return "<module>"

    uses: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any((a.asname or a.name).split(".")[-1] == _OBLIGATION or a.name == _OBLIGATION
                   for a in node.names):
                uses.append((owner(node), "import"))
            continue
        if not (isinstance(node, ast.Name) and node.id == _OBLIGATION) and not (
                isinstance(node, ast.Attribute) and node.attr == _OBLIGATION):
            continue
        parent = parents.get(id(node))
        op = "other"
        if isinstance(parent, (ast.Assign, ast.AnnAssign)) and (
                getattr(parent, "target", None) is node or node in getattr(parent, "targets", [])):
            op = "declaration"
        elif isinstance(parent, ast.Compare) and node in parent.comparators and all(
                isinstance(o, (ast.In, ast.NotIn)) for o in parent.ops):
            op = "contains"
        elif isinstance(parent, ast.Attribute) and isinstance(parents.get(id(parent)), ast.Call) \
                and parents[id(parent)].func is parent:
            op = parent.attr
        uses.append((owner(node), op))
    return uses


def test_reload_obligation_has_only_authorized_mutation_sites():
    """#1086: the obligation is armed by config_git_commit, discharged ONLY by
    casa_reload / casa_reload_triggers through the coverage table, handed to
    the deferred restart by casa_restart_supervised, and dropped on the three
    terminal paths. No reload handler touches it (reload.py does not reference
    it at all), so no internal reload — and no handler body a test replaces —
    can discharge it."""
    found: dict[tuple[str, str], int] = {}
    per_file: dict[str, int] = {}
    for path in sorted(_CODE_ROOT.rglob("*.py")):
        uses = _obligation_uses(path)
        if uses:
            per_file[path.relative_to(_CODE_ROOT).as_posix()] = len(uses)
        if path.name == "tools.py" and path.parent == _CODE_ROOT:
            for u in uses:
                found[u] = found.get(u, 0) + 1
    assert set(per_file) == {"tools.py"}, per_file
    assert found == _AUTHORIZED


# --- regression controls: each doctrine flow's own reload still discharges ------

def _seed_specialist_files(world, slug: str) -> list[str]:
    """The tracked shape a specialist install/upgrade persist commit carries
    (materialised role dir + the specialist registry) — hook-managed in
    production, written directly here; the classification is what is pinned.
    (The per-slug tuple files under specialists/<slug>/ are not reached by the
    config repo's whitelist: `*` excludes their directory.)"""
    root = world.root
    _w(root / "agents" / "specialists" / slug / "character.yaml", f"name: {slug}\n")
    _w(root / "specialists" / "registry.json", json.dumps({"installed": [slug]}))
    return [f"agents/specialists/{slug}/character.yaml", "specialists/registry.json"]


async def test_specialist_bundle_commit_discharged_by_agents(world, engagement, monkeypatch):
    """Specialist install/upgrade/rollback (doctrine: `agents`), including a
    bundle whose owned plugins changed the registry."""
    import tools as tools_mod
    paths = _seed_specialist_files(world, "finance")
    _edit_registry(world)
    await world.commit(engagement, paths + ["plugins/registry.json"])
    world.count_handler(monkeypatch, "agents")
    r = _decode(await tools_mod.casa_reload.handler({"scope": "agents"}))
    assert (r["status"], world.counted) == ("ok", ["agents:None"])
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


async def test_specialist_enabled_flip_discharged_by_agent(world, engagement, monkeypatch):
    """Flip a specialist's `enabled` flag (doctrine: `agent` for that role)."""
    import tools as tools_mod
    _w(world.root / "agents" / "specialists" / "finance" / "runtime.yaml", "enabled: false\n")
    await world.commit(engagement, ["agents/specialists/finance/runtime.yaml"])
    world.count_handler(monkeypatch, "agent")
    r = _decode(await tools_mod.casa_reload.handler(
        {"scope": "agent", "role": "specialist:finance"}))
    assert (r["status"], world.counted) == ("ok", ["agent:finance"])
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


async def test_policies_commit_discharged_by_policies_and_config_sync(
        world, engagement, monkeypatch):
    """`policies` for policies/disclosure.yaml; `config_sync` (whose cascade
    is agents + policies) covers it too."""
    import tools as tools_mod
    p = world.root / "policies" / "disclosure.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("private.", "kept private."),
                 encoding="utf-8")
    await world.commit(engagement, ["policies/disclosure.yaml"])
    r = _decode(await tools_mod.casa_reload.handler({"scope": "policies"}))
    assert r["status"] == "ok" and "reload_policy_lib" in r["actions"]
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD

    p.write_text(p.read_text(encoding="utf-8").replace("kept private.", "private!"),
                 encoding="utf-8")
    await world.commit(engagement, ["policies/disclosure.yaml"])
    world.count_handler(monkeypatch, "config_sync")
    r = _decode(await tools_mod.casa_reload.handler({"scope": "config_sync"}))
    assert (r["status"], world.counted) == ("ok", ["config_sync:None"])
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


async def test_executor_definition_discharged_by_executors(world, engagement, monkeypatch):
    import tools as tools_mod
    _w(world.root / "agents" / "executors" / "probe" / "definition.yaml", "enabled: true\n")
    await world.commit(engagement, ["agents/executors/probe/definition.yaml"])
    world.count_handler(monkeypatch, "executors")
    r = _decode(await tools_mod.casa_reload.handler({"scope": "executors"}))
    assert (r["status"], world.counted) == ("ok", ["executors:None"])
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


@pytest.mark.parametrize("rel", ["prompt.md", "observer.yaml", "doctrine/probe.md"])
async def test_executor_none_row_edit_stays_owed_until_executors(
        world, engagement, monkeypatch, rel):
    """The doctrine's table names NO reload for an executor's prompt.md,
    observer.yaml or doctrine/ edit, but the commit still arms: an engagement
    that follows those rows owes the obligation at completion (and is
    force-reloaded there). No other scope's reload discharges it — the real
    casa_reload_triggers, and the other scopes' classification through the
    real casa_reload — only `executors` (or `full`)."""
    import tools as tools_mod
    path = f"agents/executors/probe/{rel}"
    _w(world.root / path, "probe\n")
    await world.commit(engagement, [path])

    r = _decode(await tools_mod.casa_reload_triggers.handler({"role": "resident:assistant"}))
    assert r["status"] == "ok"
    assert engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD
    for scope, extra in (("agent", {"role": "assistant"}), ("agents", {}),
                         ("policies", {}), ("config_sync", {})):
        world.count_handler(monkeypatch, scope)
        r = _decode(await tools_mod.casa_reload.handler({"scope": scope, **extra}))
        assert r["status"] == "ok", (scope, r)
        assert engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD, scope
        assert _pending(engagement) == {path}, scope

    world.count_handler(monkeypatch, "executors")
    r = _decode(await tools_mod.casa_reload.handler({"scope": "executors"}))
    assert (r["status"], world.counted[-1]) == ("ok", "executors:None")
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


async def test_full_discharges_every_committed_path(world, engagement, monkeypatch):
    import tools as tools_mod
    _edit_card(world.agent_dir("assistant"), "Edited summary.")
    _w(world.root / "bindings" / "resident-assistant" / "desired.yaml", "persona: x\n")
    _w(world.root / "schema" / "probe.v1.json", "{}\n")
    await world.commit(engagement, ["agents/assistant/character.yaml",
                                    "bindings/resident-assistant/desired.yaml",
                                    "schema/probe.v1.json"])
    world.count_handler(monkeypatch, "full")
    r = _decode(await tools_mod.casa_reload.handler({"scope": "full"}))
    assert (r["status"], world.counted) == ("ok", ["full:None"])
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


async def test_restart_supervised_takes_over_the_obligation(world, engagement):
    import tools as tools_mod
    _edit_card(world.agent_dir("assistant"), "Edited summary.")
    await world.commit(engagement, ["agents/assistant/character.yaml"])
    try:
        r = _decode(await tools_mod.casa_restart_supervised.handler({}))
        assert r["deferred"] is True
        assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD
        assert engagement.id in tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD
    finally:
        tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD.discard(engagement.id)


async def test_plugin_secrets_recipe_arms_nothing(world, engagement, plugin_env_value):
    """recipes/plugin/secrets.md: commit, then `plugin_env`. plugin-env.conf is
    gitignored, so the commit is empty and nothing is owed."""
    import tools as tools_mod
    (world.root / "plugin-env.conf").write_text("CASA_D1086_PROBE=on\n", encoding="utf-8")
    r = _decode(await tools_mod.config_git_commit.handler({"message": "wire var"}))
    assert r["sha"] == "" and "warning" in r
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD
    r = _decode(await tools_mod.casa_reload.handler({"scope": "plugin_env"}))
    assert r["status"] == "ok"
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD


async def test_bundle_sequencer_internal_agents_reload_discharges_nothing(
        world, engagement, monkeypatch, tmp_path):
    """R7 (regression, green at base: reload_agents never drained). The
    specialist bundle sequencer's own `dispatch("agents")` (uninstall) is a
    tool's internal reload: an earlier specialist commit stays owed until the
    doctrine's explicit `agents` reload."""
    import reload as reload_mod
    import tools as tools_mod
    from test_plugin_tools import _State, _pr, _wire

    paths = _seed_specialist_files(world, "finance")
    await world.commit(engagement, paths)

    real_dispatch = reload_mod.dispatch
    _wire(monkeypatch, tmp_path, _State(), publish=_pr())
    monkeypatch.setattr(reload_mod, "dispatch", real_dispatch)
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", world.runtime, raising=False)
    world.count_handler(monkeypatch, "agents")

    seq = await tools_mod._bundle_reload_and_verify(
        "alpha", removed_artifact_ids=[], targets_removed=["specialist:alpha"])
    assert seq["reloaded"] == ["evicted:specialist:alpha"]
    assert world.counted == ["agents:None"]
    assert engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD

    r = _decode(await tools_mod.casa_reload.handler({"scope": "agents"}))
    assert r["status"] == "ok" and world.counted == ["agents:None", "agents:None"]
    assert engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD
