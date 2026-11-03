"""#290 — reset_channel under the retirement claim: snapshot-first ordering,
the bounded re-derive loop for the sid-less arm, and claim hygiene on every
exit path."""

import pytest

from session_registry import SessionRegistry, reset_channel
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _reg(tmp_path) -> SessionRegistry:
    return SessionRegistry(str(tmp_path / "s.json"))


async def _register(reg, key, sid):
    await reg.register(
        key, "assistant", sid, binding_digest=STUB_BINDING_DIGEST,
        speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )


async def test_claim_is_live_during_notify_reset(tmp_path):
    """The steer window opens BEFORE the pool close: a listener (the pool's
    close hook) must already observe the retirement claim."""
    reg = _reg(tmp_path)
    await _register(reg, "telegram-42", "sid-old")
    observed: list[bool] = []

    async def listener(key):
        observed.append(reg.retirement_pending(key))

    reg.add_reset_listener(listener)
    await reset_channel("telegram-42", reg)
    assert observed == [True]
    assert not reg.retirement_pending("telegram-42")   # released on exit
    assert reg.get("telegram-42") is None


async def test_racing_fresh_registration_survives_reset(tmp_path):
    """Design r1 red case (Terra T-A1): a steered-fresh turn registers sid-new
    while the reset's pool close is in flight. Snapshotting AFTER the close
    removed the FRESH session; snapshot-first with the sid guard must leave it
    standing."""
    reg = _reg(tmp_path)
    await _register(reg, "telegram-42", "sid-old")

    async def racing_listener(key):
        await _register(reg, key, "sid-new")   # steered-fresh racing turn

    reg.add_reset_listener(racing_listener)
    await reset_channel("telegram-42", reg)

    entry = reg.get("telegram-42")
    assert entry is not None and entry["sdk_session_id"] == "sid-new"
    assert not reg.retirement_pending("telegram-42")


async def test_sidless_entry_rederives_and_retires_materialized_session(
    tmp_path,
):
    """Design r2 red case (Sol S-A2): the entry is sid-less (a resume-failure
    clear), an in-flight PRE-reset turn publishes sid-x during the pool close.
    An early return left sid-x resumable; the re-derive loop must claim and
    retire it."""
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
    await reset_channel("telegram-42", reg)

    assert published == ["sid-x"]
    assert reg.get("telegram-42") is None        # the materialized pointer dropped
    assert not reg.retirement_pending("telegram-42")


async def test_no_entry_no_claim(tmp_path):
    reg = _reg(tmp_path)
    seen: list[str] = []

    async def listener(key):
        seen.append(key)

    reg.add_reset_listener(listener)
    await reset_channel("telegram-99", reg)
    assert seen == ["telegram-99"]               # the pool close still runs
    assert not reg.retirement_pending("telegram-99")


async def test_claim_released_when_the_body_raises(tmp_path, monkeypatch):
    """No exit path may leave the key steering fresh forever: a body step
    that raises inside the reset (the pointer removal) must release the claim
    in the ``finally``."""
    reg = _reg(tmp_path)
    await _register(reg, "telegram-42", "sid-two")

    async def exploding_remove(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(reg, "remove", exploding_remove)
    with pytest.raises(RuntimeError):
        await reset_channel("telegram-42", reg)
    assert not reg.retirement_pending("telegram-42")


async def test_inflight_old_turn_republish_refused_then_reset_completes(
    tmp_path,
):
    """A causally-before turn that resumed sid-old republishes it during the
    pool close: register() refuses (the claim names sid-old as dying), and
    the reset drops sid-old."""
    reg = _reg(tmp_path)
    await _register(reg, "telegram-42", "sid-old")

    async def republishing_listener(key):
        await _register(reg, key, "sid-old")   # refused mid-retirement

    reg.add_reset_listener(republishing_listener)
    await reset_channel("telegram-42", reg)

    assert reg.get("telegram-42") is None
    assert not reg.retirement_pending("telegram-42")


async def test_a_reset_tells_the_memory_writer_the_session_ended(tmp_path,
                                                                 monkeypatch):
    """The end of a session is one of the writer's cadence points: the reset
    signals it once, and does not wait for it."""
    from memory import writer as memory_writer

    ended: list[None] = []
    monkeypatch.setattr(memory_writer, "session_ended",
                        lambda: ended.append(None))
    reg = _reg(tmp_path)
    await _register(reg, "telegram-42", "sid-old")
    await reset_channel("telegram-42", reg)
    assert len(ended) == 1
    assert reg.get("telegram-42") is None
