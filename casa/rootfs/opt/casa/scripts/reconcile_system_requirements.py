#!/usr/bin/env python3
"""Boot-time reconciliation of /config/tools/ (§4.3.4)."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml

logger = logging.getLogger("reconcile_system_requirements")


def _resolves(verify_bin: str, tools_bin: Path) -> bool:
    # Only the MANAGED tools/bin entry counts: every install backend publishes
    # verify_bin there, so its absence always means the install was wiped or
    # rolled back — an unrelated same-named binary on the image PATH must not
    # mask that as ready (#334). is_file() follows symlinks and is False for a
    # dangling link (M23).
    return (tools_bin / verify_bin).is_file()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--tools-root", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(message)s")

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        logger.info("no manifest at %s — nothing to reconcile", manifest_path)
        return 0

    tools_root = Path(args.tools_root)
    tools_bin = tools_root / "bin"

    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    entries = data.get("plugins", [])

    results: list[dict] = []
    degraded = False
    for entry in entries:
        name = entry.get("name", "?")
        verify_bin = entry.get("verify_bin", "")
        if verify_bin and _resolves(verify_bin, tools_bin):
            results.append({"name": name, "status": "ready",
                            "verify_bin": verify_bin})
            continue
        logger.warning(
            "plugin %s: verify_bin %r missing — re-run the plugin's install via "
            "the configurator (plugin_update / plugin_add) to reinstall its "
            "system requirements",
            name, verify_bin,
        )
        results.append({"name": name, "status": "degraded",
                        "verify_bin": verify_bin,
                        "reason": (f"managed tools/bin/{verify_bin} missing "
                                   "or dangling")})
        degraded = True

    status_path = Path(args.status_file)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(yaml.safe_dump({"results": results}), encoding="utf-8")

    return 1 if degraded else 0


if __name__ == "__main__":
    sys.exit(main())
