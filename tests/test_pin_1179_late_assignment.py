"""#1179: a plugin assigned while an interactive launch awaits topic creation.

The session is built after ``open_engagement_topic`` returns, and ``plugin_assign``
promises an assignment applies to sessions built from then on. A target with no
``requires:`` block already resolves its plugins after the topic await. A target that
declares ``requires:`` used to reuse the requires gate's resolution, taken before the
await, so a plugin assigned in between was neither recorded on the engagement nor
launched — and resume, which reads the record, kept it out for the engagement's life.

The launch now keeps every plugin the gate admitted and adds the ones the post-topic
resolution returns beyond them; the one resulting resolution is both recorded and handed
to the session build.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import AgentConfig, DelegateEntry, RequiresConfig
from plugin_registry import ResolutionResult, ResolvedPlugin

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.unit]


def _cfg(role: str, delegates: tuple[str, ...] = (),
         requires: RequiresConfig | None = None) -> AgentConfig:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role)
    cfg.delegates = [DelegateEntry(agent=d, purpose="p", when="w") for d in delegates]
    if requires is not None:
        cfg.requires = requires
    return cfg


def _plugin(name: str, manifest_name: str = "") -> ResolvedPlugin:
    return ResolvedPlugin(
        name=name, artifact_id=f"art-{name}", path=f"/plugins/{name}",
        version="1.0.0", manifest={}, manifest_name=manifest_name)


async def _launch(monkeypatch, *, requires: RequiresConfig | None,
                  before: list, after, ):
    """Drive one interactive delegation to ``finance``. ``resolve_for`` returns
    *before* until ``open_engagement_topic`` runs, then *after* (a list of plugins,
    or an exception instance to raise). Returns the observations."""
    import agent as agent_mod
    import tools as tm

    state = {"phase": "before"}
    resolves: list[tuple[str, str]] = []

    def _resolve_for(target):
        resolves.append((state["phase"], target))
        if state["phase"] == "before":
            return ResolutionResult(registry_valid=True, plugins=list(before))
        if isinstance(after, BaseException):
            raise after
        return ResolutionResult(registry_valid=True, plugins=list(after))

    async def _open_topic(**_kw):
        # The assignment lands while the launch awaits topic creation.
        state["phase"] = "after"
        return 555

    tch = MagicMock()
    tch.engagement_permission_ok = True
    tch.engagement_supergroup_id = -1001
    tch.open_engagement_topic = AsyncMock(side_effect=_open_topic)
    tch.set_channel_state = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = tch
    reg = MagicMock()
    reg.get.return_value = None
    eng_reg = MagicMock()
    rec = MagicMock()
    rec.id = "eng1"
    eng_reg.create = AsyncMock(return_value=rec)
    eng_reg.update_plugin_profiles = AsyncMock()
    eng_reg.set_channel_state = AsyncMock()
    eng_reg.set_initial_state_emoji = AsyncMock()

    tm.init_tools(
        channel_manager=cm, bus=MagicMock(),
        specialist_registry=reg, mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=eng_reg,
        agent_role_map={
            "assistant": _cfg("assistant", delegates=("finance",)),
            "finance": _cfg("finance", requires=requires),
        },
    )
    monkeypatch.setattr(tm.plugin_registry, "resolve_for", _resolve_for)
    abort = AsyncMock()
    monkeypatch.setattr(tm, "_abort_engagement_topic", abort)

    built: list = []

    def _spy_builder(cfg, *, resolution=None, extra_casa_tools=(), plan_out=None, **_kw):
        built.append(resolution)
        opts = MagicMock()
        opts.allowed_tools = []
        return opts

    monkeypatch.setattr(tm, "_build_specialist_options", _spy_builder)
    driver = MagicMock()
    driver.start = AsyncMock()
    monkeypatch.setattr(agent_mod, "active_engagement_driver", driver)

    token = agent_mod.origin_var.set({
        "role": "assistant", "execution_role": "assistant",
        "channel": "telegram", "chat_id": "c1", "cid": "t", "user_text": "hi",
    })
    try:
        res = await tm.delegate_to_agent.handler({
            "agent": "finance", "task": "t", "context": "", "mode": "interactive",
        })
    finally:
        agent_mod.origin_var.reset(token)
    return {
        "payload": json.loads(res["content"][0]["text"]),
        "topic": tch.open_engagement_topic, "create": eng_reg.create,
        "abort": abort, "built": built, "resolves": resolves,
        "driver": driver,
    }


def _recorded_names(obs) -> list[str]:
    return [pa["name"] for pa in obs["create"].await_args.kwargs["plugin_artifacts"]]


@pytest.mark.asyncio
class TestLateAssignmentDuringTopicAwait:
    async def test_requires_target_records_and_launches_the_late_plugin(self, monkeypatch):
        mtg = _plugin("mtg")
        late = _plugin("late")
        obs = await _launch(
            monkeypatch, requires=RequiresConfig(plugins=["mtg"], tools=[]),
            before=[mtg], after=[mtg, late])

        assert obs["payload"]["status"] == "pending"
        assert obs["topic"].await_count == 1
        assert obs["create"].await_count == 1
        assert obs["abort"].await_count == 0
        assert len(obs["built"]) == 1
        built = obs["built"][0].plugins
        assert _recorded_names(obs) == [rp.name for rp in built] == ["mtg", "late"]
        # The gate's own admitted entry is kept, not swapped for the fresh one.
        assert built[0] is mtg
        assert obs["driver"].start.await_count == 1

    async def test_no_requires_target_already_picks_the_late_plugin_up(self, monkeypatch):
        mtg = _plugin("mtg")
        late = _plugin("late")
        obs = await _launch(monkeypatch, requires=None, before=[mtg], after=[mtg, late])

        assert obs["payload"]["status"] == "pending"
        assert obs["topic"].await_count == 1
        assert obs["create"].await_count == 1
        assert obs["abort"].await_count == 0
        assert len(obs["built"]) == 1
        assert _recorded_names(obs) == [rp.name for rp in obs["built"][0].plugins] == [
            "mtg", "late"]
        # One resolve, taken after the topic exists.
        assert obs["resolves"] == [("after", "specialist:finance")]
