"""Tests for the `ask_user` casa-framework tool (v0.76.0 W5b, A:§2).

Two-turn, detached DM button-question lifecycle: validate -> provenance
gate -> REGISTER -> POST -> RETURN, the resident-tap single-owner finish
hook (edit-first -> dispatch -> overwrite-on-failure), the `dm:` record
lifecycle (supersession, TTL expiry, same-DM-text cancel, Casa shutdown,
restart-with-pending), the `/new` synchronous cancel+purge ordering, and
the assistant/specialist runtime.yaml grants.
"""
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from broker_helpers import deliver
import yaml

import verdict_broker
from authz_grants import GrantKey, GrantStore
from verdict_broker import VerdictBroker

REPO = Path(__file__).resolve().parents[1]
CASA = REPO / "casa" / "rootfs" / "opt" / "casa"
AGENTS = CASA / "defaults" / "agents"
ASK_TOOL = "mcp__casa-framework__ask_user"


# ---------------------------------------------------------------------------
# shared fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_broker(monkeypatch):
    """Isolate every test on its own VerdictBroker — tools.py resolves
    ``from verdict_broker import BROKER`` at call time, so redirecting the
    module attribute here is picked up transparently."""
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _fresh_grants(monkeypatch):
    import authz_grants
    fresh = GrantStore()
    monkeypatch.setattr(authz_grants, "GRANTS", fresh)
    return fresh


class _FakeChannel:
    """Minimal telegram-channel double for ask_user: records post/edit/
    dispatch calls, configurable to fail post or dispatch."""

    def __init__(self, *, post_result=42, post_raises=None, dispatch_result=True):
        self.calls: list[tuple] = []
        self.edits: list[tuple] = []
        self._post_result = post_result
        self._post_raises = post_raises
        self._dispatch_result = dispatch_result

    async def post_dm_keyboard(
        self, *, chat_id, request_id, text, options, short_labels=False,
    ):
        self.calls.append(("post", chat_id, request_id, text, tuple(options)))
        if self._post_raises is not None:
            raise self._post_raises
        return self._post_result

    async def edit_dm_message(self, chat_id, message_id, text):
        entry = (chat_id, message_id, text)
        self.edits.append(entry)
        self.calls.append(("edit",) + entry)  # unified timeline w/ post/dispatch
        return True

    async def _dispatch_button_continuation(
        self, *, chat_id, user_id, target_role, request_id, text,
    ):
        self.calls.append(
            ("dispatch", chat_id, user_id, target_role, request_id, text),
        )
        return self._dispatch_result


def _mk_cm(channel):
    cm = MagicMock()
    cm.get = MagicMock(return_value=channel)
    return cm


def _payload(res):
    return json.loads(res["content"][0]["text"])


def _set_origin(agent_mod, **overrides):
    origin = {
        "role": "assistant",
        "channel": "telegram",
        "chat_id": "500",
        "user_id": 999,
        "message_type": "channel_in",
        "source": "telegram",
        "execution_role": "assistant",
    }
    origin.update(overrides)
    return agent_mod.origin_var.set(origin)


async def _ask(monkeypatch, *, channel=None, args=None, origin_overrides=None,
               init=True, no_channel_manager=False, tools_mod=None):
    import agent as agent_mod
    if tools_mod is None:
        import tools as tools_mod
    if channel is None:
        channel = _FakeChannel()
    if init:
        tools_mod.init_tools(
            channel_manager=(None if no_channel_manager else _mk_cm(channel)),
            bus=MagicMock(), specialist_registry=MagicMock(),
            mcp_registry=MagicMock(),
        )
    tok = _set_origin(agent_mod, **(origin_overrides or {}))
    try:
        result = await tools_mod.ask_user.handler(
            args if args is not None else
            {"question": "Proceed?", "options": ["Yes", "No"]},
        )
    finally:
        agent_mod.origin_var.reset(tok)
    return result, _payload(result), channel


# ---------------------------------------------------------------------------
# 1. exposed schema
# ---------------------------------------------------------------------------


class TestAskUserSchema:
    def test_required_is_question_and_options(self):
        import tools
        schema = tools.ask_user.input_schema
        assert schema["type"] == "object"
        assert schema["required"] == ["question", "options"]
        assert set(schema["properties"]) == {"question", "options", "timeout_s"}
        assert schema["properties"]["timeout_s"]["type"] == "number"

    def test_exposed_via_mcp_envelope_tool_schema(self):
        """The real MCP-bridge conversion path (mirrors v0.75's
        engage_executor exposed-schema coverage)."""
        from mcp_envelope import _tool_schema
        import tools
        schema = _tool_schema(tools.ask_user)
        assert schema["name"] == "ask_user"
        assert schema["inputSchema"]["required"] == ["question", "options"]

    def test_exposed_via_create_casa_tools_server(self):
        """The real init_tools/SDK-server harness: create_casa_tools()
        pre-computes each tool's wire schema at construction time."""
        import tools
        server_cfg = tools.create_casa_tools()
        # SdkMcpTool.input_schema (already an explicit JSON Schema dict with
        # type+properties) is exposed as-is by the SDK's _build_schema — see
        # claude_agent_sdk/__init__.py — so this is equivalent to, and
        # cross-checks, the direct .input_schema assertion above.
        assert server_cfg.get("instance") is not None
        found = next(t for t in tools.CASA_TOOLS if t.name == "ask_user")
        assert found.input_schema["required"] == ["question", "options"]


# ---------------------------------------------------------------------------
# 2. validation table
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize("args", [
        {"question": "", "options": ["a", "b"]},
        {"question": "   ", "options": ["a", "b"]},
        {"question": "x" * 1025, "options": ["a", "b"]},
        {"question": "q", "options": []},
        {"question": "q", "options": ["only-one"]},
        {"question": "q", "options": [f"o{i}" for i in range(9)]},
        {"question": "q", "options": ["a", "a"]},
        {"question": "q", "options": ["a", ""]},
        {"question": "q", "options": ["a", "x" * 49]},
        {"question": "q", "options": ["a", 5]},
        {"question": "q", "options": "not-a-list"},
        {"question": "q", "options": ["a", "b"], "timeout_s": "300"},
        {"question": "q", "options": ["a", "b"], "timeout_s": True},
    ])
    async def test_invalid_args_rejected(self, monkeypatch, args, _fresh_broker):
        _res, payload, channel = await _ask(monkeypatch, args=args)
        assert payload["status"] == "error"
        assert payload["kind"] == "invalid_arguments"
        assert channel.calls == []
        assert _fresh_broker.pending(namespace="resident_ask", scope="dm:500") == []

    async def test_valid_minimal_args_accepted(self, monkeypatch):
        _res, payload, _ch = await _ask(
            monkeypatch, args={"question": "Proceed?", "options": ["Yes", "No"]},
        )
        assert payload["status"] == "awaiting_user"
        assert "request_id" in payload

    @pytest.mark.parametrize("timeout_s,expected", [
        (None, 300.0),
        (10, 30.0),      # clamped up to the floor
        (5000, 570.0),   # clamped down to the ceiling
        (300, 300.0),    # within range, unchanged
        (30, 30.0),      # exact floor
        (570, 570.0),    # exact ceiling
    ])
    async def test_timeout_clamped_on_registered_ttl(
        self, monkeypatch, _fresh_broker, timeout_s, expected,
    ):
        captured = {}
        orig_register = _fresh_broker.register

        def _spy(**kw):
            req, created = orig_register(**kw)
            captured["req"] = req
            return req, created

        monkeypatch.setattr(_fresh_broker, "register", _spy)

        args = {"question": "Proceed?", "options": ["Yes", "No"]}
        if timeout_s is not None:
            args["timeout_s"] = timeout_s
        _res, payload, _ch = await _ask(monkeypatch, args=args)

        assert payload["status"] == "awaiting_user"
        assert captured["req"].timeout_s == expected


# ---------------------------------------------------------------------------
# 3. provenance gate cross-product
# ---------------------------------------------------------------------------


class TestProvenanceGate:
    async def test_dm_direct_passes(self, monkeypatch):
        _res, payload, _ch = await _ask(monkeypatch)
        assert payload["status"] == "awaiting_user"

    async def test_button_direct_passes(self, monkeypatch):
        _res, payload, _ch = await _ask(
            monkeypatch, origin_overrides={"synthetic": "button"},
        )
        assert payload["status"] == "awaiting_user"

    async def test_dm_delegated_rejected(self, monkeypatch, _fresh_broker):
        _res, payload, ch = await _ask(
            monkeypatch, origin_overrides={"execution_role": "finance"},
        )
        assert payload["status"] == "error"
        assert payload["kind"] == "unsupported_origin"
        assert ch.calls == []
        assert _fresh_broker.pending(namespace="resident_ask", scope="dm:500") == []

    async def test_button_delegated_rejected(self, monkeypatch, _fresh_broker):
        _res, payload, ch = await _ask(
            monkeypatch, origin_overrides={
                "synthetic": "button", "execution_role": "finance",
            },
        )
        assert payload["status"] == "error"
        assert payload["kind"] == "unsupported_origin"
        assert ch.calls == []

    async def test_dm_engagement_rejected(self, monkeypatch, _fresh_broker):
        import tools as tools_mod
        etok = tools_mod.engagement_var.set(types.SimpleNamespace(id="eng-1"))
        try:
            _res, payload, ch = await _ask(monkeypatch)
        finally:
            tools_mod.engagement_var.reset(etok)
        assert payload["status"] == "error"
        assert payload["kind"] == "unsupported_origin"
        assert ch.calls == []

    async def test_other_transport_rejected_wrong_channel(self, monkeypatch, _fresh_broker):
        _res, payload, ch = await _ask(
            monkeypatch, origin_overrides={"channel": "voice", "source": "voice"},
        )
        assert payload["status"] == "error"
        assert payload["kind"] == "unsupported_origin"
        assert ch.calls == []

    async def test_other_transport_rejected_wrong_message_type(self, monkeypatch, _fresh_broker):
        _res, payload, ch = await _ask(
            monkeypatch, origin_overrides={"message_type": "webhook_in"},
        )
        assert payload["status"] == "error"
        assert payload["kind"] == "unsupported_origin"
        assert ch.calls == []

    async def test_other_transport_rejected_malformed_ids(self, monkeypatch, _fresh_broker):
        _res, payload, ch = await _ask(
            monkeypatch, origin_overrides={"chat_id": "not-a-number"},
        )
        assert payload["status"] == "error"
        assert payload["kind"] == "unsupported_origin"
        assert ch.calls == []


# ---------------------------------------------------------------------------
# 4. REGISTER -> POST -> RETURN event order + detached survival
# ---------------------------------------------------------------------------


class TestRegisterPostReturnOrder:
    async def test_event_order(self, monkeypatch, _fresh_broker):
        order: list[str] = []
        orig_register = _fresh_broker.register

        def _spy_register(**kw):
            order.append("register")
            return orig_register(**kw)

        monkeypatch.setattr(_fresh_broker, "register", _spy_register)

        channel = _FakeChannel()
        orig_post = channel.post_dm_keyboard

        async def _spy_post(**kw):
            order.append("post")
            return await orig_post(**kw)

        channel.post_dm_keyboard = _spy_post

        _res, payload, _ch = await _ask(monkeypatch, channel=channel)
        order.append("return")

        assert order == ["register", "post", "return"]
        assert payload["status"] == "awaiting_user"

    async def test_detached_survival(self, monkeypatch, _fresh_broker):
        _res, payload, _ch = await _ask(monkeypatch)
        rid = payload["request_id"]
        assert rid in _fresh_broker.pending(namespace="resident_ask", scope="dm:500")


# ---------------------------------------------------------------------------
# 5. supersession
# ---------------------------------------------------------------------------


class TestSupersession:
    async def test_second_ask_expires_first_authz_untouched(
        self, monkeypatch, _fresh_broker,
    ):
        # Seed a sibling authz: record for the SAME chat — disjoint scope.
        authz_req, created = _fresh_broker.register(
            namespace="resident_ask", scope="authz:500", request_id="authz-1",
            timeout_s=120.0, meta={"kind": "authz", "chat_id": 500},
        )
        assert created is True

        _res1, payload1, _ch1 = await _ask(monkeypatch)
        rid1 = payload1["request_id"]

        _res2, payload2, _ch2 = await _ask(monkeypatch)
        rid2 = payload2["request_id"]

        assert rid1 != rid2

        outcome1 = await asyncio.wait_for(
            _fresh_broker.await_result(
                _first_req_stub(_fresh_broker, "dm:500", rid1),
            ),
            0.5,
        )
        assert outcome1 == {"outcome": "cancelled", "reason": "superseded"}

        # second ask is still live
        assert rid2 in _fresh_broker.pending(namespace="resident_ask", scope="dm:500")

        # authz: sibling record is completely untouched — still live, meta unchanged.
        assert "authz-1" in _fresh_broker.pending(
            namespace="resident_ask", scope="authz:500",
        )
        assert _fresh_broker.get_meta(
            namespace="resident_ask", scope="authz:500", request_id="authz-1",
        ) == {"kind": "authz", "chat_id": 500}
        assert authz_req is not None  # keeps the seeded req referenced for clarity


def _first_req_stub(broker, scope, rid):
    """Helper: fetch the (now-retired) PendingRequest-shaped tombstone via a
    fresh register() call, which reattaches to the retired outcome (the
    documented tombstone-reattach contract)."""
    req, _created = broker.register(
        namespace="resident_ask", scope=scope, request_id=rid, timeout_s=5.0,
    )
    return req


# ---------------------------------------------------------------------------
# 6. post failure -> delivery_failed
# ---------------------------------------------------------------------------


class TestPostFailure:
    async def test_post_returns_none(self, monkeypatch, _fresh_broker):
        channel = _FakeChannel(post_result=None)
        _res, payload, _ch = await _ask(monkeypatch, channel=channel)
        assert payload["status"] == "error"
        assert payload["kind"] == "delivery_failed"
        assert _fresh_broker.pending(namespace="resident_ask", scope="dm:500") == []

    async def test_post_raises(self, monkeypatch, _fresh_broker):
        channel = _FakeChannel(post_raises=RuntimeError("boom"))
        _res, payload, _ch = await _ask(monkeypatch, channel=channel)
        assert payload["status"] == "error"
        assert payload["kind"] == "delivery_failed"
        assert _fresh_broker.pending(namespace="resident_ask", scope="dm:500") == []

    async def test_no_channel_manager(self, monkeypatch, _fresh_broker):
        _res, payload, _ch = await _ask(monkeypatch, no_channel_manager=True)
        assert payload["status"] == "error"
        assert payload["kind"] == "delivery_failed"

    async def test_channel_not_found(self, monkeypatch, _fresh_broker):
        cm = MagicMock()
        cm.get = MagicMock(return_value=None)
        import agent as agent_mod
        import tools as tools_mod
        tools_mod.init_tools(
            channel_manager=cm, bus=MagicMock(), specialist_registry=MagicMock(),
            mcp_registry=MagicMock(),
        )
        tok = _set_origin(agent_mod)
        try:
            result = await tools_mod.ask_user.handler(
                {"question": "q", "options": ["a", "b"]},
            )
        finally:
            agent_mod.origin_var.reset(tok)
        payload = _payload(result)
        assert payload["status"] == "error"
        assert payload["kind"] == "delivery_failed"


# ---------------------------------------------------------------------------
# 7. finish-hook shape (answered / expired / dispatch-failure)
# ---------------------------------------------------------------------------


class TestFinishHookShape:
    async def test_answered_edits_then_dispatches(self, monkeypatch, _fresh_broker):
        channel = _FakeChannel(dispatch_result=True)
        _res, payload, ch = await _ask(monkeypatch, channel=channel)
        rid = payload["request_id"]

        assert deliver(_fresh_broker, 
            namespace="resident_ask", scope="dm:500", request_id=rid,
            option_index=0, actor_id=999,
        ) == "delivered"
        await _settle(lambda: len(ch.edits) >= 1 and any(
            c[0] == "dispatch" for c in ch.calls
        ))

        assert ch.edits[0][2].endswith("Answered: Yes")
        dispatch_call = next(c for c in ch.calls if c[0] == "dispatch")
        assert dispatch_call[1] == 500          # chat_id
        assert dispatch_call[2] == 999           # user_id
        assert dispatch_call[3] == "assistant"   # target_role
        assert dispatch_call[4] == rid
        assert dispatch_call[5] == f"[button answer to {rid}]: Yes"
        # edit-first contract: the "Answered: Yes" edit precedes the dispatch
        # in the unified post/edit/dispatch timeline.
        answered_edit = next(
            c for c in ch.calls if c[0] == "edit" and c[3].endswith("Answered: Yes")
        )
        assert ch.calls.index(answered_edit) < ch.calls.index(dispatch_call)
        # no failure overwrite
        assert not any("delivery failed" in e[2] for e in ch.edits)

    async def test_dispatch_failure_overwrites(self, monkeypatch, _fresh_broker):
        channel = _FakeChannel(dispatch_result=False)
        _res, payload, ch = await _ask(monkeypatch, channel=channel)
        rid = payload["request_id"]

        deliver(_fresh_broker, 
            namespace="resident_ask", scope="dm:500", request_id=rid,
            option_index=1, actor_id=999,
        )
        await _settle(lambda: len(ch.edits) >= 2)

        assert ch.edits[0][2].endswith("Answered: No")
        assert "answer received but delivery failed — please type it" in ch.edits[1][2]

    async def test_no_answer_edits_expired(self, monkeypatch, _fresh_broker):
        """Any non-"answered" terminal outcome (timeout's `no_answer`, or —
        exercised directly here via `cancel()` to avoid a real TTL wait —
        `cancelled`) takes the SAME expired-edit branch. TTL-driven timing
        itself is covered separately by TestTTLExpiry (with a lowered floor)."""
        channel = _FakeChannel()
        _res, payload, ch = await _ask(monkeypatch, channel=channel)
        rid = payload["request_id"]
        assert _fresh_broker.cancel(
            namespace="resident_ask", scope="dm:500", request_id=rid,
            reason="test",
        )
        await _settle(lambda: len(ch.edits) >= 1)
        assert "expired" in ch.edits[0][2].lower()
        assert not any(c[0] == "dispatch" for c in ch.calls)


    @pytest.mark.parametrize("reason", [
        "new_session", "unrecognised_red_case_reason",
    ])
    async def test_a_cancelled_question_does_not_claim_it_expired(
        self, monkeypatch, _fresh_broker, reason,
    ):
        """#933 — the human ask's twin of the scheduled pin.

        Nothing dispatches a continuation on this path, so the edit is the ONLY
        thing the operator is ever told about why the keyboard went away. A
        `/new` is not an expiry and must not say it was one.

        The second row is the one a `new_session` special-case would not
        satisfy: a reason no renderer has heard of still has to fall back to a
        truthful generic cancellation rather than to the expiry text.
        """
        channel = _FakeChannel()
        _res, payload, ch = await _ask(monkeypatch, channel=channel)
        assert _fresh_broker.cancel_scope(
            namespace="resident_ask", scope="dm:500", reason=reason,
        ) == 1
        await _settle(lambda: len(ch.edits) >= 1)

        assert len(ch.edits) == 1
        assert sum(c[0] == "dispatch" for c in ch.calls) == 0
        suffix = ch.edits[0][2].casefold()
        assert sum("expired" in t for t in [suffix]) == 0
        assert sum("cancel" in t for t in [suffix]) == 1


async def _settle(pred, tries=2000):
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never reached")


# ---------------------------------------------------------------------------
# 7b. R3b — readable DM buttons (full numbered options in the body, short
#     number-prefixed button labels, full chosen option on settle)
# ---------------------------------------------------------------------------


class TestDmReadableButtons:
    async def test_dm_body_has_full_numbered_options(
        self, monkeypatch, _fresh_broker,
    ):
        """The DM MESSAGE body renders the FULL options verbatim + 1-based
        numbered (render_ask_body), not just the raw question — so a long
        option is readable even though the button label is short."""
        channel = _FakeChannel()
        _res, _payload_, ch = await _ask(
            monkeypatch, channel=channel,
            args={"question": "Which account?",
                  "options": ["Personal Gmail", "Work Outlook"]},
        )
        post = next(c for c in ch.calls if c[0] == "post")
        text = post[3]
        assert text == "Which account?\n\n1. Personal Gmail\n2. Work Outlook"
        assert "1. Personal Gmail" in text
        assert "2. Work Outlook" in text

    async def test_dm_buttons_short_labeled_with_index(self):
        """v0.84.0 (round 4, spec D2): ``post_dm_keyboard`` has no per-option
        agent ``short`` to offer the whole-set resolver (``options`` is a
        plain label list) — until the deferred Haiku label-generation
        fallback ships, this call site ALWAYS floors to the numbered
        placeholder (``resolve_button_labels``' ``short_option_labels()`` wrapper
        keeps its name/signature); the body carries the full options, and the
        callback_data identity stays ``v1|resident_ask|<rid>|<i>`` (unchanged)."""
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        bot = MagicMock()
        bot.send_message = AsyncMock(
            return_value=types.SimpleNamespace(message_id=9))
        ch._bot = bot  # ``bot`` is a read-only property backed by ``_bot``

        options = ["Personal Gmail", "Configure the enterprise SSO integration"]
        body = ("Which account?\n\n1. Personal Gmail\n"
                "2. Configure the enterprise SSO integration")
        mid = await ch.post_dm_keyboard(
            chat_id=500, request_id="rid", text=body, options=options,
            short_labels=True,
        )
        assert mid == 9
        kbd = bot.send_message.call_args.kwargs["reply_markup"]
        rows = kbd.inline_keyboard
        # v0.84.0 (D2): no agent shorts anywhere → the WHOLE set floors to the
        # numbered placeholder (never a garbled per-option elision).
        assert [r[0].text for r in rows] == ["Option 1", "Option 2"]
        assert [r[0].callback_data for r in rows] == [
            "v1|resident_ask|rid|0", "v1|resident_ask|rid|1"]
        # The full body is posted verbatim (short labels never replace it).
        assert bot.send_message.call_args.kwargs["text"] == body

    async def test_dm_settle_shows_full_chosen_option(
        self, monkeypatch, _fresh_broker,
    ):
        """On answer the DM settle edit shows the FULL chosen option (not the
        short button label), appended below the full numbered body."""
        channel = _FakeChannel(dispatch_result=True)
        _res, payload, ch = await _ask(
            monkeypatch, channel=channel,
            args={"question": "Which account?",
                  "options": ["Personal Gmail", "Work Outlook"]},
        )
        rid = payload["request_id"]
        assert deliver(_fresh_broker, 
            namespace="resident_ask", scope="dm:500", request_id=rid,
            option_index=0, actor_id=999,
        ) == "delivered"
        await _settle(lambda: len(ch.edits) >= 1 and any(
            c[0] == "dispatch" for c in ch.calls))
        settle_text = ch.edits[0][2]
        assert settle_text.endswith("Answered: Personal Gmail")
        # The full numbered body is preserved below the settle line.
        assert "1. Personal Gmail" in settle_text
        assert "2. Work Outlook" in settle_text


# ---------------------------------------------------------------------------
# 8. TTL expiry (real finish hook, short timeout via monkeypatched floor)
# ---------------------------------------------------------------------------


class TestTTLExpiry:
    async def test_ttl_expiry_edits_keyboard_to_expired(self, monkeypatch, _fresh_broker):
        import tools as tools_mod
        # The W5 clamp floor is 30s — too slow for a unit test. Lower the
        # floor for the duration of this test so a short timeout_s survives
        # the clamp unchanged, exercising the REAL finish hook end to end.
        monkeypatch.setattr(tools_mod, "_ASK_TIMEOUT_MIN", 0.05)

        channel = _FakeChannel()
        _res, payload, ch = await _ask(
            monkeypatch, channel=channel, tools_mod=tools_mod,
            args={"question": "Proceed?", "options": ["Yes", "No"], "timeout_s": 0.05},
        )
        rid = payload["request_id"]

        await asyncio.wait_for(
            _fresh_broker.await_result(_first_req_stub(_fresh_broker, "dm:500", rid)),
            2.0,
        )
        await _settle(lambda: len(ch.edits) >= 1)
        assert "expired" in ch.edits[0][2].lower()


# ---------------------------------------------------------------------------
# 9. same-DM text cancel (channels/telegram.py _handle)
# ---------------------------------------------------------------------------


class _FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=1)

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)


def _fake_update(chat_id, text, user_id=999):
    user = types.SimpleNamespace(first_name="Nicola", id=user_id)
    message = types.SimpleNamespace(text=text, message_id=7)
    chat = types.SimpleNamespace(id=chat_id)
    return types.SimpleNamespace(
        message=message, effective_chat=chat, effective_user=user,
    )


async def _drain_bus(bus, target="assistant"):
    from bus import BusMessage  # noqa: F401 — for type clarity only
    q = bus.queues.get(target)
    if q is None:
        return []
    out = []
    while not q.empty():
        _prio, _seq, msg = q.get_nowait()
        out.append(msg)
        q.task_done()
    return out


class TestSameDmTextCancel:
    async def test_typed_reply_cancels_dm_and_still_dispatches(
        self, monkeypatch, _fresh_broker,
    ):
        from bus import MessageBus
        from channels.telegram import TelegramChannel

        async def _noop(_msg):
            return None

        bus = MessageBus()
        bus.register("assistant", _noop)
        bot = _FakeBot()
        ch = TelegramChannel(bot_token="T", chat_id="0", default_agent="assistant", bus=bus)
        ch._start_typing = lambda *a, **k: None
        ch._app = types.SimpleNamespace(bot=bot)

        req, created = _fresh_broker.register(
            namespace="resident_ask", scope="dm:42", request_id="rid-typed",
            timeout_s=30.0, meta={
                "options": ["Yes", "No"], "chat_id": 42, "operator_id": 999,
                "_scope": "dm:42",
            },
        )
        assert created is True

        events: list[str] = []

        async def _finish(outcome):
            events.append(outcome["outcome"])
            await ch.edit_dm_message(42, 5, "This question has expired.")

        _fresh_broker.set_finish_hook(req, _finish)

        await ch._handle(_fake_update("42", "No, actually do it now"), None)

        outcome = await asyncio.wait_for(
            _fresh_broker.await_result(_first_req_stub(_fresh_broker, "dm:42", "rid-typed")),
            0.5,
        )
        assert outcome == {"outcome": "cancelled", "reason": "typed_answer"}
        await _settle(lambda: len(bot.edited) >= 1)
        assert "expired" in bot.edited[0]["text"].lower()

        queued = await _drain_bus(bus)
        assert len(queued) == 1
        assert queued[0].content == "No, actually do it now"

    async def test_new_command_not_treated_as_typed_answer(
        self, monkeypatch, _fresh_broker,
    ):
        """/new is intercepted before the same-DM-text cancel hook and takes
        its own (new_session-reason) cancel path — not typed_answer."""
        from bus import MessageBus
        from channels.telegram import TelegramChannel

        async def _noop(_msg):
            return None

        bus = MessageBus()
        bus.register("assistant", _noop)
        bot = _FakeBot()
        ch = TelegramChannel(bot_token="T", chat_id="0", default_agent="assistant", bus=bus)
        ch._app = types.SimpleNamespace(bot=bot)

        req, _created = _fresh_broker.register(
            namespace="resident_ask", scope="dm:42", request_id="rid-new",
            timeout_s=30.0, meta={"options": ["Yes", "No"], "chat_id": 42,
                                   "operator_id": 999, "_scope": "dm:42"},
        )
        events = []

        async def _finish(outcome):
            events.append(outcome)

        _fresh_broker.set_finish_hook(req, _finish)

        await ch._handle(_fake_update("42", "/new"), None)

        outcome = await asyncio.wait_for(
            _fresh_broker.await_result(_first_req_stub(_fresh_broker, "dm:42", "rid-new")),
            0.5,
        )
        assert outcome == {"outcome": "cancelled", "reason": "new_session"}


# ---------------------------------------------------------------------------
# 10. Casa shutdown drain (extends TestBrokerShutdownOrdering — see
#     tests/test_casa_core_helpers.py for the primary shutdown-ordering
#     assertions; this test drives the REAL ask_user-built finish hook).
# ---------------------------------------------------------------------------


class TestShutdownDrainsRealAskFinishHook:
    async def test_pending_ask_keyboard_edited_before_channel_stop(
        self, monkeypatch, _fresh_broker,
    ):
        from casa_core import _drain_broker_before_channel_shutdown

        channel = _FakeChannel()
        _res, payload, ch = await _ask(monkeypatch, channel=channel)
        assert payload["status"] == "awaiting_user"

        order: list[str] = []
        cm = MagicMock()

        async def _stop_all():
            order.append("stop_all")

        cm.stop_all = AsyncMock(side_effect=_stop_all)

        orig_edit = ch.edit_dm_message

        async def _spy_edit(*a, **kw):
            order.append("edit")
            return await orig_edit(*a, **kw)

        ch.edit_dm_message = _spy_edit

        await _drain_broker_before_channel_shutdown(cm)

        assert order == ["edit", "stop_all"]
        assert "expired" in ch.edits[0][2].lower()


# ---------------------------------------------------------------------------
# 11. restart-with-pending stale toast (distinct from the in-process
#     timeout tombstone path — see test_telegram_inline_callback.py's
#     test_stale_after_timeout_expired for the tombstone-claim("stale")
#     path this contrasts with).
# ---------------------------------------------------------------------------


class TestRestartStaleToast:
    async def test_fresh_process_broker_unknown_key_is_expired_via_no_meta(
        self, _fresh_broker,
    ):
        """Simulates a Casa restart: the per-test `_fresh_broker` fixture
        already IS a brand-new VerdictBroker (no _live, no _retired — the
        old process's in-memory state is entirely gone) — a tap on the
        orphaned keyboard's old request_id lands here. get_meta returns None
        immediately (the `meta is None` early-return branch) — NOT the
        claim()-returns-"stale" tombstone path an in-process timeout takes
        (see test_telegram_inline_callback.py::test_stale_after_timeout_expired
        for that contrasting path, which DOES have a retired-tombstone
        `get_meta` hit before `claim()` returns "stale")."""
        from channels.telegram import TelegramChannel

        bot = _FakeBot()
        ch = TelegramChannel(bot=bot, chat_id=100)
        cq = types.SimpleNamespace(
            id="cq1", data="v1|resident_ask|old-rid-from-before-restart|0",
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=42)),
            answer=AsyncMock(return_value=None),
            from_user=types.SimpleNamespace(id=999),
        )
        update = types.SimpleNamespace(callback_query=cq)

        # No registration at all on this fresh broker for this rid — neither
        # _live nor _retired — proving this is genuinely a meta-is-None
        # path, distinct from a tombstoned/expired entry.
        assert _fresh_broker.get_meta(
            namespace="resident_ask", scope="dm:42",
            request_id="old-rid-from-before-restart",
        ) is None

        await ch._on_inline_callback(update, context=None)
        cq.answer.assert_awaited_once_with("expired")


# ---------------------------------------------------------------------------
# 12. /new synchronous cancel + purge ordering
# ---------------------------------------------------------------------------


class TestNewCommandOrdering:
    async def test_new_cancels_both_scopes_and_purges_grant_before_first_await(
        self, monkeypatch, _fresh_broker, _fresh_grants,
    ):
        from channels.telegram import TelegramChannel

        order: list[str] = []

        orig_cancel_scope = _fresh_broker.cancel_scope

        def _spy_cancel_scope(*, namespace, scope, reason):
            order.append(f"cancel:{scope}:{reason}")
            return orig_cancel_scope(namespace=namespace, scope=scope, reason=reason)

        monkeypatch.setattr(_fresh_broker, "cancel_scope", _spy_cancel_scope)

        orig_purge_chat = _fresh_grants.purge_chat

        def _spy_purge_chat(chat_id):
            order.append(f"purge_chat:{chat_id}")
            return orig_purge_chat(chat_id)

        monkeypatch.setattr(_fresh_grants, "purge_chat", _spy_purge_chat)

        # Seed both scopes + a REAL int-keyed grant.
        _fresh_broker.register(
            namespace="resident_ask", scope="dm:42", request_id="r1", timeout_s=30.0,
        )
        _fresh_broker.register(
            namespace="resident_ask", scope="authz:42", request_id="r2", timeout_s=30.0,
        )
        key = GrantKey(
            operator_id=999, chat_id=42, enforcement_role="finance",
            artifact_id="art-1", tool_name="invoice_reset", args_hash="deadbeef",
        )
        _fresh_grants.mint(key)

        class _RecordingBot:
            async def send_message(self, **kwargs):
                order.append("send_message")
                return types.SimpleNamespace(message_id=1)

        ch = TelegramChannel(bot_token="T", chat_id="0", default_agent="assistant")
        ch._app = types.SimpleNamespace(bot=_RecordingBot())

        await ch._handle(_fake_update("42", "/new"), None)

        assert order == [
            "cancel:dm:42:new_session",
            "cancel:authz:42:new_session",
            "purge_chat:42",
            "send_message",
        ]
        # the grant is gone (a fresh mint's consume would otherwise be True)
        assert _fresh_grants.consume(key) is False

    async def test_new_with_no_pending_state_is_a_safe_noop(self, monkeypatch, _fresh_broker):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot_token="T", chat_id="0", default_agent="assistant")
        bot = _FakeBot()
        ch._app = types.SimpleNamespace(bot=bot)

        await ch._handle(_fake_update("99", "/new"), None)
        assert len(bot.sent) == 1


# ---------------------------------------------------------------------------
# 13. runtime.yaml grants
# ---------------------------------------------------------------------------


def _allowed(path: Path) -> list[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return (data.get("tools") or {}).get("allowed") or []


class TestRuntimeYamlGrants:
    def test_assistant_has_ask_user(self):
        assert ASK_TOOL in _allowed(AGENTS / "assistant" / "runtime.yaml")

    def test_every_specialist_has_ask_user(self):
        """Task N2's no-gap cutover removed the only bundled specialist
        (finance) from the image — specialists now install from a
        component repository and the image ships none by default, so
        this FOR-ALL invariant is vacuously true over an empty set today.
        Keep the loop (rather than a hard 'must have one' assertion) so a
        regression that bundles a specialist again without the grant is
        still caught."""
        specialist_runtimes = list((AGENTS / "specialists").glob("*/runtime.yaml"))
        for rt in specialist_runtimes:
            assert ASK_TOOL in _allowed(rt), f"{rt} missing {ASK_TOOL}"

    def test_no_executor_definition_grants_ask_user(self):
        executor_defs = list((AGENTS / "executors").glob("*/definition.yaml"))
        assert executor_defs, "expected at least one executor definition.yaml"
        for defn in executor_defs:
            assert ASK_TOOL not in _allowed(defn), (
                f"{defn} must NOT grant {ASK_TOOL} (no executor definition.yaml, per plan)"
            )

    def test_butler_not_required_voice_only_channel(self):
        """butler is voice-only (ha_voice) — ask_user's provenance gate can
        never pass for it (transport dm/button requires channel=='telegram'),
        so the plan does not require the grant there. Documents the decision
        rather than asserting either way on butler's runtime.yaml."""
        butler_channels = yaml.safe_load(
            (AGENTS / "butler" / "runtime.yaml").read_text(encoding="utf-8"),
        ).get("channels") or []
        assert butler_channels == ["ha_voice"]
