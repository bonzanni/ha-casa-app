#!/usr/bin/env bash
# suite-lock.sh <lockfile> <seconds> <cmd>...
#
# Runs <cmd> under an exclusive flock on <lockfile>: the Makefile's LOCK, which
# keeps two memory-caged unit suites from running at once. A bare
# `flock -w N` waits in silence and, on expiry, exits 1, so a caller saw only
# `make: *** Error 1` with no pytest output, while another session's hung suite
# held the lock for over an hour. This wrapper says who holds the lock when the
# wait starts and again when it expires. A timeout exits 75 (EX_TEMPFAIL) with
# the message "suite lock timeout".
#
# It never skips the lock and never kills the holder: a holder using little CPU
# may be a slow test, and stopping another session's run is not this script's
# decision. The holder lines give the reader what they need to decide.
set -u

if [ "$#" -lt 3 ]; then
  echo "usage: suite-lock.sh <lockfile> <seconds> <cmd>..." >&2
  exit 64
fi
lock=$1; wait_s=$2; shift 2

holder() {
  if ! command -v lslocks >/dev/null 2>&1; then
    echo "  holder unknown (lslocks unavailable)" >&2
    return
  fi
  local pids
  pids=$(lslocks -n -o PID,PATH 2>/dev/null | awk -v p="$lock" '$2 == p {print $1}' | sort -u)
  if [ -z "$pids" ]; then
    echo "  holder unknown (lslocks names no process on $lock)" >&2
    return
  fi
  local pid kids
  for pid in $pids; do
    echo "  holder pid $pid, cwd $(readlink "/proc/$pid/cwd" 2>/dev/null || echo '?')" >&2
    kids=$(pgrep -P "$pid" 2>/dev/null | tr '\n' ',' | sed 's/,$//')
    ps -o pid=,etimes=,pcpu=,args= -p "$pid${kids:+,$kids}" 2>/dev/null \
      | awk '{printf "    pid %s  elapsed %ss  cpu %s%%  ", $1, $2, $3; $1=$2=$3=""; sub(/^ +/, ""); print}' >&2
  done
}

# The lock is taken by a `flock` process that stays alive as the command's
# parent for the command's whole lifetime, so `lslocks` names a live holder: a
# lock taken on an inherited descriptor by a `flock -n` that then exits is
# attributed to that dead pid, and a contender could name nobody (diff r1,
# Astra S2).
if ! flock -n "$lock" true; then
  echo "suite lock $lock is held — waiting up to ${wait_s} s" >&2
  holder
fi
flock -w "$wait_s" -E 75 "$lock" "$@"
rc=$?
# 75 is flock's own timeout status here; the suite's pytest never exits 75.
if [ "$rc" -eq 75 ]; then
  echo "suite lock timeout after ${wait_s} s on $lock; it is still held by:" >&2
  holder
fi
exit "$rc"
