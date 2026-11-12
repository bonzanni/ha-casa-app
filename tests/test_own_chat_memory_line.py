"""How a change of an agent's own chat turned out reaches the resident's
memory as one line of Casa's own.

The resident starts a chat's setup or a handle change with a card, but what
finishes it — the operator's tap on Create, a handle typed differently in
Telegram's form, Casa's question about which agent a new bot is for, the
restart that moves the agent — happens on Casa's cards, never in the
resident's turns. Without a line, the resident remembers only the handle it
offered, and later reads the real one as a change that failed.

The lifecycle is driven through the fake Telegram world (``own_voice_world``);
the capture itself is the host's real one, into a real store.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import own_voice_lifecycle as ovl
from own_voice_world import (
    ALEX_BOT, FIDO_BOT, OPERATOR, Crash, World, created, make_lifecycle, specialist,
)
from test_memory_capture import captured_events
from test_memory_capture import mem  # noqa: F401 — fixture

pytestmark = [pytest.mark.asyncio]

ALEX_TOKEN = f"{ALEX_BOT}:alex-token-aaaaaaaaaaaaaaaa"
NEW_BOT = FIDO_BOT + 10
NEW_TOKEN = f"{NEW_BOT}:new-token-eeeeeeeeeeeeeeee"
OLD = "HeyAlexTestBot"
OFFERED = "CasaFinance4821Bot"
TYPED = "HeyCasaFinanceTestBot"


@pytest.fixture
def world() -> World:
    w = World()
    w.configs = {"finance": specialist("Alex"), "browser": specialist("Fido")}
    w.queues = {"finance", "browser"}
    w.tokens = {ALEX_BOT: ALEX_TOKEN, NEW_BOT: NEW_TOKEN}
    return w


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "data"


async def _restart(world, root):
    lc, host = make_lifecycle(world, root)
    await lc.apply_switches()
    await lc.boot()
    return lc, host


async def _bind_alex(lc) -> None:
    await lc.post_offer("finance", OLD)
    await lc.on_created(created(ALEX_BOT, "Alex", OLD), OPERATOR)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND


async def test_a_first_chat_under_a_handle_typed_in_the_form_is_remembered(world, root):
    lc, host = make_lifecycle(world, root)
    await lc.post_offer("finance", OFFERED)
    await lc.on_created(created(ALEX_BOT, "Alex", TYPED), OPERATOR)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.UNBOUND   # Casa asks whose it is
    assert world.memory == []
    await host.answer("Alex")
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert world.memory == [("finance", f"Alex's chat is set up: @{TYPED}.")]


async def test_a_move_to_a_handle_typed_in_the_form_is_remembered_once(world, root):
    lc, _host = make_lifecycle(world, root)
    await _bind_alex(lc)
    world.memory.clear()
    await lc.replace("finance", OFFERED)
    _lc, host = make_lifecycle(world, root)
    await host.lifecycle.on_created(created(NEW_BOT, "Alex", TYPED), OPERATOR)
    await host.answer("Alex")
    assert world.restarts == 1 and world.memory == []        # nothing has moved yet
    lc2, _ = await _restart(world, root)
    assert lc2.store.bots[NEW_BOT]["phase"] == ovl.BOUND
    assert world.memory == [("finance", f"Alex's chat moved from @{OLD} to @{TYPED}.")]
    await _restart(world, root)                              # a later boot adds nothing
    assert world.memory == [("finance", f"Alex's chat moved from @{OLD} to @{TYPED}.")]


async def test_a_move_held_back_by_open_work_is_remembered(world, root):
    lc, _host = make_lifecycle(world, root)
    await _bind_alex(lc)
    world.memory.clear()
    await lc.replace("finance", OFFERED)
    await lc.on_created(created(NEW_BOT, "Alex", OFFERED), OPERATOR)
    world.open[ALEX_BOT] = ["a background job"]
    lc2, _ = await _restart(world, root)
    assert lc2.store.agents["finance"]["change"]["new_bot"] == NEW_BOT
    assert world.memory == [("finance", f"Alex's move from @{OLD} to @{OFFERED} waits: "
                                        f"something is still open in @{OLD}.")]


async def test_a_crash_once_the_binding_is_saved_keeps_the_line(world, root):
    """The process dies right after the bound phase reached disk: the line
    was written before it, and the restart (which finds the bot bound and
    greets no one) adds none."""
    lc, _host = make_lifecycle(world, root)
    await lc.post_offer("finance", OLD)
    save = lc.store.save

    async def save_then_die():
        await save()
        if (lc.store.bots.get(ALEX_BOT) or {}).get("phase") == ovl.BOUND:
            raise Crash("after the bound save")
    lc.store.save = save_then_die
    with pytest.raises(Crash):
        await lc.on_created(created(ALEX_BOT, "Alex", OLD), OPERATOR)
    lc2, _ = await _restart(world, root)
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert world.memory == [("finance", f"Alex's chat is set up: @{OLD}.")]


async def test_a_line_that_cannot_be_captured_never_stops_the_binding(world, root):
    lc, host = make_lifecycle(world, root)

    async def broken(role, text):
        raise RuntimeError("memory is down")
    host.remember = broken
    await _bind_alex(lc)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND


async def test_the_line_is_casa_s_own_event_in_the_resident_s_memory(mem, monkeypatch):
    host = ovl.TelegramHost()
    monkeypatch.setattr(host, "resident_role", lambda: "assistant")
    await host.remember("finance", f"Alex's chat is set up: @{TYPED}.")
    rows = await captured_events(mem)
    assert len(rows) == 1
    row = rows[0]
    assert (row["role"], row["channel"], row["speaker_kind"], row["origin_kind"],
            row["speaker_id"], row["origin_tier"], row["eligibility"], row["text"]) == (
        "assistant", "telegram", "casa", "system", "finance", "private",
        "own_chat_line", f"Alex's chat is set up: @{TYPED}.")
