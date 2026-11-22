"""venv install strategy (§4.3.1)."""
from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
from pathlib import Path

from .manifest import ensure_bin_claim
from .tarball import InstallResult, _atomic_symlink  # reuse dataclass + helper

logger = logging.getLogger(__name__)


def install_venv(
    *,
    plugin_name: str,
    spec: dict,
    tools_root: Path,
    timeout: int = 300,
) -> InstallResult:
    package = spec["package"]
    python = spec.get("python", "python3")
    verify_bin = spec["verify_bin"]
    # #354: refuse up front if another plugin already publishes this bin name.
    ensure_bin_claim(verify_bin, plugin_name, tools_root)

    # #1649: the new environment is built in a fresh directory inside the
    # plugin's own venv-<plugin> directory, never over the one in use, so a
    # failed install leaves the working version and its tools/bin link as they
    # were. A venv cannot be renamed into place afterwards (its scripts carry
    # their absolute path), so it stays where it was built; the venv-<plugin>
    # prefix every ownership check keys on is unchanged. Earlier releases put
    # the venv directly in venv-<plugin>; those entries are removed like any
    # superseded environment once the new one is published.
    plugin_dir = tools_root / f"venv-{plugin_name}"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    venv_dir = plugin_dir / f"g-{uuid.uuid4().hex[:8]}"
    tools_bin = tools_root / "bin"
    tools_bin.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run([python, "-m", "venv", str(venv_dir)],
                       check=True, timeout=timeout)
        subprocess.run(
            [str(venv_dir / "bin" / "pip"), "install", "--no-input", package],
            check=True, timeout=timeout,
        )
    except BaseException:
        shutil.rmtree(venv_dir, ignore_errors=True)
        raise

    source_bin = venv_dir / "bin" / verify_bin
    if not source_bin.is_file():
        shutil.rmtree(venv_dir, ignore_errors=True)
        return InstallResult(ok=False, verify_bin_resolves=False,
                             install_dir=venv_dir,
                             message=f"verify_bin {verify_bin!r} not found in venv")
    link = tools_bin / verify_bin
    # #308 review: atomic retarget — no unlink gap for a concurrent exec.
    _atomic_symlink(source_bin, link)
    for old in plugin_dir.iterdir():
        if old != venv_dir:
            if old.is_dir() and not old.is_symlink():
                shutil.rmtree(old, ignore_errors=True)
            else:
                old.unlink(missing_ok=True)

    return InstallResult(
        ok=True,
        verify_bin_resolves=link.is_symlink(),
        install_dir=venv_dir,
        message="installed via venv",
    )
