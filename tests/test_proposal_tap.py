"""S5 §3–§4: the proposal keyboard and the tap — the channel posts the
labelled text with one button per stored call and files the message; a tap
is admitted only in the design's order (presence, the live meta, the chat,
the MESSAGE, the index, the operator — then and now —, S5's own deadline),
claimed and committed once, never dispatched from the handler; the finish
hook edits the keyboard away first and then hands the tap to the desk
(INV-PROP-001). Everything else about a callback is today's path.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

import pytest

import result_broker as rb
import specialist_desk as sd
import verdict_broker as vb
from channels.telegram import TelegramChannel, _parse_callback_data
from bus import MessageBus

OPERATOR = 42
RID = "f" * 32


class _FakeBot:
    def __init__(self):
        self.sent, self.edited, self.markups = [], [], []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=501)

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        return True

    async def edit_message_reply_markup(self, **kwargs):
        self.markups.append(kwargs)
        return True


async def _noop(*a, **k):
    return None


def _channel():
    bus = MessageBus()
    bus.register("assistant", _noop)
    ch = TelegramChannel(bot_token="T", chat_id=str(OPERATOR), default_agent="assistant", bus=bus)
    ch._start_typing = lambda *a, **k: None
    ch._app = types.SimpleNamespace(bot=_FakeBot())
    ch._session_registry = None
    ch._semantic_memory = None
    return ch


def _cq(*, data=f"v1|proposal|{RID}|0", chat_id=OPERATOR, message_id=501, user_id=OPERATOR,
        message=True, user=True):
    cq = types.SimpleNamespace(
        id="cq1", data=data,
        message=(types.SimpleNamespace(message_id=message_id, chat=types.SimpleNamespace(id=chat_id),
                                       message_thread_id=None) if message else None),
        answer=AsyncMock(return_value=None),
        from_user=(types.SimpleNamespace(id=user_id) if user else None))
    return types.SimpleNamespace(callback_query=cq)


def _meta(**over):
    base = {"deadline": 10_000.0, "chat_id": OPERATOR, "operator_id": OPERATOR, "role": "finance",
            "artifact_id": "7" * 64, "plugin_seg": "probe", "label": "📊 Finance",
            "text": "📊 Finance\nPair 17?", "options": ["Yes", "No"], "revision": "",
            "message_id": 501, "owner": "d-1", "_scope": f"proposal:{OPERATOR}",
            "calls": [{"server": "api", "wire_name": "apply", "runtime_name": "mcp__plugin_probe_api__apply",
                       "proposal": False, "arguments": {"choice": "yes"}, "canonical": '{"choice":"yes"}'},
                      {"server": "api", "wire_name": "apply", "runtime_name": "mcp__plugin_probe_api__apply",
                       "proposal": False, "arguments": {"choice": "no"}, "canonical": '{"choice":"no"}'}]}
    base.update(over)
    return base


@pytest.fixture
def env(monkeypatch):
    broker = vb.VerdictBroker()
    monkeypatch.setattr(vb, "BROKER", broker)
    monkeypatch.setattr(rb, "POST_MAP", rb.PostMap())
    monkeypatch.setattr(sd, "DESKS", sd.DeskRegistry())
    monkeypatch.setattr(sd, "DESK_ECHO", rb.PostLedger(max_events=64))
    ch = _channel()
    taps = []

    async def fake_handle_tap(**kwargs):
        taps.append(kwargs)
        res = kwargs.get("reservation")
        if res is not None:
            res.release()
    monkeypatch.setattr(sd, "handle_tap", fake_handle_tap, raising=False)
    hooks = []

    def register(meta=None, deadline=None, chat=OPERATOR):
        m = _meta(**(meta or {}))
        if deadline is not None:
            m["deadline"] = asyncio.get_running_loop().time() + deadline
        else:
            m["deadline"] = asyncio.get_running_loop().time() + 3600
        req, _ = broker.register(namespace="proposal", scope=f"proposal:{chat}", request_id=RID,
                                 timeout_s=3600, detached=True, meta=m)
        broker.set_finish_hook(req, ch.proposal_finish_hook(rid=RID, req=req))
        return req
    return types.SimpleNamespace(ch=ch, bot=ch._app.bot, broker=broker, taps=taps, register=register)


async def _settle():
    for _ in range(4):
        await asyncio.sleep(0)


# --- the keyboard --------------------------------------------------------------------

def test_the_parser_accepts_the_proposal_namespace():
    assert _parse_callback_data(f"v1|proposal|{RID}|2") == ("proposal", RID, 2, None)
    assert _parse_callback_data("v1|proposal||0") == (None, None, None, None)
    assert _parse_callback_data(f"v1|proposal|{RID}|x") == (None, None, None, None)


async def test_the_keyboard_is_posted_with_one_button_per_label_and_the_message_is_filed(env):
    rec = rb.PostRecord(role="finance", operator_id=OPERATOR, plugin="probe", slot="proposal",
                        tool_use_id="call-1", owner="d-1", posted_at=1.0, kind="operator_proposal")
    mid = await env.ch.deliver_operator_proposal(OPERATOR, "📊 Finance\nPair **17**?", ["Yes", "No"], RID,
                                                 post=rec)
    assert mid == 501
    (sent,), = [env.bot.sent]
    rows = sent["reply_markup"].inline_keyboard
    assert [b.text for row in rows for b in row] == ["Yes", "No"]
    assert [b.callback_data for row in rows for b in row] == [f"v1|proposal|{RID}|0", f"v1|proposal|{RID}|1"]
    assert rb.POST_MAP.get(OPERATOR, 501) is rec


# --- the tap's admission, in the design's order --------------------------------------

async def _tap(env, update):
    await env.ch._on_inline_callback(update, context=None)
    return update.callback_query.answer.await_args.args[0]


async def test_a_callback_without_a_message_or_an_actor_is_expired_before_any_lookup(env):
    env.register()
    assert await _tap(env, _cq(message=False)) == "expired"
    assert await _tap(env, _cq(user=False)) == "expired"
    live = env.broker.pending(namespace="proposal", scope=f"proposal:{OPERATOR}")
    assert live == [RID] and env.taps == [] and env.bot.edited == []


async def test_an_unknown_request_is_expired(env):
    assert await _tap(env, _cq()) == "expired"


async def test_a_live_record_whose_chat_differs_is_expired(env):
    env.register(meta={"chat_id": 7})
    assert await _tap(env, _cq()) == "expired"
    assert env.broker.pending(namespace="proposal", scope=f"proposal:{OPERATOR}") == [RID]


async def test_a_callback_on_another_message_is_expired(env):
    env.register()
    assert await _tap(env, _cq(message_id=502)) == "expired"
    assert env.broker.pending(namespace="proposal", scope=f"proposal:{OPERATOR}") == [RID]


async def test_an_index_outside_the_calls_is_invalid(env):
    env.register()
    assert await _tap(env, _cq(data=f"v1|proposal|{RID}|2")) == "invalid"
    assert env.broker.pending(namespace="proposal", scope=f"proposal:{OPERATOR}") == [RID]


async def test_a_tap_by_someone_else_or_by_an_operator_no_longer_configured_is_not_for_them(env):
    env.register()
    assert await _tap(env, _cq(user_id=99)) == "not for you"
    env.ch._user_id_is_operator = lambda uid: False
    assert await _tap(env, _cq()) == "not for you"
    assert env.broker.pending(namespace="proposal", scope=f"proposal:{OPERATOR}") == [RID]


async def test_a_tap_past_the_deadline_is_expired_even_before_the_broker_timer_fires(env):
    env.register(deadline=-1)
    assert await _tap(env, _cq()) == "expired"
    assert env.broker.pending(namespace="proposal", scope=f"proposal:{OPERATOR}") == [RID]


async def test_a_valid_tap_commits_once_and_the_handler_dispatches_nothing_itself(env):
    env.register()
    assert await _tap(env, _cq(data=f"v1|proposal|{RID}|1")) == "✔"
    assert await _tap(env, _cq(data=f"v1|proposal|{RID}|0")) == "already answered"
    await _settle()
    # the finish hook: the keyboard edited away FIRST (the chosen label), then the desk
    assert len(env.bot.edited) == 1
    edited = env.bot.edited[0]
    assert edited["message_id"] == 501 and "☑ No" in edited["text"]
    assert len(env.taps) == 1
    tap = env.taps[0]
    assert tap["desk_role"] == "finance" and tap["idx"] == 1 and tap["request_id"] == RID
    assert tap["meta"]["calls"][1]["canonical"] == '{"choice":"no"}'


async def test_a_tap_after_expiry_or_after_a_restart_is_expired(env):
    req = env.register()
    env.broker.cancel(namespace="proposal", scope=f"proposal:{OPERATOR}", request_id=RID, reason="timeout")
    await _settle()
    assert await _tap(env, _cq()) == "expired"
    assert "⌛" in env.bot.edited[-1]["text"]
    fresh = vb.VerdictBroker()                       # a restart: the broker is empty
    vb.BROKER = fresh
    assert await _tap(env, _cq()) == "expired"


async def test_a_superseded_proposal_is_edited_and_a_tap_on_it_is_replaced(env):
    env.register(meta={"revision": "r1"})
    env.broker.cancel_where(namespace="proposal", reason="superseded", predicate=lambda r: True)
    await _settle()
    assert "↻" in env.bot.edited[-1]["text"]
    assert await _tap(env, _cq()) == "replaced"
    assert env.taps == []


# --- #1201: a tap on a settled keyboard says what happened, for the proposal's whole hour ---

def _live_meta(env):
    return env.broker.get_meta(namespace="proposal", scope=f"proposal:{OPERATOR}", request_id=RID)


def _restart(env):
    vb.BROKER = vb.VerdictBroker()                   # the broker is empty…
    env.ch = _channel()                              # …and so is the channel


@pytest.mark.parametrize("retired", [False, True], ids=["tombstone", "retired"])
@pytest.mark.parametrize("settled", ["answered", "superseded"])
@pytest.mark.parametrize("case", ["operator", "someone else", "deconfigured", "other message",
                                  "restart"])
async def test_a_tap_on_a_settled_proposal_answers_what_happened(env, monkeypatch, settled,
                                                                   retired, case):
    if retired:
        monkeypatch.setattr(vb, "_RETIRE_S", 0)      # set BEFORE the settlement
    req = env.register(meta={"revision": "r1"})
    if settled == "answered":
        first = _cq()
        assert await _tap(env, first) == "✔"
        assert first.callback_query.answer.await_count == 1
    else:
        assert env.broker.cancel_where(namespace="proposal", reason="superseded",
                                       predicate=lambda r: True) == [RID]
    await _settle()
    assert len(env.taps) == (1 if settled == "answered" else 0)
    if settled == "superseded":
        assert len(env.bot.edited) == 1 and "↻ replaced" in env.bot.edited[0]["text"]
    assert asyncio.get_running_loop().time() < req.meta["deadline"]   # inside the proposal's hour
    assert (_live_meta(env) is None) is retired      # the tombstone really is gone (or there)
    update = _cq()
    if case == "someone else":
        update = _cq(user_id=99)
    elif case == "deconfigured":
        env.ch._user_id_is_operator = lambda uid: False
    elif case == "other message":
        update = _cq(message_id=502)
    elif case == "restart":
        _restart(env)
    before = list(env.taps)
    toast = await _tap(env, update)
    await _settle()
    assert update.callback_query.answer.await_count == 1
    assert env.taps == before
    expected = {"operator": "already answered" if settled == "answered" else "replaced",
                "someone else": "not for you", "deconfigured": "not for you",
                "other message": "expired", "restart": "expired"}[case]
    assert toast == expected


@pytest.mark.parametrize("retired", [False, True], ids=["tombstone", "retired"])
@pytest.mark.parametrize("ending", ["timeout", "no_answer", "casa_shutdown"])
async def test_a_tap_on_a_proposal_ended_otherwise_is_expired(env, monkeypatch, ending, retired):
    if retired:
        monkeypatch.setattr(vb, "_RETIRE_S", 0)
    req = env.register(meta={"revision": "r1"})
    if ending == "no_answer":
        env.broker._on_timeout(req.key)
    elif ending == "casa_shutdown":
        env.broker.cancel_all(reason="casa_shutdown")
    else:
        env.broker.cancel(namespace="proposal", scope=f"proposal:{OPERATOR}", request_id=RID,
                          reason="timeout")
    await _settle()
    assert (_live_meta(env) is None) is retired
    update = _cq()
    assert await _tap(env, update) == "expired"
    await _settle()
    assert update.callback_query.answer.await_count == 1
    assert env.taps == []


@pytest.mark.parametrize("retired", [False, True], ids=["tombstone", "retired"])
async def test_a_proposal_superseded_during_its_send_answers_replaced_on_the_landed_message(
        env, monkeypatch, retired):
    if retired:
        monkeypatch.setattr(vb, "_RETIRE_S", 0)
    req = env.register(meta={"revision": "r1", "message_id": None})    # the send is in flight
    env.broker.cancel_where(namespace="proposal", reason="superseded", predicate=lambda r: True)
    await _settle()
    req.meta["message_id"] = 501                     # the send lands (the poster's own write)
    assert (_live_meta(env) is None) is retired
    update = _cq()
    assert await _tap(env, update) == "replaced"
    await _settle()
    assert update.callback_query.answer.await_count == 1
    assert env.taps == []


async def test_a_full_desk_queue_refuses_the_tap_with_the_busy_line_and_no_task(env):
    env.register()
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    places = [desk.reserve() for _ in range(sd.DESK_QUEUE_MAX)]
    assert all(places)
    assert await _tap(env, _cq()) == "✔"
    await _settle()
    assert env.taps == []
    assert any("✖ busy" in e["text"] for e in env.bot.edited)
    assert any("busy" in s.get("text", "") for s in env.bot.sent)
    assert desk.waiting == sd.DESK_QUEUE_MAX
    # the resident's line records WHICH tap was refused and why (Astra B, diff round 4),
    # as the permit-refusal path does — never the operator's notice text
    assert sd.drain_echo_lines(OPERATOR) == [f"{sd.label_for('finance')} refused your tap (Yes): busy."]


# --- diff round 1 (Astra B S2): a supersede during a pending send -------------------------

async def test_a_supersede_during_a_pending_send_marks_the_late_keyboard_replaced(env, monkeypatch):
    import json
    import tools as tools_mod
    from test_proposal_slot import SLOT, _map, _open, _post, _proposal
    store = rb.ReferenceStore(now=lambda: 1000.0)
    monkeypatch.setattr(tools_mod, "_channel_manager", types.SimpleNamespace(get=lambda n: env.ch))
    monkeypatch.setattr(tools_mod, "_agent_role_map",
                        {"finance": types.SimpleNamespace(character=types.SimpleNamespace(name="Finance"))})
    gate, sends = asyncio.Event(), []

    async def slow_send(**kwargs):
        sends.append(kwargs)
        n = len(sends)                                  # the id this send will land as
        if n == 1:
            await gate.wait()                           # the first keyboard's send is in flight…
        return types.SimpleNamespace(message_id=500 + n)
    env.bot.send_message = slow_send
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    _open(store, call="c-1")
    ref1, _ = store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(revision="r1")))
    first = asyncio.create_task(hook(_post(json.dumps({SLOT: ref1})), "c-1", {}))
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(sends) == 1 and len(env.broker.pending(namespace="proposal", scope=f"proposal:{OPERATOR}")) == 1
    _open(store, call="c-2")
    ref2, _ = store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(revision="r1")))
    await hook(_post(json.dumps({SLOT: ref2})), "c-2", {})   # …when the same revision supersedes it
    gate.set()
    await first
    await _settle()
    assert len(env.broker.pending(namespace="proposal", scope=f"proposal:{OPERATOR}")) == 1
    late = [e for e in env.bot.edited if e["message_id"] == 501]
    assert len(late) == 1 and "↻ replaced" in late[0]["text"]
    assert all("↻" not in e["text"] for e in env.bot.edited if e["message_id"] == 502)


# --- diff round 2 (Astra B S2): a proposal leaves room for its settlement line -------------

async def test_a_maximal_proposals_settlement_edit_fits_telegrams_limit(env, monkeypatch):
    import json
    import tools as tools_mod
    from text_util import utf16_len
    from test_proposal_slot import LABEL, SLOT, _map, _open, _post, _proposal
    store = rb.ReferenceStore(now=lambda: 1000.0)
    monkeypatch.setattr(tools_mod, "_channel_manager", types.SimpleNamespace(get=lambda n: env.ch))
    monkeypatch.setattr(tools_mod, "_agent_role_map",
                        {"finance": types.SimpleNamespace(character=types.SimpleNamespace(name="Finance"))})
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    label32 = "🙂" * 32                                    # 32 characters, 64 UTF-16 units: the widest label admitted
    # the largest body the deposit admits with that label: find it by probing the deposit
    body_units = 4096
    while True:
        _open(store, call=f"c-{body_units}")
        ref, err = store.deposit(client_id="c1", slot=SLOT, value=json.dumps(
            _proposal(text="🙂" * (body_units // 2), buttons=[{"label": label32, "call": {"tool": "apply", "arguments": {}}}])))
        if err is None:
            break
        store.close_call("c1", f"c-{body_units}")
        body_units -= 2
    composed = f"{LABEL}\n" + "🙂" * (body_units // 2)
    assert utf16_len(composed) <= 4096 - rb.PROPOSAL_SETTLE_RESERVE
    out = await hook(_post(json.dumps({SLOT: ref})), f"c-{body_units}", {})
    assert "casa_delivery" in out["hookSpecificOutput"]["updatedToolOutput"]
    (rid,) = env.broker.pending(namespace="proposal", scope=f"proposal:{OPERATOR}")
    assert await _tap(env, _cq(data=f"v1|proposal|{rid}|0")) == "✔"
    await _settle()
    (edit,) = env.bot.edited
    assert edit["text"].endswith(f"\n☑ {label32}") and utf16_len(edit["text"]) <= 4096


# --- diff round 5 (Terra S2): the keyboard edit fails -------------------------------------

async def test_a_failed_keyboard_edit_tells_which_button_won_before_the_dispatch(env, monkeypatch):
    """The design's order is "the keyboard edited away FIRST, then the desk":
    when Telegram refuses the edit, the operator must still see which button
    won before any effect — one labelled notice names it — and the tap is
    still applied (the broker's commit already excludes a second execution)."""
    env.register()
    order = []

    async def failing_edit(chat_id, message_id, text):
        order.append(("edit", text))
        return False
    monkeypatch.setattr(env.ch, "edit_dm_message", failing_edit)
    real_notice = env.ch.deliver_desk_notice

    async def notice(chat_id, text):
        order.append(("notice", text))
        return await real_notice(chat_id, text)
    monkeypatch.setattr(env.ch, "deliver_desk_notice", notice)
    assert await _tap(env, _cq(data=f"v1|proposal|{RID}|1")) == "✔"
    await _settle()
    assert len(env.taps) == 1 and env.taps[0]["idx"] == 1          # still applied
    kinds = [k for k, _ in order]
    assert kinds[:2] == ["edit", "notice"]                          # the telling precedes the dispatch
    assert "☑ No" in order[1][1]
    assert any("☑ No" in s.get("text", "") for s in env.bot.sent)


async def test_a_supersede_is_answered_replaced_before_its_finish_task_has_run(env):
    env.register(meta={"revision": "r1"})
    env.broker.cancel_where(namespace="proposal", reason="superseded", predicate=lambda r: True)
    # no loop turn: the finish hook's edit task has not started
    assert env.bot.edited == []
    assert await _tap(env, _cq()) == "replaced"
    await _settle()
    assert env.taps == []


async def test_a_settled_proposal_is_forgotten_at_its_deadline(env, monkeypatch):
    monkeypatch.setattr(vb, "_RETIRE_S", 0)
    req = env.register()
    assert await _tap(env, _cq()) == "✔"
    await _settle()
    assert RID in env.ch._proposal_settled
    req.meta["deadline"] = asyncio.get_running_loop().time() - 1   # the hour is over
    update = _cq()
    assert await _tap(env, update) == "expired"
    assert RID not in env.ch._proposal_settled
    assert update.callback_query.answer.await_count == 1 and len(env.taps) == 1


async def test_the_settled_memory_keeps_a_chats_latest_entries_only(env):
    import channels.telegram as tg
    deadline = asyncio.get_running_loop().time() + 3600
    other = {"chat_id": 7, "deadline": deadline}
    env.ch._remember_settled_proposal("other", other, {"outcome": "no_answer"})
    n = tg._PROPOSAL_SETTLED_PER_CHAT + 3
    for i in range(n):
        env.ch._remember_settled_proposal(f"r{i}", {"chat_id": OPERATOR, "deadline": deadline},
                                          {"outcome": "answered"})
    mine = [k for k, (m, _) in env.ch._proposal_settled.items() if m["chat_id"] == OPERATOR]
    assert len(mine) == tg._PROPOSAL_SETTLED_PER_CHAT
    assert mine[0] == "r3" and mine[-1] == f"r{n - 1}"
    assert env.ch._proposal_settled["other"] == (other, "expired")
