"""The guest pin: a catch-all PreToolUse deny for every tool outside a guest
run's allow-list, plugin tools included. The CLI runs matching hooks
concurrently and one deny does not stop its siblings, so the pin and the
broker's guest predicate each deny on their own."""
from __future__ import annotations

import re

from hooks import guest_pin_matcher
from test_guest_options import EXPORT, LOOKUP, SEARCH, SETUP, build

CTX: dict = {"signal": None}


def _pre(tool: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}}


def _denies(out: dict) -> bool:
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


async def _run_pre_tool_use(matchers, tool: str) -> list[dict]:
    """Every PreToolUse outcome the CLI would collect for *tool*: a matcher of
    ``None`` routes every call, a regex routes the names it matches."""
    outs = []
    for m in matchers:
        if m.matcher is not None and not re.fullmatch(m.matcher, tool):
            continue
        for hook in m.hooks:
            outs.append(await hook(_pre(tool), "t", CTX))
    return outs


async def test_the_pin_denies_every_tool_outside_the_list():
    pin = guest_pin_matcher(frozenset({LOOKUP, SEARCH}))
    assert pin.matcher is None and len(pin.hooks) == 1
    hook = pin.hooks[0]
    for name in ("Read", "mcp__casa-framework__ask_user",
                 "mcp__casa-framework__hand_off", SETUP, EXPORT):
        out = await hook(_pre(name), "t", CTX)
        assert _denies(out), name
        assert out["hookSpecificOutput"]["permissionDecisionReason"] == (
            f"{name} is not available here")
    assert await hook(_pre(LOOKUP), "t", CTX) == {}
    assert await hook(_pre(SEARCH), "t", CTX) == {}


async def test_removing_the_pin_lets_an_unlisted_builtin_through_the_hooks(tmp_path):
    """The mutation half: with the built options' PreToolUse matchers as they
    are, a Read is denied; with the first matcher gone, nothing else denies
    it — the pin is the layer that holds built-ins out."""
    opts = build(tmp_path)
    pre = opts.hooks["PreToolUse"]
    with_pin = await _run_pre_tool_use(pre, "Read")
    assert sum(_denies(o) for o in with_pin) == 1
    without_pin = await _run_pre_tool_use(pre[1:], "Read")
    assert sum(_denies(o) for o in without_pin) == 0
    # a plugin tool outside the list meets both layers; a listed one neither
    assert sum(_denies(o) for o in await _run_pre_tool_use(pre, EXPORT)) == 2
    assert sum(_denies(o) for o in await _run_pre_tool_use(pre, LOOKUP)) == 0
