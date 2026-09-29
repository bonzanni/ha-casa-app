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

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

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
