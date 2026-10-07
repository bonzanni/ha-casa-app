"""#1324 — a kept upgrade of a persona-override specialist says which version
the live specialist was running, as it does for a component-default one.

A specialist's instance tuple keeps its COMPONENT root in every binding mode
(a persona override only moves the persona, onto the binding), but an override
binding carries no `component_root`. The live config held nothing else, so
`tools._live_component_root` returned None for every override and both kept
tellings said which version was running "could not be established" — whether
the new version or the replaced one was live.

`activate_binding_for_config` now keeps the activated tuple's root on the live
config (`active_tuple_root`), and `_live_component_root` reads it when the
binding carries no root. One reader, so both tellings get it:

- the library-kept arm of `specialist_upgrade` (#1296/#1298): a concurrent
  reload that loaded the kept version reads "new"; no reload, the replaced
  version live, reads the ruled matched text;
- the #1146 sequencer arm, whose own reload failed: before the swap the
  replaced version is live ("was not running the new version"); after it the
  new one is (the shared kept sentence).

Production paths only: the override tuple is written by the real
`apply_persona_override`; every live config is produced by the real
`activate_binding_for_config`, reached through a real `reload.dispatch("agent")`
or called directly. Nothing hand-assigns a binding or a root. C1's
`_ConcurrentReload` (which assigns `active().binding` itself) is not used.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import plugin_registry
import reload as reload_mod
import specialist_install as si
import test_1296_library_kept_in_use_clause as c1
import test_specialist_binding_rederive as rd
from personality_binding import InstanceDir
from test_1146_kept_upgrade_reload_failed import h  # noqa: F401 — fixture
from test_1296_library_kept_in_use_clause import _role, world  # noqa: F401 — fixtures

pytestmark = pytest.mark.unit      # asyncio_mode = auto (pytest.ini)

SLUG = "mtg"


def _apply_override(specialists_root, monkeypatch, tmp_path) -> None:
    """The installed specialist's ACTIVE tuple becomes a persona override, through
    the real `apply_persona_override` (root kept, persona on the binding)."""
    from persona_install import apply_persona_override
    from persona_pack import load_persona_pack
    from test_persona_install import _write_persona_repo

    monkeypatch.setenv("CASA_CONFIG_DIR", str(tmp_path / "cfgroot"))
    dest = tmp_path / "cfgroot" / "personas" / "casa/judge" / "0.1.0"
    dest.parent.mkdir(parents=True, exist_ok=True)
    _write_persona_repo(dest, persona_id="casa/judge")
    persona = load_persona_pack(dest / "pack", dest / "manifest.json")
    instance = specialists_root / SLUG
    role = rd._cas_role(specialists_root, InstanceDir(instance).active())
    apply_persona_override(
        target_role_id=f"specialist:{SLUG}", persona=persona, role=role,
        instance_dir_root=instance, candidate_validator=lambda p, b: None)
    assert InstanceDir(instance).active().binding.mode == "override"


def _activated_cfg(specialists_root):
    """What the specialist loader hands back: an `AgentConfig` whose role slot is
    the installed component's role, activated by the REAL activation."""
    from config import AgentConfig

    role = rd._cas_role(specialists_root, InstanceDir(specialists_root / SLUG).active())
    cfg = AgentConfig(role_artifact=None, role=SLUG, role_slot=role)
    si.activate_binding_for_config(cfg, specialists_root=specialists_root)
    assert cfg.compiled_prompt_bundle is not None
    return cfg


async def _override_live(world, monkeypatch, tmp_path) -> list:
    """The specialist becomes an override and a real `dispatch("agent")` loads it
    through real activation; every later load does the same. Returns the list of
    `active.yaml` roots each load read."""
    specialists_root = world.fx.common["specialists_dir"]
    _apply_override(specialists_root, monkeypatch, tmp_path)
    loads: list = []

    def load(*a, **kw):
        cfg = _activated_cfg(specialists_root)
        loads.append(world.root())
        return cfg
    monkeypatch.setattr("agent_loader.load_agent_from_dir", load)
    res = await reload_mod.dispatch("agent", runtime=world.h.runtime, role=SLUG)
    assert res.get("status") == "ok", res
    live = world.h.runtime.agents[SLUG].config
    assert live.binding.mode == "override" and live.binding.component_root is None
    return loads


def _live_root(world):
    import tools
    return tools._live_component_root(world.h.runtime, SLUG)


# ── the library-kept arm (specialist_upgrade) ───────────────────────────────

@pytest.mark.parametrize("restart", [False, True], ids=["rerun", "restart"])
@pytest.mark.parametrize("reload_lands", [True, False], ids=["reload-lands", "no-reload"])
async def test_1324_a_kept_override_upgrade_says_which_version_is_live(
        world, monkeypatch, tmp_path, reload_lands, restart):
    """B live as a persona override; B → C library-kept (the owned swap fails
    after C is committed). With a real reload of the specialist landing during
    the swap, C is live and both fields say it was running the new version
    (#1298's "new"). With none, B is live and both fields carry the ruled
    matched text, byte for byte."""
    loads = await _override_live(world, monkeypatch, tmp_path)
    out = await world.call(world.fx.insp2, {"j": "plain-j"}, ["k"])
    assert out.get("ok") is True, out
    b = world.root()
    h = world.h
    before = h.runtime.agents[SLUG]
    insp = world.stage_c()
    loop = asyncio.get_running_loop()
    swaps: list = []
    results: list = []

    def fail_swap(**kw):
        swaps.append(world.root())
        if reload_lands:
            results.append(asyncio.run_coroutine_threadsafe(
                reload_mod.dispatch("agent", runtime=h.runtime, role=SLUG),
                loop).result(timeout=30))
        raise OSError("injected owned swap failure")
    monkeypatch.setattr(plugin_registry, "apply_owned_swap", fail_swap)
    world.cleanup_fails(restart)
    writes, n_loads, raised = len(h.runtime.agents.writes), len(loads), len(world.raised)
    out = await world.call(insp, {}, ["k", "j"])
    world.swap_fails(False)
    c = world.root()

    # The interleaving happened as specified, and nothing else did.
    assert b != c and swaps == [c]
    assert len(world.raised) - raised == 1
    exc = world.raised[-1]
    assert exc.kind == "upgrade_kept_new_version"
    assert (exc.replaced_root, exc.committed_root) == (b, c)
    assert len(h.runtime.agents.writes) - writes == int(reload_lands)
    assert loads[n_loads:] == ([c] if reload_lands else [])
    assert len(results) == int(reload_lands)
    assert all(r.get("status") == "ok" for r in results), results
    if not reload_lands:
        assert h.runtime.agents[SLUG] is before
    live = h.runtime.agents[SLUG].config
    assert live.binding.mode == "override" and live.binding.component_root is None
    assert out["kind"] == "upgrade_kept_new_version", out
    assert out["kept_new_version"] is True

    if reload_lands:
        assert out["outcome"] == (c1.NEW_RESTART_OUTCOME if restart else c1.NEW_OUTCOME)
        assert out["detail"] == (c1.NEW_RESTART_DETAIL if restart else c1.NEW_DETAIL)
    else:
        assert out["outcome"] == (c1.RULED_RESTART_OUTCOME if restart else c1.RULED_OUTCOME)
        assert out["detail"] == c1._detail(c1.RULED, restart=restart)
    assert _live_root(world) == (c if reload_lands else b)


# ── the #1146 sequencer arm: the specialist's own reload failed ─────────────

@pytest.mark.parametrize("after_swap", [False, True], ids=["before-swap", "after-swap"])
async def test_1324_a_kept_override_whose_own_reload_failed_says_which_version_is_live(
        world, monkeypatch, tmp_path, after_swap):
    """A live as a persona override; A → B is kept by the sequencer because the
    specialist's own reload failed. Failing before the swap leaves A live: "was
    not running the new version". Failing after it (trigger re-registration)
    leaves B live: the shared kept sentence. Never "could not be established"."""
    import tools

    loads = await _override_live(world, monkeypatch, tmp_path)
    a = world.root()
    h = world.h
    if after_swap:
        h.reregister_error = RuntimeError("triggers refused")
    else:
        h.registry.fail_load = OSError("scan refused")
    writes, n_loads = len(h.runtime.agents.writes), len(loads)
    out = await world.call(world.fx.insp2, {"j": "plain-j"}, ["k"])
    h.reregister_error = None
    h.registry.fail_load = None
    b = world.root()

    assert a != b
    assert out["kind"] == "reload_failed", out
    assert out["kept_new_version"] is True
    assert [e.get("kind") for e in out["reload_errors"]] == [
        "reregister_failed" if after_swap else "specialist_reload_failed"]
    assert loads[n_loads:] == [b]
    assert len(h.runtime.agents.writes) - writes == int(after_swap)
    live = h.runtime.agents[SLUG].config
    assert live.binding.mode == "override" and live.binding.component_root is None
    assert out["outcome"] != tools._KEPT_RUNNING_UNKNOWN_OUTCOME
    assert out["outcome"] == (tools._KEPT_NEW_VERSION_ENVELOPE["outcome"] if after_swap
                              else tools._KEPT_NOT_LOADED_OUTCOME)
    assert _live_root(world) == (b if after_swap else a)


# ── activation carries the tuple root ───────────────────────────────────────

@pytest.mark.parametrize("rederive", [False, True], ids=["as-stored", "rederived-597"])
def test_1324_activating_an_override_carries_the_tuple_root(tmp_path, monkeypatch, rederive):
    """After the real activation of an override tuple — as stored, and through the
    #597 re-derive path (a model flip moves the role checksum) — the config
    carries the tuple's root, and the live-root reader returns it."""
    import tools

    specialists_root, _, _ = rd._install(tmp_path, monkeypatch)
    _apply_override(specialists_root, monkeypatch, tmp_path)
    active = InstanceDir(specialists_root / SLUG).active()
    if rederive:
        monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    cfg = _activated_cfg(specialists_root)

    assert (cfg.binding.role_checksum != active.binding.role_checksum) is rederive
    assert cfg.binding.mode == "override" and cfg.binding.component_root is None
    assert getattr(cfg, "active_tuple_root", None) == active.root
    runtime = SimpleNamespace(agents={SLUG: SimpleNamespace(config=cfg)})
    assert tools._live_component_root(runtime, SLUG) == active.root


# ── regression (green at the base; not part of the red case) ────────────────

def test_1324_no_active_tuple_activates_nothing_and_carries_no_root(tmp_path):
    """A specialist with no active tuple (pending configuration, legacy) is a
    no-op for activation: no binding, and no root to read."""
    import tools
    from config import AgentConfig

    (tmp_path / SLUG).mkdir()
    role = SimpleNamespace(slot=SLUG)
    cfg = AgentConfig(role_artifact=None, role=SLUG, role_slot=role)
    si.activate_binding_for_config(cfg, specialists_root=tmp_path)
    assert cfg.binding is None
    assert getattr(cfg, "active_tuple_root", None) is None
    runtime = SimpleNamespace(agents={SLUG: SimpleNamespace(config=cfg)})
    assert tools._live_component_root(runtime, SLUG) is None


def test_1324_the_binding_root_is_read_first_and_no_binding_reads_nothing():
    """A binding that carries a root is the evidence (component-default
    behaviour cannot depend on the carrier); a config with no binding was never
    activated and has no root, whatever else it holds."""
    import tools

    def live(cfg):
        return tools._live_component_root(
            SimpleNamespace(agents={SLUG: SimpleNamespace(config=cfg)}), SLUG)

    assert live(SimpleNamespace(binding=SimpleNamespace(component_root="root-B"),
                                active_tuple_root="root-A")) == "root-B"
    assert live(SimpleNamespace(binding=None, active_tuple_root="root-A")) is None
    assert live(SimpleNamespace(binding=SimpleNamespace(component_root=None))) is None
