"""§14 test 8: the wipe, through the container-start step, on a real tree.

A canary goes into every registry row (the store's tables, the spool and its
drain snapshot, the search index, a finished and an in-progress export, a
staged restore source, a transcript of every role and of an engagement, the root's own transcript
directory, the session pointers, an explanation record, and the
executor-memory block of an open and of a completed retained engagement),
plus a staging file each named producer's atomic helper leaves when a real
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
from memory import capture, executor_block, maintenance, registry
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

    # memory.db: an event and an engagement summary
    store = MemoryStore(str(u(registry.MEMORY_DIR)))
    assert store.open()
    assert capture.append(store, _event("told " + tree.canary())).outcome == (
        capture.STORED)
    with store.transaction(write=True) as conn:
        conn.execute(
            "INSERT INTO engagement_summaries (source_key, role, "
            "procedural_epoch, tier, text, engagement_id, at) VALUES "
            "('event:s1', 'assistant', '', 'private', ?, 'e1', 'now')",
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

    # executor memory: an open engagement, provisioned, and a completed one
    # still inside its retention
    open_ws = await w.provision(root, w.EID_OPEN,
                                memory=w.memory_block(tree.canary()),
                                monkeypatch=monkeypatch)
    w.write(u(f"/data/engagement-ctl/{w.EID_OPEN}/.casa-meta.json"),
            json.dumps({"status": "UNDERGOING", "retention_until": None}))
    w.write(open_ws / f".home/.claude/projects/-data-engagements-"
            f"{w.EID_OPEN}/{w.SID}.jsonl", tree.canary())
    block = w.memory_block(tree.canary())
    done_ws = w.marked_workspace(root, w.EID_DONE, block)
    w.write(done_ws / f".home/.claude/projects/-data-engagements-"
            f"{w.EID_DONE}/{w.SID}.jsonl", tree.canary())

    # staging files, each left by a real process killed before its rename
    child = w.run_child(_kill_on_rename("\n".join([
        "import types",
        "from drivers import workspace as ws",
        f"ws.CONTROL_ROOT = {str(u('/data/engagement-ctl'))!r}",
        f"open({str(u(f'/data/engagement-ctl/{w.EID_OPEN}/.executor_memory'))!r},"
        f" 'w').write({w.memory_block(tree.canary())!r})",
        "defn = types.SimpleNamespace(type='x', prompt_template_path="
        f"{str(root / 'defs' / w.EID_OPEN / 'prompt.md')!r})",
        "rec = types.SimpleNamespace(allocated_uid=-1, origin={}, task='t')",
        f"ws.refresh_claude_md({str(open_ws)!r}, defn=defn, rec=rec)",
    ])))
    assert w.killed(child), child.stderr
    assert len(list(open_ws.glob(".CLAUDE.md.casa-tmp.*"))) == 1
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
    # what each CLAUDE.md must read afterwards: every byte outside the
    # section kept, the section empty
    facts["claude_md"] = {
        ws: _outside_markers((ws / "CLAUDE.md").read_text())
        for ws in (open_ws, done_ws)}

    # what the wipe keeps
    tree.keep(u("/data/reminders.json"), json.dumps(
        [{"id": "r1", "text": "water the plants", "due": "2026-11-03T08:00"}]))
    tree.keep(u("/data/triggers.json"), json.dumps([{"id": "t1"}]))
    tree.keep(u("/data/agent-inbox/invoice.pdf"), b"%PDF-1.4 kept")
    tree.keep(u("/config/agents/assistant/prompt.md"), "You are Ellen.\n")
    tree.keep(u("/config/agents/assistant/runtime.yaml"), "token_budget: 4000\n")
    tree.keep(u(f"/data/engagement-ctl/{w.EID_OPEN}/.session_id"), w.SID)
    tree.keep(u(f"/data/engagement-ctl/{w.EID_OPEN}/.inbound_spool.jsonl"),
              '{"n": 1}\n')
    tree.keep(open_ws / ".mcp.json", (open_ws / ".mcp.json").read_bytes())
    return facts


def _outside_markers(text: str) -> str:
    o, c = executor_block.OPEN, executor_block.CLOSE
    out, rest = [], text
    while o in rest:
        head, _, tail = rest.partition(o)
        out.append(head + o)
        rest = c + tail.partition(c)[2]
    return "".join(out) + rest


async def test_wipe_erases_every_registry_row_and_keeps_the_rest(
        tmp_path, monkeypatch):
    tree = w.Tree(tmp_path / "root")
    root = tree.root
    proc = w.fake_proc(tmp_path)
    facts = await build(tree, monkeypatch)
    before = w.hits(root, tree.canaries)
    assert len(before) >= 24, before

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
    for ws, expected in facts["claude_md"].items():
        after = (ws / "CLAUDE.md").read_text()
        assert after == expected
        assert after.count(executor_block.OPEN + executor_block.CLOSE) == 1
        assert list(ws.glob(".CLAUDE.md.casa-tmp.*")) == []
    # every transcript root is emptied
    roots = [u("/config/cc-home/.claude/projects"), u("/root/.claude/projects"),
             *u("/data/engagements").glob("*/.home/.claude/projects")]
    assert len(roots) == 4
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


def test_a_symlinked_engagement_transcript_root_is_never_followed(tmp_path):
    """An engagement's own directories are not followed: a link planted in
    place of its transcript root is not emptied through."""
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    outside = w.write(tmp_path / "elsewhere" / "keep.txt", "outside")
    claude = w.under(root, f"/data/engagements/{w.EID_OPEN}/.home/.claude")
    claude.mkdir(parents=True)
    (claude / "projects").symlink_to(outside.parent)
    assert maintenance.write_wipe_intent(str(root)) == "written"
    assert w.run_oneshot(root, proc).stdout.strip() == "done"
    assert outside.read_text() == "outside"


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

class _InCasaDriver:
    """The in_casa driver as the resume path sees it, with the CLI's measured
    answer to a resume of a session whose transcript is gone."""

    def __init__(self):
        self.alive: set[str] = set()
        self.calls: list[str] = []

    def is_alive(self, rec):
        return rec.id in self.alive

    async def resume(self, rec, sid):
        self.calls.append("resume")
        raise RuntimeError(f"No conversation found with session ID: {sid}")

    async def invalidate_session(self, rec):
        self.calls.append("invalidate")

    async def open_fresh(self, rec):
        self.calls.append("open_fresh")
        self.alive.add(rec.id)


async def _record_file(path):
    from engagement_registry import EngagementRecord, EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(path), bus=None)
    for eid, driver, status in (("e1" * 16, "in_casa", "active"),
                                ("e2" * 16, "in_casa", "completed"),
                                ("e3" * 16, "claude_code", "active"),
                                ("e4" * 16, "in_casa", "idle")):
        reg._records[eid] = EngagementRecord(
            id=eid, kind="executor", role_or_type="configurator",
            driver=driver, status=status, topic_id=7, started_at=0.0,
            last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
            completed_at=None if status == "active" else time.time(),
            # e4 was persisted before its first session id was recorded
            sdk_session_id=(w.SID if driver == "in_casa" and eid != "e4" * 16
                            else None),
            origin={}, task="Tidy the garden notes")
    await reg._write_tombstone_locked(strict=True)


async def _next_turn(path, eid):
    """The engagement's next operator turn, through the real resume gate."""
    from channels.telegram import TelegramChannel
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(path), bus=None)
    await reg.load()
    driver, notices = _InCasaDriver(), []
    channel = TelegramChannel(bot_token="T", chat_id="42",
                              default_agent="assistant", bus=None)
    channel._engagement_registry = reg
    channel._engagement_driver = driver

    async def rebuild(rec):
        await driver.invalidate_session(rec)
        await driver.open_fresh(rec)
        return "[Context reset]"

    async def to_topic(topic_id, text, *a, **k):
        notices.append(text)

    async def cleanup(rec):
        notices.append("cleanup")

    channel._engagement_context_rebuilder = rebuild
    channel.send_to_topic = to_topic
    channel._cleanup_error_topic = cleanup
    ok = await channel._resume_and_ready(reg.get(eid))
    return ok, driver.calls, notices, reg.get(eid)


@pytest.mark.parametrize("wiped", [True, False])
async def test_an_open_in_casa_engagement_continues_fresh_after_the_wipe(
        tmp_path, wiped):
    """The CLI cannot resume a session whose transcript the wipe deleted
    (measured: "No conversation found", exit 1). The step marks every open
    in_casa engagement for a context rebuild, so its next turn opens a fresh
    session and the record stays open. Without the mark (the red case) the
    turn tries the resume and is refused."""
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    record = w.under(root, "/data/engagements.json")
    record.parent.mkdir(parents=True)
    await _record_file(record)
    w.write(w.under(root, f"/config/cc-home/.claude/projects/-cwd/{w.SID}.jsonl"),
            "turns")
    if wiped:
        assert maintenance.write_wipe_intent(str(root)) == "written"
        assert w.run_oneshot(root, proc).stdout.strip() == "done"
    else:
        (w.under(root, f"/config/cc-home/.claude/projects/-cwd/{w.SID}.jsonl")
         ).unlink()                       # the transcript is gone either way
    ok, calls, notices, rec = await _next_turn(record, "e1" * 16)
    if wiped:
        assert (ok, calls, notices) == (True, ["invalidate", "open_fresh"], [])
        assert (rec.status, rec.sdk_session_id, rec.context_rebuild_pending) == (
            "active", None, False)
        rows = {r["id"]: r for r in json.loads(record.read_text())}
        assert rows["e2" * 16]["context_rebuild_pending"] is False
        assert rows["e3" * 16]["context_rebuild_pending"] is False
    else:
        assert (ok, calls) == (False, ["resume"])
        assert notices[0].startswith("Could not resume this engagement")


async def test_an_open_engagement_with_no_session_yet_also_continues(tmp_path):
    """r4 Astra S2: a record persisted before its first session id. It is
    marked like the others, so its next turn opens a fresh session rather
    than being refused as an orphan."""
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    record = w.under(root, "/data/engagements.json")
    record.parent.mkdir(parents=True)
    await _record_file(record)
    assert maintenance.write_wipe_intent(str(root)) == "written"
    assert w.run_oneshot(root, proc).stdout.strip() == "done"
    ok, calls, notices, rec = await _next_turn(record, "e4" * 16)
    assert (ok, calls, notices) == (True, ["invalidate", "open_fresh"], [])
    assert rec.status == "idle" or rec.status == "active"


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
