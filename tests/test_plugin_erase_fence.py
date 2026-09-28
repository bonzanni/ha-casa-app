"""#1070: while a plugin's erasure runs for an uninstall, its tools are refused
on every turn but the episode's own eraser call.

The fence rises when the gate dispatches the erase episode and lifts when the
episode ends not complete, when the question it answers is closed or replaced
(Keep, Cancel, asked again), when an ``erase_data=true`` call can no longer
finish a complete erasure (the plugin changed under it), or once the finishing
uninstall settled. A complete erasure keeps it up until that finishing call."""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

import plugin_erase_consent as pec
import plugin_erasure as pe
import result_broker as rb
from test_plugin_erase_flow import ART, _remove, _still_registered, _uninstall
from test_plugin_erase_flow import flow, sflow  # noqa: F401 — fixtures
from test_result_broker import (
    ARTIFACT, DM, LIST, SETUP, _Origin, _deny_reason, _map, _pre, _store,
)

RUN = "run-1"
QID = "question-1"
SUBJECT = "plugin:probe"
ERASE_TURN = {**DM, "synthetic": "plugin_erase", "plugin_erase_target": "finance",
              "plugin_erase_artifact": ARTIFACT, "plugin_erase_episode": RUN,
              "plugin_erase_subject": SUBJECT, "plugin_erase_question": QID}


def _named_map():
    m = _map()
    plugins = {seg: dataclasses.replace(p, name=seg) for seg, p in m.plugins.items()}
    return dataclasses.replace(m, plugins=plugins)


@pytest.fixture
def fence(monkeypatch):
    f = pe.EraseFence()
    monkeypatch.setattr(pe, "FENCE", f)
    return f


# --- the fence itself ----------------------------------------------------------------

def test_the_fence_follows_its_question(fence, monkeypatch):
    qs = pe.QuestionIds()
    monkeypatch.setattr(pe, "QUESTIONS", qs)
    q = qs.open(SUBJECT)
    fence.raise_(["probe"], SUBJECT, q)
    assert fence.fenced("probe") and not fence.fenced("other")
    qs.open(SUBJECT)                                   # asked again
    assert not fence.fenced("probe")
    q = qs.open(SUBJECT)
    fence.raise_(["probe"], SUBJECT, q)
    qs.close(SUBJECT, q)                               # Keep / Cancel
    assert not fence.fenced("probe")


def test_a_held_fence_outlives_the_close_and_settles_by_its_own_token(
        fence, monkeypatch):
    qs = pe.QuestionIds()
    monkeypatch.setattr(pe, "QUESTIONS", qs)
    registered = {"probe"}
    monkeypatch.setattr(pe, "_registered", lambda n: n in registered)
    q = qs.open(SUBJECT)
    fence.raise_(["probe"], SUBJECT, q)
    fence.completed(["probe"], q)
    token = fence.hold(["probe"], SUBJECT, q)
    qs.close(SUBJECT, q)
    assert fence.fenced("probe")
    fence.settle("another-call")                       # not its token
    assert fence.fenced("probe")
    fence.settle(token)
    assert not fence.fenced("probe")                   # the removal did not commit
    token = fence.hold(["probe"], SUBJECT, q)
    fence.settle(token)
    registered.clear()                                 # it did commit
    assert fence.fenced("probe")
    registered.add("probe")                            # and was reinstalled
    assert not fence.fenced("probe")


def test_a_late_lift_of_an_older_question_leaves_a_newer_fence(fence, monkeypatch):
    qs = pe.QuestionIds()
    monkeypatch.setattr(pe, "QUESTIONS", qs)
    old = qs.open(SUBJECT)
    fence.raise_(["probe"], SUBJECT, old)
    new = qs.open(SUBJECT)
    fence.raise_(["probe"], SUBJECT, new)
    fence.lift(["probe"], old)
    assert fence.fenced("probe")


def test_lift_completed_spares_a_running_episode(fence, monkeypatch):
    qs = pe.QuestionIds()
    monkeypatch.setattr(pe, "QUESTIONS", qs)
    q = qs.open(SUBJECT)
    fence.raise_(["probe"], SUBJECT, q)
    fence.lift_completed(SUBJECT)
    assert fence.fenced("probe")                       # still running
    fence.completed(["probe"], q)
    fence.lift_completed(SUBJECT)
    assert not fence.fenced("probe")


# --- the admission hook --------------------------------------------------------------

@pytest.fixture
def fenced_probe(fence, monkeypatch):
    qs = pe.QuestionIds()
    qs._current[SUBJECT] = QID
    monkeypatch.setattr(pe, "QUESTIONS", qs)
    w = pe.EraseWatch()
    monkeypatch.setattr(pe, "WATCH", w)
    fence.raise_(["probe"], SUBJECT, QID)
    return w


@pytest.mark.asyncio
async def test_a_fenced_plugins_tools_are_refused_on_any_other_turn(fenced_probe):
    store, _ = _store()
    admit = rb.make_plugin_admission_hook("finance", _named_map(), client_id="c1",
                                          store=store)
    with _Origin(DM):
        for tool in (LIST, SETUP):                     # the setup tool too
            assert "being erased" in _deny_reason(await admit(_pre(tool), "t", {}))
    pe.QUESTIONS.close(SUBJECT)                        # the operator kept it
    with _Origin(DM):
        assert await admit(_pre(LIST), "t", {}) == {}


@pytest.mark.asyncio
async def test_the_episodes_own_eraser_call_is_still_admitted(fenced_probe):
    store, _ = _store()
    admit = rb.make_plugin_admission_hook("finance", _named_map(), client_id="c1",
                                          store=store)
    fenced_probe.arm(RUN, LIST)
    with _Origin(ERASE_TURN):
        assert await admit(_pre(LIST), "t", {}) == {}
    with _Origin(DM):
        assert "being erased" in _deny_reason(await admit(_pre(LIST), "t", {}))


# --- the uninstall flow --------------------------------------------------------------

def _tap(subject=SUBJECT, artifacts=(ART,), choice=pec.ERASE):
    q = pe.QUESTIONS.open(subject)
    pec.CHOICES.mint(pec.EraseChoiceKey(42, 42, subject, artifacts, q), choice)
    return q


def _episode(monkeypatch, verdict):
    """An erase episode that waits for ``release`` and then records *verdict*
    for every spec, as the real one does."""
    release = asyncio.Event()

    async def run(specs, operator, question, subject, kind="everything"):
        await release.wait()
        outs = []
        for s in specs:
            pe.RECORDS.put(f"plugin:{s.name}", s.artifact_id, verdict, "r",
                           question, kind)
            outs.append(pe.ErasureOutcome(s.name, s.artifact_id, verdict, "r"))
        return outs
    monkeypatch.setattr(pe, "run_erase_episode", run)
    return release


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_fence_rises_at_dispatch_and_holds_through_the_finishing_call(
        flow, fence, monkeypatch):
    tm = flow.tm
    release = _episode(monkeypatch, "complete")

    async def deliver(*a, **kw):
        return None
    monkeypatch.setattr(tm, "_deliver_erasure_outcome", deliver)
    _tap()
    assert (await _remove(tm, erase_data=True))["kind"] == "erasure_running"
    assert fence.fenced("probe")
    # a premature finishing call while the eraser runs does not lift it
    assert (await _remove(tm, erase_data=True))["kind"] == "erase_not_confirmed"
    assert fence.fenced("probe")
    release.set()
    await _settle()
    assert fence.fenced("probe")                       # complete: removal owed
    seen = []
    real_unit = tm._plugin_remove_unit

    async def unit(args):
        seen.append(fence.fenced("probe"))
        return await real_unit(args)
    monkeypatch.setattr(tm, "_plugin_remove_unit", unit)
    out = await _remove(tm, erase_data=True)
    assert out["ok"] is True and not _still_registered(flow)
    assert seen == [True]                              # held through the removal
    assert fence.fenced("probe")                       # removed: stays fenced
    from test_plugin_tools import _entry
    flow.st.raw["plugins"].append(_entry())            # reinstalled
    assert not fence.fenced("probe")


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["incomplete", "unreadable"])
async def test_an_erasure_that_did_not_complete_lifts_the_fence(
        flow, fence, monkeypatch, verdict):
    release = _episode(monkeypatch, verdict)
    _tap()
    await _remove(flow.tm, erase_data=True)
    assert fence.fenced("probe")
    release.set()
    await _settle()
    assert not fence.fenced("probe") and _still_registered(flow)


@pytest.mark.asyncio
async def test_an_update_after_a_complete_erasure_lifts_the_fence_on_the_refusal(
        flow, fence, monkeypatch):
    """Design r1 (Astra S2): the finishing call can no longer consume an
    erasure of another version; the plugin it keeps must not stay fenced."""
    release = _episode(monkeypatch, "complete")
    monkeypatch.setattr(flow.tm, "_deliver_erasure_outcome",
                        lambda *a, **kw: asyncio.sleep(0))
    _tap()
    await _remove(flow.tm, erase_data=True)
    release.set()
    await _settle()
    assert fence.fenced("probe")
    flow.st.raw["plugins"][0]["artifact_id"] = "9" * 64          # the update
    out = await _remove(flow.tm, erase_data=True)
    assert out["kind"] == "erase_not_confirmed" and _still_registered(flow)
    assert not fence.fenced("probe")


@pytest.mark.asyncio
async def test_keep_after_a_complete_erasure_lifts_the_fence(flow, fence, monkeypatch):
    release = _episode(monkeypatch, "complete")
    monkeypatch.setattr(flow.tm, "_deliver_erasure_outcome",
                        lambda *a, **kw: asyncio.sleep(0))
    _tap()
    await _remove(flow.tm, erase_data=True)
    release.set()
    await _settle()
    out = await _remove(flow.tm, erase_data=False)
    assert out["ok"] is True and not fence.fenced("probe")


@pytest.mark.asyncio
async def test_a_specialist_uninstall_fences_every_erasing_plugin_until_reinstalled(
        sflow, fence, monkeypatch):
    sflow.specs = [pe.EraseSpec(**{**s.__dict__}) for s in sflow.specs] + [
        dataclasses.replace(sflow.specs[0], name="fin.tags", artifact_id="2" * 64)]
    release = _episode(monkeypatch, "complete")
    monkeypatch.setattr(sflow.tm, "_deliver_erasure_outcome",
                        lambda *a, **kw: asyncio.sleep(0))
    _tap("specialist:fin", ("1" * 64, "2" * 64))
    assert (await _uninstall(sflow.tm, erase_data=True))["kind"] == "erasure_running"
    assert fence.fenced("fin.bank") and fence.fenced("fin.tags")
    release.set()
    await _settle()
    out = await _uninstall(sflow.tm, erase_data=True)
    assert out["ok"] is True and sflow.uninstalled == [True]
    assert fence.fenced("fin.bank") and fence.fenced("fin.tags")
    monkeypatch.setattr(pe, "_registered", lambda n: n == "fin.tags")
    assert fence.fenced("fin.bank") and not fence.fenced("fin.tags")


def test_the_contract_map_names_each_plugin_by_its_registry_name(tmp_path):
    """The hook keys the fence by the name the real resolution carries."""
    from plugin_fixtures import entry, mk_artifact, mk_registry
    from plugin_grants import result_contract_map
    from plugin_registry import reload_snapshot, resolve_all
    store = tmp_path / "store"
    e = entry("probe", ["resident:assistant"])
    mk_artifact(store, "probe", e["artifact_id"], mcp_servers={"api": {}})
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    m = result_contract_map(resolve_all())
    assert m.plugins[m.plugin_seg_of("mcp__plugin_probe_api__x")].name == "probe"



# --- design / diff round 1 findings ----------------------------------------------------

@pytest.mark.asyncio
async def test_a_cancelled_waiting_removal_leaves_anothers_fence(flow, fence, monkeypatch):
    """Diff r1 (Astra S1): a removal cancelled while it waits for the lock
    settles no fence it never held."""
    tm = flow.tm
    q = pe.QUESTIONS.open(SUBJECT)
    pe.RECORDS.put(SUBJECT, ART, "complete", "gone", q)
    gate = asyncio.Event()
    real_unit = tm._plugin_remove_unit

    async def unit(args):
        await gate.wait()                              # A paused before commit
        return await real_unit(args)
    monkeypatch.setattr(tm, "_plugin_remove_unit", unit)
    a = asyncio.get_running_loop().create_task(_remove(tm, erase_data=True))
    await _settle()
    assert fence.fenced("probe")
    b = asyncio.get_running_loop().create_task(_remove(tm, erase_data=False))
    await _settle()
    b.cancel()                                         # B waits for the lock
    with pytest.raises(asyncio.CancelledError):
        await b
    assert fence.fenced("probe")
    gate.set()
    assert (await a)["ok"] is True


@pytest.mark.asyncio
async def test_a_cancelled_finishing_removal_keeps_the_fence_while_it_is_removed(
        flow, fence, monkeypatch):
    """Diff r1 (Terra S1): cancelled during its reload, the removal has
    committed; the fence stays up for the sessions that still carry it."""
    tm = flow.tm
    q = pe.QUESTIONS.open(SUBJECT)
    pe.RECORDS.put(SUBJECT, ART, "complete", "gone", q)
    parked = asyncio.Event()

    async def reload(*a, **kw):
        parked.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(tm, "_reload_and_verify_targets", reload)
    task = asyncio.get_running_loop().create_task(_remove(tm, erase_data=True))
    await parked.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _still_registered(flow)
    assert fence.fenced("probe")


@pytest.mark.asyncio
async def test_a_specialist_fence_settles_with_its_transaction_not_its_handler(
        sflow, fence, monkeypatch):
    """Diff r1 (Astra S1, Terra S1): the handler abandoned, the transaction
    still running — the fence stays up until the transaction itself ends."""
    tm = sflow.tm
    q = pe.QUESTIONS.open("specialist:fin")
    pe.RECORDS.put("plugin:fin.bank", "1" * 64, "complete", "gone", q)
    release = asyncio.Event()

    async def seq(slug, **kw):
        await release.wait()
        return {"ok": True, "reloaded": [], "verify": {}}
    monkeypatch.setattr(tm, "_bundle_reload_and_verify", seq)
    child: list = []

    async def run_body(body):                          # a runner that abandons
        child.append(asyncio.get_running_loop().create_task(body()))
        await asyncio.Event().wait()
    monkeypatch.setattr(tm, "_run_bundle_transaction", run_body)
    # registered while the transaction runs: only a HELD fence refuses now
    monkeypatch.setattr(pe, "_registered", lambda n: True)
    handler = asyncio.get_running_loop().create_task(
        _uninstall(tm, erase_data=True))
    await _settle()
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    assert fence.fenced("fin.bank")                    # the transaction runs on
    monkeypatch.setattr(pe, "_registered", lambda n: False)   # it removed them
    release.set()
    await child[0]
    assert fence.fenced("fin.bank")                    # removed: stays fenced
    monkeypatch.setattr(pe, "_registered", lambda n: True)    # reinstalled
    assert not fence.fenced("fin.bank")


@pytest.mark.asyncio
async def test_an_ordinary_removal_closes_the_question_so_a_reinstall_is_unfenced(
        flow, fence, monkeypatch):
    """Diff r1 (Astra S2): an update dropped the eraser after a complete
    erasure; removing without erase_data answers the open question."""
    release = _episode(monkeypatch, "complete")
    monkeypatch.setattr(flow.tm, "_deliver_erasure_outcome",
                        lambda *a, **kw: asyncio.sleep(0))
    _tap()
    await _remove(flow.tm, erase_data=True)
    release.set()
    await _settle()
    assert fence.fenced("probe")
    flow.specs = []                                    # the eraser is gone
    out = await _remove(flow.tm)
    assert out["ok"] is True and not fence.fenced("probe")



@pytest.mark.asyncio
async def test_a_lifted_complete_erasure_cannot_remove_after_a_revert(
        flow, fence, monkeypatch):
    """Diff r2 (Astra S1, Terra S1): once a refusal lifted the fence, writes
    may resume, so that erasure must never finish the uninstall — not even
    after the plugin is reverted to the erased version."""
    release = _episode(monkeypatch, "complete")
    monkeypatch.setattr(flow.tm, "_deliver_erasure_outcome",
                        lambda *a, **kw: asyncio.sleep(0))
    _tap()
    await _remove(flow.tm, erase_data=True)
    release.set()
    await _settle()
    flow.st.raw["plugins"][0]["artifact_id"] = "9" * 64          # the update
    assert (await _remove(flow.tm, erase_data=True))["kind"] == "erase_not_confirmed"
    assert not fence.fenced("probe")
    flow.st.raw["plugins"][0]["artifact_id"] = ART               # the revert
    out = await _remove(flow.tm, erase_data=True)
    assert out["kind"] == "erase_not_confirmed" and _still_registered(flow)
