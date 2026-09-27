"""#1046: the uninstall question and the tool flow.

Uninstalling a plugin (or a specialist bundling one) that declares an eraser
asks ONE question, posted by Casa: Keep data / Erase data / Cancel. The tap is
the authorization: ``erase_data=true`` runs only on a recorded Erase choice.
The erase then runs in the background; the plugin is removed only by a
finishing call that finds a complete erasure for the artifact the registry
still resolves. A plugin without an eraser is removed exactly as before."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import plugin_erase_consent as pec
import plugin_erasure as pe

ART = "e" * 64                           # test_plugin_tools._entry's artifact
TOOL = "mcp__plugin_probe_srv__erase_all"


# --- the consent module ------------------------------------------------------------

class _Coordinator:
    def register_challenge(self, key, **kw):
        self.key, self.kw = key, kw
        return SimpleNamespace(created=True, refused=None)


class _EditChannel:
    def __init__(self):
        self.edits = []

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edits.append(text)


def _key(subject="plugin:probe", artifacts=(ART,)):
    return pec.EraseChoiceKey(operator_id=42, chat_id=42, subject=subject,
                              artifacts=artifacts)


async def _tap(idx, *, continued=True, answered=True):
    coord, ch, grants, calls = _Coordinator(), _EditChannel(), pec.ChoiceGrants(), []

    async def cont(choice):
        calls.append(choice)
        return continued
    pec.prompt_erase_choice(coordinator=coord, channel=ch, key=_key(),
                            text="q", continue_cb=cont, grants=grants)
    assert coord.kw["options"] == ["Keep data", "Erase data", "Cancel"]
    meta: dict = {}
    if answered:
        coord.kw["on_commit_sync"](idx, meta)
    req = SimpleNamespace(meta=meta)
    finish = coord.kw["finish_factory"](7, req)
    await finish({"outcome": "answered", "option_index": idx} if answered
                 else {"outcome": "no_answer"})
    return grants, calls, ch.edits


@pytest.mark.asyncio
async def test_an_erase_tap_mints_a_single_use_erase_choice_and_continues():
    grants, calls, edits = await _tap(pec.ERASE)
    assert calls == [pec.ERASE]
    assert grants.consume(_key(), pec.ERASE) is True
    assert grants.consume(_key(), pec.ERASE) is False           # single use
    assert "Erase" in edits[-1]


@pytest.mark.asyncio
async def test_a_keep_tap_mints_no_erase_choice():
    grants, calls, _ = await _tap(pec.KEEP)
    assert calls == [pec.KEEP]
    assert grants.consume(_key(), pec.ERASE) is False


@pytest.mark.asyncio
async def test_cancel_and_no_answer_mint_nothing():
    grants, calls, edits = await _tap(pec.CANCEL)
    assert calls == [pec.CANCEL] and grants.consume(_key(), pec.ERASE) is False
    grants, calls, edits = await _tap(pec.ERASE, answered=False)
    assert calls == [] and grants.consume(_key(), pec.ERASE) is False
    assert "nothing was removed" in edits[-1]


@pytest.mark.asyncio
async def test_a_continuation_that_did_not_start_is_said_so():
    _g, _c, edits = await _tap(pec.ERASE, continued=False)
    assert "not resumed" in edits[-1]


def test_an_erase_choice_expires_and_is_bound_to_its_subject_and_artifacts(monkeypatch):
    now = [1000.0]
    g = pec.ChoiceGrants(clock=lambda: now[0])
    g.mint(_key(), pec.ERASE)
    assert g.consume(_key(artifacts=("f" * 64,)), pec.ERASE) is False
    assert g.consume(_key(subject="plugin:other"), pec.ERASE) is False
    now[0] += pec.CHOICE_TTL_S + 1
    assert g.consume(_key(), pec.ERASE) is False


def test_the_question_names_the_eraser_and_the_backups():
    text = pec.render_erase_choice("the plugin probe",
                                   [("probe", "erase_all", "Erases everything.")])
    assert "erase_all" in text and "Erases everything." in text
    assert "Home Assistant backups" in text


# --- erase spec discovery ------------------------------------------------------------

def test_erase_specs_come_from_the_resolved_manifest(tmp_path, monkeypatch):
    import plugin_registry as preg
    import tools as tools_mod
    art = tmp_path / "art"
    (art / ".claude-plugin").mkdir(parents=True)
    (art / ".mcp.json").write_text(json.dumps({"mcpServers": {"srv": {"command": "x"}}}))
    manifest = {"name": "probe", "casa": {
        "eraseTool": "erase_all",
        "protectedTools": [{"name": "erase_all", "summary": "Erases everything."}],
        "resultContract": {"version": 1, "tools": {"erase_all": {"result": "safe"}}}}}
    rp = preg.ResolvedPlugin(name="probe", artifact_id=ART, path=str(art),
                             version="1", manifest=manifest)
    plain = preg.ResolvedPlugin(name="plain", artifact_id="f" * 64, path=str(art),
                                version="1", manifest={"name": "plain"})
    monkeypatch.setattr(preg, "resolve_all", lambda: preg.ResolutionResult(
        registry_valid=True, plugins=[rp, plain]))
    entries = [{"name": "probe", "artifact_id": ART, "targets": ["resident:assistant"]},
               {"name": "plain", "artifact_id": "f" * 64, "targets": ["resident:assistant"]}]
    [spec] = tools_mod._erase_specs_for(entries)
    assert spec == pe.EraseSpec(name="probe", artifact_id=ART,
                                targets=("resident:assistant",),
                                tool_names=(TOOL,), protected=True,
                                tool="erase_all", summary="Erases everything.")


# --- the tool flow -------------------------------------------------------------------

def _spec(name="probe", artifact=ART):
    return pe.EraseSpec(name=name, artifact_id=artifact, targets=("resident:assistant",),
                        tool_names=(TOOL,), protected=False, tool="erase_all",
                        summary=None)


@pytest.fixture
def flow(monkeypatch, tmp_path):
    from test_plugin_tools import _State, _entry, _pr, _wire
    import tools as tools_mod
    st = _State()
    st.raw["plugins"].append(_entry())
    tm = _wire(monkeypatch, tmp_path, st, publish=_pr())
    state = SimpleNamespace(st=st, tm=tm, prompts=[], episodes=[],
                            specs=[_spec()], delivered=[])
    # A module asyncio.Lock binds to the first loop that contends it: give
    # every test its own.
    monkeypatch.setattr(tm, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    def specs_for(entries):
        # the spec of an entry names the artifact the registry holds for it NOW
        out = []
        for s in state.specs:
            e = next((e for e in entries if e.get("name") == s.name), None)
            if e is not None:
                out.append(pe.EraseSpec(**{**s.__dict__,
                                           "artifact_id": e.get("artifact_id")}))
        return out
    monkeypatch.setattr(tm, "_erase_specs_for", specs_for)
    monkeypatch.setattr(pec, "CHOICES", pec.ChoiceGrants())
    monkeypatch.setattr(pe, "RECORDS", pe.ErasureRecords())
    monkeypatch.setattr(pe, "QUESTIONS", pe.QuestionIds())

    class _Ch:
        chat_id = "42"
    monkeypatch.setattr(tm, "_channel_manager",
                        SimpleNamespace(get=lambda n: _Ch() if n == "telegram" else None),
                        raising=False)

    def fake_prompt(**kw):
        state.prompts.append(kw)
        return SimpleNamespace(created=True, refused=None)
    monkeypatch.setattr(pec, "prompt_erase_choice", fake_prompt)

    async def settled(handle):
        return None
    monkeypatch.setattr(tm, "_settle_install_consent_post", settled)

    async def fake_episode(specs, operator, question, subject):
        state.episodes.append((list(specs), operator, question))
        return []
    monkeypatch.setattr(pe, "run_erase_episode", fake_episode)
    return state


async def _remove(tm, **args):
    r = await tm.plugin_remove.handler({"name": "probe", **args})
    return json.loads(r["content"][0]["text"])


def _still_registered(state):
    return any(e["name"] == "probe" for e in state.st.raw["plugins"])


@pytest.mark.asyncio
async def test_a_plugin_without_an_eraser_is_removed_as_before(flow):
    flow.specs = []
    out = await _remove(flow.tm)
    assert out["ok"] is True and not _still_registered(flow)
    assert flow.prompts == [] and "erasure" not in out
    assert out["plugin_data_may_remain"] is True                 # #676 unchanged


@pytest.mark.asyncio
async def test_an_eraser_asks_first_and_removes_nothing(flow):
    out = await _remove(flow.tm)
    assert out["ok"] is False and out["kind"] == "erase_choice_pending"
    assert _still_registered(flow)
    [prompt] = flow.prompts
    assert prompt["key"] == pec.EraseChoiceKey(
        42, 42, "plugin:probe", (ART,), pe.QUESTIONS.current("plugin:probe"))
    assert "erase_all" in prompt["text"]


@pytest.mark.asyncio
async def test_remove_with_eraser_no_channel_refuses(flow, monkeypatch):
    monkeypatch.setattr(flow.tm, "_channel_manager", None, raising=False)
    out = await _remove(flow.tm)
    assert out["kind"] == "consent_channel_unavailable" and _still_registered(flow)


@pytest.mark.asyncio
async def test_keep_removes_as_before(flow):
    out = await _remove(flow.tm, erase_data=False)
    assert out["ok"] is True and not _still_registered(flow)
    assert out["plugin_data_may_remain"] is True and "erasure" not in out


@pytest.mark.asyncio
async def test_erase_true_rejects_expired_and_reused_grant(flow):
    out = await _remove(flow.tm, erase_data=True)
    assert out["kind"] == "erase_not_confirmed" and _still_registered(flow)
    assert flow.episodes == []
    q = pe.QUESTIONS.open("plugin:probe")
    key = pec.EraseChoiceKey(42, 42, "plugin:probe", (ART,), q)
    pec.CHOICES.mint(key, pec.ERASE)
    out = await _remove(flow.tm, erase_data=True)
    assert out["kind"] == "erasure_running" and _still_registered(flow)
    await asyncio.sleep(0)
    assert [s.name for s in flow.episodes[0][0]] == ["probe"]
    out = await _remove(flow.tm, erase_data=True)                # grant consumed
    assert out["kind"] == "erase_not_confirmed" and len(flow.episodes) == 1


@pytest.mark.asyncio
async def test_a_complete_erasure_lets_the_finishing_call_remove(flow):
    pe.RECORDS.put("plugin:probe", ART, "complete", "Everything erased.",
                   pe.QUESTIONS.open("plugin:probe"))
    out = await _remove(flow.tm, erase_data=True)
    assert out["ok"] is True and not _still_registered(flow)
    assert out["erasure"] == "complete"
    assert out["erase_report"] == "Everything erased."
    assert "Home Assistant backups" in out["plugin_data_note"]
    assert "plugin_data_may_remain" not in out


@pytest.mark.asyncio
async def test_record_voided_by_artifact_change(flow):
    """A complete erasure of another version does not remove this one."""
    pe.RECORDS.put("plugin:probe", "0" * 64, "complete", "old version erased",
                   pe.QUESTIONS.open("plugin:probe"))
    out = await _remove(flow.tm, erase_data=True)
    assert out["kind"] == "erase_not_confirmed" and _still_registered(flow)


@pytest.mark.asyncio
async def test_an_incomplete_erasure_never_removes(flow):
    pe.RECORDS.put("plugin:probe", ART, "incomplete", "A bank kept its consent.",
                   pe.QUESTIONS.open("plugin:probe"))
    out = await _remove(flow.tm, erase_data=True)
    assert out["kind"] == "erase_not_confirmed" and _still_registered(flow)


@pytest.mark.asyncio
async def test_the_episode_outcome_is_delivered_to_the_configurator(flow, monkeypatch):
    """The background episode's outcome continues the configurator engagement:
    complete → finish with erase_data=true; otherwise relay verbatim, nothing
    removed."""
    tm = flow.tm
    sent = []

    async def deliver(text):
        sent.append(text)
        return True
    for outs, needle in (
        ([pe.ErasureOutcome("probe", ART, "complete", "gone")], "erase_data=true"),
        ([pe.ErasureOutcome("probe", ART, "incomplete", "BANK KEPT IT")], "BANK KEPT IT"),
    ):
        await tm._deliver_erasure_outcome("plugin_remove", "probe", outs, deliver)
        assert needle in sent[-1]
    assert "Nothing was removed" in sent[-1]


# --- specialist uninstall ------------------------------------------------------------

@pytest.fixture
def sflow(flow, monkeypatch):
    import plugin_registry as preg
    owned = [{"name": "fin.bank", "artifact_id": "1" * 64, "targets": ["specialist:fin"],
              "owner": "specialist:fin"},
             {"name": "fin.tags", "artifact_id": "2" * 64, "targets": ["specialist:fin"],
              "owner": "specialist:fin"}]
    monkeypatch.setattr(flow.tm, "_owned_entries_now", lambda slug: owned)
    flow.specs = [_spec("fin.bank", "1" * 64)]
    flow.uninstalled = []

    # The REAL transaction body runs (it consumes the erasure records), with
    # the uninstall's disk and reload work stubbed.
    import specialist_install
    import specialist_bundle_journal

    def fake_uninstall(**kw):
        flow.uninstalled.append(True)
        return SimpleNamespace(slug="fin", removed_artifact_ids=[], journal_path=None)

    async def fake_seq(slug, **kw):
        return {"ok": True, "reloaded": [], "verify": {}}

    async def run_body(body):
        return await body()
    monkeypatch.setattr(specialist_install, "uninstall_specialist", fake_uninstall)
    monkeypatch.setattr(flow.tm, "_bundle_reload_and_verify", fake_seq)
    monkeypatch.setattr(specialist_bundle_journal, "complete", lambda p: None)
    monkeypatch.setattr(flow.tm, "_swap_removal_disclosure", lambda txn: {})
    monkeypatch.setattr(flow.tm, "_run_bundle_transaction", run_body)
    return flow


async def _uninstall(tm, **args):
    r = await tm.specialist_uninstall.handler({"slug": "fin", **args})
    return json.loads(r["content"][0]["text"])


@pytest.mark.asyncio
async def test_specialist_without_erasers_unchanged(sflow):
    sflow.specs = []
    out = await _uninstall(sflow.tm)
    assert out["ok"] is True and sflow.uninstalled == [True] and sflow.prompts == []


@pytest.mark.asyncio
async def test_specialist_asks_once_for_its_erasing_plugins(sflow):
    out = await _uninstall(sflow.tm)
    assert out["kind"] == "erase_choice_pending" and sflow.uninstalled == []
    [prompt] = sflow.prompts
    assert prompt["key"].subject == "specialist:fin"
    assert prompt["key"].artifacts == ("1" * 64,)


@pytest.mark.asyncio
async def test_specialist_uninstalls_only_when_every_erasure_completed(sflow):
    sflow.specs = [_spec("fin.bank", "1" * 64), _spec("fin.tags", "2" * 64)]
    q = pe.QUESTIONS.open("specialist:fin")
    pe.RECORDS.put("plugin:fin.bank", "1" * 64, "complete", "bank gone", q)
    out = await _uninstall(sflow.tm, erase_data=True)
    assert out["kind"] == "erase_not_confirmed" and sflow.uninstalled == []
    # the complete record was not spent by the refused call
    pe.RECORDS.put("plugin:fin.tags", "2" * 64, "complete", "tags gone", q)
    out = await _uninstall(sflow.tm, erase_data=True)
    assert out["ok"] is True and sflow.uninstalled == [True]
    assert [r["name"] for r in out["erase_reports"]] == ["fin.bank", "fin.tags"]


@pytest.mark.asyncio
async def test_an_update_while_the_finishing_call_waits_is_not_removed(flow):
    """Diff r1 (Terra S2 / Astra S2): the records are checked and consumed under
    the same mutation lock as the removal. A plugin_update that lands while the
    finishing call waits for the lock leaves an unerased version, which is not
    removed."""
    tm = flow.tm
    pe.RECORDS.put("plugin:probe", ART, "complete", "old version erased",
                   pe.QUESTIONS.open("plugin:probe"))
    await tm._PLUGIN_TOOLS_LOCK.acquire()
    try:
        task = asyncio.get_running_loop().create_task(_remove(tm, erase_data=True))
        await asyncio.sleep(0.01)
        flow.st.raw["plugins"][0]["artifact_id"] = "9" * 64      # the update
    finally:
        tm._PLUGIN_TOOLS_LOCK.release()
    out = await task
    assert out["ok"] is False and _still_registered(flow)
    assert out["kind"] == "erase_not_confirmed"


@pytest.mark.asyncio
async def test_specialist_erase_step_runs_inside_the_transaction(sflow, monkeypatch):
    """The bundle's erasing plugins are read, and its records consumed, inside
    the transaction body — which owns the mutation lock — so the decision and
    the uninstall see one registry state."""
    tm = sflow.tm
    pe.RECORDS.put("plugin:fin.bank", "1" * 64, "complete", "bank gone",
                   pe.QUESTIONS.open("specialist:fin"))
    in_txn = {"now": False, "reads": []}
    real_owned = tm._owned_entries_now

    def owned(slug):
        in_txn["reads"].append(in_txn["now"])
        return real_owned(slug)
    monkeypatch.setattr(tm, "_owned_entries_now", owned)
    real_run = tm._run_bundle_transaction

    async def run(body):
        in_txn["now"] = True
        try:
            return await real_run(body)
        finally:
            in_txn["now"] = False
    monkeypatch.setattr(tm, "_run_bundle_transaction", run)
    out = await _uninstall(tm, erase_data=True)
    assert out["ok"] is True and sflow.uninstalled == [True]
    assert in_txn["reads"] == [True]


@pytest.mark.asyncio
async def test_erase_true_on_a_plugin_without_an_eraser_removes_nothing(flow):
    """Diff r2 (Terra S2): an update that dropped the eraser after the Erase
    tap must not turn erase_data=true into a plain removal."""
    flow.specs = []
    out = await _remove(flow.tm, erase_data=True)
    assert out["ok"] is False and out["kind"] == "erase_unavailable"
    assert _still_registered(flow)


@pytest.mark.asyncio
async def test_an_eraser_added_while_the_removal_waits_is_asked_about(flow):
    """Diff r2 (Astra S2): the gate runs under the mutation lock, so an update
    that introduces an eraser while a plain removal waits for the lock gets the
    question instead of a removal."""
    tm = flow.tm
    real_specs = flow.specs
    flow.specs = []                                   # no eraser at call time
    await tm._PLUGIN_TOOLS_LOCK.acquire()
    try:
        task = asyncio.get_running_loop().create_task(_remove(tm))
        await asyncio.sleep(0.01)
        flow.specs = real_specs                       # the update adds one
    finally:
        tm._PLUGIN_TOOLS_LOCK.release()
    out = await task
    assert out["kind"] == "erase_choice_pending" and _still_registered(flow)


@pytest.mark.asyncio
async def test_a_fresh_question_discards_an_earlier_erasure_record(flow):
    """Diff r2 (Astra S2): a record from an earlier installation of the same
    artifact must not certify this one — asking again forgets it, so an Erase
    tap runs the eraser again."""
    pe.RECORDS.put("plugin:probe", ART, "complete", "an earlier installation",
                   pe.QUESTIONS.open("plugin:probe"))
    out = await _remove(flow.tm)
    assert out["kind"] == "erase_choice_pending"
    pec.CHOICES.mint(_current_key(), pec.ERASE)
    out = await _remove(flow.tm, erase_data=True)
    assert out["kind"] == "erasure_running" and _still_registered(flow)


@pytest.mark.asyncio
async def test_a_keep_removal_discards_the_erasure_records(flow):
    pe.RECORDS.put("plugin:probe", ART, "complete", "erased, then kept",
                   pe.QUESTIONS.open("plugin:probe"))
    out = await _remove(flow.tm, erase_data=False)
    assert out["ok"] is True
    assert pe.QUESTIONS.current("plugin:probe") is None      # its question closed


def test_a_declared_eraser_with_no_server_still_counts(tmp_path, monkeypatch):
    """Diff r3 (Terra S2): a valid casa.eraseTool on a plugin with no MCP server
    to run it is still an eraser — the uninstall asks, and an erasure cannot
    complete — never silently an ordinary removal."""
    import plugin_registry as preg
    import tools as tools_mod
    art = tmp_path / "art"
    (art / ".claude-plugin").mkdir(parents=True)          # no .mcp.json
    manifest = {"name": "probe", "casa": {
        "eraseTool": "erase_all",
        "resultContract": {"version": 1, "tools": {"erase_all": {"result": "safe"}}}}}
    rp = preg.ResolvedPlugin(name="probe", artifact_id=ART, path=str(art),
                             version="1", manifest=manifest)
    monkeypatch.setattr(preg, "resolve_all", lambda: preg.ResolutionResult(
        registry_valid=True, plugins=[rp]))
    [spec] = tools_mod._erase_specs_for(
        [{"name": "probe", "artifact_id": ART, "targets": ["resident:assistant"]}])
    assert spec.tool_names == () and spec.tool == "erase_all"


# --- diff r4: every tap, run and record belongs to one question ---------------------

def _current_key():
    return pec.EraseChoiceKey(42, 42, "plugin:probe", (ART,),
                              pe.QUESTIONS.current("plugin:probe"))


@pytest.mark.asyncio
async def test_an_erase_tap_on_an_earlier_question_is_void_after_a_new_one(flow):
    """Diff r4 (Astra S1): question A → Erase tap → question B → Cancel on B →
    a delayed erase_data=true runs nothing."""
    await _remove(flow.tm)                                   # question A
    pec.CHOICES.mint(_current_key(), pec.ERASE)              # Erase on A
    await _remove(flow.tm)                                   # question B
    b = flow.prompts[-1]
    assert await b["continue_cb"](pec.CANCEL) in (True, False)   # Cancel on B
    out = await _remove(flow.tm, erase_data=True)
    assert out["kind"] == "erase_not_confirmed" and flow.episodes == []
    assert _still_registered(flow)


@pytest.mark.asyncio
async def test_an_older_run_cannot_finish_a_newer_question(flow):
    """Diff r4 (Terra S1): run A starts; question B is asked; A then completes;
    erase_data=true without a tap on B removes nothing."""
    await _remove(flow.tm)                                   # question A
    qid_a = pe.QUESTIONS.current("plugin:probe")
    pec.CHOICES.mint(_current_key(), pec.ERASE)
    out = await _remove(flow.tm, erase_data=True)            # run A starts
    assert out["kind"] == "erasure_running"
    await asyncio.sleep(0)
    assert flow.episodes[0][2] == qid_a                      # the run carries A
    await _remove(flow.tm)                                   # question B
    pe.RECORDS.put("plugin:probe", ART, "complete", "A finished", qid_a)
    out = await _remove(flow.tm, erase_data=True)
    assert out["kind"] == "erase_not_confirmed" and _still_registered(flow)



@pytest.mark.asyncio
async def test_a_keep_tap_closes_the_question_at_once(flow):
    """Diff r6 (Terra S1): a Keep tap voids the question immediately, before the
    configurator's erase_data=false call arrives."""
    await _remove(flow.tm)
    assert pe.QUESTIONS.current("plugin:probe") is not None
    await flow.prompts[-1]["continue_cb"](pec.KEEP)
    assert pe.QUESTIONS.current("plugin:probe") is None
