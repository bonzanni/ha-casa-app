"""#1296 — a library-kept upgrade says "new and open conversations still use the
previous version" only when the specialist Casa finds running is the version the
upgrade replaced.

`specialist_upgrade`'s library-kept arm (`upgrade_kept_new_version`) returns
before the sequencer, so Casa loaded none of the kept version. Its `outcome` and
the library's `detail` said the ruled in-use sentence (ruling-1095-5/-6)
whatever was running. After an earlier failed upgrade — a kept reload failure,
or an earlier library-kept upgrade — the live agent is OLDER than the version
this upgrade replaced, and the sentence names a version as in use that is not.

The sentence is conditioned on the live agent's component root compared with
the root this upgrade replaced (captured by the library before the commit):
equal keeps the ruled text byte-for-byte; a different root says #1146's
"was not running the new version"; no root (a persona override, no live agent)
says #1146's "could not be established". Both reused phrases are dated by this
upgrade instead of "that reload".

Everything real: `tools.specialist_upgrade.handler`, `upgrade_specialist`, the
reload dispatcher, on `_UpgradeFixture`'s on-disk specialist and the #1146
harness's runtime. Only the owned-plugin swap (and, for the restart form, the
retained prior's cleanup) is made to fail.
"""
from __future__ import annotations

import json

import pytest
import yaml

import plugin_registry
import specialist_bundle_journal as journal
import specialist_install as si
import specialist_receipt
import test_1146_kept_upgrade_reload_failed as harness
from test_1146_kept_upgrade_reload_failed import h  # noqa: F401 — fixture
from test_specialist_bundle_commit import (
    _UpgradeFixture, _declare_config_schema, _subdir_stub, write_minimal_component)

pytestmark = pytest.mark.unit      # asyncio_mode = auto (pytest.ini)

REAL_SNAPSHOT = plugin_registry.reload_snapshot
REAL_LOAD = plugin_registry.load_registry
REAL_OWNED = plugin_registry.owned_entries_for
REAL_SWAP = plugin_registry.apply_owned_swap
REAL_COMPLETE = journal.complete
REAL_FINISH = journal.BundleTxn.finish_forward
REAL_UPGRADE = si.upgrade_specialist
REAL_RECEIPT = specialist_receipt.load
REAL_VALIDATE = si.validate_resume_inputs

IN_USE = "new and open conversations still use the previous version"
NOT_NEW = ", and when this upgrade returned the specialist was not running the new version"
UNKNOWN = (", and which version it was running when this upgrade returned could not be "
           "established")
RULED = ", so " + IN_USE
HEAD = "the upgrade is not active yet: the new version is kept on disk, but Casa has not loaded it"
RERUN_TAIL = ". Re-running the same upgrade finishes it."
RESTART_TAIL = (". Finishing the version it replaced failed too, so further changes to this "
                "specialist are refused until Casa restarts: restart Casa, then re-run the "
                "upgrade.")
# ruling-1095-5 / -6, byte-for-byte as shipped (tools.py at 42f3da4d).
RULED_OUTCOME = (
    "the upgrade is not active yet: the new version is kept on disk, but Casa "
    "has not loaded it, so new and open conversations still use the previous "
    "version. Re-running the same upgrade finishes it.")
RULED_RESTART_OUTCOME = (
    "the upgrade is not active yet: the new version is kept on disk, but Casa "
    "has not loaded it, so new and open conversations still use the previous "
    "version. Finishing the version it replaced failed too, so further changes "
    "to this specialist are refused until Casa restarts: restart Casa, then "
    "re-run the upgrade.")
SWAP_FAILURE = "OSError: injected owned swap failure"


def _detail(clause: str, *, restart: bool) -> str:
    """The library's detail for `mtg` with *clause* in the in-use slot; every
    other byte as shipped (specialist_install.py at 42f3da4d)."""
    kept = ("'mtg': the upgrade is not active yet. The new version is kept — the "
            "version it replaced cannot be restored whole, because a setting it kept "
            "as a plain value is now secret — but the upgrade then failed "
            f"({SWAP_FAILURE}) before Casa loaded it{clause}")
    if restart:
        return (f"{kept}. Finishing the retained prior failed too (injected cleanup "
                "failure); its undo record is kept, so further changes to this "
                "specialist are refused until Casa restarts and finishes it: restart "
                "Casa, then re-run the upgrade. Nothing was deleted")
    return (f"{kept}, and its owned plugins may still be the previous version's. "
            "Re-running the same upgrade finishes it. Nothing was deleted")


def _outcome(clause: str, *, restart: bool) -> str:
    return HEAD + clause + (RESTART_TAIL if restart else RERUN_TAIL)


def test_the_composed_ruled_texts_are_the_shipped_ones():
    """The helpers above reproduce the ruled constants exactly, so a byte
    comparison against them is a comparison against the ruling."""
    assert _outcome(RULED, restart=False) == RULED_OUTCOME
    assert _outcome(RULED, restart=True) == RULED_RESTART_OUTCOME


@pytest.fixture(autouse=True)
def _role(monkeypatch):
    """`_UpgradeFixture` installs `mtg`; the #1146 harness's runtime is built for
    its module-level ROLE. Set before `h` (autouse fixtures come first)."""
    monkeypatch.setattr(harness, "ROLE", "mtg")


class _World:
    """`_UpgradeFixture`'s installed A (0.1.0) and pending B (0.2.0, declaring a
    plain setting of A secret, so a failure after activation keeps it), driven
    through the real handler against the #1146 harness's runtime, whose live
    `mtg` agent is bound to A."""

    def __init__(self, h, tmp_path, monkeypatch):
        from personality_binding import InstanceDir

        self.h, self.tmp_path, self.mp = h, tmp_path, monkeypatch
        monkeypatch.setattr(plugin_registry, "load_registry", REAL_LOAD)
        monkeypatch.setattr(plugin_registry, "owned_entries_for", REAL_OWNED)
        monkeypatch.setattr(journal, "complete", REAL_COMPLETE)
        REAL_SNAPSHOT(registry_path=tmp_path / "snapshot.json", store_root=tmp_path / "store")
        fx = _UpgradeFixture(tmp_path / "disk", monkeypatch,
                             v2_required=("j",), v2_secret_names=("k",))
        fx.approve_v2()
        self.fx = fx
        monkeypatch.setattr(plugin_registry, "REGISTRY_PATH", fx.common["registry_path"])
        self.instance_dir = InstanceDir(fx.slug_dir)
        self.a = self.root()
        h.old.config.binding = harness._binding(self.a)
        monkeypatch.setattr(si, "validate_resume_inputs",
                            lambda **kw: REAL_VALIDATE(**kw, receipts_dir=fx.tmp_path / "receipts"))
        monkeypatch.setattr(specialist_receipt, "load",
                            lambda rid, *a, **kw: REAL_RECEIPT(
                                rid, receipts_dir=fx.tmp_path / "receipts"))
        self.raised: list = []

        def upgrade(**kw):
            try:
                result = REAL_UPGRADE(**{**fx.common, **kw, "acks": fx.acks})
            except si.SpecialistInstallError as exc:
                self.raised.append(exc)
                raise
            h.loaded_root = self.root()
            return result
        monkeypatch.setattr(si, "upgrade_specialist", upgrade)
        self.finish_calls = 0
        self.fail_cleanup = False

        def finish(txn):
            self.finish_calls += 1
            if self.fail_cleanup and self.finish_calls > 1:
                raise OSError("injected cleanup failure")
            return REAL_FINISH(txn)
        monkeypatch.setattr(journal.BundleTxn, "finish_forward", finish)

    def root(self):
        return self.instance_dir.active().root

    def prior_root(self):
        return yaml.safe_load((self.fx.slug_dir / "active.prior.yaml").read_text())["root"]

    def swap_fails(self, fails: bool):
        def fail_swap(**kw):
            raise OSError("injected owned swap failure")
        self.mp.setattr(plugin_registry, "apply_owned_swap", fail_swap if fails else REAL_SWAP)

    def cleanup_fails(self, fails: bool):
        self.fail_cleanup, self.finish_calls = fails, 0

    async def call(self, insp, config, secrets) -> dict:
        import tools
        r = await tools.specialist_upgrade.handler(dict(
            slug="mtg", component_id=insp.component_id, version=insp.version,
            root_digest=insp.root_digest, staged_dir=str(insp.staged_dir),
            receipt_id=insp.receipt_id, config=config, secret_names_provided=secrets))
        return json.loads(r["content"][0]["text"])

    async def first(self, failure: str) -> dict:
        """A → B, kept: `reload` (the sequencer's reload fails) or `library`
        (the owned swap fails after activation)."""
        if failure == "reload":
            self.h.registry.fail_load = OSError("scan refused")
        else:
            self.swap_fails(True)
        out = await self.call(self.fx.insp2, {"j": "plain-j"}, ["k"])
        self.swap_fails(False)
        self.h.registry.fail_load = None
        assert out["kept_new_version"] is True, out
        assert out["kind"] == ("reload_failed" if failure == "reload"
                               else "upgrade_kept_new_version"), out
        return out

    def stage_c(self):
        """C (0.3.0), declaring B's plain `j` secret too, so B → C is kept."""
        from specialist_registry import InstalledSpecialistIndex
        fx = self.fx
        comp, manifest = write_minimal_component(fx.tmp_path / "v3", slug="mtg")
        _declare_config_schema(comp, manifest, required=[], secret_names=["k", "j"])
        doc = json.loads(manifest.read_text())
        doc["version"] = "0.3.0"
        manifest.write_text(json.dumps(doc))
        self.mp.setattr(si, "resolve_and_fetch", _subdir_stub(comp, "c" * 40))
        idx = InstalledSpecialistIndex(specialists_dir=str(fx.tmp_path / "installed-index"))
        idx.load()
        fx.insp2 = si.inspect_specialist_repo(
            "org/repo", "v3", staging_root=fx.tmp_path / "staging3", installed_index=idx,
            mode="upgrade", target_slug="mtg", specialists_dir=fx.common["specialists_dir"],
            receipts_dir=fx.tmp_path / "receipts")
        fx.approve_v2()
        return fx.insp2

    async def second(self, restart: bool) -> dict:
        """B → C, library-kept; *restart* also fails the retained prior's cleanup."""
        insp = self.stage_c()
        self.swap_fails(True)
        self.cleanup_fails(restart)
        out = await self.call(insp, {}, ["k", "j"])
        self.swap_fails(False)
        self.cleanup_fails(False)
        return out


@pytest.fixture
def world(h, tmp_path, monkeypatch):
    return _World(h, tmp_path, monkeypatch)


def _assert_states(world, out, *, b):
    """Loaded A, prior B, committed C; nothing swapped into runtime.agents."""
    h = world.h
    c = world.root()
    assert len({world.a, b, c}) == 3
    assert world.prior_root() == b
    assert h.runtime.agents["mtg"] is h.old
    assert h.runtime.agents["mtg"].config.binding.component_root == world.a
    assert h.runtime.agents.writes == []
    assert out["kind"] == "upgrade_kept_new_version", out
    assert out["kept_new_version"] is True


def _assert_told(out, clause, *, restart):
    """Both fields carry *clause* in the in-use slot and every other byte of the
    ruled text; the FULL ruled clause appears in neither (the owned-plugins
    clause carries the substring "previous version", R11-8)."""
    if clause != RULED:
        assert IN_USE not in out["outcome"], out["outcome"]
        assert IN_USE not in out["detail"], out["detail"]
    assert out["outcome"] == _outcome(clause, restart=restart)
    assert out["detail"] == _detail(clause, restart=restart)


@pytest.mark.parametrize("first", ["reload", "library"])
@pytest.mark.parametrize("restart", [False, True], ids=["rerun", "restart"])
async def test_p1_p2_p3_an_older_live_version_is_not_named_in_use(world, first, restart):
    """P-1 (reload → library), P-2 (reload → library, restart form), P-3 step 2
    (library → library, both forms): the live agent is A, the upgrade replaced B,
    so the result says the specialist was not running the new version."""
    one = await world.first(first)
    b = world.root()
    if first == "library":
        # P-3 step 1: a first kept upgrade from a cleanly loaded A — the ruled text.
        assert one["outcome"] == RULED_OUTCOME, one
        assert one["detail"] == _detail(RULED, restart=False), one
    out = await world.second(restart)
    _assert_states(world, out, b=b)
    _assert_told(out, NOT_NEW, restart=restart)


@pytest.mark.parametrize("restart", [False, True], ids=["rerun", "restart"])
async def test_p4_a_first_kept_upgrade_from_a_clean_load_says_the_ruled_text(world, restart):
    """P-4 (green at the base, stays green): the live agent bound to A's REAL
    persisted binding — what the specialist loader assigns (`cfg.binding =
    active_tuple.binding`) — and one library-kept A → B. Byte-for-byte the
    ruled outcome and detail."""
    binding = world.instance_dir.active().binding
    assert binding.component_root == world.a
    world.h.old.config.binding = binding
    world.swap_fails(True)
    world.cleanup_fails(restart)
    out = await world.call(world.fx.insp2, {"j": "plain-j"}, ["k"])
    assert out["kind"] == "upgrade_kept_new_version", out
    assert world.h.runtime.agents.writes == []
    assert out["outcome"] == (RULED_RESTART_OUTCOME if restart else RULED_OUTCOME)
    assert out["detail"] == _detail(RULED, restart=restart)


@pytest.mark.parametrize("live", ["override", "no_agent"])
@pytest.mark.parametrize("restart", [False, True], ids=["rerun", "restart"])
async def test_p5_no_live_root_says_it_could_not_be_established(world, live, restart):
    """P-5: a persona-override binding carries no component root, and with no live
    agent there is none to read: no evidence either way."""
    if live == "override":
        world.h.old.config.binding = harness._binding(None)
    else:
        dict.pop(world.h.runtime.agents, "mtg")
    world.swap_fails(True)
    world.cleanup_fails(restart)
    out = await world.call(world.fx.insp2, {"j": "plain-j"}, ["k"])
    assert out["kind"] == "upgrade_kept_new_version", out
    assert out["kept_new_version"] is True
    _assert_told(out, UNKNOWN, restart=restart)


async def test_p7_the_library_carries_the_root_this_upgrade_replaced(world):
    """P-7: the error the real library raises on P-1's second step carries B —
    the root active.yaml named before this upgrade's commit — never C."""
    await world.first("reload")
    b = world.root()
    await world.second(False)
    c = world.root()
    assert b != c
    assert len(world.raised) == 1
    assert getattr(world.raised[0], "replaced_root", "<absent>") == b


@pytest.mark.parametrize("carrier", ["none", "absent"])
@pytest.mark.parametrize("live", ["a", "no_agent"])
@pytest.mark.parametrize("restart", [False, True], ids=["rerun", "restart"])
async def test_p8_no_carried_replaced_root_says_it_could_not_be_established(
        world, carrier, live, restart, monkeypatch):
    """P-8 (Astra, red-case specify): the real library-kept failure with its
    carried replaced root cleared (None) or stripped (attribute absent) before
    the handler sees it — no evidence of what was replaced, so neither the ruled
    text nor "not running the new version", whatever the live root."""
    upgrade = si.upgrade_specialist

    def strip(**kw):
        try:
            return upgrade(**kw)
        except si.SpecialistInstallError as exc:
            if carrier == "none":
                exc.replaced_root = None
            elif hasattr(exc, "replaced_root"):
                del exc.replaced_root
            raise
    monkeypatch.setattr(si, "upgrade_specialist", strip)
    if live == "no_agent":
        dict.pop(world.h.runtime.agents, "mtg")
    world.swap_fails(True)
    world.cleanup_fails(restart)
    out = await world.call(world.fx.insp2, {"j": "plain-j"}, ["k"])
    assert out["kind"] == "upgrade_kept_new_version", out
    assert world.root() != world.a
    assert world.h.runtime.agents.writes == []
    _assert_told(out, UNKNOWN, restart=restart)


# ── Regression controls added with the fix (green; not red cases) ───────────


def test_p6_the_four_argument_call_still_builds_the_ruled_detail(monkeypatch):
    """P-6: `_kept_new_version_error(txn, journal, slug, exc)` positionally, as
    the #1095 pin's double calls it: the ruled detail, nothing carried."""
    from pathlib import Path
    from types import SimpleNamespace

    completed = []
    monkeypatch.setattr(journal, "complete", completed.append)
    err = si._kept_new_version_error(
        SimpleNamespace(finish_forward=lambda: None), Path("/nonexistent/journal"),
        "mtg", OSError("injected owned swap failure"))
    assert completed == [Path("/nonexistent/journal")]
    assert err.kind == "upgrade_kept_new_version" and err.restart_first is False
    assert err.detail == _detail(RULED, restart=False)
    assert err.replaced_root is None
    assert err.details_by_live == {
        "matched": _detail(RULED, restart=False), "other": _detail(NOT_NEW, restart=False),
        "unknown": _detail(UNKNOWN, restart=False)}


def test_the_tool_and_the_library_carry_the_same_three_clauses():
    """The outcome and the detail choose from the same clauses, and the ruled
    outcome constants keep their exact value."""
    import tools
    assert tools._KEPT_IN_USE_CLAUSES == si._KEPT_IN_USE_CLAUSES == {
        "matched": RULED, "other": NOT_NEW, "unknown": UNKNOWN}
    assert tools._KEPT_NOT_ACTIVE_OUTCOME == RULED_OUTCOME
    assert tools._KEPT_NOT_ACTIVE_RESTART_OUTCOME == RULED_RESTART_OUTCOME


# ── #1298: a concurrent reload that loads the kept version ──────────────────

RUNNING_NEW = ", and when this upgrade returned the specialist was running the new version"
FALSE_WHEN_NEW = ("not active yet", "has not loaded it", "before Casa loaded it",
                  "was not running the new version")
# The exact texts for that state, written out independently of the helpers above.
NEW_OUTCOME = (
    "the upgrade did not finish: the new version is kept on disk, and when this "
    "upgrade returned the specialist was running the new version. Re-running the "
    "same upgrade finishes it.")
NEW_RESTART_OUTCOME = (
    "the upgrade did not finish: the new version is kept on disk, and when this "
    "upgrade returned the specialist was running the new version. Finishing the "
    "version it replaced failed too, so further changes to this specialist are "
    "refused until Casa restarts: restart Casa, then re-run the upgrade.")
NEW_DETAIL = (
    "'mtg': the upgrade did not finish. The new version is kept — the version it "
    "replaced cannot be restored whole, because a setting it kept as a plain value "
    "is now secret — but the upgrade then failed (OSError: injected owned swap "
    "failure), and when this upgrade returned the specialist was running the new "
    "version, and its owned plugins may still be the previous version's. Re-running "
    "the same upgrade finishes it. Nothing was deleted")
NEW_RESTART_DETAIL = (
    "'mtg': the upgrade did not finish. The new version is kept — the version it "
    "replaced cannot be restored whole, because a setting it kept as a plain value "
    "is now secret — but the upgrade then failed (OSError: injected owned swap "
    "failure), and when this upgrade returned the specialist was running the new "
    "version. Finishing the retained prior failed too (injected cleanup failure); "
    "its undo record is kept, so further changes to this specialist are refused "
    "until Casa restarts and finishes it: restart Casa, then re-run the upgrade. "
    "Nothing was deleted")


class _ConcurrentReload:
    """A real `reload.dispatch("agent")` for `mtg`, run on the event loop from the
    library's worker thread while `upgrade_specialist` is still running — what an
    unfenced `casa_reload` / `POST /admin/reload` scope `agent` does during the
    thread hop. The loader stand-in binds what the specialist loader binds,
    `active.yaml`'s binding (`activate_binding_for_config`: `InstanceDir(...)
    .active()`, `binding = active_tuple.binding`), never a hand-assigned root."""

    def __init__(self, world, loop):
        from personality_binding import InstanceDir

        self.world, self.loop = world, loop
        self.results: list = []
        self.disk_roots: list = []

        def load(*a, **kw):
            cfg = harness._cfg(None)
            cfg.binding = InstanceDir(world.fx.slug_dir).active().binding
            return cfg
        world.mp.setattr("agent_loader.load_agent_from_dir", load)

    def run(self):
        import asyncio
        import reload as reload_mod
        self.disk_roots.append(self.world.root())
        fut = asyncio.run_coroutine_threadsafe(
            reload_mod.dispatch("agent", runtime=self.world.h.runtime, role="mtg"), self.loop)
        self.results.append(fut.result(timeout=30))


async def _b_loaded(world) -> "_ConcurrentReload":
    """A → B, completed through the real handler (the sequencer's reload loads B
    through the loader stand-in), then C staged: B is installed and live."""
    import asyncio
    racer = _ConcurrentReload(world, asyncio.get_running_loop())
    out = await world.call(world.fx.insp2, {"j": "plain-j"}, ["k"])
    assert out.get("ok") is True, out
    b = world.root()
    assert world.h.runtime.agents["mtg"].config.binding.component_root == b
    return racer


@pytest.mark.parametrize("restart", [False, True], ids=["rerun", "restart"])
async def test_1298_a_concurrent_reload_that_loads_the_kept_version_is_told_as_running(
        world, restart):
    """#1298: B live, B → C library-kept (owned swap fails after C is committed);
    while the library is still in its worker thread a real reload of the
    specialist loads C. Both fields say that when this upgrade returned the
    specialist was running the new version, and none of the not-loaded phrases."""
    racer = await _b_loaded(world)
    b = world.root()
    h = world.h
    insp = world.stage_c()
    swaps = []

    def fail_swap_after_reload(**kw):
        swaps.append(world.root())
        racer.run()
        raise OSError("injected owned swap failure")
    world.mp.setattr(plugin_registry, "apply_owned_swap", fail_swap_after_reload)
    world.cleanup_fails(restart)
    writes, constructions, loads = len(h.runtime.agents.writes), h.constructions, h.registry.loads
    raised = len(world.raised)
    out = await world.call(insp, {}, ["k", "j"])
    world.swap_fails(False)
    c = world.root()

    # The interleaving happened as specified, and nothing else did.
    assert b != c
    assert swaps == [c] and racer.disk_roots == [c]
    assert len(racer.results) == 1 and racer.results[0].get("status") == "ok", racer.results
    assert len(h.runtime.agents.writes) - writes == 1
    assert h.constructions - constructions == 1
    assert h.registry.loads - loads == 1
    assert len(world.raised) - raised == 1
    exc = world.raised[-1]
    assert exc.kind == "upgrade_kept_new_version"
    assert "injected owned swap failure" in str(exc.__cause__)
    assert world.prior_root() == b
    assert h.runtime.agents["mtg"].config.binding.component_root == c
    assert world.finish_calls == 2
    assert out["kind"] == "upgrade_kept_new_version", out
    assert out["kept_new_version"] is True

    for field in ("outcome", "detail"):
        text = out[field]
        assert (text.count(RUNNING_NEW), *(text.count(p) for p in FALSE_WHEN_NEW)) == (
            1, 0, 0, 0, 0), (field, text)
    assert out["outcome"] == (NEW_RESTART_OUTCOME if restart else NEW_OUTCOME)
    assert out["detail"] == (NEW_RESTART_DETAIL if restart else NEW_DETAIL)


@pytest.mark.parametrize("restart", [False, True], ids=["rerun", "restart"])
async def test_1298_control_a_reload_before_the_commit_keeps_the_ruled_text(
        world, restart, monkeypatch):
    """Control: the concurrent reload lands BEFORE C is committed, so it loads B
    again; C is then committed and kept with no further reload. Live B is the
    replaced version: the ruled matched text, byte for byte."""
    racer = await _b_loaded(world)
    b = world.root()
    h = world.h
    insp = world.stage_c()
    core = si._upgrade_core

    def reload_then_commit(**kw):
        racer.run()
        return core(**kw)
    monkeypatch.setattr(si, "_upgrade_core", reload_then_commit)
    world.swap_fails(True)
    world.cleanup_fails(restart)
    writes = len(h.runtime.agents.writes)
    out = await world.call(insp, {}, ["k", "j"])
    world.swap_fails(False)
    c = world.root()

    assert b != c and racer.disk_roots == [b]
    assert len(racer.results) == 1 and racer.results[0].get("status") == "ok", racer.results
    assert len(h.runtime.agents.writes) - writes == 1
    assert h.runtime.agents["mtg"].config.binding.component_root == b
    assert out["kind"] == "upgrade_kept_new_version", out
    assert out["outcome"] == (RULED_RESTART_OUTCOME if restart else RULED_OUTCOME)
    assert out["detail"] == _detail(RULED, restart=restart)
