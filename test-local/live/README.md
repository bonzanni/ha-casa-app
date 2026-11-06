# test-local/live — live-box probe drivers (block S: WS-A delegation & fleet authz)

Drivers for running the **block S** probes against a live
Casa deployment. Unlike `../e2e/` (local docker containers), these drive
the production box over SSH; unlike `../audit/` (read-only), they drive real
turns. All probes are **agent-parameterized**: pass the role/target under test,
so the same harness verifies any future agent (a new voice resident, an MTG
specialist, an installed agent-repo agent) — onboard the agent, run the
harness, its ACL/session/channel/requires invariants are proven.

## Files

- `wsa_probe.py` — in-container driver (`docker cp` to `/tmp`, then
  `docker exec … python3 /tmp/wsa_probe.py <cmd>`). Signed SSE/WS/invoke/
  telegram drives + sessions.json inspection + expected-key computation.
  Every command takes `--role/--agent/--scope` parameters.
- `provision_probe_agents.sh` — create/remove the throwaway probe pair
  (`probe-gw` ha_voice resident with a deterministic RAWCALL prompt +
  `probe-spec` delegation-only specialist), plus `add-requires`/`drop-requires`
  to flip the A5 fail-closed gate. Role names are flags.
- `agents/` — the fixture templates (`__GW_ROLE__`/`__SPEC_ROLE__`/
  `__GHOST_ROLE__` tokens substituted at provision time).

## Probe recipes

Quick start (set your own host/container/chat-id — nothing is baked
in):

```bash
# HOST = ssh alias for the HA host; CONTAINER = casa container name;
# CASA_PROBE_TG_USER = the operator Telegram chat id for synthetic DMs.
export HOST=<your-ha-ssh-alias>
export CONTAINER=addon_<slug>_casa
export CASA_PROBE_TG_USER=<your-telegram-chat-id>
D="ssh $HOST -- sudo -n docker exec $CONTAINER"

# deploy driver (pipe it in — no scp needed)
ssh "$HOST" -- "sudo -n docker exec -i $CONTAINER tee /tmp/wsa_probe.py >/dev/null" < wsa_probe.py

# A3: capability 404 parity (no throwaway agents needed)
$D python3 /tmp/wsa_probe.py post --path /api/converse --body '{"prompt":"x","agent_role":"assistant"}'

# onboard the throwaway pair, run the delegation probes, tear down
./provision_probe_agents.sh create
$D python3 /tmp/wsa_probe.py converse --role probe-gw --scope wsa-s1 \
   --prompt 'RAWCALL {"agent":"assistant","task":"PING","context":"","mode":"sync"}'
./provision_probe_agents.sh remove
```

The RAWCALL contract (see `agents/probe-gw/prompts/system.md`) makes the
gateway relay exact `delegate_to_agent` arguments and echo the raw tool
result, so typed-error assertions (`delegation_not_declared`,
`mode_unsupported_on_voice`, `deadline_exceeded`, `dependency_unavailable`,
`busy`, `input_too_large`) are observable in the SSE stream; cross-check
against container logs (`specialist_telemetry_*`, `Delegation <id> → <role>`).
