"""Plugin-job Casa grants and protected-tool identity."""
from types import SimpleNamespace

import agent
import tools
from authz_grants import GrantIdentity, resolve_grant_identity


def test_plugin_job_offers_and_admits_only_its_two_casa_tools():
    rec = SimpleNamespace(
        kind="plugin",
        tools_allowed=("mcp__casa-framework__report_job_progress",
                       "mcp__casa-framework__emit_completion"),
    )
    assert tools.engagement_casa_grant_names(rec) == {
        "report_job_progress", "emit_completion",
    }
    assert "query_engager" not in tools.engagement_casa_grant_names(rec)


def test_other_engagement_kinds_keep_mandatory_bridge_grants():
    rec = SimpleNamespace(kind="specialist", tools_allowed=())
    assert tools.engagement_casa_grant_names(rec) == {
        grant.rsplit("__", 1)[-1]
        for grant in tools.MANDATORY_BRIDGE_CASA_GRANTS
    }


def test_plugin_job_identity_is_bound_to_its_host_and_artifact():
    artifact = "a" * 64
    rec = SimpleNamespace(
        id="job-1", kind="plugin", status="active", topic_id=42,
        role_or_type="assistant", origin={"chat_id": 42, "user_id": 7},
        plugin_artifacts=({"artifact_id": artifact},),
    )
    origin = {"role": "assistant", "execution_role": "assistant",
              "channel": "telegram", "source": "telegram",
              "message_type": "channel_in", "chat_id": 42, "user_id": 7}
    origin_token = agent.origin_var.set(origin)
    engagement_token = tools.engagement_var.set(rec)
    try:
        identity, why = resolve_grant_identity("assistant", artifact)
        assert why is None
        assert identity == GrantIdentity(7, 42, "assistant", artifact, "job-1")
        assert resolve_grant_identity("other", artifact) == (None, "engagement_unavailable")
        assert resolve_grant_identity("assistant", "b" * 64) == (None, "engagement_unavailable")
    finally:
        tools.engagement_var.reset(engagement_token)
        agent.origin_var.reset(origin_token)
