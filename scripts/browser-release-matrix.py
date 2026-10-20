#!/usr/bin/env python3
"""Which app folders a push or pull request builds, and how each one is released.

This repository holds Casa (``casa/``) and, once it exists, the optional Casa Browser app
(``casa_browser/``). ``.github/workflows/deploy.yml`` asks this script which apps to run
``.github/workflows/deploy-app.yml`` for, and ``deploy-app.yml`` asks it for an app's tag
and release settings once it knows the version.

* An app is selected when the change touches its folder. A change to the shared release
  pipeline (``SHARED_PATHS``) selects every app, so each is at least validated.
* An app whose folder has no ``config.yaml`` at the head commit is never selected: a second
  app builds and publishes nothing until it exists.
* A base that cannot be diffed (a push whose ``before`` is all zeros, or a commit this
  clone lacks) counts as a shared change rather than as no change.

Whether a selected app actually PUBLISHES is decided in ``deploy-app.yml``: only on a push,
and only when the app's release tag does not exist yet.

Usage:
    browser-release-matrix.py select --base <sha> --head <sha> [--repo-root <dir>]
        prints a JSON list of {"app", "version"} objects
    browser-release-matrix.py release --app <app> --version <version>
        prints tag=, latest_flag= and cache_scope= lines for $GITHUB_OUTPUT
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Casa first. Each entry: the release-tag prefix; the extra `gh release create` flag (Casa
# passes none, so GitHub keeps deciding which release is Latest exactly as before); and
# the builder's GitHub Actions cache scope suffix (Casa keeps the builder's per-arch
# default, the second app gets its own scope so the two images never share cache entries).
APPS: dict[str, dict[str, str]] = {
    "casa": {"tag_prefix": "v", "latest_flag": "", "cache_scope": ""},
    "casa_browser": {
        "tag_prefix": "casa_browser-v",
        "latest_flag": "--latest=false",
        "cache_scope": "casa_browser",
    },
}

SHARED_PATHS: tuple[str, ...] = (
    ".github/workflows/deploy.yml",
    ".github/workflows/deploy-app.yml",
    "scripts/browser-release-matrix.py",
)

_VERSION = re.compile(r"""^version:\s*["']?([^"'\s#]+)["']?\s*(?:#.*)?$""", re.MULTILINE)


def select_apps(changed: set[str], shared_changed: bool) -> list[str]:
    """The apps a change touches, in ``APPS`` order; every app on a shared change."""
    if shared_changed:
        return list(APPS)
    return [app for app in APPS if any(path.startswith(f"{app}/") for path in changed)]


def release_tag(app: str, version: str) -> str:
    return f"{APPS[app]['tag_prefix']}{version}"


def release_settings(app: str, version: str) -> dict[str, str]:
    return {
        "tag": release_tag(app, version),
        "latest_flag": APPS[app]["latest_flag"],
        "cache_scope": APPS[app]["cache_scope"],
    }


def manifest_version(text: str) -> str | None:
    """The top-level ``version:`` of an app manifest. Used only to name the concurrency
    group; the version a build carries comes from the Home Assistant info helper."""
    match = _VERSION.search(text)
    return match.group(1) if match else None


def _changed(repo_root: Path, base: str, head: str) -> set[str] | None:
    """Paths changed between the merge base of ``base`` and ``head``, or None when the
    base cannot be diffed."""
    if not base or set(base) == {"0"}:
        return None
    result = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--name-only", f"{base}...{head}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"note: cannot diff {base}...{head}: {result.stderr.strip()}", file=sys.stderr)
        return None
    return {line for line in result.stdout.splitlines() if line}


def select(repo_root: Path, base: str, head: str) -> list[dict[str, str]]:
    changed = _changed(repo_root, base, head)
    shared = changed is None or bool(changed & set(SHARED_PATHS))
    out = []
    for app in select_apps(changed or set(), shared):
        manifest = repo_root / app / "config.yaml"
        if not manifest.is_file():
            continue
        version = manifest_version(manifest.read_text())
        if version is None:
            raise SystemExit(f"{manifest}: no top-level version")
        out.append({"app": app, "version": version})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sel = sub.add_parser("select")
    sel.add_argument("--base", required=True)
    sel.add_argument("--head", required=True)
    sel.add_argument("--repo-root", default=".")
    rel = sub.add_parser("release")
    rel.add_argument("--app", required=True, choices=sorted(APPS))
    rel.add_argument("--version", required=True)
    args = parser.parse_args(argv)

    if args.command == "select":
        print(json.dumps(select(Path(args.repo_root), args.base, args.head)))
        return 0
    for key, value in release_settings(args.app, args.version).items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
