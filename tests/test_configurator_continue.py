"""Casa continues the configurator after an approval, or for an erase episode.

``configurator_continue.start`` never awaits: it takes the key's launch slot
and spawns one owned task that takes the configurator permit (waiting for it,
since the run that asked may still hold it), then a place on the
configurator's lane, registers the durable row with its completion callback,
and runs; the bounded runner takes the lease. These cases run the real job
registry, the real lane, the real limiter and the real lease; only the
delegated agent itself is a recorder."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import agent_lane
import approval_records
import configurator_continue as cc
import configurator_run
import tools as tools_mod
from specialist_registry import SpecialistRegistry, origin_from_job
from test_delegate_to_agent import _caller_cfg, _with_origin

pytestmark = pytest.mark.asyncio

OPERATOR = 42
CASA_BOT = 777
ASKED = "please install the weather specialist"
CONTINUATION = "The operator approved installing specialist:weather. Call it now."


class _Bus:
    def __init__(self) -> None:
        self.sent: list = []

    async def notify(self, msg) -> None:
        self.sent.append(msg)


class _Transport:
    """The Telegram transport as the launcher reads it: Casa's endpoint with
    its resident, Casa's route and the operator."""

    def __init__(self) -> None:
        self._casa = SimpleNamespace(default_agent="assistant", bot_id=CASA_BOT,
                                     has_bot_id=True)

    def casa_endpoint(self):
        return self._casa

    def casa_route(self):
        return CASA_BOT

    def operator_user_id(self):
        return OPERATOR

    def endpoint_for_route(self, route, *, site=""):
        return self._casa


class _Manager:
    def __init__(self, transport) -> None:
        self._t = transport

    def get(self, name):
        return self._t if name == "telegram" else None


class _Runs:
    """The delegated agent: records what each run saw, and can hold a run."""

    def __init__(self) -> None:
        self.seen: list = []
        self.gates: dict = {}           # key -> asyncio.Event a run waits on
        self.hook = None                # async callable(origin) run inside an ordinary run

    async def __call__(self, cfg, task_text, context_text, *, resolution=None,
                       output_format=None, tool_counts=None):
        import agent as agent_mod
        import log_cid
        origin = dict(agent_mod.origin_var.get(None) or {})
        key = "continuation" if origin.get(approval_records.APPROVAL_KEY) is not None \
            or origin.get("cid", "").startswith("appr-") else "ordinary"
        self.seen.append(SimpleNamespace(
            key=key, task=task_text, origin=origin, holder=configurator_run.holder(),
            cid=log_cid.cid_var.get(), quota=tools_mod._delegation_quota_key.get()))
        if key == "ordinary" and self.hook is not None:
            await self.hook(origin)
        gate = self.gates.get(key)
        if gate is not None:
            await gate.wait()
        return tools_mod.DelegatedOutput(text=f"{key} done")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(approval_records, "PATH", str(tmp_path / "approval-records.json"))
    approval_records.reset()
    monkeypatch.setattr(configurator_run, "LEASE_PATH", str(tmp_path / "lease.json"))
    configurator_run._reset_for_tests()
    agent_lane.reset()
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    import agent_loader
    from specialist_limits import SpecialistLimiter
    specialists = tmp_path / "specialists"
    specialists.mkdir()
    reg = SpecialistRegistry(str(specialists), tombstone_path=str(tmp_path / "del.json"))
    bus = _Bus()
    transport = _Transport()
    tools_mod.init_tools(
        _Manager(transport), bus, reg,
        agent_role_map={"assistant": _caller_cfg(delegates=("configurator",)),
                        "configurator": agent_loader.load_configurator()},
        specialist_limiter=SpecialistLimiter(max_global=4))
    runs = _Runs()
    monkeypatch.setattr(tools_mod, "_run_delegated_agent", runs)
    monkeypatch.setattr(cc, "RETRY_EVERY_S", 0.01)
    yield SimpleNamespace(tmp=tmp_path, reg=reg, bus=bus, runs=runs, transport=transport)
    cc._SLOTS.clear()
    agent_lane.reset()
    configurator_run._reset_for_tests()
    approval_records.reset()


async def _approved(values=None, *, asked=ASKED, key="weather:weather"):
    async with tools_mod._PLUGIN_TOOLS_LOCK:
        rec, _, _ = approval_records.request(
            "specialist_install", key, "identity-" + key,
            values if values is not None else {"config": {"A": "1"}, "secrets": []},
            asked=asked)
    return approval_records.approve(rec.rid)


async def _until(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


def _told():
    seen: list = []

    async def _on(outcome):
        seen.append(outcome)
    return seen, _on


def _jobs(env):
    return env.reg.job_registry.all()


async def _settled(env, n=1):
    await _until(lambda: len(env.bus.sent) >= n)
    await asyncio.sleep(0.02)


# -- 1-3: the launched run --------------------------------------------------------------

async def test_a_started_continuation_is_one_durable_task_with_the_operators_words(env):
    record = await _approved()
    seen, on = _told()
    assert cc.start(CONTINUATION, key=record.rid, approval=record, on_outcome=on) == "started"
    await _settled(env)
    (job,) = _jobs(env)
    assert job.task == ASKED                                # the operator's words, not Casa's text
    assert CONTINUATION not in job.task
    assert job.executor == "configurator" and job.delivery is None
    assert job.requester.role == "assistant"
    # the row survives a reload of the registry from disk
    from job_registry import JobRegistry
    again = JobRegistry(env.reg.job_registry.path)
    await again.load()
    row = again.get(job.id)
    assert row is not None and origin_from_job(row)["user_text"] == ASKED
    assert origin_from_job(row)["_tg_bot"] == CASA_BOT       # Casa's own bot, kept at reload
    assert seen == ["started"]


async def test_the_run_carries_the_approval_its_frozen_task_and_its_own_run_key(env):
    record = await _approved({"config": {"A": "1"}, "secrets": ["S"]})
    cc.start(CONTINUATION, key=record.rid, approval=record)
    await _settled(env)
    (run,) = env.runs.seen
    (job,) = _jobs(env)
    assert run.origin[approval_records.APPROVAL_KEY] == {"rid": record.rid,
                                                          "generation": record.generation}
    assert run.origin["_task_delivery"] is None
    assert run.task == CONTINUATION + cc.values_line(record.values)
    assert cc.APPROVED_VALUES_LINE + json.dumps(record.values, sort_keys=True) in run.task
    assert run.holder["run_key"] == job.id == run.quota     # the lease's key is the row's id
    assert run.holder["via"] == "delegation"
    # the durable row carries no approval
    assert approval_records.APPROVAL_KEY not in json.dumps(
        json.loads(open(env.reg.job_registry.path).read()))


async def test_one_completion_reaches_the_resident_with_the_operators_question(env):
    import agent as agent_mod
    record = await _approved()
    cc.start(CONTINUATION, key=record.rid, approval=record)
    await _settled(env)
    (msg,) = env.bus.sent
    (job,) = _jobs(env)
    assert msg.target == "assistant" and msg.content.delegation_id == job.id
    assert msg.content.status == "ok"
    assert agent_mod._chain_root_question(msg.content) == ASKED
    assert CONTINUATION not in agent_mod._chain_root_question(msg.content)
    assert msg.context["_tg_bot"] == CASA_BOT


async def test_the_continuation_text_names_the_call_and_the_approved_values():
    values = {"config": {"A": "1"}, "secrets": ["S"]}
    install = cc.continuation_text("specialist_install", slug="weather", component_id="c",
                                   version="1.0", root_digest="r", staged_dir="/s",
                                   receipt_id="rc", values=values)
    assert install.startswith("The operator approved installing specialist:weather.")
    assert "specialist_install_commit" in install
    assert install.endswith(cc.values_line(values))
    upgrade = cc.continuation_text("specialist_upgrade", slug="weather", values=None)
    assert "specialist_upgrade" in upgrade and "step 3" in upgrade
    assert cc.APPROVED_VALUES_LINE not in upgrade
    persona = cc.continuation_text("persona_install", persona_id="p", version="1",
                                   checksum="x", staged_dir="/s")
    assert "persona_install_commit" in persona


# -- 4: concurrent starts -------------------------------------------------------------

async def test_two_starts_of_one_approval_launch_one_run(env):
    record = await _approved()
    assert cc.start(CONTINUATION, key=record.rid, approval=record) == "started"
    assert cc.start(CONTINUATION, key=record.rid, approval=record) == "started"
    await _settled(env)
    await asyncio.sleep(0.05)
    assert len(_jobs(env)) == 1 and len(env.runs.seen) == 1


async def test_an_erase_episode_keeps_one_follower_and_runs_it_after_the_live_run(env):
    gate = env.runs.gates["continuation"] = asyncio.Event()
    told_b, on_b = _told()
    told_c, on_c = _told()
    assert cc.start("choice run", key="erase:q1", asked="uninstall x") == "started"
    await _until(lambda: len(env.runs.seen) == 1)
    assert cc.start("finish B", key="erase:q1", asked="uninstall x", on_outcome=on_b) == "queued"
    assert cc.start("finish C", key="erase:q1", asked="uninstall x", on_outcome=on_c) == "queued"
    gate.set()
    await _settled(env, 2)
    await asyncio.sleep(0.05)
    assert [r.task for r in env.runs.seen] == ["choice run", "finish C"]
    assert told_b == [] and told_c == ["started"]
    assert cc._SLOTS == {}


# -- 5: one acquisition order: permit, lane, lease ------------------------------------

async def _ordinary_delegation(task="install the weather specialist"):
    payload = await _with_origin(
        tools_mod.delegate_to_agent.handler(
            {"agent": "configurator", "task": task, "context": "", "mode": "sync"}),
        {"role": "assistant", "execution_role": "assistant", "channel": "telegram",
         "source": "telegram", "message_type": "channel_in", "chat_id": str(OPERATOR),
         "user_id": OPERATOR, "cid": "c1", "user_text": ASKED, "_operator_turn": True,
         "_tg_bot": CASA_BOT})
    return json.loads(payload["content"][0]["text"])


async def test_a_continuation_started_inside_the_asking_run_waits_for_its_permit(
        env, monkeypatch):
    """(a) the request tool's already_approved path from inside Ellen's ordinary
    configurator run: the continuation waits for the permit that run holds,
    then takes the lane and the lease after it ended."""
    monkeypatch.setattr(configurator_run, "LEASE_WAIT_S", 2.0)
    record = await _approved()
    outcomes: list = []

    async def _inside(origin):
        outcomes.append(cc.start(CONTINUATION, key=record.rid, approval=record))
        await asyncio.sleep(0.1)                    # the asking run goes on a while
    env.runs.hook = _inside
    payload = await _ordinary_delegation()
    assert payload["status"] == "pending"
    await _until(lambda: len(env.runs.seen) == 2)
    await _settled(env, 2)
    assert outcomes == ["started"]
    assert [r.key for r in env.runs.seen] == ["ordinary", "continuation"]
    assert all(m.content.status == "ok" for m in env.bus.sent)


async def test_an_ordinary_delegation_while_a_continuation_runs_refuses_busy_first(env):
    """(b) the continuation holds the permit, the lane and the lease: an ordinary
    delegation refuses busy in its prelaunch, with no row and no start line."""
    gate = env.runs.gates["continuation"] = asyncio.Event()
    record = await _approved()
    cc.start(CONTINUATION, key=record.rid, approval=record)
    await _until(lambda: len(env.runs.seen) == 1)
    lane = agent_lane.LANES.get("configurator")
    payload = await _ordinary_delegation()
    assert payload["status"] == "error" and payload["kind"] == "busy"
    assert len(_jobs(env)) == 1 and lane.waiting == 0
    gate.set()
    await _settled(env)


async def test_a_continuation_waiting_for_the_permit_holds_no_lane_place(env, monkeypatch):
    """(c) an ordinary delegation paused after its permit and before its lane
    place: the continuation waits for the permit holding nothing, so the
    ordinary run reserves, runs and ends, and the continuation runs after it."""
    monkeypatch.setattr(configurator_run, "LEASE_WAIT_S", 1.0)
    record = await _approved()
    paused, resume = asyncio.Event(), asyncio.Event()
    real = tools_mod._prelaunch

    async def _pausing(*a, **k):
        out = await real(*a, **k)
        paused.set()
        await resume.wait()
        return out
    monkeypatch.setattr(tools_mod, "_prelaunch", _pausing)
    ordinary = asyncio.create_task(_ordinary_delegation())
    await asyncio.wait_for(paused.wait(), 5)
    seen, on = _told()
    assert cc.start(CONTINUATION, key=record.rid, approval=record, on_outcome=on) == "started"
    await asyncio.sleep(0.05)
    assert agent_lane.LANES.get("configurator").waiting == 0     # it holds no place
    resume.set()
    assert (await ordinary)["status"] == "pending"
    await _settled(env, 2)
    assert [r.key for r in env.runs.seen] == ["ordinary", "continuation"]
    assert seen == ["started"] and all(m.content.status == "ok" for m in env.bus.sent)


async def test_an_expired_deadline_in_the_asking_run_does_not_reach_the_continuation(env):
    """5b: the asking run's own wait deadline has passed; the continuation
    computes its own, waits for the permit and runs. Its logs carry its own
    correlation id."""
    record = await _approved()
    limiter = tools_mod._specialist_limiter
    permit = limiter.try_acquire(f"{OPERATOR}:configurator")
    seen, on = _told()
    import log_cid
    log_cid.cid_var.set("caller-cid")
    import time
    with configurator_run.waiting_until(time.monotonic() - 1.0):
        assert cc.start(CONTINUATION, key=record.rid, approval=record, on_outcome=on) \
            == "started"
    await asyncio.sleep(0.1)
    permit.release()
    await _settled(env)
    assert seen == ["started"]
    (run,) = env.runs.seen
    assert run.cid.startswith("appr-") and run.cid == run.origin["cid"]


# -- 6-8: nothing a cancellation can leave behind ----------------------------------------

@pytest.mark.parametrize("where", ["permit", "register", "run"])
async def test_cancelling_the_caller_cannot_interrupt_the_launch(env, monkeypatch, where):
    record = await _approved()
    limiter = tools_mod._specialist_limiter
    permit = limiter.try_acquire(f"{OPERATOR}:configurator") if where == "permit" else None
    gate = asyncio.Event()
    if where == "register":
        real = env.reg.register_delegation

        async def _blocked(rec):
            await gate.wait()
            await real(rec)
        monkeypatch.setattr(env.reg, "register_delegation", _blocked)
    if where == "run":
        env.runs.gates["continuation"] = gate

    async def _caller():
        cc.start(CONTINUATION, key=record.rid, approval=record)
        await asyncio.sleep(10)

    for _ in range(5):
        caller = asyncio.create_task(_caller())
        await asyncio.sleep(0.01)
        caller.cancel()
        await asyncio.sleep(0.01)
    if permit is not None:
        permit.release()
    gate.set()
    await _settled(env)
    await asyncio.sleep(0.05)
    (job,) = _jobs(env)
    assert job.execution_state.value != "running"
    assert len(env.runs.seen) == 1
    assert cc._SLOTS == {} and agent_lane.LANES.get("configurator").waiting == 0
    assert limiter.try_acquire(f"{OPERATOR}:configurator") is not None


async def test_a_launch_that_fails_leaves_nothing_and_a_retry_launches(env, monkeypatch):
    record = await _approved()
    real = env.reg.register_delegation
    calls = {"n": 0}

    async def _once(rec):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        await real(rec)
    monkeypatch.setattr(env.reg, "register_delegation", _once)
    seen, on = _told()
    cc.start(CONTINUATION, key=record.rid, approval=record, on_outcome=on)
    await _until(lambda: seen)
    await asyncio.sleep(0.02)
    assert seen == ["failed"] and _jobs(env) == [] and cc._SLOTS == {}
    assert agent_lane.LANES.get("configurator").waiting == 0
    p = tools_mod._specialist_limiter.try_acquire(f"{OPERATOR}:configurator")
    assert p is not None
    p.release()
    assert approval_records.get(record.rid).state == "approved"
    assert cc.start(CONTINUATION, key=record.rid, approval=record) == "started"
    await _settled(env)
    assert len(_jobs(env)) == 1 and len(env.runs.seen) == 1


async def test_a_permit_wait_past_the_deadline_is_busy_and_leaves_nothing(env, monkeypatch):
    monkeypatch.setattr(configurator_run, "LEASE_WAIT_S", 0.1)
    record = await _approved()
    permit = tools_mod._specialist_limiter.try_acquire(f"{OPERATOR}:configurator")
    seen, on = _told()
    cc.start(CONTINUATION, key=record.rid, approval=record, on_outcome=on)
    await _until(lambda: seen)
    assert seen == ["busy"] and _jobs(env) == [] and cc._SLOTS == {}
    assert agent_lane.LANES.get("configurator").waiting == 0
    assert approval_records.get(record.rid).state == "approved"
    permit.release()
    assert cc.start(CONTINUATION, key=record.rid, approval=record) == "started"
    await _settled(env)
    assert len(env.runs.seen) == 1


async def test_shutdown_during_registration_leaves_the_approval_and_a_new_start_launches(
        env, monkeypatch):
    record = await _approved()
    real = env.reg.register_delegation
    entered, gate = asyncio.Event(), asyncio.Event()

    async def _slow(rec):
        await real(rec)              # the row is persisted
        entered.set()
        await gate.wait()            # ... and the task is cancelled before the callback
    monkeypatch.setattr(env.reg, "register_delegation", _slow)
    cc.start(CONTINUATION, key=record.rid, approval=record)
    await asyncio.wait_for(entered.wait(), 5)
    task = cc._SLOTS[record.rid].task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cc._SLOTS == {} and agent_lane.LANES.get("configurator").waiting == 0
    # the next boot: the registry is reloaded and recovers the cut-off row
    from job_registry import JobRegistry
    again = JobRegistry(env.reg.job_registry.path)
    await again.load()
    recovered = await again.recover_after_restart()
    (job,) = again.all()
    assert job.execution_state.value != "running" and job.id in {j.id for j in recovered}
    assert approval_records.get(record.rid).state == "approved"
    monkeypatch.setattr(env.reg, "register_delegation", real)
    assert cc.start(CONTINUATION, key=record.rid, approval=record) == "started"
    await _settled(env)
    assert len(env.runs.seen) == 1


async def test_a_superseded_approval_does_not_start(env):
    record = await _approved()
    async with tools_mod._PLUGIN_TOOLS_LOCK:
        approval_records.request("specialist_install", record.component_key, "other",
                                 {"config": {}, "secrets": []})
    assert cc.start(CONTINUATION, key=record.rid, approval=record) == "superseded"
    assert cc._SLOTS == {} and _jobs(env) == []
