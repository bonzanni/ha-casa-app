"""#451 — ONE runner owns plugin setup, released by a positive consent verdict.

Two attempts to classify *which* of two runners executes a plugin's setup tool
failed adversarial review, because mutation time has no third answer: a runner
must be named then and there. v0.161.0 deletes the second runner and makes the
episode facility's "hold, stay visible, re-check" the answer to every unknown.

This file is the acceptance matrix from the issue — one case per row — and it
drives the REAL reconcilers into the REAL episode worker. Only the outermost
seams are doubled (agent dispatch, the operator note, the registry entry the
worker resolves at dispatch time). The standing failure mode in this area is a
fake that hides a self-defeating bug: a doubled verdict would make every case
below pass by construction, since the verdict IS the thing under test.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import callback_reconcile as cr
import plugin_setup_episodes as pse
import trigger_reconcile as tr
from callback_acks import CallbackAckStore
from plugin_callbacks import ack_identity as cb_ack_identity
from plugin_callbacks import declaration_digest
from trigger_acks import TriggerAckStore
from trigger_registry import TriggerRegistry

DECLARED = "authorize"
EFFECTIVE = "plg-gmail--authorize"
TRIGGER_AUTH = {"mode": "static_header", "header": "X-API-Key"}


# ---------------------------------------------------------------------------
# Harness — real reconcilers, real worker, doubled edges only
# ---------------------------------------------------------------------------

class _NoChannel:
    """No Telegram DM reachable. Used by the unreachable-operator row; every
    other row simply never needs a keyboard, because these tests drive consent
    through the ack stores rather than through taps (the tap path has its own
    coverage in tests/test_callback_consent.py)."""

    def get(self, name):
        return None


def _plugin(*, triggers=False, callbacks=False, setup="setup_gmail",
            artifact="art-1"):
    casa: dict = {}
    if triggers:
        casa["triggers"] = [{"name": "push", "type": "webhook",
                             "target": "resident:assistant",
                             "auth": dict(TRIGGER_AUTH)}]
    if callbacks:
        casa["callbacks"] = [{"name": DECLARED}]
    if setup:
        casa["setupTool"] = setup
    return SimpleNamespace(
        name="gmail", artifact_id=artifact, path=f"/store/gmail/{artifact}",
        version="1.0.0", manifest_name="gmail",
        manifest={"name": "gmail", "casa": casa})


@pytest.fixture
def env(monkeypatch, tmp_path):
    """One live wiring: the episode store on tmp, the worker's seams doubled."""
    monkeypatch.setattr(pse, "STORE_PATH", tmp_path / "episodes.json")
    monkeypatch.setattr(pse, "_lock", None)
    monkeypatch.setattr(pse, "_kick", None)

    state = SimpleNamespace(
        dispatched=[], notes=[], plugin=_plugin(setup="setup_gmail"),
        entry={"artifact_id": "art-1", "setup_tool": "setup_gmail",
               "granted_tools": ["gmailsrv"],
               "targets": ["resident:assistant"]},
        trig_acks=TriggerAckStore(path=tmp_path / "trigger_acks.json"),
        cb_acks=CallbackAckStore(path=tmp_path / "callback_acks.json"),
        registry=TriggerRegistry(scheduler=None, app=None, bus=None),
        secrets_dir=tmp_path / "webhook_secrets",
        channel_manager=_NoChannel(),
        spool=_Spool(),
    )

    async def _dispatch(role, instruction, ctx):
        state.dispatched.append((role, instruction, ctx))
        return True

    async def _notify(text):
        state.notes.append(text)

    pse.configure(
        dispatch=_dispatch, notify_operator=_notify,
        resolve_registry_entry=lambda p: state.entry,
        ack_lookup=lambda ident: None, routes_live=lambda p: True)
    # The health regen writes to /data — not this test's subject.
    monkeypatch.setattr(tr, "_regen_health_safe", _noop)
    monkeypatch.setattr(cr, "_regen_health_safe", _noop)
    # The union half of the sealing reads its own module's DEFAULT ack store
    # (the peer reconciler does not hold the other kind's store), so injecting
    # one kind without the other makes an already-acked consent look pending
    # forever. Point both defaults at these tmp stores.
    monkeypatch.setattr(tr, "_default_acks", lambda: state.trig_acks)
    monkeypatch.setattr(cr, "_default_acks", lambda: state.cb_acks)
    # A valid public base URL. Without this the callback compute raises
    # `callback_base_url_invalid` — a NON-CONSENT gap — for every plugin
    # declaring a callback, which now (correctly) blocks the verdict. Before the
    # peer-unknown fix the trigger pass sealed blind to it, so these rows passed
    # while a gap was present: they were not testing what they claimed.
    monkeypatch.setattr(cr, "_base_url", lambda: "https://casa.example.org")
    return state


async def _noop():
    return None


class _Spool:
    """Models the DURABLE marker inventory, not just the call surface: the
    callback reconcile reads its own markers back and compares bytes, so a stub
    returning None for a marker crashes the compute the moment the base URL is
    valid enough to reach that stage."""

    def __init__(self):
        self._ready: dict = {}
        self._index: dict = {}

    def ensure_plugin_dirs(self, plugin): pass

    def write_ready(self, plugin, payload):
        self._ready[plugin] = payload

    def delete_ready(self, plugin):
        self._ready.pop(plugin, None)
        return True

    def write_index_entry(self, path, payload):
        import callback_spool
        self._index[callback_spool.index_key(path)] = payload

    def delete_index_entry(self, path):
        import callback_spool
        self._index.pop(callback_spool.index_key(path), None)
        return True

    def delete_index_key(self, key):
        self._index.pop(key, None)
        return True

    def published_plugins(self):
        return sorted(self._ready)

    def index_keys(self):
        return sorted(self._index)

    @staticmethod
    def _marker(payload):
        import callback_spool
        if payload is None:
            return callback_spool.Marker(callback_spool.MarkerState.ABSENT)
        return callback_spool.Marker(
            callback_spool.MarkerState.PRESENT, payload,
            raw=callback_spool.canonical_marker_bytes(payload))

    def read_marker(self, plugin):
        return self._marker(self._ready.get(plugin))

    def read_index_marker(self, path):
        import callback_spool
        return self._marker(self._index.get(callback_spool.index_key(path)))



def _recording(state):
    async def _dispatch(role, instruction, ctx):
        state.dispatched.append((role, instruction, ctx))
        return True
    return _dispatch


def _swallow(state):
    async def _notify(text):
        state.notes.append(text)
    return _notify


async def _unreachable(*a, **kw):
    raise AssertionError("dispatch must not happen while setup is held")


async def _reconcile(state):
    """What every lifecycle site does: run BOTH reconcilers as a pair."""
    role_configs = {"assistant": SimpleNamespace(channels=["webhook"])}

    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[state.plugin],
                               issues=[])

    def _entries():
        return [{"name": "gmail", "artifact_id": state.plugin.artifact_id,
                 "targets": ["resident:assistant"]}]

    await tr.reconcile_plugin_triggers(
        trigger_registry=state.registry, role_configs=role_configs,
        channel_manager=state.channel_manager, acks=state.trig_acks,
        secrets_dir=state.secrets_dir, prompt=True,
        resolver=_resolver, global_secret_ok=lambda: True)
    await cr.reconcile_plugin_callbacks(
        trigger_registry=state.registry, role_configs=role_configs,
        channel_manager=state.channel_manager, acks=state.cb_acks,
        spool=state.spool, resolver=_resolver, entries=_entries,
        prompt=True)
    await pse._worker_pass()


def _assert_no_gap(state):
    """Guard against the harness masking a NON-CONSENT gap. These rows are about
    the consent position; if the harness leaves a plugin gapped (an invalid base
    URL, an unassigned target) the verdict is legitimately withheld and the row
    stops testing what it claims. This caught exactly that: an unstubbed
    `_base_url` left every callback plugin gapped, and the rows passed anyway
    because the trigger pass sealed blind to the callback half."""
    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[state.plugin],
                               issues=[])

    role_configs = {"assistant": SimpleNamespace(channels=["webhook"])}
    _, _, unknown = tr.trigger_pending_for_union(
        role_configs=role_configs, resolver=_resolver)
    assert unknown == set(), f"harness leaves a trigger gap: {unknown}"


def _obligation():
    rows = pse.episodes()
    assert len(rows) <= 1, rows
    return rows[0] if rows else None


def _pending_triggers(state):
    """The reconciler's OWN pending rows. Deriving the consent identity by hand
    is a trap: the row's `auth` is the NORMALIZED map (the compute fills in
    tolerance_secs and secret_owner), so a hand-built identity hashes
    differently and the ack silently never matches."""
    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[state.plugin],
                               issues=[])
    return tr.compute_desired(
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])},
        acks=state.trig_acks, resolver=_resolver,
        global_secret_ok=lambda: True).pending


def _trigger_identity(state):
    row = _pending_triggers(state)[0]
    return tr.ack_identity(
        plugin=row["plugin"], artifact_id=row["artifact_id"],
        effective=row["effective"], target=row["target"], auth=row["auth"])


def _approve_trigger(state):
    """Persist the trigger consent ack the reconciler looks for, and record the
    approval into the round the way the consent commit step does."""
    ident = None
    for row in _pending_triggers(state):
        ident = tr.ack_identity(
            plugin=row["plugin"], artifact_id=row["artifact_id"],
            effective=row["effective"], target=row["target"],
            auth=row["auth"])
        state.trig_acks.record(
            identity=ident, plugin=row["plugin"],
            artifact_id=row["artifact_id"], effective=row["effective"],
            target=row["target"], auth=row["auth"])
        gen = str((state.trig_acks.get(ident) or {}).get("gen", ""))
        pse.record_approval_sync(plugin=row["plugin"],
                                 artifact_id=row["artifact_id"],
                                 identity=ident, gen=gen)
    assert ident is not None, "nothing was pending to approve"
    return ident


def _approve_callback(state):
    art = state.plugin.artifact_id
    digest = declaration_digest({"declared": DECLARED, "effective": EFFECTIVE})
    ident = cb_ack_identity("gmail", EFFECTIVE, digest)
    rec = state.cb_acks.record("gmail", EFFECTIVE, digest)
    pse.record_approval_sync(plugin="gmail", artifact_id=art, identity=ident,
                             gen=str(rec.get("gen", "")))
    return ident


# ---------------------------------------------------------------------------
# The eleven acceptance rows. Each asserts WHICH runner acted — and since the
# hand-back is gone, "an agent ran it" is unrepresentable: the only possible
# runners are the episode worker and nobody.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fresh_install_with_triggers_waits_for_approval(env):
    env.plugin = _plugin(triggers=True)
    await _reconcile(env)
    assert env.dispatched == []                        # never before approval
    assert _obligation()["gate"] == "awaiting_verdict"
    _approve_trigger(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1


@pytest.mark.asyncio
async def test_fresh_install_with_callbacks_only_waits_for_approval(env):
    env.plugin = _plugin(callbacks=True)
    await _reconcile(env)
    assert env.dispatched == []
    assert _obligation()["gate"] == "awaiting_verdict"
    _approve_callback(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1


@pytest.mark.asyncio
async def test_no_consent_gate_dispatches_immediately(env):
    """Nothing to wait for — but it is Casa that says so, POSITIVELY, via a
    zero-member verdict. The obligation would hold if nobody had said it."""
    env.plugin = _plugin()                             # setupTool only
    await _reconcile(env)
    assert len(env.dispatched) == 1
    assert _obligation()["status"] == "dispatched"


@pytest.mark.asyncio
async def test_update_with_triggers_never_runs_before_the_new_secret(env):
    env.plugin = _plugin(triggers=True)
    _approve_trigger(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1                    # installed + approved
    # An update: NEW artifact. A trigger ack is artifact-bound, so the consent
    # re-prompts and the re-minted secret does not exist yet.
    env.plugin = _plugin(triggers=True, artifact="art-2")
    env.entry = dict(env.entry, artifact_id="art-2")
    await _reconcile(env)
    assert len(env.dispatched) == 1                    # NOT re-run yet
    assert _obligation()["artifact_id"] == "art-2"
    assert _obligation()["gate"] == "awaiting_verdict"
    _approve_trigger(env)
    await _reconcile(env)
    assert len(env.dispatched) == 2


@pytest.mark.asyncio
async def test_update_with_callbacks_unchanged_dispatches(env):
    """#443's case. The declaration-bound ack survives, so no round opens —
    and the OLD code read that absence as "consent will run it" or as "the
    integration is dead", depending on which branch it took."""
    env.plugin = _plugin(callbacks=True)
    _approve_callback(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1
    env.plugin = _plugin(callbacks=True, artifact="art-2")
    env.entry = dict(env.entry, artifact_id="art-2")
    await _reconcile(env)
    # The ack binds the declaration, which is byte-identical → still acked →
    # zero-member verdict → the new artifact's obligation releases at once.
    assert len(env.dispatched) == 2
    assert _obligation()["artifact_id"] == "art-2"


@pytest.mark.asyncio
async def test_update_changing_the_setup_tool_runs_the_new_one(env):
    """Attempt 2's MISSED RUN: nothing binds a setup tool's identity to a
    callback ack's identity, so a changed setupTool with an unchanged
    declaration used to keep its ack, open no round, and never run at all."""
    env.plugin = _plugin(callbacks=True)
    _approve_callback(env)
    await _reconcile(env)
    assert "setup_gmail" in env.dispatched[0][1]
    env.plugin = _plugin(callbacks=True, setup="setup_gmail_v2",
                         artifact="art-2")
    env.entry = dict(env.entry, artifact_id="art-2",
                     setup_tool="setup_gmail_v2")
    await _reconcile(env)
    assert len(env.dispatched) == 2
    assert "setup_gmail_v2" in env.dispatched[1][1]


@pytest.mark.asyncio
async def test_update_changing_callbacks_never_runs_before_approval(env):
    """Attempt 2's PREMATURE RUN: a changed declaration opens a round, but the
    declaration-derived answer handed back anyway and the engager ran setup
    before the operator approved the new endpoint."""
    env.plugin = _plugin(callbacks=True)
    _approve_callback(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1
    # A DIFFERENT declared callback ⇒ different digest ⇒ the ack no longer
    # covers it ⇒ pending again.
    env.plugin = _plugin(callbacks=True, artifact="art-2")
    env.plugin.manifest["casa"]["callbacks"] = [{"name": "authorize_v2"}]
    env.entry = dict(env.entry, artifact_id="art-2")
    await _reconcile(env)
    assert len(env.dispatched) == 1                    # held, not run
    assert _obligation()["gate"] == "awaiting_verdict"


@pytest.mark.asyncio
async def test_a_plugin_with_no_declared_setup_tool_has_no_runner(env):
    """Legacy handoff-only tools are unsupported pre-1.0: nobody runs it, and
    nothing pretends otherwise. Previously such a plugin got a MANDATORY
    hand-back and the engager ran it before approval minted the secret."""
    env.plugin = _plugin(triggers=True, setup=None)
    env.entry = dict(env.entry, setup_tool=None)
    _approve_trigger(env)
    await _reconcile(env)
    assert env.dispatched == []
    assert pse.episodes() == []                        # no obligation at all
    assert env.notes == []                             # and no spurious note


@pytest.mark.asyncio
async def test_denied_consent_refuses_and_a_reprompt_rearms(env):
    env.plugin = _plugin(triggers=True)
    await _reconcile(env)
    ident = _trigger_identity(env)
    await pse.on_consent_decision(plugin="gmail", artifact_id="art-1",
                                  identity=ident, approved=False, nonce="")
    assert env.dispatched == []
    assert _obligation()["status"] == "refused"
    note = " ".join(env.notes)
    assert "Run it manually" not in note        # the operator has no such tool
    assert "trigger(s)" not in note             # it may have been a callback
    # The way back: the next reconcile re-prompts (no ack), which re-arms.
    await _reconcile(env)
    assert _obligation()["status"] == "pending"
    assert _obligation()["gen"] == 1
    _approve_trigger(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1


@pytest.mark.asyncio
async def test_unreachable_operator_holds_rather_than_choosing(env):
    """The defect #451 names directly: sealing used to live AFTER the
    reachability gate, so with no DM nothing was sealed — and a round could
    first seal on a later ordinary reload, long after a mutation had reported
    which runner owned setup. Now the verdict exists and carries members."""
    env.plugin = _plugin(triggers=True)
    env.channel_manager = _NoChannel()                 # no DM the whole time
    await _reconcile(env)
    assert env.dispatched == []
    assert _obligation()["gate"] == "awaiting_verdict"
    rnd = pse._load()["rounds"]["gmail"]
    assert rnd["artifact_id"] == "art-1"
    assert len(rnd["members"]) == 1                    # sealed, and NOT empty
    assert all(m["state"] == "open" for m in rnd["members"].values())
    # `pending` never decays out of health, so this stays actionable.
    assert [i["kind"] for i in pse.health_issues()] == ["setup_episode_pending"]


@pytest.mark.asyncio
async def test_a_failed_pending_compute_holds_rather_than_choosing(env):
    """The accessor-raises row. Neither direction is safe to guess, so the
    verdict is simply not sealed — and an unsealed verdict holds."""
    env.plugin = _plugin()                             # would release at once
    real_cb = cr.callback_pending_for_union
    real_tr = tr.trigger_pending_for_union

    def _boom(**kw):
        raise RuntimeError("boom")

    cr.callback_pending_for_union = _boom
    tr.trigger_pending_for_union = _boom
    try:
        await _reconcile(env)
        assert env.dispatched == []
        assert _obligation()["gate"] == "awaiting_verdict"
        assert "gmail" not in pse._load()["rounds"]    # nothing sealed
    finally:
        # Restore by hand, NOT monkeypatch.undo() — that would also revert the
        # fixture's STORE_PATH patch and send the next write at real /data.
        cr.callback_pending_for_union = real_cb
        tr.trigger_pending_for_union = real_tr
    # Recovery is automatic once the compute works again.
    await _reconcile(env)
    assert len(env.dispatched) == 1


# ---------------------------------------------------------------------------
# The invariant the eleven rows rest on
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_absence_of_a_round_is_never_a_permission(env):
    """INV-PLUG-010, stated as its own red case: an obligation with NO sealed
    verdict must not dispatch, however long it waits. This is attempt 1's
    mechanism — it read "no round queued right now" as "nothing to wait for"
    and dispatched before the reconcile had opened the round."""
    assert pse.ensure_obligation(plugin="gmail", artifact_id="art-1") is True
    for _ in range(5):
        await pse._worker_pass()
    assert env.dispatched == []
    assert _obligation()["status"] == "pending"


@pytest.mark.asyncio
async def test_no_shipped_doctrine_routes_setup_to_an_agent():
    """The second runner is gone from the prompts too. A stale recipe branch
    would put the decision back where it cannot be made correctly."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "casa/rootfs/opt/casa/defaults"
    banned = ("run_plugin_setup_tool", "setup_via_consent", "consent_pending")
    offenders = sorted(
        f"{p.relative_to(root)}:{t}"
        for p in root.rglob("*.md")
        for t in banned
        if t in p.read_text(encoding="utf-8"))
    assert offenders == [], offenders


@pytest.mark.asyncio
async def test_the_dispatch_claims_no_approval_that_did_not_happen(env):
    """A zero-member verdict means nobody approved anything. Saying otherwise
    in the setup turn is the same invented fact as #443's "integration is
    dead" (INV-TOOL-005)."""
    env.plugin = _plugin()
    await _reconcile(env)
    text = env.dispatched[0][1]
    assert "operator approved" not in text
    assert "needed no new consent" in text
    # ...and the approved path still says so.
    env.plugin = _plugin(triggers=True, artifact="art-2")
    env.entry = dict(env.entry, artifact_id="art-2")
    await _reconcile(env)
    _approve_trigger(env)
    await _reconcile(env)
    assert "operator approved" in env.dispatched[1][1]


def test_asyncio_is_imported_for_the_module_contract():
    """Guard against the import drifting away — _reconcile awaits real work."""
    assert asyncio is not None



# ---------------------------------------------------------------------------
# Upgrade: a v3 store must not lose an already-approved setup run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unsupported_store_version_resets_fail_closed(env):
    """Pre-1.0 there is no migration machinery: a store carrying any other
    schema_version is treated as unreadable and RESET to empty — nothing in
    it dispatches, and the reconciler re-derives obligations from live
    registry state on its next pass."""
    import json
    pse.STORE_PATH.write_text(json.dumps({
        "schema_version": 3,
        "rounds": {},
        "consumed_keys": ["deadbeef"],
        "episodes": [{
            "id": "old1", "key": "deadbeef", "plugin": "gmail",
            "artifact_id": "art-1", "setup_tool": "setup_gmail",
            "approved_identities": ["i#g1"], "status": "pending",
            "attempts": 0, "created_ts": 1.0, "updated_ts": 1.0}],
    }), encoding="utf-8")
    await pse._worker_pass()
    assert env.dispatched == []          # nothing from the stale rows runs
    assert _obligation() is None         # the v3 row is gone, not upgraded
    data = pse._load()
    assert data["schema_version"] == 4
    assert "consumed_keys" not in data


# ---------------------------------------------------------------------------
# Review round 1 findings (Sol + Terra)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_boot_creates_the_obligation_it_owes(env):
    """Both reviewers, independently: the sweep used to be gated on `prompt`,
    and BOTH boot reconcilers pass prompt=False. So a crash between a durable
    registry publish and its lifecycle reconcile left the obligation
    uncreated — no pending row, nothing in health, setup never run — which is
    exactly the recovery the level-triggered design claims to provide."""
    env.plugin = _plugin()                             # setupTool, no consent
    role_configs = {"assistant": SimpleNamespace(channels=["webhook"])}

    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[env.plugin],
                               issues=[])

    def _entries():
        return [{"name": "gmail", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]}]

    # The boot pass, exactly as casa_core makes it: no channel, prompt=False.
    await tr.reconcile_plugin_triggers(
        trigger_registry=env.registry, role_configs=role_configs,
        channel_manager=None, acks=env.trig_acks,
        secrets_dir=env.secrets_dir, prompt=False,
        resolver=_resolver, global_secret_ok=lambda: True)
    await cr.reconcile_plugin_callbacks(
        trigger_registry=env.registry, role_configs=role_configs,
        channel_manager=None, acks=env.cb_acks, spool=env.spool,
        resolver=_resolver, entries=_entries, prompt=False)
    assert _obligation() is not None, "boot owes this plugin a setup run"
    await pse._worker_pass()
    assert len(env.dispatched) == 1


@pytest.mark.asyncio
async def test_a_late_denial_cannot_revoke_a_release(env):
    """Sol: the nonce fence protects a LIVE member, but a late deny/expiry
    whose round is already consumed synthesizes a fresh round in which the
    member is absent — so the fence is skipped by construction and the denial
    used to refuse an obligation a settled round had already released. Nothing
    then re-arms it: the ack exists, so no re-prompt ever comes."""
    # The window that matters is RELEASED BUT NOT YET DISPATCHED — Sol's
    # scenario holds it on an unresolved environment variable. A row that
    # already dispatched is out of `pending` and was never at risk.
    pse.configure(
        dispatch=lambda r, i, c: _unreachable(),
        notify_operator=_swallow(env), resolve_registry_entry=lambda p: env.entry,
        ack_lookup=lambda i: None, routes_live=lambda p: True,
        secrets_ready=lambda p: False)                  # held here
    env.plugin = _plugin(triggers=True)
    await _reconcile(env)
    ident = _trigger_identity(env)
    _approve_trigger(env)
    await _reconcile(env)
    assert env.dispatched == []                        # held, not dispatched
    row = _obligation()
    assert row["status"] == "pending" and row["gate"] == "released"
    assert pse._load()["rounds"] == {}                 # round consumed
    # The superseded keyboard's expiry finally lands, with no member to fence.
    await pse.on_consent_decision(plugin="gmail", artifact_id="art-1",
                                  identity=ident, approved=False, nonce="dead")
    row = _obligation()
    assert row["status"] != "refused", "a late denial revoked an earned release"
    assert row["gate"] == "released"
    # ...and once the environment resolves, setup still runs.
    pse.configure(
        dispatch=_recording(env), notify_operator=_swallow(env),
        resolve_registry_entry=lambda p: env.entry,
        ack_lookup=lambda i: None, routes_live=lambda p: True,
        secrets_ready=lambda p: True)
    await pse._worker_pass()
    assert len(env.dispatched) == 1


# ---------------------------------------------------------------------------
# Review round 2 findings (Sol + Terra converged on all three)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approval_after_a_denial_and_a_restart_still_runs_setup(env):
    """The denial note promises that approving the consent will run setup. This
    pins that end to end, across a promptless pass in between.

    Both round-2 reviewers predicted this was BROKEN, from opposite directions,
    because re-arming was driven by whether `open_round` minted a fresh nonce —
    a fact about prompting, not about consent. Their sequences do not actually
    reach it: a row only becomes terminal by settling or dispatching, both of
    which consume the round, so `open_round` always saw an absent member and
    always minted. Re-arming now reads the reconciler's pending set directly,
    which is the condition it always meant; the proxy happened to agree only
    because of an unstated "terminal row implies no live round" invariant."""
    env.plugin = _plugin(triggers=True)
    await _reconcile(env)
    ident = _trigger_identity(env)
    await pse.on_consent_decision(plugin="gmail", artifact_id="art-1",
                                  identity=ident, approved=False, nonce="")
    assert _obligation()["status"] == "refused"
    # A promptless pass — boot after a restart, or any prompt=False reload.
    role_configs = {"assistant": SimpleNamespace(channels=["webhook"])}

    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[env.plugin],
                               issues=[])

    await tr.reconcile_plugin_triggers(
        trigger_registry=env.registry, role_configs=role_configs,
        channel_manager=None, acks=env.trig_acks,
        secrets_dir=env.secrets_dir, prompt=False,
        resolver=_resolver, global_secret_ok=lambda: True)
    # ...then the operator is re-prompted and approves.
    await _reconcile(env)
    _approve_trigger(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1, "approving after a denial must run setup"


@pytest.mark.asyncio
async def test_a_malformed_row_does_not_strand_every_plugin(env):
    """Sol + Terra: the store guard only checks that `episodes` is a LIST, so
    one non-dict element parsed fine and then raised on the first `e.get(...)`
    in `_row_for` / `episodes()` / `health_issues()`. That stranded EVERY
    plugin's setup and broke health regeneration, and the "a corrupt store must
    not brick boot" recovery never applied because the store was not corrupt at
    the level it checks."""
    import json
    pse.STORE_PATH.write_text(json.dumps({
        "schema_version": 4, "rounds": {},
        "episodes": [None, "partial write", 7],
    }), encoding="utf-8")
    assert pse.episodes() == []
    # #747 / INV-PLUG-015: the dropped rows are no longer silent — the standing
    # report gets exactly ONE registry-global row saying the history could not
    # be read, and nothing else; health regeneration itself still works.
    assert [(r["kind"], r["plugin"]) for r in pse.health_issues()] == [
        ("setup_history_unavailable", "*")]
    env.plugin = _plugin()
    await _reconcile(env)
    assert len(env.dispatched) == 1
    assert _obligation() is not None


# ---------------------------------------------------------------------------
# Review round 3 findings
# ---------------------------------------------------------------------------

def test_one_snapshot_binds_the_pending_set_and_the_candidates():
    """Sol: sharing a resolver CALLABLE pins nothing — each helper re-resolves.
    `compute_desired` could see artifact A while a concurrent snapshot reload
    published B and `setup_candidates` reported B. Rounds are keyed by PLUGIN,
    so a `(plugin, B)` entry overwrites `(plugin, A)` and a zero-member B round
    releases setup while B's consent is unapproved."""
    calls = {"n": 0}
    snaps = [
        SimpleNamespace(registry_valid=True, plugins=[_plugin(triggers=True)],
                        issues=[]),
        SimpleNamespace(registry_valid=True,
                        plugins=[_plugin(triggers=True, artifact="art-2")],
                        issues=[]),
    ]

    def _drifting(target):
        calls["n"] += 1
        return snaps[min(calls["n"] - 1, len(snaps) - 1)]

    pinned = tr.pin_resolver(_drifting)
    first = pinned(None)
    second = pinned(None)
    assert first is second, "a pinned resolver must not re-resolve"
    assert calls["n"] == 1
    # A distinct target resolves once too, and is remembered.
    pinned("resident:assistant")
    pinned("resident:assistant")
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_an_obsolete_open_member_cannot_block_settlement(env):
    """Terra: retarget a trigger without changing the artifact. Role
    invalidation does not cancel a TriggerConsentKey, so the OLD target's
    member stayed open in the same-artifact round; approving the NEW target
    could never settle it, and the old keyboard's expiry then refused the
    obligation — with the new consent acked, nothing re-armed it."""
    env.plugin = _plugin(triggers=True)
    await _reconcile(env)
    stale = _trigger_identity(env)
    assert set(pse._load()["rounds"]["gmail"]["members"]) == {stale}
    # The trigger is retargeted: a DIFFERENT consent identity, same artifact.
    env.plugin.manifest["casa"]["triggers"][0]["name"] = "push2"
    await _reconcile(env)
    members = pse._load()["rounds"]["gmail"]["members"]
    assert stale not in members, "an obsolete open member must be pruned"
    assert len(members) == 1
    # ...and the new consent alone settles the round.
    _approve_trigger(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1


@pytest.mark.asyncio
async def test_a_decided_member_is_never_pruned(env):
    """The prune must take only STILL-OPEN members: a decided one is this
    round's settlement so far, and dropping it would lose a denial."""
    env.plugin = _plugin(triggers=True)
    await _reconcile(env)
    ident = _trigger_identity(env)
    await pse.on_consent_decision(plugin="gmail", artifact_id="art-1",
                                  identity=ident, approved=False, nonce="")
    # The denial settled and consumed the round; re-seal with a DIFFERENT
    # membership and confirm the denial's effect stands.
    assert _obligation()["status"] == "refused"
    pse.open_round(plugin="gmail", artifact_id="art-1",
                   identities=["other"])
    pse.open_round(plugin="gmail", artifact_id="art-1", identities=[])
    assert pse._load()["rounds"]["gmail"]["members"] == {}
    assert env.dispatched == []


def test_pin_resolver_never_substitutes_a_default():
    """Each reconciler has its OWN `_default_resolver`. If the pinning helper
    fell back to the trigger module's default, a callback reconcile that relies
    on its own seam would resolve through the wrong one — which is the same
    module-default asymmetry as the ack stores. The caller resolves first."""
    import inspect
    sig = inspect.signature(tr.pin_resolver)
    assert list(sig.parameters) == ["resolver"]
    sentinel = SimpleNamespace(registry_valid=True, plugins=[], issues=[])
    seen = []

    def _mine(target):
        seen.append(target)
        return sentinel

    assert tr.pin_resolver(_mine)(None) is sentinel
    assert seen == [None]


@pytest.mark.asyncio
async def test_a_pruned_members_live_keyboard_cannot_refuse_the_obligation(env):
    """The round-3 prune opened this: a member can be pruned while its keyboard
    is still LIVE (the consent stops being reported pending for a non-ack
    reason — its target lost the webhook channel, say). That keyboard's expiry
    then arrived for a non-member, where the nonce fence cannot apply because
    there is no member to compare against — so it was recorded, settled the
    round on a denial nobody made, and refused the obligation with a spurious
    "you declined" note. Racing the release: in production the zero-member
    verdict usually releases first, but the kick is asynchronous."""
    pse.ensure_obligation(plugin="gmail", artifact_id="art-1",
                          consent_pending=True)
    nonces = pse.open_round(plugin="gmail", artifact_id="art-1",
                            identities=["X"])
    pse.open_round(plugin="gmail", artifact_id="art-1", identities=[])
    assert pse._load()["rounds"]["gmail"]["members"] == {}
    await pse.on_consent_decision(plugin="gmail", artifact_id="art-1",
                                  identity="X", approved=False,
                                  nonce=nonces["X"])
    row = _obligation()
    assert row["status"] != "refused"
    assert env.notes == [], "no spurious declined note"


@pytest.mark.asyncio
async def test_a_decision_with_no_round_at_all_concludes_nothing(env):
    """A decision arriving with no round is never dropped, but the round it
    synthesizes is NOT authoritative: the reconciler never sealed it, so nothing
    established the plugin's consent position. Both directions hold rather than
    conclude — an approval must not release (that was the flag's escape hatch: a
    delayed finish hook could resurrect a consumed non-authoritative round as an
    authoritative release), and a denial must not refuse, which would attribute a
    decision to a position Casa never read."""
    pse.ensure_obligation(plugin="gmail", artifact_id="art-1",
                          consent_pending=True)
    assert "gmail" not in pse._load()["rounds"]
    await pse.on_consent_decision(plugin="gmail", artifact_id="art-1",
                                  identity="X", approved=False, nonce="")
    assert _obligation()["gate"] == "awaiting_verdict"
    assert _obligation()["status"] == "pending"
    assert env.notes == []
    assert env.dispatched == []


@pytest.mark.asyncio
async def test_a_delayed_finish_cannot_resurrect_a_consumed_round(env):
    """Sol (r7), reproduced: the boot-recovery sweep consumes a
    non-authoritative round without concluding; a DELAYED finish hook then calls
    on_consent_decision, finds no round, synthesizes one — and, defaulting to
    authoritative, released the obligation the sweep had just declined to."""
    pse.ensure_obligation(plugin="gmail", artifact_id="art-1",
                          consent_pending=True)
    pse.open_round(plugin="gmail", artifact_id="art-1", identities=["x"],
                   verdict=False)
    pse.record_approval_sync(plugin="gmail", artifact_id="art-1",
                             identity="x", gen="g1")
    await pse._worker_pass()                       # consumes, concludes nothing
    assert _obligation()["gate"] == "awaiting_verdict"
    assert "gmail" not in pse._load()["rounds"]
    await pse.on_consent_decision(plugin="gmail", artifact_id="art-1",
                                  identity="x", approved=True,
                                  approval_gen="g1")
    assert _obligation()["gate"] == "awaiting_verdict", "resurrected a verdict"
    assert env.dispatched == []


# ---------------------------------------------------------------------------
# Review round 4 findings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_rejected_dispatch_holds_and_lands_later(env):
    """Sol: `_setup_dispatch` returns False when no operator DM is reachable.
    Three attempts then marked the obligation `failed` — terminal. With the
    hand-back gone there is no second runner, and nothing re-arms a terminal row
    without a pending consent, so an ungated plugin's setup was lost for good
    even after Telegram was configured."""
    rejecting = SimpleNamespace(n=0)

    async def _reject(role, instruction, ctx):
        rejecting.n += 1
        return False

    pse.configure(
        dispatch=_reject, notify_operator=_swallow(env),
        resolve_registry_entry=lambda p: env.entry,
        ack_lookup=lambda i: None, routes_live=lambda p: True)
    env.plugin = _plugin()
    await _reconcile(env)
    row = _obligation()
    assert row["status"] == "pending" and row["gate"] == "released"
    assert rejecting.n == 3                            # burst still bounded
    assert [i["kind"] for i in pse.health_issues()] == ["setup_episode_pending"]
    # Telegram is configured later; the same obligation lands.
    pse.configure(
        dispatch=_recording(env), notify_operator=_swallow(env),
        resolve_registry_entry=lambda p: env.entry,
        ack_lookup=lambda i: None, routes_live=lambda p: True)
    await pse._worker_pass()
    assert len(env.dispatched) == 1


def test_a_mixed_generation_pass_seals_no_verdict():
    """Sol (r4): caching per target is not one snapshot. If a reload publishes a
    new generation between two resolutions, the pending membership and the
    candidate set describe different registries — and sealing over that pair can
    release setup for an artifact whose consent was never approved.

    The check lives in the AUTHORITY predicate, not in the candidate sweep
    (Terra, r7): suppressing the candidate list lost setup outright, because a
    re-consent landing during a mixed pass left a terminal obligation
    un-re-armed and no later pass saw a pending consent to re-arm from. A mixed
    pass therefore still records debts and fences keyboards; it just may not
    conclude."""
    gens = iter([1, 2])

    def _drifting(target):
        return SimpleNamespace(registry_valid=True, plugins=[], issues=[],
                               generation=next(gens))

    pinned = tr.pin_resolver(_drifting)
    pinned(None)
    pinned("resident:assistant")
    assert tr.one_generation(pinned) is False
    # An unpinned resolver makes no claim either way.
    assert tr.one_generation(lambda t: None) is True


@pytest.mark.asyncio
async def test_a_non_consent_gap_never_seals_needs_no_consent(env):
    """Terra: a declared trigger with a NON-consent gap (its target has no
    webhook channel) is omitted from `desired.pending` entirely. A zero-member
    round would then assert "this artifact needs no consent" — false, since the
    plugin declares one that is unapproved and merely unaskable right now. The
    route gate would stop today's dispatch, but the VERDICT would be a lie, and
    a positive verdict is the one thing this design rests on."""
    env.plugin = _plugin(triggers=True)
    role_configs = {"assistant": SimpleNamespace(channels=[])}  # no webhook

    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[env.plugin],
                               issues=[])

    await tr.reconcile_plugin_triggers(
        trigger_registry=env.registry, role_configs=role_configs,
        channel_manager=None, acks=env.trig_acks,
        secrets_dir=env.secrets_dir, prompt=True,
        resolver=_resolver, global_secret_ok=lambda: True)
    await pse._worker_pass()
    assert "gmail" not in pse._load()["rounds"], "no verdict may be sealed"
    assert _obligation()["gate"] == "awaiting_verdict"
    assert env.dispatched == []


# ---------------------------------------------------------------------------
# Review round 5 findings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_peer_kinds_gap_also_blocks_the_verdict(env):
    """Both round-5 reviewers: the round-4 gap guard was half-applied. Each
    reconciler passed only its OWN issues, and the peer helper returned pending
    rows without them — so the paired pass sealed an empty round blind to the
    other kind's non-consent gap and released the obligation as "needs no
    consent". The route gate normally masks the dispatch, but it is derived from
    a recomputation that degrades to [] on failure, so a release earned this way
    is a real hazard. The unknown set now travels WITH the pending rows."""
    # A trigger with a non-consent gap (no webhook channel) and NO callbacks:
    # the callback pass then has neither a pending member nor an issue of its
    # own, so it sealed a ZERO-member round — "needs no consent" — for a plugin
    # whose declared trigger is unapproved and merely unaskable.
    env.plugin = _plugin(triggers=True)
    role_configs = {"assistant": SimpleNamespace(channels=["telegram"])}

    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[env.plugin],
                               issues=[])

    def _entries():
        return [{"name": "gmail", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]}]

    # The CALLBACK pass alone — its own issues are clean, so before the fix it
    # sealed a verdict knowing nothing of the trigger's gap.
    await cr.reconcile_plugin_callbacks(
        trigger_registry=env.registry, role_configs=role_configs,
        channel_manager=None, acks=env.cb_acks, spool=env.spool,
        resolver=_resolver, entries=_entries, prompt=True)
    await pse._worker_pass()
    assert _obligation()["gate"] == "awaiting_verdict"
    assert env.dispatched == []


@pytest.mark.asyncio
async def test_the_acceptance_harness_has_no_hidden_gap(env):
    """The masking guard itself, on the plugin shapes the matrix uses."""
    for plug in (_plugin(), _plugin(triggers=True), _plugin(callbacks=True)):
        env.plugin = plug
        _assert_no_gap(env)


# ---------------------------------------------------------------------------
# Review round 6 findings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_gap_in_one_kind_blocks_release_by_the_other(env):
    """Both round-6 reviewers: the gap guard only suppressed sealing when the
    plugin had NO pending identity. A plugin with an unaskable TRIGGER plus a
    healthy pending CALLBACK therefore had a round sealed containing only the
    callback — and approving that callback released setup, with the trigger's
    consent position never established."""
    env.plugin = _plugin(triggers=True, callbacks=True)
    role_configs = {"assistant": SimpleNamespace(channels=["telegram"])}  # no webhook

    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[env.plugin],
                               issues=[])

    def _entries():
        return [{"name": "gmail", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]}]

    await tr.reconcile_plugin_triggers(
        trigger_registry=env.registry, role_configs=role_configs,
        channel_manager=None, acks=env.trig_acks,
        secrets_dir=env.secrets_dir, prompt=True, resolver=_resolver,
        global_secret_ok=lambda: True)
    await cr.reconcile_plugin_callbacks(
        trigger_registry=env.registry, role_configs=role_configs,
        channel_manager=None, acks=env.cb_acks, spool=env.spool,
        resolver=_resolver, entries=_entries, prompt=True)
    # The callback is approved; the trigger remains unaskable.
    _approve_callback(env)
    await pse._worker_pass()
    assert _obligation()["gate"] == "awaiting_verdict"
    assert env.dispatched == []


@pytest.mark.asyncio
async def test_a_cross_generation_pass_cannot_release_an_existing_obligation(env):
    """Terra: suppressing only `candidates` was not enough. The pending rows were
    still collected and the round still opened, so a pass explicitly identified
    as cross-generation could settle and release a PRE-EXISTING obligation —
    contradicting the guard's own "hold this pass" contract."""
    pse.ensure_obligation(plugin="gmail", artifact_id="art-1",
                          consent_pending=True)
    tr.seal_setup_state(
        trigger_pending=[{"plugin": "gmail", "artifact_id": "art-1",
                          "effective": "e", "target": "resident:assistant",
                          "auth": dict(TRIGGER_AUTH)}],
        callback_pending=[], pending_complete=True,
        candidates=None,                      # the sweep reported unavailable
        unknown=set())
    rnd = pse._load()["rounds"]["gmail"]
    assert rnd["verdict"] is False, "a held pass must not seal a verdict"
    assert rnd["members"], "...but the keyboards still get their nonces"
    # Approving every member must NOT release it.
    for ident in list(rnd["members"]):
        await pse.on_consent_decision(plugin="gmail", artifact_id="art-1",
                                      identity=ident, approved=True,
                                      approval_gen="g1")
    assert _obligation()["gate"] == "awaiting_verdict"
    await pse._worker_pass()
    assert env.dispatched == []


@pytest.mark.asyncio
async def test_the_latest_seal_governs_the_rounds_authority(env):
    """The flag answers "could the MOST RECENT pass establish this plugin's full
    consent position?", and settlement reads it when it concludes. Making it
    sticky (ANDed across seals) looked safer and stranded the obligation instead:
    a downgraded round keeps its members, so it does not settle-and-consume until
    every member is decided, and by then nothing could restore its authority."""
    pse.ensure_obligation(plugin="p", artifact_id="a1", consent_pending=True)
    pse.open_round(plugin="p", artifact_id="a1", identities=["x"],
                   verdict=True)
    assert pse._load()["rounds"]["p"]["verdict"] is True
    pse.open_round(plugin="p", artifact_id="a1", identities=["x"],
                   verdict=False)
    assert pse._load()["rounds"]["p"]["verdict"] is False
    pse.open_round(plugin="p", artifact_id="a1", identities=["x"],
                   verdict=True)
    assert pse._load()["rounds"]["p"]["verdict"] is True    # a cleared gap heals


@pytest.mark.asyncio
async def test_a_cleared_gap_eventually_releases(env):
    """The `verdict` flag ANDs across every seal of a round and nothing upgrades
    it — the safe direction, but it must not strand the obligation. Recovery has
    to come from the round being CONSUMED at settlement or replaced on a new
    artifact. The path only exists when a NON-AUTHORITATIVE round actually holds
    members: a gapped kind (unaskable trigger) alongside a pending one (callback).
    """
    env.plugin = _plugin(triggers=True, callbacks=True)
    gapped = {"v": True}

    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[env.plugin],
                               issues=[])

    def _entries():
        return [{"name": "gmail", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]}]

    def _channels():
        return {"assistant": SimpleNamespace(
            channels=["telegram"] if gapped["v"] else ["webhook"])}

    async def _pass():
        await tr.reconcile_plugin_triggers(
            trigger_registry=env.registry, role_configs=_channels(),
            channel_manager=None, acks=env.trig_acks,
            secrets_dir=env.secrets_dir, prompt=True, resolver=_resolver,
            global_secret_ok=lambda: True)
        await cr.reconcile_plugin_callbacks(
            trigger_registry=env.registry, role_configs=_channels(),
            channel_manager=None, acks=env.cb_acks, spool=env.spool,
            resolver=_resolver, entries=_entries, prompt=True)
        await pse._worker_pass()

    await _pass()
    rnd = pse._load()["rounds"]["gmail"]
    assert rnd["verdict"] is False and rnd["members"]   # the stranding risk
    _approve_callback(env)
    await _pass()
    assert env.dispatched == [], "the trigger is still unaskable"
    # The operator wires the webhook channel: the gap clears.
    gapped["v"] = False
    await _pass()
    _approve_trigger(env)
    await _pass()
    assert len(env.dispatched) == 1, "a cleared gap must not strand setup"


@pytest.mark.asyncio
async def test_a_reconsent_during_a_mixed_pass_is_not_lost(env):
    """Terra (r7): re-arming used to be gated on the SAME predicate as the
    verdict. A re-consent observed during a non-authoritative pass therefore left
    the terminal obligation un-re-armed — and once the ack persisted, no later
    pass saw a pending consent to re-arm from, so setup was lost permanently for
    a freshly re-minted credential. Recording a debt is now ungated; only the
    CONCLUSION is strict."""
    pse.ensure_obligation(plugin="gmail", artifact_id="art-1")
    pse._update_episode(_obligation()["id"], status="dispatched")
    pending = [{"plugin": "gmail", "artifact_id": "art-1", "effective": "e",
                "target": "resident:assistant", "auth": dict(TRIGGER_AUTH)}]
    # A pass that may not conclude — but must still record that setup is owed.
    tr.seal_setup_state(trigger_pending=pending, callback_pending=[],
                        pending_complete=True,
                        candidates=[{"plugin": "gmail",
                                     "artifact_id": "art-1"}],
                        unknown=set(), single_generation=False)
    row = _obligation()
    assert row["status"] == "pending", "the re-consent must re-arm the debt"
    assert row["gen"] == 1
    assert pse._load()["rounds"]["gmail"]["verdict"] is False
    # The approval settles the non-authoritative round without concluding...
    for ident in list(pse._load()["rounds"]["gmail"]["members"]):
        await pse.on_consent_decision(plugin="gmail", artifact_id="art-1",
                                      identity=ident, approved=True,
                                      approval_gen="g2")
    assert _obligation()["gate"] == "awaiting_verdict"
    # ...and the next clean pass, with the ack now persisted, releases it.
    tr.seal_setup_state(trigger_pending=[], callback_pending=[],
                        pending_complete=True,
                        candidates=[{"plugin": "gmail",
                                     "artifact_id": "art-1"}],
                        unknown=set(), single_generation=True)
    await pse._worker_pass()
    assert len(env.dispatched) == 1


# ---------------------------------------------------------------------------
# Review round 8 findings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_malformed_rounds_container_does_not_strand_setup(env):
    """Both round-8 reviewers: `_load` used `setdefault` on `rounds`, which only
    supplies a MISSING key — a parseable `"rounds": []` survived it, and then
    every `data["rounds"].get(...)` / `.keys()` raised inside a swallowing
    except. Nothing recovered: the shape persisted across loads, no round could
    be sealed, and EVERY plugin's obligation was owed forever with no way to
    become runnable. Round 2 hardened the rows and left the container."""
    import json
    pse.STORE_PATH.write_text(json.dumps({
        "schema_version": 4, "rounds": [], "episodes": [],
    }), encoding="utf-8")
    assert pse._load()["rounds"] == {}
    env.plugin = _plugin()
    await _reconcile(env)
    assert len(env.dispatched) == 1
    assert _obligation()["status"] == "dispatched"


@pytest.mark.asyncio
async def test_a_revoke_during_the_dispatch_window_is_not_lost(env):
    """Terra: an obligation stays `pending` while `_run_episode` awaits the bus.
    A revoke landing in that window hit `ensure_obligation`'s pending
    early-return and never re-armed, so the settled RE-approval found a
    `dispatched` row and declined — leaving the re-minted secret unprovisioned
    while the already-accepted turn had provisioned the old one."""
    pse.ensure_obligation(plugin="gmail", artifact_id="art-1")
    pse.open_round(plugin="gmail", artifact_id="art-1", identities=[])
    await pse._recover_and_settle()
    row = _obligation()
    assert row["status"] == "pending" and row["gate"] == "released"
    old_id = row["id"]
    # The revoke's reconcile: the consent is pending again for this artifact.
    assert pse.ensure_obligation(plugin="gmail", artifact_id="art-1",
                                 consent_pending=True) is True
    row = _obligation()
    assert row["gate"] == "awaiting_verdict", "a stale release must re-arm"
    assert row["gen"] == 1 and row["id"] != old_id
    # The in-flight dispatch's own bookkeeping must not touch the new generation.
    pse._update_episode(old_id, status="dispatched")
    assert _obligation()["status"] == "pending"
    # ...and the re-approval releases the NEW generation.
    pse.open_round(plugin="gmail", artifact_id="art-1", identities=[])
    await pse._recover_and_settle()
    assert _obligation()["gate"] == "released"


# ---------------------------------------------------------------------------
# Review round 9 findings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_round", [
    {"artifact_id": "art-1"},                            # no members, no verdict
    {"artifact_id": "art-1", "members": []},             # members not a mapping
    {"artifact_id": "art-1", "members": {"x": "nope"}},  # member not a mapping
    {"members": {}},                                     # no artifact_id
    ["not", "a", "round"],                               # not a mapping at all
])
async def test_an_unreadable_round_is_dropped_not_obeyed(env, bad_round):
    """Both round-9 reviewers, and the third consecutive round on store shape —
    one level deeper each time (rows, then the container, then a round's
    `members`). Two failure modes, both present here: an attribute access raising
    inside a swallowing except, so nothing repaired the shape and EVERY plugin's
    settlement sweep aborted; and a missing key defaulting permissively, so a
    round with no `verdict` read as AUTHORITATIVE and released an obligation the
    reconciler never sealed."""
    import json
    pse.STORE_PATH.write_text(json.dumps({
        "schema_version": 4, "rounds": {"gmail": bad_round}, "episodes": [],
    }), encoding="utf-8")
    assert pse._load()["rounds"].get("gmail") in (None, {
        "artifact_id": "art-1", "members": {}, "verdict": False})
    # A trigger plugin must still reach a correct verdict from here.
    env.plugin = _plugin(triggers=True)
    await _reconcile(env)
    assert env.dispatched == [], "released with no sealed verdict"
    assert _obligation()["gate"] == "awaiting_verdict"
    _approve_trigger(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1


def test_a_verdict_that_cannot_be_read_is_not_authoritative():
    """The flag's default must be CLOSED at every read: a partially-written or
    hand-edited round must not release setup."""
    import inspect
    src = inspect.getsource(pse)
    assert 'get("verdict", True)' not in src, "a fail-open verdict default"


# ---------------------------------------------------------------------------
# Review round 10 findings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["false", "no", 1, [], {}, 0, None, "true"])
async def test_only_the_exact_boolean_makes_a_round_authoritative(env, verdict):
    """Both round-10 reviewers: `bool(...)` made every TRUTHY value
    authoritative, so a persisted `"verdict": "false"` released setup. Only the
    exact boolean the sealer writes may count."""
    import json
    pse.STORE_PATH.write_text(json.dumps({
        "schema_version": 4,
        "rounds": {"gmail": {"artifact_id": "art-1", "members": {},
                             "verdict": verdict}},
        "episodes": [{"id": "e1", "plugin": "gmail", "artifact_id": "art-1",
                      "gen": 0, "status": "pending",
                      "gate": "awaiting_verdict", "attempts": 0,
                      "resolve_deferrals": 0, "approved_identities": [],
                      "created_ts": 1.0, "updated_ts": 1.0}],
    }), encoding="utf-8")
    assert pse._load()["rounds"]["gmail"]["verdict"] is False
    await pse._worker_pass()
    assert _obligation()["gate"] == "awaiting_verdict"
    assert env.dispatched == []


@pytest.mark.asyncio
async def test_an_unreadable_member_state_is_never_a_decision(env):
    """Both round-10 reviewers: settlement waited only on `state == "open"`, so a
    member with an unreadable state (`{}`) was neither open nor denied and got
    counted as APPROVED — releasing setup for a consent nobody had answered.
    Completeness is now positive: every member must carry a terminal decision."""
    import json
    pse.STORE_PATH.write_text(json.dumps({
        "schema_version": 4,
        "rounds": {"gmail": {"artifact_id": "art-1", "verdict": True,
                             "members": {"id-a": {}}}},
        "episodes": [{"id": "e1", "plugin": "gmail", "artifact_id": "art-1",
                      "gen": 0, "status": "pending",
                      "gate": "awaiting_verdict", "attempts": 0,
                      "resolve_deferrals": 0, "approved_identities": [],
                      "created_ts": 1.0, "updated_ts": 1.0}],
    }), encoding="utf-8")
    # Normalisation turns the unreadable state into OPEN — the self-healing,
    # settlement-blocking direction.
    assert (pse._load()["rounds"]["gmail"]["members"]["id-a"]["state"]
            == "open")
    await pse._worker_pass()
    assert _obligation()["gate"] == "awaiting_verdict"
    assert env.dispatched == []
    # A genuine approval of that member then releases it.
    await pse.on_consent_decision(plugin="gmail", artifact_id="art-1",
                                  identity="id-a", approved=True,
                                  approval_gen="g1")
    assert _obligation()["gate"] == "released"


@pytest.mark.asyncio
async def test_settlement_requires_a_positive_approval(env):
    """The rule stated directly: a round settles only when every member carries
    `approved` or `denied`, never by elimination."""
    pse.ensure_obligation(plugin="gmail", artifact_id="art-1",
                          consent_pending=True)
    pse.open_round(plugin="gmail", artifact_id="art-1",
                   identities=["a", "b"], verdict=True)
    data = pse._load()
    data["rounds"]["gmail"]["members"]["a"] = {"state": "approved", "gen": "g1"}
    data["rounds"]["gmail"]["members"]["b"] = {"state": "weird"}
    pse._save(data)
    await pse._recover_and_settle()
    assert _obligation()["gate"] == "awaiting_verdict"


# ---------------------------------------------------------------------------
# Review round 11 finding (Sol; Terra reported none)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("members", [
    {"consent-A": []},                       # member not a mapping
    {"consent-A": {"state": "open"}, "consent-B": "nope"},
    # A non-string identity is unreachable through the JSON store (object keys
    # are always strings), so it is guarded in code but not parametrised here.
])
async def test_dropping_a_member_must_not_manufacture_a_verdict(env, members):
    """Sol: normalisation itself created the unsafe value. Dropping a malformed
    MEMBER while preserving the round's authority turned a members-bearing round
    into an authoritative ZERO-member one — which is the positive assertion "this
    artifact needs no consent" — and released setup with nothing approved. An
    unreadable member now makes the whole round unreadable, and it is dropped."""
    import json
    pse.STORE_PATH.write_text(json.dumps({
        "schema_version": 4,
        "rounds": {"gmail": {"artifact_id": "art-1", "verdict": True,
                             "members": members}},
        "episodes": [{"id": "e1", "plugin": "gmail", "artifact_id": "art-1",
                      "gen": 0, "status": "pending",
                      "gate": "awaiting_verdict", "attempts": 0,
                      "resolve_deferrals": 0, "approved_identities": [],
                      "created_ts": 1.0, "updated_ts": 1.0}],
    }), encoding="utf-8")
    assert "gmail" not in pse._load()["rounds"]
    await pse._worker_pass()
    assert _obligation()["gate"] == "awaiting_verdict"
    assert env.dispatched == []
    # The reconciler then seals a fresh, faithful round from live state.
    env.plugin = _plugin(triggers=True)
    await _reconcile(env)
    assert env.dispatched == []
    _approve_trigger(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1


# ---------------------------------------------------------------------------
# Review round 12 finding (Terra; Sol reported none)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_transient_registry_outage_does_not_strand_a_released_run(env):
    """Terra: the dispatch path gives up after a bounded number of unresolved
    retries and marks the obligation `stale`, concluding the plugin is gone. A
    transient outage during the dispatch window therefore stranded an ALREADY
    RELEASED obligation permanently — by then every consent is acked, so no
    pending signal remained to re-arm from. The reconciler seeing the plugin
    again REFUTES the staleness, so that sighting re-arms it."""
    pse.configure(
        dispatch=_recording(env), notify_operator=_swallow(env),
        resolve_registry_entry=lambda p: None,        # registry unavailable
        ack_lookup=lambda i: None, routes_live=lambda p: True)
    env.plugin = _plugin()
    await _reconcile(env)
    assert _obligation()["gate"] == "released"
    for _ in range(pse._MAX_RESOLVE_DEFERRALS + 1):
        await pse._worker_pass()
    assert _obligation()["status"] == "stale"
    assert env.dispatched == []
    # The registry recovers. No consent is pending — every ack persisted.
    pse.configure(
        dispatch=_recording(env), notify_operator=_swallow(env),
        resolve_registry_entry=lambda p: env.entry,
        ack_lookup=lambda i: None, routes_live=lambda p: True)
    await _reconcile(env)
    assert len(env.dispatched) == 1, "a recovered registry must not strand setup"


# ---------------------------------------------------------------------------
# Review round 13 finding (Sol; Terra reported none)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_losing_every_target_holds_rather_than_failing(env):
    """Sol: `failed` is not always artifact-intrinsic. Having no runnable target
    is an ASSIGNMENT state that `plugin_assign` repairs — and with the hand-back
    gone there is no manual path out of a terminal row, nor anything to re-arm it
    once every consent is acked. So it holds, like every other environmental
    gate."""
    env.plugin = _plugin()
    env.entry = dict(env.entry, targets=[])        # every target unassigned
    await _reconcile(env)
    row = _obligation()
    assert row["status"] == "pending", "an assignment gap must not be terminal"
    assert "waiting" in row["last_error"]
    assert env.dispatched == [] and env.notes == []
    # Reassigning repairs it, with no re-arm needed.
    env.entry = dict(env.entry, targets=["resident:assistant"])
    await pse._worker_pass()
    assert len(env.dispatched) == 1


@pytest.mark.asyncio
async def test_an_artifact_intrinsic_failure_stays_terminal(env):
    """The other half: an ambiguous server binding is a declaration error that no
    registry mutation repairs, so it stays terminal and tells the operator rather
    than retrying the same failure on every kick."""
    env.plugin = _plugin()
    env.entry = dict(env.entry, granted_tools=["srv_a", "srv_b"])
    await _reconcile(env)
    assert _obligation()["status"] == "failed"
    assert any("could not run" in n for n in env.notes)
    assert env.dispatched == []
