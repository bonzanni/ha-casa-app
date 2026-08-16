"""``event_reconcile`` — the plugin-event routing reconciler.

Structural sibling of ``test_callback_reconcile.py``: a pure
``compute_desired`` gate matrix, plus the locked reconcile's publish/
fail-closed/consent-prompt/revoke behavior.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

import event_reconcile as er
import event_spool
from event_acks import EventAckStore
from plugin_events import ack_identity


# ---------------------------------------------------------------------------
# fixtures / doubles
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_published(monkeypatch):
    """The published routed map is a module global — never leak between
    tests."""
    monkeypatch.setattr(er, "_routed", event_spool.ROUTING_UNAVAILABLE)
    yield


def _manifest(emits=(), subscribes=()):
    casa = {}
    if emits:
        casa["emits"] = [{"name": n} for n in emits]
    if subscribes:
        casa["subscribes"] = [{"plugin": e, "event": ev} for e, ev in subscribes]
    return {"name": "x", "casa": casa}


def _rp(name, *, artifact_id="art-1", manifest_name=None, emits=(),
       subscribes=(), manifest=None):
    return SimpleNamespace(
        name=name, artifact_id=artifact_id,
        path=f"/store/{name}/{artifact_id}", version="1.0.0",
        manifest_name=manifest_name if manifest_name is not None else name,
        manifest=_manifest(emits, subscribes) if manifest is None else manifest)


def _resolver(plugins, *, valid=True, issues=()):
    def resolve(target):
        return SimpleNamespace(registry_valid=valid, plugins=list(plugins),
                               issues=list(issues))
    return resolve


def _entries(*plugins, targets=("resident:assistant",)):
    rows = [{"name": p.name, "artifact_id": p.artifact_id,
             "targets": list(targets)} for p in plugins]

    def provider():
        return rows
    return provider


def _role_configs(**roles):
    return {role: SimpleNamespace(channels=list(channels))
            for role, channels in roles.items()}


def _identity(subscriber, artifact_id, emitter, event, targets=("resident:assistant",)):
    from plugin_events import subscribe_declaration_digest
    digest = subscribe_declaration_digest({"plugin": emitter, "event": event})
    return ack_identity(subscriber, artifact_id, emitter, event, digest,
                        sorted(targets))


def _ack(acks, subscriber, artifact_id, emitter, event,
        targets=("resident:assistant",), now=None):
    from plugin_events import subscribe_declaration_digest
    digest = subscribe_declaration_digest({"plugin": emitter, "event": event})
    return acks.record(subscriber, artifact_id, emitter, event, digest,
                       sorted(targets), now if now is not None else time.time())


class _FakeTelegram:
    chat_id = "100"

    def __init__(self):
        self.posts = []

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        return 55

    async def edit_dm_message(self, chat_id, message_id, text):
        return True


class _FakeChannelManager:
    def __init__(self, telegram=None):
        self._telegram = telegram

    def get(self, name):
        return self._telegram if name == "telegram" else None


@pytest.fixture
def fake_event_episodes(monkeypatch):
    """Patches the REAL ``event_episodes`` module's ``kick_all`` (call-
    counting) while leaving ``DISPATCH_LOCK`` as the genuine object every
    production caller shares (Important-4c, review round 1): stubbing out
    the whole module with a fresh ``SimpleNamespace`` — as this fixture
    used to — silently stopped exercising the ACTUAL shared lock the
    moment Task 8's real module existed, since ``event_reconcile``'s
    ``import event_episodes`` would resolve to the stub's own throwaway
    ``asyncio.Lock()`` instead."""
    import event_episodes
    state = SimpleNamespace(kicks=0, DISPATCH_LOCK=event_episodes.DISPATCH_LOCK)

    def _kick_all() -> None:
        state.kicks += 1

    monkeypatch.setattr(event_episodes, "kick_all", _kick_all)
    return state


# ---------------------------------------------------------------------------
# compute_desired — the gate matrix
# ---------------------------------------------------------------------------


async def test_valid_assigned_acked_subscription_routes(tmp_path):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    desired = er.compute_desired(
        role_configs=_role_configs(assistant=["telegram"]), acks=acks,
        resolver=_resolver([emitter, subscriber]),
        entries=_entries(emitter, subscriber))
    assert desired.issues == []
    assert desired.consent_needed == []
    key = ("gmail", "mail_in")
    assert key in desired.routed
    snap = desired.routed[key]["finance"]
    assert snap["subscriber"] == "finance"
    assert snap["artifact_id"] == "art-1"
    assert snap["targets"] == ["resident:assistant"]
    assert snap["ack_identity"] == _identity("finance", "art-1", "gmail", "mail_in")


async def test_invalid_subscribe_block_is_dark(tmp_path):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", manifest={
        "name": "x", "casa": {"subscribes": "not-a-list"}})
    acks = EventAckStore(path=tmp_path / "acks.json")
    desired = er.compute_desired(
        role_configs=_role_configs(assistant=["telegram"]), acks=acks,
        resolver=_resolver([emitter, subscriber]),
        entries=_entries(emitter, subscriber))
    assert desired.routed == {}
    assert any(i["reason_code"] == "event_invalid" and i["name"] == "finance"
              for i in desired.issues)


def test_emitter_missing_dark_then_heals(tmp_path):
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    role_configs = _role_configs(assistant=["telegram"])

    # emitter not installed at all
    desired = er.compute_desired(
        role_configs=role_configs, acks=acks,
        resolver=_resolver([subscriber]), entries=_entries(subscriber))
    assert desired.routed == {}
    assert any(i["reason_code"] == "event_emitter_missing" for i in desired.issues)

    # emitter installed but does not declare the event
    emitter_wrong = _rp("gmail", emits=["other_event"])
    desired2 = er.compute_desired(
        role_configs=role_configs, acks=acks,
        resolver=_resolver([emitter_wrong, subscriber]),
        entries=_entries(emitter_wrong, subscriber))
    assert desired2.routed == {}
    assert any(i["reason_code"] == "event_emitter_missing" for i in desired2.issues)

    # heals once the emitter is installed AND declares the event
    emitter = _rp("gmail", emits=["mail_in"])
    desired3 = er.compute_desired(
        role_configs=role_configs, acks=acks,
        resolver=_resolver([emitter, subscriber]),
        entries=_entries(emitter, subscriber))
    assert ("gmail", "mail_in") in desired3.routed
    assert not any(i["reason_code"] == "event_emitter_missing"
                  for i in desired3.issues)


def test_no_target_dark_then_heals(tmp_path):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in", targets=())
    role_configs = _role_configs(assistant=["telegram"])

    entries = _entries(emitter, targets=())

    def entries_no_finance_targets():
        return [{"name": "gmail", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]},
                {"name": "finance", "artifact_id": "art-1", "targets": []}]

    desired = er.compute_desired(
        role_configs=role_configs, acks=acks,
        resolver=_resolver([emitter, subscriber]),
        entries=entries_no_finance_targets)
    assert desired.routed == {}
    assert any(i["reason_code"] == "event_no_target" for i in desired.issues)

    def entries_with_finance_target():
        return [{"name": "gmail", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]},
                {"name": "finance", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]}]

    ack2 = EventAckStore(path=tmp_path / "acks2.json")
    _ack(ack2, "finance", "art-1", "gmail", "mail_in",
        targets=("resident:assistant",))
    desired2 = er.compute_desired(
        role_configs=role_configs, acks=ack2,
        resolver=_resolver([emitter, subscriber]),
        entries=entries_with_finance_target)
    assert ("gmail", "mail_in") in desired2.routed


def test_no_target_issue_once_per_subscriber_not_per_subscription(tmp_path):
    """Minor-7 (review round 1): reachability never varies across a
    subscriber's own subscribe entries — two subscriptions must still
    yield exactly ONE ``event_no_target`` issue, not two."""
    emitter_a = _rp("gmail", emits=["mail_in"])
    emitter_b = _rp("slack", emits=["msg_in"])
    subscriber = _rp("finance", subscribes=[
        ("gmail", "mail_in"), ("slack", "msg_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")

    def entries_no_finance_targets():
        return [{"name": "gmail", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]},
                {"name": "slack", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]},
                {"name": "finance", "artifact_id": "art-1", "targets": []}]

    desired = er.compute_desired(
        role_configs=_role_configs(assistant=["telegram"]), acks=acks,
        resolver=_resolver([emitter_a, emitter_b, subscriber]),
        entries=entries_no_finance_targets)
    no_target_issues = [i for i in desired.issues
                        if i["reason_code"] == "event_no_target"]
    assert len(no_target_issues) == 1


def test_pending_ack_dark_then_heals(tmp_path):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    role_configs = _role_configs(assistant=["telegram"])
    resolver = _resolver([emitter, subscriber])
    entries = _entries(emitter, subscriber)

    desired = er.compute_desired(role_configs=role_configs, acks=acks,
                                 resolver=resolver, entries=entries)
    assert desired.routed == {}
    assert any(i["reason_code"] == "event_pending_ack" for i in desired.issues)
    assert len(desired.consent_needed) == 1
    pending = desired.consent_needed[0]
    assert pending["subscriber"] == "finance"
    assert pending["emitter"] == "gmail"
    assert pending["event"] == "mail_in"

    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    desired2 = er.compute_desired(role_configs=role_configs, acks=acks,
                                  resolver=resolver, entries=entries)
    assert ("gmail", "mail_in") in desired2.routed
    assert desired2.consent_needed == []


def test_self_subscription_unscoped_refused(tmp_path):
    subscriber = _rp("finance", subscribes=[("finance", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    desired = er.compute_desired(
        role_configs=_role_configs(assistant=["telegram"]), acks=acks,
        resolver=_resolver([subscriber]), entries=_entries(subscriber))
    assert desired.routed == {}
    assert any(i["reason_code"] == "event_invalid" for i in desired.issues)


def test_self_subscription_scoped_spelling_refused(tmp_path):
    """Carried Task 2 finding: a bundled dependency naming ITSELF via its
    own SCOPED registry form must be refused even though
    ``plugin_events.parse_and_validate_subscribes``'s parse-time check only
    compares against the unscoped manifest name."""
    scoped = "slug.bank-feed"
    subscriber = SimpleNamespace(
        name=scoped, artifact_id="art-1", path=f"/store/{scoped}/art-1",
        version="1.0.0", manifest_name="bank-feed",
        manifest=_manifest(subscribes=[(scoped, "mail_in")]))
    acks = EventAckStore(path=tmp_path / "acks.json")
    desired = er.compute_desired(
        role_configs=_role_configs(assistant=["telegram"]), acks=acks,
        resolver=_resolver([subscriber]), entries=_entries(subscriber))
    assert desired.routed == {}
    assert any(i["reason_code"] == "event_invalid" for i in desired.issues)


def test_all_or_nothing_one_bad_subscription_darkens_whole_set(tmp_path):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[
        ("gmail", "mail_in"), ("nope", "ghost")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    desired = er.compute_desired(
        role_configs=_role_configs(assistant=["telegram"]), acks=acks,
        resolver=_resolver([emitter, subscriber]),
        entries=_entries(emitter, subscriber))
    assert desired.routed == {}
    assert any(i["reason_code"] == "event_emitter_missing" for i in desired.issues)


def test_artifact_update_voids_consent(tmp_path):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", artifact_id="art-1",
                     subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    role_configs = _role_configs(assistant=["telegram"])
    entries = _entries(emitter, subscriber)

    desired = er.compute_desired(role_configs=role_configs, acks=acks,
                                 resolver=_resolver([emitter, subscriber]),
                                 entries=entries)
    assert ("gmail", "mail_in") in desired.routed

    upgraded = _rp("finance", artifact_id="art-2",
                   subscribes=[("gmail", "mail_in")])
    desired2 = er.compute_desired(
        role_configs=role_configs, acks=acks,
        resolver=_resolver([emitter, upgraded]),
        entries=_entries(emitter, upgraded))
    assert desired2.routed == {}
    assert any(i["reason_code"] == "event_pending_ack" for i in desired2.issues)


def test_retarget_voids_consent(tmp_path):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in",
        targets=("resident:assistant",))
    role_configs = _role_configs(assistant=["telegram"], ops=["telegram"])

    entries_v1 = _entries(emitter, subscriber, targets=("resident:assistant",))
    desired = er.compute_desired(role_configs=role_configs, acks=acks,
                                 resolver=_resolver([emitter, subscriber]),
                                 entries=entries_v1)
    assert ("gmail", "mail_in") in desired.routed

    def entries_retargeted():
        return [{"name": "gmail", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]},
                {"name": "finance", "artifact_id": "art-1",
                 "targets": ["resident:ops"]}]

    desired2 = er.compute_desired(role_configs=role_configs, acks=acks,
                                  resolver=_resolver([emitter, subscriber]),
                                  entries=entries_retargeted)
    assert desired2.routed == {}
    assert any(i["reason_code"] == "event_pending_ack" for i in desired2.issues)


# ---------------------------------------------------------------------------
# reconcile_plugin_events — locking, publish, fail-closed, prompts
# ---------------------------------------------------------------------------


async def test_reconcile_publishes_routed_map(tmp_path, fake_event_episodes):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    issues = await er.reconcile_plugin_events(
        runtime, acks=acks, resolver=_resolver([emitter, subscriber]),
        entries=_entries(emitter, subscriber), prompt=False)
    assert issues == []
    routed = er.get_routed()
    assert routed is not event_spool.ROUTING_UNAVAILABLE
    assert ("gmail", "mail_in") in routed
    assert fake_event_episodes.kicks == 1


async def test_reconcile_compute_failure_publishes_sentinel_and_raises(
        fake_event_episodes):
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)

    def _boom(target):
        raise RuntimeError("resolver exploded")

    with pytest.raises(RuntimeError):
        await er.reconcile_plugin_events(runtime, resolver=_boom, prompt=False)
    assert er.get_routed() is event_spool.ROUTING_UNAVAILABLE
    assert fake_event_episodes.kicks == 1


async def test_reconcile_lock_serializes_stale_compute(tmp_path,
                                                        fake_event_episodes,
                                                        monkeypatch):
    """Two FULLY-ROUTABLE, mutually-exclusive maps (never a vacuous {} vs
    {} — Important-4a, review round 1): the slow (issued-first) resolver
    would route ``gmail-old``, the fast (issued-second) resolver routes
    ``gmail-new``. The slow compute holds the lock for its ENTIRE critical
    section (compute+swap), so the fast call cannot even START its own
    compute until the slow one has already published — the actual
    publication order is therefore slow-then-fast, and the final
    ``get_routed()`` content must be the FAST (later) map, never clobbered
    back to the stale (slow, earlier-issued) one."""
    # A PRIVATE lock for this test: asyncio.Lock's cross-task-contention
    # path binds to the CURRENTLY running loop on first genuine contention
    # (asyncio internals) — reusing the process-wide singleton across two
    # tests that BOTH create real contention on it would make the SECOND
    # one crash with "bound to a different event loop" once pytest-asyncio
    # hands it a fresh loop. Every OTHER test acquires the lock uncontended
    # (no cross-task race), which never touches that binding at all.
    monkeypatch.setattr(er, "_RECONCILE_LOCK", asyncio.Lock())
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)

    emitter_old = _rp("gmail-old", emits=["e"])
    sub_old = _rp("finance-old", subscribes=[("gmail-old", "e")])
    acks_old = EventAckStore(path=tmp_path / "acks-old.json")
    _ack(acks_old, "finance-old", "art-1", "gmail-old", "e")

    emitter_new = _rp("gmail-new", emits=["e"])
    sub_new = _rp("finance-new", subscribes=[("gmail-new", "e")])
    acks_new = EventAckStore(path=tmp_path / "acks-new.json")
    _ack(acks_new, "finance-new", "art-1", "gmail-new", "e")

    def slow_resolver(target):
        time.sleep(0.05)
        return SimpleNamespace(registry_valid=True,
                               plugins=[emitter_old, sub_old], issues=[])

    def fast_resolver(target):
        return SimpleNamespace(registry_valid=True,
                               plugins=[emitter_new, sub_new], issues=[])

    await asyncio.gather(
        er.reconcile_plugin_events(
            runtime, acks=acks_old, resolver=slow_resolver,
            entries=_entries(emitter_old, sub_old), prompt=False),
        er.reconcile_plugin_events(
            runtime, acks=acks_new, resolver=fast_resolver,
            entries=_entries(emitter_new, sub_new), prompt=False),
    )
    routed = er.get_routed()
    assert ("gmail-new", "e") in routed
    assert ("gmail-old", "e") not in routed
    assert fake_event_episodes.kicks == 2


async def test_reconcile_fires_deduped_consent_prompt(tmp_path, fake_event_episodes):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    telegram = _FakeTelegram()
    channel_manager = _FakeChannelManager(telegram)
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=channel_manager)

    import authz_grants
    import trigger_consent
    monkey_ok = hasattr(trigger_consent, "operator_identity")
    assert monkey_ok

    async def _run():
        return await er.reconcile_plugin_events(
            runtime, acks=acks, resolver=_resolver([emitter, subscriber]),
            entries=_entries(emitter, subscriber), prompt=True)

    import event_consent

    def _fixed_identity(channel):
        return (100, 200)

    orig = event_consent.operator_identity
    event_consent.operator_identity = _fixed_identity
    try:
        await _run()
        # a second reconcile pass with the SAME pending set must not double
        # the outstanding keyboard (in-flight dedup lives in the shared
        # ChallengeCoordinator; calling twice must not raise/duplicate).
        await _run()
        for _ in range(8):           # settle the coordinator's post driver(s)
            await asyncio.sleep(0)
    finally:
        event_consent.operator_identity = orig
    # EXACT count (Important-4b, review round 1): >=1 would pass even if
    # dedup silently failed and posted twice.
    assert len(telegram.posts) == 1


async def test_role_removed_between_prompt_and_approve_never_republishes(
        monkeypatch, tmp_path, fake_event_episodes):
    """SOL-P2a pin: the reconcile fired when the operator taps Approve
    must re-derive role_configs from the LIVE runtime at that moment, NOT
    reuse the snapshot captured when the prompt was originally posted.
    Simulates a role removed in between (a reload/reassignment): approve
    still records the ack (that commit step is synchronous and
    unconditional on the identity alone) but the reconcile it triggers
    must publish NO route to the now-gone role — never resurrect one off
    a stale role_configs closure."""
    import verdict_broker
    import authz_grants
    import agent as agent_mod
    import event_consent

    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    fresh_coord = authz_grants.ChallengeCoordinator()
    monkeypatch.setattr(authz_grants, "CHALLENGES", fresh_coord)

    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    telegram = _FakeTelegram()
    channel_manager = _FakeChannelManager(telegram)

    # LIVE at PROMPT time: "assistant" exists and is reachable.
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=channel_manager)
    monkeypatch.setattr(agent_mod, "active_runtime", runtime)

    orig_identity = event_consent.operator_identity
    event_consent.operator_identity = lambda channel: (100, 200)
    try:
        await er.reconcile_plugin_events(
            runtime, acks=acks, resolver=_resolver([emitter, subscriber]),
            entries=_entries(emitter, subscriber), prompt=True)
        for _ in range(8):        # settle the coordinator's post driver
            await asyncio.sleep(0)
    finally:
        event_consent.operator_identity = orig_identity
    assert len(telegram.posts) == 1

    # Between prompt and approve: the role is REMOVED from the LIVE
    # runtime (e.g. a reload/reassignment). The reconcile closure
    # captured role_configs={"assistant": ...} at prompt time — it must
    # NOT reuse that; it must observe THIS emptied live state instead.
    runtime.role_configs = {}

    key = next(iter(fresh_coord._entries))
    ch = fresh_coord._entries[key]
    claim = broker.claim(namespace="resident_ask", scope=ch.scope,
                         request_id=ch.rid, option_index=0, actor_id=200)
    assert not isinstance(claim, str), f"claim rejected: {claim}"
    assert broker.commit(claim) is True
    ch.req.meta["on_commit_sync"](0)

    # The ack IS recorded — approve's sync commit is unconditional on the
    # identity the operator actually saw and tapped.
    identity = _identity("finance", "art-1", "gmail", "mail_in")
    assert acks.get(identity) is not None

    for _ in range(8):            # settle the async finish/reconcile
        await asyncio.sleep(0)

    routed = er.get_routed()
    assert routed is not event_spool.ROUTING_UNAVAILABLE
    # NO route to the now-gone role — a stale role_configs reuse would
    # have republished this pair since the emitter/subscriber/ack are all
    # otherwise perfectly valid.
    assert "finance" not in (routed.get(("gmail", "mail_in")) or {})


# ---------------------------------------------------------------------------
# revoke — unroute before ack delete, both under the admission lock
# ---------------------------------------------------------------------------


async def test_revoke_unroutes_before_ack_delete(tmp_path, fake_event_episodes,
                                                  monkeypatch):
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    monkeypatch.setattr(er, "_routed", {
        ("gmail", "mail_in"): {"finance": {
            "subscriber": "finance", "artifact_id": "art-1",
            "targets": ["resident:assistant"], "ack_identity": "x"}}})

    observed = {}
    real_revoke = acks.revoke_subscriber

    def spy_revoke(subscriber):
        # At the moment the ack store is asked to revoke, the routed map
        # must ALREADY have unrouted this subscriber.
        routed = er.get_routed()
        observed["still_routed"] = subscriber in (
            routed.get(("gmail", "mail_in")) or {})
        return real_revoke(subscriber)

    acks.revoke_subscriber = spy_revoke
    removed = await er.revoke_and_unroute("finance", acks=acks)
    assert observed["still_routed"] is False
    assert len(removed) == 1
    routed = er.get_routed()
    assert "finance" not in (routed.get(("gmail", "mail_in")) or {})
    assert acks.get(_identity("finance", "art-1", "gmail", "mail_in")) is None


async def test_revoke_pair_only_unroutes_named_pair(fake_event_episodes,
                                                     monkeypatch, tmp_path):
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    _ack(acks, "finance", "art-1", "slack", "msg_in")
    monkeypatch.setattr(er, "_routed", {
        ("gmail", "mail_in"): {"finance": {
            "subscriber": "finance", "artifact_id": "art-1",
            "targets": ["resident:assistant"], "ack_identity": "x"}},
        ("slack", "msg_in"): {"finance": {
            "subscriber": "finance", "artifact_id": "art-1",
            "targets": ["resident:assistant"], "ack_identity": "y"}},
    })
    removed = await er.revoke_and_unroute(
        "finance", "gmail", "mail_in", acks=acks)
    assert len(removed) == 1
    routed = er.get_routed()
    assert "finance" not in routed[("gmail", "mail_in")]
    assert "finance" in routed[("slack", "msg_in")]


def test_revoke_with_sentinel_routed_is_a_noop(monkeypatch):
    # No published map yet — nothing to unroute, must not raise.
    er._unroute_locked("finance")
    assert er.get_routed() is event_spool.ROUTING_UNAVAILABLE


async def test_revoke_locks_are_the_real_shared_module_objects(
        fake_event_episodes):
    """Important-4c (review round 1): the revoke path must serialize on the
    ACTUAL ``event_episodes.DISPATCH_LOCK`` object every dispatch attempt
    shares — not a decoupled stand-in a test fixture minted. Proven two
    ways: identity, and a live blocking behavior (holding the real lock
    externally provably blocks a concurrent revoke)."""
    import event_episodes

    assert fake_event_episodes.DISPATCH_LOCK is event_episodes.DISPATCH_LOCK

    held = asyncio.Event()
    release = asyncio.Event()

    async def _hold_lock() -> None:
        async with event_episodes.DISPATCH_LOCK:
            held.set()
            await release.wait()

    holder = asyncio.create_task(_hold_lock())
    await held.wait()

    revoke_task = asyncio.create_task(er.revoke_and_unroute("finance"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not revoke_task.done()          # blocked on the SAME lock object

    release.set()
    await holder
    removed = await revoke_task
    assert removed == []


async def test_revoke_takes_reconcile_lock_before_dispatch_lock(
        fake_event_episodes, monkeypatch):
    """Important-3 (review round 1): revoke_and_unroute must serialize
    against an in-flight reconcile COMPUTE too (not just the dispatch
    lock) — proven by holding ``_RECONCILE_LOCK`` externally and observing
    the revoke call blocks until it releases."""
    # Private lock — see the comment in test_reconcile_lock_serializes_
    # stale_compute (asyncio.Lock's loop-binding on genuine contention).
    monkeypatch.setattr(er, "_RECONCILE_LOCK", asyncio.Lock())
    held = asyncio.Event()
    release = asyncio.Event()

    async def _hold_reconcile_lock() -> None:
        async with er._RECONCILE_LOCK:
            held.set()
            await release.wait()

    holder = asyncio.create_task(_hold_reconcile_lock())
    await held.wait()

    revoke_task = asyncio.create_task(er.revoke_and_unroute("finance"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not revoke_task.done()

    release.set()
    await holder
    await revoke_task


# ---------------------------------------------------------------------------
# Critical-1 — registry-invalid publishes the sentinel, never an authoritative {}
# ---------------------------------------------------------------------------


def test_compute_desired_marks_registry_invalid(monkeypatch):
    acks = EventAckStore(path="/does/not/matter")
    desired = er.compute_desired(
        role_configs=_role_configs(assistant=["telegram"]), acks=acks,
        resolver=_resolver([], valid=False), entries=lambda: [])
    assert desired.routed == {}
    assert desired.registry_valid is False


async def test_reconcile_registry_invalid_publishes_sentinel_not_empty_map(
        fake_event_episodes):
    """A registry-invalid compute is a FAILURE TO KNOW, not a computed
    empty — it must publish ROUTING_UNAVAILABLE (never an authoritative {},
    which would license the worker's destructive sweep), exactly like a
    raised compute exception, but WITHOUT raising (it is not a crash)."""
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    issues = await er.reconcile_plugin_events(
        runtime, resolver=_resolver([], valid=False), entries=lambda: [],
        prompt=False)
    assert issues == []
    assert er.get_routed() is event_spool.ROUTING_UNAVAILABLE
    assert fake_event_episodes.kicks == 1


async def test_reconcile_genuinely_empty_valid_compute_is_authoritative(
        fake_event_episodes):
    """The OTHER direction of Critical-1: a CLEAN compute that legitimately
    finds nothing to route (a valid, empty registry) publishes the real
    authoritative ``{}`` — never the sentinel — so the worker's destructive
    sweep (deleting unrouted pairs' emissions) is correctly PERMITTED."""
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    issues = await er.reconcile_plugin_events(
        runtime, resolver=_resolver([], valid=True), entries=lambda: [],
        prompt=False)
    assert issues == []
    routed = er.get_routed()
    assert routed == {}
    assert routed is not event_spool.ROUTING_UNAVAILABLE


# ---------------------------------------------------------------------------
# to_spool_shape / current_issues
# ---------------------------------------------------------------------------


def test_to_spool_shape_narrows_and_passes_sentinel():
    routed = {("gmail", "mail_in"): {"finance": {"subscriber": "finance"},
                                     "ops": {"subscriber": "ops"}}}
    shape = er.to_spool_shape(routed)
    assert shape == {("gmail", "mail_in"): {"finance", "ops"}}
    assert er.to_spool_shape(event_spool.ROUTING_UNAVAILABLE) \
        is event_spool.ROUTING_UNAVAILABLE


def test_current_issues_never_raises_without_runtime(monkeypatch):
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", None, raising=False)
    # The autouse fixture publishes the SENTINEL by default; decouple that
    # concern here (covered by its own dedicated pins below) to keep this
    # test's narrow intent — no runtime bound ⇒ no gate issues, no raise.
    monkeypatch.setattr(er, "_routed", {})
    assert er.current_issues() == []


def test_current_issues_surfaces_the_sentinel(monkeypatch):
    """Important-5c (review round 1): a stuck ROUTING_UNAVAILABLE map must
    be visible to the operator, not just silently inert."""
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", None, raising=False)
    monkeypatch.setattr(er, "_routed", event_spool.ROUTING_UNAVAILABLE)
    issues = er.current_issues()
    assert any(i["reason_code"] == "event_routing_unavailable" for i in issues)


def test_current_issues_no_sentinel_row_when_routed(monkeypatch):
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", None, raising=False)
    monkeypatch.setattr(er, "_routed", {})
    issues = er.current_issues()
    assert not any(i["reason_code"] == "event_routing_unavailable"
                  for i in issues)


# ---------------------------------------------------------------------------
# adjudication-f — opportunistic stale-ack pruning, clean passes only
# ---------------------------------------------------------------------------


async def test_prune_stale_ack_for_uninstalled_subscriber_on_clean_pass(
        tmp_path, fake_event_episodes):
    emitter = _rp("gmail", emits=["mail_in"])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    identity = _identity("finance", "art-1", "gmail", "mail_in")
    assert acks.get(identity) is not None

    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    # "finance" (the subscriber) is no longer resolved AT ALL — uninstalled
    # — and the pass is otherwise clean (no resolver issues).
    await er.reconcile_plugin_events(
        runtime, acks=acks, resolver=_resolver([emitter], issues=[]),
        entries=_entries(emitter), prompt=False)
    assert acks.get(identity) is None


async def test_prune_suppressed_on_a_pass_with_issues(tmp_path,
                                                       fake_event_episodes):
    emitter = _rp("gmail", emits=["mail_in"])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    identity = _identity("finance", "art-1", "gmail", "mail_in")

    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    # "finance" is likewise uninstalled, but the resolver reports an
    # unrelated issue (e.g. a resolution hiccup on some OTHER plugin) — the
    # whole pass must be untrusted for pruning, not just that plugin.
    await er.reconcile_plugin_events(
        runtime, acks=acks,
        resolver=_resolver([emitter], issues=["unrelated-issue"]),
        entries=_entries(emitter), prompt=False)
    assert acks.get(identity) is not None


async def test_prune_suppressed_when_a_subscribers_own_declaration_fails(
        tmp_path, fake_event_episodes):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", manifest={
        "name": "x", "casa": {"subscribes": "not-a-list"}})
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "billing", "art-1", "gmail", "mail_in")
    other_identity = _identity("billing", "art-1", "gmail", "mail_in")

    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    await er.reconcile_plugin_events(
        runtime, acks=acks,
        resolver=_resolver([emitter, subscriber], issues=[]),
        entries=_entries(emitter, subscriber), prompt=False)
    # "billing" isn't even resolved (would normally be pruned on a clean
    # pass), but "finance"'s OWN unparseable declaration must suppress
    # pruning for the WHOLE compute.
    assert acks.get(other_identity) is not None


def test_current_issues_includes_spool_passthrough(monkeypatch):
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", None, raising=False)
    monkeypatch.setattr(
        event_spool, "spool_issues",
        lambda: [{"reason": "event_spool_issue", "kind": "corrupt_state",
                  "emitter": "gmail", "file": "x"}])
    issues = er.current_issues()
    assert any(i["reason_code"] == "event_spool_issue" and i["name"] == "gmail"
              for i in issues)


# ---------------------------------------------------------------------------
# #582 — approve-time health regeneration (the trigger/callback mirror)
# ---------------------------------------------------------------------------


async def test_regen_health_flag_regenerates_report(monkeypatch, tmp_path,
                                                    fake_event_episodes):
    """#582: the reconcile the consent APPROVE fires must rewrite plugin-health,
    or the just-acked subscription's `event_pending_ack` row stands until an
    unrelated regeneration — which is what told the operator, eleven minutes
    and two worker passes after they approved, that the plugin "is waiting for
    your approval"."""
    import tools as tools_mod
    calls: list = []
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                        lambda extra: calls.append(extra))
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    await er.reconcile_plugin_events(
        runtime, acks=acks, resolver=_resolver([emitter, subscriber]),
        entries=_entries(emitter, subscriber), prompt=False, regen_health=True)
    assert calls == [[]]


async def test_default_reconcile_does_not_regen_health(monkeypatch, tmp_path,
                                                       fake_event_episodes):
    """The mutation/boot/reload paths regenerate health themselves — the default
    reconcile must NOT double-regen."""
    import tools as tools_mod
    calls: list = []
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                        lambda extra: calls.append(extra))
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    await er.reconcile_plugin_events(
        runtime, acks=acks, resolver=_resolver([emitter, subscriber]),
        entries=_entries(emitter, subscriber), prompt=False)
    assert calls == []


async def test_regen_health_failure_never_breaks_the_reconcile(
        monkeypatch, tmp_path, fake_event_episodes):
    import tools as tools_mod

    def _boom(extra):
        raise RuntimeError("health regen blew up")

    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", _boom)
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    issues = await er.reconcile_plugin_events(
        runtime, acks=acks, resolver=_resolver([emitter, subscriber]),
        entries=_entries(emitter, subscriber), prompt=False, regen_health=True)
    assert issues == []
    # the routing still published — a health-refresh failure is not a reconcile
    # failure
    assert "finance" in (er.get_routed().get(("gmail", "mail_in")) or {})


async def test_regen_health_runs_when_the_reconcile_itself_fails(
        monkeypatch, tmp_path, fake_event_episodes):
    """Sol design r1: the ack is durable BEFORE the reconcile runs, so a compute
    failure after an approve leaves the report saying "waiting for your
    approval" while the consent DM says "Approved, but starting delivery
    failed" — two operator surfaces disagreeing about a settled decision. The
    regeneration owes its answer on BOTH paths; the exception still propagates."""
    import tools as tools_mod
    calls: list = []
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                        lambda extra: calls.append(extra))

    def _boom(target):
        raise RuntimeError("resolver exploded")

    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    with pytest.raises(RuntimeError):
        await er.reconcile_plugin_events(
            runtime, resolver=_boom, prompt=False, regen_health=True)
    assert calls == [[]]
    # and the report it regenerated describes the fail-closed state
    assert er.get_routed() is event_spool.ROUTING_UNAVAILABLE


async def test_regen_health_holds_the_plugin_tools_guard(
        monkeypatch, tmp_path, fake_event_episodes):
    """Sol/Terra design r1 (R1-C): `_REPORT_LOCK` serializes the WRITE, not the
    computation before it, so an approve-time regeneration that starts before a
    plugin mutation commits can write its older result last and delete the row
    the mutation just added. Sol reproduced that with the real writer. The
    regeneration therefore runs under the same guard every mutation holds."""
    import tools as tools_mod
    held: list = []
    monkeypatch.setattr(
        tools_mod, "_regenerate_plugin_health",
        lambda extra: held.append(tools_mod._PLUGIN_TOOLS_LOCK.locked()))
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    await er.reconcile_plugin_events(
        runtime, acks=acks, resolver=_resolver([emitter, subscriber]),
        entries=_entries(emitter, subscriber), prompt=False, regen_health=True)
    assert held == [True]


async def test_consent_approve_regenerates_health(monkeypatch, tmp_path,
                                                  fake_event_episodes):
    """Integration, and the exact prod symptom: an operator Approve on the
    event-consent keyboard clears the stale `event_pending_ack` at once."""
    import authz_grants
    import agent as agent_mod
    import event_consent
    import tools as tools_mod
    import verdict_broker

    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    coord = authz_grants.ChallengeCoordinator()
    monkeypatch.setattr(authz_grants, "CHALLENGES", coord)
    regen: list = []
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                        lambda extra: regen.append(extra))

    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    telegram = _FakeTelegram()
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=_FakeChannelManager(telegram))
    monkeypatch.setattr(agent_mod, "active_runtime", runtime)

    orig_identity = event_consent.operator_identity
    event_consent.operator_identity = lambda channel: (100, 200)
    try:
        await er.reconcile_plugin_events(
            runtime, acks=acks, resolver=_resolver([emitter, subscriber]),
            entries=_entries(emitter, subscriber), prompt=True)
        for _ in range(8):
            await asyncio.sleep(0)
    finally:
        event_consent.operator_identity = orig_identity
    assert len(telegram.posts) == 1
    assert regen == []          # nothing regenerated yet — still pending

    key = next(iter(coord._entries))
    ch = coord._entries[key]
    claim = broker.claim(namespace="resident_ask", scope=ch.scope,
                         request_id=ch.rid, option_index=0, actor_id=200)
    assert not isinstance(claim, str), f"claim rejected: {claim}"
    assert broker.commit(claim) is True
    ch.req.meta["on_commit_sync"](0)
    # the finish hook runs as a broker-driven task whose reconcile uses
    # asyncio.to_thread — poll with real sleeps until it settles.
    for _ in range(100):
        if regen:
            break
        await asyncio.sleep(0.02)

    assert acks.get(_identity("finance", "art-1", "gmail", "mail_in")) is not None
    assert regen == [[]]        # health regenerated exactly once on approve
