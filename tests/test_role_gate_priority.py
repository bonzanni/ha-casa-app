"""Tests for tools._effective_caller_role priority flip (E-6).

Pure contextvar manipulation — no SDK, no driver, no fixtures beyond
direct imports.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


def _make_engagement(role: str = "configurator"):
    """Minimal EngagementRecord-like stub. _effective_caller_role only
    reads .role_or_type, so any object with that attribute suffices."""
    from engagement_registry import EngagementRecord
    return EngagementRecord(
        id="test-eng-1",
        kind="executor",
        role_or_type=role,
        driver="in_casa",
        status="active",
        topic_id=42,
        started_at=0.0,
        last_user_turn_ts=0.0,
        completed_at=None,
        sdk_session_id=None,
        origin={},
        task="t",
    )


class TestEffectiveCallerRolePriority:
    async def test_engagement_var_wins_over_origin_var(self):
        """When both are set, the engagement role takes precedence.

        This is the E-6 fix: inside an in_casa engagement, origin_var
        still reflects the engager's bus role ('assistant') but the
        actual caller is the engagement (e.g. 'configurator').
        """
        from tools import engagement_var, _effective_caller_role
        import agent as agent_mod

        eng = _make_engagement(role="configurator")
        eng_token = engagement_var.set(eng)
        origin_token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            assert _effective_caller_role() == "configurator"
        finally:
            agent_mod.origin_var.reset(origin_token)
            engagement_var.reset(eng_token)

    async def test_origin_var_used_when_engagement_var_none(self):
        """origin_var is the fallback when no engagement is bound."""
        from tools import engagement_var, _effective_caller_role
        import agent as agent_mod

        # Sanity: engagement_var unbound at start
        assert engagement_var.get(None) is None

        origin_token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            assert _effective_caller_role() == "assistant"
        finally:
            agent_mod.origin_var.reset(origin_token)

    async def test_returns_none_when_neither_set(self):
        """Both unbound → None (caller-side must refuse, not default)."""
        from tools import engagement_var, _effective_caller_role
        import agent as agent_mod

        assert engagement_var.get(None) is None
        assert agent_mod.origin_var.get(None) is None
        assert _effective_caller_role() is None
