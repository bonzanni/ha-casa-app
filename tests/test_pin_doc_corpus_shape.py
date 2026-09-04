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


# #668: `architecture/triggers.md` restated the plugin-declared-trigger rules
# while its own Scope disclaimed the subject.
#
# WHAT IS DELIBERATELY NOT PINNED, and why. Four review rounds tried to express
# "this document does not restate that one" mechanically — an enumerated
# passage list (twice found incomplete), then word-shingle overlap between the
# two `## Extension points` sections at several window sizes, with and without
# the shared bolded lead-ins excluded. Every variant either refused a correct
# routing sentence or missed a short residual, because a pointer that NAMES the
# subjects it routes away from is lexically indistinguishable from a copy that
# STATES them. That is not a tuning problem, and the corpus already says so:
# `docs/contributing/doc-contract.md:122-129` puts "whether the same rule has
# been restated in different words somewhere else" among the REVIEWER's
# obligations, "deliberately not by machine, because a machine test ... would
# prescribe the shape of the fix". So the general property stays a review
# obligation and this pin asserts strictly less than the change guarantees: the
# resident-schema rules survive, the route out is a path, and the one
# plugin-owned FACILITY NAME the resident document has no business carrying is
# gone. A facility name is an exact token, not a matter of degree.
DEDUPLICATED_SECTION = "Extension points"
RESIDENT_DOC = "architecture/triggers.md"
PLUGIN_DOC = "architecture/plugin-triggers.md"

# The resident-schema rules, which are properly `triggers.md`'s. Asserted so
# that a de-duplication cannot quietly delete corpus content along with the
# duplication — the risk in the opposite direction from restatement.
RESIDENT_SCHEMA_PASSAGES = [
    "v2 forbids a webhook `path` (the wildcard route provides it), while legacy "
    "v1 required one",
    "a scheduled trigger takes exactly one of an inline prompt or a prompt file",
]

TRIGGER_CONSENT_MODULE = "casa/rootfs/opt/casa/trigger_consent.py"
TRIGGER_CONSENT_OWNER = PLUGIN_DOC


def _normalized(text):
    """`text` with every run of whitespace collapsed to one space.

    Required, not cosmetic: the documents wrap at different columns, so a raw
    substring comparison finds a passage in one and MISSES it in the other. A
    pin without this passes on the pre-fix tree for the wrong reason.
    """
    return re.sub(r"\s+", " ", text).strip()


def _section(doc, heading):
    """`doc`'s `## <heading>` section, up to the next `## ` heading."""
    text = (DOCS / doc).read_text()
    match = re.search(r"\n## " + re.escape(heading) + r"\n(.*?)(?=\n## )", text, re.S)
    assert match, (doc, heading)
    return match.group(1)


def test_the_resident_trigger_document_routes_plugin_consent_away():
    """#668: `architecture/triggers.md` carried the `consent_reprompt` re-issue
    rule its own Scope assigns to `architecture/plugin-triggers.md`, and its
    Extension points offered no route there at all.

    Identical wording in two documents is the shape that silently diverges: the
    owning document is updated, the copy is not, and a reader routed to the
    resident document reads stale plugin requirements. `consent_reprompt` is
    the plugin-consent facility; naming it is precisely what this document's
    Scope disclaims.

    The resident-schema rules are asserted here on purpose, in the opposite
    direction: a de-duplication that deleted the whole block would lose corpus
    content that belongs to this document, and nothing else would notice.

    Red case demonstrated at f414c4c6: `architecture/triggers.md` contains one
    `consent_reprompt` occurrence, and its Extension points section contains no
    `](plugin-triggers.md)` link — the base tree links that path from Scope
    only, so a whole-document check would pass while the section a reader
    arrived at offered nothing.
    """
    assert "consent_reprompt" not in (DOCS / RESIDENT_DOC).read_text()
    assert "](%s)" % Path(PLUGIN_DOC).name in _section(RESIDENT_DOC, DEDUPLICATED_SECTION)

    documents = _documents()
    normalized = {doc: _normalized((DOCS / doc).read_text()) for doc in documents}
    for passage in RESIDENT_SCHEMA_PASSAGES:
        owners = [doc for doc in documents if normalized[doc].count(passage)]
        total = sum(normalized[doc].count(passage) for doc in documents)
        assert (owners, total) == ([RESIDENT_DOC], 1), passage


def test_trigger_consent_ownership_agrees_with_plugin_triggers():
    """#668: the ledger, the manifest `covers` map and the routing table name
    the SAME document for a plugin-declared module.

    `trigger_consent.py`'s own docstring says "Operator-consent DM prompts for
    plugin-declared webhook triggers … A plugin trigger routes ONLY after the
    operator taps Approve", and `docs/README.md`'s routing table sends "trigger
    consent" to `architecture/plugin-triggers.md`, while the coverage ledger
    assigned the module to `architecture/triggers.md`. A reader arriving by the
    ledger landed on a document describing none of it.

    The expected owner is the LITERAL path, deliberately not derived from
    either map under test: an oracle that reads the ledger and then compares
    the manifest to it passes when BOTH maps are moved to the wrong document
    together — and `coverage_ledger.check` passes then too, since the ledger's
    owner is among the claimants. The `when_changing` assertion is the third,
    independent anchor: it is the phrase the README routing table renders.

    Red case demonstrated at f414c4c6: the ledger owner is
    `architecture/triggers.md`, and NO manifest entry covers the module at all,
    so the claimant list is empty.
    """
    assert _ledger_owner(TRIGGER_CONSENT_MODULE) == TRIGGER_CONSENT_OWNER
    claimants = [e["doc"] for e in _entries()
                 if TRIGGER_CONSENT_MODULE in (e.get("covers") or [])]
    assert claimants == [TRIGGER_CONSENT_OWNER]
    routed = [e["doc"] for e in _entries()
              if "trigger consent" in (e.get("when_changing") or "").lower()]
    assert routed == [TRIGGER_CONSENT_OWNER]


# #687. The reload/launch explanation added to `architecture/engagements.md` and
# `architecture/turn-loop.md` answers an investigation: why an `in_casa`
# engagement turn appeared to end seconds after a mid-task reload. Three of its
# claims are the ones a future edit could drop or reverse while the paragraph
# still reads well, and each is the load-bearing half of the answer — that the
# launch turn is HOSTED inside the launching resident's own tool call; that the
# pool drain timeout is a caller-overridable DEFAULT rather than a fixed number;
# and that the benign completion is eliminated FIRST before an ended turn is
# attributed to a reload at all.
#
# WHY EXACT CLAUSES rather than a wording-tolerant matcher. The tolerant shape
# was built first — bounded-gap token-order regexes over the normalized
# paragraph — and two independent reviewers each MEASURED it accepting the
# negation of all three claims ("not inline … merely observes it"; "a fixed
# internal 120-second default the caller cannot override"; "do not eliminate the
# benign case first") because the vocabulary and its order survive a polarity
# reversal, and because the whole explanation is one 2,252-character paragraph
# in which every token is in reach of every other. A pin that stays green
# through a reversal is not pinning the claim. So this uses the mechanism the
# file already has for the same job (`INV_TOOL_005_PASSAGES`,
# `RESIDENT_SCHEMA_PASSAGES`): an exact passage, whitespace-normalized so that
# reflow is free and wording is not, case kept because case and connectives are
# part of what was reviewed.
#
# Each clause begins at its own grammatical SUBJECT, not at its verb. That is a
# rule, not a preference: a clause starting at the verb can be negated from
# outside the pinned span — "each entry's lock NEVER is awaited up to a drain
# timeout — a default the caller may override" leaves a verb-anchored literal
# matching once, which a reviewer reproduced.
#
# The trade this makes, stated rather than hidden: a copy-edit that PRESERVES a
# claim while rewording it fails this test. That is intended — the author then
# updates the literal in the same diff, which puts the change in front of a
# reviewer — and it is the opposite failure from the silent one above.
RELOAD_LAUNCH_CLAIMS = [
    (
        "launch-turn-hosted-in-the-launchers-tool-call",
        "architecture/engagements.md",
        "`engage_executor` awaits the driver's `start()` inline, so the whole "
        "first turn runs inside the launching resident's own tool call",
    ),
    (
        "drain-timeout-is-a-caller-overridable-default",
        "architecture/turn-loop.md",
        "each entry's lock is awaited up to a drain timeout — a default the "
        "caller may override",
    ),
    (
        "benign-case-eliminated-before-blaming-a-reload",
        "architecture/engagements.md",
        "Before attributing an ended turn to a reload at all, eliminate the "
        "benign case first",
    ),
]


def _text_corpus():
    """Every manifested TEXT path: `document`, `index` and `generated`.

    Deliberately wider than `_documents()`. A uniqueness count taken over
    `kind: document` alone is evadable by copying a claim into `README.md`,
    which the manifest carries as `kind: index` — reproduced by a reviewer, who
    measured the duplicate leaving the document-only count at exactly one. The
    `meta` entries are the manifest and coverage YAML themselves, which assert
    no corpus content and are excluded for that reason.
    """
    return [e["doc"] for e in _entries() if e.get("kind", "document") != "meta"]


def test_reload_launch_explanation_keeps_its_reviewed_claims():
    """#687: three reviewed claims of the reload/launch explanation are each
    present exactly once across the manifested text corpus, in the document
    that must carry it.

    What this pins is DECLARED and narrow: presence, uniqueness and ownership
    of three exact affirmative clauses. It does not claim the paragraph is
    true, complete, or that no other document restates it in different words —
    `docs/contributing/doc-contract.md` assigns "whether any of it is true" and
    the equivalent-restatement question to the reviewer on purpose, "because a
    machine test ... would prescribe the shape of the fix".

    Three failures are caught at once by the counted pair: the claim deleted or
    reworded (total 0), the claim copied elsewhere (total 2), and the claim
    moved off the document a reader is routed to (the wrong owner).

    Red case demonstrated at 51e8a64f, the pre-fix parent: none of the three
    clauses exists anywhere in the corpus, so every marker counts 0 and each
    assertion fails on its own.

    Mutation-checked separately, each with the other two left intact: a
    polarity reversal of each clause fails that marker alone; copying one
    clause into another manifested document fails that marker alone on the
    total; moving one clause fails that marker alone on the owner.
    """
    corpus = _text_corpus()
    normalized = {doc: _normalized((DOCS / doc).read_text()) for doc in corpus}

    for marker, expected_owner, claim in RELOAD_LAUNCH_CLAIMS:
        owners = [doc for doc in corpus if normalized[doc].count(claim)]
        total = sum(normalized[doc].count(claim) for doc in corpus)
        assert (owners, total) == ([expected_owner], 1), (marker, owners, total)
