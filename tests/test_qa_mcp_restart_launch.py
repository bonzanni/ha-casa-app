"""Guard: the push gate runs a real `claude_code` launch, and says so out loud.

`test-local/e2e/test_mcp_restart_survival.sh` is the only enabled harness in
`qa.yml` that performs a real `ClaudeCodeDriver.start` (compile the s6-rc
database, start the service, drive the MCP surface across a restart). Until
#937 it lived only in `tier3-hardening`, which runs on `schedule` and
`workflow_dispatch` and never on a push or a pull request — so a base-image
drift that broke every launch on every released image was green in every
push-time tier for three weeks.

Two properties are pinned here, and both are about workflow TEXT: this is a
pure-unit parse of `.github/workflows/qa.yml` and it neither runs docker nor
executes the harness.

1. **The step is on the push gate.** `tier2-functional` — which runs on push,
   pull_request and workflow_dispatch — invokes the harness exactly once, with
   its mock-CLI env block, unconditionally.
2. **It cannot pass silently.** The harness exits 0 with a `SKIP:` line when
   `CASA_USE_MOCK_CLAUDE` is not `1`, so a step that lost its `env:` block would
   be green while launching nothing. The step's run body therefore asserts, in
   the asserted-positive direction, that the harness reached its terminal pass
   marker — which fires on every run and so covers a dropped env block, an
   early `exit 0` anywhere in the harness, and any future silent-skip path.

The tier3 copy is retained, not moved: `nightly-alarm` (`needs:
[tier1-smoke, tier3-hardening]`) is the alarm for the scheduled run, and a move
would take the step out of the schedule entirely.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "qa.yml"

HARNESS = "test-local/e2e/test_mcp_restart_survival.sh"
MARKER = "=== ALL PASS — mcp_restart_survival ==="

# The tier2 step's run body, pinned verbatim. The body IS the guarantee — a
# bare `bash <harness>` is green on a silent skip — so it is compared as text
# rather than sampled for keywords.
TIER2_RUN = (
    "set -euo pipefail\n"
    f"bash {HARNESS} 2>&1 | tee /tmp/mcp-restart-survival.log\n"
    f"grep -Fxq -- '{MARKER}' /tmp/mcp-restart-survival.log"
)

# tier3's copy is unchanged from what it has always been.
TIER3_RUN = f"bash {HARNESS}"


def _workflow() -> dict:
    # BaseLoader keeps every scalar a string: `on:` stays the key "on" rather
    # than being resolved to the boolean True, and `CASA_USE_MOCK_CLAUDE: "1"`
    # stays "1" rather than becoming an int under a laxer loader.
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _harness_steps(job: dict) -> list[dict]:
    """Steps that actually INVOKE the harness.

    Selected by the path appearing in `run`, never by step name: a commented-out
    block is not a step at all after parsing, and a name can say anything.
    """
    return [s for s in job.get("steps", []) if HARNESS in (s.get("run") or "")]


def test_tier2_runs_mcp_restart_and_requires_completion() -> None:
    wf = _workflow()
    triggers = wf["on"]
    assert "push" in triggers and "pull_request" in triggers, sorted(triggers)

    tier2 = wf["jobs"]["tier2-functional"]
    condition = tier2["if"]
    for event in ("push", "pull_request", "workflow_dispatch"):
        assert f"'{event}'" in condition, (
            f"tier2-functional does not run on {event}: {condition!r}")

    steps = _harness_steps(tier2)
    assert len(steps) == 1, (
        f"tier2 harness count={len(steps)}, expected 1 — the only real "
        f"claude_code launch in the workflow must run on the push gate")
    step = steps[0]

    assert step.get("env", {}).get("CASA_USE_MOCK_CLAUDE") == "1", (
        f"step must keep its mock-CLI env block, got {step.get('env')!r}")
    assert "if" not in step, f"the step must be unconditional, got {step['if']!r}"
    assert "continue-on-error" not in step, "step must not mask its own failure"
    assert "continue-on-error" not in tier2, "job must not mask a step failure"
    assert step.get("shell") == "bash", (
        f"the run body is bash (pipefail, here-safe grep), got "
        f"{step.get('shell')!r}")
    assert step["run"].strip() == TIER2_RUN, (
        "the tier2 run body must assert the harness reached its terminal pass "
        f"marker.\n--- expected ---\n{TIER2_RUN}\n--- got ---\n{step['run'].strip()}")


def test_tier3_retains_its_scheduled_copy() -> None:
    """COPY, not move — `nightly-alarm` needs `tier3-hardening`."""
    wf = _workflow()
    tier3 = wf["jobs"]["tier3-hardening"]
    steps = _harness_steps(tier3)
    assert len(steps) == 1, (
        f"tier3 harness count={len(steps)}, expected 1 — the scheduled copy is "
        f"what nightly-alarm covers")
    step = steps[0]
    assert step.get("env", {}).get("CASA_USE_MOCK_CLAUDE") == "1", step.get("env")
    assert step["run"].strip() == TIER3_RUN, step["run"]
    assert "tier3-hardening" in wf["jobs"]["nightly-alarm"]["needs"]
