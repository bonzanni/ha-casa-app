"""H-1 fix (v0.34.0): casa_reload defers the Supervisor restart POST
until ``_finalize_engagement`` runs, so the bus message + Honcho
summary land BEFORE the addon container kill arrives.

Pre-fix (v0.33.x) live evidence — exploration3 P4.2 + P4.3 + P11.1
(2026-05-01) — the doctrine path
``commit -> casa_reload -> emit_completion`` had casa_reload POSTing
``addons/self/restart`` synchronously, which scheduled an async
container kill ~13s later that cancelled the SDK subprocess BEFORE the
model could call emit_completion. Engagement stuck status=active
forever; no user-DM completion message.

The fix splits casa_reload into two phases:

1. In-engagement call: drain the v0.33.1 G-2 pending-reload obligation,
   register a deferred-restart marker, return immediately with
   ``{supervisor_status: 200, deferred: true}`` — NO POST.
2. End of ``_finalize_engagement``: if the deferred marker is set AND
   outcome=completed, perform the actual Supervisor POST. The bus
   message + Honcho meta-summary have already landed by this point.

Out-of-engagement calls (operator /invoke, etc.) still POST inline.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def _drain_deferred_set():
    """Tests share module-level state — drain per-test so engagement
    ids from earlier cases don't leak into later ones."""
    import tools as tools_mod
    yield
    tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD.clear()


def _mock_supervisor_session(status: int = 200):
    """Build a MagicMock chain that mimics aiohttp's ClientSession+post
    context-manager pair so the test can inspect whether a POST was
    actually issued without standing up a real HTTP server."""
    fake_resp = MagicMock()
    fake_resp.status = status
    fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_resp.__aexit__ = AsyncMock(return_value=None)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session_cm)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    post_calls: list[str] = []

    def _post(url, **_kwargs):
        post_calls.append(url)
        return fake_resp

    session_cm.post = MagicMock(side_effect=_post)
    return session_cm, post_calls


# ---------------------------------------------------------------------------
# casa_reload itself
# ---------------------------------------------------------------------------


class TestCasaReloadDefers:
    """The tool body's branch on engagement_var presence."""

    async def test_in_engagement_does_not_post(self, _drain_deferred_set):
        """When called inside an active engagement, casa_restart_supervised
        must not POST Supervisor. It must return ``deferred: True`` and
        register the engagement id in the deferred-restart set.

        v0.35.0 note: the old no-arg ``casa_reload()`` shape moved to
        ``casa_restart_supervised``. The H-1 defer behavior carries
        forward unchanged on the new tool.
        """
        import tools as tools_mod
        from tools import casa_restart_supervised, engagement_var
        from engagement_registry import EngagementRecord

        rec = EngagementRecord(
            id="eng-defer", kind="executor",
            role_or_type="configurator", driver="in_casa",
            status="active", topic_id=1, started_at=0.0,
            last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
            completed_at=None, sdk_session_id=None, origin={}, task="",
        )

        session_cm, post_calls = _mock_supervisor_session(200)
        tok = engagement_var.set(rec)
        try:
            with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "x"}):
                with patch("aiohttp.ClientSession", return_value=session_cm):
                    result = await casa_restart_supervised.handler({})
        finally:
            engagement_var.reset(tok)

        payload = json.loads(result["content"][0]["text"])
        assert payload["supervisor_status"] == 200
        assert payload["deferred"] is True
        assert "deferred until engagement finalizes" in payload["message"]
        assert post_calls == [], (
            "casa_restart_supervised must NOT POST Supervisor when "
            f"called inside an engagement; got POST calls: {post_calls}"
        )
        assert rec.id in tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD

    async def test_out_of_engagement_posts_inline(self, _drain_deferred_set):
        """Operator-driven (or otherwise out-of-engagement) calls
        still POST Supervisor synchronously — there is no
        ``_finalize_engagement`` to defer to.

        Bind ``origin_var={"role": "configurator"}`` to pass the
        privileged-role guard without engaging the engagement_var
        deferred-path.
        """
        import agent as agent_mod
        from tools import casa_restart_supervised

        session_cm, post_calls = _mock_supervisor_session(200)
        tok = agent_mod.origin_var.set({"role": "configurator"})
        try:
            with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "x"}):
                with patch("aiohttp.ClientSession", return_value=session_cm):
                    result = await casa_restart_supervised.handler({})
        finally:
            agent_mod.origin_var.reset(tok)

        payload = json.loads(result["content"][0]["text"])
        assert payload["supervisor_status"] == 200
        assert "deferred" not in payload, (
            "out-of-engagement calls must NOT carry the deferred "
            f"marker; got: {payload}"
        )
        assert post_calls == ["http://supervisor/addons/self/restart"], (
            "out-of-engagement casa_restart_supervised must POST "
            f"Supervisor synchronously; got: {post_calls}"
        )


# ---------------------------------------------------------------------------
# _finalize_engagement honors the deferred marker
# ---------------------------------------------------------------------------


class TestFinalizeEngagementHonorsDeferred:
    async def test_completed_with_deferred_posts_supervisor(
        self, _drain_deferred_set, tmp_path,
    ):
        """outcome=completed AND deferred marker set => POST Supervisor
        at end of finalize, AFTER all bus + Honcho writes."""
        import tools as tools_mod
        from tools import _finalize_engagement
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None,
        )
        rec = await reg.create(
            kind="executor", role_or_type="configurator",
            driver="in_casa", task="t",
            origin={"role": "assistant", "channel": "telegram", "chat_id": "1"},
            topic_id=42,
        )
        tools_mod.init_tools(
            channel_manager=None, bus=None,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD.add(rec.id)

        session_cm, post_calls = _mock_supervisor_session(200)
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "x"}):
            with patch("aiohttp.ClientSession", return_value=session_cm):
                await _finalize_engagement(
                    rec, outcome="completed", text="done.",
                    artifacts=[], next_steps=[], driver=None,
                )

        assert post_calls == ["http://supervisor/addons/self/restart"], (
            "deferred POST must fire at end of finalize on completed; "
            f"got: {post_calls}"
        )
        assert rec.id not in tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD

    async def test_cancelled_drains_marker_without_posting(
        self, _drain_deferred_set, tmp_path,
    ):
        """outcome=cancelled => drain the marker without POSTing.

        If the model asked for a supervised restart mid-flight and then
        the user cancelled, the platform shouldn't unilaterally restart:
        that decision is the operator's.
        """
        import tools as tools_mod
        from tools import _finalize_engagement
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None,
        )
        rec = await reg.create(
            kind="executor", role_or_type="configurator",
            driver="in_casa", task="t",
            origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )
        tools_mod.init_tools(
            channel_manager=None, bus=None,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD.add(rec.id)

        session_cm, post_calls = _mock_supervisor_session(200)
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "x"}):
            with patch("aiohttp.ClientSession", return_value=session_cm):
                await _finalize_engagement(
                    rec, outcome="cancelled", text="aborted.",
                    artifacts=[], next_steps=[], driver=None,
                )

        assert post_calls == [], (
            f"cancelled engagement must NOT POST; got: {post_calls}"
        )
        assert rec.id not in tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD

    async def test_error_drains_marker_without_posting(
        self, _drain_deferred_set, tmp_path,
    ):
        """outcome=error: same shape as cancelled. The reload decision
        is the operator's. Drain the marker either way."""
        import tools as tools_mod
        from tools import _finalize_engagement
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None,
        )
        rec = await reg.create(
            kind="executor", role_or_type="configurator",
            driver="in_casa", task="t",
            origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )
        tools_mod.init_tools(
            channel_manager=None, bus=None,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD.add(rec.id)

        session_cm, post_calls = _mock_supervisor_session(200)
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "x"}):
            with patch("aiohttp.ClientSession", return_value=session_cm):
                await _finalize_engagement(
                    rec, outcome="error", text="bailed.",
                    artifacts=[], next_steps=[], driver=None,
                )

        assert post_calls == []
        assert rec.id not in tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD

    async def test_completed_without_deferred_does_not_post(
        self, _drain_deferred_set, tmp_path,
    ):
        """No deferred marker (e.g., no_op engagement that didn't call
        casa_reload) => do not POST."""
        import tools as tools_mod
        from tools import _finalize_engagement
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None,
        )
        rec = await reg.create(
            kind="executor", role_or_type="configurator",
            driver="in_casa", task="t",
            origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )
        tools_mod.init_tools(
            channel_manager=None, bus=None,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        # NOTE: not adding rec.id to _ENGAGEMENTS_DEFERRED_HARD_RELOAD.

        session_cm, post_calls = _mock_supervisor_session(200)
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "x"}):
            with patch("aiohttp.ClientSession", return_value=session_cm):
                await _finalize_engagement(
                    rec, outcome="completed", text="no-op.",
                    artifacts=[], next_steps=[], driver=None,
                )

        assert post_calls == [], (
            "no deferred marker => no POST regardless of outcome; "
            f"got: {post_calls}"
        )
