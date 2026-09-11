"""Task 8: sourced plugin-dependency resolution, the seven-step validation
order, the trusted source receipt, and the new InspectionResult fields
(spec §1, §3.2.1)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import specialist_install
import specialist_receipt
from specialist_component import load_specialist_component
from specialist_install import (
    SpecialistInstallError,
    inspect_specialist_repo,
    resolve_dependency_closure,
)
from specialist_install_consent import render_install_consent_message
from specialist_registry import InstalledSpecialistIndex

try:
    from tests.specialist_fixtures import write_bundled_plugin, write_minimal_component
except ImportError:
    from specialist_fixtures import write_bundled_plugin, write_minimal_component

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_plugin_registry_snapshot(tmp_path):
    """`plugin_registry` keeps a process-global cached snapshot (`_snapshot`)
    that only lazily reloads when `None` — a stale snapshot left by an
    earlier test module (in the same pytest process) would otherwise leak
    into this module's `_manifest_name_collisions`/`_env_name_conflicts`
    real (non-monkeypatched) code paths. Point it at a fresh, empty,
    tmp_path-scoped registry before every test in this file for
    determinism — mirrors the explicit `reload_snapshot(...)` pattern every
    other plugin_registry-adjacent test module already uses."""
    import plugin_registry
    plugin_registry.reload_snapshot(registry_path=tmp_path / "registry.json",
                                    store_root=tmp_path / "store")
    yield


def _stub_resolve_and_fetch(component_root: Path):
    """Mirrors test_specialist_install.py's own stub: a monkeypatched
    `specialist_install.resolve_and_fetch` that just copies an already-built
    local component tree into `dest` and returns a fake 40-hex commit sha —
    never real network/git I/O."""

    def _stub(repo: str, ref: str, subdir: str, dest: Path, *, expected_revision: str | None = None) -> str:
        shutil.copytree(component_root, dest)
        return "a" * 40

    return _stub


def _add_dependency_row(manifest_path: Path, row: dict) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dependencies"].append(row)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _bundled_dep_row(identifier: str, digest: str, path: str) -> dict:
    return {
        "kind": "plugin/implementation", "identifier": identifier, "digest": digest,
        "source": {"type": "bundled", "path": path},
    }


def _inspect(tmp_path: Path, component_dir: Path, *, slug: str = "mtg-test",
             monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_dir))
    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()
    return inspect_specialist_repo(
        "org/repo", "main",
        staging_root=tmp_path / "staging",
        installed_index=index,
        receipts_dir=tmp_path / "receipts",
    )


# ---------------------------------------------------------------------------
# Happy path: bundled dep resolves, receipt round-trips.
# ---------------------------------------------------------------------------


def test_bundled_dep_resolves_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    digest = write_bundled_plugin(component_dir, "mtg")
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    result = _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)

    assert result.slug == "mtg-test"
    assert len(result.plugin_resolutions) == 1
    row = result.plugin_resolutions[0]
    assert row.scoped_name == "mtg-test.mtg"
    assert row.manifest_name == "mtg"
    assert row.identifier == "mtg"
    assert row.content_digest == digest
    assert row.source_type == "bundled"
    assert result.receipt_id != ""
    assert result.receipt_digest != ""

    loaded = specialist_receipt.load(result.receipt_id, receipts_dir=tmp_path / "receipts")
    assert loaded is not None
    assert loaded.receipt_digest == result.receipt_digest
    assert loaded.slug == "mtg-test"
    assert len(loaded.plugins) == 1
    assert loaded.plugins[0].content_digest == digest


# ---------------------------------------------------------------------------
# Digest mismatch.
# ---------------------------------------------------------------------------


def test_bundled_digest_mismatch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    write_bundled_plugin(component_dir, "mtg")
    wrong_digest = "sha256:" + "9" * 64
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", wrong_digest, "plugins/mtg"))

    with pytest.raises(SpecialistInstallError) as exc:
        _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)
    assert exc.value.kind == "dependency_unavailable"


# ---------------------------------------------------------------------------
# Prohibitions.
# ---------------------------------------------------------------------------


def test_bundled_triggers_prohibited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    digest = write_bundled_plugin(component_dir, "mtg", triggers="not-a-valid-shape")
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    with pytest.raises(SpecialistInstallError) as exc:
        _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)
    assert exc.value.kind == "bundled_triggers_unsupported"


def test_bundled_sysreqs_prohibited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    digest = write_bundled_plugin(
        component_dir, "mtg", sysreqs=[{"type": "apt", "package": "libx"}])
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    with pytest.raises(SpecialistInstallError) as exc:
        _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)
    assert exc.value.kind == "bundled_sysreqs_unsupported"


def test_env_name_collision_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    digest = write_bundled_plugin(component_dir, "mtg")
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    monkeypatch.setattr(specialist_install, "_env_name_conflicts",
                        lambda *a, **k: ["SOME_API_KEY"])

    with pytest.raises(SpecialistInstallError) as exc:
        _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)
    assert exc.value.kind == "env_name_collision"


def test_sibling_env_name_collision_detected():
    # Whole-branch E: two sourced plugins in the SAME bundle each requiring the
    # same env name collide, even though neither collides with an installed one.
    from specialist_install import DependencyResolution, _sibling_env_name_collisions
    deps = (
        DependencyResolution(kind="plugin/implementation", identifier="a",
                             digest="sha256:" + "a" * 64, available=True, detail="",
                             env_names=("SHARED_KEY", "A_ONLY")),
        DependencyResolution(kind="plugin/implementation", identifier="b",
                             digest="sha256:" + "b" * 64, available=True, detail="",
                             env_names=("SHARED_KEY", "B_ONLY")),
        DependencyResolution(kind="persona", identifier="p@1", digest="x",
                             available=True, detail=""),
    )
    assert _sibling_env_name_collisions(deps) == ["SHARED_KEY"]
    # A single sourced plugin never self-collides.
    assert _sibling_env_name_collisions(deps[:1]) == []


def test_sibling_env_collision_raises_env_name_collision_in_inspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Whole-branch E: inspect surfaces the sibling collision as env_name_collision.
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    d1 = write_bundled_plugin(component_dir, "aa", env_names=["SHARED_KEY"])
    d2 = write_bundled_plugin(component_dir, "bb", env_names=["SHARED_KEY"])
    _add_dependency_row(manifest_path, _bundled_dep_row("aa", d1, "plugins/aa"))
    _add_dependency_row(manifest_path, _bundled_dep_row("bb", d2, "plugins/bb"))
    with pytest.raises(SpecialistInstallError) as exc:
        _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)
    assert exc.value.kind == "env_name_collision"


def test_manifest_name_collision_precheck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    digest = write_bundled_plugin(component_dir, "mtg")
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    import plugin_registry

    fake_registry = plugin_registry.RegistryData(raw={}, entries=[{
        "name": "mtg", "targets": ["specialist:mtg-test"],
        "source": {"type": "github", "repo": "x/y", "ref": "v1",
                   "revision": "git:" + "a" * 40, "subdir": ""},
        "version": "1.0.0", "artifact_id": "irrelevant",
    }])
    monkeypatch.setattr(plugin_registry, "snapshot_registry", lambda: fake_registry)

    with pytest.raises(SpecialistInstallError) as exc:
        _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)
    assert exc.value.kind == "manifest_name_collision"


def test_symlink_escape_in_bundled_path_fails(tmp_path: Path) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    digest = write_bundled_plugin(component_dir, "mtg")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    (component_dir / "plugins" / "mtg" / "escape").symlink_to(outside)
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    component = load_specialist_component(component_dir, manifest_path)
    resolutions = resolve_dependency_closure(component, component_dir)
    plugin_rows = [r for r in resolutions if r.kind == "plugin/implementation"]
    assert len(plugin_rows) == 1
    assert plugin_rows[0].available is False
    assert "unsafe_archive" in plugin_rows[0].detail


# ---------------------------------------------------------------------------
# Legacy sourceless dep — must keep working unchanged.
# ---------------------------------------------------------------------------


def test_sourceless_dep_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin_store import content_checksum

    installed_dir = tmp_path / "installed-artifact"
    installed_dir.mkdir()
    (installed_dir / "file.txt").write_text("hello", encoding="utf-8")
    digest = "sha256:" + content_checksum(installed_dir)

    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    _add_dependency_row(manifest_path, {
        "kind": "plugin/implementation", "identifier": "mtg", "digest": digest,
    })
    component = load_specialist_component(component_dir, manifest_path)

    import plugin_registry
    from plugin_registry import ResolutionResult, ResolvedPlugin

    # #929: the closure binds ONE registry snapshot for all its rows
    # (`pinned_resolver`, #454) instead of calling `resolve_all` per row, so
    # THAT is the seam a double has to stand in for. `resolve_all` is left
    # raising: if the closure ever reaches for the unpinned per-row reader
    # again, this test says so rather than passing on a double nobody reads.
    snapshot = ResolutionResult(registry_valid=True, plugins=[
        ResolvedPlugin(name="mtg", artifact_id="x", path=str(installed_dir),
                       version="1.0.0", manifest={}),
    ])

    def _pinned():
        resolve = lambda target=None: snapshot  # noqa: E731
        resolve.generation = 0
        return resolve

    def _unpinned():
        raise AssertionError("the closure must resolve against one pinned snapshot")

    monkeypatch.setattr(plugin_registry, "pinned_resolver", _pinned)
    monkeypatch.setattr(plugin_registry, "resolve_all", _unpinned)

    resolutions = resolve_dependency_closure(component, component_dir)
    plugin_rows = [r for r in resolutions if r.kind == "plugin/implementation"]
    assert len(plugin_rows) == 1
    assert plugin_rows[0].available is True


# ---------------------------------------------------------------------------
# Fix-round-1 (consent-review CRITICAL, spec §3.2): the receipt row must
# enumerate a bundled plugin's MCP servers/commands, protected tools, and
# secrets surface (env names) — not just identity + a content digest.
# ---------------------------------------------------------------------------


def test_bundled_dep_receipt_row_carries_consent_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    digest = write_bundled_plugin(
        component_dir, "mtg",
        protected_tools=["danger_tool"],
        env_names=["SOME_API_KEY"],
        mcp_command_servers={"tools": {"command": "python3", "args": ["-m", "mtgserver"]}},
    )
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    result = _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)

    row = result.plugin_resolutions[0]
    assert row.mcp_servers == (
        "main: url https://example.invalid/mcp",
        "tools: python3 -m mtgserver",
    )
    assert row.protected_tools == ("danger_tool",)
    assert row.env_names == ("SOME_API_KEY",)

    # Round-trip through the persisted (attested) receipt sidecar.
    loaded = specialist_receipt.load(result.receipt_id, receipts_dir=tmp_path / "receipts")
    assert loaded is not None
    loaded_row = loaded.plugins[0]
    assert loaded_row.mcp_servers == row.mcp_servers
    assert loaded_row.protected_tools == row.protected_tools
    assert loaded_row.env_names == row.env_names

    # The three fields are ATTESTED — they must move the receipt digest, not
    # just ride along inert. A row stripped of them must hash differently.
    bare_row = specialist_receipt.PluginReceiptRow(
        identifier=row.identifier, scoped_name=row.scoped_name,
        manifest_name=row.manifest_name, version=row.version,
        source_type=row.source_type, repo=row.repo, ref=row.ref,
        revision=row.revision, subdir=row.subdir,
        content_digest=row.content_digest, staged_path=row.staged_path,
    )
    bare_digest = specialist_receipt.compute_receipt_digest(
        slug=result.slug, component_repo="org/repo", component_ref="main",
        component_revision="git:" + "a" * 40, component_subdir="",
        plugins=(bare_row,),
    )
    assert bare_digest != loaded.receipt_digest


def test_render_consent_message_includes_bundled_plugin_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    digest = write_bundled_plugin(
        component_dir, "mtg",
        protected_tools=["danger_tool"],
        env_names=["SOME_API_KEY"],
        mcp_command_servers={"tools": {"command": "python3", "args": ["-m", "mtgserver"]}},
    )
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    result = _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)

    text = render_install_consent_message(result)

    # Non-vacuous: the literal server name, protected-tool name, and env
    # name must all appear — not just a generic "see receipt" placeholder.
    assert "tools: python3 -m mtgserver" in text
    assert "danger_tool" in text
    assert "SOME_API_KEY" in text


# ---------------------------------------------------------------------------
# Minor-2 regression: strip-before-checksum must apply BEFORE both the
# declared-digest comparison (inspect must still pass) and the receipt row's
# content_digest (must equal the STRIPPED tree's digest, never the raw
# on-disk-with-cruft digest).
# ---------------------------------------------------------------------------


def test_bundled_pycache_stripped_before_checksum_and_inspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    # `stripped_digest` is computed over a CLEAN tree — no bytecode cruft yet.
    stripped_digest = write_bundled_plugin(component_dir, "mtg")

    plugin_dir = component_dir / "plugins" / "mtg"
    pycache_dir = plugin_dir / "__pycache__"
    pycache_dir.mkdir()
    (pycache_dir / "x.cpython-311.pyc").write_bytes(b"fake-bytecode")
    (plugin_dir / "x.pyc").write_bytes(b"fake-bytecode")

    # The manifest pins the STRIPPED-tree digest — exactly what a real
    # publish (`_stage_and_swap`) would have computed after its own strip.
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", stripped_digest, "plugins/mtg"))

    result = _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)

    row = result.plugin_resolutions[0]
    assert row.content_digest == stripped_digest

    loaded = specialist_receipt.load(result.receipt_id, receipts_dir=tmp_path / "receipts")
    assert loaded is not None
    assert loaded.plugins[0].content_digest == stripped_digest

    # The staged tree itself must have been stripped in place (belt-and-
    # suspenders on the mechanism, not just the resulting digest). Note:
    # `result.staged_dir` is the STAGING COPY `_stub_resolve_and_fetch` made
    # (via `shutil.copytree`) — a distinct tree from the fixture's original
    # `component_dir`, which is left untouched.
    staged_plugin_dir = result.staged_dir / "plugins" / "mtg"
    assert not (staged_plugin_dir / "__pycache__").exists()
    assert not (staged_plugin_dir / "x.pyc").exists()
