"""The executor-memory section of an engagement workspace's ``CLAUDE.md``.

The prompt template's ``{executor_memory}`` slot is filled with a block of
memory-derived text. From this release every writer wraps that block in two
fixed markers, so the wipe can find the section again and empty it while
keeping every other byte of the file. Memory text can never forge a marker:
both marker strings are removed from the block before it is wrapped.

Files written before this release carry the block with no markers. The wipe
then locates it through the control directory's cached copy, and uses
:data:`LEGACY_HEADER` to tell an empty block from a cache that was cut off
while it was being written (``memory.maintenance``).
"""
from __future__ import annotations

OPEN = "<!-- casa:executor-memory -->"
CLOSE = "<!-- /casa:executor-memory -->"

#: The template placeholder the section replaces.
SLOT = "{executor_memory}"

#: The first line of every non-empty block the pre-release producer wrote; an
#: empty block rendered as nothing at all.
LEGACY_HEADER = "## Prior engagements (lessons learned)"


def strip_markers(text: str) -> str:
    """*text* with every marker string removed, repeated until none is left
    (removing one occurrence can join the pieces of another)."""
    while OPEN in text or CLOSE in text:
        text = text.replace(OPEN, "").replace(CLOSE, "")
    return text


def marked(block: str) -> str:
    """*block* as a delimited section."""
    return OPEN + strip_markers(block or "") + CLOSE


def fill(template_text: str, block: str) -> str:
    """*template_text* with every slot replaced by the marked *block*."""
    return template_text.replace(SLOT, marked(block))
