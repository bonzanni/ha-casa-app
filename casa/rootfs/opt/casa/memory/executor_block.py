"""The executor-memory section of an engagement workspace's ``CLAUDE.md``.

The prompt template's ``{executor_memory}`` slot is filled with a block of
memory-derived text. From this release every writer wraps that block in two
fixed markers, so the wipe can find the section again and empty it while
keeping every other byte of the file. Memory text can never forge a marker:
both marker strings are removed from the block before it is wrapped.

The rest of the file — the template with the task, context and world-state
values filled in, in one pass — is neutralised as a whole (a marker string's
``<`` written as ``&lt;``, which reads the same) before the marked block goes
in, so a marker pair in a file this release writes comes only from the
memory slot, however its pieces were assembled.

Files written before this release carry the block with no markers. The wipe
then locates it through the control directory's cached copy, and uses
:data:`LEGACY_HEADER` to tell an empty block from a cache that was cut off
while it was being written (``memory.maintenance``).
"""
from __future__ import annotations

import re
import uuid

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
    """*template_text* filled in one pass: each placeholder in *values* gets
    its value and any other placeholder is left as written; values are never
    scanned again. The whole text is then neutralised — so a marker cannot
    be assembled from a template piece and a value either — and only then
    does the slot become the marked *block*. A marker can therefore come
    only from the block's own wrapper."""
    sentinel = f"\x00casa-memory-slot-{uuid.uuid4().hex}\x00"

    def _one(match: re.Match) -> str:
        token = match.group(0)
        if token == SLOT:
            return sentinel
        return values[token] if token in values else token

    text = neutralise(_PLACEHOLDER.sub(_one, template_text))
    return text.replace(sentinel, marked(block))
