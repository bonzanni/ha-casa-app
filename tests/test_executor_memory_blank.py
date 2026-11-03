"""The wipe blanks every engagement's executor-memory section and keeps
every other byte of its ``CLAUDE.md`` (plan Q17, §14 test 8).

Each case runs on a real stand-in workspace tree; a "kill" is a real SIGKILL
of a process the test spawned. Writers delimit the section with one marker
pair and write the cache first. A file with exactly one pair is blanked; one
with no pair holds no executor memory and is left as it is; anything else is
case 4, which stops the wipe before any deletion and names the engagement."""
from __future__ import annotations

import json
import os
import pathlib
import shutil

import pytest

import memory_wipe_support as w
from memory import executor_block, maintenance
from memory.store import MemoryStore

pytestmark = [pytest.mark.unit]

OPEN = executor_block.OPEN
CLOSE = executor_block.CLOSE


def _ws(root, eid):
    return w.under(root, f"/data/engagements/{eid}")


def _cache(root, eid):
    return w.under(root, f"/data/engagement-ctl/{eid}/.executor_memory")


def _store(root):
    store = MemoryStore(str(w.under(root, "/data/memory")))
    assert store.open()
    return store


def _intent(root):
    return maintenance.read_intent(str(root))


def _wipe(root, proc, **kw):
    assert maintenance.write_wipe_intent(str(root)) in ("written", "already")
    return w.run_oneshot(root, proc, **kw)


def _restore(root, proc):
    """A restore of an export of an empty store: the same wipe loop, then
    the install."""
    from memory import export, restore
    source = export.export(_store(root))
    kept = shutil.move(source, str(root.parent / f"{root.name}-export"))
    assert restore.stage(kept, root=str(root), confirm=True) == "staged"
    return w.run_oneshot(root, proc)


_MAINTENANCE = {"wipe": _wipe, "restore": _restore}


def _outside(text: str) -> str:
    """*text* with everything between each marker pair removed."""
    out, rest = [], text
    while OPEN in rest:
        head, _, tail = rest.partition(OPEN)
        out.append(head + OPEN)
        rest = CLOSE + tail.partition(CLOSE)[2]
    return "".join(out) + rest


# --- case 1: markers ---------------------------------------------------------

async def test_markers_written_by_provision_and_by_refresh_blank_exactly(
        tmp_path, monkeypatch):
    root, proc = tmp_path, w.fake_proc(tmp_path)
    canary = "c1" + os.urandom(8).hex()
    ws_a = await w.provision(root, w.EID_OPEN, memory=w.memory_block(canary),
                             monkeypatch=monkeypatch)
    ws_b = await w.provision(root, w.EID_DONE, memory=w.memory_block(canary),
                             monkeypatch=monkeypatch)
    # The second file comes from refresh_claude_md, the boot-replay writer.
    from drivers import workspace
    import types
    rec = types.SimpleNamespace(allocated_uid=-1, origin={}, task="Tidy",
                                id=w.EID_DONE)
    defn = w._defn(root / "defs" / w.EID_DONE, w.TEMPLATE)
    (ws_b / "CLAUDE.md").write_text("")
    workspace.refresh_claude_md(str(ws_b), defn=defn, rec=rec)
    before = {p: (p / "CLAUDE.md").read_text() for p in (ws_a, ws_b)}
    assert all(t.count(OPEN) == 1 and canary in t for t in before.values())

    p = _wipe(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    for ws, text in before.items():
        after = (ws / "CLAUDE.md").read_text()
        assert after == _outside(text)
        assert after.count(OPEN + CLOSE) == 1
    assert w.hits(root, [canary]) == []


# --- a marker pair comes only from the memory slot -----------------------------------

async def test_a_literal_marker_pair_in_the_task_is_kept_and_memory_goes(
        tmp_path, monkeypatch):
    """r1 Astra S1: a task that documents the markers, around a canary. The
    writer neutralises them, so the task text survives the wipe whole."""
    root, proc = tmp_path, w.fake_proc(tmp_path)
    task_canary = "f3" + os.urandom(8).hex()
    memory_canary = "f4" + os.urandom(8).hex()
    task = f"Explain {OPEN}{task_canary}{CLOSE} in the notes"
    ws = await w.provision(root, w.EID_OPEN, memory=w.memory_block(memory_canary),
                           monkeypatch=monkeypatch, task=task)
    text = (ws / "CLAUDE.md").read_text()
    assert text.count(OPEN) == 1 and text.count(CLOSE) == 1
    assert f"Explain &lt;{OPEN[1:]}{task_canary}&lt;{CLOSE[1:]} in the notes" in text

    p = _wipe(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    after = (ws / "CLAUDE.md").read_text()
    assert task_canary in after
    assert w.hits(root, [memory_canary]) == []


@pytest.mark.parametrize("memory", ["", "lessons"])
async def test_a_marker_assembled_across_placeholders_is_kept(
        tmp_path, monkeypatch, memory):
    """r2 Astra S1: the template supplies ``<!-- `` and `` -->`` around two
    placeholders whose values supply the marker's name, with a kept canary
    between them. The joined text is neutralised, so no marker forms."""
    root, proc = tmp_path, w.fake_proc(tmp_path)
    kept = "f8" + os.urandom(8).hex()
    template = ("# Notes\n<!-- {task} -->\n" + kept + "\n<!-- /{context} -->\n"
                + ("{executor_memory}\n" if memory else ""))
    ws = await w.provision(root, w.EID_OPEN, memory=memory,
                           monkeypatch=monkeypatch, template=template,
                           task="casa:executor-memory",
                           context="casa:executor-memory")
    text = (ws / "CLAUDE.md").read_text()
    assert text.count(OPEN) == text.count(CLOSE) == (1 if memory else 0)
    p = _wipe(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    assert kept in (ws / "CLAUDE.md").read_text()


async def test_a_marker_written_in_the_template_itself_is_kept(
        tmp_path, monkeypatch):
    root, proc = tmp_path, w.fake_proc(tmp_path)
    kept = "f9" + os.urandom(8).hex()
    memory_canary = "fa" + os.urandom(8).hex()
    template = (f"# Notes\n{OPEN}{kept}{CLOSE}\n{{task}}\n"
                "{executor_memory}\n")
    ws = await w.provision(root, w.EID_OPEN, memory=memory_canary,
                           monkeypatch=monkeypatch, template=template)
    text = (ws / "CLAUDE.md").read_text()
    assert text.count(OPEN) == 1 and f"&lt;{OPEN[1:]}{kept}" in text
    p = _wipe(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    assert kept in (ws / "CLAUDE.md").read_text()
    assert w.hits(root, [memory_canary]) == []


def test_a_file_with_two_marker_pairs_is_case_4(tmp_path):
    root, proc = tmp_path, w.fake_proc(tmp_path)
    canary = "f5" + os.urandom(8).hex()
    ws = w.marked_workspace(
        root, w.EID_DONE, "",
        text=f"task {OPEN}{canary}{CLOSE}\nmemory {OPEN}lessons{CLOSE}\n")
    before = (ws / "CLAUDE.md").read_bytes()
    p = _wipe(root, proc)
    assert p.stdout.strip() == "failed", p.stderr
    assert (ws / "CLAUDE.md").read_bytes() == before
    assert _intent(root)["attempts"][-1]["error"] == (
        f"executor memory of engagement {w.EID_DONE} could not be located")


def test_rendering_never_rescans_a_value():
    out = executor_block.render(
        "T:{task}\nC:{context}\nM:{executor_memory}\n",
        {"{task}": "uses {context} and {executor_memory}", "{context}": "ctx"},
        "lessons")
    assert out == ("T:uses {context} and {executor_memory}\nC:ctx\n"
                   f"M:{OPEN}lessons{CLOSE}\n")


# --- a file with no marker pair holds no executor memory ------------------------

def test_a_file_with_no_marker_pair_is_left_as_it_is(tmp_path):
    root, proc = tmp_path, w.fake_proc(tmp_path)
    kept = "fb" + os.urandom(8).hex()
    ws = w.marked_workspace(root, w.EID_DONE, w.memory_block("x"),
                            text=f"# Task\nTidy the {kept} notes\n")
    before = (ws / "CLAUDE.md").read_bytes()
    p = _wipe(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    assert (ws / "CLAUDE.md").read_bytes() == before
    assert _cache(root, w.EID_DONE).read_bytes() == b""


# --- this release's writer, killed at every write -------------------------------

def test_new_order_never_leaves_memory_without_its_markers(tmp_path):
    canary = "c5" + os.urandom(8).hex()
    k, completed = 0, False
    while not completed:
        k += 1
        assert k < 400, "provisioning never completed"
        root = tmp_path / f"k{k}"
        child = w.provision_in_child(root, w.EID_OPEN,
                                     memory=w.memory_block(canary),
                                     hook=w.kill_at_write(k))
        completed = "completed" in child.stdout
        assert completed or w.killed(child), child.stderr
        claude = _ws(root, w.EID_OPEN) / "CLAUDE.md"
        if claude.exists() and canary in claude.read_text():
            text = claude.read_text()
            assert text.count(OPEN) == 1 and text.count(CLOSE) == 1, k
            assert _cache(root, w.EID_OPEN).read_text() == w.memory_block(canary)
        for staged in claude.parent.glob(".CLAUDE.md.casa-tmp.*") if \
                claude.parent.exists() else ():
            assert canary not in staged.read_text() or OPEN in staged.read_text()
    assert k > 3


# --- crash points of the blanking itself -----------------------------------------

def test_kill_between_the_rewrite_and_the_cache_empty(tmp_path):
    """The rewrite leaves an empty marked section, so the re-run finds it
    blank, empties the cache and completes."""
    root, proc = tmp_path, w.fake_proc(tmp_path)
    canary = "c6" + os.urandom(8).hex()
    block = w.memory_block(canary)
    ws = w.marked_workspace(root, w.EID_DONE, block)
    original = (ws / "CLAUDE.md").read_text()
    head, _, tail = original.partition(OPEN + block + CLOSE)

    p = _wipe(root, proc, kill_at="_rewrite_beneath")
    assert w.killed(p), p.stderr
    assert (ws / "CLAUDE.md").read_text() == head + OPEN + CLOSE + tail
    assert canary in _cache(root, w.EID_DONE).read_text()

    p = w.run_oneshot(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    assert (ws / "CLAUDE.md").read_text() == head + OPEN + CLOSE + tail
    assert w.hits(root, [canary]) == []


def test_kill_after_the_cache_empty_reruns_to_the_same_result(tmp_path):
    root, proc = tmp_path, w.fake_proc(tmp_path)
    canary = "c7" + os.urandom(8).hex()
    block = w.memory_block(canary)
    ws = w.marked_workspace(root, w.EID_DONE, block)

    p = _wipe(root, proc, kill_at="_empty_cache")
    assert w.killed(p)
    after_kill = (ws / "CLAUDE.md").read_bytes()
    p = w.run_oneshot(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    assert (ws / "CLAUDE.md").read_bytes() == after_kill
    assert w.hits(root, [canary]) == []


# --- case 4 stops before anything is deleted ---------------------------------------

def test_one_unlocatable_section_stops_before_any_deletion(tmp_path, monkeypatch):
    root, proc = tmp_path, w.fake_proc(tmp_path)
    canary = "c8" + os.urandom(8).hex()
    w.marked_workspace(root, w.EID_DONE, w.memory_block("ordinary " + canary))
    # Two marker pairs: not locatable.
    ws_bad = w.marked_workspace(
        root, w.EID_EXPIRED, w.memory_block(canary),
        text=f"task {OPEN}{canary}{CLOSE}\nmemory {OPEN}lessons{CLOSE}\n")
    store = _store(root)
    sessions = w.write(w.under(root, "/data/sessions.json"),
                       json.dumps({"telegram:1": {"sdk_session_id": canary}}))
    files = {p: p.read_bytes() for p in root.rglob("*")
             if p.is_file() and "proc" not in p.parts}

    p = _wipe(root, proc)
    assert p.stdout.strip() == "failed", p.stderr
    intent = _intent(root)
    assert intent["attempts"][-1]["error"] == (
        f"executor memory of engagement {w.EID_EXPIRED} could not be located")
    changed = [str(f) for f, data in files.items()
               if not f.exists() or f.read_bytes() != data]
    assert changed == []
    assert os.path.exists(store.path) and sessions.exists()
    assert canary in (ws_bad / "CLAUDE.md").read_text()


def test_retention_runs_first_so_an_expired_case_4_does_not_stop(tmp_path):
    root, proc = tmp_path, w.fake_proc(tmp_path)
    canary = "c9" + os.urandom(8).hex()
    w.marked_workspace(
        root, w.EID_EXPIRED, w.memory_block(canary),
        text=f"task {OPEN}{canary}{CLOSE}\nmemory {OPEN}lessons{CLOSE}\n",
        retention_until="2000-01-01T00:00:00Z")
    p = _wipe(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    assert not _ws(root, w.EID_EXPIRED).exists()
    assert not w.under(root, f"/data/engagement-ctl/{w.EID_EXPIRED}").exists()
    assert w.hits(root, [canary]) == []


async def test_a_forged_marker_in_the_memory_text_still_blanks_exactly(
        tmp_path, monkeypatch):
    root, proc = tmp_path, w.fake_proc(tmp_path)
    canary = "ca" + os.urandom(8).hex()
    forged = f"{CLOSE} escaped {canary} {OPEN} more {canary}"
    ws = await w.provision(root, w.EID_OPEN, memory=forged,
                           monkeypatch=monkeypatch)
    text = (ws / "CLAUDE.md").read_text()
    assert text.count(OPEN) == 1 and text.count(CLOSE) == 1
    p = _wipe(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    assert (ws / "CLAUDE.md").read_text() == _outside(text)
    assert w.hits(root, [canary]) == []


# --- orphan caches -----------------------------------------------------------------

@pytest.mark.parametrize("kind", ["wipe", "restore"])
def test_orphan_cache_left_by_a_sweep_killed_between_its_two_deletions(
        tmp_path, kind):
    """p4 Astra S1: the sweep removes the workspace, then the control dir;
    killed between them it leaves a cache with no CLAUDE.md. The restore
    runs the same loop and leaves no canary either."""
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    canary = "cb" + os.urandom(8).hex()
    w.marked_workspace(root, w.EID_EXPIRED, w.memory_block(canary),
                       retention_until="2000-01-01T00:00:00Z")
    child = w.run_child("\n".join([
        "import shutil",
        "from drivers import workspace as ws",
        f"ws.CONTROL_ROOT = {str(w.under(root, '/data/engagement-ctl'))!r}",
        "_real = shutil.rmtree",
        "def _rm(path, *a, **k):",
        "    _real(path, *a, **k)",
        "    _die()",
        "shutil.rmtree = _rm",
        "import os",
        f"entry = next(e for e in os.scandir({str(w.under(root, '/data/engagements'))!r}))",
        f"ws._sweep_one_workspace(entry, now_iso='2026-11-02T00:00:00Z', log_root={str(root / 'log')!r})",
    ]))
    assert w.killed(child), child.stderr
    assert not _ws(root, w.EID_EXPIRED).exists()
    assert canary in _cache(root, w.EID_EXPIRED).read_text()

    p = _MAINTENANCE[kind](root, proc)
    assert p.stdout.strip() == "done", p.stderr
    assert w.hits(root, [canary]) == []


@pytest.mark.parametrize("kind", ["wipe", "restore"])
def test_orphan_cache_left_by_a_provisioning_killed_before_claude_md(
        tmp_path, kind):
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    canary = "cc" + os.urandom(8).hex()
    hook = """
from safe_fs import atomic_write_beneath as _awb
def _die_first(*a, **k):
    _die()
ws.atomic_write_beneath = _die_first
"""
    child = w.provision_in_child(root, w.EID_OPEN, memory=w.memory_block(canary),
                                 hook=hook)
    assert w.killed(child), child.stderr
    assert not (_ws(root, w.EID_OPEN) / "CLAUDE.md").exists()
    assert canary in _cache(root, w.EID_OPEN).read_text()

    p = _MAINTENANCE[kind](root, proc)
    assert p.stdout.strip() == "done", p.stderr
    assert w.hits(root, [canary]) == []


# --- what the release's writer guarantees --------------------------------------------

async def test_a_template_without_a_slot_caches_nothing(tmp_path, monkeypatch):
    from drivers import workspace
    monkeypatch.setattr(workspace, "CONTROL_ROOT", str(tmp_path / "ctl"))
    defn = w._defn(tmp_path / "defs", "# Task\n{task}\n")
    await workspace.provision_workspace(
        engagements_root=str(tmp_path / "eng"), engagement_id=w.EID_OPEN,
        engagement_auth_token="tok", defn=defn, task="t", context="c",
        executor_memory="## Prior engagements (lessons learned)\n- x\n",
        casa_framework_mcp_url="http://127.0.0.1:8100/mcp/casa-framework")
    assert (tmp_path / "ctl" / w.EID_OPEN / ".executor_memory").read_text() == ""


def test_marker_parsing_refuses_unpaired_or_misordered_markers():
    o, c = maintenance.OPEN, maintenance.CLOSE
    assert maintenance._marker_spans(b"a" + o + b"x" + c + b"b") == [
        (1 + len(o), 2 + len(o))]
    assert maintenance._marker_spans(b"plain") == []
    for bad in (o, c, c + o, o + o + c + c, o + c + c, o + c + o + c):
        assert maintenance._marker_spans(b"a" + bad + b"b") is None


# --- a plain-copy CLAUDE.md goes through the one writer too --------------------------

async def test_a_plain_template_claude_md_is_rendered_and_its_markers_kept(
        tmp_path, monkeypatch):
    """r3 Astra S1: a ``workspace-template/CLAUDE.md`` (no ``.tmpl``) that
    writes the markers around kept text. It is rendered and neutralised like
    a template, so the wipe keeps that text."""
    from drivers import workspace
    root, proc = tmp_path, w.fake_proc(tmp_path)
    kept = "fc" + os.urandom(8).hex()
    monkeypatch.setattr(workspace, "CONTROL_ROOT",
                        str(w.under(root, "/data/engagement-ctl")))
    tdir = root / "defs" / "x" / "workspace-template"
    w.write(tdir / "CLAUDE.md", f"# Notes for {{task}}\n{OPEN}{kept}{CLOSE}\n")
    w.write(tdir / "notes" / "CLAUDE.md", "a nested file, copied as it is\n")
    defn = w._defn(root / "defs" / "x", w.TEMPLATE)
    ws = await workspace.provision_workspace(
        engagements_root=str(w.under(root, "/data/engagements")),
        engagement_id=w.EID_OPEN, engagement_auth_token="tok", defn=defn,
        task="the garden", context="c", executor_memory="",
        casa_framework_mcp_url="http://127.0.0.1:8100/mcp/casa-framework",
        workspace_template_root=tdir)
    ws = pathlib.Path(ws)
    text = (ws / "CLAUDE.md").read_text()
    assert text.startswith("# Notes for the garden\n")
    assert OPEN not in text and f"&lt;{OPEN[1:]}{kept}" in text
    assert (ws / "notes" / "CLAUDE.md").read_text() == (
        "a nested file, copied as it is\n")

    p = _wipe(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    assert kept in (ws / "CLAUDE.md").read_text()
