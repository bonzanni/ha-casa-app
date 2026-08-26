#!/usr/bin/env bash
# Behavioural harness for scripts/docs_impact.sh — the documentation-drift gate.
#
# The gate runs in two places (scripts/gate.sh at pre-push, docs.yml as backstop)
# and both call this one script, so testing the script tests what ships. Earlier
# revisions of this harness sliced the logic out of the workflow YAML by string
# markers; extracting the logic into a script retired that fragility.
#
# The cases below drive the waiver half — parsing, reason quality, per-document
# scope, which commit is read — by calling the script's own parsing block against
# synthetic commits. The surrounding impacted/touched/deleted decision is driven
# with injected values, standing in for what the manifest and the diff produce.
#
# Run: bash tests/test_docs_impact_ack.sh
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

fails=0
pass() { echo "ok   $1"; }
fail() { echo "FAIL $1"; shift; [ $# -gt 0 ] && printf '%s\n' "$@" | sed 's/^/       /'; fails=$((fails + 1)); }

# The decision, lifted from the SCRIPT (not from YAML): everything from the waiver
# collection to the final refusal. Anchored on lines that are themselves
# load-bearing, so a restructure fails loudly instead of testing nothing.
extract_block() {
  python3 - "$repo_root" <<'PY_INNER'
import pathlib, sys
src = (pathlib.Path(sys.argv[1]) / "scripts/docs_impact.sh").read_text()
start = ': > "$tmp/acked.txt"'
if start not in src:
    raise SystemExit("harness: waiver block start marker not found — "
                     "scripts/docs_impact.sh was restructured; update this harness")
block = src[src.index(start):].rstrip()
for needle in ('"$tmp/missing.txt"', 'acked.txt', '$impacted', 'ack_commit'):
    if needle not in block:
        raise SystemExit(f"harness: extracted block lost {needle!r} — "
                         "the decision moved out of the slice")
print(block)
PY_INNER
}

block="$(extract_block)"

# Drive the block with a given impacted/touched/deleted set.
run_block() {  # run_block <impacted> <touched> <deleted>   [ack_commit in env]
  ( set -euo pipefail
    cd "$work/repo"
    tmp="$work/run"; rm -rf "$tmp"; mkdir -p "$tmp"
    err() { echo "docs-impact: $*" >&2; }
    impacted="$1" touched="$2" deleted="$3"
    ack_commit="${ack_commit-$(git rev-parse HEAD)}"
    eval "$block" )
}

expect() {  # expect <name> <ok|fail> <impacted> <touched> <deleted> [needle]
  local name="$1" want="$2" imp="$3" tch="$4" del="$5" needle="${6:--}" rc=0 out
  out="$(run_block "$imp" "$tch" "$del" 2>&1)" || rc=$?
  if [ "$want" = ok ] && [ "$rc" -ne 0 ]; then fail "$name (wanted pass, rc=$rc)" "$out"; return; fi
  if [ "$want" = fail ] && [ "$rc" -eq 0 ]; then fail "$name (wanted failure)" "$out"; return; fi
  if [ "$needle" != "-" ] && ! printf '%s' "$out" | grep -qF -- "$needle"; then
    fail "$name (missing '$needle')" "$out"; return
  fi
  pass "$name"
}

git init -q "$work/repo"
cd "$work/repo"
# Deliberately not address-shaped: this repo's pre-commit hook refuses anything
# matching an email pattern, and git does not validate the field.
git config user.email harness; git config user.name harness
git commit -q --allow-empty -m "base"
git branch -M main
tip() { git commit -q --allow-empty -m "$1"; }
reset_pr() { git checkout -q main; git checkout -q -B pr; }

D1=architecture/telegram.md
D2=architecture/turn-loop.md

# --- the gate still bites -------------------------------------------------
reset_pr; tip "change with no waiver"
expect "unwaived impacted doc fails" fail "$D1" "" "" "these docs claim it but did not change"

reset_pr; tip "change"
expect "touched doc satisfies the gate" ok "$D1" "$D1" ""

# --- a well-formed waiver -------------------------------------------------
reset_pr; tip "change

Docs-impact: $D1 — none, the claimed symbols were not modified"
expect "reasoned waiver accepted" ok "$D1" "" "" "acknowledged for $D1"

# --- reasons that are not reasons ----------------------------------------
reset_pr; tip "change

Docs-impact: $D1"
expect "no separator rejected" fail "$D1" "" "" "needs"

reset_pr; tip "change

Docs-impact: $D1 —"
expect "separator with nothing after it rejected" fail "$D1" "" "" "no real reason"

reset_pr; tip "change

Docs-impact: $D1 — ."
expect "punctuation-only reason rejected" fail "$D1" "" "" "no real reason"

reset_pr; tip "change

Docs-impact: $D1 — --"
expect "second separator as reason rejected" fail "$D1" "" "" "no real reason"

reset_pr; tip "change

Docs-impact: $D1 — $D1"
expect "doc name echoed back rejected" fail "$D1" "" "" "no real reason"

reset_pr; tip "change

Docs-impact: $D1 - still accurate here"
expect "plain hyphen separator accepted" ok "$D1" "" "" "acknowledged for $D1"

# --- no blanket waiver ----------------------------------------------------
reset_pr; tip "change

Docs-impact: $D1 — reason one"
expect "one waiver does not cover a second doc" fail "$D1
$D2" "" "" "$D2"

reset_pr; tip "change

Docs-impact: $D1 — reason one
Docs-impact: $D2 — reason two"
expect "per-document waivers cover both" ok "$D1
$D2" "" ""

# --- a waiver may not contradict the diff it ships with -------------------
# RED CASE for #685 / INV-DOC-008 (specified by Terra, drive run 2026-08-25).
# With impacted == touched == {D1}, `I - T` is empty, so ANY waiver is
# necessarily contradictory. Before the fix this passes: the waiver parses,
# then the per-document loop finds D1 in `touched` and short-circuits before
# ever consulting acked.txt, so the claim is compared to no diff at all.
reset_pr; tip "change

Docs-impact: $D1 — prose reviewed, but document is updated"
expect "waiver for a touched impacted document is contradictory" \
  fail "$D1" "$D1" "" "contradictory=1"

# --- the waiver is a statement about the FINAL diff -----------------------
reset_pr
tip "change

Docs-impact: $D1 — considered at the time"
tip "a later commit that changed more"
expect "waiver in an earlier commit does not carry" fail "$D1" "" "" "did not change"

git checkout -q main
tip "base-side waiver

Docs-impact: $D1 — waived on main"
git checkout -q -B pr; tip "change with no waiver of its own"
expect "base-side waiver does not carry" fail "$D1" "" "" "did not change"

# --- an indented example is prose, not a waiver ---------------------------
reset_pr; tip "document the mechanism

Write it like this:
    Docs-impact: $D1 — some reason"
expect "indented example is not a waiver" fail "$D1" "" "" "did not change"

# --- a deleted claimant is refused outright -------------------------------
reset_pr; tip "change"
expect "deleting a claimant is refused" fail "$D1" "" "$D1" "deleted in the same change"

# --- document names match WHOLE, never as substrings ----------------------
# Terra r2 found the harness blind to `grep -qxF` decaying to `grep -qF`: with
# no name that contains another, substring matching passes every case. These
# two pin it on both the waiver and the touched paths.
# SUPER must genuinely CONTAIN SUB, or the case proves nothing: the realistic
# form is a waiver written with the `docs/` prefix the impacted list never uses.
SUB=architecture/memory.md
SUPER=docs/architecture/memory.md
case "$SUPER" in *"$SUB"*) ;; *) fail "harness bug: SUPER does not contain SUB"; esac
reset_pr; tip "change

Docs-impact: $SUPER — written with the wrong path form"
expect "waiver naming a superstring path does not cover the document" fail "$SUB" "" "" "$SUB"

reset_pr; tip "change"
expect "touching a superstring path does not cover the document" fail "$SUB" "$SUPER" "" "$SUB"

# --- the production checkout is a MERGE commit ----------------------------
# On `pull_request`, actions/checkout leaves HEAD at GitHub's synthetic merge
# commit, whose message carries no waiver. The step must read the contributor's
# tip instead — the bug both reviewers caught in round 2.
reset_pr
tip "change

Docs-impact: $D1 — none, claimed symbols untouched"
pr_tip="$(git rev-parse HEAD)"
git checkout -q main
git merge -q --no-ff -m "Merge $pr_tip into main" pr
ack_commit="$pr_tip" expect "waiver is read from the PR tip, not the merge commit" \
  ok "$D1" "" "" "acknowledged for $D1"
unset ack_commit || true
expect "reading the merge commit instead finds no waiver" fail "$D1" "" "" "did not change"
git checkout -q main; git reset -q --hard HEAD~1

# --- the named commit is the one consulted --------------------------------
reset_pr; tip "change

Docs-impact: $D1 — some genuine reason"
ack_commit="$(git rev-parse HEAD)" expect "tip waiver applies" ok "$D1" "" "" "acknowledged for $D1"
unset ack_commit || true

# --- the gate is not disabled by deleting the manifest --------------------
# Terra+Sol: gate.sh used to call this only when docs/manifest.yaml existed at
# HEAD, so a commit could delete the manifest, change any claimed surface, and
# skip the check. That guard is gone; assert the caller has no such condition.
# Scope to the docs-impact STEP: step 1/7 (the corpus verifier) legitimately
# guards on a manifest existing, and grepping the whole file would confuse the
# two — a false pass here is worse than no check.
gate_step="$(sed -n '/==> 2\/7 docs-impact/,/^echo "==> 3\//p' "$repo_root/scripts/gate.sh")"
if [ -z "$gate_step" ]; then
  fail "could not locate the docs-impact step in gate.sh — renumbered?"
elif printf '%s' "$gate_step" | grep -qE '^\s*if .*(manifest\.yaml|-f docs/)'; then
  fail "gate.sh conditions the docs-impact call on a file test"
else
  pass "gate.sh calls docs-impact unconditionally"
fi

# --- the CI backstop covers direct pushes, not only pull requests ---------
if grep -qE "^\s+if: github.event_name == 'pull_request'\s*$" \
     <(sed -n '/name: Docs impact on claimed surfaces/,/^\s*run:/p' \
         "$repo_root/.github/workflows/docs.yml"); then
  fail "docs.yml docs-impact step is pull_request-only — a direct push skips it"
else
  pass "docs.yml docs-impact step also runs on push"
fi

echo
[ "$fails" -eq 0 ] && { echo "docs-impact gate: all checks passed"; exit 0; }
echo "docs-impact gate: $fails check(s) failed"; exit 1
