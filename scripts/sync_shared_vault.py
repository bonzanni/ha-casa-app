#!/usr/bin/env python3
"""Keep Casa Browser's copies of the shared secrets files identical to Casa's.

Casa (``casa/``) and the optional Casa Browser app (``casa_browser/``) ship some files of
the built-in secrets store and its page byte for byte. Casa's tree is the source; the
browser's tree holds the copies. ``SHARED_FILES`` names them, relative to each app's code
root.

Usage:
    sync_shared_vault.py           copies every shared file from Casa's tree to the browser's
    sync_shared_vault.py --check   copies nothing; prints each shared file that differs or is
                                   missing in the browser's tree and exits 1 if there is any
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CASA_ROOT = REPO_ROOT / "casa" / "rootfs" / "opt" / "casa"
BROWSER_ROOT = REPO_ROOT / "casa_browser" / "rootfs" / "opt" / "casa_browser"

SHARED_FILES: tuple[str, ...] = (
    "vault_store.py",
    "vault_page.py",
    "vault_page_server.py",
    "vault_static/ha.css",
    "vault_static/vault.css",
    "vault_static/theme.js",
    "vault_static/page.js",
)


def sync(source: Path, target: Path) -> None:
    """Copy every shared file from ``source`` to ``target``, bytes exactly."""
    for name in SHARED_FILES:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source / name).read_bytes())


def differing(source: Path, target: Path) -> list[str]:
    """The shared files whose copy in ``target`` is missing or not identical to ``source``."""
    found = []
    for name in SHARED_FILES:
        copy = target / name
        if not copy.is_file() or copy.read_bytes() != (source / name).read_bytes():
            found.append(name)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="compare only; exit 1 if a copy differs or is missing")
    args = parser.parse_args(argv)

    if not args.check:
        sync(CASA_ROOT, BROWSER_ROOT)
        print(f"copied {len(SHARED_FILES)} shared files")
        return 0

    found = differing(CASA_ROOT, BROWSER_ROOT)
    for name in found:
        state = "missing" if not (BROWSER_ROOT / name).is_file() else "differs"
        print(f"{state}: {name}")
    if found:
        return 1
    print(f"ok: {len(SHARED_FILES)} shared files identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
