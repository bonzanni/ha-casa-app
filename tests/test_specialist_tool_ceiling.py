"""#541 — the specialist casa-framework tool ceiling (install-time layer).

A third-party bundle's ``role.yaml`` may grant casa-framework tools only
from the consumer-safe allowlist; the bare server-level grant and any
spawn/privilege tool are rejected by ``load_specialist_component`` BEFORE
any consent prompt can be built. Non-casa entries (CC built-ins, plugin
servers) are out of this ceiling's scope.
"""
from pathlib import Path

import pytest

from specialist_component import (
    SPECIALIST_CASA_TOOL_ALLOWLIST,
    load_specialist_component,
    specialist_casa_tool_violations,
)

try:
    from tests.specialist_fixtures import write_minimal_component
except ImportError:
    from specialist_fixtures import write_minimal_component


def test_allowlist_is_exactly_the_converged_set():
    # Design rounds 1-4 (2026-08-13). send_message OUT (unbounded outbound
    # spam primitive — r2 both reviewers); write-side reminder/schedule and
    # voice-job tools OUT (they act on the RESIDENT's state via the
    # inherited actor — r1 Sol); get_schedule kept (read-only, defended by
    # both reviewers in r3); send_media kept but quota-bound at dispatch.
    assert SPECIALIST_CASA_TOOL_ALLOWLIST == frozenset({
        "query_engager", "emit_completion", "react", "ask_user",
        "recall_memory", "ack_event", "get_schedule", "send_media",
    })


def test_allowlisted_grants_load(tmp_path: Path):
    component_dir, manifest_path = write_minimal_component(
        tmp_path, tools_allowed=[
            "Read", "WebSearch",
            "mcp__casa-framework__recall_memory",
            "mcp__casa-framework__send_media",
            "mcp__plugin_bank-feed_bank-feed__list_banks",
        ])
    comp = load_specialist_component(component_dir, manifest_path)
    assert comp.slug == "mtg-test"


@pytest.mark.parametrize("grant", [
    "mcp__casa-framework",                          # server-level = everything
    "mcp__casa-framework__engage_executor",         # spawn
    "mcp__casa-framework__delegate_to_agent",       # spawn
    "mcp__casa-framework__plugin_add",              # plugin management
    "mcp__casa-framework__config_git_commit",       # config mutation
    "mcp__casa-framework__get_item_fields",         # secrets
    "mcp__casa-framework__list_vault_items",        # secrets
    "mcp__casa-framework__cancel_engagement",       # arbitrary-id, no ownership
    "mcp__casa-framework__delete_engagement_workspace",
    "mcp__casa-framework__set_reminder",            # writes resident triggers
    "mcp__casa-framework__send_message",            # unbounded outbound send
    "mcp__casa-framework__specialist_install_commit",
])
def test_forbidden_grant_rejected_before_consent(tmp_path: Path, grant: str):
    component_dir, manifest_path = write_minimal_component(
        tmp_path, tools_allowed=[grant])
    with pytest.raises(ValueError) as exc_info:
        load_specialist_component(component_dir, manifest_path)
    assert grant in str(exc_info.value)


class TestDispatchCeiling:
    """#541 layer 3 — the central-wrapper ceiling revokes forbidden grants
    for ALREADY-LIVE specialist records (both transports), including records
    pinned before the install/load ceilings existed."""

    def _bound(self, kind):
        import tools as tools_mod
        eng = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        eng.kind = kind
        eng.context_rebuild_pending = False
        return tools_mod, eng

    @pytest.mark.asyncio
    async def test_specialist_record_cannot_reach_forbidden_casa_tool(self):
        import json
        tools_mod, eng = self._bound("specialist")
        tok = tools_mod.engagement_var.set(eng)
        try:
            r = await tools_mod.plugin_list.handler({})
        finally:
            tools_mod.engagement_var.reset(tok)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "specialist_tool_ceiling"
        assert r.get("is_error") is True

    @pytest.mark.asyncio
    async def test_specialist_record_keeps_allowlisted_tool(self):
        import json
        tools_mod, eng = self._bound("specialist")
        tok = tools_mod.engagement_var.set(eng)
        try:
            # query_engager proceeds past the ceiling into its own handler
            # (which then errors on the unwired registry — anything but the
            # ceiling refusal proves the gate let it through).
            r = await tools_mod.query_engager.handler({})
        finally:
            tools_mod.engagement_var.reset(tok)
        payload = json.loads(r["content"][0]["text"])
        assert payload.get("kind") != "specialist_tool_ceiling"

    @pytest.mark.asyncio
    async def test_executor_record_is_not_ceilinged(self):
        import json
        tools_mod, eng = self._bound("executor")
        tok = tools_mod.engagement_var.set(eng)
        try:
            r = await tools_mod.plugin_list.handler({})
        finally:
            tools_mod.engagement_var.reset(tok)
        payload = json.loads(r["content"][0]["text"])
        assert payload.get("kind") != "specialist_tool_ceiling"

    @pytest.mark.asyncio
    async def test_unbound_caller_is_not_ceilinged(self):
        import json
        import tools as tools_mod
        r = await tools_mod.plugin_list.handler({})
        payload = json.loads(r["content"][0]["text"])
        assert payload.get("kind") != "specialist_tool_ceiling"


def test_violations_helper_names_every_offender():
    offenders = specialist_casa_tool_violations([
        "Read",
        "mcp__casa-framework",
        "mcp__casa-framework__engage_executor",
        "mcp__casa-framework__recall_memory",   # allowlisted — not named
        "mcp__plugin_x_x__tool",                # out of scope — not named
    ])
    assert offenders == [
        "mcp__casa-framework", "mcp__casa-framework__engage_executor",
    ]
