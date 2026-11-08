"""Pinning tests for the corpus's own contract invariants (docs corpus).

These pin observable properties of the tracked docs/ tree itself: the parts of
the doctrine and doc-contract rules a test can check mechanically. Each test
names the invariant it pins and records the demonstrated red case.
"""
import posixpath
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


# #947: the setup-dispatch gate (INV-PLUG-011, INV-PLUG-016) is declared apart
# from the setup obligation (INV-PLUG-010), and four prose passages in other
# documents cite the gate by a markdown PATH link. Nothing resolves a prose link
# (#761), so a split that moves the gate but leaves one of these links on the
# obligation's document passes the verifier, the navigation check and the
# coverage ledger. Each passage is located by text that excludes its link; the
# link beside it is resolved relative to the citing document and compared with
# the declaring document the manifest names — never a hard-coded owner path.
SETUP_GATE_INVARIANTS = ("INV-PLUG-011", "INV-PLUG-016")

# (citing document, link-independent locator, where the link sits, invariant cited)
SETUP_GATE_CITATIONS = [
    ("architecture/callbacks.md", "reads the marker pair", "before", "INV-PLUG-011"),
    ("architecture/triggers.md", "its route check (", "after", "INV-PLUG-016"),
    ("architecture/plugin-triggers.md", "(INV-PLUG-011 in ", "after", "INV-PLUG-011"),
    ("architecture/plugin-triggers.md",
     "while either stands or while any publication has landed since (", "after",
     "INV-PLUG-016"),
]

_INLINE_LINK = r"\[[^\]]*\]\(([^)\s]+)\)"
_SOURCEMAP_BLOCK = re.compile(r"<!-- BEGIN SOURCEMAP -->.*?<!-- END SOURCEMAP -->", re.S)


def test_setup_dispatch_gate_ownership_and_inbound_links():
    """#947: the gate's invariants have their own declaring document, and every
    inbound path citation of the gate arrives at the document that declares
    what it cites.

    Red case demonstrated: at d2e9b4cb one document declares INV-PLUG-010,
    -011 and -016, so the separated-owner count is 0, not 2 (the only arm red
    at the base). The four link arms are regression arms, green at the base:
    after the split, reverting any ONE of the four re-pointings to
    `plugin-setup.md` turns exactly that position of `matched` to 0.
    """
    obligation_owner = _declaring_document("INV-PLUG-010")
    separated = sum(_declaring_document(inv) != obligation_owner
                    for inv in SETUP_GATE_INVARIANTS)
    assert separated == 2, separated

    matched = []
    for doc, locator, position, inv in SETUP_GATE_CITATIONS:
        body = _normalized(_SOURCEMAP_BLOCK.sub("", (DOCS / doc).read_text()))
        assert body.count(locator) == 1, (doc, locator, body.count(locator))
        at = body.index(locator)
        if position == "before":
            links = re.findall(_INLINE_LINK + r" $", body[:at])
        else:
            links = re.findall("^" + _INLINE_LINK, body[at + len(locator):])
        assert len(links) == 1, (doc, locator, len(links))
        target = posixpath.normpath(posixpath.join(posixpath.dirname(doc), links[0]))
        matched.append(int(target == _declaring_document(inv)))
    assert matched == [1, 1, 1, 1], matched


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
    "a scheduled trigger takes exactly one of an inline prompt, a prompt file or a plugin job",
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


# #687. The reload/launch explanation added to `architecture/engagements.md` (moved
# with INV-ENG-011 to `architecture/engagement-turn-outcomes.md` by #1151) and
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
        # v0.314.0 (INV-ENG-021): the clause this marker pinned — that the
        # whole first turn ran inside the launching resident's own tool call
        # — described the defect the detach removed; the reviewed claim is
        # now the opposite, and it is owned by the same document.
        "launch-turn-runs-in-an-anchored-owner-after-pending",
        "architecture/engagement-turn-outcomes.md",
        "The launch turn runs in an owner Casa anchors, on the engagement's "
        "own client, after the launching tool call has already answered "
        "`pending`",
    ),
    (
        "drain-timeout-is-a-caller-overridable-default",
        "architecture/sdk-client-pool.md",
        "each entry's lock is awaited up to a drain timeout — a default the "
        "caller may override",
    ),
    (
        "benign-case-eliminated-before-blaming-a-reload",
        "architecture/engagement-turn-outcomes.md",
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


def test_mutation_envelope_and_committed_removal_disclosure_are_separate():
    """#1113: keep the mutation envelope's three invariants together,
    separate from committed-removal disclosure and its failure paragraphs.

    DECLARED ownership partition: the split establishes this property.
    This pins ownership and paragraph location, identified by lead phrases,
    only—not prose correctness, one-hop sufficiency, or byte-identical
    payload movement; those remain reviewer obligations.

    The seam is the one the document names itself at c31c1e70:
    ``docs/architecture/plugin-mutation-tools.md:11-14`` lists "what a
    committed removal discloses about what it leaves behind" as its third
    subject beside the commit-then-converge ordering and what an envelope
    may claim; ``docs/architecture/tools-interface.md:16-19`` names the
    same three clauses, ``docs/architecture/plugin-erasure.md:17-18``
    already assigns the removal disclosure to INV-TOOL-007, and the row's
    ``when_changing`` (``docs/manifest.d/architecture-plugin-m-r.yaml:6``)
    routes "the removal disclosure" as its own clause while
    ``defines_invariants`` (``:15``) lists all four ids on one document.
    INV-DOC-007 (``docs/contributing/doc-contract.md:75``) owes the split.

    At c31c1e70 all four invariants resolve to one document: the ownership
    counts are (1, 1), not (1, 2). Both paragraph checks independently yield
    (1, 1) at base and are regression arms.

    Specified by **astra** in the drive red-case round.
    """
    envelope = {
        _declaring_document(inv)
        for inv in ("INV-TOOL-003", "INV-TOOL-004", "INV-TOOL-005")
    }
    removal = _declaring_document("INV-TOOL-007")
    counts = (len(envelope), len(envelope | {removal}))
    assert counts == (1, 2), (counts, envelope, removal)

    texts = {
        doc: _normalized((DOCS / doc).read_text())
        for doc in _documents()
    }
    for lead in (
        "**A removal path raises after the commit.**",
        "**A removal's caller is cancelled after the commit.**",
    ):
        total = sum(text.count(lead) for text in texts.values())
        owned = texts[removal].count(lead)
        assert (total, owned) == (1, 1), (lead, total, owned)


def test_memory_labelling_and_retention_lifecycle_are_separate():
    """#1129: separate how an item is labelled on the way in to long-term
    memory from the retention lifecycle.

    The seam is the one the document names itself at 790629e8:
    ``docs/architecture/memory-lifecycle.md:11-15`` lists "how each item is
    labelled on the way in (its sensitivity tier, its speaker provenance,
    and the content addressing that deduplicates it)" as its own subject;
    the row's ``summary`` and ``when_changing``
    (``docs/manifest.d/architecture-f-m.yaml:207-208``) route it as its own
    clause while ``defines_invariants`` (``:238``) lists all nine ids on one
    document. INV-DOC-007 (``docs/contributing/doc-contract.md:75``) owes
    the split.

    At 790629e81d12b2cad0cefa449f806c83a292a3dd, ownership counts
    are (1, 1, 1), not (1, 1, 2). All six payload-location checks
    independently yield (1, 1) at base and are regression arms.

    Pins ownership and lead location; byte-identical movement and
    completeness of the passages remain review obligations. The write-trust
    and completeness rules (INV-MEM-005, INV-MEM-016) later moved on to
    ``architecture/memory-capture.md`` with the rest of capture eligibility,
    so the lifecycle set names the ids that stayed with the lifecycle; and the
    retain-path ids (the content addressing, the tag gate, the tier floor, the
    save guard and the background reset) were retired with the retain path,
    so each set names its surviving ids and the labelling leads are today's.

    Specified by **astra** in the drive red-case round.
    """
    labelling = {
        _declaring_document(inv)
        for inv in (
            "INV-MEM-012", "INV-MEM-022", "INV-MEM-026",
        )
    }
    lifecycle = {
        _declaring_document(inv)
        for inv in (
            "INV-MEM-013", "INV-MEM-021", "INV-MEM-031",
        )
    }
    counts = (
        len(labelling),
        len(lifecycle),
        len(labelling | lifecycle),
    )
    assert counts == (1, 1, 2), (counts, labelling, lifecycle)

    owner = _declaring_document("INV-MEM-012")
    texts = {
        doc: _normalized((DOCS / doc).read_text())
        for doc in _text_corpus()
    }
    for lead in (
        "**A captured item is named by what occurred, never by what it "
        "says.**",
        "**A captured item starts `private` and provisional, with a "
        "floor.**",
        "**The classifier is given the item as data.**",
        "**Tier classification fails.**",
    ):
        total = sum(text.count(lead) for text in texts.values())
        owned = texts[owner].count(lead)
        assert (total, owned) == (1, 1), (lead, total, owned)


def test_turn_conduct_and_warm_client_life_have_distinct_owners():
    """#899: the conduct of one turn and the life of the warm client that turn
    runs on are two subjects, so INV-TURN-004 and INV-TURN-001 are declared by
    two different documents.

    DECLARED under D34 rather than pinned from prior support: the base tree
    does not have this property — one document declared all eleven INV-TURN
    ids — and that is the defect the split repairs. Its evidence is what the
    two ids say at ``f291d1d9``. ``docs/architecture/turn-loop.md:37``
    declares INV-TURN-001: the resume-versus-fresh decision is re-derived
    under the POOL ENTRY'S LOCK and a cached client is reused only on an exact
    session-id match — a statement about a client that outlives the turn.
    ``:74`` declares INV-TURN-004: a memory read that raises does not fail the
    turn, which proceeds with an empty memory block — a statement about what
    one turn assembles and how far it gets. Two subjects, two declaring
    documents.

    Scope, stated so the declaration claims no more than the change
    guarantees: this pins the OWNERSHIP SPLIT and nothing else. It does not
    claim either document's prose is correct, one-hop sufficient, or that the
    payload moved byte-identically — ``docs/contributing/doc-contract.md``
    assigns each of those to a reviewer on purpose, "because a machine test
    for 'a split happened' would prescribe the shape of the fix". Both owners
    are resolved through the MANIFEST, never a hard-coded path, so this
    follows either invariant if it is ever rehoused — unlike
    ``RELOAD_LAUNCH_CLAIMS`` above, whose hard-coded owner literal this same
    change had to update by hand.

    Red case demonstrated at f291d1d9, the pre-split base: both ids resolve to
    ``architecture/turn-loop.md`` — they are the first and fourth entries of
    one row's ``defines_invariants`` — so the two owners are equal and this
    fails. Not an import or missing-path failure: the new document is never
    named here.

    Mutation-checked: returning INV-TURN-001 to the turn-loop row, and
    separately moving INV-TURN-004 to the pool row, each fails this test
    alone.

    Specified by **astra** in the drive red-case round; accepted by **terra**.
    """
    assert (_declaring_document("INV-TURN-004")
            != _declaring_document("INV-TURN-001"))


# #903: the three responses the bridge returns when casa-main's internal socket
# is unreachable. Each entry is ONE complete response description — the answer,
# its transport status and its exact payload — not a mention of a concept, so a
# document that names the condition without saying what a caller gets does not
# satisfy it.
SOCKET_UNREACHABLE_CLAIMS = (
    "When casa-main's internal socket is unreachable, `tools/call` returns "
    "JSON-RPC error `-32000` with message "
    "`casa_temporarily_unavailable: casa-main internal socket unreachable`.",

    "When casa-main's internal socket is unreachable, `POST /hooks/resolve` "
    "returns HTTP 200 with "
    '`hookSpecificOutput.hookEventName: "PreToolUse"`, '
    '`permissionDecision: "deny"`, and '
    '`permissionDecisionReason: "casa_temporarily_unavailable: casa-main '
    'internal socket unreachable"` — the tool-call face\'s message verbatim '
    "(#880).",

    "When casa-main's internal socket is unreachable, registered "
    "`POST /internal/channel/*` routes return HTTP 503 with body "
    '`{"ok": false, "error": "casa_temporarily_unavailable"}`.',
)


# #908: the two remaining conditions under which the bridge's hook-resolution
# route answers `deny`. #903 covered the third (the internal socket being
# unreachable) and nothing else — every bullet it published is prefixed "When
# casa-main's internal socket is unreachable" — so a reader of the owning
# document met one trigger and could reasonably read it as the only one. As
# above, each entry is ONE complete response description: the trigger, the
# transport status and the exact payload.
OTHER_HOOK_REFUSAL_CLAIMS = (
    'When the bridge cannot parse the request body as JSON, '
    '`POST /hooks/resolve` returns HTTP 200 with '
    '`hookSpecificOutput.hookEventName: "PreToolUse"`, '
    '`permissionDecision: "deny"`, and '
    '`permissionDecisionReason: "svc_casa_mcp /hooks/resolve: malformed JSON"`.',

    'When hook forwarding raises `aiohttp.ClientError` other than '
    '`aiohttp.ClientConnectorError`, or raises `asyncio.TimeoutError`, '
    '`POST /hooks/resolve` returns HTTP 200 with '
    '`hookSpecificOutput.hookEventName: "PreToolUse"`, '
    '`permissionDecision: "deny"`, and '
    '`permissionDecisionReason: "Permission relay failed: forwarder error '
    'talking to casa-main ({type(exc).__name__}: {exc or \'no detail\'}). '
    'The tool was not run."`, with the reason interpolated from the caught `exc`.',

    # #912: the arm that keeps a bridge DEFECT from becoming a permission
    # grant. Its diagnostic half is part of the description on purpose — an
    # ERROR-with-traceback is what distinguishes it from the two WARN arms
    # above, and a reader who cannot make that distinction cannot tell a
    # programming error from a transport failure.
    'When hook forwarding raises any other exception, '
    '`POST /hooks/resolve` returns HTTP 200 with '
    '`hookSpecificOutput.hookEventName: "PreToolUse"`, '
    '`permissionDecision: "deny"`, and '
    '`permissionDecisionReason: "Permission relay failed: unexpected bridge '
    'error ({type(exc).__name__}). The tool was not run. Check addon logs."`, '
    'and the exception is logged at ERROR with its traceback.',
)


def test_turn_limit_stop_and_turn_conduct_are_separate():
    """Separate what happens when a resident turn stops at its turn limit
    from the rest of the turn's conduct.

    The seam is the one the document names itself at 3f6f752b:
    ``docs/architecture/turn-loop.md:11-12`` lists "what bounds the turn" as
    its own clause beside what is assembled before the model is called and
    how the call is retried; the row's ``summary``
    (``docs/manifest.d/architecture-t-z.yaml:124``, "and what bounds it")
    routes it as its own clause while ``defines_invariants`` (``:138``)
    lists INV-TURN-014 beside the seven conduct ids on one document. #1143
    took the document past the 25 KB ceiling with that one invariant's
    block, and INV-DOC-007 (``docs/contributing/doc-contract.md:75``) owes
    the split.

    At 3f6f752b121bb72fa283843ab6b4465b0e53f54e, ownership counts are
    (1, 1, 1), not (1, 1, 2). All five payload-location checks independently
    yield (1, 1) at base and are regression arms.

    Pins ownership and lead location; byte-identical movement and
    completeness of the passages remain review obligations.
    """
    limits = {_declaring_document("INV-TURN-014")}
    conduct = {
        _declaring_document(inv)
        for inv in (
            "INV-TURN-004", "INV-TURN-005", "INV-TURN-007", "INV-TURN-008",
            "INV-TURN-009", "INV-TURN-012", "INV-TURN-013",
        )
    }
    counts = (len(limits), len(conduct), len(limits | conduct))
    assert counts == (1, 1, 2), (counts, limits, conduct)

    owner = _declaring_document("INV-TURN-014")
    texts = {
        doc: _normalized((DOCS / doc).read_text())
        for doc in _text_corpus()
    }
    for lead in (
        "The CLI ends a turn that would need one model call more than its "
        "limit",
        "The fact is recorded in the result-message arm both attempt paths "
        "share",
        "Where the line goes is decided by the server-stamped origin route",
        "The assistant's limit is `tools.max_turns` in its `runtime.yaml`, 80",
        "What it does not cover: a delegated or job turn's limit",
    ):
        total = sum(text.count(lead) for text in texts.values())
        owned = texts[owner].count(lead)
        assert (total, owned) == (1, 1), (lead, total, owned)


def test_output_scope_properties_and_admission_are_separate():
    """#1235: separate the turn scope's own properties — whether a turn
    streams, whether its final reply is closing silence, where an untrusted
    webhook turn's output goes — from the admission-and-disclosure envelope.

    The seam is the one the corpus names itself at 9636f81d:
    ``docs/architecture/output-boundary.md:11-15`` lists "the scope every
    dispatched turn is minted with" beside the admission of model text, the
    read-before-describe obligation and a stored payload's note;
    INV-OUT-006 (``:188``) declares the three decisions "properties of its
    scope"; the row's ``summary``/``when_changing``
    (``docs/manifest.d/architecture-n-personality.yaml:5-6``) route "the turn
    scope" as its own clause while ``defines_invariants`` (``:29``) lists
    INV-OUT-001..006 on one document. The document is past the 25 KB ceiling
    at that base and INV-DOC-007 (``docs/contributing/doc-contract.md:75``)
    owes the split.

    At 9636f81d all six resolve to one document: the ownership counts are
    (1, 1), not (1, 2). The six payload-location checks independently yield
    (1, 1) at base and are regression arms.

    Pins ownership and lead location; byte-identical movement and
    completeness of the passages remain review obligations.

    Specified by **astra** in the drive red-case round.
    """
    retained = {
        _declaring_document(f"INV-OUT-{i:03d}") for i in range(1, 6)
    }
    moved = _declaring_document("INV-OUT-006")
    counts = (len(retained), len(retained | {moved}))
    assert counts == (1, 2), (counts, retained, moved)

    texts = {
        doc: _normalized((DOCS / doc).read_text())
        for doc in _text_corpus()
    }
    for lead in (
        "**INV-OUT-006**: Whether a turn streams, whether its final reply is "
        "closing silence,",
        "The three used to be inline checks",
        "The delivered webhook reply (#1142) is "
        "`TurnScope.delivers_to_operator`,",
        "What it does not cover: `send_media` still requires a Telegram origin "
        "of its own,",
        "The exception is the operator's ruling on #1075, for the turns that do "
        "not stream.",
        "Two facts ride out of admission for the durable-announcement discharge "
        "(#1079,",
    ):
        total = sum(text.count(lead) for text in texts.values())
        owned = texts[moved].count(lead)
        assert (total, owned) == (1, 1), (lead, total, owned)


def test_turn_outcomes_row_moves_to_launch_shard():
    """#1288: the split separates the launch-and-telling turn-outcomes row
    from the E manifest shard.

    The seam is the one the corpus names itself at 814ce23c:
    ``docs/manifest.yaml:15-17`` declares ``architecture-e-launch.yaml`` the
    shard of "the engagement launch and telling documents (split from E at
    the 40 KB index ceiling)", and the turn-outcomes row
    (``docs/manifest.d/architecture-e.yaml:183-185``) is about the launch
    turn's death report and the follow-up turn's notice. The E shard is past
    its index ceiling at that base, and INV-DOC-007
    (``docs/contributing/doc-contract.md:75``) owes the split.

    Red case at 814ce23c: the row's only shard is
    ``manifest.d/architecture-e.yaml``, so the ``sources`` assertion fails.
    The row moves unchanged: its parsed content is pinned by digest to its
    base value, and no later change of the split edits it. The eval-framework
    row stays in the E shard (membership only, no count over the shard).
    v0.347.11 binds the executor closing-text tests to INV-ENG-011 in this row,
    and the claude_code driver's removal then drops its boot-replay and
    claude_code anchors and bindings from it, and v0.351.0
    drops the executor closing-text tests again, so the digest is the row as
    it stands after those changes.

    Specified by **astra** in the drive red-case round.
    """
    import hashlib
    import json

    target = "architecture/engagement-turn-outcomes.md"
    rows = [entry for entry in _entries() if entry["doc"] == target]
    assert len(rows) == 1, len(rows)
    sources = [
        source.relative_to(DOCS).as_posix()
        for source in sorted((DOCS / "manifest.d").glob("*.yaml"))
        if any(entry["doc"] == target
               for entry in yaml.safe_load(source.read_text()))
    ]
    assert sources == ["manifest.d/architecture-e-launch.yaml"], sources
    digest = hashlib.sha256(
        json.dumps(rows[0], sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert digest == (
        "01dfeddde37311e7f9f9ae8a2aa4e66bc3"
        "6fe0a0cf18252b28cfbaaf9fc88784"
    ), digest
    retained = yaml.safe_load(
        (DOCS / "manifest.d/architecture-e.yaml").read_text()
    )
    assert any(entry["doc"] == "architecture/eval-framework.md"
               for entry in retained)


def test_transcript_reaping_and_durable_finalization_are_separate():
    """#1195: the split separates the transcript reaper from how a durable
    engagement ends.

    The seam is the one the document names itself at 814ce23c:
    ``docs/architecture/engagement-finalization.md:11-15`` joins how a
    durable engagement ends to "where a finished `in_casa` engagement's CLI
    transcripts are deleted"; the row's ``when_changing``
    (``docs/manifest.d/architecture-e.yaml:131-133``) ends "or deleting a
    finished engagement's transcripts", while ``defines_invariants``
    (``:154``) lists the reaper's invariant (INV-ENG-027 today) beside
    INV-ENG-001/002/005/010 on one
    document. The document is past the 25 KB ceiling at that base and
    INV-DOC-007 (``docs/contributing/doc-contract.md:75``) owes the split.

    At 814ce23c all five ids resolve to one document: the ownership counts
    are (1, 1), not (1, 2). The ledger arm and the five payload-location
    checks independently hold at base and are regression arms.

    Pins ownership and lead location; byte-identical movement and
    completeness of the passages remain review obligations.

    Specified by **astra** in the drive red-case round.
    """
    moved = _declaring_document("INV-ENG-027")
    retained = {_declaring_document(inv)
                for inv in ("INV-ENG-001", "INV-ENG-002", "INV-ENG-010")}
    counts = (len(retained), len(retained | {moved}))
    assert counts == (1, 2), (counts, retained, moved)
    assert _ledger_owner(
        "casa/rootfs/opt/casa/engagement_transcript_reaper.py"
    ) == moved
    texts = {doc: _normalized((DOCS / doc).read_text())
             for doc in _text_corpus()}
    for lead in (
        "Casa owns transcript deletion:",
        "The condition is the status",
        "The folder lookup is the SDK's own private helper",
        "What it does not cover, and these are gaps",
        "A transcript removal fails.",
    ):
        total = sum(text.count(lead) for text in texts.values())
        owned = texts[moved].count(lead)
        assert (total, owned) == (1, 1), (lead, total, owned)


PLUGIN_SHARD_RANGES = {
    "architecture/plugin-authorization.md": "manifest.d/architecture-plugin-a-d.yaml",
    "architecture/plugin-delivered-files.md": "manifest.d/architecture-plugin-a-d.yaml",
    "architecture/plugin-delivered-slots.md": "manifest.d/architecture-plugin-a-d.yaml",
    "architecture/plugin-access-profiles.md": "manifest.d/architecture-plugin-a-d.yaml",
    "architecture/plugin-erasure.md": "manifest.d/architecture-plugin-e-l.yaml",
    "architecture/plugin-events.md": "manifest.d/architecture-plugin-e-l.yaml",
    "architecture/plugin-handoff.md": "manifest.d/architecture-plugin-e-l.yaml",
    "architecture/plugin-health.md": "manifest.d/architecture-plugin-e-l.yaml",
}

# Parsed rows at 0fca2ec9, sha256 of json.dumps(row, sort_keys=True). The
# plugin-erasure row is absent on purpose: the erase-episode split (#1270)
# edits it in the same change. v0.347.3: the plugin-delivered-slots row gains
# one `covers` source (result_broker.compose_operator_link_button, the link's
# button form) and nothing else; its digest is that row's.
# The plugin-delivered-files and plugin-handoff rows were re-pinned when the
# claude_code driver's removal dropped their private-outbox and dropped-uid
# bindings and prose.
# The plugin-health row was re-pinned when v0.351.0 moved the
# install hold from the engagement to the configurator lease (the plugin
# invariant that held it was retired for INV-CFG-018).
PLUGIN_SHARD_ROW_DIGESTS = {
    "architecture/plugin-authorization.md":
        "3d428f89a763a6365d072808fb7564576b964ba30cc6b1e77871eec9a45ff4e6",
    "architecture/plugin-delivered-files.md":
        "06be628ad8c9cd6134135f26b8eb4c8e5934a528e1946133b419997878f9328e",
    "architecture/plugin-delivered-slots.md":
        "a5e5e2693943fc701b9e5109f94ac5cc89317e11beb9f87cfe1283086df40975",
    "architecture/plugin-events.md":
        "30ee8209c4833435cbb1d8f2243963f3cbe21716465c6ebd3689a975a93006e2",
    "architecture/plugin-access-profiles.md":
        "ca1c473801e423b91cffc4744b02bd49ce7a0d78332e13a9b6670e703e85133e",
    "architecture/plugin-handoff.md":
        "83914c68bf237085ef09f7ff8937b9bbf9dac214c3a769b40e7308850975a4f2",
    "architecture/plugin-health.md":
        "be13868187b5bc55611f63b46b79ec7742d3acd00037276b1bc82a806a63c4f4",
}


def test_plugin_manifest_rows_follow_their_shard_ranges():
    """#1322: the split separates the plugin rows by the range each shard
    label names.

    The rule is the one the corpus states at 0fca2ec9:
    ``scripts/verify_docs.py:1300-1309`` says a label names the range the
    shard owns and the path follows the label, since a retained range string
    "would put a false range claim on whichever shard kept it";
    ``docs/manifest.yaml:35-37`` allowlists ``architecture-plugin-a-l.yaml``
    as the shard of plugin-a through plugin-l; ``doc-contract.md:162-166``
    gives every shard file its own ``kind: meta`` entry. The shard is past
    its index ceiling at that base, and INV-DOC-007
    (``docs/contributing/doc-contract.md:75``) owes the split.

    Red case at 0fca2ec9: every one of the eight rows sits in
    ``manifest.d/architecture-plugin-a-l.yaml``, so the first ``sources``
    assertion fails. Seven rows move unchanged and are pinned by digest to
    their base value; the plugin-erasure row is not, because the
    erase-episode split edits it. No count over either successor shard.

    Specified by **astra** in the drive red-case round.
    """
    import hashlib
    import json

    entries = _entries()
    shards = {
        source.relative_to(DOCS).as_posix(): yaml.safe_load(source.read_text())
        for source in sorted((DOCS / "manifest.d").glob("*.yaml"))
    }
    for doc, expected in PLUGIN_SHARD_RANGES.items():
        sources = [
            path for path, rows in shards.items()
            if any(row["doc"] == doc for row in rows)
        ]
        assert sources == [expected], (doc, sources)
        rows = [row for row in entries if row["doc"] == doc]
        assert len(rows) == 1, (doc, len(rows))
        if doc in PLUGIN_SHARD_ROW_DIGESTS:
            digest = hashlib.sha256(
                json.dumps(rows[0], sort_keys=True).encode("utf-8")
            ).hexdigest()
            assert digest == PLUGIN_SHARD_ROW_DIGESTS[doc], (doc, digest)
    old = "manifest.d/architecture-plugin-a-l.yaml"
    assert not (DOCS / old).exists(), old
    assert sum(row["doc"] == old for row in entries) == 0
    for suffix in ("a-d", "e-l"):
        path = f"manifest.d/architecture-plugin-{suffix}.yaml"
        kinds = [row.get("kind") for row in entries if row["doc"] == path]
        assert kinds == ["meta"], (path, kinds)


def test_erase_episode_and_uninstall_flow_are_separate():
    """#1270: the split separates the erase episode from the uninstall flow
    and the plugin-author contract.

    The seam is the one the document names itself at 0fca2ec9:
    ``docs/architecture/plugin-erasure.md:11-16`` lists "the erase episode
    that runs the chosen eraser" as its own subject beside the author
    contract and result convention, the question, the finishing call and the
    env clearing; the row's ``when_changing``
    (``docs/manifest.d/architecture-plugin-a-l.yaml:74``) routes "the erase
    episode, the plugin_erase marker" as its own clause, while
    ``defines_invariants`` (``:99``) lists INV-PLUG-038/039/042 beside
    034/035/036/037/040/041 on one document. The document is past the 25 KB
    ceiling at that base and INV-DOC-007
    (``docs/contributing/doc-contract.md:75``) owes the split.

    At 0fca2ec9 all nine ids resolve to one document: the ownership counts
    are (1, 1, 1), not (1, 1, 2). The ledger arm (both modules stay with the
    flow, which keeps the result convention ``plugin_store.py`` cites) and
    the seven payload-location checks independently hold at base and are
    regression arms.

    Pins ownership and lead location; byte-identical movement and
    completeness of the passages remain review obligations.

    Specified by **astra** in the drive red-case round.
    """
    episode = {
        _declaring_document(f"INV-PLUG-{i:03d}")
        for i in (38, 39, 42)
    }
    flow = {
        _declaring_document(f"INV-PLUG-{i:03d}")
        for i in (34, 35, 36, 37, 40, 41)
    }
    counts = (len(episode), len(flow), len(episode | flow))
    assert counts == (1, 1, 2), (counts, episode, flow)
    owner = _declaring_document("INV-PLUG-038")
    for module in ("plugin_erasure.py", "plugin_erase_consent.py"):
        assert _ledger_owner(f"casa/rootfs/opt/casa/{module}") == (
            _declaring_document("INV-PLUG-034")
        ), module
    texts = {doc: _normalized((DOCS / doc).read_text())
             for doc in _text_corpus()}
    for lead in (
        "The erase episode runs in the background.",
        "The eraser reports after the wait.",
        "A new kind of Casa-dispatched plugin turn** follows the marker "
        "table rule",
        "Each plugin's outcome is recorded as an erasure record",
        "Enforced by the result broker's admission, result and failure hooks.",
        "Enforced by `plugin_erasure.turn_ended`",
        "Enforced by `plugin_erasure.EraseFence`",
    ):
        total = sum(text.count(lead) for text in texts.values())
        owned = texts[owner].count(lead)
        assert (total, owned) == (1, 1), (lead, total, owned)


def test_question_lifecycle_and_tool_interface_are_separate():
    """#1153: the split separates the question lifecycle from the tool
    interface.

    The seam is the one the manifest names itself at 0fca2ec9:
    ``docs/manifest.d/architecture-t-z.yaml:66-67`` — the tools-interface
    row's ``summary`` and ``when_changing`` — name "the question lifecycle"
    as its own clause beside the registry, the result contract and
    completion semantics, while ``defines_invariants`` (``:80``) lists
    INV-TOOL-008 beside INV-TOOL-001/002/006 on one document. The document
    is past the 25 KB ceiling at that base and INV-DOC-007
    (``docs/contributing/doc-contract.md:75``) owes the split.

    At 0fca2ec9 all four ids resolve to one document: the ownership counts
    are (1, 1), not (1, 2). The four payload-location checks independently
    hold at base and are regression arms.

    Pins ownership and lead location; byte-identical movement and
    completeness of the passages remain review obligations.

    Specified by **astra** in the drive red-case round.
    """
    moved = _declaring_document("INV-TOOL-008")
    retained = {_declaring_document(inv)
                for inv in ("INV-TOOL-001", "INV-TOOL-006")}
    counts = (len(retained), len(retained | {moved}))
    assert counts == (1, 2), (counts, retained, moved)
    texts = {doc: _normalized((DOCS / doc).read_text())
             for doc in _text_corpus()}
    for lead in (
        "A question cannot be delivered.",
        "A question is over before the tool returns.",
        "Enforced by one synchronous sample of that live map",
        "The delivery marker is not the oracle and never was",
    ):
        total = sum(text.count(lead) for text in texts.values())
        owned = texts[moved].count(lead)
        assert (total, owned) == (1, 1), (lead, total, owned)
