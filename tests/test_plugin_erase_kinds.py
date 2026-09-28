"""#1067: two erasers, three uninstall outcomes.

A plugin may declare a data-only eraser (``casa.eraseDataOnlyTool``) beside
``casa.eraseTool``. The uninstall question offers "Erase data, keep sign-ins"
and "Erase everything" only when every erasing plugin declares that kind; the
tap records the kind, so ``erase_data=true`` runs the eraser the operator
chose and never one the model chose. After a removal that followed an Erase
everything, Casa clears the plugin's plugin-env.conf references — never a name
another installed plugin still uses — and reloads the plugin environment."""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

import plugin_erase_consent as pec
import plugin_erasure as pe

from test_plugin_erase_flow import (  # noqa: F401 — the shared fixtures
    ART, TOOL, _Coordinator, _EditChannel, _key, _remove, _still_registered,
    _uninstall, flow, sflow,
)

DATA_TOOL = "mcp__plugin_probe_srv__erase_ledger"


def _spec(name="probe", artifact=ART, *, everything=True, data_only=True):
    return pe.EraseSpec(
        name=name, artifact_id=artifact, targets=("resident:assistant",),
        tool_names=(TOOL,) if everything else (), protected=False,
        tool="erase_all" if everything else "", summary=None,
        data_tool="erase_ledger" if data_only else "",
        data_tool_names=(DATA_TOOL,) if data_only else ())


# --- the manifest field -------------------------------------------------------------

def _manifest(**casa):
    base = {"resultContract": {"version": 1, "tools": {
        "erase_all": {"result": "safe"}, "erase_ledger": {"result": "safe"},
        "risky": {"result": "capability"}}}}
    return {"name": "probe", "casa": {**base, **casa}}


def test_a_data_only_eraser_is_read_from_the_manifest():
    from plugin_store import manifest_erase_data_only_tool
    assert manifest_erase_data_only_tool(_manifest()) is None
    assert manifest_erase_data_only_tool(
        _manifest(eraseDataOnlyTool="erase_ledger")) == "erase_ledger"


@pytest.mark.parametrize("casa", [
    {"eraseDataOnlyTool": "Erase-Ledger"},                        # not a tool name
    {"eraseDataOnlyTool": None},                                   # explicit null
    {"eraseDataOnlyTool": "risky"},                                # not safe
    {"eraseDataOnlyTool": "undeclared"},                           # not in contract
    {"eraseDataOnlyTool": "erase_all", "eraseTool": "erase_all"},  # same as eraseTool
    {"eraseDataOnlyTool": "setup_x", "setupTool": "setup_x"},      # the setup tool
])
def test_a_malformed_data_only_eraser_refuses_and_fails_the_verdict(casa):
    from plugin_store import StoreError, manifest_erase_data_only_tool
    with pytest.raises(StoreError) as exc:
        manifest_erase_data_only_tool(_manifest(**casa))
    assert exc.value.reason_code == "erase_tool_invalid"


def test_the_install_path_and_the_stored_verdict_check_the_data_only_eraser(tmp_path):
    import os
    from pathlib import Path
    import plugin_store
    from plugin_store import METADATA_FILENAME, StoreError, content_checksum
    from test_plugin_erase_manifest import _publish
    with pytest.raises(StoreError) as exc:
        _publish(tmp_path / "a", {**_manifest(eraseDataOnlyTool="risky"), "name": "p"})
    assert exc.value.reason_code == "erase_tool_invalid"
    res = _publish(tmp_path / "b", {"name": "p", "version": "1.0.0"})
    art = Path(res.path)
    pj = art / ".claude-plugin" / "plugin.json"
    os.chmod(art, 0o755)
    os.chmod(art / ".claude-plugin", 0o755)
    os.chmod(pj, 0o644)
    pj.write_text(json.dumps({**_manifest(eraseDataOnlyTool="Bad"), "name": "p"}),
                  encoding="utf-8")
    meta_path = art / METADATA_FILENAME
    os.chmod(meta_path, 0o644)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["content_checksum"] = content_checksum(art)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    assert plugin_store.artifact_verdict(
        art, name="p", repo="o/r", revision="git:" + "a" * 40,
        subdir="", artifact_id=res.artifact_id) == "erase_tool_invalid"


# --- which options the question offers ---------------------------------------------

@pytest.mark.parametrize("specs, offered", [
    ([_spec()], (pec.KEEP, pec.ERASE_DATA_ONLY, pec.ERASE, pec.CANCEL)),
    ([_spec(data_only=False)], (pec.KEEP, pec.ERASE, pec.CANCEL)),
    ([_spec(everything=False)], (pec.KEEP, pec.ERASE_DATA_ONLY, pec.CANCEL)),
    ([_spec("a", "1" * 64, data_only=False), _spec("b", "2" * 64, everything=False)],
     (pec.KEEP, pec.CANCEL)),
    ([_spec("a", "1" * 64), _spec("b", "2" * 64, data_only=False)],
     (pec.KEEP, pec.ERASE, pec.CANCEL)),
])
def test_an_option_is_offered_only_when_every_erasing_plugin_declares_it(specs, offered):
    import tools as tools_mod
    assert tools_mod._offered_erase_choices(specs) == offered


def test_the_question_says_what_each_offered_option_keeps():
    text = pec.render_erase_choice(
        "the plugin probe", [("probe", "erase_ledger", None), ("probe", "erase_all", None)],
        (pec.KEEP, pec.ERASE_DATA_ONLY, pec.ERASE, pec.CANCEL))
    assert "Erase data, keep sign-ins" in text and "Erase everything" in text
    assert "Home Assistant backups" in text
    only = pec.render_erase_choice("the plugin probe", [], (pec.KEEP, pec.CANCEL))
    assert "No erase option is offered" in only and "Erase everything" not in only


# --- the tap records the kind --------------------------------------------------------

async def _tap_on(choices, idx):
    coord, ch, grants, calls = _Coordinator(), _EditChannel(), pec.ChoiceGrants(), []

    async def cont(choice):
        calls.append(choice)
        return True
    pec.prompt_erase_choice(coordinator=coord, channel=ch, key=_key(), text="q",
                            continue_cb=cont, grants=grants, choices=choices)
    meta: dict = {}
    coord.kw["on_commit_sync"](idx, meta)
    await coord.kw["finish_factory"](7, SimpleNamespace(meta=meta))(
        {"outcome": "answered", "option_index": idx})
    return coord, grants, calls, ch.edits


@pytest.mark.asyncio
async def test_a_button_index_means_the_choice_it_shows():
    four = (pec.KEEP, pec.ERASE_DATA_ONLY, pec.ERASE, pec.CANCEL)
    coord, grants, calls, edits = await _tap_on(four, 1)
    assert coord.kw["options"] == ["Keep data", "Erase data, keep sign-ins",
                                   "Erase everything", "Cancel"]
    assert calls == [pec.ERASE_DATA_ONLY] and "keep sign-ins" in edits[-1]
    assert grants.consume_erase(_key()) == pec.ERASE_DATA_ONLY
    assert grants.consume_erase(_key()) is None                      # single use
    _c, grants, calls, _e = await _tap_on(four, 2)
    assert calls == [pec.ERASE] and grants.consume_erase(_key()) == pec.ERASE
    _c, grants, calls, _e = await _tap_on(four, 3)
    assert calls == [pec.CANCEL] and grants.consume_erase(_key()) is None
    # index 1 of the three-button question is Erase everything, not data-only
    _c, grants, calls, _e = await _tap_on((pec.KEEP, pec.ERASE, pec.CANCEL), 1)
    assert calls == [pec.ERASE] and grants.consume_erase(_key()) == pec.ERASE


# --- the tool flow -------------------------------------------------------------------

def _current_key(subject="plugin:probe", artifacts=(ART,)):
    return pec.EraseChoiceKey(42, 42, subject, artifacts, pe.QUESTIONS.current(subject))


@pytest.mark.asyncio
async def test_the_question_posts_the_declared_options(flow):
    flow.specs = [_spec()]
    out = await _remove(flow.tm)
    assert out["kind"] == "erase_choice_pending"
    [prompt] = flow.prompts
    assert prompt["choices"] == (pec.KEEP, pec.ERASE_DATA_ONLY, pec.ERASE, pec.CANCEL)
    assert "erase_ledger" in prompt["text"] and "erase_all" in prompt["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice, kind, tool, names", [
    (pec.ERASE_DATA_ONLY, pe.DATA_ONLY, "erase_ledger", (DATA_TOOL,)),
    (pec.ERASE, pe.EVERYTHING, "erase_all", (TOOL,)),
])
async def test_erase_true_runs_the_eraser_the_operator_tapped(flow, choice, kind, tool,
                                                              names):
    flow.specs = [_spec()]
    await _remove(flow.tm)
    pec.CHOICES.mint(_current_key(), choice)
    out = await _remove(flow.tm, erase_data=True)
    assert out["kind"] == "erasure_running" and _still_registered(flow)
    await asyncio.sleep(0)
    [(specs, _op, _q, ran)] = flow.episodes
    assert ran == kind
    assert [(s.tool, s.tool_names) for s in specs] == [(tool, names)]


@pytest.mark.asyncio
async def test_a_kind_no_longer_declared_is_unavailable(flow):
    """A data-only tap whose plugin no longer declares that eraser at the call
    runs nothing and removes nothing."""
    flow.specs = [_spec()]
    await _remove(flow.tm)
    pec.CHOICES.mint(_current_key(), pec.ERASE_DATA_ONLY)
    flow.specs = [_spec(data_only=False)]
    out = await _remove(flow.tm, erase_data=True)
    assert out["kind"] == "erase_unavailable" and flow.episodes == []
    assert _still_registered(flow)


# --- the finishing call and the references ------------------------------------------

@pytest.fixture
def conf(tmp_path, monkeypatch):
    import plugin_env_conf
    path = tmp_path / "plugin-env.conf"
    path.write_text("# header\nPROBE_TOKEN=op://v/probe/token\n"
                    "PROBE_MODE=sandbox\nSHARED_KEY=op://v/shared/key\n"
                    "OTHER_TOKEN=op://v/other/token\n")
    monkeypatch.setattr(plugin_env_conf, "PLUGIN_ENV_CONF_PATH", path)
    return path


@pytest.fixture
def resolved(tmp_path, monkeypatch):
    """The resolved snapshot: probe uses PROBE_TOKEN, SHARED_KEY and (through
    casa.setupProvides only) PROBE_MODE; other uses SHARED_KEY and OTHER_TOKEN."""
    import plugin_registry as preg

    def art(name, mcp_env, manifest_casa=None):
        d = tmp_path / f"art-{name}"
        (d / ".claude-plugin").mkdir(parents=True)
        (d / ".mcp.json").write_text(json.dumps({"mcpServers": {"srv": {
            "command": "x", "env": mcp_env}}}))
        return preg.ResolvedPlugin(name=name, artifact_id=ART, path=str(d),
                                   version="1",
                                   manifest={"name": name, "casa": manifest_casa or {}})
    plugins = [
        art("probe", {"T": "${PROBE_TOKEN}", "S": "${SHARED_KEY:-}"},
            {"setupTool": "setup_probe", "setupProvides": ["CASA_PLUGIN_PROBE_MODE"]}),
        art("other", {"S": "${SHARED_KEY}", "O": "${OTHER_TOKEN}"}),
    ]
    monkeypatch.setattr(preg, "resolve_all", lambda: preg.ResolutionResult(
        registry_valid=True, plugins=plugins))
    return plugins


def test_the_names_to_clear_are_the_plugins_own(resolved):
    import tools as tools_mod
    assert tools_mod._env_names_to_clear({"probe"}) == [
        "CASA_PLUGIN_PROBE_MODE", "PROBE_TOKEN"]                    # never SHARED_KEY


@pytest.fixture
def reloads(flow, monkeypatch):
    calls = []

    async def fake_reload():
        calls.append(True)
        return True
    monkeypatch.setattr(flow.tm, "_reload_plugin_env_after_clear", fake_reload)
    return calls


@pytest.mark.asyncio
async def test_an_everything_removal_clears_the_plugins_references(flow, conf, reloads,
                                                                   monkeypatch):
    monkeypatch.setattr(flow.tm, "_env_names_to_clear",
                        lambda erased: ["PROBE_MODE", "PROBE_TOKEN"]
                        if erased == {"probe"} else [])
    pe.RECORDS.put("plugin:probe", ART, "complete", "All gone.",
                   pe.QUESTIONS.open("plugin:probe"), pe.EVERYTHING)
    out = await _remove(flow.tm, erase_data=True)
    assert out["ok"] is True and not _still_registered(flow)
    assert out["erasure"] == "complete" and out["erasure_kind"] == "everything"
    assert out["env_references_cleared"] == ["PROBE_MODE", "PROBE_TOKEN"]
    assert "cleared its plugin-env.conf references" in out["plugin_data_note"]
    assert conf.read_text() == ("# header\nSHARED_KEY=op://v/shared/key\n"
                                "OTHER_TOKEN=op://v/other/token\n")
    assert reloads == [True] and "env_reload_ok" not in out


@pytest.mark.asyncio
async def test_a_data_only_removal_keeps_every_reference(flow, conf, reloads, monkeypatch):
    before = conf.read_text()
    monkeypatch.setattr(flow.tm, "_env_names_to_clear",
                        lambda erased: pytest.fail("a data-only removal clears nothing"))
    pe.RECORDS.put("plugin:probe", ART, "complete", "Ledger gone, sessions kept.",
                   pe.QUESTIONS.open("plugin:probe"), pe.DATA_ONLY)
    out = await _remove(flow.tm, erase_data=True)
    assert out["ok"] is True and not _still_registered(flow)
    assert out["erasure_kind"] == "data_only" and "env_references_cleared" not in out
    assert "keeping its sign-ins" in out["plugin_data_note"]
    assert conf.read_text() == before and reloads == []


@pytest.mark.asyncio
async def test_a_keep_removal_and_a_refused_finish_clear_nothing(flow, conf, reloads):
    before = conf.read_text()
    pe.RECORDS.put("plugin:probe", ART, "incomplete", "A bank kept its consent.",
                   pe.QUESTIONS.open("plugin:probe"), pe.EVERYTHING)
    out = await _remove(flow.tm, erase_data=True)
    assert out["kind"] == "erase_not_confirmed"
    out = await _remove(flow.tm, erase_data=False)
    assert out["ok"] is True and "env_references_cleared" not in out
    assert conf.read_text() == before and reloads == []


@pytest.mark.asyncio
async def test_a_failed_reload_is_said_and_the_lines_stay_cleared(flow, conf, monkeypatch):
    async def failed():
        return False
    monkeypatch.setattr(flow.tm, "_reload_plugin_env_after_clear", failed)
    monkeypatch.setattr(flow.tm, "_env_names_to_clear", lambda erased: ["PROBE_TOKEN"])
    pe.RECORDS.put("plugin:probe", ART, "complete", "All gone.",
                   pe.QUESTIONS.open("plugin:probe"), pe.EVERYTHING)
    out = await _remove(flow.tm, erase_data=True)
    assert out["ok"] is True and out["env_reload_ok"] is False
    assert "PROBE_TOKEN" not in conf.read_text()


@pytest.mark.asyncio
async def test_the_reference_reload_runs_after_the_lock_is_released(flow, conf,
                                                                   monkeypatch):
    held = []

    async def reload():
        held.append(flow.tm._PLUGIN_TOOLS_LOCK.locked())
        return True
    monkeypatch.setattr(flow.tm, "_reload_plugin_env_after_clear", reload)
    monkeypatch.setattr(flow.tm, "_env_names_to_clear", lambda erased: ["PROBE_TOKEN"])
    pe.RECORDS.put("plugin:probe", ART, "complete", "All gone.",
                   pe.QUESTIONS.open("plugin:probe"), pe.EVERYTHING)
    await _remove(flow.tm, erase_data=True)
    assert held == [False]


@pytest.mark.asyncio
async def test_a_specialist_clears_only_its_erased_plugins(sflow, conf, monkeypatch):
    asked = []

    def names(erased):
        asked.append(set(erased))
        return ["PROBE_TOKEN"]
    monkeypatch.setattr(sflow.tm, "_env_names_to_clear", names)

    async def ok():
        return True
    monkeypatch.setattr(sflow.tm, "_reload_plugin_env_after_clear", ok)
    pe.RECORDS.put("plugin:fin.bank", "1" * 64, "complete", "bank gone",
                   pe.QUESTIONS.open("specialist:fin"), pe.EVERYTHING)
    out = await _uninstall(sflow.tm, erase_data=True)
    assert out["ok"] is True and sflow.uninstalled == [True]
    assert asked == [{"fin.bank"}]                    # fin.tags has no eraser
    assert out["env_references_cleared"] == ["PROBE_TOKEN"]


# --- vault items an eraser left (#1073) ---------------------------------------------

LEFT = (("EnableBanking Key", True), ("EnableBanking", None))


@pytest.mark.asyncio
async def test_an_everything_removal_names_the_vault_items_the_eraser_left(
        flow, conf, reloads, monkeypatch):
    monkeypatch.setattr(flow.tm, "_env_names_to_clear", lambda erased: ["PROBE_TOKEN"])
    pe.RECORDS.put("plugin:probe", ART, "complete", "All gone but two items.",
                   pe.QUESTIONS.open("plugin:probe"), pe.EVERYTHING, LEFT)
    out = await _remove(flow.tm, erase_data=True)
    assert out["ok"] is True and not _still_registered(flow)
    assert out["erasure_kind"] == "everything"
    assert out["unrecorded_vault_items"] == [
        {"plugin": "probe", "title": "EnableBanking Key", "found": True},
        {"plugin": "probe", "title": "EnableBanking", "found": None}]
    note = out["plugin_data_note"]
    assert note.startswith(flow.tm._ERASED_EVERYTHING_NOTE)
    assert note.endswith(flow.tm._UNRECORDED_VAULT_ITEMS_NOTE)
    assert "deletion by hand" in note


@pytest.mark.asyncio
async def test_an_eraser_that_left_nothing_keeps_the_plain_note(flow, conf, reloads,
                                                                monkeypatch):
    monkeypatch.setattr(flow.tm, "_env_names_to_clear", lambda erased: ["PROBE_TOKEN"])
    pe.RECORDS.put("plugin:probe", ART, "complete", "All gone.",
                   pe.QUESTIONS.open("plugin:probe"), pe.EVERYTHING)
    out = await _remove(flow.tm, erase_data=True)
    assert "unrecorded_vault_items" not in out
    assert out["plugin_data_note"] == flow.tm._ERASED_EVERYTHING_NOTE


@pytest.mark.asyncio
async def test_a_data_only_removal_also_names_the_vault_items_left(flow, conf, reloads):
    pe.RECORDS.put("plugin:probe", ART, "complete", "Ledger gone.",
                   pe.QUESTIONS.open("plugin:probe"), pe.DATA_ONLY, LEFT[:1])
    out = await _remove(flow.tm, erase_data=True)
    assert out["unrecorded_vault_items"] == [
        {"plugin": "probe", "title": "EnableBanking Key", "found": True}]
    assert out["plugin_data_note"] == (flow.tm._ERASED_DATA_ONLY_NOTE
                                       + flow.tm._UNRECORDED_VAULT_ITEMS_NOTE)


@pytest.mark.asyncio
async def test_a_failed_clear_and_items_left_are_both_said(flow, conf, reloads,
                                                           monkeypatch):
    import plugin_env_conf

    def boom(names):
        raise PermissionError("read-only")
    monkeypatch.setattr(plugin_env_conf, "remove_entries", boom)
    monkeypatch.setattr(flow.tm, "_env_names_to_clear", lambda erased: ["PROBE_TOKEN"])
    pe.RECORDS.put("plugin:probe", ART, "complete", "r",
                   pe.QUESTIONS.open("plugin:probe"), pe.EVERYTHING, LEFT)
    out = await _remove(flow.tm, erase_data=True)
    assert out["env_references_not_cleared"] == ["PROBE_TOKEN"]
    assert out["plugin_data_note"] == (flow.tm._ERASED_EVERYTHING_UNCLEARED_NOTE
                                       + flow.tm._UNRECORDED_VAULT_ITEMS_NOTE)


@pytest.mark.asyncio
async def test_a_specialist_names_the_items_left_per_plugin(sflow, conf, monkeypatch):
    monkeypatch.setattr(sflow.tm, "_env_names_to_clear", lambda erased: [])
    pe.RECORDS.put("plugin:fin.bank", "1" * 64, "complete", "bank gone",
                   pe.QUESTIONS.open("specialist:fin"), pe.EVERYTHING, LEFT[1:])
    out = await _uninstall(sflow.tm, erase_data=True)
    assert out["ok"] is True
    assert out["unrecorded_vault_items"] == [
        {"plugin": "fin.bank", "title": "EnableBanking", "found": None}]
    note = out.get("plugin_data_note") or out["erased_note"]
    assert note.endswith(sflow.tm._UNRECORDED_VAULT_ITEMS_NOTE)


# --- the conf writer lock ------------------------------------------------------------

def test_a_reference_set_during_a_clear_is_not_lost(conf, monkeypatch):
    """Design r1 (Terra S2, Astra S2): every conf writer holds one lock across
    its read-modify-write, so a set that overlaps a clear lands after it."""
    import plugin_env_conf
    entered, release = threading.Event(), threading.Event()
    real = plugin_env_conf._remove_entries_locked

    def slow(names):
        entered.set()
        release.wait(5)
        return real(names)
    monkeypatch.setattr(plugin_env_conf, "_remove_entries_locked", slow)
    clearer = threading.Thread(
        target=plugin_env_conf.remove_entries, args=(["PROBE_TOKEN"],))
    clearer.start()
    assert entered.wait(5)
    setter = threading.Thread(
        target=plugin_env_conf.set_entry, args=("NEW_TOKEN", "op://v/new/t"))
    setter.start()
    setter.join(0.2)
    assert setter.is_alive()                          # waits for the clear
    release.set()
    clearer.join(5)
    setter.join(5)
    text = conf.read_text()
    assert "NEW_TOKEN=op://v/new/t" in text and "PROBE_TOKEN" not in text


def test_remove_entries_keeps_every_other_line(conf):
    import plugin_env_conf
    assert plugin_env_conf.remove_entries(["PROBE_TOKEN", "ABSENT", "PROBE_MODE"]) == [
        "PROBE_MODE", "PROBE_TOKEN"]
    assert conf.read_text() == ("# header\nSHARED_KEY=op://v/shared/key\n"
                                "OTHER_TOKEN=op://v/other/token\n")
    assert plugin_env_conf.remove_entry("ABSENT") is False


@pytest.mark.asyncio
async def test_the_reference_reload_holds_the_plugin_guard_while_it_dispatches(monkeypatch):
    """INV-CFG-011: the plugin_env handler takes the plugin guard, so this
    entry point takes it first, before the reload lock."""
    import agent as agent_mod
    import reload as reload_mod
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    monkeypatch.setattr(agent_mod, "active_runtime", object(), raising=False)
    seen = []

    async def dispatch(scope, **kw):
        seen.append((scope, tools_mod._PLUGIN_TOOLS_LOCK.locked()))
        return {"status": "ok"}

    async def regen(scope, result):
        seen.append(("regen", tools_mod._PLUGIN_TOOLS_LOCK.locked()))
    monkeypatch.setattr(reload_mod, "dispatch", dispatch)
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health_after_reload", regen)
    assert await tools_mod._reload_plugin_env_after_clear() is True
    assert seen == [("plugin_env", True), ("regen", False)]


@pytest.mark.asyncio
async def test_a_failed_clear_is_reported_never_claimed(flow, conf, reloads, monkeypatch):
    """Diff r1 (Terra S2): a rewrite that fails leaves the lines, and the result
    says so instead of claiming them cleared."""
    import plugin_env_conf

    def boom(names):
        raise PermissionError("read-only")
    monkeypatch.setattr(plugin_env_conf, "remove_entries", boom)
    monkeypatch.setattr(flow.tm, "_env_names_to_clear", lambda erased: ["PROBE_TOKEN"])
    pe.RECORDS.put("plugin:probe", ART, "complete", "All gone.",
                   pe.QUESTIONS.open("plugin:probe"), pe.EVERYTHING)
    out = await _remove(flow.tm, erase_data=True)
    assert out["ok"] is True and out["env_references_cleared"] == []
    assert out["env_references_not_cleared"] == ["PROBE_TOKEN"]
    assert "could NOT clear" in out["plugin_data_note"]
    assert "Casa cleared" not in out["plugin_data_note"]
    assert "PROBE_TOKEN" in conf.read_text() and reloads == []


@pytest.mark.asyncio
async def test_a_cancelled_removal_holds_the_lock_until_the_clear_settles(flow, conf,
                                                                          reloads,
                                                                          monkeypatch):
    """Diff r1 (Astra S2): cancelling the removal while the conf rewrite runs
    keeps the mutation lock until the rewrite finished, so no install can wire
    a name in between and then lose it."""
    import plugin_env_conf
    entered, release = threading.Event(), threading.Event()
    real = plugin_env_conf.remove_entries

    def slow(names):
        entered.set()
        release.wait(5)
        return real(names)
    monkeypatch.setattr(plugin_env_conf, "remove_entries", slow)
    monkeypatch.setattr(flow.tm, "_env_names_to_clear", lambda erased: ["PROBE_TOKEN"])
    pe.RECORDS.put("plugin:probe", ART, "complete", "All gone.",
                   pe.QUESTIONS.open("plugin:probe"), pe.EVERYTHING)
    task = asyncio.get_running_loop().create_task(_remove(flow.tm, erase_data=True))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.05)
    assert flow.tm._PLUGIN_TOOLS_LOCK.locked()             # still held
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not flow.tm._PLUGIN_TOOLS_LOCK.locked()
    assert "PROBE_TOKEN" not in conf.read_text()
