"""The tool handlers that emit or store model text (#1038 §5, §6.4, §6.5, §7):
each admits through the turn's scope, reports the exact line Casa added, and
refuses when no scope is bound. ``list_inbound_files`` arms the obligation;
``set_reminder`` stores the resolved note beside the entry; a delegation
stores it on its record and hands its child a scope view.
"""
from __future__ import annotations

import json
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import agent as agent_mod
import agent_inbox
import output_boundary as ob
import tools
from channels import DeliveryOutcome
from output_boundary import Admitted, IntentKind as K
from output_boundary_testing import scope as _scope

pytestmark = [pytest.mark.unit]

INVOICE = ("/data/agent-inbox/assistant/ready/1758500000000-a1b2.pdf", "invoice.pdf")
ANSWERED = "Casa: Test answered without opening “invoice.pdf” in this turn."
WROTE = "Casa: Test wrote this without opening “invoice.pdf”."


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _payload(result: dict) -> dict:
    return json.loads(_text(result))


class _Channel:
    name = "telegram"

    def __init__(self, outcome=DeliveryOutcome.DELIVERED):
        self.sent: list = []
        self.posted: list = []
        self.edited: list = []
        self._outcome = outcome

    async def send(self, message, context):
        self.sent.append(message)
        return self._outcome

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options, short_labels=False):
        self.posted.append(text)
        return 77

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edited.append(text)
        return True

    async def _dispatch_button_continuation(self, **kw):
        return True


class _CM:
    def __init__(self, *channels):
        self.channels = {c.name: c for c in channels}

    def get(self, name):
        return self.channels.get(name)


@pytest.fixture
def armed():
    """A bound origin whose scope listed the invoice and read nothing."""
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram",
                                      "chat_id": 500, "user_id": 7, "cid": "c",
                                      "message_type": "channel_in", "source": "telegram",
                                      "execution_role": "assistant",
                                      "turn_scope": s})
    yield s
    agent_mod.origin_var.reset(token)


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------

async def test_send_message_admits_and_reports_the_line(monkeypatch, armed):
    ch = _Channel()
    monkeypatch.setattr(tools, "_channel_manager", _CM(ch))
    out = _text(await tools.send_message.handler({"message": "It's €412.", "channel": "telegram"}))
    assert isinstance(ch.sent[0], Admitted)
    assert ch.sent[0] == ANSWERED + "\n\nIt's €412."
    assert out == f"Message sent via telegram. Casa prefixed: “{ANSWERED}”"


async def test_send_message_owing_nothing_keeps_todays_result_text(monkeypatch):
    ch = _Channel()
    monkeypatch.setattr(tools, "_channel_manager", _CM(ch))
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram",
                                      "turn_scope": _scope()})
    try:
        out = _text(await tools.send_message.handler({"message": "Bins out.", "channel": "telegram"}))
    finally:
        agent_mod.origin_var.reset(token)
    assert out == "Message sent via telegram."
    assert isinstance(ch.sent[0], Admitted) and ch.sent[0] == "Bins out."


async def test_send_message_with_no_scope_bound_is_refused(monkeypatch):
    ch = _Channel()
    monkeypatch.setattr(tools, "_channel_manager", _CM(ch))
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram"})
    try:
        res = await tools.send_message.handler({"message": "x", "channel": "telegram"})
    finally:
        agent_mod.origin_var.reset(token)
    assert res.get("is_error") is True
    assert "turn provenance" in _text(res)
    assert ch.sent == []


async def test_a_not_delivered_send_is_still_an_error_after_admission(monkeypatch, armed):
    ch = _Channel(outcome=DeliveryOutcome.NOT_DELIVERED)
    monkeypatch.setattr(tools, "_channel_manager", _CM(ch))
    res = await tools.send_message.handler({"message": "x", "channel": "telegram"})
    assert res.get("is_error") is True
    assert "Casa prefixed" not in _text(res)


# ---------------------------------------------------------------------------
# send_media: the caption, and the refused arm
# ---------------------------------------------------------------------------

async def test_classify_send_reports_a_refused_caption_not_a_down_channel():
    class _Ch:
        async def send_media(self, *a, **k):
            raise ob.UnadmittedText("caption")
    out = await tools._classify_send(_Ch(), b"x", "document", "x.pdf", {"chat_id": 1}, "cap")
    assert out["status"] == "error" and out["kind_error"] == "refused"


# ---------------------------------------------------------------------------
# ask_user: the body and its settle edits
# ---------------------------------------------------------------------------

async def test_ask_user_posts_an_admitted_body_carrying_the_line(monkeypatch, armed):
    """The keyboard body is admitted under the turn's scope; its settle edits
    derive from that same body by construction (tools.py's finish hook closes
    over ``body``), so the line they carry is this one."""
    ch = _Channel()
    monkeypatch.setattr(tools, "_channel_manager", _CM(ch))
    res = await tools.ask_user.handler({"question": "Which one?", "options": ["a", "b"]})
    assert ch.posted, _text(res)
    body = ch.posted[0]
    assert isinstance(body, Admitted)
    assert body.startswith(ANSWERED + "\n\nWhich one?")
    assert body.annotations == (ANSWERED,)


# ---------------------------------------------------------------------------
# list_inbound_files arms the obligation from what it showed
# ---------------------------------------------------------------------------

async def test_list_inbound_files_arms_the_obligation_with_the_listed_files(tmp_path):
    agent_inbox._reset_for_tests()
    ib = agent_inbox.open_inbox("assistant", str(tmp_path))
    agent_inbox._inboxes["assistant"] = ib
    try:
        import os
        with open(os.path.join(ib.ready_dir, "1758500000000-0123456789abcdef.pdf"), "wb") as f:
            f.write(b"%PDF-1.4\n")
        s = _scope()
        token = agent_mod.origin_var.set({"role": "assistant", "execution_role": "assistant",
                                          "turn_scope": s})
        try:
            await tools.list_inbound_files.handler({})
        finally:
            agent_mod.origin_var.reset(token)
        line = ("Casa: Test answered without opening “1758500000000-0123456789abcdef.pdf” in this turn.")
        assert s.admit(K.FINAL_REPLY, "It's €412").annotations == (line,)
    finally:
        agent_inbox._reset_for_tests()


# ---------------------------------------------------------------------------
# set_reminder stores the resolved note beside the entry
# ---------------------------------------------------------------------------

def _reminder_runtime(tmp_path):
    agents_dir = tmp_path / "agents"
    (agents_dir / "assistant").mkdir(parents=True)
    path = agents_dir / "assistant" / "triggers.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 1, "triggers": []}))
    registry = MagicMock()
    registry.register_agent = MagicMock()
    registry.has_job = MagicMock(return_value=True)
    runtime = types.SimpleNamespace(
        agents_dir=str(agents_dir), trigger_registry=registry,
        role_configs={"assistant": types.SimpleNamespace(role="assistant", channels=["telegram"])},
    )
    return runtime, path, registry


async def test_set_reminder_stores_the_note_and_registers_a_spec_that_carries_it(tmp_path, monkeypatch, armed):
    runtime, path, registry = _reminder_runtime(tmp_path)
    monkeypatch.setattr(tools, "_runtime", runtime)
    monkeypatch.setenv("CASA_TZ", "Europe/Amsterdam")
    from timekeeping import resolve_tz
    resolve_tz.cache_clear()
    res = await tools.set_reminder.handler({"text": "Pay the €412 invoice", "at": "2099-08-03T08:00:00+02:00"})
    payload = _payload(res)
    assert payload["status"] == "ok", payload
    assert payload["note"] == WROTE
    doc = yaml.safe_load(path.read_text())
    entry = doc["triggers"][0]
    assert entry["output_note"] == WROTE
    assert "Pay the €412 invoice" in entry["prompt"] and WROTE not in entry["prompt"]
    spec = registry.register_agent.call_args.args[1][0]
    assert spec.output_note == WROTE


async def test_set_reminder_after_the_read_stores_no_note(tmp_path, monkeypatch, armed):
    runtime, path, registry = _reminder_runtime(tmp_path)
    monkeypatch.setattr(tools, "_runtime", runtime)
    armed.note_read_ok(INVOICE[0])
    res = await tools.set_reminder.handler({"text": "Pay the €412 invoice", "at": "2099-08-03T08:00:00+02:00"})
    assert _payload(res)["status"] == "ok"
    entry = yaml.safe_load(path.read_text())["triggers"][0]
    assert "output_note" not in entry
    assert registry.register_agent.call_args.args[1][0].output_note == ""


# ---------------------------------------------------------------------------
# INV-OUT-004, second half: an emitting handler that outlives its turn commits
# under the scope it captured at entry — never the holder's current contents
# ---------------------------------------------------------------------------

async def test_a_paused_send_media_commits_under_the_scope_captured_at_entry(tmp_path, monkeypatch):
    """Turn A's send_media pauses inside the outbox capture (an await that a
    pooled client's next turn can straddle); turn B rewrites the holder IN PLACE
    before it resumes. The caption must still carry A's line: the scope is
    read off the handler's own entry snapshot, not looked up again after the
    await (Astra r2: a post-await lookup would pass every other test here)."""
    import asyncio
    import os
    import threading
    import plugin_outbox

    ob_ = plugin_outbox.init_outbox(str(tmp_path / "plugin-outbox"))
    ch = MagicMock()
    ch.send_media = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = ch
    tools.init_tools(channel_manager=cm, bus=MagicMock(), specialist_registry=MagicMock(),
                     mcp_registry=MagicMock(), trigger_registry=MagicMock(),
                     engagement_registry=MagicMock())
    a_scope = _scope()
    a_scope.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    holder = {"role": "assistant", "channel": "telegram", "chat_id": 1197017861,
              "turn_scope": a_scope}
    entered, release = threading.Event(), threading.Event()
    real_capture = plugin_outbox.PluginOutbox.capture

    def _slow_capture(self, claim, kind):
        entered.set()
        release.wait(5)
        return real_capture(self, claim, kind)

    monkeypatch.setattr(plugin_outbox.PluginOutbox, "capture", _slow_capture)
    path = os.path.join(ob_._root_realpath, "invoice.pdf")
    with open(path, "wb") as fh:
        fh.write(b"%PDF-1.4\n" + b"x" * 64)
    token = agent_mod.origin_var.set(holder)
    try:
        task = asyncio.create_task(tools.send_media.handler(
            {"path": path, "kind": "document", "caption": "Here it is"}))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        holder.clear()
        holder.update({"role": "assistant", "channel": "telegram", "chat_id": 2,
                       "turn_scope": _scope(id="turn-B")})
        release.set()
        res = await task
    finally:
        agent_mod.origin_var.reset(token)
        ob_.close()
        plugin_outbox._OUTBOX = None
    assert _payload(res)["status"] == "ok", _payload(res)
    caption = ch.send_media.await_args.kwargs["caption"]
    assert isinstance(caption, Admitted)
    assert caption == ANSWERED + "\n\nHere it is"
    assert ch.send_media.await_args.kwargs["context"]["chat_id"] == 1197017861


def test_current_scope_prefers_the_entry_snapshot_over_the_holder():
    """INV-OUT-004's mechanism, pinned directly: every emitting tool admits
    before its first await today, so no handler can reach a post-await lookup
    — the mutant "read the holder again" is unreachable through them and the
    paused-handler test above cannot kill it. This can: the holder now holds
    turn B, the handler's entry snapshot is turn A's, and A's scope is what
    the resolution returns."""
    a_scope = _scope(id="turn-A")
    entry_snapshot = {"role": "assistant", "channel": "telegram", "turn_scope": a_scope}
    holder = {"role": "assistant", "channel": "telegram", "turn_scope": _scope(id="turn-B")}
    token = agent_mod.origin_var.set(holder)
    try:
        assert tools._current_scope(entry_snapshot) is a_scope
        assert tools._current_scope().id == "turn-B"       # no snapshot given: the holder
    finally:
        agent_mod.origin_var.reset(token)


async def test_an_authz_challenge_body_is_admitted_under_the_turn(monkeypatch, armed):
    """A protected-tool challenge interpolates the model's own tool arguments,
    so its body is model text (round 1, Astra): posted through the same
    keyboard path as `ask_user`, it is admitted under the initiating turn's
    scope and carries the line when that turn listed a file and read none."""
    from test_authz_grants import _create, _fresh_env, _settle
    broker, coord, channel = _fresh_env(monkeypatch)
    _key, handle = _create(coord, channel,
                           canonical_json='{"amount":412,"id":"INV-1"}')
    await _settle()
    assert channel.posts, "the challenge was not posted"
    body = channel.posts[0][2]
    assert isinstance(body, Admitted)
    assert body.startswith(ANSWERED)
    assert "INV-1" in body                      # the model's arguments are still there
    assert body.annotations == (ANSWERED,)
