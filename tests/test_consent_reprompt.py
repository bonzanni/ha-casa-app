"""#494 — on-demand consent re-issue (`consent_reprompt`).

The committing consent surface is the server-posted DM keyboard; when it
expires or is missed, agents used to relay the question through an ask —
which accepts the tap, acks ✔, and commits NOTHING. These tests pin the
recovery mechanism shipped for that incident:

* ``consent_denials`` — commit-ordered latest-decision registry (Approve
  clears, Deny records, expiry writes nothing);
* per-kind ``reprompt_pending`` — PROMPT-ONLY (no reconcile, no setup-round
  sealing/re-arming: the round-2 design wedge is pinned as a red case);
* ``plugin_setup_episodes.open_member_nonce`` — read-only nonce reuse;
* the refused-obligation re-arm at the synchronous approve commit;
* ``retire_for_removed`` — removal leaves no unsealable `pending` row;
* the ``consent_reprompt`` tool — delivery classified from
  ``ChallengeHandle.settled_post()``, never inferred from pending rows.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import callback_reconcile as cr
import consent_denials
import plugin_setup_episodes as pse
from callback_acks import CallbackAckStore
from plugin_callbacks import ack_identity, declaration_digest


# ---------------------------------------------------------------------------
# fixtures / doubles (mirrors tests/test_callback_reconcile.py)
# ---------------------------------------------------------------------------


def _manifest(names):
    return {"name": "x", "casa": {"callbacks": [{"name": n} for n in names]}}


def _plugin(name="gmail", artifact_id="art-1", callbacks=("authorize",)):
    return SimpleNamespace(
        name=name, artifact_id=artifact_id,
        path=f"/store/{name}/{artifact_id}", version="1.0.0",
        manifest_name=name, manifest=_manifest(callbacks))


def _resolver(plugins, *, valid=True, issues=()):
    def resolve(target):
        return SimpleNamespace(registry_valid=valid, plugins=list(plugins),
                               issues=list(issues))
    return resolve


def _entries(*plugins, targets=("resident:assistant",)):
    rows = [{"name": p.name, "artifact_id": p.artifact_id,
             "targets": list(targets)} for p in plugins]
    return lambda: rows


def _identity(plugin="gmail", declared="authorize"):
    effective = f"plg-{plugin}--{declared}"
    return ack_identity(plugin, effective,
                        declaration_digest({"declared": declared,
                                            "effective": effective}))


class _FakeTelegram:
    chat_id = "100"

    def __init__(self, *, deliver=True):
        self.posts = []
        self._deliver = deliver

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        if not self._deliver:
            return None
        self.posts.append((chat_id, request_id, text, tuple(options)))
        return 55

    async def edit_dm_message(self, chat_id, message_id, text):
        return True


class _FakeChannelManager:
    def __init__(self, telegram=None):
        self._telegram = telegram

    def get(self, name):
        return self._telegram if name == "telegram" else None


def _runtime(telegram):
    return SimpleNamespace(
        trigger_registry=SimpleNamespace(
            replace_callback_overlay=lambda o: None),
        role_configs={"assistant": SimpleNamespace(channels=["telegram"])},
        channel_manager=_FakeChannelManager(telegram))


@pytest.fixture(autouse=True)
def _clean_denials(monkeypatch):
    monkeypatch.setattr(consent_denials, "_denied", set())


@pytest.fixture(autouse=True)
def _fresh_challenge_state(monkeypatch):
    import authz_grants
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER",
                        verdict_broker.VerdictBroker())
    monkeypatch.setattr(authz_grants, "CHALLENGES",
                        authz_grants.ChallengeCoordinator())


@pytest.fixture
def episodes_store(tmp_path, monkeypatch):
    monkeypatch.setattr(pse, "STORE_PATH", tmp_path / "episodes.json")
    monkeypatch.setattr(pse, "_worker_task", None)
    monkeypatch.setattr(pse, "_lock", asyncio.Lock())
    monkeypatch.setattr(pse, "_kick", asyncio.Event())
    monkeypatch.setattr(pse, "_resolve_registry_entry",
                        lambda plugin: {"artifact_id": "art-1"})
    return tmp_path / "episodes.json"


# ---------------------------------------------------------------------------
# consent_denials — commit-ordered latest decision
# ---------------------------------------------------------------------------


def test_denials_latest_decision_wins():
    k = consent_denials.key("callback", "abc")
    assert not consent_denials.denied(k)
    consent_denials.record(k)
    assert consent_denials.denied(k)
    consent_denials.clear(k)
    assert not consent_denials.denied(k)
    consent_denials.clear(k)              # idempotent


def test_denial_keys_are_kind_scoped():
    consent_denials.record(consent_denials.key("trigger", "abc"))
    assert not consent_denials.denied(consent_denials.key("callback", "abc"))


class _CaptureCoordinator:
    """Captures register_challenge kwargs without posting anything."""

    def __init__(self):
        self.calls = []

    def register_challenge(self, key, **kwargs):
        self.calls.append((key, kwargs))
        return SimpleNamespace(created=True, refused=None)


def _commit_hook_for_callback(coord_calls):
    (_key, kwargs), = coord_calls
    return kwargs["on_commit_sync"]


def test_callback_commit_hooks_write_the_registry(tmp_path, episodes_store):
    import callback_consent
    coord = _CaptureCoordinator()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    effective = "plg-gmail--authorize"
    digest = declaration_digest({"declared": "authorize",
                                 "effective": effective})
    callback_consent.prompt_callback_consent(
        coordinator=coord, channel=_FakeTelegram(), chat_id=100,
        operator_id=100, plugin="gmail", artifact_id="art-1",
        declared="authorize", effective=effective,
        declaration_digest=digest, acks=acks)
    hook = _commit_hook_for_callback(coord.calls)
    identity = ack_identity("gmail", effective, digest)
    k = consent_denials.key("callback", identity)

    hook(1, {})                                    # Deny
    assert consent_denials.denied(k)
    hook(0, {})                                    # Approve — latest wins
    assert not consent_denials.denied(k)
    assert acks.get(identity) is not None


def test_trigger_and_event_commit_hooks_write_the_registry(tmp_path,
                                                           episodes_store):
    import event_consent
    import trigger_consent
    from event_acks import EventAckStore
    from trigger_acks import TriggerAckStore
    import plugin_events
    import plugin_triggers

    coord = _CaptureCoordinator()
    t_acks = TriggerAckStore(path=tmp_path / "t.json")
    auth = {"mode": "none"}
    trigger_consent.prompt_trigger_consent(
        coordinator=coord, channel=_FakeTelegram(), chat_id=100,
        operator_id=100, plugin="gmail", artifact_id="art-1",
        effective="plg-gmail--hook", target="resident:assistant",
        auth=auth, acks=t_acks)
    t_ident = plugin_triggers.ack_identity(
        plugin="gmail", artifact_id="art-1", effective="plg-gmail--hook",
        target="resident:assistant", auth=auth)
    hook = _commit_hook_for_callback([coord.calls[0]])
    hook(1, {})
    assert consent_denials.denied(consent_denials.key("trigger", t_ident))
    hook(0, {})
    assert not consent_denials.denied(consent_denials.key("trigger", t_ident))

    e_acks = EventAckStore(path=tmp_path / "e.json")
    event_consent.prompt_event_consent(
        coordinator=coord, channel=_FakeTelegram(), chat_id=100,
        operator_id=100, subscriber="fin", artifact_id="art-1",
        emitter="gmail", event="mail", digest="d1",
        targets=["resident:assistant"], acks=e_acks)
    e_ident = plugin_events.ack_identity(
        "fin", "art-1", "gmail", "mail", "d1", ["resident:assistant"])
    hook = _commit_hook_for_callback([coord.calls[1]])
    hook(1, {})
    assert consent_denials.denied(consent_denials.key("event", e_ident))
    hook(0, {})
    assert not consent_denials.denied(consent_denials.key("event", e_ident))


# ---------------------------------------------------------------------------
# plugin_setup_episodes — read-only nonce + refused re-arm + removal retire
# ---------------------------------------------------------------------------


def test_open_member_nonce_reads_only_open_members(episodes_store):
    nonces = pse.open_round(plugin="gmail", artifact_id="art-1",
                            identities=["i1"], verdict=True)
    assert pse.open_member_nonce("gmail", "i1") == nonces["i1"]
    assert pse.open_member_nonce("gmail", "i2") == ""
    assert pse.open_member_nonce("ghost", "i1") == ""
    before = json.loads(episodes_store.read_text())
    pse.open_member_nonce("gmail", "i1")
    assert json.loads(episodes_store.read_text()) == before   # read-only


def test_refused_obligation_rearms_on_sync_approval(episodes_store):
    assert pse.ensure_obligation(plugin="gmail", artifact_id="art-1")
    pse.open_round(plugin="gmail", artifact_id="art-1", identities=["i1"],
                   verdict=True)
    # Expiry path: the member denies with its own nonce → obligation refused.
    data = pse._load()
    data["rounds"]["gmail"]["members"]["i1"]["state"] = "denied"
    released, _ = pse._settle_locked(data, "gmail")
    pse._save(data)
    assert not released
    row = pse._row_for(pse._load(), "gmail")
    assert row["status"] == "refused"

    # The #494 recovery: an on-demand keyboard's Approve commits — the SAME
    # yield-free step re-arms the refused obligation.
    pse.record_approval_sync(plugin="gmail", artifact_id="art-1",
                             identity="i1", gen="1")
    row = pse._row_for(pse._load(), "gmail")
    assert row["status"] == "pending"
    assert row["gate"] == "awaiting_verdict"
    assert int(row["gen"]) == 1


def test_rearm_declines_when_plugin_does_not_resolve(episodes_store,
                                                     monkeypatch):
    assert pse.ensure_obligation(plugin="gmail", artifact_id="art-1")
    data = pse._load()
    pse._row_for(data, "gmail").update({"status": "refused"})
    pse._save(data)
    monkeypatch.setattr(pse, "_resolve_registry_entry", lambda plugin: None)
    pse.record_approval_sync(plugin="gmail", artifact_id="art-1",
                             identity="i1", gen="1")
    assert pse._row_for(pse._load(), "gmail")["status"] == "refused"


def test_rearm_is_narrow_refused_only(episodes_store):
    assert pse.ensure_obligation(plugin="gmail", artifact_id="art-1")
    data = pse._load()
    pse._row_for(data, "gmail").update({"status": "failed"})
    pse._save(data)
    pse.record_approval_sync(plugin="gmail", artifact_id="art-1",
                             identity="i1", gen="1")
    assert pse._row_for(pse._load(), "gmail")["status"] == "failed"


def test_retire_for_removed_stales_row_and_drops_round(episodes_store):
    assert pse.ensure_obligation(plugin="gmail", artifact_id="art-1")
    pse.open_round(plugin="gmail", artifact_id="art-1", identities=["i1"],
                   verdict=True)
    pse.retire_for_removed("gmail")
    data = pse._load()
    row = pse._row_for(data, "gmail")
    assert row["status"] == "stale"          # decays out of health
    assert row["last_error"] == "plugin removed"
    assert "gmail" not in data["rounds"]


# ---------------------------------------------------------------------------
# callback reprompt_pending — prompt-only, denial-aware, ack-rechecked
# ---------------------------------------------------------------------------


async def _drain():
    for _ in range(8):
        await asyncio.sleep(0)


async def test_reprompt_posts_committing_keyboard_with_open_nonce(
        tmp_path, episodes_store, monkeypatch):
    """The #494 recovery: an expired consent DM is re-postable on demand,
    threading the SEALED member's nonce (read-only) into the new keyboard."""
    import callback_consent
    captured = {}
    real_prompt = callback_consent.prompt_callback_consent

    def spy(**kwargs):
        captured.update(kwargs)
        return real_prompt(**kwargs)

    monkeypatch.setattr(callback_consent, "prompt_callback_consent", spy)
    nonces = pse.open_round(plugin="gmail", artifact_id="art-1",
                            identities=[_identity()], verdict=True)
    telegram = _FakeTelegram()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    p = _plugin()
    report = []
    store_before = episodes_store.read_bytes()
    await cr.reprompt_pending(_runtime(telegram), report=report, acks=acks,
                              resolver=_resolver([p]), entries=_entries(p))
    await _drain()
    assert len(telegram.posts) == 1
    assert captured["setup_nonce"] == nonces[_identity()]
    (row,) = report
    assert row["kind"] == "callback" and row["plugin"] == "gmail"
    assert await row["handle"].settled_post() == "posted"
    # PROMPT-ONLY: the setup-round store is byte-identical (the round-2
    # design wedge — resealing/re-arming from the on-demand path — pinned).
    assert episodes_store.read_bytes() == store_before


async def test_reprompt_skips_denied_identity(tmp_path, episodes_store):
    telegram = _FakeTelegram()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    consent_denials.record(consent_denials.key("callback", _identity()))
    p = _plugin()
    report = []
    await cr.reprompt_pending(_runtime(telegram), report=report, acks=acks,
                              resolver=_resolver([p]), entries=_entries(p))
    await _drain()
    assert telegram.posts == []
    assert report == [{"kind": "callback", "plugin": "gmail",
                       "name": "plg-gmail--authorize", "status": "denied"}]


async def test_reprompt_fires_again_after_approve_clears_denial(
        tmp_path, episodes_store):
    """Deny → (mutation-fired keyboard) Approve → the registry is cleared, so
    a later reprompt may fire again for a NEW pending consent."""
    k = consent_denials.key("callback", _identity())
    consent_denials.record(k)
    consent_denials.clear(k)               # the approve commit step clears
    telegram = _FakeTelegram()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    p = _plugin()
    report = []
    await cr.reprompt_pending(_runtime(telegram), report=report, acks=acks,
                              resolver=_resolver([p]), entries=_entries(p))
    await _drain()
    assert len(telegram.posts) == 1


async def test_reprompt_rereads_ack_store_before_registering(
        tmp_path, episodes_store, monkeypatch):
    """Design r3 (Sol): a concurrent Approve landing during the compute must
    not earn a fresh keyboard — the ack store is re-read synchronously."""
    telegram = _FakeTelegram()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    p = _plugin()
    real_compute = cr.compute_desired

    def racing_compute(**kwargs):
        desired = real_compute(**kwargs)
        # The operator taps Approve while the compute runs.
        effective = "plg-gmail--authorize"
        digest = declaration_digest({"declared": "authorize",
                                     "effective": effective})
        acks.record(plugin="gmail", effective=effective,
                    declaration_digest=digest)
        return desired

    monkeypatch.setattr(cr, "compute_desired", racing_compute)
    report = []
    await cr.reprompt_pending(_runtime(telegram), report=report, acks=acks,
                              resolver=_resolver([p]), entries=_entries(p))
    await _drain()
    assert telegram.posts == []
    assert report == [{"kind": "callback", "plugin": "gmail",
                       "name": "plg-gmail--authorize",
                       "status": "already_acked"}]


async def test_reprompt_no_channel_is_a_noop(tmp_path, episodes_store):
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    report = []
    await cr.reprompt_pending(
        SimpleNamespace(role_configs={}, channel_manager=None,
                        trigger_registry=object()),
        report=report, acks=acks, resolver=_resolver([_plugin()]),
        entries=_entries(_plugin()))
    assert report == []


# ---------------------------------------------------------------------------
# the consent_reprompt tool — delivery-truth classification
# ---------------------------------------------------------------------------


def _tool_env(monkeypatch, telegram, *, runtime="auto"):
    import agent as agent_mod
    import tools
    if runtime == "auto":
        runtime = _runtime(telegram)
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)
    monkeypatch.setattr(tools, "_regenerate_plugin_health", lambda x: None)
    return tools


def _handle(settled, created=True, refused=None):
    async def settled_post():
        return settled
    return SimpleNamespace(created=created, refused=refused,
                           settled_post=settled_post)


def _patch_kind(monkeypatch, mod_name, rows):
    import importlib
    mod = importlib.import_module(mod_name)

    async def fake(runtime, *, report, **kwargs):
        report.extend(rows)

    monkeypatch.setattr(mod, "reprompt_pending", fake)


async def _run_tool(tools):
    res = await tools.consent_reprompt.handler({})
    return json.loads(res["content"][0]["text"])


async def test_tool_reports_posted_from_settled_outcome(monkeypatch):
    tools = _tool_env(monkeypatch, _FakeTelegram())
    _patch_kind(monkeypatch, "trigger_reconcile", [])
    _patch_kind(monkeypatch, "callback_reconcile", [
        {"kind": "callback", "plugin": "gmail", "name": "x",
         "handle": _handle("posted")}])
    _patch_kind(monkeypatch, "event_reconcile", [])
    payload = await _run_tool(tools)
    assert payload["ok"] is True
    assert payload["reprompted"] == 1
    assert payload["rows"][0]["status"] == "posted"


async def test_tool_deduped_live_keyboard_is_not_claimed_reposted(monkeypatch):
    tools = _tool_env(monkeypatch, _FakeTelegram())
    _patch_kind(monkeypatch, "trigger_reconcile", [])
    _patch_kind(monkeypatch, "callback_reconcile", [
        {"kind": "callback", "plugin": "gmail", "name": "x",
         "handle": _handle("posted", created=False)}])
    _patch_kind(monkeypatch, "event_reconcile", [])
    payload = await _run_tool(tools)
    assert payload["ok"] is True
    assert payload["reprompted"] == 0
    assert payload["rows"][0]["status"] == "already_pending"


async def test_tool_delivery_failure_is_loud(monkeypatch):
    """Design r1 (Sol+Terra): pending rows are NOT delivery — when every
    needed keyboard failed to post, the tool fails typed, never ok:true."""
    tools = _tool_env(monkeypatch, _FakeTelegram())
    _patch_kind(monkeypatch, "trigger_reconcile", [])
    _patch_kind(monkeypatch, "callback_reconcile", [
        {"kind": "callback", "plugin": "gmail", "name": "x",
         "handle": _handle("delivery_failed")}])
    _patch_kind(monkeypatch, "event_reconcile", [])
    payload = await _run_tool(tools)
    assert payload["ok"] is False
    assert payload["kind"] == "delivery_failed"
    assert payload["rows"][0]["status"] == "delivery_failed"


async def test_tool_deduped_handle_delivery_failure_still_fails(monkeypatch):
    """Design r3 (Sol): created=False must not short-circuit classification —
    a deduped handle whose shared driver failed is a delivery failure."""
    tools = _tool_env(monkeypatch, _FakeTelegram())
    _patch_kind(monkeypatch, "trigger_reconcile", [])
    _patch_kind(monkeypatch, "callback_reconcile", [
        {"kind": "callback", "plugin": "gmail", "name": "x",
         "handle": _handle("delivery_failed", created=False)}])
    _patch_kind(monkeypatch, "event_reconcile", [])
    payload = await _run_tool(tools)
    assert payload["ok"] is False
    assert payload["kind"] == "delivery_failed"


async def test_tool_denied_rows_reported_not_reposted(monkeypatch):
    tools = _tool_env(monkeypatch, _FakeTelegram())
    _patch_kind(monkeypatch, "trigger_reconcile", [
        {"kind": "trigger", "plugin": "gmail", "name": "x",
         "status": "denied"}])
    _patch_kind(monkeypatch, "callback_reconcile", [])
    _patch_kind(monkeypatch, "event_reconcile", [])
    payload = await _run_tool(tools)
    assert payload["ok"] is True
    assert payload["reprompted"] == 0
    assert payload["rows"][0]["status"] == "denied"
    assert "DENIED" in payload["message"]


async def test_tool_nothing_pending(monkeypatch):
    tools = _tool_env(monkeypatch, _FakeTelegram())
    for m in ("trigger_reconcile", "callback_reconcile", "event_reconcile"):
        _patch_kind(monkeypatch, m, [])
    payload = await _run_tool(tools)
    assert payload["ok"] is True
    assert payload["reprompted"] == 0
    assert payload["rows"] == []


async def test_tool_runtime_unavailable(monkeypatch):
    tools = _tool_env(monkeypatch, None, runtime=None)
    payload = await _run_tool(tools)
    assert payload == {"ok": False, "kind": "runtime_unavailable",
                       "message": "runtime not ready"}


async def test_tool_dm_unreachable_posts_nothing(monkeypatch):
    called = []

    class _NoDM:
        chat_id = "-100"          # group chat — operator_identity → None

    tools = _tool_env(monkeypatch, _NoDM())
    import callback_reconcile

    async def marker(runtime, *, report, **kwargs):
        called.append(True)

    monkeypatch.setattr(callback_reconcile, "reprompt_pending", marker)
    payload = await _run_tool(tools)
    assert payload["ok"] is False
    assert payload["kind"] == "dm_unreachable"
    assert called == []


# ---------------------------------------------------------------------------
# diff-gate round 1 fixes (Sol/Terra)
# ---------------------------------------------------------------------------


def test_rearm_runs_before_ack_persist(tmp_path, episodes_store, monkeypatch):
    """Sol diff r1 S2: the re-arm and the ack live in different files, so
    their ORDER is the crash contract — re-arm first. A crash after the
    re-arm but before the ack leaves the consent pending (re-prompted later);
    the reverse order left an approved consent's obligation refused forever."""
    import callback_consent
    coord = _CaptureCoordinator()
    seq = []
    monkeypatch.setattr(pse, "rearm_refused_sync",
                        lambda **kw: (seq.append("rearm"), True)[1])

    class _SeqAcks:
        def record(self, **kw):
            seq.append("ack")
            return {"gen": 1}

        def get(self, identity):
            return {"gen": 1}

    effective = "plg-gmail--authorize"
    digest = declaration_digest({"declared": "authorize",
                                 "effective": effective})
    callback_consent.prompt_callback_consent(
        coordinator=coord, channel=_FakeTelegram(), chat_id=100,
        operator_id=100, plugin="gmail", artifact_id="art-1",
        declared="authorize", effective=effective,
        declaration_digest=digest, acks=_SeqAcks())
    hook = _commit_hook_for_callback(coord.calls)
    hook(0, {})
    assert seq[:2] == ["rearm", "ack"]


def test_trigger_rearm_runs_before_ack_persist(tmp_path, episodes_store,
                                               monkeypatch):
    import trigger_consent
    coord = _CaptureCoordinator()
    seq = []
    monkeypatch.setattr(pse, "rearm_refused_sync",
                        lambda **kw: (seq.append("rearm"), True)[1])

    class _SeqAcks:
        def record(self, **kw):
            seq.append("ack")

        def get(self, identity):
            return {"gen": 1}

    trigger_consent.prompt_trigger_consent(
        coordinator=coord, channel=_FakeTelegram(), chat_id=100,
        operator_id=100, plugin="gmail", artifact_id="art-1",
        effective="plg-gmail--hook", target="resident:assistant",
        auth={"mode": "none"}, acks=_SeqAcks())
    hook = _commit_hook_for_callback(coord.calls)
    hook(0, {})
    assert seq[:2] == ["rearm", "ack"]


def test_failed_required_rearm_aborts_the_commit(tmp_path, episodes_store,
                                                 monkeypatch):
    """Sol diff r2: a REQUIRED re-arm whose save fails must abort the commit
    BEFORE the ack write — recording the ack anyway recreates the no-exit
    refused-forever window. The raise lands in the coordinator's documented
    swallow; `acked` stays absent, so the finish hook shows internal-error."""
    import callback_consent
    # A refused row makes the re-arm REQUIRED; the failing save fails it.
    assert pse.ensure_obligation(plugin="gmail", artifact_id="art-1")
    data = pse._load()
    pse._row_for(data, "gmail").update({"status": "refused"})
    pse._save(data)
    monkeypatch.setattr(pse, "_save",
                        lambda d: (_ for _ in ()).throw(OSError("disk")))
    assert pse.rearm_refused_sync(plugin="gmail", artifact_id="art-1") is False

    coord = _CaptureCoordinator()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    effective = "plg-gmail--authorize"
    digest = declaration_digest({"declared": "authorize",
                                 "effective": effective})
    callback_consent.prompt_callback_consent(
        coordinator=coord, channel=_FakeTelegram(), chat_id=100,
        operator_id=100, plugin="gmail", artifact_id="art-1",
        declared="authorize", effective=effective,
        declaration_digest=digest, acks=acks)
    hook = _commit_hook_for_callback(coord.calls)
    meta = {}
    with pytest.raises(RuntimeError):
        hook(0, meta)
    assert "acked" not in meta
    assert acks.get(ack_identity("gmail", effective, digest)) is None


def test_rearm_refused_sync_rearms_durably(episodes_store):
    assert pse.ensure_obligation(plugin="gmail", artifact_id="art-1")
    data = pse._load()
    pse._row_for(data, "gmail").update({"status": "refused"})
    pse._save(data)
    pse.rearm_refused_sync(plugin="gmail", artifact_id="art-1")
    row = pse._row_for(pse._load(), "gmail")
    assert row["status"] == "pending" and row["gate"] == "awaiting_verdict"


async def test_plugin_remove_retires_setup_obligation_on_the_loop(
        episodes_store, monkeypatch):
    """Sol diff r1 S1: the retire must run SYNCHRONOUSLY on the event loop —
    a threaded retire can interleave a loop-confined feed's load/save and be
    overwritten by its stale `pending` snapshot."""
    import tools
    seen = {}

    def spying_retire(plugin):
        # get_running_loop() raises in a to_thread worker — this is the pin.
        asyncio.get_running_loop()
        seen["plugin"] = plugin

    monkeypatch.setattr(pse, "retire_for_removed", spying_retire)
    monkeypatch.setattr(
        tools, "_plugin_remove_sync",
        lambda name: {"ok": True, "name": name, "artifact_id": "art-1",
                      "targets": []})
    monkeypatch.setattr(tools, "_invalidate_lifecycle",
                        lambda **kw: None)

    async def fake_seq(name, targets, expect):
        return {}

    async def fake_remove_cbs(name):
        return None

    monkeypatch.setattr(tools, "_reload_and_verify_targets", fake_seq)
    monkeypatch.setattr(tools, "_remove_plugin_callbacks", fake_remove_cbs)
    res = await tools.plugin_remove.handler({"name": "gmail"})
    payload = json.loads(res["content"][0]["text"])
    assert payload["ok"] is True
    assert seen == {"plugin": "gmail"}


async def test_reprompt_compute_failure_is_a_visible_error_row(
        tmp_path, episodes_store, monkeypatch):
    """Sol/Terra diff r1: a compute failure must never read as "nothing
    pending" — it appends an error row the tool turns into a typed failure."""
    def boom(**kwargs):
        raise RuntimeError("synthetic compute failure")

    monkeypatch.setattr(cr, "compute_desired", boom)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    report = []
    await cr.reprompt_pending(_runtime(_FakeTelegram()), report=report,
                              acks=acks)
    assert report == [{"kind": "callback", "plugin": "", "name": "",
                       "status": "error"}]


async def test_tool_error_row_yields_typed_failure(monkeypatch):
    tools = _tool_env(monkeypatch, _FakeTelegram())
    _patch_kind(monkeypatch, "trigger_reconcile", [
        {"kind": "trigger", "plugin": "", "name": "", "status": "error"}])
    _patch_kind(monkeypatch, "callback_reconcile", [
        {"kind": "callback", "plugin": "gmail", "name": "x",
         "handle": _handle("posted")}])
    _patch_kind(monkeypatch, "event_reconcile", [])
    payload = await _run_tool(tools)
    assert payload["ok"] is False
    assert payload["kind"] == "reprompt_failed"
    # The kind that DID post still reports it in rows — nothing hidden.
    assert {"kind": "callback", "plugin": "gmail", "name": "x",
            "status": "posted"} in payload["rows"]


async def test_tool_kind_raise_yields_typed_failure(monkeypatch):
    tools = _tool_env(monkeypatch, _FakeTelegram())
    import importlib
    mod = importlib.import_module("trigger_reconcile")

    async def boom(runtime, *, report, **kwargs):
        raise RuntimeError("synthetic kind failure")

    monkeypatch.setattr(mod, "reprompt_pending", boom)
    _patch_kind(monkeypatch, "callback_reconcile", [])
    _patch_kind(monkeypatch, "event_reconcile", [])
    payload = await _run_tool(tools)
    assert payload["ok"] is False
    assert payload["kind"] == "reprompt_failed"


def test_tool_registered_and_granted():
    import tools
    names = {t.name for t in tools.CASA_TOOLS}
    assert "consent_reprompt" in names
