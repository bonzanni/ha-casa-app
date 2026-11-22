"""npm install strategy (§4.3.1)."""
from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
from pathlib import Path

from .manifest import ensure_bin_claim
from .tarball import InstallResult, _atomic_symlink

logger = logging.getLogger(__name__)


def install_npm(
    *,
    plugin_name: str,
    spec: dict,
    tools_root: Path,
    timeout: int = 180,
) -> InstallResult:
    package = spec["package"]
    verify_bin = spec["verify_bin"]
    # #354: refuse up front if another plugin already publishes this bin name.
    ensure_bin_claim(verify_bin, plugin_name, tools_root)

    # Per-plugin prefix (M25): a shared tools_root/"npm" prefix means the
    # two-stage-commit rollback in tools.py (shutil.rmtree(install_dir)) would
    # wipe node_modules for EVERY npm plugin and dangle their symlinks. Namespace
    # per plugin (mirrors venv-{plugin_name}) so rollback is isolated. Also
    # avoids npm 7+ ideal-tree reification pruning a sibling plugin as extraneous.
    # #1649: each install gets a fresh prefix inside the plugin's own
    # npm/<plugin> directory, never the one in use, so a failed install leaves
    # the working version and its tools/bin link as they were (same layout as
    # venv.py). Earlier releases installed directly into npm/<plugin>; those
    # entries are removed like any superseded prefix once the new one is
    # published.
    plugin_dir = tools_root / "npm" / plugin_name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    npm_root = plugin_dir / f"g-{uuid.uuid4().hex[:8]}"
    npm_root.mkdir()
    tools_bin = tools_root / "bin"
    tools_bin.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            ["npm", "install", "--prefix", str(npm_root), "--no-audit",
             "--no-fund", "--loglevel=error", package],
            check=True, timeout=timeout,
        )
    except BaseException:
        shutil.rmtree(npm_root, ignore_errors=True)
        raise

    source_bin = npm_root / "node_modules" / ".bin" / verify_bin
    if not source_bin.is_file():
        shutil.rmtree(npm_root, ignore_errors=True)
        return InstallResult(
            ok=False, verify_bin_resolves=False,
            install_dir=npm_root,
            message=f"verify_bin {verify_bin!r} not produced by npm install",
        )
    link = tools_bin / verify_bin
    # #308 review: atomic retarget — no unlink gap for a concurrent exec.
    _atomic_symlink(source_bin, link)
    for old in plugin_dir.iterdir():
        if old != npm_root:
            if old.is_dir() and not old.is_symlink():
                shutil.rmtree(old, ignore_errors=True)
            else:
                old.unlink(missing_ok=True)

    return InstallResult(
        ok=True, verify_bin_resolves=link.is_symlink(),
        install_dir=npm_root, message="installed via npm",
    )
