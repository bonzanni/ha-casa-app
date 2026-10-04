"""S1b: a job may require a specialist host; one live job per plugin manifest name.

Every launch-level case runs ``start_job`` on the real launch, engagement
registry, in-casa driver and Telegram channel (``test_start_job.runtime``), with
the real plugin registry and the real ``find_job_host`` / ``startable_jobs``
restored over that fixture's seams. Each one asserts counts — live records,
SDK clients, topics opened, pending start claims — never only a status.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
from types import SimpleNamespace

import pytest

import background_jobs as jobs
import plugin_registry
import tools
from config import DelegateEntry
from engagement_registry import EngagementRegistry
from plugin_fixtures import entry, mk_artifact, mk_registry, owned_entry
from plugin_store import METADATA_FILENAME, StoreError, manifest_jobs
from test_background_jobs_declaration import _job
from test_start_job import Client, call, runtime  # noqa: F401  (fixture)
from test_job_fresh_conversation import home  # noqa: F401  (fixture)

# The real functions, captured before the `runtime` fixture replaces them.
FIND = jobs.find_job_host
LIST = jobs.startable_jobs


_ABSENT = object()


def _jobs(host=_ABSENT, names=("classify",)):
    out = []
    for name in names:
        job = _job(name=name)
        if host is not _ABSENT:
            job["host"] = host
        out.append(job)
    return out


def _artifact(tmp_path, e, decls=None, *, manifest_name=None):
    extra = {"casa": {"jobs": decls}} if decls else None
    root = mk_artifact(tmp_path / "store", e["name"], e["artifact_id"],
                       manifest_name=manifest_name, extra_manifest=extra,
                       revision=e["source"]["revision"],
                       extra_files={"skills/classify/SKILL.md": "fixture"})
    if manifest_name is not None:
        meta = json.loads((root / METADATA_FILENAME).read_text())
        meta["manifest_name"] = manifest_name
        (root / METADATA_FILENAME).write_text(json.dumps(meta))
    return root


def _load(tmp_path, entries):
    plugin_registry.reload_snapshot(registry_path=mk_registry(tmp_path, entries),
                                    store_root=tmp_path / "store")


def _real_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "find_job_host", FIND)
    monkeypatch.setattr(jobs, "startable_jobs", LIST)
    monkeypatch.setattr(tools, "_PLUGIN_JOB_ROOT", tmp_path / "workers")


def _add_backup(runtime):
    """A second specialist delegate, `backup`, after `finance`."""
    cfg = dataclasses.replace(runtime.cfg)
    cfg.role = "backup"
    tools._specialist_registry._configs["backup"] = cfg
    tools._agent_role_map["backup"] = cfg
    runtime.caller.delegates.append(DelegateEntry(agent="backup", purpose="p", when="w"))


async def start(name):
    async def handler(args):
        return await tools.start_job.handler({**args, "job": name})
    return await call(tool=SimpleNamespace(handler=handler))


def _counts(runtime, registry=None):
    reg = registry or tools._engagement_registry
    return (len(reg.active_and_idle()), len(Client.instances),
            runtime.bot.create_forum_topic.await_count,
            len(jobs._pending_plugin_job_starts))


def _hold_first_topic(runtime):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def open_topic(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return SimpleNamespace(message_thread_id=40 + calls)

    runtime.bot.create_forum_topic.side_effect = open_topic
    return entered, release


async def _reload(runtime, monkeypatch, rec, *, resume):
    """A restart: a fresh registry loaded from the tombstone and, with
    *resume*, the old client ended and the record resumed on the real driver."""
    restored = EngagementRegistry(tombstone_path=runtime.registry._tombstone_path, bus=None)
    await restored.load()
    monkeypatch.setattr(tools, "_engagement_registry", restored)
    if resume:
        await runtime.driver.cancel(rec)
        assert len(runtime.driver._clients) == 0
        Client.instances.clear()
        runtime.driver._record_lookup = restored.get
        again = restored.get(rec.id)
        await runtime.driver.resume(again, again.sdk_session_id or "resumed-session")
        assert runtime.driver.is_alive(again)
        assert len(runtime.driver._clients) == len(Client.instances) == 1
    return restored


# ---------------------------------------------------------------------------
# Declaration (red case 5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["resident", 1, None, True, "Specialist", ""])
def test_host_other_than_specialist_is_jobs_invalid(value):
    with pytest.raises(StoreError) as exc:
        manifest_jobs({"name": "ledger", "casa": {"jobs": _jobs(host=value)}})
    assert exc.value.reason_code == "jobs_invalid"
    assert "host" in str(exc.value)


def test_host_specialist_is_accepted_and_absence_is_unchanged():
    assert manifest_jobs({"name": "ledger", "casa": {"jobs": _jobs("specialist")}})[0]["host"] == "specialist"
    assert "host" not in manifest_jobs({"name": "ledger", "casa": {"jobs": _jobs()}})[0]


# ---------------------------------------------------------------------------
# Listing (red cases 1 and 2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["specialist", None])
def test_specialist_job_is_hosted_by_the_delegate_not_the_resident(tmp_path, monkeypatch, host):
    monkeypatch.setattr(plugin_registry, "_snapshot", None)
    e = entry("ledger", ["resident:assistant", "specialist:finance"])
    _artifact(tmp_path, e, _jobs(host) if host else _jobs())
    _load(tmp_path, [e])
    found = FIND("ledger:classify", "assistant", ["finance"])
    listed = LIST("assistant", ["finance"])
    assert [(h.kind, h.role, h.decl.qualified_name) for h in listed] == [
        ("specialist", "finance", "ledger:classify") if host
        else ("resident", "assistant", "ledger:classify")]
    assert (found.kind, found.role) == (("specialist", "finance") if host else ("resident", "assistant"))
    assert found.decl.host == host


async def test_specialist_job_without_a_declaring_delegate_is_not_startable(runtime, tmp_path, monkeypatch):
    _real_discovery(monkeypatch, tmp_path)
    e = entry("ledger", ["resident:assistant"])
    _artifact(tmp_path, e, _jobs("specialist"))
    _load(tmp_path, [e])
    assert LIST("assistant", ["finance"]) == []
    result = await start("ledger:classify")
    assert result["kind"] == "job_not_declared"
    assert result["message"] == "Startable jobs: none"
    assert _counts(runtime) == (0, 0, 0, 0)


# ---------------------------------------------------------------------------
# The per-plugin guard (red case 3, pinned) and the marker (behaviour 2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("second_host", ["specialist", "resident"])
@pytest.mark.parametrize("reload_record", [False, True])
async def test_specialist_job_record_blocks_a_second_start(runtime, tmp_path, monkeypatch,
                                                           second_host, reload_record):
    """Today's guard, pinned: no `host` field, so this passes before S1b too."""
    _real_discovery(monkeypatch, tmp_path)
    e = entry("ledger", ["specialist:finance"])
    _artifact(tmp_path, e, _jobs())
    _load(tmp_path, [e])
    first = await start("ledger:classify")
    assert first["status"] == "pending", first
    assert first["agent"] == "finance"
    await tools.drain_launch_turns()
    rec = runtime.registry.get(first["engagement_id"])
    if reload_record:
        await _reload(runtime, monkeypatch, rec, resume=False)
    if second_host == "resident":
        # The same installed plugin, now assigned to the resident instead.
        moved = entry("ledger", ["resident:assistant"], revision="git:" + "b" * 40)
        _artifact(tmp_path, moved, _jobs())
        _load(tmp_path, [moved])
        assert FIND("ledger:classify", "assistant", ["finance"]).kind == "resident"
    second = await start("ledger:classify")
    assert second.get("kind") == "job_busy", second
    assert second["engagement_id"] == rec.id
    assert _counts(runtime) == (1, 1, 1, 0)


@pytest.mark.parametrize("host", ["specialist", None])
async def test_specialist_job_records_its_selected_plugin_without_a_model(runtime, tmp_path,
                                                                          monkeypatch, host):
    """Behaviour 2: a specialist-hosted job pins the selected plugin's registry
    identity, with no `model`; the record stays a specialist engagement that
    persists the marker and resumes as a specialist, not as a plugin worker."""
    _real_discovery(monkeypatch, tmp_path)
    e = entry("ledger", ["resident:assistant", "specialist:finance"] if host
              else ["specialist:finance"])
    _artifact(tmp_path, e, _jobs(host) if host else _jobs())
    _load(tmp_path, [e])
    first = await start("ledger:classify")
    assert first["status"] == "pending", first
    assert first["agent"] == "finance"
    await tools.drain_launch_turns()
    rec = runtime.registry.get(first["engagement_id"])
    assert (rec.kind, rec.role_or_type) == ("specialist", "finance")
    assert rec.origin["plugin_job"] == {"plugin": "ledger"}
    assert _counts(runtime) == (1, 1, 1, 0)
    restored = await _reload(runtime, monkeypatch, rec, resume=False)
    assert restored.get(rec.id).origin["plugin_job"] == {"plugin": "ledger"}
    opts = tools.build_engagement_resume_options(restored.get(rec.id), "resume-id")
    assert jobs.JOB_CASA_GRANTS[0] in opts.allowed_tools
    assert opts.system_prompt != tools._PLUGIN_JOB_PROMPT


# ---------------------------------------------------------------------------
# Unassigned during the held topic creation (red case 6), and the legacy
# records that race left behind before the marker existed (6b, 6d)
# ---------------------------------------------------------------------------

async def _race_unassign(runtime, tmp_path, monkeypatch):
    """Start ledger:classify on finance, unassign ledger from finance while the
    topic is being created, and return the settled record. finance also loads
    an incidental `helper`, so the record pins only helper."""
    _real_discovery(monkeypatch, tmp_path)
    _add_backup(runtime)
    e = entry("ledger", ["specialist:finance", "specialist:backup"])
    _artifact(tmp_path, e, _jobs("specialist", names=("classify", "scan")))
    helper = entry("helper", ["specialist:finance"])
    _artifact(tmp_path, helper)
    _load(tmp_path, [e, helper])
    entered, release = _hold_first_topic(runtime)
    first_task = asyncio.create_task(start("ledger:classify"))
    await asyncio.wait_for(entered.wait(), 3)
    assert _counts(runtime) == (0, 0, 1, 1)
    load, save = plugin_registry.load_registry, plugin_registry.save_registry
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda path=tmp_path / "registry.json": load(path))
    monkeypatch.setattr(plugin_registry, "save_registry",
                        lambda data: save(data, tmp_path / "registry.json"))
    changed = tools._plugin_unassign_sync(name="ledger", target="specialist:finance")
    assert changed["ok"] and changed["was_assigned"], changed
    tools._invalidate_lifecycle(roles=["specialist:finance"])
    plugin_registry.reload_snapshot(registry_path=tmp_path / "registry.json",
                                    store_root=tmp_path / "store")
    try:
        # Inside the pre-record window: the pending claim refuses.
        assert (await start("ledger:scan"))["kind"] == "job_busy"
    finally:
        release.set()
        first = await first_task  # never leave the held launch behind
    assert first["status"] == "pending", first
    await tools.drain_launch_turns()
    rec = runtime.registry.get(first["engagement_id"])
    assert [a["name"] for a in rec.plugin_artifacts] == ["helper"]
    assert FIND("ledger:classify", "assistant", ["finance", "backup"]).role == "backup"
    return rec


@pytest.mark.parametrize("second_name", ["ledger:classify", "ledger:scan"])
@pytest.mark.parametrize("reload_record", [False, True])
async def test_unassign_during_topic_creation_still_blocks(runtime, tmp_path, monkeypatch,
                                                           second_name, reload_record):
    rec = await _race_unassign(runtime, tmp_path, monkeypatch)
    assert rec.origin["plugin_job"] == {"plugin": "ledger"}
    if reload_record:
        await _reload(runtime, monkeypatch, rec, resume=True)
    second = await start(second_name)
    assert second.get("kind") == "job_busy", second
    assert second["engagement_id"] == rec.id
    assert _counts(runtime) == (1, 1, 1, 0)


async def test_the_marker_names_the_plugin_selected_at_start_not_a_later_resolution(
        runtime, tmp_path, monkeypatch):
    """Behaviour 2: the marker is the SELECTED host's plugin, taken before the
    launch awaits. While topic creation is held, `ledger` is unassigned from
    finance; a later resolution of the same job would now pick backup's own
    installation of that manifest, `backup.ledger`. The record still names
    `ledger`, the plugin that was actually started."""
    _real_discovery(monkeypatch, tmp_path)
    _add_backup(runtime)
    e = entry("ledger", ["specialist:finance"])
    _artifact(tmp_path, e, _jobs("specialist"))
    other = owned_entry(name="backup.ledger", owner="specialist:backup", manifest_name="ledger",
                        repo="o/r", subdir="")
    other["source"]["ref"] = "v1"
    _artifact(tmp_path, other, _jobs("specialist"), manifest_name="ledger")
    _load(tmp_path, [e, other])
    entered, release = _hold_first_topic(runtime)
    first_task = asyncio.create_task(start("ledger:classify"))
    await asyncio.wait_for(entered.wait(), 3)
    try:
        _load(tmp_path, [dict(e, targets=[]), other])
        later = FIND("ledger:classify", "assistant", ["finance", "backup"])
        assert (later.role, later.plugin.name) == ("backup", "backup.ledger")
    finally:
        release.set()
        first = await first_task  # never leave the held launch behind
    assert first["status"] == "pending", first
    await tools.drain_launch_turns()
    rec = runtime.registry.get(first["engagement_id"])
    assert rec.origin["plugin_job"] == {"plugin": "ledger"}
    second = await start("ledger:classify")
    assert second.get("kind") == "job_busy", second
    assert _counts(runtime) == (1, 1, 1, 0)


@pytest.mark.parametrize("second_name", ["ledger:classify", "ledger:scan"])
@pytest.mark.parametrize("resume", [False, True])
async def test_legacy_markerless_record_without_its_declaring_row_blocks(
        runtime, tmp_path, monkeypatch, second_name, resume):
    """6b (same job) and 6d (another job of the same manifest): a record the
    pre-marker writer left after the unassign race names only `helper`."""
    rec = await _race_unassign(runtime, tmp_path, monkeypatch)
    rec.origin.pop("plugin_job")
    await runtime.registry.persist_origin(rec.id)
    await _reload(runtime, monkeypatch, rec, resume=resume)
    assert "plugin_job" not in tools._engagement_registry.get(rec.id).origin
    second = await start(second_name)
    assert second.get("kind") == "job_busy", second
    assert second["engagement_id"] == rec.id
    assert _counts(runtime) == (1, 1, 1, 0)


@pytest.mark.parametrize("second_name", ["ledger:classify", "ledger:scan"])
async def test_legacy_record_without_manifest_name_blocks(runtime, tmp_path, monkeypatch, second_name):
    """6d: a markerless record whose declaring row predates `manifest_name`."""
    _real_discovery(monkeypatch, tmp_path)
    _add_backup(runtime)
    e = entry("ledger", ["specialist:finance", "specialist:backup"])
    _artifact(tmp_path, e, _jobs(names=("classify", "scan")))
    _load(tmp_path, [e])
    first = await start("ledger:classify")
    await tools.drain_launch_turns()
    rec = runtime.registry.get(first["engagement_id"])
    rec.origin.pop("plugin_job")
    for artifact in rec.plugin_artifacts:
        artifact.pop("manifest_name")
    await runtime.registry.persist_origin(rec.id)
    await _reload(runtime, monkeypatch, rec, resume=True)
    # The second start would land on backup: finance no longer declares it.
    e["targets"] = ["specialist:backup"]
    _load(tmp_path, [e])
    second = await start(second_name)
    assert second.get("kind") == "job_busy", second
    assert _counts(runtime) == (1, 1, 1, 0)


# ---------------------------------------------------------------------------
# The pre-record window (red cases 6c and 6d)
# ---------------------------------------------------------------------------

def _two_installations(tmp_path, *, a_targets=None, b_targets=("resident:assistant",)):
    """`finance.ledger` (owned by finance) and `ledger`: two installed plugins
    sharing the manifest name `ledger`, each declaring classify and scan."""
    a = owned_entry(name="finance.ledger", owner="specialist:finance", manifest_name="ledger",
                    repo="o/r", subdir="", targets=a_targets)
    a["source"]["ref"] = "v1"
    _artifact(tmp_path, a, _jobs(names=("classify", "scan")), manifest_name="ledger")
    b = entry("ledger", list(b_targets))
    _artifact(tmp_path, b, _jobs(names=("classify", "scan")))
    return a, b


@pytest.mark.parametrize("installations", ["one", "two"])
@pytest.mark.parametrize("second_name", ["ledger:classify", "ledger:scan"])
async def test_concurrent_starts_of_one_manifest_claim_once(runtime, tmp_path, monkeypatch,
                                                           installations, second_name):
    """6c (same job) and 6d's pending window (another job): inside the first
    start's pre-record window, the second is refused — with one installed
    plugin, and with two installations sharing the manifest name."""
    _real_discovery(monkeypatch, tmp_path)
    if installations == "one":
        e = entry("ledger", ["specialist:finance"])
        _artifact(tmp_path, e, _jobs(names=("classify", "scan")))
        _load(tmp_path, [e])
    else:
        a, b = _two_installations(tmp_path)
        _load(tmp_path, [a])  # only finance's installation, for the first start
    entered, release = _hold_first_topic(runtime)
    first_task = asyncio.create_task(start("ledger:classify"))
    await asyncio.wait_for(entered.wait(), 3)
    if installations == "two":
        _load(tmp_path, [a, b])
        assert FIND(second_name, "assistant", ["finance"]).plugin.name == "ledger"
    try:
        second = await start(second_name)
        assert second.get("kind") == "job_busy", second
        assert "engagement_id" not in second
        assert _counts(runtime) == (0, 0, 1, 1)
    finally:
        release.set()
        first = await first_task  # never leave the held launch behind
    assert first["status"] == "pending", first
    await tools.drain_launch_turns()
    assert _counts(runtime) == (1, 1, 1, 0)
    assert getattr(jobs, "_pending_manifest_job_starts", {}) == {}


# ---------------------------------------------------------------------------
# Distinct manifests stay independent (red case 7); one manifest's
# installations serialise (red case 8, the accepted trade-off)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("legacy", [False, True])
async def test_an_incidental_artifact_does_not_block_its_own_plugin(runtime, tmp_path, monkeypatch, legacy):
    _real_discovery(monkeypatch, tmp_path)
    e = entry("ledger", ["specialist:finance"])
    _artifact(tmp_path, e, _jobs())
    helper = entry("helper", ["specialist:finance", "resident:assistant"])
    _artifact(tmp_path, helper, _jobs())
    _load(tmp_path, [e, helper])
    first = await start("ledger:classify")
    assert first["status"] == "pending", first
    await tools.drain_launch_turns()
    rec = runtime.registry.get(first["engagement_id"])
    assert {a["name"] for a in rec.plugin_artifacts} == {"ledger", "helper"}
    if legacy:
        rec.origin.pop("plugin_job", None)
        for artifact in rec.plugin_artifacts:
            artifact.pop("manifest_name")
        await runtime.registry.persist_origin(rec.id)
        await _reload(runtime, monkeypatch, rec, resume=False)
    second = await start("helper:classify")
    assert second["status"] == "pending", second
    await tools.drain_launch_turns()
    assert _counts(runtime) == (2, 2, 2, 0)
    assert {r.origin["job"]["name"] for r in tools._engagement_registry.active_and_idle()} == {
        "ledger:classify", "helper:classify"}


@pytest.mark.parametrize("second_name", ["ledger:classify", "ledger:scan"])
@pytest.mark.parametrize("reload_record", [False, True])
async def test_installations_sharing_a_manifest_name_serialise(runtime, tmp_path, monkeypatch,
                                                              second_name, reload_record):
    _real_discovery(monkeypatch, tmp_path)
    a, b = _two_installations(tmp_path)
    _load(tmp_path, [a])
    first = await start("ledger:classify")
    assert first["status"] == "pending", first
    await tools.drain_launch_turns()
    rec = runtime.registry.get(first["engagement_id"])
    assert rec.origin["plugin_job"] == {"plugin": "finance.ledger"}
    if reload_record:
        await _reload(runtime, monkeypatch, rec, resume=False)
    _load(tmp_path, [a, b])
    host = FIND(second_name, "assistant", ["finance"])
    assert (host.kind, host.plugin.name) == ("resident", "ledger")
    second = await start(second_name)
    assert second.get("kind") == "job_busy", second
    assert second["engagement_id"] == rec.id
    assert _counts(runtime) == (1, 1, 1, 0)


# ---------------------------------------------------------------------------
# host + session together, end to end (S1b x S1)
# ---------------------------------------------------------------------------

REAL_JOB_AFTER_TURN = jobs.job_after_turn


async def test_a_fresh_specialist_hosted_job_runs_end_to_end_and_is_reaped(
        runtime, tmp_path, monkeypatch, home):
    """A declaration with BOTH `"host": "specialist"` and `"session": "fresh"`,
    from a resident that also holds the plugin: it routes to finance, the launch
    names its job id once, each of its two batches is preceded by exactly one
    reset and starts with the brief, the sid ledger names all three sessions,
    the job ends, and the #1162 reaper deletes exactly those transcripts. A
    path that dropped `session` for specialist-host declarations would run
    resume-mode batches and fail here."""
    from drivers.in_casa_driver import InCasaDriver
    from test_job_fresh_conversation import (
        RESET, FreshClient, _project, _seed, _snapshot, _uuids, ledger)
    from test_background_jobs_loop import result, text_frame
    import agent
    import engagement_transcript_reaper

    _real_discovery(monkeypatch, tmp_path)
    e = entry("ledger", ["resident:assistant", "specialist:finance"])
    _artifact(tmp_path, e, [dict(_job(), host="specialist", session="fresh")])
    _load(tmp_path, [e])
    host = FIND("ledger:classify", "assistant", ["finance"])
    assert (host.kind, host.role, host.decl.session, host.decl.host) == (
        "specialist", "finance", "fresh", "specialist")

    launch_sid, b1, b2, unrelated = _uuids(4)
    clients = []

    async def report():
        reply = await tools.report_job_progress.handler(
            {"summary": "Handled rows", "progressed": True})
        assert json.loads(reply["content"][0]["text"]) == {"ok": True}

    async def complete():
        reply = await tools.emit_completion.handler({"text": "All rows handled"})
        assert json.loads(reply["content"][0]["text"]).get("kind") != "unread_inbound"

    class SdkClient(FreshClient):
        def __init__(self, options):
            super().__init__()
            self.options = options
            self.sid = launch_sid
            self.next_sid = {1: b1, 2: b2}.__getitem__
            self.scripts = [[text_frame("Starting the job"), result()],
                            [text_frame("Batch work"), report, result()],
                            [complete, result()]]
            clients.append(self)

    # The production wiring casa_core gives the channel and driver, so the
    # batch loop runs for real after the launch turn.
    channel, reg = runtime.channel, runtime.registry
    driver = InCasaDriver(topic_stream_factory=channel.create_topic_stream,
                          persist_session_id=reg.persist_session_id,
                          record_lookup=reg.get,
                          begin_turn_delivery=reg.begin_turn_delivery)
    channel._engagement_driver = driver
    channel._driver_admit_inbound = lambda r, t: driver.admit_inbound(r.id, t)
    channel._driver_discharge_inbound = lambda r, t: driver.discharge_inbound(r.id, t)
    channel._driver_inbound_held = lambda r, t: driver.inbound_token_held(r.id, t)
    channel._driver_turn_incomplete = lambda r, t: driver.followup_turn_incomplete(r.id, t)

    async def send(r, text, *, tg_message_id=None, inbound_token=None, batch=None):
        await driver.send_user_turn(r, text, inbound_token=inbound_token, batch=batch)
    channel._driver_send_user_turn = send
    monkeypatch.setattr(agent, "active_engagement_driver", driver)
    monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", SdkClient)
    monkeypatch.setattr(tools, "ClaudeSDKClient", SdkClient)
    monkeypatch.setattr(jobs, "job_after_turn", REAL_JOB_AFTER_TURN)

    started = await start("ledger:classify")
    assert started["status"] == "pending", started
    assert started["agent"] == "finance"
    await tools.drain_launch_turns()

    async def settle():
        for _ in range(200):
            tasks = list(channel._turn_tasks | channel._inbound_cleanup_tasks
                         | tools._finalize_tail_tasks | reg._deferred_persists)
            if not tasks:
                return
            await asyncio.gather(*tasks)
            await asyncio.sleep(0)
        pytest.fail("tasks did not settle")
    await asyncio.wait_for(settle(), 10)

    rec = reg.get(started["engagement_id"])
    assert (rec.kind, rec.role_or_type) == ("specialist", "finance")
    assert rec.origin["plugin_job"] == {"plugin": "ledger"}
    assert rec.origin["job"]["session"] == "fresh"
    assert len(clients) == 1
    prompts = clients[0].prompts
    launch = prompts[0]
    assert [line for line in launch.splitlines() if line.startswith("Job id:")] == [
        f"Job id: {rec.id}"]
    brief = jobs.job_brief(rec)
    title = host.decl.title
    assert prompts[1:] == [RESET, f"{brief}\n\n{jobs.batch_prompt(1, title)}",
                           RESET, f"{brief}\n\n{jobs.batch_prompt(2, title)}"]
    assert clients[0].clears == 2
    assert ledger(rec.origin) == [launch_sid, b1, b2]
    assert rec.status == "completed"
    assert reg.active_and_idle() == []
    # No resident-hosted worker was ever created.
    assert [r.kind for r in reg._records.values()] == ["specialist"]

    project = _project(home, tools.specialist_cwd(runtime.cfg))
    for sid in (launch_sid, b1, b2, unrelated):
        _seed(project, sid)
    keep = {k: v for k, v in _snapshot(project).items()
            if k.parts[0] in (f"{unrelated}.jsonl", unrelated)}
    assert len(keep) == 2
    counts = await engagement_transcript_reaper.reap_engagement_transcripts(reg)
    for sid in (launch_sid, b1, b2):
        assert not (project / f"{sid}.jsonl").exists(), sid
        assert not (project / sid).exists(), sid
    assert _snapshot(project) == keep
    assert counts["deleted"] == 6 and counts["errors"] == 0


# ---------------------------------------------------------------------------
# #1173: a job whose strict terminal write is pending still occupies its
# plugin and manifest name (INV-BGJOB-006)
# ---------------------------------------------------------------------------

_LEDGER_JOB = {"name": "ledger:classify", "title": "Classify"}


def _ledger_host(kind, role, installed, name="scan"):
    decl = jobs.JobDecl(qualified_name=f"ledger:{name}", plugin="ledger", name=name,
                        skill=f"ledger:{name}", title=name.title(), summary=None,
                        batches=None, turns_per_batch=None)
    return jobs.JobHost(kind=kind, role=role, decl=decl,
                        plugin=SimpleNamespace(name=installed))


def _live_ledger_jobs(reg):
    return sum(1 for r in reg.active_and_idle()
               if jobs.job_manifest_name((r.origin.get("job") or {}).get("name")) == "ledger")


async def _specialist_ledger_job(tmp_path, limiter, prior):
    """A live specialist-hosted `finance.ledger` job holding its role's permit."""
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    rec = await reg.create("specialist", "finance", "in_casa", "classify",
                           {"job": dict(_LEDGER_JOB),
                            "plugin_job": {"plugin": "finance.ledger"}}, 7)
    rec.permit = limiter.try_acquire("finance:engagement")
    assert rec.permit is not None
    if prior == "idle":
        await reg.mark_idle(rec.id)
    assert rec.status == prior
    return reg, rec


# Every contender's permit scope differs from the record's `finance:engagement`,
# so the held permit refuses none of them: only the job guard can.
_CONTENDERS = [("resident", "assistant", "finance.ledger"),
               ("resident", "assistant", "ledger"),
               ("specialist", "backup", "finance.ledger"),
               ("specialist", "backup", "ledger")]


@pytest.mark.parametrize("contender", _CONTENDERS, ids=lambda c: f"{c[0]}-{c[2]}")
@pytest.mark.parametrize("outcome", ["completed", "cancelled", "error"])
@pytest.mark.parametrize("prior", ["active", "idle"])
async def test_a_start_during_a_pending_strict_terminal_write_is_refused(
        tmp_path, monkeypatch, prior, outcome, contender):
    """A start that lands while the job's strict terminal write is pending is
    refused `job_busy`; the write then fails, the record rolls back to live,
    and exactly one live job carries the manifest name. At the base the start
    was admitted and, carried through its create, left two."""
    from specialist_limits import SpecialistLimiter

    limiter = SpecialistLimiter(2)
    reg, rec = await _specialist_ledger_job(tmp_path, limiter, prior)
    host = _ledger_host(*contender)
    plugin = jobs.host_plugin_name(host)
    jobs.release_job_start(plugin)
    entered, release = threading.Event(), threading.Event()
    real_write = reg._write_tombstone

    def held_then_failed(snapshot):
        entered.set()
        release.wait(10)
        raise OSError("disk full")

    monkeypatch.setattr(reg, "_write_tombstone", held_then_failed)
    transition = asyncio.ensure_future(
        reg.try_transition_terminal(rec.id, outcome, strict=True))
    refusal, claimed_during = None, None
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        assert reg.active_and_idle() == []          # the status filter is unchanged
        refusal = jobs.claim_job_start(host, reg)
        claimed_during = plugin in jobs._pending_plugin_job_starts
        if host.kind == "resident":
            permit, busy = jobs.acquire_job_permit(host, limiter)
        else:
            permit, busy = limiter.try_acquire(f"{host.role}:engagement"), None
        assert permit is not None and busy is None  # cross-scope: not the permit's job
        permit.release()
    finally:
        release.set()
        with pytest.raises(OSError):
            await transition
        monkeypatch.setattr(reg, "_write_tombstone", real_write)
    try:
        if refusal is None:
            # What the admitted launch does next: its record commits after the
            # rollback, beside the restored one.
            await reg.create("specialist", host.role, "in_casa", "scan",
                             {"job": {"name": host.decl.qualified_name, "title": "Scan"},
                              "plugin_job": {"plugin": plugin}}, 8)
    finally:
        jobs.release_job_start(plugin)

    # One tuple, so the base shows every wrong count at once: (None, True, 2).
    assert ((refusal or {}).get("kind"), claimed_during, _live_ledger_jobs(reg)) == (
        "job_busy", False, 1)
    assert refusal["engagement_id"] == rec.id
    assert rec.status == prior
    assert plugin not in jobs._pending_plugin_job_starts


@pytest.mark.parametrize("contender", _CONTENDERS, ids=lambda c: f"{c[0]}-{c[2]}")
async def test_a_durably_ended_job_frees_its_plugin_at_once(tmp_path, contender):
    """The other half of the rule: once the strict terminal write has
    committed, the job no longer occupies anything and a start is admitted."""
    from specialist_limits import SpecialistLimiter

    limiter = SpecialistLimiter(2)
    reg, rec = await _specialist_ledger_job(tmp_path, limiter, "active")
    host = _ledger_host(*contender)
    plugin = jobs.host_plugin_name(host)
    jobs.release_job_start(plugin)
    assert await reg.try_transition_terminal(rec.id, "completed", strict=True)
    try:
        assert jobs.claim_job_start(host, reg) is None
        assert plugin in jobs._pending_plugin_job_starts
    finally:
        jobs.release_job_start(plugin)
    assert _live_ledger_jobs(reg) == 0
    assert rec.permit is None or limiter.try_acquire("finance:engagement") is not None


@pytest.mark.parametrize("write_fails", [True, False], ids=["rolled-back", "committed"])
@pytest.mark.parametrize("cancel_caller", [False, True], ids=["awaited", "caller-cancelled"])
async def test_the_pending_window_opens_at_the_commit_and_closes_when_the_write_settles(
        tmp_path, monkeypatch, write_fails, cancel_caller):
    """`job_occupants()` holds the record exactly while its strict terminal
    write is pending; `active_and_idle()` keeps its meaning throughout, and
    the window closes however the write settles — a cancelled caller
    included — so a plugin is never left occupied by a durably ended job."""
    from specialist_limits import SpecialistLimiter

    reg, rec = await _specialist_ledger_job(tmp_path, SpecialistLimiter(2), "active")
    entered, release = threading.Event(), threading.Event()
    real_write = reg._write_tombstone

    def held(snapshot):
        entered.set()
        release.wait(10)
        if write_fails:
            raise OSError("disk full")
        real_write(snapshot)

    monkeypatch.setattr(reg, "_write_tombstone", held)
    transition = asyncio.ensure_future(
        reg.try_transition_terminal(rec.id, "completed", strict=True))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        assert (len(reg.active_and_idle()), [r.id for r in reg.job_occupants()]) == (0, [rec.id])
        if cancel_caller:
            transition.cancel()
            await asyncio.sleep(0)
            assert [r.id for r in reg.job_occupants()] == [rec.id]
    finally:
        release.set()
        outcome = await asyncio.gather(transition, return_exceptions=True)
    if cancel_caller:
        assert isinstance(outcome[0], asyncio.CancelledError)
    elif write_fails:
        assert isinstance(outcome[0], OSError)
    else:
        assert outcome == [True]
    assert reg._terminal_pending == set()
    live = 1 if write_fails else 0
    assert (rec.status, len(reg.active_and_idle()), len(reg.job_occupants())) == (
        "active" if write_fails else "completed", live, live)


async def test_an_unrelated_plugin_starts_while_another_jobs_terminal_write_is_pending(
        tmp_path, monkeypatch):
    """The pending window occupies only the record's own plugin and manifest
    name: a job of another manifest is admitted inside it."""
    from specialist_limits import SpecialistLimiter

    reg, rec = await _specialist_ledger_job(tmp_path, SpecialistLimiter(2), "active")
    other = dataclasses.replace(
        _ledger_host("resident", "assistant", "helper"),
        decl=dataclasses.replace(_ledger_host("resident", "assistant", "helper").decl,
                                 qualified_name="helper:scan", plugin="helper"))
    jobs.release_job_start("helper")
    entered, release = threading.Event(), threading.Event()

    def held(snapshot):
        entered.set()
        release.wait(10)
        raise OSError("disk full")

    monkeypatch.setattr(reg, "_write_tombstone", held)
    transition = asyncio.ensure_future(
        reg.try_transition_terminal(rec.id, "cancelled", strict=True))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        assert len(reg.job_occupants()) == 1
        assert jobs.claim_job_start(other, reg) is None
        assert jobs.pending_plugin_job_start("helper") == ("helper:scan", "Scan")
    finally:
        jobs.release_job_start("helper")
        release.set()
        with pytest.raises(OSError):
            await transition


async def test_the_installed_plugin_check_also_counts_a_pending_terminal_write(
        tmp_path, monkeypatch):
    """The per-installed-plugin check reads the same occupants as the
    manifest check: a pending record of this installation whose job carries
    an older manifest name (the plugin was renamed while it ran) is matched
    only by its `plugin_job` marker, and still refuses."""
    from specialist_limits import SpecialistLimiter

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    rec = await reg.create("specialist", "finance", "in_casa", "classify",
                           {"job": {"name": "oldledger:classify", "title": "Classify"},
                            "plugin_job": {"plugin": "finance.ledger"}}, 7)
    rec.permit = SpecialistLimiter(2).try_acquire("finance:engagement")
    host = _ledger_host("resident", "assistant", "finance.ledger")
    jobs.release_job_start("finance.ledger")
    assert jobs.running_job_for_manifest(reg, "ledger") is None
    entered, release = threading.Event(), threading.Event()

    def held(snapshot):
        entered.set()
        release.wait(10)
        raise OSError("disk full")

    monkeypatch.setattr(reg, "_write_tombstone", held)
    transition = asyncio.ensure_future(
        reg.try_transition_terminal(rec.id, "completed", strict=True))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        refusal = jobs.claim_job_start(host, reg)
    finally:
        jobs.release_job_start("finance.ledger")
        release.set()
        with pytest.raises(OSError):
            await transition
    assert (refusal or {}).get("kind") == "job_busy"
    assert refusal["engagement_id"] == rec.id
