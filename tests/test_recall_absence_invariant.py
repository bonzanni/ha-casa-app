"""A recall that never ran must never be reported as "nothing was found".

#201. Casa's recall contract has three outcomes — hits / zero_hits /
unavailable — because an agent that says "I have no record of that" when the
store merely timed out is lying to the operator.

A blank query was a silent fourth case wearing the second one's clothes: it
reached a speaking surface — `query_engager` accepted a blank question and
returned `status="unknown"`, which its tool description defines as "the memory
was searched and holds nothing relevant". No search had run.

The fix: **the blank query is a caller bug**, not an outcome —
`query_engager` rejects it as `empty_query` at the tool boundary. The template
test below keeps the shipped executor prompts from asserting that Casa has no
memory: an executor starts with no memory block, and its prompt must not read
that as "nothing is known".

SCOPE (Sol + Terra, Important): the template tests cover the SHIPPED DEFAULTS
under `defaults/agents/executors` only. Live executors load from
`/config/agents/executors` (`casa_core.py`), so an operator-edited prompt can
still assert absence and no test will catch it. Enforcing the invariant at load
or render time would close that gap and is deliberately not attempted here —
this guards the templates Casa ships and the ones a contributor adds.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CASA_ROOT = (
    Path(__file__).resolve().parent.parent
    / "casa" / "rootfs" / "opt" / "casa"
)
EXECUTORS_DIR = CASA_ROOT / "defaults" / "agents" / "executors"

# Wordings that assert Casa HAS NO memory, rather than simply omitting it.
# Deliberately broad, and still only a backstop: no blacklist can enumerate
# what a person might write.
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
    too. Scanning one filename only
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
    # Every .tmpl is scanned too: cheap, and it catches a template moved into
    # a sibling file.
    files.update(p for p in EXECUTORS_DIR.rglob("*.tmpl") if p.is_file())
    return sorted(files)


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


def test_no_executor_template_claims_casa_has_no_memory():
    """No shipped executor prompt asserts that Casa holds nothing: an
    executor starts without a memory block, which is not absence."""
    offenders: list[str] = []
    for template in _executor_template_files():
        haystack = template.read_text(encoding="utf-8").lower()
        offenders += [
            f"{template.parent.name}/{template.name}: {claim!r}"
            for claim in _ABSENCE_CLAIMS if claim in haystack
        ]

    assert not offenders, (
        "An executor prompt asserts Casa has no memory — the conflation #201 "
        "is about. Drop the wording:\n  " + "\n  ".join(offenders)
    )


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
