"""The docs CI workflow carries the sync-enforcement steps.

The sync property the corpus machinery provides: a PR that adds a substantial
module, an option, a tool, a route or an s6 unit goes red until the coverage
ledger assigns it; a PR that breaks a pinned invariant goes red in the unit
suite; a PR that moves a documented symbol goes red in the anchor check. This
test pins that the workflow actually runs the pieces — deleting a step from
docs.yml must fail here, not pass silently.
"""
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "docs.yml"


def test_pin_docs_workflow_runs_verifier_ledger_and_impact():
    """Red case demonstrated: removing the coverage_ledger check line from
    docs.yml fails this test."""
    text = WORKFLOW.read_text()
    # The corpus verifier (anchors, invariant-test bindings, allowlist).
    assert "scripts.verify_docs" in text
    # The code-derived coverage ledger, both directions.
    assert "coverage_ledger.py check" in text
    # Generated navigation is current.
    assert "--check-nav" in text
    # The docs-impact decision on changed paths. It no longer lives inline in
    # the workflow: it is scripts/docs_impact.sh, called by BOTH the workflow
    # and scripts/gate.sh (which is what actually binds, since a CI check
    # reports only after a PR exists and can be merged past — PR #383). Pin
    # every link of that chain, which is stricter than the old check for a flag
    # in the workflow text: a comment could satisfy that, and one nearly did.
    root = WORKFLOW.parents[2]

    def _invokes(path, script):
        """A real invocation, not a mention. Terra caught the first version of
        this pinning bare substrings: both callers name the script in comments,
        so deleting the executable line still passed. Match a line whose first
        word is the script (optionally via `bash`), with comments excluded.

        DIRECT invocation is deliberately the contract. Calls wrapped in `exec`,
        `env`, a variable, or a same-line `set -x;` are not recognised and will
        fail this test — which is fail-closed, and cheap to fix by calling the
        script plainly. Both callers do."""
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            words = stripped.split()
            if words[0] == script or (words[0] == "bash" and len(words) > 1
                                      and words[1] == script):
                return True
        return False

    assert _invokes(WORKFLOW, "scripts/docs_impact.sh"), \
        "docs.yml must CALL scripts/docs_impact.sh, not merely mention it"
    assert _invokes(root / "scripts" / "gate.sh", "scripts/docs_impact.sh"), \
        "gate.sh must CALL scripts/docs_impact.sh — it is the binding copy"
    # And the script itself must still DRIVE the verifier's impact mode — on a
    # live line. Sol caught the final link of the chain still being a bare
    # substring: commenting out the verifier pipeline left `--impact` present in
    # prose and the test green.
    impact_lines = [
        ln for ln in (root / "scripts" / "docs_impact.sh").read_text().splitlines()
        if "--impact" in ln and not ln.strip().startswith("#")
    ]
    assert impact_lines, (
        "scripts/docs_impact.sh must run verify_docs --impact on a live line, "
        "not merely mention it"
    )
    # The operating cards must keep routing agents into the corpus — pinned as
    # the substantive directive pattern, not a bare filename an unrelated
    # sentence could satisfy.
    assert 'docs/README\\.md.{0,20}routing table' in text


def test_pin_operating_cards_carry_the_routing_directive():
    """The properties the workflow's card-check enforces, asserted directly
    over newline-normalised text — cards are wrapped prose, and the first CI
    execution of the unnormalised grep failed on a phrase spanning a line
    break (a check never seen red, failing wrong).

    Red case demonstrated: rewording either phrase in a card fails this test.
    """
    import re

    root = WORKFLOW.parents[2]
    for card in ("CLAUDE.md", "AGENTS.md"):
        flat = " ".join((root / card).read_text().split())
        assert "verifiable from the public commit alone" in flat, card
        assert re.search(r"docs/README\.md.{0,20}routing table", flat), card


def test_pin_claude_coverage_module_scope_matches_code():
    """`CLAUDE.md`'s claim about the coverage ledger's module floor is the code's.

    The declared property, stated in full because the pin enforces its FORM as
    well as its content: `CLAUDE.md` states the ledger's module-size floor
    exactly once, in the paragraph that names the ledger, in one of two
    recognised forms — the literal "no size floor", or ">=N lines" — and that
    stated floor equals `scripts/coverage_ledger.py`'s `MIN_MODULE_LINES`
    (clamped at zero) and is zero. The grammar is deliberately closed: a
    semantically correct paraphrase it does not recognise fails here rather
    than passing, so a rewording of that one sentence goes past a human instead
    of silently unbinding the claim. This says nothing about any other fact in
    the card.

    The card is unbound prose: nothing manifests it, no `covers` anchor names
    it, the ledger does not enumerate it and the verifier lists it only as a
    `PROSE_TOKENS` exemption. That is why `CLAUDE.md` told contributors for the
    whole life of the corpus that the ledger fires for "a substantial module
    (>=100 lines)" while `scripts/coverage_ledger.py` has read
    `MIN_MODULE_LINES = 0` since the same commit that published both (#752).

    So this reads BOTH sides and compares them. The floor is parsed out of the
    ledger's own AST — not a comment, not a constant copied here, which would
    drift in exactly the way this test exists to catch. The card's claim is
    parsed out of the one paragraph that names the ledger, under a deliberately
    narrow grammar: "no size floor" means zero, ">=N lines" means N, and
    anything else is no claim at all. Wording the grammar does not recognise
    fails rather than passes, which is the fail-closed direction — a card that
    has gone vague about the floor is the state this defect started in.

    Red cases demonstrated (mutations run against the fixed tree, each one red):
    restoring "(>=100 lines)"; deleting the floor clause; replacing it with
    vague prose; stating it twice contradictorily; raising MIN_MODULE_LINES to
    100 with the card unchanged; and setting card and code to the SAME positive
    threshold — the last is caught by the second comparison, which pins the
    no-floor property itself and not merely agreement between the two files.
    """
    import ast
    import re

    root = WORKFLOW.parents[2]
    ledger_src = (root / "scripts" / "coverage_ledger.py").read_text()

    assignments = []
    for node in ast.parse(ledger_src).body:
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, int)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "MIN_MODULE_LINES":
                assignments.append(node.value.value)
    assert len(assignments) == 1, (
        "expected exactly one module-level integer MIN_MODULE_LINES assignment "
        f"in scripts/coverage_ledger.py, found {len(assignments)}"
    )
    code_floor = assignments[0]

    claim = re.compile(r"no size floor|(?:≥|>=)\s*(\d+)\s*lines?")

    def claimed_floors(text):
        return [0 if m.group(1) is None else int(m.group(1))
                for m in claim.finditer(" ".join(text.split()))]

    card = (root / "CLAUDE.md").read_text()
    ledger_paragraphs = [p for p in card.split("\n\n")
                         if re.search(r"coverage[ _]ledger", p)]
    assert len(ledger_paragraphs) == 1, (
        "expected exactly one CLAUDE.md paragraph describing the coverage "
        f"ledger, found {len(ledger_paragraphs)}"
    )
    paragraph = ledger_paragraphs[0]
    assert "module" in paragraph, \
        "the ledger paragraph must say what the ledger enumerates"

    in_card = claimed_floors(card)
    in_paragraph = claimed_floors(paragraph)
    assert len(in_card) == 1 and len(in_paragraph) == 1, (
        "CLAUDE.md must make exactly ONE module-size claim about the coverage "
        f"ledger, in the paragraph that describes it; found {in_card} in the "
        f"card and {in_paragraph} in that paragraph. The recognised forms are "
        "the literal 'no size floor' and '>=N lines' (or the same with the "
        "typographic sign); a correct paraphrase outside them fails here on "
        "purpose, so reword the sentence into one of them or widen this "
        "grammar deliberately."
    )
    claimed_floor = in_paragraph[0]

    assert claimed_floor == max(0, code_floor), (
        f"CLAUDE.md claims the coverage ledger's module floor is "
        f"{claimed_floor} lines; scripts/coverage_ledger.py enumerates every "
        f"module with loc >= MIN_MODULE_LINES = {code_floor}"
    )
    assert claimed_floor == 0, (
        "the ledger has no size floor and the card must not introduce one: "
        f"both now say {claimed_floor} lines"
    )
