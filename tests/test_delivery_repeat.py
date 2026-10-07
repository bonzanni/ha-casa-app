"""#1312: a repeated plugin delivery is the same delivery (INV-PLUG-050).

A delivered slot's deposit may carry a ``key``. After a PROVEN delivery the key
is remembered per plugin and operator; a later deposit carrying it sends
nothing and gets the original delivery's receipt, with ``repeat: true``. A
proposal repeats only while its original keyboard is live, unclaimed and of the
same artifact. The memory is recorded at the proof point, survives a restart,
and a failed write never undoes a delivery.

Driven through the real store, result hook, ``_deliver_and_replace`` and verdict
broker with the recorder channels of the delivery suites.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

import delivery_keys as dk
import result_broker as rb
import test_operator_message_delivery as msg
import test_proposal_slot as prop
from channels import DeliveryOutcome
from test_operator_message_delivery import names, recorder  # noqa: F401
from test_proposal_slot import env  # noqa: F401

KEY = "r1042"


@pytest.fixture(autouse=True)
def keys(tmp_path, monkeypatch):
    store = dk.DeliveryKeys(str(tmp_path / "delivery_keys.json"))
    monkeypatch.setattr(dk, "KEYS", store)
    return store


# --- messages ---------------------------------------------------------------------

async def _deliver_message(store, hook, *, call, key=KEY):
    msg._open(store, call=call)
    ref, err = store.deposit(client_id="c1", slot=msg.SLOT, value=msg.BODY,
                             **({"key": key} if key is not None else {}))
    assert err is None
    out = await hook(msg._post(json.dumps({msg.SLOT: ref})), call, {})
    return ref, json.loads(msg._replacement(out))


@pytest.mark.asyncio
async def test_a_repeated_key_sends_nothing_and_returns_the_original_receipt(recorder, names):
    store, _ = msg._store()
    hook = rb.make_result_hook(msg._map(), client_id="c1", store=store)
    _, first = await _deliver_message(store, hook, call="call-1")
    ref2, again = await _deliver_message(store, hook, call="call-2")
    assert len(recorder.deliveries) == 1
    assert first["casa_delivery"] == {"slot": msg.SLOT, "status": "delivered",
                                      "to": "operator_chat", "pages": 1}
    assert again == {msg.SLOT: ref2,
                     "casa_delivery": {**first["casa_delivery"], "repeat": True}}
    assert ref2 not in store._refs or store._refs[ref2].used     # the repeat's deposit is spent


@pytest.mark.asyncio
async def test_a_new_key_or_no_key_posts_as_before(recorder, names):
    store, _ = msg._store()
    hook = rb.make_result_hook(msg._map(), client_id="c1", store=store)
    await _deliver_message(store, hook, call="call-1")
    await _deliver_message(store, hook, call="call-2", key="r1043")
    await _deliver_message(store, hook, call="call-3", key=None)
    await _deliver_message(store, hook, call="call-4", key=None)
    assert len(recorder.deliveries) == 4


@pytest.mark.parametrize("bad", ["", " ", "a b", "x" * 129, "é", 7, ["r1"]])
def test_a_malformed_key_is_refused_and_mints_nothing(bad, names):
    store, _ = msg._store()
    msg._open(store)
    assert store.deposit(client_id="c1", slot=msg.SLOT, value=msg.BODY, key=bad) == (None, "bad_key")
    assert store._refs == {}


@pytest.mark.asyncio
async def test_an_unproven_send_is_not_remembered(recorder, names, keys):
    store, _ = msg._store()
    hook = rb.make_result_hook(msg._map(), client_id="c1", store=store)
    recorder.outcome = DeliveryOutcome.NOT_DELIVERED
    _, first = await _deliver_message(store, hook, call="call-1")
    assert first.get("casa_result_withheld") is True
    recorder.outcome = DeliveryOutcome.DELIVERED
    _, again = await _deliver_message(store, hook, call="call-2")
    assert len(recorder.deliveries) == 2 and "repeat" not in again["casa_delivery"]


@pytest.mark.asyncio
async def test_the_memory_survives_a_restart(recorder, names, keys, tmp_path, monkeypatch):
    store, _ = msg._store()
    hook = rb.make_result_hook(msg._map(), client_id="c1", store=store)
    await _deliver_message(store, hook, call="call-1")
    path = str(tmp_path / "delivery_keys.json")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    reloaded = dk.DeliveryKeys()
    reloaded.load(path)                                   # a new process
    monkeypatch.setattr(dk, "KEYS", reloaded)
    store2, _ = msg._store()
    hook2 = rb.make_result_hook(msg._map(), client_id="c1", store=store2)
    _, again = await _deliver_message(store2, hook2, call="call-2")
    assert len(recorder.deliveries) == 1 and again["casa_delivery"]["repeat"] is True


@pytest.mark.asyncio
async def test_a_failed_write_keeps_the_delivery_and_the_memory(recorder, names, tmp_path, monkeypatch):
    unwritable = dk.DeliveryKeys(str(tmp_path / "missing-dir" / "delivery_keys.json"))
    unwritable.record("probe", 1, "r0", kind="operator_message", detail={"pages": 1})   # never raises
    assert unwritable.lookup("probe", 1, "r0") is not None
    monkeypatch.setattr(dk, "KEYS", unwritable)
    store, _ = msg._store()
    hook = rb.make_result_hook(msg._map(), client_id="c1", store=store)
    _, first = await _deliver_message(store, hook, call="call-1")
    assert first["casa_delivery"]["status"] == "delivered"
    _, again = await _deliver_message(store, hook, call="call-2")
    assert len(recorder.deliveries) == 1 and again["casa_delivery"]["repeat"] is True


@pytest.mark.asyncio
async def test_two_same_key_deliveries_at_once_send_once(recorder, names):
    """Two sessions of one plugin (one in-flight call each: a session cannot
    bind two deposits of one slot at once — ambiguous_call) deliver the same
    key together: one post, and the second gets the repeat receipt."""
    store, _ = msg._store()
    recorder.block = asyncio.Event()
    hooks, refs = [], []
    for client, call in (("c1", "call-1"), ("c2", "call-2")):
        store.open_call(client_id=client, artifact_id=msg.ARTIFACT, tool_name=msg.REPORT,
                        tool_use_id=call, identity=msg._identity(), provides=(msg.SLOT,),
                        delivers={msg.SLOT: "operator_message"})
        ref, err = store.deposit(client_id=client, slot=msg.SLOT, value=msg.BODY, key=KEY)
        assert err is None
        refs.append(ref)
        hooks.append(rb.make_result_hook(msg._map(), client_id=client, store=store))
    tasks = [asyncio.create_task(h(msg._post(json.dumps({msg.SLOT: r})), c, {}))
             for h, r, c in zip(hooks, refs, ("call-1", "call-2"))]
    await recorder.entered.wait()
    await asyncio.sleep(0.01)
    assert len(recorder.deliveries) == 1                 # the second waits on the key
    recorder.block.set()
    outs = [json.loads(msg._replacement(o)) for o in await asyncio.gather(*tasks)]
    assert len(recorder.deliveries) == 1
    assert sorted(bool(o["casa_delivery"].get("repeat")) for o in outs) == [False, True]
    assert dk.KEYS._locks == {}                           # retired when the last holder left


@pytest.mark.parametrize("entry", [
    {"at": 1.0, "kind": "operator_message"},                       # no detail
    {"at": 1.0, "kind": "operator_message", "detail": []},
    {"at": "x", "kind": "operator_message", "detail": {}},
    {"at": float("nan"), "kind": "operator_message", "detail": {}},
    {"at": True, "kind": "operator_message", "detail": {}},
    {"at": 10**400, "kind": "operator_message", "detail": {"pages": 1}},
    {"at": -10**400, "kind": "operator_message", "detail": {"pages": 1}},
    {"at": 1.0, "detail": {}},                                     # no kind
    {"at": 1.0, "kind": "operator_proposal", "detail": {}, "proposal": ["s"]},
    {"at": 1.0, "kind": "operator_message", "detail": {}},          # a message has pages
    {"at": 1.0, "kind": "operator_message", "detail": {"pages": 1}, "extra": 1},
    {"at": 1.0, "kind": "operator_proposal", "detail": {"proposal_id": "x", "buttons": 2}},
    {"at": 1.0, "kind": "operator_poll", "detail": {}},
    {"at": 1.0, "kind": [], "detail": {}},
    {"at": 1.0, "kind": {}, "detail": {}},
], ids=["no-detail", "detail-list", "at-str", "at-nan", "at-bool", "at-huge", "at-huge-neg",
        "no-kind", "proposal-short", "message-no-pages", "extra-member",
        "proposal-no-registration", "unknown-kind", "kind-list", "kind-dict"])
def test_a_malformed_entry_is_dropped_at_load(entry, tmp_path):
    path = tmp_path / "delivery_keys.json"
    import time as _time
    good = {"at": _time.time(), "kind": "operator_message", "detail": {"pages": 1}}
    if isinstance(entry.get("at"), float) and entry["at"] == 1.0:
        entry = {**entry, "at": _time.time()}         # only its shape is wrong, not its age
    path.write_text(json.dumps({'["probe", 42, "bad"]': entry, '["probe", 42, "good"]': good},
                               allow_nan=True))
    k = dk.DeliveryKeys()
    k.load(str(path))
    assert list(k._entries) == ['["probe", 42, "good"]']


def test_a_corrupt_or_missing_file_loads_empty(tmp_path):
    path = tmp_path / "delivery_keys.json"
    k = dk.DeliveryKeys()
    k.load(str(path))                                      # missing
    assert k._entries == {}
    path.write_text("{not json")
    k.load(str(path))
    assert k._entries == {}


def test_entries_expire_and_are_bounded_per_plugin_and_operator(tmp_path, monkeypatch):
    k = dk.DeliveryKeys(str(tmp_path / "k.json"))
    monkeypatch.setattr(dk, "MAX_PER_SCOPE", 2)
    now = [1000.0]
    monkeypatch.setattr(dk.time, "time", lambda: now[0])
    for i in range(3):
        now[0] += 1
        k.record("probe", 42, f"r{i}", kind="operator_message", detail={"pages": 1})
    k.record("other", 42, "r0", kind="operator_message", detail={"pages": 1})
    assert k.lookup("probe", 42, "r0") is None             # oldest of its scope dropped
    assert k.lookup("probe", 42, "r2") is not None
    assert k.lookup("other", 42, "r0") is not None         # another plugin keeps its own
    now[0] += dk.RETENTION_S + 1
    assert k.lookup("probe", 42, "r2") is None             # expired


# --- proposals --------------------------------------------------------------------

async def _deliver_proposal(env, *, call, identity=None, warning=None, revision="r-9"):
    prop._open(env.store, call=call, identity=identity)
    ref, err = env.store.deposit(client_id="c1", slot=prop.SLOT,
                                 value=json.dumps(prop._proposal(revision=revision)), key=KEY)
    assert err is None
    c = env.store.close_call("c1", call)
    out = await rb._deliver_and_replace(env.store, prop.SEG, c, {prop.SLOT: ref}, warning=warning)
    return json.loads(out["hookSpecificOutput"]["updatedToolOutput"])


@pytest.mark.asyncio
async def test_a_proposal_repeats_only_while_its_keyboard_is_live(env):
    first = await _deliver_proposal(env, call="call-1")
    again = await _deliver_proposal(env, call="call-2")
    assert len(env.rec.proposals) == 1
    assert again["casa_delivery"] == {**first["casa_delivery"], "repeat": True}
    # superseded by a newer card of the same revision (its record retired, its
    # meta still readable): the key's next delivery is a fresh card
    prop._open(env.store, call="call-3")
    ref, _ = env.store.deposit(client_id="c1", slot=prop.SLOT,
                               value=json.dumps(prop._proposal()), key="r2000")
    c = env.store.close_call("c1", "call-3")
    await rb._deliver_and_replace(env.store, prop.SEG, c, {prop.SLOT: ref})
    rid = first["casa_delivery"]["proposal_id"]
    assert env.broker.get_meta(namespace="proposal", scope="proposal:42", request_id=rid)
    fresh = await _deliver_proposal(env, call="call-4")
    assert len(env.rec.proposals) == 3 and "repeat" not in fresh["casa_delivery"]


@pytest.mark.asyncio
async def test_a_proposal_of_another_artifact_posts_a_fresh_card(env):
    await _deliver_proposal(env, call="call-1")
    fresh = await _deliver_proposal(env, call="call-2", identity=prop._identity(artifact_id="7" * 64))
    assert len(env.rec.proposals) == 2 and "repeat" not in fresh["casa_delivery"]


@pytest.mark.asyncio
async def test_a_repeated_proposal_still_tells_its_rewritten_input(env):
    notices = []

    async def notice(chat_id, text):
        notices.append((chat_id, text))
        return True
    env.rec.deliver_desk_notice = notice
    await _deliver_proposal(env, call="call-1")
    again = await _deliver_proposal(env, call="call-2", warning="⚠ the call was rewritten")
    assert len(env.rec.proposals) == 1 and again["casa_delivery"]["repeat"] is True
    assert notices == [(42, f"{prop.LABEL} ⚠ the call was rewritten")]


@pytest.mark.asyncio
async def test_a_proposal_is_remembered_at_its_proof_before_any_later_await(env, keys):
    """A cancellation after the post is proven (here: during the tell notice)
    cannot lose the memory: the retry repeats instead of posting again."""
    started = asyncio.Event()

    async def stuck_notice(chat_id, text):
        started.set()
        await asyncio.Event().wait()
    env.rec.deliver_desk_notice = stuck_notice
    long_warning = "⚠ " + "w" * 4000                      # cannot fit the card: told after
    task = asyncio.create_task(_deliver_proposal(env, call="call-1", warning=long_warning))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert keys.lookup(prop.SEG, 42, KEY) is not None
    again = await _deliver_proposal(env, call="call-2")
    assert len(env.rec.proposals) == 1 and again["casa_delivery"]["repeat"] is True


# --- files ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_repeated_file_consumes_its_staged_file_and_sends_nothing(monkeypatch):
    import tools as tools_mod
    calls = []

    class _Outbox:
        def claim(self, path):
            calls.append(("claim", path))
            return "claim-1"

        def remove_claim(self, claim):
            calls.append(("remove", claim))
    monkeypatch.setattr(tools_mod, "outbox_for_current_context", lambda: _Outbox())
    await rb._settle_repeat("probe", msg._identity(), rb.OPERATOR_FILE, "/outbox/q3.zip", None)
    assert calls == [("claim", "/outbox/q3.zip"), ("remove", "claim-1")]

    class _Broken:
        def claim(self, path):
            raise OSError("gone")
    monkeypatch.setattr(tools_mod, "outbox_for_current_context", lambda: _Broken())
    await rb._settle_repeat("probe", msg._identity(), rb.OPERATOR_FILE, "/outbox/q3.zip", None)


@pytest.mark.asyncio
async def test_a_key_remembered_for_another_kind_is_not_a_repeat(recorder, names, keys):
    keys.record(msg.PLUGIN, 42, KEY, kind="operator_file", detail={"kind": "zip"})
    store, _ = msg._store()
    hook = rb.make_result_hook(msg._map(), client_id="c1", store=store)
    _, out = await _deliver_message(store, hook, call="call-1")
    assert len(recorder.deliveries) == 1 and "repeat" not in out["casa_delivery"]


@pytest.mark.asyncio
async def test_a_proposal_superseded_during_the_repeat_tell_is_posted_fresh(env):
    """The repeat's tell awaits; a newer card of the same revision supersedes
    the original meanwhile: the retry is decided again and posts a fresh card
    (without a second tell) instead of reporting a dead keyboard delivered."""
    gate, started, notices = asyncio.Event(), asyncio.Event(), []

    async def notice(chat_id, text):
        notices.append(text)
        started.set()
        await gate.wait()
        return True
    env.rec.deliver_desk_notice = notice
    await _deliver_proposal(env, call="call-1")
    task = asyncio.create_task(_deliver_proposal(env, call="call-2", warning="⚠ rewritten"))
    await started.wait()
    prop._open(env.store, call="call-3")
    ref, _ = env.store.deposit(client_id="c1", slot=prop.SLOT,
                               value=json.dumps(prop._proposal()), key="r2000")
    c = env.store.close_call("c1", "call-3")
    await rb._deliver_and_replace(env.store, prop.SEG, c, {prop.SLOT: ref})   # supersedes
    gate.set()
    out = await task
    assert "repeat" not in out["casa_delivery"] and len(env.rec.proposals) == 3
    assert len(notices) == 1 and "⚠ rewritten" not in env.rec.proposals[-1][1]


@pytest.mark.asyncio
async def test_a_cancelled_proposal_keeps_its_echo_and_its_repeat_adds_none(env, keys):
    started = asyncio.Event()

    async def stuck_notice(chat_id, text):
        started.set()
        await asyncio.Event().wait()
    env.rec.deliver_desk_notice = stuck_notice
    task = asyncio.create_task(_deliver_proposal(env, call="call-1", warning="⚠ " + "w" * 4000))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    env.rec.deliver_desk_notice = None
    await _deliver_proposal(env, call="call-2")
    owner = rb._post_owner(prop._identity())
    assert len(rb.POSTS.drain(owner)) == 1


@pytest.mark.asyncio
async def test_an_unusable_remembered_entry_is_dropped_and_the_post_delivered(recorder, names, keys):
    """Whatever slipped past the load check (here: corrupted after load), an
    entry that cannot be used is no entry — the deposit is delivered fresh."""
    keys.record(msg.PLUGIN, 42, KEY, kind="operator_message", detail={"pages": 1})
    ident = keys._id(msg.PLUGIN, 42, KEY)
    keys._entries[ident]["at"] = 10**400
    store, _ = msg._store()
    hook = rb.make_result_hook(msg._map(), client_id="c1", store=store)
    _, out = await _deliver_message(store, hook, call="call-1")
    assert len(recorder.deliveries) == 1 and out["casa_delivery"]["status"] == "delivered"
    assert "repeat" not in out["casa_delivery"]


def test_a_bad_identifier_is_dropped_and_the_bounds_hold_at_load(tmp_path, monkeypatch):
    monkeypatch.setattr(dk, "MAX_PER_SCOPE", 2)
    monkeypatch.setattr(dk.time, "time", lambda: 10_000.0)
    good = lambda at: {"at": at, "kind": "operator_message", "detail": {"pages": 1}}  # noqa: E731
    raw = {json.dumps(["probe", 42, f"r{i}"]): good(1000.0 + i) for i in range(4)}
    raw[json.dumps(["probe", 42, "old"])] = good(10_000.0 - dk.RETENTION_S - 1)
    raw["not-json"] = good(1000.0)
    raw[json.dumps(["probe", "42", "r9"])] = good(1000.0)
    raw[json.dumps(["probe", 42, "bad key"])] = good(1000.0)
    path = tmp_path / "delivery_keys.json"
    path.write_text(json.dumps(raw))
    k = dk.DeliveryKeys()
    k.load(str(path))
    assert sorted(k._entries) == [json.dumps(["probe", 42, "r2"]), json.dumps(["probe", 42, "r3"])]


@pytest.mark.asyncio
async def test_an_entry_corrupted_after_load_is_dropped_at_use(recorder, names, keys):
    keys.record(msg.PLUGIN, 42, KEY, kind="operator_message", detail={"pages": 1})
    keys._entries[keys._id(msg.PLUGIN, 42, KEY)]["detail"] = {}
    store, _ = msg._store()
    hook = rb.make_result_hook(msg._map(), client_id="c1", store=store)
    _, out = await _deliver_message(store, hook, call="call-1")
    assert len(recorder.deliveries) == 1 and "repeat" not in out["casa_delivery"]
    assert keys._id(msg.PLUGIN, 42, KEY) in keys._entries      # the fresh delivery is remembered


@pytest.mark.asyncio
async def test_a_proposal_past_its_deadline_is_posted_fresh(env):
    """Past its deadline the tap path answers "expired", even before the
    broker's timer retired the record: no repeat for that card."""
    first = await _deliver_proposal(env, call="call-1")
    rid = first["casa_delivery"]["proposal_id"]
    meta = env.broker.get_meta(namespace="proposal", scope="proposal:42", request_id=rid)
    meta["deadline"] = asyncio.get_running_loop().time() - 1     # its timer has not run yet
    assert env.broker.is_live_unclaimed(namespace="proposal", scope="proposal:42", request_id=rid)
    fresh = await _deliver_proposal(env, call="call-2")
    assert len(env.rec.proposals) == 2 and "repeat" not in fresh["casa_delivery"]


def test_entry_validation_never_raises_on_a_decoded_value():
    for kind in ([], {}, None, 3):
        assert dk._entry_ok({"at": 1.0, "kind": kind, "detail": {}}) is False


def test_a_load_survives_a_validator_that_raises(tmp_path, monkeypatch):
    import time as _time
    path = tmp_path / "delivery_keys.json"
    good = {"at": _time.time(), "kind": "operator_message", "detail": {"pages": 1}}
    path.write_text(json.dumps({json.dumps(["probe", 42, "r1"]): good}))

    def boom(entry):
        raise RuntimeError("unforeseen")
    monkeypatch.setattr(dk, "_entry_ok", boom)
    k = dk.DeliveryKeys()
    k.load(str(path))                                    # never raises
    assert k._entries == {}


@pytest.mark.asyncio
async def test_a_file_repeat_keeps_its_decision_when_the_key_expires_during_cleanup(monkeypatch, keys):
    """The repeat consumes the staged file; a key that expires meanwhile must
    not turn the call into a fresh send of a file that is gone."""
    import tools as tools_mod
    now = [1000.0]
    monkeypatch.setattr(dk.time, "time", lambda: now[0])
    keys.record("probe", 42, KEY, kind="operator_file", detail={"kind": "zip"})
    now[0] += dk.RETENTION_S - 1

    class _Outbox:
        def claim(self, path):
            now[0] += 2                                   # the key expires during cleanup
            return "claim-1"

        def remove_claim(self, claim):
            pass
    monkeypatch.setattr(tools_mod, "outbox_for_current_context", lambda: _Outbox())
    sent = []

    async def no_send(*a, **k):
        sent.append(a)
        raise AssertionError("a repeat must not send")
    monkeypatch.setattr(rb, "_post_operator_file", no_send)
    store = rb.ReferenceStore(now=lambda: 1000.0)
    store.open_call(client_id="c1", artifact_id="a" * 64, tool_name="mcp__plugin_probe_api__f",
                    tool_use_id="call-1", identity=msg._identity(), provides=("f",),
                    delivers={"f": "operator_file"})
    ref, err = store.deposit(client_id="c1", slot="f", value="/outbox/q3.zip", kind="zip", key=KEY)
    assert err is None
    c = store.close_call("c1", "call-1")
    out = await rb._deliver_and_replace(store, "probe", c, {"f": ref})
    receipt = json.loads(out["hookSpecificOutput"]["updatedToolOutput"])
    assert receipt["casa_delivery"]["repeat"] is True and sent == []
