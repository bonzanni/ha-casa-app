"""AR-4: /new closes the warm client before it drops the session pointer."""
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


async def test_reset_channel_notifies_before_the_pointer_drops(tmp_path):
    """The pool close runs while the pointer still names the session, and the
    pointer is gone once the reset returns."""
    from session_registry import reset_channel

    reg = SessionRegistry(str(tmp_path / "sessions.json"))
    await reg.register("telegram-1", "assistant", "sid-1", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    seen = []

    async def listener(key):
        seen.append((key, (reg.get(key) or {}).get("sdk_session_id")))
    reg.add_reset_listener(listener)

    await reset_channel("telegram-1", reg)
    assert seen == [("telegram-1", "sid-1")]
    assert reg.get("telegram-1") is None
