# MCP blip spike

**Purpose:** resolve §10.2 of the Plan 4a design spec — does Claude Code's MCP
HTTP client retry `tools/call` after a mid-call connection loss?

**Location:** run ON the HA host (real `claude` CLI needed). Not CI-gated.

## Run

```bash
ssh <ha-host> -t 'cd /tmp && rm -rf mcp_blip && git clone ... && \
  bash mcp_blip/run.sh'
```

## Interpretation

- `RETRY OBSERVED` printed in server stdout → **pessimistic client**. Casa
  restart mid-`emit_completion` is safe because the handler is already
  idempotent on `engagement_id`. No 3.6 work needed for this ship.
- No `RETRY OBSERVED` within 30s → **optimistic client**. File a new
  ROADMAP item: either promote 3.6 to Plan 4b co-requisite (extract
  casa-framework MCP to its own s6 service) OR ship a reconciler that
  notices stuck engagements on the Casa side.

## Result

(Fill in after running the spike. Paste 5–10 lines of server stdout. State
which of the two branches above fires. Feed the result into CHANGELOG
[0.13.1] and ROADMAP.)
