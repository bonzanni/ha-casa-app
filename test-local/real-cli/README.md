# Real-CLI gate (S5 stored-call buttons)

The e2e harness swaps the Claude Agent SDK for an offline mock. This directory is the
opposite: it runs the **real** `claude_agent_sdk` and its bundled CLI against a fixture
MCP server, to measure what the CLI does to a tool call's arguments between the model,
the hooks and the server. The S5 design (`operator_proposal` stored-call buttons) makes
these measurements a precondition of the build.

- `fixture_server.py` — a stdlib stdio MCP server with one tool, `probe`, that records every
  argument object it receives to the file named by `GATE_EVIDENCE`.
- `run_gate.py` — drives five sessions (normalisation; a denied call; an `updatedInput`
  rewrite; a nonce-only rewrite; Casa's real pinned-turn hook set with two PreToolUse
  matchers on one plugin-named tool, called twice) and judges each.

## Running it

The credential is read from **one environment variable at run time**
(`CLAUDE_CODE_OAUTH_TOKEN`, as Casa's own service exports it). Supply it from outside this
tree — for example through `op run -- …` or an inline read from your secret manager — and
run from the repository root with the Linux venv:

```bash
venv_test/bin/python test-local/real-cli/run_gate.py
```

It prints PASS/FAIL per case and writes `evidence/gate-<timestamp>.json`. The `evidence/`
directory is ignored by git: the evidence file belongs with the private review record, not
in this repository. Nothing here names a vault, an item or a secret reference.

`GATE_MODEL` overrides the model (default: Haiku). The run costs a handful of short turns.
