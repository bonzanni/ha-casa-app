"""Engagement-conduct regression guards on the `ask` tool docstring.

The plugin-developer doctrine and workspace template these guards also read
went with the plugin-developer; the `ask` docstring is what the executing model
still reads at tool-selection time, so its pause/silence contract, the
options-not-prose counter-example, the `short` steer and the no-pre-labeling
rule stay pinned here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parent.parent


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text)


@pytest.fixture(scope="module")
def ask_docstring() -> str:
    import inspect

    import sys

    sys.path.insert(0, str(_ROOT / "casa/rootfs/opt/casa"))
    from channels import casa_engagement_channel as cec

    return (inspect.getdoc(cec.ask) or "").lower()


# --- ask docstring carries the pause + silence contract --------------------


def test_ask_docstring_pause_and_silence_contract(ask_docstring):
    assert "no_answer" in ask_docstring
    assert "pause" in ask_docstring        # engagement PAUSED
    assert "silent" in ask_docstring       # end your turn silently
    assert "re-ask" in ask_docstring       # do NOT re-ask
    # round 4 (D3): the `embedded_options` refusal is retired — the docstring
    # must not claim a framework refusal that no longer exists.
    assert "embedded_options" not in ask_docstring


# --- round 4 (D2-D5, Task B2) additions --------------------------------------


_SILENT_YIELD_COUNTEREXAMPLE = (
    "the ask posted as an open-ended anchor. i'll end my turn and wait for "
    "the operator's answer before proceeding."
)


def test_ask_docstring_options_not_prose_inline_parenthetical_counterexample(
    ask_docstring,
):
    assert "must go in" in ask_docstring or "never as prose" in ask_docstring
    assert "(a)" in ask_docstring and "(b)" in ask_docstring


def test_ask_docstring_short_is_prominent(ask_docstring):
    """D2: the ask tool description makes `short` an OBVIOUS, steered path —
    not a buried afterthought. It must appear in the first ~500 chars (right
    alongside the `options` shape description) and be phrased as guidance
    the agent should follow, not just documented shape."""
    assert "short" in ask_docstring
    lead = ask_docstring[:500]
    assert "short" in lead, (
        "`short` must be prominent (near the top of the docstring), "
        "not buried after unrelated detail"
    )
    assert "supply" in ask_docstring or "couple of words" in ask_docstring


def test_ask_docstring_no_pre_labeling_options_or_question(ask_docstring):
    assert "q7:" in ask_docstring
    assert "never pre-" in ask_docstring


#
# P1-A: mirror the canonical plan into TodoWrite for operator visibility
# (display authority, not execution authority; updates land BEFORE an
# ask/terminal boundary; the checklist never replaces required `ask` buttons
# or ask-then-stop). P3: when narration is otherwise permitted, narrate in
# complete sentences and never end a segment with a dangling colon
# announcing content that won't stream — with explicit precedence under
# ask-then-stop / end-turns-silently / buttons-always.
