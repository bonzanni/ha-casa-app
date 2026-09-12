# Local Testing

Test the Casa add-on locally in Docker. The real Claude Agent SDK is swapped
for an offline mock so runtime tests need no OAuth token.

## Quick start (tiered)

The `make -C test-local` targets mirror the CI tier structure (see
`.github/workflows/qa.yml`). Pick the tier that matches the level of
confidence you want before pushing:

```bash
make -C test-local test-tier1    # ~3 min: smoke + voice (is the system on fire?)
make -C test-local test-tier2    # ~10 min: tier 1 + unit pytest + 6 e2e + engagement E-block
make -C test-local test-tier3    # ~5-10 min: hardening — slow pytest + concurrency + MCP restart
make -C test-local test-all      # tier 1 → tier 2 → tier 3 sequentially
make -C test-local clean         # remove leftover containers
```

Tier ladder rule of thumb:

| Tier | When to run | What it asserts |
|---|---|---|
| `tier1` | Before every commit | Build, `/healthz`, dashboard, voice SSE + WS smoke. If this fails, nothing else matters. |
| `tier2` | Before every push | Functional contract — unit pytest + the engagement E-block + the rest of the e2e suite. The PR-equivalent. CI's `tier2-functional` additionally runs the MCP restart-survival harness (#937); this `make` target does not, so run `test-tier3` too when touching the driver. |
| `tier3` | Before risky merges + nightly | Timing-sensitive + chaos: concurrency under simulated SDK latency, MCP-server restart-survival mid-flight. Run this when touching anything in the engagement / driver / MCP path. |

Windows Git Bash typically ships without GNU Make. Either install it
(`scoop install make` or `choco install make`) or run the underlying
scripts directly — each is self-contained and builds the image on demand:

```bash
bash test-local/e2e/test_smoke.sh
bash test-local/e2e/test_invoke_sessions.sh
bash test-local/e2e/test_voice_sse.sh
```

### Which base your run used (#942)

`Dockerfile.test` mirrors `casa/Dockerfile`'s **floating** base tag rather than
pinning a digest (#937), and docker builds from whatever copy of that tag is
already in the local store. So a tier can report green against a base the
product has not shipped for months — measured, and it cost two harness runs.

- **The `make` targets resolve the tag on every build** (`--pull`). That is
  where the refresh lives, because CI never invokes `make` (see "Adding a new
  e2e test" below), so this flag can only ever fail a local build.
- **Offline, or during a registry outage or rate limit, `--pull` fails the
  build outright** even though the image is in your store. `DOCKER_PULL=` is
  the way back, and it builds exactly as it did before:

  ```bash
  make -C test-local test-tier1 DOCKER_PULL=
  make test-image DOCKER_PULL=
  ```

- **The scripts never pull.** `common.sh`'s `build_image` runs in CI as well as
  locally, and a mandatory registry resolution there would turn an upstream
  outage into a red push gate. Instead every build reports the base it got:

  ```
  [e2e] e2e base io.hass.version=2026.08.0 (freshness not checked here)
  ```

  That is a diagnostic, not a guarantee. It says which base you built on; it
  does not say that base is current. Nothing here — and nothing in
  `tests/test_build_from_parity.py`, which disclaims the same thing — compares
  either reference against the registry.
- **Two paths are not covered at all**: `make test-docker`
  (`tests/test_baseline_runtime_assert.py` builds `casa/Dockerfile` itself, and
  that build also runs in CI) and the by-hand live build below. Pull those by
  hand when the base matters.

## What the e2e suite covers

| Script | Tier | Covers |
|---|---|---|
| `e2e/test_smoke.sh` | 1 | build, `/healthz`, dashboard startup race |
| `e2e/test_voice_sse.sh` | 1 | SSE transport for `/api/converse` |
| `e2e/test_voice_ws.sh` | 1 | WebSocket transport for `/api/converse/ws` (needs host `pip install aiohttp`) |
| `e2e/test_invoke_sessions.sh` | 2 | per-invoke session isolation |
| `e2e/test_external_surface.sh` | 2 | external HTTP surface on port 18065 |
| `e2e/test_specialist_delegation.sh` | 2 | role-keyed specialist delegation |
| `e2e/test_scheduling.sh` | 2 | scheduled-task primitives |
| `e2e/test_engagement_E.sh` | 2 | E-0..E-10 — Tier-2 specialist + Configurator engagements (mock TG forum supergroup) |
| `e2e/test_concurrency.sh` | 3 | parallel `/invoke` with simulated SDK latency |
| `e2e/test_mcp_restart_survival.sh` | 2 + 3 | engagement TCP keep-alive across `svc-casa-mcp` restart (needs `CASA_USE_MOCK_CLAUDE=1`). The only enabled harness that performs a real `ClaudeCodeDriver.start`, so since #937 CI runs it in `tier2-functional` (the push gate) as well as in `tier3-hardening` (the scheduled copy `nightly-alarm` covers). |
| `e2e/test_engagement_D.sh` | 3 | D-1..D-12 — claude_code driver lifecycle on mock-CLI overlay (needs `CASA_USE_MOCK_CLAUDE=1`; CI step currently commented out, see "Deferred CI steps" below) |
| `e2e/test_engagement_P.sh` | 2 | P-1..P-9 — plugin-developer + Configurator install flow (needs `CASA_USE_MOCK_CLAUDE=1` + `CASA_PLAN_4B=1`; CI step currently commented out) |

`e2e/common.sh` provides shared helpers (`build_image`, `wait_healthy`,
`stop_container`, `assert_log_contains`, `start_mock_telegram_server`,
`build_image_with_mock_cli`).

## CI tier contract

The `qa.yml` workflow runs four parallel jobs gated by per-event `if:`
clauses. Same wallclock as the slowest tier — no `needs:` chaining.

| Event | tier1-smoke | tier2-functional + baseline-runtime | tier3-hardening |
|---|---|---|---|
| PR push | yes | yes | no |
| Master push | yes | yes | no |
| Nightly cron (04:00 UTC) | yes | no (already verified on master push) | yes |
| Manual `workflow_dispatch` | yes | yes | yes |

Two properties of that table are load-bearing and are pinned by
`tests/test_qa_mcp_restart_launch.py` rather than left to this prose:

- `tier2-functional` runs the MCP restart-survival harness, so every push and
  every PR performs one real `claude_code` launch. Until #937 the only enabled
  harness that launches one sat in `tier3-hardening`, which no push or PR runs
  — and a base-image drift that broke every launch on every released image
  (#925) was green in every push-time tier for three weeks.
- `tier3-hardening` keeps its own copy of that step. The step is COPIED, not
  moved: `nightly-alarm` is `needs: [tier1-smoke, tier3-hardening]`, so a move
  would take the harness out of the scheduled run and out of the alarm.

The harness exits 0 with a `SKIP:` line when `CASA_USE_MOCK_CLAUDE` is unset,
so the tier2 step greps its own log for the harness's terminal pass marker: a
dropped `env:` block, or any other early `exit 0`, turns the step red instead of
green-and-empty. The two e2e image builds also print the built image's
`io.hass.version` label, because `test-local/Dockerfile.test` now mirrors
`casa/Dockerfile`'s floating base rather than pinning a digest — the base a run
actually used is in that run's log, not in the tree.

What that check does NOT establish: it detects a broken launch, it does not
gate publication. `qa.yml` and `deploy.yml` both start on the same push to
`main` and neither waits for the other, so the harness reports alongside a
release that may already be published, not before it (#958 — an operator
decision). And it runs on `ubuntu-latest`, so it covers `amd64` only, while
`casa/config.yaml` publishes `aarch64` as well (#957). Both limits are
pre-existing and this change narrows neither; what changed is that a broken
launch now shows up on the push that causes it instead of in a scheduled run
weeks later.

Trigger tier 3 manually from any branch:

```bash
gh workflow run qa.yml --ref master
```

### Deferred CI steps

Two engagement steps are commented out in `qa.yml` pending the
v0.14.10 D/P-block sweep follow-up:

- `tier2-functional` → `Engagement P-block` (`test_engagement_P.sh`)
- `tier3-hardening` → `Engagement D-block` (`test_engagement_D.sh`)

The split scripts exist and parse cleanly. To re-enable, drop the
leading `# ` markers on the YAML block — the `env:` block + step body
are already in place. Both run locally today; the comment-out is a
CI-side decision deferred until the harness regressions tracked since
v0.14.8 are swept.

## Adding new tests

When adding a new e2e script, three wiring points must update in the
same PR (see `reference_casa_build_ci.md` in auto-memory for why each
matters):

1. The script itself in `test-local/e2e/test_<name>.sh`.
2. The corresponding `Makefile` target (`test-tier{1,2,3}`).
3. A named step in `.github/workflows/qa.yml` under the matching
   tier job. Note: CI does NOT invoke `make`; each step is enumerated
   individually so the GHA UI can show per-step status.

Pick the tier by what the test asserts:

- **Tier 1** — gross-breakage signal only. Boot, root paths, voice
  smoke. Cheap (≤ 1 min added).
- **Tier 2** — functional contract for a feature. Should fail if the
  feature is broken; should not fail on timing/load/chaos.
- **Tier 3** — explicitly exercises timing, concurrency, restart
  survival, or otherwise can be flaky on cold-cache runners. Will not
  block PRs.

When in doubt, default to tier 2.

## Manually running with real credentials

Copy `options.json.example` to `options.json` and fill in real tokens. The
file is gitignored. Build and run without the mock by using the production
Dockerfile directly:

```bash
docker build -f casa/Dockerfile -t casa-live .
docker run --rm -p 8080:8080 -v $(pwd)/test-local/options.json:/data/options.json casa-live
```

Endpoints:

- `http://localhost:8080/healthz` — aiohttp health check
- `http://localhost:8080/` — status dashboard
- `http://localhost:8080/terminal/` — ttyd web terminal (if enabled)

## Editing options

Edit `test-local/options.json` (untracked) to change agent names, tokens, etc.

## Legacy targets

The pre-tiering targets are kept for back-compat:

```bash
make -C test-local test          # smoke + runtime + voice
make -C test-local test-smoke    # build + /healthz + dashboard
make -C test-local test-runtime  # /invoke + concurrency
make -C test-local test-voice    # SSE + WS smoke
```

These map to a subset of the tier targets and don't include the
engagement E-block. Prefer `test-tier1` / `test-tier2` for new work.
