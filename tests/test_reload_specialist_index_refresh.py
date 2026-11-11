"""Plan 2 review finding (no GH issue — found alongside GH #200): boot
(casa_core.py) constructs the process-wide ``InstalledSpecialistIndex`` and
publishes it via ``specialist_registry.set_active_installed_index``, but every
install/upgrade/rollback/uninstall + specialist-tier reload re-scan used to
build a FRESH local index (inside ``specialist_materialize.
current_specialist_roles_dir``, via ``reload.py``'s ``_specialist_roles_dir``
helper) and never republish it — so ``live_installed_specialist_slugs()`` /
``live_collision_slugs()`` / ``get_installed_instance()`` (admin/inspection,
Task-14 handlers) served BOOT-TIME state forever, never seeing a specialist
installed/upgraded/rolled-back/uninstalled after boot.

Fixed by having ``reload._specialist_roles_dir`` build the index itself
(mirroring casa_core.py's own boot sequence) and call
``set_active_installed_index`` before handing it to
``current_specialist_roles_dir``.

Autouse fixture saves/restores ``specialist_registry._active_index`` around
every test in THIS file (not repo-wide, not conftest.py — this is the only
file that drives a REAL, non-mock refresh of that global via a genuine
reload path) so it can never leak into other test files."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _restore_active_specialist_index():
    import specialist_registry as specialist_registry_mod

    original = specialist_registry_mod._active_index
    yield
    specialist_registry_mod._active_index = original


def _install_specialist(tmp_path: Path, *, slug: str) -> tuple[Path, Path]:
    """Commits a real, fully-installed specialist under a tmp_path tree —
    the same dance test_reload_specialist_roles_overlay.py uses — so this
    test exercises the genuine InstalledSpecialistIndex.load() path rather
    than a hand-rolled active.yaml."""
    from specialist_component import load_specialist_component
    from specialist_install import (
        InspectionResult, commit_specialist_install,
        compute_install_root_digest, resolve_dependency_closure,
    )
    from approval_values import record_specialist_ack
    from specialist_install_consent import SpecialistInstallAckStore
    from test_specialist_install import _write_component

    specialists_dir = tmp_path / "specialists"
    agents_specialists_dir = tmp_path / "config" / "agents" / "specialists"
    staged = _write_component(tmp_path / "staged", slug=slug)
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=staged,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    values = record_specialist_ack(acks, inspection)
    commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        approved_values=values,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )
    return specialists_dir, agents_specialists_dir


def test_specialist_tier_reload_refreshes_the_process_wide_installed_index(tmp_path):
    import specialist_registry as specialist_registry_mod
    from reload import _specialist_roles_dir

    specialists_dir, agents_specialists_dir = _install_specialist(tmp_path, slug="gizmo")

    # Simulate stale boot-time state: an index that predates the install
    # above (e.g. boot happened before "gizmo" was ever installed).
    specialist_registry_mod.set_active_installed_index(
        specialist_registry_mod.InstalledSpecialistIndex(str(tmp_path / "boot-time-empty")))
    assert "gizmo" not in specialist_registry_mod.live_installed_specialist_slugs()

    runtime = SimpleNamespace(
        config_dir=str(specialists_dir.parent),
        agents_dir=str(agents_specialists_dir.parent),
    )
    # Every specialist-tier reload call site funnels through this ONE helper.
    # Round-3 F3: the helper is now async (its lock+I/O runs in a worker thread
    # via asyncio.to_thread) so a concurrent install can't stall the event loop.
    asyncio.run(_specialist_roles_dir(runtime))

    assert "gizmo" in specialist_registry_mod.live_installed_specialist_slugs()
    assert specialist_registry_mod.get_installed_instance("gizmo") is not None


def test_leaked_index_from_a_reload_would_be_visible_without_the_fixture_restore():
    """Sanity check for the fixture itself: mutate the global directly (no
    reload involved) so the NEXT test can prove the autouse fixture actually
    restored the pristine value on unwind, not just left it around."""
    import specialist_registry as specialist_registry_mod

    specialist_registry_mod.set_active_installed_index(
        specialist_registry_mod.InstalledSpecialistIndex(str(Path("/nonexistent-marker-for-test"))))
    assert specialist_registry_mod._active_index is not None
    assert specialist_registry_mod._active_index._dir == Path("/nonexistent-marker-for-test")


def test_save_restore_keeps_the_next_test_clean():
    """Proves the autouse fixture undid the previous test's mutation: if it
    had leaked, `_active_index` here would still be the marker index the
    prior test installed."""
    import specialist_registry as specialist_registry_mod

    current_dir = getattr(specialist_registry_mod._active_index, "_dir", None)
    assert current_dir != Path("/nonexistent-marker-for-test")


# ---------------------------------------------------------------------------
# Whole-branch review round 6, F2 — index-publication coherence.
# `current_specialist_roles_dir(publish=True)` publishes the freshly-loaded index
# IN-LOCK (last-wins), not pre-lock in each reload worker. These pin: (a) last-wins
# semantics, (b) a coherent global under concurrent publishers, (c) publish=False
# (the default — test/ad-hoc callers) never mutating the global.
# ---------------------------------------------------------------------------


def test_publish_true_is_last_wins_across_sequential_resolves(tmp_path):
    import specialist_registry as specialist_registry_mod
    from specialist_materialize import current_specialist_roles_dir

    specialists_dir, agents_specialists_dir = _install_specialist(tmp_path, slug="gizmo")

    # Two DISTINCT index objects over the SAME on-disk tree — as two concurrent
    # reload workers would each build. The LAST publish (obj_b) must win.
    obj_a = specialist_registry_mod.InstalledSpecialistIndex(str(specialists_dir))
    obj_b = specialist_registry_mod.InstalledSpecialistIndex(str(specialists_dir))

    current_specialist_roles_dir(
        installed_index=obj_a, specialists_dir=specialists_dir,
        agents_specialists_dir=agents_specialists_dir, publish=True,
    )
    assert specialist_registry_mod._active_index is obj_a

    current_specialist_roles_dir(
        installed_index=obj_b, specialists_dir=specialists_dir,
        agents_specialists_dir=agents_specialists_dir, publish=True,
    )
    # Last lock-holder's freshly-loaded object is the published global — identity
    # AND content coherence (its in-lock load() populated the slug).
    assert specialist_registry_mod._active_index is obj_b
    assert "gizmo" in specialist_registry_mod.live_installed_specialist_slugs()


def test_two_concurrent_publish_resolves_leave_a_coherent_global(tmp_path):
    import threading

    import specialist_registry as specialist_registry_mod
    from specialist_materialize import current_specialist_roles_dir

    specialists_dir, agents_specialists_dir = _install_specialist(tmp_path, slug="gizmo")

    obj_a = specialist_registry_mod.InstalledSpecialistIndex(str(specialists_dir))
    obj_b = specialist_registry_mod.InstalledSpecialistIndex(str(specialists_dir))
    barrier = threading.Barrier(2)

    def _resolve(obj):
        barrier.wait()
        current_specialist_roles_dir(
            installed_index=obj, specialists_dir=specialists_dir,
            agents_specialists_dir=agents_specialists_dir, publish=True,
        )

    threads = [threading.Thread(target=_resolve, args=(o,)) for o in (obj_a, obj_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    # The in-lock publish serializes: the global is EXACTLY one of the two passed
    # objects — never a torn/third state — and it is fully loaded (content).
    assert specialist_registry_mod._active_index in (obj_a, obj_b)
    assert "gizmo" in specialist_registry_mod.live_installed_specialist_slugs()
    assert specialist_registry_mod.get_installed_instance("gizmo") is not None


def test_publish_false_default_never_mutates_the_global(tmp_path):
    import specialist_registry as specialist_registry_mod
    from specialist_materialize import current_specialist_roles_dir

    specialists_dir, agents_specialists_dir = _install_specialist(tmp_path, slug="gizmo")

    sentinel = specialist_registry_mod.InstalledSpecialistIndex(str(tmp_path / "sentinel-boot"))
    specialist_registry_mod.set_active_installed_index(sentinel)

    # Default publish=False — the ad-hoc/test caller must NOT touch the global.
    current_specialist_roles_dir(
        installed_index=specialist_registry_mod.InstalledSpecialistIndex(str(specialists_dir)),
        specialists_dir=specialists_dir,
        agents_specialists_dir=agents_specialists_dir,
    )
    assert specialist_registry_mod._active_index is sentinel
