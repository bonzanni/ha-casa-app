"""Agent-home directory provisioning for in_casa agents.

Creates /config/agent-home/<role>/.claude/settings.json (for hooks + user
edits). Idempotent — preserves user-added entries (P-3 drift policy).

Under the unified plugin architecture (v0.71.0), plugin ASSIGNMENT lives in
the registry, not in per-agent-home ``enabledPlugins`` — this module no longer
seeds it. A pre-existing ``enabledPlugins`` key (from an older deploy) is left
untouched (user data is never deleted); nothing reads it anymore.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from pathlib import Path

from atomic_io import fsync_directory

logger = logging.getLogger(__name__)


def provision_agent_home(
    *,
    role: str,
    home_root: Path | str,
    defaults_root: Path | str,
) -> None:
    home_root = Path(home_root)
    defaults_root = Path(defaults_root)

    agent_dir = home_root / role
    claude_dir = agent_dir / ".claude"
    settings_path = claude_dir / "settings.json"

    # Load existing settings (preserve user edits).
    existing: dict = {}
    if settings_path.is_file():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # #355: unparseable content may be the truncated remains of real
            # user settings (crash mid-rewrite, interrupted edit) — leave the
            # bytes in place for repair instead of replacing them with {}.
            logger.warning(
                "settings.json at %s is not valid JSON — preserving the "
                "file for repair (not rewriting)", settings_path)
            return
    if not isinstance(existing, dict):
        logger.warning(
            "settings.json at %s is not a JSON object — recreating",
            settings_path,
        )
        existing = {}

    # v0.71.0: no enabledPlugins seeding — plugin assignment is the registry's
    # job. A stale key from an older deploy is preserved (never deleted).

    # Write back atomically (#355 + Terra/Sol r1): a plain truncate-and-write
    # left invalid JSON behind on a crash mid-write. A UNIQUE same-directory
    # temp file (mkstemp — no fixed-name race between concurrent provisions)
    # + os.replace never exposes a partial file; the try covers the WRITE as
    # well as the replace so no temp litter survives any failure; and the
    # destination's existing mode is preserved (an operator-tightened 0600
    # must not widen to the umask default). The temp file is fsynced before
    # the replace and the directory after it (#1689), as atomic_io does, so a
    # power loss cannot bring the file back empty.
    claude_dir.mkdir(parents=True, exist_ok=True)
    try:
        prior_mode = stat.S_IMODE(os.stat(settings_path).st_mode)
    except OSError:
        prior_mode = 0o644  # new file: match the previous write_text default
    fd, tmp_name = tempfile.mkstemp(
        dir=claude_dir, prefix=".settings.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(existing, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, prior_mode)
        os.replace(tmp_name, settings_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    fsync_directory(claude_dir)
    logger.info("agent-home provisioned: role=%s settings=%s", role, settings_path)


def provision_all_homes(
    *,
    role_configs: dict,
    specialist_configs: dict,
    home_root: Path | str,
    defaults_root: Path | str,
) -> None:
    """Provision an agent-home for every in_casa resident or specialist.

    Iterates the union of `role_configs` and `specialist_configs`,
    delegating each role to ``provision_agent_home``. Idempotent — safe
    to call on every boot.

    Executors are deliberately excluded: they run with
    ``cwd=/config`` per
    ``tools.py::_build_executor_options``, not from an
    ``agent-home/<role>/`` directory. Adding executors here would create
    empty unused dirs.

    Each role's provisioning is wrapped in its own try/except so a
    single malformed plugins.yaml cannot take down the boot — the
    failing role is logged at WARNING and skipped; the rest continue.
    """
    for role in {**role_configs, **specialist_configs}:
        try:
            provision_agent_home(
                role=role, home_root=home_root, defaults_root=defaults_root,
            )
        except Exception as exc:  # noqa: BLE001 — boot non-fatal; isolates one role from others
            logger.warning(
                "agent-home provisioning failed for role=%s: %s", role, exc,
            )
