"""Tarball install strategy (§4.3.1).

Security notes (v0.14.6):
- Tar/zip extraction is path-validated and symlink-rejected to prevent
  zip-slip / symlink-escape attacks. ``tarfile.extractall(filter=...)``
  is only available on Python 3.11.4+; production currently runs
  3.11.2 so we validate members manually.
- ``install_cmd`` must be an argv list (not a shell string). Pre-v0.14.6
  it was passed to ``subprocess.run(..., shell=True)``, which let any
  marketplace author execute arbitrary shell as root. Backwards-
  incompatible with any legacy entry that used a string; the first-
  party marketplace ships with no such entry.
- ``url`` must be ``http://``/``https://``. ``file://``/``ftp://``/etc.
  are refused so a poisoned marketplace can't read arbitrary host paths.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
import uuid
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from atomic_io import fsync_directory

from .manifest import ensure_bin_claim

logger = logging.getLogger(__name__)

_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
_ALLOWED_INSTALL_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class IntegrityError(RuntimeError):
    """sha256 mismatch — treated as unrecoverable."""


class UnsafeArchiveError(RuntimeError):
    """Refused: archive contains symlink, device, or path-traversal entry."""


@dataclass
class InstallResult:
    ok: bool
    verify_bin_resolves: bool
    install_dir: Path
    message: str = ""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_url(url: str) -> None:
    """Refuse non-http(s) schemes — file://, ftp://, jar://, etc."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES:
        raise UnsafeArchiveError(
            f"refusing url with scheme {parsed.scheme!r}; "
            f"allowed: {sorted(_ALLOWED_URL_SCHEMES)}"
        )


def _safe_tar_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract a tarball after validating every member.

    Uses ``filter='data'`` on Python 3.11.4+. Falls back to manual
    validation (no symlinks, no hardlinks, no devices, no FIFOs, no
    absolute paths, no parent-directory traversal) on older Pythons.
    """
    try:
        tf.extractall(dest, filter="data")
        return
    except TypeError:
        pass  # Python <3.11.4 — fall back to manual validation
    except (tarfile.TarError, OSError) as exc:
        raise UnsafeArchiveError(f"tar extract refused by data filter: {exc}") from exc

    target_root = dest.resolve()
    safe_members: list[tarfile.TarInfo] = []
    for m in tf.getmembers():
        if m.issym() or m.islnk():
            raise UnsafeArchiveError(
                f"refusing to extract symlink/hardlink {m.name!r} from tarball"
            )
        if m.isdev() or m.isfifo():
            raise UnsafeArchiveError(
                f"refusing to extract device/fifo {m.name!r} from tarball"
            )
        # An empty/absolute name or one with .. components is rejected by
        # resolved-path containment.
        member_path = (dest / m.name).resolve()
        try:
            member_path.relative_to(target_root)
        except ValueError as exc:
            raise UnsafeArchiveError(
                f"tar member {m.name!r} resolves outside extract_dir"
            ) from exc
        safe_members.append(m)
    tf.extractall(dest, members=safe_members)


def _safe_zip_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract a zip archive after validating every member.

    zipfile.extractall sanitises ``..`` and absolute paths, but it still
    extracts symlinks (external_attr high nibble == 0xA) as regular
    files containing the target string on Python <3.12 and as actual
    symlinks on 3.12+. Both behaviours are unsafe — the symlink either
    becomes a confused-deputy bait file or escapes containment outright.
    Reject up front.
    """
    target_root = dest.resolve()
    for info in zf.infolist():
        # Symlinks: high four bits of external_attr's mode == 0xA000.
        mode = (info.external_attr >> 16) & 0xF000
        if mode == 0xA000:
            raise UnsafeArchiveError(
                f"refusing to extract symlink {info.filename!r} from zip"
            )
        member_path = (dest / info.filename).resolve()
        try:
            member_path.relative_to(target_root)
        except ValueError as exc:
            raise UnsafeArchiveError(
                f"zip member {info.filename!r} resolves outside extract_dir"
            ) from exc
    zf.extractall(dest)


def _validate_extract_path(extract_dir: Path, extract: str) -> Path:
    """Resolve `extract` against extract_dir and ensure containment.

    Pre-v0.14.6 a malicious marketplace `extract: "../../../"` escaped
    the temp dir entirely. shutil.copytree on the parent then dragged
    arbitrary host files into install_dir. Containment check closes that.
    """
    if extract == ".":
        return extract_dir
    target_root = extract_dir.resolve()
    candidate = (extract_dir / extract).resolve()
    try:
        candidate.relative_to(target_root)
    except ValueError as exc:
        raise UnsafeArchiveError(
            f"extract path {extract!r} resolves outside the extract dir"
        ) from exc
    return candidate


def _atomic_symlink(target: Path, link: Path) -> None:
    """Publish/retarget *link* -> *target* in one atomic step (temp symlink +
    ``os.replace``), so a concurrent exec of the launcher never observes it
    missing — the pre-#308 ``unlink`` + ``symlink_to`` pair had a window with
    no link at all. Shared by every install strategy."""
    tmp = link.parent / f".{link.name}.lnk-{uuid.uuid4().hex[:8]}"
    os.symlink(target, tmp)
    try:
        os.replace(tmp, link)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _locate_verify_bin(root: Path, verify_bin: str) -> Path | None:
    for candidate in [root / "bin" / verify_bin, root / verify_bin]:
        if candidate.is_file():
            return candidate
    for candidate in root.rglob(verify_bin):
        if candidate.is_file():
            return candidate
    return None


def _fsync_tree(root: Path) -> None:
    """Best-effort durability for a freshly staged tree (review round 2): the
    rename-based publication below is only old-or-new across a POWER crash if
    the renamed content itself reached disk first — see atomic_io's module
    docstring for the same reasoning. Installs are rare, so walking the tree
    is acceptable. Never raises."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            try:
                fd = os.open(os.path.join(dirpath, fname), os.O_RDONLY)
            except OSError:
                continue
            try:
                os.fsync(fd)
            except OSError:
                pass
            finally:
                os.close(fd)
        fsync_directory(dirpath)


def _reclaim_superseded_generations(
    gens_dir: Path, tools_bin: Path, verify_bin: str,
) -> None:
    """Start-of-install reclaim (review round 2): remove every generation of
    THIS plugin except the one the launcher currently serves, plus crashed
    staging leftovers.

    Running this at the START of the next install — never right after
    publication — leaves the superseded generation on disk for a whole
    install-to-install grace window, so an in-flight consumer that resolved
    the launcher just before a retarget can finish against its tree. The
    per-plugin ``gens_dir`` namespace means no glob can ever match another
    plugin's directories, whatever its name's prefix relationship."""
    # Terra r4-1 / Sol r5-1: on a verify_bin RENAME the incoming name has no
    # link yet, so serving-detection keyed on it alone would sweep the tree
    # the plugin's PRIOR launcher still serves — and keying on the manifest
    # instead fails open when the manifest is corrupt (it deliberately reads
    # as empty). So: a directory that ANY existing tools/bin launcher points
    # into is serving, whatever the manifest says. Bounded — one readlink
    # per published launcher.
    current_targets: list[Path] = []
    try:
        links = list(tools_bin.iterdir())
    except OSError:
        links = []
    for lnk in links:
        try:
            current_targets.append(Path(os.readlink(lnk)))
        except OSError:
            continue

    def _serving(candidate: Path) -> bool:
        return any(t.is_relative_to(candidate) for t in current_targets)

    try:
        entries = list(gens_dir.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if not _serving(entry):
            shutil.rmtree(entry, ignore_errors=True)


def install_tarball(
    *,
    plugin_name: str,
    spec: dict,
    tools_root: Path,
    timeout: int = 120,
) -> InstallResult:
    url = spec["url"]
    expected = spec["sha256"]
    extract = spec.get("extract", ".")
    verify_bin = spec.get("verify_bin")
    install_cmd = spec.get("install_cmd")
    version = spec.get("version", "latest")

    _validate_url(url)
    # #308: validate install_cmd's shape up front — pre-fix this ran after
    # the existing install had already been destroyed, so a malformed spec
    # left no working install behind.
    if install_cmd is not None and (
        not isinstance(install_cmd, list)
        or not all(isinstance(a, str) for a in install_cmd)
    ):
        raise UnsafeArchiveError(
            "install_cmd must be a list of strings (argv); "
            "shell-string form was removed in v0.14.6 to close a "
            "marketplace-authored shell-injection vector"
        )

    # Review round 2 (Terra P1-1): a tarball requirement with no usable
    # verify_bin can never succeed (the orchestrator gates on the launcher
    # resolving), so refuse before ANY filesystem mutation — pre-fix it ran
    # the whole install, published nothing, and still reclaimed the prior
    # generations, dangling the old launcher. plugin_store.manifest_sysreqs
    # refuses this at manifest level; this is the belt for direct callers.
    if not verify_bin or not isinstance(verify_bin, str):
        return InstallResult(
            ok=False, verify_bin_resolves=False,
            install_dir=tools_root / "tarball" / plugin_name,
            message="tarball requirement declares no verify_bin",
        )

    # #354: refuse up front if another plugin already publishes this bin name
    # (manifest row OR — corrupt-manifest-proof — a live launcher pointing
    # into another plugin's tree; Sol r5-1).
    ensure_bin_claim(verify_bin, plugin_name, tools_root)

    tools_root.mkdir(parents=True, exist_ok=True)
    tools_bin = tools_root / "bin"
    tools_bin.mkdir(parents=True, exist_ok=True)
    # #308 (review round 2): installs land in a UNIQUE generation directory
    # under a PER-PLUGIN namespace (`tarball/<plugin>/<version>.g-<nonce>`),
    # never in place. The serving generation is never moved or deleted by
    # this install: publication is one atomic launcher retarget, and
    # superseded generations are reclaimed only at the START of the NEXT
    # install (an install-to-install grace window for in-flight consumers
    # of the old tree). The per-plugin directory makes reclamation immune
    # to plugin-name prefix collisions and covers version changes too.
    gens_dir = tools_root / "tarball" / plugin_name
    gens_dir.mkdir(parents=True, exist_ok=True)
    # Terra r3-1: make the (possibly just-created) ancestor chain durable
    # BEFORE anything is published beneath it — a power loss must never
    # recover the durable launcher while losing a new ancestor's entry.
    fsync_directory(tools_root.parent)
    fsync_directory(tools_root)
    fsync_directory(tools_root / "tarball")
    _reclaim_superseded_generations(gens_dir, tools_bin, verify_bin)
    install_dir = gens_dir / f"{version}.g-{uuid.uuid4().hex[:8]}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "archive"
        # H13: bound the download by `timeout` (per blocking socket op). The
        # old urlretrieve() had no timeout and used the global default (None),
        # so a stalled marketplace server hung the caller — and, since this
        # runs on casa-main's single event loop, the entire add-on — forever.
        with urllib.request.urlopen(url, timeout=timeout) as resp, \
                archive.open("wb") as out:  # noqa: S310 (scheme validated above)
            shutil.copyfileobj(resp, out)
        actual = _sha256(archive)
        if actual != expected:
            raise IntegrityError(
                f"sha256 mismatch for {url}: got {actual}, expected {expected}"
            )

        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zf:
                _safe_zip_extract(zf, extract_dir)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive) as tf:
                _safe_tar_extract(tf, extract_dir)
        else:
            raise RuntimeError(f"unsupported archive format: {url}")

        source = _validate_extract_path(extract_dir, extract)

        # Build the replacement in a hidden staging sibling inside the
        # plugin's own generation namespace (same filesystem, so the
        # publication rename below is atomic; crashed leftovers are swept
        # by the start-of-install reclaim).
        staging_root = Path(tempfile.mkdtemp(dir=gens_dir, prefix=".staging-"))
        try:
            staging_tree = staging_root / "tree"
            shutil.copytree(source, staging_tree)

            if install_cmd is not None:
                env = {
                    "CASA_TOOLS": str(tools_root),
                    "PATH": _ALLOWED_INSTALL_PATH,
                }
                subprocess.run(
                    install_cmd, shell=False, cwd=staging_tree,
                    env=env, check=True, timeout=timeout,
                )

            # Locate verify_bin in the staged tree BEFORE publishing: a
            # replacement that would not provide the declared binary must
            # not displace a working install.
            found = _locate_verify_bin(staging_tree, verify_bin)
            if found is None:
                return InstallResult(
                    ok=True,
                    verify_bin_resolves=False,
                    install_dir=install_dir,
                    message=(
                        f"verify_bin {verify_bin!r} not present in staged "
                        "tree; existing install left untouched"
                    ),
                )
            rel_bin = found.relative_to(staging_tree)

            # Power-crash durability (review round 2): the staged bytes must
            # be on disk BEFORE the renames that make them reachable, and
            # each rename's directory is fsynced after it — otherwise a
            # power loss could recover a durable launcher pointing at an
            # incomplete or missing generation.
            _fsync_tree(staging_tree)
            # Land the verified tree at its unique generation path — the
            # target never pre-exists, so this cannot displace anything.
            os.replace(staging_tree, install_dir)
            fsync_directory(gens_dir)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    # Publish: atomically retarget the launcher symlink at the new
    # generation. Until this single os.replace the previous generation's
    # link (and tree) keep serving; after it, the new one does. No unlink
    # gap — and the previous generation stays on disk until the next
    # install's reclaim, so in-flight consumers of it are undisturbed.
    source_bin = install_dir / rel_bin
    _atomic_symlink(source_bin, tools_bin / verify_bin)
    fsync_directory(tools_bin)
    link = tools_bin / verify_bin
    resolves = link.is_symlink() and link.resolve().is_file()

    return InstallResult(
        ok=True,
        verify_bin_resolves=resolves,
        install_dir=install_dir,
        message="installed via tarball",
    )
