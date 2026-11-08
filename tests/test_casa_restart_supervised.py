"""Tests for casa_restart_supervised (replacement for old casa_reload no-arg)."""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def configurator_origin():
    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        yield
    finally:
        agent_mod.origin_var.reset(tok)


class TestCasaRestartSupervised:
    async def test_happy_path_posts_supervisor(self, configurator_origin):
        from tools import casa_restart_supervised
        fake_resp = MagicMock(); fake_resp.status = 200
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=None)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session_cm)
        session_cm.__aexit__ = AsyncMock(return_value=None)
        session_cm.post = MagicMock(return_value=fake_resp)
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "x"}):
            with patch("aiohttp.ClientSession", return_value=session_cm):
                result = await casa_restart_supervised.handler({})
        payload = json.loads(result["content"][0]["text"])
        assert payload["supervisor_status"] == 200

    async def test_unprivileged_caller_refused(self):
        import agent as agent_mod
        from tools import casa_restart_supervised
        tok = agent_mod.origin_var.set({"role": "assistant"})
        try:
            result = await casa_restart_supervised.handler({})
        finally:
            agent_mod.origin_var.reset(tok)
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"

    async def test_a_delegated_run_defers_to_the_restart_button(self):
        """Inside a configurator run (a delegation id on the origin) nothing
        is POSTed: the run's answer gets the Restart now button."""
        import agent as agent_mod
        import configurator_run
        from tools import casa_restart_supervised
        tok = agent_mod.origin_var.set({"role": "assistant",
                                        "execution_role": "configurator",
                                        "_delegation_id": "d-1"})
        try:
            with patch.dict(os.environ, {}, clear=True):
                result = await casa_restart_supervised.handler({})
        finally:
            agent_mod.origin_var.reset(tok)
            configurator_run._reset_for_tests()
        payload = json.loads(result["content"][0]["text"])
        assert payload["deferred"] is True
