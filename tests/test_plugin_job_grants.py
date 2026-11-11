"""Plugin-job Casa grants and protected-tool identity."""
from types import SimpleNamespace

import agent
import background_jobs
import tools
from authz_grants import GrantIdentity, resolve_grant_identity


def test_plugin_job_offers_and_admits_only_its_two_casa_tools():
    """A plugin job's whole casa-framework surface is its two job tools —
    no union with the specialist grants, so a worker never holds
    query_engager. The session builder offers exactly this tuple and filters
    Casa's MCP server to it (pinned end to end in
    test_plugin_job_launch.py::test_worker_options_record_and_topic)."""
    names = {g.rsplit("__", 1)[-1]
             for g in background_jobs.PLUGIN_JOB_CASA_GRANTS}
    assert names == {"report_job_progress", "emit_completion"}
    assert len(background_jobs.PLUGIN_JOB_CASA_GRANTS) == 2
    assert "mcp__casa-framework__query_engager" in tools.SPECIALIST_CASA_GRANTS
    assert "mcp__casa-framework__query_engager" not in (
        background_jobs.PLUGIN_JOB_CASA_GRANTS)


def test_plugin_job_identity_is_bound_to_its_host_and_artifact(monkeypatch):
    from two_endpoints import CASA_BOT, build_two_endpoints
    monkeypatch.setattr(tools, "_channel_manager", build_two_endpoints().manager)
    artifact = "a" * 64
    rec = SimpleNamespace(
        id="job-1", kind="plugin", status="active",
        role_or_type="assistant",
        origin={"chat_id": 42, "user_id": 7, "_tg_bot": CASA_BOT},
        plugin_artifacts=({"artifact_id": artifact},),
    )
    origin = {"role": "assistant", "execution_role": "assistant",
              "channel": "telegram", "source": "telegram",
              "message_type": "channel_in", "chat_id": 42, "user_id": 7,
              "_tg_bot": CASA_BOT}
    origin_token = agent.origin_var.set(origin)
    engagement_token = tools.engagement_var.set(rec)
    try:
        identity, why = resolve_grant_identity("assistant", artifact)
        assert why is None
        assert identity == GrantIdentity(
            operator_id=7, bot_id=CASA_BOT, chat_id=42,
            enforcement_role="assistant", artifact_id=artifact,
            engagement_id="job-1")
        assert resolve_grant_identity("other", artifact) == (None, "engagement_unavailable")
        assert resolve_grant_identity("assistant", "b" * 64) == (None, "engagement_unavailable")
    finally:
        tools.engagement_var.reset(engagement_token)
        agent.origin_var.reset(origin_token)
