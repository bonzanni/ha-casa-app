"""Read/write /config/system-requirements.yaml (P-7 schema)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from atomic_io import atomic_write_text

MANIFEST_PATH: Path = Path("/config/system-requirements.yaml")


def read_manifest() -> dict:
    if not MANIFEST_PATH.is_file():
        return {"plugins": []}
    # Sol round-5/6: tolerate a malformed/unreadable manifest — return an empty
    # view rather than raising, so a corrupt system-requirements.yaml can't make
    # plugin verification (which reads this) raise before health regeneration.
    # Covers YAML syntax errors, invalid UTF-8, a non-mapping root, AND non-dict
    # / nameless list entries (so downstream `p["name"]` indexing can't crash).
    try:
        data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return {"plugins": []}
    if not isinstance(data, dict):
        return {"plugins": []}
    plugins = data.get("plugins")
    data["plugins"] = ([p for p in plugins
                        if isinstance(p, dict) and p.get("name")]
                       if isinstance(plugins, list) else [])
    return data


def _write(data: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Atomic (temp-file + fsync + os.replace): a crash mid-write must not
    # undermine the manifest's crash-recovery purpose with a truncated file.
    atomic_write_text(MANIFEST_PATH, yaml.safe_dump(data, sort_keys=True))


class BinOwnershipError(RuntimeError):
    """Another plugin already publishes this tools/bin name (#354)."""


def _rel_owned_by(parts: "tuple[str, ...]", plugin_name: str) -> bool:
    """Whether a tools_root-relative path lies inside *plugin_name*'s own
    install namespace (tarball generations, venv, npm). Shared by
    claim/retire so the two ownership answers can't drift."""
    if not parts:
        return False
    head = parts[0]
    return (
        head == f"venv-{plugin_name}"
        or (head == "npm" and len(parts) > 1 and parts[1] == plugin_name)
        or (head == "tarball" and len(parts) > 1 and parts[1] == plugin_name)
        or head == plugin_name
    )


def ensure_bin_claim(verify_bin: str, plugin_name: str,
                     tools_root: "Path | None" = None) -> None:
    """Refuse a tools/bin publication that would overwrite another plugin's.

    #354: every strategy symlinks its ``verify_bin`` into the single global
    ``tools/bin`` namespace and pre-fix unconditionally unlinked an existing
    entry — plugin B installing ``toolx`` silently repointed plugin A's
    ``toolx`` at B's tree while both reported ready.

    Sol r5-1/r6-1 discipline — the checks must not fail open when either
    signal is unavailable, and ambiguity fails CLOSED:

    1. A readable manifest row for the bin decides outright: another
       plugin's row refuses; the claimant's own row allows.
    2. With NO recorded owner (a fresh name, or a corrupt manifest that
       deliberately reads as empty) and *tools_root* given: an EXISTING
       entry may be replaced only when it is a symlink whose absolute
       target lies inside the claimant's UNAMBIGUOUS modern namespace
       (``tarball/<plugin>``, ``venv-<plugin>``, ``npm/<plugin>``).
       Anything else occupying the name — a regular file, a relative or
       outside-tools_root symlink (Sol r7-1), another plugin's namespace,
       or a legacy flat ``<name>-<version>`` dir whose prefix-ambiguous
       naming cannot prove ownership (``foo`` vs ``foo-bar``) — refuses. A
       healthy manifest row is the way a legacy-layout owner passes;
       without one, nobody replaces an occupied name."""
    for p in read_manifest()["plugins"]:
        if p.get("verify_bin") == verify_bin:
            if p.get("name") != plugin_name:
                raise BinOwnershipError(
                    f"tools/bin/{verify_bin} is already published by plugin "
                    f"{p.get('name')!r}; refusing to overwrite it for "
                    f"{plugin_name!r}")
            return  # positively the claimant's own recorded bin
    if tools_root is None:
        return
    link = Path(tools_root) / "bin" / verify_bin
    try:
        os.lstat(link)
    except OSError:
        return  # absent — a fresh name is free to claim
    # Occupied with no recorded owner (Sol r7-1): ONLY a symlink whose
    # absolute target lies inside the claimant's unambiguous modern
    # namespace may be replaced. A regular file, a relative or
    # outside-tools_root symlink, or a target in anyone else's (or a
    # prefix-ambiguous legacy) tree all refuse — occupancy without provable
    # ownership fails closed.
    try:
        raw = os.readlink(link)
    except OSError:
        raise BinOwnershipError(
            f"tools/bin/{verify_bin} exists with no recorded owner and is "
            f"not a symlink; refusing to replace it for {plugin_name!r}")
    try:
        rel = Path(raw).relative_to(Path(tools_root))
    except ValueError:
        raise BinOwnershipError(
            f"tools/bin/{verify_bin} exists with no recorded owner and its "
            f"target ({raw!r}) is outside the managed tools tree; refusing "
            f"to repoint it for {plugin_name!r}")
    if ".." in rel.parts or "." in rel.parts:
        # Sol r8-1: a crafted `a/../b` target defeats lexical namespace
        # classification (relative_to keeps the dot-dots, so the FIRST
        # component can name one plugin while the path resolves into
        # another's tree). Installer-created links never contain them.
        raise BinOwnershipError(
            f"tools/bin/{verify_bin} exists with no recorded owner and its "
            f"target ({raw!r}) contains traversal components; refusing to "
            f"repoint it for {plugin_name!r}")
    parts = rel.parts
    modern_own = bool(parts) and (
        parts[0] == f"venv-{plugin_name}"
        or (parts[0] == "npm" and len(parts) > 1 and parts[1] == plugin_name)
        or (parts[0] == "tarball" and len(parts) > 1 and parts[1] == plugin_name)
    )
    if not modern_own:
        raise BinOwnershipError(
            f"tools/bin/{verify_bin} exists with no recorded owner and its "
            f"target ({raw!r}) is not provably {plugin_name!r}'s own install "
            "tree; refusing to repoint a live launcher on ambiguity")


def retire_stale_bin(verify_bin: str, plugin_name: str, tools_root: Path) -> None:
    """#354 (Sol round-1 P1-4): a plugin whose update changes its
    ``verify_bin`` would otherwise leave the OLD name published in the global
    ``tools/bin`` forever — resolving to the retained old tarball generation,
    or dangling for venv/npm — invisible to verification, which only checks
    the new name. Remove the old link, but ONLY when it is a symlink whose
    (textual, so dangling links are covered too) target lies inside this
    plugin's own install namespace under *tools_root* — never a link another
    plugin now owns. Best-effort; never raises."""
    link = Path(tools_root) / "bin" / verify_bin
    try:
        raw = os.readlink(link)
    except OSError:
        return  # absent, or not a symlink — never touch
    try:
        rel = Path(raw).relative_to(Path(tools_root))
    except ValueError:
        return  # points outside tools_root — not ours to manage
    if ".." in rel.parts or "." in rel.parts:
        return  # traversal-bearing target — never classify, never touch (Sol r8-1)
    if not _rel_owned_by(rel.parts, plugin_name):
        return
    try:
        os.unlink(link)
    except OSError:
        pass


def add_plugin_entry(entry: dict) -> None:
    data = read_manifest()
    name = entry["name"]
    data["plugins"] = [p for p in data["plugins"] if p["name"] != name]
    data["plugins"].append(entry)
    _write(data)


def remove_plugin_entry(name: str) -> None:
    data = read_manifest()
    data["plugins"] = [p for p in data["plugins"] if p["name"] != name]
    _write(data)
