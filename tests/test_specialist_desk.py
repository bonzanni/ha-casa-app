"""S4 §4/§6: the desk — one specialist's thread with one chat, kept as a
bounded dialogue log (never an SDK resume), reset after idle, one use at a
time under its lock with a count-bounded queue — and the body-free echo the
chat's resident drains at its next turn (INV-DESK-003, the echo half of
INV-DESK-002).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import specialist_desk as sd
import tools as tools_mod


def _desk(now=1000.0):
    reg = sd.DeskRegistry(now=lambda: now)
    return reg, reg.get_or_create(42, "finance")


# --- registry and window ---------------------------------------------------------

def test_a_desk_is_keyed_by_int_chat_and_role_and_created_once():
    reg = sd.DeskRegistry()
    a = reg.get_or_create(42, "finance")
    assert reg.get_or_create(42, "finance") is a
    assert reg.get_or_create(42, "concierge") is not a
    assert reg.get_or_create(43, "finance") is not a
    assert reg.get(42, "finance") is a and reg.get(7, "finance") is None
    assert (a.chat_id, a.role, a.log, a.last_used, a.waiting) == (42, "finance", [], None, 0)
    assert isinstance(a.lock, asyncio.Lock)
    assert isinstance(sd.DESKS, sd.DeskRegistry)


def test_the_window_holds_twelve_exchanges_of_two_sides_oldest_dropped_each_side_clipped():
    _, desk = _desk()
    for i in range(14):
        desk.append("operator", f"q{i}", now=1000.0 + i)
        desk.append("specialist", f"a{i}", now=1000.0 + i)
    assert [e.text for e in desk.log] == [t for i in range(2, 14) for t in (f"q{i}", f"a{i}")]
    assert len(desk.log) == 2 * sd.DESK_LOG_EXCHANGES == 24 and desk.last_used == 1013.0
    desk.append("specialist", "x" * 1000, now=2000.0)
    assert desk.log[-1].text == "x" * (sd.DESK_LOG_SIDE_CHARS - len(sd.CLIP)) + sd.CLIP
    assert len(desk.log[-1].text) == sd.DESK_LOG_SIDE_CHARS == 400


def test_the_rendered_block_is_budgeted_by_dropping_whole_oldest_exchanges():
    sides = []
    for i in range(24):
        sides.append(sd.Exchange("operator", f"q{i:02d}" + "a" * 396, 1000.0 + i))
        sides.append(sd.Exchange("specialist", f"a{i:02d}" + "a" * 396, 1000.0 + i))
    block = sd.render_block(sides, budget=sd.DESK_LOG_CHARS)
    assert block.startswith("<desk>\n") and block.endswith("\n</desk>")
    assert len(block) <= sd.DESK_LOG_CHARS == 10_000
    kept = [line for line in block.splitlines() if line.startswith("[")]
    assert len(kept) < 48 and len(kept) % 2 == 0                        # whole exchanges only
    assert kept[0].split("] ", 1)[1].startswith("q") and kept[-1].split("] ", 1)[1].startswith("a23")
    # a lone newest exchange over the budget is clipped to fit
    small = sd.render_block([sd.Exchange("operator", "z" * 300, 1.0)], budget=120)
    assert len(small) <= 120 and small.startswith("<desk>")
    assert sd.render_block([]) == ""


def test_an_idle_desk_starts_empty_on_its_next_use():
    reg, desk = _desk()
    desk.append("operator", "hello", now=1000.0)
    desk.begin_use(now=1000.0 + sd.DESK_IDLE_S - 1)
    assert [e.text for e in desk.log] == ["hello"]
    desk.begin_use(now=1000.0 + sd.DESK_IDLE_S + 1)
    assert desk.log == [] and sd.DESK_IDLE_S == 3600


def test_the_queue_reserves_at_most_three_waiters():
    _, desk = _desk()
    places = [desk.reserve() for _ in range(sd.DESK_QUEUE_MAX)]
    assert all(p is not None for p in places)
    assert desk.reserve() is None and desk.waiting == 3
    places[0].release()
    places[0].release()                                          # idempotent
    assert desk.waiting == 2 and desk.reserve() is not None
    assert sd.DESK_QUEUE_MAX == 3


def test_the_delegation_fit_leaves_the_residents_context_room():
    exchanges = [sd.Exchange("operator", "a" * 400, 1.0 + i) for i in range(12)]
    full = sd.fit_block_for_delegation(exchanges, context_len=0)
    assert len(full) <= sd.DESK_CONTEXT_CHARS - sd.DESK_FRAMING_CHARS
    tight = sd.fit_block_for_delegation(exchanges, context_len=8000)
    assert len(tight) <= sd.DESK_CONTEXT_CHARS - 8000 - sd.DESK_FRAMING_CHARS
    assert sd.fit_block_for_delegation(exchanges, context_len=sd.DESK_CONTEXT_CHARS) == ""
    assert (sd.DESK_CONTEXT_CHARS, sd.DESK_QUOTE_CHARS, sd.DESK_TASK_CHARS) == (12_500, 2000, 4096)


# --- who may hold a desk ---------------------------------------------------------

def test_a_desk_target_is_a_live_declared_delegate_of_the_resident(monkeypatch):
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "assistant": SimpleNamespace(kind="resident", delegates=(SimpleNamespace(agent="finance"),)),
        "finance": SimpleNamespace(kind="specialist", delegates=()),
        "concierge": SimpleNamespace(kind="specialist", delegates=()),
    })
    assert sd.desk_target_ok("assistant", "finance") is True
    assert sd.desk_target_ok("assistant", "concierge") is False      # live specialist, not declared (the ACL alone)
    assert sd.desk_target_ok("assistant", "butler") is False         # not in the map at all
    assert sd.desk_target_ok("finance", "assistant") is False        # not declared either way
    assert sd.desk_target_ok("assistant", "assistant") is False      # the resident itself
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "assistant": SimpleNamespace(kind="resident", delegates=(SimpleNamespace(agent="finance"),))})
    assert sd.desk_target_ok("assistant", "finance") is False        # declared but not dispatchable
    # the resident is never its own desk, even when its declarations name itself —
    # pinned in isolation: the double is a specialist-kind resident so that only the
    # self check can refuse it
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "finance": SimpleNamespace(kind="specialist", delegates=(SimpleNamespace(agent="finance"),))})
    assert tools_mod.declares_delegate("finance", "finance")
    assert sd.desk_target_ok("finance", "finance") is False


def test_a_desk_target_is_a_specialist_never_another_resident(monkeypatch):
    """Round 6: a resident the chat's resident declares as a delegate (the
    delegation tool allows resident targets) is dispatchable, yet a reply on
    its post is today's path — a desk is a specialist's thread."""
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "assistant": SimpleNamespace(kind="resident", delegates=(
            SimpleNamespace(agent="finance"), SimpleNamespace(agent="gary"))),
        "finance": SimpleNamespace(kind="specialist", delegates=()),
        "gary": SimpleNamespace(kind="resident", delegates=()),
    })
    assert tools_mod.declares_delegate("assistant", "gary")
    assert sd.desk_target_ok("assistant", "gary") is False
    assert sd.desk_target_ok("assistant", "finance") is True
    assert sd.is_specialist(tools_mod._agent_role_map["finance"]) is True
    assert sd.is_specialist(tools_mod._agent_role_map["gary"]) is False
    assert sd.is_specialist(SimpleNamespace()) is False               # no kind: not a specialist
    # the delegation gate is the same predicate: a resident target is no desk use
    origin = {"_operator_turn": True, "channel": "telegram", "chat_id": 42}
    assert sd.desk_for_delegation(origin, tools_mod._agent_role_map["gary"], "gary") is None
    assert sd.desk_for_delegation(origin, tools_mod._agent_role_map["finance"], "finance") is not None


def test_only_a_plain_operator_dm_turns_delegation_enters_a_desk(monkeypatch):
    """Round 7: §8 admits a delegation from an operator-DM origin — the turn
    provenance's `dm`, a Telegram turn with NO synthetic marker. A button
    continuation (`synthetic: "button"`, which also carries `_operator_turn`)
    and a plugin-setup turn delegate exactly as in v0.340.0: no desk."""
    monkeypatch.setattr(sd, "DESKS", sd.DeskRegistry())
    cfg = SimpleNamespace(kind="specialist", delegates=())
    dm = {"_operator_turn": True, "channel": "telegram", "chat_id": 42}
    assert sd.desk_for_delegation(dm, cfg, "finance") is not None
    for marker in ("button", "plugin_setup", "anything"):
        assert sd.desk_for_delegation({**dm, "synthetic": marker}, cfg, "finance") is None, marker
    assert sd.DESKS.get(42, "finance") is not None and len(sd.DESKS._desks) == 1


# --- the resident's echo ---------------------------------------------------------

def test_the_echo_is_drained_once_bounded_and_body_free(monkeypatch):
    import result_broker as rb
    monkeypatch.setattr(sd, "DESK_ECHO", rb.PostLedger(max_events=64))
    assert sd.prompt_prefix(42) == ""
    for i in range(7):
        sd.record_echo(42, f"📊 Finance answered your reply ({i + 1} pages).")
    sd.record_echo(42, "📊 Finance " + "n" * 200)
    lines = sd.drain_echo_lines(42)
    assert len(lines) == rb.ECHO_MAX_LINES + 1 and lines[-1] == "…and 3 more."
    assert all(len(line) <= rb.ECHO_LINE_MAX for line in lines)
    assert sd.drain_echo_lines(42) == []                            # once
    sd.record_echo(42, "📊 Finance answered your reply (1 page).")
    assert sd.prompt_prefix(42) == "(front desk) 📊 Finance answered your reply (1 page).\n\n"
    assert sd.prompt_prefix(42) == ""
    sd.record_echo(0, "never recorded")
    assert sd.prompt_prefix(0) == ""


# --- round 2 folds: admission follows reservation order ------------------------------

@pytest.mark.asyncio
async def test_admission_follows_reservation_order_even_when_the_later_holder_arrives_first():
    """A delegation reserves its place, then awaits its registration; a reply
    reserved after it must not be admitted first just because its task
    reached the desk first (arrival order = reservation order)."""
    _, desk = _desk()
    first, second = desk.reserve(), desk.reserve()
    order = []

    async def use(label, reservation):
        async with desk.use(reservation):
            order.append(label)
            await asyncio.sleep(0.01)
    t2 = asyncio.create_task(use("second", second))
    await asyncio.sleep(0.01)
    assert order == [] and desk.waiting == 2 and not desk.lock.locked()
    t1 = asyncio.create_task(use("first", first))
    await asyncio.gather(t1, t2)
    assert order == ["first", "second"] and desk.waiting == 0 and not desk.lock.locked()


@pytest.mark.asyncio
async def test_an_abandoned_reservation_lets_the_next_in_line_through():
    _, desk = _desk()
    first, second = desk.reserve(), desk.reserve()
    done = asyncio.Event()

    async def use(reservation):
        async with desk.use(reservation):
            done.set()
    t2 = asyncio.create_task(use(second))
    await asyncio.sleep(0.01)
    assert not done.is_set()
    first.release()                                            # gave up before admission
    await asyncio.wait_for(t2, 1.0)
    assert done.is_set() and desk.waiting == 0
