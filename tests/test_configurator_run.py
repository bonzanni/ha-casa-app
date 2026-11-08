"""The configurator as a delegated run in the operator's DM (engagement trim,
stage 4): the privileged tools follow the EXECUTING role, one lease admits one
configurator run at a time from every origin, the lease survives a shutdown
cancel and is announced once at boot, a restart the configurator needs is a
"Restart now" button spent on its first tap, install time does not count
toward the ten-minute ceiling, and plugin-health DMs are held while a run
holds the lease."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio


# -- helpers ------------------------------------------------------------------

@pytest.fixture
def lease_file(tmp_path, monkeypatch):
    import configurator_run
    path = tmp_path / "configurator-lease.json"
    monkeypatch.setattr(configurator_run, "LEASE_PATH", str(path))
    configurator_run._reset_for_tests()
    yield path
    configurator_run._reset_for_tests()


def _configurator_cfg():
    import agent_loader
    return agent_loader.load_configurator()


def _origin(**extra):
    base = {"role": "assistant", "channel": "telegram", "chat_id": "123",
            "cid": "c", "user_text": "x"}
    base.update(extra)
    return base


def _payload(result):
    return json.loads(result["content"][0]["text"])


# -- the privileged tools follow the executing role ---------------------------

_PRIVILEGED = ("config_git_commit", "casa_reload", "casa_restart_supervised",
               "config_trigger_upsert", "config_trigger_delete", "casa_reload_triggers")


async def test_caller_role_is_the_executing_configurator_in_a_delegated_run():
    import agent as agent_mod
    import tools
    tok = agent_mod.origin_var.set(_origin(execution_role="configurator"))
    try:
        assert tools._effective_caller_role() == "configurator"
    finally:
        agent_mod.origin_var.reset(tok)


@pytest.mark.parametrize("name", _PRIVILEGED)
async def test_privileged_tool_refuses_another_executing_role(name):
    """A delegated specialist called by Ellen is refused, though origin['role']
    names Ellen: the executing role is what counts."""
    import agent as agent_mod
    import tools
    tok = agent_mod.origin_var.set(_origin(execution_role="finance"))
    try:
        payload = _payload(await getattr(tools, name).handler(
            {"message": "m", "scope": "agent", "role": "assistant",
             "trigger": {}, "name": "x"}))
    finally:
        agent_mod.origin_var.reset(tok)
    assert payload["kind"] == "not_authorized"


@pytest.mark.parametrize("name", _PRIVILEGED)
async def test_privileged_tool_admits_the_configurator_executing_role(name, monkeypatch):
    """Every one of the six gates reads the executing role: patched to see the
    gate's verdict without running the tool's body."""
    import agent as agent_mod
    import tools
    seen: list = []

    def _refuse(tool_name, caller):
        seen.append((tool_name, caller))
        return tools._result({"status": "error", "kind": "not_authorized"})

    monkeypatch.setattr(tools, "_refuse_unprivileged", _refuse)
    monkeypatch.setattr(tools, "_PRIVILEGED_CONFIG_ROLES", frozenset())
    tok = agent_mod.origin_var.set(_origin(execution_role="configurator"))
    try:
        await getattr(tools, name).handler({"message": "m"})
    finally:
        agent_mod.origin_var.reset(tok)
    assert seen == [(name, "configurator")]


# -- the configurator is a delegate with a desk --------------------------------

async def test_the_image_owned_configurator_loads_with_its_guards():
    cfg = _configurator_cfg()
    assert cfg.role == "configurator" and cfg.kind == "executor"
    assert cfg.cwd == "/config"
    assert cfg.memory.token_budget == 0
    assert cfg.tools.max_turns >= 200
    policies = {h.get("policy") for h in cfg.hooks.pre_tool_use}
    assert {"path_scope", "casa_config_guard", "managed_component_guard",
            "commit_size_guard", "trigger_file_write_guard",
            "response_shape_write_guard", "resident_prompt_write_guard",
            "block_dangerous_bash"} <= policies
    assert "mcp__casa-framework__emit_completion" not in cfg.tools.allowed
    assert "mcp__casa-framework__query_engager" not in cfg.tools.allowed
    assert cfg.system_prompt.strip()


async def test_role_map_carries_the_configurator(monkeypatch):
    import tools
    runtime = SimpleNamespace(role_configs={}, specialist_registry=None,
                              agent_registry=None)
    monkeypatch.setattr(tools, "_agent_role_map", {})
    tools.sync_agent_role_map(runtime)
    assert tools._agent_role_map["configurator"].kind == "executor"


async def test_a_swipe_reply_on_a_configurator_post_reaches_its_desk(monkeypatch):
    """D1: the desk route admits the configurator (an executor) as it admits a
    specialist, and a resident's DM delegation to it uses that same desk."""
    import specialist_desk
    import tools
    cfg = _configurator_cfg()
    monkeypatch.setattr(tools, "_agent_role_map", {"configurator": cfg})
    monkeypatch.setattr(tools, "declares_delegate", lambda res, role: role == "configurator")
    assert specialist_desk.desk_target_ok("assistant", "configurator") is True
    origin = _origin(_operator_turn=True, chat_id=123)
    desk = specialist_desk.desk_for_delegation(origin, cfg, "configurator")
    assert desk is not None


async def test_delegation_to_the_configurator_always_runs_detached(monkeypatch):
    """Ellen's turn never awaits the run (a commit reloading her would cancel
    it): a sync request comes back pending at once."""
    import tools
    assert tools._delegation_mode_for(_configurator_cfg(), "sync") == "async"
    assert tools._delegation_mode_for(SimpleNamespace(kind="specialist"), "sync") == "sync"


# -- one lease for every origin ------------------------------------------------

async def _blocking_runner(started: asyncio.Event, release: asyncio.Event):
    async def _run(cfg, task_text, context_text, *, resolution=None,
                   output_format=None, tool_counts=None):
        started.set()
        await release.wait()
        return SimpleNamespace(text="done", run_aborted=False)
    return _run


async def test_a_second_run_from_another_origin_waits_for_the_lease(lease_file, monkeypatch):
    import agent as agent_mod
    import tools
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(tools, "_run_delegated_agent",
                        await _blocking_runner(started, release))
    cfg = _configurator_cfg()
    tok = agent_mod.origin_var.set(_origin(execution_role="configurator",
                                           _delegation_id="deleg-1"))
    try:
        first = asyncio.create_task(tools._run_delegated_agent_bounded(cfg, "add plugin x", ""))
    finally:
        agent_mod.origin_var.reset(tok)
    await asyncio.wait_for(started.wait(), 5)
    assert json.loads(lease_file.read_text())["task"] == "add plugin x"
    started.clear()
    tok = agent_mod.origin_var.set(_origin(execution_role="configurator",
                                           _delegation_id="turn-2",
                                           desk={"role": "configurator", "chat_id": 123}))
    try:
        second = asyncio.create_task(tools._run_delegated_agent_bounded(cfg, "yes", ""))
    finally:
        agent_mod.origin_var.reset(tok)
    await asyncio.sleep(0.05)
    assert not started.is_set()            # waiting, nothing of it ran
    release.set()
    await asyncio.wait_for(first, 5)
    await asyncio.wait_for(started.wait(), 5)
    rec = json.loads(lease_file.read_text())
    assert rec["run_key"] == "turn-2" and rec["via"] == "desk"
    await asyncio.wait_for(second, 5)
    assert not lease_file.exists()


async def test_past_the_bound_a_second_run_is_refused_with_the_busy_text(lease_file, monkeypatch):
    import agent as agent_mod
    import configurator_run
    import specialist_desk
    import tools
    monkeypatch.setattr(configurator_run, "LEASE_WAIT_S", 0.05)
    started, release = asyncio.Event(), asyncio.Event()
    calls: list = []
    runner = await _blocking_runner(started, release)

    async def _counting(*a, **k):
        calls.append(a[1])
        return await runner(*a, **k)

    monkeypatch.setattr(tools, "_run_delegated_agent", _counting)
    cfg = _configurator_cfg()
    tok = agent_mod.origin_var.set(_origin(execution_role="configurator",
                                           _delegation_id="deleg-1"))
    try:
        first = asyncio.create_task(
            tools._run_delegated_agent_bounded(cfg, "install the weather plugin", ""))
        await asyncio.wait_for(started.wait(), 5)
        with pytest.raises(configurator_run.ConfiguratorBusy) as exc:
            await tools._run_delegated_agent_bounded(cfg, "apply persona Ada", "")
    finally:
        agent_mod.origin_var.reset(tok)
    assert isinstance(exc.value, specialist_desk.DeskBusy)
    assert "install the weather plugin" in str(exc.value)
    assert "try again shortly" in str(exc.value)
    assert calls == ["install the weather plugin"]
    release.set()
    await asyncio.wait_for(first, 5)


async def test_a_new_run_waits_for_running_plugin_erasures(lease_file, monkeypatch):
    import configurator_run
    import tools
    gate = asyncio.Event()

    async def _erase():
        await gate.wait()

    task = asyncio.create_task(_erase())
    tools._ERASE_TASKS.add(task)
    task.add_done_callback(tools._ERASE_TASKS.discard)
    acquiring = asyncio.create_task(
        configurator_run.acquire(run_key="k", via="delegation", task="t"))
    await asyncio.sleep(0.05)
    assert not acquiring.done()
    gate.set()
    lease = await asyncio.wait_for(acquiring, 5)
    lease.release()


async def test_an_exception_before_the_run_releases_the_lease(lease_file, monkeypatch):
    import agent as agent_mod
    import tools

    def _boom(*a, **k):
        raise RuntimeError("no task")

    monkeypatch.setattr(tools, "_run_delegated_agent", _boom)
    cfg = _configurator_cfg()
    tok = agent_mod.origin_var.set(_origin(execution_role="configurator",
                                           _delegation_id="d"))
    try:
        with pytest.raises(RuntimeError):
            await tools._run_delegated_agent_bounded(cfg, "t", "")
    finally:
        agent_mod.origin_var.reset(tok)
    assert not lease_file.exists()
    import configurator_run
    assert configurator_run.holder() is None


async def test_a_specialist_run_takes_no_lease(lease_file, monkeypatch):
    import tools
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(tools, "_run_delegated_agent",
                        await _blocking_runner(started, release))
    run = asyncio.create_task(tools._run_delegated_agent_bounded(
        SimpleNamespace(role="finance", kind="specialist"), "t", ""))
    await asyncio.wait_for(started.wait(), 5)
    assert not lease_file.exists()
    release.set()
    await asyncio.wait_for(run, 5)


# -- interrupted runs: the lease file outlives a shutdown ----------------------

async def test_a_shutdown_cancel_keeps_the_lease_file_and_boot_announces_it_once(
        lease_file, monkeypatch):
    import agent as agent_mod
    import configurator_run
    import tools
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(tools, "_run_delegated_agent",
                        await _blocking_runner(started, release))
    monkeypatch.setattr(agent_mod, "process_stopping", lambda: True)
    cfg = _configurator_cfg()
    tok = agent_mod.origin_var.set(_origin(execution_role="configurator",
                                           _delegation_id="turn-9",
                                           desk={"role": "configurator", "chat_id": 123}))
    try:
        run = asyncio.create_task(tools._run_delegated_agent_bounded(cfg, "apply persona Ada", ""))
    finally:
        agent_mod.origin_var.reset(tok)
    await asyncio.wait_for(started.wait(), 5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert lease_file.exists()                       # kept for the next boot
    assert configurator_run.holder() is None         # but nobody holds it now

    record = configurator_run.claim_interrupted()
    assert record["task"] == "apply persona Ada"
    assert not lease_file.exists()
    assert configurator_run.claim_interrupted() is None
    sent: list = []

    async def _notify(text):
        sent.append(text)

    await configurator_run.announce_interrupted(record, recovered_ids=set(), notify=_notify)
    assert len(sent) == 1
    assert "apply persona Ada" in sent[0] and "Nothing was replayed" in sent[0]


async def test_a_cancel_outside_shutdown_releases_the_file(lease_file, monkeypatch):
    import agent as agent_mod
    import tools
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(tools, "_run_delegated_agent",
                        await _blocking_runner(started, release))
    monkeypatch.setattr(agent_mod, "process_stopping", lambda: False)
    tok = agent_mod.origin_var.set(_origin(execution_role="configurator", _delegation_id="d"))
    try:
        run = asyncio.create_task(tools._run_delegated_agent_bounded(_configurator_cfg(), "t", ""))
    finally:
        agent_mod.origin_var.reset(tok)
    await asyncio.wait_for(started.wait(), 5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert not lease_file.exists()


async def test_the_boot_notice_is_skipped_when_the_job_registry_announces_the_run(lease_file):
    import configurator_run
    sent: list = []

    async def _notify(text):
        sent.append(text)

    record = {"run_key": "deleg-1", "via": "delegation", "task": "t", "started_at": 0.0}
    await configurator_run.announce_interrupted(record, recovered_ids={"deleg-1"}, notify=_notify)
    assert sent == []


async def test_an_unreadable_lease_file_counts_as_held(lease_file):
    import configurator_run
    lease_file.write_text("{not json")
    record = configurator_run.claim_interrupted()
    assert record is not None and record["task"] == "a configuration task"
    assert not lease_file.exists()


# -- the Restart now button ----------------------------------------------------

class _KeyboardChannel:
    def __init__(self):
        self.questions: list = []
        self.edits: list = []

    async def deliver_operator_question(self, chat_id, body, labels, rid):
        self.questions.append((chat_id, body, list(labels), rid))
        return 900 + len(self.questions)

    async def edit_dm_message(self, chat_id, message_id, text, buttons=None):
        self.edits.append((message_id, text))
        return True


async def test_restart_needed_in_a_run_offers_a_button_spent_on_its_first_tap(
        lease_file, monkeypatch):
    import agent as agent_mod
    import configurator_run
    import result_broker
    import supervisor_restart
    import tools
    from verdict_broker import BROKER

    posted: list = []

    async def _no_post():
        posted.append("POST")
        return {"supervisor_status": 200}

    monkeypatch.setattr(supervisor_restart, "request_app_restart", _no_post)
    channel = _KeyboardChannel()
    monkeypatch.setattr(result_broker, "_telegram_channel", lambda: channel)
    monkeypatch.setattr(configurator_run, "_operator_identity",
                        lambda: SimpleNamespace(chat_id=123, operator_id=123))

    tok = agent_mod.origin_var.set(_origin(execution_role="configurator",
                                           _delegation_id="turn-5"))
    try:
        payload = _payload(await tools.casa_restart_supervised.handler({}))
    finally:
        agent_mod.origin_var.reset(tok)
    assert payload["deferred"] is True
    assert posted == []                               # nothing restarted inline

    assert await configurator_run.offer_restart("turn-5") is True
    assert await configurator_run.offer_restart("turn-5") is False     # once
    assert len(channel.questions) == 1
    _, body, labels, rid = channel.questions[0]
    assert labels == ["Restart now"]

    meta = BROKER.get_meta(namespace="resident_ask", scope="ask:123", request_id=rid)
    claim = BROKER.claim(namespace="resident_ask", scope=meta["_scope"], request_id=rid,
                         option_index=0, actor_id=123)
    assert BROKER.commit(claim) is True
    await BROKER.drain_hooks()
    assert posted == ["POST"]
    second = BROKER.claim(namespace="resident_ask", scope=meta["_scope"], request_id=rid,
                          option_index=0, actor_id=123)
    assert isinstance(second, str)                    # spent: a second tap is refused
    assert posted == ["POST"]


async def test_an_expired_restart_button_says_so(lease_file, monkeypatch):
    import configurator_run
    import result_broker
    from verdict_broker import BROKER
    channel = _KeyboardChannel()
    monkeypatch.setattr(result_broker, "_telegram_channel", lambda: channel)
    monkeypatch.setattr(configurator_run, "_operator_identity",
                        lambda: SimpleNamespace(chat_id=123, operator_id=123))
    monkeypatch.setattr(configurator_run, "RESTART_BUTTON_TTL_S", 0.05)
    configurator_run.note_restart("turn-6", "apply persona")
    assert await configurator_run.offer_restart("turn-6") is True
    await asyncio.sleep(0.2)
    await BROKER.drain_hooks()
    assert channel.edits and "expired" in channel.edits[-1][1]


async def test_a_restart_outside_a_delegated_run_posts_inline(monkeypatch):
    import agent as agent_mod
    import supervisor_restart
    import tools
    posted: list = []

    async def _post():
        posted.append(1)
        return {"supervisor_status": 200}

    monkeypatch.setattr(supervisor_restart, "request_app_restart", _post)
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        payload = _payload(await tools.casa_restart_supervised.handler({}))
    finally:
        agent_mod.origin_var.reset(tok)
    assert posted == [1] and payload["supervisor_status"] == 200


# -- D3: install time does not count toward the ceiling ------------------------

@pytest.mark.parametrize("name", ["plugin_add", "plugin_update",
                                  "specialist_install_inspect", "specialist_install_commit",
                                  "specialist_upgrade", "persona_install_inspect",
                                  "persona_install_commit"])
async def test_install_tools_pause_the_ceiling(name, monkeypatch):
    import tools
    clock = tools._CeilingClock(600.0)
    seen: list = []
    target = getattr(tools, name)
    # the fence wrapper (tools._fence_wrapped) closes over the paused layer
    paused = next(c.cell_contents for c in target.handler.__closure__
                  if hasattr(c.cell_contents, "__wrapped__"))

    async def _probe(args):
        seen.append(clock.left())
        return {"content": [{"type": "text", "text": "{}"}]}

    monkeypatch.setattr(paused, "__wrapped__", _probe)
    tok = tools._ceiling_clock.set(clock)
    try:
        await target.handler({})
    finally:
        tools._ceiling_clock.reset(tok)
    assert seen == [None]                    # paused while the body ran
    assert clock.left() is not None          # and resumed after


# -- D4: plugin health DMs are held while the lease is held --------------------

async def test_plugins_being_installed_are_held_until_the_lease_ends(lease_file, monkeypatch):
    import configurator_run
    import tools
    notified: list = []

    async def _notify():
        notified.append(1)

    monkeypatch.setattr(tools, "_notify_plugin_health_if_possible", _notify)
    lease = await configurator_run.acquire(run_key="k", via="delegation", task="t")
    tools._record_plugin_installer(["weather", "weather@bundle"])
    assert tools.plugins_under_live_install() == {"weather", "weather@bundle"}
    lease.release()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert tools.plugins_under_live_install() == set()
    assert notified == [1]


async def test_no_lease_means_nothing_is_held(lease_file):
    import tools
    tools._record_plugin_installer(["weather"])
    assert tools.plugins_under_live_install() == set()


async def test_a_run_still_unwinding_past_the_teardown_bound_keeps_the_lease(
        lease_file, monkeypatch):
    """The bounded wait may return while the run's task still unwinds past
    the teardown bound; the lease is released only when that task really ends."""
    import agent as agent_mod
    import configurator_run
    import tools
    started, unwound = asyncio.Event(), asyncio.Event()

    async def _stubborn(cfg, task_text, context_text, *, resolution=None,
                        output_format=None, tool_counts=None):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await unwound.wait()         # a teardown that takes its time
            raise

    monkeypatch.setattr(tools, "_run_delegated_agent", _stubborn)
    monkeypatch.setattr(tools, "_CEILING_TEARDOWN_BOUND_S", 0.05)
    monkeypatch.setattr(agent_mod, "process_stopping", lambda: False)
    tok = agent_mod.origin_var.set(_origin(execution_role="configurator", _delegation_id="d"))
    try:
        run = asyncio.create_task(tools._run_delegated_agent_bounded(_configurator_cfg(), "t", ""))
    finally:
        agent_mod.origin_var.reset(tok)
    await asyncio.wait_for(started.wait(), 5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert configurator_run.holder() is not None      # still unwinding: still held
    assert lease_file.exists()
    unwound.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert configurator_run.holder() is None
    assert not lease_file.exists()
