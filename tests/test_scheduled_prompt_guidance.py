"""The surfaces this module enumerates state the closing-silence convention for
a scheduled trigger prompt (#932), and no configurator doctrine file that
names a per-trigger prompt path is missing from that enumeration.

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
the convention once, plus the recipe for EDITING an existing per-trigger prompt,
which points at ``trigger/add.md`` for the shape rather than restating it. That
enumeration is checked rather than claimed as a total: the last test in this
module fails when a configurator doctrine file naming a per-trigger prompt path
is neither covered nor exempted with a reason.

It is a pin on TEXT and claims nothing about runtime — a hand-authored prompt
that omits the clause still delivers twice, by design, because the alternative
shapes are the two the pinning tests above refuse.

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


@pytest.mark.parametrize("path", EXAMPLE_SURFACES, ids=lambda p: p.name)
def test_shape_a_is_defined_by_the_delivery_tool_not_by_tool_use(path):
    """Gate-owned review round, terra, S2. "The turn uses a tool" is the wrong
    test: a turn may call `ack_event`, read the calendar or query Home Assistant
    and still deliver through its OWN final text, and giving that prompt the
    closing clause suppresses its only delivery. So every surface names the
    DELIVERY tools where it defines the shape, and says in terms that a
    non-delivery tool call does not decide the question."""
    text = _normalized(path)
    assert "send_message" in text and "send_media" in text
    assert "DELIVERY tool call" in text or "delivery tool call" in text


# --- Candidate review, round 1, S2: a FIFTH authoring surface ---------------
#
# `recipes/prompt/edit.md` is where a request to change an EXISTING trigger
# prompt routes: its table names a resident's `prompts/<trigger>.md` as
# editable, and it carried none of this. A prompt edited from it is saved
# without the closing instruction and its trigger goes on delivering twice —
# the defect this change exists to remove, reachable through a surface the
# change did not touch. The recipe's form is a routing table, so it carries the
# convention verbatim and DEFERS the shape distinction to `trigger/add.md`
# rather than growing a second copy of a definition that took three rounds to
# settle.
#
# The second test is the durable half. The prose no longer claims a total, so
# nothing here has to be re-asserted when a surface is added: a doctrine file
# that names a per-trigger prompt path is either covered or exempted with a
# reason, and a new one fails this test until someone says which.

_DOCTRINE = (
    REPO_ROOT
    / "casa/rootfs/opt/casa/defaults/agents/executors/configurator/doctrine"
)
PROMPT_EDIT = _DOCTRINE / "recipes/prompt/edit.md"

# COVERED: the file instructs authoring or editing that prompt's TEXT, so it
# states the convention or points at the recipe that does.
COVERED_DOCTRINE = {
    "recipes/trigger/add.md",
    "recipes/trigger/update.md",
    "recipes/prompt/edit.md",
}
# EXEMPT: the file names the path for some other reason, recorded here.
EXEMPT_DOCTRINE = {
    "recipes/trigger/remove.md": "offers to delete the file; authors no text",
    "reload.md": "picks the reload scope after an edit",
    "architecture.md": "a layout table row saying where the file lives",
    "safety.md": "an authorization row: allowed, but inert without a reload",
}


def test_the_prompt_edit_recipe_carries_the_convention_and_defers_the_shape():
    text = _normalized(PROMPT_EDIT)
    assert text.count(CONVENTION) == 1
    assert "recipes/trigger/add.md" in text
    assert "Every scheduled prompt says how the turn ends" in text
    # One wording, one place: it must not grow its own shape definition, since
    # a second copy is what drifts.
    assert "shape A" not in text and "shape B" not in text


def test_no_doctrine_file_naming_a_per_trigger_prompt_is_unclassified():
    """The enumeration above is checked, not claimed. A doctrine file that
    names `prompts/<...>` fails here until it is covered or exempted."""
    named = {
        str(path.relative_to(_DOCTRINE))
        for path in sorted(_DOCTRINE.rglob("*.md"))
        if "prompts/<" in path.read_text(encoding="utf-8")
    }
    assert named == COVERED_DOCTRINE | set(EXEMPT_DOCTRINE)
