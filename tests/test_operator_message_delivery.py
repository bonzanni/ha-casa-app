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
import tools as tools_mod
from authz_grants import GrantIdentity
from channels import DeliveryOutcome
from channels.tg_richtext import render_paged
from plugin_grants import PluginContract, ResultContractMap, ToolContract
from text_util import utf16_len

ARTIFACT = "5" * 64
PLUGIN = "probe"
REPORT = "mcp__plugin_probe_api__report"      # capability, delivers operator_message
SLOT = "report"
BODY = "Q3 summary\nRevenue up 4% on the quarter, token SUMMARY-9c1d."
LABEL = "📊 Finance"


def _identity(**over) -> GrantIdentity:
    base = dict(operator_id=42, chat_id=42, enforcement_role="finance",
                artifact_id=ARTIFACT, engagement_id="")
    base.update(over)
    return GrantIdentity(**base)


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
    def __init__(self, outcome=DeliveryOutcome.DELIVERED, raise_exc=None,
                 sleep_s=0.0, block=None):
        self.outcome, self.raise_exc, self.sleep_s, self.block = (
            outcome, raise_exc, sleep_s, block)
        self.deliveries: list[tuple] = []
        self.other_sends: list[tuple] = []
        self.entered = asyncio.Event()
        self.chat_id = "42"

    async def deliver_operator_message(self, chat_id, text, *, post=None):
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


class _Manager:
    def __init__(self, channel):
        self._channel = channel

    def get(self, name):
        return self._channel if name == "telegram" else None


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
    assert recorder.deliveries == [(4242, LABEL + "\n" + BODY)]
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
    assert recorder.deliveries == [(42, "📊 finance\n" + BODY)]


@pytest.mark.asyncio
async def test_the_receipt_counts_the_pages_the_plan_has(recorder, names):
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    body = "\n".join(f"row {i}: SUMMARY-9c1d" for i in range(400))      # > 1 page
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=body)
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    pages = len(render_paged(LABEL + "\n" + body))
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


# --- the real channel method: the plan is judged whole, before the first send ----

def _channel():
    from unittest.mock import AsyncMock, MagicMock
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(bot=MagicMock(), chat_id="42")
    app = MagicMock()
    app.bot.send_message = AsyncMock(return_value=True)
    ch._app = app
    return ch, app


@pytest.mark.asyncio
async def test_the_real_channel_method_sends_each_page_and_nothing_else():
    from channels.telegram import TelegramChannel
    ch, app = _channel()
    text = LABEL + "\n" + "\n".join(f"row {i} **bold**" for i in range(500))
    pages = render_paged(text)
    assert len(pages) > 1
    assert await ch.deliver_operator_message(42, text) is DeliveryOutcome.DELIVERED
    assert app.bot.send_message.await_count == len(pages)
    for call, (display, entities) in zip(app.bot.send_message.await_args_list, pages):
        # exactly these: no parse_mode, no message_thread_id (a thread id
        # would post the page into a topic)
        assert call.kwargs == {"chat_id": 42, "text": display, "entities": entities}
    ch2 = TelegramChannel(bot=object(), chat_id="42")
    ch2._app = None
    assert await ch2.deliver_operator_message(42, text) is DeliveryOutcome.NOT_DELIVERED


@pytest.mark.asyncio
async def test_a_single_page_is_sent_through_the_multi_page_loop_never_the_authored_retry():
    """The platform refuses the entities: the resend is the page's plain
    fallback chunk (display + link targets), never the authored markdown."""
    from telegram.error import BadRequest
    from unittest.mock import AsyncMock
    ch, app = _channel()
    text = LABEL + "\nSee **this** [note](https://e.example/n)"
    app.bot.send_message = AsyncMock(side_effect=[BadRequest("can't parse entities"), True])
    assert await ch.deliver_operator_message(42, text) is DeliveryOutcome.DELIVERED
    assert app.bot.send_message.await_count == 2
    retry = app.bot.send_message.await_args_list[1].kwargs
    assert retry == {"chat_id": 42, "text": LABEL + "\nSee this note (https://e.example/n)"}


@pytest.mark.asyncio
async def test_a_refused_pages_fallback_chunks_all_go_out_before_delivered():
    """A page at the budget whose plain form overflows: the display, then the
    link targets as their own message — every chunk returns before DELIVERED."""
    from telegram.error import BadRequest, TimedOut
    from unittest.mock import AsyncMock
    ch, app = _channel()
    url = "https://e.example/" + "q" * 60
    text = LABEL + "\n" + "a" * 4050 + f"[t]({url})"
    pages = render_paged(text)
    assert len(pages) == 1
    display, entities = pages[0]
    assert utf16_len(display) <= 4096 < utf16_len(display + f" ({url})")
    app.bot.send_message = AsyncMock(side_effect=[BadRequest("x"), True, True])
    assert await ch.deliver_operator_message(42, text) is DeliveryOutcome.DELIVERED
    assert [c.kwargs["text"] for c in app.bot.send_message.await_args_list] == [
        display, display, url]
    # a tail chunk that fails propagates: the broker reads it as not proven
    app.bot.send_message = AsyncMock(side_effect=[BadRequest("x"), True, TimedOut()])
    with pytest.raises(TimedOut):
        await ch.deliver_operator_message(42, text)


@pytest.mark.asyncio
async def test_a_later_page_failure_propagates():
    from telegram.error import TimedOut
    from unittest.mock import AsyncMock
    ch, app = _channel()
    text = LABEL + "\n" + "\n".join(f"row {i}" for i in range(900))
    assert len(render_paged(text)) >= 2
    app.bot.send_message = AsyncMock(side_effect=[True, TimedOut()])
    with pytest.raises(TimedOut):
        await ch.deliver_operator_message(42, text)
    assert app.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_a_plan_over_the_page_cap_sends_nothing():
    ch, app = _channel()
    text = LABEL + "\n" + "a" * (4096 * (rb.MAX_MESSAGE_PAGES + 1))
    assert len(render_paged(text)) > rb.MAX_MESSAGE_PAGES
    assert await ch.deliver_operator_message(42, text) is DeliveryOutcome.NOT_DELIVERED
    assert app.bot.send_message.await_count == 0
    assert rb.MAX_MESSAGE_PAGES == 6


@pytest.mark.asyncio
async def test_a_link_destination_no_chunk_can_carry_whole_sends_nothing():
    """Rounds 3–4 of the design: ``_plain_fallback_chunks`` silently drops a
    destination over one message; the plan inspects the rendered entities
    and refuses the whole post instead, before the first send."""
    ch, app = _channel()
    url = "https://e.example/" + "q" * 4200
    text = LABEL + "\nopen [the page](" + url + ") now"
    display, entities = render_paged(text)[0]
    assert any(str(e.type) == "text_link" and utf16_len(e.url) > 4096 for e in entities)
    assert await ch.deliver_operator_message(42, text) is DeliveryOutcome.NOT_DELIVERED
    assert app.bot.send_message.await_count == 0
    # the shared helper is untouched: it still filters silently
    assert ch._plain_fallback_chunks(display, entities) == [display]


@pytest.mark.asyncio
async def test_the_hook_withholds_a_refused_plan_with_zero_sends(names, monkeypatch):
    """End to end through the real channel method: a plan the channel refuses
    reaches the model as withheld, and nothing was posted."""
    ch, app = _channel()
    monkeypatch.setattr(tools_mod, "_channel_manager", _Manager(ch), raising=False)
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    body = "\n".join("[x](https://e.example/" + "q" * 4200 + ")" for _ in range(2))
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=body)
    assert err is None
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert app.bot.send_message.await_count == 0
    _assert_withheld_not_delivered(out, store, ref)


@pytest.mark.asyncio
async def test_a_page_over_the_platform_budget_sends_nothing(monkeypatch):
    """The paginator's contract keeps every page within the budget; the plan
    still judges it (a page over the budget is one the operator cannot
    receive complete), so the guard is pinned against a paginator that
    broke its contract."""
    import channels.telegram as tg
    ch, app = _channel()
    monkeypatch.setattr(tg, "render_paged", lambda text: [("x" * 5000, None)])
    assert await ch.deliver_operator_message(42, LABEL + "\nbody") is DeliveryOutcome.NOT_DELIVERED
    assert app.bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_a_destination_the_fallback_would_split_across_messages_sends_nothing():
    """Two destinations each within one message, but whose joined overflow
    form the plain splitter cuts INSIDE a destination: the plan checks that
    every destination occurs whole in some fallback chunk, never only that
    each chunk fits, and refuses with zero sends."""
    from telegram.error import BadRequest
    from unittest.mock import AsyncMock
    ch, app = _channel()
    u1 = "https://e.example/" + "a" * (4096 - len("https://e.example/"))
    u2 = "https://e.example/" + "b" * (4096 - len("https://e.example/"))
    assert utf16_len(u1) == utf16_len(u2) == 4096
    text = LABEL + f"\n[one]({u1}) [two]({u2})"
    display, entities = render_paged(text)[0]
    chunks = ch._plain_fallback_chunks(display, entities)
    assert all(utf16_len(c) <= 4096 for c in chunks)          # every chunk fits…
    assert not any(u2 in c for c in chunks)                     # …and u2 arrives in pieces
    app.bot.send_message = AsyncMock(side_effect=[BadRequest("x"), True, True, True, True])
    assert await ch.deliver_operator_message(42, text) is DeliveryOutcome.NOT_DELIVERED
    assert app.bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_a_fallback_chunk_over_the_platform_budget_sends_nothing(monkeypatch):
    """The plain splitter keeps every chunk within the budget; the plan still
    judges each chunk, pinned against a helper that broke that contract."""
    from channels.telegram import TelegramChannel
    from channels.tg_richtext import plain_with_link_targets
    ch, app = _channel()
    monkeypatch.setattr(TelegramChannel, "_plain_fallback_chunks",
                        lambda self, display, entities: [
                            plain_with_link_targets(display, entities), "x" * 4097])
    text = LABEL + "\nSee [note](https://e.example/n)"
    assert await ch.deliver_operator_message(42, text) is DeliveryOutcome.NOT_DELIVERED
    assert app.bot.send_message.await_count == 0


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
