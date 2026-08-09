#!/usr/bin/env bash
# Bounded stderr ring [D:§W1, Sol B5/r5-B3/r6-B2/B3/r7-B4]. Unique per-epoch file
# (run script: .stderr.<EPOCH>.log; MY_EPOCH is $3). LC_ALL=C → bash string ops
# count BYTES not codepoints; `read -N CHUNK` reads at most CHUNK bytes/iteration
# (bounded memory even for a multi-MB no-newline line — all probe-verified).
export LC_ALL=C
FILE="$1"; MAX="${2:-65536}"; MY_EPOCH="${3:-0}"; CHUNK=2048
# Task 4 (containment stage 2): FILE is now the CONTROL-DIR ".stderr.<epoch>.log"
# path (the run template's cwd stays the WORKSPACE, so a bare ".spawn_epoch"
# read here would always miss). Derive the control dir from FILE's own
# dirname rather than trusting cwd — this is exactly the directory the run
# template publishes ".spawn_epoch" into, whatever that directory is.
CTL_DIR=$(dirname -- "$FILE")
# STALE-EPOCH FENCE (r7-B4/r8-B4): my file is prunable once the current spawn
# epoch is >= MY_EPOCH+4. A check-BEFORE-open has a TOCTOU hole (pass the check,
# E+4 publishes+sweeps, THEN we create the path). So the fence is checked AFTER
# every open: create → re-check → if stale, remove OUR OWN just-created path(s)
# and exit. Whoever creates a stale path deletes it themselves — the invariant
# does not depend on winning a race with the sweeper. (.spawn_epoch is published
# ATOMICALLY by the run script via tmp+mv, so we never read a torn value.)
_stale() { local c; c=$(cat "$CTL_DIR/.spawn_epoch" 2>/dev/null || echo "$MY_EPOCH"); [ "$(( c - MY_EPOCH ))" -ge 4 ]; }
_fence() {  # close fd 3 if open, remove OWN paths, exit — post-open stale check
  exec 3>&- 2>/dev/null || true
  rm -f "$FILE" "$FILE.1" 2>/dev/null || true
  exit 0
}
_stale && exit 0                        # fast path: stale before any create
: > "$FILE"; rm -f "$FILE.1" 2>/dev/null || true
exec 3>>"$FILE"                          # HOLD the write fd open (r6-B2)
_stale && _fence                         # r8-B4: post-OPEN re-check (self-unlink)
size=0
while IFS= read -r -N "$CHUNK" chunk || [ -n "$chunk" ]; do
  printf '%s' "$chunk" >&3
  size=$(( size + ${#chunk} ))          # LC_ALL=C ⇒ ${#chunk} is a BYTE count
  if [ "$size" -gt "$MAX" ]; then        # rotate: close, then reopen fresh
    exec 3>&-
    _stale && _fence                     # fence BEFORE recreating on rotation
    mv -f "$FILE" "$FILE.1"; : > "$FILE"; exec 3>>"$FILE"; size=0
    _stale && _fence                     # r8-B4: post-REOPEN re-check (self-unlink)
  fi
  chunk=""
done
exec 3>&-
# NUL bytes cannot round-trip through a bash variable; stderr is text (JSON/log
# lines) so this is acceptable for best-effort diagnostics.
