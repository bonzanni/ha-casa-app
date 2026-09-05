"""#850 — the response-shape refusal's own words must state the real mechanism.

The denial an agent receives when it tries to edit a resident's
`agents/<role>/response_shape.yaml` is relayed to the operator, so it has to
say what actually happens to that file: the loader reads and renders it into
the composed FALLBACK prompt, and no part of that fallback is served while the
resident's compiled bundle is active. The old wording opened with "is not
read" — false (the loader opens the file on every load) and contradicting the
same denial's closing sentence, "Reading this file is fine". A correct refusal
carrying a wrong explanation is the kind of statement this pin exists to stop
shipping.

Pinned as counts over the five routed primitives: the four file tools and the
one Bash form, because the constant is formatted at three sites (two file-tool
arms, one Bash arm) and all three must carry the same explanation. This is a
wording pin on `_RESPONSE_SHAPE_WRITE_DENY` alone; the guard's behaviour is
pinned in `tests/test_response_shape_write_guard.py`, from which nothing here
is imported.
"""

from __future__ import annotations

import asyncio

TARGET = "/config/agents/assistant/response_shape.yaml"
CALLS = (
    ("Write", {"file_path": TARGET}),
    ("Edit", {"file_path": TARGET}),
    ("MultiEdit", {"file_path": TARGET}),
    ("NotebookEdit", {"notebook_path": TARGET}),
    ("Bash", {"command": f"sed -i 's/x/y/' {TARGET}"}),
)


def _reasons() -> list[str]:
    from hooks import HOOK_POLICIES
    cb = HOOK_POLICIES["response_shape_write_guard"]["factory"]()

    async def run() -> list[str]:
        out = []
        for tool, tool_input in CALLS:
            res = await cb({"tool_name": tool, "cwd": "/config",
                            "tool_input": tool_input}, None, {})
            hso = (res or {}).get("hookSpecificOutput", {})
            if hso.get("permissionDecision") == "deny":
                out.append(hso.get("permissionDecisionReason", ""))
        return out

    return asyncio.run(run())


def test_the_denial_says_not_served_and_never_not_read():
    """(denials, saying_not_served, saying_not_read) == (5, 5, 0).

    Mutations, each against the committed constant: restore the old "is not
    read for a persona-bound resident" opening and the tuple is (5, 0, 5); a
    constant saying both phrases gives (5, 5, 5); one saying neither gives
    (5, 0, 0). Only the mechanism wording satisfies all three terms.
    """
    reasons = _reasons()
    assert (
        len(reasons),
        sum("is not served" in r for r in reasons),
        sum("is not read" in r for r in reasons),
    ) == (5, 5, 0)
