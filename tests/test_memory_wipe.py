"""§14 test 8: the wipe, through the container-start step, on a real tree.

A canary goes into every registry row (the store's tables, the spool and its
drain snapshot, the search index, a finished and an in-progress export, a
staged restore source, a transcript of every role, the root's own transcript
directory, the session pointers and an explanation record), plus a staging file each named producer's atomic helper leaves when a real
process is killed between its write and its rename. A further canary is
captured after consent, in the window before the restart.

After the step: 0 canary bytes anywhere, every kept item byte-identical,
the session pointers reset, and nothing left that a spool drain or a reaper
could bring back (INV-MEM-038)."""
from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

import memory_wipe_support as w
from memory import capture, maintenance, registry
from memory.store import MemoryStore

pytestmark = [pytest.mark.unit]


def _event(text: str):
    return capture.event(role="assistant", channel="telegram",
                         speaker_kind="operator", origin_kind="operator",
                         text=text, eligibility="eligible",
                         origin_tier="private")


def _kill_on_rename(body: str) -> str:
    """*body* run with ``os.replace`` and ``os.rename`` killing the process:
    a staging file is written and fsynced, and never renamed."""
    return "\n".join([
        "import os",
        "def _boom(*a, **k):",
        "    _die()",
        "os.replace = _boom",
        "os.rename = _boom",
        body,
    ])


_RECORD = ("explanation_store.ExplanationRecord(correlation_id={cid!r},"
           " role_id='assistant', kind='turn', resolved_model='m',"
           " persona_ref=None, role_checksum='r', binding_digest=None,"
           " dependency_digests=(), effective_config_digest=None,"
           " lifecycle_state=None, projection='p', static_prompt_digest='d',"
           " static_prompt_estimated_tokens=1, memory_tiers=(),"
           " memory_attributions=(), tool_calls=(), denials=(),"
           " memory_text={text!r})")


async def build(tree: w.Tree, monkeypatch) -> dict:
    root = tree.root
    u = lambda p: w.under(root, p)            # noqa: E731
    facts = {}

    # memory.db: an event, and a row in a table an earlier schema created
    # and this one no longer declares — an install created before a table's
    # removal still holds it, and the wipe must take it too
    store = MemoryStore(str(u(registry.MEMORY_DIR)))
    assert store.open()
    assert capture.append(store, _event("told " + tree.canary())).outcome == (
        capture.STORED)
    with store.transaction(write=True) as conn:
        conn.execute("CREATE TABLE engagement_summaries (text TEXT NOT NULL)")
        conn.execute("INSERT INTO engagement_summaries (text) VALUES (?)",
                     ("summary " + tree.canary(),))
    # the spool and the snapshot a cut-off drain leaves
    w.write(u(f"{registry.MEMORY_DIR}/spool.jsonl"),
            json.dumps({"text": tree.canary()}) + "\n")
    w.write(u(f"{registry.MEMORY_DIR}/spool.draining.jsonl"),
            json.dumps({"text": tree.canary()}) + "\n")
    # the search index with its sidecar
    w.write(u(f"{registry.MEMORY_DIR}/search.db"), tree.canary())
    w.write(u(f"{registry.MEMORY_DIR}/search.db-wal"), tree.canary())
    # exports: one finished, one being written; a staged restore source
    w.write(u(f"{registry.MEMORY_DIR}/exports/memory-2026-10-01.tar"),
            tree.canary())
    w.write(u(f"{registry.MEMORY_DIR}/exports/.memory-2026-11-02.tar.part"),
            tree.canary())
    w.write(u(f"{registry.MEMORY_DIR}/restore-source/built.db"), tree.canary())
    # transcripts of every role, and the root's own real directory
    for role in ("assistant", "butler", "finance", "configurator"):
        w.write(u(f"/config/cc-home/.claude/projects/-config-agent-home-"
                  f"{role}/{w.SID}.jsonl"), tree.canary())
    w.write(u("/root/.claude/projects/-root/old.jsonl"), tree.canary())
    # session pointers
    w.write(u("/data/sessions.json"), json.dumps(
        {"telegram:1": {"sdk_session_id": tree.canary()}}))
    # an explanation record
    import explanation_store
    rec = eval(_RECORD.format(cid="abcd1234", text="remembered " + tree.canary()),
               {"explanation_store": explanation_store})
    explanation_store.ExplanationStore(u("/data/explanations")).record(rec)

    # staging files, each left by a real process killed before its rename
    child = w.run_child(_kill_on_rename("\n".join([
        "from session_registry import SessionRegistry",
        f"SessionRegistry({str(u('/data/sessions.json'))!r})._write("
        f"{{'telegram:2': {{'sdk_session_id': {tree.canary()!r}}}}})",
    ])))
    assert w.killed(child), child.stderr
    assert len(list(u("/data").glob(".sessions.json.*.tmp"))) == 1
    child = w.run_child(_kill_on_rename("\n".join([
        "import explanation_store, pathlib",
        "r = " + _RECORD.format(cid="ef567890", text=tree.canary()),
        "explanation_store.ExplanationStore(pathlib.Path("
        f"{str(u('/data/explanations'))!r})).record(r)",
    ])))
    assert w.killed(child), child.stderr
    assert len(list(u("/data/explanations").glob("*.tmp"))) == 1
    # what the wipe keeps
    tree.keep(u("/data/reminders.json"), json.dumps(
        [{"id": "r1", "text": "water the plants", "due": "2026-11-03T08:00"}]))
    tree.keep(u("/data/triggers.json"), json.dumps([{"id": "t1"}]))
    tree.keep(u("/data/agent-inbox/invoice.pdf"), b"%PDF-1.4 kept")
    tree.keep(u("/config/agents/assistant/prompt.md"), "You are Ellen.\n")
    tree.keep(u("/config/agents/assistant/runtime.yaml"), "token_budget: 4000\n")
    return facts


async def test_wipe_erases_every_registry_row_and_keeps_the_rest(
        tmp_path, monkeypatch):
    tree = w.Tree(tmp_path / "root")
    root = tree.root
    proc = w.fake_proc(tmp_path)
    await build(tree, monkeypatch)
    before = w.hits(root, tree.canaries)
    assert len(before) >= 17, before

    # consent at a door, then a turn in the window captures another canary
    assert maintenance.write_wipe_intent(str(root)) == "written"
    store = MemoryStore(str(w.under(root, registry.MEMORY_DIR)))
    assert store.open()
    assert capture.append(store, _event("window " + tree.canary())).outcome == (
        capture.STORED)

    p = w.run_oneshot(root, proc)
    assert p.returncode == 0 and p.stdout.strip() == "done", p.stderr

    assert w.hits(root, tree.canaries) == []
    assert w.kept_changes(tree) == []
    assert maintenance.read_intent(str(root)) is None
    u = lambda p: w.under(root, p)            # noqa: E731
    assert json.loads(u("/data/sessions.json").read_text()) == {}
    for gone in ("spool.jsonl", "spool.draining.jsonl", "search.db",
                 "search.db-wal", "exports", "restore-source"):
        assert not (u(registry.MEMORY_DIR) / gone).exists(), gone
    assert not u("/data/explanations").exists()
    assert list(u("/data").glob(".sessions.json.*.tmp")) == []
    # every transcript root is emptied
    roots = [u("/config/cc-home/.claude/projects"), u("/root/.claude/projects")]
    assert sum(len(list(d.iterdir())) for d in roots) == 0

    # a fresh, empty store records the outcome; nothing can be re-imported
    conn = sqlite3.connect(store.path)
    try:
        assert conn.execute("SELECT count(*) FROM events").fetchone() == (0,)
        meta = dict(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()
    record = json.loads(meta["last_maintenance"])
    assert (record["kind"], record["outcome"], record["notice_sent"]) == (
        "wipe", "done", False)
    assert set(meta) == {"last_maintenance"}
    assert capture.drain_spool(store) == 0


def test_the_done_line_is_claimed_once(tmp_path):
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    assert maintenance.write_wipe_intent(str(root)) == "written"
    assert w.run_oneshot(root, proc).stdout.strip() == "done"
    store = MemoryStore(str(w.under(root, registry.MEMORY_DIR)))
    assert store.open()
    lines = [maintenance.claim_outcome_notice(store) for _ in range(3)]
    assert lines == [maintenance.FORGOTTEN_LINE, None, None]


def test_without_an_intent_nothing_is_touched_but_an_orphan_restore_source(
        tmp_path):
    tree = w.Tree(tmp_path / "root")
    root, proc = tree.root, w.fake_proc(tmp_path)
    tree.keep(w.under(root, "/data/memory/memory.db"), "not a canary")
    tree.keep(w.under(root, "/data/sessions.json"), '{"a": 1}')
    w.write(w.under(root, "/data/memory/restore-source/built.db"), "staged")
    assert w.run_oneshot(root, proc).stdout.strip() == "none"
    assert w.kept_changes(tree) == []
    assert not w.under(root, "/data/memory/restore-source").exists()


def test_every_registry_row_has_a_wipe_handler():
    missing = [e.name for e in registry.REGISTRY
               if (e.kind, e.wipe) not in maintenance._HANDLERS]
    assert missing == []


def test_a_row_with_no_handler_stops_the_wipe(tmp_path, monkeypatch):
    """The loop is driven by the registry: a row it does not know how to wipe
    fails the attempt instead of being skipped."""
    monkeypatch.setattr(maintenance, "_HANDLERS", {})
    with pytest.raises(maintenance.MaintenanceError, match="no wipe rule"):
        maintenance.wipe_loop(root=str(tmp_path / "root"))


def test_a_killed_intent_write_leaves_staging_the_wipe_deletes(tmp_path):
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    child = w.run_child(_kill_on_rename("\n".join([
        "from memory import maintenance as m",
        f"m.write_wipe_intent({str(root)!r})",
    ])))
    assert w.killed(child), child.stderr
    memdir = w.under(root, registry.MEMORY_DIR)
    assert len(list(memdir.glob(".maintenance.json.*.tmp"))) == 1
    assert maintenance.read_intent(str(root)) is None
    assert maintenance.write_wipe_intent(str(root)) == "written"
    assert w.run_oneshot(root, proc).stdout.strip() == "done"
    assert list(memdir.glob(".maintenance.json*")) == []


# --- an open in_casa engagement after the wipe --------------------------------------

async def _record_file(path):
    from engagement_registry import EngagementRecord, EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(path))
    for eid, driver, status in (("e1" * 16, "in_casa", "active"),
                                ("e2" * 16, "in_casa", "completed"),
                                ("e3" * 16, "in_casa", "cancelled"),
                                ("e4" * 16, "in_casa", "idle")):
        reg._records[eid] = EngagementRecord(
            id=eid, kind="executor", role_or_type="configurator",
            driver=driver, status=status, started_at=0.0,
            last_user_turn_ts=0.0,
            completed_at=None if status == "active" else time.time(),
            # e4 was persisted before its first session id was recorded
            sdk_session_id=(w.SID if driver == "in_casa" and eid != "e4" * 16
                            else None),
            origin={}, task="Tidy the garden notes")
    await reg._write_tombstone_locked(strict=True)


async def _resume(path, eid, monkeypatch):
    """The job's resume through the real in_casa driver, over the record
    file as the step left it. Returns the refusal (or ``None``), whether a
    session was built for it, and the loaded record."""
    import tools
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(path))
    await reg.load()
    built: list = []
    monkeypatch.setattr(tools, "build_engagement_resume_options",
                        lambda rec, sid: built.append(sid))
    driver = InCasaDriver(record_lookup=reg.get)
    rec = reg.get(eid)
    try:
        await driver.resume(rec, rec.sdk_session_id or "")
    except Exception as exc:  # noqa: BLE001 — the refusal is the result
        refusal = exc
    else:
        refusal = None
    return refusal, len(built), driver.is_alive(rec), rec


@pytest.mark.parametrize("eid", ["e1" * 16, "e4" * 16])
async def test_an_open_in_casa_engagement_is_not_resumed_after_the_wipe(
        tmp_path, monkeypatch, eid):
    """The CLI cannot resume a session whose transcript the wipe deleted
    (measured: "No conversation found", exit 1). The step marks every open
    in_casa record — active or idle, with a session or persisted before its
    first one — and never a terminal one; the driver refuses to resume a
    marked record before it builds a session for it."""
    from drivers.in_casa_driver import DriverNotAliveError
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    record = w.under(root, "/data/engagements.json")
    record.parent.mkdir(parents=True)
    await _record_file(record)
    w.write(w.under(root, f"/config/cc-home/.claude/projects/-cwd/{w.SID}.jsonl"),
            "turns")
    assert maintenance.write_wipe_intent(str(root)) == "written"
    assert w.run_oneshot(root, proc).stdout.strip() == "done"
    rows = {r["id"]: r for r in json.loads(record.read_text())}
    assert sorted(i for i, r in rows.items() if r["context_rebuild_pending"]) == [
        "e1" * 16, "e4" * 16]

    refusal, built, alive, rec = await _resume(record, eid, monkeypatch)

    assert isinstance(refusal, DriverNotAliveError)
    assert (built, alive) == (0, False)
    assert rec.status in ("active", "idle")


# --- a background job after the wipe ------------------------------------------------

JOB_ID = "f1" * 16


async def _wiped_job(tmp_path, monkeypatch, *, awaiting=None):
    """One open job's record, through the container-start step, then Casa's
    boot wiring around it: the real registry loaded from the file the step
    left, the real in_casa driver and the real job runner wired as
    ``casa_core`` wires them. The CLI is the session driver tests' fake; the
    finalize funnel is recorded."""
    import background_jobs as bj
    import job_inbox
    import tools
    from claude_agent_sdk import ClaudeAgentOptions
    from drivers import in_casa_driver as drv
    from engagement_registry import EngagementRecord, EngagementRegistry
    try:
        from tests.test_job_session_driver import FakeCLI
    except ImportError:  # pragma: no cover — direct-path collection
        from test_job_session_driver import FakeCLI

    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    record = w.under(root, "/data/engagements.json")
    record.parent.mkdir(parents=True)
    job = {"title": "Garden notes", "skill": "q:job", "context": "",
           "brief_uuid": "b0000000-0000-4000-8000-000000000001"}
    if awaiting is not None:
        job[bj.AWAITING_KEY] = awaiting
    writer = EngagementRegistry(tombstone_path=str(record))
    writer._records[JOB_ID] = EngagementRecord(
        id=JOB_ID, kind="specialist", role_or_type="finance",
        driver="in_casa", status="active", started_at=0.0,
        last_user_turn_ts=0.0, completed_at=None, sdk_session_id=w.SID,
        origin={"channel": "telegram", "_origin_route": "telegram",
                "_origin_clearance": "private", "job": job},
        task="Tidy the garden notes")
    await writer._write_tombstone_locked(strict=True)
    w.write(w.under(root, f"/config/cc-home/.claude/projects/-cwd/{w.SID}.jsonl"),
            "turns")
    assert maintenance.write_wipe_intent(str(root)) == "written"
    assert w.run_oneshot(root, proc).stdout.strip() == "done"

    FakeCLI.instances, FakeCLI.events = [], []
    FakeCLI.on_enter = FakeCLI.hold_disconnect = None
    monkeypatch.setattr(drv, "ClaudeSDKClient", FakeCLI)
    monkeypatch.setattr(job_inbox, "INBOX_ROOT", tmp_path / "job-inbox")
    transcript = tmp_path / f"{w.SID}.jsonl"
    transcript.write_text("")
    monkeypatch.setattr(job_inbox, "transcript_path", lambda cwd, sid: transcript)
    monkeypatch.setattr(tools, "build_engagement_resume_options",
                        lambda rec, sid: ClaudeAgentOptions(resume=sid))
    finalized: list[dict] = []

    async def finalize(rec, **kw):
        finalized.append(kw)
        rec.status = kw["outcome"]
    monkeypatch.setattr(tools, "_finalize_engagement", finalize)

    class Channel:
        def __init__(self):
            self.notices: list = []

        def __getattr__(self, name):
            # Any post or send is recorded, so "nothing posted" is checked.
            if not name.lstrip("_").startswith(("send", "post", "deliver")):
                raise AttributeError(name)

            async def _record(*a, **kw):
                self.notices.append((name, a, kw))
            return _record

    reg = EngagementRegistry(tombstone_path=str(record))
    await reg.load()
    channel = Channel()
    monkeypatch.setattr(bj, "_states", {})
    monkeypatch.setattr(bj, "_runner", None)
    from agent_log_double import RecordingLogs
    runner = bj.JobRunner(registry=reg, channel=channel,
                          agent_logs=RecordingLogs())
    driver = drv.InCasaDriver(
        record_lookup=reg.get,
        begin_turn_delivery=reg.begin_turn_delivery,
        on_turn_end=runner.turn_ended, on_session_lost=runner.session_lost,
        on_operator_written=bj.note_operator_input)
    runner.driver = driver
    bj.configure(runner)
    return reg, driver, channel, finalized, FakeCLI


async def test_a_wiped_job_is_not_resumed_at_boot_and_ends_as_an_error(
        tmp_path, monkeypatch):
    """The wipe deleted the job's session transcript, so the one long session
    cannot continue. Boot's resume of every open job logs that it is
    resuming, opens no session for the job and ends it as an error: "could
    not be resumed after a restart"."""
    import casa_core
    reg, driver, channel, finalized, cli = await _wiped_job(tmp_path, monkeypatch)
    assert reg.get(JOB_ID).context_rebuild_pending is True

    await casa_core._resume_background_jobs(reg, channel)

    import background_jobs
    assert background_jobs._runner.agent_logs.texts() == ["↻ Resuming after a restart"]
    assert channel.notices == []
    assert cli.instances == []
    assert [(f["outcome"], f["text"]) for f in finalized] == [
        ("error", "could not be resumed after a restart")]


async def test_a_wiped_job_waiting_on_the_operator_ends_at_their_next_message(
        tmp_path, monkeypatch):
    """A job that was waiting for the operator, with nothing journaled, is
    left alone at boot. Their next message is accepted, but the session it
    would be written into cannot be resumed, so the job ends as an error:
    "its session ended unexpectedly"."""
    import asyncio

    import background_jobs as bj
    import casa_core
    reg, driver, channel, finalized, cli = await _wiped_job(
        tmp_path, monkeypatch, awaiting=bj.AWAITING_ASKED)

    await casa_core._resume_background_jobs(reg, channel)
    assert channel.notices == [] and finalized == []

    driver.submit_operator(reg.get(JOB_ID), "carry on")
    for _ in range(200):
        if finalized:
            break
        await asyncio.sleep(0.005)

    assert cli.instances == []
    assert [(f["outcome"], f["text"]) for f in finalized] == [
        ("error", "its session ended unexpectedly")]
    await driver.cancel(reg.get(JOB_ID))


async def test_a_valid_record_file_keeps_every_record_and_field(tmp_path):
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    record = w.under(root, "/data/engagements.json")
    record.parent.mkdir(parents=True)
    await _record_file(record)
    before = json.loads(record.read_text())
    assert maintenance.write_wipe_intent(str(root)) == "written"
    assert w.run_oneshot(root, proc).stdout.strip() == "done"
    after = json.loads(record.read_text())
    marked = {r["id"] for r in after if r.get("context_rebuild_pending")}
    assert marked == {"e1" * 16, "e4" * 16}
    for row in after:
        row["context_rebuild_pending"] = next(
            b for b in before if b["id"] == row["id"])["context_rebuild_pending"]
    assert after == before
    assert (record.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize("content", [
    b'[{"id": "e1", "task": "KEEP-{canary}"',                 # not JSON
    b'{"rows": "KEEP-{canary}"}',                              # not a list
    b'[{"id": "e1", "status": "active", "task": "KEEP-{canary}"}]',  # no driver
    b'[{"id": "e1", "status": "active", "driver": "in_casa"}, "KEEP-{canary}"]',
    b'["\xff KEEP-{canary}"]',                                 # not UTF-8
])
def test_an_unreadable_record_file_is_left_byte_identical(tmp_path, content):
    """r4 S1 (both reviewers): the step never repairs the engagement record
    file. One it cannot read strictly is left exactly as it is, nothing is
    marked, and the wipe still completes."""
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    canary = os.urandom(8).hex()
    data = content.replace(b"{canary}", canary.encode())
    record = w.write(w.under(root, "/data/engagements.json"), data)
    assert maintenance.write_wipe_intent(str(root)) == "written"
    p = w.run_oneshot(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    assert record.read_bytes() == data
