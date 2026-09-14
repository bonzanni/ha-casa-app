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

import ast
import re
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


# --- #962 / #963: the class the surfaces name, and the rules they state -----
#
# #962: `ask_user` gained a scheduled arm (#573) and so joined the tools that put
# a scheduled turn's message in the operator's chat, and no surface said so. A
# scheduled turn that asks and then narrates delivers twice. #963: the rule that
# a tool call which is not a delivery decides nothing was stated four ways and
# pinned nowhere — the frozen case above stays green with add.md's rule block
# replaced by its opposite. The red cases below each bind their own literals, so
# a later edit to a shared constant cannot quietly change what they pin.

_TOOLS_PY = REPO_ROOT / "casa/rootfs/opt/casa/tools.py"


def test_every_delivering_tool_is_named_on_every_surface():
    """#962. Every tool whose successful call puts a scheduled turn's message
    in the operator's chat is named, by its declared name, on each surface that
    defines how such a prompt ends. The class is stipulated here; whether it is
    still the whole class is what the tool-axis classification checks."""
    tree = ast.parse(_TOOLS_PY.read_text(encoding="utf-8"))
    registry = [
        node for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "CASA_TOOLS"
    ]
    assert len(registry) == 1, ("CASA_TOOLS", len(registry))
    assert isinstance(registry[0].value, ast.Tuple)
    registered = [e.id for e in registry[0].value.elts if isinstance(e, ast.Name)]

    missing = []
    for key in ("send_message", "send_media", "ask_user"):
        assert registered.count(key) == 1, (
            f"tools.py::{key}", "CASA_TOOLS", registered.count(key))
        functions = [
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == key
        ]
        assert len(functions) == 1, (f"tools.py::{key}", "async def", len(functions))
        decorators = [
            d for d in functions[0].decorator_list
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Name)
            and d.func.id == "tool"
        ]
        assert len(decorators) == 1, (f"tools.py::{key}", "@tool", len(decorators))
        first = decorators[0].args[0]
        assert isinstance(first, ast.Constant) and isinstance(first.value, str)
        declared = first.value
        for path in SURFACES:
            count = len(re.findall(
                r"(?<!\w)" + re.escape(declared) + r"(?!\w)", _normalized(path)))
            if count < 1:
                missing.append((f"tools.py::{key}", str(path.relative_to(REPO_ROOT)), count))
    assert missing == []


def test_every_surface_states_the_non_delivery_rule_in_one_wording():
    """Pins a WORDING on five carriers, not a closed class of tools.

    What this pins is a WORDING, not a closed class. Nothing in the tree reads
    these files at runtime — they are prompt surfaces consumed by a model — so
    there is no property here a test could execute, and the strongest assertion
    available is a string match on prose. Measured, it catches the three
    mutations this module was green on before it existed: deleting the rule,
    re-wording it on one surface, and replacing it with its opposite. It catches
    none of these: a contradiction added beside the sentence, a surface that
    carries the sentence and then works its example the other way, or the prompt
    an author actually writes after reading it. A reader must not take this test
    as evidence that the class of tools is closed — what closes that class
    against the code is
    `test_no_declared_tool_is_unclassified_for_the_closing_convention`, and what
    closes nothing at all is the runtime, which is #960's.
    """
    NON_DELIVERY_RULE = (
        "Which prompts the clause belongs to is decided by where the operator's "
        "copy of the message comes from, never by whether the turn calls a tool: "
        "a tool call that is not a delivery decides nothing here."
    )
    counts = {
        str(path.relative_to(REPO_ROOT)): _normalized(path).count(NON_DELIVERY_RULE)
        for path in [*SURFACES, PROMPT_EDIT]
    }
    assert counts == dict.fromkeys(counts, 1)


def test_every_carrier_says_what_a_turn_outputs_when_the_ask_was_not_awaiting():
    """Pins a WORDING on five carriers, not a behaviour.

    A scheduled `ask_user` call can return without posting anything — the
    operator already has a question or an authorization challenge waiting, or
    the post landed no message id — and a turn that then outputs only the
    sentinel delivers NOTHING. Naming `ask_user` as a delivery (the case above)
    is only safe beside this rule. Its reach, stated: it pins that every carrier
    says what the turn outputs when the ask was not awaiting the operator's
    answer; it does not run that turn, and it does not make an agent follow the
    sentence. It deliberately says nothing about `send_message` or `send_media`,
    whose reports are not reliable in either direction (#990), so no rule an
    author could follow from them is stated.
    """
    ASK_RULE = (
        "A turn that asks with `ask_user` has put its question in the chat only "
        "when the ask reports that it is awaiting the operator's answer; when it "
        "reports anything else, the turn outputs what the ask reported as its "
        "final text instead of the sentinel."
    )
    counts = {
        str(path.relative_to(REPO_ROOT)): _normalized(path).count(ASK_RULE)
        for path in [*SURFACES, PROMPT_EDIT]
    }
    assert counts == dict.fromkeys(counts, 1)
