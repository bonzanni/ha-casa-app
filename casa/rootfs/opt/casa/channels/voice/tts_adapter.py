"""Rewrite canonical [tag] syntax to the agent's configured dialect.

Canonical form in agent personalities: [confident], [warm], etc.
Dialects: square_brackets (identity) | parens | none.
"""

from __future__ import annotations

import re

_VALID = ("square_brackets", "parens", "none")

# Leading-only: one or more CANONICAL [tag] atoms at the start of the
# block, each followed by optional whitespace. Parentheticals are never
# stripped (#357, review round 2): the canonical tag syntax is square
# brackets only, there is no closed tag vocabulary to whitelist, and any
# shape heuristic ("short lowercase phrase") also matches substantive
# prose — "(do not take it) Call emergency services." must be spoken in
# full. The cost is that a non-canonical parens tag under dialect `none`
# is read aloud; the alternative cost is deleting safety-relevant speech.
_LEADING_TAGS_RE = re.compile(r"^(?:\s*\[[^\]]*\]\s*)+")
_ANY_SQUARE_TAG_RE = re.compile(r"\[([^\]]+)\]")
# #594: what counts as something a listener HEARS — see `has_speech`.
_WORD_RE = re.compile(r"\w")


def has_speech(block: str) -> bool:
    """True when *block* carries words, not only canonical ``[tag]`` atoms.

    #594: a configured line can be schema-valid and still say nothing aloud —
    ``"[flat]"`` is a delivery instruction with no sentence after it. Under
    dialect ``none`` rendering already reduces that to ``""``, but under
    ``square_brackets`` and ``parens`` the rendered string stays non-empty
    while the listener hears nothing, so testing the RENDERED text for
    emptiness answers the wrong question. Decided on the CANONICAL form, with
    the same leading-tag definition rendering itself uses, so the answer does
    not vary with the persona's dialect.

    The test is for a WORD-BEARING character, not for leftover non-whitespace
    (both reviewers, round 4). ``_LEADING_TAGS_RE`` stops at the first ``]``,
    so a malformed nested tag — ``"[flat [warm]]"`` — leaves ``"]"`` behind,
    and reading that residue as speech let a retraction be spoken with ``"]"``
    as its entire reason. A parenthetical still counts as speech: rendering
    deliberately preserves it (it can be safety-relevant), so this must agree.
    """
    return bool(_WORD_RE.search(_LEADING_TAGS_RE.sub("", block or "")))


class TagDialectAdapter:
    def __init__(self, dialect: str) -> None:
        if dialect not in _VALID:
            raise ValueError(
                f"Invalid tag_dialect {dialect!r}; must be one of {_VALID}"
            )
        self._dialect = dialect

    def render(self, block: str) -> str:
        if self._dialect == "square_brackets":
            return block
        if self._dialect == "parens":
            return _ANY_SQUARE_TAG_RE.sub(lambda m: f"({m.group(1)})", block)
        # 'none' — strip any leading run of canonical [tag] atoms
        return _LEADING_TAGS_RE.sub("", block).lstrip()
