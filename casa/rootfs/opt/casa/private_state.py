"""Declared inventory of private runtime state, and the pass that enforces it.

GHSA-569r-7crq-xr43. Since v0.170.0 (containment Stage 2) each ``claude_code``
executor engagement runs under its own non-root uid with an empty capability
set. Root-owned runtime state was nevertheless being created world-readable, so
that uid could read the Supervisor/HASSIO bearer tokens (and through the
Supervisor API, transitively, every add-on's stored secrets), the global webhook
secret, up to ~21 MB of a *sibling* engagement's stdout, and the resident
agent's transcripts.

The cause was systemic rather than a set of oversights — there are three write
idioms in the tree (``atomic_io``, a hand-rolled temp-file replace, and shell
redirection), and ``atomic_io.atomic_write_text`` deliberately lands a fresh
file at ``0644`` when no ``mode=`` is passed so that ``mkstemp``'s ``0600``
cannot leak onto a replaced inode. This module is therefore the single source of
truth for *which* paths are private and at *what* mode, independent of which
idiom wrote them.

Two things follow from that, and both are load-bearing:

**The pass is what repairs a deployed install.** Correct write sites alone do
not: on an upgraded instance every affected file already exists at ``0644``, and
``mode=None`` preserves an existing file's mode, so a file whose next write is
days away (``topic-ledger.json``) would stay exposed until then. :func:`enforce`
runs on every boot and repairs what it finds.

**Directories are tightened, never walked.** Removing ``x`` for others on
``/config/cc-home`` makes every path beneath it unreachable regardless of the
modes inside, so the pass never recurses and never has to make a decision about
the ~198 files in there. What it must NOT do is tighten ``/data`` or
``/config`` themselves: the dropped uid traverses both to reach its own
workspace and its assigned plugin artifacts.

The three ``CREDENTIAL`` entries are special only in what a *failure to repair
them* means. :func:`credential_modes_ok` is re-checked at each point of use that
can put a ``claude_code`` engagement on the road — the fresh-launch uid-drop
preflight, boot replay's per-record resume, and uid allocation — rather than
latched once at boot. A latch was the original design and was cut: it needed a
carrier, clearing semantics, and a guarantee that every start path consulted it,
and two review rounds each found a path it failed to cover. A ``stat`` of three
files at the point of use cannot go stale and cannot be forgotten by a path that
does not exist yet.
"""
from __future__ import annotations

import glob
import logging
import os
import stat
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Failure to repair one of these means Casa cannot honour the guarantee this
#: module exists for, so uid-dropped engagements refuse to start.
CREDENTIAL = "credential"
#: Confidentiality-only. A failure logs ERROR and refuses nothing.
PRIVATE = "private"


@dataclass(frozen=True)
class Entry:
    """One declared path. ``why`` is the audit trail: it says what becomes
    readable if this entry regresses, so a later reader can judge the mode
    without re-deriving the threat model."""

    path: str
    mode: int
    kind: str
    why: str


# --- files ---------------------------------------------------------------
# Individually tightened because /data itself must stay traversable.
FILES: tuple[Entry, ...] = (
    Entry("/run/s6/container_environment/SUPERVISOR_TOKEN", 0o600, CREDENTIAL,
          "Supervisor API bearer: reads every add-on's stored options, so it "
          "transitively exposes the Claude OAuth token, the GitHub PAT and the "
          "1Password service-account token. engagement_run_template.sh unsets "
          "it from the environment; that is theatre while the file is readable."),
    Entry("/run/s6/container_environment/HASSIO_TOKEN", 0o600, CREDENTIAL,
          "Second bearer for the same Supervisor API surface."),
    Entry("/data/webhook_secret", 0o600, CREDENTIAL,
          "Global HMAC secret: allows forging signed inbound /invoke/* calls."),

    Entry("/data/sessions.json", 0o600, PRIVATE,
          "SDK session ids and per-channel bindings for the whole fleet."),
    Entry("/data/topic-ledger.json", 0o600, PRIVATE,
          "Terminal topic records across every channel."),
    Entry("/data/jobs.json", 0o600, PRIVATE,
          "Delegated job rows: creator identity, task, context, result and "
          "failure text for resident and specialist work."),
    Entry("/data/plugin-health.json", 0o600, PRIVATE,
          "Per-plugin health state, regenerated every boot."),
    Entry("/data/plugin-setup-episodes.json", 0o600, PRIVATE,
          "Plugin setup transcripts and their outcomes."),

    Entry("/data/callback_acks.json", 0o600, PRIVATE,
          "Consent ledger — operator decisions."),
    Entry("/data/event_acks.json", 0o600, PRIVATE,
          "Consent ledger — operator decisions."),
    Entry("/data/webhook_trigger_acks.json", 0o600, PRIVATE,
          "Consent ledger — operator decisions."),
    Entry("/data/persona_install_acks.json", 0o600, PRIVATE,
          "Consent ledger — operator decisions."),
    Entry("/data/specialist_install_acks.json", 0o600, PRIVATE,
          "Consent ledger — operator decisions."),
)

# --- file globs ----------------------------------------------------------
FILE_GLOBS: tuple[Entry, ...] = (
    Entry("/data/*.corrupt", 0o600, PRIVATE,
          "Quarantine alias: session_registry archives a corrupt sessions.json "
          "aside with os.replace, which PRESERVES the pre-upgrade 0644 inode "
          "under a name no per-file entry covers."),
    Entry("/data/*.casabak", 0o600, PRIVATE,
          "Quarantine alias, as above (topic_ledger). config_sync's own "
          ".casabak sidecars live under /config and are unaffected."),
)

# --- directories ---------------------------------------------------------
# Tightening the directory alone is sufficient: without `x` for others nothing
# beneath it is reachable by name.
DIRS: tuple[Entry, ...] = (
    Entry("/data/cold-retain-retry", 0o700, PRIVATE,
          "Pending cold-retain records: SDK session ids, transcript dirs and "
          "speaker provenance."),
    Entry("/config/cc-home", 0o700, PRIVATE,
          "The resident agent's Claude home, including its transcripts."),
    Entry("/config/.git", 0o700, PRIVATE,
          "/config snapshot history, which can still hold values that were "
          "later tightened to 0600 in the working tree."),
)

DIR_GLOBS: tuple[Entry, ...] = (
    Entry("/var/log/casa-engagement-*", 0o700, PRIVATE,
          "A SIBLING engagement's captured stdout, up to ~21 MB each. This is "
          "the cross-engagement boundary containment Stage 2 exists to create."),
)


@dataclass
class Report:
    """Outcome of one :func:`enforce` pass."""

    #: (path, old_mode, new_mode) for each repair actually performed.
    changed: list[tuple[str, int, int]] = field(default_factory=list)
    #: (path, error) for every path that could not be repaired.
    failures: list[tuple[str, str]] = field(default_factory=list)
    #: Subset of ``failures`` whose entry is ``CREDENTIAL``.
    credential_failures: list[str] = field(default_factory=list)
    #: Declared paths that turned out to be symlinks and were left alone.
    skipped_symlinks: list[str] = field(default_factory=list)


def _resolve(root: str, declared: str) -> str:
    """Join an absolute declared path under *root*.

    ``root`` is ``"/"`` in production and a tmp dir in tests; it is not a
    security boundary, just a test seam.
    """
    if root in ("", "/"):
        return declared
    return os.path.join(root, declared.lstrip("/"))


def _targets(root: str) -> list[tuple[str, Entry]]:
    """Every concrete (path, entry) pair to act on, globs already expanded."""
    out: list[tuple[str, Entry]] = []
    for entry in FILES + DIRS:
        out.append((_resolve(root, entry.path), entry))
    for entry in FILE_GLOBS + DIR_GLOBS:
        for hit in sorted(glob.glob(_resolve(root, entry.path))):
            out.append((hit, entry))
    return out


def enforce(*, root: str = "/") -> Report:
    """Repair the mode of every declared path that exists.

    Absent paths are skipped silently — most of the inventory is created
    lazily, and a fresh install legitimately has almost none of it. A declared
    path that is a *symlink* is skipped and reported rather than followed:
    ``chmod`` resolves symlinks, and repairing "through" one would apply a
    private mode to whatever it points at. (No engagement uid can plant such a
    symlink today — every parent directory here is root-owned and not
    world-writable — so this is a guard against corruption and against a future
    writer, not a live race.)

    Never raises. The caller decides what a credential-class failure means; see
    :func:`credential_modes_ok`.
    """
    report = Report()
    for path, entry in _targets(root):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            report.failures.append((path, str(exc)))
            if entry.kind == CREDENTIAL:
                report.credential_failures.append(path)
            logger.error("private-state: lstat(%s) failed: %s", path, exc)
            continue

        if stat.S_ISLNK(info.st_mode):
            report.skipped_symlinks.append(path)
            logger.error(
                "private-state: %s is a symlink; refusing to chmod through it "
                "(expected a real %s)", path,
                "directory" if entry.mode & 0o100 else "file",
            )
            if entry.kind == CREDENTIAL:
                report.credential_failures.append(path)
            continue

        current = stat.S_IMODE(info.st_mode)
        if current == entry.mode:
            logger.debug("private-state: %s already %o", path, entry.mode)
            continue
        try:
            os.chmod(path, entry.mode)
        except OSError as exc:
            report.failures.append((path, str(exc)))
            if entry.kind == CREDENTIAL:
                report.credential_failures.append(path)
            logger.error(
                "private-state: chmod(%s, %o) failed: %s — %s",
                path, entry.mode, exc, entry.why,
            )
            continue
        report.changed.append((path, current, entry.mode))
        logger.info(
            "private-state: repaired %s %o -> %o", path, current, entry.mode)

    if report.credential_failures:
        logger.critical(
            "private-state: %d credential path(s) could not be made private "
            "(%s) — uid-dropped engagements will refuse to start",
            len(report.credential_failures),
            ", ".join(report.credential_failures),
        )
    return report


def credential_modes_ok(*, root: str = "/") -> list[str]:
    """The offending credential paths, or ``[]`` when all are private.

    Called at each point of use that can start a uid-dropped engagement, rather
    than consulted from a boot-time latch — see the module docstring for why the
    latch was cut. Absence is not an offence: a path that does not exist exposes
    nothing, and refusing on it would break a fresh install that has not
    generated its webhook secret yet.
    """
    offenders: list[str] = []
    for entry in FILES:
        if entry.kind != CREDENTIAL:
            continue
        path = _resolve(root, entry.path)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.error("private-state: lstat(%s) failed: %s", path, exc)
            offenders.append(path)
            continue
        if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            offenders.append(path)
    return offenders
