"""#1677: what a specialist bundle's owned-set swap does to callback consent.

A plugin that LEAVES a specialist's owned set loses its callback consents and
its callback spool; a plugin that ENTERS it starts with neither, whatever an
earlier life of that name left behind; a plugin on both sides of the swap keeps
both. Before this rule a bundle relied on the callback reconcile's prune, which
any standing resolver issue anywhere in the registry skips, so an uninstalled
specialist's consent stayed on file and a reinstall of the same declaration was
routed with no consent card.

Built like the #1671 bundle world in ``test_event_wiring.py``. REAL: the four
bundle handlers, the bundle sequencer and its compensation, a
``CallbackAckStore`` and a ``CallbackSpool`` on disk, the callback reconcile
writing a ``TriggerRegistry`` callback overlay, and the consent prompt (through
a fake Telegram transport, so a posted card is observable). Stubbed: the
library transactions (reduced to the owned-set swap they commit and the names
they record for it; ``test_specialist_bundle_commit.py`` pins that recording
against the real ones), the reload (reduced to the callback reconcile it runs),
and the verify/health/own-chat side effects.

The world always carries a resolver issue on an unrelated plugin: the
condition under which the reconcile's prune does not run.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from types import SimpleNamespace

import pytest

_SLUG = "mtg"
_DECLARED = "authorize"


def _name(short: str) -> str:
    """The scoped registry name of the owned plugin *short*."""
    return f"{_SLUG}.{short}"


def _effective(short: str) -> str:
    return f"plg-{_name(short)}--{_DECLARED}"


def _world(monkeypatch, tmp_path, *, owned=("auth",), acked=("auth",)):
    """*owned*: the plugins the specialist owns at the start, each at ``v1``
    and each declaring one callback. *acked*: the plugin names whose callback
    the operator approved, installed now or not."""
    import agent as agent_mod
    import approval_records
    import authz_grants
    import callback_acks
    import callback_reconcile
    import callback_spool
    import event_acks
    import plugin_callbacks
    import plugin_registry
    import plugin_setup_episodes
    import reload as reload_mod
    import specialist_bundle_journal
    import specialist_install
    import tools as tools_mod
    import trigger_acks
    import verdict_broker
    from test_callback_reconcile import _FakeChannelManager, _FakeTelegram
    from test_event_wiring import _SLUG as their_slug, _staged_bundle
    from trigger_registry import TriggerRegistry

    assert their_slug == _SLUG
    # Staged before the registry is patched below.
    args = _staged_bundle(monkeypatch, tmp_path)

    monkeypatch.setattr(approval_records, "PATH",
                        str(tmp_path / "approval-records.json"))
    monkeypatch.setattr(approval_records, "_RECORDS", None)
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    monkeypatch.setattr(authz_grants, "CHALLENGES",
                        authz_grants.ChallengeCoordinator())
    monkeypatch.setattr(callback_reconcile, "_base_url",
                        lambda: "https://casa.example.org")
    monkeypatch.setattr(plugin_setup_episodes, "STORE_PATH",
                        tmp_path / "plugin-setup-episodes.json")

    made: dict = {}

    def plugin(short, revision="v1"):
        """The owned plugin *short* at *revision* (one object per pair)."""
        if (short, revision) not in made:
            name = _name(short)
            path = tmp_path / "store" / name / revision
            path.mkdir(parents=True)
            made[(short, revision)] = SimpleNamespace(
                name=name, path=str(path), version=revision, manifest_name=short,
                artifact_id=plugin_registry.compute_artifact_id(
                    repo="acme/" + name, revision=revision, subdir="", name=name),
                manifest={"name": name,
                          "casa": {"callbacks": [{"name": _DECLARED}]}},
                owned=True, targets=[f"specialist:{_SLUG}"])
        return made[(short, revision)]

    other = SimpleNamespace(
        name="gmail", path=str(tmp_path / "store" / "gmail"), version="v1",
        manifest_name="gmail", manifest={"name": "gmail", "casa": {}},
        artifact_id=plugin_registry.compute_artifact_id(
            repo="acme/gmail", revision="v1", subdir="", name="gmail"),
        owned=False, targets=["resident:assistant"])
    standing = plugin_registry.PluginIssue(
        name="weather", target="executor:browser", stage="registry",
        reason_code=plugin_registry.IGNORED_EXECUTOR_TARGET, artifact_id=None)
    state = {"plugins": [other, *(plugin(s) for s in owned)], "fail_reloads": 0}

    def resolver(target=None):
        return SimpleNamespace(registry_valid=True,
                               plugins=list(state["plugins"]),
                               issues=[standing])

    def entries():
        return [{"name": p.name, "artifact_id": p.artifact_id,
                 "targets": list(p.targets),
                 **({"owner": f"specialist:{_SLUG}"} if p.owned else {})}
                for p in state["plugins"]]

    acks_path = tmp_path / "callback_acks.json"
    monkeypatch.setattr(callback_acks, "ACKS",
                        callback_acks.CallbackAckStore(acks_path))
    monkeypatch.setattr(event_acks, "ACKS",
                        event_acks.EventAckStore(tmp_path / "event_acks.json"))
    monkeypatch.setattr(trigger_acks, "ACKS",
                        trigger_acks.TriggerAckStore(tmp_path / "trig.json"))
    spool = callback_spool.CallbackSpool(tmp_path / "cb")
    monkeypatch.setattr(callback_spool, "get_spool", lambda: spool)

    telegram = _FakeTelegram()
    registry = TriggerRegistry(scheduler=None, app=None, bus=None)
    runtime = SimpleNamespace(
        role_configs={"assistant": SimpleNamespace(channels=["telegram"])},
        channel_manager=_FakeChannelManager(telegram), agents={},
        agents_dir=None, trigger_registry=registry)
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)

    # The real reconcile; the store is read off the module at call time,
    # because a restart replaces it.
    async def reconcile():
        await callback_reconcile.reconcile_plugin_callbacks(
            trigger_registry=registry, role_configs=runtime.role_configs,
            channel_manager=runtime.channel_manager, acks=callback_acks.ACKS,
            spool=spool, resolver=resolver, entries=entries, prompt=True)
        for _ in range(8):           # let the consent card's post land
            await asyncio.sleep(0)

    def restart():
        """Casa restarts: the consent store is read again from its file."""
        monkeypatch.setattr(callback_acks, "ACKS",
                            callback_acks.CallbackAckStore(acks_path))

    # The agent/agents reload scopes run the callback reconcile.
    async def dispatch(scope, *, runtime, role=None):
        await reconcile()
        if state["fail_reloads"]:
            state["fail_reloads"] -= 1
            return {"status": "error", "kind": "reload_failed"}
        return {"status": "ok"}

    async def nothing(*a, **k):
        return None

    async def no_engagements(slug, *, reason, acknowledged=None):
        return []

    monkeypatch.setattr(reload_mod, "dispatch", dispatch)
    monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda *a, **k: SimpleNamespace(valid=True))
    monkeypatch.setattr(
        plugin_registry, "owned_entries_for",
        lambda slug, reg: [e for e in entries()
                           if e.get("owner") == f"specialist:{slug}"])
    monkeypatch.setattr(
        plugin_registry, "resolve_for",
        lambda t: SimpleNamespace(plugins=[p for p in state["plugins"]
                                           if t in p.targets]))
    monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                        lambda plugin_name: {"ready": True, "reasons": [],
                                             "targets": []})
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                        lambda issues: None)
    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible", nothing)
    monkeypatch.setattr(tools_mod, "_record_plugin_installer", lambda names: None)
    monkeypatch.setattr(tools_mod, "_own_chat_hook", nothing)
    monkeypatch.setattr(tools_mod, "_own_chat_disclosure", lambda role: None)
    monkeypatch.setattr(tools_mod, "_bound_persona", lambda slug: None)
    monkeypatch.setattr(tools_mod, "_open_specialist_engagements",
                        lambda slug: [])
    monkeypatch.setattr(tools_mod, "close_specialist_engagements",
                        no_engagements)
    monkeypatch.setattr(tools_mod, "_prune_bundle_receipt", lambda rid: None)
    monkeypatch.setattr(specialist_install, "reclaim_staging_tree",
                        lambda p: None)
    monkeypatch.setattr(specialist_bundle_journal, "complete", lambda p: None)

    # The library transactions, reduced to the owned-set swap they commit and
    # what they record about it: which names the swap dropped and which it
    # added. `rollback_disk` is the compensation's restore of the pre-swap set.
    def swap_to(targets):
        before = [p for p in state["plugins"] if p.owned]
        state["plugins"] = [other, *targets]
        old, new = [p.name for p in before], [p.name for p in targets]
        new_ids = {p.artifact_id for p in targets}

        def rollback_disk():
            state["plugins"] = [other, *before]

        return SimpleNamespace(
            slug=_SLUG, journal_path=str(tmp_path / "j.json"),
            owned_swap_committed=True,
            before_entries=[{"name": p.name, "artifact_id": p.artifact_id}
                            for p in before],
            before_tuple_files={"active.yaml": "installed"} if before else {},
            removed_artifact_ids=tuple(p.artifact_id for p in before
                                       if p.artifact_id not in new_ids),
            new_artifact_ids=tuple(sorted(new_ids)),
            removed_owned_names=tuple(n for n in old if n not in new),
            added_owned_names=tuple(n for n in new if n not in old),
            rollback_disk=rollback_disk, activation_kept=lambda: False)

    # What each library call publishes; a test sets the ones it uses.
    library: dict = {}

    def activated(op):
        return (SimpleNamespace(slug=_SLUG, state="active",
                                last_activation_error=None),
                swap_to(library[op]))

    monkeypatch.setattr(specialist_install, "uninstall_specialist",
                        lambda **k: swap_to([]))
    monkeypatch.setattr(specialist_install, "commit_specialist_install",
                        lambda *a, **k: activated("install"))
    monkeypatch.setattr(specialist_install, "upgrade_specialist",
                        lambda **k: activated("upgrade"))
    monkeypatch.setattr(specialist_install, "rollback_specialist",
                        lambda **k: activated("rollback"))

    def identity(short):
        eff = _effective(short)
        return _name(short), eff, plugin_callbacks.declaration_digest(
            {"declared": _DECLARED, "effective": eff})

    for short in acked:            # what the operator's Approve records
        callback_acks.ACKS.record(*identity(short))

    def consent_on_file(short) -> bool:
        """Read from the file, as the next start would."""
        return callback_acks.CallbackAckStore(acks_path).get(
            plugin_callbacks.ack_identity(*identity(short))) is not None

    def routed(short) -> bool:
        """Would a GET /callback/<effective> be delivered to the plugin?"""
        return registry.get_callback(_effective(short)) is not None

    def pending(short) -> list:
        """The consent the reconcile is waiting for, for *short*'s callback."""
        desired = callback_reconcile.compute_desired(
            role_configs=runtime.role_configs, acks=callback_acks.ACKS,
            resolver=resolver, entries=entries)
        assert desired.prunable is False       # the prune never runs here
        return [p for p in desired.pending
                if p["effective"] == _effective(short)]

    def cards(short) -> list:
        return [p for p in telegram.posts
                if f"/callback/{_effective(short)}" in p[2]]

    def leave_flows(short):
        """An authorization still under way for *short* (a minted state the
        provider has not answered) and one whose result waits to be collected.
        Returns the check that both are still in the spool."""
        name = _name(short)
        spool.ensure_plugin_dirs(name)
        open_state, done_state = (secrets.token_urlsafe(24) for _ in range(2))
        waiting = callback_spool.mint(spool.root / name, open_state)
        callback_spool.mint(spool.root / name, done_state)
        done = callback_spool.state_hash(done_state)
        claim = spool.claim(name, done, now=time.time())
        assert spool.publish_result(
            claim, {"v": 1, "plugin": name, "effective": _effective(short)},
        ) is callback_spool.PublishOutcome.PUBLISHED
        assert waiting.is_file() and spool.has_result(name, done)
        return lambda: (waiting.is_file(), spool.has_result(name, done))

    return SimpleNamespace(
        tools=tools_mod, args=args, plugin=plugin, state=state, library=library,
        reconcile=reconcile, restart=restart, consent_on_file=consent_on_file,
        routed=routed, pending=pending, cards=cards, leave_flows=leave_flows)


async def _uninstall(w) -> None:
    from test_tools_specialist_install import _payload
    res = _payload(await w.tools.specialist_uninstall.handler({"slug": _SLUG}))
    assert res["ok"] is True, res


async def _run(w, op: str, *, ok: bool = True) -> dict:
    """Run the bundle handler for *op* (install, upgrade or rollback)."""
    from test_tools_specialist_install import _approved_call, _payload
    if op == "rollback":
        res = await w.tools.specialist_rollback.handler({"slug": _SLUG})
    else:
        tool = (w.tools.specialist_install_commit if op == "install"
                else w.tools.specialist_upgrade)
        res = await _approved_call(tool, w.args)
    res = _payload(res)
    assert res["ok"] is ok, res
    return res


def test_a_transaction_that_never_swapped_names_no_added_plugin():
    """A pending or failed upgrade leaves the owned set as it was: whatever
    the transaction carries, nothing entered and nothing is reset."""
    import tools as tools_mod
    carried = dict(added_owned_names=(_name("auth"),), removed_owned_names=())
    assert tools_mod._swap_added_names(SimpleNamespace(
        owned_swap_committed=True, **carried)) == [_name("auth")]
    assert tools_mod._swap_added_names(SimpleNamespace(
        owned_swap_committed=False, **carried)) == []
    assert tools_mod._swap_added_names(SimpleNamespace()) == []


@pytest.mark.asyncio
class TestLeavingASpecialistBundleRevokesItsCallbackConsent:
    async def test_a_reinstall_after_uninstall_asks_again(
            self, monkeypatch, tmp_path):
        w = _world(monkeypatch, tmp_path)
        w.library["install"] = [w.plugin("auth")]      # the same declaration
        flows_alive = w.leave_flows("auth")
        await w.reconcile()
        assert w.routed("auth") and w.cards("auth") == []   # approved, delivering

        await _uninstall(w)
        # the uninstall itself revokes: nothing waits for a later prune
        assert not w.routed("auth")
        assert not w.consent_on_file("auth")
        assert flows_alive() == (False, False)
        await _run(w, "install")

        assert not w.routed("auth")
        assert len(w.pending("auth")) == 1
        assert len(w.cards("auth")) == 1               # the operator is asked
        # and nothing on file brings it back at the next start
        assert not w.consent_on_file("auth")
        w.restart()
        await w.reconcile()
        assert not w.routed("auth")
        assert len(w.pending("auth")) == 1

    @pytest.mark.parametrize("op", ["upgrade", "rollback"])
    async def test_a_plugin_the_new_generation_drops_loses_its_consent(
            self, monkeypatch, tmp_path, op):
        w = _world(monkeypatch, tmp_path, owned=("auth", "extra"),
                   acked=("auth", "extra"))
        w.library[op] = [w.plugin("auth", "v2")]       # `extra` is not carried
        await w.reconcile()
        assert w.routed("auth") and w.routed("extra")

        await _run(w, op)

        assert not w.consent_on_file("extra")
        # the plugin that stayed is untouched
        assert w.consent_on_file("auth")
        assert w.routed("auth") and w.cards("auth") == []
        # wherever `extra` comes back from, it is asked about again
        w.restart()
        w.state["plugins"].append(w.plugin("extra"))
        await w.reconcile()
        assert not w.routed("extra")
        assert len(w.pending("extra")) == 1


@pytest.mark.asyncio
class TestEnteringASpecialistBundleStartsWithNoCallbackConsent:
    async def test_a_consent_stranded_by_an_earlier_uninstall_is_not_inherited(
            self, monkeypatch, tmp_path):
        """The state an install can be in from before this rule: the plugin
        is gone and its consent is still on file."""
        w = _world(monkeypatch, tmp_path, owned=(), acked=("auth",))
        w.library["install"] = [w.plugin("auth")]
        await w.reconcile()
        assert w.consent_on_file("auth") and not w.routed("auth")

        await _run(w, "install")

        assert not w.routed("auth")
        assert len(w.pending("auth")) == 1
        assert len(w.cards("auth")) == 1
        assert not w.consent_on_file("auth")

    async def test_flows_left_in_the_spool_do_not_survive_the_install(
            self, monkeypatch, tmp_path):
        w = _world(monkeypatch, tmp_path, owned=(), acked=())
        w.library["install"] = [w.plugin("auth")]
        flows_alive = w.leave_flows("auth")
        await w.reconcile()
        assert flows_alive() == (True, True)

        await _run(w, "install")

        assert flows_alive() == (False, False)


@pytest.mark.asyncio
class TestAPluginThatStaysInTheBundleKeepsItsCallbackConsent:
    async def test_an_upgrade_and_a_rollback_keep_consent_and_spool(
            self, monkeypatch, tmp_path):
        w = _world(monkeypatch, tmp_path)
        w.library["upgrade"] = [w.plugin("auth", "v2")]
        w.library["rollback"] = [w.plugin("auth", "v1")]
        flows_alive = w.leave_flows("auth")
        await w.reconcile()
        assert w.routed("auth")

        for op in ("upgrade", "rollback"):
            await _run(w, op)
            assert w.routed("auth"), op
            assert w.consent_on_file("auth"), op
            assert w.pending("auth") == [] and w.cards("auth") == [], op
            assert flows_alive() == (True, True), op

    @pytest.mark.parametrize("op", ["install", "upgrade"])
    async def test_a_failed_change_that_is_rolled_back_keeps_them_too(
            self, monkeypatch, tmp_path, op):
        """The sequencer fails, so the compensation restores the owned set and
        runs the sequencer again over it: that second run resets nothing."""
        w = _world(monkeypatch, tmp_path)
        w.library[op] = [w.plugin("auth", "v2")]
        flows_alive = w.leave_flows("auth")
        await w.reconcile()
        assert w.routed("auth")
        w.state["fail_reloads"] = 1

        res = await _run(w, op, ok=False)

        assert res.get("rolled_back") is True, res
        assert [p.version for p in w.state["plugins"] if p.owned] == ["v1"]
        assert w.consent_on_file("auth")
        assert w.routed("auth")
        assert w.pending("auth") == [] and w.cards("auth") == []
        assert flows_alive() == (True, True)
