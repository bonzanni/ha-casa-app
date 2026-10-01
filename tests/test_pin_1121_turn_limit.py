"""#1121 / #1137 — a resident turn that stops at its turn limit.

Operator rulings (verbatim heads): #1121 "When a resident turn stops at its turn
limit, Casa says so, keeps what was already written, and does not retry." — for
scheduled-type turns refined to "the line instead names the task, says it
stopped at its step limit before finishing, and offers to redo it on request";
#1137 "raise the assistant's per-turn step limit to 80, and keep untrusted
webhook turns at 20."

Red cases for INV-TURN-014, specified by **astra** (drive redcase round, MODE:
SPECIFY, against ``ebd8cc3f45231a0137175de995e52eacccf1ea8f``). Every case drives
the REAL ``Agent.handle_message`` -> ``_process`` -> ``_make_on_message`` fold,
with a scripted SDK client behind ``sdk_client_pool._default_make_client`` —
which serves the pooled path and the per-turn bypass alike — ending on the CLI
2.1.273 ``error_max_turns`` result. Voice runs through the real transport
handlers with no socket. The line is matched by a stable fragment, never
verbatim: the rulings say "roughly".
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from claude_agent_sdk import ResultMessage

import agent as agent_mod
from bus import BusMessage, MessageBus, MessageType
from channels import DeliveryOutcome
from error_kinds import ErrorKind, _USER_MESSAGES
from ingress_identity import ingress_identity
from session_registry import SessionRegistry, build_scoped_session_key
from specialist_registry import DelegationComplete
from test_agent_process import (
    ScriptedToolClient, _make_agent, _make_agent_with_registry, _mk_assistant,
    _mk_tool_result, _mk_tool_use, patch_retry_sleep,
)
from test_buffered_closing_silence import SENT, _Hook, _resident, _send
from test_resume_fault_streak import _align_seeded_surface, _seeded_registry

pytestmark = [pytest.mark.asyncio]

CASA = Path(__file__).resolve().parent.parent / "casa" / "rootfs" / "opt" / "casa"
SID = "limit-sid"
OPERATOR = 4242
LINE = "step limit"


def _limit(sid: str = SID) -> ResultMessage:
    # Field-for-field the bundled CLI 2.1.273 error_max_turns variant.
    return ResultMessage(
        subtype="error_max_turns", duration_ms=313823, duration_api_ms=300000,
        is_error=True, num_turns=21, session_id=sid, stop_reason="tool_use",
        total_cost_usd=1.5, usage={"input_tokens": 1, "output_tokens": 1},
        result=None, errors=["Reached maximum number of turns (20)"],
        terminal_reason="max_turns",
    )


def _work(n: int = 3) -> list:
    s = []
    for i in range(n):
        s.append(_mk_tool_use(f"t{i}", "mcp__plugin_x__record_search", {}))
        s.append(_mk_tool_result(f"t{i}", is_error=False, text="ok"))
    return s


def _limit_script(text: str | None) -> list:
    s = _work()
    if text is not None:
        s.append(_mk_assistant(text))
    s += [_mk_tool_use("t9", "mcp__plugin_x__record_search", {}),
          _mk_tool_result("t9", is_error=False, text="ok"), _limit()]
    return s


def _sdk_error_attempt() -> list:
    # ConnectionError classifies as SDK_ERROR, which retry consumes.
    return _work(1) + [ConnectionError("broken")]


class _Client(ScriptedToolClient):
    """ScriptedToolClient that runs hooks inside the stream and records the
    options each client was built with. Nothing is appended after a script:
    the limit result is the terminal message."""

    options: list = []

    def __init__(self, options):
        _Client.options.append(options)
        super().__init__(options)

    async def receive_response(self):
        for item in self._script:
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, _Hook):
                await item.fn()
                continue
            yield item


def _reset(*scripts) -> None:
    _Client.options = []
    ScriptedToolClient.reset(*scripts)


class _Tg:
    """Telegram double. ``chat_id`` is the operator's DM (positive), so the
    operator-addressing seam resolves; ``send`` reports delivery."""

    name = "telegram"

    def __init__(self, stream: bool = True) -> None:
        self.chat_id = OPERATOR
        self.is_ready = True
        self._stream = stream
        self.send = AsyncMock(return_value=DeliveryOutcome.DELIVERED)
        self.send_response = AsyncMock(return_value=DeliveryOutcome.DELIVERED)
        self.finalize_stream = AsyncMock(return_value=DeliveryOutcome.DELIVERED)
        self.finalize_response_stream = AsyncMock(
            return_value=DeliveryOutcome.DELIVERED)
        self.turn_finished = AsyncMock()

    def create_on_token(self, _context):
        async def _on_token(_text: str) -> None:
            return None
        return _on_token

    def texts(self, method: str) -> list[str]:
        return [str(c.args[0]) for c in getattr(self, method).await_args_list]

    def lines(self) -> list[tuple[str, dict]]:
        return [(str(c.args[0]), c.args[1]) for c in self.send.await_args_list
                if LINE in str(c.args[0]).lower()]

    def all_calls(self) -> int:
        return sum(getattr(self, m).await_count for m in (
            "send", "send_response", "finalize_stream",
            "finalize_response_stream"))


def _limit_warnings(caplog, role: str, channel: str) -> list[str]:
    out = []
    for r in caplog.records:
        m = r.getMessage()
        if (r.levelno == logging.WARNING and "turn limit" in m.lower()
                and role in m and channel in m and "21" in m):
            out.append(m)
    return out


def _dm(chat: str = "123", trusted: bool = True, role: str = "assistant",
        text: str = "go and check now") -> BusMessage:
    m = BusMessage(type=MessageType.CHANNEL_IN, source="telegram", target=role,
                   content=text, channel="telegram",
                   context={"chat_id": chat})
    if trusted:
        m.trusted_user_origin = ingress_identity(
            "telegram", sender_id="9001", sender_is_operator=True)
    return m


def _scheduled(channel: str = "telegram", trigger: str = "heartbeat") -> BusMessage:
    return BusMessage(
        type=MessageType.SCHEDULED, source="scheduler", target="assistant",
        content="Run the check.", channel=channel,
        context={"chat_id": f"interval-{trigger}", "trigger": trigger,
                 "cid": "sched-1"},
    )


async def _run(agent, msg):
    with patch("sdk_client_pool._default_make_client", _Client), \
            patch_retry_sleep():
        return await agent.handle_message(msg)


# ---------------------------------------------------------------------------
# Telegram user turns (pooled)
# ---------------------------------------------------------------------------


async def test_rc1_dm_progress_stays_and_one_line_follows(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    agent = _make_agent(tmp_path, role="assistant")
    tg = _Tg()
    agent._channel_manager.register(tg)
    _reset(_limit_script("One new lead worth chasing..."))
    await _run(agent, _dm())
    assert tg.texts("finalize_response_stream") == ["One new lead worth chasing..."]
    assert tg.texts("finalize_stream") == []
    lines = tg.lines()
    assert len(lines) == 1 and tg.send.await_count == 1
    assert "continue" in lines[0][0].lower()
    assert lines[0][1].get("chat_id") == "123"       # the turn's own chat
    assert len(_limit_warnings(caplog, "assistant", "telegram")) == 1


async def test_rc2_dm_no_text_gets_the_line_alone(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    agent = _make_agent(tmp_path, role="assistant")
    tg = _Tg()
    agent._channel_manager.register(tg)
    _reset(_limit_script(None))
    await _run(agent, _dm())
    assert tg.texts("finalize_response_stream") == []
    assert tg.texts("finalize_stream") == []
    lines = tg.lines()
    assert len(lines) == 1 and tg.send.await_count == 1
    assert lines[0][1].get("chat_id") == "123"
    assert len(_limit_warnings(caplog, "assistant", "telegram")) == 1


async def test_rc3_retry_then_limit_is_not_an_error_and_not_a_strike(
        tmp_path, caplog):
    caplog.set_level(logging.INFO)
    reg = await _seeded_registry(tmp_path)
    agent = _make_agent_with_registry(reg, role="butler")
    tg = _Tg()
    agent._channel_manager.register(tg)
    _align_seeded_surface(reg, agent)
    _reset(_sdk_error_attempt(), _limit_script(None))
    await _run(agent, _dm(chat="fault-scope", role="butler"))
    assert len(ScriptedToolClient.instances) == 2      # the retry, no more
    assert _USER_MESSAGES[ErrorKind.SDK_ERROR] not in (
        tg.texts("finalize_stream") + tg.texts("send"))
    assert len(tg.lines()) == 1
    e = reg._data[build_scoped_session_key("telegram", "butler", "fault-scope")]
    assert e.get("sdk_session_id") == SID
    assert e.get("resume_fault_streak") is None
    assert e.get("resume_fault_sid") is None


# ---------------------------------------------------------------------------
# Scheduled turns (bypass) — ruling-1121-2's wording
# ---------------------------------------------------------------------------


async def test_rc4_scheduled_no_text_names_the_task_and_offers_a_redo(
        tmp_path, caplog):
    caplog.set_level(logging.INFO)
    agent = _make_agent(tmp_path, role="assistant")
    tg = _Tg()
    agent._channel_manager.register(tg)
    _reset(_limit_script(None))
    await _run(agent, _scheduled())
    lines = tg.lines()
    assert len(lines) == 1 and tg.send.await_count == 1
    assert "heartbeat" in lines[0][0] and "redo" in lines[0][0].lower()
    # the scheduled turn's own Telegram chat (its session label, which the
    # transport resolves to the operator DM) — not a rerouted address
    assert lines[0][1].get("chat_id") == "interval-heartbeat"
    assert tg.texts("send_response") == []
    e = agent._session_registry.get(build_scoped_session_key(
        "telegram", "assistant", "interval-heartbeat"))
    assert e is not None and e.get("sdk_session_id") == SID
    assert len(_limit_warnings(caplog, "assistant", "telegram")) == 1


async def test_rc5_scheduled_text_is_delivered_then_one_line(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    tg = _Tg()
    agent._channel_manager.register(tg)
    _reset(_limit_script("Two of five invoices filed."))
    await _run(agent, _scheduled(trigger="morning-briefing"))
    assert tg.texts("send_response") == ["Two of five invoices filed."]
    lines = tg.lines()
    assert len(lines) == 1 and "morning-briefing" in lines[0][0]
    assert lines[0][1].get("chat_id") == "interval-morning-briefing"


async def test_rc6_rule1_suppression_holds_and_the_line_still_goes(
        tmp_path, monkeypatch):
    agent, stub = await _resident(tmp_path)
    results: list = []
    _reset(_work(1) + [_send(results), _mk_assistant("Done."),
                       _mk_assistant("<silent/>"), _limit()])
    msg = BusMessage(type=MessageType.SCHEDULED, source="scheduler",
                     target=agent.config.role, content="Run the check.",
                     channel="telegram",
                     context={"chat_id": "bins-reminder", "cid": "sched-1",
                              "trigger": "bins-reminder"})
    try:
        await _run(agent, msg)
    finally:
        await agent.aclose()
    sends = [str(c.args[0]) for c in stub.send.await_args_list]
    assert len(sends) == 2
    assert sends[0] == SENT
    assert LINE in sends[1].lower()
    assert stub.final_texts() == []


# ---------------------------------------------------------------------------
# Narrations (durable announcement stays owed when only Casa's line went out)
# ---------------------------------------------------------------------------


def _narration(channel: str = "telegram", chat: str = "123") -> tuple:
    ack = AsyncMock()
    msg = BusMessage(
        type=MessageType.NOTIFICATION, source="finance", target="assistant",
        content=DelegationComplete(
            delegation_id="deleg-1", agent="finance", status="ok",
            text="Ledger reconciled.",
            origin={"role": "assistant", "channel": channel, "chat_id": chat,
                    "cid": "route-1", "user_text": "reconcile the ledger"},
        ),
        channel=channel,
        context={"chat_id": chat, "cid": "route-1", "delegation_id": "deleg-1"},
        on_delivery=ack,
    )
    return msg, ack


@pytest.mark.parametrize("text", ["<silent/>", None], ids=["sentinel", "no_text"])
async def test_rc7_narration_with_only_casas_line_stays_owed(tmp_path, text):
    agent = _make_agent(tmp_path, role="assistant")
    tg = _Tg()
    agent._channel_manager.register(tg)
    msg, ack = _narration()
    _reset(_limit_script(text))
    await _run(agent, msg)
    assert len(tg.lines()) == 1
    assert ack.await_count == 0


async def test_rc8_voice_origin_narration_line_reaches_the_operator_chat(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    tg = _Tg()
    agent._channel_manager.register(tg)
    msg, ack = _narration(channel="voice", chat="kitchen")
    _reset(_limit_script(None))
    await _run(agent, msg)
    lines = tg.lines()
    assert len(lines) == 1
    assert lines[0][1].get("chat_id") == OPERATOR
    assert ack.await_count == 0


# ---------------------------------------------------------------------------
# A Casa-started turn that STREAMS in the operator chat gets ONE line
# ---------------------------------------------------------------------------


async def test_rc9_streaming_plugin_setup_turn_gets_exactly_one_line(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    tg = _Tg()
    agent._channel_manager.register(tg)
    msg = BusMessage(
        type=MessageType.CHANNEL_IN, source="telegram", target="assistant",
        content="Set up the plugin.", channel="telegram",
        context={"chat_id": OPERATOR, "user_id": OPERATOR, "cid": "setup-1",
                 "synthetic": "plugin_setup", "setup_episode": "ep-1",
                 "plugin_setup_target": "quarterly-accounting"},
    )
    _reset(_limit_script("Connected the ledger; now the mailbox..."))
    with patch("plugin_setup_episodes.dispatch_still_owed", return_value=True):
        await _run(agent, msg)
    assert tg.texts("finalize_response_stream") == [
        "Connected the ledger; now the mailbox..."]
    lines = tg.lines()
    assert len(lines) == 1 and tg.send.await_count == 1
    assert "setup" in lines[0][0].lower()


# ---------------------------------------------------------------------------
# Webhook surfaces
# ---------------------------------------------------------------------------


async def test_rc10_trusted_invoke_body_is_text_then_the_line(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    tg = _Tg()
    agent._channel_manager.register(tg)
    msg = BusMessage(type=MessageType.REQUEST, source="webhook",
                     target="assistant", content="status?", channel="webhook",
                     context={"chat_id": "named-caller", "_origin_route": "invoke"},
                     trusted_user_origin=ingress_identity("invoke"))
    _reset(_limit_script("progress"))
    resp = await _run(agent, msg)
    body = str(resp.content)
    assert body.startswith("progress\n\n")
    assert LINE in body[len("progress\n\n"):].lower()
    assert tg.all_calls() == 0


async def _restricted_max_turns(agent, route: str | None) -> int:
    origin = {"_origin_route": route} if route else {}
    token = agent_mod.origin_var.set(origin)
    try:
        opts = await agent._build_options(
            channel="webhook", channel_key="k", is_fresh=True,
            resume_sid=None, user_text="x")
    finally:
        agent_mod.origin_var.reset(token)
    return opts.max_turns


async def test_rc11_untrusted_webhook_sends_nothing_and_runs_at_20(
        tmp_path, caplog):
    caplog.set_level(logging.INFO)
    agent = _make_agent(tmp_path, role="assistant")
    agent.config.tools.max_turns = 80
    tg = _Tg()
    agent._channel_manager.register(tg)
    msg = BusMessage(
        type=MessageType.SCHEDULED, source="webhook", target="assistant",
        content="Webhook 'x' triggered with payload: {}", channel="webhook",
        trusted_user_origin=ingress_identity("webhook_trigger", webhook_name="x",
                                             clearance="public"),
        context={"webhook_name": "x", "cid": "w-1",
                 "_origin_route": "webhook_trigger", "_origin_clearance": "public",
                 "chat_id": "0b6c1b1e-6d7e-4a43-9c55-6f1f0d3a2b11"},
    )
    _reset(_limit_script(None))
    await _run(agent, msg)
    assert tg.all_calls() == 0
    assert len(_limit_warnings(caplog, "assistant", "webhook")) == 1
    assert await _restricted_max_turns(agent, "webhook_trigger") == 20


async def test_rc12_scheduled_trigger_on_webhook_channel_tells_the_operator(
        tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    agent.config.tools.max_turns = 80
    tg = _Tg()
    agent._channel_manager.register(tg)
    _reset(_limit_script(None))
    await _run(agent, _scheduled(channel="webhook", trigger="nightly-sync"))
    lines = tg.lines()
    assert len(lines) == 1 and tg.send.await_count == 1
    assert "nightly-sync" in lines[0][0]
    assert lines[0][1].get("chat_id") == OPERATOR
    assert await _restricted_max_turns(agent, None) == 20


async def test_rc17_scheduled_voice_trigger_tells_the_operator(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    tg = _Tg()
    agent._channel_manager.register(tg)
    _reset(_limit_script(None))
    await _run(agent, _scheduled(channel="voice", trigger="evening-lights"))
    lines = tg.lines()
    assert len(lines) == 1 and "evening-lights" in lines[0][0]
    assert lines[0][1].get("chat_id") == OPERATOR


async def test_rc18_line_and_warning_survive_a_raising_delivery(
        tmp_path, caplog):
    caplog.set_level(logging.INFO)
    agent = _make_agent(tmp_path, role="assistant")
    tg = _Tg()
    tg.finalize_response_stream = AsyncMock(
        side_effect=RuntimeError("channel down"))
    agent._channel_manager.register(tg)
    _reset(_limit_script("progress"))
    with pytest.raises(RuntimeError, match="channel down"):
        await _run(agent, _dm())
    assert len(tg.lines()) == 1
    assert len(_limit_warnings(caplog, "assistant", "telegram")) == 1


# ---------------------------------------------------------------------------
# #1137 values
# ---------------------------------------------------------------------------


async def test_rc15_assistant_limit_is_80_with_its_mirror():
    # agent_loader._build_runtime_fields reads runtime.yaml's tools.max_turns
    # into cfg.tools.max_turns — the only reader of a resident's limit; the
    # role.yaml copy is the mirror the ruling keeps in step.
    runtime = yaml.safe_load(
        (CASA / "defaults/agents/assistant/runtime.yaml").read_text())
    role = yaml.safe_load(
        (CASA / "defaults/roles/resident/assistant/role.yaml").read_text())
    assert runtime["tools"]["max_turns"] == 80
    assert role["tools"]["max_turns"] == 80


# ---------------------------------------------------------------------------
# Voice — the real transport handlers, no socket
# ---------------------------------------------------------------------------

from channels.voice import channel as vc_mod  # noqa: E402
from channels.voice.channel import VoiceChannel  # noqa: E402
from voice_auth_helpers import VOICE_TEST_SECRET  # noqa: E402


class _VoiceCfg:
    class tts:
        tag_dialect = "square_brackets"
    memory = type("M", (), {"token_budget": 800})()
    role = "butler"
    voice_errors: dict[str, str] = {}
    channels: list[str] = ["ha_voice"]


class _Mem:
    async def ensure_session(self, *a, **kw): return None
    async def get_context(self, *a, **kw): return ""
    async def add_turn(self, *a, **kw): return None
    async def profile(self, bank: str) -> str: return ""


class _RawWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, frame: dict, **_kw) -> None:
        self.sent.append(frame)


class _Req(dict):
    def __init__(self, body: bytes) -> None:
        super().__init__(cid="voice-cid")
        self._body = body

    async def read(self) -> bytes:
        return self._body


class _Resp:
    def __init__(self, *a, **kw) -> None:
        pass

    async def prepare(self, _request) -> None:
        return None


async def _voice_frames(tmp_path, transport: str, *scripts) -> list[dict]:
    """One utterance through the REAL handler and the REAL Agent behind a
    real bus. Frames are normalised to ``{"type", ...data}``."""
    bus = MessageBus()
    agent = _make_agent(tmp_path, role="butler")
    channel = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": _VoiceCfg()}, memory=_Mem(),
        idle_timeout=300,
    )
    agent._channel_manager.register(channel)
    bus.register("butler", agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop("butler"))
    _reset(*scripts)
    frames: list[dict] = []
    try:
        with patch("sdk_client_pool._default_make_client", _Client), \
                patch_retry_sleep():
            if transport == "ws":
                ws = _RawWs()
                await channel._run_ws_utterance(
                    ws, {"text": "status?", "agent_role": "butler",
                         "scope_id": "vs1"},
                    "u1", asyncio.get_running_loop().time() + 60.0)
                frames = [dict(f) for f in ws.sent]
            else:
                async def _rec(_resp, event, data):
                    frames.append({"type": event, **data})
                body = json.dumps({"prompt": "status?", "agent_role": "butler",
                                   "scope_id": "vs1"}).encode()
                with patch.object(channel, "_verify", return_value=True), \
                        patch.object(vc_mod, "_write_sse", _rec), \
                        patch.object(vc_mod.web, "StreamResponse", _Resp):
                    await channel._sse_handler(_Req(body))
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        await agent.aclose()
    return frames


TRANSPORTS = [pytest.param("ws", id="ws"), pytest.param("sse", id="sse")]


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_rc13_voice_speaks_the_held_tail_then_the_line(
        tmp_path, transport):
    frames = await _voice_frames(
        tmp_path, transport, _limit_script("First sentence. Held tail"))
    kinds = [f["type"] for f in frames]
    assert kinds[-1] == "done" and "error" not in kinds
    blocks = [f for f in frames if f["type"] == "block"]
    texts = [f["text"] for f in blocks]
    tail_at = next(i for i, t in enumerate(texts) if "Held tail" in t)
    assert len(texts) == tail_at + 2                 # the tail, then ONE line
    line = texts[-1]
    assert "step" in line.lower() and "disregard" not in line.lower()
    assert line[:1].isspace()                        # frames concatenate verbatim
    assert blocks[tail_at]["final"] is False and blocks[-1]["final"] is True


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_rc14_voice_zero_speech_after_retry_keeps_the_s1_line(
        tmp_path, transport):
    frames = await _voice_frames(
        tmp_path, transport, _sdk_error_attempt(), _limit_script(None))
    errors = [f for f in frames if f["type"] == "error"]
    assert [f["kind"] for f in errors] == ["empty_turn"]
    assert [f for f in frames if f["type"] == "done"] == []


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_rc16_voice_progress_only_then_limit_speaks_the_line(
        tmp_path, transport):
    async def _progress():
        await agent_mod.origin_var.get()["_progress_sink"](
            "One moment — checking.")
    frames = await _voice_frames(
        tmp_path, transport, [_Hook(_progress)] + _limit_script(None))
    kinds = [f["type"] for f in frames]
    assert kinds.count("error") == 0
    blocks = [f["text"] for f in frames if f["type"] == "block"]
    assert len(blocks) == 2
    assert "One moment" in blocks[0] and "step" in blocks[1].lower()
    assert kinds[-1] == "done"
