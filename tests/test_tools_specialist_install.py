"""Task N1b Step 26: tool-level tests for the configurator MCP tools
specialist_install_inspect/specialist_install_commit (tools.py). The brief
provides no tests for these tools directly — designed here per its
instructions: (a) a commit whose recomputed root_digest mismatches the
caller-supplied args must refuse without ever calling
commit_specialist_install; (b) a commit with no recorded consent ack must
refuse with kind "consent_missing", never touching the real /data acks
store; (c) an inspect whose underlying inspect_specialist_repo fails must
surface the same structured kind, never raise."""
import json

import pytest


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _inject_fake_receipt(monkeypatch, *, plugins=(), slug="mtg"):
    """Task 10: the commit/upgrade tools require a loadable receipt (by opaque
    id) before anything else. Inject a fake so tests exercising the LATER gates
    (root_digest, consent) reach them. receipt_digest="" so the consent
    identity matches acks recorded with the default receipt_digest.

    Whole-branch D: the fake carries `slug` (matching the component under test)
    so `_assert_receipt_matches_inspection`'s id/digest/slug binding passes — a
    real SourceReceipt always has a slug."""
    import specialist_receipt
    from types import SimpleNamespace
    fake = SimpleNamespace(receipt_id="a" * 32, receipt_digest="", slug=slug,
                           plugins=tuple(plugins))
    monkeypatch.setattr(specialist_receipt, "load", lambda rid, *a, **k: fake)
    return fake


def _stub_bundle_sequencer(monkeypatch):
    """No-op the bundle sequencer + journal-complete so tool-wiring tests don't
    touch the real plugin snapshot / health / journal files."""
    import tools as tools_mod
    import specialist_bundle_journal

    async def _seq(slug, *, removed_artifact_ids, targets_removed):
        return {"ok": True, "reloaded": [], "verify": {},
                "reload_errors": [], "removed_artifact_ids": list(removed_artifact_ids)}

    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq)
    monkeypatch.setattr(specialist_bundle_journal, "complete", lambda p: None)


@pytest.mark.asyncio
async def test_specialist_install_commit_rejects_a_changed_root_digest(
    monkeypatch, tmp_path,
) -> None:
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    import specialist_install
    from tools import specialist_install_commit

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")

    # commit_specialist_install is the ONLY function that writes into the
    # CAS/specialists tree (its own docstring) — a checksum mismatch must be
    # rejected BEFORE it is ever called, so nothing is persisted. Spy on the
    # module attribute the tool's local `from specialist_install import
    # commit_specialist_install` re-reads at call time.
    def _must_not_be_called(*args, **kwargs):
        raise AssertionError(
            "commit_specialist_install must never be called on a root_digest mismatch")

    monkeypatch.setattr(specialist_install, "commit_specialist_install", _must_not_be_called)
    _inject_fake_receipt(monkeypatch)

    result = await specialist_install_commit.handler({
        "component_id": component.component_id, "version": component.version,
        "slug": component.slug, "staged_dir": str(staged), "receipt_id": "a" * 32,
        "root_digest": "sha256:" + "f" * 64,  # deliberately wrong — never the real digest
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "checksum_changed"


@pytest.mark.asyncio
async def test_specialist_install_commit_rejects_without_a_recorded_consent_ack(
    monkeypatch, tmp_path,
) -> None:
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import compute_install_root_digest, resolve_dependency_closure
    import specialist_install_consent
    from specialist_install_consent import SpecialistInstallAckStore
    from tools import specialist_install_commit

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())

    # The tool constructs its ack store via a bare `SpecialistInstallAckStore()`
    # call (production default path /data/specialist_install_acks.json) —
    # never write to that real path from a test. The tool's local
    # `from specialist_install_consent import SpecialistInstallAckStore`
    # re-reads the module attribute at call time, so patching it here is
    # sufficient — redirect the no-arg construction to a tmp_path file.
    tmp_acks_path = tmp_path / "acks.json"

    class _TmpAckStore(SpecialistInstallAckStore):
        def __init__(self, path=None):  # noqa: ARG002 — tool always calls with no args
            super().__init__(path=tmp_acks_path)

    monkeypatch.setattr(specialist_install_consent, "SpecialistInstallAckStore", _TmpAckStore)
    _inject_fake_receipt(monkeypatch)

    result = await specialist_install_commit.handler({
        "component_id": component.component_id, "version": component.version,
        "slug": component.slug, "staged_dir": str(staged), "root_digest": root_digest,
        "receipt_id": "a" * 32,
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "consent_missing"
    # Nothing persisted: is_acked() never writes, and consent_missing raises
    # before commit_specialist_install's first CAS/InstanceDir write.
    assert not tmp_acks_path.exists()


@pytest.mark.asyncio
async def test_specialist_install_inspect_surfaces_a_structured_failure(monkeypatch) -> None:
    import specialist_install
    from specialist_install import SpecialistInstallError
    from tools import specialist_install_inspect

    def _boom(*args, **kwargs):
        raise SpecialistInstallError("fetch_failed", "simulated fetch failure")

    # The tool's local `from specialist_install import inspect_specialist_repo`
    # re-reads the module attribute at call time — patch it here.
    monkeypatch.setattr(specialist_install, "inspect_specialist_repo", _boom)

    result = await specialist_install_inspect.handler({"repo": "owner/repo", "ref": "main"})

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "fetch_failed"


# ---------------------------------------------------------------------------
# specialist_upgrade / specialist_rollback / specialist_uninstall (Task N1c)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_specialist_install_commit_requires_a_receipt_id(monkeypatch, tmp_path) -> None:
    """Task 10: the commit tool loads the trusted receipt by opaque id ONLY;
    a missing/unloadable id fails closed BEFORE any staged bytes are read."""
    from tools import specialist_install_commit

    result = await specialist_install_commit.handler({
        "component_id": "x/y", "version": "0.1.0", "slug": "mtg",
        "staged_dir": str(tmp_path / "staged"), "root_digest": "sha256:" + "a" * 64,
        # no receipt_id
    })
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "receipt_required"


@pytest.mark.asyncio
async def test_commit_sequencer_failure_compensates_with_new_artifact_ids(
    monkeypatch, tmp_path,
) -> None:
    """Task 10 sequencer-failure compensation: when _bundle_reload_and_verify
    raises, the tool rolls the disk state back and re-runs the sequencer with
    the NEW set's artifact ids as `removed` (un-publishing the runtime state),
    completes the journal, then re-raises."""
    from types import SimpleNamespace
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import compute_install_root_digest, resolve_dependency_closure
    import specialist_install
    import specialist_bundle_journal
    import tools as tools_mod
    from tools import specialist_install_commit

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
    _inject_fake_receipt(monkeypatch)

    rolled_back = []
    completed = []
    txn = SimpleNamespace(
        slug="mtg", removed_artifact_ids=(), new_artifact_ids=("NEWAID",),
        journal_path="/tmp/j.json",
        rollback_disk=lambda: rolled_back.append(True))

    def _fake_commit(*a, **k):
        return SimpleNamespace(slug="mtg", state="active"), txn

    monkeypatch.setattr(specialist_install, "commit_specialist_install", _fake_commit)
    monkeypatch.setattr(specialist_bundle_journal, "complete",
                        lambda p: completed.append(p))

    seq_calls = []

    async def _seq(slug, *, removed_artifact_ids, targets_removed):
        seq_calls.append(list(removed_artifact_ids))
        if len(seq_calls) == 1:
            raise RuntimeError("reload blew up")
        return {"reloaded": [], "verify": {}}

    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq)

    with pytest.raises(RuntimeError):
        await specialist_install_commit.handler({
            "component_id": component.component_id, "version": component.version,
            "slug": "mtg", "staged_dir": str(staged), "root_digest": root_digest,
            "receipt_id": "a" * 32,
        })

    assert rolled_back == [True]                 # disk restored
    assert seq_calls == [[], ["NEWAID"]]         # compensating pass un-publishes the NEW set
    assert completed == ["/tmp/j.json"]          # journal completed


@pytest.mark.asyncio
async def test_commit_seq_not_ready_compensates_and_reports_ok_false(
    monkeypatch, tmp_path,
) -> None:
    """Whole-branch B: a sequencer that returns ok:false (a not-ready owned
    binding / failed postcondition — NOT an exception) must compensate (roll the
    disk back, un-publish, complete the journal) and surface ok:false, never
    complete the journal + report success."""
    from types import SimpleNamespace
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import compute_install_root_digest, resolve_dependency_closure
    import specialist_install
    import specialist_bundle_journal
    import tools as tools_mod
    from tools import specialist_install_commit

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
    _inject_fake_receipt(monkeypatch)

    rolled_back = []
    completed = []
    txn = SimpleNamespace(
        slug="mtg", removed_artifact_ids=(), new_artifact_ids=("NEWAID",),
        journal_path="/tmp/j.json",
        rollback_disk=lambda: rolled_back.append(True))
    monkeypatch.setattr(specialist_install, "commit_specialist_install",
                        lambda *a, **k: (SimpleNamespace(slug="mtg", state="active"), txn))
    monkeypatch.setattr(specialist_bundle_journal, "complete",
                        lambda p: completed.append(p))

    seq_calls = []

    async def _seq(slug, *, removed_artifact_ids, targets_removed):
        seq_calls.append(list(removed_artifact_ids))
        if len(seq_calls) == 1:
            return {"ok": False, "kind": "postcondition_failed", "reloaded": [],
                    "reload_errors": [], "not_ready": ["mtg.mtg"],
                    "absent_violations": [], "verify": {}}
        return {"ok": True, "reloaded": [], "verify": {}}

    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq)

    result = await specialist_install_commit.handler({
        "component_id": component.component_id, "version": component.version,
        "slug": "mtg", "staged_dir": str(staged), "root_digest": root_digest,
        "receipt_id": "a" * 32,
    })
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "postcondition_failed"
    assert rolled_back == [True]                  # disk restored
    assert seq_calls == [[], ["NEWAID"]]          # compensating pass un-publishes NEW set
    assert completed == ["/tmp/j.json"]           # journal completed (not left dangling)


@pytest.mark.asyncio
async def test_compensate_fresh_install_runs_uninstall_shaped_sweep(monkeypatch) -> None:
    """P1-2: compensating a FAILED fresh install (before-state carries no
    active.yaml) must run the uninstall-shaped sequencer — evict the just-
    created live agent + verify absent — not the install-shaped reconstruct.
    Reusing the forward install-shaped call left the fresh-install rollback's
    live agent reachable."""
    from types import SimpleNamespace
    import tools as tools_mod
    import specialist_bundle_journal

    captured: dict = {}

    async def _seq(slug, *, removed_artifact_ids, targets_removed):
        captured["targets_removed"] = list(targets_removed)
        captured["removed"] = list(removed_artifact_ids)
        return {"ok": True, "reloaded": [], "verify": {}}

    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq)
    completed: list = []
    monkeypatch.setattr(specialist_bundle_journal, "complete",
                        lambda p: completed.append(p))

    txn = SimpleNamespace(
        slug="mtg", new_artifact_ids=("NEWAID",), journal_path="/tmp/j.json",
        before_tuple_files={"active.yaml": None},   # NOT installed before
        rollback_disk=lambda: None)
    res = await tools_mod._bundle_compensate(txn)

    assert res == {"disk_ok": True, "runtime_ok": True}
    assert captured["targets_removed"] == ["specialist:mtg"]   # evict sweep
    assert captured["removed"] == ["NEWAID"]                    # un-publish the new set
    assert completed == ["/tmp/j.json"]                        # journal completed


@pytest.mark.asyncio
async def test_compensate_upgrade_reconstructs_prior_generation(monkeypatch) -> None:
    """P1-2 converse: compensating a FAILED upgrade (before-state HAD an
    active.yaml — the specialist stays installed after the rollback)
    reconstructs the prior generation's agent (install-shaped, targets_removed
    empty), never evicts it."""
    from types import SimpleNamespace
    import tools as tools_mod
    import specialist_bundle_journal

    captured: dict = {}

    async def _seq(slug, *, removed_artifact_ids, targets_removed):
        captured["targets_removed"] = list(targets_removed)
        return {"ok": True, "reloaded": [], "verify": {}}

    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq)
    monkeypatch.setattr(specialist_bundle_journal, "complete", lambda p: None)

    txn = SimpleNamespace(
        slug="mtg", new_artifact_ids=("NEWAID",), journal_path="/tmp/j.json",
        before_tuple_files={"active.yaml": "id: specialist:mtg\n"},  # installed before
        rollback_disk=lambda: None)
    await tools_mod._bundle_compensate(txn)

    assert captured["targets_removed"] == []   # reconstruct, not evict


@pytest.mark.asyncio
async def test_seq_failure_with_failed_rollback_tags_compensation_failed(
    monkeypatch, tmp_path,
) -> None:
    """P1-1: when the sequencer fails AND the disk rollback itself raises (e.g.
    an unreadable registry — rollback_disk fails closed rather than persist a
    partial doc), the journal is LEFT in-progress (boot reconciliation is the
    backstop) and the ok:false envelope is tagged compensation_failed."""
    from types import SimpleNamespace
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import compute_install_root_digest, resolve_dependency_closure
    import specialist_install
    import specialist_bundle_journal
    import tools as tools_mod
    from tools import specialist_install_commit

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
    _inject_fake_receipt(monkeypatch)

    completed: list = []

    def _boom_rollback():
        raise RuntimeError("registry unreadable — cannot roll back")

    txn = SimpleNamespace(
        slug="mtg", removed_artifact_ids=(), new_artifact_ids=("NEWAID",),
        journal_path="/tmp/j.json", before_tuple_files={"active.yaml": None},
        rollback_disk=_boom_rollback)
    monkeypatch.setattr(specialist_install, "commit_specialist_install",
                        lambda *a, **k: (SimpleNamespace(slug="mtg", state="active"), txn))
    monkeypatch.setattr(specialist_bundle_journal, "complete",
                        lambda p: completed.append(p))

    async def _seq(slug, *, removed_artifact_ids, targets_removed):
        return {"ok": False, "kind": "postcondition_failed", "reloaded": [],
                "reload_errors": [], "not_ready": ["mtg.mtg"],
                "absent_violations": [], "verify": {}}

    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq)

    payload = _payload(await specialist_install_commit.handler({
        "component_id": component.component_id, "version": component.version,
        "slug": "mtg", "staged_dir": str(staged), "root_digest": root_digest,
        "receipt_id": "a" * 32,
    }))
    assert payload["ok"] is False
    assert payload["kind"] == "postcondition_failed"
    assert payload["compensation_failed"] is True   # rollback failed → boot backstop
    assert completed == []                           # journal LEFT in-progress


@pytest.mark.asyncio
async def test_sequencer_passes_env_pending_owned_plugin_real_verify(
    monkeypatch, tmp_path,
) -> None:
    """P1-3: an owned bundled plugin declaring an UNRESOLVED secret lands in
    pending-configuration — the REAL _tool_verify_plugin_state reports top-level
    ready=False (secret unresolved) with NO integrity/binding reason, and the
    REAL bundle sequencer must NOT treat that as a not-ready failure (no
    compensation). Only the reload/agent/health I/O seams are stubbed — verify
    and the sequencer's gate run for real against a real registry + store
    artifact."""
    import agent as agent_mod
    import plugin_registry
    import tools as tools_mod
    import system_requirements.manifest as mani
    import plugin_env_conf as pec
    from plugin_fixtures import entry, mk_artifact
    from plugin_store import content_checksum, write_metadata

    store = tmp_path / "store"
    reg_path = tmp_path / "registry.json"
    # Owned entry whose (repo/ref/revision/subdir) match mk_artifact's metadata
    # defaults (o/r, v1, git:aaa…, "") so artifact_verdict passes. The plugin's
    # .mcp.json declares ${MTG_API_KEY} — an unresolved secret.
    e = entry("mtg.mtg", ["specialist:mtg"])
    e["owner"] = "specialist:mtg"
    e["manifest_name"] = "mtg"
    root = mk_artifact(store, "mtg.mtg", e["artifact_id"], manifest_name="mtg",
                       mcp_servers={"s": {"env": {"K": "${MTG_API_KEY}"}}})
    # An OWNED artifact's metadata must carry manifest_name too (artifact_verdict
    # checks it for owned entries); mk_artifact doesn't forward it, so re-stamp
    # the metadata with the same identity + manifest_name (content unchanged).
    write_metadata(root, name="mtg.mtg", repo="o/r", ref="v1",
                   revision="git:" + "a" * 40, subdir="", artifact_id=e["artifact_id"],
                   version="1.0.0", checksum=content_checksum(root), manifest_name="mtg")
    reg_path.write_text(
        json.dumps({"schema_version": 1, "plugins": [e]}), encoding="utf-8")

    # Redirect the production-default registry/store/manifest/env-conf reads at
    # tmp — verify (called from the sequencer with NO test seams) uses them.
    real_load = plugin_registry.load_registry
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda path=reg_path: real_load(path))
    monkeypatch.setattr(plugin_registry, "STORE_ROOT", store)
    monkeypatch.setattr(mani, "MANIFEST_PATH", tmp_path / "sysreq.yaml")      # absent → no sysreqs
    monkeypatch.setattr(pec, "PLUGIN_ENV_CONF_PATH", tmp_path / "plugin-env.conf")  # absent → secret unresolved
    monkeypatch.delenv("MTG_API_KEY", raising=False)

    # Non-verify I/O seams: no runtime, no snapshot reload, no health/notify/invalidate.
    monkeypatch.setattr(agent_mod, "active_runtime", None, raising=False)
    monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", lambda issues: None)

    async def _notify():
        return None

    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible", _notify)
    monkeypatch.setattr(tools_mod, "_invalidate_lifecycle", lambda **k: None)

    seq = await tools_mod._bundle_reload_and_verify(
        "mtg", removed_artifact_ids=[], targets_removed=[])

    v = seq["verify"]["mtg.mtg"]
    assert v["ready"] is False                       # config-pending (secret unresolved)
    assert v["secrets"][0]["var"] == "MTG_API_KEY"
    assert v["secrets"][0]["status"] == "unresolved"
    # #533: config-pending now carries its NAMED readiness code — still no
    # integrity/binding failure code.
    assert v["reasons"] == ["env_unresolved"]
    # ...yet the sequencer does NOT compensate — pending-config is verified-legal.
    assert seq["not_ready"] == []
    assert seq["ok"] is True


@pytest.mark.asyncio
async def test_bundle_sequencer_uninstall_evicts_and_verifies_absent(monkeypatch) -> None:
    """Whole-branch C: the uninstall sequencer runs the full agents add/EVICT
    sweep (not a single-role reconstruct) and fails the postcondition unless the
    removed specialist's agent + scoped names are absent."""
    from types import SimpleNamespace
    import agent as agent_mod
    import plugin_registry
    import reload as reload_mod
    import tools as tools_mod

    calls = []

    async def _dispatch(scope, *, runtime, role=None):
        calls.append((scope, role))
        return {"status": "ok"}

    monkeypatch.setattr(reload_mod, "dispatch", _dispatch)
    monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(plugin_registry, "load_registry", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(plugin_registry, "owned_entries_for", lambda slug, reg: [])
    monkeypatch.setattr(plugin_registry, "resolve_for",
                        lambda t: SimpleNamespace(plugins=[]))
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", lambda issues: None)

    async def _notify():
        return None

    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible", _notify)
    monkeypatch.setattr(tools_mod, "_invalidate_lifecycle", lambda **k: None)

    # Agent already evicted -> the full agents sweep is dispatched, ok.
    monkeypatch.setattr(agent_mod, "active_runtime",
                        SimpleNamespace(agents={}, agents_dir=None), raising=False)
    seq = await tools_mod._bundle_reload_and_verify(
        "mtg", removed_artifact_ids=[], targets_removed=["specialist:mtg"])
    assert ("agents", None) in calls          # full add/evict sweep, not ("agent","mtg")
    assert seq["ok"] is True

    # Agent STILL registered -> absent violation -> ok False.
    monkeypatch.setattr(agent_mod, "active_runtime",
                        SimpleNamespace(agents={"mtg": object()}, agents_dir=None),
                        raising=False)
    seq2 = await tools_mod._bundle_reload_and_verify(
        "mtg", removed_artifact_ids=[], targets_removed=["specialist:mtg"])
    assert seq2["ok"] is False
    assert "agent:mtg" in seq2["absent_violations"]


@pytest.mark.asyncio
async def test_uninstall_tool_maps_bundle_required_to_envelope(monkeypatch) -> None:
    # Whole-branch M: a typed refusal from uninstall_specialist must surface as
    # a structured ok:false, not a raw exception.
    import specialist_install
    from specialist_install import SpecialistInstallError
    from tools import specialist_uninstall

    def _boom(*, slug, **kwargs):
        raise SpecialistInstallError("bundle_required", "owned entries present")

    monkeypatch.setattr(specialist_install, "uninstall_specialist", _boom)
    result = await specialist_uninstall.handler({"slug": "mtg"})
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "bundle_required"


@pytest.mark.asyncio
async def test_upgrade_tool_maps_bad_staged_dir(monkeypatch, tmp_path) -> None:
    # Whole-branch M: a vanished/corrupt staged_dir surfaces as staged_dir_invalid
    # (mirrors specialist_install_commit's guard), never a raw OSError.
    from tools import specialist_upgrade
    _inject_fake_receipt(monkeypatch)
    result = await specialist_upgrade.handler({
        "slug": "mtg", "component_id": "c/x", "version": "1.0.0",
        "root_digest": "sha256:" + "a" * 64,
        "staged_dir": str(tmp_path / "does-not-exist"), "receipt_id": "a" * 32,
    })
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "staged_dir_invalid"


@pytest.mark.asyncio
async def test_specialist_upgrade_rejects_a_changed_root_digest(monkeypatch, tmp_path) -> None:
    """Mirrors test_specialist_install_commit_rejects_a_changed_root_digest —
    the same fresh re-validation gate the brief mandates for the upgrade
    tool: a caller-supplied root_digest that no longer matches the reloaded
    staged bytes must refuse BEFORE upgrade_specialist is ever called."""
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    import specialist_install
    from tools import specialist_upgrade

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("upgrade_specialist must never be called on a root_digest mismatch")

    # The tool's local `from specialist_install import upgrade_specialist`
    # re-reads the module attribute at call time — patch it here.
    monkeypatch.setattr(specialist_install, "upgrade_specialist", _must_not_be_called)
    _inject_fake_receipt(monkeypatch)

    result = await specialist_upgrade.handler({
        "slug": component.slug, "component_id": component.component_id,
        "version": component.version, "staged_dir": str(staged), "receipt_id": "a" * 32,
        "root_digest": "sha256:" + "f" * 64,  # deliberately wrong
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "checksum_changed"


@pytest.mark.asyncio
async def test_specialist_rollback_tool_passes_through_no_prior_tuple(monkeypatch) -> None:
    import specialist_install
    from specialist_install import SpecialistInstallError
    from tools import specialist_rollback

    def _boom(*, slug, **kwargs):
        raise SpecialistInstallError("no_prior_tuple", f"{slug!r} has no retained prior tuple")

    # The tool's local `from specialist_install import rollback_specialist`
    # re-reads the module attribute at call time — patch it here.
    monkeypatch.setattr(specialist_install, "rollback_specialist", _boom)

    result = await specialist_rollback.handler({"slug": "mtg"})

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "no_prior_tuple"


@pytest.mark.asyncio
async def test_specialist_uninstall_tool_calls_uninstall_specialist_and_reports_ok(monkeypatch) -> None:
    import specialist_install
    from tools import specialist_uninstall

    from types import SimpleNamespace

    calls: list[dict] = []

    def _fake_uninstall(*, slug, **kwargs):
        calls.append({"slug": slug})
        return SimpleNamespace(slug=slug, removed_artifact_ids=(), new_artifact_ids=(),
                               journal_path="/tmp/does-not-matter.json")

    # The tool's local `from specialist_install import uninstall_specialist`
    # re-reads the module attribute at call time — patch it here.
    monkeypatch.setattr(specialist_install, "uninstall_specialist", _fake_uninstall)
    _stub_bundle_sequencer(monkeypatch)

    result = await specialist_uninstall.handler({"slug": "mtg"})

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["slug"] == "mtg"
    assert calls == [{"slug": "mtg"}]


# ---------------------------------------------------------------------------
# Round-5b (Sol P1): specialist_install_inspect must verify the consent
# keyboard can actually post — or skip it entirely when the ack ledger
# already holds this exact install identity — instead of returning ok:true
# into a flow that strands forever at commit's consent_missing.
# ---------------------------------------------------------------------------

import asyncio
from types import SimpleNamespace


def _fake_inspection(tmp_path, *, plugin_resolutions=(), receipt_digest=""):
    return SimpleNamespace(
        component_id="casa.spec.mtg", version="1.0.0", slug="mtg",
        component_checksum="sha256:" + "a" * 64,
        root_digest="sha256:" + "b" * 64,
        mission="Answer MTG rules questions.",
        default_persona_ref="mtg-judge@1.0.0",
        default_persona_checksum="sha256:" + "c" * 64,
        required_config_names=(), required_secret_names=(),
        dependencies=(), staged_dir=tmp_path / "staged",
        # Fix round 1 (task-12): a real inspect_specialist_repo call ALWAYS
        # populates these (specialist_install.py InspectionResult) — every
        # fake here mirrors that so the ok_payload's receipt_id/
        # receipt_digest/plugins wiring is exercised the same as production.
        # `receipt_digest` defaults to "" (legacy-shaped fake) but callers
        # exercising the fix-round-1 (task-13) pre-auth identity fix pass a
        # real digest — specialist_receipt.compute_receipt_digest's own
        # docstring: a REAL inspect_specialist_repo call ALWAYS issues a
        # non-empty receipt_digest, plugins or not.
        receipt_id="d" * 32, receipt_digest=receipt_digest,
        plugin_resolutions=plugin_resolutions,
    )


def _wire_inspect(monkeypatch, tmp_path, *, channel=None, plugin_resolutions=(),
                   receipt_digest=""):
    """Patch the network/disk seams: inspect returns a fake staged result,
    the ack store lives under tmp_path (never /data), and _channel_manager
    serves ``channel`` (None = no telegram channel configured)."""
    import specialist_install
    import specialist_install_consent
    from specialist_install_consent import SpecialistInstallAckStore
    import tools as tools_mod

    fake = _fake_inspection(
        tmp_path, plugin_resolutions=plugin_resolutions, receipt_digest=receipt_digest)
    monkeypatch.setattr(
        specialist_install, "inspect_specialist_repo", lambda *a, **k: fake)
    tmp_acks = tmp_path / "acks.json"

    class _TmpAckStore(SpecialistInstallAckStore):
        def __init__(self, path=None):  # noqa: ARG002 — tool calls with no args
            super().__init__(path=tmp_acks)

    monkeypatch.setattr(
        specialist_install_consent, "SpecialistInstallAckStore", _TmpAckStore)
    monkeypatch.setattr(
        tools_mod, "_channel_manager", SimpleNamespace(get=lambda name: channel))
    return fake, _TmpAckStore


class _Handle:
    """Stub ChallengeHandle: refused / settled-post outcome / never-settles."""

    def __init__(self, refused=None, settled="posted", hang=False):
        self.refused = refused
        self._settled = settled
        self._hang = hang

    async def settled_post(self):
        if self._hang:
            await asyncio.Event().wait()  # cancelled by the tool's wait_for bound
        return self._settled


@pytest.mark.asyncio
async def test_inspect_without_channel_returns_consent_channel_unavailable(
    monkeypatch, tmp_path,
) -> None:
    from tools import specialist_install_inspect
    _wire_inspect(monkeypatch, tmp_path, channel=None)

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is False
    assert payload["kind"] == "consent_channel_unavailable"
    assert "no telegram channel" in payload["detail"]


@pytest.mark.asyncio
async def test_inspect_with_preacked_ledger_skips_keyboard(
    monkeypatch, tmp_path,
) -> None:
    """Pre-authorized path: valid ledger consent for this EXACT identity
    (the same install_consent_identity binding commit validates) -> ok:true
    with NO keyboard attempt — works with no Telegram at all."""
    import specialist_install_consent
    from specialist_install_consent import install_consent_identity
    from tools import specialist_install_inspect

    fake, tmp_store_cls = _wire_inspect(monkeypatch, tmp_path, channel=None)
    identity = install_consent_identity(
        component_id=fake.component_id, version=fake.version,
        root_digest=fake.root_digest, slug=fake.slug)
    tmp_store_cls().record(
        identity=identity, component_id=fake.component_id, version=fake.version,
        component_checksum=fake.root_digest, slug=fake.slug)

    def _must_not_post(**kwargs):
        raise AssertionError("keyboard must not be attempted on a pre-acked install")

    monkeypatch.setattr(
        specialist_install_consent, "prompt_specialist_install_consent", _must_not_post)

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is True
    assert payload["consent"] == "pre_authorized"
    assert payload["root_digest"] == fake.root_digest


@pytest.mark.asyncio
async def test_inspect_preacked_with_receipt_digest_is_pre_authorized(
    monkeypatch, tmp_path,
) -> None:
    """Fix round 1 (task-13 e2e finding): a REAL inspect_specialist_repo call
    ALWAYS issues a non-empty receipt_digest (specialist_receipt.compute_
    receipt_digest — plugins or not), and the consent-record path
    (prompt_specialist_install_consent's _on_commit_sync) and the commit/
    upgrade consent gates (specialist_install.py) all bind receipt_digest
    into the identity. Before this fix, specialist_install_inspect's own
    pre-auth check computed the identity WITHOUT receipt_digest, so an ack
    recorded (the normal way, with the digest) was never recognized here —
    the keyboard re-posted, and in a Telegram-less container this fell all
    the way to consent_channel_unavailable. Record the ack exactly the way
    _on_commit_sync does (WITH receipt_digest) and assert inspect now
    recognizes it as pre-authorized with no keyboard attempt."""
    import specialist_install_consent
    from specialist_install_consent import install_consent_identity
    from tools import specialist_install_inspect

    receipt_digest = "sha256:" + "f" * 64
    fake, tmp_store_cls = _wire_inspect(
        monkeypatch, tmp_path, channel=None, receipt_digest=receipt_digest)
    identity = install_consent_identity(
        component_id=fake.component_id, version=fake.version,
        root_digest=fake.root_digest, slug=fake.slug, receipt_digest=receipt_digest)
    tmp_store_cls().record(
        identity=identity, component_id=fake.component_id, version=fake.version,
        component_checksum=fake.root_digest, slug=fake.slug, receipt_digest=receipt_digest)

    def _must_not_post(**kwargs):
        raise AssertionError("keyboard must not be attempted on a pre-acked install")

    monkeypatch.setattr(
        specialist_install_consent, "prompt_specialist_install_consent", _must_not_post)

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is True
    assert payload["consent"] == "pre_authorized"
    assert payload["receipt_digest"] == receipt_digest


@pytest.mark.asyncio
async def test_inspect_legacy_ack_without_receipt_digest_fails_closed(
    monkeypatch, tmp_path,
) -> None:
    """Converse of the fix above: an ack recorded the OLD/legacy way — its
    identity computed WITHOUT receipt_digest, e.g. by the pre-fix buggy
    inspect code, or a genuinely pre-Task-7 ack — must NOT satisfy a fresh
    inspection that carries a real (non-empty) receipt_digest. Fail-closed:
    the ledger lookup misses, so the tool falls through past the
    pre-authorized branch to the keyboard-post attempt; with no telegram
    channel wired that surfaces as consent_channel_unavailable rather than
    silently treating a digest-less legacy ack as covering the bundled
    closure."""
    from specialist_install_consent import install_consent_identity
    from tools import specialist_install_inspect

    receipt_digest = "sha256:" + "f" * 64
    fake, tmp_store_cls = _wire_inspect(
        monkeypatch, tmp_path, channel=None, receipt_digest=receipt_digest)
    legacy_identity = install_consent_identity(
        component_id=fake.component_id, version=fake.version,
        root_digest=fake.root_digest, slug=fake.slug)  # no receipt_digest — legacy shape
    tmp_store_cls().record(
        identity=legacy_identity, component_id=fake.component_id, version=fake.version,
        component_checksum=fake.root_digest, slug=fake.slug)  # no receipt_digest recorded either

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is False
    assert payload["kind"] == "consent_channel_unavailable"
    assert "consent" not in payload


@pytest.mark.asyncio
async def test_preacked_receipt_digest_identity_also_satisfies_real_commit(
    tmp_path,
) -> None:
    """Round-trips the fix: the SAME ack (identity computed WITH
    receipt_digest, exactly as inspect now records/checks it) also satisfies
    the real commit_specialist_install consent gate — proving the
    follow-on commit succeeds once inspect's pre-auth check stops rejecting
    a receipt-digest-bound ack. Uses a hand-built InspectionResult (receipt=
    None keeps commit_specialist_install on its legacy, non-bundle-journal
    path) since only the consent-identity gate is under test here."""
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import (
        InspectionResult, commit_specialist_install, compute_install_root_digest,
        resolve_dependency_closure,
    )
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
    receipt_digest = "sha256:" + "f" * 64

    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest,
        mission=str(component.role.role["mission"]),
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps,
        staged_dir=staged, receipt_digest=receipt_digest,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug,
        receipt_digest=inspection.receipt_digest)
    acks.record(identity=identity, component_id=inspection.component_id,
                version=inspection.version, component_checksum=inspection.root_digest,
                slug=inspection.slug, receipt_digest=inspection.receipt_digest)

    instance = commit_specialist_install(
        inspection=inspection, receipt=None, config={}, secret_names_provided=frozenset(),
        acks=acks, specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents-specialists",
    )
    assert instance.state == "active"


@pytest.mark.asyncio
async def test_inspect_ok_payload_surfaces_receipt_and_plugins(monkeypatch, tmp_path) -> None:
    """Fix round 1 (task-12): specialist_install_commit/specialist_upgrade
    REQUIRE args["receipt_id"] (their MCP schemas mark it "required") — an
    ok_payload missing it would dead-end every real install/upgrade at
    receipt_required. Also asserts the "plugins" tool-payload mirror of what
    the consent DM already enumerates (render_install_consent_message)."""
    from specialist_receipt import PluginReceiptRow
    from specialist_install_consent import install_consent_identity
    from tools import specialist_install_inspect

    row = PluginReceiptRow(
        identifier="mtg-corpus", scoped_name="mtg__mtg-corpus", manifest_name="mtg-corpus",
        version="1.0.0", source_type="plugin", repo="owner/mtg-corpus", ref="main",
        revision="d" * 40, subdir="", content_digest="sha256:" + "e" * 64,
        staged_path=str(tmp_path / "staged" / ".dep-plugins" / "mtg-corpus"),
        mcp_servers=("corpus: python server.py",), protected_tools=("corpus_search",),
        env_names=("MTG_CORPUS_TOKEN",),
    )
    fake, tmp_store_cls = _wire_inspect(
        monkeypatch, tmp_path, channel=None, plugin_resolutions=(row,))
    identity = install_consent_identity(
        component_id=fake.component_id, version=fake.version,
        root_digest=fake.root_digest, slug=fake.slug)
    tmp_store_cls().record(
        identity=identity, component_id=fake.component_id, version=fake.version,
        component_checksum=fake.root_digest, slug=fake.slug)

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))

    assert payload["ok"] is True
    assert payload["receipt_id"] == fake.receipt_id
    assert payload["receipt_id"]  # non-empty
    assert payload["receipt_digest"] == fake.receipt_digest
    assert payload["plugins"] == [{
        "scoped_name": "mtg__mtg-corpus", "manifest_name": "mtg-corpus", "version": "1.0.0",
        "mcp_servers": ["corpus: python server.py"], "protected_tools": ["corpus_search"],
        "env_names": ["MTG_CORPUS_TOKEN"],
    }]


@pytest.mark.asyncio
async def test_inspect_receipt_id_round_trips_into_commit(monkeypatch, tmp_path) -> None:
    """Round trip (fix round 1, task-12): the receipt_id
    specialist_install_inspect's ok_payload carries, fed back verbatim as
    args["receipt_id"] into specialist_install_commit, is the id
    specialist_receipt.load is actually called with — proving the one-flow
    install the configurator recipe drives (inspect -> commit) is wired end
    to end rather than dead-ending at receipt_required."""
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import compute_install_root_digest, resolve_dependency_closure
    import specialist_install
    import specialist_install_consent
    import specialist_receipt
    import tools as tools_mod
    from specialist_install_consent import (
        SpecialistInstallAckStore, install_consent_identity,
    )
    from tools import specialist_install_commit, specialist_install_inspect

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())

    real_receipt_id = "9" * 32
    fake_inspection = SimpleNamespace(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest,
        mission="Answer test questions.", default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps,
        staged_dir=staged, receipt_id=real_receipt_id, receipt_digest="",
        plugin_resolutions=(),
    )
    monkeypatch.setattr(
        specialist_install, "inspect_specialist_repo", lambda *a, **k: fake_inspection)
    tmp_acks = tmp_path / "acks.json"

    class _TmpAckStore(SpecialistInstallAckStore):
        def __init__(self, path=None):  # noqa: ARG002 — tool calls with no args
            super().__init__(path=tmp_acks)

    monkeypatch.setattr(
        specialist_install_consent, "SpecialistInstallAckStore", _TmpAckStore)
    monkeypatch.setattr(
        tools_mod, "_channel_manager", SimpleNamespace(get=lambda name: None))
    identity = install_consent_identity(
        component_id=component.component_id, version=component.version,
        root_digest=root_digest, slug=component.slug)
    _TmpAckStore().record(
        identity=identity, component_id=component.component_id, version=component.version,
        component_checksum=root_digest, slug=component.slug)

    inspect_payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert inspect_payload["ok"] is True
    assert inspect_payload["receipt_id"] == real_receipt_id

    load_calls: list[str] = []

    def _load(rid, *a, **k):
        load_calls.append(rid)
        return SimpleNamespace(receipt_id=rid, receipt_digest="", plugins=())

    monkeypatch.setattr(specialist_receipt, "load", _load)

    instance = SimpleNamespace(slug="mtg", state="active")
    txn = SimpleNamespace(
        slug="mtg", removed_artifact_ids=(), new_artifact_ids=("AID1",),
        journal_path=str(tmp_path / "journal.json"))
    monkeypatch.setattr(
        specialist_install, "commit_specialist_install", lambda *a, **k: (instance, txn))
    _stub_bundle_sequencer(monkeypatch)

    commit_payload = _payload(await specialist_install_commit.handler({
        "component_id": inspect_payload["component_id"], "version": inspect_payload["version"],
        "slug": inspect_payload["slug"], "staged_dir": inspect_payload["staged_dir"],
        "root_digest": inspect_payload["root_digest"],
        "receipt_id": inspect_payload["receipt_id"],
    }))

    # The receipt loaded is the EXACT id the inspect payload carried — the
    # round trip works — and the commit reaches success, never dead-ending
    # at receipt_required. Two loads since #346: the pre-lock load plus the
    # in-lock re-check (a concurrent bundle may prune it while we wait).
    assert load_calls == [real_receipt_id, real_receipt_id]
    assert commit_payload["ok"] is True
    assert commit_payload["slug"] == "mtg"


@pytest.mark.asyncio
@pytest.mark.parametrize("state,expect_pruned", [
    ("pending-configuration", False),
    ("active", True),
])
async def test_commit_prunes_receipt_only_on_active(
        monkeypatch, tmp_path, state, expect_pruned) -> None:
    """#331: a pending-configuration commit RETAINS the source receipt — the
    follow-up configure re-commit requires it (receipt_required otherwise),
    and pre-fix the unconditional prune made a first-commit-pending component
    impossible to activate through the supported flow. An active commit still
    consumes it. Sol r2-1: the STAGING TREE follows the same boundary — the
    tool layer consumes it only after the sequencer succeeded, so a
    compensated failure (or a pending outcome) leaves the retry's bytes
    intact."""
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import compute_install_root_digest, resolve_dependency_closure
    import specialist_install
    import specialist_receipt
    import tools as tools_mod
    from tools import specialist_install_commit

    staging_parent = tmp_path / ".staging"
    staging_parent.mkdir()
    staged = _write_component(staging_parent / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())

    receipt_id = "7" * 32
    monkeypatch.setattr(
        specialist_receipt, "load",
        lambda rid, *a, **k: SimpleNamespace(receipt_id=rid, receipt_digest="", plugins=()))
    pruned: list[str] = []
    monkeypatch.setattr(tools_mod, "_prune_bundle_receipt", pruned.append)

    instance = SimpleNamespace(slug="mtg", state=state)
    txn = SimpleNamespace(
        slug="mtg", removed_artifact_ids=(), new_artifact_ids=(),
        journal_path=str(tmp_path / "journal.json"))
    monkeypatch.setattr(
        specialist_install, "commit_specialist_install", lambda *a, **k: (instance, txn))
    _stub_bundle_sequencer(monkeypatch)

    payload = _payload(await specialist_install_commit.handler({
        "component_id": component.component_id, "version": component.version,
        "slug": component.slug, "staged_dir": str(staged),
        "root_digest": root_digest, "receipt_id": receipt_id,
    }))
    assert payload["ok"] is True
    assert payload["state"] == state
    assert (pruned == [receipt_id]) is expect_pruned
    # Staging consumption mirrors receipt consumption exactly (Sol r2-1).
    assert staged.exists() is (not expect_pruned)


@pytest.mark.asyncio
async def test_inspect_happy_path_posts_keyboard(monkeypatch, tmp_path) -> None:
    import specialist_install_consent
    from tools import specialist_install_inspect

    calls: list[dict] = []

    def _prompt(**kwargs):
        calls.append(kwargs)
        return _Handle(settled="posted")

    _wire_inspect(monkeypatch, tmp_path, channel=SimpleNamespace(chat_id="123"))
    monkeypatch.setattr(
        specialist_install_consent, "prompt_specialist_install_consent", _prompt)

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is True
    assert payload["consent"] == "keyboard_posted"
    assert len(calls) == 1
    assert calls[0]["chat_id"] == 123 and calls[0]["operator_id"] == 123


@pytest.mark.asyncio
@pytest.mark.parametrize("handle,expected_kind", [
    (_Handle(refused="args_too_large"), "consent_prompt_refused"),
    (_Handle(settled="delivery_failed"), "consent_delivery_failed"),
    (_Handle(settled="inactive"), "consent_prompt_inactive"),
    (_Handle(hang=True), "consent_post_unsettled"),
])
async def test_inspect_post_failures_are_structured(
    monkeypatch, tmp_path, handle, expected_kind,
) -> None:
    import specialist_install_consent
    import tools as tools_mod
    from tools import specialist_install_inspect

    _wire_inspect(monkeypatch, tmp_path, channel=SimpleNamespace(chat_id="123"))
    monkeypatch.setattr(
        specialist_install_consent, "prompt_specialist_install_consent",
        lambda **kwargs: handle)
    # Bounded: shrink the settle bound instead of waiting 30s (and never
    # patch <module>.asyncio.sleep — memory-cage rule).
    monkeypatch.setattr(tools_mod, "_INSTALL_CONSENT_POST_TIMEOUT_S", 0.05)

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is False
    assert payload["kind"] == expected_kind


@pytest.mark.asyncio
async def test_inspect_prompt_exception_is_structured(monkeypatch, tmp_path) -> None:
    import specialist_install_consent
    from tools import specialist_install_inspect

    def _boom(**kwargs):
        raise RuntimeError("registration blew up")

    _wire_inspect(monkeypatch, tmp_path, channel=SimpleNamespace(chat_id="123"))
    monkeypatch.setattr(
        specialist_install_consent, "prompt_specialist_install_consent", _boom)

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is False
    assert payload["kind"] == "consent_prompt_failed"
    assert "registration blew up" in payload["detail"]


# ---------------------------------------------------------------------------
# v0.102.0 (#217): the inspect tool captures the requesting configurator
# engagement into reconcile_cb; on Approve+ack that callback delivers a
# synthetic RESUME turn through the channel's resume-if-needed delivery seam
# (deliver_system_turn) so the install proceeds without a manual operator
# nudge. reconcile_cb runs from the tap-callback finish hook — it must NEVER
# raise into it. These tests drive reconcile_cb directly (captured off the
# prompt kwargs), so they never depend on Telegram delivery mechanics.
# ---------------------------------------------------------------------------


def _capture_reconcile(monkeypatch):
    """Patch prompt_specialist_install_consent to a posting stub that records
    the reconcile_cb the inspect tool built, and return the capture dict."""
    import specialist_install_consent

    cap: dict = {}

    def _prompt(**kwargs):
        cap["reconcile_cb"] = kwargs["reconcile_cb"]
        return _Handle(settled="posted")

    monkeypatch.setattr(
        specialist_install_consent, "prompt_specialist_install_consent", _prompt)
    return cap


def _resume_channel(*, registry, deliver):
    return SimpleNamespace(
        chat_id="123", _engagement_registry=registry, deliver_system_turn=deliver)


@pytest.mark.asyncio
async def test_reconcile_cb_resumes_the_captured_engagement(monkeypatch, tmp_path) -> None:
    from tools import specialist_install_inspect, engagement_var

    delivered: list = []
    rec = SimpleNamespace(id="eng-abc", driver="in_casa", status="active")
    registry = SimpleNamespace(get=lambda eid: rec if eid == "eng-abc" else None)

    async def _deliver(r, text):
        delivered.append((r, text))

    _wire_inspect(monkeypatch, tmp_path,
                  channel=_resume_channel(registry=registry, deliver=_deliver))
    cap = _capture_reconcile(monkeypatch)

    token = engagement_var.set(SimpleNamespace(id="eng-abc"))
    try:
        payload = _payload(await specialist_install_inspect.handler(
            {"repo": "owner/repo", "ref": "main"}))
    finally:
        engagement_var.reset(token)
    assert payload["consent"] == "keyboard_posted"

    # The tap-callback finish hook fires reconcile_cb after Approve+ack.
    # #662: the callback REPORTS its outcome — a bare None here is what let a
    # late tap show "installing" with nothing installed.
    assert await cap["reconcile_cb"]() is True
    assert len(delivered) == 1
    assert delivered[0][0] is rec  # resolved for the captured engagement id
    assert "specialist_install_commit" in delivered[0][1]
    assert "specialist:mtg" in delivered[0][1]  # _fake_inspection slug


@pytest.mark.asyncio
async def test_reconcile_cb_swallows_a_delivery_failure(monkeypatch, tmp_path) -> None:
    from tools import specialist_install_inspect, engagement_var

    rec = SimpleNamespace(id="eng-abc", driver="in_casa", status="active")
    registry = SimpleNamespace(get=lambda eid: rec)

    async def _deliver(r, text):
        raise RuntimeError("delivery blew up")

    _wire_inspect(monkeypatch, tmp_path,
                  channel=_resume_channel(registry=registry, deliver=_deliver))
    cap = _capture_reconcile(monkeypatch)

    token = engagement_var.set(SimpleNamespace(id="eng-abc"))
    try:
        await specialist_install_inspect.handler({"repo": "owner/repo", "ref": "main"})
    finally:
        engagement_var.reset(token)

    # Fail-safe: reconcile_cb never propagates into the tap-callback path —
    # and #662: it reports the swallowed failure instead of a bare None.
    assert await cap["reconcile_cb"]() is False  # must not raise


@pytest.mark.asyncio
async def test_reconcile_cb_is_a_noop_when_the_engagement_is_gone(monkeypatch, tmp_path) -> None:
    from tools import specialist_install_inspect, engagement_var

    delivered: list = []
    registry = SimpleNamespace(get=lambda eid: None)  # engagement TTL-expired / gone

    async def _deliver(r, text):
        delivered.append((r, text))

    _wire_inspect(monkeypatch, tmp_path,
                  channel=_resume_channel(registry=registry, deliver=_deliver))
    cap = _capture_reconcile(monkeypatch)

    token = engagement_var.set(SimpleNamespace(id="eng-abc"))
    try:
        await specialist_install_inspect.handler({"repo": "owner/repo", "ref": "main"})
    finally:
        engagement_var.reset(token)

    assert await cap["reconcile_cb"]() is False
    assert delivered == []


@pytest.mark.asyncio
async def test_reconcile_cb_is_a_noop_when_no_engagement_was_captured(
    monkeypatch, tmp_path,
) -> None:
    from tools import specialist_install_inspect

    delivered: list = []
    rec = SimpleNamespace(id="eng-abc", driver="in_casa", status="active")
    registry = SimpleNamespace(get=lambda eid: rec)

    async def _deliver(r, text):
        delivered.append((r, text))

    _wire_inspect(monkeypatch, tmp_path,
                  channel=_resume_channel(registry=registry, deliver=_deliver))
    cap = _capture_reconcile(monkeypatch)

    # engagement_var is left at its default (None) — no configurator context.
    await specialist_install_inspect.handler({"repo": "owner/repo", "ref": "main"})
    assert await cap["reconcile_cb"]() is False
    assert delivered == []


@pytest.mark.asyncio
async def test_second_commit_on_an_active_slug_is_a_clean_typed_error(
    monkeypatch, tmp_path,
) -> None:
    """#5 idempotency: a resume turn PLUS a stray manual nudge could each fire
    specialist_install_commit. The second commit, on the now-active slug, must
    fail closed with a clean typed kind the LLM handles — never corrupt state
    or raise unstructured. commit_specialist_install's `_refuse_if_active_present`
    raises SpecialistInstallError("concurrent_mutation"); the tool maps it to
    ok:false/kind."""
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import compute_install_root_digest, resolve_dependency_closure
    import specialist_install
    from tools import specialist_install_commit

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())

    # Stand in for commit_specialist_install raising the already-active guard —
    # the SAME SpecialistInstallError("concurrent_mutation") _refuse_if_active_
    # present raises on a second commit of a live slug. Verifies the tool's
    # typed-error mapping, not the CAS machinery (covered in test_specialist_install).
    from specialist_install import SpecialistInstallError

    def _already_active(*args, **kwargs):
        raise SpecialistInstallError(
            "concurrent_mutation", "'mtg': an active install appeared under a "
            "concurrent install while acquiring the lock")

    monkeypatch.setattr(specialist_install, "commit_specialist_install", _already_active)
    _inject_fake_receipt(monkeypatch)

    # Consent must be present so the flow reaches commit_specialist_install.
    import specialist_install_consent
    from specialist_install_consent import (
        SpecialistInstallAckStore, install_consent_identity,
    )
    tmp_acks = tmp_path / "acks.json"

    class _TmpAckStore(SpecialistInstallAckStore):
        def __init__(self, path=None):  # noqa: ARG002
            super().__init__(path=tmp_acks)

    monkeypatch.setattr(
        specialist_install_consent, "SpecialistInstallAckStore", _TmpAckStore)
    identity = install_consent_identity(
        component_id=component.component_id, version=component.version,
        root_digest=root_digest, slug=component.slug)
    _TmpAckStore().record(
        identity=identity, component_id=component.component_id, version=component.version,
        component_checksum=root_digest, slug=component.slug)

    payload = _payload(await specialist_install_commit.handler({
        "component_id": component.component_id, "version": component.version,
        "slug": component.slug, "staged_dir": str(staged), "root_digest": root_digest,
        "receipt_id": "a" * 32,
    }))
    assert payload["ok"] is False
    assert payload["kind"] == "concurrent_mutation"


@pytest.mark.asyncio
async def test_specialist_upgrade_rechecks_the_receipt_under_the_lock(
    monkeypatch, tmp_path,
) -> None:
    """#346: the receipt is loaded BEFORE _PLUGIN_TOOLS_LOCK is acquired. A
    concurrent same-receipt bundle completing while this call waits on the
    lock PRUNES the receipt; proceeding anyway made the second upgrade treat
    the tuple commit as a no-op yet still rotate the owned-plugins sidecar —
    desyncing active.prior (V1) from owned-plugins.prior (V2), so a later
    rollback activates one generation's tuple with another's plugins. The
    tool must re-check the receipt under the lock and refuse."""
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import compute_install_root_digest, resolve_dependency_closure
    import specialist_install
    import specialist_receipt
    from tools import specialist_upgrade

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())

    from types import SimpleNamespace
    fake = SimpleNamespace(receipt_id="a" * 32, receipt_digest="", slug="mtg", plugins=())
    calls = {"n": 0}

    def _load(rid, *a, **k):
        calls["n"] += 1
        # First (pre-lock) load sees the receipt; by the in-lock re-check a
        # concurrent bundle has pruned it.
        return fake if calls["n"] == 1 else None

    monkeypatch.setattr(specialist_receipt, "load", _load)

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError(
            "upgrade_specialist must not run once the receipt was pruned by a "
            "concurrent bundle")

    monkeypatch.setattr(specialist_install, "upgrade_specialist", _must_not_be_called)

    result = await specialist_upgrade.handler({
        "slug": "mtg", "component_id": component.component_id,
        "version": component.version, "root_digest": root_digest,
        "staged_dir": str(staged), "receipt_id": "a" * 32,
    })
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "receipt_required"
    assert calls["n"] >= 2, "the receipt must be re-loaded under the lock"


# ---------------------------------------------------------------------------
# #488: setup_env_unprovisioned must not block the bundle gate.
# #491: a compensated failure must SAY it rolled back, and log the verdict.
# ---------------------------------------------------------------------------


def test_binding_blocked_exempts_setup_env_unprovisioned() -> None:
    """#488: `setup_env_unprovisioned` is the documented "loads, reports
    unprovisioned, setup tool runs next" state (#429) — on a fresh install of
    a `casa.setupProvides` plugin it is the NORMAL state, and it must not be
    read as an integrity failure at either the top level or the row level."""
    import tools as tools_mod

    v = {"reasons": ["setup_env_unprovisioned"],
         "targets": [{"target": "specialist:mtg", "ready": False,
                      "reasons": ["setup_env_unprovisioned"]}]}
    assert tools_mod._bundle_binding_blocked(v) is False

    # A genuine integrity code alongside it still blocks — at both levels.
    assert tools_mod._bundle_binding_blocked(
        {"reasons": ["setup_env_unprovisioned", "artifact_invalid"],
         "targets": []}) is True
    assert tools_mod._bundle_binding_blocked(
        {"reasons": [],
         "targets": [{"target": "specialist:mtg", "ready": False,
                      "reasons": ["setup_env_unprovisioned",
                                  "reload_required"]}]}) is True


@pytest.mark.asyncio
async def test_sequencer_passes_setup_provides_unprovisioned_real_verify(
    monkeypatch, tmp_path,
) -> None:
    """#488: fresh install of a bundled plugin declaring `casa.setupProvides`
    — the REAL verify reports top-level `setup_env_unprovisioned` (only the
    plugin's own setup tool can provision it, and it cannot run until the
    plugin is installed), and the REAL bundle sequencer must NOT compensate.
    Mirrors test_sequencer_passes_env_pending_owned_plugin_real_verify: only
    the reload/agent/health I/O seams are stubbed."""
    import agent as agent_mod
    import plugin_registry
    import tools as tools_mod
    import system_requirements.manifest as mani
    import plugin_env_conf as pec
    from plugin_fixtures import entry, mk_artifact
    from plugin_store import content_checksum, write_metadata

    store = tmp_path / "store"
    reg_path = tmp_path / "registry.json"
    e = entry("mtg.mtg", ["specialist:mtg"])
    e["owner"] = "specialist:mtg"
    e["manifest_name"] = "mtg"
    root = mk_artifact(
        store, "mtg.mtg", e["artifact_id"], manifest_name="mtg",
        mcp_servers={"s": {"env": {"K": "${CASA_PLUGIN_MTG_KEY}"}}},
        extra_manifest={"casa": {"setupTool": "setup_mtg",
                                 "setupProvides": ["CASA_PLUGIN_MTG_KEY"]}})
    write_metadata(root, name="mtg.mtg", repo="o/r", ref="v1",
                   revision="git:" + "a" * 40, subdir="", artifact_id=e["artifact_id"],
                   version="1.0.0", checksum=content_checksum(root), manifest_name="mtg")
    reg_path.write_text(
        json.dumps({"schema_version": 1, "plugins": [e]}), encoding="utf-8")

    real_load = plugin_registry.load_registry
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda path=reg_path: real_load(path))
    monkeypatch.setattr(plugin_registry, "STORE_ROOT", store)
    monkeypatch.setattr(mani, "MANIFEST_PATH", tmp_path / "sysreq.yaml")
    monkeypatch.setattr(pec, "PLUGIN_ENV_CONF_PATH", tmp_path / "plugin-env.conf")
    monkeypatch.delenv("CASA_PLUGIN_MTG_KEY", raising=False)

    monkeypatch.setattr(agent_mod, "active_runtime", None, raising=False)
    monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", lambda issues: None)

    async def _notify():
        return None

    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible", _notify)
    monkeypatch.setattr(tools_mod, "_invalidate_lifecycle", lambda **k: None)

    seq = await tools_mod._bundle_reload_and_verify(
        "mtg", removed_artifact_ids=[], targets_removed=[])

    v = seq["verify"]["mtg.mtg"]
    assert v["ready"] is False                             # still not ready…
    assert "setup_env_unprovisioned" in v["reasons"]       # …for exactly this reason
    row = next(s for s in v["secrets"] if s["var"] == "CASA_PLUGIN_MTG_KEY")
    assert row["status"] == "unprovisioned"
    # …yet the sequencer does NOT compensate: the plugin must load
    # unprovisioned for its setup tool to ever run (#429).
    assert seq["not_ready"] == []
    assert seq["ok"] is True


@pytest.mark.asyncio
async def test_seq_failure_successful_compensation_tags_rolled_back(
    monkeypatch,
) -> None:
    """#491: a SUCCESSFUL compensation must be stated in the envelope —
    `rolled_back: true` plus a human-readable outcome — so the caller can
    tell "your install was undone" from "your install exists but is not
    ready". (Pre-fix, only a FAILED compensation was marked.)"""
    from types import SimpleNamespace
    import tools as tools_mod
    import specialist_bundle_journal

    async def _seq(slug, *, removed_artifact_ids, targets_removed):
        return {"ok": True, "reloaded": [], "verify": {}}

    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq)
    monkeypatch.setattr(specialist_bundle_journal, "complete", lambda p: None)

    txn = SimpleNamespace(
        slug="mtg", new_artifact_ids=("NEWAID",), journal_path="/tmp/j.json",
        before_tuple_files={"active.yaml": None},
        rollback_disk=lambda: None)
    env = await tools_mod._bundle_seq_failure(
        txn, {"kind": "postcondition_failed", "not_ready": ["mtg.mtg"]},
        slug="mtg")

    assert env["ok"] is False
    assert env["rolled_back"] is True
    assert "rolled back" in env["outcome"]
    assert "compensation_failed" not in env
    assert "runtime_compensation_incomplete" not in env


@pytest.mark.asyncio
async def test_seq_failure_incomplete_runtime_compensation_is_disclosed(
    monkeypatch,
) -> None:
    """#491 (Sol design r1): the disk rollback succeeding while the
    compensating SEQUENCER fails (e.g. the just-created agent could not be
    evicted) must not read as "prior state fully restored" — the envelope
    carries `rolled_back` (the disk state IS rolled back) plus
    `runtime_compensation_incomplete`."""
    from types import SimpleNamespace
    import tools as tools_mod
    import specialist_bundle_journal

    async def _seq_fail(slug, *, removed_artifact_ids, targets_removed):
        return {"ok": False, "kind": "reload_failed",
                "reload_errors": [{"target": "specialist:mtg"}]}

    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq_fail)
    monkeypatch.setattr(specialist_bundle_journal, "complete", lambda p: None)

    txn = SimpleNamespace(
        slug="mtg", new_artifact_ids=("NEWAID",), journal_path="/tmp/j.json",
        before_tuple_files={"active.yaml": None},
        rollback_disk=lambda: None)
    env = await tools_mod._bundle_seq_failure(
        txn, {"kind": "postcondition_failed"}, slug="mtg")

    assert env["rolled_back"] is True
    assert env["runtime_compensation_incomplete"] is True
    assert "compensation_failed" not in env

    # A RAISING compensating sequencer grades the same way.
    async def _seq_raise(slug, *, removed_artifact_ids, targets_removed):
        raise RuntimeError("agent eviction blew up")

    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq_raise)
    env2 = await tools_mod._bundle_seq_failure(
        txn, {"kind": "postcondition_failed"}, slug="mtg")
    assert env2["rolled_back"] is True
    assert env2["runtime_compensation_incomplete"] is True


@pytest.mark.asyncio
async def test_bundle_sequencer_failure_logs_warning(
    monkeypatch, caplog,
) -> None:
    """#491: the blocking verify verdict must be diagnosable from logs —
    pre-fix, a failing sequencer returned ok:false with no log line and the
    verdict existed solely inside the tool result."""
    from types import SimpleNamespace
    import agent as agent_mod
    import plugin_registry
    import reload as reload_mod
    import tools as tools_mod

    async def _dispatch(scope, *, runtime, role=None):
        return {"status": "ok"}

    monkeypatch.setattr(reload_mod, "dispatch", _dispatch)
    monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(plugin_registry, "load_registry", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(plugin_registry, "owned_entries_for", lambda slug, reg: [])
    monkeypatch.setattr(plugin_registry, "resolve_for",
                        lambda t: SimpleNamespace(plugins=[]))
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", lambda issues: None)

    async def _notify():
        return None

    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible", _notify)
    monkeypatch.setattr(tools_mod, "_invalidate_lifecycle", lambda **k: None)

    # Uninstall sweep with the agent STILL registered -> absent violation.
    monkeypatch.setattr(agent_mod, "active_runtime",
                        SimpleNamespace(agents={"mtg": object()}, agents_dir=None),
                        raising=False)
    with caplog.at_level("WARNING"):
        seq = await tools_mod._bundle_reload_and_verify(
            "mtg", removed_artifact_ids=[], targets_removed=["specialist:mtg"])
    assert seq["ok"] is False
    rec = next(r for r in caplog.records
               if r.levelname == "WARNING" and "mtg" in r.getMessage()
               and "agent:mtg" in r.getMessage())
    assert "postcondition_failed" in rec.getMessage()


@pytest.mark.asyncio
async def test_commit_success_surfaces_bundled_plugins_required_env_vars(
    monkeypatch, tmp_path,
) -> None:
    """#499: the install flow left bundled plugins' declared env vars unwired,
    guaranteeing a requires-gate refusal on the specialist's first tool call
    and a second configurator engagement. The commit result now mirrors
    plugin_add's `required_env_vars` (per bundled plugin, from the consented
    receipt rows) so the install recipe wires them in the SAME engagement.
    Plugins declaring no env vars are omitted."""
    from types import SimpleNamespace
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import compute_install_root_digest, resolve_dependency_closure
    import specialist_install
    import specialist_bundle_journal
    import tools as tools_mod
    from tools import specialist_install_commit

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
    _inject_fake_receipt(monkeypatch, plugins=(
        SimpleNamespace(scoped_name="mtg.bankfeed", manifest_name="bankfeed",
                        env_names=("BANKFEED_OP_VAULT",)),
        SimpleNamespace(scoped_name="mtg.mtg", manifest_name="mtg",
                        env_names=()),
    ))
    _stub_bundle_sequencer(monkeypatch)

    txn = SimpleNamespace(slug="mtg", removed_artifact_ids=(),
                          new_artifact_ids=("NEWAID",),
                          journal_path="/tmp/j.json",
                          rollback_disk=lambda: None)
    monkeypatch.setattr(
        specialist_install, "commit_specialist_install",
        lambda *a, **k: (SimpleNamespace(slug="mtg", state="active"), txn))
    monkeypatch.setattr(tools_mod, "_prune_bundle_receipt", lambda rid: None)
    monkeypatch.setattr(specialist_install, "reclaim_staging_tree",
                        lambda p: None)

    result = await specialist_install_commit.handler({
        "component_id": component.component_id, "version": component.version,
        "slug": "mtg", "staged_dir": str(staged), "root_digest": root_digest,
        "receipt_id": "a" * 32,
    })
    payload = _payload(result)
    assert payload["ok"] is True
    # Keyed by the SCOPED registry name (review r1): the identity
    # set_plugin_env_reference / verify_plugin_state take for an owned
    # plugin — the bare manifest_name is not_registered there. Env-less
    # plugins omitted.
    assert payload["required_env_vars"] == {
        "mtg.bankfeed": ["BANKFEED_OP_VAULT"]}


# ---------------------------------------------------------------------------
# #662 red case (specified by the red-case reviewer at 1c8033bb; frozen once
# accepted). A tap that lands after its requesting engagement is terminal or
# gone must not leave the DM claiming an install. These run the PRODUCTION
# reconcile_cb the inspect tool builds — not a stub — through the REAL consent
# finish hook and the REAL Telegram lifecycle gate, so a fix that merely
# tightens the finish hook while the production callback still exits `None`
# cannot pass.
# ---------------------------------------------------------------------------

_662_CORRECTIVE = (
    "⚠️ Approved and saved — but the install of 'mtg' was not "
    "started automatically. Start a new configurator engagement and re-run "
    "the install; the approval recorded for this exact version is reused "
    "if it still applies."
)


def _capture_real_consent(monkeypatch):
    """Wrap prompt_specialist_install_consent, keeping the REAL prompt (hence
    the real _on_commit_sync/_finish) and the production reconcile_cb, and
    swapping in only a capturing coordinator."""
    import specialist_install_consent as sic

    real = sic.prompt_specialist_install_consent
    cap: dict = {}

    class _Coordinator:
        def register_challenge(self, key, **kwargs):
            cap["on_commit_sync"] = kwargs["on_commit_sync"]
            cap["finish_factory"] = kwargs["finish_factory"]
            return _Handle(settled="posted")

    def _prompt(**kwargs):
        cap["reconcile_cb"] = kwargs["reconcile_cb"]
        kwargs["coordinator"] = _Coordinator()
        return real(**kwargs)

    monkeypatch.setattr(sic, "prompt_specialist_install_consent", _prompt)
    return cap


def _662_channel(registry, edits):
    """A channel carrying the REAL deliver_system_turn/_resume_and_ready, so a
    terminal record is rejected by production code rather than by a fake."""
    from types import MethodType

    from channels.telegram import TelegramChannel

    chan = SimpleNamespace(
        chat_id="701", _engagement_registry=registry,
        _engagement_handler_locks={}, _driver_admit_inbound=None,
        _driver_discharge_inbound=None, _engagement_driver=None,
        _engagement_context_rebuilder=None, _stopping=False,
        _turn_tasks=set(), _rebuild_preambles={},
    )

    async def _edit_dm_message(chat_id, message_id, text):
        edits.append((chat_id, message_id, text))

    chan.edit_dm_message = _edit_dm_message
    chan._resume_and_ready = MethodType(TelegramChannel._resume_and_ready, chan)
    chan.deliver_system_turn = MethodType(TelegramChannel.deliver_system_turn, chan)
    return chan


def _662_registry(edits, ack_path, observations):
    """Registry whose every lookup records (DM edits so far, acks so far) —
    the ordering proof: the ack must precede reconciliation, and the sole DM
    edit must follow it."""
    from specialist_install_consent import SpecialistInstallAckStore

    rec = SimpleNamespace(id="eng-abc", topic_id=9, driver="in_casa",
                          status="active", sdk_session_id=None)
    state = {"present": True}

    def _get(eid):
        acked = len(SpecialistInstallAckStore(path=ack_path)._load()) \
            if ack_path.exists() else 0
        observations.append((len(edits), acked))
        if not state["present"] or eid != "eng-abc":
            return None
        return rec

    return SimpleNamespace(get=_get), rec, state


@pytest.mark.asyncio
@pytest.mark.parametrize("tap_state", ["gone", "terminal"])
async def test_662_terminal_or_gone_engagement_uses_the_production_outcome(
    monkeypatch, tmp_path, tap_state,
) -> None:
    from tools import specialist_install_inspect, engagement_var

    edits: list = []
    observations: list = []
    ack_path = tmp_path / "acks.json"
    registry, rec, state = _662_registry(edits, ack_path, observations)
    _wire_inspect(monkeypatch, tmp_path,
                  channel=_662_channel(registry, edits))
    cap = _capture_real_consent(monkeypatch)

    token = engagement_var.set(SimpleNamespace(id="eng-abc"))
    try:
        payload = _payload(await specialist_install_inspect.handler(
            {"repo": "owner/repo", "ref": "main"}))
    finally:
        engagement_var.reset(token)
    assert payload["consent"] == "keyboard_posted"

    # The tap lands AFTER the requesting engagement is gone / terminal.
    if tap_state == "gone":
        state["present"] = False
    else:
        rec.status = "completed"

    req = SimpleNamespace(meta={})
    cap["on_commit_sync"](0, req.meta)
    finish = cap["finish_factory"](88, req)
    await finish({"outcome": "answered", "option_index": 0})

    from specialist_install_consent import SpecialistInstallAckStore
    ack_count = len(SpecialistInstallAckStore(path=ack_path)._load())
    actual = (ack_count, observations[0], len(edits), edits)
    assert actual == (1, (0, 1), 1, [(701, 88, _662_CORRECTIVE)]), (
        f"approval facts: {actual!r}")


@pytest.mark.asyncio
@pytest.mark.parametrize("tap_state", ["gone", "terminal"])
async def test_662_production_reconcile_cb_reports_a_literal_false(
    monkeypatch, tmp_path, tap_state,
) -> None:
    """The half a finish-hook truthiness patch cannot fake: the PRODUCTION
    callback must return the literal `False`, not a bare `None`."""
    from tools import specialist_install_inspect, engagement_var

    edits: list = []
    observations: list = []
    ack_path = tmp_path / "acks.json"
    registry, rec, state = _662_registry(edits, ack_path, observations)
    _wire_inspect(monkeypatch, tmp_path,
                  channel=_662_channel(registry, edits))
    cap = _capture_real_consent(monkeypatch)

    token = engagement_var.set(SimpleNamespace(id="eng-abc"))
    try:
        await specialist_install_inspect.handler(
            {"repo": "owner/repo", "ref": "main"})
    finally:
        engagement_var.reset(token)

    if tap_state == "gone":
        state["present"] = False
    else:
        rec.status = "completed"

    result = await cap["reconcile_cb"]()
    actual = (len(observations), [result])
    assert actual == (1 if tap_state == "gone" else 2, [False]), (
        f"reconciliation facts: {actual!r}")
