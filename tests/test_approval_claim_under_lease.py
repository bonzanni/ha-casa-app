"""An approval starts its run only while it is approved and current, and is
used by exactly one attempt: the governed call's, under the plugin-tools lock,
right before the library call, whatever that call's outcome.

The configurator lease, the job registry, the lane, the limiter, the approval
store, ``_run_bundle_transaction`` and the plugin-tools guard are real; the
library calls, the staged-bytes check and the post-commit sequencer are
doubles that record what reached them."""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

import approval_records
import configurator_continue as cc
import configurator_run
import tools as tools_mod
from specialist_install_consent import identity_for_inspection
from test_configurator_continue import (  # noqa: F401 — the shared fixture
    CONTINUATION, OPERATOR, _settled, _until, env,
)
from test_delegate_to_agent import _with_origin

pytestmark = pytest.mark.asyncio

SLUG = "weather"
VALUES = {"config": {"A": "1"}, "secrets": []}
ARGS = {"component_id": "weather", "version": "1.0.0", "root_digest": "sha256:" + "d" * 64,
        "slug": SLUG, "staged_dir": "/staged/weather", "receipt_id": "r" * 32,
        "config": {"A": "1"}, "secret_names_provided": []}


@pytest.fixture
def lib(monkeypatch, tmp_path):
    """The library behind the governed tools, as doubles: each call is
    recorded; ``verify_fails`` makes the post-commit check fail (the tool
    then compensates); ``pause`` holds the library call in its worker
    thread."""
    import specialist_bundle_journal
    import specialist_install
    import specialist_install_consent
    import specialist_receipt
    state = SimpleNamespace(calls=[], verify_fails=False, pause=None, paused=threading.Event(),
                            raises=None)
    component = SimpleNamespace(
        component_id="weather", version="1.0.0", slug=SLUG, checksum="c" * 64,
        role=SimpleNamespace(role={"mission": "weather"}), default_persona_ref=None,
        default_persona_checksum=None)
    receipt = SimpleNamespace(receipt_id="r" * 32, receipt_digest="", plugins=(), slug=SLUG)
    checked = SimpleNamespace(ok=True, kind="", detail="", receipt=receipt, component=component,
                              dependencies=(), root_digest=ARGS["root_digest"])
    monkeypatch.setattr(specialist_install, "validate_resume_inputs", lambda **kw: checked)
    monkeypatch.setattr(specialist_receipt, "load", lambda rid, *a, **k: receipt)

    def _library(name):
        def _call(**kw):
            state.calls.append((name, kw.get("approved_values")))
            if state.pause is not None:
                state.paused.set()
                state.pause.wait(5)
            if state.raises is not None:
                raise state.raises
            return (SimpleNamespace(state="active", slug=SLUG, last_activation_error=None),
                    SimpleNamespace(slug=SLUG, removed_artifact_ids=[], journal_path="",
                                    removed_owned_names=()))
        return _call
    monkeypatch.setattr(specialist_install, "commit_specialist_install",
                        _library("commit_specialist_install"))
    monkeypatch.setattr(specialist_install, "upgrade_specialist", _library("upgrade_specialist"))

    async def _seq(slug, *, removed_artifact_ids, targets_removed):
        return {"ok": not state.verify_fails, "reloaded": [], "verify": {}}

    async def _seq_failure(txn, seq, *, slug):
        state.calls.append(("compensated", None))
        return {"ok": False, "kind": "bundle_sequence_failed", "rolled_back": True}

    async def _nothing(*a, **k):
        return None
    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq)
    monkeypatch.setattr(tools_mod, "_bundle_seq_failure", _seq_failure)
    monkeypatch.setattr(tools_mod, "_after_own_chat_change", _nothing)
    monkeypatch.setattr(tools_mod, "_bound_persona", lambda slug: None)
    monkeypatch.setattr(tools_mod, "_prune_bundle_receipt", lambda rid: None)
    monkeypatch.setattr(tools_mod, "_swap_removal_disclosure", lambda txn: {})
    monkeypatch.setattr(tools_mod, "_open_specialist_engagements", lambda slug: [])
    monkeypatch.setattr(specialist_install, "reclaim_staging_tree", lambda p: None)
    monkeypatch.setattr(specialist_bundle_journal, "complete", lambda p: None)
    monkeypatch.setattr(specialist_install_consent, "SpecialistInstallAckStore",
                        lambda *a, **k: SimpleNamespace(is_acked=lambda i: True))
    # the request tool's card: recorded, never posted
    monkeypatch.setattr(specialist_install, "preview_effective_config",
                        lambda kind, insp, *, slug, config, secret_names_provided, **k:
                        dict(config))
    cards: list = []

    def _card(**kw):
        cards.append(kw["record"])
        return SimpleNamespace(refused=None)
    monkeypatch.setattr(specialist_install_consent, "prompt_specialist_install_consent", _card,
                        raising=False)

    async def _posted(handle):
        return None
    monkeypatch.setattr(tools_mod, "_settle_install_consent_post", _posted)
    monkeypatch.setattr(specialist_install_consent, "supersede_card", lambda r, **k: None,
                        raising=False)
    monkeypatch.setattr(tools_mod, "_casa_card_target", lambda: (
        SimpleNamespace(bot_id=777), SimpleNamespace(bot_id=777, chat_id=OPERATOR), None))
    state.cards = cards
    state.inspection = tools_mod._inspection_from_checked(checked, ARGS["staged_dir"])
    return state


async def _approved(lib, kind="specialist_install", values=VALUES):
    identity = identity_for_inspection(lib.inspection, values=values)
    async with tools_mod._PLUGIN_TOOLS_LOCK:
        rec, _, _ = approval_records.request(kind, f"weather:{SLUG}", identity, values,
                                             asked="install weather")
    return approval_records.approve(rec.rid)


def _origin_with(record):
    return {"role": "assistant", "execution_role": "configurator", "channel": "telegram",
            "chat_id": OPERATOR, "cid": "c", "user_text": "install weather",
            approval_records.APPROVAL_KEY: {"rid": record.rid,
                                            "generation": record.generation}}


async def _call(tool, record, args=ARGS):
    out = await _with_origin(tool.handler(dict(args)), _origin_with(record))
    return json.loads(out["content"][0]["text"])


async def _request(record, args=None):
    """The request tool from the run that carries *record*."""
    args = args or {"slug": SLUG, "root_digest": ARGS["root_digest"],
                    "staged_dir": ARGS["staged_dir"], "receipt_id": ARGS["receipt_id"],
                    "mode": "install", "config": {"A": "2"}, "secret_names_provided": []}
    return await _call(tools_mod.specialist_install_request_approval, record, args)


# -- 1: the start check -----------------------------------------------------------------

async def test_a_run_whose_approval_was_replaced_while_it_waited_runs_nothing(env, lib):
    """The voice path's shape: another run holds the lease directly (no lane)."""
    record = await _approved(lib)
    held = await configurator_run.acquire(run_key="voice", via="delegation", task="t")
    cc.start(CONTINUATION, key=record.rid, approval=record)
    await asyncio.sleep(0.05)
    async with tools_mod._PLUGIN_TOOLS_LOCK:
        approval_records.request("specialist_install", record.component_key, "other",
                                 {"config": {}, "secrets": []})
    held.release()
    await _settled(env)
    assert env.runs.seen == []
    (msg,) = env.bus.sent
    assert msg.content.status == "error" and msg.content.kind == "approval_superseded"


# -- 2: one tap, one attempt -------------------------------------------------------------

@pytest.mark.parametrize("ending", ["succeeds", "verify_fails", "library_raises", "cancelled"])
async def test_the_attempt_uses_the_approval_whatever_follows(env, lib, ending):
    record = await _approved(lib)
    if ending == "verify_fails":
        lib.verify_fails = True
    if ending == "library_raises":
        lib.raises = OSError("disk")
    if ending == "cancelled":
        lib.pause = threading.Event()
        task = asyncio.create_task(_call(tools_mod.specialist_install_commit, record))
        await asyncio.to_thread(lib.paused.wait, 5)
        task.cancel()
        lib.pause.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif ending == "library_raises":
        with pytest.raises(OSError):
            await _call(tools_mod.specialist_install_commit, record)
    else:
        await _call(tools_mod.specialist_install_commit, record)
    assert [c for c in lib.calls if c[0] == "commit_specialist_install"] == [
        ("commit_specialist_install", VALUES)]
    assert approval_records.get(record.rid).state == "used"
    assert cc.start(CONTINUATION, key=record.rid, approval=record) == "superseded"
    payload = await _request(record, {**_request_args(), "config": {"A": "1"}})
    assert payload["approval"] == "requested" and len(lib.cards) == 1


def _request_args():
    return {"slug": SLUG, "root_digest": ARGS["root_digest"], "staged_dir": ARGS["staged_dir"],
            "receipt_id": ARGS["receipt_id"], "mode": "install", "config": {"A": "1"},
            "secret_names_provided": []}


# -- 3: no attempt, no use ---------------------------------------------------------------

async def test_a_run_that_makes_no_governed_call_leaves_the_approval_for_the_next(env, lib):
    record = await _approved(lib)
    cc.start(CONTINUATION, key=record.rid, approval=record)        # X1: the recorder only
    await _settled(env)
    assert len(env.runs.seen) == 1
    assert approval_records.get(record.rid).state == "approved"
    results: list = []
    real = env.runs.__call__

    async def _governed(cfg, task_text, context_text, **kw):
        out = await real(cfg, task_text, context_text, **kw)
        import agent as agent_mod
        payload = await tools_mod.specialist_install_commit.handler(dict(ARGS))
        results.append(json.loads(payload["content"][0]["text"]))
        assert agent_mod.origin_var.get()[approval_records.APPROVAL_KEY]["rid"] == record.rid
        return out
    tools_mod._run_delegated_agent = _governed
    try:
        assert cc.start(CONTINUATION, key=record.rid, approval=record) == "started"
        await _settled(env, 2)
    finally:
        tools_mod._run_delegated_agent = env.runs
    assert results and results[0]["ok"] is True
    assert approval_records.get(record.rid).state == "used"


# -- 4: two governed calls in one run ---------------------------------------------------

async def test_a_second_identical_call_after_a_compensated_upgrade_is_refused(env, lib):
    record = await _approved(lib, kind="specialist_upgrade")
    lib.verify_fails = True
    first = await _call(tools_mod.specialist_upgrade, record)
    assert first["ok"] is False and approval_records.get(record.rid).state == "used"
    second = await _call(tools_mod.specialist_upgrade, record)
    assert second["kind"] == "approval_used"
    assert [c[0] for c in lib.calls].count("upgrade_specialist") == 1


async def test_two_identical_calls_at_once_make_one_library_call(env, lib):
    record = await _approved(lib, kind="specialist_upgrade")
    lib.verify_fails = True
    out = await asyncio.gather(_call(tools_mod.specialist_upgrade, record),
                               _call(tools_mod.specialist_upgrade, record))
    assert sorted(p.get("kind") for p in out) == ["approval_used", "bundle_sequence_failed"]
    assert [c[0] for c in lib.calls].count("upgrade_specialist") == 1


# -- 5: a newer request in the same run, before the attempt -------------------------------

async def test_a_request_with_other_values_before_the_attempt_stops_the_old_approval(env, lib):
    record = await _approved(lib)
    assert (await _request(record))["approval"] == "requested"     # B, with A=2
    payload = await _call(tools_mod.specialist_install_commit, record)
    assert payload["kind"] == "approval_superseded"
    assert lib.calls == []


# -- 6: a request while the attempt's write is in flight ---------------------------------

async def test_a_request_waits_for_the_write_its_run_already_started(env, lib):
    record = await _approved(lib)
    lib.pause = threading.Event()
    commit = asyncio.create_task(_call(tools_mod.specialist_install_commit, record))
    await asyncio.to_thread(lib.paused.wait, 5)
    request = asyncio.create_task(_request(record))
    await asyncio.sleep(0.1)
    assert not request.done()
    assert approval_records.current(record.component_key).rid == record.rid  # no B yet
    lib.pause.set()
    assert (await commit)["ok"] is True
    assert (await request)["approval"] == "requested"
    assert approval_records.current(record.component_key).rid != record.rid


# -- 7: an approval is for one action ------------------------------------------------------

async def test_an_install_approval_does_not_authorise_an_upgrade(env, lib, monkeypatch):
    record = await _approved(lib, kind="specialist_install")
    payload = await _call(tools_mod.specialist_upgrade, record)
    assert payload["kind"] == "approval_required"
    assert payload["detail"] == approval_records.OTHER_KIND_DETAIL
    assert lib.calls == []
    # and at the attempt itself, should the entry check have passed
    monkeypatch.setattr(approval_records, "fence", lambda *a, **k: None)
    payload = await _call(tools_mod.specialist_upgrade, record)
    assert payload["kind"] == "approval_required" and lib.calls == []
    assert approval_records.get(record.rid).state == "approved"


# -- 8: busy before the attempt -----------------------------------------------------------

async def test_a_run_busy_past_the_deadline_keeps_the_approval(env, lib, monkeypatch):
    monkeypatch.setattr(configurator_run, "LEASE_WAIT_S", 0.1)
    record = await _approved(lib)
    held = await configurator_run.acquire(run_key="voice", via="delegation", task="audit")
    cc.start(CONTINUATION, key=record.rid, approval=record)
    await _settled(env)
    (msg,) = env.bus.sent
    assert msg.content.kind == "busy"
    assert approval_records.get(record.rid).state == "approved"
    held.release()
    results: list = []
    real = env.runs.__call__

    async def _governed(cfg, task_text, context_text, **kw):
        await real(cfg, task_text, context_text, **kw)
        payload = await tools_mod.specialist_install_commit.handler(dict(ARGS))
        results.append(json.loads(payload["content"][0]["text"]))
        return tools_mod.DelegatedOutput(text="done")
    tools_mod._run_delegated_agent = _governed
    try:
        cc.start(CONTINUATION, key=record.rid, approval=record)
        await _settled(env, 2)
    finally:
        tools_mod._run_delegated_agent = env.runs
    assert results[0]["ok"] is True and approval_records.get(record.rid).state == "used"
