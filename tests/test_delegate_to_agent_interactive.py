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
    async def test_opens_topic_and_creates_engagement(self, tmp_path, monkeypatch):
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
        # Driver + start side-effect
        driver = MagicMock()
        driver.start = AsyncMock()
        agent_mod.active_engagement_driver = driver

        res = await launch_job("Plan Q2")
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "pending"
        assert payload["topic_id"] == 555
        tch.open_engagement_topic.assert_awaited_once()
        driver.start.assert_awaited_once()
        assert reg.by_topic_id(555) is not None

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
        driver.start = AsyncMock()
        agent_mod.active_engagement_driver = driver

        res = await launch_job("Plan Q2")
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "pending"
        rec = reg.by_topic_id(556)
        assert rec is not None
        assert all(a["name"] != "finplug" for a in rec.plugin_artifacts)

    async def test_kind_engagement_not_configured_when_supergroup_empty(
        self, tmp_path, monkeypatch,
    ):
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
        res = await launch_job("x")
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "engagement_not_configured"

    async def test_long_task_topic_name_fits_telegram_budget(
        self, tmp_path, monkeypatch,
    ):
        """E-9 regression: a 500-char job title must not produce a topic
        name >128 UTF-8 bytes (Telegram createForumTopic limit) AND
        must not slice mid-word.

        v0.37.1 D-1: the post-create edit_forum_topic rename block is
        gone — the specialist path now uses the U3 title format
        directly at open_engagement_topic time. concise_task() already
        bounds the body to U3_TASK_BYTE_BUDGET; truncate_for_topic
        appends '…' inside concise_task when truncation kicks in.
        """
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import init_tools

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"),
        )
        tch = MagicMock()
        tch.engagement_permission_ok = True
        tch.engagement_supergroup_id = -1001
        tch.open_engagement_topic = AsyncMock(return_value=555)
        tch.send_to_topic = AsyncMock()
        tch.bot = MagicMock()
        tch.bot.edit_forum_topic = AsyncMock()
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
        driver = MagicMock(); driver.start = AsyncMock()
        agent_mod.active_engagement_driver = driver

        long_task = (
            "Add a one-line personality trait to Ellen's agent config. "
            "Something like: \"TRAIT_NAME\" or any other identifier "
            "you find suitable for the experiment, considering the "
            "downstream impact on tone, register, and the conversation "
            "patterns used by Ellen across her supported channels."
        )
        assert len(long_task) > 128

        # the topic is named after the job's title
        await launch_job(job=_job(title=long_task))

        # open_engagement_topic was called; assert byte budget.
        tch.open_engagement_topic.assert_awaited_once()
        open_kwargs = tch.open_engagement_topic.await_args.kwargs
        open_name = open_kwargs["name"]
        assert len(open_name.encode("utf-8")) <= 128
        # E-9: when the task is longer than the budget, the topic name
        # MUST end with the '…' ellipsis added by truncate_for_topic
        # (invoked indirectly via concise_task).
        assert open_name.endswith("…"), (
            f"E-9: helper not invoked (no ellipsis); got {open_name!r}"
        )

        # v0.37.1 D-1: post-create edit_forum_topic rename is gone —
        # the U3 title is set at open time, no follow-up rename.
        tch.bot.edit_forum_topic.assert_not_called()

    async def test_driver_start_failure_flips_and_closes_topic(
        self, tmp_path, monkeypatch,
    ):
        """L23 leak guard: a failed driver.start in the job launch
        path must not leave the just-created topic open
        forever — it must be flipped to 'failed' and closed."""
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
        driver.start = AsyncMock(side_effect=RuntimeError("boom"))
        agent_mod.active_engagement_driver = driver

        res = await launch_job("Plan Q2")
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "driver_start_failed"

        rec = reg.by_topic_id(555)
        assert rec is not None
        tch.update_topic_state.assert_awaited_once_with(
            engagement_id=rec.id, new_state="failed",
        )
        tch.close_topic.assert_awaited_once_with(thread_id=555)


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
        driver = MagicMock(); driver.start = AsyncMock(); driver.open = AsyncMock()
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
        driver.start.assert_not_awaited()
        driver.open.assert_not_awaited()
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

        async def start(r, channel):
            resumed.append(r.id)
        monkeypatch.setattr(background_jobs, "start_next_batch", start)
        await casa_core._resume_background_jobs(reg, object())
        assert resumed == []

        payload = json.loads((await launch_job("Classify"))["content"][0]["text"])
        assert payload["kind"] == "engagement_busy"
        assert payload["engagement_id"] == legacy.id
        tch.open_engagement_topic.assert_not_awaited()
