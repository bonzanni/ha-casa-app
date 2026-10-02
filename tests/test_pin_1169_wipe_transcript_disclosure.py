"""#1169: every operator-facing statement of what a long-term-memory wipe
removes says that conversation transcripts on disk are not removed, and none
claims that everything durable is removed.

The wipe drops the bank, the retry spool and the session pointers
(``memory_wipe._wipe_locked``); dropping a pointer deletes no transcript, and
a transcript outlives its pointer (``architecture/memory-lifecycle.md``,
INV-MEM-017's "What it does not cover"). The three surfaces pinned here are
the user-facing paragraph in ``casa/DOCS.md``, INV-MEM-014's disclosure
paragraph in ``architecture/memory-wipe.md``, and ``WipeReport.residual_note``,
which both doors return through ``summary()``.

Each paragraph is extracted between two markers that must each occur once and
in order, and an empty extraction is red, so a renamed marker cannot make the
pin vacuous. The disclosure is matched as a whole clause in generic wording —
it never requires a list of which transcripts survive — and a mere mention of
the word "transcript" does not satisfy it.

Specified by Astra (red-case round, cluster W attempt 1). Red at 8de7e97c:
DOCS.md and residual_note carry "everything durable ... removed" and no
disclosure; memory-wipe.md never mentions transcripts.
"""

import re
from pathlib import Path

import pytest
from memory_wipe import WipeReport

REPO = Path(__file__).resolve().parents[1]

MARKERS = {
    "docs": (
        "casa/DOCS.md",
        "**Wiping long-term memory**",
        "Casa also carries a consent-gated agent door",
    ),
    "architecture": (
        "docs/architecture/memory-wipe.md",
        "What it does not cover, deliberately disclosed:",
        "## Failure behavior",
    ),
}


def _normalize(text):
    return " ".join(
        text.translate(str.maketrans("", "", "*_`")).casefold().split()
    )


def _paragraph(source, opening, closing):
    assert source.count(opening) == 1, "opening marker count"
    assert source.count(closing) == 1, "closing marker count"
    start, end = source.index(opening), source.index(closing)
    assert start < end, "marker order"
    body = _normalize(source[start + len(opening):end])
    assert len(body) > 0, "empty extraction"
    return body


_SUBJECT = (
    r"(?:conversation transcripts on disk|"
    r"on-disk conversation transcripts|"
    r"conversation transcripts stored on disk)"
)
_DISCLOSURE = re.compile(
    r"(?:^|[.;:]\s+)(?:"
    + _SUBJECT
    + r" (?:are not (?:removed|deleted|erased) by (?:the )?wipe|"
    r"remain on disk after (?:the )?wipe)|"
    r"(?:the )?wipe does not (?:remove|delete|erase) "
    + _SUBJECT
    + r")(?=[.;:]|$)"
)
_OVERCLAIM = re.compile(
    r"\beverything durable\s*(?:\([^)]*\)\s*)?"
    r"(?:is|was|has been)\s+(?:removed|deleted|erased)\b"
)


def _surface(name):
    if name == "report":
        return _normalize(WipeReport().residual_note)
    path, opening, closing = MARKERS[name]
    return _paragraph(
        (REPO / path).read_text(encoding="utf-8"), opening, closing
    )


@pytest.mark.parametrize("surface", ["docs", "architecture", "report"])
def test_wipe_discloses_disk_transcripts_are_not_removed(surface):
    assert len(_DISCLOSURE.findall(_surface(surface))) >= 1, (
        f"{surface}: missing transcript non-removal disclosure"
    )


@pytest.mark.parametrize("surface", ["docs", "architecture", "report"])
def test_wipe_does_not_claim_everything_durable_removed(surface):
    assert len(_OVERCLAIM.findall(_surface(surface))) == 0, (
        f"{surface}: unscoped durable erasure claim"
    )
