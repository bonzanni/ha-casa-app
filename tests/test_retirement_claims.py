"""#290/#411 — SessionRegistry retirement claims: the per-key claim SET that
steers racing turns to fresh sessions and refuses re-registration of a dying
sid while a reset/wipe runs."""

import pytest

from session_registry import SessionRegistry
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _reg(tmp_path) -> SessionRegistry:
    return SessionRegistry(str(tmp_path / "sessions.json"))


async def _register(reg, key, sid):
    await reg.register(
        key, "resident:assistant", sid, binding_digest=STUB_BINDING_DIGEST,
        speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )


class TestClaimSet:
    async def test_pending_while_any_claim_live(self, tmp_path):
        reg = _reg(tmp_path)
        assert not reg.retirement_pending("k")
        t1 = reg.begin_retirement("k", "sid-a")
        assert reg.retirement_pending("k")
        t2 = reg.begin_retirement("k", "sid-a")  # overlapping owner (wipe over /new)
        reg.end_retirement("k", t2)
        # Design r2 (Sol/Terra convergent): the earlier owner's protection
        # survives the later owner ending — a single lossy slot cleared it.
        assert reg.retirement_pending("k")
        reg.end_retirement("k", t1)
        assert not reg.retirement_pending("k")

    async def test_end_with_foreign_token_is_noop(self, tmp_path):
        reg = _reg(tmp_path)
        t1 = reg.begin_retirement("k", "sid-a")
        reg.end_retirement("k", object())   # stale/foreign token
        assert reg.retirement_pending("k")
        reg.end_retirement("k", t1)
        assert not reg.retirement_pending("k")

    async def test_claims_are_per_key(self, tmp_path):
        reg = _reg(tmp_path)
        reg.begin_retirement("k1", "sid-a")
        assert not reg.retirement_pending("k2")

    async def test_end_is_idempotent(self, tmp_path):
        reg = _reg(tmp_path)
        t = reg.begin_retirement("k", "sid-a")
        reg.end_retirement("k", t)
        reg.end_retirement("k", t)   # no KeyError, no effect
        assert not reg.retirement_pending("k")


class TestRegisterRefusal:
    async def test_dying_sid_refused_other_sids_proceed(self, tmp_path):
        """The dying sid cannot re-arm the pointer mid-retirement; a
        steered-fresh turn's NEW sid must still register (it is the racing
        turn's live conversation)."""
        reg = _reg(tmp_path)
        await _register(reg, "k", "sid-old")
        token = reg.begin_retirement("k", "sid-old")
        await _register(reg, "k", "sid-old")   # in-flight old turn republish
        # refused: entry unchanged in generation terms — remove it and check
        await _register(reg, "k", "sid-new")   # steered-fresh turn
        assert reg.get("k")["sdk_session_id"] == "sid-new"
        reg.end_retirement("k", token)

    async def test_refusal_does_not_move_generation(self, tmp_path):
        """A refused registration is a no-op: the #526 generation must not
        move, or the retirement's guarded save/remove would decline for a
        registration that never landed."""
        reg = _reg(tmp_path)
        await _register(reg, "k", "sid-old")
        gen_before = reg.generation("k")
        token = reg.begin_retirement("k", "sid-old")
        await _register(reg, "k", "sid-old")
        assert reg.generation("k") == gen_before
        reg.end_retirement("k", token)

    async def test_dying_sid_registers_normally_after_end(self, tmp_path):
        reg = _reg(tmp_path)
        await _register(reg, "k", "sid-old")
        token = reg.begin_retirement("k", "sid-old")
        reg.end_retirement("k", token)
        await _register(reg, "k", "sid-old")
        assert reg.get("k")["sdk_session_id"] == "sid-old"

    async def test_none_sid_claim_refuses_nothing(self, tmp_path):
        """A claim on a sid-less entry steers turns but must not block any
        registration (there is no dying sid to protect against)."""
        reg = _reg(tmp_path)
        token = reg.begin_retirement("k", None)
        await _register(reg, "k", "sid-new")
        assert reg.get("k")["sdk_session_id"] == "sid-new"
        reg.end_retirement("k", token)
