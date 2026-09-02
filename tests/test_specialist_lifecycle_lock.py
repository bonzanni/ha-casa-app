"""#810 (INV-SPEC-011, the transaction boundary) — every specialist-generation
transaction runs whole under one lifecycle lock.

Install, upgrade, rollback, uninstall and a specialist persona override are
each ONE transaction: at every release of the lifecycle lock the active tuple,
the active owned-plugins sidecar and the registry's owned rows are one
generation. The lock is taken by the LIBRARY function in the worker thread
around its whole body — sampling, journal begin, registry swap, tuple and
sidecar commit — so a cancelled tool handler cannot release it mid-section.
The tool layer additionally holds its own plugin-tools lock over the whole
specialist arm of ``persona_apply``, and the override is journaled.

Every case runs the REAL lifecycle functions over the shipped bundle fixtures
on ``tmp_path``; the two-thread interleavings use a two-way handshake at a
named seam and assert destination-specific COUNTS (commits, publications,
refusal kinds, journal states) — never ``thread.is_alive()`` and never a fake
transaction body.

Specified externally (sol, red-case round, MODE: SPECIFY); acceptance runs
against the tests-only commit that carries this file.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path

import pytest

import plugin_registry
import specialist_bundle_journal
import specialist_install
from broker_helpers import wait_until
from test_specialist_rollback_owned_generation import (
    _SLUG, _install, _override, _prep_bundle, _read_doc, _rollback, _sidecar_paths,
    _slug_dir, _upgrade, generation_rows, registry_rows,
)
from test_wholebranch_security_fixes import (
    _load_specialist_persona_role, _publish_installed_copy,
)

_PAUSE_TIMEOUT = 10.0
# How long B is given to commit while A is paused: pre-fix B's whole local
# transaction completes well inside this; post-fix B is blocked on the
# lifecycle lock for exactly this long, and the count it produces is 0.
_B_WINDOW = 1.5


@pytest.fixture(autouse=True)
def _fresh_registry_snapshot(tmp_path):
    plugin_registry.reload_snapshot(registry_path=tmp_path / "snap-registry.json",
                                    store_root=tmp_path / "snap-store")
    yield


def _lifecycle_lock_modules():
    """The modules that publish the lifecycle lock, or [] when the base has none
    — so a test fails on its COUNT (0 releases), never on an AttributeError."""
    import personality_binding
    import specialist_materialize
    return [m for m in (personality_binding, specialist_materialize)
            if getattr(m, "SPECIALIST_LIFECYCLE_LOCK", None) is not None]


class _RecordingLock:
    """Forwards to the real lock; runs ``on_release`` on every outer release."""

    def __init__(self, real, on_release) -> None:
        self._real = real
        self._on_release = on_release
        self.releases = 0

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *exc):
        self.releases += 1
        try:
            self._on_release()
        finally:
            return self._real.__exit__(*exc)

    def acquire(self, *a, **k):
        return self._real.acquire(*a, **k)

    def release(self):
        self.releases += 1
        self._on_release()
        return self._real.release()

    def locked(self):
        return self._real.locked()


def _sha_or_none(path: Path) -> "str | None":
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _triple(slug_dir: Path, reg_path: Path) -> tuple:
    active_sidecar, _ = _sidecar_paths(slug_dir)
    return (_sha_or_none(slug_dir / "active.yaml"), _sha_or_none(active_sidecar),
            registry_rows(reg_path))


def _install_recording_lock(monkeypatch, on_release) -> "_RecordingLock | None":
    mods = _lifecycle_lock_modules()
    if not mods:
        return None
    wrapper = _RecordingLock(getattr(mods[0], "SPECIALIST_LIFECYCLE_LOCK"), on_release)
    for m in mods:
        monkeypatch.setattr(m, "SPECIALIST_LIFECYCLE_LOCK", wrapper)
    return wrapper


# ---------------------------------------------------------------------------
# T1 — every lifecycle-lock release is generation-coherent
# ---------------------------------------------------------------------------


def test_t1_every_lifecycle_lock_release_is_one_generation(tmp_path: Path, monkeypatch) -> None:
    """T1: install G1 → upgrade G2 → override G2O → rollback (to G2) → uninstall,
    the lifecycle lock wrapped so each outer release records the triple
    ``(sha256(active.yaml), sha256(owned-plugins.yaml), registry rows)``: five
    releases, five transitions, zero mixed releases, and the release triples
    are exactly the settled post-operation triples. RED at base: there is no
    lifecycle lock, so the release count is 0."""
    releases: list[tuple] = []
    holder = {}

    def _record():
        releases.append(_triple(holder["slug_dir"], holder["reg"]))

    _install_recording_lock(monkeypatch, _record)

    # The fixture's paths are fixed by tmp_path, so the recorder can know them
    # before the first transaction's release.
    holder["slug_dir"] = tmp_path / "specialists" / _SLUG
    holder["reg"] = tmp_path / "registry.json"
    ctx = _install(tmp_path, monkeypatch, ["mtg"])
    slug_dir, reg = _slug_dir(ctx), ctx.kw["registry_path"]
    assert (slug_dir, reg) == (holder["slug_dir"], holder["reg"])
    g1 = _triple(slug_dir, reg)

    _upgrade(tmp_path, monkeypatch, ctx, ["mtg", "extra"], root="v2", ref="v2",
             sha="b" * 40, version="0.2.0")
    g2 = _triple(slug_dir, reg)
    _override(tmp_path, monkeypatch, ctx)
    g2o = _triple(slug_dir, reg)
    _rollback(ctx)
    g2_again = _triple(slug_dir, reg)
    txn = specialist_install.uninstall_specialist(
        slug=_SLUG, bundle=True, acks=ctx.acks, specialists_dir=ctx.kw["specialists_dir"],
        agents_specialists_dir=ctx.kw["agents_specialists_dir"], registry_path=reg,
        ops_dir=ctx.kw["ops_dir"])
    specialist_bundle_journal.complete(txn.journal_path)
    empty = _triple(slug_dir, reg)

    declared = [g1, g2, g2o, g2_again, empty]
    transitions = sum(1 for i, t in enumerate(releases)
                      if t != (releases[i - 1] if i else (None, None, ())))
    mixed = sum(1 for t in releases if t not in declared)

    assert len(releases) == 5
    assert transitions == 5
    assert mixed == 0
    assert releases == [g1, g2, g2o, g2_again, empty]
    assert g2_again == g2
    assert empty == (None, None, ())


# ---------------------------------------------------------------------------
# T2 — deterministic two-thread interleavings at named seams
# ---------------------------------------------------------------------------


class _Handshake:
    """Two-way handshake: the paused thread signals ``entered`` and waits on
    ``release``; the test waits for ``entered`` and decides when to release."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.entered_count = 0

    def pause(self) -> None:
        self.entered_count += 1
        self.entered.set()
        self.release.wait(_PAUSE_TIMEOUT)


class _Counters:
    """Destination-specific counts attributed to the thread named 'B'."""

    def __init__(self, monkeypatch) -> None:
        from personality_binding import InstanceDir
        self.b_commits = 0
        self.b_publishes = 0
        real_commit = InstanceDir.commit_desired_to_active
        real_publish = specialist_install._publish_owned_plugins
        me = self

        def _commit(self_dir):
            if threading.current_thread().name == "B":
                me.b_commits += 1
            return real_commit(self_dir)

        def _publish(*a, **k):
            if threading.current_thread().name == "B":
                me.b_publishes += 1
            return real_publish(*a, **k)

        monkeypatch.setattr(InstanceDir, "commit_desired_to_active", _commit)
        monkeypatch.setattr(specialist_install, "_publish_owned_plugins", _publish)


class _Outcome:
    def __init__(self) -> None:
        self.successes = 0
        self.refusal_kinds: list[str] = []
        self.errors: list[BaseException] = []

    def run(self, fn):
        try:
            fn()
            self.successes += 1
        except specialist_install.SpecialistInstallError as exc:
            self.refusal_kinds.append(exc.kind)
        except BaseException as exc:  # noqa: BLE001 — recorded, asserted below
            self.errors.append(exc)

    @property
    def terminal(self) -> int:
        return self.successes + len(self.refusal_kinds) + len(self.errors)


def _pause_before(monkeypatch, name: str, hs: _Handshake, *, only_op: "str | None" = None):
    """Wrap ``specialist_install.<name>`` (or the journal's ``begin`` when
    ``name == 'begin'``) so the FIRST matching call pauses on the handshake
    before running the real function."""
    if name == "begin":
        real = specialist_bundle_journal.begin
        fired = {"done": False}

        def _wrapped(op, slug, **k):
            if (only_op is None or op == only_op) and not fired["done"]:
                fired["done"] = True
                path = real(op, slug, **k)
                hs.pause()          # AFTER begin, before the tuple commit
                return path
            return real(op, slug, **k)

        monkeypatch.setattr(specialist_bundle_journal, "begin", _wrapped)
        return
    real = getattr(specialist_install, name)
    fired = {"done": False}

    def _wrapped(*a, **k):
        if not fired["done"]:
            fired["done"] = True
            hs.pause()
        return real(*a, **k)

    monkeypatch.setattr(specialist_install, name, _wrapped)


def _mixed(slug_dir: Path, reg_path: Path, rows_by_root: dict) -> int:
    """1 when the released state is not one generation: the registry's owned
    rows differ from the active sidecar's, or a root whose owned set is known
    carries another set."""
    from personality_binding import InstanceDir
    active = InstanceDir(slug_dir).active()
    if active is None:
        return 0 if registry_rows(reg_path) == () else 1
    active_sidecar, _ = _sidecar_paths(slug_dir)
    sidecar = generation_rows(_read_doc(active_sidecar))
    if registry_rows(reg_path) != sidecar:
        return 1
    known = rows_by_root.get(active.root)
    return 0 if known is None or known == sidecar else 1


def _direct_upgrade_kw(tmp_path: Path, monkeypatch, ctx):
    """A plugin-less v3 inspected in upgrade mode, for the DIRECT (receipt-less)
    library arm — a sourced-dependency component would be refused there."""
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    up = _prep_bundle(tmp_path, monkeypatch, [], root="v3", ref="v3", sha="c" * 40,
                      base_ctx=ctx, version="0.3.0")
    # B's consent lives in its OWN ledger: a bundle uninstall (arm 4) retires
    # every ack of the slug, and B's refusal must then be the missing ACTIVE,
    # not the missing consent.
    acks = SpecialistInstallAckStore(path=tmp_path / "acks-b.json")
    insp = up.inspection
    acks.record(identity=install_consent_identity(
        component_id=insp.component_id, version=insp.version, root_digest=insp.root_digest,
        slug=insp.slug, receipt_digest=insp.receipt_digest),
        component_id=insp.component_id, version=insp.version,
        component_checksum=insp.root_digest, slug=insp.slug,
        receipt_digest=insp.receipt_digest)
    return dict(slug=_SLUG, inspection=up.inspection, receipt=None, config={},
                secret_names_provided=frozenset(), acks=acks,
                specialists_dir=ctx.kw["specialists_dir"],
                agents_specialists_dir=ctx.kw["agents_specialists_dir"])


def _apply_override_tolerant(**kw):
    """Call ``apply_persona_override`` with the journaled-bundle keywords; at a
    base whose signature lacks them, retry without — so the arm fails on its
    COUNTS, not on the signature."""
    from persona_install import apply_persona_override
    try:
        return apply_persona_override(**kw)
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        stripped = {k: v for k, v in kw.items()
                    if k not in ("bundle", "acks", "registry_path", "ops_dir")}
        return apply_persona_override(**stripped)


@pytest.mark.parametrize("arm", ["upgrade_vs_override", "rollback_vs_upgrade",
                                 "override_vs_rollback", "uninstall_vs_upgrade",
                                 "direct_uninstall_vs_upgrade"])
def test_t2_a_paused_transaction_fences_the_other_thread(tmp_path: Path, monkeypatch,
                                                         arm: str) -> None:
    """T2: A is paused at a named seam INSIDE its transaction; B is a second
    real lifecycle call on another thread. While A is paused, B's commit and
    publication counts are 0; after A is released both reach a terminal
    outcome and no release is mixed. Arms 1-3 both succeed; arms 4-5 (A is an
    uninstall) see B refuse ``no_active_tuple`` with 0 commits. RED at base:
    arms 1, 2, 4 and 5 let B commit between A's separate materialize-lock
    scopes (arm 5: A samples before entering its removal lock); arm 3 has no
    journaled override, so its seam is never entered."""
    from personality_binding import InstanceDir

    ctx = _install(tmp_path, monkeypatch, [] if arm == "direct_uninstall_vs_upgrade" else ["mtg"])
    reg = ctx.kw["registry_path"]
    slug_dir = _slug_dir(ctx)
    rows_by_root = {InstanceDir(slug_dir).active().root: registry_rows(reg)}
    if arm in ("rollback_vs_upgrade", "override_vs_rollback"):
        _upgrade(tmp_path, monkeypatch, ctx, ["mtg", "extra"], root="v2", ref="v2",
                 sha="b" * 40, version="0.2.0")
        rows_by_root[InstanceDir(slug_dir).active().root] = registry_rows(reg)
    hs = _Handshake()
    counters = _Counters(monkeypatch)
    a_out, b_out = _Outcome(), _Outcome()

    if arm == "upgrade_vs_override":
        up = _prep_bundle(tmp_path, monkeypatch, ["mtg", "extra"], root="v2", ref="v2",
                          sha="b" * 40, base_ctx=ctx, version="0.2.0")
        _pause_before(monkeypatch, "_publish_owned_plugins", hs)
        specialists_dir = ctx.kw["specialists_dir"]
        persona, role = _load_specialist_persona_role(specialists_dir, _SLUG)
        _publish_installed_copy(persona, specialists_dir, _SLUG, tmp_path, monkeypatch)
        a_fn = lambda: specialist_install.upgrade_specialist(**up.kw)  # noqa: E731
        b_fn = lambda: _apply_override_tolerant(  # noqa: E731
            target_role_id=f"specialist:{_SLUG}", persona=persona, role=role,
            instance_dir_root=slug_dir, candidate_validator=lambda p, b: None)
    elif arm == "rollback_vs_upgrade":
        _pause_before(monkeypatch, "_rollback_core", hs)
        kw3 = _direct_upgrade_kw(tmp_path, monkeypatch, ctx)
        a_fn = lambda: _rollback(ctx)  # noqa: E731
        b_fn = lambda: specialist_install.upgrade_specialist(**kw3)  # noqa: E731
    elif arm == "override_vs_rollback":
        _pause_before(monkeypatch, "begin", hs, only_op="persona_override")
        specialists_dir = ctx.kw["specialists_dir"]
        persona, role = _load_specialist_persona_role(specialists_dir, _SLUG)
        _publish_installed_copy(persona, specialists_dir, _SLUG, tmp_path, monkeypatch)
        a_fn = lambda: _apply_override_tolerant(  # noqa: E731
            target_role_id=f"specialist:{_SLUG}", persona=persona, role=role,
            instance_dir_root=slug_dir, candidate_validator=lambda p, b: None,
            bundle=True, acks=ctx.acks, registry_path=reg, ops_dir=ctx.kw["ops_dir"])
        b_fn = lambda: _rollback(ctx)  # noqa: E731
    else:
        _pause_before(monkeypatch, "_uninstall_core", hs)
        kw3 = _direct_upgrade_kw(tmp_path, monkeypatch, ctx)
        bundle = arm == "uninstall_vs_upgrade"
        a_fn = lambda: specialist_install.uninstall_specialist(  # noqa: E731
            slug=_SLUG, bundle=bundle, acks=ctx.acks,
            specialists_dir=ctx.kw["specialists_dir"],
            agents_specialists_dir=ctx.kw["agents_specialists_dir"],
            registry_path=reg, ops_dir=ctx.kw["ops_dir"])
        b_fn = lambda: specialist_install.upgrade_specialist(**kw3)  # noqa: E731

    a = threading.Thread(target=a_out.run, args=(a_fn,), name="A")
    b = threading.Thread(target=b_out.run, args=(b_fn,), name="B")
    a.start()
    entered = hs.entered.wait(_PAUSE_TIMEOUT)
    assert hs.entered_count == 1, (entered, a_out.errors)
    b.start()
    b.join(_B_WINDOW)
    b_commits_while_paused = counters.b_commits
    b_publishes_while_paused = counters.b_publishes
    hs.release.set()
    a.join(_PAUSE_TIMEOUT)
    b.join(_PAUSE_TIMEOUT)

    assert b_commits_while_paused == 0
    assert b_publishes_while_paused == 0
    assert a_out.errors == [] and b_out.errors == []
    assert a_out.terminal + b_out.terminal == 2
    assert _mixed(slug_dir, reg, rows_by_root) == 0
    if arm in ("upgrade_vs_override", "rollback_vs_upgrade", "override_vs_rollback"):
        assert a_out.successes == 1
        assert b_out.successes == 1
    else:
        assert a_out.successes == 1
        assert b_out.refusal_kinds == ["no_active_tuple"]
        assert counters.b_commits == 0
        assert counters.b_publishes == 0


# ---------------------------------------------------------------------------
# T3 — a stale caller role is refused inside the library boundary
# ---------------------------------------------------------------------------


def test_t3_a_stale_caller_role_is_refused_at_the_library_boundary(
        tmp_path: Path, monkeypatch) -> None:
    """T3: install under opus, materialize and keep the opus role, flip the
    live resolution to sonnet WITHOUT touching the active tuple, then apply a
    specialist override with the stale opus role: refused
    ``concurrent_mutation`` with 0 tuple writes, 0 sidecar writes, 0 journals
    and the tree byte-identical. RED at base: the function checks only whether
    ``active_before`` changed while it ran, and commits an override bound to
    the stale opus role."""
    import personality_binding
    from test_specialist_binding_rederive import _cas_role, _install as _install_ha, _tree_bytes
    from personality_binding import InstanceDir

    specialists_root, _agents_root, _ = _install_ha(tmp_path, monkeypatch)
    instance_path = specialists_root / _SLUG
    persona, _ = _load_specialist_persona_role(specialists_root, _SLUG)
    _publish_installed_copy(persona, specialists_root, _SLUG, tmp_path, monkeypatch)
    opus_role = _cas_role(specialists_root, InstanceDir(instance_path).active())
    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    assert _cas_role(specialists_root, InstanceDir(instance_path).active()).checksum \
        != opus_role.checksum
    tree_before = _tree_bytes(instance_path)

    counts = {"tuple": 0, "sidecar": 0, "journal": 0}
    real_atomic = personality_binding.atomic_write_instance_tuple
    real_write_doc = personality_binding.write_owned_plugins
    real_begin = specialist_bundle_journal.begin
    monkeypatch.setattr(personality_binding, "atomic_write_instance_tuple",
                        lambda p, t: (counts.__setitem__("tuple", counts["tuple"] + 1),
                                      real_atomic(p, t))[1])
    monkeypatch.setattr(personality_binding, "write_owned_plugins",
                        lambda p, d: (counts.__setitem__("sidecar", counts["sidecar"] + 1),
                                      real_write_doc(p, d))[1])
    monkeypatch.setattr(specialist_bundle_journal, "begin",
                        lambda *a, **k: (counts.__setitem__("journal", counts["journal"] + 1),
                                         real_begin(*a, **k))[1])

    with pytest.raises(specialist_install.SpecialistInstallError) as raised:
        _apply_override_tolerant(
            target_role_id=f"specialist:{_SLUG}", persona=persona, role=opus_role,
            instance_dir_root=instance_path, candidate_validator=lambda p, b: None)

    assert raised.value.kind == "concurrent_mutation"
    assert counts["tuple"] == 0
    assert counts["sidecar"] == 0
    assert counts["journal"] == 0
    assert _tree_bytes(instance_path) == tree_before


# ---------------------------------------------------------------------------
# T4-T5 — the tool layer: the plugin-tools lock over the specialist arm, and a
# cancelled handler that cannot release the library transaction early
# ---------------------------------------------------------------------------


def _fresh_plugin_tools_lock(monkeypatch):
    import tools as tools_mod
    lock = asyncio.Lock()
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", lock)
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK_OWNER", None)
    return lock


@pytest.mark.asyncio
async def test_t4_persona_apply_holds_the_plugin_tools_lock_over_specialist_discovery(
        tmp_path: Path, monkeypatch) -> None:
    """T4: with the real plugin-tools lock HELD, one specialist ``persona_apply``
    and one resident ``persona_apply`` are started: before the release the
    specialist arm has loaded the index 0 times, materialized 0 specialist
    roles and produced 0 results, while the resident arm has produced 1; after
    the release the specialist counts are 1/1/1. RED at base: the specialist
    arm loads the index and materializes the role before it ever reaches the
    lock (0 → 1 while the lock is still held)."""
    import role_slot
    import specialist_registry
    from test_specialist_binding_rederive import _install as _install_ha
    from test_tools_specialist_install import _payload
    from tools import persona_apply

    specialists_root, _agents_root, _ = _install_ha(tmp_path, monkeypatch)
    # The specialist's own pack, published where the tool resolves it; the
    # resident pack is a different id so the two arms resolve independently.
    specialist_persona, _ = _load_specialist_persona_role(specialists_root, _SLUG)
    _publish_installed_copy(specialist_persona, specialists_root, _SLUG, tmp_path, monkeypatch)
    config_root = Path(tmp_path / "config-root")
    monkeypatch.setenv("CASA_CONFIG_DIR", str(config_root))
    from test_persona_install import _write_persona_repo
    ellen_dir = config_root / "personas" / "casa/ellen" / "0.1.0"
    ellen_dir.mkdir(parents=True, exist_ok=True)
    _write_persona_repo(ellen_dir, persona_id="casa/ellen", version="0.1.0")
    monkeypatch.setenv("CASA_BINDINGS_DIR", str(tmp_path / "bindings-root"))

    from specialist_install import cas_store_dir, parse_component_root
    from personality_binding import InstanceDir
    _, _, checksum = parse_component_root(InstanceDir(specialists_root / _SLUG).active().root)
    cas_dir = cas_store_dir(checksum, store_root=specialists_root / "store")

    counts = {"index_loads": 0, "specialist_roles": 0}

    class _CountingIndex:
        def __init__(self, *a, **k) -> None:
            pass

        def load(self) -> None:
            counts["index_loads"] += 1

        def installed_component_role_dirs(self) -> dict:
            return {_SLUG: cas_dir}

    real_materialize = role_slot.materialize_role

    def _counting_materialize(*, source, options):
        role = real_materialize(source=source, options=options)
        if role.kind == "specialist":
            counts["specialist_roles"] += 1
        return role

    monkeypatch.setattr(specialist_registry, "InstalledSpecialistIndex", _CountingIndex)
    monkeypatch.setattr(role_slot, "materialize_role", _counting_materialize)
    lock = _fresh_plugin_tools_lock(monkeypatch)

    results = {"specialist": [], "resident": []}

    async def _run(kind, args):
        results[kind].append(_payload(await persona_apply.handler(args)))

    await lock.acquire()
    try:
        specialist_task = asyncio.create_task(_run("specialist", {
            "target_role_id": f"specialist:{_SLUG}",
            "persona_id": specialist_persona.persona_id,
            "persona_version": specialist_persona.version}))
        resident_task = asyncio.create_task(_run("resident", {
            "target_role_id": "resident:assistant",
            "persona_id": "casa/ellen", "persona_version": "0.1.0"}))
        await wait_until(lambda: len(results["resident"]) == 1)
        assert results["resident"][0].get("ok") is True, results["resident"][0]
        index_loads_held = counts["index_loads"]
        specialist_roles_held = counts["specialist_roles"]
        specialist_results_held = len(results["specialist"])
    finally:
        lock.release()
    await asyncio.wait_for(asyncio.gather(specialist_task, resident_task), timeout=30)

    assert index_loads_held == 0
    assert specialist_roles_held == 0
    assert specialist_results_held == 0
    assert counts["index_loads"] == 1
    assert counts["specialist_roles"] == 1
    assert len(results["specialist"]) == 1


@pytest.mark.asyncio
async def test_t5_a_cancelled_handler_cannot_release_the_library_transaction_early(
        tmp_path: Path, monkeypatch) -> None:
    """T5: a specialist ``persona_apply`` whose worker is paused INSIDE the
    lifecycle lock after its journal ``begin``; the handler task is cancelled
    (count 1); a direct rollback B started on another thread commits 0 times
    while the worker is paused; after the worker is released it completes
    (1), its journal is terminal (completed 1, in progress 0), B then commits
    exactly once, and boot reconciliation performs 0 rollbacks. RED at base:
    the override is unjournaled (no ``persona_override`` journal is ever begun
    or completed) and there is no whole-body lifecycle lock."""
    import persona_install
    import specialist_registry
    from personality_binding import InstanceDir
    from test_specialist_binding_rederive import _install as _install_ha, _upgrade as _upgrade_ha
    from test_specialist_bundle_commit import _write_registry
    from tools import persona_apply

    specialists_root, agents_root, _ = _install_ha(tmp_path, monkeypatch)
    upgraded = _upgrade_ha(tmp_path, specialists_root, agents_root, version="0.2.0")
    assert upgraded.state == "active"
    instance_path = specialists_root / _SLUG
    persona, _ = _load_specialist_persona_role(specialists_root, _SLUG)
    _publish_installed_copy(persona, specialists_root, _SLUG, tmp_path, monkeypatch)
    from specialist_install import cas_store_dir, parse_component_root
    _, _, checksum = parse_component_root(InstanceDir(instance_path).active().root)
    cas_dir = cas_store_dir(checksum, store_root=specialists_root / "store")
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [])
    ops_dir = tmp_path / "ops"
    from specialist_install_consent import SpecialistInstallAckStore
    acks = SpecialistInstallAckStore(path=tmp_path / "acks-override.json")

    class _Index:
        def __init__(self, *a, **k) -> None:
            pass

        def load(self) -> None:
            pass

        def installed_component_role_dirs(self) -> dict:
            return {_SLUG: cas_dir}

    monkeypatch.setattr(specialist_registry, "InstalledSpecialistIndex", _Index)
    _fresh_plugin_tools_lock(monkeypatch)

    hs = _Handshake()
    _pause_before(monkeypatch, "begin", hs, only_op="persona_override")
    completed: list[str] = []
    real_complete = specialist_bundle_journal.complete

    def _counting_complete(path):
        op = json.loads(Path(path).read_text()).get("op")
        real_complete(path)
        completed.append(op)

    monkeypatch.setattr(specialist_bundle_journal, "complete", _counting_complete)

    worker_done = {"count": 0}
    real_apply = persona_install.apply_persona_override

    def _bound_apply(**kw):
        # The handler names the production instance root; bind it to this
        # test's tree and hand the library the journaled-bundle keywords.
        kw["instance_dir_root"] = instance_path
        try:
            return real_apply(**kw, bundle=True, acks=acks, registry_path=registry_path,
                              ops_dir=ops_dir)
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
            return real_apply(**kw)
        finally:
            worker_done["count"] += 1

    monkeypatch.setattr(persona_install, "apply_persona_override", _bound_apply)

    from personality_binding import InstanceDir as _ID
    b_commits = {"count": 0}
    real_commit = _ID.commit_desired_to_active

    def _commit(self_dir):
        if threading.current_thread().name == "B":
            b_commits["count"] += 1
        return real_commit(self_dir)

    monkeypatch.setattr(_ID, "commit_desired_to_active", _commit)

    task = asyncio.create_task(persona_apply.handler({
        "target_role_id": f"specialist:{_SLUG}",
        "persona_id": persona.persona_id, "persona_version": persona.version}))
    await wait_until(lambda: hs.entered.is_set() or task.done(), timeout=_PAUSE_TIMEOUT)
    assert hs.entered_count == 1, (task.result() if task.done() else "not entered")

    task.cancel()
    cancellations = 0
    try:
        await task
    except asyncio.CancelledError:
        cancellations += 1

    b_out = _Outcome()
    b = threading.Thread(target=b_out.run, name="B", args=(
        lambda: specialist_install.rollback_specialist(
            slug=_SLUG, bundle=False, specialists_dir=specialists_root,
            agents_specialists_dir=agents_root),))
    b.start()
    await asyncio.to_thread(b.join, _B_WINDOW)
    b_commits_while_paused = b_commits["count"]

    hs.release.set()
    await wait_until(lambda: worker_done["count"] == 1, timeout=_PAUSE_TIMEOUT)
    await asyncio.to_thread(b.join, _PAUSE_TIMEOUT)

    in_progress = [p for p in ops_dir.glob("*.json")
                   if json.loads(p.read_text()).get("state") == "in-progress"] \
        if ops_dir.is_dir() else []
    actions = specialist_bundle_journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path, specialists_dir=specialists_root,
        acks_path=acks.path, agents_specialists_dir=agents_root)

    assert cancellations == 1
    assert b_commits_while_paused == 0
    assert worker_done["count"] == 1
    assert completed.count("persona_override") == 1
    assert len(in_progress) == 0
    assert b_out.errors == [] and b_out.refusal_kinds == []
    assert b_commits["count"] == 1
    assert sum(1 for a in actions if a.get("action") == "rolled_back") == 0
