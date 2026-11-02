"""Role-scoped collision-safe session keys + v2 migration + consumers (spec A2).

asyncio_mode is ``auto`` (pytest.ini), so async tests run without an explicit
marker; the module marker is therefore ONLY ``unit`` — applying
``pytest.mark.asyncio`` module-wide would tag the synchronous tests here and
emit spurious "marked async but not an async function" warnings.
"""
from __future__ import annotations
import copy, json, re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pytest
from session_reg_helpers import (
    RESIDENT_DIGEST,
    STUB_BINDING_DIGEST,
    STUB_SPEAKER_PROV,
    STUB_USER_PROV,
    resident_role_id,
)
pytestmark = [pytest.mark.unit]


class TestScopedKey:
    def test_channel_first_so_partition_yields_channel(self):
        from session_registry import build_scoped_session_key as k
        key = k("voice", "concierge", "dev-1")
        assert key.partition("-")[0] == "voice"          # consumers rely on this
        assert "-v2-" in key

    def test_distinct_roles_distinct_keys(self):
        from session_registry import build_scoped_session_key as k
        assert k("voice", "butler", "d") != k("voice", "concierge", "d")

    def test_hyphen_tuples_cannot_collide(self):
        from session_registry import build_scoped_session_key as k
        assert k("voice", "a", "b-c") != k("voice", "a-b", "c")

    def test_charset_safe_and_bounded(self):
        from session_registry import build_scoped_session_key as k
        key = k("voice", "butler", "device-abc")
        assert re.fullmatch(r"[A-Za-z0-9_-]+", key) and len(key) <= 100

    def test_none_scope_maps_to_default(self):
        from session_registry import build_scoped_session_key as k
        assert k("voice", "butler", None) == k("voice", "butler", "default")


class TestResumeRoleCheck:
    def _entry(self, agent=None):
        e = {
            "sdk_session_id": "s1",
            "last_active": datetime.now(timezone.utc).isoformat(),
            "binding_digest": RESIDENT_DIGEST,
        }
        if agent is not None:
            e["agent"] = agent
        return e

    def test_mismatched_agent_starts_new(self):
        from agent import _resume_decision
        d = _resume_decision(
            "voice", self._entry(resident_role_id("butler")),
            datetime.now(timezone.utc),
            role_id=resident_role_id("concierge"), binding_digest=RESIDENT_DIGEST,
        )
        assert d.action == "new"

    def test_matching_agent_resumes(self):
        from agent import _resume_decision
        d = _resume_decision(
            "voice", self._entry(resident_role_id("butler")),
            datetime.now(timezone.utc),
            role_id=resident_role_id("butler"), binding_digest=RESIDENT_DIGEST,
        )
        assert d.action == "resume"

    def test_legacy_entry_without_agent_still_starts_new_when_role_given(self):
        # Strict: migration drops agent-less voice entries, so any agent-less
        # entry seen with a role is treated as non-matching (missing snapshot).
        from agent import _resume_decision
        d = _resume_decision(
            "voice", self._entry(None), datetime.now(timezone.utc),
            role_id=resident_role_id("butler"), binding_digest=RESIDENT_DIGEST,
        )
        assert d.action == "new"


class TestResumeAuthorityRoleBound:
    """IMPORTANT-2 (review): prove the role gate is wired at the TWO real
    resume-authority seams, not merely in the pure helper. Chosen approach:
    exercise BOTH real call sites directly with a mismatched-agent registry
    entry (rather than two full alternating-Agent turns) — it is deterministic,
    light, and fails precisely if EITHER seam drops ``role``:

      (a) the ``decide=`` wrapper installed in ``Agent.__init__`` (the pooled
          path's resume authority), called via ``agent._pool._decide``;
      (b) the bypass-path ``_resume_decision(..., role=self.config.role)`` call
          inside ``Agent._process``, driven with the pool disabled.

    Both use a real ``Agent`` (real config.role, real pool wrapper). Uses the
    established cross-test-module import pattern (cf. test_authz_hook.py) for
    the FakeClient/_make_agent_with_registry harness.
    """

    def test_pool_decide_wrapper_binds_role(self, tmp_path):
        # Seam (a): a FRESH entry recorded under a DIFFERENT agent must NOT
        # resume — the wrapper passes role=butler, so _resume_decision's strict
        # gate returns "new". Omitting role would make a fresh entry "resume".
        from test_agent_process import _make_agent_with_registry
        from session_registry import SessionRegistry
        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        butler = _make_agent_with_registry(reg, role="butler")
        now = datetime.now(timezone.utc)
        fresh = {"agent": "concierge", "sdk_session_id": "cx",
                 "last_active": (now - timedelta(minutes=2)).isoformat()}
        assert butler._pool._decide("voice", fresh, now).action == "new"
        # Sanity: a role+binding-MATCHING fresh entry still resumes through the
        # wrapper (Task 9: the entry must carry butler's canonical role_id AND
        # its binding_digest, not the short slug).
        match = dict(
            fresh, agent=resident_role_id("butler"), binding_digest=RESIDENT_DIGEST,
        )
        assert butler._pool._decide("voice", match, now).action == "resume"

    async def test_bypass_path_binds_role(self, tmp_path, monkeypatch):
        # Seam (b): pool OFF → _process takes _attempt_bypass_turn. A fresh
        # entry at butler's OWN channel_key but under agent="concierge" must
        # start fresh (resume=None). Omitting role in the bypass call would
        # resume the concierge sid → captured_options.resume == "concierge-sid".
        from test_agent_process import FakeClient, _make_agent_with_registry, _msg
        from session_registry import SessionRegistry, build_scoped_session_key
        monkeypatch.setenv("SDK_CLIENT_POOL", "off")
        FakeClient.reset()
        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        key = build_scoped_session_key("voice", "butler", "shared-dev")
        reg._data[key] = {
            "agent": "concierge", "sdk_session_id": "concierge-sid",
            "last_active": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
        }
        butler = _make_agent_with_registry(reg, role="butler")
        with patch("sdk_client_pool._default_make_client", FakeClient):
            await butler._process(_msg("voice", "shared-dev", "hi"))
        assert FakeClient.captured_options.resume is None, (
            "bypass path resumed a different agent's session — role not bound"
        )
        # The turn re-registered the key under butler's own canonical identity.
        assert reg.get(key)["agent"] == resident_role_id("butler")


class TestBootWebhookPurge:
    async def test_purge_is_idempotent_and_leaves_v2_data_byte_identical(self, tmp_path):
        # Idempotence proven against POPULATED v2 data, not an empty
        # registry — a re-run must return zero AND leave every non-webhook
        # entry byte-identical.
        from session_registry import SessionRegistry, build_scoped_session_key
        path = tmp_path / "sessions.json"; path.write_text("{}")
        reg = SessionRegistry(str(path))
        v2key = build_scoped_session_key("telegram", "assistant", "999")
        reg._data[v2key] = {
            "agent": "assistant", "sdk_session_id": "x",
            "last_active": "2026-07-14T00:00:00+00:00",
        }
        before = copy.deepcopy(reg._data)
        assert reg.purge_webhook_sessions() == 0
        assert reg.purge_webhook_sessions() == 0
        assert reg._data == before
        assert list(reg._data) == [v2key]        # key unchanged, not re-hashed

    async def test_purges_all_webhook_entries(self, tmp_path):
        # Release A / Layer 4: persisted webhook session entries (any key
        # shape) are PURGED at boot — their origin route (invoke vs
        # webhook_trigger) is unknowable, so they must never be resumed or
        # treated as trusted. Non-webhook entries are untouched.
        from session_registry import SessionRegistry, build_scoped_session_key
        path = tmp_path / "sessions.json"; path.write_text("{}")
        reg = SessionRegistry(str(path))
        v2_webhook = build_scoped_session_key("webhook", "assistant", "some-uuid")
        reg._data = {
            "webhook-stray": {"agent": "assistant", "sdk_session_id": "a",
                              "last_active": "2026-07-14T00:00:00+00:00"},
            v2_webhook: {"agent": "assistant", "sdk_session_id": "b",
                         "last_active": "2026-07-14T00:00:00+00:00"},
            "telegram-999": {"agent": "assistant", "sdk_session_id": "c",
                             "last_active": "2026-07-14T00:00:00+00:00"},
        }
        assert reg.purge_webhook_sessions() == 2
        assert not any(k.startswith("webhook-") for k in reg.all_entries())
        assert reg._data["telegram-999"]["sdk_session_id"] == "c"

    async def test_boot_wires_the_purge_and_persists_it(self, tmp_path):
        """The purge is only worth anything if boot actually runs it and the
        result reaches DISK — an in-memory purge resurrects on the next read
        of sessions.json. Pins both halves: casa_core's boot sequence names
        purge_webhook_sessions + save, and the purge+save pair leaves the
        reopened file free of webhook rows. Red case demonstrated: deleting
        the purge call from casa_core.main (or its save) fails this test."""
        import inspect
        import json as _json

        import casa_core
        from session_registry import SessionRegistry

        # (a) Wiring: the boot path invokes the purge and saves on change.
        src = inspect.getsource(casa_core.main)
        assert "purge_webhook_sessions()" in src
        purge_at = src.index("purge_webhook_sessions()")
        assert "await session_registry.save()" in src[purge_at:], (
            "boot purge is not followed by a durable save")

        # (b) Durability: purge + save leaves the reopened file clean.
        path = tmp_path / "sessions.json"
        path.write_text(_json.dumps({
            "webhook-stray": {"agent": "assistant", "sdk_session_id": "a",
                              "last_active": "2026-07-14T00:00:00+00:00"},
            "telegram-999": {"agent": "assistant", "sdk_session_id": "c",
                             "last_active": "2026-07-14T00:00:00+00:00"},
        }))
        reg = SessionRegistry(str(path))
        assert reg.purge_webhook_sessions() == 1
        await reg.save()
        reopened = SessionRegistry(str(path))
        assert not any(k.startswith("webhook-")
                       for k in reopened.all_entries())
        assert reopened.get("telegram-999")["sdk_session_id"] == "c"


class TestVoicePoolRoleKeyed:
    def test_two_roles_one_scope_distinct_sessions(self):
        # Pool-KEYING isolation only (VoiceSessionPool is not where resume is
        # decided — that authority is covered by TestResumeAuthorityRoleBound).
        from channels.voice.session import VoiceSessionPool
        pool = VoiceSessionPool(idle_timeout=300)
        a = pool.ensure("dev", role="butler")
        b = pool.ensure("dev", role="concierge")
        assert a.session_key != b.session_key
        assert pool.get("dev", role="butler") is a


class TestWebhookOneshotScopeClassSurvivesV2:
    """Sweeper contract (brief §Step2 note): a v2 webhook one-shot key's hashed
    remainder is never uuid-shaped, so the sweeper must read the persisted
    ``scope_class`` marker rather than re-deriving it from the key."""

    async def test_v2_webhook_oneshot_still_gets_webhook_ttl(self, tmp_path):
        from session_registry import SessionRegistry, build_scoped_session_key
        from session_sweeper import SessionSweeper

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        uuid_scope = "550e8400-e29b-41d4-a716-446655440000"
        key = build_scoped_session_key("webhook", "assistant", uuid_scope)
        await reg.register(key, "assistant", "sid-1", scope_class="webhook_oneshot", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)

        assert reg.get(key)["scope_class"] == "webhook_oneshot"
        # A one-shot naming a transcript is kept for the transcript's
        # retention (INV-MEM-031), not just the short webhook TTL; past it,
        # it goes. The short TTL itself is pinned in test_session_sweeper.
        sweeper = SessionSweeper(
            registry=reg, session_ttl_days=30, webhook_session_ttl_days=1,
        )
        reg._data[key]["last_active"] = (
            datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        await sweeper._sweep_once()
        assert reg.get(key) is not None
        reg._data[key]["last_active"] = (
            datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        await sweeper._sweep_once()
        assert reg.get(key) is None, "v2 webhook one-shot must be evicted once retention passed"

    async def test_stored_webhook_entry_is_purged_at_boot(self, tmp_path):
        # Release A / Layer 4: ANY persisted webhook entry is DROPPED at
        # boot, because its origin route is unknowable. NEW webhook
        # one-shots still get scope_class at register() time and the short
        # webhook TTL — see test_v2_webhook_oneshot_still_gets_webhook_ttl.
        from session_registry import SessionRegistry, build_scoped_session_key

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        uuid_scope = "550e8400-e29b-41d4-a716-446655440000"
        aged = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        reg._data[f"webhook-{uuid_scope}"] = {
            "agent": "assistant", "sdk_session_id": "sid-1", "last_active": aged,
        }
        assert reg.purge_webhook_sessions() == 1
        v2key = build_scoped_session_key("webhook", "assistant", uuid_scope)
        assert reg.get(v2key) is None
        assert not any(k.startswith("webhook-") for k in reg.all_entries())
