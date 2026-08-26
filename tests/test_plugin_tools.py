"""§3.9/§3.13 plugin_add + plugin_update: the mutation-sequencing contract
(publish → sysreqs → activate → snapshot-reload → reconstruct → verify →
health) that structurally kills the stale-version incident."""
from __future__ import annotations

import copy
import json

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class _State:
    def __init__(self):
        self.log: list[str] = []
        self.raw = {"schema_version": 1, "seeded_defaults": [], "plugins": []}


def _pr(name="probe", version="1.2.0", sysreqs=None):
    from plugin_store import PublishResult
    manifest = {"name": name, "version": version}
    if sysreqs is not None:
        manifest["casa"] = {"systemRequirements": sysreqs}
    return PublishResult(name=name, artifact_id="a" * 64,
                         revision="git:" + "b" * 40, version=version,
                         path=f"/store/{name}/" + "a" * 64, manifest=manifest)


def _entry(name="probe", version="1.0.0"):
    """A valid registered entry (its stored revision predates _pr()'s)."""
    return {"name": name,
            "source": {"type": "github", "repo": "o/r", "ref": "v1",
                       "revision": "git:" + "a" * 40, "subdir": ""},
            "artifact_id": "e" * 64, "version": version,
            "targets": ["resident:assistant"]}


def _wire(monkeypatch, tmp_path, st, *, publish=None, publish_exc=None,
          sysreq_exc=None, dispatch_status="ok", with_runtime=True,
          resolved_sha="b" * 40, resolve_exc=None):
    import tools as tools_mod
    import agent as agent_mod
    import plugin_registry as preg
    import reload as reload_mod
    from plugin_registry import RegistryData, ResolutionResult

    def fake_load(path=None):
        return RegistryData(raw=copy.deepcopy(st.raw), entries=[],
                            entry_issues=[], valid=True)

    def fake_save(data, path=None):
        st.raw = copy.deepcopy(data.raw)
        st.log.append("save")

    def fake_resolve(repo, ref, **k):
        st.log.append("resolve")
        if resolve_exc is not None:
            raise resolve_exc
        return resolved_sha

    def fake_publish(*, name, repo, ref, subdir="", commit=None):
        st.log.append("publish")
        if publish_exc is not None:
            raise publish_exc
        return publish

    def fake_install_req(*, plugin_name, requirements, tools_root):
        st.log.append("install_requirements")
        if sysreq_exc is not None:
            raise sysreq_exc
        return []

    async def fake_dispatch(scope, *, runtime, role=None):
        st.log.append(f"dispatch:{role}")
        return {"status": dispatch_status}

    async def fake_reconcile_from_runtime(runtime):
        # The sequencer's in-process trigger reconcile — a no-op stub so the
        # harness doesn't drive the real reconciler against a bare runtime.
        return None

    def fake_reload_snapshot():
        # Sol diff-review B1: the stub must PUBLISH a real frozen snapshot —
        # a non-publishing stub made snapshot_generation() lazily re-invoke
        # this stub (extra log entries, order-dependent false green) or, with
        # a 0-fallback, turned the generation fence into a no-op in tests.
        st.log.append("reload_snapshot")
        prev = preg._snapshot
        preg._snapshot = preg._Snapshot(
            registry=fake_load(), registry_path=tmp_path / "registry.json",
            store_root=tmp_path / "store",
            generation=(prev.generation + 1 if prev is not None else 1))

    import system_requirements.manifest as _mani
    monkeypatch.setattr(_mani, "MANIFEST_PATH", tmp_path / "sysreq-manifest.yaml")
    monkeypatch.setattr(preg, "_snapshot", None)   # per-test isolation
    monkeypatch.setattr(preg, "load_registry", fake_load)
    monkeypatch.setattr(preg, "save_registry", fake_save)
    monkeypatch.setattr(preg, "reload_snapshot", fake_reload_snapshot)
    monkeypatch.setattr(preg, "resolve_all",
                        lambda: ResolutionResult(registry_valid=True))
    monkeypatch.setattr(tools_mod.plugin_store, "resolve_ref", fake_resolve)
    monkeypatch.setattr(tools_mod.plugin_store, "publish", fake_publish)
    monkeypatch.setattr(tools_mod, "install_requirements", fake_install_req)
    monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                        lambda *, plugin_name: {"ready": True})
    monkeypatch.setattr(reload_mod, "dispatch", fake_dispatch)
    import trigger_reconcile
    monkeypatch.setattr(trigger_reconcile, "reconcile_from_runtime",
                        fake_reconcile_from_runtime)
    monkeypatch.setattr(agent_mod, "active_runtime",
                        object() if with_runtime else None, raising=False)
    monkeypatch.setattr(tools_mod, "_PLUGIN_HEALTH_PATH",
                        str(tmp_path / "plugin-health.json"))
    return tools_mod


async def test_plugin_add_happy_activates_and_sequences(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish=_pr(sysreqs=[{"type": "tarball", "url": "x", "verify_bin": "xbin"}]))
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["artifact_id"] == "a" * 64
    assert payload["version"] == "1.2.0"
    # Registry gained the entry.
    assert [e["name"] for e in st.raw["plugins"]] == ["probe"]
    # §3.9/C.2 ORDER is load-bearing:
    # resolve → publish → sysreqs → save → snapshot → reload.
    assert st.log == ["resolve", "publish", "install_requirements", "save",
                      "reload_snapshot", "dispatch:assistant"]


async def test_plugin_add_marks_engagement_preactivated(monkeypatch, tmp_path):
    """#222/#231: the in-process reload+reconcile activates the plugin BEFORE
    the trailing config_git_commit would arm the reload obligation, so a fully
    successful sequence marks the engagement PRE-ACTIVATED — config_git_commit
    reads that to skip arming the (scopeless-erroring, redundant) obligation
    for the plugin-registry persist commit."""
    import types

    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    tools_mod._ENGAGEMENTS_PREACTIVATED.clear()
    eng = types.SimpleNamespace(id="e" * 32)
    token = tools_mod.engagement_var.set(eng)
    try:
        r = await tools_mod.plugin_add.handler({
            "name": "probe", "repo": "o/r", "ref": "v1",
            "targets": ["resident:assistant"]})
        assert json.loads(r["content"][0]["text"])["ok"] is True
        assert eng.id in tools_mod._ENGAGEMENTS_PREACTIVATED
    finally:
        tools_mod.engagement_var.reset(token)
        tools_mod._ENGAGEMENTS_PREACTIVATED.discard(eng.id)


async def test_plugin_add_reload_error_does_not_mark_preactivated(monkeypatch, tmp_path):
    """A reload that errored must NOT mark the engagement pre-activated — a
    real activation miss still needs the guard's forced reload (Sol/Terra:
    require full success incl. postcondition before suppressing the guard)."""
    import types

    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      dispatch_status="error")
    tools_mod._ENGAGEMENTS_PREACTIVATED.clear()
    eng = types.SimpleNamespace(id="f" * 32)
    token = tools_mod.engagement_var.set(eng)
    try:
        await tools_mod.plugin_add.handler({
            "name": "probe", "repo": "o/r", "ref": "v1",
            "targets": ["resident:assistant"]})
        assert eng.id not in tools_mod._ENGAGEMENTS_PREACTIVATED
    finally:
        tools_mod.engagement_var.reset(token)
        tools_mod._ENGAGEMENTS_PREACTIVATED.discard(eng.id)


async def test_failed_mutation_clears_prior_preactivation_marker(monkeypatch, tmp_path):
    """Sol re-review: a marker from an earlier SUCCESSFUL mutation must not
    survive a LATER FAILED mutation in the same engagement — otherwise a
    plugins-only commit could consume the stale credit and mask the failed
    (un-activated) change. Each mutation attempt supersedes the marker."""
    import types
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      dispatch_status="error")   # this activation FAILS
    eng = types.SimpleNamespace(id="a" * 32)
    tools_mod._ENGAGEMENTS_PREACTIVATED.add(eng.id)   # a prior success left it
    token = tools_mod.engagement_var.set(eng)
    try:
        await tools_mod.plugin_add.handler({
            "name": "probe", "repo": "o/r", "ref": "v1",
            "targets": ["resident:assistant"]})
        # The failed attempt cleared the stale marker and did NOT re-add it.
        assert eng.id not in tools_mod._ENGAGEMENTS_PREACTIVATED
    finally:
        tools_mod.engagement_var.reset(token)
        tools_mod._ENGAGEMENTS_PREACTIVATED.discard(eng.id)


async def test_resolved_observability_prefers_fresh_manifest(monkeypatch):
    """#241: the setup declaration must be read from the freshly-published
    manifest (the artifact just activated), NOT a resolve_all() snapshot that
    can momentarily still hold the OLD artifact (no setupTool) mid-update."""
    import types
    import tools as tools_mod
    import plugin_registry as preg
    import plugin_store as pstore

    STALE = {"name": "elevenlabs"}                      # old artifact: no setup
    FRESH = {"name": "elevenlabs",
             "casa": {"setupTool": "setup_elevenlabs_voicemail",
                      "triggers": [{"name": "voicemail"}]}}
    rp = types.SimpleNamespace(name="elevenlabs", manifest=STALE,
                               manifest_name="elevenlabs")
    monkeypatch.setattr(preg, "resolve_all",
                        lambda: types.SimpleNamespace(plugins=[rp]))
    monkeypatch.setattr(tools_mod, "grants_for_resolved", lambda _rp: [])
    monkeypatch.setattr(tools_mod, "required_env_vars_for_resolved", lambda _rp: [])
    monkeypatch.setattr(pstore, "manifest_setup_tool",
                        lambda m: (m.get("casa") or {}).get("setupTool"))
    monkeypatch.setattr(pstore, "manifest_triggers",
                        lambda m, pname: (m.get("casa") or {}).get("triggers", []))

    # The bug: re-deriving from the stale resolved snapshot → no declaration.
    assert tools_mod._resolved_observability(
        "elevenlabs")["setup_tool"] is None
    # The fix: the freshly-published manifest is authoritative.
    fresh = tools_mod._resolved_observability("elevenlabs", manifest=FRESH)
    assert fresh["setup_tool"] == "setup_elevenlabs_voicemail"
    # #451: there is no setup_via_consent any more — nothing classifies a
    # runner at mutation time, because there is only one runner.
    assert "setup_via_consent" not in fresh


async def test_plugin_update_result_omits_internal_manifest(monkeypatch, tmp_path):
    """The threaded _published_manifest is an internal carrier — it must be
    popped before the tool result so the raw manifest never reaches the model."""
    st = _State()
    st.raw["plugins"].append(_entry(name="probe"))
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(version="2.0.0"))
    r = await tools_mod.plugin_update.handler({"name": "probe", "new_ref": "v2"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert "_published_manifest" not in payload
    assert "setup_tool" in payload          # observability fields still present
    assert "setup_via_consent" not in payload   # #451: no runner classification


async def test_plugin_add_ref_not_found_pre_mutation(monkeypatch, tmp_path):
    from plugin_store import RefNotFound
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish_exc=RefNotFound("404"))
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "phantom",
        "targets": ["resident:assistant"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["kind"] == "ref_not_found"
    assert st.raw["plugins"] == []            # registry byte-identical
    assert "save" not in st.log and "dispatch:assistant" not in st.log


async def test_plugin_add_resolve_unavailable_distinct(monkeypatch, tmp_path):
    from plugin_store import ResolveUnavailable
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish_exc=ResolveUnavailable("net"))
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    assert json.loads(r["content"][0]["text"])["kind"] == "resolve_unavailable"


async def test_plugin_add_sysreq_failure_leaves_registry_unchanged(
        monkeypatch, tmp_path):
    from system_requirements.orchestrator import OrchestrationError
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish=_pr(sysreqs=[{"type": "tarball", "url": "x", "verify_bin": "xbin"}]),
                      sysreq_exc=OrchestrationError("boom"))
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["kind"] == "system_requirements_failed"
    assert st.raw["plugins"] == []            # activation never happened
    assert "save" not in st.log


async def test_plugin_add_duplicate_name_refused(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append({"name": "probe"})
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    assert json.loads(r["content"][0]["text"])["kind"] == "plugin_exists"
    assert "publish" not in st.log            # refused pre-publish


async def test_plugin_add_bad_target_grammar_refused(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1", "targets": ["butler"]})
    assert json.loads(r["content"][0]["text"])["kind"] == "invalid_target"
    assert st.log == []


async def test_plugin_update_derives_version_from_manifest(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append({
        "name": "probe",
        "source": {"type": "github", "repo": "o/r", "ref": "v1",
                   "revision": "git:" + "c" * 40, "subdir": ""},
        "artifact_id": "c" * 64, "version": "1.1.0",
        "targets": ["specialist:finance"]})
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(version="2.0.0"))
    r = await tools_mod.plugin_update.handler({"name": "probe", "new_ref": "v2"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["version"] == "2.0.0"      # FR5: derived, not supplied
    entry = st.raw["plugins"][0]
    assert entry["version"] == "2.0.0"
    assert entry["artifact_id"] == "a" * 64
    assert entry["source"]["ref"] == "v2"


async def test_plugin_update_installs_new_requirements_before_activation(
        monkeypatch, tmp_path):
    from system_requirements.orchestrator import OrchestrationError
    st = _State()
    st.raw["plugins"].append({
        "name": "probe",
        "source": {"type": "github", "repo": "o/r", "ref": "v1",
                   "revision": "git:" + "c" * 40, "subdir": ""},
        "artifact_id": "c" * 64, "version": "1.1.0", "targets": []})
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish=_pr(version="2.0.0",
                                  sysreqs=[{"type": "npm", "package": "x", "verify_bin": "xbin"}]),
                      sysreq_exc=OrchestrationError("boom"))
    r = await tools_mod.plugin_update.handler({"name": "probe", "new_ref": "v2"})
    assert json.loads(r["content"][0]["text"])["kind"] == \
        "system_requirements_failed"
    assert st.raw["plugins"][0]["version"] == "1.1.0"   # pointer NOT moved
    assert st.log.index("install_requirements") < len(st.log)
    assert "save" not in st.log


async def test_plugin_update_unknown_name_refused(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_update.handler({"name": "ghost", "new_ref": "v2"})
    assert json.loads(r["content"][0]["text"])["kind"] == "not_registered"
    assert "publish" not in st.log


async def test_reload_dispatch_error_makes_mutation_not_ok(monkeypatch, tmp_path):
    """Sol F7: real dispatch envelope is {'status': 'ok'} — an error status
    counts as a reload failure and the mutation reports ok:false."""
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      dispatch_status="error")
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["kind"] == "reload_failed"
    assert payload["reload_errors"]           # carries the failed target


async def test_failed_mutation_leaves_blocking_health_issue(monkeypatch, tmp_path):
    """R2-4: a failed mutation must persist a blocking health issue, never a
    green report."""
    import plugin_health
    st = _State()
    hp = tmp_path / "plugin-health.json"
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      dispatch_status="error")
    await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    report = plugin_health.load_report(hp)
    assert any(i["reason_code"] == "reload_failed"
               for i in report["issues"])


async def test_mutation_regenerates_health_report(monkeypatch, tmp_path):
    import plugin_health
    st = _State()
    hp = tmp_path / "plugin-health.json"
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    assert plugin_health.load_report(hp) is not None       # rewritten


async def test_error_core_short_circuits_wrapper(monkeypatch, tmp_path):
    """R2-3: an error sync-core never reaches the reload tail."""
    import tools as tools_mod
    called = {"seq": 0}

    async def spy_seq(*a, **kw):
        called["seq"] += 1
        return {"ok": True}

    monkeypatch.setattr(tools_mod, "_plugin_add_sync",
                        lambda **kw: {"ok": False, "kind": "x"})
    monkeypatch.setattr(tools_mod, "_reload_and_verify_targets", spy_seq)
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1", "targets": []})
    assert json.loads(r["content"][0]["text"])["kind"] == "x"
    assert called["seq"] == 0                 # reload tail NOT reached


async def test_mutating_tools_do_not_stall_event_loop(monkeypatch, tmp_path):
    import asyncio
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.001)

    t = asyncio.create_task(tick())
    await asyncio.sleep(0)
    await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    t.cancel()
    assert ticks >= 1


# --- Task 12: assign / unassign / remove / list -----------------------------

class _FakeAgent:
    def __init__(self, binding):
        self.active_plugin_binding = dict(binding)
        # A real Agent always exposes these; the mutation's post-reconstruct
        # force-resolve (Sol round-4) calls _get_plugin_resolution when the
        # binding hasn't been captured yet.
        self._plugin_resolution = object()

    async def _get_plugin_resolution(self):
        self._plugin_resolution = object()
        return self._plugin_resolution


class _FakeRuntime:
    def __init__(self, agents):
        self.agents = agents


def _registered(st, name="probe", targets=None):
    st.raw["plugins"].append({
        "name": name,
        "source": {"type": "github", "repo": "o/r", "ref": "v1",
                   "revision": "git:" + "c" * 40, "subdir": ""},
        "artifact_id": "c" * 64, "version": "1.0.0",
        "targets": list(targets or [])})


async def test_plugin_assign_roundtrip(monkeypatch, tmp_path):
    st = _State()
    _registered(st, targets=[])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_assign.handler({
        "name": "probe", "target": "specialist:finance"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True and payload["was_assigned"] is False
    assert st.raw["plugins"][0]["targets"] == ["specialist:finance"]
    assert "reload_snapshot" in st.log and "dispatch:finance" in st.log


async def test_plugin_assign_idempotent(monkeypatch, tmp_path):
    st = _State()
    _registered(st, targets=["specialist:finance"])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_assign.handler({
        "name": "probe", "target": "specialist:finance"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["was_assigned"] is True
    assert "save" not in st.log            # no-op: not re-saved


async def test_plugin_unassign_removes_target(monkeypatch, tmp_path):
    st = _State()
    _registered(st, targets=["specialist:finance", "resident:assistant"])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    monkeypatch.setattr(__import__("agent"), "active_runtime",
                        _FakeRuntime({"finance": _FakeAgent({})}), raising=False)
    r = await tools_mod.plugin_unassign.handler({
        "name": "probe", "target": "specialist:finance"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True and payload["was_assigned"] is True
    assert st.raw["plugins"][0]["targets"] == ["resident:assistant"]


async def test_unassign_postcondition_is_absence(monkeypatch, tmp_path):
    """Sol F7: a reconstructed agent that STILL binds the plugin flips the tool
    to postcondition_failed; a clean one returns ok."""
    st = _State()
    _registered(st, targets=["specialist:finance"])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    import agent as agent_mod
    # Stub agent that WRONGLY keeps the binding → postcondition_failed.
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _FakeRuntime({"finance": _FakeAgent({"probe": "x"})}),
                        raising=False)
    r = await tools_mod.plugin_unassign.handler({
        "name": "probe", "target": "specialist:finance"})
    assert json.loads(r["content"][0]["text"])["ok"] is False

    # Reconstructed cleanly (binding gone) → ok.
    st2 = _State(); _registered(st2, targets=["specialist:finance"])
    tools_mod = _wire(monkeypatch, tmp_path, st2, publish=_pr())
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _FakeRuntime({"finance": _FakeAgent({})}), raising=False)
    r = await tools_mod.plugin_unassign.handler({
        "name": "probe", "target": "specialist:finance"})
    assert json.loads(r["content"][0]["text"])["ok"] is True


async def test_plugin_remove_keeps_seeded_defaults(monkeypatch, tmp_path):
    """§3.1 no-resurrection: removing a seeded default keeps its name in
    seeded_defaults so a later seed_defaults does NOT re-add it."""
    import plugin_registry
    st = _State()
    st.raw["seeded_defaults"] = ["probe"]
    _registered(st, name="probe", targets=["executor:plugin-developer"])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_remove.handler({"name": "probe"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True and payload["artifact_retained"] is True
    assert st.raw["plugins"] == []
    assert st.raw["seeded_defaults"] == ["probe"]     # untouched


async def test_plugin_remove_unknown_refused(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_remove.handler({"name": "ghost"})
    assert json.loads(r["content"][0]["text"])["kind"] == "not_registered"


async def test_plugin_list_reports_presence_and_seeded(monkeypatch, tmp_path):
    st = _State()
    st.raw["seeded_defaults"] = ["probe"]
    _registered(st, name="probe", targets=["executor:plugin-developer"])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_list.handler({})
    payload = json.loads(r["content"][0]["text"])
    assert payload["registry_valid"] is True
    row = payload["plugins"][0]
    assert row["name"] == "probe"
    assert row["seeded_default"] is True
    assert row["artifact_present"] is False           # store dir absent in test
    assert row["targets"] == ["executor:plugin-developer"]


def test_plugin_add_schema_subdir_optional():
    """Sol #15: the plugin_add schema must NOT mark subdir required — the
    shorthand {key: type} form marked every key required, so a root-plugin call
    omitting subdir was rejected by the MCP input validator before the handler
    (which defaults it) ever ran."""
    import tools
    schema = tools.plugin_add.input_schema
    assert schema.get("type") == "object"
    assert "subdir" not in schema["required"]
    assert set(schema["required"]) == {"name", "repo", "ref", "targets"}


def test_plugin_add_sync_rejects_bad_subdir_and_nonstring_target():
    """Sol round-3 M: a bad subdir / non-string target returns an envelope, not
    an uncaught crash outside it."""
    from tools import _plugin_add_sync
    r = _plugin_add_sync(name="p", repo="o/r", ref="v1", subdir="../x",
                         targets=["specialist:finance"])
    assert r == {"ok": False, "kind": "invalid_subdir", "subdir": "../x"}
    r2 = _plugin_add_sync(name="p", repo="o/r", ref="v1", targets=[1])
    assert r2["kind"] == "invalid_target"


def test_install_sysreqs_no_reqs_clears_stale_row(monkeypatch):
    """Sol round-3 M: an update to a manifest with NO requirements clears a stale
    manifest row (add_plugin_entry replaces by name on the has-reqs path)."""
    import tools as tools_mod
    removed = []
    monkeypatch.setattr(tools_mod, "remove_manifest", lambda n: removed.append(n))
    r = tools_mod._install_plugin_sysreqs("p", {"name": "p", "version": "2"})
    assert r is None
    assert removed == ["p"]


# --- C.2 identity guards (v0.74.0) ------------------------------------------


async def test_update_revision_mismatch_aborts_before_everything(
        monkeypatch, tmp_path):
    """C.2 step 2: expected_revision mismatch is a hard abort BEFORE
    publish/sysreqs/registry mutation."""
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      resolved_sha="c" * 40)
    core = tools_mod._plugin_update_sync(
        name="probe", new_ref="v1.2.0", expected_revision="git:" + "b" * 40)
    assert core == {"ok": False, "kind": "revision_mismatch",
                    "expected_revision": "b" * 40,
                    "resolved_revision": "c" * 40}
    for step in ("publish", "install_requirements", "save"):
        assert step not in st.log, step


async def test_update_tag_version_mismatch_aborts_before_sysreqs(
        monkeypatch, tmp_path):
    """C.2 step 4: a vX.Y.Z ref must equal 'v'+manifest.version — abort
    BEFORE sysreq install and registry mutation."""
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(version="1.2.0"))
    core = tools_mod._plugin_update_sync(name="probe", new_ref="v9.9.9")
    assert core["ok"] is False and core["kind"] == "tag_version_mismatch"
    assert "install_requirements" not in st.log and "save" not in st.log


async def test_update_non_tag_ref_skips_tag_version_guard(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(version="1.2.0"))
    core = tools_mod._plugin_update_sync(name="probe", new_ref="master")
    assert core["ok"] is True


async def test_update_matching_tag_and_revision_proceeds_in_order(
        monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish=_pr(version="1.2.0",
                                  sysreqs=[{"type": "tarball", "url": "x", "verify_bin": "xbin"}]))
    core = tools_mod._plugin_update_sync(
        name="probe", new_ref="v1.2.0", expected_revision="b" * 40)
    assert core["ok"] is True
    assert st.log.index("resolve") < st.log.index("publish") \
        < st.log.index("install_requirements") < st.log.index("save")


async def test_add_revision_mismatch_aborts(monkeypatch, tmp_path):
    """C.2 applies to plugin_add too — abort BEFORE publish, sysreq install,
    and registry mutation (r2-B4: ordering asserted, not just no-save)."""
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish=_pr(sysreqs=[{"type": "tarball", "url": "x", "verify_bin": "xbin"}]),
                      resolved_sha="c" * 40)
    core = tools_mod._plugin_add_sync(
        name="probe", repo="o/r", ref="v1.2.0",
        targets=["resident:assistant"], expected_revision="b" * 40)
    assert core["kind"] == "revision_mismatch"
    assert st.log == ["resolve"]              # NOTHING after the guard ran
    for step in ("publish", "install_requirements", "save"):
        assert step not in st.log, step
    assert st.raw["plugins"] == []            # registry byte-identical


async def test_add_tag_version_mismatch_aborts_before_sysreqs_and_save(
        monkeypatch, tmp_path):
    """r2-B7: the add-side tag guard, with the same pre-sysreq/pre-save abort."""
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish=_pr(version="1.2.0",
                                  sysreqs=[{"type": "tarball", "url": "x", "verify_bin": "xbin"}]))
    core = tools_mod._plugin_add_sync(
        name="probe", repo="o/r", ref="v9.9.9",
        targets=["resident:assistant"])
    assert core["ok"] is False and core["kind"] == "tag_version_mismatch"
    assert "install_requirements" not in st.log and "save" not in st.log
    assert st.raw["plugins"] == []            # registry byte-identical


async def test_add_name_mismatch_from_publish_reports_manifest_name(
        monkeypatch, tmp_path):
    """Naming harmonization (2026-07-19, Sol v093-1): publish's
    validate_manifest rejects a wrong caller-supplied name — the tool payload
    must carry the canonical `manifest_name` so the configurator
    self-corrects in ONE retry (repo `casa-plugin-gmail` ⇒ plugin `gmail`)."""
    from plugin_store import StoreError
    st = _State()
    exc = StoreError("manifest name 'gmail' != 'casa-plugin-gmail'",
                     reason_code="name_mismatch",
                     detail={"manifest_name": "gmail"})
    tools_mod = _wire(monkeypatch, tmp_path, st, publish_exc=exc)
    core = tools_mod._plugin_add_sync(
        name="casa-plugin-gmail", repo="o/casa-plugin-gmail", ref="v1.2.0",
        targets=["resident:assistant"])
    assert core["ok"] is False and core["kind"] == "name_mismatch"
    assert core["manifest_name"] == "gmail"
    assert "install_requirements" not in st.log and "save" not in st.log
    assert st.raw["plugins"] == []            # registry byte-identical


async def test_update_name_mismatch_from_publish_reports_manifest_name(
        monkeypatch, tmp_path):
    """Update-path analog: a new manifest that renames the plugin surfaces
    `name_mismatch` + `manifest_name`. (Unlike add, retrying update with the
    manifest name would be `not_registered` — a rename is an explicit
    add/migration, per the recipe.)"""
    from plugin_store import StoreError
    st = _State()
    st.raw["plugins"].append(_entry())
    exc = StoreError("manifest name 'renamed' != 'probe'",
                     reason_code="name_mismatch",
                     detail={"manifest_name": "renamed"})
    tools_mod = _wire(monkeypatch, tmp_path, st, publish_exc=exc)
    core = tools_mod._plugin_update_sync(name="probe", new_ref="v1.2.0")
    assert core["ok"] is False and core["kind"] == "name_mismatch"
    assert core["manifest_name"] == "renamed"
    assert "install_requirements" not in st.log and "save" not in st.log


def test_validate_manifest_name_mismatch_carries_manifest_name(tmp_path):
    """Store-level: the StoreError itself must carry the canonical name."""
    import json as _json
    from plugin_store import StoreError, validate_manifest
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        _json.dumps({"name": "gmail", "version": "1.0.0"}), encoding="utf-8")
    with pytest.raises(StoreError) as ei:
        validate_manifest(tmp_path, "casa-plugin-gmail")
    assert ei.value.reason_code == "name_mismatch"
    assert ei.value.detail == {"manifest_name": "gmail"}


async def test_add_invalid_expected_revision_rejected(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    core = tools_mod._plugin_add_sync(
        name="probe", repo="o/r", ref="v1.2.0",
        targets=["resident:assistant"], expected_revision="not-a-sha")
    assert core == {"ok": False, "kind": "invalid_expected_revision",
                    "expected_revision": "not-a-sha"}


async def test_resolver_taxonomy_maps_to_envelope_kinds(monkeypatch, tmp_path):
    import plugin_store
    st = _State()
    st.raw["plugins"].append(_entry())
    cases = [
        (plugin_store.RefNotFound("x"), {"ok": False, "kind": "ref_not_found"}),
        (plugin_store.ResolveAuthFailed("x"),
         {"ok": False, "kind": "resolve_auth_failed"}),
        (plugin_store.SourceEmpty("x"), {"ok": False, "kind": "source_empty"}),
        (plugin_store.ResolveUnavailable("x", retry_after_s=42.0),
         {"ok": False, "kind": "resolve_unavailable", "retry_after_s": 42.0}),
        (plugin_store.ResolveUnavailable("x"),
         {"ok": False, "kind": "resolve_unavailable"}),
    ]
    for exc, expected in cases:
        tools_mod = _wire(monkeypatch, tmp_path, st, resolve_exc=exc)
        core = tools_mod._plugin_update_sync(name="probe", new_ref="v1.2.0")
        assert core == expected, expected["kind"]


# --- §E pinned mutation envelope (v0.74.0) -----------------------------------


async def test_envelope_pre_activation_failure_is_pinned_shape(
        monkeypatch, tmp_path):
    """Guard failure: pin never moved; kind/verify still present (spec §E).

    Pins INV-TOOL-003 (envelope half; the lock is structural). Red case demonstrated: defaulting activation_committed to True on plugin_update's failure path fails this test.
    """
    import plugin_store
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      resolve_exc=plugin_store.RefNotFound("x"))
    res = await tools_mod.plugin_update.handler(
        {"name": "probe", "new_ref": "v9.9.9"})
    payload = json.loads(res["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["kind"] == "ref_not_found"
    assert payload["activation_committed"] is False
    assert payload["runtime_ready"] is False
    assert payload["verify"] == {}
    assert res.get("is_error") is True          # outer MCP flag …
    assert "is_error" not in payload            # … never a payload field


async def test_envelope_committed_but_not_ready(monkeypatch, tmp_path):
    """activation_committed:true + runtime_ready:false = 'pin moved, runtime
    not caught up' — callers retry the RELOAD, never the activation.

    Pins INV-TOOL-004. Red case demonstrated: reporting activation_committed False after the registry save fails this test.
    """
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      dispatch_status="error")
    res = await tools_mod.plugin_update.handler(
        {"name": "probe", "new_ref": "v1.2.0"})
    payload = json.loads(res["content"][0]["text"])
    assert payload["activation_committed"] is True
    assert payload["runtime_ready"] is False
    assert payload["ok"] is False
    assert payload["kind"] == "reload_failed"
    assert "verify" in payload
    assert res.get("is_error") is True


async def test_envelope_fully_ok_has_kind_none(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    res = await tools_mod.plugin_update.handler(
        {"name": "probe", "new_ref": "v1.2.0"})
    payload = json.loads(res["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["kind"] is None              # pinned shape: present, None
    assert payload["activation_committed"] is True
    assert payload["runtime_ready"] is True
    assert payload["verify"] == {"ready": True}
    assert res.get("is_error") is not True


async def test_add_envelope_pre_activation_failure_is_pinned_shape(
        monkeypatch, tmp_path):
    """r2-B6: §E names plugin_add too — same pinned shape on its
    pre-activation failure path."""
    import plugin_store
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      resolve_exc=plugin_store.RefNotFound("x"))
    res = await tools_mod.plugin_add.handler(
        {"name": "probe", "repo": "o/r", "ref": "v9.9.9",
         "targets": ["resident:assistant"]})
    payload = json.loads(res["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["kind"] == "ref_not_found"
    assert payload["activation_committed"] is False
    assert payload["runtime_ready"] is False
    assert payload["verify"] == {}
    assert res.get("is_error") is True
    assert "is_error" not in payload


async def test_add_envelope_fully_ok_has_kind_none(monkeypatch, tmp_path):
    """r2-B6: add-side success carries the full pinned payload."""
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    res = await tools_mod.plugin_add.handler(
        {"name": "probe", "repo": "o/r", "ref": "v1.2.0",
         "targets": ["resident:assistant"]})
    payload = json.loads(res["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["kind"] is None
    assert payload["activation_committed"] is True
    assert payload["runtime_ready"] is True
    assert payload["verify"] == {"ready": True}
    assert res.get("is_error") is not True


async def test_add_envelope_committed_but_not_ready(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      dispatch_status="error")
    res = await tools_mod.plugin_add.handler(
        {"name": "probe", "repo": "o/r", "ref": "v1.2.0",
         "targets": ["resident:assistant"]})
    payload = json.loads(res["content"][0]["text"])
    assert payload["activation_committed"] is True
    assert payload["runtime_ready"] is False
    assert payload["ok"] is False
    assert payload["kind"] == "reload_failed"
    assert res.get("is_error") is True


def test_plugin_remove_clears_manifest_row(monkeypatch, tmp_path):
    """Sol round-3 M: removing a plugin drops its system-requirement manifest row."""
    import tools as tools_mod
    st = _State()
    st.raw["plugins"].append({
        "name": "gone", "source": {"type": "github", "repo": "o/r", "ref": "v1",
        "revision": "git:" + "a" * 40, "subdir": ""}, "artifact_id": "c" * 64,
        "version": "1.0.0", "targets": ["specialist:finance"]})
    _wire(monkeypatch, tmp_path, st)
    removed = []
    monkeypatch.setattr(tools_mod, "remove_manifest", lambda n: removed.append(n))
    r = tools_mod._plugin_remove_sync(name="gone")
    assert r["ok"] is True and removed == ["gone"]


async def test_mutation_generation_race_retries_reload_then_fails_explicit(
        monkeypatch, tmp_path):
    """D2: a reloaded target whose snapshot generation disagrees with the
    post-reload snapshot triggers ONE re-dispatch retry (a real
    re-resolution), then explicit snapshot_raced — never graded stale."""
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    import plugin_registry as preg
    import agent as agent_mod
    from types import SimpleNamespace

    class _StaleSnapAgent:
        # generation pinned at 1; snapshot_generation() below returns 99 —
        # permanently mismatched, so both attempts fail.
        plugin_binding_snapshot = SimpleNamespace(binding={}, generation=1)

    runtime = SimpleNamespace(agents={"assistant": _StaleSnapAgent()})
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)
    monkeypatch.setattr(preg, "snapshot_generation", lambda: 99)
    res = await tools_mod.plugin_update.handler(
        {"name": "probe", "new_ref": "v1.2.0"})
    payload = json.loads(res["content"][0]["text"])
    assert payload["activation_committed"] is True
    assert payload["runtime_ready"] is False
    assert payload["ok"] is False
    assert payload["kind"] == "snapshot_raced"
    # ONE retry: the agent reload was dispatched twice for the target.
    assert st.log.count("dispatch:assistant") == 2


# --- A:§3.3/§3.4 lifecycle invalidation ordering (v0.76.0, r1-B8/r2-B5) ------


def _spy_grants_and_challenges(monkeypatch, tools_mod, st):
    """Patch GRANTS.purge_artifact/purge_role + CHALLENGES.cancel_matching to
    log into st.log so ordering can be asserted against the existing
    resolve/publish/save/reload_snapshot/dispatch trail."""
    monkeypatch.setattr(
        tools_mod.GRANTS, "purge_artifact",
        lambda aid: st.log.append(f"purge_artifact:{aid}") or 0)
    monkeypatch.setattr(
        tools_mod.GRANTS, "purge_role",
        lambda role: st.log.append(f"purge_role:{role}") or 0)
    monkeypatch.setattr(
        tools_mod.CHALLENGES, "cancel_matching",
        lambda **kw: st.log.append(
            f"cancel_matching:role={kw.get('role')}:"
            f"artifact={kw.get('artifact')}") or 0)


async def test_plugin_update_invalidates_old_artifact_post_commit_pre_await(
        monkeypatch, tmp_path):
    """r1-B8: plugin_update captures the OLD artifact_id BEFORE the mutation
    and invalidates its grants/challenges AFTER commit, BEFORE the first
    post-commit await (reload_snapshot)."""
    st = _State()
    st.raw["plugins"].append({
        "name": "probe",
        "source": {"type": "github", "repo": "o/r", "ref": "v1",
                   "revision": "git:" + "c" * 40, "subdir": ""},
        "artifact_id": "c" * 64, "version": "1.1.0",
        "targets": ["specialist:finance"]})
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(version="2.0.0"))
    _spy_grants_and_challenges(monkeypatch, tools_mod, st)
    r = await tools_mod.plugin_update.handler({"name": "probe", "new_ref": "v2"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    old_id = "c" * 64
    assert f"purge_artifact:{old_id}" in st.log
    assert f"cancel_matching:role=None:artifact={old_id}" in st.log
    i_save = st.log.index("save")
    i_purge = st.log.index(f"purge_artifact:{old_id}")
    i_reload = st.log.index("reload_snapshot")
    assert i_save < i_purge < i_reload
    # Only the OLD artifact is invalidated, never the NEW one.
    assert f"purge_artifact:{'a' * 64}" not in st.log


async def test_aborted_plugin_update_invalidates_nothing(monkeypatch, tmp_path):
    """An ABORTED mutation (a pre-activation guard/resolve failure)
    invalidates NOTHING."""
    from plugin_store import RefNotFound
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st, publish_exc=RefNotFound("404"))
    _spy_grants_and_challenges(monkeypatch, tools_mod, st)
    r = await tools_mod.plugin_update.handler({"name": "probe", "new_ref": "v2"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is False
    assert not any(e.startswith(("purge_artifact", "purge_role", "cancel_matching"))
                  for e in st.log)


async def test_plugin_remove_invalidates_artifact_and_every_target_role(
        monkeypatch, tmp_path):
    """plugin_remove purges by the retained artifact_id AND by every former
    target's NORMALIZED role (a tier-qualified target invalidates the
    PLAIN-role grant, r2-B5)."""
    st = _State()
    _registered(st, name="probe",
               targets=["specialist:finance", "resident:butler"])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    _spy_grants_and_challenges(monkeypatch, tools_mod, st)
    r = await tools_mod.plugin_remove.handler({"name": "probe"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert f"purge_artifact:{'c' * 64}" in st.log
    assert "purge_role:finance" in st.log     # tier prefix stripped
    assert "purge_role:butler" in st.log


async def test_plugin_unassign_invalidates_by_normalized_role(
        monkeypatch, tmp_path):
    """r2-B5: a tier-qualified target ('specialist:finance') invalidates the
    PLAIN-role grant ('finance') via normalize_role. Removing ONE target
    must not purge_artifact — the plugin/artifact stays valid for its other
    targets."""
    st = _State()
    _registered(st, targets=["specialist:finance"])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    _spy_grants_and_challenges(monkeypatch, tools_mod, st)
    monkeypatch.setattr(__import__("agent"), "active_runtime",
                        _FakeRuntime({"finance": _FakeAgent({})}), raising=False)
    r = await tools_mod.plugin_unassign.handler({
        "name": "probe", "target": "specialist:finance"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True and payload["was_assigned"] is True
    assert "purge_role:finance" in st.log
    assert "cancel_matching:role=finance:artifact=None" in st.log
    assert not any(e.startswith("purge_artifact") for e in st.log)


async def test_noop_unassign_invalidates_nothing(monkeypatch, tmp_path):
    """r2-B5: a NO-OP unassign (the plugin was never assigned to this
    target) invalidates NOTHING."""
    st = _State()
    _registered(st, targets=["resident:butler"])   # NOT assigned to finance
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    _spy_grants_and_challenges(monkeypatch, tools_mod, st)
    r = await tools_mod.plugin_unassign.handler({
        "name": "probe", "target": "specialist:finance"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True and payload["was_assigned"] is False
    assert not any(e.startswith(("purge_artifact", "purge_role", "cancel_matching"))
                  for e in st.log)


# --- #211: pending specialist targets (plugin-before-specialist order) -------

def _dir_runtime(tmp_path, *, agents=None, roles=()):
    """A runtime stand-in with a REAL agents_dir tree, so the sequencer's
    pending pre-check exercises the same dir-existence source of truth
    reload.reload_agent consults (agents/<role>, agents/specialists/<role>)."""
    from types import SimpleNamespace
    agents_dir = tmp_path / "agents"
    (agents_dir / "specialists").mkdir(parents=True, exist_ok=True)
    for role in roles:
        (agents_dir / role).mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(agents=dict(agents or {}), agents_dir=str(agents_dir))


async def test_plugin_add_pending_specialist_target_reports_ok(
        monkeypatch, tmp_path):
    """#211: adding a plugin that targets a NOT-yet-installed specialist is
    the documented install order (the specialist's dependency closure hashes
    the installed plugin artifact) — ok:true + pending_targets, never
    reload_failed, and no blocking health issue."""
    import agent as agent_mod
    import plugin_health
    st = _State()
    hp = tmp_path / "plugin-health.json"
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _dir_runtime(tmp_path), raising=False)
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["specialist:mtg"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["kind"] is None
    assert payload["activation_committed"] is True
    assert payload["runtime_ready"] is False        # pending ≠ ready
    assert payload["pending_targets"] == ["specialist:mtg"]
    assert payload["reload_errors"] == []
    assert payload["reloaded"] == []
    assert r.get("is_error") is not True
    assert "dispatch:mtg" not in st.log             # reload never dispatched
    report = plugin_health.load_report(hp)
    assert all(i["reason_code"] != "reload_failed" for i in report["issues"])


async def test_plugin_add_installed_specialist_target_still_dispatches(
        monkeypatch, tmp_path):
    """A specialist whose agent directory EXISTS is not pending — the reload
    dispatch (and its failure semantics) are unchanged."""
    import agent as agent_mod
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      dispatch_status="error")
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        _dir_runtime(tmp_path, roles=["specialists/mtg"]), raising=False)
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["specialist:mtg"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["kind"] == "reload_failed"
    assert payload["pending_targets"] == []
    assert "dispatch:mtg" in st.log


async def test_plugin_add_unknown_resident_role_still_reload_failed(
        monkeypatch, tmp_path):
    """Regression: resident targets NEVER classify pending — an unknown
    resident role keeps today's hard failure exactly."""
    import agent as agent_mod
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      dispatch_status="error")
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _dir_runtime(tmp_path), raising=False)
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:ghost"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["kind"] == "reload_failed"
    assert payload["pending_targets"] == []
    assert payload["reload_errors"]


async def test_plugin_add_specialist_target_ignores_resident_name_collision(
        monkeypatch, tmp_path):
    """Round-1 review P1: a specialist: target is installed ONLY at
    agents/specialists/<role>. A resident dir sharing the bare name must NOT
    read as installed — that would dispatch a cross-tier RESIDENT reload for
    a specialist target instead of reporting it pending."""
    import agent as agent_mod
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    # agents/mtg exists as a RESIDENT-position dir; specialists/mtg does not.
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _dir_runtime(tmp_path, roles=["mtg"]), raising=False)
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["specialist:mtg"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["pending_targets"] == ["specialist:mtg"]
    assert payload["reload_errors"] == []
    assert "dispatch:mtg" not in st.log     # the resident was NOT reloaded


async def test_plugin_add_mixed_live_reload_and_pending_specialist(
        monkeypatch, tmp_path):
    """One live resident reload ok + one pending specialist: ok:true, only
    the specialist is pending, runtime_ready stays false."""
    import agent as agent_mod
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        _dir_runtime(tmp_path, roles=["assistant"]), raising=False)
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant", "specialist:mtg"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["kind"] is None
    assert payload["reloaded"] == ["resident:assistant"]
    assert payload["pending_targets"] == ["specialist:mtg"]
    assert payload["runtime_ready"] is False
    assert "dispatch:assistant" in st.log and "dispatch:mtg" not in st.log


async def test_plugin_add_mixed_reload_error_and_pending_specialist(
        monkeypatch, tmp_path):
    """One REAL reload error + one pending specialist: errors win (ok:false,
    kind reload_failed) but pending_targets is still reported."""
    import agent as agent_mod
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      dispatch_status="error")
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        _dir_runtime(tmp_path, roles=["assistant"]), raising=False)
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant", "specialist:mtg"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["kind"] == "reload_failed"
    assert payload["pending_targets"] == ["specialist:mtg"]
    assert [e["target"] for e in payload["reload_errors"]] == \
        ["resident:assistant"]


async def test_plugin_assign_pending_specialist_gets_same_treatment(
        monkeypatch, tmp_path):
    """plugin_assign uses the same sequencer — assigning to a still-
    uninstalled specialist is pending, not reload_failed."""
    import agent as agent_mod
    st = _State()
    _registered(st, targets=[])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _dir_runtime(tmp_path), raising=False)
    r = await tools_mod.plugin_assign.handler({
        "name": "probe", "target": "specialist:mtg"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["pending_targets"] == ["specialist:mtg"]
    assert payload["runtime_ready"] is False
    assert "dispatch:mtg" not in st.log


async def test_plugin_unassign_absent_path_still_dispatches(
        monkeypatch, tmp_path):
    """Regression: expect='absent' (unassign/remove) NEVER classifies
    pending — the reload dispatch still runs even when the specialist's
    agent directory is absent."""
    import agent as agent_mod
    st = _State()
    _registered(st, targets=["specialist:mtg"])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _dir_runtime(tmp_path), raising=False)
    r = await tools_mod.plugin_unassign.handler({
        "name": "probe", "target": "specialist:mtg"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["pending_targets"] == []
    assert "dispatch:mtg" in st.log             # absent path unchanged


# --- #676 INV-TOOL-006: a persisting committed removal discloses what survives -


async def test_plugin_remove_discloses_persisting_cli_data_but_unassign_does_not_v2(
        monkeypatch, tmp_path):
    """Red case for INV-TOOL-006 (issue #676, option 2 — honest disclosure).

    A COMMITTED plugin removal — here the committed-but-not-ready arm, the
    weakest ok payload the operator can be handed — must disclose that the
    CLI-managed per-plugin persistent data directory was NOT deleted, that no
    provider revocation was performed, and that a reinstall re-attaches. A
    NON-persisting envelope (a no-op plugin_unassign) carries none of it, so an
    unconditional implementation cannot satisfy this pin.

    (v2: the first specification stubbed the sequencer on the shared module
    attribute and never restored it, so its own negative arm could not reach a
    successful no-op unassign. Re-specified and re-accepted; the superseded
    version is gone rather than edited.)
    """
    disclosure_keys = {
        "plugin_data_may_remain",
        "provider_revocation_performed",
        "plugin_data_note",
        "plugin_data_plugins",
    }

    # Positive: removal has committed, but runtime convergence failed.
    st = _State()
    _registered(st, name="probe", targets=["resident:assistant"])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    real_seq = tools_mod._reload_and_verify_targets

    async def stub_seq(name, targets, *, expect):
        return {"ok": False, "kind": "postcondition_failed",
                "activation_committed": True, "runtime_ready": False,
                "reloaded": [], "reload_errors": [], "pending_targets": [],
                "verify": {}}

    monkeypatch.setattr(tools_mod, "_reload_and_verify_targets", stub_seq)
    r = await tools_mod.plugin_remove.handler({"name": "probe"})
    removed = json.loads(r["content"][0]["text"])

    # Both predicates establish the committed-but-not-ready removal arm.
    assert sum((removed["activation_committed"] is True,
                removed["runtime_ready"] is False)) == 2
    # Exactly the three ordinary-removal disclosures; the cascaded-removal
    # plugin-list field belongs to specialist_uninstall and is absent here.
    assert sum(key in removed for key in disclosure_keys) == 3
    # Both required boolean values are exact — a present-but-false "may remain"
    # or a claimed provider revocation is worse than silence.
    assert sum((removed.get("plugin_data_may_remain") is True,
                removed.get("provider_revocation_performed") is False)) == 2
    note = removed.get("plugin_data_note", "").lower()
    assert sum(fragment in note for fragment in (
        "cli-managed", "persistent", "not deleted", "reinstall")) == 4

    # Restore the real module function before the separate negative operation.
    monkeypatch.setattr(tools_mod, "_reload_and_verify_targets", real_seq)

    # Negative: a no-op unassignment is not a committed plugin removal.
    st2 = _State()
    _registered(st2, name="probe", targets=["resident:butler"])
    tools_mod2 = _wire(monkeypatch, tmp_path, st2, publish=_pr())
    r2 = await tools_mod2.plugin_unassign.handler({
        "name": "probe", "target": "specialist:finance"})
    unassigned = json.loads(r2["content"][0]["text"])

    assert unassigned["ok"] is True
    assert sum(key in unassigned for key in disclosure_keys) == 0


def _disclosure_keys():
    return {"plugin_data_may_remain", "provider_revocation_performed",
            "plugin_data_note", "plugin_data_plugins"}


async def test_plugin_remove_refusals_disclose_nothing(monkeypatch, tmp_path):
    """#676: a refusal precedes the registry write — nothing committed, so
    nothing survived, and a survival warning there would be a claim about state
    the operator still has. All three pre-commit refusals carry zero fields."""
    import plugin_registry as preg
    from plugin_registry import RegistryData

    st = _State()
    _registered(st, name="probe", targets=["resident:assistant"])
    st.raw["plugins"].append({
        "name": "fin.owned", "owner": "specialist:finance",
        "artifact_id": "d" * 64,
        "version": "1.0.0", "targets": [],
        "source": {"type": "github", "repo": "o/r", "ref": "v1",
                   "revision": "git:" + "d" * 40, "subdir": ""}})
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())

    kinds = []
    for name in ("ghost", "fin.owned"):
        r = await tools_mod.plugin_remove.handler({"name": name})
        payload = json.loads(r["content"][0]["text"])
        kinds.append(payload["kind"])
        assert sum(k in payload for k in _disclosure_keys()) == 0

    monkeypatch.setattr(preg, "load_registry", lambda path=None: RegistryData(
        raw={}, entries=[], entry_issues=[], valid=False))
    r = await tools_mod.plugin_remove.handler({"name": "probe"})
    payload = json.loads(r["content"][0]["text"])
    kinds.append(payload["kind"])
    assert sum(k in payload for k in _disclosure_keys()) == 0
    assert kinds == ["not_registered", "owned_by_specialist", "registry_invalid"]


async def test_plugin_update_discloses_nothing(monkeypatch, tmp_path):
    """#676 negative arm: an update replaces the artifact and keeps the entry —
    the data surviving it is by design and is not a removal."""
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(version="2.0.0"))
    r = await tools_mod.plugin_update.handler({"name": "probe", "new_ref": "v2"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert sum(k in payload for k in _disclosure_keys()) == 0


def test_the_disclosure_notes_claim_only_what_they_can():
    """Terra diff-review r6: the note is relayed VERBATIM to the operator, so
    its own opening is an operator-facing claim. "Casa removed the registry
    entry only" was false — a removal also purges the artifact grants, cancels
    the challenges and revokes the trigger consents. Third occurrence of one
    shape (an operator-facing claim overstating what Casa did), so the clause
    is cut rather than qualified: each note states the two facts it can
    establish and nothing about Casa's internal teardown."""
    import tools as tools_mod

    notes = (tools_mod._PLUGIN_DATA_NOTE_COMMITTED,
             tools_mod._PLUGIN_DATA_NOTE_ATTEMPTED,
             tools_mod._PLUGIN_DATA_NOTE_INDETERMINATE)
    assert sum("registry entr" in n.lower() and "only" in n.lower()
               for n in notes) == 0
    # Every revocation statement names the provider side.
    for n in notes:
        low = n.lower()
        assert low.count("revocation") == low.count("provider revocation")
        assert "revoked" not in low or "revoke at the provider" in low
    assert sum("not deleted" in n.lower() for n in notes) == 3


async def test_plugin_remove_description_discloses_survival_without_claiming_more(
        monkeypatch, tmp_path):
    """#676: the description is the only surface an agent reads BEFORE calling.
    It must state the survival — and must not claim a deletion or revocation."""
    import tools as tools_mod
    desc = tools_mod.plugin_remove.description.lower()
    assert sum(fragment in desc for fragment in (
        "cli-managed", "persistent", "not delete", "reinstall",
        "no provider-side revocation")) == 5
    for claim in ("revokes the", "deletes the plugin's data",
                  "authorization is revoked", "credentials are deleted"):
        assert claim not in desc


def test_removal_recipes_instruct_the_engager_to_surface_the_note():
    """#676: the payload-to-operator seam. Every disclosure field can be
    present and correct while the shipped doctrine never tells the engager to
    relay it — in which case the operator learns nothing, which is the outcome
    the change exists to prevent."""
    from pathlib import Path
    import tools as tools_mod

    root = (Path(tools_mod.__file__).parent / "defaults/agents/executors"
            / "configurator/doctrine/recipes")
    # Whitespace-normalized: a fragment assertion that a line rewrap can break
    # is pinning the prose's layout, not its instruction.
    def _flat(p):
        return " ".join((root / p).read_text().lower().split())

    remove = _flat("plugin/remove.md")
    uninstall = _flat("specialist/uninstall.md")

    assert sum(fragment in remove for fragment in (
        "claude_plugin_data", "not deleted", "re-attaches",
        "no provider-side revocation", "plugin_data_note")) == 5
    # Naming the field is not instructing the engager to relay it: the
    # disclosure paragraph names it too, so a mutation deleting the reporting
    # step survived a mention-only assertion.
    assert sum(fragment in remove for fragment in (
        "report `plugin_data_note` to the operator verbatim",
        "restate it as a deletion or a revocation")) == 2
    # Sol/Terra diff-review r1: the recipe covers unassign TOO, and
    # plugin_unassign never carries the note — an unconditional reporting step
    # invites a survival warning after an operation that removed nothing.
    assert "only when the result carries `plugin_data_note`" in remove
    # Sol diff-review r2: the payload says "may remain" because Casa cannot see
    # whether the plugin ever stored anything. A recipe that tells the engager
    # the operator will learn authorizations SURVIVED converts that into a
    # confident claim about credentials Casa never observed. Both recipes must
    # carry the qualifier, and neither may make the categorical claim.
    assert sum(fragment in text for text, fragment in (
        (remove, "may have survived"), (uninstall, "may have survived"))) == 2
    for text in (remove, uninstall):
        assert "authorizations survived" not in text
    # Sol diff-review r3: both removal recipes must carry the raise-path
    # guidance, not just the direct one — a raised uninstall can persist
    # cascaded removals with no envelope to carry the caveat, and an asymmetry
    # here leaves that seam open on the path that removes MORE.
    assert sum("may have taken effect" in text for text in (remove, uninstall)) == 2
    assert sum("`plugin_list()`" in text for text in (remove, uninstall)) == 2
    # Terra diff-review r4: a removal DOES revoke Casa's own authorizations —
    # _invalidate_lifecycle purges the artifact grants and trigger consents,
    # _remove_plugin_callbacks revokes the persisted callback consents. An
    # unqualified "Casa revoked nothing" contradicts the code; every no-
    # revocation statement is provider-scoped, and both recipes say which side
    # of Casa each fact lives on.
    assert sum("at the provider" in text for text in (remove, uninstall)) == 2
    for text in (remove, uninstall):
        assert "nothing was revoked. " not in text
        assert "casa performed neither. " not in text
    # Rounds 4 and 5, same shape twice: an operator-facing claim about
    # revocation that overstates what Casa did. Round 4 killed the unqualified
    # "Casa revoked nothing" (false — a removal DOES tear down Casa's own
    # grants and consents). Round 5 found the replacement equally wrong in two
    # ways: `specialist_uninstall` never calls _remove_plugin_callbacks at all,
    # and even on the direct path the revoke is best-effort and swallows its
    # own failure. So the affirmative claim is CUT, not sharpened — neither
    # recipe asserts a Casa-side revocation in either direction.
    for text in (remove, uninstall):
        assert "consents for the plugin are revoked" not in text
        assert "consents for those plugins are revoked" not in text
    assert sum(fragment in text for text, fragment in (
        (remove, "not reported in the result"),
        (uninstall, "this result does not report it"))) == 2

    # Sol diff-review r8: an upgrade and a rollback swap the owned set
    # wholesale, so either can leave a plugin removed when its compensation
    # fails. Their recipes owe the same conditional relay — unpinned recipe
    # prose is exactly the seam round 3 measured open.
    # Sol diff-review a3-r1: the install recipe owes the same relay. The
    # install commit's swap runs unconditionally, so it can drop a stale owned
    # entry and return the disclosure — and a payload no recipe tells the
    # engager to relay is a disclosure the operator never sees, which is the
    # outcome this change exists to prevent.
    for name in ("specialist/upgrade.md", "specialist/rollback.md",
                 "specialist/install.md"):
        text = _flat(name)
        assert sum(fragment in text for fragment in (
            "if the result carries `plugin_data_note`",
            "relay it verbatim with the names in `plugin_data_plugins`")) == 2
        # Sol diff-review a3-r2: the relay must sit OUTSIDE the state
        # branches. The failed-compensation arm returns ok:false with no
        # `state` field at all, so an instruction living under
        # `state == "active"` is never reached on exactly the outcome whose
        # removal PERSISTED. All four specialist recipes say so in the same
        # words, and the pin covers the uninstall recipe too.
        assert "on any outcome, including an `ok:false` result" in text
        # Sol diff-review r10: the note is emitted on the INDETERMINATE arm too,
        # where the removal is explicitly unknown. A recipe that glosses the
        # field as "a plugin ended up removed" turns that into a confirmed
        # removal in the operator's report.
        assert "do not restate it as a confirmed removal" in text
        assert "ended up removed" not in text
        # Terra handback review (attempt 2): the note now also reaches these
        # two recipes from the SUCCESS path — an upgrade or a rollback whose
        # owned-set swap dropped a plugin — where it IS a confirmed removal.
        # The relay instruction has to admit that arm, or an engager told only
        # about the compensation/indeterminate cases hedges a removal that
        # actually happened.
        assert sum(fragment in text for fragment in (
            "the successful owned-set swap dropped those entries",
            "unless the note itself says so")) == 2
    # The plugin-env clarification: clearing an entry needs its OWN reload, and
    # is not credential deletion.
    assert sum(fragment in remove for fragment in (
        'casa_reload(scope="plugin_env")',
        "neither credential deletion nor provider revocation")) == 2
    assert sum(fragment in uninstall for fragment in (
        "plugin_data_note", "plugin_data_plugins", "claude_plugin_data",
        "not deleted", "no revocation was performed at the provider")) == 5
    assert "on any outcome, including an `ok:false` result" in uninstall
    assert sum(fragment in uninstall for fragment in (
        "report it to the operator verbatim",
        "do not restate it as a deletion or a revocation")) == 2
