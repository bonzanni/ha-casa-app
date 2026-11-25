#!/usr/bin/env bash
# keep-suite-output.sh <name> <cmd>...
#
# Runs <cmd>, showing its output as it comes, and keeps that output when <cmd>
# fails. The gate runs its suites through this: a test that fails once in a
# parallel run and passes on the re-run used to leave nothing behind unless the
# caller happened to capture the terminal, so the failure could not be told
# apart from its other candidate causes afterwards.
#
# A failing run's output moves to <git common dir>/casa-gate-failures/, named by
# time, commit and <name> plus a unique suffix, and the path is printed. The
# common directory is used because it outlives a worktree that is removed once
# its branch has merged. A passing run keeps nothing. The exit status is <cmd>'s
# own.
set -u

if [ "$#" -lt 2 ]; then
  echo "usage: keep-suite-output.sh <name> <cmd>..." >&2
  exit 64
fi
name=$1; shift

log="$(mktemp "$(git rev-parse --absolute-git-dir)/casa-gate-$name.XXXXXX")" || exit 1

set -o pipefail
"$@" 2>&1 | tee "$log"
status=$?

if [ "$status" -eq 0 ]; then
  rm -f "$log"
  exit 0
fi

keep="$(git rev-parse --path-format=absolute --git-common-dir)/casa-gate-failures"
stem="$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=9 HEAD)-$name"
# mktemp reserves the name, so two failures in one second keep two files.
if mkdir -p "$keep" && kept="$(mktemp "$keep/$stem.XXXXXX.log")" && mv "$log" "$kept"; then
  echo "✋ the $name suite failed; its output is kept at $kept" >&2
else
  echo "✋ the $name suite failed; its output could not be moved and is at $log" >&2
fi
exit "$status"
