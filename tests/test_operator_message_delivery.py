"""S3: a plugin slot declared ``operator_message`` reaches the operator only as
pages Casa posts to the chat of the call's grant identity, headed by a label
Casa derives from the specialist's display name (the plugin cannot influence
it), rendered from the deposited text with no model between; proven delivery
of EVERY page replaces the result with a receipt, and anything short withholds
the result and drops the deposit (INV-PLUG-045).

Same discipline as the link suite: the hooks are driven directly with an
in-process recorder channel installed where the hook looks for it
(``tools._channel_manager``) — no socket, no Telegram. Counts and raw store
membership, never ``reference_count()``.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import result_broker as rb
from channels.tg_rich import credited_quote
import tools as tools_mod
from authz_grants import GrantIdentity
from own_voice_fixtures import open_own_chats, with_delivery
from channels import DeliveryOutcome
from channels import tg_rich
from plugin_grants import PluginContract, ResultContractMap, ToolContract
from telegram_channel_double import make_transport
from two_endpoints import CASA_BOT as BOT

ARTIFACT = "5" * 64
PLUGIN = "probe"
REPORT = "mcp__plugin_probe_api__report"      # capability, delivers operator_message
SLOT = "report"
BODY = "Q3 summary\nRevenue up 4% on the quarter, token SUMMARY-9c1d."
LABEL = "Finance"


def _identity(**over) -> GrantIdentity:
    base = dict(operator_id=42, chat_id=42, enforcement_role="finance",
                artifact_id=ARTIFACT, engagement_id="", bot_id=BOT)
    base.update(over)
    return with_delivery(GrantIdentity(**base))


def _map() -> ResultContractMap:
    tools = {
        REPORT: ToolContract(ARTIFACT, PLUGIN, "capability", (SLOT,), {},
                             {SLOT: "operator_message"}),
    }
    plugins = {PLUGIN: PluginContract(ARTIFACT, True, frozenset())}
    return ResultContractMap(tools=tools, plugins=plugins)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class _Recorder:
    default_agent, bot_id, has_bot_id, on_identity = "assistant", BOT, True, None
    def __init__(self, outcome=DeliveryOutcome.DELIVERED, raise_exc=None,
                 sleep_s=0.0, block=None):
        self.outcome, self.raise_exc, self.sleep_s, self.block = (
            outcome, raise_exc, sleep_s, block)
        self.deliveries: list[tuple] = []
        self.other_sends: list[tuple] = []
        self.entered = asyncio.Event()
        self.chat_id = "42"

    async def deliver_operator_message(self, chat_id, text, *, post=None, silent=False):
        self.deliveries.append((chat_id, text))
        self.entered.set()
        if self.block is not None:
            await self.block.wait()
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.outcome

    async def deliver_operator_link(self, *a, **kw):
        self.other_sends.append(("deliver_operator_link", a, kw))
        return DeliveryOutcome.DELIVERED

    async def send_response(self, message, context):
        self.other_sends.append(("send_response", message, context))
        return DeliveryOutcome.DELIVERED

    async def send(self, message, context):
        self.other_sends.append(("send", message, context))
        return DeliveryOutcome.DELIVERED


def _Manager(channel):
    """The channel manager with Telegram's transport over *channel*, Casa's
    endpoint on bot 111; ``None`` (no Telegram) for no channel."""
    return make_transport(channel) if channel is not None else None


@pytest.fixture(autouse=True)
def finance_chat_open(monkeypatch):
    """Finance's own chat is open: these tests are about what its delivered
    slots do, not about whether it may post at all."""
    return open_own_chats(monkeypatch, "finance")


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(tools_mod, "_channel_manager", _Manager(rec), raising=False)
    return rec


@pytest.fixture
def names(monkeypatch):
    """The persona display name the ``<delegates>`` block advertises, from
    the one map Casa resolves delegation targets against."""
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "finance": SimpleNamespace(character=SimpleNamespace(name="Finance"))})


def _store():
    clock = _Clock()
    return rb.ReferenceStore(now=clock), clock


def _open(store, *, call="call-1", identity=None):
    store.open_call(client_id="c1", artifact_id=ARTIFACT, tool_name=REPORT,
                    tool_use_id=call, identity=identity or _identity(),
                    provides=(SLOT,), delivers={SLOT: "operator_message"})


def _post(response, tool=REPORT):
    return {"hook_event_name": "PostToolUse", "tool_name": tool,
            "tool_input": {}, "tool_response": response}


def _replacement(out) -> str:
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    return hso["updatedToolOutput"]


def _assert_withheld_not_delivered(out, store, ref):
    body = _replacement(out)
    parsed = json.loads(body)
    assert parsed["casa_result_withheld"] is True
    assert "message" in parsed["reason"] and "link" not in parsed["reason"]
    assert "delivered" not in parsed
    assert body.count("SUMMARY-9c1d") == 0
    assert ref not in store._refs                       # RAW membership
    assert store._inflight == {}


# --- the deposit --------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "", "   ", "\n\t\n", "a\x00b", "cr\rlf", "bell\x07", "x" * 12_001,
], ids=["empty", "blank", "whitespace-only", "nul", "cr", "bell", "over-cap"])
def test_deposit_for_a_message_slot_refuses_a_bad_body(value, names):
    store, _ = _store()
    _open(store)
    assert store.deposit(client_id="c1", slot=SLOT, value=value) == (None, "bad_message")
    assert store._refs == {}


def test_deposit_for_a_message_slot_accepts_newline_tab_and_the_cap(names):
    store, _ = _store()
    _open(store)
    ref, err = store.deposit(client_id="c1", slot=SLOT,
                             value="line\twith tab\nand newline " + "é" * 11_974)
    assert err is None and rb.is_reference(ref)
    assert rb.MAX_MESSAGE_CHARS == 12_000


def test_deposit_for_a_message_slot_ignores_caption_label_and_kind(names):
    """The plugin cannot influence the label: whatever it sends beside the
    body is not judged and not kept."""
    store, _ = _store()
    _open(store)
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=BODY,
                             caption=7, label="www.evil", kind=["x"])
    assert err is None
    r = store._refs[ref]
    assert (r.caption, r.label, r.media_kind) == ("", "", "")


# --- the success path ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_delivered_message_posts_once_labelled_to_the_identity_chat_and_returns_the_receipt(recorder, names):
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store, identity=_identity(chat_id=4242, operator_id=4242))
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=BODY, label="Plugin says")
    assert err is None
    out = await hook(_post(json.dumps({SLOT: ref, "text": "minted"})), "call-1", {})
    assert recorder.deliveries == [(4242, credited_quote(BODY, LABEL))]
    assert recorder.other_sends == []
    body = _replacement(out)
    receipt = json.loads(body)
    assert receipt == {SLOT: ref, "text": "minted",
                       "casa_delivery": {"slot": SLOT, "status": "delivered",
                                         "to": "operator_chat", "pages": 1}}
    assert body.count("SUMMARY-9c1d") == 0
    assert store._refs[ref].used is True and store._refs[ref].value == ""
    assert store.take_for_delivery(ref) is None
    assert store._inflight == {}


@pytest.mark.asyncio
async def test_the_label_is_the_roles_persona_name_or_the_role_itself(recorder, monkeypatch):
    monkeypatch.setattr(tools_mod, "_agent_role_map", {})
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=BODY)
    await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert recorder.deliveries == [(42, credited_quote(BODY, "finance"))]


@pytest.mark.asyncio
async def test_the_receipt_counts_the_pages_the_plan_has(recorder, names):
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    # past one rich message once prepared: each "_" is sent as "&#95;"
    body = "\n\n".join(f"row {i}: SUMMARY_9c1d " + "_" * 60 for i in range(110))
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=body)
    assert err is None
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    pages = len(tg_rich.split(credited_quote(body, LABEL)))
    assert pages > 1
    assert json.loads(_replacement(out))["casa_delivery"]["pages"] == pages


@pytest.mark.asyncio
async def test_a_no_post_result_passes_unchanged_with_nothing_sent(recorder, names):
    """INV-PLUG-028 is inherited: every provided slot null, nothing deposited."""
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    assert await hook(_post(json.dumps({SLOT: None, "note": "nothing"})), "call-1", {}) == {}
    assert recorder.deliveries == [] and store._inflight == {}


# --- not proven ⇒ withheld, dropped -------------------------------------------

@pytest.mark.asyncio
async def test_not_delivered_outcome_withholds_and_drops(recorder, names):
    recorder.outcome = DeliveryOutcome.NOT_DELIVERED
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=BODY)
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert len(recorder.deliveries) == 1
    _assert_withheld_not_delivered(out, store, ref)


@pytest.mark.asyncio
async def test_channel_exception_withholds_and_drops(recorder, names):
    recorder.raise_exc = RuntimeError("page 2 timed out: " + BODY)
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=BODY)
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    _assert_withheld_not_delivered(out, store, ref)


@pytest.mark.asyncio
async def test_channel_absent_withholds_and_drops(monkeypatch, names):
    monkeypatch.setattr(tools_mod, "_channel_manager", None, raising=False)
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=BODY)
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    _assert_withheld_not_delivered(out, store, ref)


@pytest.mark.asyncio
async def test_delivery_past_the_bound_withholds_and_drops(recorder, names, monkeypatch):
    monkeypatch.setattr(rb, "DELIVERY_TIMEOUT_S", 0.02)
    recorder.sleep_s = 0.5
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=BODY)
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    _assert_withheld_not_delivered(out, store, ref)


@pytest.mark.asyncio
async def test_cancellation_at_the_delivery_await_propagates_and_drops(recorder, names):
    recorder.block = asyncio.Event()
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=BODY)
    task = asyncio.create_task(hook(_post(json.dumps({SLOT: ref})), "call-1", {}))
    await asyncio.wait_for(recorder.entered.wait(), 2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ref not in store._refs and store._inflight == {}


# --- the real channel method: the page count is judged before the first send ----

def _channel():
    from unittest.mock import AsyncMock, MagicMock
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(bot=MagicMock(), chat_id="42", known_bot_id=BOT)
    app = MagicMock()
    app.bot.send_message = AsyncMock(return_value=True)
    app.bot.do_api_request = AsyncMock(return_value=MagicMock(message_id=1))
    ch._app = app
    return ch, app


def _rich_calls(app) -> list[dict]:
    calls = app.bot.do_api_request.await_args_list
    assert all(c.args[0] == "sendRichMessage" for c in calls)
    return [c.kwargs["api_kwargs"] for c in calls]


def _two_parts() -> str:
    """A body credited to the specialist that one rich message cannot hold: two parts."""
    from channels.tg_rich import split
    text = credited_quote("\n\n".join(f"row {i} **bold** " + "x" * 300 for i in range(150)),
                          LABEL)
    assert len(split(text)) == 2
    return text


@pytest.mark.asyncio
async def test_the_real_channel_method_sends_each_page_and_nothing_else():
    from channels.telegram import TelegramChannel
    from channels.tg_rich import prepare, split
    ch, app = _channel()
    text = _two_parts()
    assert await ch.deliver_operator_message(42, text) is DeliveryOutcome.DELIVERED
    # exactly these: the agent's Markdown, no message_thread_id (a thread id
    # would post the page into a topic), and nothing through send_message
    assert _rich_calls(app) == [
        {"chat_id": 42, "rich_message": {"markdown": prepare(part)}} for part in split(text)]
    app.bot.send_message.assert_not_awaited()
    ch2 = TelegramChannel(bot=object(), chat_id="42", known_bot_id=BOT)
    ch2._app = None
    assert await ch2.deliver_operator_message(42, text) is DeliveryOutcome.NOT_DELIVERED


@pytest.mark.asyncio
async def test_a_short_message_is_one_rich_message():
    from channels.tg_rich import prepare
    ch, app = _channel()
    text = credited_quote("See **this** [note](https://e.example/n)", LABEL)
    assert await ch.deliver_operator_message(42, text) is DeliveryOutcome.DELIVERED
    assert _rich_calls(app) == [{"chat_id": 42, "rich_message": {"markdown": prepare(text)}}]


@pytest.mark.asyncio
async def test_a_refused_message_is_resent_unformatted_before_delivered():
    from telegram.error import BadRequest, TimedOut
    from unittest.mock import AsyncMock, MagicMock
    from channels.tg_rich import plain_blocks
    ch, app = _channel()
    text = credited_quote("See **this** [note](https://e.example/n)", LABEL)
    app.bot.do_api_request = AsyncMock(side_effect=[BadRequest("x"), MagicMock(message_id=2)])
    assert await ch.deliver_operator_message(42, text) is DeliveryOutcome.DELIVERED
    first, second = _rich_calls(app)
    assert "markdown" in first["rich_message"]
    assert second["rich_message"] == {"blocks": plain_blocks(text)}
    # a resend that fails propagates: the broker reads it as not proven
    app.bot.do_api_request = AsyncMock(side_effect=[BadRequest("x"), TimedOut()])
    with pytest.raises(TimedOut):
        await ch.deliver_operator_message(42, text)


@pytest.mark.asyncio
async def test_a_long_link_destination_goes_out_whole_in_one_message():
    ch, app = _channel()
    url = "https://e.example/" + "q" * 4200
    text = credited_quote("open [the page](" + url + ") now", LABEL)
    assert await ch.deliver_operator_message(42, text) is DeliveryOutcome.DELIVERED
    (kw,) = _rich_calls(app)
    assert kw["rich_message"]["markdown"].count(url) == 1


@pytest.mark.asyncio
async def test_a_silent_message_sends_every_part_without_a_notification():
    ch, app = _channel()
    text = _two_parts()
    assert await ch.deliver_operator_message(42, text, silent=True) is DeliveryOutcome.DELIVERED
    calls = _rich_calls(app)
    assert len(calls) == 2
    assert all(kw["disable_notification"] is True for kw in calls)


@pytest.mark.asyncio
async def test_every_landed_part_is_filed_under_the_post():
    from unittest.mock import MagicMock
    ch, app = _channel()
    filed = []
    ch._record_post = lambda chat, sent, post: filed.append((chat, sent, post))
    post = object()
    await ch.deliver_operator_message(42, _two_parts(), post=post)
    assert [(chat, p) for chat, _sent, p in filed] == [(42, post), (42, post)]


@pytest.mark.asyncio
async def test_a_later_page_failure_propagates():
    from telegram.error import TimedOut
    from unittest.mock import AsyncMock, MagicMock
    ch, app = _channel()
    app.bot.do_api_request = AsyncMock(side_effect=[MagicMock(message_id=1), TimedOut()])
    with pytest.raises(TimedOut):
        await ch.deliver_operator_message(42, _two_parts())
    assert app.bot.do_api_request.await_count == 2


@pytest.mark.asyncio
async def test_a_plan_over_the_page_cap_sends_nothing(monkeypatch):
    ch, app = _channel()
    assert rb.MAX_MESSAGE_PAGES == 6
    monkeypatch.setattr(rb, "MAX_MESSAGE_PAGES", 1)
    assert await ch.deliver_operator_message(42, _two_parts()) is DeliveryOutcome.NOT_DELIVERED
    assert app.bot.do_api_request.await_count == 0
    assert app.bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_the_hook_withholds_a_refused_plan_with_zero_sends(names, monkeypatch):
    """End to end through the real channel method: a plan the channel refuses
    reaches the model as withheld, and nothing was posted."""
    from channels.tg_rich import split
    ch, app = _channel()
    monkeypatch.setattr(tools_mod, "_channel_manager", _Manager(ch), raising=False)
    monkeypatch.setattr(rb, "MAX_MESSAGE_PAGES", 1)
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    body = "💶" * 9_000                       # 36,000 bytes: past one rich message
    assert len(split(credited_quote(body, LABEL))) > 1
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=body)
    assert err is None
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert app.bot.do_api_request.await_count == 0
    assert app.bot.send_message.await_count == 0
    _assert_withheld_not_delivered(out, store, ref)


@pytest.mark.parametrize("value", [
    "Total: 1 000 EUR", "Total: 1 000 EUR", "family: 👨‍👩‍👧",
    "shalom ‏עברית", "line next",
], ids=["nbsp", "narrow-nbsp", "zwj-emoji", "rtl-mark", "line-separator"])
def test_deposit_for_a_message_slot_accepts_unicode_that_is_not_a_control(value, names):
    store, _ = _store()
    _open(store)
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=value)
    assert err is None and rb.is_reference(ref)


@pytest.mark.parametrize("value", ["del\x7f", "nel\x85", "esc\x1b[31m"],
                         ids=["del", "nel", "escape"])
def test_deposit_for_a_message_slot_refuses_every_other_control(value, names):
    store, _ = _store()
    _open(store)
    assert store.deposit(client_id="c1", slot=SLOT, value=value) == (None, "bad_message")
