"""#290 — reset_channel under the retirement claim: snapshot-first ordering,
the bounded re-derive loop for the sid-less arm, and claim hygiene on every
exit path."""

from unittest.mock import AsyncMock, patch

import pytest

import session_saver
from session_saver import reset_channel
from session_registry import SessionRegistry
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _reg(tmp_path) -> SessionRegistry:
    return SessionRegistry(str(tmp_path / "s.json"))


async def _register(reg, key, sid):
    await reg.register(
        key, "assistant", sid, binding_digest=STUB_BINDING_DIGEST,
        speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )


_MSGS = [type("M", (), {"type": "user", "message": {"content": "hi"}})()]


async def test_claim_is_live_during_notify_reset(tmp_path, monkeypatch):
    """The steer window opens BEFORE the flush-close: a listener (the pool's
    close hook) must already observe the retirement claim."""
    reg = _reg(tmp_path)
    await _register(reg, "telegram-42", "sid-old")
    observed: list[bool] = []

    async def listener(key):
        observed.append(reg.retirement_pending(key))

    reg.add_reset_listener(listener)
    sem = AsyncMock()

    async def fake_classify(content: str) -> str:
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    with patch("session_saver.get_session_messages", return_value=_MSGS):
        await reset_channel("telegram-42", reg, sem, channel="telegram")
    assert observed == [True]
    assert not reg.retirement_pending("telegram-42")   # released on exit


async def test_racing_fresh_registration_survives_reset(tmp_path, monkeypatch):
    """Design r1 red case (Terra T-A1): a steered-fresh turn registers sid-new
    while the reset's flush-close is in flight. The v1 ordering snapshotted
    AFTER the close and retained+removed the FRESH session; snapshot-first
    with sid guards must leave it standing."""
    reg = _reg(tmp_path)
    await _register(reg, "telegram-42", "sid-old")

    async def racing_listener(key):
        await _register(reg, key, "sid-new")   # steered-fresh racing turn

    reg.add_reset_listener(racing_listener)
    sem = AsyncMock()
    captured = {}

    async def capturing_save(channel_key, registry, semantic_memory, **kwargs):
        captured.update(kwargs)
        return False

    monkeypatch.setattr(session_saver, "save_session", capturing_save)
    await reset_channel("telegram-42", reg, sem, channel="telegram")

    assert captured.get("expected_sid") == "sid-old"     # never sid-new
    entry = reg.get("telegram-42")
    assert entry is not None and entry["sdk_session_id"] == "sid-new"
    assert not reg.retirement_pending("telegram-42")


async def test_sidless_entry_rederives_and_retires_materialized_session(
    tmp_path, monkeypatch,
):
    """Design r2 red case (Sol S-A2): the entry is sid-less (a resume-failure
    clear), an in-flight PRE-reset turn publishes sid-x during the
    flush-close. The v2 early-return left sid-x resumable; the re-derive loop
    must claim and retire it (pre-batch parity)."""
    reg = _reg(tmp_path)
    await _register(reg, "telegram-42", "sid-x")
    await reg.clear_sdk_session("telegram-42")   # sid-less entry remains
    assert reg.get("telegram-42") is not None

    published: list[str] = []

    async def publishing_listener(key):
        # Fires on BOTH notify_reset passes; publish only once (the pre-reset
        # turn completing). The second pass holds a claim on sid-x, so a
        # repeat register of sid-x would be refused anyway.
        if not published:
            published.append("sid-x")
            await _register(reg, key, "sid-x")

    reg.add_reset_listener(publishing_listener)
    sem = AsyncMock()

    async def fake_classify(content: str) -> str:
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    with patch("session_saver.get_session_messages", return_value=_MSGS):
        await reset_channel("telegram-42", reg, sem, channel="telegram")

    sem.retain.assert_awaited_once()             # the materialized session was retained
    assert reg.get("telegram-42") is None        # …and its pointer dropped
    assert not reg.retirement_pending("telegram-42")


async def test_no_entry_no_claim_no_retain(tmp_path):
    reg = _reg(tmp_path)
    sem = AsyncMock()
    await reset_channel("telegram-99", reg, sem, channel="telegram")
    sem.retain.assert_not_awaited()
    assert not reg.retirement_pending("telegram-99")


async def test_claim_released_when_save_raises(tmp_path, monkeypatch):
    """No exit path may leave the key steering fresh forever."""
    reg = _reg(tmp_path)
    await _register(reg, "telegram-42", "sid-old")
    sem = AsyncMock()

    async def exploding_save(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(session_saver, "save_session", exploding_save)
    with pytest.raises(RuntimeError):
        await reset_channel("telegram-42", reg, sem, channel="telegram")
    assert not reg.retirement_pending("telegram-42")


async def test_inflight_old_turn_republish_refused_then_reset_completes(
    tmp_path, monkeypatch,
):
    """A causally-before turn that resumed sid-old republishes it during the
    flush-close: register() refuses (the claim names sid-old as dying), and
    the reset retains+drops sid-old exactly once."""
    reg = _reg(tmp_path)
    await _register(reg, "telegram-42", "sid-old")

    async def republishing_listener(key):
        await _register(reg, key, "sid-old")   # refused mid-retirement

    reg.add_reset_listener(republishing_listener)
    sem = AsyncMock()

    async def fake_classify(content: str) -> str:
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    with patch("session_saver.get_session_messages", return_value=_MSGS):
        await reset_channel("telegram-42", reg, sem, channel="telegram")

    sem.retain.assert_awaited_once()
    assert reg.get("telegram-42") is None


async def test_a_wipe_during_the_flush_close_makes_the_reset_discard(
    tmp_path, monkeypatch,
):
    """#578 (Sol design-r2 S1): the fence's capture-point contract, applied to
    the one caller that snapshots earlier than ``save_session``'s entry.

    ``reset_channel`` commits to its source data at its snapshot, then awaits
    ``notify_reset``. ``save_session`` used to capture the fence generation at
    its OWN entry — after that await — so a wipe completing in the window was
    invisible to it: the captured generation already matched the post-wipe
    value, ``StaleGeneration`` never fired, and the reset retained a pre-wipe
    transcript into the bank the wipe had just emptied, behind a report that
    said the wipe completed.

    Red case: bump the generation during the flush-close (what a completing
    wipe does at ``memory_wipe.py``'s exclusive section) and require the retain
    to be DISCARDED. Fails when ``save_session`` captures its own generation.
    """
    from memory_wipe import FENCE

    reg = _reg(tmp_path)
    await _register(reg, "telegram-42", "sid-old")

    async def wipe_completes_mid_close(key):
        # Exactly what _ExclusiveSection.__aenter__ does once writers drain.
        FENCE._generation += 1

    reg.add_reset_listener(wipe_completes_mid_close)
    sem = AsyncMock()

    async def fake_classify(content: str) -> str:
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    before = FENCE._generation
    try:
        with patch("session_saver.get_session_messages", return_value=_MSGS):
            await reset_channel("telegram-42", reg, sem, channel="telegram")
        sem.retain.assert_not_awaited()
    finally:
        FENCE._generation = before
