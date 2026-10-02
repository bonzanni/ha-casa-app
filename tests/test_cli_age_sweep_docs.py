"""The corpus states WHY the CLI's own transcript cleanup never runs under Casa
(#1181, INV-MEM-021).

Before #1181 two documents said that cleanup "never fires under Casa's SDK
invocation" while three launch sites made it fire. The statement is true now
only because of a reason — no launch loads the user settings source or passes
``cleanupPeriodDays`` — so both places must carry that reason, where a reader
deciding whether Casa can rely on the hold of INV-MEM-017 will look.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

DOCS = Path(__file__).resolve().parents[1] / "docs" / "architecture"


def _paragraph(text: str, lead: str) -> str:
    """The paragraph that starts with *lead* (non-empty, or the test fails)."""
    for para in re.split(r"\n\s*\n", text):
        if para.lstrip().startswith(lead):
            return " ".join(para.split())
    raise AssertionError(f"no paragraph starts with {lead!r}")


def test_memory_lifecycle_declares_why_the_cli_cleanup_is_off():
    para = _paragraph((DOCS / "memory-lifecycle.md").read_text(encoding="utf-8"),
                      "**INV-MEM-021**:")
    assert "user settings source" in para
    assert "`cleanupPeriodDays`" in para
    assert "never enabled by a Casa launch" in para


def test_memory_lifecycle_inv_mem_017_names_the_cli_cleanup():
    text = (DOCS / "memory-lifecycle.md").read_text(encoding="utf-8")
    para = _paragraph(text, "The sweep is Casa's only deleter of resident transcripts")
    assert "INV-MEM-021" in para


def test_engagement_finalization_gives_the_reason_with_the_claim():
    para = _paragraph(
        (DOCS / "engagement-finalization.md").read_text(encoding="utf-8"),
        "Casa owns transcript deletion:")
    assert "never fires" in para
    assert "`cleanupPeriodDays`" in para
    assert "INV-MEM-021" in para
