---
last_reviewed: 2026-08-25
---

# How to keep this corpus true and readable

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The rules a document in this corpus follows, and which of them a machine checks. It does
not decide what may be published at all — that is
[`../doctrine/publishing.md`](../doctrine/publishing.md).

## Mental model

The audience is an AI agent that has just arrived, has no history with this codebase, and
is about to change something. It will read one or two files, act, and move on. Everything
below follows from that: a document is a working surface for a stranger under time
pressure, not a reference work.

Two consequences do most of the work. **Front-load**, so an agent that stops reading after
two sections has still got the load-bearing part. And **stay small**, so a document fits in
context *beside* the code it describes — which is the actual working set, and the reason
there is an enforced 25 KB ceiling rather than a suggestion. The ceiling is a tripwire, not
a tree-state prohibition: the change that crosses it lands, and the next change touching
that document must split it first.

## Contracts & invariants

**INV-DOC-001**: Every document repeats the code-wins line verbatim at the top, under front matter carrying `last_reviewed`.

`last_reviewed` is the one field here that is not a fact about the software. A commit proves
the string was written, not that anyone read the file that day — it is an author's claim,
kept because the rotation below needs something to sort on. Treat it as a claim, and do not
build anything on it that a lie would break.

**INV-DOC-002**: An architecture document follows one section order — Scope, Mental model, Contracts & invariants, Failure behavior, Extension points, Source & test map — so an agent can skim positionally; documents in the other directories keep the code-wins line and the source map but shape their own sections.

**INV-DOC-003**: A document describes the present system — no changelog voice, no "as of version X", no discrepancy log, no TODOs, no open questions; past tense may explain a mechanism, never track the document's own history.

**INV-DOC-004**: A cross-cutting rule is defined exactly once, as `**INV-AREA-NNN**: <single-line statement>`, and referenced by id rather than restated. Issue ids sparingly — an id per local rule is bureaucracy, and the define-once check makes over-issuing visibly expensive; nothing mechanical enforces a minimum reference count.

Three conventions about what an invariant *says*, learned the expensive way and
reviewer-enforced. An invariant states a **guarantee**; a known limitation belongs in the
"what it does not cover" prose under it — with its tracking issue — never red-pinned as
the invariant's own substance, because a pin on the gap makes the eventual fix arrive as
"breaking an invariant", inverted. A change that flips what an invariant asserts
**retires the id and mints a new one**: the ids exist to be referenced, and an id whose
meaning silently reverses between releases poisons every reference made under the old
meaning. And the one-line statement should be a bounded claim about a mechanism, not an
absolute — an absolute ("everything is X") invites an unbounded hunt for exceptions,
while the enforced boundary plus an honest not-covered clause is checkable and stays
true.

**INV-DOC-005**: Anchors are typed symbols (`path/to/file.py::Class.method`, `config.yaml::schema.key`, `tests/test_x.py::test_name`) or tracked bare paths; never `file:line`, which rots silently on the next edit.

**INV-DOC-009**: An invariant id cited in a comment or a docstring of a tracked Python file outside `docs/`, in a family the corpus defines, must be an id the corpus defines.

The idiom already dominates: code prose cites invariant ids far more often than it names document paths, and an id is the one handle that survives a split, because an invariant is defined once and the generated index maps it to its document. What was missing was the other direction of the check the corpus already runs inside `docs/` — so a retired or renumbered id stayed cited from a test docstring indefinitely, which is how a citation of the retired OBS 002 id outlived it. The check reads Python comments and PEP 258 docstrings only, and only for families the corpus defines: a synthetic fixture citing a family this corpus never defines is not a citation, and a rule that fired on the verifier's own test data would be waved through. A shell or Markdown comment outside `docs/` is NOT checked.

**INV-DOC-008**: A `Docs-impact:` waiver is checked against the diff it ships with — the waived documents are exactly the documents the change impacts and does not update, no document is waived twice, the reserved token `none` asserts that set is empty, only the tip commit may carry a waiver line, a subject line is never one, and every tip carries a line; whether a waiver's reason is true remains a review question.

**INV-DOC-007**: Growth splits, it never appends: the change that crosses the size ceiling lands visibly, the next change touching the over-ceiling document — its file or its manifest row — must split it first, a new path may not be born over the ceiling, and the ceiling is not raised; nothing shards on its own.

## Failure behavior

Knowing which rules a machine enforces matters, because claiming a convention is enforced is
how a corpus rots while passing.

**CI enforces**: the size trigger — on a pull request, judged against the merge-base: a
first crossing of the 25 KB ceiling (40 KB for generated indexes and manifest shards)
lands with a notice, a change touching a document already past its ceiling — its file or
its manifest row, at the ceiling of its manifest `kind` as it stands in the tree — fails
until the document ends back under it, and no document may be born over the ceiling, which
is also what refuses an over-ceiling rename or copy, since a renamed or copied file is a
new path; local and push-event runs report over-ceiling documents without failing, so the
pull-request check is the enforcing caller; allowlist exactness in both directions against
`git ls-files`; admitted extensions; manifest schema and required fields; anchor resolution
and containment; the marker pair in every document; invariant define-once, reference
resolution, and declaration accuracy; that an invariant id cited in a comment or a PEP 258
docstring of a tracked Python file outside `docs/` — a file named `*.py` or carrying a Python
shebang — resolves, when its family is one the corpus defines; that every declared invariant carries at least one
`invariant_tests` binding to a tracked file that is not the missing-test sentinel, with any
named test node resolved structurally against the file — module-level functions and
class-qualified `Class::method` identifiers both — so a binding that would not collect
fails the build (that the reference is a genuine *pinning* test is still established by
the red-case discipline, not by CI); the code-derived
coverage ledger in both directions (every enumerated
surface assigned to a document or excluded with a reason, no stale entries) — and, where
a surface is claimed by some document's `covers`, that the ledger's assignment agrees
with a claimant, so the ledger and the impact rule cannot name different owners for one
path; the required
skeleton; and that all generated navigation is current.

**The pre-push gate enforces**, before anything is published rather than after a pull
request exists: the same corpus verification, and the documentation-impact rule — a change
touching a surface some document claims must update that document, or carry a
per-document reasoned waiver (`Docs-impact: <doc> — <why the prose is still true>`) in the
tip commit. CI runs the identical script as a backstop for pushes made with hooks
uninstalled. The order matters and was learned the hard way: as a CI-only check it reported
after the pull request existed, leaving a red mark that a fast merge could pass, and one
did — six documents' worth of drift shipped that way.

A waiver is *checked against the diff it ships with*. The waived documents must be
exactly the documents the change impacts and does not update: waiving a document the
same change edits is refused (the waiver says the prose is still true, so you did not
edit it — the diff says otherwise), as is waiving a document the change does not impact,
and as is waiving one document twice. When no document needs a waiver the tip says so in
the same grammar, with the reserved token `Docs-impact: none — <reason>`; every tip
carries a line, so the absence of one is itself a refusal. Only the tip commit may carry
a waiver line: the squash message concatenates every commit's, so a line left behind in
an earlier commit would otherwise reach `main` waiving a diff it never saw, and the
pull-request check and the post-merge check would judge different text for one change.

What no layer can enforce is that a waiver is *sincere*. Whether the reason is true — or
is the one a reviewer read, rather than one substituted when the squash message was
written — is a review question; that is why the waiver is recorded in the commit under
its author's name. The gate checks which documents are named, not whether the sentence
beside each is honest.

**A reviewer enforces**: front matter and the code-wins line; the section order; present
tense; whether a document is one-hop sufficient; whether the same rule has been restated in
different words somewhere else; whether an over-ceiling document's resolution genuinely
reorganizes rather than trims content to get back under the number — reduce-to-fit is
refused in review, deliberately not by machine, because a machine test for "a split
happened" would prescribe the shape of the fix, and a legitimate resolution may move
content to an existing document and create nothing; and whether any of it is true. No script can tell a correct
rule from a plausible one.

Those reviewer obligations are the periodic sweep's job, per release, oldest `last_reviewed`
first. That rotation is a convention: nothing mechanical blocks a release on an unswept
document, so the sort on `last_reviewed` is what keeps ageing visible.

## Extension points

Adding a document means adding a manifest entry — `doc`, `summary`, `when_changing`, and the
`covers`, `tests`, `related` and `defines_invariants` it actually has. The verifier, gate
and CI refuse an unmanifested tracked document wherever they run; like every layer in
`doctrine/publishing.md`, that is enforced defence, not an absolute guarantee against a
deliberate bypass.

The manifest itself splits the same way it makes documents split: at its index ceiling,
entries move to `manifest.d/<name>.yaml` shards — each a plain top-level list loaded
together with the root by the verifier and the coverage ledger, and each shard file
carrying its own `kind: meta` entry in the allowlist. Documents never live under
`manifest.d/`; the verifier refuses one there.

`when_changing` is phrased as the *task* an agent is about to do, not the subsystem name.
An agent knows what it is about to change before it knows which subsystem owns it, and the
routing table has to meet it where it is.

Never hand-edit a generated block or file: `llms.txt`, the invariant index — which shards by
family letter across `doctrine/invariants.md` (A-M) and `doctrine/invariants-n-z.md` (N-Z),
because one file outgrew the index ceiling — the routing
table between the README's markers, and each document's Source & test map are all rendered
from the manifest (root plus shards). Hand-kept indexes rot behind the corpus they index; generated ones
cannot. Regenerate with `python -m scripts.verify_docs . --write-nav`.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `scripts/verify_docs.py::verify`
- `scripts/verify_docs.py::check_ceilings`
- `scripts/verify_docs.py::_check_sourcemap`
- `scripts/verify_docs.py::_check_invariants`
- `scripts/verify_docs.py::_check_prose_invariant_references`
- `scripts/verify_docs.py::write_nav`
- `scripts/coverage_ledger.py::check`

**Tests**
- `tests/test_verify_docs.py`
- `tests/test_coverage_ledger.py`
- `tests/test_docs_impact_contract.py`

**Related**
- [`doctrine/publishing.md`](../doctrine/publishing.md)
<!-- END SOURCEMAP -->
