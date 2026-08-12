"""The ``/data/callbacks`` spool — the authorization-callback protocol
(dirs/ready/index layout, claim/TTL/publish, the mint contract,
sweep/recovery; INV-CB-002).

An unauthenticated browser redirect deposits a short-lived bearer credential
(an OAuth authorization code) into this spool; an ephemeral consumer process
picks it up. Casa is the untrusted middle: it never parses what a consumer
minted and never keeps a credential longer than its own TTL. Three rules make
that safe, and every method below is written to preserve them:

**One clock — mtime.** ``rename(2)`` and ``link(2)`` preserve an inode's
mtime, so a pending file's mtime *is* its mint time and survives the claim;
a result's mtime is its final-write time (the inode records the last write,
not the publication instant). Each TTL therefore runs off its OWN file's
mtime: a 29-minute authorization flow must not have its freshly-written
result expire on arrival. Casa never reads or parses a pending file's
content — no consumer-supplied timestamps exist anywhere in the protocol. A
materially FUTURE mtime (beyond ``SKEW_S``) is fail-closed everywhere: the
entry is deleted, so a forward clock jump can never mint records that regain
validity when the clock returns.

**Publish-once, never a partial.** Nothing in ``pending/``, ``.claims/`` or
``results/`` is ever created by an overwriting rename. Every publication is a
``link(2)`` of an already-complete inode, whose ``EEXIST`` is the atomic
arbiter: the claim (``pending/<h>.json`` → ``.claims/<h>``) has exactly one
winner however many processes race it, and a replayed redirect can never
rewrite a result. A collector polling ``results/`` can never observe a short
record, because the name only ever appears already-written and fsynced.

**Fail-closed FDs.** All work is openat-relative to directory FDs opened
``O_NOFOLLOW`` from a pinned root FD, so a swapped symlink or a concurrent
plugin removal absorbs writes into an unlinked directory instead of racing a
recreation. A closed instance never falls through to a ``dir_fd=None`` op
(which would resolve against the process CWD).

Deliberately **simpler than** ``plugin_outbox``'s ``.reap`` ownership
protocol: that machinery exists for same-name republication by an untrusted
producer, which is structurally absent here — names are sha256 hashes of
fresh random states, so "the name now denotes a different, fresher inode" is
not a case that arises.

Same-uid processes are outside the threat model (as for ``plugin_outbox``):
all plugin processes run as root in one container. Dirs are 0770 and files
0600 as defence in depth, not as an inter-plugin boundary.

Leaf module (stdlib plus the pure ``callback_attempts`` sibling): importable
from the reconciler, the HTTP handler, the sweeper job and a consumer test
alike.
"""
from __future__ import annotations

import enum
import errno
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import callback_attempts

logger = logging.getLogger(__name__)

SPOOL_ROOT_ENV = "CASA_CALLBACK_SPOOL_ROOT"
ROOT = Path("/data/callbacks")

# TTLs and allowances — all in seconds, all measured against a
# file's OWN mtime.
SKEW_S = 300                 # future-mtime allowance; beyond it: fail closed
PENDING_TTL_S = 1800         # a minted state is claimable for 30 min
RESULT_TTL_S = 900           # a plaintext code is retained for 15 min at most
RESTORE_GRACE_S = 60         # periodic recovery never restores a young claim
TEMP_TTL_S = 300             # `.part` / `.tmp-<hash>` residue age-sweep
QUIESCENCE_S = 24 * 3600     # orphan-dir GC floor
MAX_PENDING = 256            # per-plugin caps: a buggy consumer must not
MAX_RESULTS = 256            # fill /data
MAX_ATTEMPTS = 2048          # attempt files are a few hundred bytes; the cap
#                              bounds disk and scan cost, not policy
MAX_COLLECT = 64             # consumer-held `.collect-*` inodes per plugin
REMOVAL_RECORD_PRUNE_S = 7 * 24 * 3600     # a NOTED removal record is kept a
#                              week (the operator can still ask about it),
#                              then pruned
REMOVAL_RECORD_MAX_AGE_S = 30 * 24 * 3600  # hard bound, NOTED records only
#                              (#532: un-noted = a notice still owed — never
#                              age-pruned; accumulation is bounded by the
#                              plugin-removal rate)
MARKER_STATE_MAX_BYTES = 1 << 16   # a ready.json / index entry read back for
#                              the reconcile payload compare is tiny (a few
#                              hundred bytes); anything past this small cap is
#                              INVALID, never a large read

REMOVAL_SCHEMA_VERSION = 1
REMOVAL_REASONS = frozenset({"remove", "orphan_gc"})
_REMOVAL_KEYS = frozenset({"v", "plugin", "count", "reason", "ts", "noted",
                           "noted_ts"})

PENDING_DIR = "pending"
RESULTS_DIR = "results"
CLAIMS_DIR = ".claims"
ATTEMPTS_DIR = "attempts"    # per-flow attempt ledger (casa-written; the
#                              consumer's one verb there is the ack rename)
INDEX_DIR = ".index"
REMOVALS_DIR = ".removals"   # root-level removal-record store — a reserved
#                              dot-root name, like every dot-prefixed root entry
ACK_PREFIX = ".ack-"         # consumer receipt-token grammar under attempts/
READY_NAME = "ready.json"
DIR_ID_NAME = ".dir-id"      # per-plugin-dir identity token (see Claim)
TEMP_PREFIX = ".tmp-"        # reserved result-temp grammar under .claims/
COLLECT_PREFIX = ".collect-"  # consumer-held claim under results/
PART_SUFFIX = ".part"
REPLACE_TEMP_INFIX = ".tmp-"  # staging grammar of _replace_json: `.<name>.tmp-…`

DIR_MODE = 0o770
FILE_MODE = 0o600

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_FILE_FLAGS = (os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
                   | os.O_CLOEXEC)
#: Marker reads are NON-BLOCKING: O_NONBLOCK means opening a FIFO (or any other
#: pipe/device masquerading as ready.json) returns immediately instead of
#: blocking for a writer, so a hostile/garbage on-disk marker can never hang
#: the reconciler; the ``S_ISREG`` gate then rejects it before any read.
_MARKER_READ_FLAGS = (os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
                      | os.O_CLOEXEC)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DIR_ID_RE = re.compile(r"^[0-9a-f]{32}$")
#: The ONLY attempt fields the delivery worker may set
#: (:meth:`CallbackSpool.update_attempt_nudge`). Status, outcome, identity and
#: the opaque ``meta`` stay the spool's own derivation.
_NUDGE_FIELDS = frozenset({"nudges", "last_nudge_ts", "next_nudge_ts",
                           "deferrals", "noted"})


class MarkerState(enum.Enum):
    """Three-state result of a durable-marker read (:meth:`CallbackSpool.
    read_marker`). ABSENT (no file) and INVALID (present but non-regular /
    unreadable / oversized / malformed) are DISTINCT: a stale-but-INVALID
    marker must be republished, never mistaken for absent (and so left to
    survive a failed rewrite)."""

    ABSENT = "absent"
    INVALID = "invalid"
    PRESENT = "present"


@dataclass(frozen=True)
class Marker:
    """A durable marker's on-disk state and, when PRESENT, its payload.

    ``raw`` carries the exact on-disk bytes (PRESENT only), so the reconcile
    can compare BYTE-STRICTLY against :func:`canonical_marker_bytes` of the
    desired payload — a reorder, a type diff, an extra key or a whitespace diff
    all differ and are rewritten, while casa's own freshly-written marker (same
    canonical helper) is byte-identical and so never churns."""

    state: MarkerState
    payload: "dict | None" = None
    raw: "bytes | None" = None


class SpoolClosed(RuntimeError):
    """Raised by administrative operations on a closed spool. The request-path
    operations (:meth:`CallbackSpool.claim` / :meth:`publish_result`) return a
    neutral refusal instead — they must never raise into the HTTP handler."""


# ---------------------------------------------------------------------------
# names / keys
# ---------------------------------------------------------------------------


def canonical_marker_bytes(payload: dict) -> bytes:
    """The ONE canonical on-disk form of a durable marker's payload — the exact
    bytes the marker WRITER (:meth:`CallbackSpool._replace_json`, i.e.
    ``write_ready`` / ``write_index_entry``) emits, and the exact bytes the
    reconcile compares an on-disk marker against.

    Sorted keys + the most compact separators + no ASCII-escaping, UTF-8. Because
    writer and compare share this single helper, casa's own freshly-written
    marker is always byte-identical to ``canonical_marker_bytes(desired)`` — a
    steady-state pass finds it unchanged (no churn, no serialization-drift
    footgun) while ANY on-disk drift (a key reorder, a ``true``/``1.0`` type
    diff, an extra key, a whitespace diff) differs and is rewritten.

    ``allow_nan=False`` (in lockstep with ``callback_attempts._canonical_text``,
    its pure-side twin): a non-finite float would otherwise be emitted as the
    NON-STANDARD ``NaN``/``Infinity`` literal — bytes no conforming reader
    accepts and no fail-closed validator takes back, so the record would be
    write-only. Raising here instead is what every writer already handles
    (``TypeError``/``ValueError`` ⇒ the write is refused, nothing is
    published)."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def state_hash(state: str) -> str:
    """The spool name for a consumer-minted ``state`` — sha256 hex. The hash,
    not the state, is what casa and the delivery nudge ever handle."""
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def index_key(artifact_realpath: str) -> str:
    """Discovery key for ``.index/`` — sha256 of the RESOLVED artifact root.

    A consumer computes ``sha256(realpath($CLAUDE_PLUGIN_ROOT))``, the one
    value it provably knows; it cannot know its casa registry name (bundled
    plugins are registered under scoped ``slug.manifest_name``). Resolving
    here as well as caller-side keeps the two ends symmetric when either is
    handed a symlinked path."""
    return hashlib.sha256(
        os.path.realpath(os.fspath(artifact_realpath)).encode("utf-8"),
    ).hexdigest()


def in_flight_key(plugin: str, state_hash_hex: str) -> str:
    """Key of the in-process in-flight set. Plugin-qualified: hashes are only
    unique within a plugin's own spool dir."""
    return f"{plugin}/{state_hash_hex}"


def spool_root() -> Path:
    """The configured spool root (``CASA_CALLBACK_SPOOL_ROOT`` overrides the
    ``/data`` default — the env key is exported to plugin subprocesses)."""
    return Path(os.environ.get(SPOOL_ROOT_ENV) or ROOT)


def _safe_component(name: str) -> bool:
    """True iff *name* is a single control-free path component (not . / ..)."""
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\0" in name:
        return False
    return not any(ord(c) < 0x20 for c in name)


def _is_hash(name: str) -> bool:
    return bool(_HASH_RE.match(name))


def _is_replace_temp(name: str) -> bool:
    """True for :meth:`CallbackSpool._replace_json` staging residue
    (``.ready.json.tmp-<pid>-<uuid>``, ``.<index-key>.json.tmp-…``) — a crash
    between staging and the rename leaves one, and nothing else would ever
    remove it."""
    return name.startswith(".") and REPLACE_TEMP_INFIX in name


def _hash_of_pending(name: str) -> str | None:
    if name.endswith(".json") and _is_hash(name[:-5]):
        return name[:-5]
    return None


def _is_clock(value) -> bool:
    """A usable clock field: a real, FINITE number (never a bool). Finiteness
    is not pedantry — a ``NaN`` slipped into ``ts``/``noted_ts`` makes every
    age comparison False, which would make the record immortal in exactly the
    store whose whole point is a hard age bound."""
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _is_removal_name(name: str) -> bool:
    """The removal-record file grammar: a plain ``<plugin>-<uuid4hex>.json``
    component. Dot-prefixed names (this module's own staging residue) are
    excluded, so a listing can never mistake a half-written stage for a
    record."""
    return (_safe_component(name) and not name.startswith(".")
            and name.endswith(".json"))


def _validate_removal(obj) -> dict | None:
    """Total fail-closed validation of a removal record — a copy, or ``None``.

    Same discipline as :func:`callback_attempts.validate_attempt`: exact key
    set, real bools, finite numbers, a known ``reason``, and a CONSISTENCY
    gate between ``noted`` and ``noted_ts`` (a record claiming it was noted
    but carrying no clock would be pruned by neither rule — it would sit
    there until the hard bound with no way to tell the worker it is done).
    Never raises: a corrupt file must read as invalid so the reader retires
    it, never as an exception into the worker pass."""
    try:
        if not isinstance(obj, dict) or set(obj) != _REMOVAL_KEYS:
            return None
        if isinstance(obj["v"], bool) or obj["v"] != REMOVAL_SCHEMA_VERSION:
            return None
        plugin = obj["plugin"]
        if not isinstance(plugin, str) or not _safe_component(plugin) \
                or plugin.startswith("."):
            return None
        count = obj["count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        if obj["reason"] not in REMOVAL_REASONS:
            return None
        if not _is_clock(obj["ts"]):
            return None
        if not isinstance(obj["noted"], bool):
            return None
        noted_ts = obj["noted_ts"]
        if noted_ts is not None and not _is_clock(noted_ts):
            return None
        if obj["noted"] != (noted_ts is not None):
            return None
        return dict(obj)
    except Exception:  # noqa: BLE001 — total by contract
        return None


def _hash_of_collect(name: str) -> str | None:
    """The hash a consumer-held ``.collect-<64hex>-<uuid>`` entry names, or
    ``None`` for anything that only resembles one (a near-miss is residue, not
    a hold). The single grammar shared by enumeration and the sweep, so a name
    casa will not attribute to a flow is never given that flow's outcome."""
    if not name.startswith(COLLECT_PREFIX):
        return None
    rest = name[len(COLLECT_PREFIX):]
    if len(rest) >= 65 and rest[64] == "-" and _is_hash(rest[:64]):
        return rest[:64]
    return None


# ---------------------------------------------------------------------------
# low-level fd helpers
# ---------------------------------------------------------------------------


def _fsync(fd: int, what: str) -> None:
    """Best-effort fsync following the ``atomic_io`` convention: at every call
    site the content is already committed and the caller's decision has been
    made, so misreporting a completed operation as failed would be strictly
    worse than the lost ordering guarantee (which only matters across a power
    crash). Repeated failure surfaces via these warnings."""
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.warning("callback-spool: fsync of %s failed: %s", what, exc)


class FsyncFailed(OSError):
    """An fsync whose failure the caller MUST observe — the strict-durability
    counterpart of :func:`_fsync`'s convention. At a strict call site the
    caller's decision has NOT yet been made: a write-ahead outcome must be
    proven durable before the deletion that depends on it, so a swallowed
    fsync failure would let a crash erase the record while the deletion
    survives."""


def _fsync_strict(fd: int, what: str) -> None:
    """Strict fsync: raises :class:`FsyncFailed` on ANY failure (chaining the
    original), so the caller skips its dependent action this pass and a later
    pass retries. *what* names a directory or a role — never a hash
    (INV-CB-006: an ``FsyncFailed`` may be logged by its catcher)."""
    try:
        os.fsync(fd)
    except OSError as exc:
        raise FsyncFailed(
            exc.errno if exc.errno is not None else errno.EIO,
            f"fsync of {what} failed") from exc


def _open_dir(name: str, dir_fd: int) -> int:
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def _lstat_quiet(name: str, dir_fd: int):
    try:
        return os.lstat(name, dir_fd=dir_fd)
    except OSError:
        return None


#: Sentinel for :func:`_marker_lstat`: a metadata read that FAILED (any OSError
#: other than ENOENT), so the entry's presence is UNKNOWN — distinct from a
#: genuine absence (``None``). Never conflated with "gone".
_LSTAT_ERROR: object = object()


def _marker_lstat(name: str, dir_fd: int):
    """``lstat`` for the marker retirement/confirmation path, three-valued:

    * a ``stat_result`` — the entry is PRESENT;
    * ``None`` — the entry is genuinely ABSENT (``FileNotFoundError`` / ENOENT);
    * :data:`_LSTAT_ERROR` — a metadata read FAILED for any other reason
      (EACCES, EIO, ELOOP, ENOTDIR, …), so presence is UNKNOWN.

    Unlike :func:`_lstat_quiet` — which maps EVERY error to ``None`` and is
    therefore only safe where "absent-on-any-error" is a fail-SAFE best-effort
    peek — this never reads a non-ENOENT failure as absence. A retirement that
    cannot confirm removal must report FAILURE (so the reconciler surfaces
    ``callback_spool_error``), never a false success that leaves a surviving
    marker mistaken for gone."""
    try:
        return os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        return _LSTAT_ERROR


def _regular_stat(name: str, dir_fd: "int | None"):
    """``lstat`` of *name* when it is a REGULAR file, else ``None`` — the
    "is this artifact usable as a derivation source" probe. A missing
    directory FD, a vanished entry and a non-regular inode (a swapped-in
    FIFO/symlink/directory whose content must never be read) all degrade to
    ``None``: the derivation simply loses that source and falls through to
    the next one."""
    if dir_fd is None:
        return None
    st = _lstat_quiet(name, dir_fd)
    return st if st is not None and stat.S_ISREG(st.st_mode) else None


def _listdir_quiet(dir_fd: int) -> list[str]:
    try:
        return os.listdir(dir_fd)
    except OSError:
        return []


def _unlink_quiet(name: str, dir_fd: int) -> bool:
    try:
        os.unlink(name, dir_fd=dir_fd)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        # Never log the entry name: spool names are state hashes, and the log
        # surfaces are the ones INV-CB-006 keeps free of callback identifiers.
        logger.warning("callback-spool: unlink failed (errno %s)", exc.errno)
        return False


def _remove_entry(name: str, dir_fd: int, st) -> bool:
    """Remove any entry type. A directory under ``pending/`` or ``results/``
    is impossible in the protocol, so it is residue — ``rmtree`` (dir_fd is
    3.11+; the container base is 3.12) rather than a failing unlink."""
    try:
        if st is not None and stat.S_ISDIR(st.st_mode):
            shutil.rmtree(name, dir_fd=dir_fd)
            return True
    except OSError as exc:
        logger.warning("callback-spool: rmtree failed (errno %s)", exc.errno)
        return False
    return _unlink_quiet(name, dir_fd)


def _retire_marker_entry(name: str, dir_fd: int) -> bool:
    """Retire a durable-marker entry of ANY type (regular file, directory,
    symlink, FIFO, …) openat-relative to *dir_fd*.

    Returns True when the entry is now ABSENT — removed, or already gone — and
    False when a genuine removal FAILED and the entry SURVIVES. The caller must
    surface a False rather than treat it as absent (a stale/invalid marker that
    was NOT removed still blocks republication). Type-aware via ``_remove_entry``
    (``lstat`` + dir-vs-file), so a directory-shaped marker is ``rmtree``d rather
    than left behind by a raw ``unlink`` that assumes a regular file.

    Both the pre-removal type probe and the post-removal re-confirmation use
    :func:`_marker_lstat`, so ONLY a genuine ENOENT counts as absence: a
    metadata failure (EACCES/EIO/…) BEFORE removal is NOT read as "already
    gone" (it fails closed to False), and the same failure on the
    RE-CONFIRMATION is NOT read as "now gone" (a removal whose success cannot
    be confirmed is reported as failure, not a silent false success). A benign
    remove/vanish race still reports success (the confirmation sees ENOENT)."""
    st = _marker_lstat(name, dir_fd)
    if st is None:
        return True                          # genuinely absent (ENOENT)
    if st is _LSTAT_ERROR:
        return False                         # presence UNKNOWN — fail closed
    _remove_entry(name, dir_fd, st)
    return _marker_lstat(name, dir_fd) is None   # True only on confirmed ENOENT


def _write_new_file(name: str, dir_fd: int, data: bytes) -> None:
    """Create *name* exclusively (0600), write it whole, fsync it. Raises on
    any failure, leaving no partially-visible final name (the caller only ever
    publishes an already-fsynced inode by link)."""
    fd = os.open(name, _NEW_FILE_FLAGS, FILE_MODE, dir_fd=dir_fd)
    try:
        # The requested mode is only ever narrowed by the umask, never
        # widened — but pin it so the on-disk mode is deterministic.
        os.fchmod(fd, FILE_MODE)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        _fsync(fd, "staged file")
    finally:
        os.close(fd)


#: Sentinel for a token whose state could not be ESTABLISHED (an I/O failure
#: mid-probe). Distinct from ``None`` (positively absent or malformed) for the
#: same reason ``_LSTAT_ERROR`` is distinct from ENOENT: an unknowable state
#: is never grounds for the repair path to retire a possibly-valid token.
_TOKEN_ERROR = object()

#: Sentinel for an inventory scan whose subject could not be ESTABLISHED (a
#: directory that would not open for anything but ENOENT). Distinct from
#: ``None`` (genuinely absent, i.e. provably nothing to settle) for the same
#: reason the two sentinels above are: an unknowable state is never grounds
#: for the purge it would license.
_SCAN_ERROR: object = object()


def _classify_dir_token(dir_fd: int):
    """Three-state probe of the plugin dir's identity token (``.dir-id``):
    a valid token string; ``None`` when the entry is POSITIVELY absent or
    malformed (non-regular, wrong size, bad grammar — safe to repair); or
    :data:`_TOKEN_ERROR` when an ``open``/``fstat``/``read`` failure left its
    state unknowable (never safe to repair). A stat-pair compare alone cannot
    carry directory identity — ext4 recycles a freed inode number
    immediately, so a recreated directory can present the exact
    ``(st_dev, st_ino)`` of the one it replaced."""
    try:
        fd = os.open(DIR_ID_NAME, _MARKER_READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None                          # positively absent
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None                      # positively a symlink — malformed
        return _TOKEN_ERROR
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != 32:
            return None                      # positively malformed
        chunks = bytearray()
        while len(chunks) < 33:
            piece = os.read(fd, 33 - len(chunks))
            if not piece:
                break
            chunks += piece
        # A regular 32-byte file with no legitimate concurrent writer must
        # read back exactly 32 bytes; anything else means the probe raced
        # something and the state is unknowable, not proven malformed.
        if len(chunks) != 32:
            return _TOKEN_ERROR
    except OSError:
        return _TOKEN_ERROR
    finally:
        os.close(fd)
    token = chunks.decode("ascii", errors="replace")
    return token if _DIR_ID_RE.match(token) else None


def _read_dir_token(dir_fd: int) -> str | None:
    """The GATE view of :func:`_classify_dir_token`: anything but a valid
    token — absent, malformed or unknowable alike — is ``None``, and the
    caller refuses. Only :meth:`CallbackSpool.ensure_plugin_dirs`'s repair
    path needs the three-state distinction."""
    token = _classify_dir_token(dir_fd)
    return token if isinstance(token, str) else None


def _read_marker_at(name: str, dir_fd: int) -> Marker:
    """Total, non-blocking, three-state marker read openat-relative to
    *dir_fd*.

    ABSENT is returned ONLY for a genuinely missing file (``ENOENT``). Every
    other obstruction is INVALID, never conflated with absent: a symlink
    (``O_NOFOLLOW`` ⇒ ``ELOOP``), a FIFO / socket / directory (rejected by the
    ``S_ISREG`` gate BEFORE any ``read`` — and the open is ``O_NONBLOCK``, so a
    FIFO cannot even block), an oversized body (bounded read), non-UTF-8, or a
    non-object JSON value. Mirrors the fail-closed FD discipline the rest of
    this module uses (``O_NOFOLLOW`` + ``fstat`` + ``S_ISREG``)."""
    try:
        fd = os.open(name, _MARKER_READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return Marker(MarkerState.ABSENT)
    except OSError:
        return Marker(MarkerState.INVALID)   # symlink (ELOOP), ENXIO, perms, …
    try:
        try:
            st = os.fstat(fd)
        except OSError:
            return Marker(MarkerState.INVALID)
        if not stat.S_ISREG(st.st_mode):
            return Marker(MarkerState.INVALID)   # FIFO/socket/dir: no read
        chunks: list[bytes] = []
        size = 0
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return Marker(MarkerState.INVALID)
            if not chunk:
                break
            size += len(chunk)
            if size > MARKER_STATE_MAX_BYTES:
                return Marker(MarkerState.INVALID)
            chunks.append(chunk)
    finally:
        os.close(fd)
    body = b"".join(chunks)
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001 — the reader is TOTAL: not just
        # ValueError/UnicodeDecodeError but e.g. a RecursionError from a
        # deeply-nested body that fits the size cap (60k opening brackets)
        # must decode to INVALID, never escape and violate the never-raises
        # contract the reconciler relies on.
        return Marker(MarkerState.INVALID)
    if not isinstance(obj, dict):
        return Marker(MarkerState.INVALID)
    # Expose the RAW on-disk bytes so the reconcile can compare byte-strictly
    # against canonical_marker_bytes(desired) — a parse-equal but not
    # byte-identical marker (reorder/type/whitespace/extra-key) is DIFFERING.
    return Marker(MarkerState.PRESENT, obj, raw=body)


def _read_envelope_at(name: str, dir_fd: int) -> dict | None:
    """Bounded, fail-closed read of a consumer-authored mint envelope,
    openat-relative to *dir_fd* (a ``pending/`` or ``.claims/`` FD).

    TOTAL: any defect — missing, unopenable, non-regular (the ``S_ISREG``
    gate runs before any read; the ``O_NONBLOCK`` open means a FIFO cannot
    even block), a read fault, more than ``ENVELOPE_MAX_BYTES`` bytes (the
    read stops at the cap + 1, so an oversized body is detected without
    ever being read whole), or anything :func:`callback_attempts.
    parse_envelope` rejects — degrades to ``None`` (meta unknown), never an
    exception. The state was already consumed by the time this runs, so
    refusal would buy nothing (spec §4)."""
    try:
        fd = os.open(name, _MARKER_READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
        except OSError:
            return None
        limit = callback_attempts.ENVELOPE_MAX_BYTES + 1
        chunks: list[bytes] = []
        size = 0
        while size < limit:
            try:
                chunk = os.read(fd, limit - size)
            except OSError:
                return None
            if not chunk:
                break
            size += len(chunk)
            chunks.append(chunk)
    finally:
        os.close(fd)
    return callback_attempts.parse_envelope(b"".join(chunks))


def _strict_replace_at(name: str, dir_fd: int, data: bytes, *, parent_fd: int,
                       what: str, parent_what: str, role: str) -> bool:
    """Staged replace with STRICT durability at every step — the write a
    dependent deletion keys on (an attempt's write-ahead outcome, a removal
    record's abort notice).

    Sequence, and why each step is strict: stage 0600 + ``_fsync_strict`` the
    STAGED FILE (a failure aborts BEFORE the rename, leaving the previous
    record intact), rename over the target, then ``_fsync_strict`` the
    directory and the PARENT that names it (a failure returns False although
    the new record may already be VISIBLE — that is correct: the caller skips
    its dependent action this pass and a later pass converges).

    Returns True only when fully durable; never raises. Every warning names
    the *role*, never a hash or a payload (INV-CB-006)."""
    tmp = f".{name}{REPLACE_TEMP_INFIX}{os.getpid()}-{uuid.uuid4().hex}"
    try:
        fd = os.open(tmp, _NEW_FILE_FLAGS, FILE_MODE, dir_fd=dir_fd)
        try:
            os.fchmod(fd, FILE_MODE)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            # Strict, unlike _write_new_file's best-effort fsync: the rename
            # below is what the caller's dependent action keys on, so the
            # staged bytes must be proven durable FIRST.
            _fsync_strict(fd, f"staged {role}")
        finally:
            os.close(fd)
    except OSError as exc:                     # FsyncFailed included
        _unlink_quiet(tmp, dir_fd)
        logger.warning("callback-spool: %s staging failed (errno %s)",
                       role, exc.errno)
        return False
    try:
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError as exc:
        _unlink_quiet(tmp, dir_fd)
        logger.warning("callback-spool: %s publish failed (errno %s)",
                       role, exc.errno)
        return False
    try:
        _fsync_strict(dir_fd, what)
        # …and the entry that NAMES that directory: on a dir created by an
        # earlier call this is the only pass that can still prove it durable,
        # and a crash that loses it loses every record inside.
        _fsync_strict(parent_fd, parent_what)
    except FsyncFailed as exc:
        logger.warning("callback-spool: %s dir fsync failed (errno %s)",
                       role, exc.errno)
        return False
    return True


def _link_once(src: str, src_dir_fd: int, dst: str, dst_dir_fd: int) -> bool:
    """Publish-once primitive: ``link(2)`` is the atomic no-replace rename the
    stdlib does not expose (there is no ``renameat2``/``RENAME_NOREPLACE``
    binding in CPython). ``EEXIST`` means a concurrent winner published first
    and is the arbiter of exactly-once; the caller never clobbers.

    ``follow_symlinks=False`` links the symlink itself rather than its target,
    so a swapped-in symlink cannot smuggle an outside inode into the spool —
    the subsequent ``S_ISREG`` gate then rejects it.

    Returns True when this caller published, False on ``EEXIST``; any other
    error propagates (the caller decides the fail-closed outcome).
    """
    try:
        os.link(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
                follow_symlinks=False)
        return True
    except FileExistsError:
        return False


# ---------------------------------------------------------------------------
# value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """Exclusive ownership of one consumed state. ``mtime`` is the MINT time
    (preserved through the claim link); ``dir_token`` pins the plugin spool
    directory this claim was taken from, so a removal + reinstall between
    claim and publish fails closed instead of depositing a credential into a
    different (recreated) directory. The token — a random ``.dir-id`` minted
    when the directory is created — is what carries that identity;
    ``dir_dev``/``dir_ino`` are kept as a second, cheaper gate, but cannot
    carry it alone (ext4 recycles a freed inode number immediately, so a
    recreated directory can present the exact stat pair of its
    predecessor)."""

    plugin: str
    state_hash: str
    mtime: float
    dir_dev: int
    dir_ino: int
    dir_token: str

    @property
    def key(self) -> str:
        return in_flight_key(self.plugin, self.state_hash)


class PublishOutcome(enum.Enum):
    """Tri-state result of :meth:`CallbackSpool.publish_result` (spec §5).

    Exactly three states — there is deliberately no fourth: only
    ``FAILED_RECORDED``, a failure whose ``done/publish_failed`` outcome was
    proven durable by a strict write, authorizes the caller to discard the
    claim (fail-closed single-use). ``FAILED_UNRECORDED`` means nothing
    durable was written — the caller must LEAVE the claim so the recovery
    pass restores the flow instead of a transient fault silently eating it.
    ``PUBLISHED`` needs nothing further from the caller but the nudge kick.
    """

    PUBLISHED = "published"
    FAILED_RECORDED = "failed_recorded"
    FAILED_UNRECORDED = "failed_unrecorded"


@dataclass(frozen=True)
class _ArtifactDirs:
    """The three artifact-directory FDs a write-ahead derivation probes —
    everything that can witness what a flow's hash still means on disk.

    Any of them may be ``None`` (that directory would not open this pass):
    the derivation loses that source rather than failing, because a derived
    record with less provenance is still a record, and the deletion it
    precedes is what must never happen unrecorded."""

    pend: "int | None"
    results: "int | None"
    claims: "int | None"


@dataclass
class RecoveryReport:
    restored: list[tuple[str, str]] = field(default_factory=list)
    nudges: list[tuple[str, str]] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)
    #: Claims whose flow already carries a DURABLE terminal outcome: the
    #: deletion that outcome authorized was interrupted, and recovery
    #: completes it instead of restoring the flow. Deliberately not folded
    #: into ``dropped`` (which means "aged out here, outcome recorded here").
    completed_terminal: list[tuple[str, str]] = field(default_factory=list)
    temps_cleared: int = 0
    anomalies: list[str] = field(default_factory=list)


@dataclass
class AttemptsReport:
    """What one :meth:`CallbackSpool.attempts_pass` did to the ledger.

    Every counter names a DERIVED change: nothing here destroys a
    credential-bearing artifact (that is the sweep's job, write-ahead), and
    nothing here is authoritative over the artifacts — the pass exists to
    make the ledger agree with them."""

    #: attempt files created for a flow that had none (spec §3.3), plus
    #: unreadable records rebuilt from the artifacts that survive
    materialized: int = 0
    #: open ``result_ready`` attempts settled ``collected`` by the five §6
    #: probes
    collected: int = 0
    #: provisional records rewritten from artifacts that contradict them —
    #: a terminal attempt beside a live artifact, or a ``result_ready`` one
    #: whose flow rewound to a live pending
    reopened: int = 0
    #: OPEN ``result_ready`` attempts whose ``claimed`` flag was raised from a
    #: live ``.collect-*`` hold. Deliberately its own counter: nothing is
    #: rewritten from the artifacts here — the record keeps its binding, its
    #: mint clock and the worker's schedule, and only the flag moves.
    claimed_raised: int = 0
    #: ``.ack-<hash>`` receipt tokens consumed — every artifact of the hash
    #: proven gone, then the token itself (spec §7)
    acks_consumed: int = 0
    #: receipt tokens KEPT this pass: an in-flight publisher owns the hash, a
    #: probe was UNKNOWN, or a strict fsync failed. Not a failure — the token
    #: is durable and the next pass finishes the teardown.
    acks_deferred: int = 0
    #: terminal attempt files retired at ``ATTEMPT_RETENTION_S``
    aged_out: int = 0
    #: attempt files removed by the ``MAX_ATTEMPTS`` ladder
    capped: int = 0
    #: cap evictions SKIPPED because the ``evicted`` outcome would not go
    #: durable — the open record survives and the next pass retries
    skipped_undurable: int = 0
    anomalies: list = field(default_factory=list)


@dataclass
class SweepReport:
    deleted_pending: int = 0
    deleted_results: int = 0
    deleted_temps: int = 0
    deleted_collect: int = 0
    deleted_collect_capped: int = 0
    deleted_anomalous: int = 0
    deleted_capped: int = 0
    #: Flow-retiring deletions SKIPPED this pass because the write-ahead
    #: outcome could not be proven durable (INV-CB-007): the artifact lives
    #: a few minutes longer and the next pass retries. Not a deletion, so
    #: deliberately outside ``total``.
    skipped_undurable: int = 0
    capped: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (self.deleted_pending + self.deleted_results
                + self.deleted_temps + self.deleted_collect
                + self.deleted_collect_capped
                + self.deleted_anomalous + self.deleted_capped)


# ---------------------------------------------------------------------------
# consumer-side reference helpers — the executable half of the consumer
# contract (mint / collect / ack), deliberately module-level on plain paths
# and dir FDs, never CallbackSpool methods: a consumer imports the protocol
# instead of re-implementing it.
# ---------------------------------------------------------------------------


def mint(plugin_dir: Path | str, state: str, meta=None) -> Path:
    """Mint a pending state — the CONSUMER's half of the contract, kept here
    as the executable reference (and the tests' minting primitive).

    The payload is the v2 envelope ``{"v": 2, "meta": <meta>}`` in the ONE
    canonical byte form (:func:`canonical_marker_bytes`); ``meta`` is
    whatever non-secret, JSON-serializable context the consumer needs to
    recognize the flow in a later life — never tokens, verifiers or any
    bearer material (its retention equals the attempt's). An envelope whose
    canonical bytes exceed ``callback_attempts.ENVELOPE_MAX_BYTES`` is
    refused with ``ValueError`` BEFORE any file is created — no ``.part``
    residue on refusal.

    ``pending/<hash>.json.part`` is written 0600 and fsynced, then published
    once by ``link(2)``; the final name existing is a hard error (state
    reuse), never an overwrite. Returns the published path.
    """
    envelope = canonical_marker_bytes({"v": 2, "meta": meta})
    if len(envelope) > callback_attempts.ENVELOPE_MAX_BYTES:
        raise ValueError(
            "canonical mint envelope exceeds ENVELOPE_MAX_BYTES")
    h = state_hash(state)
    pending = Path(plugin_dir) / PENDING_DIR
    part, final = f"{h}.json{PART_SUFFIX}", f"{h}.json"
    dir_fd = os.open(pending, _DIR_FLAGS)
    try:
        _write_new_file(part, dir_fd, envelope)
        if not _link_once(part, dir_fd, final, dir_fd):
            _unlink_quiet(part, dir_fd)
            raise FileExistsError(
                errno.EEXIST, "state already minted", final)
        _fsync(dir_fd, str(pending))
        _unlink_quiet(part, dir_fd)
        _fsync(dir_fd, str(pending))
    finally:
        os.close(dir_fd)
    return pending / final


def collect(plugin_dir: Path | str, state_hash_hex: str) -> tuple[dict, Path]:
    """Collect a published result — the CONSUMER's pickup verb.

    ``results/<h>.json`` is renamed to the consumer-held name
    ``results/.collect-<h>-<uuid>`` and read only AFTER the rename, so the
    rename's exactly-one-winner is what arbitrates a pickup race, never a
    read of a name a sweep could still retire. A ``FileNotFoundError``
    propagates untouched: the attempt-first publish ordering opens a brief
    window where the attempt is visible before the result link lands, so
    ENOENT while the attempt still says ``result_ready`` is RETRYABLE, never
    ackable — the retry loop is the caller's. A held file that reads back
    non-regular, oversized or malformed raises ``ValueError`` (naming no
    content).

    Returns ``(record, held_path)``. **The consumer keeps the held file
    until ack and NEVER unlinks it** (nothing is unlinked here either): the
    ``.collect-*`` entry is the flow's crash journal — a successor finds it,
    the attempt shows ``claimed: true``, and the consumer's own store is the
    tiebreaker — and ack-teardown is what removes it, with every other
    artifact of the hash.
    """
    if not _is_hash(state_hash_hex):
        raise ValueError("malformed state hash")
    results = Path(plugin_dir) / RESULTS_DIR
    held = f"{COLLECT_PREFIX}{state_hash_hex}-{uuid.uuid4().hex}"
    dir_fd = os.open(results, _DIR_FLAGS)
    try:
        os.rename(f"{state_hash_hex}.json", held,
                  src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        marker = _read_marker_at(held, dir_fd)
    finally:
        os.close(dir_fd)
    if marker.state is not MarkerState.PRESENT:
        raise ValueError("collected result is unreadable or malformed")
    return marker.payload, results / held


def ack(plugin_dir: Path | str, state_hash_hex: str) -> bool:
    """Acknowledge an attempt — the CONSUMER's receipt, and its one verb
    under ``attempts/``.

    Ack = rename ``attempts/<h>.json`` → ``attempts/.ack-<h>`` — a rename,
    not an unlink, so casa's staged-replace can never resurrect an acked
    attempt; a pre-existing ``.ack-<h>`` is simply replaced (same flow, same
    meaning). ENOENT — on the rename or the ``attempts/`` dir itself —
    means ALREADY SETTLED (acked earlier, or the flow torn down): ``True``,
    idempotently, with nothing to witness.

    The attempts directory is then fsynced STRICTLY: the ack witness must be
    crash-durable (spec §7), because the consumer treats the flow as settled
    from this point — only after its own commit point, the exchange result
    durably in its own store. ``FsyncFailed`` PROPAGATES: an unwitnessed ack
    must not be treated as settled (the rename may have happened; re-acking
    re-witnesses). ``True`` only on witness durable.

    The fsync runs on the ENOENT arm too, and that is the point of re-acking
    after a failure: a first call whose rename SUCCEEDED and whose fsync then
    failed leaves the source absent, so the retry finds ENOENT — returning
    True there without an fsync would report an unwitnessed rename as
    settled, and a power loss would roll it back under a consumer that has
    already moved on. The only unwitnessable case is the directory itself
    being gone, which is the teardown having completed.
    """
    if not _is_hash(state_hash_hex):
        raise ValueError("malformed state hash")
    attempts = Path(plugin_dir) / ATTEMPTS_DIR
    try:
        dir_fd = os.open(attempts, _DIR_FLAGS)
    except FileNotFoundError:
        return True                          # flow torn down — settled
    try:
        try:
            os.rename(f"{state_hash_hex}.json",
                      f"{ACK_PREFIX}{state_hash_hex}",
                      src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except FileNotFoundError:
            pass                 # already acked / settled — still witness it
        _fsync_strict(dir_fd, ATTEMPTS_DIR)
    finally:
        os.close(dir_fd)
    return True


# ---------------------------------------------------------------------------
# the spool
# ---------------------------------------------------------------------------


class CallbackSpool:
    """One instance, owned by casa-core, pinned to the spool root.

    ``_lock`` serializes the dir-FD syscalls against :meth:`close` so a
    concurrent close (which nulls the root FD) can never interleave between
    the closed-check and a syscall — otherwise a ``dir_fd=None`` op would
    resolve against the process CWD (a fail-open). The request-path
    operations are a handful of syscalls each; the background passes (sweep,
    recovery) hold the lock for a whole scan, which is bounded by the
    per-plugin caps and runs off the event loop. The lock is NOT what makes
    consumption exactly-once — the real arbiter is ``link(2)``'s EEXIST,
    which holds across processes where no in-process lock can.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self._closed = False
        self._in_flight: set[str] = set()
        os.makedirs(self.root, mode=DIR_MODE, exist_ok=True)
        self._root_fd = os.open(os.path.realpath(self.root), _DIR_FLAGS)
        try:
            os.fchmod(self._root_fd, DIR_MODE)
        except OSError as exc:                          # pragma: no cover
            logger.warning("callback-spool: chmod of root failed: %s", exc)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                os.close(self._root_fd)
            except OSError:
                pass
            self._root_fd = -1

    def _require_open(self) -> None:
        if self._closed:
            raise SpoolClosed("callback spool is closed")

    # -- directories --------------------------------------------------------

    def ensure_plugin_dirs(self, plugin: str) -> None:
        """Create ``<plugin>/{pending,results,.claims,attempts}`` at 0770.
        Idempotent — reconcile calls it on every pass. ``mkdir``'s mode is
        masked by the umask, so each directory's mode is pinned through its
        own O_NOFOLLOW FD afterwards."""
        # A leading dot is reserved for casa's own root-level structures
        # (``.index``, ``.removals``): such a directory would be created here
        # but skipped by _plugin_dirs(), so it would never be swept, recovered
        # or GC'd.
        if not _safe_component(plugin) or plugin.startswith("."):
            raise ValueError(f"unsafe plugin spool name {plugin!r}")
        with self._lock:
            self._require_open()
            self._mkdir(plugin, self._root_fd)
            pfd = _open_dir(plugin, self._root_fd)
            try:
                self._chmod_dir(pfd, plugin)
                for sub in (PENDING_DIR, RESULTS_DIR, CLAIMS_DIR,
                            ATTEMPTS_DIR):
                    self._mkdir(sub, pfd)
                    sfd = _open_dir(sub, pfd)
                    try:
                        self._chmod_dir(sfd, sub)
                    finally:
                        os.close(sfd)
                probe = _classify_dir_token(pfd)
                if probe is _TOKEN_ERROR:
                    # State unknowable (a transient I/O failure) — NEVER
                    # grounds to retire what may be a valid, live token that
                    # in-flight claims still carry. Abort; the next pass
                    # re-probes.
                    raise OSError(errno.EIO,
                                  f"{DIR_ID_NAME} state unknowable")
                if probe is None:
                    self._repair_dir_token(pfd)
                _fsync(pfd, plugin)
            finally:
                os.close(pfd)
            _fsync(self._root_fd, str(self.root))

    @staticmethod
    def _repair_dir_token(pfd: int) -> None:
        """Mint (or repair) the identity token, serialized cross-process by an
        exclusive ``flock`` on the plugin dir fd and RE-probed under it — a
        concurrent pass may have minted between the caller's probe and the
        lock, and a VALID token is never retired (exactly-once per directory
        life; in-flight claims carry it). A recreated directory always gets a
        FRESH token; the token, not the recyclable inode number, is what
        claim/discard/publish compare (see Claim.dir_token). A dir that
        existed before the token was introduced gets one on the first pass
        through here — claims taken before that mint fail closed, exactly
        like any other unprovable identity."""
        fcntl.flock(pfd, fcntl.LOCK_EX)
        try:
            probe = _classify_dir_token(pfd)
            if probe is _TOKEN_ERROR:
                raise OSError(errno.EIO,
                              f"{DIR_ID_NAME} state unknowable")
            if probe is None:
                if not _retire_marker_entry(DIR_ID_NAME, pfd):
                    raise OSError(errno.EIO,
                                  f"invalid {DIR_ID_NAME} survives retire")
                _write_new_file(DIR_ID_NAME, pfd,
                                uuid.uuid4().hex.encode("ascii"))
        finally:
            fcntl.flock(pfd, fcntl.LOCK_UN)

    @staticmethod
    def _mkdir(name: str, dir_fd: int) -> bool:
        """Create *name* if absent; True when this call created it (so the
        caller can fsync the parent exactly once, when it matters)."""
        try:
            os.mkdir(name, DIR_MODE, dir_fd=dir_fd)
            return True
        except FileExistsError:
            return False

    @staticmethod
    def _chmod_dir(fd: int, what: str) -> None:
        try:
            os.fchmod(fd, DIR_MODE)
        except OSError as exc:                          # pragma: no cover
            logger.warning("callback-spool: chmod of %s failed: %s", what, exc)

    def _plugin_fd(self, plugin: str) -> int:
        """Open a per-plugin spool dir relative to the pinned root.

        The name guard lives HERE rather than at each entry point: this is the
        single funnel through which every plugin-scoped operation resolves a
        directory, so ``..`` (or any other non-component name) cannot escape
        the pinned root through a path an individual caller forgot to
        validate. Raises ``ValueError`` — never an ``OSError`` — so a caller's
        "directory is missing" branch can never absorb a traversal attempt.
        """
        if not _safe_component(plugin):
            raise ValueError(f"unsafe plugin spool name {plugin!r}")
        return _open_dir(plugin, self._root_fd)

    def _plugin_dirs(self) -> list[str]:
        """Names of the per-plugin spool dirs. EVERY dot-prefixed root entry
        is reserved for casa's own structures (``.index``, ``.removals``, and
        anything future) and excluded — from enumeration, and therefore from
        sweep, recovery and orphan GC alike. No legitimate plugin dir can be
        shadowed: :meth:`ensure_plugin_dirs` refuses dotted names. Stray
        non-directory entries are excluded as before."""
        out = []
        for name in _listdir_quiet(self._root_fd):
            if name.startswith(".") or not _safe_component(name):
                continue
            st = _lstat_quiet(name, self._root_fd)
            if st is not None and stat.S_ISDIR(st.st_mode):
                out.append(name)
        return sorted(out)

    # -- readiness marker + discovery index ---------------------------------

    def write_ready(self, plugin: str, payload: dict) -> None:
        """Publish ``<plugin>/ready.json`` — the POSITIVE readiness marker,
        written only AFTER the routing overlay swap so it can never be falsely
        positive. Replacing (not publish-once): reconcile rebuilds the marker
        on every pass, and the marker is advisory — the overlay alone decides
        what the endpoint serves, so a stale marker cannot open a route."""
        with self._lock:
            self._require_open()
            pfd = self._plugin_fd(plugin)
            try:
                self._replace_json(READY_NAME, pfd, payload, plugin)
            finally:
                os.close(pfd)

    def delete_ready(self, plugin: str) -> bool:
        """Retire the marker (of ANY type) and fsync its directory — done BEFORE
        the unrouting overlay swap, so a crash mid-unroute can only leave the
        route closed with the marker already gone (never the reverse).

        Returns True when the marker is now absent (removed, or already gone),
        False when a genuine removal FAILED and the entry survives — surfaced by
        the reconciler rather than mistaken for both-absent. Type-aware: a
        directory/FIFO/symlink-shaped ``ready.json`` is removed (a raw unlink
        would fail on a directory and leave it to block republication)."""
        with self._lock:
            self._require_open()
            try:
                pfd = self._plugin_fd(plugin)
            except FileNotFoundError:
                return True                  # dir already gone — marker absent
            except OSError:
                return False                 # dir unreachable — not confirmed gone
            try:
                gone = _retire_marker_entry(READY_NAME, pfd)
                _fsync(pfd, plugin)
                return gone
            finally:
                os.close(pfd)

    def read_marker(self, plugin: str) -> Marker:
        """Three-state read of ``<plugin>/ready.json`` — the reconciler's
        durable on-disk truth.

        Returns ABSENT (no marker), INVALID (present but non-regular /
        unreadable / oversized / malformed), or PRESENT with the payload.
        Distinguishing INVALID from ABSENT is what lets a stale-but-unreadable
        (e.g. oversized, or a swapped-in FIFO) marker be republished rather than
        conflated with absent and left to survive a failed rewrite. Never
        blocks and never raises (see :func:`_read_marker_at`)."""
        with self._lock:
            if self._closed or not _safe_component(plugin):
                return Marker(MarkerState.ABSENT)
            try:
                pfd = self._plugin_fd(plugin)
            except FileNotFoundError:
                return Marker(MarkerState.ABSENT)   # no plugin dir = no marker
            except (OSError, ValueError):
                return Marker(MarkerState.INVALID)
            try:
                return _read_marker_at(READY_NAME, pfd)
            finally:
                os.close(pfd)

    def read_index_marker(self, artifact_realpath: str) -> Marker:
        """Three-state read of ``.index/<key>.json`` for an artifact path —
        the durable-marker companion of :meth:`read_marker` (same semantics)."""
        with self._lock:
            if self._closed:
                return Marker(MarkerState.ABSENT)
            try:
                ifd = _open_dir(INDEX_DIR, self._root_fd)
            except FileNotFoundError:
                return Marker(MarkerState.ABSENT)   # no index dir = no entry
            except OSError:
                return Marker(MarkerState.INVALID)
            try:
                return _read_marker_at(
                    f"{index_key(artifact_realpath)}.json", ifd)
            finally:
                os.close(ifd)

    def write_index_entry(self, artifact_realpath: str, payload: dict) -> None:
        """Publish ``.index/<sha256(realpath(artifact_root))>.json`` — how a
        consumer finds its spool dir without knowing its registry name."""
        with self._lock:
            self._require_open()
            ifd = self._index_fd(create=True)
            try:
                self._replace_json(f"{index_key(artifact_realpath)}.json",
                                   ifd, payload, INDEX_DIR)
            finally:
                os.close(ifd)

    def delete_index_entry(self, artifact_realpath: str) -> bool:
        """Retire the ``.index`` entry (of ANY type) for *artifact_realpath*.
        Returns True when it is now absent, False when a removal genuinely
        failed (see :meth:`delete_ready`)."""
        with self._lock:
            self._require_open()
            try:
                ifd = self._index_fd(create=False)
            except FileNotFoundError:
                return True                  # no index dir — entry absent
            except OSError:
                return False
            try:
                gone = _retire_marker_entry(
                    f"{index_key(artifact_realpath)}.json", ifd)
                _fsync(ifd, INDEX_DIR)
                return gone
            finally:
                os.close(ifd)

    def published_plugins(self) -> list[str]:
        """Plugin dirs that currently carry a ``ready.json`` marker of ANY type —
        the DURABLE readiness inventory the reconciler reconciles against the
        desired routed set. Reading on-disk truth (not the in-memory previous
        overlay, which is empty across a restart) is what lets a marker for a
        plugin no longer routed be retired after a reboot.

        A non-regular ``ready.json`` (a swapped-in directory/FIFO/symlink) is
        INCLUDED, not silently omitted: it is an INVALID orphan that must still
        be enumerated so a trustworthy pass retires it — omitting it would leave
        an invalid marker to survive forever and block republication."""
        with self._lock:
            if self._closed:
                return []
            out: list[str] = []
            for plugin in self._plugin_dirs():
                try:
                    pfd = self._plugin_fd(plugin)
                except (OSError, ValueError):
                    continue
                try:
                    if _lstat_quiet(READY_NAME, pfd) is not None:
                        out.append(plugin)
                finally:
                    os.close(pfd)
            return out

    def index_keys(self) -> list[str]:
        """Discovery-index keys currently published under ``.index/`` — the
        ``<sha256>.json`` entries (staging residue excluded), returned WITHOUT
        the ``.json`` suffix. Enumeration is by NAME, so a non-regular entry (a
        swapped-in directory/FIFO/symlink named ``<sha256>.json``) is included
        too — an INVALID orphan the reconciler must still see to retire. The
        reconciler retires any key the desired routed set no longer covers;
        :meth:`delete_index_key` retires one by key."""
        with self._lock:
            if self._closed:
                return []
            try:
                ifd = _open_dir(INDEX_DIR, self._root_fd)
            except OSError:
                return []
            try:
                out: list[str] = []
                for name in _listdir_quiet(ifd):
                    if _is_replace_temp(name):
                        continue
                    if name.endswith(".json") and _is_hash(name[:-5]):
                        out.append(name[:-5])
                return sorted(out)
            finally:
                os.close(ifd)

    def delete_index_key(self, key: str) -> bool:
        """Retire one discovery-index entry (of ANY type) by its already-computed
        KEY. Returns True when the entry is now absent, False when a removal
        genuinely failed (see :meth:`delete_ready`).

        The durable-inventory reconcile knows the on-disk key (a sha256 hex),
        not the artifact path it was derived from, so it cannot go through
        :meth:`delete_index_entry` (which re-hashes a path). Guarded like every
        index op — a non-hash key raises ``ValueError`` rather than resolving
        outside ``.index/`` — and a closed spool raises ``SpoolClosed``."""
        if not _is_hash(key):
            raise ValueError(f"unsafe index key {key!r}")
        with self._lock:
            self._require_open()
            try:
                ifd = self._index_fd(create=False)
            except FileNotFoundError:
                return True                  # no index dir — entry absent
            except OSError:
                return False
            try:
                gone = _retire_marker_entry(f"{key}.json", ifd)
                _fsync(ifd, INDEX_DIR)
                return gone
            finally:
                os.close(ifd)

    def _index_fd(self, *, create: bool) -> int:
        if create and self._mkdir(INDEX_DIR, self._root_fd):
            # The directory entry itself must be durable before an entry
            # inside it is: otherwise a power crash can keep the index entry's
            # inode while losing the directory that names it.
            _fsync(self._root_fd, str(self.root))
        fd = _open_dir(INDEX_DIR, self._root_fd)
        if create:
            self._chmod_dir(fd, INDEX_DIR)
        return fd

    def _replace_json(self, name: str, dir_fd: int, payload: dict,
                      what: str) -> None:
        """Atomic replacing publish for the two ADVISORY files (ready.json and
        an index entry): staged 0600 + fsync, then renamed over the target,
        then a directory fsync. Never used for pending/claims/results — those
        are publish-once by ``link(2)``. Serialized through the SHARED
        :func:`canonical_marker_bytes`, so a marker casa writes is byte-identical
        to what the reconcile's compare treats as unchanged — no steady-state
        churn."""
        data = canonical_marker_bytes(payload)
        tmp = f".{name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        _write_new_file(tmp, dir_fd, data)
        try:
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except OSError:
            _unlink_quiet(tmp, dir_fd)
            raise
        _fsync(dir_fd, what)

    # -- in-flight set ------------------------------------------------------

    def in_flight(self) -> set[str]:
        """Plugin-qualified hashes a handler is currently processing. The
        periodic recovery pass skips these: handler and recovery run in the
        SAME process, so this set is what makes "a live claim is never
        restored under a working handler" true. Boot passes have no live
        handlers by construction and ignore it."""
        with self._lock:
            return set(self._in_flight)

    # -- claim ----------------------------------------------------------

    def claim(self, plugin: str, state_hash_hex: str, *, now: float) -> Claim | None:
        """Consume ``pending/<hash>.json`` exactly once.

        Every refusal returns ``None`` — replay, expired, never-minted and
        unknown-plugin all lose identically, because the caller renders one
        neutral response for all of them (INV-CB-005) and a differentiated
        outcome would be an enumeration oracle.

        Sequence: ``link`` into ``.claims/<hash>`` (EEXIST/ENOENT ⇒ lose) →
        fsync ``.claims/`` → unlink the pending name → fsync ``pending/``.
        Linking BEFORE unlinking is what makes a crash recoverable: the worst
        residue is a claim plus its pending twin, which the recovery pass
        converges. The TTL/skew gates then run on the claim's own mtime —
        which ``link`` preserved, so it is still the MINT time.
        """
        if not _safe_component(plugin) or not _is_hash(state_hash_hex):
            return None
        with self._lock:
            if self._closed:
                return None                  # fail closed; never raise into HTTP
            try:
                pfd = self._plugin_fd(plugin)
            except (OSError, ValueError):
                return None                  # unrouted / removed plugin
            try:
                dir_st = os.fstat(pfd)
                dir_token = _read_dir_token(pfd)
                if dir_token is None:
                    return None      # identity unprovable — refuse
                try:
                    pend = _open_dir(PENDING_DIR, pfd)
                except OSError:
                    return None
                try:
                    claims = _open_dir(CLAIMS_DIR, pfd)
                except OSError:
                    os.close(pend)
                    return None
                try:
                    return self._claim_locked(plugin, state_hash_hex, now,
                                              pend, claims, dir_st, dir_token)
                finally:
                    os.close(pend)
                    os.close(claims)
            finally:
                os.close(pfd)

    def _claim_locked(self, plugin: str, h: str, now: float, pend: int,
                      claims: int, dir_st, dir_token: str) -> Claim | None:
        src = f"{h}.json"
        try:
            if not _link_once(src, pend, h, claims):
                return None      # a claim already exists — replay loses
        except OSError:
            return None          # never minted, vanished, or an FS fault
        _fsync(claims, CLAIMS_DIR)
        _unlink_quiet(src, pend)
        _fsync(pend, PENDING_DIR)

        # Every refusal arm below LEAVES the claim entry in `.claims/` (and
        # runs no fsync — nothing changed on disk): the request path performs
        # no flow-retiring deletions, so an expired/future/non-regular claim
        # is reaped by the recovery pass, which records its write-ahead
        # outcome first (INV-CB-007). Unlinking here would destroy the flow's
        # last artifact with no durable record of why.
        st = _lstat_quiet(h, claims)
        if st is None or not stat.S_ISREG(st.st_mode):
            # A non-regular inode can only have come from a symlinked pending
            # name (linked as the symlink itself, never followed) — refuse it.
            return None
        if st.st_mtime > now + SKEW_S:
            logger.info("callback-spool: refusing future-mtime claim (%s)", plugin)
            return None
        if now - st.st_mtime > PENDING_TTL_S:
            return None
        claim = Claim(plugin=plugin, state_hash=h, mtime=st.st_mtime,
                      dir_dev=dir_st.st_dev, dir_ino=dir_st.st_ino,
                      dir_token=dir_token)
        self._in_flight.add(claim.key)
        return claim

    def discard_claim(self, claim: Claim) -> None:
        """Drop a claim without publishing (the handler's refusal paths).
        The state stays consumed — that is the point of claim-by-rename."""
        with self._lock:
            self._in_flight.discard(claim.key)
            if self._closed:
                return
            try:
                pfd = self._plugin_fd(claim.plugin)
            except (OSError, ValueError):
                return
            try:
                # Same identity gate as the publish path: after a removal +
                # reinstall this name denotes a different directory, and the
                # claim being dropped is not ours to delete there. The token
                # is what proves identity; the stat pair is a second gate
                # only (an inode number is recycled, a token never).
                st = os.fstat(pfd)
                if (st.st_dev, st.st_ino) != (claim.dir_dev, claim.dir_ino):
                    return
                if _read_dir_token(pfd) != claim.dir_token:
                    return
                try:
                    claims = _open_dir(CLAIMS_DIR, pfd)
                except OSError:
                    return
                try:
                    _unlink_quiet(claim.state_hash, claims)
                    _fsync(claims, CLAIMS_DIR)
                finally:
                    os.close(claims)
            finally:
                os.close(pfd)

    # -- publish --------------------------------------------------------

    def publish_result(self, claim: Claim, record: dict) -> PublishOutcome:
        """Publish the result for *claim* — attempt-first, never partially
        visible, with a tri-state outcome (spec §§3.1/4/5).

        Sequence under the lock, after the dir-identity gate (whose failure
        is ``FAILED_UNRECORDED``): (1) three-state presence probe of the
        claim — genuinely ABSENT (an ack-teardown or a concurrent path
        consumed the flow) or UNKNOWN writes NOTHING and returns
        ``FAILED_UNRECORDED``; (2) bounded read of the claim inode's mint
        envelope (malformed ⇒ ``meta`` None — the state is already consumed,
        refusal buys nothing); (3) the record is augmented with ``meta`` and
        ``minted_ts`` (= the claim's preserved mint mtime; record ``v``
        stays 1) and serialized; (4) the ``result_ready`` attempt file is
        written BEFORE the result name can exist, so a consumer that
        collects-and-acks the instant the result appears never acks into
        ENOENT; (5) the record is staged in ``.claims/.tmp-<hash>``, linked
        into ``results/<hash>.json`` (publish-once) and fsynced; the temp is
        unlinked before the claim so a credential-bearing inode never keeps
        a second hard link past publication.

        Outcomes:

        * ``PUBLISHED`` — the result is durable. Also returned on the
          ``EEXIST`` anomaly (a result DOES exist for the hash): temp and
          claim are cleaned exactly as before, the step-4 attempt stands as
          ``result_ready``, and the flow converges through normal inference.
        * ``FAILED_RECORDED`` — staging/link (or serializing the record)
          failed AND the failure is durably recorded (``done/
          publish_failed``, strict write). The claim is LEFT here: the
          CALLER owns the discard decision, and only this outcome
          authorizes it (state stays consumed — fail-closed single-use,
          INV-CB-002).
        * ``FAILED_UNRECORDED`` — nothing durable was written (closed
          spool, identity drift, absent claim, or the attempt/outcome write
          itself failed). The caller must LEAVE the claim so recovery
          restores the flow to ``pending/`` rather than a transient failure
          silently eating it.

        Every arm renders the same neutral response upstream (INV-CB-005);
        no arm logs state, hash or meta content (INV-CB-006).
        """
        with self._lock:
            try:
                return self._publish_guarded(claim, record)
            finally:
                # Cleared no matter how this exits: a hash left in the set
                # would make the periodic recovery pass skip its claim for the
                # rest of the process's life.
                self._in_flight.discard(claim.key)

    def _publish_guarded(self, claim: Claim, record: dict) -> PublishOutcome:
        """Open the claim's directories (fail-closed on identity drift) and
        run the publish sequence. Called with ``_lock`` held."""
        if self._closed:
            return PublishOutcome.FAILED_UNRECORDED
        try:
            pfd = self._plugin_fd(claim.plugin)
        except (OSError, ValueError):
            logger.warning("callback-spool: plugin dir vanished before publish")
            return PublishOutcome.FAILED_UNRECORDED
        try:
            st = os.fstat(pfd)
            if ((st.st_dev, st.st_ino) != (claim.dir_dev, claim.dir_ino)
                    or _read_dir_token(pfd) != claim.dir_token):
                # Removed + recreated between claim and publish: this is a
                # different directory (fresh token — the stat pair alone can
                # recur, since ext4 recycles freed inode numbers). Fail
                # closed rather than deposit a credential into a re-installed
                # plugin's spool.
                logger.warning("callback-spool: plugin dir replaced mid-flow; "
                               "refusing to publish")
                return PublishOutcome.FAILED_UNRECORDED
            try:
                claims = _open_dir(CLAIMS_DIR, pfd)
            except OSError:
                return PublishOutcome.FAILED_UNRECORDED
            try:
                results = _open_dir(RESULTS_DIR, pfd)
            except OSError:
                os.close(claims)
                return PublishOutcome.FAILED_UNRECORDED
            try:
                return self._publish_locked(claim, record, claims, results)
            finally:
                os.close(claims)
                os.close(results)
        finally:
            os.close(pfd)

    def _publish_locked(self, claim: Claim, record: dict, claims: int,
                        results: int) -> PublishOutcome:
        h = claim.state_hash
        now = time.time()
        # 1. Three-state presence probe. A genuinely ABSENT claim means the
        # flow was consumed underneath us (ack-teardown won): write NOTHING —
        # no attempt, no result — or the teardown would be un-torn. UNKNOWN
        # is never read as absent, and never grounds to write either.
        st = _marker_lstat(h, claims)
        if st is None or st is _LSTAT_ERROR:
            return PublishOutcome.FAILED_UNRECORDED
        # 2. Mint-envelope transport (spec §4): the claim IS the pending
        # inode, so its envelope carries the consumer's meta; any defect
        # degrades to None.
        envelope = _read_envelope_at(h, claims)
        meta = envelope["meta"] if envelope else None
        # 3. Augment + serialize FIRST: an unserializable record must fail
        # before the attempt claims a result is coming.
        record = dict(record, meta=meta, minted_ts=claim.mtime)
        rec = callback_attempts.new_attempt(
            state_hash=h, minted_ts=claim.mtime, status="result_ready",
            meta=meta, now=now)
        try:
            payload = json.dumps(record).encode("utf-8")
        except (TypeError, ValueError):
            logger.warning(
                "callback-spool: result record is not serializable")
            return self._record_publish_failure(claim, rec, now)
        # 4. Attempt BEFORE any result-side work (spec §3.1): the attempt
        # file must be visible before the result name can ever exist.
        if not self.write_attempt(claim.plugin, h, rec, strict=False):
            # Nothing durable yet — leave the claim, write nothing else.
            return PublishOutcome.FAILED_UNRECORDED
        # 5. Stage + link + fsync, exactly the pre-attempt sequence.
        tmp, final = f"{TEMP_PREFIX}{h}", f"{h}.json"
        # A pre-existing temp is residue from a crashed attempt at THIS hash:
        # the in-flight set excludes a live writer, so reclaiming the
        # deterministic name is safe (and recovery clears it the other way).
        _unlink_quiet(tmp, claims)
        try:
            _write_new_file(tmp, claims, payload)
        except OSError as exc:
            logger.warning("callback-spool: result staging failed (errno %s)",
                           exc.errno)
            _unlink_quiet(tmp, claims)
            return self._record_publish_failure(claim, rec, now)
        try:
            published = _link_once(tmp, claims, final, results)
        except OSError as exc:
            # A genuine FS fault (not EEXIST): drop only the temp.
            logger.warning("callback-spool: result publish failed (errno %s)",
                           exc.errno)
            _unlink_quiet(tmp, claims)
            _fsync(claims, CLAIMS_DIR)
            return self._record_publish_failure(claim, rec, now)
        if published:
            _fsync(results, RESULTS_DIR)
        else:
            # EEXIST anomaly: a result DOES exist for this hash, so the
            # step-4 result_ready attempt is accurate — clean up as always
            # and let the flow converge through normal inference.
            logger.warning(
                "callback-spool: result already exists for a claimed state "
                "(plugin=%s) — anomaly, dropping", claim.plugin)
        _unlink_quiet(tmp, claims)
        _fsync(claims, CLAIMS_DIR)
        _unlink_quiet(h, claims)
        _fsync(claims, CLAIMS_DIR)
        return PublishOutcome.PUBLISHED

    def _record_publish_failure(self, claim: Claim, rec: dict,
                                now: float) -> PublishOutcome:
        """Record ``done/publish_failed`` STRICTLY (spec §5): only a proven-
        durable outcome authorizes the handler's discard, so a strict-write
        failure downgrades to ``FAILED_UNRECORDED`` and the claim survives
        for recovery. The claim is LEFT on both arms — the caller decides."""
        done = callback_attempts.terminalize(rec, "publish_failed", now=now)
        if self.write_attempt(claim.plugin, claim.state_hash, done,
                              strict=True):
            return PublishOutcome.FAILED_RECORDED
        return PublishOutcome.FAILED_UNRECORDED

    # -- read side (delivery nudge / consumers) -----------------------------

    def has_result(self, plugin: str, state_hash_hex: str) -> bool:
        with self._lock:
            if self._closed or not _safe_component(plugin) \
                    or not _is_hash(state_hash_hex):
                return False
            try:
                pfd = self._plugin_fd(plugin)
            except (OSError, ValueError):
                return False
            try:
                results = _open_dir(RESULTS_DIR, pfd)
            except OSError:
                os.close(pfd)
                return False
            try:
                st = _lstat_quiet(f"{state_hash_hex}.json", results)
                return st is not None and stat.S_ISREG(st.st_mode)
            finally:
                os.close(results)
                os.close(pfd)

    def result_mtime(self, plugin: str, state_hash_hex: str) -> float | None:
        """The result inode's mtime — its durable PUBLISH time — or ``None``.

        The delivery worker anchors the result-phase nudge cadence (+0, +60,
        +180, +480) on this clock rather than on a field of its own: the
        result file is written once and never rewritten, so its mtime IS the
        publish instant, durable across restarts without a second source of
        truth (the one-clock doctrine).

        Three-state-safe by degrading to ``None`` for BOTH "no result" and
        "cannot tell" (a metadata failure, a non-regular inode, a closed
        spool, a bad name). The caller treats ``None`` as "no anchor" and
        falls back to a relative advance — never as "mtime 0", which would
        schedule every remaining nudge in 1970."""
        with self._lock:
            if self._closed or not _safe_component(plugin) \
                    or not _is_hash(state_hash_hex):
                return None
            try:
                pfd = self._plugin_fd(plugin)
            except (OSError, ValueError):
                return None
            try:
                try:
                    results = _open_dir(RESULTS_DIR, pfd)
                except OSError:
                    return None
                try:
                    st = _regular_stat(f"{state_hash_hex}.json", results)
                    return None if st is None else float(st.st_mtime)
                finally:
                    os.close(results)
            finally:
                os.close(pfd)

    def list_results(self, plugin: str) -> list[str]:
        """Published result hashes for *plugin* (the recovery invariant's
        input: any result lacking a settled episode is re-enqueued)."""
        with self._lock:
            if self._closed or not _safe_component(plugin):
                return []
            try:
                pfd = self._plugin_fd(plugin)
            except (OSError, ValueError):
                return []
            try:
                results = _open_dir(RESULTS_DIR, pfd)
            except OSError:
                os.close(pfd)
                return []
            try:
                return sorted(
                    h for h in (_hash_of_pending(n)
                                for n in _listdir_quiet(results))
                    if h is not None)
            finally:
                os.close(results)
                os.close(pfd)

    def plugins(self) -> list[str]:
        with self._lock:
            if self._closed:
                return []
            return self._plugin_dirs()

    # -- attempts ledger ------------------------------------------------

    def read_attempt(self, plugin: str, h: str) -> Marker:
        """Three-state read of ``attempts/<h>.json`` — the per-flow attempt
        record, under the same marker discipline as :meth:`read_marker`.

        ABSENT for a genuinely missing file (or a closed spool, an unknown
        plugin, a bad name — the fail-quiet arms every sibling read shares);
        INVALID for anything present but untrustworthy (non-regular,
        oversized, malformed — a consumer-scribbled file must read as INVALID
        so the caller re-derives it from artifacts, never as truth). Never
        blocks and never raises."""
        with self._lock:
            if self._closed or not _safe_component(plugin) or not _is_hash(h):
                return Marker(MarkerState.ABSENT)
            try:
                pfd = self._plugin_fd(plugin)
            except FileNotFoundError:
                return Marker(MarkerState.ABSENT)
            except (OSError, ValueError):
                return Marker(MarkerState.INVALID)
            try:
                try:
                    afd = _open_dir(ATTEMPTS_DIR, pfd)
                except FileNotFoundError:
                    return Marker(MarkerState.ABSENT)   # pre-upgrade dir
                except OSError:
                    return Marker(MarkerState.INVALID)
                try:
                    return _read_marker_at(f"{h}.json", afd)
                finally:
                    os.close(afd)
            finally:
                os.close(pfd)

    def write_attempt(self, plugin: str, h: str, rec: dict, *,
                      strict: bool = False) -> bool:
        """Atomically replace ``attempts/<h>.json`` with the canonical bytes
        of *rec* (the :meth:`_replace_json` discipline — attempt files are
        replacing ADVISORY records, never publish-once artifacts). Creates
        ``attempts/`` on demand (a pre-upgrade plugin dir grows one on the
        first write, exactly as :meth:`ensure_plugin_dirs` would have).

        ``strict=False`` (default): best-effort fsyncs; True once the replace
        sequence completed. ``strict=True`` — the write-ahead-outcome
        variant: the STAGED file is fsynced strictly BEFORE the rename (a
        failure aborts with the previous record intact), then ``attempts/``
        and then the PLUGIN DIR that names it are fsynced strictly AFTER it
        (a failure returns False although the new record may already be
        VISIBLE — that is correct: the caller skips the dependent deletion
        this pass, and a visible terminal record beside a live artifact is
        the provisional state the next pass re-derives). True only when
        fully durable.

        The plugin-parent fsync is strict on EVERY strict call, not only the
        one that created ``attempts/``: a v0.146 dir has no ``attempts/``
        yet, so the creating call's best-effort parent fsync is exactly where
        a silent failure lands — and every later call sees ``created=False``
        and would never retry it, leaving a whole directory entry that a
        power loss can drop while the deletions it authorized survive
        (INV-CB-007).

        Never raises into callers: every failure is False plus a warning that
        names no hash (INV-CB-006)."""
        if not _safe_component(plugin) or not _is_hash(h):
            return False
        with self._lock:
            if self._closed:
                return False
            try:
                pfd = self._plugin_fd(plugin)
            except (OSError, ValueError):
                return False
            try:
                try:
                    created = self._mkdir(ATTEMPTS_DIR, pfd)
                    if created:
                        # The directory entry itself must be durable before
                        # an entry inside it is (see _index_fd).
                        _fsync(pfd, plugin)
                    afd = _open_dir(ATTEMPTS_DIR, pfd)
                except OSError as exc:
                    logger.warning("callback-spool: attempts dir unavailable "
                                   "(errno %s)", exc.errno)
                    return False
                try:
                    if created:
                        self._chmod_dir(afd, ATTEMPTS_DIR)
                    return self._write_attempt_at(afd, pfd, plugin,
                                                  f"{h}.json", rec, strict)
                finally:
                    os.close(afd)
            finally:
                os.close(pfd)

    def _write_attempt_at(self, afd: int, pfd: int, plugin: str, name: str,
                          rec: dict, strict: bool) -> bool:
        """The staged-replace sequence of :meth:`write_attempt`, openat-
        relative to the attempts dir FD (*pfd* is the plugin dir that names
        it — the strict path proves that entry durable too). Called with
        ``_lock`` held."""
        if not strict:
            try:
                self._replace_json(name, afd, rec, ATTEMPTS_DIR)
                return True
            except (OSError, TypeError, ValueError) as exc:
                logger.warning("callback-spool: attempt write failed "
                               "(errno %s)", getattr(exc, "errno", None))
                return False
        try:
            data = canonical_marker_bytes(rec)
        except (TypeError, ValueError):
            logger.warning("callback-spool: attempt record is not "
                           "serializable")
            return False
        # A False here means the new record may be VISIBLE but is not proven
        # durable; the caller must skip its dependent deletion this pass (a
        # visible terminal record beside a live artifact converges next pass).
        return _strict_replace_at(name, afd, data, parent_fd=pfd,
                                  what=ATTEMPTS_DIR, parent_what=plugin,
                                  role="attempt")

    def update_attempt_nudge(self, plugin: str, h: str, **fields) -> bool:
        """Merge delivery bookkeeping into ``attempts/<h>.json``.

        The ONE write the delivery worker owns. Only the five nudge fields
        may be set (``nudges``, ``last_nudge_ts``, ``next_nudge_ts``,
        ``deferrals``, ``noted``) — status, outcome, ``meta`` and the flow's
        identity belong to the spool's own derivation, and a worker that
        could rewrite them would turn an advisory schedule into a second
        source of truth.

        Read-merge-write under ``_lock`` (re-entrant, so the nested
        :meth:`write_attempt` is the same critical section): the record is
        validated on the way in AND after the merge, so a nonsense update is
        refused rather than persisted. Best-effort durability by design —
        no deletion depends on it, and a lost update costs exactly one
        duplicate nudge, which the consumer's collect is idempotent against
        (INV-CB-008's at-least-once boundary).

        False on anything that did not go through: absent/unreadable/invalid
        record, an out-of-vocabulary field, a merge that fails validation, a
        failed write. The caller simply re-derives on its next pass."""
        if not fields or not set(fields) <= _NUDGE_FIELDS:
            return False
        with self._lock:
            marker = self.read_attempt(plugin, h)
            if marker.state is not MarkerState.PRESENT:
                return False
            rec = callback_attempts.validate_attempt(marker.payload,
                                                     expect_hash=h)
            if rec is None:
                return False
            merged = callback_attempts.validate_attempt(dict(rec, **fields),
                                                        expect_hash=h)
            if merged is None:
                logger.warning("callback-spool: rejected an invalid attempt "
                               "nudge update (%r)", plugin)
                return False
            return self.write_attempt(plugin, h, merged)

    def list_attempts(self, plugin: str) -> list[tuple[str, dict]]:
        """``(hash, validated record)`` for every readable, schema-valid
        ``attempts/<h>.json``, sorted by hash. Only VALID pairs — a malformed
        file is never returned as truth; it is named by
        :meth:`list_invalid_attempts` instead. ``[]`` on a closed spool or a
        missing plugin/attempts dir."""
        return self._scan_attempts(plugin)[0]

    def list_invalid_attempts(self, plugin: str) -> list[str]:
        """Hashes whose attempt file EXISTS but is INVALID or schema-
        malformed — the re-derivation worklist (rewritten from live
        artifacts, or retired, by a later pass). ``[]`` on a closed spool or
        a missing dir."""
        return self._scan_attempts(plugin)[1]

    def _scan_attempts(self, plugin: str) -> tuple[list[tuple[str, dict]],
                                                   list[str]]:
        with self._lock:
            if self._closed or not _safe_component(plugin):
                return [], []
            try:
                pfd = self._plugin_fd(plugin)
            except (OSError, ValueError):
                return [], []
            try:
                try:
                    afd = _open_dir(ATTEMPTS_DIR, pfd)
                except OSError:
                    return [], []
                try:
                    valid: list[tuple[str, dict]] = []
                    invalid: list[str] = []
                    for name in _listdir_quiet(afd):
                        h = _hash_of_pending(name)
                        if h is None:
                            continue         # tokens/residue: not attempts
                        marker = _read_marker_at(name, afd)
                        if marker.state is MarkerState.ABSENT:
                            continue         # vanished mid-scan
                        # expect_hash: the NAME is the flow's identity, so a
                        # record embedding a different hash is INVALID and
                        # goes to the re-derivation worklist — never onto the
                        # worker's read surface carrying another flow's
                        # identity.
                        rec = (callback_attempts.validate_attempt(
                                   marker.payload, expect_hash=h)
                               if marker.state is MarkerState.PRESENT
                               else None)
                        if rec is None:
                            invalid.append(h)
                        else:
                            valid.append((h, rec))
                    valid.sort(key=lambda pair: pair[0])
                    return valid, sorted(invalid)
                finally:
                    os.close(afd)
            finally:
                os.close(pfd)

    def list_ack_tokens(self, plugin: str) -> list[str]:
        """Hashes for which the consumer left a receipt token — entries named
        exactly ``.ack-<64hex>`` under ``attempts/``. Near-misses are ignored
        (they are residue, not receipts). ``[]`` on a closed spool or a
        missing dir."""
        with self._lock:
            if self._closed or not _safe_component(plugin):
                return []
            try:
                pfd = self._plugin_fd(plugin)
            except (OSError, ValueError):
                return []
            try:
                try:
                    afd = _open_dir(ATTEMPTS_DIR, pfd)
                except OSError:
                    return []
                try:
                    return sorted(
                        name[len(ACK_PREFIX):]
                        for name in _listdir_quiet(afd)
                        if name.startswith(ACK_PREFIX)
                        and _is_hash(name[len(ACK_PREFIX):]))
                finally:
                    os.close(afd)
            finally:
                os.close(pfd)

    def collect_held_hashes(self, plugin: str) -> list[str]:
        """Hashes named by live consumer-held ``results/.collect-<h>-<uuid>``
        entries — exactly 64 hex after the prefix, then ``-`` (anything else
        is residue, not a hold). Deduplicated and sorted; ``[]`` on a closed
        spool or a missing dir. Enumeration is by NAME only — casa never
        opens a consumer-held file."""
        with self._lock:
            if self._closed or not _safe_component(plugin):
                return []
            try:
                pfd = self._plugin_fd(plugin)
            except (OSError, ValueError):
                return []
            try:
                try:
                    results = _open_dir(RESULTS_DIR, pfd)
                except OSError:
                    return []
                try:
                    return sorted({
                        h for h in (_hash_of_collect(name)
                                    for name in _listdir_quiet(results))
                        if h is not None})
                finally:
                    os.close(results)
            finally:
                os.close(pfd)

    # -- write-ahead outcomes (INV-CB-007) ------------------------------

    def _derive_attempt(self, plugin: str, h: str, *, pend_fd: "int | None",
                        results_fd: "int | None", claims_fd: "int | None",
                        now: float) -> dict:
        """What casa knows about the flow named *h* — the ONE derivation every
        write-ahead site shares (so the record a deletion depends on never
        varies with which pass happened to reach the artifact first).

        An existing, schema-VALID attempt file is the record: it carries the
        consumer's ``meta``, the true mint clock and the worker's schedule
        state, none of which artifacts alone can reconstruct. Only when there
        is none (or it is malformed — a consumer-scribbled file is never
        truth) is a record derived from the artifacts that survive."""
        marker = self.read_attempt(plugin, h)
        if marker.state is MarkerState.PRESENT:
            # Bound to the NAME: a record claiming another flow's hash is not
            # this flow's record, and a write-ahead outcome derived from it
            # would carry that identity (and its ``meta``) onto the deletion
            # this call precedes.
            rec = callback_attempts.validate_attempt(marker.payload,
                                                     expect_hash=h)
            if rec is not None:
                return rec
        return self._derive_from_artifacts(
            plugin, h, pend_fd=pend_fd, results_fd=results_fd,
            claims_fd=claims_fd, now=now)

    def _derive_from_artifacts(self, plugin: str, h: str, *,
                               pend_fd: "int | None",
                               results_fd: "int | None",
                               claims_fd: "int | None",
                               now: float) -> dict:
        """A fresh attempt record built from the artifacts alone (spec §3.3).

        Mint clock and ``meta`` come from the first surviving source, in the
        order the mint travels: the ``pending/`` inode's envelope (mtime = the
        mint time), else the claim inode's (``link(2)`` preserved that mtime),
        else the casa-authored result record's own ``meta``/``minted_ts``
        transport keys. A flow known ONLY by a consumer-held ``.collect-*``
        name yields ``meta``/``minted_ts`` None: that file belongs to its
        holder and casa never opens it — it only reads the name.

        Status follows the same precedence the re-derivation rule uses: a live
        result or hold means ``result_ready`` (``claimed`` iff a hold exists),
        anything else ``awaiting_redirect``."""
        meta = None
        minted_ts = None
        sourced = False
        st = _regular_stat(f"{h}.json", pend_fd)
        if st is not None:
            envelope = _read_envelope_at(f"{h}.json", pend_fd)
            meta = envelope["meta"] if envelope else None
            minted_ts = st.st_mtime
            sourced = True
        if not sourced:
            st = _regular_stat(h, claims_fd)
            if st is not None:
                envelope = _read_envelope_at(h, claims_fd)
                meta = envelope["meta"] if envelope else None
                minted_ts = st.st_mtime
                sourced = True
        has_result = _regular_stat(f"{h}.json", results_fd) is not None
        if has_result and not sourced:
            # Casa's OWN record (not consumer-authored): the ordinary marker
            # read, whose 64 KiB cap is ample for a callback result.
            marker = _read_marker_at(f"{h}.json", results_fd)
            if marker.state is MarkerState.PRESENT:
                meta = marker.payload.get("meta")
                ts = marker.payload.get("minted_ts")
                minted_ts = ts if isinstance(ts, (int, float)) \
                    and not isinstance(ts, bool) else None
        held = self._collect_present(h, results_fd)
        return callback_attempts.new_attempt(
            state_hash=h, minted_ts=minted_ts,
            status="result_ready" if (has_result or held)
            else "awaiting_redirect",
            meta=meta, claimed=held, now=now)

    @staticmethod
    def _collect_present(h: str, results_fd: "int | None") -> bool:
        """True iff a conforming ``results/.collect-<h>-<uuid>`` entry exists.
        By NAME only — the held file is the consumer's."""
        if results_fd is None:
            return False
        return any(_hash_of_collect(name) == h
                   for name in _listdir_quiet(results_fd))

    def _write_ahead(self, plugin: str, h: str, outcome: str, *,
                     dirs: _ArtifactDirs, now: float,
                     claimed: "bool | None" = None) -> bool:
        """Record *outcome* for the flow *h* durably, BEFORE the deletion that
        depends on it (INV-CB-007). Returns False when the strict write could
        not prove durability — the caller then skips its deletion this pass,
        so a power crash can never take the credential-bearing artifact and
        its record together."""
        rec = self._derive_attempt(plugin, h, pend_fd=dirs.pend,
                                   results_fd=dirs.results,
                                   claims_fd=dirs.claims, now=now)
        done = callback_attempts.terminalize(rec, outcome, now=now,
                                             claimed=claimed)
        return self.write_attempt(plugin, h, done, strict=True)

    def _repair_after_lost_deletion(self, plugin: str, h: str, *,
                                    dirs: _ArtifactDirs, now: float) -> None:
        """The re-derivation rule at its sharpest edge (spec §6): the deletion
        that the just-written terminal outcome authorized did NOT happen — the
        entry was gone, typically because a consumer's ``collect`` rename won
        the race between the record and the unlink.

        The terminal label is therefore provisional. Re-probe (three-state:
        an UNKNOWN result probe defers to the next pass rather than guessing)
        and, when a live result or hold now witnesses the contradiction,
        rewrite the attempt OPEN from what exists. A genuinely absent flow
        keeps its terminal record — that is the deletion having simply been
        completed by someone else."""
        if dirs.results is None:
            return
        res = _marker_lstat(f"{h}.json", dirs.results)
        if res is _LSTAT_ERROR:
            return                           # UNKNOWN — never guess
        if res is None and not self._collect_present(h, dirs.results):
            return                           # nothing survives to contradict it
        self._rewrite_from_artifacts(plugin, h, dirs=dirs, now=now)

    def _rewrite_from_artifacts(self, plugin: str, h: str, *,
                                dirs: _ArtifactDirs,
                                now: float) -> "dict | None":
        """Rewrite the attempt for *h* from the artifacts that exist NOW — the
        re-derivation rule's one write path (spec §6), shared by the
        lost-deletion repair, the invalid-file rebuild and the reopen/rewind
        arms of :meth:`attempts_pass`.

        Returns the record written, or ``None`` when the write failed. Not a
        write-ahead: no deletion depends on this rewrite, so a failure simply
        leaves the provisional record for the next pass to repair."""
        rec = self._derive_from_artifacts(
            plugin, h, pend_fd=dirs.pend, results_fd=dirs.results,
            claims_fd=dirs.claims, now=now)
        # What survives may no longer carry the consumer's binding — a hold is
        # a NAME, and casa never opens the file it names — so keep what the
        # provisional record still knows rather than dropping it (§4). A
        # record that will not validate knows nothing: nothing is carried.
        prior = callback_attempts.validate_attempt(
            self.read_attempt(plugin, h).payload, expect_hash=h)
        if prior is not None:
            for key in ("meta", "minted_ts"):
                if rec[key] is None:
                    rec[key] = prior[key]
        return rec if self.write_attempt(plugin, h, rec) else None

    # -- the attempts pass ----------------------------------------------

    def attempts_pass(self, *, now: float, boot: bool) -> AttemptsReport:
        """Reconcile the per-flow ledger against the artifacts (spec §§3.3,
        6, 9) — the standing rule that makes the attempt file a DERIVED
        record rather than a second source of truth.

        Four phases per plugin, in this order and for this reason:

        1. **Ack consumption** — the consumer's receipt supersedes every
           record, so it runs before anything can re-materialize what the
           receipt retires.
        2. **Materialization** — every hash present in ``pending/`` ∪
           ``results/`` ∪ ``.claims/`` ∪ the ``.collect-*`` held names that
           has no attempt file gets one, derived from whichever source
           survives (§3.3). A flow casa knows about always has a record.
        3. **Re-derivation** — an unreadable record is rewritten from live
           artifacts or, with none, retired as an anomaly; a terminal record
           coexisting with ANY live artifact of its hash is provisional and
           is rewritten open; a ``result_ready`` record whose flow rewound
           to a live pending goes back to ``awaiting_redirect``; and an open
           ``result_ready`` record whose hash has a live hold has ``claimed``
           raised in place (§6).
        4. **Receipt inference** — an open ``result_ready`` attempt whose
           five §6 probes all confirm absence is settled ``collected``.
        5. **Bounds** — terminal records age out at
           ``ATTEMPT_RETENTION_S``; ``MAX_ATTEMPTS`` evicts oldest-terminal
           first, with a strict terminalize-then-remove valve for the
           pathological open-attempt overflow (§9).

        Ordering matters: materialization before re-derivation (so a fresh
        record is judged against the same artifacts), re-derivation before
        inference (so a rewound flow is never mistaken for a collected one),
        and the bounds last (so nothing is retired before it is correct).

        A periodic pass (``boot=False``) skips in-flight hashes exactly as
        recovery does — handler and pass run in the same process, and a
        handler between claim and publish is still building the artifact
        state this pass would read. A boot pass has no live handlers by
        construction.
        """
        report = AttemptsReport()
        with self._lock:
            if self._closed:
                return report
            for plugin in self._plugin_dirs():
                try:
                    pfd = self._plugin_fd(plugin)
                except (OSError, ValueError):
                    continue
                try:
                    self._attempts_plugin(plugin, pfd, now, boot, report)
                finally:
                    os.close(pfd)
        return report

    def _attempts_plugin(self, plugin: str, pfd: int, now: float, boot: bool,
                         report: AttemptsReport) -> None:
        """One plugin's attempts pass, with the four artifact directory FDs
        opened ONCE: every phase reads the same on-disk state, so a flow
        cannot be judged against one directory as it was and another as it
        became. Called with ``_lock`` held."""
        fds: dict[str, "int | None"] = {}
        for sub in (PENDING_DIR, RESULTS_DIR, CLAIMS_DIR):
            try:
                fds[sub] = _open_dir(sub, pfd)
            except OSError:
                fds[sub] = None              # that source is simply lost
        try:
            try:
                created = self._mkdir(ATTEMPTS_DIR, pfd)
                if created:
                    _fsync(pfd, plugin)      # the dir before entries in it
                afd = _open_dir(ATTEMPTS_DIR, pfd)
            except OSError as exc:
                logger.warning("callback-spool: attempts dir unavailable "
                               "(errno %s)", exc.errno)
                return
            try:
                if created:
                    self._chmod_dir(afd, ATTEMPTS_DIR)
                dirs = _ArtifactDirs(pend=fds[PENDING_DIR],
                                     results=fds[RESULTS_DIR],
                                     claims=fds[CLAIMS_DIR])
                # 1. Ack tokens FIRST: a receipt supersedes every record, so
                # the teardown must run before any phase can re-materialize
                # what the receipt retires.
                self._consume_ack_tokens(plugin, afd, dirs, boot, report)
                valid, invalid = self._scan_attempts(plugin)
                records = dict(valid)
                self._materialize_attempts(plugin, dirs, records,
                                           set(invalid), now, report)
                self._rederive_attempts(plugin, afd, dirs, records, invalid,
                                        now, report)
                self._infer_receipts(plugin, dirs, records, now, boot, report)
                self._bound_attempts(plugin, afd, records, now, report)
            finally:
                os.close(afd)
        finally:
            for fd in fds.values():
                if fd is not None:
                    os.close(fd)

    @staticmethod
    def _probe(name: str, dir_fd: "int | None") -> "bool | None":
        """Three-state presence probe: True (present), False (confirmed
        ENOENT), ``None`` (UNKNOWN — a metadata failure, or a directory that
        would not open). Only a confirmed absence is ever read as absence
        (spec §6, "proved absence, not assumed absence")."""
        if dir_fd is None:
            return None
        st = _marker_lstat(name, dir_fd)
        if st is _LSTAT_ERROR:
            return None
        return st is not None

    @staticmethod
    def _probe_collect(h: str, results_fd: "int | None") -> "bool | None":
        """The same three states for the ``.collect-<h>-*`` enumeration: a
        listing that FAILS is UNKNOWN, never an empty directory (which
        ``_listdir_quiet`` would make it)."""
        if results_fd is None:
            return None
        try:
            names = os.listdir(results_fd)
        except OSError:
            return None
        return any(_hash_of_collect(name) == h for name in names)

    # -- ack consumption (spec §7) --------------------------------------

    def _consume_ack_tokens(self, plugin: str, afd: int, dirs: _ArtifactDirs,
                            boot: bool, report: AttemptsReport) -> None:
        """Consume the consumer's receipts: for every ``attempts/.ack-<h>``
        token, retire EVERY artifact of that hash and then the token itself
        (spec §7).

        The ack is the consumer's durable declaration that it has absorbed the
        flow's outcome, so it SUPERSEDES the record: nothing here is written
        write-ahead and no outcome is recorded (INV-CB-007 arm (a)) — the
        deletions' sole audience has already spoken. Two edges follow from
        that: a PREMATURE ack (result or hold still live) kills the artifacts
        instead of leaving an orphan to be re-materialized and nudged forever,
        and an ack on an open ``awaiting_redirect`` attempt is the consumer's
        ABORT verb (the pending dies here rather than expiring noisily later).

        A periodic pass SKIPS any in-flight hash (guard (a) of spec §7): a
        handler between ``claim()`` and ``publish_result()`` holds no lock and
        is still building this flow's artifact state. Deferring costs nothing —
        the token is a rename, so it is durable, and the next pass tears down
        even the result the racing publisher managed to publish. A boot pass
        has no live handlers by construction, so it never defers."""
        for h in self.list_ack_tokens(plugin):
            if not boot and in_flight_key(plugin, h) in self._in_flight:
                continue                     # the publisher owns it this pass
            if not self._teardown_flow(h, afd, dirs):
                report.acks_deferred += 1
                continue
            # Only now, with every artifact a CONFIRMED ENOENT: the token's
            # job is done, so a best-effort fsync is enough — losing the
            # token's removal to a crash only replays a teardown that finds
            # nothing left to do.
            if _unlink_quiet(f"{ACK_PREFIX}{h}", afd):
                _fsync(afd, ATTEMPTS_DIR)
                report.acks_consumed += 1
            else:
                report.acks_deferred += 1

    def _teardown_flow(self, h: str, afd: int, dirs: _ArtifactDirs) -> bool:
        """Run the §7 deletion sequence for *h* until every artifact of the
        flow is PROVEN gone. True only on all-confirmed-ENOENT — the sole
        licence to delete the receipt token.

        A PRESENT artifact on the proof step means something raced the
        teardown (a collector rename, a casa replace resurrecting the attempt
        file), so the sequence runs once more — bounded at two, because a
        second failure is no longer a race and the durable token is what
        carries the work to the next pass. An UNKNOWN probe or a failed
        strict fsync keeps the token immediately: absence is proved, never
        assumed, and an unprovable teardown must not consume the receipt."""
        for _round in range(2):
            try:
                self._teardown_once(h, afd, dirs)
            except FsyncFailed as exc:
                # No hash in the message (INV-CB-006).
                logger.warning("callback-spool: ack teardown fsync failed "
                               "(errno %s)", exc.errno)
                return False
            proven = self._teardown_proven(h, afd, dirs)
            if proven is None:               # UNKNOWN — never guess
                return False
            if proven:
                return True
        return False

    def _teardown_once(self, h: str, afd: int, dirs: _ArtifactDirs) -> None:
        """One deletion sweep over every artifact class of *h*, in the ONE
        order that converges (spec §7, Sol r6), then a strict fsync of EVERY
        artifact directory available this pass. Raises :class:`FsyncFailed`.

        ``pending``/``.claims``/the claim temp/``results/<h>.json`` go FIRST,
        and the ``.collect-<h>-*`` enumeration comes AFTER them: a collector
        rename racing the teardown moves the result's bytes to a name only an
        enumeration performed after that unlink can see, so nothing is
        stranded. The attempt file goes LAST — a staged replace can resurrect
        it right up to that point (which is why the ack is a rename, not an
        unlink: the token no replace can erase is what brings us back here).

        **Every directory, every pass — not only the ones this pass changed
        (Sol r7).** The token is consumed on the strength of the whole
        teardown, not of one pass's unlinks, and the retry that finishes the
        job is exactly the pass with nothing left to unlink: pass 1 removes
        the result and its directory fsync FAILS (token correctly kept), then
        pass 2 finds the name already absent from the page cache, changes
        nothing, and would — fsyncing only what it touched — prove ENOENT and
        delete the token having proven NOTHING durable. A power loss then
        rolls pass 1's unlink back and resurrects an unreceipted credential
        with no token left to drive its teardown. Re-fsyncing a directory
        that did not change is free when it is already durable and is the
        only way to observe that it is not."""

        def drop(name: str, fd: "int | None") -> None:
            if fd is not None:
                _unlink_quiet(name, fd)

        drop(f"{h}.json", dirs.pend)
        drop(h, dirs.claims)
        drop(f"{TEMP_PREFIX}{h}", dirs.claims)
        drop(f"{h}.json", dirs.results)
        if dirs.results is not None:
            for name in _listdir_quiet(dirs.results):
                if _hash_of_collect(name) == h:
                    drop(name, dirs.results)
        drop(f"{h}.json", afd)
        for fd, what in ((dirs.pend, PENDING_DIR), (dirs.claims, CLAIMS_DIR),
                         (dirs.results, RESULTS_DIR), (afd, ATTEMPTS_DIR)):
            # Strict: the token is deleted on the strength of these removals —
            # this pass's and every earlier pass's — so a crash must not roll
            # one back under a consumed receipt. A directory that would not
            # open (``None``) is not skipped silently: the proof step's own
            # UNKNOWN probe against that missing FD keeps the token anyway.
            if fd is not None:
                _fsync_strict(fd, what)

    def _teardown_proven(self, h: str, afd: int,
                         dirs: _ArtifactDirs) -> "bool | None":
        """The proof step: True when every artifact of *h* is a confirmed
        ENOENT, False when one is PRESENT (something raced the sweep), None
        when any probe is UNKNOWN — a directory that would not open included,
        which is why a lost artifact FD keeps the token instead of licensing
        its deletion."""
        probes = (self._probe(f"{h}.json", dirs.pend),
                  self._probe(h, dirs.claims),
                  self._probe(f"{TEMP_PREFIX}{h}", dirs.claims),
                  self._probe(f"{h}.json", dirs.results),
                  self._probe_collect(h, dirs.results),
                  self._probe(f"{h}.json", afd))
        if any(p is None for p in probes):
            return None
        return not any(probes)

    def _flow_artifacts_live(self, h: str, dirs: _ArtifactDirs) -> bool:
        """True iff ANY artifact of the flow *h* is still on disk — the
        witness that contradicts a terminal record (spec §6). Best-effort by
        design: an unreadable directory yields no witness, so the record is
        left alone and the next pass re-examines it."""
        for name, fd in ((f"{h}.json", dirs.pend), (h, dirs.claims),
                         (f"{h}.json", dirs.results)):
            if fd is not None and _lstat_quiet(name, fd) is not None:
                return True
        return self._collect_present(h, dirs.results)

    def _materialize_attempts(self, plugin: str, dirs: _ArtifactDirs,
                              records: dict, invalid: set, now: float,
                              report: AttemptsReport) -> None:
        """Spec §3.3 phase 3: give every hash casa can see a record.

        The union is read by NAME from all four sources; hashes whose record
        merely fails to VALIDATE are left to the re-derivation phase (they
        have a file, and overwriting it here would double-count the same
        repair)."""
        union: set[str] = set()
        if dirs.pend is not None:
            union.update(h for h in (_hash_of_pending(n)
                                     for n in _listdir_quiet(dirs.pend))
                         if h is not None)
        if dirs.results is not None:
            for name in _listdir_quiet(dirs.results):
                h = _hash_of_pending(name) or _hash_of_collect(name)
                if h is not None:
                    union.add(h)
        if dirs.claims is not None:
            union.update(n for n in _listdir_quiet(dirs.claims) if _is_hash(n))
        for h in sorted(union - set(records) - invalid):
            rec = self._derive_attempt(plugin, h, pend_fd=dirs.pend,
                                       results_fd=dirs.results,
                                       claims_fd=dirs.claims, now=now)
            # Not a write-ahead: no deletion depends on a materialization.
            if self.write_attempt(plugin, h, rec):
                records[h] = rec
                report.materialized += 1

    def _rederive_attempts(self, plugin: str, afd: int, dirs: _ArtifactDirs,
                           records: dict, invalid: list, now: float,
                           report: AttemptsReport) -> None:
        """Spec §6 + amendment 6: make every record agree with the artifacts.

        An INVALID file (consumer-scribbled, truncated, non-regular) is
        rewritten from live artifacts when any exist, and otherwise RETIRED
        with an anomaly — never read as an ack, never a reason to delete an
        artifact, and never given a terminal outcome casa cannot justify.

        A record that contradicts a live artifact is then rewritten open, in
        the amendment-6 precedence: result or hold ⇒ ``result_ready``
        (``claimed`` iff a hold exists), else pending or claim ⇒
        ``awaiting_redirect`` — both of which
        :meth:`_derive_from_artifacts` already encodes.

        An ALREADY-open ``result_ready`` record is the third case, and it is
        not a rewrite: a live hold raises its ``claimed`` flag in place (see
        :meth:`_raise_claimed`). The rewind arm is tried first — a live hold
        is precisely what proves the flow did NOT rewind — so the two arms
        can never both fire for one record."""
        for h in invalid:
            if self._flow_artifacts_live(h, dirs):
                rec = self._rewrite_from_artifacts(plugin, h, dirs=dirs,
                                                   now=now)
                if rec is not None:
                    records[h] = rec
                    report.materialized += 1
                continue
            name = f"{h}.json"
            st = _lstat_quiet(name, afd)
            if st is not None and _remove_entry(name, afd, st):
                _fsync(afd, ATTEMPTS_DIR)
            # No hash in the message (INV-CB-006): the anomaly is the CLASS.
            report.anomalies.append(
                f"{plugin}: unreadable attempt record retired")
        for h, rec in sorted(records.items()):
            if rec["status"] == "done":
                if not self._flow_artifacts_live(h, dirs):
                    continue                 # nothing contradicts it
            elif rec["status"] == "result_ready":
                # The flow rewound: recovery restored the crashed claim to
                # pending/ and no result or hold survives to say otherwise.
                if not (self._probe(f"{h}.json", dirs.pend) is True
                        and self._probe(f"{h}.json", dirs.results) is False
                        and self._probe_collect(h, dirs.results) is False):
                    # Not a rewind. A hold may still have appeared since the
                    # record was written, and that is a flag change, not a
                    # rewrite.
                    self._raise_claimed(plugin, h, dirs, records, report)
                    continue
            else:
                continue
            new = self._rewrite_from_artifacts(plugin, h, dirs=dirs, now=now)
            if new is not None:
                records[h] = new
                report.reopened += 1

    def _raise_claimed(self, plugin: str, h: str, dirs: _ArtifactDirs,
                       records: dict, report: AttemptsReport) -> None:
        """Raise ``claimed`` on an OPEN ``result_ready`` record whose hash has
        a live ``.collect-<h>-*`` hold (spec §6).

        The flag is the SUCCESSOR CONSUMER's signal while the hold is still
        live: it "tells its next life the payload may or may not have been
        seen", and its own store is the tiebreaker. Left false, a successor
        calls :func:`collect`, takes the ``FileNotFoundError`` that §7 makes
        retryable-but-never-ackable, and retries until the hold ages out
        instead of learning its predecessor already took the result. The
        terminal write-ahead and the reopen-from-terminal path both already
        set the flag from this same witness (:meth:`_collect_present`, the
        one :meth:`_derive_from_artifacts` uses); only the already-open
        record was left stale.

        Deliberately a MINIMAL merge rather than a
        :meth:`_rewrite_from_artifacts`: the record is not contradicted by
        the artifacts, it is merely incomplete, and rebuilding it would
        discard the worker's durable schedule state (``nudges``,
        ``next_nudge_ts``, ``deferrals``, ``noted``) — resetting a spent
        redelivery budget, which INV-CB-008 bounds. Not a write-ahead either:
        no deletion depends on the flag, so a failed write simply leaves the
        next pass to raise it again from the same hold.

        Ordering is inert for the receipt inference that follows: a live hold
        already blocks it at probe 2, and the inference never reads
        ``claimed``.
        """
        rec = records[h]
        if rec["claimed"] or not self._collect_present(h, dirs.results):
            return
        raised = dict(rec, claimed=True)
        if self.write_attempt(plugin, h, raised):
            records[h] = raised
            report.claimed_raised += 1

    def _infer_receipts(self, plugin: str, dirs: _ArtifactDirs, records: dict,
                        now: float, boot: bool,
                        report: AttemptsReport) -> None:
        """Spec §6: settle an open ``result_ready`` attempt as ``collected``.

        The five probes run in the NORMATIVE order and short-circuit: any
        PRESENT stops the inference, any UNKNOWN defers it to the next pass.

        1. ``results/<h>.json`` confirmed absent;
        2. no ``results/.collect-<h>-*`` entry remains;
        3. no ``.claims/<h>`` (attempt-first publishing makes "attempt says
           ``result_ready``, claim still live" a real crash state);
        4. no ``pending/<h>.json`` (recovery may have rewound the flow);
        5. casa did not itself SUCCESSFULLY delete the artifacts — which
           reaching here with an OPEN attempt already proves: every casa
           deletion writes its terminal outcome first (INV-CB-007), so a
           completed casa deletion has left a ``done`` record and this arm is
           unreachable for it. A provisional record whose deletion LOST to
           ENOENT is exactly what the re-derivation phase above reopened.
        """
        for h, rec in sorted(records.items()):
            if rec["status"] != "result_ready":
                continue
            if not boot and in_flight_key(plugin, h) in self._in_flight:
                continue
            if self._probe(f"{h}.json", dirs.results) is not False:
                continue
            if self._probe_collect(h, dirs.results) is not False:
                continue
            if self._probe(h, dirs.claims) is not False:
                continue
            if self._probe(f"{h}.json", dirs.pend) is not False:
                continue
            done = callback_attempts.terminalize(rec, "collected", now=now)
            # Not a write-ahead: no deletion depends on this outcome (there
            # is nothing left to delete), so a failed write simply means the
            # next pass infers it again from the same absences.
            if self.write_attempt(plugin, h, done):
                records[h] = done
                report.collected += 1

    def _bound_attempts(self, plugin: str, afd: int, records: dict,
                        now: float, report: AttemptsReport) -> None:
        """Spec §9: the retention bound and the ``MAX_ATTEMPTS`` ladder.

        Age-out needs no write-ahead — the file IS the record being retired,
        at the bound the invariant names. The cap deletes oldest-``ended_ts``
        TERMINAL files first; only a pathological overflow reaches the open
        arm, where each victim is terminalized ``evicted`` STRICTLY and the
        file removed ONLY when that is proven durable (amendment 10), so
        what the cap destroys is always a terminal record. ``.ack-*`` tokens
        are neither counted nor evicted: they are the consumer's receipts in
        flight, and the ack phase consumes them."""
        retained: list[tuple[str, dict]] = []
        touched = False
        for h, rec in sorted(records.items()):
            ended = rec["ended_ts"]
            if rec["status"] == "done" and ended is not None \
                    and now - ended > callback_attempts.ATTEMPT_RETENTION_S:
                if _unlink_quiet(f"{h}.json", afd):
                    report.aged_out += 1
                    touched = True
                continue
            retained.append((h, rec))
        excess = len(retained) - MAX_ATTEMPTS
        if excess > 0:
            logger.warning("callback-spool: %s holds %d attempt records — "
                           "oldest-terminal-first eviction applied",
                           plugin, len(retained))
            terminal = sorted(
                (pair for pair in retained if pair[1]["status"] == "done"),
                key=lambda pair: (pair[1]["ended_ts"] or 0.0, pair[0]))
            for h, _rec in terminal[:excess]:
                if _unlink_quiet(f"{h}.json", afd):
                    report.capped += 1
                    excess -= 1
                    touched = True
            for h, rec in self._eviction_order(retained)[:max(excess, 0)]:
                done = callback_attempts.terminalize(rec, "evicted", now=now)
                if not self.write_attempt(plugin, h, done, strict=True):
                    report.skipped_undurable += 1
                    continue
                if _unlink_quiet(f"{h}.json", afd):
                    report.capped += 1
                    touched = True
        if touched:
            _fsync(afd, ATTEMPTS_DIR)

    @staticmethod
    def _eviction_order(retained: list) -> list:
        """The open attempts of *retained*, oldest mint first — a record with
        no mint clock (a hold-only materialization) is what casa knows least
        about, so it sorts oldest."""
        return sorted((pair for pair in retained
                       if pair[1]["status"] != "done"),
                      key=lambda pair: (
                          pair[1]["minted_ts"]
                          if pair[1]["minted_ts"] is not None else 0.0,
                          pair[0]))

    # -- recovery -------------------------------------------------------

    def recovery_pass(self, *, now: float, boot: bool) -> RecoveryReport:
        """Converge ``.claims/`` residue left by a crash.

        Two phases per plugin, in this order and for this reason: orphan
        ``.tmp-<hash>`` temps are unlinked and made durable FIRST, so that
        when the same hash's claim is restored to ``pending/`` the retry does
        not find its deterministic temp name occupied.

        Then per claim: stale or future-mtime ⇒ delete; a matching published
        result ⇒ report for the delivery nudge and remove the claim (never
        re-mint a completed flow); a flow whose attempt already carries a
        durable terminal outcome ⇒ complete the deletion that outcome
        authorized (never re-mint a flow whose END is on record); otherwise
        restore it to ``pending/`` by publish-once link, keeping its mint
        mtime, so the crash window between claim and result write does not
        silently eat a flow.

        A periodic pass additionally skips in-flight hashes and claims younger
        than :data:`RESTORE_GRACE_S`; a boot pass has no live handlers by
        construction and skips neither.
        """
        report = RecoveryReport()
        with self._lock:
            if self._closed:
                return report
            for plugin in self._plugin_dirs():
                try:
                    pfd = self._plugin_fd(plugin)
                except (OSError, ValueError):
                    continue
                try:
                    self._recover_plugin(plugin, pfd, now, boot, report)
                finally:
                    os.close(pfd)
        return report

    def _recover_plugin(self, plugin: str, pfd: int, now: float, boot: bool,
                        report: RecoveryReport) -> None:
        try:
            claims = _open_dir(CLAIMS_DIR, pfd)
        except OSError:
            return
        try:
            try:
                pend = _open_dir(PENDING_DIR, pfd)
            except OSError:
                return
            try:
                results = _open_dir(RESULTS_DIR, pfd)
            except OSError:
                os.close(pend)               # no leak on the second open
                return
            try:
                names = _listdir_quiet(claims)
                self._recover_temps(plugin, claims, names, boot, report)
                for name in names:
                    if name.startswith(TEMP_PREFIX):
                        continue             # handled in the temp phase
                    self._recover_claim(plugin, name, claims, pend, results,
                                        now, boot, report)
            finally:
                os.close(pend)
                os.close(results)
        finally:
            os.close(claims)

    def _recover_temps(self, plugin: str, claims: int, names: list[str],
                       boot: bool, report: RecoveryReport) -> None:
        for name in names:
            if not name.startswith(TEMP_PREFIX):
                continue
            h = name[len(TEMP_PREFIX):]
            if not boot and _is_hash(h) \
                    and in_flight_key(plugin, h) in self._in_flight:
                continue                     # a live writer owns this temp
            if _unlink_quiet(name, claims):
                _fsync(claims, CLAIMS_DIR)
                report.temps_cleared += 1

    def _recover_claim(self, plugin: str, name: str, claims: int, pend: int,
                       results: int, now: float, boot: bool,
                       report: RecoveryReport) -> None:
        if not _is_hash(name):
            st = _lstat_quiet(name, claims)
            _remove_entry(name, claims, st)
            _fsync(claims, CLAIMS_DIR)
            report.anomalies.append(f"{plugin}: unparseable claim entry")
            return
        if not boot and in_flight_key(plugin, name) in self._in_flight:
            return
        st = _lstat_quiet(name, claims)
        if st is None:
            return
        # A published result outranks EVERY age gate and every type anomaly on
        # the claim side: the flow completed, only its delivery nudge may be
        # missing, and the result's own TTL is the thing that bounds it. The
        # claim's mtime is the MINT time, so a slow authorization (minted 31
        # minutes ago, result written seconds before the crash) would otherwise
        # be "stale" here and its nudge silently dropped while the credential
        # sits live in results/.
        res = _lstat_quiet(f"{name}.json", results)
        if res is not None and stat.S_ISREG(res.st_mode):
            # CUSTODY TRANSFER, not a retirement: the flow continues in
            # results/ under its own TTL, so no outcome is recorded here.
            report.nudges.append((plugin, name))
            _remove_entry(name, claims, st)
            _fsync(claims, CLAIMS_DIR)
            return
        dirs = _ArtifactDirs(pend=pend, results=results, claims=claims)
        if not stat.S_ISREG(st.st_mode):
            if not self._write_ahead(plugin, name, "expired", dirs=dirs,
                                     now=now):
                report.anomalies.append(
                    f"{plugin}: outcome not durable — claim kept")
                return
            _remove_entry(name, claims, st)
            _fsync(claims, CLAIMS_DIR)
            report.anomalies.append(f"{plugin}: non-regular claim entry")
            return
        if st.st_mtime > now + SKEW_S or now - st.st_mtime > PENDING_TTL_S:
            if not self._write_ahead(plugin, name, "expired", dirs=dirs,
                                     now=now):
                report.anomalies.append(
                    f"{plugin}: outcome not durable — claim kept")
                return
            _unlink_quiet(name, claims)
            _fsync(claims, CLAIMS_DIR)
            report.dropped.append((plugin, name))
            return
        if not boot and now - st.st_mtime < RESTORE_GRACE_S:
            return
        # A DURABLE terminal record outranks restoration. The write-ahead
        # rule (§5) says the record precedes the deletion it authorizes, so
        # a valid `done` attempt beside a surviving claim means casa already
        # recorded this flow's end and only the deletion was interrupted —
        # typically `publish_failed` written strictly, then a crash before
        # the handler's discard. Restoring would re-mint a state whose end
        # is on record, reopening a terminal attempt and handing a consumed
        # state a second life (INV-CB-002). Complete the deletion instead.
        rec = callback_attempts.validate_attempt(
            self.read_attempt(plugin, name).payload, expect_hash=name)
        if rec is not None and rec["status"] == "done":
            _unlink_quiet(name, claims)
            _fsync(claims, CLAIMS_DIR)
            report.completed_terminal.append((plugin, name))
            return
        try:
            restored = _link_once(name, claims, f"{name}.json", pend)
        except OSError as exc:
            # ``exc`` itself is not logged: an OSError from ``linkat`` carries
            # the operand names, i.e. the state hash (INV-CB-006 hygiene).
            logger.warning("callback-spool: claim restore failed (errno %s)",
                           exc.errno)
            return
        if restored:
            _fsync(pend, PENDING_DIR)
            report.restored.append((plugin, name))
        # EEXIST: a pending twin already exists (a crash between the claim's
        # link and the source unlink) — the claim is simply superseded.
        _unlink_quiet(name, claims)
        _fsync(claims, CLAIMS_DIR)

    # -- sweep ----------------------------------------------------------

    def sweep(self, *, now: float) -> SweepReport:
        """TTL + future-mtime deletion across the name classes, then the
        per-plugin caps. Bare ``.claims/<hash>`` entries are deliberately NOT
        swept: they belong to the recovery pass, and deleting a young claim
        here would silently eat an in-flight authorization.

        Every deletion that RETIRES A FLOW — an expired or capped pending, an
        expired, capped or held result, a hash-named anomaly — first records
        the flow's terminal outcome on its attempt file, durably (INV-CB-007,
        spec §5). A record that will not go durable skips its deletion this
        pass (``skipped_undurable``), so a crash can never destroy the
        credential-bearing artifact and the only record of why together.
        Residue that names no flow (``.part``, ``.tmp-*``, staged-replace
        temps, a malformed ``.collect-`` name, unattributable names) is
        recorded nowhere — there is no flow to record."""
        report = SweepReport()
        with self._lock:
            if self._closed:
                return report
            for plugin in self._plugin_dirs():
                try:
                    pfd = self._plugin_fd(plugin)
                except (OSError, ValueError):
                    continue
                try:
                    self._sweep_plugin(plugin, pfd, now, report)
                finally:
                    os.close(pfd)
            self._sweep_index(now, report)
        return report

    def _sweep_index(self, now: float, report: SweepReport) -> None:
        """`.index/` holds reconcile-owned entries plus, after a crash, this
        module's staging residue — only the latter is sweep-owned."""
        try:
            ifd = _open_dir(INDEX_DIR, self._root_fd)
        except OSError:
            return
        try:
            if self._sweep_replace_temps(ifd, now, report):
                _fsync(ifd, INDEX_DIR)
        finally:
            os.close(ifd)

    def _sweep_plugin(self, plugin: str, pfd: int, now: float,
                      report: SweepReport) -> None:
        # The plugin dir's own level: ready.json staging residue only. Nothing
        # else there is sweep-owned (ready.json is reconcile's, the four
        # subdirs are handled below).
        if self._sweep_replace_temps(pfd, now, report):
            _fsync(pfd, plugin)
        # All four FDs are opened up front: a write-ahead outcome is derived
        # from whatever the OTHER directories still hold, so no handler can
        # work from its own directory alone. A directory that will not open is
        # None rather than fatal — the remaining ones are still swept.
        fds: dict[str, "int | None"] = {}
        for sub in (PENDING_DIR, RESULTS_DIR, CLAIMS_DIR, ATTEMPTS_DIR):
            try:
                fds[sub] = _open_dir(sub, pfd)
            except OSError:
                fds[sub] = None
        try:
            dirs = _ArtifactDirs(pend=fds[PENDING_DIR],
                                 results=fds[RESULTS_DIR],
                                 claims=fds[CLAIMS_DIR])
            # Read ONCE per pass: the cap ladder (spec §9) evicts entries with
            # no open attempt before entries that still have one.
            open_attempts = {h for h, rec in self.list_attempts(plugin)
                             if rec["status"] != "done"}
            for sub, handler in ((PENDING_DIR, self._sweep_pending),
                                 (RESULTS_DIR, self._sweep_results)):
                fd = fds[sub]
                if fd is None:
                    continue
                handler(plugin, fd, now, report, dirs, open_attempts)
                _fsync(fd, sub)
            for sub, simple in ((CLAIMS_DIR, self._sweep_claims),
                                (ATTEMPTS_DIR, self._sweep_attempts)):
                fd = fds[sub]
                if fd is None:
                    continue
                simple(plugin, fd, now, report)
                _fsync(fd, sub)
        finally:
            for fd in fds.values():
                if fd is not None:
                    os.close(fd)

    def _sweep_replace_temps(self, fd: int, now: float,
                             report: SweepReport) -> bool:
        """Age-sweep `_replace_json` staging residue in *fd*. Returns True if
        anything was removed (so the caller fsyncs)."""
        removed = False
        for name in _listdir_quiet(fd):
            if not _is_replace_temp(name):
                continue
            st = _lstat_quiet(name, fd)
            if st is None:
                continue
            if not stat.S_ISREG(st.st_mode) or self._expired(st, now, TEMP_TTL_S):
                if _remove_entry(name, fd, st):
                    report.deleted_temps += 1
                    removed = True
        return removed

    def _expired(self, st, now: float, ttl: float) -> bool:
        # Future beyond the skew allowance is fail-closed: a forward clock
        # jump must not park entries that regain validity when it returns.
        return st.st_mtime > now + SKEW_S or now - st.st_mtime > ttl

    def _sweep_pending(self, plugin: str, fd: int, now: float,
                       report: SweepReport, dirs: _ArtifactDirs,
                       open_attempts: set[str]) -> None:
        live: list[tuple[float, str, "str | None"]] = []
        for name in _listdir_quiet(fd):
            st = _lstat_quiet(name, fd)
            if st is None:
                continue
            is_part = name.endswith(PART_SUFFIX)
            h = _hash_of_pending(name[:-len(PART_SUFFIX)] if is_part else name)
            # A `.part` names no minted state (the mint publishes by link, so
            # the final name is what a flow ever was): it is residue, and
            # residue is recorded NOWHERE — neither here nor at the cap.
            flow = None if is_part else h
            if h is None or not stat.S_ISREG(st.st_mode):
                if flow is not None and not self._write_ahead(
                        plugin, flow, "expired", dirs=dirs, now=now):
                    report.skipped_undurable += 1
                    continue
                _remove_entry(name, fd, st)
                report.deleted_anomalous += 1
                report.anomalies.append(f"{plugin}/pending: {name!r}")
                continue
            if self._expired(st, now, TEMP_TTL_S if is_part else PENDING_TTL_S):
                if flow is not None and not self._write_ahead(
                        plugin, flow, "expired", dirs=dirs, now=now):
                    report.skipped_undurable += 1
                    continue
                if _unlink_quiet(name, fd):
                    if is_part:
                        report.deleted_temps += 1
                    else:
                        report.deleted_pending += 1
                continue
            # `.part` files count toward the cap: they occupy the same
            # directory and a consumer looping on a failing publish would
            # otherwise fill /data with staging files the cap never sees.
            live.append((st.st_mtime, name, flow))
        self._apply_cap(plugin, PENDING_DIR, fd, live, MAX_PENDING, report,
                        dirs=dirs, now=now, open_attempts=open_attempts)

    def _sweep_results(self, plugin: str, fd: int, now: float,
                       report: SweepReport, dirs: _ArtifactDirs,
                       open_attempts: set[str]) -> None:
        live: list[tuple[float, str, "str | None"]] = []
        held: list[tuple[float, str, str]] = []
        for name in _listdir_quiet(fd):
            st = _lstat_quiet(name, fd)
            if st is None:
                continue
            flow = _hash_of_collect(name)
            is_collect = flow is not None
            if not is_collect:
                flow = _hash_of_pending(name)
            # A ``.collect-`` name that does not PARSE names no flow — the
            # single grammar decides, here as in enumeration. It is residue,
            # exactly like a `.part` in pending/: aged on TEMP_TTL_S,
            # recorded NOWHERE, and still counted toward MAX_RESULTS. Reading
            # the bare prefix as "a collect entry" would have excluded it
            # from MAX_RESULTS while the grammar kept it out of MAX_COLLECT,
            # so thousands of `.collect-junk` names evaded BOTH bounds.
            residue = flow is None and name.startswith(COLLECT_PREFIX)
            if not stat.S_ISREG(st.st_mode) or (flow is None and not residue):
                if flow is not None and not self._write_ahead(
                        plugin, flow, "expired", dirs=dirs, now=now,
                        claimed=True if is_collect else None):
                    report.skipped_undurable += 1
                    continue
                _remove_entry(name, fd, st)
                if residue:
                    report.deleted_temps += 1
                else:
                    report.deleted_anomalous += 1
                    report.anomalies.append(f"{plugin}/results: {name!r}")
                continue
            if self._expired(st, now, TEMP_TTL_S if residue else RESULT_TTL_S):
                # A held entry aged out is `claimed`: the consumer renamed it
                # but never acked, so casa cannot say whether it was read. A
                # base-named result asserts nothing about `claimed` (None) —
                # the record's own flag is knowledge, never to be downgraded.
                if flow is not None and not self._write_ahead(
                        plugin, flow, "expired_unread", dirs=dirs, now=now,
                        claimed=True if is_collect else None):
                    report.skipped_undurable += 1
                    continue
                if _unlink_quiet(name, fd):
                    if residue:
                        report.deleted_temps += 1
                    elif is_collect:
                        report.deleted_collect += 1
                    else:
                        report.deleted_results += 1
                elif flow is not None:
                    self._repair_after_lost_deletion(plugin, flow, dirs=dirs,
                                                     now=now)
                continue
            if is_collect:
                held.append((st.st_mtime, name, flow))
            else:
                live.append((st.st_mtime, name, flow))   # flow None: residue
        # Consumer-held `.collect-*` entries are excluded from MAX_RESULTS
        # (they are already claimed work) and bounded by their OWN cap, so a
        # rename-happy consumer cannot hold unbounded credential inodes.
        self._apply_cap(plugin, RESULTS_DIR, fd, live, MAX_RESULTS, report,
                        dirs=dirs, now=now, open_attempts=open_attempts)
        self._apply_collect_cap(plugin, fd, held, report, dirs=dirs, now=now)

    def _sweep_attempts(self, plugin: str, fd: int, now: float,
                        report: SweepReport) -> None:
        """Grammar-aware hygiene for ``attempts/``. Only TWO names belong
        there — ``<64hex>.json`` (casa's record) and ``.ack-<64hex>`` (the
        consumer's receipt) — and both have their own lifecycle; everything
        else is residue: this module's own staged-replace temps after a crash,
        or something nothing in the protocol writes. Residue ages out on
        ``TEMP_TTL_S`` and records NO outcome — an unattributable name names
        no flow."""
        for name in _listdir_quiet(fd):
            if not _is_replace_temp(name) and (
                    _hash_of_pending(name) is not None
                    or (name.startswith(ACK_PREFIX)
                        and _is_hash(name[len(ACK_PREFIX):]))):
                continue                     # a live record or a receipt
            st = _lstat_quiet(name, fd)
            if st is None:
                continue
            if not stat.S_ISREG(st.st_mode) or self._expired(st, now,
                                                             TEMP_TTL_S):
                if _remove_entry(name, fd, st):
                    report.deleted_temps += 1

    def _sweep_claims(self, plugin: str, fd: int, now: float,
                      report: SweepReport) -> None:
        for name in _listdir_quiet(fd):
            if not name.startswith(TEMP_PREFIX):
                continue                     # bare claims belong to recovery
            h = name[len(TEMP_PREFIX):]
            if _is_hash(h) and in_flight_key(plugin, h) in self._in_flight:
                continue                     # a live writer owns this temp
            st = _lstat_quiet(name, fd)
            if st is None:
                continue
            if not stat.S_ISREG(st.st_mode) or self._expired(st, now, TEMP_TTL_S):
                if _remove_entry(name, fd, st):
                    report.deleted_temps += 1

    def _apply_cap(self, plugin: str, sub: str, fd: int,
                   live: list[tuple[float, str, "str | None"]], cap: int,
                   report: SweepReport, *, dirs: _ArtifactDirs, now: float,
                   open_attempts: set[str]) -> None:
        """Enforce a per-directory cap, victims chosen by the spec §9 ladder:

        0. residue — a ``.part`` or a malformed ``.collect-`` name (``flow``
           is None): it names no minted state, so destroying it retires
           nothing and is recorded nowhere;
        1. entries whose flow has no OPEN attempt (a final or attempt-less
           flow);
        2. entries whose flow still has an open attempt — evicted last.

        mtime (then name) ranks only WITHIN a rank: ranking residue and a
        real flow together by age alone lets a newer ``.part`` survive while
        an older genuine flow is destroyed and recorded ``evicted``. Cap
        pressure is pathological by definition, and what it destroys should
        be what casa knows least about. Every hash-named victim is recorded
        ``evicted`` write-ahead; a victim whose record will not go durable is
        skipped this pass (the cap converges on the next one)."""
        if len(live) <= cap:
            return

        def _rank(item) -> int:
            flow = item[2]
            if flow is None:
                return 0
            return 2 if flow in open_attempts else 1

        live.sort(key=lambda item: (_rank(item), item[0], item[1]))
        for _mtime, name, flow in live[:len(live) - cap]:
            if flow is not None and not self._write_ahead(
                    plugin, flow, "evicted", dirs=dirs, now=now):
                report.skipped_undurable += 1
                continue
            if _unlink_quiet(name, fd):
                report.deleted_capped += 1
            elif flow is not None:
                self._repair_after_lost_deletion(plugin, flow, dirs=dirs,
                                                 now=now)
        report.capped.append(f"{plugin}/{sub}")
        logger.warning("callback-spool: %s/%s exceeded %d entries — "
                       "residue-then-attempt-less-first deletion applied",
                       plugin, sub, cap)

    def _apply_collect_cap(self, plugin: str, fd: int,
                           held: list[tuple[float, str, str]],
                           report: SweepReport, *, dirs: _ArtifactDirs,
                           now: float) -> None:
        """Bound the consumer-held ``.collect-*`` inodes (spec §9). Oldest
        first — every hold is equally "claimed, unconfirmed", so age is the
        only ranking — and each victim is recorded ``expired_unread,
        claimed`` write-ahead, exactly as its TTL age-out would."""
        if len(held) <= MAX_COLLECT:
            return
        held.sort()
        for _mtime, name, flow in held[:len(held) - MAX_COLLECT]:
            if not self._write_ahead(plugin, flow, "expired_unread",
                                     dirs=dirs, now=now, claimed=True):
                report.skipped_undurable += 1
                continue
            if _unlink_quiet(name, fd):
                report.deleted_collect_capped += 1
        report.capped.append(f"{plugin}/{RESULTS_DIR}/{COLLECT_PREFIX}*")
        logger.warning("callback-spool: %s held %d+ collected results — "
                       "oldest-first deletion applied", plugin, MAX_COLLECT)

    # -- removal records (spec §10 — the INV-CB-007 removal exception) -----

    def _removals_fd(self, *, create: bool) -> int:
        """The ``.removals/`` store FD, created on demand — a RESERVED
        dot-root entry, so it is excluded from plugin enumeration and
        therefore from sweep, recovery and orphan GC alike (a record must
        outlive the plugin dir whose abort it records)."""
        if create and self._mkdir(REMOVALS_DIR, self._root_fd):
            # The directory entry itself must be durable before an entry
            # inside it is (see _index_fd); the record write's own strict
            # root fsync is what finally proves it.
            _fsync(self._root_fd, str(self.root))
        fd = _open_dir(REMOVALS_DIR, self._root_fd)
        if create:
            self._chmod_dir(fd, REMOVALS_DIR)
        return fd

    @staticmethod
    def _listdir_strict(fd: int) -> "list[str] | None":
        """Every entry name under an ALREADY-OPEN directory FD, or ``None``
        when the listing could not be PROVED — the tri-state counterpart of
        :func:`_listdir_quiet`, which maps every failure to ``[]``.

        Reading a faulting listing as "nothing here" is fail-OPEN for the
        callers that cannot take their answer back: a purge, and the
        quiescence scan that licenses one."""
        try:
            return os.listdir(fd)
        except OSError:
            return None

    @staticmethod
    def _names_strict(sub: str, pfd: int) -> "list[str] | None":
        """Every entry name in ``<plugin>/<sub>``, or ``None`` when the
        listing could not be PROVED.

        The tri-state counterpart of ``_open_dir`` + :func:`_listdir_quiet`,
        and the reason the removal inventory is not built on the latter: a
        genuinely ABSENT directory (ENOENT) is provably empty (``[]``), while
        EVERY other failure — EACCES, EIO, an ``opendir`` that faults, a
        listing that faults mid-read — leaves the contents UNKNOWN. Reading
        that as "nothing here" is fail-OPEN for the one caller that cannot
        take it back: a purge."""
        try:
            fd = _open_dir(sub, pfd)
        except FileNotFoundError:
            return []
        except OSError:
            return None
        try:
            return CallbackSpool._listdir_strict(fd)
        finally:
            os.close(fd)

    def _artifact_inventory(self, pfd: int) -> "set[str] | None":
        """Hashes named by the plugin's credential-bearing artifacts —
        ``pending/`` ∪ ``.claims/`` ∪ ``results/`` (published and
        consumer-held alike) — or ``None`` when any of the three listings is
        unprovable. By NAME only: casa never opens a held file.

        Deliberately NOT best-effort, unlike :meth:`_flow_artifacts_live`:
        there, a lost directory costs a witness and the next pass re-examines
        the record; here the answer licenses a purge, so an unprovable
        directory must defer it (Sol r7)."""
        out: set[str] = set()
        for sub in (PENDING_DIR, CLAIMS_DIR, RESULTS_DIR):
            names = self._names_strict(sub, pfd)
            if names is None:
                return None
            for name in names:
                if sub == CLAIMS_DIR:
                    if _is_hash(name):
                        out.add(name)      # a temp names no minted state
                    continue
                h = _hash_of_pending(name)
                if h is None and sub == RESULTS_DIR:
                    h = _hash_of_collect(name)
                if h is not None:
                    out.add(h)
        return out

    def _attempt_inventory(self, pfd: int) -> "tuple[set[str], set[str]] | None":
        """``(recorded, acked)`` for the plugin's ``attempts/`` dir — hashes
        that HAVE an attempt file, and hashes whose receipt token is present —
        or ``None`` when the listing is unprovable.

        Membership is by NAME, not by whether the record parses: a
        ``<h>.json`` that will not validate (scribbled, truncated, or simply
        unreadable this instant) is still casa's evidence that the flow
        existed and nobody acked it. Counting only what parses would let a
        single failed read erase a flow from the notice a removal owes its
        operator — the same fail-open shape as an unprovable listing, one
        entry down."""
        names = self._names_strict(ATTEMPTS_DIR, pfd)
        if names is None:
            return None
        acked = {name[len(ACK_PREFIX):] for name in names
                 if name.startswith(ACK_PREFIX)
                 and _is_hash(name[len(ACK_PREFIX):])}
        recorded = {h for h in (_hash_of_pending(n) for n in names)
                    if h is not None}
        return recorded, acked

    def _plugin_fd_strict(self, plugin: str) -> "int | None | object":
        """The plugin dir FD for an inventory scan: an FD, ``None`` when the
        directory is genuinely ABSENT (there is nothing to settle), or
        :data:`_SCAN_ERROR` when it could not be opened for any other reason
        (UNKNOWN — the caller defers)."""
        if self._closed or not _safe_component(plugin):
            return _SCAN_ERROR
        try:
            return self._plugin_fd(plugin)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return _SCAN_ERROR

    def _unacked_attempts(self, plugin: str) -> "set[str] | None":
        """Hashes with an attempt file and no ``.ack-<h>`` receipt — open OR
        terminal (Sol r3): a terminal attempt nobody acked is an outcome the
        consumer has not read yet, which is precisely what a removal is about
        to destroy. ``None`` when the inventory could not be proved."""
        with self._lock:
            pfd = self._plugin_fd_strict(plugin)
            if pfd is None:
                return set()
            if pfd is _SCAN_ERROR:
                return None
            try:
                inventory = self._attempt_inventory(pfd)
            finally:
                os.close(pfd)
            if inventory is None:
                return None
            recorded, acked = inventory
            return recorded - acked

    def _unsettled_hashes(self, plugin: str) -> "set[str] | None":
        """Every flow of *plugin* with unfinished business — the union spec
        §10 counts before a purge: (attempt records ∪ pendings ∪ claims ∪
        results ∪ consumer-held collects) MINUS the receipt tokens.

        The artifacts are unioned in unconditionally (not only when they
        carry an attempt file): a flow casa can still see is a flow the purge
        aborts, whether or not the ledger has caught up with it. The ack is
        the one settling verb, and it settles the FLOW rather than merely its
        ledger entry, so the tokens are subtracted from the COMPLETE union
        (Sol r7): a premature ack whose result or hold is still on disk
        (teardown has not run yet) would otherwise be counted, and the
        operator told a flow was aborted that its consumer has already
        absorbed (INV-CB-007 arm (a)).

        ``None`` when any directory or listing could not be proved: a purge
        is irreversible, so an unprovable inventory DEFERS it — it is never
        read as "nothing here"."""
        with self._lock:
            pfd = self._plugin_fd_strict(plugin)
            if pfd is None:
                return set()
            if pfd is _SCAN_ERROR:
                return None
            try:
                attempts = self._attempt_inventory(pfd)
                artifacts = self._artifact_inventory(pfd)
            finally:
                os.close(pfd)
            if attempts is None or artifacts is None:
                return None
            recorded, acked = attempts
            return (recorded | artifacts) - acked

    def _write_removal_record(self, plugin: str, count: int, reason: str, *,
                              now: float) -> bool:
        """Write ``.removals/<plugin>-<uuid4hex>.json`` with STRICT
        durability — the abort's record, and the licence for the purge that
        follows it (plan amendment 11).

        The per-flow ledger dies with the plugin dir, so this record (and the
        at-least-once operator note the worker drives from it) is the whole
        of what INV-CB-007 promises across a removal. Returns True only when
        the record is proven durable; never raises, and never logs anything
        but the plugin name (INV-CB-006)."""
        rec = {"v": REMOVAL_SCHEMA_VERSION, "plugin": plugin,
               "count": int(count), "reason": reason, "ts": float(now),
               "noted": False, "noted_ts": None}
        try:
            data = canonical_marker_bytes(rec)
        except (TypeError, ValueError):        # pragma: no cover — all plain
            logger.warning("callback-spool: removal record is not "
                           "serializable (%r)", plugin)
            return False
        try:
            rfd = self._removals_fd(create=True)
        except OSError as exc:
            logger.warning("callback-spool: removals store unavailable "
                           "(errno %s)", exc.errno)
            return False
        try:
            return _strict_replace_at(
                f"{plugin}-{uuid.uuid4().hex}.json", rfd, data,
                parent_fd=self._root_fd, what=REMOVALS_DIR,
                parent_what=str(self.root), role="removal record")
        finally:
            os.close(rfd)

    def list_removal_records(self) -> list[tuple[str, dict]]:
        """``(filename, record)`` for every schema-valid removal record,
        sorted by filename — the worker's note worklist.

        A malformed entry is RETIRED here rather than returned: it can drive
        no note (nothing in it is trustworthy), and leaving it would make the
        worker re-read the same garbage on every pass. Staging residue and
        foreign names are skipped, not retired — :meth:`prune_removal_records`
        ages residue out on the ``TEMP_TTL_S`` clock the rest of the spool
        uses. ``[]`` on a closed spool or a store that does not exist."""
        with self._lock:
            if self._closed:
                return []
            try:
                rfd = self._removals_fd(create=False)
            except OSError:
                return []
            try:
                out: list[tuple[str, dict]] = []
                retired = False
                for name in sorted(_listdir_quiet(rfd)):
                    if not _is_removal_name(name):
                        continue
                    marker = _read_marker_at(name, rfd)
                    if marker.state is MarkerState.ABSENT:
                        continue             # vanished mid-scan
                    rec = (_validate_removal(marker.payload)
                           if marker.state is MarkerState.PRESENT else None)
                    if rec is None:
                        retired |= _retire_marker_entry(name, rfd)
                        logger.warning("callback-spool: unreadable removal "
                                       "record retired")
                        continue
                    out.append((name, rec))
                if retired:
                    _fsync(rfd, REMOVALS_DIR)
                return out
            finally:
                os.close(rfd)

    def mark_removal_noted(self, filename: str, *, now: float) -> bool:
        """Record that the operator has been told about this removal —
        ``noted=True, noted_ts=now``, written STRICTLY.

        The removal note is deliberately notify-THEN-mark (spec §10): the
        note is dispatched first and this mark follows, so a crash in the
        window costs one duplicate DM rather than a silently lost notice.
        False on any failure — an unreadable/invalid record, a store that
        will not open, a write that will not go durable — and the caller
        simply retries on its next pass."""
        if not _is_removal_name(filename):
            return False
        with self._lock:
            if self._closed:
                return False
            try:
                rfd = self._removals_fd(create=False)
            except OSError:
                return False
            try:
                marker = _read_marker_at(filename, rfd)
                rec = (_validate_removal(marker.payload)
                       if marker.state is MarkerState.PRESENT else None)
                if rec is None:
                    return False
                rec = dict(rec, noted=True, noted_ts=float(now))
                try:
                    data = canonical_marker_bytes(rec)
                except (TypeError, ValueError):   # pragma: no cover
                    return False
                return _strict_replace_at(
                    filename, rfd, data, parent_fd=self._root_fd,
                    what=REMOVALS_DIR, parent_what=str(self.root),
                    role="removal record")
            finally:
                os.close(rfd)

    def prune_removal_records(self, *, now: float) -> int:
        """Retire spent removal records; returns how many were removed.

        Two clocks, NOTED records only (#532, design r2): a noted record
        goes at ``noted_ts + REMOVAL_RECORD_PRUNE_S`` — the operator has
        had the notice and a week to ask about it — or at the
        ``ts + REMOVAL_RECORD_MAX_AGE_S`` hard bound. An UN-noted record
        is never age-pruned: it is the only evidence an operator notice is
        still owed, and the honest notify seam means "un-noted" now
        reflects real non-delivery (a 30-day outage window used to delete
        the note at boot seconds before the channel came up). Un-noted
        accumulation is bounded by the plugin-removal rate, an
        operator-action rate. A record created weeks ago but noted
        yesterday is inside its retention window and stays.

        Staging residue (a crash between stage and rename) ages out here on
        the shared ``TEMP_TTL_S`` clock; it is not counted, being nobody's
        record. Invalid records are :meth:`list_removal_records`'s to
        retire."""
        with self._lock:
            if self._closed:
                return 0
            try:
                rfd = self._removals_fd(create=False)
            except OSError:
                return 0
            try:
                pruned = 0
                touched = False
                for name in sorted(_listdir_quiet(rfd)):
                    if _is_replace_temp(name):
                        st = _lstat_quiet(name, rfd)
                        if st is not None and now - st.st_mtime > TEMP_TTL_S:
                            touched |= _unlink_quiet(name, rfd)
                        continue
                    if not _is_removal_name(name):
                        continue
                    marker = _read_marker_at(name, rfd)
                    rec = (_validate_removal(marker.payload)
                           if marker.state is MarkerState.PRESENT else None)
                    if rec is None:
                        continue             # not this method's to judge
                    if not rec["noted"]:
                        continue             # un-noted = a notice still owed
                    noted_age = now - rec["noted_ts"]
                    spent = noted_age > REMOVAL_RECORD_PRUNE_S
                    aged = now - rec["ts"] > REMOVAL_RECORD_MAX_AGE_S
                    if not spent and not aged:
                        continue
                    if _unlink_quiet(name, rfd):
                        pruned += 1
                        touched = True
                if touched:
                    _fsync(rfd, REMOVALS_DIR)
                return pruned
            finally:
                os.close(rfd)

    # -- gated orphan-dir GC ----------------------------------------------

    def gc_orphan_dirs(self, *, registry_valid: bool,
                       member_plugins: set[str], now: float) -> list[str]:
        """Remove spool dirs of plugins that are no longer installed.

        **Gated, not fail-destructive**: with anything other than a valid
        registry load this is a NO-OP, because a membership set derived from a
        failed load would vaporize every plugin's in-flight authorizations.
        Membership keys on registry ENTRIES (not resolution success — an
        artifact checksum hiccup must not delete a live spool), and a dir is
        removed only when it has been quiescent for :data:`QUIESCENCE_S` AND
        holds no entry younger than the pending TTL.

        A quiescent dir cannot hold a live *flow* (every credential TTL is
        long expired) but it CAN hold terminal-unacked attempts still inside
        their 7-day retention — casa crashed after the registry removal but
        before :meth:`remove_plugin`. Those get the same durable removal
        record (reason ``orphan_gc``) BEFORE the purge, so the retention
        promise degrades to the §10 removal exception instead of being
        silently violated; a record that will not go durable SKIPS that dir
        this pass (it stays quiescent, so the next pass retries).

        Every scan behind that decision is PROVING, never peeking: neither
        the quiescence walk (:meth:`_newest_mtime`) nor the attempt inventory
        (:meth:`_unacked_attempts`) may read a directory it could not open or
        list as "nothing here" — an unprovable dir SKIPS this pass too. Both
        answers license the same irreversible purge, and a faulting disk is
        exactly when a dir looks quiescent and empty while holding live
        credentials and unread outcomes.
        """
        if registry_valid is not True:
            return []
        removed: list[str] = []
        with self._lock:
            if self._closed:
                return []
            for plugin in self._plugin_dirs():
                if plugin in member_plugins:
                    continue
                newest = self._newest_mtime(plugin)
                if newest is None:
                    continue                 # the dir is gone: nothing to GC
                if newest is _SCAN_ERROR:
                    # Quiescence could not be PROVED (a directory or an entry
                    # under it would not be read). An unprovable tree must not
                    # read as an old, empty one — that is the same fail-open
                    # the inventory below refuses, one step earlier and with
                    # the same irreversible consequence. The dir stays as it
                    # is, so the next pass tries again.
                    logger.warning("callback-spool: orphan GC of %r deferred "
                                   "— its spool tree could not be scanned",
                                   plugin)
                    continue
                age = now - newest
                # Both spec conditions, kept independent on purpose: today
                # QUIESCENCE_S subsumes the pending-TTL floor, but a future
                # TTL change must not be able to invert that relationship.
                if age < QUIESCENCE_S or age < PENDING_TTL_S:
                    continue
                unacked = self._unacked_attempts(plugin)
                if unacked is None:
                    # The inventory could not be PROVED (a listing faulted):
                    # an empty answer here would purge a dir that may hold
                    # unacked outcomes, with no record. The dir stays
                    # quiescent, so the next pass tries again.
                    logger.warning("callback-spool: orphan GC of %r deferred "
                                   "— its attempt inventory is unprovable",
                                   plugin)
                    continue
                if unacked and not self._write_removal_record(
                        plugin, len(unacked), "orphan_gc", now=now):
                    # No record, no purge (amendment 11): the dir is still
                    # quiescent, so the next pass tries again.
                    logger.warning("callback-spool: orphan GC of %r deferred "
                                   "— removal record not durable", plugin)
                    continue
                try:
                    shutil.rmtree(plugin, dir_fd=self._root_fd)
                except OSError as exc:
                    # errno only: an OSError from the tree walk carries the
                    # ENTRY it failed on, and under results/ or attempts/
                    # that filename IS a state hash (INV-CB-006).
                    logger.warning("callback-spool: orphan GC of %r failed "
                                   "(errno %s)", plugin, exc.errno)
                    continue
                _fsync(self._root_fd, str(self.root))
                removed.append(plugin)
                logger.info("callback-spool: removed orphan spool dir %r", plugin)
        return removed

    def remove_plugin(self, plugin: str) -> bool:
        """Recursively delete a plugin's ENTIRE spool dir (removal lifecycle).
        The explicit, immediate counterpart of the age-gated
        :meth:`gc_orphan_dirs`: when the operator removes a plugin, its
        in-flight authorizations are gone with it, so there is nothing to
        preserve and no quiescence window to honour.

        Guarded exactly like every dir op — an unsafe / dotted name raises
        ``ValueError`` (never resolves against the CWD), a closed spool is a
        no-op — and the root is fsynced afterwards so the unlink is durable.
        ``ready.json`` lives INSIDE the plugin dir, so this retires it too;
        the ``.index`` discovery entry keyed by the plugin's ARTIFACT PATH
        (not derivable from the plugin name) is retired separately by the
        reconcile's durable marker pass. Returns True when a dir was removed.

        **Abort-with-notice, and never without it (spec §10, amendment 11).**
        The purge destroys the per-flow ledger with the plugin, so before it
        runs the §10 union of unsettled flows is counted and — when non-zero
        — recorded durably under ``.removals/``. A record that will not go
        durable SKIPS the purge and returns False: a purge with no record is
        the one outcome INV-CB-007's removal exception cannot absorb. So does
        an inventory that could not be PROVED — a faulting listing must never
        read as a zero union, which would purge exactly the credentials and
        outcomes the record exists to account for. The caller treats both as
        best-effort (the plugin is already unrouted) and the orphan GC
        converges on the dir later. A zero-union removal has nothing to tell
        anyone and purges directly.

        Documented residual (spec §10): the union scan is check-then-act
        against a same-uid FD holder, which is outside this facility's threat
        model — a consumer holding a pre-removal directory FD can mint after
        the scan, into a plugin already unrouted, so only that flow's abort
        notice can be lost.
        """
        if not _safe_component(plugin) or plugin.startswith("."):
            raise ValueError(f"unsafe plugin spool name {plugin!r}")
        with self._lock:
            if self._closed:
                return False
            unsettled = self._unsettled_hashes(plugin)
            if unsettled is None:
                logger.warning("callback-spool: purge of %r skipped — its "
                               "unsettled inventory is unprovable", plugin)
                return False
            if unsettled and not self._write_removal_record(
                    plugin, len(unsettled), "remove", now=time.time()):
                logger.warning("callback-spool: purge of %r skipped — the "
                               "removal record would not go durable", plugin)
                return False
            try:
                shutil.rmtree(plugin, dir_fd=self._root_fd)
            except FileNotFoundError:
                return False
            except OSError as exc:
                # errno only (INV-CB-006): the exception text names the entry
                # the tree walk failed on, which under results/ or attempts/
                # is a state hash.
                logger.warning("callback-spool: remove_plugin %r failed "
                               "(errno %s)", plugin, exc.errno)
                return False
            _fsync(self._root_fd, str(self.root))
            logger.info("callback-spool: removed spool dir %r (plugin removed)",
                        plugin)
            return True

    def _newest_mtime(self, plugin: str) -> "float | None | object":
        """Newest mtime anywhere in the plugin's spool tree, including the
        directories themselves (a directory's mtime moves whenever an entry is
        added or removed, which is exactly the quiescence signal).

        Three-valued, for the same reason the inventory scans behind the same
        purge are (``_plugin_fd_strict``, :meth:`_names_strict`): a float when
        the whole tree was walked; ``None`` when the plugin dir is genuinely
        ABSENT (nothing to GC); :data:`_SCAN_ERROR` when ANY part of the walk
        — the dir open, a listing, an entry's metadata — could not be PROVED.
        An unprovable tree must never present as "old and empty": that is the
        quiescence half of the fail-open a faulting inventory is, and it
        licenses the same irreversible purge."""
        pfd = self._plugin_fd_strict(plugin)
        if pfd is None or pfd is _SCAN_ERROR:
            return pfd
        try:
            newest = self._newest_in(pfd, depth=3)
        finally:
            os.close(pfd)
        return _SCAN_ERROR if newest is None else newest

    def _newest_in(self, fd: int, depth: int) -> "float | None":
        """Newest mtime at or under an open directory FD, or ``None`` when any
        step of the walk faulted — propagated up through the recursion, never
        swallowed at the level that met it. Only a genuine ENOENT is benign
        (an entry that vanished mid-scan is one this pass need not age); every
        other failure leaves an entry whose age is UNKNOWN, and an unread
        entry could be the newest one."""
        try:
            newest = os.fstat(fd).st_mtime
        except OSError:                                  # pragma: no cover
            return None
        names = self._listdir_strict(fd)
        if names is None:
            return None
        for name in names:
            st = _marker_lstat(name, fd)
            if st is None:
                continue                     # vanished mid-scan (ENOENT)
            if st is _LSTAT_ERROR:
                return None                  # this entry's age is UNKNOWN
            newest = max(newest, st.st_mtime)
            if depth > 0 and stat.S_ISDIR(st.st_mode):
                try:
                    sub = _open_dir(name, fd)
                except FileNotFoundError:
                    continue                 # vanished mid-scan
                except OSError:
                    return None
                try:
                    deeper = self._newest_in(sub, depth - 1)
                finally:
                    os.close(sub)
                if deeper is None:
                    return None
                newest = max(newest, deeper)
        return newest


# ---------------------------------------------------------------------------
# Module singleton (initialised once at boot by casa_core, like the outbox).
# ---------------------------------------------------------------------------

_SPOOL: "CallbackSpool | None" = None


def init_spool(root: "Path | str | None" = None) -> "CallbackSpool":
    """Create (or replace) the process-wide spool. Boot wiring only — the
    reconciler, the HTTP handler and the sweeper all read :func:`get_spool`."""
    global _SPOOL
    _SPOOL = CallbackSpool(spool_root() if root is None else root)
    logger.info("callback-spool initialised at %s", _SPOOL.root)
    return _SPOOL


def get_spool() -> "CallbackSpool | None":
    """The process-wide spool, or ``None`` before boot wired one (every
    caller degrades explicitly rather than resolving paths against the CWD)."""
    return _SPOOL
