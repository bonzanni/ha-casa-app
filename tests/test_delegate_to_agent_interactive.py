"""The job launch owner's engagement launch, and the retired interactive mode.

Interactive specialist engagements (``delegate_to_agent(mode='interactive')``)
are retired in favour of the desk; a specialist's background job still runs as
an engagement in its own topic through the one launch owner,
``tools._launch_interactive_engagement``. The shared specialist fixtures here
are imported by other suites."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import (
    AgentConfig, CharacterConfig, DelegateEntry, MemoryConfig, SessionConfig,
    ToolsConfig,
)

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _make_assistant_cfg():
    """Caller cfg declaring `finance` as a delegate (spec A1 ACL fixture).

    Mirrors the assistant's real `delegates.yaml`, which declares finance —
    see `casa/rootfs/opt/casa/defaults/agents/assistant/delegates.yaml`.
    """
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="assistant")
    cfg.delegates = [DelegateEntry(agent="finance", purpose="p", when="w")]
    return cfg


def _make_alex_cfg():
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="finance")
    cfg.character = CharacterConfig(name="Alex", archetype="finance",
                                     card="", prompt="You are Alex.")
    cfg.enabled = True
    cfg.model = "sonnet"
    cfg.tools = ToolsConfig(allowed=["Read", "Write"], disallowed=[],
                            permission_mode="acceptEdits", max_turns=20)
    cfg.mcp_server_names = ["casa-framework"]
    cfg.memory = MemoryConfig(token_budget=0)
    cfg.session = SessionConfig(strategy="ephemeral", idle_timeout=0)
    cfg.channels = []
    cfg.system_prompt = "You are Alex."
    return cfg


def _job(title="Plan Q2"):
    import background_jobs as jobs
    return jobs.JobDecl("finplan:plan", "finplan", "plan", "finplan:plan",
                        title, None)


async def launch_job(task="Plan Q2", context="", *, origin=None, job=None,
                     agent="finance"):
    """Launch a specialist-hosted job exactly as ``_start_job_on_host`` hands
    it to the launch owner (the plugin pin stamped on the origin first)."""
    import agent as agent_mod
    import tools

    job = job or _job()
    origin = dict(origin or {
        "role": "assistant", "channel": "telegram",
        "chat_id": "c1", "cid": "x", "user_text": "hi", "scope": "business",
    })
    origin.setdefault("plugin_job", {"plugin": job.plugin})
    token = agent_mod.origin_var.set(origin)
    try:
        return await tools._launch_interactive_engagement(
            agent, task, context, origin, job=job)
    finally:
        agent_mod.origin_var.reset(token)


class TestJobLaunchEngagement:
    async def test_creates_the_engagement_and_opens_no_topic(self, tmp_path, monkeypatch):
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        tch = MagicMock()
        tch.engagement_permission_ok = True
        tch.engagement_supergroup_id = -1001
        tch.open_engagement_topic = AsyncMock(return_value=555)
        tch.send_to_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        specialist_reg = MagicMock()
        specialist_reg.get.return_value = _make_alex_cfg()
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=specialist_reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
            agent_role_map={"assistant": _make_assistant_cfg()},
        )
        driver = MagicMock()
        driver.open = AsyncMock()
        agent_mod.active_engagement_driver = driver

        res = await launch_job("Plan Q2")
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "pending"
        assert "topic_id" not in payload
        tch.open_engagement_topic.assert_not_awaited()
        tch.setup_engagement_features.assert_not_called()
        driver.open.assert_awaited_once()
        rec = reg.get(payload["engagement_id"])
        assert rec is not None and rec.topic_id is None
        # the brief is the session's first write, queued once
        assert driver.enqueue_brief.call_count == 1
        assert driver.enqueue_brief.call_args.args[0] is rec

    async def test_record_excludes_env_withheld_plugin(self, tmp_path,
                                                       monkeypatch):
        # #424 r3 (Terra r3-1 / Sol r3-2): the engagement record must pin
        # what the session actually LAUNCHED with. Recording the unfiltered
        # resolution lets a later resume — after the secret is wired — load
        # a plugin mid-engagement that the engagement never started with.
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from plugin_registry import reload_snapshot
        from tools import init_tools
        try:
            from tests.plugin_fixtures import entry, mk_artifact, mk_registry
        except ImportError:
            from plugin_fixtures import entry, mk_artifact, mk_registry

        store = tmp_path / "store"
        e = entry("finplug", ["specialist:finance"])
        mk_artifact(store, "finplug", e["artifact_id"],
                    mcp_servers={"finplug": {"env": {"K": "${FIN_REC_KEY}"}}})
        reload_snapshot(registry_path=mk_registry(tmp_path, [e]),
                        store_root=store)
        monkeypatch.delenv("FIN_REC_KEY", raising=False)

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        tch = MagicMock()
        tch.engagement_permission_ok = True
        tch.engagement_supergroup_id = -1001
        tch.open_engagement_topic = AsyncMock(return_value=556)
        tch.send_to_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        specialist_reg = MagicMock()
        specialist_reg.get.return_value = _make_alex_cfg()
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=specialist_reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
            agent_role_map={"assistant": _make_assistant_cfg()},
        )
        driver = MagicMock()
        driver.open = AsyncMock()
        agent_mod.active_engagement_driver = driver

        res = await launch_job("Plan Q2")
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "pending"
        rec = reg.get(payload["engagement_id"])
        assert rec is not None
        assert all(a["name"] != "finplug" for a in rec.plugin_artifacts)

    async def test_a_job_launches_with_no_log_group(self, tmp_path, monkeypatch):
        """No forum group configured and no right to manage topics: the job
        launches all the same."""
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        tch = MagicMock()
        tch.engagement_permission_ok = False
        tch.engagement_supergroup_id = 0
        cm = MagicMock(); cm.get.return_value = tch
        specialist_reg = MagicMock()
        specialist_reg.get.return_value = _make_alex_cfg()
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=specialist_reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
            agent_role_map={"assistant": _make_assistant_cfg()},
        )
        driver = MagicMock(); driver.open = AsyncMock()
        agent_mod.active_engagement_driver = driver
        res = await launch_job("x")
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "pending", payload
        assert driver.enqueue_brief.call_count == 1

    @pytest.mark.parametrize("channel", ["webhook", "voice_ws"])
    async def test_a_request_from_another_channel_is_refused(
            self, tmp_path, monkeypatch, channel):
        """A job talks to the operator in their Telegram direct chat: a
        request that came from another channel cannot start one."""
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        cm = MagicMock(); cm.get.return_value = MagicMock()
        specialist_reg = MagicMock()
        specialist_reg.get.return_value = _make_alex_cfg()
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=specialist_reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
            agent_role_map={"assistant": _make_assistant_cfg()},
        )
        driver = MagicMock(); driver.open = AsyncMock()
        agent_mod.active_engagement_driver = driver
        res = await launch_job("x", origin={
            "role": "assistant", "channel": channel, "chat_id": "c1",
            "cid": "x", "user_text": "hi"})
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "engagement_wrong_origin", payload
        assert reg.active_and_idle() == []
        driver.open.assert_not_awaited()

    async def test_driver_start_failure_ends_the_record(
        self, tmp_path, monkeypatch,
    ):
        """L23 leak guard: a failed driver.open in the job launch path ends
        its record ``error`` and writes nothing into the session."""
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        tch = MagicMock()
        tch.engagement_permission_ok = True
        tch.engagement_supergroup_id = -1001
        tch.open_engagement_topic = AsyncMock(return_value=555)
        tch.send_to_topic = AsyncMock()
        tch.update_topic_state = AsyncMock()
        tch.close_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        specialist_reg = MagicMock()
        specialist_reg.get.return_value = _make_alex_cfg()
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=specialist_reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
            agent_role_map={"assistant": _make_assistant_cfg()},
        )
        driver = MagicMock()
        driver.open = AsyncMock(side_effect=RuntimeError("boom"))
        agent_mod.active_engagement_driver = driver

        res = await launch_job("Plan Q2")
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "driver_start_failed"
        assert driver.enqueue_brief.call_count == 0

        [rec] = list(reg._records.values())
        assert rec.status == "error"
        assert rec.origin["error_kind"] == "driver_start_failed"
        tch.open_engagement_topic.assert_not_awaited()
        tch.close_topic.assert_not_awaited()


class TestInteractiveModeRetired:
    """Interactive specialist engagements are retired: a call that still
    names ``mode='interactive'`` — a model's, or one a stored scheduled prompt
    or trigger brief still asks for — is refused with a typed error that names
    what to do instead, and nothing is launched."""

    def _wire(self, tmp_path):
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        tch = MagicMock()
        tch.engagement_permission_ok = True
        tch.engagement_supergroup_id = -1001
        tch.open_engagement_topic = AsyncMock(return_value=555)
        cm = MagicMock(); cm.get.return_value = tch
        specialist_reg = MagicMock()
        specialist_reg.get.return_value = _make_alex_cfg()
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=specialist_reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
            agent_role_map={"assistant": _make_assistant_cfg(),
                            "finance": _make_alex_cfg()},
        )
        driver = MagicMock(); driver.open = AsyncMock()
        agent_mod.active_engagement_driver = driver
        return reg, tch, driver

    @pytest.mark.parametrize("channel", ["telegram", "voice"])
    async def test_an_interactive_delegation_is_refused_and_names_the_alternative(
        self, tmp_path, channel, caplog,
    ):
        import logging
        import agent as agent_mod
        import tools

        reg, tch, driver = self._wire(tmp_path)
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": channel,
            "chat_id": "c1", "cid": "x", "user_text": "let me talk to Alex",
            "scope": "business",
        })
        try:
            with caplog.at_level(logging.WARNING, logger="tools"):
                res = await tools.delegate_to_agent.handler({
                    "agent": "finance", "task": "Talk Q2 through", "context": "",
                    "mode": "interactive",
                })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "interactive_retired"
        # names the alternative: a sync delegation, and the person replying
        # to the specialist's own message in their chat (the desk)
        assert "mode='sync'" in payload["message"]
        assert "reply" in payload["message"]
        # nothing launched, nothing silently coerced into another mode
        tch.open_engagement_topic.assert_not_awaited()
        driver.open.assert_not_awaited()
        assert driver.enqueue_brief.call_count == 0
        assert reg.active_and_idle() == []
        assert not [r for r in caplog.records if "unrecognised mode" in r.getMessage()]

    async def test_the_schema_lets_a_stale_call_reach_the_refusal(self):
        """The SDK validates arguments against the input schema before the
        handler runs, and a value outside the enum would come back as a bare
        validation error that names no alternative. The retired value stays
        admitted so the handler's refusal is what the model reads, and the
        schema itself says it is retired."""
        import jsonschema
        import tools

        schema = tools.delegate_to_agent.input_schema
        jsonschema.validate(
            instance={"agent": "finance", "task": "t", "context": "",
                      "mode": "interactive"},
            schema=schema)
        assert "retired" in schema["properties"]["mode"]["description"]

    async def test_an_interactive_engagement_left_open_by_an_older_install_loads_and_holds(
        self, tmp_path, monkeypatch,
    ):
        """No migration: a specialist engagement an older install opened with
        the retired mode (a ``specialist`` record with no ``job``) loads from
        the tombstone, is not resumed at boot (only jobs are), and stays its
        specialist's open conversation — a job on that specialist is refused
        ``engagement_busy`` until the operator ends it."""
        import background_jobs
        import casa_core
        import tools
        from engagement_registry import EngagementRegistry

        old = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        legacy = await old.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="Walk me through Q2",
            origin={"role": "assistant", "channel": "telegram", "chat_id": "c1",
                    "delegation_depth": 1},
            topic_id=777)

        reg, tch, driver = self._wire(tmp_path)
        await reg.load()
        rec = reg.get(legacy.id)
        assert rec is not None and rec.status in ("active", "idle")
        assert "job" not in rec.origin

        resumed: list = []

        class _Runner:
            async def resume_at_boot(self, r):
                resumed.append(r.id)
        monkeypatch.setattr(background_jobs, "_runner", _Runner())
        await casa_core._resume_background_jobs(reg, object())
        assert resumed == []

        payload = json.loads((await launch_job("Classify"))["content"][0]["text"])
        assert payload["kind"] == "engagement_busy"
        assert payload["engagement_id"] == legacy.id
        tch.open_engagement_topic.assert_not_awaited()
