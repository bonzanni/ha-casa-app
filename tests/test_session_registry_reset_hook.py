"""AR-4: /new must flush (close) the warm client BEFORE the reset's retain
reads the transcript from disk."""
from __future__ import annotations

import pytest

from session_registry import SessionRegistry
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV

pytestmark = pytest.mark.asyncio


async def test_notify_reset_calls_listener_and_unsubscribes(tmp_path):
    reg = SessionRegistry(str(tmp_path / "sessions.json"))
    calls = []

    async def listener(key):
        calls.append(key)

    unsub = reg.add_reset_listener(listener)
    await reg.notify_reset("telegram-1")
    assert calls == ["telegram-1"]
    unsub()
    await reg.notify_reset("telegram-1")
    assert calls == ["telegram-1"]


async def test_notify_reset_survives_listener_error(tmp_path):
    reg = SessionRegistry(str(tmp_path / "sessions.json"))
    async def bad(key): raise RuntimeError("boom")
    seen = []
    async def good(key): seen.append(key)
    reg.add_reset_listener(bad)
    reg.add_reset_listener(good)
    await reg.notify_reset("k")            # must not raise
    assert seen == ["k"]


async def test_reset_channel_notifies_before_the_retain(tmp_path, monkeypatch):
    """#878 retargeted the seam from save_session to retain_cold_session; the
    ordering this test exists for — flush-close BEFORE the transcript read — is
    unchanged, and so is the trailing pointer removal."""
    import session_saver
    reg = SessionRegistry(str(tmp_path / "sessions.json"))
    await reg.register("telegram-1", "assistant", "sid-1", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    order = []

    async def listener(key): order.append(f"reset:{key}")
    reg.add_reset_listener(listener)

    async def fake_retain(old, **kw):
        order.append("retain")
    monkeypatch.setattr(session_saver, "retain_cold_session", fake_retain)

    await session_saver.reset_channel(
        "telegram-1", reg, object(), channel="telegram",
    )
    assert order == ["reset:telegram-1", "retain"]
    assert reg.get("telegram-1") is None
