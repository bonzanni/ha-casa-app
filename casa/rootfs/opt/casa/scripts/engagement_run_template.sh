#!/command/with-contenv bash
# Casa claude_code engagement run script (v0.75.0 — interactive-engagements
# design §W1). bash REQUIRED: process substitution below.
set -e

unset TELEGRAM_BOT_TOKEN WEBHOOK_SECRET SUPERVISOR_TOKEN HASSIO_TOKEN \
      {EXTRA_UNSET}

export HOME="/data/engagements/{ID}/.home"
export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1
export MCP_TOOL_TIMEOUT=660000     # [D:§W5] must exceed the 585s ask client bound
# v0.131.0: CLI 2.1.219 changed the default nested-subagent spawn depth from
# 1 to 3. Engagements already deny Agent/Task outright (workspace.py settings
# guard); this pin preserves the pre-2.1.219 contract against config drift.
export CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1
{EXTRA_EXPORT}
cd "/data/engagements/{ID}"

# Task 4 (containment stage 2): every file below is root-only run-state and
# lives in the control dir, NOT the workspace this script `cd`s into — the
# engagement's own CLI is --add-dir'd into the workspace only, so it can never
# read, write, or symlink-redirect any of these.
CTL="/data/engagement-ctl/{ID}"

# v0.131.0: .session_id is written by casa-core, never by the engagement's own
# CLI. Accept only an exact UUID and pass it as a single =-joined argv token so
# a crafted value can never expand into extra CLI flags (mirrors
# claude-agent-sdk 0.2.121's resume hardening; the old idiom word-split the
# file's content unquoted into argv). Runs BEFORE the ringlog stderr redirect
# below so a rejection notice reaches the container log, not just the
# per-epoch stderr file. The `cat` guard keeps a check-then-read race (file
# deleted in between) from aborting the spawn under `set -e`.
RESUME_ARGS=()
if [ -f "$CTL/.session_id" ]; then
  SID="$(cat "$CTL/.session_id" 2>/dev/null || true)"
  if [[ "$SID" =~ ^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$ ]]; then
    RESUME_ARGS=("--resume=$SID")
  else
    echo "casa: engagement {ID_SHORT}: ignoring malformed .session_id; starting a fresh session" >&2
  fi
fi

# Per-spawn bounded stderr + stream-correlated epoch. UNIQUE file per epoch —
# NOT a mod-4 slot (Sol r5-B2): bash does not wait for the process-substitution
# consumer when the exec'd child exits, so a lingering ringlog for epoch E could
# still append/rotate a REUSED slot while epoch E+4 truncates it → mixed stderr.
# A unique `.stderr.<EPOCH>.log` filename means a lingering consumer only ever
# writes to ITS OWN epoch's file; disk stays bounded by pruning old epochs.
EPOCH=$(( $(cat "$CTL/.spawn_epoch" 2>/dev/null || echo 0) + 1 ))
printf '%s\n' "$EPOCH" > "$CTL/.spawn_epoch.tmp" && mv -f "$CTL/.spawn_epoch.tmp" "$CTL/.spawn_epoch"  # atomic publish (r8-B4)
STDERR_LOG="$CTL/.stderr.$EPOCH.log"
# SWEEP-prune every epoch file <= EPOCH-4 (r6-B2): a single exact `EPOCH-4` rm
# would leak on a crash-skipped spawn or a briefly-resurrected path; sweeping
# revisits all stale files each spawn, keeping the total bounded (~4 epochs).
# Safe against a lingering writer because ringlog holds its fd (no resurrection).
for _f in "$CTL"/.stderr.*.log; do
  _e=${_f#"$CTL"/.stderr.}; _e=${_e%.log}
  if [ "$_e" -le "$(( EPOCH - 4 ))" ] 2>/dev/null; then rm -f "$_f" "$_f.1"; fi
done
exec 2> >(/opt/casa/scripts/ringlog.sh "$STDERR_LOG" 65536 "$EPOCH")   # pass epoch for the stale fence (r7-B4)
printf '{"casa_control": "spawn", "epoch": %s}\n' "$EPOCH"   # NDJSON, pre-exec

exec <"$CTL/stdin.fifo"

exec claude --channels server:casa-engagement-channel \
            --print --verbose --output-format stream-json \
            "${RESUME_ARGS[@]}" \
            --permission-mode {PERMISSION_MODE} \
            {ADD_DIR_FLAGS} \
            {PLUGIN_DIR_FLAGS}
