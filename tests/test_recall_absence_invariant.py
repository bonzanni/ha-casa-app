"""A recall that never ran must never be reported as "nothing was found".

#201. Casa's recall contract has three outcomes — hits / zero_hits /
unavailable — because an agent that says "I have no record of that" when the
store merely timed out is lying to the operator.

A blank query was a silent fourth case wearing the second one's clothes: it
reached a speaking surface — `query_engager` accepted a blank question and
returned `status="unknown"`, which its tool description defines as "the memory
was searched and holds nothing relevant". No search had run.

The fix has two halves, and this module guards both:

1. **The blank query is a caller bug**, not an outcome: `query_engager`
   rejects it as `empty_query` at the tool boundary.
2. **Empty renders as silence.** `_fetch_executor_archive` returns `""` when
   the memory store is unavailable and runs cold, which is safe *only* because its result
   lands in the `{executor_memory}` prompt slot, where empty means the section
   is absent. Absence of a block is not a claim of absence. That reasoning
   lives in the TEMPLATES, so the template tests below pin it: the moment a
   prompt says "no prior engagements found", running cold starts asserting
   absence and `_fetch_executor_archive` needs a real third state.

SCOPE (Sol + Terra, Important): the template tests cover the SHIPPED DEFAULTS
under `defaults/agents/executors` only. Live executors load from
`/config/agents/executors` (`casa_core.py`), so an operator-edited prompt can
still assert absence and no test will catch it. Enforcing the invariant at load
or render time would close that gap and is deliberately not attempted here —
this guards the templates Casa ships and the ones a contributor adds.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

CASA_ROOT = (
    Path(__file__).resolve().parent.parent
    / "casa" / "rootfs" / "opt" / "casa"
)
EXECUTORS_DIR = CASA_ROOT / "defaults" / "agents" / "executors"

MEMORY_SLOT = "{executor_memory}"

# The other substitution slots an executor prompt may legitimately place
# immediately above the memory slot. Enumerated so that a NEW slot has to be
# added here consciously rather than exempting itself by shape.
_SIBLING_SLOTS = frozenset({
    "{task}", "{context}", "{world_state_summary}", "{executor_type}",
    MEMORY_SLOT,
})

# Wordings that assert Casa HAS NO memory, rather than simply omitting it.
# Deliberately broad: this is a backstop behind the structural checks, not the
# primary guard, because no blacklist can enumerate what a person might write.
_ABSENCE_CLAIMS = (
    "no prior engagement", "no previous engagement", "no past engagement",
    "no lessons", "no relevant memor", "no memories", "no memory of",
    "nothing in memory", "nothing relevant was recalled", "nothing was recalled",
    "no record", "no past work", "memory is empty", "empty memory",
    "you have no", "there is no history", "no known history",
    "no relevant history", "none available", "zero results", "no results",
    "nothing to draw on", "no context appears", "assume there is no",
)


def _executor_template_files() -> list[Path]:
    """Every file an executor's prompt can actually be rendered from.

    Not just ``prompt.md`` (Sol + Terra, Important): ``agent_loader`` honours a
    per-executor ``prompt_template_file``, and every ``.tmpl`` file is scanned
    too (the since-removed Claude Code driver rendered one through
    the same ``{executor_memory}`` substitution). Scanning one filename only
    would let a rename walk straight past this guard.
    """
    files: set[Path] = set()
    for defn_path in EXECUTORS_DIR.rglob("definition.yaml"):
        try:
            defn = yaml.safe_load(defn_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:  # a malformed definition is another test's job
            continue
        name = defn.get("prompt_template_file", "prompt.md")
        if isinstance(name, str) and name:
            candidate = defn_path.parent / name
            if candidate.is_file():
                files.add(candidate)
    files.update(p for p in EXECUTORS_DIR.rglob("prompt.md") if p.is_file())
    # Only CLAUDE.md.tmpl is interpolated at render time (Sol, Minor) — the
    # rest of a workspace-template tree is copied verbatim — but every .tmpl is
    # scanned anyway: cheap, and it catches a slot moved into a sibling file.
    files.update(p for p in EXECUTORS_DIR.rglob("*.tmpl") if p.is_file())
    return sorted(files)


def _templates_with_memory_slot() -> list[Path]:
    return [
        p for p in _executor_template_files()
        if MEMORY_SLOT in p.read_text(encoding="utf-8")
    ]


def test_every_configured_prompt_template_exists_and_is_scanned():
    """A `prompt_template_file` pointing at a missing file would silently
    shrink this guard's coverage to nothing."""
    missing: list[str] = []
    for defn_path in EXECUTORS_DIR.rglob("definition.yaml"):
        defn = yaml.safe_load(defn_path.read_text(encoding="utf-8")) or {}
        name = defn.get("prompt_template_file", "prompt.md")
        if not (defn_path.parent / name).is_file():
            missing.append(f"{defn_path.parent.name}: {name}")
    assert not missing, (
        "executor definition names a prompt template that does not exist:\n  "
        + "\n  ".join(missing)
    )


def test_at_least_one_executor_prompt_uses_the_memory_slot():
    """Guard the guard: if the slot is renamed, the tests below would pass
    vacuously over an empty file list and stop protecting anything."""
    assert _templates_with_memory_slot(), (
        f"no executor template contains {MEMORY_SLOT} — was the slot renamed? "
        "These tests must be retargeted, not deleted."
    )


def test_no_executor_template_claims_casa_has_no_memory():
    """Every template, not only the ones holding the slot (Sol, Important).

    A template that DROPS the slot and keeps "prior engagements: none
    available" is worse, not better: it asserts absence unconditionally — even
    when the recall returned hits — while a sibling `prompt.md` that still has
    the slot keeps the non-vacuity test green.
    """
    offenders: list[str] = []
    for template in _executor_template_files():
        haystack = (
            template.read_text(encoding="utf-8").replace(MEMORY_SLOT, "").lower()
        )
        offenders += [
            f"{template.parent.name}/{template.name}: {claim!r}"
            for claim in _ABSENCE_CLAIMS if claim in haystack
        ]

    assert not offenders, (
        "An executor prompt asserts Casa has no memory. Empty is also what a "
        "FAILED read returns "
        "(_fetch_executor_archive runs cold on an unavailable store), so the executor "
        "would state absence during a backend outage — the exact conflation "
        "#201 is about. Either drop the wording, or give that path a real "
        "third state:\n  " + "\n  ".join(offenders)
    )


def _is_label(line: str) -> bool:
    """Does `line`, sitting directly above the slot, introduce it as a section?

    Only two things are exempt outright, because neither can title anything:
    a sibling substitution slot, and a horizontal rule.

    A list marker is NOT an exemption (Sol, round 4): `- Prior engagements:`
    and `1. Memory context` are labels that happen to be bulleted. The marker
    is stripped and the remainder judged on its own merits, so a bulleted
    label is caught while a bulleted *sentence* still passes.
    """
    if line in _SIBLING_SLOTS:
        return False
    if len(line) >= 3 and set(line) <= set("-*_ "):  # --- *** ___ rules
        return False

    # Strip a list marker and judge what it actually introduces.
    stripped = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", line).strip()
    if not stripped:
        return False

    # A CLOSING tag cannot open a section (Terra, round 4) — `</task_context>`
    # directly above the slot is ordinary layout, not a heading.
    if stripped.startswith("</"):
        return False
    # Headings are labels by MARKER, whatever punctuation they end with — the
    # `## Prior engagements.` bypass (Sol, round 3).
    if stripped.startswith(("#", "<")):
        return True
    if stripped.endswith(":"):
        return True
    if stripped.startswith("**") and stripped.endswith("**"):
        return True
    if stripped.startswith("__") and stripped.endswith("__"):
        return True
    # Anything else that is not a finished sentence reads as a label, at any
    # length (a 60-character cap was the round-2 bypass).
    return not stripped.endswith((".", "?", "!"))


def test_memory_slot_is_never_introduced_by_a_label():
    """The slot must stand alone, unlabelled.

    A preceding label is a claim of absence by layout rather than by sentence:
    with an empty block the executor reads a heading with nothing under it.
    The header ships INSIDE the value instead — `_fetch_executor_archive`
    prepends it only when there is actually a digest.

    Structural, not a blacklist (Sol + Terra). Two earlier attempts leaked:

    * "short, or ends with a colon, or starts with #" let a 60+ character
      label through;
    * "a label unless it ends in sentence punctuation" let `## Prior
      engagements.` through — a heading that happens to end in a period — and
      falsely rejected list items and horizontal rules.

    So headings are judged by their MARKER, never by punctuation, and layout
    that cannot introduce a section (rules, list items, sibling slots) is
    exempted explicitly rather than by accident.
    """
    offenders: list[str] = []
    for template in _templates_with_memory_slot():
        lines = template.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if MEMORY_SLOT in line and line.strip() != MEMORY_SLOT:
                offenders.append(
                    f"{template.name}:{i+1}: slot shares a line with other "
                    f"text: {line.strip()!r}"
                )
                continue
            if line.strip() != MEMORY_SLOT:
                continue
            preceding = next(
                (p.strip() for p in reversed(lines[:i]) if p.strip()), "",
            )
            if not preceding:
                continue
            # A sibling substitution slot is not a label for ours — it
            # renders as its own content or as nothing. Enumerated, not
            # "anything in braces" (Sol, Important): an arbitrary
            # {looks_like_a_slot} must not become an exemption.
            if _is_label(preceding):
                offenders.append(
                    f"{template.name}:{i+1}: preceded by {preceding!r}, which "
                    "reads as an empty section when the recall returns nothing"
                )

    assert not offenders, "\n  ".join(["memory slot is labelled:", *offenders])


@pytest.mark.asyncio
async def test_query_engager_rejects_a_blank_question_instead_of_claiming_absence():
    """The bug #201 named, at the surface where it actually spoke.

    `status="unknown"` is documented as "the memory was searched and holds
    nothing relevant". A blank question must never reach it.
    """
    import tools

    class _Engagement:
        id = "eng-1"
        role_or_type = "configurator"
        origin = {"channel": "telegram"}

    token = tools.engagement_var.set(_Engagement())
    try:
        for blank in ("", "   "):
            result = await tools.query_engager.handler(
                {"question": blank, "max_tokens": 500},
            )
            payload = result["content"][0]["text"]
            assert '"kind": "empty_query"' in payload or "empty_query" in payload, (
                f"blank question returned {payload!r}"
            )
            assert '"unknown"' not in payload, (
                "a blank question was reported as a completed zero-hit search"
            )
    finally:
        tools.engagement_var.reset(token)


def tools_source_path() -> str:
    import tools
    return tools.__file__
