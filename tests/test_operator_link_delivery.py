"""#1015: a plugin slot declared ``operator_link`` reaches the operator only
as ONE message Casa posts to the chat of the call's grant identity, after the
result's structural check; the hook's replacement receipt is the only
model-visible statement that it was delivered; anything short of proven
delivery withholds the result and drops the deposit (INV-PLUG-025).

The hooks are driven directly with an in-process recorder channel installed
where the hook looks for it (``tools._channel_manager``) — no socket, no
Telegram, per the sandbox rule. Counts and raw store membership, never
``reference_count()`` (which sweeps used entries and would pass on a mutant
that dropped nothing). Async tests are marked individually.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import agent as agent_mod
import result_broker as rb
import tools as tools_mod
from authz_grants import GrantIdentity
from channels import DeliveryOutcome
from plugin_grants import PluginContract, ResultContractMap, ToolContract
from text_util import utf16_len

ARTIFACT = "3" * 64
PLUGIN = "probe"
LINK = "mcp__plugin_probe_api__link"                 # capability, delivers
FETCH = "mcp__plugin_probe_api__fetch"               # capability, no delivery
SETUP_CAP = "mcp__plugin_probe_api__setup_probe"     # the setup tool, declared capability + delivers
SETUP_PLAIN = "mcp__plugin_plain_api__setup_plain"   # a setup tool with no capability entry
URL = "https://Bank.Example.com/approve?session=SESSION-7f3e"
HOST = "bank.example.com"
SLOT = "approval_link"


def _identity(**over) -> GrantIdentity:
    base = dict(operator_id=42, chat_id=42, enforcement_role="finance",
                artifact_id=ARTIFACT, engagement_id="")
    base.update(over)
    return GrantIdentity(**base)


def _map() -> ResultContractMap:
    tools = {
        LINK: ToolContract(ARTIFACT, PLUGIN, "capability", (SLOT,), {},
                           {SLOT: "operator_link"}),
        FETCH: ToolContract(ARTIFACT, PLUGIN, "capability", ("token",), {}),
        SETUP_CAP: ToolContract(ARTIFACT, PLUGIN, "capability", ("auth_url",), {},
                                {"auth_url": "operator_link"}),
    }
    plugins = {
        PLUGIN: PluginContract(ARTIFACT, True, frozenset({SETUP_CAP})),
        "plain": PluginContract("4" * 64, True, frozenset({SETUP_PLAIN})),
    }
    return ResultContractMap(tools=tools, plugins=plugins)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class _Recorder:
    """The Telegram channel double: records every delivery the hook asks for
    and every OTHER send, so a test can assert the link went to the identity
    chat and nowhere else."""

    def __init__(self, outcome=DeliveryOutcome.DELIVERED, raise_exc=None,
                 sleep_s=0.0, block=None):
        self.outcome, self.raise_exc, self.sleep_s, self.block = (
            outcome, raise_exc, sleep_s, block)
        self.deliveries: list[tuple] = []
        self.other_sends: list[tuple] = []
        self.entered = asyncio.Event()
        self.chat_id = "42"

    async def deliver_operator_link(self, chat_id, text, entities, plain):
        self.deliveries.append((chat_id, text, entities, plain))
        self.entered.set()
        if self.block is not None:
            await self.block.wait()
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.outcome

    async def send_response(self, message, context):
        self.other_sends.append(("send_response", message, context))
        return DeliveryOutcome.DELIVERED

    async def send(self, message, context):
        self.other_sends.append(("send", message, context))
        return DeliveryOutcome.DELIVERED

    async def send_to_topic(self, *a, **kw):
        self.other_sends.append(("send_to_topic", a, kw))
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


def _store():
    clock = _Clock()
    return rb.ReferenceStore(now=clock), clock


def _open(store, tool=LINK, *, call="call-1", provides=(SLOT,),
          delivers=None, identity=None):
    if delivers is None:
        delivers = {SLOT: "operator_link"}
    store.open_call(client_id="c1", artifact_id=ARTIFACT, tool_name=tool,
                    tool_use_id=call, identity=identity or _identity(),
                    provides=provides, delivers=delivers)


def _post(tool, response, tool_input=None):
    return {"hook_event_name": "PostToolUse", "tool_name": tool,
            "tool_input": dict(tool_input or {}), "tool_response": response}


def _pre(tool, tool_input=None):
    return {"hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": dict(tool_input or {})}


def _replacement(out) -> str:
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    return hso["updatedToolOutput"]


def _assert_withheld_not_delivered(out, store, ref):
    body = _replacement(out)
    parsed = json.loads(body)
    assert parsed["casa_result_withheld"] is True
    assert "link" in parsed["reason"] and "delivered" not in parsed
    assert body.count(URL) == 0 and body.count("SESSION-7f3e") == 0
    assert ref not in store._refs                       # RAW membership
    assert store._inflight == {}


# --- the success path -----------------------------------------------------------

@pytest.mark.asyncio
async def test_delivered_link_posts_once_to_the_identity_chat_and_returns_the_receipt(recorder):
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store, identity=_identity(chat_id=4242, operator_id=4242))
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=URL,
                             caption="Rabobank, NL — one-time link, 30 minutes",
                             label="Approve at Rabobank")
    assert err is None
    out = await hook(_post(LINK, json.dumps({SLOT: ref, "text": "minted"})), "call-1", {})
    # exactly one send, to the identity's chat, nothing anywhere else
    assert len(recorder.deliveries) == 1 and recorder.other_sends == []
    chat_id, text, entities, plain = recorder.deliveries[0]
    assert chat_id == 4242
    assert text == ("Approve at Rabobank (bank.example.com)\n"
                    "Rabobank, NL — one-time link, 30 minutes")
    assert len(entities) == 1
    ent = entities[0]
    assert (str(ent.type), ent.offset, ent.length, ent.url) == (
        "text_link", 0, utf16_len("Approve at Rabobank (bank.example.com)"), URL)
    assert plain == ("Approve at Rabobank (bank.example.com): " + URL +
                     "\nRabobank, NL — one-time link, 30 minutes")
    # the receipt: every producer field, the reference, the claim, no URL byte
    body = _replacement(out)
    receipt = json.loads(body)
    assert receipt == {SLOT: ref, "text": "minted",
                       "casa_delivery": {"slot": SLOT, "status": "delivered",
                                         "to": "operator_chat"}}
    assert body.count(URL) == 0 and body.count("SESSION-7f3e") == 0
    # the reference is spent: unredeemable, swept, the call closed
    assert store._refs[ref].used is True and store._refs[ref].value == ""
    assert store.arm(reference=ref, identity=_identity(chat_id=4242, operator_id=4242),
                     slot=SLOT, client_id="c1", tool_use_id="d") is None
    assert store.take_for_delivery(ref) is None
    assert store._inflight == {}


@pytest.mark.asyncio
async def test_entity_offsets_are_utf16_units_on_a_non_bmp_label(recorder):
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    label = "\U0001F511 Sign in"                       # a surrogate pair leads
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=URL, label=label)
    assert err is None
    await hook(_post(LINK, json.dumps({SLOT: ref})), "call-1", {})
    _chat, text, entities, _plain = recorder.deliveries[0]
    link_text = f"{label} ({HOST})"
    assert text == link_text
    assert entities[0].length == utf16_len(link_text) == len(link_text) + 1
    assert entities[0].offset == 0


@pytest.mark.asyncio
async def test_host_is_printed_from_the_url_never_from_the_label(recorder):
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=URL, label="Approve here")
    await hook(_post(LINK, json.dumps({SLOT: ref})), "call-1", {})
    _chat, text, _entities, plain = recorder.deliveries[0]
    assert text == f"Approve here ({HOST})"
    assert text.count("Bank.Example.com") == 0          # lower-cased hostname
    assert plain.startswith(f"Approve here ({HOST}): https://")
    # default label when the producer sends none
    _open(store, call="call-2")
    ref2, _ = store.deposit(client_id="c1", slot=SLOT, value=URL)
    await hook(_post(LINK, json.dumps({SLOT: ref2})), "call-2", {})
    assert recorder.deliveries[1][1] == f"Open ({HOST})"


# --- every ordinary failure branch withholds and drops --------------------------

@pytest.mark.asyncio
async def test_not_delivered_outcome_withholds_and_drops(recorder):
    recorder.outcome = DeliveryOutcome.NOT_DELIVERED
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=URL)
    out = await hook(_post(LINK, json.dumps({SLOT: ref})), "call-1", {})
    assert len(recorder.deliveries) == 1
    _assert_withheld_not_delivered(out, store, ref)


@pytest.mark.asyncio
async def test_channel_exception_withholds_and_drops(recorder):
    recorder.raise_exc = RuntimeError("TimedOut after the send")
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=URL)
    out = await hook(_post(LINK, json.dumps({SLOT: ref})), "call-1", {})
    assert len(recorder.deliveries) == 1
    _assert_withheld_not_delivered(out, store, ref)


@pytest.mark.asyncio
async def test_channel_absent_withholds_and_drops(monkeypatch):
    monkeypatch.setattr(tools_mod, "_channel_manager", _Manager(None), raising=False)
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=URL)
    out = await hook(_post(LINK, json.dumps({SLOT: ref})), "call-1", {})
    _assert_withheld_not_delivered(out, store, ref)
    monkeypatch.setattr(tools_mod, "_channel_manager", None, raising=False)
    _open(store, call="call-2")
    ref2, _ = store.deposit(client_id="c1", slot=SLOT, value=URL)
    out = await hook(_post(LINK, json.dumps({SLOT: ref2})), "call-2", {})
    _assert_withheld_not_delivered(out, store, ref2)


@pytest.mark.asyncio
async def test_delivery_past_the_bound_withholds_and_drops(recorder, monkeypatch):
    recorder.sleep_s = 5.0
    monkeypatch.setattr(rb, "DELIVERY_TIMEOUT_S", 0.05)
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=URL)
    out = await asyncio.wait_for(
        hook(_post(LINK, json.dumps({SLOT: ref})), "call-1", {}), 2.0)
    assert len(recorder.deliveries) == 1
    _assert_withheld_not_delivered(out, store, ref)


@pytest.mark.asyncio
async def test_a_deposit_that_cannot_be_taken_withholds_with_zero_sends(recorder):
    store, clock = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=URL)
    clock.t += rb.reference_ttl_s() + 1                 # expired before the hook
    out = await hook(_post(LINK, json.dumps({SLOT: ref})), "call-1", {})
    assert recorder.deliveries == []
    _assert_withheld_not_delivered(out, store, ref)


@pytest.mark.asyncio
async def test_a_malformed_delivered_result_sends_nothing(recorder):
    """Delivery runs AFTER the structural check: a result that omits the
    slot, or carries a foreign reference, posts zero messages and is
    withheld as a bad capability result (the delivery-before-validation
    mutant)."""
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=URL)
    out = await hook(_post(LINK, json.dumps({"text": "no slot"})), "call-1", {})
    assert recorder.deliveries == []
    body = _replacement(out)
    assert json.loads(body)["casa_result_withheld"] is True
    assert body.count(URL) == 0 and ref not in store._refs
    _open(store, call="call-2")
    ref2, _ = store.deposit(client_id="c1", slot=SLOT, value=URL)
    await hook(_post(LINK, json.dumps({SLOT: rb.new_reference()})), "call-2", {})
    assert recorder.deliveries == [] and ref2 not in store._refs


@pytest.mark.asyncio
async def test_an_undelivered_capability_result_still_passes_unchanged(recorder):
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store, FETCH, provides=("token",), delivers={})
    ref, _ = store.deposit(client_id="c1", slot="token", value="tok")
    assert await hook(_post(FETCH, json.dumps({"token": ref})), "call-1", {}) == {}
    assert recorder.deliveries == [] and ref in store._refs


# --- cancellation: the CLI's deadline -------------------------------------------

@pytest.mark.asyncio
async def test_cancellation_at_the_delivery_await_propagates_and_drops(recorder):
    """The hook's own cancellation (the CLI abandoning it) re-raises with NO
    replacement; the call and the deposit are gone by raw membership. The
    model then holds the delivery-neutral original — measured at the CLI
    seam in the design record (a cancelled PostToolUse hook: the original
    result reaches model input with zero URL bytes and zero receipts)."""
    recorder.block = asyncio.Event()
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=URL)
    task = asyncio.ensure_future(
        hook(_post(LINK, json.dumps({SLOT: ref})), "call-1", {}))
    await asyncio.wait_for(recorder.entered.wait(), 2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()                             # no replacement exists
    assert ref not in store._refs and store._inflight == {}
    assert len(recorder.deliveries) == 1


# --- the real channel method (Astra, diff round 1: a mutant dropping the send
# survived every recorder-based test) -------------------------------------------

@pytest.mark.asyncio
async def test_the_real_channel_method_sends_once_and_falls_back_plain():
    from unittest.mock import AsyncMock, MagicMock
    from telegram.error import BadRequest, TimedOut
    from channels.telegram import TelegramChannel
    text, entities, plain = rb.compose_operator_link(URL, caption="c", label="Approve")
    # not started: nothing sent, not proven
    ch = TelegramChannel(bot=MagicMock(), chat_id="42")
    ch._app = None
    assert await ch.deliver_operator_link(42, text, entities, plain) is DeliveryOutcome.NOT_DELIVERED
    # started: exactly one send_message with the entity; DELIVERED
    app = MagicMock()
    app.bot.send_message = AsyncMock(return_value=True)
    ch._app = app
    assert await ch.deliver_operator_link(42, text, entities, plain) is DeliveryOutcome.DELIVERED
    assert app.bot.send_message.await_count == 1
    kw = app.bot.send_message.await_args.kwargs
    # exactly these three: no parse_mode, and no message_thread_id — a thread
    # id would post the link into a topic, the defect #1015 exists to remove
    assert kw == {"chat_id": 42, "text": text, "entities": entities}
    # the platform refuses the entity: one plain retry spelling the URL out
    app.bot.send_message = AsyncMock(side_effect=[BadRequest("can't parse entities"), True])
    assert await ch.deliver_operator_link(42, text, entities, plain) is DeliveryOutcome.DELIVERED
    assert app.bot.send_message.await_count == 2
    assert app.bot.send_message.await_args.kwargs == {"chat_id": 42, "text": plain}
    # a TimedOut propagates: the broker reads it as not proven
    app.bot.send_message = AsyncMock(side_effect=TimedOut())
    with pytest.raises(TimedOut):
        await ch.deliver_operator_link(42, text, entities, plain)
    assert app.bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_casas_warnings_on_the_send_path_log_the_error_type_only(recorder, caplog):
    """Casa's own two warnings on the send path — the send primitive's
    plain-fallback line and the broker's delivery-failure line — name the
    error's type, never its text, which can quote the refused message. That
    is all this pins: log content is outside INV-PLUG-025 by operator ruling
    (2026-09-16: the link is not a bearer credential and expires), so the
    client library's own loggers are not policed."""
    import logging
    from unittest.mock import AsyncMock, MagicMock
    from telegram.error import BadRequest
    from channels.telegram import TelegramChannel
    caplog.set_level(logging.DEBUG)
    text, entities, plain = rb.compose_operator_link(URL, label="Approve")
    ch = TelegramChannel(bot=MagicMock(), chat_id="42")
    app = MagicMock()
    app.bot.send_message = AsyncMock(side_effect=[BadRequest("rejected URL " + URL), True])
    ch._app = app
    assert await ch.deliver_operator_link(42, text, entities, plain) is DeliveryOutcome.DELIVERED
    assert app.bot.send_message.await_count == 2
    # the broker path: the channel raises with the URL in its text
    recorder.raise_exc = RuntimeError("refused: " + URL)
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=URL)
    out = await hook(_post(LINK, json.dumps({SLOT: ref})), "call-1", {})
    _assert_withheld_not_delivered(out, store, ref)
    logged = "\n".join(r.getMessage() for r in caplog.records) + caplog.text
    assert logged.count(URL) == 0 and logged.count("SESSION-7f3e") == 0
    assert "fell back to plain" in logged and "delivery failed" in logged


# --- the setup tool ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_setup_tool_declared_capability_takes_the_path_through_both_real_hooks(recorder):
    """Admission opens the call (identity required), the result hook closes
    it and delivers; zero in-flight calls after. An undeclared setup tool is
    untouched by both."""
    from test_result_broker import DM, _Origin
    store, _ = _store()
    pre = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=store)
    post = rb.make_result_hook(_map(), client_id="c1", store=store)
    # no operator-bound turn: refused before it runs, nothing registered
    with _Origin(None):
        out = await pre(_pre(SETUP_CAP), "s-0", {})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert store._inflight == {}
    with _Origin(DM):
        assert await pre(_pre(SETUP_CAP), "s-1", {}) == {}
    assert len(store._inflight) == 1
    ref, err = store.deposit(client_id="c1", slot="auth_url", value=URL,
                             label="Sign in with Google")
    assert err is None
    out = await post(_post(SETUP_CAP, json.dumps({"auth_url": ref, "redirect_uri": "r"})),
                     "s-1", {})
    assert len(recorder.deliveries) == 1 and recorder.deliveries[0][0] == 42
    receipt = json.loads(_replacement(out))
    assert receipt["casa_delivery"]["status"] == "delivered"
    assert receipt["auth_url"] == ref and receipt["redirect_uri"] == "r"
    assert store._inflight == {}
    # the undeclared setup tool: both hooks pass it as before
    assert await pre(_pre(SETUP_PLAIN), "p-1", {}) == {}
    assert await post(_post(SETUP_PLAIN, json.dumps({"auth_url": URL})), "p-1", {}) == {}
    assert store._inflight == {} and len(recorder.deliveries) == 1


# --- end to end: a specialist engagement's link lands in the origin chat -----------

@pytest.mark.asyncio
async def test_an_engagements_link_goes_to_the_origin_chat_not_the_topic(recorder):
    """The engagement identity's chat is the origin chat the operator asked
    in; the topic (a negative supergroup id) receives nothing."""
    from test_result_broker import _Origin
    rec = SimpleNamespace(
        id="eng-1", kind="specialist", status="active", topic_id=555,
        role_or_type="finance",
        origin={"role": "assistant", "channel": "telegram", "chat_id": 42,
                "user_id": 42, "message_type": "channel_in", "source": "telegram",
                "execution_role": "assistant"})
    topic_origin = {"role": "finance", "execution_role": "finance",
                    "channel": "telegram", "chat_id": -100123, "user_id": 42,
                    "message_type": "channel_in", "source": "telegram"}
    store, _ = _store()
    pre = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=store)
    post = rb.make_result_hook(_map(), client_id="c1", store=store)
    with _Origin(topic_origin, engagement=rec):
        assert await pre(_pre(LINK), "e-1", {}) == {}
        call = next(iter(store._inflight.values()))
        assert (call.identity.chat_id, call.identity.engagement_id) == (42, "eng-1")
        ref, err = store.deposit(client_id="c1", slot=SLOT, value=URL)
        assert err is None
        out = await post(_post(LINK, json.dumps({SLOT: ref})), "e-1", {})
    assert [d[0] for d in recorder.deliveries] == [42]
    assert recorder.other_sends == []
    assert json.loads(_replacement(out))["casa_delivery"]["status"] == "delivered"
