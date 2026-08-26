"""#606 — the plugin routing sentinel, and the health regeneration that must
run on BOTH exits of a consent-approve reconcile.

The defect: both reconcilers raise from inside `_RECONCILE_LOCK` while the
health regeneration sat AFTER the `async with`, so the raise path skipped it
entirely — an approve-time reconcile that failed left the acked trigger's stale
`trigger_pending_ack` row standing with nothing to clear it.

Mirroring the event twin's `try/finally` alone was double-DO-NOT-SHIP'd on the
issue, for a reason these tests pin. On the raise path the overlay was already
swapped to `{}` — indistinguishable from "nothing should route" — and
`current_issues()` returned `issue_state()[1]`, discarding `ok`, so a failed
compute degraded to `[]`. Regenerating health there would have replaced one
false report with a worse one: all-clear, while plugin ingress was shut.

So the three arms ship together: a typed sentinel that closes ingress at every
accessor and is never confusable with an authoritative empty map, health rows
that state the two ways routing can be unknown, and only then the `finally`.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import callback_reconcile as cr
import trigger_reconcile as tr
import trigger_registry as treg
from trigger_registry import ROUTING_UNAVAILABLE, TriggerRegistry

pytestmark = pytest.mark.unit

AUTH = {"mode": "static_header", "header": "X-API-Key",
        "tolerance_secs": 300, "secret_owner": "casa"}
OVERLAY = {"plg-x--y": {"role": "assistant", "clearance": "private",
                        "auth": AUTH}}
CB_OVERLAY = {"plg-x--auth": {"plugin": "x", "declared": "auth",
                              "path": "/store/x"}}


def _registry():
    return TriggerRegistry(scheduler=None, app=None, bus=None)


# --- the sentinel itself ---------------------------------------------------

def test_the_sentinel_is_a_unique_singleton_equal_only_to_itself():
    assert ROUTING_UNAVAILABLE is treg.ROUTING_UNAVAILABLE
    assert ROUTING_UNAVAILABLE == ROUTING_UNAVAILABLE
    assert ROUTING_UNAVAILABLE != {}
    assert {} != ROUTING_UNAVAILABLE
    assert not isinstance(ROUTING_UNAVAILABLE, dict)


def test_a_fresh_registry_starts_unavailable_not_empty():
    """Before the first successful reconcile nothing authoritative has been
    computed. Starting at `{}` claimed otherwise — it is a positive statement
    that nothing should route, made by a process that has computed nothing."""
    r = _registry()
    assert r.plugin_overlay_unavailable() is True
    assert r.callback_overlay_unavailable() is True


def test_an_authoritative_empty_map_is_not_unavailable():
    """The distinction the whole station rests on: `{}` is a real compute
    result. Both close ingress; only one is a claim."""
    r = _registry()
    r.replace_plugin_overlay({})
    r.replace_callback_overlay({})
    assert r.plugin_overlay_unavailable() is False
    assert r.callback_overlay_unavailable() is False
    assert r.plugin_overlay_names() == []


# --- containment: every accessor closes ingress -----------------------------

def test_every_plugin_accessor_closes_ingress_under_the_sentinel():
    """The fail-open risk this station carries: one accessor that forgets the
    sentinel keeps a plugin route live behind unknown state. All of them are
    exercised here, not one representative."""
    r = _registry()
    r.replace_plugin_overlay(OVERLAY)
    assert r.get_webhook_target("plg-x--y") == "assistant"
    assert r.get_clearance("plg-x--y") == "private"
    assert r.get_auth_policy("plg-x--y") == AUTH
    assert r.plugin_overlay_names() == ["plg-x--y"]

    r.replace_plugin_overlay(ROUTING_UNAVAILABLE)
    assert r.get_webhook_target("plg-x--y") is None
    assert r.get_auth_policy("plg-x--y") is None
    assert r.get_clearance("plg-x--y") == "public"     # never a stale tier
    assert r.plugin_overlay_names() == []


def test_every_callback_accessor_closes_ingress_under_the_sentinel():
    r = _registry()
    r.replace_callback_overlay(CB_OVERLAY)
    assert r.get_callback("plg-x--auth") is not None
    assert r.callback_overlay_names() == ["plg-x--auth"]

    r.replace_callback_overlay(ROUTING_UNAVAILABLE)
    assert r.get_callback("plg-x--auth") is None
    assert r.callback_overlay_names() == []


def test_resident_trigger_routing_is_untouched_by_the_sentinel():
    """The sentinel is a statement about the PLUGIN overlay only. Closing a
    resident's own webhook because a plugin reconcile failed would be a far
    larger outage than the one being contained."""
    r = _registry()
    r._webhook_targets["daily"] = "assistant"
    r._webhook_clearances["daily"] = "private"
    r._webhook_auth_policies["daily"] = AUTH
    r.replace_plugin_overlay(ROUTING_UNAVAILABLE)
    assert r.get_webhook_target("daily") == "assistant"
    assert r.get_clearance("daily") == "private"
    assert r.get_auth_policy("daily") == AUTH


def test_the_snapshot_returns_the_singleton_by_identity():
    """Not a copy, and emphatically not `{}`: a caller deriving a swept
    replacement from a coerced empty dict would publish an authoritative
    "nothing should route" built from state it never had."""
    r = _registry()
    r.replace_plugin_overlay(ROUTING_UNAVAILABLE)
    assert r.plugin_overlay_snapshot() is ROUTING_UNAVAILABLE


def test_replacing_with_the_sentinel_stores_the_singleton_not_a_copy():
    r = _registry()
    r.replace_plugin_overlay(ROUTING_UNAVAILABLE)
    r.replace_callback_overlay(ROUTING_UNAVAILABLE)
    assert r._plugin_overlay is ROUTING_UNAVAILABLE
    assert r._callback_overlay is ROUTING_UNAVAILABLE


def test_a_real_map_still_round_trips_and_is_copied():
    r = _registry()
    src = dict(OVERLAY)
    r.replace_plugin_overlay(src)
    src["plg-later--z"] = {"role": "a", "clearance": "public", "auth": AUTH}
    assert r.plugin_overlay_names() == ["plg-x--y"]     # copied, not aliased


# --- publication authority --------------------------------------------------

def _tr_resolver(*, valid=True, plugins=()):
    def resolve(target):
        return SimpleNamespace(registry_valid=valid, plugins=list(plugins),
                               issues=[])
    return resolve


async def _tr_reconcile(registry, *, resolver, tmp_path, regen_health=False,
                        acks=None):
    from trigger_acks import TriggerAckStore
    return await tr.reconcile_plugin_triggers(
        trigger_registry=registry,
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])},
        acks=acks or TriggerAckStore(path=tmp_path / "acks.json"),
        secrets_dir=tmp_path / "secrets", prompt=False,
        resolver=resolver, global_secret_ok=lambda: True,
        regen_health=regen_health)


async def test_an_invalid_registry_publishes_the_sentinel_not_an_empty_map(
        tmp_path):
    """The seam finding this arm exists for. `compute_desired` returns NORMALLY
    on an invalid registry, with an empty overlay — so a success branch that
    published whatever it got would swap in `{}` and CLEAR the sentinel. No
    reconcile failed, yet no authoritative computation happened either."""
    r = _registry()
    r.replace_plugin_overlay(OVERLAY)
    await _tr_reconcile(r, resolver=_tr_resolver(valid=False),
                        tmp_path=tmp_path)
    assert r.plugin_overlay_unavailable() is True
    assert r.get_webhook_target("plg-x--y") is None


async def test_an_authoritative_pass_publishes_a_real_map_and_clears_it(
        tmp_path):
    """The converse, so "always publish the sentinel" cannot pass: a valid
    registry that resolves no plugins is a real result, and it must land as an
    authoritative empty map."""
    r = _registry()
    assert r.plugin_overlay_unavailable() is True
    await _tr_reconcile(r, resolver=_tr_resolver(valid=True), tmp_path=tmp_path)
    assert r.plugin_overlay_unavailable() is False
    assert r.plugin_overlay_names() == []


async def test_a_compute_failure_publishes_the_sentinel_and_re_raises(
        tmp_path):
    """Fail-closed is preserved exactly — the old behavior swapped `{}` here —
    and so is the propagation the caller depends on to log it."""
    r = _registry()
    r.replace_plugin_overlay(OVERLAY)

    def boom(target):
        raise RuntimeError("resolver exploded")

    with pytest.raises(RuntimeError, match="resolver exploded"):
        await _tr_reconcile(r, resolver=boom, tmp_path=tmp_path)
    assert r.plugin_overlay_unavailable() is True
    assert r.get_webhook_target("plg-x--y") is None


# --- the regeneration, on both exits, outside the lock ----------------------

@pytest.fixture
def regen_spy(monkeypatch):
    """Count regenerations and record whether _RECONCILE_LOCK was held when
    each ran. The lock ORDER is the thing: _regen_health_safe takes
    tools._plugin_tools_guard, and a `finally` placed INSIDE the `async with`
    would nest that guard under _RECONCILE_LOCK, which is the inversion the
    dossier warns about."""
    calls: list = []

    def _spy(module):
        async def _regen():
            calls.append(module._RECONCILE_LOCK.locked())
        monkeypatch.setattr(module, "_regen_health_safe", _regen)
    _spy(tr)
    _spy(cr)
    return calls


async def test_regen_runs_on_the_failure_path_outside_the_lock(
        tmp_path, regen_spy):
    """#606's original defect: the regeneration sat after the `async with` and
    the exception left over it, so a failed approve-time reconcile regenerated
    nothing at all."""
    r = _registry()

    def boom(target):
        raise RuntimeError("resolver exploded")

    with pytest.raises(RuntimeError):
        await _tr_reconcile(r, resolver=boom, tmp_path=tmp_path,
                            regen_health=True)
    assert regen_spy == [False]        # once, and NOT under the reconcile lock


async def test_regen_runs_on_the_success_path_outside_the_lock(
        tmp_path, regen_spy):
    r = _registry()
    await _tr_reconcile(r, resolver=_tr_resolver(), tmp_path=tmp_path,
                        regen_health=True)
    assert regen_spy == [False]


@pytest.mark.parametrize("failing", [True, False])
async def test_regen_health_false_regenerates_nothing(tmp_path, regen_spy,
                                                      failing):
    """No double regen: the mutation, boot and reload paths pass
    regen_health=False and regenerate themselves. The `finally` must not have
    turned that into an unconditional extra pass."""
    r = _registry()

    def boom(target):
        raise RuntimeError("resolver exploded")

    resolver = boom if failing else _tr_resolver()
    if failing:
        with pytest.raises(RuntimeError):
            await _tr_reconcile(r, resolver=resolver, tmp_path=tmp_path)
    else:
        await _tr_reconcile(r, resolver=resolver, tmp_path=tmp_path)
    assert regen_spy == []


# --- the two health rows, and why a one-shot failure yields ONE of them -----

@pytest.fixture
def live_runtime(monkeypatch):
    """Install the registry where the health consumer actually reads it.

    Handing a registry only to a reconciler does NOT exercise
    `current_issues()`, which reaches for `agent.active_runtime
    .trigger_registry` — a test that skips this reports green while measuring
    nothing.
    """
    import agent as agent_mod
    registry = _registry()
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        SimpleNamespace(trigger_registry=registry,
                        role_configs={"assistant":
                                      SimpleNamespace(channels=["webhook"])}),
        raising=False)
    return registry


def _codes(module):
    return [i.reason_code for i in module.current_issues()]


async def test_a_one_shot_failure_yields_exactly_one_routing_row(
        tmp_path, live_runtime, monkeypatch):
    """Both seam reviewers reproduced this independently, and it retracts an
    earlier "two rows" contract.

    The regeneration runs AFTER the failure is consumed, and `current_issues()`
    performs a FRESH, independent computation. If that second computation
    succeeds — which is what "one-shot" MEANS — `issue_state().ok` is True and
    there is no state row. A test that keeps the failure mock active through
    the regeneration passes while falsely claiming to test this case, so the
    resolver here fails on its FIRST pass only, and the second pass having
    actually run is asserted.
    """
    passes = {"n": 0}

    def flaky(target):
        passes["n"] += 1
        if passes["n"] == 1:
            raise RuntimeError("resolver exploded")
        return SimpleNamespace(registry_valid=True, plugins=[], issues=[])

    monkeypatch.setattr(tr, "_default_resolver", lambda: flaky)
    monkeypatch.setattr(tr, "SECRETS_DIR", tmp_path / "secrets")
    with pytest.raises(RuntimeError):
        await _tr_reconcile(live_runtime, resolver=flaky, tmp_path=tmp_path)

    assert live_runtime.plugin_overlay_unavailable() is True
    before = passes["n"]
    assert _codes(tr) == ["trigger_routing_unavailable"]
    assert passes["n"] > before          # the second computation really ran


async def test_a_persistent_failure_yields_two_independent_rows(
        tmp_path, live_runtime, monkeypatch):
    """Two rows because the two conditions have independent clearing
    predicates: one says the applied overlay is not authoritative, the other
    says a fresh computation could not run."""
    def boom(target):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(tr, "_default_resolver", lambda: boom)
    monkeypatch.setattr(tr, "SECRETS_DIR", tmp_path / "secrets")
    with pytest.raises(RuntimeError):
        await _tr_reconcile(live_runtime, resolver=boom, tmp_path=tmp_path)

    assert _codes(tr) == ["trigger_routing_unavailable",
                          "trigger_state_unavailable"]


async def test_a_successful_pass_emits_neither_row(tmp_path, live_runtime,
                                                   monkeypatch):
    resolver = _tr_resolver()
    monkeypatch.setattr(tr, "_default_resolver", lambda: resolver)
    monkeypatch.setattr(tr, "SECRETS_DIR", tmp_path / "secrets")
    await _tr_reconcile(live_runtime, resolver=resolver, tmp_path=tmp_path)
    assert _codes(tr) == []


def test_no_state_row_before_the_runtime_is_up(monkeypatch):
    """`issue_state()` legitimately reports ok=False with no runtime, or with a
    runtime that has no role configs. Emitting a row there would put an
    unavailable claim in the report on every boot, and a row that is always
    there is a row nobody reads."""
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", None, raising=False)
    assert _codes(tr) == []
    assert _codes(cr) == []
    monkeypatch.setattr(agent_mod, "active_runtime",
                        SimpleNamespace(trigger_registry=None,
                                        role_configs={}), raising=False)
    assert _codes(tr) == []
    assert _codes(cr) == []


def test_current_issues_never_raises_even_when_every_probe_explodes(
        monkeypatch):
    """A health pass must always regenerate. A probe that explodes is treated
    as unavailable — the fail-closed direction for a disclosure."""
    import agent as agent_mod

    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(
        agent_mod, "active_runtime",
        SimpleNamespace(
            trigger_registry=SimpleNamespace(
                plugin_overlay_unavailable=boom,
                callback_overlay_unavailable=boom),
            role_configs={"assistant": SimpleNamespace(channels=["webhook"])}),
        raising=False)
    assert "trigger_routing_unavailable" in _codes(tr)
    assert "callback_routing_unavailable" in _codes(cr)


# --- the callback mirror ----------------------------------------------------

async def _cr_reconcile(registry, *, resolver, tmp_path, regen_health=False):
    from callback_acks import CallbackAckStore
    return await cr.reconcile_plugin_callbacks(
        trigger_registry=registry,
        role_configs={"assistant": SimpleNamespace(channels=["telegram"])},
        acks=CallbackAckStore(path=tmp_path / "cb-acks.json"),
        spool=SimpleNamespace(), entries=lambda: [], prompt=False,
        resolver=resolver, regen_health=regen_health)


async def test_callback_invalid_registry_publishes_the_sentinel(tmp_path):
    r = _registry()
    r.replace_callback_overlay(CB_OVERLAY)
    await _cr_reconcile(r, resolver=_tr_resolver(valid=False),
                        tmp_path=tmp_path)
    assert r.callback_overlay_unavailable() is True
    assert r.get_callback("plg-x--auth") is None


async def test_callback_compute_failure_publishes_the_sentinel_and_re_raises(
        tmp_path):
    r = _registry()
    r.replace_callback_overlay(CB_OVERLAY)

    def boom(target):
        raise RuntimeError("resolver exploded")

    with pytest.raises(RuntimeError, match="resolver exploded"):
        await _cr_reconcile(r, resolver=boom, tmp_path=tmp_path)
    assert r.callback_overlay_unavailable() is True


async def test_callback_regen_runs_on_the_failure_path_outside_the_lock(
        tmp_path, regen_spy):
    r = _registry()

    def boom(target):
        raise RuntimeError("resolver exploded")

    with pytest.raises(RuntimeError):
        await _cr_reconcile(r, resolver=boom, tmp_path=tmp_path,
                            regen_health=True)
    assert regen_spy == [False]


# --- the one raw snapshot consumer -----------------------------------------

async def test_revoke_preserves_the_sentinel_instead_of_sweeping_it(
        monkeypatch, tmp_path):
    """`trigger_ack_revoke` is the only production reader of the raw snapshot.
    Sweeping under the sentinel is wrong twice over: `.items()` on it raises,
    and deriving `{}` from it publishes an authoritative "nothing should route"
    built from state this process never had. Under the sentinel plugin ingress
    is already closed, so the direct sweep is a no-op."""
    import agent as agent_mod
    import tools as tools_mod
    import trigger_acks

    registry = _registry()                      # starts at the sentinel
    swaps: list = []
    original = registry.replace_plugin_overlay

    def counting(overlay):
        swaps.append(overlay)
        original(overlay)
    monkeypatch.setattr(registry, "replace_plugin_overlay", counting)
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        SimpleNamespace(trigger_registry=registry, role_configs={}),
        raising=False)
    monkeypatch.setattr(trigger_acks.ACKS, "revoke_plugin", lambda n: [])
    monkeypatch.setattr(tools_mod, "_PLUGIN_HEALTH_PATH",
                        str(tmp_path / "health.json"))
    monkeypatch.setattr(tools_mod.CHALLENGES, "cancel_matching",
                        lambda **k: None)

    async def _noop(*a, **k):
        return []
    monkeypatch.setattr(tr, "reconcile_from_runtime", _noop)
    monkeypatch.setattr(cr, "reconcile_from_runtime", _noop)

    await tools_mod.trigger_ack_revoke.handler({"name": "x"})

    assert swaps == []                                   # zero direct sweeps
    assert registry.plugin_overlay_unavailable() is True  # still the singleton


# --- arm 4: the event twin, verification only -------------------------------

def test_the_event_twin_already_states_its_own_unavailable_routing(monkeypatch):
    """#606 arm 4 asked whether the shipped event twin needs this health row.
    It does not: it already emits one, and the regeneration already
    concatenates it. Verified, not reimplemented."""
    import event_reconcile
    import event_spool
    monkeypatch.setattr(event_reconcile, "_routed",
                        event_spool.ROUTING_UNAVAILABLE)
    codes = [i["reason_code"] for i in event_reconcile.current_issues()]
    assert "event_routing_unavailable" in codes


@pytest.mark.parametrize("module", [tr, cr])
def test_one_issue_state_call_feeds_both_the_row_and_the_issues(
        monkeypatch, module):
    """Review r1 S2. `current_issues()` computed `issue_state()` twice: once to
    decide the state row, once for the returned issues. Two independent
    computations of the same thing can disagree — a first that succeeds passes
    the guard, a second that fails degrades to [] — and the pass then reports a
    green, empty result produced by a computation that failed. Calling it once
    is the fix; this pins that it IS once.
    """
    import agent as agent_mod
    from trigger_reconcile import IssueState

    calls = {"n": 0}

    def flaky(resolver=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return IssueState(True, [], set())
        return IssueState(False, [], set())

    monkeypatch.setattr(module, "issue_state", flaky)
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        SimpleNamespace(trigger_registry=None,
                        role_configs={"assistant":
                                      SimpleNamespace(channels=["webhook"])}),
        raising=False)
    module.current_issues()
    assert calls["n"] == 1
