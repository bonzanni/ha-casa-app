#!/usr/bin/env bash
# Plan 4b/3.6 — mid-restart survival e2e test.
#
# Validates the headline guarantee of v0.14.0: bouncing casa-main
# (s6-rc -d/-u change svc-casa) does NOT take svc-casa-mcp down or sever
# the tool-call surface. Mid-restart tool calls return JSON-RPC
# -32000 casa_temporarily_unavailable; post-restart calls succeed.
#
# WHICH TOOL THE PROBE CALLS, AND WHY (#585)
# ------------------------------------------
# Every call below is UNBOUND — it carries no X-Casa-Engagement-Id/-Token
# pair, because this probe provisions no engagement. Since v0.166.0 the
# bridge grant-gate fails closed for an unbound caller
# (`engagement_casa_grant_names(None)` returns an empty set, tools.py), so
# `list_engagement_workspaces` — what M-2 used to call — is refused -32004
# before it runs. That refusal is CORRECT: a real /mcp/casa-framework tool
# call comes from an engagement workspace, whose .mcp.json bakes the header
# pair (workspace.py), so no legitimate NON-TERMINAL tool call arrives on
# this route unbound. (Unbound completion retries do — that is the
# exemption below. Other routes on 8100 authenticate differently: the
# /internal/channel/* family carries its token in the body.) The probe
# simply predated the gate, and had been failing every scheduled run since
# 2026-08-09.
#
# The round-trip steps therefore call `emit_completion`, the one tool the
# gate deliberately exempts (_TERMINAL_BINDING_TOOLS, for completion
# retries that legitimately arrive unbound). With no engagement it returns
# a dispatched `not_in_engagement` result and touches nothing else
# (tools.py — it returns before validation, registry access or
# finalization), so M-2/M-6 still prove the WHOLE path: forwarder → Unix
# socket → casa-main's handler → `await fn(arguments)` → response. Pinned
# unit-side by test_tools_call_unbound_emit_completion_still_dispatches.
#
# M-2b/M-7 pin the gate itself, so the behaviour that broke this probe is
# now asserted rather than merely worked around — and M-7 additionally
# shows a casa-main restart does not reopen it.
#
# THE ENGAGEMENT-SIDE BLOCK, M-8..M-11 (#586)
# -------------------------------------------
# M-1..M-7 make only UNBOUND calls, so they cannot see the path a real
# engagement uses: no credential pair, no granted non-terminal tool, no CLI
# subprocess. M-8..M-11 provision ONE real `claude_code` engagement through
# production code (real workspace, real `.mcp.json` credential, real s6
# service, real mock-CLI subprocess under its own dropped uid) and have THAT
# process make the calls.
#
# What each step proves, and what it does not:
#
#   M-9  the delivery seam confers authority. casa-main reloads the record
#        (boot reconcile rewrites `active`->`idle`), boot replay respawns the
#        CLI, the inbound spool redelivers the queued turn, and the turn's
#        authenticated call to a GRANTED non-terminal tool DISPATCHES. Before
#        #588 the record stayed `idle` and every such call was refused
#        -32004, blaming a grant the engagement holds. The step asserts the
#        boot-reconcile log line too: without it the step could be green
#        having never exercised the defect at all.
#   M-10 the same call on an ordinarily-`active` record dispatches.
#   M-11 the same call, after a bounce that redelivers nothing, is refused
#        -32006 engagement_not_live (#587). "Redelivers nothing" is ARRANGED,
#        not assumed (#614): the step drains the spool BEFORE the bounce, and
#        after it requires both an empty spool AND a record `idle` on disk.
#        All three fail as SETUP, distinctly from the product assertion —
#        because the state this step needs is one the system does not otherwise
#        guarantee, and a step that cannot reach it must say so rather than
#        blame the code it is pointed at.
#
#        The post-bounce spool check alone is NOT enough, and a mutation check
#        proved it: a redelivery that lands and COMPLETES before the first poll
#        re-empties the spool while the record stays `active`. That is why the
#        status is read too, and why it is read asymmetrically (see M-11).
#
# M-10 and M-11 inject their turn STRAIGHT INTO the control FIFO. That is a
# turn delivery, it just bypasses `_write_to_fifo` — so neither step says
# anything about the delivery seam, and neither is claimed to. Held constant
# across the pair, the injection makes the record's liveness the only
# variable: live dispatches, not-live is refused, and the refusal names the
# real reason. The ORDERING guarantee inside the seam (the record is `active`
# before the CLI can see the first byte) is not provable here at all — an
# e2e cannot make that race deterministic — and is pinned instead by the unit
# red cases in tests/test_claude_code_driver.py::TestTurnDeliveryAdmission.
#
# The result assertion is identity-bearing: `list_engagement_workspaces`
# filters its listing to the CALLER's own engagement when a record is bound
# (tools.py), so "exactly one workspace, and it is ours" cannot be produced
# by an unbound dispatch, by the forwarder, or by a stub.
#
# M-11 carries that property in the OTHER direction too (#614). Identifying the
# turn by "the response-file set changed" is satisfied by a turn Casa
# redelivered, so M-11 stamps a nonce on its own call and asserts on the
# response bearing it. Its refusal is then unambiguously an answer to the turn
# it injected, not to somebody else's.
#
# Mock-CLI gated (CASA_USE_MOCK_CLAUDE=1). Auto-skips otherwise.

set -euo pipefail

if [ "${CASA_USE_MOCK_CLAUDE:-0}" != "1" ]; then
    echo "SKIP: CASA_USE_MOCK_CLAUDE=1 required"
    exit 0
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/common.sh"

build_image_with_mock_cli

NAME="casa-mcp-restart-$$"
start_container "$NAME"
trap "stop_container '$NAME' >/dev/null 2>&1 || true" EXIT

wait_healthy "$NAME"

echo "=== M-1: confirm svc-casa-mcp is bound on 8100 ==="
RESP=$(MSYS_NO_PATHCONV=1 docker exec "$NAME" curl -fsSL -X POST \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' \
    http://127.0.0.1:8100/mcp/casa-framework)
echo "  initialize: $RESP"
echo "$RESP" | python3 -c 'import json, sys; d = json.load(sys.stdin); assert d.get("result", {}).get("serverInfo", {}).get("name") == "casa-framework", d' \
    || fail "M-1 svc-casa-mcp not responding on 8100"
pass "M-1 svc-casa-mcp bound on 8100"

# Assert a tools/call response is a DISPATCHED emit_completion result — not
# merely a JSON-RPC reply. `.result` alone would be satisfied by anything the
# forwarder itself can answer, so this decodes the content block and checks
# the tool's own payload: only casa-main running the handler produces it.
assert_dispatched() {
    local label="$1" resp="$2"
    echo "$resp" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d.get("result") is not None, d
payload = json.loads(d["result"]["content"][0]["text"])
assert payload.get("kind") == "not_in_engagement", payload
' || fail "$label expected a dispatched emit_completion result; got: $resp"
}

# Assert the grant-gate refused an unbound non-terminal tool (#585).
assert_gated() {
    local label="$1" resp="$2"
    echo "$resp" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d.get("error", {}).get("code") == -32004, d
assert "tool_not_granted" in d["error"]["message"], d
' || fail "$label expected -32004 tool_not_granted; got: $resp"
}

call_tool() {   # call_tool <id> <tool-name>
    MSYS_NO_PATHCONV=1 docker exec "$NAME" curl -fsSL -X POST \
        -H "Content-Type: application/json" \
        -d "{\"jsonrpc\":\"2.0\",\"id\":$1,\"method\":\"tools/call\",\"params\":{\"name\":\"$2\",\"arguments\":{}}}" \
        http://127.0.0.1:8100/mcp/casa-framework
}

echo "=== M-2: tool call before bounce — expect a dispatched result ==="
PRE=$(call_tool 2 emit_completion)
echo "  pre-bounce: $PRE"
assert_dispatched "M-2" "$PRE"
pass "M-2 pre-bounce tool call dispatched in casa-main"

echo "=== M-2b: unbound non-terminal tool — expect the grant-gate refusal ==="
GATED=$(call_tool 21 list_engagement_workspaces)
echo "  unbound: $GATED"
assert_gated "M-2b" "$GATED"
pass "M-2b unbound non-terminal tool refused -32004 (gate live)"

echo "=== M-3: bounce casa-main (svc-casa down) ==="
MSYS_NO_PATHCONV=1 docker exec "$NAME" s6-rc -d change svc-casa
sleep 1

echo "=== M-4: tool call during bounce — expect -32000 ==="
DOWN=$(call_tool 3 emit_completion)
echo "  during-bounce: $DOWN"
echo "$DOWN" | python3 -c 'import json, sys; d = json.load(sys.stdin); assert d.get("error", {}).get("code") == -32000, d' \
    || fail "M-4 expected error.code -32000; got: $DOWN"
pass "M-4 mid-bounce returned casa_temporarily_unavailable"

echo "=== M-5: bring casa-main back up + wait for socket ==="
MSYS_NO_PATHCONV=1 docker exec "$NAME" s6-rc -u change svc-casa
# Poll for casa-main's Unix socket to come back (up to 12s).
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if MSYS_NO_PATHCONV=1 docker exec "$NAME" test -S /run/casa/internal.sock; then
        echo "  socket ready after ${i}s"
        break
    fi
    sleep 1
done

echo "=== M-6: tool call after bounce — expect a dispatched result ==="
POST=$(call_tool 4 emit_completion)
echo "  post-bounce: $POST"
assert_dispatched "M-6" "$POST"
pass "M-6 post-bounce tool call dispatched in casa-main"

echo "=== M-7: gate still closed after the restart ==="
POST_GATED=$(call_tool 41 list_engagement_workspaces)
echo "  post-bounce unbound: $POST_GATED"
assert_gated "M-7" "$POST_GATED"
pass "M-7 restart did not reopen the grant-gate"

# ---------------------------------------------------------------------------
# M-8..M-11 — the engagement side (#586). See the header for what each proves.
# ---------------------------------------------------------------------------

in_c() { MSYS_NO_PATHCONV=1 docker exec "$NAME" "$@"; }

# Response files the mock CLI writes, newest last. They are dotfiles in the
# uid-owned workspace, so `ls -a`.
casa_call_files() {
    in_c sh -c "ls -a /data/engagements/$EID/ 2>/dev/null | grep '^\.mock_casa_call\.' | sort" \
        2>/dev/null || true
}

# Wait for a response file NEWER than the ones already present, and echo it.
# `prev` is the output of an earlier casa_call_files.
wait_for_new_casa_call() {
    local prev="$1" timeout="${2:-60}" now newest
    local end=$(( $(date +%s) + timeout ))
    while [ "$(date +%s)" -lt "$end" ]; do
        now="$(casa_call_files)"
        if [ "$now" != "$prev" ]; then
            newest="$(printf '%s\n' "$now" | tail -1)"
            [ -n "$newest" ] && { printf '%s\n' "$newest"; return 0; }
        fi
        sleep 1
    done
    return 1
}

# Wait for the response file carrying `_probe_token == $2`, and echo it (#614).
# Unlike wait_for_new_casa_call, a turn Casa REDELIVERED cannot satisfy this:
# only the turn the caller injected carries the nonce.
wait_for_token_casa_call() {
    local token="$1" timeout="${2:-30}" f
    local end=$(( $(date +%s) + timeout ))
    while [ "$(date +%s)" -lt "$end" ]; do
        for f in $(casa_call_files); do
            if in_c sh -c "cat /data/engagements/$EID/$f 2>/dev/null" \
                | TOKEN="$token" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("_probe_token") == os.environ["TOKEN"] else 1)
' 2>/dev/null; then
                printf '%s\n' "$f"; return 0
            fi
        done
        sleep 1
    done
    return 1
}

# Echo the engagement's blocking inbound envelopes — those in state `queued` or
# `delivered`, the two that `_InboundSpool.on_spawn` redelivers on the next CLI
# spawn. "" means the spool holds nothing that a bounce could redeliver.
#
# FAIL-CLOSED (Sol r1 Q3): a spool that cannot be read or parsed echoes
# `UNREADABLE`, never "". A read that could not happen returns the same
# emptiness as a read that found nothing, and only one of them means the
# premise holds. The file itself is written temp+fsync+os.replace
# (claude_code_driver.py `_persist`), so a torn read is not expected — this is
# belt-and-braces, not an observed failure.
blocking_envelopes() {
    local spool="/data/engagement-ctl/$EID/.inbound_spool.jsonl" raw
    # Absent is UNREADABLE, not empty: by this point M-8 has enqueued through
    # the spool, and `_prune` rewrites the file empty rather than removing it,
    # so a missing file is an anomaly and must not read as "nothing pending".
    if ! in_c test -f "$spool" >/dev/null 2>&1; then
        printf 'UNREADABLE\n'; return 0
    fi
    raw="$(in_c sh -c "cat '$spool'" 2>/dev/null)" || {
        printf 'UNREADABLE\n'; return 0
    }
    printf '%s\n' "$raw" | python3 -c '
import json, sys
out = []
for ln in sys.stdin:
    ln = ln.strip()
    if not ln:
        continue
    try:
        d = json.loads(ln)
    except Exception:
        print("UNREADABLE"); sys.exit(0)
    # Mirror the product: `_Envelope.from_line` defaults a MISSING state to
    # "queued", so a row without one is redeliverable and must read as blocking
    # here too — treating it as nonblocking would be a fail-OPEN in the one
    # helper whose job is to fail closed. Any state this harness does not
    # recognise is UNREADABLE for the same reason: an unclassifiable row is not
    # an absent one.
    st = d.get("state", "queued")
    if st not in ("queued", "delivered", "consumed"):
        print("UNREADABLE"); sys.exit(0)
    if st in ("queued", "delivered"):
        out.append("%s/seq%s" % (st, d.get("seq")))
print(",".join(out))
' 2>/dev/null || printf 'UNREADABLE\n'
}

# The engagement is bound => the listing is filtered to its OWN workspace.
assert_own_workspace() {
    local label="$1" file="$2"
    in_c sh -c "cat /data/engagements/$EID/$file" | EID="$EID" python3 -c '
import json, os, sys
d = json.load(sys.stdin)
assert "error" not in d, d
payload = json.loads(d["result"]["content"][0]["text"])
ws = payload["workspaces"]
assert len(ws) == 1, f"expected exactly the caller own workspace, got {ws}"
assert ws[0]["engagement_id"] == os.environ["EID"], ws
' || {
        in_c sh -c "cat /data/engagements/$EID/$file" >&2
        fail "$label expected a dispatched result listing only this engagement"
    }
}

echo "=== M-8: provision a real engagement with a turn queued for it ==="
# casa-main goes down FIRST: the harness below is the only live UidAllocator on
# the counter file while it runs, and casa-main cannot rewrite
# /data/engagements.json from its own in-memory state over the new record.
MSYS_NO_PATHCONV=1 docker exec "$NAME" s6-rc -d change svc-casa
sleep 1

read -r -d '' PROVISION_PY <<'PY' || true
"""Provision ONE claude_code engagement, then leave a turn queued with its
service down. Everything happens in a SINGLE process: a second process would
load() the tombstone again, and load() is what performs the `active -> idle`
boot reconcile — which is casa-main's job to do, and log, when it comes up."""
import asyncio
import json
import subprocess
import sys

sys.path.insert(0, "/opt/casa")

MCP_URL = "http://127.0.0.1:8100/mcp/casa-framework"
GRANT = "mcp__casa-framework__list_engagement_workspaces"


async def main():
    import casa_core
    import private_state
    from engagement_registry import EngagementRegistry
    from engagement_uids import UID_BASE, UidAllocator
    from executor_registry import ExecutorRegistry
    from drivers.claude_code_driver import ClaudeCodeDriver
    from drivers.workspace import inbound_spool_path

    # casa-main does this at boot before replaying anything: the allocator
    # refuses to hand a uid to a dropped engagement while credential-class
    # paths are readable beyond root. This harness stands in for casa-main, so
    # it runs the same repair rather than racing how far boot got.
    private_state.enforce()

    alloc = UidAllocator("/data/engagement-uids.json")
    reg = EngagementRegistry(
        tombstone_path="/data/engagements.json", bus=None, uid_allocator=alloc)
    await reg.load()
    known, dir_owners = casa_core._gather_reconstruct_evidence(
        reg, data_dir="/data")
    alloc.reconstruct(known, dir_owners)

    # The definition boot replay will re-derive this engagement from, VERBATIM:
    # a hand-built fixture would render a different settings floor and could be
    # refused at resume for a reason that has nothing to do with this probe.
    exec_reg = ExecutorRegistry("/config/agents/executors")
    exec_reg.load()
    defn = exec_reg.definition_any("plugin-developer")
    assert defn is not None, f"no definition; have {exec_reg.list_types_any()}"
    assert defn.driver == "claude_code", defn.driver

    rec = await reg.create(
        kind="executor", role_or_type="plugin-developer", driver="claude_code",
        task="restart-survival probe", topic_id=None,
        origin={"channel": "telegram", "chat_id": "1"},
        tools_allowed=(GRANT,),
    )
    # Fail as SETUP, loudly: without an injected + reconstructed allocator the
    # record keeps the sentinel uid and start() refuses at the uid preflight,
    # which would otherwise surface much later as an unexplained empty probe.
    assert rec.allocated_uid >= UID_BASE, (
        f"no uid allocated ({rec.allocated_uid})")

    async def _noop(*_a, **_kw):
        return None

    drv = ClaudeCodeDriver(
        engagements_root="/data/engagements", send_to_topic=_noop,
        casa_framework_mcp_url=MCP_URL, registry=reg,
        executor_defn_lookup=exec_reg.definition_any,
    )
    await drv.start(rec, prompt="probe launch", options=defn)
    await asyncio.sleep(3.0)

    # Take the CLI away, so the turn below finds no reader and stays `queued`
    # for casa-main to redeliver. (It is also a live check that no admission
    # happens without a reader: the write below times out.)
    subprocess.run(["s6-rc", "-d", "change", f"engagement-{rec.id}"],
                   capture_output=True, text=True)
    await asyncio.sleep(1.0)

    disposition = await drv._inbound[rec.id].enqueue(
        "/mock casa_call list_engagement_workspaces")
    assert disposition == "queued", disposition

    states = [json.loads(ln)["state"]
              for ln in open(inbound_spool_path(rec.id)) if ln.strip()]
    assert "queued" in states, f"nothing left to redeliver: {states}"

    on_disk = {r["id"]: r["status"]
               for r in json.load(open("/data/engagements.json"))}
    assert on_disk[rec.id] == "active", (
        "the record must reach casa-main as 'active' so ITS load() performs "
        f"the boot reconcile: {on_disk[rec.id]}")

    print("ENGAGEMENT_ID=" + rec.id)
    print("OK")

asyncio.run(main())
PY

PROV_TMP="$(mktemp)"
printf '%s\n' "$PROVISION_PY" > "$PROV_TMP"
MSYS_NO_PATHCONV=1 docker cp "$PROV_TMP" "$NAME:/tmp/_provision.py" >/dev/null
rm -f "$PROV_TMP"
# The enqueue deliberately waits out the no-reader deadline (~20s).
if ! PROV_OUT=$(MSYS_NO_PATHCONV=1 docker exec "$NAME" \
        /opt/casa/venv/bin/python /tmp/_provision.py 2>&1); then
    printf '%s\n' "$PROV_OUT" >&2
    fail "M-8 provisioning harness exited non-zero"
fi
printf '%s\n' "$PROV_OUT" | tail -5 >&2
EID="$(printf '%s\n' "$PROV_OUT" | sed -n 's/^ENGAGEMENT_ID=//p')"
[ -n "$EID" ] || fail "M-8 harness printed no engagement id"
pass "M-8 provisioned engagement ${EID:0:8} with a turn queued"

echo "=== M-9: casa-main back up — the redelivered turn must carry authority ==="
BEFORE_M9="$(casa_call_files)"
MSYS_NO_PATHCONV=1 docker exec "$NAME" s6-rc -u change svc-casa
for i in $(seq 1 20); do
    if MSYS_NO_PATHCONV=1 docker exec "$NAME" test -S /run/casa/internal.sock; then
        echo "  socket ready after ${i}s"
        break
    fi
    sleep 1
done

# The record must actually have BEEN idled — otherwise this step could pass
# without ever exercising the defect.
if ! wait_for_text_in_log "$NAME" "boot reconcile: engagement ${EID:0:8}" 30; then
    fail "M-9 casa-main never reconciled ${EID:0:8} active->idle; nothing was proved"
fi
pass "M-9a boot reconcile idled the record (the state the defect needs)"

# A turn redelivered before casa-main's internal socket is serving gets the
# designed retryable -32000, so wait for a DISPATCHED answer rather than
# asserting on the first file to appear. Redelivery is at-least-once by
# construction (an envelope clears only on positive turn_start evidence), so
# there may legitimately be more than one.
M9_FILE="$(wait_for_new_casa_call "$BEFORE_M9" 60)" \
    || fail "M-9 the redelivered turn produced no MCP call at all"
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if in_c sh -c "cat /data/engagements/$EID/$M9_FILE" | grep -q '"result"'; then
        break
    fi
    sleep 2
    M9_FILE="$(casa_call_files | tail -1)"
done
echo "  m-9 response file: $M9_FILE"
assert_own_workspace "M-9" "$M9_FILE"
pass "M-9 redelivered turn dispatched a granted non-terminal tool as itself"

echo "=== M-10: the same call on a live record ==="
BEFORE_M10="$(casa_call_files)"
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
    "printf '/mock casa_call list_engagement_workspaces\n' > /data/engagement-ctl/$EID/stdin.fifo"
M10_FILE="$(wait_for_new_casa_call "$BEFORE_M10" 30)" \
    || fail "M-10 the injected turn produced no MCP call"
assert_own_workspace "M-10" "$M10_FILE"
pass "M-10 authenticated granted call from the engagement dispatches"

echo "=== M-11: same call, record not live — refused as NOT LIVE, not ungranted ==="
# #614 — M-11's premise is that this bounce redelivers NOTHING, and that is not
# a property of the bounce. Boot replay's down-first sweep respawns the CLI on
# every casa-main restart; `_InboundSpool.on_spawn` reverts any envelope still
# `delivered` to `queued` and pumps it; and that write goes through
# `_write_to_fifo`, whose synchronous `begin_turn_delivery` correctly flips a
# boot-reconciled `idle` record straight back to `active` (#588). So the premise
# holds only while the spool is empty ACROSS the bounce — which the step used to
# assume, and lost the race on in CI.
#
# Drained BEFORE the bounce, not after. Nothing re-idles a record except boot
# reconcile, so once a redelivery has re-armed it, waiting afterwards can only
# ever time out. The envelope being waited on is M-8's, redelivered at M-9 —
# M-10 wrote straight to the FIFO and created none.
#
# Waited on, not deleted: draining by hand would make the step hermetic and
# erase the at-least-once redelivery this block exists to exercise.
echo "  M-11 setup: waiting for the spool to hold nothing redeliverable"
M11_DRAIN_END=$(( $(date +%s) + 30 ))
while :; do
    M11_PENDING="$(blocking_envelopes)"
    [ -z "$M11_PENDING" ] && break
    if [ "$(date +%s)" -ge "$M11_DRAIN_END" ]; then
        fail "M-11 SETUP: the M-8 envelope never cleared (spool still \
'$M11_PENDING' after 30s) — either turn_start evidence is not reaching the \
spool or the relay is merely slow. The step did NOT reach the state it tests; \
this is not a product failure."
    fi
    sleep 1
done

MSYS_NO_PATHCONV=1 docker exec "$NAME" s6-rc -d change svc-casa
sleep 2
MSYS_NO_PATHCONV=1 docker exec "$NAME" s6-rc -u change svc-casa
for i in $(seq 1 20); do
    if MSYS_NO_PATHCONV=1 docker exec "$NAME" test -S /run/casa/internal.sock; then
        break
    fi
    sleep 1
done

# Confirm the premise survived the bounce — a condition, not the flat `sleep 8`
# this replaces. With an empty spool there is nothing for `on_spawn` to pump, so
# the only two callers that can re-arm a record (`begin_turn_delivery`, reached
# only through `_write_to_fifo`; and `update_user_turn`, reached only from an
# inbound Telegram message) are both unreachable here.
M11_CONFIRM_END=$(( $(date +%s) + 30 ))
while :; do
    M11_PENDING="$(blocking_envelopes)"
    [ -z "$M11_PENDING" ] && break
    if [ "$(date +%s)" -ge "$M11_CONFIRM_END" ]; then
        fail "M-11 SETUP: the bounce carried a redelivery (spool '$M11_PENDING') \
— the record is live by design and the refusal under test is unreachable."
    fi
    sleep 1
done

# The spool check above is NECESSARY but not SUFFICIENT, and a mutation check
# proved it: a redelivery that lands and COMPLETES before the first poll leaves
# the spool empty again while the record stays `active`, so the confirm passed
# and the step still reported a correct dispatch as a product failure.
#
# The record's on-disk status closes that, but only in one direction. Read
# ASYMMETRICALLY:
#
#   `active`     ⇒ SETUP failure. Sound: after boot reconcile writes `idle`,
#                  the only thing that writes `active` back is
#                  `begin_turn_delivery`, so disk-`active` implies the record
#                  the gate binds on is active.
#   `idle`       ⇒ proves NOTHING on its own. `begin_turn_delivery` persists
#                  through a fire-and-forget task (`_schedule_tombstone_persist`),
#                  so disk can read `idle` while memory is already `active`.
#                  What makes `idle` trustworthy HERE is the drained spool: with
#                  nothing to pump, `begin_turn_delivery` is unreachable.
#   anything else ⇒ SETUP failure (fail closed; an unreadable status is not an
#                  idle one).
#
# So neither signal is load-bearing alone: the pre-bounce drain makes the state
# unreachable, and this check refuses to proceed if it happened anyway.
M11_STATUS="$(in_c sh -c 'cat /data/engagements.json 2>/dev/null' \
    | EID="$EID" python3 -c '
import json, os, sys
try:
    print({r["id"]: r["status"] for r in json.load(sys.stdin)}.get(
        os.environ["EID"], "ABSENT"))
except Exception:
    print("UNREADABLE")
' 2>/dev/null || printf 'UNREADABLE\n')"
if [ "$M11_STATUS" != "idle" ]; then
    fail "M-11 SETUP: record status is '$M11_STATUS', not 'idle' — the bounce \
did not leave it dormant (a redelivery re-armed it, or the status is \
unreadable). The refusal under test is unreachable and this is NOT a product \
failure."
fi
echo "  M-11 setup ok: spool clear across the bounce, record idle"

# The nonce is what makes this assertion read ITS OWN turn. Waiting for the
# response-file SET to change — what this step used to do — is satisfied just as
# well by a turn Casa redelivered, and that is how a correct dispatch came to be
# read as M-11's answer.
M11_TOKEN="m11-$$-$(date +%s)"
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
    "printf '/mock casa_call list_engagement_workspaces $M11_TOKEN\n' > /data/engagement-ctl/$EID/stdin.fifo"
M11_FILE="$(wait_for_token_casa_call "$M11_TOKEN" 30)" \
    || fail "M-11 the injected turn produced no MCP call carrying $M11_TOKEN"
in_c sh -c "cat /data/engagements/$EID/$M11_FILE" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d.get("error", {}).get("code") == -32006, d
assert "engagement_not_live" in d["error"]["message"], d
assert "tool_not_granted" not in d["error"]["message"], d
' || {
    in_c sh -c "cat /data/engagements/$EID/$M11_FILE" >&2
    fail "M-11 expected -32006 engagement_not_live"
}
pass "M-11 a record with no delivered turn is refused as not live"

echo "=== ALL PASS — mcp_restart_survival ==="
