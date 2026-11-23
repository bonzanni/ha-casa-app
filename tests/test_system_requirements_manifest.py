"""manifest.yaml reader/writer for /addon_configs/casa/system-requirements.yaml."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from system_requirements.manifest import (
    BinOwnershipError,
    add_plugin_entry as add_manifest_entry,
    ensure_bin_claim,
    remove_plugin_entry as remove_manifest_entry,
    read_manifest,
    retire_stale_bin,
)

pytestmark = pytest.mark.unit


def test_roundtrip(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "system-requirements.yaml"
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH", path)

    add_manifest_entry({
        "name": "p",
        "winning_strategy": "tarball",
        "install_dir": "/t/p-1.0",
        "verify_bin": "p",
        "pin_sha256": "a" * 64,
        "declared_at": "2026-04-24T00:00:00Z",
    })
    data = read_manifest()
    assert len(data["plugins"]) == 1
    assert data["plugins"][0]["name"] == "p"


def test_remove(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "m.yaml"
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH", path)
    add_manifest_entry({"name": "p", "winning_strategy": "tarball",
                        "install_dir": "/t/p-1.0", "verify_bin": "p",
                        "declared_at": "2026-04-24T00:00:00Z"})
    remove_manifest_entry("p")
    assert read_manifest() == {"plugins": []}


def test_write_is_atomic_crash_keeps_original(tmp_path: Path, monkeypatch) -> None:
    """A crash BETWEEN the temp write and os.replace must leave the prior
    system-requirements.yaml intact (not truncated), preserving its
    crash-recovery purpose."""
    import atomic_io

    path = tmp_path / "system-requirements.yaml"
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH", path)
    add_manifest_entry({"name": "p", "winning_strategy": "tarball",
                        "install_dir": "/t/p-1.0", "verify_bin": "p",
                        "declared_at": "2026-04-24T00:00:00Z"})
    before = path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash before replace")

    monkeypatch.setattr(atomic_io.os, "replace", boom)
    with pytest.raises(RuntimeError):
        add_manifest_entry({"name": "q", "winning_strategy": "venv",
                            "install_dir": "/t/venv-q", "verify_bin": "q",
                            "declared_at": "2026-04-24T00:00:00Z"})

    assert path.read_text(encoding="utf-8") == before
    data = read_manifest()
    assert [p["name"] for p in data["plugins"]] == ["p"]
    import os as _os
    leftovers = [f for f in _os.listdir(tmp_path) if f != "system-requirements.yaml"]
    assert leftovers == []


def test_ensure_bin_claim_ownership(tmp_path: Path, monkeypatch) -> None:
    """#354: a tools/bin name already published by ANOTHER plugin is refused;
    the same plugin (reinstall/upgrade) and an unclaimed name are allowed."""
    path = tmp_path / "system-requirements.yaml"
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH", path)

    ensure_bin_claim("toolx", "plugin-a")   # empty manifest: allowed
    add_manifest_entry({"name": "plugin-a", "winning_strategy": "npm",
                        "install_dir": "/t/npm/plugin-a", "verify_bin": "toolx",
                        "declared_at": "2026-08-01T00:00:00Z"})
    ensure_bin_claim("toolx", "plugin-a")   # own reinstall: allowed
    ensure_bin_claim("other", "plugin-b")   # different name: allowed
    with pytest.raises(BinOwnershipError, match="plugin-a"):
        ensure_bin_claim("toolx", "plugin-b")
    # Owner removal releases the claim.
    remove_manifest_entry("plugin-a")
    ensure_bin_claim("toolx", "plugin-b")


def test_ensure_bin_claim_refuses_live_link_takeover_with_corrupt_manifest(
        tmp_path: Path, monkeypatch) -> None:
    """#354 (Sol r5-1): a corrupt manifest deliberately reads as empty — that
    must NOT authorize plugin B to take over a live launcher that visibly
    points into plugin A's tree. The link-target check is manifest-
    independent; the rightful owner (and an outside-target link) still pass."""
    import os

    path = tmp_path / "system-requirements.yaml"
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH", path)
    path.write_text("{ this is: not: valid yaml", encoding="utf-8")

    tools = tmp_path / "tools"
    (tools / "bin").mkdir(parents=True)
    os.symlink(tools / "tarball" / "plugin-a" / "1.0.g-x" / "bin" / "toolx",
               tools / "bin" / "toolx")

    with pytest.raises(BinOwnershipError, match="not provably"):
        ensure_bin_claim("toolx", "plugin-b", tools)
    ensure_bin_claim("toolx", "plugin-a", tools)   # rightful owner: allowed

    # Sol r7-1: occupancy without provable ownership fails closed — an
    # outside-tools_root symlink, a regular file, and a relative symlink
    # all refuse when no manifest row backs the claim.
    os.symlink("/usr/bin/env", tools / "bin" / "envtool")
    with pytest.raises(BinOwnershipError, match="outside"):
        ensure_bin_claim("envtool", "plugin-b", tools)
    (tools / "bin" / "plainfile").write_text("x", encoding="utf-8")
    with pytest.raises(BinOwnershipError, match="not a symlink"):
        ensure_bin_claim("plainfile", "plugin-b", tools)
    os.symlink("relative/target", tools / "bin" / "reltool")
    with pytest.raises(BinOwnershipError, match="outside"):
        ensure_bin_claim("reltool", "plugin-b", tools)

    # Sol r8-1: a target that names the claimant's namespace but traverses
    # into another plugin's tree via `..` must refuse — lexical
    # classification never trusts dot-dot components.
    os.symlink(tools / "tarball" / "plugin-b" / ".." / ".." / "tarball"
               / "plugin-a" / "1.0.g-x" / "bin" / "sneaky",
               tools / "bin" / "sneaky")
    with pytest.raises(BinOwnershipError, match="traversal"):
        ensure_bin_claim("sneaky", "plugin-b", tools)

    # Sol r6-1: a LEGACY flat-dir target is prefix-ambiguous (`foo` vs
    # `foo-bar`), so with no recorded owner NOBODY may repoint it — not even
    # the name whose prefix matches. A healthy manifest row is how the
    # legacy owner passes.
    os.symlink(tools / "foo-bar-1.0" / "bin" / "toolz", tools / "bin" / "toolz")
    with pytest.raises(BinOwnershipError):
        ensure_bin_claim("toolz", "foo", tools)          # prefix-sharing sibling
    with pytest.raises(BinOwnershipError):
        ensure_bin_claim("toolz", "foo-bar", tools)      # even the likely owner
    # With a readable manifest row, the recorded owner passes and others refuse.
    path.write_text("", encoding="utf-8")   # manifest healthy again (empty)
    add_manifest_entry({"name": "foo-bar", "winning_strategy": "tarball",
                        "install_dir": str(tools / "foo-bar-1.0"),
                        "verify_bin": "toolz",
                        "declared_at": "2026-08-01T00:00:00Z"})
    ensure_bin_claim("toolz", "foo-bar", tools)
    with pytest.raises(BinOwnershipError):
        ensure_bin_claim("toolz", "foo", tools)


def test_retire_stale_bin_removes_only_owned_links(tmp_path: Path) -> None:
    """#354 (review): a verify_bin rename retires the OLD published link, but
    only when it is a symlink into this plugin's own install namespace —
    another plugin's link, a non-symlink, or an outside-target link is never
    touched. Dangling links (venv/npm rebuilt) are covered via the textual
    target."""
    import os

    tools = tmp_path / "tools"
    bin_dir = tools / "bin"
    bin_dir.mkdir(parents=True)

    # Owned (tarball generation dir) — removed, dangling target included.
    os.symlink(tools / "tarball" / "plug" / "1.0.g-abc" / "bin" / "oldtool",
               bin_dir / "oldtool")
    retire_stale_bin("oldtool", "plug", tools)
    assert not (bin_dir / "oldtool").is_symlink()

    # Owned venv layout — removed.
    os.symlink(tools / "venv-plug" / "bin" / "vtool", bin_dir / "vtool")
    retire_stale_bin("vtool", "plug", tools)
    assert not (bin_dir / "vtool").is_symlink()

    # Owned npm layout — removed.
    os.symlink(tools / "npm" / "plug" / "node_modules" / ".bin" / "ntool",
               bin_dir / "ntool")
    retire_stale_bin("ntool", "plug", tools)
    assert not (bin_dir / "ntool").is_symlink()

    # ANOTHER plugin's namespace — untouched.
    os.symlink(tools / "other-2.0.g-def" / "bin" / "theirs", bin_dir / "theirs")
    retire_stale_bin("theirs", "plug", tools)
    assert (bin_dir / "theirs").is_symlink()

    # Target outside tools_root — untouched.
    os.symlink("/usr/bin/env", bin_dir / "outside")
    retire_stale_bin("outside", "plug", tools)
    assert (bin_dir / "outside").is_symlink()

    # A regular file (not a symlink) — untouched.
    (bin_dir / "plainfile").write_text("x", encoding="utf-8")
    retire_stale_bin("plainfile", "plug", tools)
    assert (bin_dir / "plainfile").is_file()

    # Absent link — no raise.
    retire_stale_bin("nonexistent", "plug", tools)


def test_read_manifest_tolerates_malformed_yaml(tmp_path, monkeypatch):
    """Sol round-5: a corrupt manifest returns an empty view, never raises — so
    plugin verification that reads it can't crash before health regeneration."""
    path = tmp_path / "system-requirements.yaml"
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH", path)
    path.write_text("{ this: is: not: valid: yaml", encoding="utf-8")
    assert read_manifest() == {"plugins": []}
    # A top-level non-mapping (list) also degrades to empty.
    path.write_text("- a\n- b\n", encoding="utf-8")
    assert read_manifest() == {"plugins": []}
    # Sol round-6: non-dict / nameless list entries are dropped (no p["name"] crash).
    path.write_text("plugins:\n  - oops\n  - {}\n  - {name: keep, verify_bin: x}\n",
                    encoding="utf-8")
    assert [p["name"] for p in read_manifest()["plugins"]] == ["keep"]
    # Invalid UTF-8 bytes degrade to empty, not a UnicodeDecodeError.
    path.write_bytes(b"\xff\xfe bad bytes")
    assert read_manifest() == {"plugins": []}
