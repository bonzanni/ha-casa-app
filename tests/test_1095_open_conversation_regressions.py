"""#1095 — what the warning and the removal close must NOT change.

Regression tests in the change's diff (not red cases). The first, second and
fourth are green at the base too; the scope test also asserts that the close
ran, and the two lock tests pin the new close itself (labelled pins), so those
three need the change. Each names the mutant of the #1095 code it kills
(measured by hand; the run's mutation table lists the results):

- with no open conversations, every commit point behaves as before (mutant:
  always warn);
- an uninstall that does not commit closes nothing (mutant: the close moved
  before the sequencer);
- only the removed specialist's specialist-kind conversations are closed —
  never an executor's (the configurator's own), another specialist's, or a
  plugin job's (mutant: the kind filter dropped);
- the sequencer-ran kept arm keeps its "active" text (mutant: the not-active
  telling written into the shared envelope);
- the close runs inline under the locks its callers hold — the plugin-tools
  mutation lock at the uninstall, the reload read lock with a writer queued at
  the teardown — and completes without waiting on them (mutant: the close
  spawns a task that takes one of those locks and awaits it).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from test_pin_1095_removal_close import _open, funnel, reg  # noqa: F401 — fixtures
from test_pin_1095_warn_then_act import rollback  # noqa: F401 — fixture
from test_plugin_erase_flow import _spec, flow, sflow  # noqa: F401 — fixtures
from test_reload_disabled_specialist_scopes import ROLE, harness  # noqa: F401 — fixture

import tools as _tools_at_import

# The real transaction runner, captured before any fixture swaps it out.
_REAL_RUN = _tools_at_import._run_bundle_transaction


def _out(r) -> dict:
    return json.loads(r["content"][0]["text"])


async def test_no_open_conversations_means_no_warning(reg, sflow, funnel, rollback):
    import tools as tools_mod
    await _open(reg, slug="travel", topic=103)          # another specialist's
    out = _out(await tools_mod.specialist_rollback.handler({"slug": "fin"}))
    assert out.get("ok") is True and rollback.calls == 1, out
    sflow.specs = []
    out = _out(await tools_mod.specialist_uninstall.handler({"slug": "fin"}))
    assert out.get("ok") is True and sflow.uninstalled == [True], out
    assert "closed_conversations" not in out


async def test_an_uninstall_that_does_not_commit_closes_nothing(reg, sflow, funnel, monkeypatch):
    sflow.specs = []
    a = await _open(reg)

    async def failed_seq(slug, **kw):
        return {"ok": False, "kind": "reload_failed", "reloaded": [], "verify": {}}
    monkeypatch.setattr(sflow.tm, "_bundle_reload_and_verify", failed_seq)

    async def seq_failure(txn, seq, *, slug):
        return {"ok": False, "kind": seq["kind"], "rolled_back": True}
    monkeypatch.setattr(sflow.tm, "_bundle_seq_failure", seq_failure)
    out = _out(await sflow.tm.specialist_uninstall.handler(
        {"slug": "fin", "acknowledged_conversations": [a.id]}))
    assert out.get("ok") is False and out.get("rolled_back") is True, out
    assert reg.get(a.id).status == "active"
    assert funnel.tch.close_topic.await_count == 0


async def test_only_the_removed_specialists_conversations_close(reg, sflow, funnel):
    sflow.specs = []
    a = await _open(reg)
    configurator = await _open(reg, slug="fin", kind="executor", topic=105,
                               task="the configurator itself")
    other = await _open(reg, slug="travel", topic=103)
    plugin_job = await _open(reg, slug="fin", kind="plugin", topic=106)
    out = _out(await sflow.tm.specialist_uninstall.handler(
        {"slug": "fin", "acknowledged_conversations": [a.id]}))
    assert out.get("ok") is True, out
    assert reg.get(a.id).status == "cancelled"
    assert [reg.get(i).status for i in (configurator.id, other.id, plugin_job.id)] == [
        "active", "active", "active"]


async def test_the_sequencer_ran_kept_arm_keeps_its_active_text(monkeypatch):
    import tools as tools_mod

    async def kept(txn):
        return {"kept_new_version": True, "disk_ok": True}
    monkeypatch.setattr(tools_mod, "_bundle_compensate", kept)
    monkeypatch.setattr(tools_mod, "_swap_removal_disclosure", lambda txn: {})
    env = await tools_mod._bundle_seq_failure(
        SimpleNamespace(slug="fin"), {"ok": False, "kind": "postcondition_failed"}, slug="fin")
    assert env["kept_new_version"] is True
    assert "active and stays active" in env["outcome"]
    assert "not active yet" not in env["outcome"]


async def test_the_uninstall_close_runs_inside_the_mutation_lock_without_waiting_on_it(
        reg, sflow, funnel, monkeypatch):
    """The real bundle transaction: its child task holds `_PLUGIN_TOOLS_LOCK`
    while the close runs. Bounded so a close that waits on that lock fails
    here by timeout instead of hanging the suite."""
    import tools as tools_mod
    sflow.specs = []
    monkeypatch.setattr(tools_mod, "_run_bundle_transaction", _REAL_RUN)
    seen: list = []
    real_close = tools_mod.close_specialist_engagements

    async def close(slug, **kw):
        seen.append(tools_mod._PLUGIN_TOOLS_LOCK.locked())
        return await real_close(slug, **kw)
    monkeypatch.setattr(tools_mod, "close_specialist_engagements", close)
    a = await _open(reg)
    out = _out(await asyncio.wait_for(tools_mod.specialist_uninstall.handler(
        {"slug": "fin", "acknowledged_conversations": [a.id]}), 5))
    assert out.get("ok") is True, out
    assert seen == [True]
    assert reg.get(a.id).status == "cancelled"


async def test_the_teardown_close_completes_under_the_reader_with_a_writer_queued(
        reg, funnel, harness, monkeypatch):
    """The four teardown callers hold the reload read lock. The lock is FIFO:
    a reader requested now would queue behind the writer queued here, which
    waits for the held reader — so a close that awaited anything taking the
    reload lock would never finish. It must finish."""
    import reload as reload_mod
    monkeypatch.setattr(reload_mod, "_INCOMPLETE_RETIREMENTS", set())
    monkeypatch.setattr(reload_mod, "_GLOBAL_RW", reload_mod._RWLock())
    rw = reload_mod._global_rw()
    a = await _open(reg, slug=ROLE)
    await rw.acquire_read()
    writer = asyncio.get_running_loop().create_task(rw.acquire_write())
    await asyncio.sleep(0)
    try:
        actions: list = []
        await asyncio.wait_for(
            reload_mod._teardown_disabled_specialist(actions, harness.runtime, ROLE), 5)
        assert reg.get(a.id).status == "cancelled"
        assert not writer.done()
    finally:
        await rw.release_read()
        await asyncio.wait_for(writer, 5)
        await rw.release_write()



async def test_a_confirmed_uninstalls_continuations_carry_its_acknowledgement(
        reg, sflow, funnel, monkeypatch):
    """Diff review r1 (terra S2): the calls Casa's continuations tell the model
    to make — after a Keep tap, and after the eraser's result — name the
    acknowledged conversations, so following them verbatim commits instead of
    warning the operator a second time. Read back from the delivered text, not
    assumed."""
    import ast
    import re
    import plugin_erase_consent as pec
    import plugin_erasure as pe
    tm = sflow.tm
    delivered: list = []

    def deliverer(channel, eng):
        async def deliver(text):
            delivered.append(text)
            return True
        return deliver
    monkeypatch.setattr(tm, "_engagement_deliverer", deliverer)
    a = await _open(reg)
    out = _out(await tm.specialist_uninstall.handler(
        {"slug": "fin", "acknowledged_conversations": [a.id]}))
    assert out.get("kind") == "erase_choice_pending", out
    [prompt] = sflow.prompts
    assert await prompt["continue_cb"](pec.KEEP) is True
    [keep] = delivered
    call = re.search(r"specialist_uninstall\((.*?)\) now", keep).group(1)
    kwargs = {k.strip(): ast.literal_eval(v.strip().replace("false", "False")
                                          .replace("true", "True"))
              for k, v in (part.split("=", 1) for part in
                           re.split(r",\s*(?=\w+=)", call))}
    assert kwargs.get("acknowledged_conversations") == [a.id], keep
    out = _out(await tm.specialist_uninstall.handler(kwargs))
    assert out.get("ok") is True, out
    assert sflow.uninstalled == [True] and reg.get(a.id).status == "cancelled"
    delivered.clear()
    await tm._deliver_erasure_outcome(
        "specialist_uninstall", "fin",
        [pe.ErasureOutcome("fin.bank", "1" * 64, "complete", "gone")],
        deliverer(None, None), f", acknowledged_conversations={[a.id]!r}")
    assert f"acknowledged_conversations={[a.id]!r}" in delivered[-1]


async def test_an_acknowledgement_must_name_one_of_the_specialists_engagements(
        reg, rollback):
    """Diff review r2 (terra S2): a made-up id, or another specialist's, does
    not acknowledge the warning; an id the warning listed still does after
    that conversation closed, and a conversation opened since is named."""
    import tools as tools_mod
    a = await _open(reg)
    other = await _open(reg, slug="travel", topic=103)
    for bogus in (["not-an-engagement-id"], [other.id]):
        out = _out(await tools_mod.specialist_rollback.handler(
            {"slug": "fin", "acknowledged_conversations": bogus}))
        assert out.get("kind") == "open_conversations_unconfirmed", (bogus, out)
    assert rollback.calls == 0
    await reg.try_transition_terminal(a.id, "cancelled")
    b = await _open(reg, topic=102, task="Plan the holiday budget")
    out = _out(await tools_mod.specialist_rollback.handler(
        {"slug": "fin", "acknowledged_conversations": [a.id]}))
    assert out.get("ok") is True and rollback.calls == 1, out
    assert [row["engagement_id"] for row in out.get("opened_after_confirmation", [])] == [b.id]


async def test_boot_does_not_resume_a_job_whose_disabled_close_failed(reg, monkeypatch):
    """Diff review r2 (terra S2): the boot pass returns what it could not close,
    and the job resume that follows it skips exactly those."""
    import inspect
    import background_jobs
    import casa_core
    import reload as reload_mod
    import tools as tools_mod
    monkeypatch.setattr(reload_mod, "_INCOMPLETE_RETIREMENTS", set())
    stuck = await _open(reg, slug="travel", topic=103, job={"title": "Fares"})
    fine = await _open(reg, slug="fin", topic=104, job={"title": "Budget"})

    async def failing(engagement, **kw):
        return tools_mod.FinalizeResult.PERSIST_FAILED
    monkeypatch.setattr(tools_mod, "_finalize_engagement", failing)

    async def notify(cm, text):
        return None
    monkeypatch.setattr(casa_core, "operator_notify", notify)

    class _Specs:
        def is_disabled(self, role):
            return role == "travel"
    left = await casa_core._close_disabled_specialist_engagements(reg, _Specs())
    assert left == {stuck.id}
    assert "travel" in reload_mod._INCOMPLETE_RETIREMENTS
    resumed: list = []

    async def start(rec, channel):
        resumed.append(rec.id)
    monkeypatch.setattr(background_jobs, "start_next_batch", start)

    async def notice(channel, rec, text):
        return None
    monkeypatch.setattr(tools_mod, "_post_engagement_notice", notice)
    await casa_core._resume_background_jobs(reg, object(), skip=left)
    assert resumed == [fine.id]
    src = inspect.getsource(casa_core.main)
    assert src.count("await _resume_background_jobs(engagement_registry, telegram_channel, "
                     "skip=left_open)") == 1
