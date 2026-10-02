"""S3: a plugin slot declared ``operator_file`` reaches the operator only as
ONE media send Casa makes through the kind's media policy to the chat of the
call's grant identity, captioned with the label Casa derives from the
specialist's display name (the plugin cannot influence it) and the plugin's
caption beneath as the deposit validated it; proven delivery replaces the
result with a receipt, and anything short withholds the result and drops the
deposit (INV-PLUG-046). The file is claimed from the plugin outbox exactly as
``send_media`` claims it, and is consumed on every outcome.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

import plugin_outbox
import result_broker as rb
import tools as tools_mod
from authz_grants import GrantIdentity
from channels import DeliveryOutcome
from plugin_grants import PluginContract, ResultContractMap, ToolContract
from plugin_outbox import PluginOutbox

ARTIFACT = "6" * 64
PLUGIN = "probe"
EXPORT = "mcp__plugin_probe_api__export"      # capability, delivers operator_file
SLOT = "export"
LABEL = "📊 Finance"
DATA = b"quarter,revenue\nQ3,EXPORT-4e2a\n"


def _identity(**over) -> GrantIdentity:
    base = dict(operator_id=42, chat_id=42, enforcement_role="finance",
                artifact_id=ARTIFACT, engagement_id="")
    base.update(over)
    return GrantIdentity(**base)


def _map() -> ResultContractMap:
    tools = {
        EXPORT: ToolContract(ARTIFACT, PLUGIN, "capability", (SLOT,), {},
                             {SLOT: "operator_file"}),
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
                 sleep_s=0.0):
        self.outcome, self.raise_exc, self.sleep_s = outcome, raise_exc, sleep_s
        self.deliveries: list[tuple] = []
        self.other_sends: list[tuple] = []
        self.chat_id = "42"

    async def deliver_operator_file(self, chat_id, content, kind, filename, caption, *, post=None):
        self.deliveries.append((chat_id, content, kind, filename, caption))
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.outcome

    async def send_media(self, *a, **kw):
        self.other_sends.append(("send_media", a, kw))

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
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "finance": SimpleNamespace(character=SimpleNamespace(name="Finance"))})


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    """The shared outbox, installed where ``send_media`` finds it; no
    engagement is bound, so the hook claims from here."""
    ob = PluginOutbox(str(tmp_path / "plugin-outbox"))
    monkeypatch.setattr(plugin_outbox, "_OUTBOX", ob)
    tok = tools_mod.engagement_var.set(None)
    yield ob
    tools_mod.engagement_var.reset(tok)
    ob.close()


def _put(ob: PluginOutbox, name: str, data: bytes = DATA) -> str:
    path = os.path.join(ob._root_realpath, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _store():
    clock = _Clock()
    return rb.ReferenceStore(now=clock), clock


def _open(store, *, call="call-1", identity=None):
    store.open_call(client_id="c1", artifact_id=ARTIFACT, tool_name=EXPORT,
                    tool_use_id=call, identity=identity or _identity(),
                    provides=(SLOT,), delivers={SLOT: "operator_file"})


def _post(response, tool=EXPORT):
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
    assert "file" in parsed["reason"] and "link" not in parsed["reason"]
    assert "delivered" not in parsed
    assert body.count("EXPORT-4e2a") == 0
    assert ref not in store._refs
    assert store._inflight == {}


def _outbox_is_empty(ob: PluginOutbox) -> bool:
    root = ob._root_realpath
    files = [n for n in os.listdir(root) if not n.startswith(".")]
    claims = os.listdir(ob._claims_realpath)
    return files == [] and claims == []


# --- the deposit --------------------------------------------------------------

@pytest.mark.parametrize("kind", [None, "", "pdf", 7, ["text"]],
                         ids=["missing", "empty", "unknown", "int", "list"])
def test_deposit_for_a_file_slot_requires_a_media_kind(kind, names):
    store, _ = _store()
    _open(store)
    assert store.deposit(client_id="c1", slot=SLOT, value="/x/report.csv",
                         kind=kind) == (None, "bad_kind")
    assert store._refs == {}


@pytest.mark.parametrize("caption", [7, "a\nb", "a\x00b", "c" * 1015],
                         ids=["int", "newline", "nul", "composed-overflow"])
def test_deposit_for_a_file_slot_refuses_a_bad_caption(caption, names):
    """The COMPOSED caption — label line, newline, plugin caption — must fit
    ``send_media``'s cap; overflow is refused, never truncated."""
    store, _ = _store()
    _open(store)
    assert store.deposit(client_id="c1", slot=SLOT, value="/x/report.csv",
                         kind="text", caption=caption) == (None, "bad_caption")
    assert store._refs == {}


def test_deposit_for_a_file_slot_keeps_kind_and_a_fitting_caption(names):
    store, _ = _store()
    _open(store)
    ref, err = store.deposit(client_id="c1", slot=SLOT, value="/x/report.csv",
                             kind="text", caption="c" * 1014, label="Evil label")
    assert err is None
    r = store._refs[ref]
    assert (r.media_kind, r.caption, r.label) == ("text", "c" * 1014, "")
    assert len(LABEL) + 1 + 1014 == rb.MAX_FILE_CAPTION_CHARS == tools_mod._CAPTION_MAX
    assert store.take_for_delivery(ref) == (
        "/x/report.csv", "c" * 1014, "", _identity(), "text")


def test_deposit_for_a_file_slot_does_not_read_the_file(names, tmp_path):
    """A missing or out-of-outbox path is judged at delivery, not here."""
    store, _ = _store()
    _open(store)
    ref, err = store.deposit(client_id="c1", slot=SLOT,
                             value=str(tmp_path / "nowhere.txt"), kind="text")
    assert err is None and rb.is_reference(ref)


# --- the success path ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_delivered_file_is_claimed_sent_labelled_and_receipted(recorder, names, outbox):
    path = _put(outbox, "report.csv")
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store, identity=_identity(chat_id=4242, operator_id=4242))
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=path, kind="text",
                             caption="Q3 export")
    assert err is None
    out = await hook(_post(json.dumps({SLOT: ref, "rows": 1})), "call-1", {})
    assert recorder.deliveries == [(4242, DATA, "text", "report.csv", LABEL + "\nQ3 export")]
    assert recorder.other_sends == []
    body = _replacement(out)
    receipt = json.loads(body)
    assert receipt == {SLOT: ref, "rows": 1,
                       "casa_delivery": {"slot": SLOT, "status": "delivered",
                                         "to": "operator_chat", "kind": "text"}}
    assert body.count("EXPORT-4e2a") == 0 and "report.csv" not in body
    assert _outbox_is_empty(outbox)                      # consumed
    assert store._refs[ref].used is True and store._inflight == {}


@pytest.mark.asyncio
async def test_a_file_with_no_plugin_caption_is_still_labelled(recorder, names, outbox):
    path = _put(outbox, "report.csv")
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=path, kind="text")
    await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert recorder.deliveries[0][4] == LABEL


@pytest.mark.asyncio
async def test_an_engagements_file_is_claimed_from_its_private_outbox(recorder, names, tmp_path, monkeypatch):
    """A uid-dropped engagement's producer writes a private outbox; the
    claim is derived from the authenticated record, exactly as send_media's."""
    from engagement_uids import UID_BASE
    private = PluginOutbox(str(tmp_path / "private"))
    shared = PluginOutbox(str(tmp_path / "shared"))
    monkeypatch.setattr(plugin_outbox, "_OUTBOX", shared)
    monkeypatch.setattr(plugin_outbox, "get_engagement_outbox",
                        lambda uid, **kw: private if uid == UID_BASE + 3 else None)
    rec = SimpleNamespace(id="eng-1", status="active", allocated_uid=UID_BASE + 3,
                          origin={"chat_id": 42})
    path = _put(private, "report.csv")
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store, identity=_identity(engagement_id="eng-1"))
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=path, kind="text")
    tok = tools_mod.engagement_var.set(rec)
    try:
        out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    finally:
        tools_mod.engagement_var.reset(tok)
        private.close()
        shared.close()
    assert json.loads(_replacement(out))["casa_delivery"]["status"] == "delivered"
    assert recorder.deliveries[0][1] == DATA


# --- not proven ⇒ withheld, dropped, file consumed ----------------------------

@pytest.mark.asyncio
async def test_a_path_outside_the_outbox_is_withheld_with_zero_sends(recorder, names, outbox, tmp_path):
    elsewhere = tmp_path / "elsewhere.csv"
    elsewhere.write_bytes(DATA)
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=str(elsewhere), kind="text")
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert recorder.deliveries == []
    _assert_withheld_not_delivered(out, store, ref)
    assert elsewhere.exists()                            # never touched


@pytest.mark.asyncio
async def test_a_missing_file_is_withheld_with_zero_sends(recorder, names, outbox):
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT,
                           value=os.path.join(outbox._root_realpath, "gone.csv"), kind="text")
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert recorder.deliveries == []
    _assert_withheld_not_delivered(out, store, ref)


@pytest.mark.asyncio
async def test_a_policy_refusal_withholds_and_still_consumes_the_file(recorder, names, outbox):
    """The kind's magic gate refuses (a NUL byte is not text): nothing is
    sent, the result is withheld, and the claimed file is gone either way."""
    path = _put(outbox, "report.csv", b"EXPORT-4e2a\x00binary")
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=path, kind="text")
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert recorder.deliveries == []
    _assert_withheld_not_delivered(out, store, ref)
    assert _outbox_is_empty(outbox)


@pytest.mark.asyncio
async def test_an_extension_outside_the_kinds_policy_is_withheld_and_consumed(recorder, names, outbox):
    path = _put(outbox, "report.csv", b"%PDF-1.4 EXPORT-4e2a")
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=path, kind="document")
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert recorder.deliveries == []
    _assert_withheld_not_delivered(out, store, ref)
    assert _outbox_is_empty(outbox)


@pytest.mark.asyncio
async def test_not_delivered_outcome_withholds_and_drops(recorder, names, outbox):
    recorder.outcome = DeliveryOutcome.NOT_DELIVERED
    path = _put(outbox, "report.csv")
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=path, kind="text")
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert len(recorder.deliveries) == 1
    _assert_withheld_not_delivered(out, store, ref)
    assert _outbox_is_empty(outbox)


@pytest.mark.asyncio
async def test_channel_exception_withholds_and_drops(recorder, names, outbox):
    recorder.raise_exc = RuntimeError("upload failed: report.csv EXPORT-4e2a")
    path = _put(outbox, "report.csv")
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=path, kind="text")
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    _assert_withheld_not_delivered(out, store, ref)
    assert _outbox_is_empty(outbox)


@pytest.mark.asyncio
async def test_channel_absent_withholds_and_drops(monkeypatch, names, outbox):
    monkeypatch.setattr(tools_mod, "_channel_manager", None, raising=False)
    path = _put(outbox, "report.csv")
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=path, kind="text")
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    _assert_withheld_not_delivered(out, store, ref)


@pytest.mark.asyncio
async def test_delivery_past_the_file_bound_withholds_and_drops(recorder, names, outbox, monkeypatch):
    monkeypatch.setattr(rb, "FILE_DELIVERY_TIMEOUT_S", 0.02)
    recorder.sleep_s = 0.5
    path = _put(outbox, "report.csv")
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=path, kind="text")
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    _assert_withheld_not_delivered(out, store, ref)
    assert _outbox_is_empty(outbox)


def test_the_file_bound_is_forty_five_seconds_under_the_hook_deadline():
    assert rb.FILE_DELIVERY_TIMEOUT_S == 45.0 < rb.HOOK_TIMEOUT_S


# --- the real channel method ---------------------------------------------------

@pytest.mark.asyncio
async def test_the_real_channel_method_sends_once_through_the_kinds_policy_method():
    from unittest.mock import AsyncMock, MagicMock
    from telegram import InputFile
    from telegram.error import TimedOut
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(bot=MagicMock(), chat_id="42")
    ch._app = None
    assert await ch.deliver_operator_file(42, DATA, "text", "r.csv", LABEL) is DeliveryOutcome.NOT_DELIVERED
    app = MagicMock()
    app.bot.send_document = AsyncMock(return_value=True)
    app.bot.send_photo = AsyncMock(return_value=True)
    ch._app = app
    assert await ch.deliver_operator_file(42, DATA, "text", "r.csv", LABEL + "\nc") is DeliveryOutcome.DELIVERED
    assert app.bot.send_document.await_count == 1 and app.bot.send_photo.await_count == 0
    args, kwargs = app.bot.send_document.await_args
    assert args[0] == 42 and isinstance(args[1], InputFile) and args[1].filename == "r.csv"
    # the caption as the bytes it is: no parse_mode, no thread id
    assert kwargs == {"caption": LABEL + "\nc"}
    assert await ch.deliver_operator_file(42, b"\x89PNG", "photo", "r.png", LABEL) is DeliveryOutcome.DELIVERED
    assert app.bot.send_photo.await_count == 1
    app.bot.send_document = AsyncMock(side_effect=TimedOut())
    with pytest.raises(TimedOut):
        await ch.deliver_operator_file(42, DATA, "text", "r.csv", LABEL)


@pytest.mark.asyncio
async def test_a_claim_that_lands_after_the_bound_is_still_removed(recorder, names, outbox, monkeypatch):
    """The outbox lock is held through the whole file bound: the claim lands
    after the hook has already withheld. Cancelling a thread does not stop
    it, so claim, capture and removal are one synchronous unit with its own
    cleanup — the file is consumed on this outcome too."""
    import threading
    import time
    monkeypatch.setattr(rb, "FILE_DELIVERY_TIMEOUT_S", 0.05)
    path = _put(outbox, "report.csv")
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=path, kind="text")
    outbox._lock.acquire()                      # the production sweep holds this lock too
    try:
        out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    finally:
        outbox._lock.release()
    _assert_withheld_not_delivered(out, store, ref)
    assert recorder.deliveries == []
    deadline = time.monotonic() + 5
    while not _outbox_is_empty(outbox) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert _outbox_is_empty(outbox)             # the late claim did not survive
