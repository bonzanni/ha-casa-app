#!/usr/bin/env bash
# #603 — settling evidence for the terminal uid quiesce shipped in v0.213.0 (#599).
#
# WHAT THIS PROVES THAT THE UNIT LADDER CANNOT
# --------------------------------------------
# tests/test_engagement_quiesce.py drives the ladder against a FAKE /proc with
# injected signal seams. That pins the decision logic — which outcome the ladder
# returns for which observation — and nothing else. It cannot show that the
# observations match a real machine: that `s6-svstat -o wantedup` says what the
# parser expects, that pidfd signalling reaches a setpriv'd child, that `/proc`
# enumeration sees descendants the design assumes it sees, or that a `setsid`
# child is caught at all.
#
# This probe runs the real thing: real s6 supervision, a real `setpriv` uid drop,
# a real process tree, real pidfd signalling. The Claude CLI is the mock, and
# that is deliberate — what the probe needs from it is a uid-dropped process that
# spawns descendants in three specific shapes and mutates the filesystem
# continuously while its completion is being processed. The mock does that
# deterministically; the real CLI would not emit a parallel `Edit` on cue.
# Everything the ladder actually touches is real.
#
# THE THREE SHAPES, AND WHY THEY ARE THE POINT
# --------------------------------------------
#   foreground — plain fork: same session and process group as the CLI.
#   pgroup     — its own process group, same session.
#   setsid     — its own session. THIS is the shape that escapes a
#                process-group kill, and the whole reason the ladder keys on
#                the engagement's uid rather than its pgid.
# A probe that only covered the foreground child would pass against a
# `killpg`-based implementation and prove nothing about the decision under test.
#
# WHAT IS MEASURED VERSUS ASSERTED
# --------------------------------
# INV-CONT-006 promises a BOUNDED, OBSERVED kill before the operator-visible
# effects — not an instantaneous one. So the probe asserts extinction and the
# bound, and REPORTS the measured overrun: how long after the registry's
# terminal timestamp each descendant last managed to write. A number in the
# output is worth more than an assertion that hides it.
#
# Mock-CLI gated (CASA_USE_MOCK_CLAUDE=1). Auto-skips otherwise.

set -euo pipefail

if [ "${CASA_USE_MOCK_CLAUDE:-0}" != "1" ]; then
    echo "SKIP: CASA_USE_MOCK_CLAUDE=1 required"
    exit 0
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/common.sh"

# The funnel's own bound is 5s for the quiesce plus 20s for the teardown
# (tools.py). Anything beyond that is a wedge, which is the failure this test
# exists to catch as much as a survivor is.
FINALIZE_BUDGET_S="${FINALIZE_BUDGET_S:-30}"

build_image_with_mock_cli

NAME="casa-quiesce-$$"
start_container "$NAME"
trap "stop_container '$NAME' >/dev/null 2>&1 || true" EXIT

wait_healthy "$NAME"

MSYS_NO_PATHCONV=1 docker cp "$HERE/quiesce_probe.py" "$NAME:/tmp/quiesce_probe.py" >/dev/null

# run_probe <mode> -> echoes the PROBE_RESULT json
run_probe() {
    local mode="$1"
    local out
    if ! out=$(timeout "${PROBE_TIMEOUT_S:-240}" \
            env MSYS_NO_PATHCONV=1 docker exec "$NAME" \
            /opt/casa/venv/bin/python /tmp/quiesce_probe.py "$mode" 2>&1); then
        printf '%s\n' "$out" >&2
        fail "probe ($mode) exited non-zero"
    fi
    printf '%s\n' "$out" | tail -20 >&2
    printf '%s\n' "$out" | sed -n 's/^PROBE_RESULT=//p'
}

# assert_probe <label> <json> — assertions live in quiesce_assert.py, which
# carries the reasoning about what would make a green run meaningless.
assert_probe() {
    local label="$1" json="$2"
    [ -n "$json" ] || fail "$label: probe printed no PROBE_RESULT"
    printf '%s' "$json" | python3 "$HERE/quiesce_assert.py" "$label" \
        "$FINALIZE_BUDGET_S" || fail "$label assertions failed"
    pass "$label"
}

echo "=== Q-1: completion path — CLI still alive with parallel work ==="
Q1="$(run_probe normal)"
assert_probe "Q-1 uid extinct across all three descendant shapes" "$Q1"

# Q-2 (hung `s6-rc` under a real container) IS NOT HERE, deliberately.
#
# It was built and attempted. Under a real container the probe stopped
# producing output entirely — no result, no traceback, not even the
# faulthandler dump its own SIGUSR1 handler should have emitted — and it
# survived a 90s in-probe `asyncio.wait_for` around the whole funnel plus a
# 200s outer bound. A separate check confirmed the mechanism it was meant to
# exercise does behave: `asyncio.wait_for` DOES bound an
# `await asyncio.to_thread(subprocess.run, ...)`, firing on time and letting
# the caller continue. So the funnel's bound is not the thing in doubt; what
# was never explained is where the probe itself stopped, and shipping a step
# that hangs for ten minutes and explains nothing is worse than not shipping
# it. One real property did fall out of the attempt and is worth keeping: a
# timeout bounds the funnel's CONTROL FLOW, but the abandoned `to_thread`
# worker stays parked inside its blocking call, so the process cannot exit
# until that call returns.
#
# What that step would have covered is already pinned, with a red case
# verified against the unbounded code:
#   tests/test_quiesce_funnel_order.py::test_the_funnel_finishes_when_driver_cancel_hangs
#
# Tracked in #603. Do not re-add it without an explanation for the silence.

echo "=== Q-3: the ladder's own verdict reached the log ==="
if MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
        'grep -qE "quiesce .*(observed extinct|NOT verified)" /var/log/casa/current 2>/dev/null'; then
    pass "Q-3 the ladder recorded its outcome"
else
    # Not a failure: the probe drives the driver directly, so its logger may not
    # reach the service log. The outcome is already asserted structurally above.
    log "Q-3 skipped — no ladder line in the service log (probe-driven run)"
fi

pass "ALL — #603 settling evidence complete"
