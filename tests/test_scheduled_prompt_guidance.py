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


# --- Round 2 of diff review, terra, two S2s, generalised rather than patched ---
#
# Stating the convention is not enough: an EXAMPLE prompt is what the
# configurator copies, and an example that carries the closing clause
# unconditionally silences a turn whose own final text is the delivery
# (`_strips_to_silence` clears it and nothing is sent), while an example that
# carries no ending at all reproduces the original double message. So every
# surface that shows an example prompt shows BOTH shapes, labelled, and no
# surface shows one ending as the default.

SHAPE_A = "After the send, output the sentinel `<silent/>` and nothing else."
SHAPE_B = ("If there is nothing worth sending, output the sentinel `<silent/>` "
           "and nothing else.")

EXAMPLE_SURFACES = [
    _RECIPES / "add.md",
    _RECIPES / "update.md",
    REPO_ROOT / "casa/DOCS.md",
]


@pytest.mark.parametrize("path", EXAMPLE_SURFACES, ids=lambda p: p.name)
def test_both_prompt_shapes_are_shown_wherever_an_example_prompt_is(path):
    """Neither ending may be presented alone: a surface that shows one shape's
    example shows the other's beside it."""
    text = _normalized(path)
    assert SHAPE_A in text
    assert SHAPE_B in text
    assert "shape A" in text and "shape B" in text


@pytest.mark.parametrize("path", [_RECIPES / "add.md", _RECIPES / "update.md"],
                         ids=lambda p: p.name)
def test_the_upsert_template_does_not_hardcode_one_ending(path):
    """The `config_trigger_upsert` template — the line the configurator fills in
    and calls — must defer to the shape, never carry an ending of its own."""
    template = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                if "prompt=\"<" in ln]
    assert len(template) == 1, template
    assert "shape requires" in template[0]
    assert "<silent/>" not in template[0]
