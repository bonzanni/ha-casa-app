"""INV-CFG-003 (widened, #672): an in-process reload of an ALREADY-LIVE resident
neither hot-swaps it nor promotes its staged binding on disk before the restart.

The defect this pins: the three reload openers (agent, triggers, the policies
cascade) all load through ``reload._load_agent_with_overlay_retry``, which
called ``agent_loader.load_agent_from_dir`` without ``binding_commit`` — so the
loader's boot-time reconcile COMMITTED a staged ``desired.yaml`` to
``active.yaml`` on disk, and only THEN did the identity guard refuse the
hot-swap. Between that commit and the restart the live resident kept serving
the outgoing persona while nothing on disk named it any more, so
``persona_remove`` deleted a persona a resident was still running.

Everything here is real: the SHIPPED assistant agent directory, the real
``load_agent_from_dir`` (never monkeypatched), the real reconcile, real tuple
files under ``tmp_path`` through the ``$CASA_BINDINGS_DIR`` / ``$CASA_CONFIG_DIR``
seams, and the real reference scan and removal.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SHIPPED_AGENTS = _REPO / "casa/rootfs/opt/casa/defaults/agents"
_SHIPPED_POLICIES = _REPO / "casa/rootfs/opt/casa/defaults/policies/disclosure.yaml"


def _installed_versions(config_root: Path, persona_id: str) -> list[str]:
    directory = config_root / "personas" / persona_id
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


@pytest.mark.asyncio
async def test_policies_reload_does_not_promote_a_live_residents_staged_reset(
        tmp_path: Path, monkeypatch, caplog) -> None:
    """RED at the base: after ``dispatch("policies")`` the on-disk active tuple
    is the image-default reset (the cascade committed it before its guard),
    while the live resident still serves the override — and the override is
    then removable although it is still being served."""
    import agent_loader
    import reload as reload_mod
    import tools
    from agent_loader import load_agent_from_dir
    from persona_install import (
        PersonaInstallAckStore, apply_persona_override, persona_references,
        remove_installed_persona,
    )
    from personality_binding import (
        IMAGE_DEFAULT_PERSONA_BY_SLOT, InstanceDir, make_instance_tuple,
        materialize_image_default_binding,
    )
    from policies import load_policies
    from role_artifact import load_role_artifact
    from role_slot import _ha_model_options, materialize_role
    from specialist_install import SpecialistInstallError
    from test_persona_install import install_persona_for_apply
    from test_reload import _make_runtime

    bindings_root = tmp_path / "bindings"
    monkeypatch.setenv("CASA_BINDINGS_DIR", str(bindings_root))
    # Installs casa/ellen@0.9.0 under <tmp>/config-root/personas and points
    # $CASA_CONFIG_DIR at that root — the seam every persona consumer reads.
    persona = install_persona_for_apply(
        tmp_path, monkeypatch, persona_id="casa/ellen", version="0.9.0")
    config_root = tmp_path / "config-root"
    (config_root / "policies").mkdir(parents=True)
    shutil.copy2(_SHIPPED_POLICIES, config_root / "policies" / "disclosure.yaml")
    specialists_root = tmp_path / "specialists"
    ops_root = specialists_root / ".ops"
    ops_root.mkdir(parents=True)
    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")
    agent_dir = str(_SHIPPED_AGENTS / "assistant")
    policy_lib = load_policies(str(_SHIPPED_POLICIES))
    scan = dict(bindings_dir=bindings_root, specialists_dir=specialists_root, ops_dir=ops_root)

    def _remove():
        return remove_installed_persona(
            persona_id="casa/ellen", version="0.9.0",
            personas_root=config_root / "personas", acks=acks, **scan)

    # 1. Stage the override through the real apply path, exactly as
    #    persona_apply does (same role materialisation, same compile proof),
    #    then perform the first REAL load: a resident that is not yet live
    #    commits and activates its binding in one step.
    role = materialize_role(
        source=load_role_artifact(
            Path(agent_loader.DEFAULT_ROLES_DIR) / "resident" / "assistant"),
        options=_ha_model_options())
    instance_dir = InstanceDir(bindings_root / "resident-assistant")
    apply_persona_override(
        target_role_id="resident:assistant", persona=persona, role=role,
        instance_dir_root=bindings_root / "resident-assistant",
        candidate_validator=agent_loader.make_candidate_compile_validator(role))
    live_cfg = load_agent_from_dir(agent_dir, policies=policy_lib)
    assert live_cfg.binding.mode == "override"
    assert live_cfg.binding.persona_id == "casa/ellen"
    assert live_cfg.binding.persona_version == "0.9.0"
    assert instance_dir.desired() is None
    assert instance_dir.active().binding.binding_digest == live_cfg.binding_digest

    # 2. Stage a REAL reset to the image default — what resident_persona_reset
    #    writes — without any restart.
    default_ref = IMAGE_DEFAULT_PERSONA_BY_SLOT["assistant"]
    default_pack = tools._resolve_local_persona(default_ref)
    reset_binding = materialize_image_default_binding(
        role=live_cfg.role_slot, persona=default_pack, image_default_root=default_ref)
    instance_dir.stage_desired(make_instance_tuple(
        root=default_ref, binding=reset_binding, config_snapshot={}))
    assert instance_dir.desired().binding.mode == "image-default"

    # 3. An unrelated policies reload cascades over every live resident.
    runtime = _make_runtime()
    runtime.config_dir = str(config_root)
    runtime.agents_dir = str(_SHIPPED_AGENTS)
    runtime.role_configs = {"assistant": live_cfg}
    live_agent = object()
    runtime.agents = {"assistant": live_agent}
    runtime.specialist_registry.all_configs = lambda: {}
    construct_calls: list = []
    monkeypatch.setattr(
        reload_mod, "_construct_agent",
        lambda *, cfg, runtime: construct_calls.append(cfg))
    reload_mod.register_handler("policies", reload_mod.reload_policies)
    with caplog.at_level(logging.WARNING, logger="reload"):
        result = await reload_mod.dispatch("policies", runtime=runtime)
    assert result["status"] == "ok"

    # The guard fired and the live side is untouched — true before and after.
    warnings = [
        r for r in caplog.records
        if "policies cascade: role=assistant personality identity changed" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert construct_calls == []
    assert runtime.role_configs["assistant"] is live_cfg
    assert runtime.agents["assistant"] is live_agent

    # 4. THE PIN: disk still names the SERVED override; the reset is still only
    #    staged. At the base the cascade had already promoted the reset here.
    active = instance_dir.active()
    desired = instance_dir.desired()
    assert active.binding.mode == "override"
    assert active.binding.persona_id == "casa/ellen"
    assert active.binding.persona_version == "0.9.0"
    assert desired is not None
    assert desired.binding.mode == "image-default"
    assert desired.binding.persona_id == "casa/ellen"
    assert desired.binding.persona_version == "0.1.0"

    # ...so the reference scan still pins the served persona and removal
    # refuses, exactly as the recipe promises until the restart.
    refs = persona_references(**scan)
    assert len(refs["casa/ellen@0.9.0"]) == 1
    assert refs["casa/ellen@0.9.0"][0].referrer == "resident:assistant"
    assert refs["casa/ellen@0.9.0"][0].source == "active.yaml"
    assert _installed_versions(config_root, "casa/ellen") == ["0.9.0"]
    with pytest.raises(SpecialistInstallError) as raised:
        _remove()
    assert raised.value.kind == "persona_pinned"
    assert _installed_versions(config_root, "casa/ellen") == ["0.9.0"]

    # 5. The restart: boot's committing load promotes and activates together,
    #    and only then does the outgoing persona become removable.
    boot_cfg = load_agent_from_dir(agent_dir, policies=policy_lib)
    assert boot_cfg.binding.mode == "image-default"
    assert instance_dir.desired() is None
    assert instance_dir.active().binding.mode == "image-default"
    refs = persona_references(**scan)
    assert len(refs.get("casa/ellen@0.9.0", [])) == 0
    removed = _remove()
    assert removed["ok"] is True
    assert _installed_versions(config_root, "casa/ellen") == []


@pytest.mark.asyncio
async def test_overlay_loader_routes_binding_commit_by_liveness_and_tier(
        tmp_path: Path, monkeypatch) -> None:
    """The routing decision itself, at the one helper all three openers share:
    an already-live resident is loaded WITHOUT committing; a resident that is
    not live yet, and every specialist load (the retry included), commits. RED
    at the base: no call carries the keyword at all."""
    import agent_loader
    import reload as reload_mod
    from test_reload import _make_runtime

    agents_dir = tmp_path / "agents"
    (agents_dir / "assistant").mkdir(parents=True)
    (agents_dir / "specialists" / "mtg").mkdir(parents=True)

    async def _roles_dir(runtime):
        return str(tmp_path / "roles-overlay")

    monkeypatch.setattr(reload_mod, "_specialist_roles_dir", _roles_dir)

    def _run(*, tier: str, role_configs: dict, agent_dir: Path, fail_first: bool):
        calls: list[dict] = []

        def _recording_load(path, **kwargs):
            calls.append(dict(kwargs))
            if fail_first and len(calls) == 1:
                raise RuntimeError("overlay raced")
            return object()

        monkeypatch.setattr(agent_loader, "load_agent_from_dir", _recording_load)
        runtime = _make_runtime()
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = role_configs
        return calls, runtime

    live_calls, runtime = _run(
        tier="resident", role_configs={"assistant": object()},
        agent_dir=agents_dir / "assistant", fail_first=False)
    await reload_mod._load_agent_with_overlay_retry(
        runtime, str(agents_dir / "assistant"), policies=None, tier="resident")

    non_live_calls, runtime = _run(
        tier="resident", role_configs={}, agent_dir=agents_dir / "assistant",
        fail_first=False)
    await reload_mod._load_agent_with_overlay_retry(
        runtime, str(agents_dir / "assistant"), policies=None, tier="resident")

    specialist_calls, runtime = _run(
        tier="specialist", role_configs={}, agent_dir=agents_dir / "specialists" / "mtg",
        fail_first=True)
    await reload_mod._load_agent_with_overlay_retry(
        runtime, str(agents_dir / "specialists" / "mtg"), policies=None, tier="specialist")

    assert len(live_calls) == 1
    assert [c.get("binding_commit") for c in live_calls] == [False]
    assert len(non_live_calls) == 1
    assert [c.get("binding_commit") for c in non_live_calls] == [True]
    assert len(specialist_calls) == 2
    assert [c.get("binding_commit") for c in specialist_calls] == [True, True]
