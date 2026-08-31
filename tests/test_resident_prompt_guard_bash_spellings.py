"""#631 — the Bash half's cwd evidence, pinned separately.

This file is deliberately NOT part of the accepted red case
(`tests/test_resident_prompt_write_guard.py`), which is frozen. It pins a
finding the gate-owned review reproduced against the first implementation:

    cwd=/config
    printf x > agents/assistant/prompts/system.md

The first spelling of the evidence check glued `cwd` onto the command text and
searched for `/config/agents` as a substring. That text reads
`... agents/assistant/prompts/system.md /config`, in which `/config/agents`
never appears contiguously — so the executor's own natural relative spelling
was ALLOWED while the absolute one was denied. Measured before the fix:
(relative, absolute) = (allow, DENY).

The evidence is now a PREDICATE on the working directory, which has no such
seam. Both directions are asserted, because a predicate that simply denied
everything would satisfy the first half alone.
"""

from __future__ import annotations

import asyncio

GUARD = "resident_prompt_write_guard"
WORKSPACE = "/data/engagements/" + "a" * 32


def _guard():
    from hooks import HOOK_POLICIES
    return HOOK_POLICIES[GUARD]["factory"]()


async def _denies(cb, cwd: str, command: str) -> bool:
    out = await cb({"tool_name": "Bash", "cwd": cwd,
                    "tool_input": {"command": command}}, None, {})
    return bool(out) and (out["hookSpecificOutput"]["permissionDecision"]
                          == "deny")


def test_bash_evidence_covers_every_in_config_spelling_and_no_others():
    """(denied_in_config, denied_outside_config) == (4, 0).

    The four denied are the spellings an agent inside `/config` actually uses;
    the three allowed are the workspace writes the two-segment needle exists to
    protect, plus a read.
    """
    cb = _guard()
    must_deny = [
        # The reproduced bypass: an executor's relative spelling from /config.
        ("/config", "printf x > agents/assistant/prompts/system.md"),
        # The absolute spelling, which the first implementation did catch.
        ("/config", "printf x > /config/agents/assistant/prompts/system.md"),
        # A resident runs from its own agent home; its evidence is the cwd.
        ("/config/agents/assistant", 'printf x > prompts/"system.md"'),
        # A traversal from a sibling directory under /config.
        ("/config/workspace", "printf x > ../agents/butler/prompts/system.md"),
    ]
    must_allow = [
        (WORKSPACE, f"printf x > {WORKSPACE}/prompts/system.md"),
        (WORKSPACE, "printf x > prompts/system.md"),
        # Reads stay allowed wherever they are issued.
        ("/config", "cat agents/assistant/prompts/system.md"),
    ]

    async def run() -> tuple[int, int]:
        denied = 0
        for cwd, cmd in must_deny:
            if await _denies(cb, cwd, cmd):
                denied += 1
        leaked = 0
        for cwd, cmd in must_allow:
            if await _denies(cb, cwd, cmd):
                leaked += 1
        return denied, leaked

    assert asyncio.run(run()) == (4, 0)


def test_lexically_equivalent_separator_spellings_are_denied_too():
    """(denied, leaked) == (7, 0).

    The SECOND finding the gate-owned review reproduced against this text
    predicate, and the reason the fix normalizes rather than recognising one
    more spelling. Measured before it, from `cwd=/config`:

        printf x > /config/agents/assistant/prompts/./system.md   -> ALLOW
        printf x > agents/assistant/prompts/./system.md           -> ALLOW
        printf x > /config/agents/assistant/prompts//system.md    -> ALLOW

    while the canonical spelling was denied. `//` was not in the finding; it
    fell out of the same measurement, which is the point of retiring the class
    instead of the case. Every command below names exactly the file the
    canonical spelling names, and the kernel resolves them identically.

    The `must_allow` half is load-bearing: collapsing separators must not
    manufacture a needle. A plugin developer's own workspace copy stays
    writable in every one of the same spellings.
    """
    cb = _guard()
    must_deny = [
        ("/config", "printf x > /config/agents/assistant/prompts/./system.md"),
        ("/config", "printf x > agents/assistant/prompts/./system.md"),
        ("/config", "printf x > /config/agents/assistant/prompts//system.md"),
        ("/config", "printf x > /config//agents///assistant/prompts////system.md"),
        ("/config", "printf x > /config/agents/assistant/prompts/././system.md"),
        ("/config", "printf x > /config/./agents//assistant/./prompts/.//system.md"),
        # Quote stripping runs first, so noise may arrive from inside quotes.
        ("/config/agents/assistant", 'printf x > prompts/"."/system.md'),
    ]
    must_allow = [
        (WORKSPACE, f"printf x > {WORKSPACE}/prompts/./system.md"),
        (WORKSPACE, f"printf x > {WORKSPACE}/prompts//system.md"),
        (WORKSPACE, "printf x > prompts/./system.md"),
        # A dotted basename is a DIFFERENT file, not a noisy spelling of this
        # one: `prompts/."system".md` is `prompts/.system.md` to the shell.
        ("/config", 'printf x > /config/agents/assistant/prompts/."system".md'),
        # Reads stay allowed in the noisy spellings too.
        ("/config", "cat /config/agents/assistant/prompts/./system.md"),
    ]

    async def run() -> tuple[int, int]:
        denied = 0
        for cwd, cmd in must_deny:
            if await _denies(cb, cwd, cmd):
                denied += 1
        leaked = 0
        for cwd, cmd in must_allow:
            if await _denies(cb, cwd, cmd):
                leaked += 1
        return denied, leaked

    assert asyncio.run(run()) == (7, 0)
