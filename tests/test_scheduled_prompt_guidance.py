"""Every Casa-controlled surface that tells an author how to write a scheduled
trigger prompt states the closing-silence convention (#932).

A SCHEDULED turn that delivers its message with ``tools.send_message`` and then
ends with ordinary prose delivers TWO messages to the same operator chat: the
send happens immediately and marks nothing the final-text path can read
(``tools.py`` ``send_message``), and the turn's closing text is then delivered
through ``send_response`` unless it strips to the silence sentinel exactly
(``agent.py``). Neither half may be narrowed — the imperative send stays first
and unconditional (fail-noisy, never a lost reminder: the v0.132.0
morning-briefing lesson), and prose after a sentinel is still delivered (the G-3
recant contract). So the gap is closed PER PROMPT, in the prompt text, which is
what ``set_reminder`` and the event wakes already do for the prompts Casa
authors itself (#511, #534).

This pins the same convention on the surfaces that instruct somebody ELSE to
author a scheduled prompt: the configurator's two trigger recipes, the
operator-facing ``triggers.yaml`` guidance, and the corpus document that records
the convention once. It is a pin on TEXT and claims nothing about runtime — a
hand-authored prompt that omits the clause still delivers twice, by design,
because the alternative shapes are the two the pinning tests above refuse.

Read-only: these files are read out of the repository in place, never copied, so
nothing here depends on the tree being writable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_RECIPES = (
    REPO_ROOT
    / "casa/rootfs/opt/casa/defaults/agents/executors/configurator"
    / "doctrine/recipes/trigger"
)

# The one wording, carried verbatim by every surface below. Whitespace is
# normalized on both sides so a document may wrap it however its prose wraps.
CONVENTION = (
    "For interval/cron/date prompts whose turn delivers its own message, keep "
    "the send instruction first and unconditional, and end the prompt with: "
    "After the send, output the sentinel `<silent/>` and nothing else."
)

SURFACES = [
    _RECIPES / "add.md",
    _RECIPES / "update.md",
    REPO_ROOT / "casa/DOCS.md",
    REPO_ROOT / "docs/architecture/triggers.md",
]


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


@pytest.mark.parametrize("path", SURFACES, ids=lambda p: p.name)
def test_closing_silence_convention(path):
    assert _normalized(path).count(CONVENTION) == 1


def test_the_webhook_route_is_not_told_to_carry_a_prompt():
    """INV-TRIG-013 guard on this change: a webhook trigger carries no prompt
    at all, so the convention must stay scoped to interval/cron/date and the
    recipe must keep saying the schema refuses a webhook prompt."""
    add = _normalized(_RECIPES / "add.md")
    assert "A webhook trigger has no prompt and the schema refuses one" in add
    assert "`prompt`/`prompt_file` are **refused by the schema** for a webhook" in add
