"""R5 (v0.89.0): the `react` framework tool — a stateless, order-safe,
active-record-gated emoji ack on the LATEST operator message.

Tested THROUGH the real internal /tools/call handler (so engagement_var is
bound exactly as production binds it — active records only), a REAL
EngagementRegistry, a REAL TelegramChannel, and a fake bot at the wire.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import tools
from channels import ChannelManager
from channels.telegram import TelegramChannel

pytestmark = [pytest.mark.unit]

SUPERGROUP = -1001
TOPIC = 555


class _ReactBot:
    """Fake Telegram bot recording set_message_reaction calls at the wire."""

    def __init__(self) -> None:
        self.reactions: list = []
        self.raise_exc: Exception | None = None

    async def set_message_reaction(self, chat_id, message_id, reaction, **kw):
        if self.raise_exc is not None:
            raise self.raise_exc
        self.reactions.append((chat_id, message_id, reaction))
        return True

    async def send_message(self, chat_id, text, message_thread_id=None, **kw):
        return MagicMock(message_id=1)


def _mk_update(*, message_id, text="do the thing", thread_id=TOPIC,
               chat_id=SUPERGROUP, user_id=77):
    u = MagicMock()
    u.message = MagicMock()
    u.message.chat = MagicMock()
    u.message.chat.id = chat_id
    u.message.text = text
    u.message.message_thread_id = thread_id
    u.message.from_user = MagicMock(id=user_id)
    u.message.message_id = message_id
    return u


async def _drain(ch) -> None:
    tasks = list(getattr(ch, "_turn_tasks", ()) or ())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest_asyncio.fixture
async def wired(tmp_path):
    """Real registry + real channel + real internal handler, fake bot."""
    from engagement_registry import EngagementRegistry
    from internal_handlers import _make_internal_tools_call_handler

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="executor", role_or_type="plugin-developer", driver="in_casa",
        task="t", origin={"role": "assistant", "channel": "telegram"},
        topic_id=TOPIC,
        # v0.166.0: react is a bridge tool, so the engagement must grant it for
        # the gate to dispatch — as the real plugin-developer definition does.
        tools_allowed=("mcp__casa-framework__react",),
    )

    bot = _ReactBot()
    ch = TelegramChannel(bot=bot, chat_id=100, engagement_supergroup_id=SUPERGROUP)
    ch._engagement_registry = reg
    ch._driver_send_user_turn = AsyncMock()

    cm = ChannelManager()
    cm.register(ch)
    tools.init_tools(
        channel_manager=cm, bus=MagicMock(), specialist_registry=MagicMock(),
        mcp_registry=MagicMock(), trigger_registry=MagicMock(),
        engagement_registry=reg,
    )

    app = web.Application()
    app.router.add_post(
        "/internal/tools/call",
        _make_internal_tools_call_handler(
            tool_dispatch={"react": tools.react.handler}, engagement_registry=reg,
        ),
    )

    class _Fx:
        def __init__(self):
            self.reg = reg
            self.rec = rec
            self.bot = bot
            self.ch = ch
            self.app = app

        async def react(self, eng_id, emoji="👍"):
            async with TestClient(TestServer(self.app)) as client:
                resp = await client.post(
                    "/internal/tools/call",
                    json={"name": "react", "arguments": {"emoji": emoji},
                          "engagement_id": eng_id,
                          "engagement_token": rec.auth_token},
                )
                body = await resp.json()
                # A grant-gate/auth rejection has no content block — hand the
                # raw body back so the caller can assert on the error.
                if "content" not in body:
                    return body
                return json.loads(body["content"][0]["text"])

    yield _Fx()


@pytest.mark.asyncio
async def test_react_targets_latest_operator_message(wired):
    """Two operator messages arrive; react targets the SECOND (latest) id,
    never eng.origin (which carries no message_id at all)."""
    await wired.ch.handle_update(_mk_update(message_id=100))
    await wired.ch.handle_update(_mk_update(message_id=101))
    await _drain(wired.ch)

    payload = await wired.react(wired.rec.id, "👍")
    assert payload["status"] == "ok"
    assert len(wired.bot.reactions) == 1
    chat_id, message_id, reaction = wired.bot.reactions[0]
    assert chat_id == SUPERGROUP
    assert message_id == 101                       # the LATEST, not 100, not origin
    assert reaction[0].emoji == "👍"


@pytest.mark.asyncio
async def test_react_monotonic_out_of_order(wired):
    """M+1 recorded first, then a delayed older M arrives — the stored target
    stays M+1 (concurrent handle_update; delivery order is not send order)."""
    await wired.ch.handle_update(_mk_update(message_id=101))   # newer first
    await wired.ch.handle_update(_mk_update(message_id=100))   # delayed older
    await _drain(wired.ch)

    payload = await wired.react(wired.rec.id, "👀")
    assert payload["status"] == "ok"
    _, message_id, _ = wired.bot.reactions[0]
    assert message_id == 101                       # monotonic guard held


@pytest.mark.asyncio
async def test_react_no_inbound_yields_no_current_inbound(wired):
    """Active engagement but nothing recorded (post-restart / no inbound yet)
    → fail-soft no-op, no reaction, no turn abort."""
    payload = await wired.react(wired.rec.id, "👍")
    assert payload["status"] == "no_current_inbound"
    assert wired.bot.reactions == []


@pytest.mark.asyncio
async def test_react_terminated_engagement_no_reaction(wired):
    """A STALE target present in the map, but the engagement is terminated:
    the internal handler binds no active record, so the call is rejected
    before react runs — NO reaction can ever fire on a finished engagement.
    (The react tool's own no_current_inbound guard remains as defense in
    depth; the bridge stops the call one step earlier.) #587: the refusal
    names the record's liveness rather than the grant."""
    await wired.ch.handle_update(_mk_update(message_id=101))
    await _drain(wired.ch)
    # stale target is present
    assert wired.ch.get_current_inbound(wired.rec.id) is not None

    await wired.reg.mark_cancelled(wired.rec.id)

    payload = await wired.react(wired.rec.id, "👍")
    assert payload["error"]["code"] == -32006, payload
    assert "engagement_not_live" in payload["error"]["message"]
    assert wired.bot.reactions == []               # non-live rejection = guarantee


@pytest.mark.asyncio
async def test_react_invalid_emoji_fails_soft(wired):
    """Telegram refuses an unavailable emoji (BadRequest) → soft error result,
    never a raised exception / turn abort."""
    from telegram.error import BadRequest

    await wired.ch.handle_update(_mk_update(message_id=101))
    await _drain(wired.ch)
    wired.bot.raise_exc = BadRequest("Reaction is not supported")

    payload = await wired.react(wired.rec.id, "🦄")
    assert payload["status"] == "error"
    assert payload["kind"] == "invalid_emoji"      # classified, not raised


@pytest.mark.asyncio
async def test_react_invalid_arguments(wired):
    payload = await wired.react(wired.rec.id, emoji="")
    assert payload["status"] == "error"
    assert payload["kind"] == "invalid_arguments"
    assert wired.bot.reactions == []


def test_react_doctrine_marks_non_decisional():
    """Doctrine states reactions are non-decisional and never an approval
    shortcut for the verdict broker / ask."""
    doctrine = pathlib.Path(
        "casa/rootfs/opt/casa/defaults/agents/executors/"
        "plugin-developer/doctrine/engagement-conduct.md"
    ).read_text(encoding="utf-8")
    low = doctrine.lower()
    assert "react" in low
    assert "non-decisional" in low or "not a decision" in low
    # explicitly forbids using a reaction as an ask/approval shortcut
    assert "approval" in low and "ask" in low


def test_react_never_touches_ask_answer_seam():
    """Structural guarantee: react cannot consume/settle an open ask — it never
    references the answer-reservation / broker / ask-gate machinery."""
    src = inspect.getsource(tools.react.handler)
    for forbidden in ("reserve_answer", "rollback_answer", "settle",
                      "BROKER", "ASK_GATES", "answer_token"):
        assert forbidden not in src, f"react must not reference {forbidden!r}"


def test_react_granted_by_definition_not_by_a_blanket_resume_grant():
    """react is a per-definition grant, not a launch/resume-mandatory one.

    v0.166.0: the bridge grant-gate admits react only when the engagement's
    record carries it, and the record is pinned from the definition. So react
    must come from the plugin-developer DEFINITION — the resume builder must NOT
    add it unconditionally, or a resumed configurator (whose definition lacks
    react) would be offered a tool the gate rejects."""
    import yaml
    defn = yaml.safe_load(pathlib.Path(
        "casa/rootfs/opt/casa/defaults/agents/executors/"
        "plugin-developer/definition.yaml"
    ).read_text(encoding="utf-8"))
    assert "mcp__casa-framework__react" in defn["tools"]["allowed"]

    # The mandatory grant sets never carry react.
    assert "mcp__casa-framework__react" not in tools.SPECIALIST_CASA_GRANTS
    assert "mcp__casa-framework__react" not in tools.MANDATORY_BRIDGE_CASA_GRANTS
    # The resume builder adds only the mandatory grants, so react is not handed
    # to an executor whose definition omits it.
    src = inspect.getsource(tools.build_engagement_resume_options)
    assert "SPECIALIST_CASA_GRANTS" in src
    assert "EXECUTOR_CASA_GRANTS" not in src


def test_react_registered_in_casa_tools():
    assert tools.react in tools.CASA_TOOLS
    assert tools.react.name == "react"
