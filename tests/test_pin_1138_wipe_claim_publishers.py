"""#1138: every document the #633 comment block in
``tests/test_capability_parity.py`` names as a publisher of the claim "no
shipped agent is granted ``wipe_memory``" actually states that claim.

The #894 split moved the wipe out of ``architecture/memory-lifecycle.md`` into
``architecture/memory-wipe.md``, and the comment kept citing the old document.
Nothing resolves a document path cited in a test comment, so the stale
citation survived every gate.

The pin reads the documents the block cites, as the block cites them, and
looks for the claim by phrase across the whole document — never by line range,
and never through a table keyed by path, which would only restate the edit.
Every phrasing occurs in ``memory-wipe.md`` only inside its "Which door a
shipped install actually has" paragraph; the tool name alone would not do,
because it also appears where the doors are described. An empty extraction is
red, so a renamed marker or a dropped citation cannot make the pin vacuous.

Red case demonstrated at f9b87cf0: the block cites memory-lifecycle.md, which
states none of the phrasings.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLAIM_PHRASES = {
    "no shipped agent is granted it",
    "no shipped runtime, role or executor artifact names the wipe tool",
    "is held by nobody",
}


def _normalize(text):
    return " ".join(
        text.translate(str.maketrans("", "", "*_")).casefold().split()
    )


def test_wipe_claim_publishers_state_no_shipped_grant():
    source = (REPO / "tests/test_capability_parity.py").read_text(
        encoding="utf-8"
    )
    opening = re.search(
        r"^# #633 — the wipe door is granted to NOBODY, and the corpus says so$",
        source, re.MULTILINE,
    )
    closing = re.search(r"^GRANT_CARRIERS = ", source, re.MULTILINE)
    assert opening is not None, "#633 opening marker missing"
    assert closing is not None, "#633 closing marker missing"
    assert opening.start() < closing.start(), "#633 markers out of order"

    paths = set(re.findall(
        r"(?<![\w./-])[\w./-]+\.md(?![\w./-])",
        source[opening.end():closing.start()],
    ))
    assert paths, "#633 publisher citations are empty"

    for relative in sorted(paths):
        document = (REPO / relative).resolve()
        assert document.is_relative_to(REPO), (
            f"#633 publisher outside repo: {relative}"
        )
        assert document.is_file(), (
            f"#633 publisher is not a file: {relative}"
        )
        text = _normalize(document.read_text(encoding="utf-8"))
        assert any(phrase in text for phrase in CLAIM_PHRASES), (
            "#633 publisher does not state the no-shipped-grant claim: "
            f"{relative}"
        )
