"""#900: the SDK retry / resume-fault knob list has exactly ONE owning document.

Three signals name the owner of that knob list, and nothing in the committed
corpus machinery compares them against each other:

* ``docs/coverage.yaml``'s four ``env:`` rows — but an ``env:`` item keys no
  manifest ``covers`` anchor, so ``scripts/coverage_ledger.py`` checks only
  that the assigned document is manifested, never that its prose says anything
  about the variable;
* the corpus prose that actually lists the four knobs with their defaults;
* the ``docs/`` path that ``casa/rootfs/opt/casa/agent.py``'s
  ``RESUME_FAULT_LIMIT`` comment sends a reader to for that list — and
  ``scripts/verify_docs.py`` resolves no ``docs/`` PATH cited from outside
  ``docs/`` at all (INV-DOC-009, ``scripts/verify_docs.py:1032``, extracts
  ``INV-*`` IDS from Python prose and nothing else; the path gap is open issue
  #761).

So the two failures this pins are both green under every committed gate: the
comment pointing one hop short of the list (#900 itself), and a later change
moving the four ledger rows back onto ``architecture/turn-loop.md``, silently
reversing the ownership half of the split that shipped in ``06edfc56``.

Scoped deliberately to these four variables. A general rule over every ``env:``
row is not available: measured at ``06edfc56``, 7 of the 56 ``env:`` rows carry
a document that does not name the variable.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OWNER = "architecture/sdk-client-pool.md"

#: The four knobs and the defaults the owning document states for them.
KNOBS = {
    "SDK_RETRY_MAX_ATTEMPTS": "3",
    "SDK_RETRY_INITIAL_MS": "500",
    "SDK_RETRY_CAP_MS": "8000",
    "SDK_RESUME_FAULT_LIMIT": "2",
}

_ROW = re.compile(
    r"^- item: env:(" + "|".join(KNOBS) + r")\n  doc: (\S+)$", re.MULTILINE
)
_LISTED = re.compile(r"`(" + "|".join(KNOBS) + r")` \((?:default )?(\d+)\)")
_DOC_PATH = re.compile(r"docs/[A-Za-z0-9_./-]+\.md")


def _ledger_rows() -> list[tuple[str, str]]:
    text = (DOCS / "coverage.yaml").read_text(encoding="utf-8")
    return _ROW.findall(text)


def _resume_fault_limit_comment() -> str:
    """The contiguous ``#:`` block immediately above the annotated constant.

    Block-scoped on purpose: a correct path anywhere else in ``agent.py`` must
    not satisfy the assertion about THIS comment.
    """
    lines = (
        ROOT / "casa" / "rootfs" / "opt" / "casa" / "agent.py"
    ).read_text(encoding="utf-8").splitlines()
    constant = next(
        i for i, line in enumerate(lines)
        if line.startswith("RESUME_FAULT_LIMIT:")
    )
    block: list[str] = []
    for line in reversed(lines[:constant]):
        if not line.startswith("#:"):
            break
        block.append(line)
    assert block, "no `#:` comment block precedes RESUME_FAULT_LIMIT"
    return "\n".join(reversed(block))


def test_ledger_assigns_all_four_knobs_to_one_document() -> None:
    rows = _ledger_rows()
    # A list, never a dict: collapsing hides a duplicated row.
    assert sorted(rows) == sorted((name, OWNER) for name in KNOBS)


def test_exactly_one_corpus_document_lists_the_four_knobs() -> None:
    naming = {
        str(p.relative_to(DOCS))
        for p in DOCS.rglob("*.md")
        if all(name in p.read_text(encoding="utf-8") for name in KNOBS)
    }
    assert naming == {OWNER}


def test_the_owning_document_states_each_default_exactly_once() -> None:
    prose = (DOCS / OWNER).read_text(encoding="utf-8")
    listed = _LISTED.findall(prose)
    assert sorted(listed) == sorted(KNOBS.items())


def test_the_constant_comment_points_at_the_document_that_lists_the_knobs() -> None:
    assert _DOC_PATH.findall(_resume_fault_limit_comment()) == [f"docs/{OWNER}"]
