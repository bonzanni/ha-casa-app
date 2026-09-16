"""#1004: ``casa_reload`` / ``casa_reload_triggers`` accept the tier-prefixed
role forms every plugin surface uses for the same role.

Measured on production 2026-09-16: the configurator, having just passed
``targets=["resident:assistant"]`` to ``plugin_add`` and read
``reloaded: ["resident:assistant"]`` back, called
``casa_reload(scope="agent", role="resident:assistant")`` and got
``unknown_role``. The role IS known — under its directory name. The tool is
the trust boundary for that argument, so it canonicalises there: a leading
``resident:`` or ``specialist:`` is stripped before dispatch, and the reply
names the role the reload actually ran for.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio]


@pytest.fixture
def configurator_origin():
    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        yield
    finally:
        agent_mod.origin_var.reset(tok)


@pytest.fixture
def bound_runtime(monkeypatch):
    import agent as agent_mod
    runtime = MagicMock()
    monkeypatch.setattr(agent_mod, "active_runtime", runtime)
    return runtime


@pytest.fixture
def captured_dispatch(monkeypatch):
    """Replace ``reload.dispatch`` with a recorder that answers ``ok``."""
    import reload as reload_mod
    rec = AsyncMock(side_effect=lambda scope, **kw: {
        "status": "ok", "scope": scope, "role": kw.get("role"), "actions": []})
    monkeypatch.setattr(reload_mod, "dispatch", rec)
    # The post-reload health regeneration reaches plugin state; keep it out.
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health_after_reload",
                        AsyncMock(return_value=None))
    return rec


def _payload(result):
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("given, expected", [
    ("resident:assistant", "assistant"),
    ("specialist:finance", "finance"),
    ("assistant", "assistant"),
    ("  resident:butler ", "butler"),
])
async def test_casa_reload_agent_strips_the_tier_prefix(
        configurator_origin, bound_runtime, captured_dispatch, given, expected):
    from tools import casa_reload
    result = await casa_reload.handler({"scope": "agent", "role": given})
    payload = _payload(result)
    assert payload["status"] == "ok"
    assert captured_dispatch.await_args.kwargs["role"] == expected
    assert payload["role"] == expected


@pytest.mark.parametrize("given, expected", [
    ("resident:assistant", "assistant"),
    ("specialist:finance", "finance"),
])
async def test_casa_reload_triggers_strips_the_tier_prefix(
        configurator_origin, bound_runtime, captured_dispatch, given, expected):
    from tools import casa_reload_triggers
    result = await casa_reload_triggers.handler({"role": given})
    payload = _payload(result)
    assert payload["status"] == "ok"
    assert captured_dispatch.await_args.kwargs["role"] == expected
    assert payload["role"] == expected


@pytest.mark.parametrize("given", ["resident:", "specialist:", "resident: "])
async def test_a_bare_prefix_is_still_role_required(
        configurator_origin, bound_runtime, captured_dispatch, given):
    """Stripping the prefix must not turn an empty role into a dispatch."""
    from tools import casa_reload
    result = await casa_reload.handler({"scope": "agent", "role": given})
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["kind"] == "role_required"
    captured_dispatch.assert_not_awaited()


async def test_an_unknown_tier_prefix_is_not_stripped(
        configurator_origin, bound_runtime, captured_dispatch):
    """Only the two tier namespaces are canonicalised; anything else is the
    role name as given, and the reload answers for it (here: unknown)."""
    from tools import casa_reload
    await casa_reload.handler({"scope": "agent", "role": "executor:configurator"})
    assert captured_dispatch.await_args.kwargs["role"] == "executor:configurator"
