#!/usr/bin/env bash
# Tier-2 e2e: resident-to-resident delegation (delegate_to_agent → butler)
#
# Verifies the casa_core wiring (v0.15.0 Tasks 5/7/11) actually populates
# tools._agent_role_map and tools._agent_registry at boot, and that
# delegate_to_agent resolves a resident target end-to-end in the running
# container. Negative path: mode=interactive on a resident is rejected.
#
# Tier 2 (functional) per the v0.14.11+ tiering model. Runs under the
# tier2-functional job in .github/workflows/qa.yml on push+PR.

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

NAME="casa-deleg-e-$$"

cleanup_all() {
    docker stop "$NAME" >/dev/null 2>&1 || true
}
trap cleanup_all EXIT

build_image

log "DE-0: boot container (default config)"
start_container "$NAME" >/dev/null
wait_healthy "$NAME"
pass "DE-0 container healthy"

# ---------------------------------------------------------------------------
# Helper: run_harness <label> <py>
# Same pattern as test_engagement_E.sh — copies py source to /tmp inside the
# container and executes it via the casa venv. Fails if the last line != "OK".
# ---------------------------------------------------------------------------
run_harness() {
    local label="$1"; shift
    local py="$1"; shift
    local host_tmp
    host_tmp="$(mktemp)"
    printf '%s\n' "$py" > "$host_tmp"
    MSYS_NO_PATHCONV=1 docker cp "$host_tmp" "$NAME:/tmp/_harness.py" >/dev/null
    rm -f "$host_tmp"
    local out
    # Use with-contenv so s6-exported env vars (PRIMARY_AGENT_MODEL etc.) are
    # available to the harness — same environment the running casa_core sees.
    if ! out=$(MSYS_NO_PATHCONV=1 docker exec "$NAME" \
               /command/with-contenv /opt/casa/venv/bin/python /tmp/_harness.py 2>&1); then
        printf '%s\n' "$out" >&2
        fail "$label harness exited non-zero"
    fi
    printf '%s\n' "$out" | tail -20 >&2
    printf '%s\n' "$out" | tail -1 | grep -qF "OK" \
        || fail "$label harness did not print OK as final line"
}

# ---------------------------------------------------------------------------
# DE-1 — Registry wiring + sync delegation + interactive-rejection.
#
# The harness initialises tools itself (via init_tools) by loading agents from
# /config/agents — the same directory that casa_core uses at
# boot. This validates the full wiring path (Tasks 5/7/11) as it would execute
# inside the running add-on.
#
# Assertion points:
#   1. load_all_agents + _build_role_registry yields both assistant + butler.
#   2. AgentRegistry.build resolves role_to_name correctly.
#   3. After init_tools, delegate_to_agent(mode=sync) with a monkey-patched
#      _run_delegated_agent returns status=ok, agent=butler, and the expected
#      text.
#   4. delegate_to_agent(mode=interactive) on a resident target is rejected with
#      kind=interactive_not_supported (Task 8 behavior).
# ---------------------------------------------------------------------------
log "DE-1: merged-role-map wiring + sync delegation + interactive-rejection"

read -r -d '' DE1_PY <<'PY' || true
import asyncio, json, sys
sys.path.insert(0, "/opt/casa")

from agent_loader import load_all_agents, load_all_specialists
from agent_registry import AgentRegistry
from policies import load_policies
from tools import init_tools
import tools
import agent as agent_mod

CONFIG_DIR = "/config"
AGENTS_DIR = CONFIG_DIR + "/agents"

# ---------- 1. Load agents from disk (mirrors casa_core boot sequence) ----------
policy_lib = load_policies(CONFIG_DIR + "/policies/disclosure.yaml")
role_configs = load_all_agents(AGENTS_DIR, policies=policy_lib)
# DE-1 v0.37.11: load_all_specialists returns (dict, failed) since v0.37.9 O-2b.
specialist_configs, _failed = load_all_specialists(AGENTS_DIR + "/specialists")

assert "assistant" in role_configs, \
    f"assistant not in role_configs; keys={list(role_configs)}"
assert "butler" in role_configs, \
    f"butler not in role_configs; keys={list(role_configs)}"

# ---------- 2. Build merged role map + AgentRegistry ----------
merged = {}
merged.update(role_configs)
merged.update(specialist_configs)

agent_registry = AgentRegistry.build(
    residents=role_configs, specialists=specialist_configs,
)
ellen_name = agent_registry.role_to_name("assistant")
tina_name  = agent_registry.role_to_name("butler")
assert ellen_name == "Ellen", f"role_to_name('assistant')={ellen_name!r}"
assert tina_name  == "Tina",  f"role_to_name('butler')={tina_name!r}"
print("HARNESS: AgentRegistry.build OK", file=sys.stderr)

# ---------- 3. Stub collaborators for init_tools ----------
class _FakeSpecialistRegistry:
    """Minimal stub — only the methods delegate_to_agent's sync path calls."""
    def get(self, name): return None
    async def register_delegation(self, record): pass
    async def complete_delegation(self, did): pass
    async def fail_delegation(self, did, exc): pass
    async def cancel_delegation(self, did): pass

class _FakeMcpRegistry:
    pass

class _FakeBus:
    pass

class _FakeChanMgr:
    pass

init_tools(
    _FakeChanMgr(), _FakeBus(), _FakeSpecialistRegistry(), _FakeMcpRegistry(),
    agent_role_map=merged,
    agent_registry=agent_registry,
    trigger_registry=None,
    engagement_registry=None,
    executor_registry=None,
)
print("HARNESS: init_tools OK", file=sys.stderr)

# ---------- 4. Verify module-level state is populated ----------
assert tools._agent_role_map, "agent_role_map still empty after init_tools"
assert "assistant" in tools._agent_role_map, \
    f"assistant missing from role map; keys={list(tools._agent_role_map)}"
assert "butler" in tools._agent_role_map, \
    f"butler missing from role map; keys={list(tools._agent_role_map)}"
assert tools._agent_registry is not None, \
    "agent_registry is None after init_tools — Task 11 wiring missing"
print("HARNESS: module-level wiring verified", file=sys.stderr)

# ---------- 5. Monkey-patch _run_delegated_agent to bypass SDK ----------
# Keep the production signature explicit so a future drift fails this e2e seam
# loudly instead of being hidden by a catch-all.
observed_output_formats = []
async def _fake_run(
    cfg, task_text, context_text, resolution=None, output_format=None,
    tool_counts=None,
):
    observed_output_formats.append(output_format)
    return tools.DelegatedOutput(
        text="Lights are off.", structured_output=None,
    )

tools._run_delegated_agent = _fake_run

# ---------- 6. Set origin (required by delegate_to_agent) ----------
token = agent_mod.origin_var.set({
    "role": "assistant",
    "channel": "telegram",
    "chat_id": "1",
    "user_id": 1,
    "cid": "abc",
    "user_text": "Tina, turn off the kitchen lights",
    "delegation_depth": 0,
})

async def main():
    # --- 6a. Sync delegation resolves butler ---
    res = await tools.delegate_to_agent({
        "agent": "butler",
        "task": "turn off the kitchen lights",
        "context": "",
        "mode": "sync",
    })
    payload = json.loads(res["content"][0]["text"])
    assert payload["status"] == "ok", \
        f"sync delegation status not ok: {payload!r}"
    assert payload["agent"] == "butler", \
        f"agent != butler: {payload!r}"
    assert "Lights are off" in payload["text"], \
        f"text missing from payload: {payload!r}"
    assert observed_output_formats == [None], \
        f"sync E-block output_format drifted: {observed_output_formats!r}"
    print("HARNESS: sync delegation OK", file=sys.stderr)

    # --- 6b. Interactive mode on a resident is rejected (Task 8) ---
    # butler has channels=[...] so is_resident=True.
    butler_cfg = tools._agent_role_map["butler"]
    assert getattr(butler_cfg, "channels", []), \
        "butler has no channels — is_resident check will not trigger"

    res2 = await tools.delegate_to_agent({
        "agent": "butler",
        "task": "x",
        "context": "",
        "mode": "interactive",
    })
    payload2 = json.loads(res2["content"][0]["text"])
    assert payload2["status"] == "error", \
        f"interactive mode not rejected: {payload2!r}"
    assert payload2["kind"] == "interactive_not_supported", \
        f"wrong kind for interactive rejection: {payload2!r}"
    print("HARNESS: interactive-rejection OK", file=sys.stderr)

asyncio.run(main())
agent_mod.origin_var.reset(token)
print("OK")
PY

run_harness "DE-1" "$DE1_PY"
pass "DE-1 merged-role-map + sync delegation + interactive-reject all green"

stop_container "$NAME"
