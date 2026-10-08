"""#1352 — after ``/new`` the old conversation is saved in the background.

The Telegram ``/new`` arm awaits ``reset_channel`` inside the per-chat serial
lock, and the reset holds turn admission and the key's write gate. Before
#1352 the reset's body ended with the retain itself (tier classification plus
the memory write), so the operator's next message waited 18-62 s behind it.
The reset now writes the conversation's retry record first, drops the pointer,
and leaves the retain to a background task that removes the record when it
lands. These tests drive the REAL reset, a REAL on-disk ``SessionRegistry``,
the REAL SDK transcript reader over a real ``.jsonl`` and the REAL spool and
reaper; only tier classification and the memory backend are substituted.
"""
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

import session_saver
from session_saver import reset_channel
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

_SID = "550e8400-e29b-41d4-a716-446655440000"
_KEY = "telegram-42"


def _write_transcript(project_dir, sid):
    from claude_agent_sdk._internal.sessions import _get_project_dir

    d = _get_project_dir(str(project_dir))
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "uuid": "u1", "parentUuid": None,
         "message": {"role": "user", "content": "Remember the blue box."}},
        {"type": "assistant", "uuid": "u2", "parentUuid": "u1",
         "message": {"role": "assistant",
                     "content": [{"type": "text",
                                  "text": "The blue box is upstairs."}]}},
    ]
    (d / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def _records(retry_dir) -> list[dict]:
    root = Path(retry_dir)
    if not root.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(root.glob("*.json"))]


class _Memory:
    """Records every retain and bank deletion, in order."""

    def __init__(self, *, failing: bool = False) -> None:
        self.failing = failing
        self.retains: list[tuple] = []
        self.events: list[str] = []

    async def document_tags(self, bank, document_id):
        return None  # #1123: reads as never saved

    async def retain(self, bank, items, *, async_=False):
        self.retains.append((bank, items))
        self.events.append("retain")
        if self.failing:
            raise RuntimeError("hindsight down")

    async def delete_bank(self, bank):
        self.events.append("delete_bank")
        return True


async def _settle() -> None:
    """Wait for every background reset retain this test started."""
    pending = list(getattr(session_saver, "_RESET_RETAINS", ()))
    if pending:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=5)


@pytest.fixture
def env(tmp_path, monkeypatch):
    import agent as _agent
    from claude_agent_sdk import get_session_messages
    from session_registry import SessionRegistry

    project_dir = tmp_path / "agent-home" / "assistant"
    project_dir.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setattr(
        _agent, "agent_home_for_role_id", lambda role: str(project_dir))
    classify_gate = asyncio.Event()
    classify_gate.set()

    async def fake_classify(content: str) -> str:
        await classify_gate.wait()
        return "private"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    retry_dir = tmp_path / "cold-retain-retry"
    monkeypatch.setattr(session_saver, "_COLD_RETAIN_RETRY_DIR", str(retry_dir))
    _write_transcript(project_dir, _SID)
    assert len(get_session_messages(_SID, str(project_dir))) == 2
    registry_path = tmp_path / "sessions.json"
    return types.SimpleNamespace(
        reg=SessionRegistry(str(registry_path)), registry_path=registry_path,
        project_dir=project_dir, retry_dir=retry_dir,
        classify_gate=classify_gate,
    )


async def _register(reg, key=_KEY, sid=_SID):
    await reg.register(
        key, "assistant", sid, binding_digest=STUB_BINDING_DIGEST,
        speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )


async def test_the_next_message_is_dispatched_while_the_save_still_runs(env):
    """The defect: a follow-up after /new waited for the whole save. With the
    classification held open, /new must still return and the follow-up must
    reach the bus; the save then completes on its own."""
    from bus import MessageBus
    from channels.telegram import TelegramChannel

    from session_registry import build_scoped_session_key

    key = build_scoped_session_key("telegram", "assistant", "42")
    await _register(env.reg, key=key)
    sem = _Memory()
    bus = MessageBus()

    async def _noop(_msg):
        return None
    bus.register("assistant", _noop)
    ch = TelegramChannel(bot_token="T", chat_id="0", default_agent="assistant",
                         bus=bus)
    ch._start_typing = lambda *a, **k: None
    sent: list[dict] = []

    async def send_message(**kw):
        sent.append(kw)
        return types.SimpleNamespace(message_id=1)
    ch._app = types.SimpleNamespace(
        bot=types.SimpleNamespace(send_message=send_message))
    ch._session_registry = env.reg
    ch._semantic_memory = sem

    def _update(text):
        return types.SimpleNamespace(
            message=types.SimpleNamespace(text=text, message_id=7),
            effective_chat=types.SimpleNamespace(id="42"),
            effective_user=types.SimpleNamespace(first_name="Nicola", id=1),
        )

    env.classify_gate.clear()          # the save is now "slow"
    try:
        await asyncio.wait_for(ch._handle(_update("/new"), None), timeout=2)
        await asyncio.wait_for(ch._handle(_update("hello"), None), timeout=2)
        q = bus.queues["assistant"]
        queued = []
        while not q.empty():
            queued.append(q.get_nowait()[2].content)
        assert queued == ["hello"]
        assert sem.retains == []       # the save really was still running
        assert env.reg.get(key) is None
    finally:
        env.classify_gate.set()
    await _settle()
    assert len(sem.retains) == 1
    assert _records(env.retry_dir) == []


async def test_the_record_is_on_disk_before_the_pointer_is_dropped(env):
    await _register(env.reg)
    seen_at_remove: list[list[str]] = []
    real_remove = env.reg.remove

    async def observing_remove(key, **kw):
        seen_at_remove.append(
            [r["sdk_session_id"] for r in _records(env.retry_dir)])
        return await real_remove(key, **kw)

    env.reg.remove = observing_remove
    await asyncio.wait_for(reset_channel(_KEY, env.reg, _Memory(), channel="telegram"), timeout=2)
    assert seen_at_remove == [[_SID]]
    await _settle()


async def test_a_restart_before_the_background_save_still_banks_it(env):
    """Casa stops right after /new returned: the background retain never ran.
    The record survives, and the reaper's boot sweep — against a registry
    reloaded from disk — banks the conversation and consumes the record."""
    from freshness_reaper import FreshnessReaper
    from session_registry import SessionRegistry

    await _register(env.reg)
    sem = _Memory()
    await asyncio.wait_for(reset_channel(_KEY, env.reg, sem, channel="telegram"), timeout=2)
    pending = list(getattr(session_saver, "_RESET_RETAINS", ()))
    assert len(pending) == 1
    for task in pending:
        task.cancel()                   # killed before it ever started
    await asyncio.gather(*pending, return_exceptions=True)
    assert sem.retains == []
    assert [r["sdk_session_id"] for r in _records(env.retry_dir)] == [_SID]

    reaper = FreshnessReaper(
        registry=SessionRegistry(str(env.registry_path)), semantic_memory=sem,
        directory_for=lambda role: str(env.project_dir),
    )
    await reaper.sweep_once()
    assert len(sem.retains) == 1
    assert [i["content"] for i in sem.retains[0][1]] == [
        "Remember the blue box.", "The blue box is upstairs."]
    assert _records(env.retry_dir) == []


async def test_a_landed_save_removes_its_record_and_a_failed_one_keeps_it(env):
    await _register(env.reg)
    ok = _Memory()
    await asyncio.wait_for(reset_channel(_KEY, env.reg, ok, channel="telegram"), timeout=2)
    await _settle()
    assert len(ok.retains) == 1
    assert _records(env.retry_dir) == []

    await _register(env.reg)
    down = _Memory(failing=True)
    await asyncio.wait_for(reset_channel(_KEY, env.reg, down, channel="telegram"), timeout=2)
    await _settle()
    assert len(down.retains) == 1
    records = _records(env.retry_dir)
    assert [(r["sdk_session_id"], r["attempts"]) for r in records] == [(_SID, 0)]


async def test_the_record_stays_until_the_retain_has_landed(env):
    """The record is the only handle on the conversation once the pointer is
    gone, so it must outlive every step before the memory write returns: held
    open inside the backend's retain, the record is still on disk."""
    await _register(env.reg)
    in_retain = asyncio.Event()
    release = asyncio.Event()

    class _SlowMemory(_Memory):
        async def retain(self, bank, items, *, async_=False):
            in_retain.set()
            await release.wait()
            await super().retain(bank, items, async_=async_)

    sem = _SlowMemory()
    await asyncio.wait_for(reset_channel(_KEY, env.reg, sem, channel="telegram"), timeout=2)
    await asyncio.wait_for(in_retain.wait(), timeout=2)
    assert [r["sdk_session_id"] for r in _records(env.retry_dir)] == [_SID]
    release.set()
    await _settle()
    assert len(sem.retains) == 1
    assert _records(env.retry_dir) == []


@pytest.mark.parametrize("channel_key, channel", [("voice-7", "voice")])
async def test_a_recall_only_channel_records_and_retains_nothing(
    env, channel_key, channel,
):
    await _register(env.reg, key=channel_key)
    sem = _Memory()
    await asyncio.wait_for(reset_channel(channel_key, env.reg, sem, channel=channel), timeout=2)
    assert not getattr(session_saver, "_RESET_RETAINS", ())
    assert sem.retains == []
    assert _records(env.retry_dir) == []
    assert env.reg.get(channel_key) is None


async def test_a_memory_clear_right_after_the_reset_leaves_nothing_behind(
    env, monkeypatch,
):
    """The clear starts the moment /new returns, before the background retain
    has run. Whatever the interleaving, nothing is written into the bank after
    it is deleted, and no record of the old conversation is left."""
    import memory_wipe as mw
    import session_gate

    monkeypatch.setattr(session_gate, "TURN_ADMISSION", session_gate.TurnAdmission())
    fence = mw.RetainFence()
    monkeypatch.setattr(mw, "FENCE", fence)
    monkeypatch.setattr(mw.mental_models, "schedule_reconcile",
                        lambda *a, **k: None)

    await _register(env.reg)
    sem = _Memory()
    await asyncio.wait_for(reset_channel(_KEY, env.reg, sem, channel="telegram"), timeout=2)
    report = await mw.wipe_long_term_memory(
        registry=env.reg, semantic_memory=sem, fence=fence, bank="casa",
        retry_dir=env.retry_dir,
    )
    await _settle()
    assert report.spool_records_dropped == 1
    assert "delete_bank" in sem.events
    assert "retain" not in sem.events[sem.events.index("delete_bank"):]
    assert _records(env.retry_dir) == []


async def test_a_memory_clear_during_the_background_save_waits_for_it(
    env, monkeypatch,
):
    """The other order: the background retain is inside the fence (held in
    classification) when the clear starts. The clear waits for it, then
    deletes the bank and the (already consumed) record."""
    import memory_wipe as mw
    import session_gate

    monkeypatch.setattr(session_gate, "TURN_ADMISSION", session_gate.TurnAdmission())
    fence = mw.RetainFence()
    monkeypatch.setattr(mw, "FENCE", fence)
    monkeypatch.setattr(mw.mental_models, "schedule_reconcile",
                        lambda *a, **k: None)

    await _register(env.reg)
    sem = _Memory()
    env.classify_gate.clear()
    await asyncio.wait_for(reset_channel(_KEY, env.reg, sem, channel="telegram"), timeout=2)
    for _ in range(20):
        await asyncio.sleep(0)          # let the retain enter the fence
    wipe = asyncio.create_task(mw.wipe_long_term_memory(
        registry=env.reg, semantic_memory=sem, fence=fence, bank="casa",
        retry_dir=env.retry_dir,
    ))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not wipe.done()
    env.classify_gate.set()
    await asyncio.wait_for(wipe, timeout=5)
    await _settle()
    assert sem.events == ["retain", "delete_bank"]
    assert _records(env.retry_dir) == []
