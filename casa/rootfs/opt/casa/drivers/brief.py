"""Structured engagement-brief envelope (W3/Sol B10).

Pure, stdlib-only helpers used by ``tools.py`` (engage_executor). No I/O, no
framework imports — this module is the single neutral owner of the brief
contract, so importing it cannot create a cycle.

Design (frozen §W3/§W2/§211):
  - A brief is ``{objective, acceptance_criteria, process_requirements,
    context, interaction_required}``; only ``objective`` is required.
  - The RAW brief is the single authoritative source. It is persisted
    VERBATIM on ``engagement.origin["brief"]`` (no injected default keys);
    every consumer DERIVES the rendered markdown block from it at use time
    via ``normalize_brief`` + ``render_brief_task`` — a persisted derived
    ``rendered_task`` would violate §211 and could go stale.
  - ``render_brief_task`` produces the W3 content for the executor's
    system_prompt through the ONE ``{task}`` seam. The completion-accounting
    line is UNCONDITIONAL whenever a brief is present.
"""

from __future__ import annotations

from typing import Any

# The completion contract [D:§W3] — appended UNCONDITIONALLY whenever a brief
# is present, regardless of interaction_required (Sol r2-B8).
COMPLETION_ACCOUNTING_LINE = (
    "Your completion summary MUST account for the objective, each acceptance "
    "criterion, and each process requirement."
)

def validate_brief(brief: Any) -> str | None:
    """Validate the presence/type rules of a raw brief object.

    Returns an error string (suitable for an ``invalid_arguments`` message)
    or ``None`` when the brief is well-formed. The task-XOR-brief and the
    legacy-top-level-context checks are the HANDLER's job (a JSON Schema /
    this validator can't cleanly express the cross-field XOR).

    Rules (Sol B10):
      - ``objective`` is required and must be a non-empty ``str``.
      - when PRESENT, ``acceptance_criteria`` / ``process_requirements`` must
        be ``list``s of non-empty ``str`` (MAY be empty lists).
      - when PRESENT, ``context`` must be a ``str``.
      - when PRESENT, ``interaction_required`` must be a ``bool`` (not merely
        truthy — ``"yes"`` / ``1`` are rejected).
    """
    if not isinstance(brief, dict):
        return "brief must be an object"

    objective = brief.get("objective")
    if not isinstance(objective, str) or not objective:
        return "brief.objective is required and must be a non-empty string"

    for key in ("acceptance_criteria", "process_requirements"):
        if key in brief:
            value = brief[key]
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item for item in value
            ):
                return f"brief.{key} must be a list of non-empty strings"

    if "context" in brief and not isinstance(brief["context"], str):
        return "brief.context must be a string"

    # ``bool`` is a subclass of ``int`` — check bool FIRST and reject any
    # non-bool (so ``interaction_required: "yes"`` / ``1`` fail, per Sol B10).
    if "interaction_required" in brief and not isinstance(
        brief["interaction_required"], bool
    ):
        return "brief.interaction_required must be a boolean (true/false)"

    return None


def normalize_brief(brief: dict) -> dict:
    """The omitted-field defaults VIEW of a validated brief (Sol r4-B6).

    Callers MUST have validated first (``validate_brief`` is None). Omission
    is legal; the defaults are computed here into a SEPARATE view — the raw
    brief on ``origin["brief"]`` is never mutated / never gains default keys.
    """
    return {
        "objective": brief["objective"],
        "acceptance_criteria": list(brief.get("acceptance_criteria") or []),
        "process_requirements": list(brief.get("process_requirements") or []),
        "context": brief.get("context") or "",
        "interaction_required": bool(brief.get("interaction_required", False)),
    }


def render_brief_task(normalized: dict) -> str:
    """Render the W3 markdown block for the ``{task}`` seam.

    Sections: Objective / Acceptance criteria (omitted cleanly when empty) /
    Process requirements (EXACT verbatim strings; omitted when empty) /
    Context (when non-empty) — then the UNCONDITIONAL completion-accounting
    line.
    """
    parts: list[str] = ["## Objective\n" + normalized["objective"]]

    acceptance = normalized["acceptance_criteria"]
    if acceptance:
        parts.append(
            "## Acceptance criteria\n"
            + "\n".join(f"- {c}" for c in acceptance)
        )

    process = normalized["process_requirements"]
    if process:
        parts.append(
            "## Process requirements (VERBATIM — follow these)\n"
            + "\n".join(f"- {p}" for p in process)
        )

    context = normalized["context"]
    if context:
        parts.append("## Context\n" + context)

    parts.append(COMPLETION_ACCOUNTING_LINE)

    return "\n\n".join(parts)
