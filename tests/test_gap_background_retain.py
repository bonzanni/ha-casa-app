# tests/test_gap_background_retain.py
"""Gap-triggered cold-session retain is claim-free, backgroundable, voice-safe."""
import inspect
import pytest

import session_saver
import agent as agent_mod

pytestmark = [pytest.mark.unit]


class _Msg:
    def __init__(self, mtype, text):
        self.type = mtype
        self.message = {"role": mtype, "content": text}


class _Sem:
    def __init__(self):
        self.retained = []

    async def document_tags(self, bank, document_id):
        return None  # #1123: reads as never saved

    async def retain(self, bank, items, *, async_=True):
        self.retained.append((bank, [i["tags"] for i in items]))


async def fake_classify(c):
    return "friends"


def _snapshot(*, with_provenance: bool):
    """Build a SessionEntrySnapshot the reduced retain_cold_session consumes."""
    from agent import snapshot_session_entry
    from speaker_provenance import provenance_mapping
    from session_reg_helpers import STUB_SPEAKER_PROV, STUB_USER_PROV
    entry = {"agent": "resident:assistant", "sdk_session_id": "s1"}
    if with_provenance:
        entry["speaker_provenance"] = provenance_mapping(STUB_SPEAKER_PROV)
        entry["user_provenance"] = provenance_mapping(STUB_USER_PROV)
    return snapshot_session_entry(entry)


async def test_retain_cold_session_telegram(monkeypatch):
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    monkeypatch.setattr(session_saver, "get_session_messages",
                        lambda sid, directory: [_Msg("user", "hi")])
    sem = _Sem()
    await session_saver.retain_cold_session(
        _snapshot(with_provenance=True), directory="/tmp",
        channel="telegram", semantic_memory=sem,
    )
    # One retain to the shared bank; the single user turn is tier-tagged "friends".
    assert len(sem.retained) == 1
    bank, tagsets = sem.retained[0]
    assert bank == "casa"
    assert tagsets[0][0] == "friends"                       # tier tag first
    assert any(t.startswith("casa-source-") for t in tagsets[0])  # + provenance tag


async def test_retain_cold_session_voice_noop():
    sem = _Sem()
    await session_saver.retain_cold_session(
        _snapshot(with_provenance=False), directory="/tmp",
        channel="voice", semantic_memory=sem,
    )
    assert sem.retained == []  # recall-only channel never retains


async def test_retain_cold_session_no_provenance_noop(monkeypatch):
    """A legacy/corrupt snapshot with no usable provenance retains NOTHING —
    memory is never written with invented authorship."""
    monkeypatch.setattr(session_saver, "get_session_messages",
                        lambda sid, directory: [_Msg("user", "hi")])
    sem = _Sem()
    await session_saver.retain_cold_session(
        _snapshot(with_provenance=False), directory="/tmp",
        channel="telegram", semantic_memory=sem,
    )
    assert sem.retained == []


class _FailingSem:
    def __init__(self):
        self.calls = 0

    async def document_tags(self, bank, document_id):
        return None  # #1123: reads as never saved

    async def retain(self, bank, items, *, async_=True):
        self.calls += 1
        raise RuntimeError("hindsight down")


async def test_failed_cold_retain_spools_a_durable_retry_record(tmp_path, monkeypatch):
    """#345: the next-turn-after-gap path retains OUTSIDE the registry, so once
    the new turn overwrites the entry there is no in-registry retry — a
    transient Hindsight outage lost the old transcript for good. A failed
    retain must leave a durable retry record."""
    import json
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    monkeypatch.setattr(session_saver, "get_session_messages",
                        lambda sid, directory: [_Msg("user", "hi")])
    await session_saver.retain_cold_session(
        _snapshot(with_provenance=True), directory="/tmp",
        channel="telegram", semantic_memory=_FailingSem(), retry_dir=tmp_path,
    )
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record["sdk_session_id"] == "s1"
    assert record["directory"] == "/tmp"
    assert record["channel"] == "telegram"
    assert record["attempts"] == 0
    assert record["speaker_provenance"] and record["user_provenance"]


async def test_retry_spooled_cold_retains_retains_and_unlinks(tmp_path, monkeypatch):
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    monkeypatch.setattr(session_saver, "get_session_messages",
                        lambda sid, directory: [_Msg("user", "hi")])
    await session_saver.retain_cold_session(
        _snapshot(with_provenance=True), directory="/tmp",
        channel="telegram", semantic_memory=_FailingSem(), retry_dir=tmp_path,
    )
    assert len(list(tmp_path.glob("*.json"))) == 1

    sem = _Sem()  # Hindsight is back
    await session_saver.retry_spooled_cold_retains(sem, retry_dir=tmp_path)
    assert len(sem.retained) == 1
    assert sem.retained[0][0] == "casa"
    assert list(tmp_path.glob("*.json")) == []  # settled → record removed


async def test_retry_spooled_increments_attempts_and_gives_up_at_the_cap(
    tmp_path, monkeypatch, caplog,
):
    import json
    import logging
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    monkeypatch.setattr(session_saver, "get_session_messages",
                        lambda sid, directory: [_Msg("user", "hi")])
    await session_saver.retain_cold_session(
        _snapshot(with_provenance=True), directory="/tmp",
        channel="telegram", semantic_memory=_FailingSem(), retry_dir=tmp_path,
    )
    (record_path,) = tmp_path.glob("*.json")

    failing = _FailingSem()
    await session_saver.retry_spooled_cold_retains(failing, retry_dir=tmp_path)
    assert json.loads(record_path.read_text())["attempts"] == 1  # still spooled

    # Poison record (e.g. transcript reaped): give up at the cap, loudly.
    record = json.loads(record_path.read_text())
    record["attempts"] = session_saver._COLD_RETAIN_MAX_ATTEMPTS - 1
    record_path.write_text(json.dumps(record))
    with caplog.at_level(logging.ERROR):
        await session_saver.retry_spooled_cold_retains(failing, retry_dir=tmp_path)
    assert list(tmp_path.glob("*.json")) == []
    assert any("giving up" in r.getMessage() for r in caplog.records)


async def test_unreadable_retry_record_is_dropped_not_looped(tmp_path, caplog):
    import logging
    (tmp_path / "garbage.json").write_text("{not json")
    sem = _Sem()
    with caplog.at_level(logging.ERROR):
        await session_saver.retry_spooled_cold_retains(sem, retry_dir=tmp_path)
    assert list(tmp_path.glob("*.json")) == []
    assert sem.retained == []


async def test_voice_and_provenance_less_cold_retains_never_spool(tmp_path, monkeypatch):
    monkeypatch.setattr(session_saver, "get_session_messages",
                        lambda sid, directory: [_Msg("user", "hi")])
    await session_saver.retain_cold_session(
        _snapshot(with_provenance=True), directory="/tmp",
        channel="voice", semantic_memory=_FailingSem(), retry_dir=tmp_path,
    )
    await session_saver.retain_cold_session(
        _snapshot(with_provenance=False), directory="/tmp",
        channel="telegram", semantic_memory=_FailingSem(), retry_dir=tmp_path,
    )
    assert list(tmp_path.glob("*.json")) == []


def test_gap_branch_does_not_await_save_session():
    src = inspect.getsource(agent_mod.Agent._process)
    assert "await save_session(" not in src        # gap save is no longer inline/blocking
    assert "_spawn_cold_retain(" in src            # it schedules a background retain


def test_agent_has_background_tasks_set():
    # __init__ initialises the tracking set
    src = inspect.getsource(agent_mod.Agent.__init__)
    assert "_bg_tasks" in src


async def test_cancelled_cold_retain_still_spools(tmp_path, monkeypatch):
    """Terra r1 (#345): shutdown cancels these unawaited background tasks —
    CancelledError bypassed the spool write, losing exactly the
    registry-decoupled transcript the spool exists to protect. The spool write
    is synchronous, so it can run in the cancel path before re-raising."""
    import asyncio
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    monkeypatch.setattr(session_saver, "get_session_messages",
                        lambda sid, directory: [_Msg("user", "hi")])
    started = asyncio.Event()

    class _Hanging:
        async def document_tags(self, bank, document_id):
            return None  # #1123: reads as never saved

        async def retain(self, *a, **k):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(session_saver.retain_cold_session(
        _snapshot(with_provenance=True), directory="/tmp",
        channel="telegram", semantic_memory=_Hanging(), retry_dir=tmp_path,
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(list(tmp_path.glob("*.json"))) == 1
