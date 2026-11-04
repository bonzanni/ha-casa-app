#!/usr/bin/env bash
# Does a change need the Casa Browser unit suite? scripts/gate.sh asks before step 6.
#
#   scripts/browser-suite-affected.sh <base-sha> <head-sha>
#   scripts/browser-suite-affected.sh --casa-only-roots   (one root per line)
#
# Prints ONE line and exits 0:
#   run: <reason>    the suite must run
#   skip: <reason>   every changed path is Casa-only
# Any failure exits non-zero, and the gate reads that as "run".
#
# Decided from git facts only: the paths in the tree diff base..head, with rename
# detection OFF so a move reports both its old and its new path. No cache, no
# timestamps, no working tree.
#
# FAIL-CLOSED by construction. The list below names what is proven NOT to reach the
# browser suite; every other path counts as shared — casa_browser/ itself, and also the
# Makefile, scripts/, pytest.ini, .githooks/ (test_scaffold reads its binary allowlist),
# .gitattributes (it decides the bytes a checkout writes), .github/ and any new top-level
# path. A path is "proven" here because, when the gate does run the suite, it runs it in
# an export of HEAD WITHOUT these roots (scripts/gate.sh): a browser test that came to
# depend on one of them fails there, at the commit that introduced the dependency.
set -euo pipefail

CASA_ONLY_ROOTS=(casa/ tests/ docs/ test-local/)

# The gate removes exactly these roots from the tree it runs the suite in.
if [ "${1:-}" = "--casa-only-roots" ] && [ "$#" -eq 1 ]; then
  printf '%s\n' "${CASA_ONLY_ROOTS[@]%/}"
  exit 0
fi
[ "$#" -eq 2 ] || { echo "usage: $0 <base-sha> <head-sha> | --casa-only-roots" >&2; exit 2; }
base="$1" head="$2"
cd "$(git rev-parse --show-toplevel)"

# Into a file, not a process substitution: a failed diff must stop the script (set -e)
# rather than read as an empty change list.
list="$(mktemp)"
trap 'rm -f "$list"' EXIT
git diff --no-renames --no-ext-diff --no-relative --ignore-submodules=none --name-only -z "$base" "$head" -- > "$list"
paths=()
while IFS= read -r -d '' p; do paths+=("$p"); done < "$list"

for p in "${paths[@]}"; do
  casa_only=
  for root in "${CASA_ONLY_ROOTS[@]}"; do
    case "$p" in "$root"*) casa_only=1; break ;; esac
  done
  if [ -z "$casa_only" ]; then
    echo "run: ${p//$'\n'/\\n} changed in $base..$head"   # one line, whatever the name
    exit 0
  fi
done
echo "skip: no change under casa_browser/ or shared files in $base..$head (${#paths[@]} path(s), all under ${CASA_ONLY_ROOTS[*]})"
