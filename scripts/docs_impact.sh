#!/usr/bin/env bash
# The documentation-drift gate: a change to a claimed code surface must come with
# a change to the document that claims it, or a reasoned per-document waiver.
#
# Usage: scripts/docs_impact.sh <base-ref> <ack-commit>
#
#   base-ref     what this change is measured against (origin/main, or the PR base)
#   ack-commit   the commit whose message carries the waivers — the TIP of the work,
#                and the ONLY commit in the range allowed to carry one (#685)
#
# Exit 0 = every impacted document was updated or waived. Exit 1 = it was not, or
# the input was unusable. It fails CLOSED: an unreadable manifest, an empty
# ack-commit and an unresolvable base are all refusals, never silent passes.
#
# WHY THIS IS A SCRIPT AND NOT A CI STEP. It runs in two places on purpose:
#
#   * `scripts/gate.sh` (the pre-push gate) — the control that actually binds.
#     A CI check reports AFTER a pull request exists, which leaves a red mark
#     somebody can merge past; that is exactly what happened on PR #383, where
#     this check failed naming six documents and the batch was squash-merged
#     with `--admin` a minute later, before it reported. Run at pre-push, the
#     same logic refuses before anything is published, and the attestation
#     `.githooks/pre-push` demands cannot be produced without it.
#   * `.github/workflows/docs.yml` — the backstop that catches a push made with
#     hooks uninstalled or `--no-verify`, and anything arriving from elsewhere.
#
# One implementation, two callers: a second copy would drift, and the copy that
# drifted would be the one nobody was watching.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

base="${1:?usage: docs_impact.sh <base-ref> <ack-commit>}"
ack_commit="${2:?usage: docs_impact.sh <base-ref> <ack-commit>}"

# GitHub Actions renders ::error:: as an annotation; a terminal wants plain text.
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  err() { echo "::error::$*"; }
else
  err() { echo "✋ docs-impact: $*" >&2; }
fi

git rev-parse --verify --quiet "$base" >/dev/null || {
  err "base ref '$base' does not resolve — cannot tell what changed."
  exit 1
}
git rev-parse --verify --quiet "$ack_commit" >/dev/null || {
  err "ack commit '$ack_commit' does not resolve — cannot read waivers."
  exit 1
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

python_bin="python3"
[ -x venv_test/bin/python ] && python_bin="venv_test/bin/python"

changed="$(git diff --name-only "$base"...HEAD)"

# A version-only bump to the app manifest is not a schema change. BOTH added and
# removed lines are inspected: deleting an option produces no added schema line,
# and treating that as version-only would skip the check on an option removal —
# the change most likely to invalidate a document.
if printf '%s\n' "$changed" | grep -qx 'casa/config\.yaml'; then
  substantive="$(git diff -U0 "$base"...HEAD -- casa/config.yaml \
    | grep -E '^[+-]' | grep -vE '^(\+\+\+|---)' | grep -vE '^[+-]version:' || true)"
  if [ -z "$substantive" ]; then
    changed="$(printf '%s\n' "$changed" | grep -vx 'casa/config\.yaml' || true)"
  fi
fi

# The claim map is editable in the same change, so consult the BASE manifest too:
# deleting a covers anchor must not delete the obligation.
#
# FAIL CLOSED. Substituting an empty manifest on any error silently drops every
# base-side claim — the exact obligations this exists to preserve. An empty one is
# accepted only when the base genuinely has no manifest, which is true until the
# corpus first lands and false forever after.
# Terra: distinguish GENUINELY ABSENT from UNREADABLE. `cat-file -e` returns
# non-zero for both, and treating a corrupt or unfetched object as "the base has
# no manifest" erases every base-side claim — failing open in the one case where
# the obligations matter most. Ask the tree what exists first; only a path the
# base does not list is absent.
base_has_manifest=0
if git ls-tree --name-only "$base" -- docs/manifest.yaml 2>/dev/null | grep -qx 'docs/manifest.yaml'; then
  base_has_manifest=1
fi
if [ "$base_has_manifest" = 1 ]; then
  if ! git cat-file -e "$base:docs/manifest.yaml" 2>/dev/null; then
    err "$base lists docs/manifest.yaml but its object is unreadable."
    err "Refusing rather than dropping every base-side claim. Try: git fetch --all"
    exit 1
  fi
  git show "$base:docs/manifest.yaml" > "$tmp/base-manifest.yaml"
  # #367: base-side claims live in manifest shards too. Each shard is a top-level
  # YAML list, so plain concatenation yields one valid list. `grep || true`: a base
  # with no shards (grep exit 1) must not kill the run under pipefail.
  git ls-tree --name-only "$base" -- docs/manifest.d/ 2>/dev/null \
    | { grep '\.yaml$' || true; } | while read -r shard; do
        git show "$base:$shard" >> "$tmp/base-manifest.yaml"
      done
else
  echo "note: $base has no docs/manifest.yaml yet — no base-side claims to carry"
  : > "$tmp/base-manifest.yaml"
fi

impacted="$(printf '%s\n' "$changed" \
  | "$python_bin" -m scripts.verify_docs . --impact --base-manifest "$tmp/base-manifest.yaml")"
# #685: there is deliberately NO early return on an empty impacted set. It used
# to stand here, and it meant that on a diff impacting nothing, not one waiver
# line was ever read — so a malformed line rode to `main` untouched and only the
# push arm, evaluating the concatenated squash body against a larger cumulative
# diff, ever parsed it and reddened a branch that had been green. Everything the
# waiver block decides is now reachable on every invocation.

# A DELETED doc is not an updated doc.
touched="$(git diff --name-only --diff-filter=d "$base"...HEAD | grep '^docs/' | sed 's|^docs/||' || true)"
deleted="$(git diff --name-only --diff-filter=D "$base"...HEAD | grep '^docs/' | sed 's|^docs/||' || true)"

# A claimed surface can genuinely change without invalidating the prose — the claim
# is file-level, so editing one function in a file whose OTHER symbol the document
# quotes impacts nothing readers can see. Requiring a doc edit anyway would buy
# cosmetic commits and teach everyone to make them, which is how a gate stops
# meaning anything. So there is an explicit, per-document, reasoned waiver,
# recorded in a commit message and therefore in history:
#
#     Docs-impact: architecture/tools-interface.md — none (claimed symbols unchanged)
#
# Per-document ON PURPOSE: one blanket waiver would let a six-document change
# through on a single line, and looking at each document is the whole point.
#
# Read from the ACK COMMIT ONLY, as a trailer at column zero. This mirrors
# scripts/attest.sh, whose receipts a new commit voids on purpose: a waiver is a
# statement about the diff as it finally stands, so adding another commit must
# invalidate it rather than let a line written five commits ago — possibly since
# reverted, cherry-picked, or merged in from elsewhere — waive a surface it never
# saw. Column zero also stops an indented EXAMPLE inside prose (a commit that
# documents this very mechanism, say) from acting as a live waiver.
#
# ACCEPTED RESIDUAL, narrowed on the record by #685. The original text closed
# with "do not add a diff-digest binding scheme without evidence of a real case
# it would have caught". That evidence now exists and is named: the #683 squash
# (a7ad0aa0) carries 30 column-zero Docs-impact lines over 10 unique documents,
# three of them documents the same cumulative range UPDATED; the #694 squash
# (f4e624cf) carries 23 lines, three of which lack the required separator and
# were seen only by the push arm. So the CLAIM half is now checked — see the set
# contract below — and note that it is not a digest scheme: it binds each waiver
# to the impacted/touched sets this script already derives, with no new artifact
# to keep in sync.
#
# What remains residual, unchanged and unweakened: this cannot verify that a
# waiver is SINCERE. `--amend --no-edit` after further edits, `git commit -C
# <old>`, or a reason of "abc" all satisfy the letter, and a reason substituted
# by hand in a squash message is indistinguishable from one written that way in
# the first place. No text check can do better. Substance is a review question,
# and the waiver is recorded under its author's name for exactly that reason.
#
# WHAT THE TWO ARMS SEE, stated exactly, because they do not read the same text
# (#685): the pull-request arm reads the branch tip's message, the push arm on
# `main` reads the squash commit's, which under this repository's
# `COMMIT_MESSAGES` default is the concatenation of every branch commit's
# message. The provenance rule below — only the ack commit may carry a waiver
# line, and a subject line is never a waiver — is what makes those two texts
# carry the same waiver set: the tip's body lines survive concatenation at
# column zero, every other commit's subject is bulleted, and no other commit is
# allowed to contribute a body line. Therefore:
#
#   * DETECTED — a squash text whose waiver SET differs from the cumulative
#     diff: extra, missing, contradictory or duplicated documents.
#   * DETECTED — total loss of the waiver lines (a merge-message mode that
#     drops constituent bodies, an emptied or replaced message). Because the ack
#     commit must always carry at least one line, the push arm finds zero and
#     refuses on the first merge that hits it, rather than degrading silently
#     until some later change happens to need a waiver.
#   * NOT DETECTED — a set-preserving substitution: the same document tokens
#     with different but syntactically valid REASONS. That is the sincerity
#     residual above. Detecting it would require binding the push text to the
#     pull request's head message through the GitHub API, which would put a
#     network dependency and seven new refusal paths inside the only check that
#     catches documentation drift. Deliberately not done.
: > "$tmp/acked.txt"
: > "$tmp/non-tip.txt"
: > "$tmp/missing.txt"
: > "$tmp/contradictory.txt"
: > "$tmp/irrelevant.txt"
: > "$tmp/duplicate.txt"

# PROVENANCE (#685). Only the ACK COMMIT may carry a waiver line. The range is
# merge-base(base, ack)..ack minus the ack itself — the same merge-base
# semantics the impacted/touched/deleted sets use above, and the same commit set
# GitHub shows as a pull request's commits, so an "Update branch" merge of the
# base moves the merge base forward and drops the base's own waiver-carrying
# commits out of both together.
#
# The lines are COUNTED, never parsed: a malformed line in an intermediate
# commit must be refused AS an intermediate line. Parsing it would report a
# grammar error for a line that has no business existing at all, which is what
# #694 looked like from the push arm, after publication.
ack_full="$(git rev-parse --verify "$ack_commit^{commit}")"
# A FILE, never a pipe, for the same reason the waiver loop below uses one.
: > "$tmp/range.txt"
if merge_point="$(git merge-base "$base" "$ack_commit" 2>/dev/null)"; then
  git rev-list "$merge_point..$ack_commit" > "$tmp/range.txt"
elif [ -z "$(git log -1 --format=%P "$ack_commit")" ]; then
  # A ROOT commit summarises nothing: it has no history, so no earlier commit's
  # message can be concatenated with its own and there is no provenance question
  # to answer. This is the shape an off-graph DRY RUN takes — building the
  # prospective squash message as a parentless object and asking whether it
  # would pass — and refusing it would refuse the one check that catches the
  # arm asymmetry before the merge rather than after.
  echo "note: '$ack_commit' is a root commit — no earlier commit can carry a waiver"
else
  # Not a root, and no shared history with the base: intermediate commits exist
  # and this cannot enumerate them. Refuse rather than report a provenance count
  # of zero that was never measured.
  err "'$base' and '$ack_commit' have no common ancestor, and '$ack_commit' is"
  err "not a root commit — the commits belonging to this change cannot be"
  err "enumerated, so a waiver's provenance cannot be checked. Refusing rather"
  err "than guessing."
  exit 1
fi
while IFS= read -r rev; do
  [ -n "$rev" ] || continue
  [ "$rev" = "$ack_full" ] && continue
  # tail -n +2: the SUBJECT is never a waiver, on any commit. `%B` puts it at
  # column zero, so a subject-line waiver parses on the pull-request arm — but
  # the squash formatter prefixes every constituent subject with "* ", so the
  # push arm sees nothing. Skipping line one everywhere is what closes that.
  n="$(git log -1 --format=%B "$rev" | tail -n +2 \
       | { grep -c '^[Dd]ocs-[Ii]mpact:' || true; })"
  [ "$n" -gt 0 ] && printf '%s %s\n' "$rev" "$n" >> "$tmp/non-tip.txt"
done < "$tmp/range.txt"
non_tip="$(wc -l < "$tmp/non-tip.txt" | tr -d ' ')"

git log -1 --format=%B "$ack_commit" | tail -n +2 \
  | sed -n 's/^[Dd]ocs-[Ii]mpact:[[:space:]]*//p' \
  > "$tmp/acks.txt"
# Read from a FILE, never a pipe: a `while` on the right of a pipe runs in a
# subshell, where `exit 1` would end the subshell and let the run continue.
ack_lines=0
none_claimed=0
while IFS= read -r ack; do
  [ -n "$ack" ] || continue
  ack_lines=$((ack_lines + 1))
  ack_doc="${ack%%[[:space:]]*}"
  ack_rest="$(printf '%s' "${ack#"$ack_doc"}" | sed 's/^[[:space:]]*//')"
  # A separator is required, so `Docs-impact: <doc> <doc>` cannot pass by looking
  # like a reason.
  case "$ack_rest" in
    "—"*|"--"*|"-"*) ;;
    *)
      err "Docs-impact for '$ack_doc' needs '— <reason>'."
      exit 1 ;;
  esac
  ack_reason="$(printf '%s' "$ack_rest" | sed 's/^\(—\|--\|-\)[[:space:]]*//')"
  # A REAL reason: at least three alphanumerics, so punctuation, a second
  # separator, or a lone character cannot stand in for having thought about it;
  # and not merely the document's own name echoed back.
  ack_core="$(printf '%s' "$ack_reason" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]')"
  ack_docname="$(printf '%s' "$ack_doc" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]')"
  if [ "${#ack_core}" -lt 3 ] || [ "$ack_core" = "$ack_docname" ]; then
    err "Docs-impact for '$ack_doc' has no real reason: '$ack_reason'"
    err "Say why the prose is still true, in words."
    exit 1
  fi
  # `none` is a RESERVED TOKEN, not a document: it asserts that this change
  # leaves no document needing a waiver. No corpus path can collide with it —
  # every one carries a top directory and a suffix. Without the reservation the
  # set contract below would classify the form CLAUDE.md prescribes for a
  # tests-only change as an irrelevant waiver and refuse every one of them.
  if [ "$ack_doc" = "none" ]; then
    none_claimed=1
    echo "note: docs-impact reserved token 'none' — $ack_reason"
    continue
  fi
  printf '%s\n' "$ack_doc" >> "$tmp/acked.txt"
  echo "note: docs-impact acknowledged for $ack_doc — $ack_reason"
done < "$tmp/acks.txt"
ack_unique="$(sort -u "$tmp/acked.txt" | { grep -c . || true; })"

# One path per line, never `for doc in $impacted`: word splitting would break a
# path containing a space into fragments, and matching those fragments against
# `touched` could satisfy the check while the real claiming document is untouched.
# The verifier separately forbids whitespace in manifest paths; this does not rely
# on that holding.
#
# T is I ∩ touched, not the raw touched-path list: a document the diff edits but
# whose surfaces this change does not touch is not part of the obligation, and
# counting it would understate what still needs a waiver.
impacted_n=0
touched_n=0
while IFS= read -r doc; do
  [ -n "$doc" ] || continue
  impacted_n=$((impacted_n + 1))
  if printf '%s\n' "$deleted" | grep -qxF -- "$doc"; then
    err "$doc claimed a changed surface and was deleted in the same change."
    err "Retire a claimed document on its own, not alongside the change."
    exit 1
  fi
  if printf '%s\n' "$touched" | grep -qxF -- "$doc"; then
    touched_n=$((touched_n + 1))
    continue
  fi
  grep -qxF -- "$doc" "$tmp/acked.txt" 2>/dev/null && continue
  printf '%s\n' "$doc" >> "$tmp/missing.txt"
done <<< "$impacted"

# THE CROSS-CHECK (#685). Until this landed, a waiver's claim was compared to no
# diff at all: the loop above consults acked.txt only for documents the range did
# NOT touch, so a waiver naming a document the range updates was never
# contradicted and shipped into the audit trail unchallenged. The set contract
# is `A = I − T`, evaluated in both directions.
while IFS= read -r doc; do
  [ -n "$doc" ] || continue
  if ! printf '%s\n' "$impacted" | grep -qxF -- "$doc"; then
    printf '%s\n' "$doc" >> "$tmp/irrelevant.txt"
  elif printf '%s\n' "$touched" | grep -qxF -- "$doc"; then
    printf '%s\n' "$doc" >> "$tmp/contradictory.txt"
  fi
done < <(sort -u "$tmp/acked.txt")
sort "$tmp/acked.txt" | uniq -d > "$tmp/duplicate.txt"

missing="$(wc -l < "$tmp/missing.txt" | tr -d ' ')"
contradictory="$(wc -l < "$tmp/contradictory.txt" | tr -d ' ')"
irrelevant="$(wc -l < "$tmp/irrelevant.txt" | tr -d ' ')"
duplicate="$(wc -l < "$tmp/duplicate.txt" | tr -d ' ')"
untouched_n=$((impacted_n - touched_n))

# One deterministic line, counts and not statuses, on every decided path. An
# exit code cannot say WHICH predicate fired, and a mutation check that reads
# only exit codes cannot tell one guard from another.
summary() {
  echo "docs-impact: impacted=$impacted_n touched=$touched_n" \
       "ack_lines=$ack_lines ack_unique=$ack_unique" \
       "missing=$missing contradictory=$contradictory" \
       "irrelevant=$irrelevant duplicate=$duplicate non_tip=$non_tip"
}

if [ "$non_tip" -gt 0 ]; then
  summary
  err "only the tip commit may carry a Docs-impact line; these do not:"
  while IFS= read -r line; do err "  ${line% *} — ${line##* } line(s)"; done < "$tmp/non-tip.txt"
  err "A waiver is a statement about the diff as it finally stands, and the"
  err "squash message concatenates every commit's, so a line left behind in an"
  err "earlier commit reaches \`main\` waiving a diff it never saw. Move every"
  err "Docs-impact line to the tip (git rebase -i, or reword), or drop it."
  exit 1
fi

if [ "$contradictory" -gt 0 ] || [ "$irrelevant" -gt 0 ] || [ "$duplicate" -gt 0 ]; then
  summary
  while IFS= read -r doc; do
    [ -n "$doc" ] || continue
    err "$doc is WAIVED and also UPDATED by this change."
    err "  A waiver says the prose is still true so you did not edit it. The"
    err "  diff says you did. Drop the waiver, or drop the edit."
  done < "$tmp/contradictory.txt"
  while IFS= read -r doc; do
    [ -n "$doc" ] || continue
    err "$doc is waived but this change does not impact it."
    err "  Nothing claims a surface this change touches. Drop the line, or fix"
    err "  the path — the impacted list never carries the \`docs/\` prefix."
  done < "$tmp/irrelevant.txt"
  while IFS= read -r doc; do
    [ -n "$doc" ] || continue
    err "$doc is waived more than once — which reason is the real one?"
  done < "$tmp/duplicate.txt"
  exit 1
fi

if [ "$none_claimed" = 1 ] && [ "$untouched_n" -gt 0 ]; then
  summary
  err "'Docs-impact: none' says no document needs a waiver. These do:"
  while IFS= read -r doc; do err "  $doc"; done < "$tmp/missing.txt"
  exit 1
fi

if [ -s "$tmp/missing.txt" ]; then
  summary
  err "changed a claimed surface; these docs claim it but did not change:"
  while IFS= read -r doc; do err "  $doc"; done < "$tmp/missing.txt"
  err "Update each document, or acknowledge it in the tip commit message:"
  err "  Docs-impact: <doc> — <why the prose is still true>"
  exit 1
fi

# The ack commit must ALWAYS carry a line — a per-document waiver, or the
# reserved `Docs-impact: none — <reason>` when nothing needs one. Last, so that
# a commit which is ALSO missing a waiver gets told which document, not merely
# that a line is absent. Its purpose is detection: the push arm reads a squash
# message this repository does not produce and cannot inspect before the merge,
# so if that message ever stops carrying constituent bodies, this refuses on the
# very next merge instead of staying quiet until some later change happens to
# need a waiver.
if [ "$ack_lines" -eq 0 ]; then
  summary
  err "$ack_commit carries no Docs-impact line."
  err "Every tip states what this change does to the documentation, even when"
  err "the answer is nothing:"
  err "  Docs-impact: none — <why no document is affected>"
  exit 1
fi

summary
