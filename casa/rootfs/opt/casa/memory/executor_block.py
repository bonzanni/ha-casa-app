"""The executor-memory section of an engagement workspace's ``CLAUDE.md``.

The prompt template's ``{executor_memory}`` slot is filled with a block of
memory-derived text. From this release every writer wraps that block in two
fixed markers, so the wipe can find the section again and empty it while
keeping every other byte of the file. Memory text can never forge a marker:
both marker strings are removed from the block before it is wrapped.

Every other input — the template text and the task, context and world-state
values — has any marker string neutralised (its ``<`` written as ``&lt;``,
which reads the same), and the template is filled in one pass, so a value is
never scanned for placeholders again. A marker pair in a file this release
writes therefore comes only from the memory slot.

Files written before this release carry the block with no markers. The wipe
then locates it through the control directory's cached copy, and uses
:data:`LEGACY_HEADER` to tell an empty block from a cache that was cut off
while it was being written (``memory.maintenance``).
"""
from __future__ import annotations

import re

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


#: The placeholders the executor templates use.
PLACEHOLDERS = ("{executor_type}", "{task}", "{context}",
                "{world_state_summary}", SLOT)
_PLACEHOLDER = re.compile("|".join(re.escape(p) for p in PLACEHOLDERS))


def neutralise(text: str) -> str:
    """*text* with every marker string made inert: its leading ``<`` becomes
    ``&lt;``. Nothing is removed, and no new ``<`` appears, so no marker can
    form."""
    return (text.replace(OPEN, "&lt;" + OPEN[1:])
                .replace(CLOSE, "&lt;" + CLOSE[1:]))


def render(template_text: str, values: dict[str, str], block: str) -> str:
    """*template_text* filled in one pass: the slot becomes the marked
    *block*, each placeholder in *values* its neutralised value, and any other
    placeholder is left as written. Values are never scanned again."""
    def _one(match: re.Match) -> str:
        token = match.group(0)
        if token == SLOT:
            return marked(block)
        return neutralise(values[token]) if token in values else token
    return _PLACEHOLDER.sub(_one, neutralise(template_text))
