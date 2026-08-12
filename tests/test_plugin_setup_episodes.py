"""v0.112.0 — durable post-consent setup episodes (casa-plugin-elevenlabs#2).

Round-ledger model (impl review r1): membership registered at PROMPT time,
settlement = all members decided (deny/expiry settle too — never ack
counting), approvals keyed by ack generation (re-consent mints a new
episode), single-lock settlement, unambiguous server binding, terminal-state
decay/supersession.
"""

from __future__ import annotations

import json

import pytest

import plugin_setup_episodes as pse
from plugin_store import StoreError, manifest_setup_tool


# ---------------------------------------------------------------------------
# Manifest contract
# ---------------------------------------------------------------------------

def test_manifest_setup_tool_absent_is_none():
    assert manifest_setup_tool({}) is None
    assert manifest_setup_tool({"casa": {}}) is None
    assert manifest_setup_tool({"casa": "nope"}) is None


def test_manifest_setup_tool_valid():
    m = {"casa": {"setupTool": "setup_elevenlabs_voicemail"}}
    assert manifest_setup_tool(m) == "setup_elevenlabs_voicemail"


@pytest.mark.parametrize("bad", [
    "", "voicemail_setup", "setup_", "setup_Voicemail", "setup_a b",
    "setup_ünïcode", "setup_" + "x" * 65, 7, None, ["setup_x"],
])
def test_manifest_setup_tool_malformed_refuses(bad):
    with pytest.raises(StoreError) as exc:
        manifest_setup_tool({"casa": {"setupTool": bad}})
    assert exc.value.reason_code == "setup_tool_invalid"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Configure the module against fakes + a tmp store."""
    monkeypatch.setattr(pse, "STORE_PATH", tmp_path / "episodes.json")
    monkeypatch.setattr(pse, "_worker_task", None)
    monkeypatch.setattr(pse, "_lock", None)
    monkeypatch.setattr(pse, "_kick", None)

    state = {
        "entry": {
            "artifact_id": "art-1",
            "targets": ["resident:assistant"],
            "granted_tools": ["mcp__plugin_elevenlabs_elevenlabs"],
            "setup_tool": "setup_elevenlabs_voicemail",
        },
        "dispatches": [],
        "dispatch_ok": True,
        "notes": [],
        "sleeps": [],
    }

    async def dispatch(role, text, context):
        state["dispatches"].append((role, text, context))
        return state["dispatch_ok"]

    async def notify(text):
        state["notes"].append(text)

    async def fake_sleep(s):
        state["sleeps"].append(s)

    pse.configure(
        dispatch=dispatch, notify_operator=notify,
        resolve_registry_entry=lambda plugin: state["entry"],
        sleep=fake_sleep,
    )
    return state


def wired_dispatch(state):
    async def dispatch(role, text, context):
        state["dispatches"].append((role, text, context))
        return state["dispatch_ok"]
    return dispatch


def wired_notify(state):
    async def notify(text):
        state["notes"].append(text)
    return notify


async def _drain_pending(state):
    for ep in pse.episodes("pending"):
        await pse._run_episode(ep)


def _owe(plugin="elevenlabs", artifact="art-1", consent_pending=False):
    """What the reconciler sweep does first: record that Casa owes this exact
    artifact a setup run (#451). Sealing a round without this models a plugin
    that declares no ``casa.setupTool``.

    ``consent_pending`` is what the sweep passes when it can see an UNACKED
    consent for the artifact — the input that re-arms a terminal row. The
    helpers below set it because they are about to open a member, which is
    precisely the state the reconciler would report as pending."""
    return pse.ensure_obligation(plugin=plugin, artifact_id=artifact,
                                 consent_pending=consent_pending)


def _prompt(plugin="elevenlabs", artifact="art-1", identity="id-a"):
    _owe(plugin, artifact, consent_pending=True)
    return pse.open_round(plugin=plugin, artifact_id=artifact,
                          identities=[identity]).get(identity, "")


def _open(identities, plugin="elevenlabs", artifact="art-1"):
    _owe(plugin, artifact, consent_pending=True)
    return pse.open_round(plugin=plugin, artifact_id=artifact,
                          identities=identities)


def _released(plugin="elevenlabs"):
    """The obligations the worker may now dispatch — the post-#451 shape of
    what ``episodes("pending")`` used to mean (a round that settled and minted
    an episode). An obligation still holding for a verdict is `pending` too,
    so tests that mean "setup is authorized" must say so explicitly."""
    return [e for e in pse.episodes("pending")
            if e.get("gate") == "released" and e.get("plugin") == plugin]


async def _decide(plugin="elevenlabs", artifact="art-1", identity="id-a",
                  approved=True, gen="g1", nonce=""):
    await pse.on_consent_decision(
        plugin=plugin, artifact_id=artifact, identity=identity,
        approved=approved, approval_gen=gen if approved else "",
        nonce=nonce)


# ---------------------------------------------------------------------------
# Settlement (round ledger)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_prompt_approve_settles(wired):
    _prompt()
    await _decide()
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["plugin"] == "elevenlabs"
    assert eps[0]["approved_identities"] == ["id-a#g1"]
    # round consumed
    assert pse._load()["rounds"] == {}


@pytest.mark.asyncio
async def test_round_waits_for_all_members(wired):
    _open(["id-a", "id-b"])
    await _decide(identity="id-a")
    assert _released() == []              # id-b still open — obligation holds
    await _decide(identity="id-b", gen="g2")
    eps = _released()
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a#g1", "id-b#g2"]


@pytest.mark.asyncio
async def test_mixed_round_settles_without_dispatch(wired):
    # impl r3 all-approved gate: the deny DECIDES member b (settlement still
    # comes from the round ledger, never ack counting) but a mixed round
    # must NOT run the plugin-wide setup tool — operator note instead.
    _open(["id-a", "id-b"])
    await _decide(identity="id-a", approved=True)
    assert _released() == []
    await _decide(identity="id-b", approved=False)
    assert _released() == []
    assert pse.episodes()[0]["status"] == "refused"
    assert any("was NOT run" in n for n in wired["notes"])


@pytest.mark.asyncio
async def test_deny_only_round_notes_and_skips(wired):
    _prompt()
    await _decide(approved=False)
    assert _released() == []
    assert pse.episodes()[0]["status"] == "refused"
    assert any("was NOT run" in n for n in wired["notes"])


@pytest.mark.asyncio
async def test_reprompt_reopens_member(wired):
    # expiry decided the member; the next reconcile re-prompts → the round
    # must WAIT for the fresh decision again.
    _open(["id-a", "id-b"])
    await _decide(identity="id-b", approved=False)   # expired
    _prompt(identity="id-b")                          # re-prompted (merge)
    await _decide(identity="id-a", approved=True)
    assert _released() == []                          # id-b open again
    await _decide(identity="id-b", approved=True, gen="g9")
    assert len(_released()) == 1


@pytest.mark.asyncio
async def test_stale_nonce_expiry_ignored(wired):
    # impl r3/r5: after a member is decided (expired) and RE-OPENED with a
    # fresh keyboard/nonce, a LATE callback from the FIRST keyboard (old
    # nonce) must not re-decide the member; the fresh keyboard governs.
    n1 = _prompt(identity="id-a")
    await _decide(identity="id-a", approved=False, nonce=n1)  # keyboard 1 expires
    n2 = _prompt(identity="id-a")                     # re-prompt, fresh nonce
    assert n1 and n2 and n1 != n2
    await _decide(identity="id-a", approved=False, nonce=n1)  # STALE late cb
    rounds = pse._load()["rounds"]
    assert rounds["elevenlabs"]["members"]["id-a"]["state"] == "open"
    await _decide(identity="id-a", approved=True, nonce=n2)
    assert len(pse.episodes("pending")) == 1


@pytest.mark.asyncio
async def test_sync_approval_record_plus_boot_recovery(wired, monkeypatch):
    # impl r3 crash window: ack persisted + sync approval recorded, process
    # dies before the finish hook. Boot recovery settles from the ledger.
    _prompt(identity="id-a")
    pse.record_approval_sync(plugin="elevenlabs", artifact_id="art-1",
                             identity="id-a", gen="g7")
    # "restart": fresh lock/kick, ack lookup available
    monkeypatch.setattr(pse, "_lock", None)
    monkeypatch.setattr(pse, "_kick", None)
    pse.configure(
        dispatch=lambda *a: None, notify_operator=None,
        resolve_registry_entry=lambda plugin: wired["entry"],
        ack_lookup=lambda identity: "g7")
    await pse._recover_and_settle()
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a#g7"]


@pytest.mark.asyncio
async def test_boot_recovery_approves_open_member_from_ack(wired, monkeypatch):
    # crash BEFORE even the sync record: the persisted ack alone recovers.
    _prompt(identity="id-a")
    monkeypatch.setattr(pse, "_lock", None)
    monkeypatch.setattr(pse, "_kick", None)
    pse.configure(
        dispatch=lambda *a: None, notify_operator=None,
        resolve_registry_entry=lambda plugin: wired["entry"],
        ack_lookup=lambda identity: "g8")
    await pse._recover_and_settle()
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a#g8"]


@pytest.mark.asyncio
async def test_reconsent_new_generation_mints_new_episode(wired):
    # impl-r1: identical tuple, NEW approval generation → new episode key.
    _prompt()
    await _decide(gen="g1")
    await _drain_pending(wired)
    assert pse.episodes()[0]["status"] == "dispatched"
    _prompt()                                         # revoke → re-prompt
    await _decide(gen="g2")                           # re-approve, new gen
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a#g2"]


@pytest.mark.asyncio
async def test_round_ledger_survives_restart(wired, tmp_path, monkeypatch):
    # impl-r1: durable ACROSS the consent round — approve a, "restart"
    # (fresh module state, same store file), deny b → episode still fires.
    _open(["id-a", "id-b"])
    await _decide(identity="id-a", approved=True)
    monkeypatch.setattr(pse, "_lock", None)   # simulate process restart
    monkeypatch.setattr(pse, "_kick", None)
    pse.configure(
        dispatch=lambda *a: None, notify_operator=None,
        resolve_registry_entry=lambda plugin: wired["entry"])
    await _decide(identity="id-b", approved=True, gen="g5")
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a#g1", "id-b#g5"]


@pytest.mark.asyncio
async def test_unprompted_decision_synthesizes_round(wired):
    # A decision with no registered prompt (store reset) is never dropped —
    # it synthesizes a round and is recorded. #451 r7: that round is NOT
    # authoritative, because the reconciler never sealed it and so nothing
    # established the plugin's consent position. The decision is kept; the
    # obligation holds until a pass that CAN establish the position seals a
    # verdict (see test_a_delayed_finish_cannot_resurrect_a_consumed_round).
    _owe()
    await _decide()
    assert _released() == []
    assert pse._load()["rounds"] == {}                # round consumed
    assert pse.episodes()[0]["gate"] == "awaiting_verdict"


@pytest.mark.asyncio
async def test_new_artifact_resets_round(wired):
    _prompt(artifact="art-OLD", identity="id-old")
    _prompt(artifact="art-1", identity="id-new")      # new generation
    await _decide(artifact="art-1", identity="id-new")
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["artifact_id"] == "art-1"
    assert eps[0]["approved_identities"] == ["id-new#g1"]


@pytest.mark.asyncio
async def test_stale_artifact_decision_ignored(wired):
    # impl r2 (both reviewers): a LATE decision from a superseded artifact
    # must never replace the current round. prompt A(art-OLD) → update →
    # prompt B(art-1) → late art-OLD decision → ignored; art-1 completes.
    _prompt(artifact="art-OLD", identity="id-old")
    _prompt(artifact="art-1", identity="id-new")      # prompt path resets
    await _decide(artifact="art-OLD", identity="id-old", approved=False)
    rounds = pse._load()["rounds"]
    assert rounds["elevenlabs"]["artifact_id"] == "art-1"   # round intact
    assert _released() == []
    await _decide(artifact="art-1", identity="id-new", approved=True)
    eps = _released()
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-new#g1"]


@pytest.mark.asyncio
async def test_consumed_key_replay_never_recreates(wired):
    # impl r2 (Sol): a replayed stale generation must not recreate its
    # consumed episode and prune the current one — tombstoned keys refuse
    # the claim.
    _prompt()
    await _decide(gen="g1")
    await _drain_pending(wired)
    _prompt()
    await _decide(gen="g2")                           # supersedes g1
    eps = pse.episodes()
    assert len(eps) == 1 and eps[0]["approved_identities"] == ["id-a#g2"]
    await _decide(gen="g1")                           # stale replay
    eps = pse.episodes()
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a#g2"]   # g2 survives


@pytest.mark.asyncio
async def test_settlement_without_setup_tool_is_noop(wired):
    # #451: "no setup tool" is now expressed by the reconciler recording NO
    # obligation for the plugin (the candidate sweep skips it), so settlement
    # has nothing to release. open_round still runs — its nonces fence the
    # consent keyboards regardless of whether setup is owed.
    wired["entry"] = dict(wired["entry"], setup_tool=None)
    pse.open_round(plugin="gmail", artifact_id="art-9", identities=["id-x"])
    await _decide(plugin="gmail", artifact="art-9", identity="id-x")
    assert pse.episodes() == []


@pytest.mark.asyncio
async def test_new_episode_supersedes_old_ones(wired):
    _prompt()
    await _decide(gen="g1")
    await _drain_pending(wired)
    _prompt()
    await _decide(gen="g2")
    eps = pse.episodes()
    assert len(eps) == 1                              # old pruned
    assert eps[0]["status"] == "pending"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_targets_assistant_with_exact_tool(wired):
    _prompt()
    await _decide()
    await _drain_pending(wired)
    assert len(wired["dispatches"]) == 1
    role, text, ctx = wired["dispatches"][0]
    assert role == "assistant"
    assert "mcp__plugin_elevenlabs_elevenlabs__setup_elevenlabs_voicemail" in text
    assert ctx["synthetic"] == "plugin_setup"
    ep = pse.episodes()[0]
    assert ep["status"] == "dispatched"
    assert ctx["setup_episode"] == ep["id"]


@pytest.mark.asyncio
async def test_specialist_only_target_delegates_via_assistant(wired):
    wired["entry"] = dict(wired["entry"], targets=["specialist:finance"])
    _prompt()
    await _decide()
    await _drain_pending(wired)
    role, text, _ = wired["dispatches"][0]
    assert role == "assistant"
    assert "'finance'" in text and "Delegate" in text
    assert "do not substitute" in text


@pytest.mark.asyncio
async def test_ambiguous_server_binding_fails_episode(wired):
    # impl-r1: zero or several server grants → FAIL with reason, never an
    # unqualified or guessed namespaced name.
    wired["entry"] = dict(wired["entry"], granted_tools=[
        "mcp__plugin_x_a", "mcp__plugin_x_b"])
    _prompt()
    await _decide()
    await _drain_pending(wired)
    assert wired["dispatches"] == []
    ep = pse.episodes()[0]
    assert ep["status"] == "failed"
    assert "ambiguous" in ep["last_error"]


@pytest.mark.asyncio
async def test_stale_artifact_never_fires(wired):
    _prompt()
    await _decide()
    wired["entry"] = dict(wired["entry"], artifact_id="art-2")  # superseded
    await _drain_pending(wired)
    assert wired["dispatches"] == []
    assert pse.episodes()[0]["status"] == "stale"
    assert any("dropped" in n for n in wired["notes"])


@pytest.mark.asyncio
async def test_dispatch_failure_retries_then_holds(wired):
    # #451 r4 (Sol): a rejected dispatch HOLDS rather than going terminal. Bus
    # rejection is transient — most often no operator DM is reachable yet — and
    # with the hand-back gone there is no second runner to compensate, so a
    # terminal `failed` lost an ungated plugin's setup for good (nothing
    # re-arms a terminal row without a pending consent). The burst is still
    # bounded WITHIN the pass; the row stays actionable in health.
    wired["dispatch_ok"] = False
    _prompt()
    await _decide()
    await _drain_pending(wired)
    assert len(wired["dispatches"]) == 3              # bounded retries
    row = pse.episodes()[0]
    assert row["status"] == "pending" and row["gate"] == "released"
    assert "not accepted" in row["last_error"]
    assert wired["notes"] == []                       # nothing to tell yet
    # ...and it lands on a later kick once dispatch works.
    wired["dispatch_ok"] = True
    await _drain_pending(wired)
    assert pse.episodes()[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_boot_redispatch_of_pending_episode(wired):
    _prompt()
    await _decide()
    raw = json.loads(pse.STORE_PATH.read_text())
    assert raw["episodes"][0]["status"] == "pending"
    await _drain_pending(wired)                       # boot-kicked drain
    assert pse.episodes()[0]["status"] == "dispatched"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_issues_surface_and_decay(wired, monkeypatch):
    # A genuinely terminal outcome decays; a HOLD does not (a dispatch that has
    # not landed stays actionable indefinitely — see
    # test_dispatch_failure_retries_then_holds).
    _prompt()
    await _decide()
    pse._update_episode(pse.episodes()[0]["id"], status="failed",
                        last_error="ambiguous server binding")
    rows = pse.health_issues()
    assert rows and rows[0]["kind"] == "setup_episode_failed"
    # decay: age the failure past the window → no longer surfaced
    import time as _time
    far_future = _time.time() + pse._HEALTH_DECAY_S + 10.0
    monkeypatch.setattr(pse, "_now", lambda: far_future)
    assert pse.health_issues() == []


@pytest.mark.asyncio
async def test_sealed_batch_never_settles_partially(wired):
    # impl r4: open_round seals BOTH members before any keyboard exists —
    # a fast approve on the first cannot settle a partial round.
    _open(["id-a", "id-b"])
    await _decide(identity="id-a", approved=True)
    assert _released() == []                           # sealed: waits for b
    await _decide(identity="id-b", approved=True, gen="g2")
    assert len(pse.episodes("pending")) == 1


@pytest.mark.asyncio
async def test_reopened_subset_keeps_earlier_decisions(wired):
    # a later batch re-prompting a subset must not erase earlier decisions.
    _open(["id-a", "id-b"])
    await _decide(identity="id-a", approved=True)
    _open(["id-b"])                                    # re-prompt subset
    await _decide(identity="id-b", approved=True, gen="g2")
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a#g1", "id-b#g2"]


@pytest.mark.asyncio
async def test_route_gate_holds_dispatch_until_live(wired, monkeypatch):
    # impl r4 (Sol): a pending episode does not dispatch while the plugin's
    # routes are down; a later kick after the reconcile heals dispatches.
    live = {"v": False}
    monkeypatch.setattr(pse, "_routes_live", lambda plugin: live["v"])
    _prompt()
    await _decide()
    await _drain_pending(wired)
    assert wired["dispatches"] == []
    ep = pse.episodes()[0]
    assert ep["status"] == "pending"
    assert "waiting for live trigger route" in (ep.get("last_error") or "")
    live["v"] = True
    await _drain_pending(wired)                        # post-reconcile kick
    assert len(wired["dispatches"]) == 1
    assert pse.episodes()[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_secrets_gate_holds_dispatch_until_ready(wired, monkeypatch):
    # #423: a pending episode does not dispatch while the plugin's required
    # env vars are unresolved — the setup tool's MCP server would spawn with
    # literal ${VAR} placeholders. A later kick after the secrets resolve
    # (plugin_env reload) dispatches.
    ready = {"v": False}
    monkeypatch.setattr(pse, "_secrets_ready", lambda plugin: ready["v"])
    _prompt()
    await _decide()
    await _drain_pending(wired)
    assert wired["dispatches"] == []
    ep = pse.episodes()[0]
    assert ep["status"] == "pending"
    assert "waiting for plugin secrets" in (ep.get("last_error") or "")
    ready["v"] = True
    await _drain_pending(wired)                        # post-reload kick
    assert len(wired["dispatches"]) == 1
    assert pse.episodes()[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_secrets_gate_exception_holds_pending(wired, monkeypatch):
    # #423: a raising readiness check fails CLOSED — the episode stays
    # pending (retried on later kicks), never dispatches into a session
    # whose secrets state is unknown.
    def boom(plugin):
        raise RuntimeError("readiness check broke")
    monkeypatch.setattr(pse, "_secrets_ready", boom)
    _prompt()
    await _decide()
    await _drain_pending(wired)
    assert wired["dispatches"] == []
    assert pse.episodes()[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_execution_gate_holds_until_target_can_load_plugin(wired,
                                                                 monkeypatch):
    # #423 r2 (Sol 1 / Terra 1): secrets resolving is not enough — the
    # EXECUTING agent may still hold a snapshot built while the plugin was
    # withheld (env-unresolved), and a dispatch into that session consumes
    # the episode against a session without the tool. Hold until the target
    # can actually load it; the agent-reload kick retries.
    ready = {"v": False}
    monkeypatch.setattr(pse, "_execution_ready",
                        lambda role, plugin, artifact_id: ready["v"])
    _prompt()
    await _decide()
    await _drain_pending(wired)
    assert wired["dispatches"] == []
    ep = pse.episodes()[0]
    assert ep["status"] == "pending"
    assert "waiting for target agent" in (ep.get("last_error") or "")
    ready["v"] = True
    await _drain_pending(wired)                        # post-agent-reload kick
    assert len(wired["dispatches"]) == 1
    assert pse.episodes()[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_execution_gate_checks_the_executing_role(wired, monkeypatch):
    # The gate must receive the EXECUTING role (plugin_dispatch.execution_role
    # — the specialist on the delegation branch, not the assistant courier).
    seen = []
    monkeypatch.setattr(
        pse, "_execution_ready",
        lambda role, plugin, artifact_id:
            seen.append((role, plugin, artifact_id)) or True)
    _prompt()
    await _decide()
    await _drain_pending(wired)
    assert seen == [("assistant", "elevenlabs", "art-1")]
    assert len(wired["dispatches"]) == 1


@pytest.mark.asyncio
async def test_execution_gate_skips_specialist_branch(wired, monkeypatch):
    # #423 r3 (Terra r2-1): specialists are NOT boot-registered runtime
    # agents — they build options FRESH per delegation, resolving the
    # CURRENT environment (and the specialist builder withholds
    # env-unresolved plugins). Consulting the resident-binding seam for a
    # specialist-only plugin would strand its episode forever.
    wired["entry"]["targets"] = ["specialist:finance"]
    monkeypatch.setattr(pse, "_execution_ready",
                        lambda role, plugin, artifact_id: False)
    _prompt()
    await _decide()
    await _drain_pending(wired)
    assert len(wired["dispatches"]) == 1           # NOT held by the seam
    assert pse.episodes()[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_configure_wires_secrets_ready(wired):
    # #423: the seam arrives through configure(), like routes_live; a
    # configure without the kwarg resets it (no stale gate across boots).
    pse.configure(
        dispatch=wired_dispatch(wired), notify_operator=wired_notify(wired),
        resolve_registry_entry=lambda plugin: wired["entry"],
        secrets_ready=lambda plugin: False,
    )
    _prompt()
    await _decide()
    await _drain_pending(wired)
    assert wired["dispatches"] == []
    assert pse.episodes()[0]["status"] == "pending"
    pse.configure(
        dispatch=wired_dispatch(wired), notify_operator=wired_notify(wired),
        resolve_registry_entry=lambda plugin: wired["entry"],
    )
    await _drain_pending(wired)                        # kwarg absent ⇒ no gate
    assert len(wired["dispatches"]) == 1


@pytest.mark.asyncio
async def test_blank_feed_gen_preserves_recorded_gen(wired):
    # impl r4 (Terra): a feed whose acks.get failed (gen="") must not
    # overwrite the durably-recorded generation.
    _prompt()
    pse.record_approval_sync(plugin="elevenlabs", artifact_id="art-1",
                             identity="id-a", gen="g7")
    await _decide(gen="")                              # blank async feed
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a#g7"]


@pytest.mark.asyncio
async def test_open_round_preserves_open_member_nonce(wired):
    # impl r5 (Terra): a re-seal of an ALREADY-OPEN member keeps its nonce
    # (its consent keyboard is deduped and still carries the original
    # callback+nonce) — so a later deny/expiry from that keyboard is NOT
    # rejected as stale.
    n1 = _open(["id-a"])["id-a"]
    n2 = _open(["id-a"])["id-a"]      # reconcile re-fires while keyboard live
    assert n1 == n2
    # the retained keyboard's expiry (carrying n1) must decide the member
    await _decide(identity="id-a", approved=False, nonce=n1)
    assert any("was NOT run" in n for n in wired["notes"])


@pytest.mark.asyncio
async def test_open_round_fresh_nonce_after_decision(wired):
    # a member RE-OPENED after a terminal decision (old keyboard gone, fresh
    # keyboard posts) gets a FRESH nonce.
    n1 = _open(["id-a"])["id-a"]
    await _decide(identity="id-a", approved=False, nonce=n1)   # expired
    n2 = _open(["id-a"])["id-a"]                                # re-prompt
    assert n2 != n1
    assert pse._load()["rounds"]["elevenlabs"]["members"]["id-a"]["state"] \
        == "open"


@pytest.mark.asyncio
async def test_legacy_plugin_denial_is_silent(wired):
    # impl r6 (Terra): a plugin with NO casa.setupTool must settle silently
    # on the deny path too — no spurious "setup tool NOT run" note.
    # #451: expressed by the reconciler recording NO obligation for it.
    wired["entry"] = dict(wired["entry"], setup_tool=None)
    pse.open_round(plugin="gmail", artifact_id="art-1",
                   identities=["id-a", "id-b"])
    await _decide(plugin="gmail", identity="id-a", approved=True)
    await _decide(plugin="gmail", identity="id-b", approved=False)
    assert pse.episodes() == []
    assert wired["notes"] == []


@pytest.mark.asyncio
async def test_release_survives_an_unavailable_registry(wired):
    # impl r7 (Sol) pinned that a registry UNAVAILABLE at final approval must
    # not lose a declared plugin's setup. #451 makes that structural rather
    # than a retry: settlement no longer consults the registry at all, so the
    # obligation is RELEASED regardless and only the DISPATCH defers.
    pse.configure(
        dispatch=wired_dispatch(wired), notify_operator=wired_notify(wired),
        resolve_registry_entry=lambda p: None,        # unavailable
        ack_lookup=lambda i: None)
    _prompt()
    await _decide()
    assert len(_released()) == 1                       # released, not lost
    assert pse._load()["rounds"] == {}                 # round consumed
    assert wired["dispatches"] == []                   # but nothing dispatched
    # once the registry resolves, the SAME released obligation dispatches.
    pse.configure(
        dispatch=wired_dispatch(wired), notify_operator=wired_notify(wired),
        resolve_registry_entry=lambda p: wired["entry"],
        ack_lookup=lambda i: None)
    await pse._worker_pass()
    assert pse.episodes()[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_persistently_unresolvable_obligation_dropped(wired, monkeypatch):
    # a plugin uninstalled after consent never resolves. #451 moves the bound
    # from settlement (which no longer resolves) to DISPATCH: the round is
    # consumed once, the obligation released, and the released obligation goes
    # stale after _MAX_RESOLVE_DEFERRALS rather than retrying forever.
    monkeypatch.setattr(pse, "_MAX_RESOLVE_DEFERRALS", 3)
    pse.configure(
        dispatch=wired_dispatch(wired), notify_operator=wired_notify(wired),
        resolve_registry_entry=lambda p: None, ack_lookup=lambda i: None)
    _prompt()
    await _decide()
    for _ in range(5):
        await pse._worker_pass()
    assert pse._load()["rounds"] == {}                # consumed, not retained
    assert pse.episodes()[0]["status"] == "stale"     # bounded
    assert wired["dispatches"] == []


@pytest.mark.asyncio
async def test_run_episode_unavailable_resolution_retries_not_stale(wired):
    # impl r8 (Sol+Terra): a durably-settled episode whose DISPATCH-time
    # resolution is transiently unavailable must NOT be marked stale — it
    # stays pending and dispatches on a later kick once resolution recovers.
    _prompt()
    await _decide()                                   # episode created (art-1)
    calls = {"n": 0}

    def flaky(plugin):
        calls["n"] += 1
        return None if calls["n"] == 1 else wired["entry"]

    pse.configure(
        dispatch=wired_dispatch(wired), notify_operator=wired_notify(wired),
        resolve_registry_entry=flaky)
    await _drain_pending(wired)                        # 1st resolve → None
    assert wired["dispatches"] == []
    ep = pse.episodes()[0]
    assert ep["status"] == "pending"                  # NOT stale
    assert ep["resolve_deferrals"] == 1
    await _drain_pending(wired)                        # 2nd resolve → entry
    assert len(wired["dispatches"]) == 1
    assert pse.episodes()[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_run_episode_persistent_unresolve_goes_stale_bounded(wired, monkeypatch):
    monkeypatch.setattr(pse, "_MAX_RESOLVE_DEFERRALS", 3)
    _prompt()
    await _decide()
    pse.configure(
        dispatch=wired_dispatch(wired), notify_operator=wired_notify(wired),
        resolve_registry_entry=lambda p: None)
    for _ in range(5):
        await _drain_pending(wired)
    assert pse.episodes()[0]["status"] == "stale"
    assert wired["dispatches"] == []


@pytest.mark.asyncio
async def test_worker_pass_signals_retry_on_unavailable(wired):
    # impl r9 (Terra): a pass that DEFERS on transient unavailability returns
    # True so the worker schedules a delayed self-kick — recovery is not left
    # to a coalesced reconcile kick.
    _prompt()
    await _decide()
    pse.configure(
        dispatch=wired_dispatch(wired), notify_operator=wired_notify(wired),
        resolve_registry_entry=lambda p: None)     # always unavailable
    assert await pse._worker_pass() is True
    # a successful resolve dispatches and does NOT ask for retry
    pse.configure(
        dispatch=wired_dispatch(wired), notify_operator=wired_notify(wired),
        resolve_registry_entry=lambda p: wired["entry"])
    assert await pse._worker_pass() is False
    assert len(wired["dispatches"]) == 1


@pytest.mark.asyncio
async def test_schedule_retry_kicks_after_delay(wired, monkeypatch):
    # the delayed self-kick fires the Event after the (injected) backoff, so
    # the worker wakes without any external kick.
    import asyncio
    monkeypatch.setattr(pse, "_lock", None)
    monkeypatch.setattr(pse, "_kick", None)
    monkeypatch.setattr(pse, "_retry_task", None)
    slept = {"d": None}

    async def fake_sleep(d):
        slept["d"] = d

    pse.configure(
        dispatch=wired_dispatch(wired), notify_operator=wired_notify(wired),
        resolve_registry_entry=lambda p: wired["entry"], sleep=fake_sleep)
    assert not pse._kick.is_set()
    pse._schedule_retry(5.0)
    await asyncio.sleep(0)              # let the retry task run
    await asyncio.sleep(0)
    assert slept["d"] == 5.0
    assert pse._kick.is_set()          # woken with no external kick


@pytest.mark.asyncio
async def test_settlement_never_defers_on_an_unavailable_registry(wired):
    # #451: impl r10 added a settlement-path retry because settlement resolved
    # the registry and could defer with no pending episode to drive recovery.
    # Settlement no longer resolves anything, so there is nothing to defer —
    # the round is consumed exactly once and the obligation released.
    pse.configure(
        dispatch=wired_dispatch(wired), notify_operator=wired_notify(wired),
        resolve_registry_entry=lambda p: None,        # stays unavailable
        ack_lookup=lambda i: None)
    _prompt()
    await _decide()
    assert pse._load()["rounds"] == {}
    assert "settle_deferrals" not in json.dumps(pse._load())
    assert len(_released()) == 1


# ---------------------------------------------------------------------------
# #521 — execution-outcome correlation
# ---------------------------------------------------------------------------

_NS = "mcp__plugin_elevenlabs_elevenlabs__setup_elevenlabs_voicemail"


async def _dispatched(wired):
    """Settle + dispatch one resident-target episode; return its row."""
    _prompt()
    await _decide()
    await _drain_pending(wired)
    ep = pse.episodes()[0]
    assert ep["status"] == "dispatched"
    return ep


@pytest.mark.asyncio
async def test_dispatched_write_clears_last_error_and_binds_expected_tool(
        wired):
    # Issue #521 ask 2: the row used to carry a stale gate-hold message
    # ("waiting for live trigger route") into its terminal dispatched state.
    _prompt()
    await _decide()
    ep = _released()[0]
    pse._update_episode(ep["id"], last_error="waiting for live trigger route")
    await _drain_pending(wired)
    row = pse.episodes()[0]
    assert row["status"] == "dispatched"
    assert row["last_error"] == ""
    # Resident execution target: the dispatched session itself must carry
    # this exact namespaced tool — recorded for the outcome correlation.
    assert row["expected_tool"] == _NS


@pytest.mark.asyncio
async def test_specialist_dispatch_binds_no_expected_tool(wired):
    # The assistant is only a delegation COURIER for a specialist target;
    # its own session never carries the tool, so no availability claim can
    # be made about the dispatched session (delivery-only semantics stand).
    wired["entry"]["targets"] = ["specialist:finance"]
    await _dispatched(wired)
    assert pse.episodes()[0]["expected_tool"] == ""


@pytest.mark.asyncio
async def test_report_tool_ran_keeps_episode_consumed(wired):
    ep = await _dispatched(wired)
    pse.report_dispatch_outcome(
        ep["id"], tools_used_ok={_NS}, tools_attempted={_NS},
        available_tools={_NS})
    assert pse.episodes()[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_report_tool_available_unattempted_keeps_episode_consumed(
        wired):
    # The invariant is "consumed ⇒ the tool was available to the turn" —
    # an available tool the agent chose not to call is the agent's own
    # reply's business, not a dispatch failure.
    ep = await _dispatched(wired)
    pse.report_dispatch_outcome(
        ep["id"], tools_used_ok=set(), tools_attempted=set(),
        available_tools={_NS, "Read"})
    assert pse.episodes()[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_report_tool_absent_marks_retryable(wired):
    # The observed live failure (#521): cold-connect session after agent
    # reconstruction, plugin MCP server absent — the turn ends with zero
    # tool uses and an init list without the tool.
    ep = await _dispatched(wired)
    pse.report_dispatch_outcome(
        ep["id"], tools_used_ok=set(), tools_attempted=set(),
        available_tools={"Read"})
    row = pse.episodes()[0]
    assert row["status"] == "pending"
    assert row["gate"] == "released"        # verdict already earned — kept
    assert row["execution_retries"] == 1
    assert row["attempts"] == 0             # bus-retry budget restored
    assert "setup tool" in row["last_error"]


@pytest.mark.asyncio
async def test_report_all_errors_overrides_availability(wired):
    # Sol design r1: a tool the init LISTS can still be categorically
    # uncallable in the turn (denied / erroring server). An attempted call
    # whose every observed result is an error outranks the availability
    # listing — the turn did not run the tool.
    ep = await _dispatched(wired)
    pse.report_dispatch_outcome(
        ep["id"], tools_used_ok=set(), tools_attempted={_NS},
        available_tools={_NS})
    assert pse.episodes()[0]["status"] == "pending"
    assert pse.episodes()[0]["execution_retries"] == 1


@pytest.mark.asyncio
async def test_report_unknown_availability_unattempted_marks_retryable(wired):
    # Warm-reuse session: no init replay, no attempt — availability UNKNOWN.
    # Fail toward the invariant (bounded), never toward silent consumption.
    ep = await _dispatched(wired)
    pse.report_dispatch_outcome(
        ep["id"], tools_used_ok=set(), tools_attempted=set(),
        available_tools=None)
    assert pse.episodes()[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_report_specialist_row_is_noop(wired):
    wired["entry"]["targets"] = ["specialist:finance"]
    ep = await _dispatched(wired)
    pse.report_dispatch_outcome(
        ep["id"], tools_used_ok=set(), tools_attempted=set(),
        available_tools=set())
    assert pse.episodes()[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_report_non_dispatched_row_is_noop(wired):
    # Sol diff r1 hardening: give the pending row an expected_tool so this
    # pins the STATUS guard specifically — without it, dropping the guard
    # would still no-op on the (dispatch-only) expected_tool being absent.
    _prompt()
    await _decide()
    ep = _released()[0]
    pse._update_episode(ep["id"], expected_tool=_NS)
    pse.report_dispatch_outcome(
        ep["id"], tools_used_ok=set(), tools_attempted=set(),
        available_tools=set())
    assert pse.episodes()[0]["status"] == "pending"
    assert "execution_retries" not in pse.episodes()[0]


@pytest.mark.asyncio
async def test_report_unknown_episode_is_noop(wired):
    await _dispatched(wired)
    pse.report_dispatch_outcome(
        "no-such-id", tools_used_ok=set(), tools_attempted=set(),
        available_tools=set())
    assert pse.episodes()[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_retryable_row_redispatches_and_keeps_counter(wired):
    ep = await _dispatched(wired)
    pse.report_dispatch_outcome(
        ep["id"], tools_used_ok=set(), tools_attempted=set(),
        available_tools=set())
    assert pse.episodes()[0]["execution_retries"] == 1
    await _drain_pending(wired)             # the next kick re-dispatches
    row = pse.episodes()[0]
    assert row["status"] == "dispatched"
    assert row["id"] == ep["id"]            # same obligation, not a re-arm
    assert row["execution_retries"] == 1    # dispatch write must not reset it
    assert len(wired["dispatches"]) == 2


@pytest.mark.asyncio
async def test_execution_retries_exhaust_to_failed_with_note(wired):
    import asyncio
    ep = await _dispatched(wired)
    for i in range(1, 3):
        pse.report_dispatch_outcome(
            ep["id"], tools_used_ok=set(), tools_attempted=set(),
            available_tools=set())
        assert pse.episodes()[0]["status"] == "pending"
        assert pse.episodes()[0]["execution_retries"] == i
        await _drain_pending(wired)
        assert pse.episodes()[0]["status"] == "dispatched"
    pse.report_dispatch_outcome(
        ep["id"], tools_used_ok=set(), tools_attempted=set(),
        available_tools=set())
    row = pse.episodes()[0]
    assert row["status"] == "failed"
    assert row["execution_retries"] == 3
    await asyncio.sleep(0)                  # note is scheduled, not awaited
    assert any("manually" in n for n in wired["notes"])
