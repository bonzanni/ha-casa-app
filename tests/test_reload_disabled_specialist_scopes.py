"""#786 — `enabled: false` on a specialist is honoured by EVERY reload scope
that reads it, not only `scope=agent` (INV-CFG-012).

At the base only `reload_agent`'s arm enforced the predicate. Measured through
the real `reload.dispatch` with a real `MessageBus`: after `agents`, `policies`,
`config_sync`, `full` and `triggers` read a specialist's `enabled: false`, a
message to the role was still ACCEPTED by the bus; `policies`/`full` constructed
a NEW Agent from the disabled config; `policies` left the delegation map
resolving the role; `triggers` re-registered its jobs and routes through the
retirement-hook path; `agents`/`config_sync`/`full` reported
`evicted_specialist_<role>` while the old Agent kept consuming its queue.

Every test here drives the REAL dispatcher, a REAL `MessageBus` (consumer loop
running), a REAL `CasaRuntime`, the REAL `AgentRegistry.build`, the real
`scheduled_asks`/`authz_grants`; only the filesystem-touching leaves are
stubbed, the way the survey measured it: the agent loader, `_construct_agent`,
a specialist-registry stand-in whose `load()` re-reads the loader's current
answer, `config_sync.run`, the plugin snapshot, the three post-reload
reconcilers, the setup-episode kick, the secret mint, and the `executors`
handler under `full`.

Assertions are COUNTS. The delegation map is inspected directly after the
dispatch — never re-synced by the test first, because `sync_agent_role_map`
would heal the very state under test. `reregister_for` calls are recorded with
their kwargs so the INV-TRIG-016 clause — a disabled specialist's routes are
unwound by `_unroute`, which passes NO `before_install` hook — is a count too.

Red at the base, per scope, for the reason the module docstring names; the
enabled controls, the same-named-resident control and race B are green at the
base by design and say so.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from broker_helpers import wait_until

pytestmark = pytest.mark.unit      # asyncio_mode = auto (pytest.ini)

ROLE = "finance"
TEARDOWN = "teardown_disabled_specialist"


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


def _cfg(*, enabled, role=ROLE, triggers=None):
    """The specialist's on-disk config as the loader would return it. The
    `character` block is what `AgentRegistry.build` and the delegation
    display-name derivation read; `triggers` carries one cron spec so a
    registration is observable."""
    if triggers is None:
        triggers = [SimpleNamespace(name="daily", type="cron", schedule="0 9 * * *",
                                    channel="telegram", prompt="q")]
    return SimpleNamespace(
        enabled=enabled, role=role, triggers=list(triggers), channels=[],
        character=SimpleNamespace(name=role.capitalize(), card=""),
        tools=SimpleNamespace(allowed=[]), delegates=[],
    )


class _Disk:
    """The file the loader reads. Tests flip `enabled` between reloads; every
    load returns a FRESH config object built from the current flag, so a
    stale object can never stand in for a stale read."""

    def __init__(self, *, enabled):
        self.enabled = enabled
        self.loads = 0

    def load(self, *a, **kw):
        self.loads += 1
        return _cfg(enabled=self.enabled)


class _Registry:
    """Stand-in with the base registry's accessor contract
    (`specialist_registry.py:256-282`): `all_configs()`/`get()` are ENABLED
    ONLY; `is_disabled()`/`disabled_roles()` expose the disabled set; `load()`
    re-scans by reading the disk stand-in's CURRENT flag. `fail_load` makes a
    scan raise, for the failure-injection arms."""

    def __init__(self, disk: _Disk, *, enabled_now: bool):
        self._disk = disk
        self._enabled = {ROLE: _cfg(enabled=True)} if enabled_now else {}
        self._disabled = set() if enabled_now else {ROLE}
        self.loads = 0
        self.fail_load: BaseException | None = None

    def load(self, *, roles_dir=None):
        self.loads += 1
        if self.fail_load is not None:
            raise self.fail_load
        cfg = self._disk.load()
        if getattr(cfg, "enabled", True) is False:
            self._enabled, self._disabled = {}, {ROLE}
        else:
            self._enabled, self._disabled = {ROLE: cfg}, set()

    def all_configs(self):
        return dict(self._enabled)

    def get(self, role):
        return self._enabled.get(role)

    def is_disabled(self, role):
        return role in self._disabled

    def disabled_roles(self):
        return sorted(self._disabled)

    def load_failures(self):
        return []


class _TriggerSpy:
    """Records every `reregister_for` call with its kwargs. `fail_unroute`
    makes the empty-declaration call (the `_unroute` shape) raise."""

    def __init__(self):
        self.calls: list = []
        self.fail_unroute: BaseException | None = None

    def reregister_for(self, role, triggers, channels, **kw):
        self.calls.append(SimpleNamespace(
            args=(role, list(triggers), list(channels)), kwargs=dict(kw)))
        if self.fail_unroute is not None and not triggers and not channels:
            raise self.fail_unroute

    def plugin_overlay_unavailable(self):
        return False

    def callback_overlay_unavailable(self):
        return False


def _fake_agent(tag: str):
    async def handle_message(msg):
        from bus import BusMessage, MessageType
        return BusMessage(type=MessageType.RESPONSE, source=msg.target,
                          target=msg.source, content=f"served-by:{tag}")
    agent = MagicMock(name=f"agent-{tag}")
    agent.handle_message = handle_message
    agent.aclose = AsyncMock()
    agent.active_plugin_binding = {}
    agent.config = SimpleNamespace(delegates=[])
    return agent


class _Harness:
    def __init__(self, *, disk, registry, runtime, bus, trigger_registry,
                 constructed, unroutes):
        self.disk = disk
        self.registry = registry
        self.runtime = runtime
        self.bus = bus
        self.trigger_registry = trigger_registry
        self.constructed = constructed
        self.unroutes = unroutes

    # -- probes ---------------------------------------------------------
    async def send_checked(self):
        from bus import BusMessage, MessageType
        return await self.bus.send_checked(BusMessage(
            type=MessageType.NOTIFICATION, source="test", target=ROLE, content="ping"))

    def live_count(self) -> int:
        return sum(r == ROLE for r in self.runtime.agents)

    def map_count(self) -> int:
        import tools as tools_mod
        return sum(r == ROLE for r in tools_mod._agent_role_map)

    def empty_unroutes(self):
        return [c for c in self.trigger_registry.calls if c.args == (ROLE, [], [])]

    def hooked_calls(self):
        return [c for c in self.trigger_registry.calls if "before_install" in c.kwargs]


def _flat(actions) -> str:
    """One string over the envelope's rows, composition rows included
    (`config_sync` nests a sub-handler's list as `agents:[...]`)."""
    return "\n".join(str(a) for a in actions)


def _teardown_rows(actions, role=ROLE) -> int:
    """How many rows name a disabled teardown of *role* — the bare
    `teardown_disabled_specialist` (single-role scopes) or the `_<role>`
    suffixed row (multi-role scopes), under any composition prefix."""
    return _flat(actions).count(f"{TEARDOWN}_{role}") + sum(
        1 for a in actions if str(a).split(":")[-1] == TEARDOWN)


def _construct_or_register_rows(actions, role=ROLE) -> int:
    flat = _flat(actions)
    return (flat.count("construct_agent") + flat.count("reregister_bus")
            + flat.count("reregister_triggers") + flat.count(f"added_specialist_{role}")
            + flat.count(f"registered_triggers_{role}"))


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    """A live, enabled `finance` specialist as a prior reload leaves it —
    registry enabled, `runtime.agents` holding an Agent whose bus consumer is
    running, the delegation map naming it — with the file now saying
    `enabled: false`. Leaves stubbed exactly as the survey measured."""
    import reload as reload_mod
    import tools as tools_mod
    from agent_registry import AgentRegistry
    from bus import MessageBus
    from runtime import CasaRuntime

    agents_dir = tmp_path / "agents"
    (agents_dir / "specialists" / ROLE).mkdir(parents=True)
    (tmp_path / "policies").mkdir()
    (tmp_path / "secrets").mkdir()

    disk = _Disk(enabled=False)
    registry = _Registry(disk, enabled_now=True)
    trigger_registry = _TriggerSpy()
    constructed: list[str] = []
    unroutes: list[str] = []

    monkeypatch.setattr("agent_loader.load_agent_from_dir", disk.load)
    monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
    monkeypatch.setattr("agent_home.provision_agent_home",
                        lambda *, role, home_root, defaults_root: None)

    def construct(*, cfg, runtime, agent_registry=None):
        constructed.append(cfg.role)
        return _fake_agent(f"NEW{len(constructed)}(enabled={cfg.enabled})")
    monkeypatch.setattr(reload_mod, "_construct_agent", construct)

    real_unroute = reload_mod._unroute

    def unroute(reg, role):
        unroutes.append(role)
        return real_unroute(reg, role)
    monkeypatch.setattr(reload_mod, "_unroute", unroute)

    # Filesystem-touching leaves, inert.
    monkeypatch.setattr("config_sync.run", lambda **kw: 0)
    monkeypatch.setattr("plugin_registry.reload_snapshot", lambda: None)
    monkeypatch.setattr("trigger_reconcile.reconcile_from_runtime", AsyncMock())
    monkeypatch.setattr("callback_reconcile.reconcile_from_runtime", AsyncMock())
    monkeypatch.setattr("event_reconcile.reconcile_plugin_events", AsyncMock())
    monkeypatch.setattr("plugin_setup_episodes.kick", lambda: None)
    monkeypatch.setattr("resident_trigger_secrets.mint_for_specs",
                        lambda specs, *, secrets_dir, role: [])
    monkeypatch.setattr("trigger_reconcile.SECRETS_DIR", str(tmp_path / "secrets"))

    async def stub_executors(rt, *, role=None):
        return ["stubbed"]
    monkeypatch.setitem(reload_mod._HANDLERS, "executors", stub_executors)

    bus = MessageBus()
    old = _fake_agent("OLD(enabled=True)")
    bus.register(ROLE, old.handle_message)
    bus.start_agent_loop(ROLE)

    runtime = CasaRuntime(
        agents={ROLE: old}, role_configs={}, specialist_registry=registry,
        executor_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_registry=AgentRegistry.build(residents={}, specialists=registry.all_configs()),
        trigger_registry=trigger_registry, mcp_registry=MagicMock(),
        session_registry=MagicMock(), channel_manager=MagicMock(),
        bus=bus, engagement_driver=MagicMock(), claude_code_driver=MagicMock(),
        policy_lib=MagicMock(),
        config_dir=str(tmp_path), agents_dir=str(agents_dir),
        home_root=str(tmp_path / "home"), defaults_root="/opt/casa",
    )
    # The boot-like delegation map: the enabled config, seeded BEFORE the
    # dispatch and never re-synced by a test.
    monkeypatch.setattr(tools_mod, "_agent_role_map", dict(registry.all_configs()))
    monkeypatch.setattr(tools_mod, "_specialist_registry", registry, raising=False)

    h = _Harness(disk=disk, registry=registry, runtime=runtime, bus=bus,
                 trigger_registry=trigger_registry, constructed=constructed,
                 unroutes=unroutes)
    # The pre-state the survey started every row from.
    assert await h.send_checked() == "accepted"
    assert h.live_count() == 1 and h.map_count() == 1
    assert registry.is_disabled(ROLE) is False
    try:
        yield h
    finally:
        for name in list(bus.queues):
            bus.unregister(name)
        for t in bus.agent_loop_tasks():
            t.cancel()
        await asyncio.gather(*bus.agent_loop_tasks(), return_exceptions=True)


async def _dispatch(h: _Harness, scope: str, *, role=None):
    from reload import dispatch
    return await dispatch(scope, runtime=h.runtime, role=role)


def _assert_retired(h: _Harness, scope: str, envelope: dict, *, expect_scans: int = 1):
    """The per-scope outcome INV-CFG-012 promises, as counts."""
    assert envelope["status"] == "ok", envelope
    assert h.live_count() == 0, (scope, h.runtime.agents)
    assert h.constructed.count(ROLE) == 0, (scope, h.constructed)
    assert h.map_count() == 0, (scope, "delegation map still resolves the role")
    assert h.registry.is_disabled(ROLE) is True, (scope, "registry never re-scanned")
    empty = h.empty_unroutes()
    assert len(empty) == 1, (scope, h.trigger_registry.calls)
    assert sum("before_install" in c.kwargs for c in empty) == 0, (scope, empty)
    assert len(h.hooked_calls()) == 0, (scope, h.trigger_registry.calls)
    assert _teardown_rows(envelope["actions"]) == 1, (scope, envelope["actions"])
    assert _construct_or_register_rows(envelope["actions"]) == 0, (scope, envelope["actions"])


# --------------------------------------------------------------------------
# the five unpinned scopes — red at the base, one row each (dossier 786 §3)
# --------------------------------------------------------------------------


async def test_agents_sweep_retires_a_disabled_specialist_it_reports(harness):
    """Row B at the base: the re-scan drops the role and the envelope says
    `evicted_specialist_finance`, but the runtime eviction is keyed on the
    DIRECTORY (still present), so the old Agent, its bus target, jobs and
    routes all survive — `send_checked` is still `accepted`."""
    h = harness
    env = await _dispatch(h, "agents")
    _assert_retired(h, "agents", env)
    assert await h.send_checked() == "no_target"
    actions = env["actions"]
    assert actions.count(f"{TEARDOWN}_{ROLE}") == 1, actions
    # The registry-diff row is unchanged and exactly once: the registry DID
    # drop the role in this re-scan.
    assert actions.count(f"evicted_specialist_{ROLE}") == 1, actions


async def test_policies_cascade_retires_instead_of_constructing(harness):
    """Row C at the base: the cascade loads the disabled config, constructs
    ONE new Agent from it, replaces the live one, registers its bus handler,
    never re-scans the registry, and leaves the delegation map naming it."""
    h = harness
    env = await _dispatch(h, "policies")
    _assert_retired(h, "policies", env)
    assert await h.send_checked() == "no_target"
    actions = env["actions"]
    assert actions.count(f"{TEARDOWN}_{ROLE}") == 1, actions
    assert "cascaded_to_1_roles" in actions, actions


async def test_config_sync_inherits_the_agents_retirement(harness):
    """Row D at the base: `config_sync` cascades `agents` FIRST, which reports
    the registry eviction and leaves the directory-present Agent live; the
    `policies` cascade that follows sees `cascaded_to_0_roles` and cannot
    repair it."""
    h = harness
    env = await _dispatch(h, "config_sync")
    _assert_retired(h, "config_sync", env)
    assert await h.send_checked() == "no_target"
    # The row lives INSIDE the nested `agents:[...]` composition row.
    assert _flat(env["actions"]).count(f"{TEARDOWN}_{ROLE}") == 1, env["actions"]


async def test_full_retires_once_and_never_constructs(harness):
    """Row E at the base — the worst arm: `policies` constructs and registers
    a NEW Agent from the disabled config, `agents` then reports it evicted and
    keeps it (directory present), and the per-role loop iterates the
    post-sweep set so the only honouring arm is never reached."""
    h = harness
    env = await _dispatch(h, "full")
    _assert_retired(h, "full", env)
    assert await h.send_checked() == "no_target"
    # Exactly one retirement across the whole composition (the specifier
    # named the `agents:` step; the composition order decides which step
    # reads the file first — the COUNT is the outcome, the prefix is not).
    assert _teardown_rows(env["actions"]) == 1, env["actions"]


async def test_triggers_unroutes_instead_of_registering(harness):
    """Row F at the base: the disabled config's cron is REGISTERED through
    `_register_and_reconcile` — `reregister_for(role, ['daily'], [])` WITH the
    `before_install` retirement hook — and the registry is re-scanned only
    afterwards; the Agent keeps its queue and the map keeps the role."""
    h = harness
    env = await _dispatch(h, "triggers", role=ROLE)
    _assert_retired(h, "triggers", env)
    assert await h.send_checked() == "no_target"
    actions = env["actions"]
    assert actions.count(TEARDOWN) == 1, actions
    assert "reregister_triggers" not in actions, actions
    # No registration of the disabled declaration at all — not even one.
    assert sum(c.args[1] != [] for c in h.trigger_registry.calls) == 0, h.trigger_registry.calls


# --------------------------------------------------------------------------
# the teardown's caller-facing contract, and the controls (green at the base)
# --------------------------------------------------------------------------


async def test_in_flight_request_is_cancelled_exactly_once(harness):
    """A request the disabled role is serving when the sweep reads
    `enabled: false` is settled with the cancelled-caller convention
    (`bus.py:363`) exactly once; at the base the handler is never cancelled
    and the caller waits out the request timeout."""
    from bus import BusMessage, MessageType
    h = harness
    started = asyncio.Event()
    park = asyncio.Event()
    completions: list[str] = []

    async def parked_handler(msg):
        started.set()
        await park.wait()
        completions.append(msg.id)
        return BusMessage(type=MessageType.RESPONSE, source=ROLE, target="test",
                          content="late")
    h.bus.register(ROLE, parked_handler)

    msg = BusMessage(type=MessageType.REQUEST, source="test", target=ROLE, content="q")
    req = asyncio.create_task(h.bus.request(msg, timeout=60))
    await wait_until(started.is_set, timeout=5.0)

    env = await _dispatch(h, "agents")
    assert env["status"] == "ok", env
    resp = await asyncio.wait_for(req, 5.0)
    assert resp.content == f"handler error: cancelled: {msg.id}"
    assert completions == []
    assert len([r for r in [resp] if r.reply_to == msg.id]) == 1
    park.set()


async def test_enabled_specialist_is_still_reconstructed_by_policies(harness):
    """Control, green at the base: the gate must not touch an ENABLED
    specialist — the cascade constructs it exactly once, registers its bus
    handler, and names no teardown."""
    h = harness
    h.disk.enabled = True
    env = await _dispatch(h, "policies")
    assert env["status"] == "ok", env
    assert h.constructed.count(ROLE) == 1, h.constructed
    assert h.live_count() == 1
    assert await h.send_checked() == "accepted"
    assert _teardown_rows(env["actions"]) == 0, env["actions"]
    assert len(h.unroutes) == 0


async def test_enabled_specialist_is_still_registered_by_triggers(harness):
    """Control, green at the base: an ENABLED specialist's declaration is
    registered exactly once (with the retirement hook, as every install
    is), nothing is unrouted, and no teardown is named."""
    h = harness
    h.disk.enabled = True
    env = await _dispatch(h, "triggers", role=ROLE)
    assert env["status"] == "ok", env
    registrations = [c for c in h.trigger_registry.calls if c.args[1] != []]
    assert len(registrations) == 1, h.trigger_registry.calls
    assert [t.name for t in registrations[0].args[1]] == ["daily"]
    assert len(h.empty_unroutes()) == 0
    assert "reregister_triggers" in env["actions"], env["actions"]
    assert _teardown_rows(env["actions"]) == 0, env["actions"]
    assert h.live_count() == 1


async def test_enabled_specialist_is_still_backfilled_by_agents(harness):
    """Control, green at the base: a registry-known ENABLED specialist with
    no Agent object yet is backfilled exactly once and bus-registered; no
    teardown is named."""
    h = harness
    h.disk.enabled = True
    h.bus.unregister(ROLE)
    h.runtime.agents.clear()
    env = await _dispatch(h, "agents")
    assert env["status"] == "ok", env
    assert h.constructed.count(ROLE) == 1, h.constructed
    assert h.live_count() == 1
    assert await h.send_checked() == "accepted"
    assert _teardown_rows(env["actions"]) == 0, env["actions"]
    assert len(h.unroutes) == 0


async def test_same_named_resident_is_never_torn_down_from_disabled_roles(harness, tmp_path):
    """Control, green at the base: a RESIDENT whose name the registry lists
    among its disabled specialists (a resident config carries no `enabled`
    attribute) keeps its Agent and its bus consumer through an `agents`
    sweep; the disabled set is a specialist fact and must never reach a
    resident."""
    h = harness
    (tmp_path / "agents" / ROLE).mkdir()
    resident_cfg = _cfg(enabled=True)
    del resident_cfg.enabled
    resident_cfg.channels = ["telegram"]
    h.runtime.role_configs[ROLE] = resident_cfg
    h.runtime.refresh_personality_maps()
    h.registry._enabled, h.registry._disabled = {}, {ROLE}
    before = dict(h.bus.handlers)
    env = await _dispatch(h, "agents")
    assert env["status"] == "ok", env
    assert h.live_count() == 1
    assert h.bus.handlers == before
    assert await h.send_checked() == "accepted"
    assert _teardown_rows(env["actions"]) == 0, env["actions"]
    assert len(h.unroutes) == 0


# --------------------------------------------------------------------------
# the disabled DECISION is made under `agent:<role>` (seam round 2, S1)
# --------------------------------------------------------------------------


def _waiters(lock) -> int:
    waiters = getattr(lock, "_waiters", None)
    return len(waiters) if waiters else 0


async def test_race_a_triggers_arm_reads_and_retires_under_the_role_lock(harness, monkeypatch):
    """An enabled `scope=agent` reload is paused inside `_construct_agent`;
    the file flips to `enabled: false`; a `triggers` reload is dispatched.
    The triggers arm must PARK on `agent:finance` before reading anything,
    then read the file and retire what the agent arm installed: live 0.
    At the base the triggers arm never takes that lock (it reads and acts
    concurrently with the swap window), so the wait for it to park times
    out — the defect is the absence of the serialisation."""
    import reload as reload_mod
    import threading
    h = harness
    h.disk.enabled = True
    entered = threading.Event()
    release = threading.Event()

    def paused_construct(*, cfg, runtime, agent_registry=None):
        h.constructed.append(cfg.role)
        entered.set()
        assert release.wait(10), "construct never released"
        return _fake_agent(f"NEW(enabled={cfg.enabled})")
    monkeypatch.setattr(reload_mod, "_construct_agent", paused_construct)

    agent_task = asyncio.create_task(_dispatch(h, "agent", role=ROLE))
    await wait_until(entered.is_set, timeout=5.0)
    h.disk.enabled = False
    lock = reload_mod._get_lock(reload_mod._lock_key("agent", ROLE))
    triggers_task = asyncio.create_task(_dispatch(h, "triggers", role=ROLE))
    try:
        await wait_until(lambda: lock.locked() and _waiters(lock) == 1, timeout=5.0)
    finally:
        release.set()
    env_agent, env_triggers = await asyncio.gather(agent_task, triggers_task)
    assert env_agent["status"] == "ok", env_agent
    assert env_triggers["status"] == "ok", env_triggers
    assert h.live_count() == 0, h.runtime.agents
    assert await h.send_checked() == "no_target"
    assert env_triggers["actions"].count(TEARDOWN) == 1, env_triggers["actions"]
    assert h.registry.is_disabled(ROLE) is True


async def test_race_b_sweep_revalidates_under_the_role_lock_and_skips(harness):
    """The `agents` sweep scans while the file says `enabled: false`, then
    parks on `agent:finance` behind an `agent` reload that reads
    `enabled: true` and installs. Under the lock the sweep re-validates
    against the registry that reload re-scanned — enabled — and SKIPS:
    live 1, no teardown row. Green at the base by design (the base sweep
    never tears a directory-present role down at all); a sweep that acts on
    its pre-lock snapshot fails it."""
    import reload as reload_mod
    h = harness
    lock = reload_mod._get_lock(reload_mod._lock_key("agent", ROLE))
    await lock.acquire()
    try:
        agent_task = asyncio.create_task(_dispatch(h, "agent", role=ROLE))
        await wait_until(lambda: _waiters(lock) == 1, timeout=5.0)
        sweep_task = asyncio.create_task(_dispatch(h, "agents"))
        # The sweep's own scan reads the file DISABLED …
        await wait_until(lambda: h.registry.loads == 1, timeout=5.0)
        await asyncio.sleep(0)
        # … and only THEN does the file flip: the agent reload (first in
        # the lock's queue) reads it enabled.
        h.disk.enabled = True
    finally:
        lock.release()
    env_agent, env_sweep = await asyncio.gather(agent_task, sweep_task)
    assert env_agent["status"] == "ok", env_agent
    assert env_sweep["status"] == "ok", env_sweep
    assert "construct_agent" in env_agent["actions"], env_agent["actions"]
    assert h.live_count() == 1, h.runtime.agents
    assert await h.send_checked() == "accepted"
    assert _teardown_rows(env_sweep["actions"]) == 0, env_sweep["actions"]
    assert h.registry.is_disabled(ROLE) is False


# --------------------------------------------------------------------------
# no swallowed failure on the retirement path (seam rounds 1-3, generalised)
# --------------------------------------------------------------------------


async def test_failed_bus_unregister_is_named_and_unroute_still_runs(harness, monkeypatch):
    h = harness

    async def boom(name):
        raise RuntimeError("bus refused")
    monkeypatch.setattr(h.bus, "unregister_and_wait", boom)
    env = await _dispatch(h, "agent", role=ROLE)
    assert env["status"] == "ok", env
    assert env["actions"].count("teardown_incomplete_bus_unregister") == 1, env["actions"]
    assert len(h.empty_unroutes()) == 1


async def test_failed_unroute_is_named(harness):
    h = harness
    h.trigger_registry.fail_unroute = RuntimeError("scheduler refused")
    env = await _dispatch(h, "agent", role=ROLE)
    assert env["status"] == "ok", env
    assert env["actions"].count("teardown_incomplete_unroute") == 1, env["actions"]
    assert env["actions"].count(TEARDOWN) == 1


async def test_failed_sweep_scan_admits_no_disabled_candidate(harness):
    """Registry (last generation) says disabled, the file now says enabled,
    the Agent is live, and this sweep's scan RAISES: a stale generation must
    not retire a re-enabled role — zero teardowns, the failure named."""
    h = harness
    h.registry._enabled, h.registry._disabled = {}, {ROLE}
    h.disk.enabled = True
    h.registry.fail_load = OSError("scan refused")
    env = await _dispatch(h, "agents")
    assert env["status"] == "ok", env
    assert env["actions"].count("specialist_scan_failed") == 1, env["actions"]
    assert _teardown_rows(env["actions"]) == 0, env["actions"]
    assert h.live_count() == 1
    assert await h.send_checked() == "accepted"


async def test_failed_role_map_refresh_is_named(harness, monkeypatch):
    import tools as tools_mod

    def boom(runtime):
        raise RuntimeError("map refused")
    monkeypatch.setattr(tools_mod, "sync_agent_role_map", boom)
    h = harness
    env = await _dispatch(h, "agent", role=ROLE)
    assert env["status"] == "ok", env
    assert env["actions"].count("refresh_role_map_failed") == 1, env["actions"]
    assert env["actions"].count(TEARDOWN) == 1


async def test_cascade_rescan_failure_keeps_the_teardown_rows_and_names_itself(harness):
    """The policies cascade tears the role down, then the registry re-scan
    raises: the rows already earned survive and the failure is a row naming
    the STEP (the reload error's kind, `specialist_reload_failed` — the same
    kind the single-role scopes return as their error envelope), so the
    envelope says both what happened and what did not. The specifier wrote
    the row as `failed:finance:OSError`; the acceptor's scope round asked
    that a failed step be NAMED, and an exception's type name does not name
    a step — so the row carries the kind, and the type name only for an
    exception that has none."""
    h = harness
    h.registry.fail_load = OSError("scan refused")
    env = await _dispatch(h, "policies")
    assert env["status"] == "ok", env
    assert env["actions"].count(f"{TEARDOWN}_{ROLE}") == 1, env["actions"]
    assert env["actions"].count(f"failed:{ROLE}:specialist_reload_failed") == 1, env["actions"]
    assert env["actions"].count(f"failed:{ROLE}:OSError") == 0, env["actions"]
    assert h.live_count() == 0


async def test_failed_registry_rebuild_is_a_named_error_after_the_teardown(harness, monkeypatch):
    """Acceptor's scope round: the CORE teardown succeeds, then the
    `AgentRegistry` rebuild raises. The single-role scope must return an
    error envelope whose KIND names that step and whose message says the
    teardown already happened — at the base the raise reaches the dispatcher
    as an `unexpected` error carrying only the exception's text, so the
    registry entry the role still holds is neither removed nor named."""
    from agent_registry import AgentRegistry
    h = harness
    real_build = AgentRegistry.build

    def boom(**kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(AgentRegistry, "build", staticmethod(boom))
    env = await _dispatch(h, "agent", role=ROLE)
    assert env["status"] == "error", env
    assert env["kind"] == "agent_registry_rebuild_failed", env
    assert "torn down" in env["message"], env
    assert h.live_count() == 0
    assert await h.send_checked() == "no_target"
    monkeypatch.setattr(AgentRegistry, "build", real_build)


async def test_cascade_rebuild_failure_keeps_the_teardown_rows_and_names_the_step(harness, monkeypatch):
    """The same arm through the policies cascade: the teardown row survives
    and the failure row names the rebuild step, not the exception's type."""
    from agent_registry import AgentRegistry
    h = harness
    real_build = AgentRegistry.build

    def boom(**kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(AgentRegistry, "build", staticmethod(boom))
    env = await _dispatch(h, "policies")
    assert env["status"] == "ok", env
    assert env["actions"].count(f"{TEARDOWN}_{ROLE}") == 1, env["actions"]
    assert env["actions"].count(f"failed:{ROLE}:agent_registry_rebuild_failed") == 1, env["actions"]
    assert env["actions"].count(f"failed:{ROLE}:RuntimeError") == 0, env["actions"]
    assert h.live_count() == 0
    monkeypatch.setattr(AgentRegistry, "build", real_build)
