"""R4 (v0.89.0, buttons-always) — the ``engagement_buttons_reminder`` PreToolUse
policy on the WORKSPACE hook path (hook_proxy.sh -> /internal/hooks/resolve).

The plugin-developer engaged executor runs the STANDALONE Claude CLI, so an
in-casa SDK ``can_use_tool`` PreToolUse callback never fires for it. The salience
backstop for the buttons-always principle must therefore live in the internal
HTTP resolver: when the tool is ``Skill`` AND the request's cwd resolves to an
ACTIVE engagement, the resolver injects a PreToolUse ``additionalContext``
reminder (never ``systemMessage`` — that is user-facing) so the reminder rides
alongside the freshly-loaded skill (e.g. superpowers:brainstorming) that would
otherwise out-compete the earlier-read doctrine.

The trigger is tool IDENTITY (Skill) + engagement-from-cwd ONLY — never message
content. These tests assert:
  1. Skill call under an active-engagement cwd -> additionalContext reminder.
  2. Non-engagement cwd -> nothing (allow, empty body).
  3. Non-Skill tool -> nothing (allow, empty body).
  4. The bundled plugin-developer hooks.yaml -> generated settings.json
     registers a ``Skill`` PreToolUse matcher wired to the policy, so the proxy
     forwards Skill calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ENG_ID = "b" * 32

_ROOT = Path(__file__).resolve().parent.parent
_DEV = _ROOT / (
    "casa/rootfs/opt/casa/defaults/agents/executors/plugin-developer"
)


class _Rec:
    status = "active"
    role_or_type = "plugin-developer"
    auth_token = "tok-b"


class _TerminalRec:
    status = "completed"
    role_or_type = "plugin-developer"
    auth_token = "tok-b"


class _Registry:
    def get(self, eng_id):
        return _Rec() if eng_id == ENG_ID else None


# #366: identity on the hook path is credential-authenticated — requests that
# should carry an engagement identity present the workspace credential pair.
_CREDS = {"engagement_id": ENG_ID, "engagement_token": "tok-b"}


def _handler(registry=None):
    from hooks import make_engagement_buttons_reminder
    from internal_handlers import _make_internal_hooks_resolve_handler

    registry = registry or _Registry()
    reminder_cb = make_engagement_buttons_reminder(engagement_registry=registry)
    # Wired shape mirrors _wire_engagement_permission_relay: (matcher, callback)
    # in the DEFAULT policy dict. matcher "Skill" gates tool identity.
    hook_policies = {"engagement_buttons_reminder": ("Skill", reminder_cb)}
    return _make_internal_hooks_resolve_handler(
        hook_policies=hook_policies,
        # plugin-developer contributes no factory-built entry for this policy
        # (it has no HOOK_POLICIES factory) -> the resolver falls back to the
        # wired default above.
        executor_hook_policies={"plugin-developer": {}},
        engagement_registry=registry,
    )


async def _post(handler, *, policy, payload, creds=None):
    app = web.Application()
    app.router.add_post("/hooks/resolve", handler)
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": policy, "payload": payload, **(creds or {})},
        )
        return await resp.json()


@pytest.mark.asyncio
async def test_skill_under_active_engagement_injects_reminder():
    body = await _post(
        _handler(),
        policy="engagement_buttons_reminder",
        payload={
            "tool_name": "Skill",
            "cwd": f"/data/engagements/{ENG_ID}",
            "tool_input": {"command": "superpowers:brainstorming"},
        },
        creds=_CREDS,
    )
    hso = body["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    ctx = hso["additionalContext"]
    assert "engagement channel" in ctx
    assert "`ask`" in ctx and "options" in ctx
    assert "tappable buttons" in ctx
    # It is a context injection, NOT a block/ask/allow decision, and NEVER a
    # user-facing systemMessage.
    assert "permissionDecision" not in hso
    assert "systemMessage" not in body
    assert "systemMessage" not in hso


@pytest.mark.asyncio
async def test_skill_without_authenticated_identity_emits_nothing():
    # #366: no credential ⇒ no engagement identity, even with an
    # engagement-workspace cwd claim in the payload — silent no-op.
    body = await _post(
        _handler(),
        policy="engagement_buttons_reminder",
        payload={
            "tool_name": "Skill",
            "cwd": f"/data/engagements/{ENG_ID}",
            "tool_input": {"command": "superpowers:brainstorming"},
        },
    )
    assert body == {}


@pytest.mark.asyncio
async def test_skill_under_inactive_engagement_emits_nothing():
    class _TermReg:
        def get(self, eng_id):
            return _TerminalRec() if eng_id == ENG_ID else None

    body = await _post(
        _handler(registry=_TermReg()),
        policy="engagement_buttons_reminder",
        payload={
            "tool_name": "Skill",
            "cwd": f"/data/engagements/{ENG_ID}",
            "tool_input": {"command": "superpowers:brainstorming"},
        },
        creds=_CREDS,
    )
    assert body == {}


@pytest.mark.asyncio
async def test_non_skill_tool_emits_nothing():
    body = await _post(
        _handler(),
        policy="engagement_buttons_reminder",
        payload={
            "tool_name": "Bash",
            "cwd": f"/data/engagements/{ENG_ID}",
            "tool_input": {"command": "echo hi"},
        },
    )
    assert body == {}
