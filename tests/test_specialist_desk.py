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
    assert kept[0].split(": ", 1)[1].startswith("q") and kept[-1].split(": ", 1)[1].startswith("a23")
    # a lone newest exchange over the budget is clipped to fit
    head = len("<desk>\n" + sd.DESK_FRAME + "\n") + len("\n</desk>")
    small = sd.render_block([sd.Exchange("operator", "z" * 300, 1.0)], budget=head + 120)
    assert len(small) <= head + 120 and small.startswith("<desk>") and sd.CLIP in small
    # a budget that cannot hold the frame renders nothing rather than overrun
    assert sd.render_block([sd.Exchange("operator", "z", 1.0)], budget=head - 1) == ""
    assert sd.render_block([]) == ""


# --- #1192: the block is the specialist's OWN conversation -------------------------

def _stamp(at):
    import time
    return time.strftime("%H:%M", time.localtime(at))


def test_the_block_frames_the_log_as_the_specialists_own_exchanges_with_party_labels():
    log = [sd.Exchange("operator", "what is 2+2?", 1000.0),
           sd.Exchange("specialist", "4", 1000.0),
           sd.Exchange("resident", "draft the march invoice", 2000.0),
           sd.Exchange("specialist", "drafted", 2000.0)]
    block = sd.render_block(log, resident_name="Ellen")
    lines = block.splitlines()
    assert lines[0] == "<desk>" and lines[1] == sd.DESK_FRAME and lines[-1] == "</desk>"
    assert "your own" in sd.DESK_FRAME and "not a transcript" in sd.DESK_FRAME
    assert lines[2:-1] == [
        f"[{_stamp(1000.0)}] the operator: what is 2+2?",
        f"[{_stamp(1000.0)}] you: 4",
        f"[{_stamp(2000.0)}] Ellen, on the operator's behalf: draft the march invoice",
        f"[{_stamp(2000.0)}] you: drafted"]
    # the stored log is untouched: only the rendering names the parties
    assert [e.who for e in log] == ["operator", "specialist", "resident", "specialist"]
    for old in ("[operator ", "[specialist ", "[resident "):
        assert old not in block


def test_the_framed_block_keeps_every_bound_with_the_longest_labels():
    name = "N" * 500                                         # a resident name is clipped
    sides = []
    for i in range(sd.DESK_LOG_EXCHANGES):
        sides.append(sd.Exchange("resident", f"r{i:02d}" + "a" * 396, 1000.0 + i))
        sides.append(sd.Exchange("specialist", f"s{i:02d}" + "a" * 396, 1000.0 + i))
    block = sd.render_block(sides, resident_name=name)
    assert len(block) <= sd.DESK_LOG_CHARS == 10_000
    kept = [line for line in block.splitlines() if line.startswith("[")]
    assert len(kept) % 2 == 0 and kept[-1].split(": ", 1)[1].startswith("s11")
    label = kept[0].split("] ", 1)[1].split(": ", 1)[0]
    assert label == sd.clip(name, sd.DESK_NAME_CHARS) + ", on the operator's behalf"
    # the operator/specialist window at saturation keeps as many exchanges as before #1192
    sides = []
    for i in range(sd.DESK_LOG_EXCHANGES):
        sides.append(sd.Exchange("operator", "a" * 400, 1000.0 + i))
        sides.append(sd.Exchange("specialist", "a" * 400, 1000.0 + i))
    kept = [line for line in sd.render_block(sides).splitlines() if line.startswith("[")]
    assert len(kept) == 2 * 11
    # the delegation's fitted budget holds the frame too
    for ctx in (0, 4000, 8000):
        fitted = sd.fit_block_for_delegation(sides, context_len=ctx)
        assert len(fitted) <= min(sd.DESK_CONTEXT_CHARS - ctx - sd.DESK_FRAMING_CHARS, sd.DESK_LOG_CHARS)
        assert fitted.startswith("<desk>\n" + sd.DESK_FRAME + "\n")


def test_the_desk_turn_context_says_the_message_is_from_the_same_operator():
    record = SimpleNamespace(slot="report", posted_at=900.0)
    block = sd.render_block([sd.Exchange("operator", "q", 1.0), sd.Exchange("specialist", "a", 1.0)],
                            resident_name="Ellen")
    context = sd._compose_context(block, "📊 Finance\nQ3", record, 1000.0, resident_name="Ellen")
    frame = sd.turn_frame("Ellen")
    assert context.startswith(frame + "\n\n" + block)
    assert "from the operator" in frame and "same operator" in frame and "now" in frame
    assert "Ellen did not write or relay it" in frame and "<desk>" not in frame
    assert len(frame) <= sd.DESK_FRAMING_CHARS // 2
    cont = sd.turn_frame("Ellen", continuation=True)
    assert cont != frame and "Casa" in cont and "decision" in cont and "same operator" in cont
    assert len(cont) <= sd.DESK_FRAMING_CHARS // 2 and "<desk>" not in cont
    assert sd._compose_context(block, None, None, 1000.0, resident_name="Ellen",
                               continuation=True) == cont + "\n\n" + block
    # first use: the frame alone, no block
    assert sd._compose_context("", None, None, 1000.0, resident_name="Ellen") == frame
    # the whole context fits its cap by construction at the largest block and quote
    big = sd.render_block([sd.Exchange("operator", "a" * 400, 1.0 + i) for i in range(24)],
                          resident_name="N" * 500)
    assert len(big) > sd.DESK_LOG_CHARS - 500
    quote = "q" * 5000
    full = sd._compose_context(big, quote, record, 1000.0, resident_name="N" * 500)
    assert len(full) <= sd.DESK_CONTEXT_CHARS
    assert full.endswith(sd.clip(quote, sd.DESK_QUOTE_CHARS))           # nothing cut by the cap


def test_a_slash_reply_context_carries_its_line_and_still_fits_by_construction():
    """#1198: the slash line is its own part after the frame, only for a reply
    task starting with "/", and the cap still holds with nothing cut."""
    record = SimpleNamespace(slot="report", posted_at=900.0)
    block = sd.render_block([sd.Exchange("operator", "q", 1.0)], resident_name="Ellen")
    frame = sd.turn_frame("Ellen")
    plain = sd._compose_context(block, None, None, 1000.0, resident_name="Ellen")
    assert sd._compose_context(block, None, None, 1000.0, resident_name="Ellen",
                               task="  /new") == frame + "\n\n" + sd.SLASH_TASK_LINE + "\n\n" + block
    for task in ("more", "a /b", "", None):
        assert sd._compose_context(block, None, None, 1000.0, resident_name="Ellen",
                                   task=task) == plain
    assert sd._compose_context(block, None, None, 1000.0, resident_name="Ellen",
                               continuation=True, task="/new") == (
        sd.turn_frame("Ellen", continuation=True) + "\n\n" + block)
    big = sd.render_block([sd.Exchange("operator", "a" * 400, 1.0 + i) for i in range(24)],
                          resident_name="N" * 500)
    quote = "q" * 5000
    full = sd._compose_context(big, quote, record, 1000.0, resident_name="N" * 500, task="/new")
    assert len(full) <= sd.DESK_CONTEXT_CHARS
    assert sd.SLASH_TASK_LINE in full
    assert full.endswith(sd.clip(quote, sd.DESK_QUOTE_CHARS))           # nothing cut by the cap


def test_a_file_turn_context_never_carries_the_slash_line():
    """#1198 x S6: a file turn's task is Casa's note (``[casa file] …``), so a caption
    starting with "/" leaves the file frame and the block alone."""
    block = sd.render_block([sd.Exchange("operator", "q", 1.0)], resident_name="Ellen")
    task = ("[casa file] The operator sent you a file: s.pdf (PDF, 10 bytes). It is in your "
            "inbox at /x/s.pdf. File it with your plugin and report what you did.\n"
            "The operator wrote: /new")
    assert sd._compose_context(block, None, None, 1000.0, resident_name="Ellen", file=True,
                               task=task) == sd.turn_frame("Ellen", file=True) + "\n\n" + block


def test_a_slash_reply_context_at_the_full_block_budget_cuts_nothing():
    """#1198: at a block of exactly DESK_LOG_CHARS (the most render_block returns), the
    longest kept resident name, the longest slot a manifest can declare (64 characters)
    and a full quote, the slash reply's context fits DESK_CONTEXT_CHARS unsliced."""
    name = "E" * sd.DESK_NAME_CHARS
    sides = [[who, 395] for _ in range(sd.DESK_LOG_EXCHANGES) for who in ("resident", "specialist")]

    def block_of(sides):
        return sd.render_block([sd.Exchange(who, who[0] * n, 990.0) for who, n in sides],
                               resident_name=name)

    i = len(sides) - 1                    # grow the newest sides, each within the side bound
    while len(block_of(sides)) < sd.DESK_LOG_CHARS:
        if sides[i][1] == sd.DESK_LOG_SIDE_CHARS:
            i -= 1
        sides[i][1] += 1
    block = block_of(sides)
    assert len(block) == sd.DESK_LOG_CHARS
    record = SimpleNamespace(slot="s" * 64, posted_at=900.0)
    quote = "q" * (sd.DESK_QUOTE_CHARS - 1) + "Z"
    full = sd._compose_context(block, quote, record, 1000.0, resident_name=name, task="/new")
    when = sd.time.strftime("%Y-%m-%d %H:%M", sd.time.localtime(900.0))
    assert full == "\n\n".join([
        sd.turn_frame(name), sd.SLASH_TASK_LINE, block,
        f"The operator replied to your post (slot {'s' * 64}, posted {when}) which read:\n" + quote])
    assert len(full) <= sd.DESK_CONTEXT_CHARS


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
