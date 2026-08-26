"""Pinning tests for the corpus's own contract invariants (docs corpus).

These pin observable properties of the tracked docs/ tree itself: the parts of
the doctrine and doc-contract rules a test can check mechanically. Each test
names the invariant it pins and records the demonstrated red case.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

CODE_WINS = (
    "> Code is the source of truth. This file is a map; when it and the code "
    "disagree, the code wins."
)

SECTION_ORDER = [
    "## Scope", "## Mental model", "## Contracts & invariants",
    "## Failure behavior", "## Extension points", "## Source & test map",
]


def _entries():
    # #367: the manifest shards at its index ceiling — read the root plus every
    # docs/manifest.d/*.yaml shard, or these pins silently skip sharded docs.
    entries = []
    for source in [DOCS / "manifest.yaml"] + sorted((DOCS / "manifest.d").glob("*.yaml")):
        entries.extend(yaml.safe_load(source.read_text()))
    return entries


def _documents():
    return [
        e["doc"] for e in _entries()
        if e.get("kind", "document") == "document"
    ]


def _body_without_front_matter(text):
    if text.startswith("---\n"):
        end = text.index("\n---\n", 4)
        return text[end + 5:]
    return text


def test_pin_inv_doc_001_front_matter_and_code_wins_line():
    """INV-DOC-001: every document repeats the code-wins line verbatim at the
    top, under front matter carrying last_reviewed.

    Red case demonstrated: removing the code-wins line from one document
    fails this test.
    """
    for doc in _documents():
        text = (DOCS / doc).read_text()
        assert text.startswith("---\n"), doc
        front = text[4:text.index("\n---\n", 4)]
        assert "last_reviewed:" in front, doc
        assert CODE_WINS in text, doc


def test_pin_inv_doc_002_architecture_documents_follow_the_section_order():
    """INV-DOC-002: an architecture document follows the one section order.

    Red case demonstrated: swapping two section headings in one architecture
    document fails this test.
    """
    for doc in _documents():
        if not doc.startswith("architecture/"):
            continue
        text = (DOCS / doc).read_text()
        positions = [text.index(h) for h in SECTION_ORDER]
        assert positions == sorted(positions), doc


def test_pin_inv_doc_003_no_changelog_voice_markers():
    """INV-DOC-003: a document describes the present system — no TODOs, no
    "as of version" changelog voice. (The tense rule itself is a reviewer
    obligation; these are its mechanical markers.)

    Red case demonstrated: adding a TODO to one document fails this test.
    """
    for doc in _documents():
        body = _body_without_front_matter((DOCS / doc).read_text())
        # The rule's own definition line quotes the banned markers; exempt it.
        lines = [l for l in body.splitlines() if "INV-DOC-003" not in l]
        body = "\n".join(lines)
        assert not re.search(r"\bTODO\b", body), doc
        assert not re.search(r"as of version \d", body, re.IGNORECASE), doc


def test_pin_inv_pub_002_doctrine_carries_no_dated_history():
    """INV-PUB-002: doctrine states the mechanism, never the incident. The
    mechanical marker a test can pin: outside front matter, no doctrine
    document contains an ISO date — dates are incident-history shaped.

    Red case demonstrated: adding a dated incident sentence to a doctrine
    document fails this test.
    """
    for doc in _documents():
        if not doc.startswith("doctrine/"):
            continue
        body = _body_without_front_matter((DOCS / doc).read_text())
        assert not re.search(r"\b20\d{2}-\d{2}-\d{2}\b", body), doc


def test_documents_loader_sees_sharded_entries():
    """Terra r1 (#367): with architecture entries moved to docs/manifest.d/
    shards, a root-only loader silently exempts every architecture document
    from the front-matter, section-order and no-changelog pins."""
    docs = _documents()
    assert any(d.startswith("architecture/") for d in docs)


INV_DECLARATION = re.compile(r"(?m)^\*\*(INV-[A-Z0-9]+-\d+)\*\*:")

# #744. Two passages explain INV-TOOL-005 — the first continues its closing
# "which runner executes the setup tool" paragraph, the second names the
# `tests/test_assistant_prompts.py` wording pins the manifest binds to
# INV-TOOL-005. Both drifted into INV-TOOL-006's block when INV-TOOL-006 was
# inserted between them and the invariant they describe.
INV_TOOL_005_PASSAGES = [
    "See [`plugin-setup.md`](plugin-setup.md) (INV-PLUG-010) for what releases the run.",
    "This invariant is also pinned as *wording*:",
]


def _declaring_document(inv):
    """The single manifested document whose `defines_invariants` names `inv`."""
    owners = [e["doc"] for e in _entries()
              if inv in (e.get("defines_invariants") or [])]
    assert len(owners) == 1, (inv, owners)
    return owners[0]


def _invariant_block(inv):
    """`inv`'s declaration through to the next invariant declaration below it.

    Resolved through the manifest rather than a hard-coded corpus path, so the
    pin follows the invariant when a document is split.
    """
    text = (DOCS / _declaring_document(inv)).read_text()
    hits = [m for m in INV_DECLARATION.finditer(text) if m.group(1) == inv]
    assert len(hits) == 1, (inv, len(hits))
    following = INV_DECLARATION.search(text, hits[0].end())
    return text[hits[0].start():following.start() if following else len(text)]


def test_inv_tool_005_supporting_passages_sit_in_its_own_block():
    """#744: a passage that explains an invariant belongs to that invariant's
    block, not to the neighbour that was later inserted above it.

    Red case demonstrated: at f267816a both passages are the sole corpus
    occurrence and both sit inside INV-TOOL-006's block, so the counted tuple
    is (1, 0, 1) rather than (1, 1, 0).

    The corpus-wide count is part of the tuple on purpose: without it a copy
    into INV-TOOL-005 would pass while the misattributed original stayed put.
    """
    documents = _documents()
    block_005 = _invariant_block("INV-TOOL-005")
    block_006 = _invariant_block("INV-TOOL-006")
    for passage in INV_TOOL_005_PASSAGES:
        corpus = sum((DOCS / doc).read_text().count(passage) for doc in documents)
        assert (corpus, block_005.count(passage), block_006.count(passage)) == (1, 1, 0), passage


def _ledger_owner(item):
    """The document `docs/coverage.yaml` assigns `item` to."""
    ledger = yaml.safe_load((DOCS / "coverage.yaml").read_text())
    owners = [e["doc"] for e in ledger
              if e.get("item") == item and e.get("doc")]
    assert len(owners) == 1, (item, owners)
    return owners[0]


def test_every_document_naming_consent_reprompt_links_to_its_contract():
    """A cross-document route is a LINK to a path, never a document's title or
    a facility name.

    Two rounds of review found the same shape here: `plugin-triggers.md`,
    `callbacks.md` and `triggers.md` routed a `consent_reprompt` reader to "the
    tool interface" — the title of `tools-interface.md` — and
    `plugin-events.md` to "the callback facility". A title reference is
    invisible to a grep for the path, so a split cannot enumerate what it
    broke, and it is silently wrong the moment that path's content moves.

    The owning document is resolved from the coverage ledger rather than
    hard-coded, so this follows the tool if it is ever reassigned. The
    SOURCEMAP block is excluded on purpose: a generated `related` edge at the
    foot of the file is not the route the prose paragraph offers.

    Red case demonstrated: restoring any one of the four prose references to
    its title form fails this test, each independently.
    """
    owner = _ledger_owner("tool:consent_reprompt")
    link = re.compile(r"\]\((?:\.\./architecture/)?" + re.escape(Path(owner).name) + r"\)")
    checked = 0
    for doc in _documents():
        if doc == owner or not doc.startswith("architecture/"):
            continue
        text = (DOCS / doc).read_text()
        if "consent_reprompt" not in text:
            continue
        body = text.split("<!-- BEGIN SOURCEMAP -->")[0]
        assert link.search(body), doc
        checked += 1
    assert checked >= 4, checked
