"""#1142 — a plugin webhook trigger may declare ``deliver: operator``.

A plugin webhook turn (``channel="webhook"``, ``_origin_route="webhook_trigger"``)
used to drop its final reply silently: ``channel_manager.get("webhook")`` is
``None``, so the delivery block never ran. A trigger now opts in per manifest
entry; for such a trigger every output path of the turn — the final reply, a
classified error, the #650 retry-tainted-silence error — reaches the operator's
Telegram chat with a FRESH delivery context, and ``send_message`` is not offered
(one send path per turn). Triggers that do not declare it are unchanged.

Every delivery assertion runs against the REAL ``TelegramChannel`` with a
recording bot (never a stub channel), counts the Bot API sends and names the
exact chat. Every turn message is built by the REAL ``/webhook/{name}``
handler over a REAL ``TriggerRegistry`` overlay, and the turn runs the real
``Agent.handle_message`` → ``_process`` path with a scripted SDK client.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import mcp.types as mcp_types
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

import retry as retry_mod
from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from error_kinds import ErrorKind
from mcp_registry import McpServerRegistry
from plugin_triggers import parse_and_validate
from session_registry import SessionRegistry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:  # pragma: no cover — run from inside tests/
    from role_artifact_stub import STUB_ROLE_ARTIFACT

ROLE = "assistant"
OPERATOR_CHAT = "4242"
EFFECTIVE = "plg-elevenlabs--voicemail"
GLOBAL_SECRET = "global-webhook-secret"
SEND_TOOL = "mcp__casa-framework__send_message"
RECALL_TOOL = "mcp__casa-framework__recall_memory"


# ---------------------------------------------------------------------------
# Test 1 — the manifest field
# ---------------------------------------------------------------------------


def _m(**over):
    t = {"name": "voicemail", "type": "webhook", "target": "resident:assistant",
         "auth": {"mode": "static_header"}}
    t.update(over)
    return {"casa": {"triggers": [t]}}


@pytest.mark.parametrize("value", ["none", "operator"])
def test_deliver_accepts_the_two_enum_values(value):
    trig, errs = parse_and_validate("elevenlabs", _m(deliver=value))
    assert errs == []
    assert len(trig) == 1 and trig[0]["deliver"] == value


def test_deliver_defaults_to_none():
    trig, errs = parse_and_validate("elevenlabs", _m())
    assert errs == []
    assert trig[0]["deliver"] == "none"


@pytest.mark.parametrize("value", ["telegram", "Operator", "", None, True, 1,
                                   ["operator"]])
def test_any_other_deliver_value_rejects_the_set(value):
    _trig, errs = parse_and_validate("elevenlabs", _m(deliver=value))
    assert len(errs) == 1 and "deliver" in errs[0], errs


def test_other_unknown_keys_are_still_rejected():
    _trig, errs = parse_and_validate(
        "elevenlabs", _m(deliver="operator", notify="operator"))
    assert len(errs) == 1 and "unknown key" in errs[0] and "notify" in errs[0]


# ---------------------------------------------------------------------------
# Test 2 — reconcile → overlay → webhook_route, and the pending consent
# ---------------------------------------------------------------------------

AUTH = {"mode": "static_header", "header": "X-API-Key",
        "tolerance_secs": 300, "secret_owner": "casa"}


def _plugin(deliver=None, artifact_id="art-1"):
    t = {"name": "voicemail", "type": "webhook", "target": "resident:assistant",
         "auth": {"mode": "static_header", "header": "X-API-Key"}}
    if deliver is not None:
        t["deliver"] = deliver
    return SimpleNamespace(
        name="elevenlabs", artifact_id=artifact_id, path="/store/elevenlabs",
        version="1.0.0", manifest={"name": "x", "casa": {"triggers": [t]}})


def _resolver(p):
    def resolve(_target):
        return SimpleNamespace(registry_valid=True, plugins=[p])
    return resolve


class _FakeTelegram:
    chat_id = "100"

    def __init__(self):
        self.posts = []

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        return 55

    async def edit_dm_message(self, chat_id, message_id, text):
        return True


class _FakeChannelManager:
    def __init__(self, telegram):
        self._telegram = telegram

    def get(self, name):
        return self._telegram if name == "telegram" else None


async def _reconcile(tmp_path, p, *, acked: bool, telegram=None):
    import trigger_reconcile as tr
    from plugin_triggers import ack_identity
    from trigger_acks import TriggerAckStore
    from trigger_registry import TriggerRegistry

    registry = TriggerRegistry(scheduler=None, app=None, bus=None)
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    if acked:
        ident = ack_identity(plugin="elevenlabs", artifact_id=p.artifact_id,
                             effective=EFFECTIVE, target="resident:assistant",
                             auth=AUTH)
        acks.record(identity=ident, plugin="elevenlabs",
                    artifact_id=p.artifact_id, effective=EFFECTIVE,
                    target="resident:assistant", auth=AUTH)
    issues = await tr.reconcile_plugin_triggers(
        trigger_registry=registry,
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])},
        channel_manager=_FakeChannelManager(telegram) if telegram else None,
        acks=acks, secrets_dir=tmp_path / "webhook_secrets", prompt=True,
        resolver=_resolver(p), global_secret_ok=lambda: True)
    return registry, issues


@pytest.mark.asyncio
@pytest.mark.parametrize("declared,expected", [
    ("operator", "operator"), ("none", "none"), (None, "none")])
async def test_the_route_record_carries_deliver(tmp_path, declared, expected):
    registry, issues = await _reconcile(tmp_path, _plugin(declared), acked=True)
    assert issues == []
    route = registry.webhook_route(EFFECTIVE)
    assert route is not None and route["resident"] is False
    assert route["deliver"] == expected


def test_a_resident_route_reads_none():
    from config import TriggerSpec
    from trigger_registry import TriggerRegistry

    registry = TriggerRegistry(scheduler=MagicMock(), app=web.Application(),
                               bus=MagicMock())
    registry.register_agent(
        "assistant", [TriggerSpec(name="doorbell", type="webhook")],
        channels=["webhook"])
    route = registry.webhook_route("doorbell")
    assert route is not None and route["resident"] is True
    assert route["deliver"] == "none"


@pytest.mark.asyncio
async def test_the_pending_consent_shows_deliver_operator(tmp_path, monkeypatch):
    import authz_grants
    import trigger_reconcile as tr
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    monkeypatch.setattr(authz_grants, "CHALLENGES",
                        authz_grants.ChallengeCoordinator())

    p = _plugin("operator")
    desired = tr.compute_desired(
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])},
        acks=_EmptyAcks(), resolver=_resolver(p), global_secret_ok=lambda: True)
    assert len(desired.pending) == 1
    assert desired.pending[0]["deliver"] == "operator"

    telegram = _FakeTelegram()
    _registry, issues = await _reconcile(tmp_path, p, acked=False,
                                         telegram=telegram)
    assert [i.reason_code for i in issues] == ["trigger_pending_ack"]
    for _ in range(8):
        await asyncio.sleep(0)
    assert len(telegram.posts) == 1
    assert "each fire sends you a Telegram message" in telegram.posts[0][2]


@pytest.mark.asyncio
async def test_a_none_trigger_consent_does_not_promise_a_message(
        tmp_path, monkeypatch):
    import authz_grants
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    monkeypatch.setattr(authz_grants, "CHALLENGES",
                        authz_grants.ChallengeCoordinator())
    telegram = _FakeTelegram()
    await _reconcile(tmp_path, _plugin(), acked=False, telegram=telegram)
    for _ in range(8):
        await asyncio.sleep(0)
    assert len(telegram.posts) == 1
    assert "Telegram message" not in telegram.posts[0][2]


class _EmptyAcks:
    def get(self, _ident):
        return None


# ---------------------------------------------------------------------------
# Test 3 — ingress stamping by the real /webhook/{name} handler
# ---------------------------------------------------------------------------


def _registry_with(deliver: str | None):
    from trigger_registry import TriggerRegistry
    registry = TriggerRegistry(scheduler=MagicMock(), app=web.Application(),
                               bus=MagicMock())
    entry = {"plugin": "elevenlabs", "role": ROLE, "clearance": "public",
             "auth": {"mode": "hmac_body", "header": "X-Webhook-Signature",
                      "tolerance_secs": 300, "secret_owner": "casa"},
             "identity": "ident#1"}
    if deliver is not None:
        entry["deliver"] = deliver
    registry.replace_plugin_overlay({EFFECTIVE: entry})
    return registry


async def _ingress(deliver: str | None, payload: dict | None = None,
                   name: str = EFFECTIVE) -> BusMessage:
    """One POST through the REAL wildcard handler; returns the bus message."""
    from casa_core import _make_webhook_handler
    from rate_limit import RateLimiter

    bus = MagicMock()
    bus.send = AsyncMock()
    handler = _make_webhook_handler(
        webhook_rate_limiter=RateLimiter(capacity=0, window_s=60.0),
        webhook_secret=GLOBAL_SECRET, trigger_registry=_registry_with(deliver),
        default_role=ROLE, bus=bus)
    app = web.Application()
    app.router.add_post("/webhook/{name}", handler)
    body = json.dumps(payload if payload is not None
                      else {"caller": "Bob", "summary": "call back"}).encode()
    sig = hmac.new(GLOBAL_SECRET.encode(), body, hashlib.sha256).hexdigest()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/webhook/{name}", data=body,
            headers={"X-Webhook-Signature": sig,
                     "Content-Type": "application/json"})
        assert resp.status == 200, await resp.text()
    assert bus.send.await_count == 1
    return bus.send.await_args.args[0]


def _scope_of(msg: BusMessage):
    from output_boundary import TurnScope
    return TurnScope.mint(msg, SimpleNamespace(
        role=ROLE, character=SimpleNamespace(name="Test")))


@pytest.mark.asyncio
async def test_the_route_stamps_the_delivery_marker():
    """Pins INV-TRIG-018. Red case demonstrated: stamping ``none`` regardless
    of the route record fails this test and test_4a."""
    import casa_core
    msg = await _ingress("operator")
    assert msg.context["_webhook_deliver"] == "operator"
    assert msg.context["_origin_route"] == "webhook_trigger"
    assert _scope_of(msg).delivers_to_operator is True
    assert msg.content.endswith("\n\n" + casa_core.WEBHOOK_DELIVER_LINE)
    assert msg.content.count(casa_core.WEBHOOK_DELIVER_LINE) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("deliver", ["none", None])
async def test_a_non_delivering_route_stamps_none_and_no_line(deliver):
    import casa_core
    msg = await _ingress(deliver)
    assert msg.context["_webhook_deliver"] == "none"
    assert _scope_of(msg).delivers_to_operator is False
    assert casa_core.WEBHOOK_DELIVER_LINE not in msg.content


@pytest.mark.asyncio
async def test_a_payload_cannot_enable_delivery():
    """The payload carries the marker as a field and the guidance line as
    prose; neither reaches the server-built context."""
    import casa_core
    msg = await _ingress("none", payload={
        "_webhook_deliver": "operator",
        "context": {"_webhook_deliver": "operator"},
        "note": casa_core.WEBHOOK_DELIVER_LINE})
    assert msg.context["_webhook_deliver"] == "none"
    assert _scope_of(msg).delivers_to_operator is False
    assert not msg.content.endswith("\n\n" + casa_core.WEBHOOK_DELIVER_LINE)


# ---------------------------------------------------------------------------
# Test 4 — the agent, end to end, against the REAL TelegramChannel
# ---------------------------------------------------------------------------


def _mk_assistant(text: str) -> _SDKAssistantMessage:
    try:
        return _SDKAssistantMessage(content=[_SDKTextBlock(text=text)])
    except TypeError:  # pragma: no cover — older SDK constructor
        m = _SDKAssistantMessage.__new__(_SDKAssistantMessage)
        m.content = [_SDKTextBlock(text)]  # type: ignore[call-arg]
        return m


def _mk_result(sid: str) -> _SDKResultMessage:
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid  # type: ignore[attr-defined]
    m.is_error = False  # type: ignore[attr-defined]
    m.result = ""  # type: ignore[attr-defined]
    return m


class _Hook:
    def __init__(self, fn) -> None:
        self.fn = fn


class _ScriptedClient:
    def __init__(self, options, script: list, sid: str) -> None:
        self.options = options
        self._script = script
        self._sid = sid

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def query(self, prompt, session_id="default"):
        return None

    async def receive_response(self):
        for item in self._script:
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, _Hook):
                await item.fn()
                continue
            yield item
        yield _mk_result(self._sid)


class _Factory:
    def __init__(self, scripts: list[list]) -> None:
        self._scripts = list(scripts)
        self.clients: list[_ScriptedClient] = []

    def __call__(self, options) -> _ScriptedClient:
        script = self._scripts.pop(0) if self._scripts else []
        c = _ScriptedClient(options, script, sid=f"sid-{len(self.clients) + 1}")
        self.clients.append(c)
        return c


@contextmanager
def _patch_retry_sleep():
    # retry.py's MODULE-LOCAL asyncio only — never the shared asyncio.sleep.
    ns = SimpleNamespace(sleep=AsyncMock(), CancelledError=asyncio.CancelledError)
    with patch.object(retry_mod, "asyncio", ns):
        yield


class _RecordingBot:
    """The Bot API surface the channel's delivery calls reach — recorded."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))

    async def send_chat_action(self, **kwargs):
        return None


def _telegram():
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(bot_token="T", chat_id=OPERATOR_CHAT,
                         default_agent=ROLE)
    bot = _RecordingBot()
    ch._app = SimpleNamespace(bot=bot)  # type: ignore[assignment]
    return ch, bot


def _resident(tmp_path, *, with_telegram: bool = True):
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=ROLE, model="claude-sonnet-4-6",
        system_prompt="You are helpful.", character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
    )
    agent = Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(), channel_manager=ChannelManager(),
    )
    ch, bot = _telegram()
    if with_telegram:
        agent._channel_manager.register(ch)
    import tools
    tools.init_tools(
        channel_manager=agent._channel_manager, bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
    )
    return agent, ch, bot


async def _run(agent, msg, factory, monkeypatch):
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    with _patch_retry_sleep():
        try:
            return await agent.handle_message(msg)
        finally:
            await agent.aclose()


def _chats(bot) -> list[str]:
    return [str(s["chat_id"]) for s in bot.sent]


def _texts(bot) -> list[str]:
    return [str(s["text"]) for s in bot.sent]


@pytest.mark.asyncio
async def test_4a_an_opted_in_final_reply_reaches_the_operator(
        tmp_path, monkeypatch):
    agent, ch, bot = _resident(tmp_path)
    msg = await _ingress("operator")
    factory = _Factory([[_mk_assistant("Voicemail from Bob: call him back.")]])
    await _run(agent, msg, factory, monkeypatch)
    assert len(factory.clients) == 1
    assert _chats(bot) == [str(ch.chat_id)] == [OPERATOR_CHAT]
    assert "Voicemail from Bob: call him back." in _texts(bot)[0]


@pytest.mark.asyncio
async def test_4b_a_numeric_context_chat_id_cannot_redirect_the_reply(
        tmp_path, monkeypatch):
    """Negative control for the fresh delivery context: a numeric ``chat_id``
    in the execution context would override the channel default.

    Pins INV-OUT-006. Red case demonstrated: delivering with ``msg.context``
    instead of the fresh ``{}`` fails this test (the reply goes to 777001)."""
    agent, ch, bot = _resident(tmp_path)
    msg = await _ingress("operator")
    msg.context["chat_id"] = "777001"
    factory = _Factory([[_mk_assistant("Voicemail from Bob.")]])
    await _run(agent, msg, factory, monkeypatch)
    assert _chats(bot) == [OPERATOR_CHAT]


@pytest.mark.asyncio
async def test_4c_closing_silence_stays_suppressed(tmp_path, monkeypatch):
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("operator")
    factory = _Factory([[_mk_assistant("<silent/>")]])
    await _run(agent, msg, factory, monkeypatch)
    assert bot.sent == []


@pytest.mark.asyncio
async def test_4d_a_classified_error_reaches_the_operator(tmp_path, monkeypatch):
    """Pins INV-OUT-006. Red case demonstrated: routing only a successful
    reply to the operator (errors on the message's own channel) fails this."""
    from agent import _USER_MESSAGES
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("operator")
    msg.context["chat_id"] = "777001"
    factory = _Factory([[RuntimeError("boom")]])
    await _run(agent, msg, factory, monkeypatch)
    assert _chats(bot) == [OPERATOR_CHAT]
    assert _texts(bot) == [_USER_MESSAGES[ErrorKind.UNKNOWN]]


@pytest.mark.asyncio
async def test_4d_retry_tainted_silence_reaches_the_operator(
        tmp_path, monkeypatch):
    """#650: a retried attempt that ends in a sentinel is a failed ask on a
    trusted-origin turn; its mapped error goes to the operator too."""
    from agent import _USER_MESSAGES
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("operator")
    assert msg.trusted_user_origin is not None
    msg.context["chat_id"] = "777001"
    factory = _Factory([[RuntimeError("rate limit exceeded")],
                        [_mk_assistant("<silent/>")]])
    await _run(agent, msg, factory, monkeypatch)
    assert len(factory.clients) == 2
    assert _chats(bot) == [OPERATOR_CHAT]
    assert _texts(bot) == [_USER_MESSAGES[ErrorKind.RATE_LIMIT]]


@pytest.mark.asyncio
@pytest.mark.parametrize("deliver", ["none", None])
async def test_4e_a_non_opted_in_webhook_turn_sends_nothing(
        tmp_path, monkeypatch, deliver):
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress(deliver)
    factory = _Factory([[_mk_assistant("Voicemail from Bob.")]])
    await _run(agent, msg, factory, monkeypatch)
    assert len(factory.clients) == 1
    assert bot.sent == []


@pytest.mark.asyncio
async def test_4f_a_trusted_invoke_turn_is_unaffected(tmp_path):
    """``/invoke`` is not a webhook-trigger route: even a caller-supplied
    marker is stripped at ingress, nothing reaches Telegram, and the reply is
    the RESPONSE the caller blocks on."""
    from casa_core import build_invoke_message
    agent, _ch, bot = _resident(tmp_path)
    msg = build_invoke_message(ROLE, "hello", {"context": {
        "_webhook_deliver": "operator", "chat_id": "777001"}})
    assert "_webhook_deliver" not in msg.context
    with patch.object(agent, "_process", AsyncMock(return_value="the answer")):
        out = await agent.handle_message(msg)
    await agent.aclose()
    assert bot.sent == []
    assert out is not None and str(out.content) == "the answer"


@pytest.mark.asyncio
async def test_4g_no_health_notice_rides_an_opted_in_reply(
        tmp_path, monkeypatch):
    """The plugin-health notice is an operator-turn courtesy; the helper does
    not exclude untrusted turns itself, so the opted-in path skips it."""
    import plugin_health
    plugin_health._notice_memo.clear()
    monkeypatch.setattr(
        plugin_health, "render_notice",
        lambda role, path=plugin_health.HEALTH_PATH: "PLUGIN-DEGRADED x")
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("operator")
    factory = _Factory([[_mk_assistant("Voicemail from Bob.")]])
    await _run(agent, msg, factory, monkeypatch)
    assert _chats(bot) == [OPERATOR_CHAT]
    assert "PLUGIN-DEGRADED" not in _texts(bot)[0]
    plugin_health._notice_memo.clear()


@pytest.mark.asyncio
async def test_no_operator_channel_logs_and_drops(tmp_path, monkeypatch, caplog):
    agent, _ch, bot = _resident(tmp_path, with_telegram=False)
    msg = await _ingress("operator")
    factory = _Factory([[_mk_assistant("Voicemail from Bob.")]])
    with caplog.at_level("WARNING"):
        await _run(agent, msg, factory, monkeypatch)
    assert bot.sent == []
    assert any("webhook deliver: no operator channel" in r.getMessage()
               for r in caplog.records)


# ---------------------------------------------------------------------------
# Test 5 — one send path: send_message is not offered, and refused
# ---------------------------------------------------------------------------


def _listed_tools(options) -> set[str]:
    server = options.mcp_servers["casa-framework"]["instance"]
    handler = server.request_handlers[mcp_types.ListToolsRequest]
    listed = asyncio.run(handler(mcp_types.ListToolsRequest(method="tools/list")))
    return {t.name for t in listed.root.tools}


def _restricted(delivers: bool):
    from agent import build_restricted_webhook_options
    return build_restricted_webhook_options(
        model="m", role=ROLE, system_prompt="p", max_turns=3,
        agent_home="/tmp", resume_sid=None, delivers_to_operator=delivers)


def test_5_opted_in_options_do_not_offer_send_message():
    """Pins INV-TRIG-018. Red case demonstrated: dropping ``send_message``
    from ``allowed_tools`` only, while the server is still built from the full
    set, leaves it in the listing and fails this test."""
    opts = _restricted(True)
    assert SEND_TOOL not in opts.allowed_tools
    assert RECALL_TOOL in opts.allowed_tools
    assert SEND_TOOL in opts.disallowed_tools
    assert _listed_tools(opts) == {"recall_memory"}


def test_5_non_opted_in_options_are_unchanged():
    opts = _restricted(False)
    assert opts.allowed_tools == [RECALL_TOOL, SEND_TOOL]
    assert SEND_TOOL not in opts.disallowed_tools
    assert _listed_tools(opts) == {"recall_memory", "send_message"}


def _send_hook(results: list):
    async def _go():
        import tools
        results.append(await tools.send_message.handler(
            {"message": "Bob called (tool send).", "channel": "voice"}))
    return _Hook(_go)


@pytest.mark.asyncio
async def test_5_an_opted_in_turn_builds_the_reduced_runtime_and_refuses_the_tool(
        tmp_path, monkeypatch):
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("operator")
    results: list = []
    factory = _Factory([[_send_hook(results), _mk_assistant("Bob called.")]])
    await _run(agent, msg, factory, monkeypatch)
    opts = factory.clients[0].options
    assert SEND_TOOL not in opts.allowed_tools
    assert SEND_TOOL in opts.disallowed_tools
    assert len(results) == 1 and results[0].get("is_error") is True
    assert ("this webhook's final reply is delivered to the operator; reply "
            "instead of calling send_message") in results[0]["content"][0]["text"]
    # Exactly one message: the final reply. The refused tool sent nothing.
    assert _chats(bot) == [OPERATOR_CHAT]
    assert _texts(bot) == ["Bob called."]


@pytest.mark.asyncio
async def test_5_a_non_opted_in_turn_keeps_send_message(tmp_path, monkeypatch):
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("none")
    results: list = []
    factory = _Factory([[_send_hook(results), _mk_assistant("Bob called.")]])
    await _run(agent, msg, factory, monkeypatch)
    opts = factory.clients[0].options
    assert SEND_TOOL in opts.allowed_tools
    assert len(results) == 1 and not results[0].get("is_error")
    # The tool send, bound to the operator's Telegram; the final text is not
    # delivered (today's behaviour for a non-opted-in trigger).
    assert _chats(bot) == [OPERATOR_CHAT]
    assert "Bob called (tool send)." in _texts(bot)[0]


@pytest.mark.asyncio
async def test_5_the_handler_refuses_on_an_opted_in_scope(monkeypatch):
    """The handler reads the scope's registered fact — no options involved.

    Pins INV-TRIG-018. Red case demonstrated: removing the handler's
    ``delivers_to_operator`` refusal fails this test."""
    import agent as agent_mod
    import tools
    ch, bot = _telegram()
    cm = ChannelManager()
    cm.register(ch)
    monkeypatch.setattr(tools, "_channel_manager", cm)
    for deliver, refused in (("operator", True), ("none", False)):
        msg = await _ingress(deliver)
        token = agent_mod.origin_var.set({
            "role": ROLE, "channel": "webhook",
            "chat_id": msg.context["chat_id"], "turn_scope": _scope_of(msg)})
        try:
            out = await tools.send_message.handler(
                {"message": "hello", "channel": "telegram"})
        finally:
            agent_mod.origin_var.reset(token)
        assert bool(out.get("is_error")) is refused
    assert _chats(bot) == [OPERATOR_CHAT]


# ---------------------------------------------------------------------------
# #1142 × #1121: an opted-in fire that stops at its turn limit tells the
# operator. Without this, a limit stop with nothing written is the very silent
# loss #1142 closes; a NON-opted-in fire keeps #1121's log-only route (rc11).
# ---------------------------------------------------------------------------

def _limit_result(sid: str) -> _SDKResultMessage:
    # Field-for-field the bundled CLI 2.1.273 error_max_turns variant.
    return _SDKResultMessage(
        subtype="error_max_turns", duration_ms=1000, duration_api_ms=900,
        is_error=True, num_turns=21, session_id=sid, stop_reason="tool_use",
        total_cost_usd=0.1, usage={"input_tokens": 1, "output_tokens": 1},
        result=None, errors=["Reached maximum number of turns (20)"],
        terminal_reason="max_turns",
    )


class _LimitClient(_ScriptedClient):
    async def receive_response(self):
        for item in self._script:
            yield item
        yield _limit_result(self._sid)


class _LimitFactory(_Factory):
    def __call__(self, options) -> _ScriptedClient:
        script = self._scripts.pop(0) if self._scripts else []
        c = _LimitClient(options, script, sid=f"sid-{len(self.clients) + 1}")
        self.clients.append(c)
        return c


@pytest.mark.asyncio
@pytest.mark.parametrize("written", [None, "Voicemail from Bob"])
async def test_an_opted_in_limit_stop_tells_the_operator(
        tmp_path, monkeypatch, written):
    agent, ch, bot = _resident(tmp_path)
    msg = await _ingress("operator")
    script = [] if written is None else [_mk_assistant(written)]
    await _run(agent, msg, _LimitFactory([script]), monkeypatch)
    assert set(_chats(bot)) == {str(OPERATOR_CHAT)}
    lines = [t for t in _texts(bot) if "step limit" in t]
    assert len(lines) == 1
    assert EFFECTIVE in lines[0]
    if written is not None:
        assert any(written in t for t in _texts(bot))


@pytest.mark.asyncio
async def test_a_non_opted_in_limit_stop_still_sends_nothing(
        tmp_path, monkeypatch):
    agent, ch, bot = _resident(tmp_path)
    msg = await _ingress("none")
    await _run(agent, msg, _LimitFactory([[]]), monkeypatch)
    assert bot.sent == []


# ---------------------------------------------------------------------------
# #1158 — `deliver: operator_always`: silence is not an outcome. Every accepted
# fire ends in exactly one operator message: the reply, a classified error,
# the limit-stop line, or — when the reply is silent or empty — Casa's fallback.
# ---------------------------------------------------------------------------

FALLBACK = f"The '{EFFECTIVE}' webhook fired; I had nothing to add."


def test_always_is_accepted_by_the_manifest():
    trig, errs = parse_and_validate("elevenlabs", _m(deliver="operator_always"))
    assert errs == []
    assert trig[0]["deliver"] == "operator_always"


@pytest.mark.asyncio
async def test_always_the_route_record_carries_it(tmp_path):
    registry, issues = await _reconcile(
        tmp_path, _plugin("operator_always"), acked=True)
    assert issues == []
    assert registry.webhook_route(EFFECTIVE)["deliver"] == "operator_always"


@pytest.mark.asyncio
async def test_always_ingress_stamps_it_with_a_line_that_never_invites_silence():
    import casa_core
    msg = await _ingress("operator_always")
    assert msg.context["_webhook_deliver"] == "operator_always"
    scope = _scope_of(msg)
    assert scope.delivers_to_operator is True
    assert scope.silence_forbidden is True
    assert msg.content.endswith("\n\n" + casa_core.WEBHOOK_DELIVER_ALWAYS_LINE)
    assert casa_core.WEBHOOK_DELIVER_LINE not in msg.content
    assert "<silent/>" not in casa_core.WEBHOOK_DELIVER_ALWAYS_LINE


@pytest.mark.asyncio
async def test_always_is_not_set_for_the_other_values():
    for deliver in ("operator", "none"):
        assert _scope_of(await _ingress(deliver)).silence_forbidden is False


@pytest.mark.asyncio
async def test_always_the_pending_consent_names_it(tmp_path, monkeypatch):
    import authz_grants
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    monkeypatch.setattr(authz_grants, "CHALLENGES",
                        authz_grants.ChallengeCoordinator())
    telegram = _FakeTelegram()
    await _reconcile(tmp_path, _plugin("operator_always"), acked=False,
                     telegram=telegram)
    for _ in range(8):
        await asyncio.sleep(0)
    assert len(telegram.posts) == 1
    assert "exactly one Telegram message" in telegram.posts[0][2]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["<silent/>", "  <silent/>\n<silent/> ", "", "   "])
async def test_always_a_silent_reply_becomes_one_fallback_message(
        tmp_path, monkeypatch, reply):
    """Pins INV-TRIG-021: a silent or empty reply still yields exactly one
    operator message, Casa's fallback naming the webhook."""
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("operator_always")
    script = [_mk_assistant(reply)] if reply else []
    await _run(agent, msg, _Factory([script]), monkeypatch)
    assert _chats(bot) == [OPERATOR_CHAT]
    assert _texts(bot) == [FALLBACK]


@pytest.mark.asyncio
async def test_always_a_real_reply_is_the_one_message(tmp_path, monkeypatch):
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("operator_always")
    await _run(agent, msg, _Factory([[_mk_assistant("Robocall, 12 s.")]]),
               monkeypatch)
    assert _chats(bot) == [OPERATOR_CHAT]
    assert len(bot.sent) == 1 and "Robocall, 12 s." in _texts(bot)[0]


@pytest.mark.asyncio
async def test_always_an_error_is_the_one_message(tmp_path, monkeypatch):
    from agent import _USER_MESSAGES
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("operator_always")
    await _run(agent, msg, _Factory([[RuntimeError("boom")]]), monkeypatch)
    assert _texts(bot) == [_USER_MESSAGES[ErrorKind.UNKNOWN]]


@pytest.mark.asyncio
async def test_always_retry_tainted_silence_is_the_error_not_the_fallback(
        tmp_path, monkeypatch):
    """A failed fire must not be reported as one with nothing to add."""
    from agent import _USER_MESSAGES
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("operator_always")
    factory = _Factory([[RuntimeError("rate limit exceeded")],
                        [_mk_assistant("<silent/>")]])
    await _run(agent, msg, factory, monkeypatch)
    assert _texts(bot) == [_USER_MESSAGES[ErrorKind.RATE_LIMIT]]


@pytest.mark.asyncio
async def test_always_a_silent_limit_stop_sends_only_the_limit_line(
        tmp_path, monkeypatch):
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("operator_always")
    await _run(agent, msg, _LimitFactory([[_mk_assistant("<silent/>")]]),
               monkeypatch)
    assert len(bot.sent) == 1
    assert "step limit" in _texts(bot)[0] and FALLBACK not in _texts(bot)


@pytest.mark.asyncio
async def test_always_offers_no_send_message(tmp_path, monkeypatch):
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("operator_always")
    results: list = []
    factory = _Factory([[_send_hook(results), _mk_assistant("<silent/>")]])
    await _run(agent, msg, factory, monkeypatch)
    opts = factory.clients[0].options
    assert SEND_TOOL not in opts.allowed_tools
    assert len(results) == 1 and results[0].get("is_error") is True
    assert _texts(bot) == [FALLBACK]


@pytest.mark.asyncio
async def test_plain_operator_silence_is_still_suppressed(tmp_path, monkeypatch):
    """Negative control: `deliver: operator` keeps INV-OUT-006's suppression."""
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("operator")
    await _run(agent, msg, _Factory([[_mk_assistant("<silent/>")]]), monkeypatch)
    assert bot.sent == []


def _error_result(sid: str) -> _SDKResultMessage:
    # The CLI's non-retryable execution failure, returned (not raised) by the
    # pool — diff r1 S2 (Astra): it must not read as "nothing to add".
    return _SDKResultMessage(
        subtype="error_during_execution", duration_ms=1000, duration_api_ms=900,
        is_error=True, num_turns=2, session_id=sid, stop_reason=None,
        total_cost_usd=0.1, usage={"input_tokens": 1, "output_tokens": 1},
        result=None)


class _ErrorClient(_ScriptedClient):
    async def receive_response(self):
        for item in self._script:
            yield item
        yield _error_result(self._sid)


class _ErrorFactory(_Factory):
    def __call__(self, options) -> _ScriptedClient:
        script = self._scripts.pop(0) if self._scripts else []
        c = _ErrorClient(options, script, sid=f"sid-{len(self.clients) + 1}")
        self.clients.append(c)
        return c


@pytest.mark.asyncio
@pytest.mark.parametrize("script", [[], ["<silent/>"]])
async def test_always_a_returned_error_result_is_the_error_not_the_fallback(
        tmp_path, monkeypatch, script):
    from agent import _USER_MESSAGES
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("operator_always")
    await _run(agent, msg,
               _ErrorFactory([[_mk_assistant(t) for t in script]]), monkeypatch)
    assert _chats(bot) == [OPERATOR_CHAT]
    assert _texts(bot) == [_USER_MESSAGES[ErrorKind.SDK_ERROR]]


@pytest.mark.asyncio
async def test_plain_operator_returned_error_result_is_unchanged(
        tmp_path, monkeypatch):
    """Negative control: other turn kinds keep today's handling."""
    agent, _ch, bot = _resident(tmp_path)
    msg = await _ingress("operator")
    await _run(agent, msg, _ErrorFactory([[_mk_assistant("<silent/>")]]),
               monkeypatch)
    assert bot.sent == []
