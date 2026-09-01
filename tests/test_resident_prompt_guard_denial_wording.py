"""#631 / D36 — the refusal's own words must not contradict INV-PERS-012.

The declaration's first clause was corrected from "is not read" (false: the
loader opens the file on every load) to "is read only into the composed
fallback prompt and is not served when its compiled bundle is active". The
denial an agent sees is relayed to the operator, so it has to say the same
thing: a correct refusal carrying a wrong explanation is the kind of statement
this change exists to stop shipping. Pinned as counts over the four routed
primitives.
"""

from __future__ import annotations

import asyncio

PRIMITIVES = (
    ("Write", "file_path"),
    ("Edit", "file_path"),
    ("MultiEdit", "file_path"),
    ("NotebookEdit", "notebook_path"),
)
TARGET = "/config/agents/assistant/prompts/system.md"


def _reasons() -> list[str]:
    from hooks import HOOK_POLICIES
    cb = HOOK_POLICIES["resident_prompt_write_guard"]["factory"]()

    async def run() -> list[str]:
        out = []
        for tool, arg in PRIMITIVES:
            res = await cb({"tool_name": tool, "cwd": "/config",
                            "tool_input": {arg: TARGET}}, None, {})
            hso = (res or {}).get("hookSpecificOutput", {})
            if hso.get("permissionDecision") == "deny":
                out.append(hso.get("permissionDecisionReason", ""))
        return out

    return asyncio.run(run())


def test_the_denial_says_not_served_and_never_not_read():
    """(denials, saying_not_served, saying_not_read) == (4, 4, 0).

    Mutation: restore the old "is not read for a persona-bound resident"
    phrasing and the tuple becomes (4, 0, 4).
    """
    reasons = _reasons()
    assert (
        len(reasons),
        sum("is not served" in r for r in reasons),
        sum("is not read" in r for r in reasons),
    ) == (4, 4, 0)
