"""Boot-time reconciliation of /addon_configs/casa/tools/ (§4.3.4)."""
from __future__ import annotations

import json
import shutil
import sys
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

RECONCILER = Path("casa/rootfs/opt/casa/scripts/reconcile_system_requirements.py")


def _write_manifest(path: Path, entries: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"plugins": entries}), encoding="utf-8")


def test_noop_when_tools_present(tmp_path: Path) -> None:
    tools_root = tmp_path / "tools"
    (tools_root / "bin").mkdir(parents=True)
    fake = tools_root / "bin" / "fakebin"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)

    manifest = tmp_path / "m.yaml"
    _write_manifest(manifest, [{
        "name": "face-rec",
        "winning_strategy": "tarball",
        "install_dir": str(tools_root / "face-rec-1.0.0"),
        "verify_bin": "fakebin",
        "declared_at": "2026-04-24T00:00:00Z",
    }])
    status = tmp_path / "status.yaml"

    r = subprocess.run([sys.executable, str(RECONCILER),
                        "--manifest", str(manifest),
                        "--tools-root", str(tools_root),
                        "--status-file", str(status),
                        "--log-level", "warning"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = yaml.safe_load(status.read_text())
    assert data["results"][0]["status"] == "ready"


def test_exits_nonzero_on_degraded(tmp_path: Path) -> None:
    tools_root = tmp_path / "tools"
    tools_root.mkdir()

    manifest = tmp_path / "m.yaml"
    _write_manifest(manifest, [{
        "name": "broken",
        "winning_strategy": "tarball",
        "install_dir": str(tools_root / "broken-0.0.0"),
        "verify_bin": "nothere",
        "pin_sha256": "0" * 64,
        "declared_at": "2026-04-24T00:00:00Z",
    }])
    status = tmp_path / "status.yaml"

    r = subprocess.run([sys.executable, str(RECONCILER),
                        "--manifest", str(manifest),
                        "--tools-root", str(tools_root),
                        "--status-file", str(status),
                        "--log-level", "warning"],
                       capture_output=True, text=True)
    assert r.returncode != 0
    data = yaml.safe_load(status.read_text())
    assert data["results"][0]["status"] == "degraded"


def test_dangling_symlink_reports_degraded(tmp_path: Path) -> None:
    """M23: a rolled-back install leaves a dangling verify_bin symlink (target
    rmtree'd). is_symlink() is still True for it, so the old check masked the
    breakage as ready. is_file() follows the link and is False, so it is now
    correctly reported degraded."""
    tools_root = tmp_path / "tools"
    tools_bin = tools_root / "bin"
    tools_bin.mkdir(parents=True)
    # bin symlink survives, install_dir rmtree'd — target never created.
    (tools_bin / "fakebin").symlink_to(tools_root / "face-rec-1.0.0" / "fakebin")

    manifest = tmp_path / "m.yaml"
    _write_manifest(manifest, [{
        "name": "face-rec",
        "winning_strategy": "tarball",
        "install_dir": str(tools_root / "face-rec-1.0.0"),
        "verify_bin": "fakebin",
        "declared_at": "2026-04-24T00:00:00Z",
    }])
    status = tmp_path / "status.yaml"

    r = subprocess.run([sys.executable, str(RECONCILER),
                        "--manifest", str(manifest),
                        "--tools-root", str(tools_root),
                        "--status-file", str(status),
                        "--log-level", "warning"],
                       capture_output=True, text=True)
    # "fakebin" is on no PATH, so the shutil.which fallback cannot mask this.
    assert r.returncode != 0
    data = yaml.safe_load(status.read_text())
    assert data["results"][0]["status"] == "degraded"


def test_path_shadow_does_not_mask_missing_managed_install(tmp_path: Path) -> None:
    """#334: an unrelated same-named executable on the image PATH must not
    satisfy a plugin requirement whose managed tools/bin entry is gone —
    every install backend publishes verify_bin into tools/bin, so a missing
    managed entry always means the install was wiped/rolled back."""
    tools_root = tmp_path / "tools"
    tools_bin = tools_root / "bin"
    tools_bin.mkdir(parents=True)
    # Managed entry dangling (install_dir wiped), like the M23 case…
    (tools_bin / "fakebin").symlink_to(tools_root / "face-rec-1.0.0" / "fakebin")
    # …but an unrelated binary of the same name IS on PATH (the image's).
    shadow_dir = tmp_path / "imagebin"
    shadow_dir.mkdir()
    shadow = shadow_dir / "fakebin"
    shadow.write_text("#!/bin/sh\n", encoding="utf-8")
    shadow.chmod(0o755)

    manifest = tmp_path / "m.yaml"
    _write_manifest(manifest, [{
        "name": "face-rec",
        "winning_strategy": "venv",
        "install_dir": str(tools_root / "face-rec-1.0.0"),
        "verify_bin": "fakebin",
        "declared_at": "2026-04-24T00:00:00Z",
    }])
    status = tmp_path / "status.yaml"

    r = subprocess.run([sys.executable, str(RECONCILER),
                        "--manifest", str(manifest),
                        "--tools-root", str(tools_root),
                        "--status-file", str(status),
                        "--log-level", "warning"],
                       capture_output=True, text=True,
                       env={"PATH": str(shadow_dir)})
    assert r.returncode != 0
    data = yaml.safe_load(status.read_text())
    assert data["results"][0]["status"] == "degraded"
