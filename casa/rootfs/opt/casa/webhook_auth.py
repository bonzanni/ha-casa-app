"""Per-trigger webhook auth verification (Release A).

Leaf module — stdlib only (plus ``log_redact`` for secret registration, wired
by callers). Pure verifiers: no IO, no clock access (``now`` is injected so the
handler owns time and tests stay deterministic).

Three modes (spec A1):

* ``hmac_body``       — hex HMAC-SHA256 of the raw body, header
  ``X-Webhook-Signature`` by default. Uses the global ``WEBHOOK_SECRET``.
* ``static_header``   — constant-time compare of a header value against the
  per-trigger secret (ElevenLabs agent tools, n8n).
* ``timestamped_hmac``— ``t=<unix>,v0=<hex>`` where
  ``v0 = HMAC_SHA256(secret, "{t}.{body}")`` (ElevenLabs post-call, Stripe-
  style), gated by a tolerance window.

All comparisons are constant-time on bytes; a non-ASCII or malformed header
yields ``False`` (→ 401 at the handler), never an exception (→ 500). This
mirrors the L4 lesson in the Telegram update handler.
"""
from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Mapping

# Casa-minted secrets: 32 urlsafe bytes → exactly 43 base64url chars.
_CASA_TOKEN_NBYTES = 32
_CASA_TOKEN_LEN = 43
# Provider (opaque) secret bounds (spec A2, Sol r4-4).
_PROVIDER_MAX = 4096
# Orphan staging-file sweep window.
_TMP_SWEEP_SECS = 60

# #620 — the resident mint receipt. Proof that Casa GENERATED the bytes that
# are in a slot right now, which is the fact a retirement would have to stand
# on: `_valid_value` cannot tell a casa token from an operator credential
# (a 43-byte token satisfies the provider rule too), so nothing else in this
# module can answer "may this be destroyed".
#
# FIVE characters, not `.mint.json`: `triggers.v1.json` caps a trigger name at
# 248 bytes against a 255-byte NAME_MAX, reserving exactly 7 for a suffix. A
# 10-byte suffix overflows at 258 and raises ENAMETOOLONG for a schema-legal
# name. (`.rot.json` already overflows at 257 — pre-existing, and its state
# machine has no caller outside tests.)
MINT_RECEIPT_SUFFIX = ".mint"
_RECEIPT_VERSION = 1
_RECEIPT_KEYS = frozenset({"v", "minted_by", "value_sha256"})
# The digest is shape-checked BEFORE it is compared. `hmac.compare_digest`
# raises TypeError on a str holding non-ASCII, and this function is documented
# total — a hand-written receipt would otherwise propagate that out through
# the reload report instead of reading as `unproven`.
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Strict single-instance parse: exactly ``t=<digits>,v0=<lowercase-hex>`` with
# no leading/trailing/interior whitespace and no extra fields. Both fields are
# LENGTH-BOUNDED so an attacker cannot force a huge ``int()`` (a 5000-digit
# timestamp would otherwise raise before auth → 500): a unix timestamp is ~10
# digits, and a SHA-256 hex digest is 64 chars.
_TS_RE = re.compile(r"^t=(\d{1,19}),v0=([0-9a-f]{1,128})$")


def usable_webhook_secret(value: str) -> str:
    """#333 (Terra r1): never use an unresolved ``op://`` reference as an HMAC
    key. A vault path is a predictable, non-secret string — verifying against
    it is fail-open (anything that knows the reference can sign). Returning
    empty means every authenticated request is rejected loudly until
    resolution succeeds or the operator inlines the secret."""
    return "" if value.startswith("op://") else value


def _ct_eq(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


def _header_ascii_bytes(headers: Mapping[str, str], name: str) -> bytes | None:
    """The header value as ASCII bytes, ``None`` if absent, ``b""`` if it holds
    a non-ASCII value (which then guarantees a constant-time mismatch rather
    than raising on ``compare_digest``)."""
    v = headers.get(name)
    if v is None:
        return None
    try:
        return v.encode("ascii")
    except UnicodeEncodeError:
        return b""


def verify(
    mode: str,
    *,
    body: bytes,
    headers: Mapping[str, str],
    secret: bytes,
    header_name: str,
    tolerance_secs: int,
    now: int,
) -> bool:
    """Return whether the request authenticates under ``mode``.

    Fail-closed: an empty ``secret`` never authenticates.
    """
    if not secret:
        return False

    if mode == "hmac_body":
        got = _header_ascii_bytes(headers, header_name)
        if got is None:
            return False
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest().encode("ascii")
        return _ct_eq(got, expected)

    if mode == "static_header":
        got = _header_ascii_bytes(headers, header_name)
        return got is not None and _ct_eq(got, secret)

    if mode == "timestamped_hmac":
        raw = headers.get(header_name)
        if raw is None:
            return False
        try:
            raw.encode("ascii")
        except UnicodeEncodeError:
            return False
        m = _TS_RE.match(raw)
        if not m:
            return False
        t = int(m.group(1))
        if abs(now - t) > tolerance_secs:
            return False
        expected = hmac.new(
            secret, f"{t}.".encode() + body, hashlib.sha256,
        ).hexdigest()
        return _ct_eq(m.group(2).encode("ascii"), expected.encode("ascii"))

    return False


# ---------------------------------------------------------------------------
# Per-trigger secret storage (spec A2) — crash-safe staging, ownership-aware
# validation. All reads/writes are fail-closed: an invalid or unreadable slot
# yields ``None`` and the caller omits the trigger (never an open pass-through).
# ---------------------------------------------------------------------------


def _valid_value(owner: str, raw: bytes) -> bool:
    """Owner-appropriate validation of a stored secret's bytes."""
    if owner == "casa":
        return len(raw) == _CASA_TOKEN_LEN and raw.isascii()
    # provider: opaque, non-empty, bounded, printable ASCII.
    if not raw or len(raw) > _PROVIDER_MAX:
        return False
    return all(0x20 <= b < 0x7F for b in raw)


def _sweep_orphans(secrets_dir: Path) -> None:
    now = time.time()
    for p in secrets_dir.glob(".tmp-*"):
        try:
            if now - p.stat().st_mtime > _TMP_SWEEP_SECS:
                p.unlink()
        except OSError:
            pass


def _read_raw(name: str, secrets_dir: Path) -> bytes | None:
    """The slot's bytes with NO owner validation; ``None`` on any anomaly.

    ``O_NOFOLLOW`` rejects a symlinked final name; ``fstat`` rejects a
    non-regular file. An ``OSError`` from the read itself propagates, exactly
    as it did before this was split out.

    Split from :func:`_read_final` for #620: provenance asks "did Casa
    generate THESE bytes", a question with no owner in it. A casa-minted token
    is also a valid provider value, so an owner-validated read would describe
    a different file than the one that authenticates.
    """
    path = secrets_dir / name
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        return os.read(fd, _PROVIDER_MAX + 1)
    finally:
        os.close(fd)


def _read_final(name: str, owner: str, secrets_dir: Path) -> bytes | None:
    """Read and owner-validate the live secret; ``None`` on any anomaly."""
    raw = _read_raw(name, secrets_dir)
    if raw is None:
        return None
    return raw if _valid_value(owner, raw) else None


def _publish(name: str, value: bytes, secrets_dir: Path) -> bool:
    """Atomically publish ``value`` at ``name`` via staging + linkat.

    The final name never holds a partial file: a private staging file is
    written IN FULL (short writes are looped, #622) and fsynced, then
    hard-linked into place (``EEXIST`` if a concurrent winner already
    published — never clobbered). A write that cannot complete raises with
    the staging file removed and the final name untouched, so the name stays
    mintable once the filesystem recovers.

    Returns whether THIS call created the live name. ``False`` means a
    concurrent winner published first and its file was kept. #620: a caller
    that records provenance may only do so when it won — the winner can be an
    operator hand-placing a credential between our staging and our link, and
    certifying those bytes as Casa's would hand a future retirement a licence
    to destroy something Casa can neither regenerate nor import.
    """
    secrets_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = secrets_dir / f".tmp-{os.getpid()}-{secrets.token_hex(8)}"
    fd = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        # #622: a single ``os.write`` may consume FEWER bytes than requested.
        # Discarding the return staged a partial value, fsynced it, and linked
        # it into place as the live slot — permanently, because ``os.link``
        # below never clobbers and nothing in Casa unlinks a resident slot. The
        # trigger then 401s forever and its name can never be reused. Loop
        # until the whole buffer is on the fd, so a genuinely exhausted disk
        # RAISES here instead, before anything is published.
        buf = memoryview(value)
        while buf:
            buf = buf[os.write(fd, buf):]
        os.fsync(fd)
    except BaseException:
        # Leave no partial staging file: `_sweep_orphans` would only remove it
        # 60s later, and until then it is a readable fragment of a secret.
        try:
            staging.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(fd)
    dir_fd = os.open(secrets_dir, os.O_DIRECTORY | os.O_NOFOLLOW)
    won = True
    try:
        try:
            os.link(staging, secrets_dir / name)
        except FileExistsError:
            won = False  # a concurrent winner published first — keep theirs
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
        try:
            staging.unlink()
        except OSError:
            pass
    return won


def read_secret(name: str, *, owner: str, secrets_dir: Path) -> bytes | None:
    """Read the live secret for ``name`` (fail-closed)."""
    return _read_final(name, owner, Path(secrets_dir))


# The auth modes that carry a PER-TRIGGER secret under ``secrets_dir``.
# ``hmac_body`` is deliberately absent: it verifies against the one global
# secret and writes nothing here. This module owns secret semantics, so it
# owns the constant; `trigger_reconcile` aliases it rather than restating it.
PER_TRIGGER_SECRET_MODES = ("static_header", "timestamped_hmac")


def probe_secret(name: str, *, owner: str, secrets_dir: Path) -> tuple[str, str]:
    """Classify the slot for ``name`` into one of FOUR states, with a reason.

    ``_read_final`` answers a different question — "give me bytes I may verify
    with" — and collapses five distinct conditions to ``None``: absent,
    unreadable, non-regular, symlinked, and present-but-invalid. That is right
    for the request path, which must fail closed on all of them, and wrong for
    anything that decides what to DO about a slot, because only ``absent``
    may be minted into and only ``absent`` may be reported as "not there yet".

    ENOENT is the ONLY absent condition. ENOTDIR in particular must not read
    as absent: it means a regular file sits where ``secrets_dir`` should be,
    and ``ensure_secret`` would then raise ``EEXIST`` from ``mkdir`` on every
    single pass, forever.

    Returns ``(state, detail)`` where state is ``readable`` | ``absent`` |
    ``unreadable`` | ``invalid``. ``detail`` never contains secret bytes — a
    byte count or an errno name, never the value.
    """
    path = Path(secrets_dir) / name
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return "absent", "no file at this name"
    except OSError as exc:
        return "unreadable", f"{errno.errorcode.get(exc.errno, exc.errno)} opening the file"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "unreadable", "not a regular file"
        raw = os.read(fd, _PROVIDER_MAX + 1)
    except OSError as exc:
        return "unreadable", f"{errno.errorcode.get(exc.errno, exc.errno)} reading the file"
    finally:
        os.close(fd)
    if _valid_value(owner, raw):
        return "readable", f"{len(raw)} bytes, valid for owner {owner!r}"
    if owner == "casa":
        return "invalid", (
            f"{len(raw)} bytes; a casa secret must be exactly "
            f"{_CASA_TOKEN_LEN} ASCII characters")
    return "invalid", (
        f"{len(raw)} bytes; a provider secret must be 1-{_PROVIDER_MAX} "
        f"printable ASCII characters")


def retire_secret(name: str, *, secrets_dir: Path) -> None:
    """Remove EVERY slot for ``name`` — the live secret, a staged ``.next``,
    the rotation state file, and the ``.ident`` consent binding (Release B
    artifact retirement).

    Called when the owning plugin artifact changes or is removed, BEFORE any
    re-approval can mint a replacement — a new artifact never inherits the
    old one's credentials. Missing files/dir are fine; never raises.
    Best-effort by design: non-inheritance does NOT depend on this
    succeeding — :func:`ensure_secret_for_identity` rekeys at activation
    whenever the surviving secret's identity binding doesn't match.
    """
    if not name:
        return
    secrets_dir = Path(secrets_dir)
    for fname in (name, _next_name(name), f"{name}.rot.json",
                  f"{name}.ident", f"{name}{MINT_RECEIPT_SUFFIX}"):
        try:
            (secrets_dir / fname).unlink()
        except OSError:
            pass
    try:
        _fsync_dir(secrets_dir)
    except OSError:
        pass


def retire_secrets_with_prefix(prefix: str, *, secrets_dir: Path) -> list[str]:
    """Retire EVERY slot whose base name starts with *prefix* (Sol shipB-r1
    P1-4: the revoke path must retire from the FILESYSTEM inventory, never
    from ack records — a revoke that deleted the records would otherwise
    leave the secret for the next artifact to inherit). Returns the retired
    base names; never raises."""
    if not prefix:
        return []
    secrets_dir = Path(secrets_dir)
    try:
        names = [p.name for p in secrets_dir.iterdir()]
    except OSError:
        return []
    bases: set[str] = set()
    for n in names:
        base = n
        for suffix in (".rot.json", ".next", ".ident", MINT_RECEIPT_SUFFIX):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        if base.startswith(prefix):
            bases.add(base)
    for base in sorted(bases):
        retire_secret(base, secrets_dir=secrets_dir)
    return sorted(bases)


def ensure_secret(name: str, *, owner: str, secrets_dir: Path) -> bytes | None:
    """Return the live secret for ``name``, minting it for ``owner="casa"`` if
    absent. For ``owner="provider"`` this is read-only (Casa never mints a
    provider secret) — returns ``None`` until imported.
    """
    secrets_dir = Path(secrets_dir)
    if secrets_dir.exists():
        _sweep_orphans(secrets_dir)
    existing = _read_final(name, owner, secrets_dir)
    if existing is not None:
        return existing
    if owner != "casa":
        return None
    _publish(name, secrets.token_urlsafe(_CASA_TOKEN_NBYTES).encode("ascii"),
             secrets_dir)
    return _read_final(name, owner, secrets_dir)


# ---------------------------------------------------------------------------
# Resident mint provenance (#620, INV-TRIG-014).
#
# #620 is that a deleted resident webhook leaves its secret behind and a
# same-name trigger inherits it. Retiring the secret is the fix, and it is
# BLOCKED: `_valid_value("provider", <a 43-byte casa token>)` is True, so no
# shape check separates a Casa token from an operator credential — and Casa can
# neither regenerate nor import the latter (#621), which makes destroying one
# unreconstructible loss.
#
# A receipt is the missing durable fact. It certifies the exact BYTES it was
# written for, never merely its own presence, so every way a value can change
# without Casa minting it — a hand replacement, `rotation_promote`'s replace, a
# restore from backup — self-invalidates it and the answer falls back to
# `unproven`. Nothing here retires anything: this records the fact a later,
# owner-aware retirement would have to stand on. #620 stays open.
# ---------------------------------------------------------------------------


def _receipt_path(name: str, secrets_dir: Path) -> Path:
    return secrets_dir / f"{name}{MINT_RECEIPT_SUFFIX}"


def _write_mint_receipt(name: str, value: bytes, secrets_dir: Path) -> bool:
    """Record that Casa generated exactly ``value`` at ``name``. Never raises.

    Best-effort ON PURPOSE. The secret is already published and usable by the
    time this runs; raising would turn a missing PROOF into a trigger that
    401s forever, and `mint_for_specs` records a failure only for a raised
    exception. A filesystem fault must not reshape the surface. A slot with no
    receipt simply reads `unproven`, which is the fail-closed answer and can
    never authorise destruction.

    Stages under `.tmp-` so `_sweep_orphans` reclaims a crash remnant, writes
    in full (#622 — a short write would stage a truncated digest, which reads
    as a MISMATCH and is therefore safe, but pointless), fsyncs, then replaces
    atomically. The payload holds a digest, never the value.
    """
    payload = json.dumps({
        "v": _RECEIPT_VERSION,
        "minted_by": "casa",
        "value_sha256": hashlib.sha256(value).hexdigest(),
    }).encode("ascii")
    try:
        tmp = secrets_dir / f".tmp-mint-{os.getpid()}-{secrets.token_hex(8)}"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                     0o600)
        try:
            buf = memoryview(payload)
            while buf:
                buf = buf[os.write(fd, buf):]
            os.fsync(fd)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
        os.replace(tmp, _receipt_path(name, secrets_dir))
        _fsync_dir(secrets_dir)
        return True
    except OSError:
        return False


def _read_receipt(name: str, secrets_dir: Path) -> dict | None:
    """The parsed receipt, or ``None`` for every anomaly.

    Unlike :func:`_read_state`, a malformed receipt is reported and **NEVER
    deleted**. Speculative deletion in this directory is the whole hazard;
    an unreadable receipt already fails closed as `unproven`.
    """
    try:
        raw = _read_raw(f"{name}{MINT_RECEIPT_SUFFIX}", secrets_dir)
    except OSError:
        return None
    if raw is None:
        return None
    try:
        rec = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(rec, dict) or set(rec) != _RECEIPT_KEYS:
        return None
    # `type(...) is int` and not `isinstance`: `True == 1`, so a receipt
    # carrying `"v": true` would otherwise pass the version check.
    if type(rec["v"]) is not int or rec["v"] != _RECEIPT_VERSION:
        return None
    if rec["minted_by"] != "casa":
        return None
    digest = rec["value_sha256"]
    if not isinstance(digest, str) or not _HEX64_RE.match(digest):
        return None
    return rec


def resident_secret_provenance(name: str, *, secrets_dir: Path) -> str:
    """``casa_minted`` | ``unproven`` | ``absent`` for the slot at ``name``.

    ``casa_minted`` means Casa GENERATED the bytes that are in the slot right
    now. It is the ONLY state a future retirement may act on, and it requires
    all of: a regular, non-symlinked live file; a regular, non-symlinked,
    parseable receipt of the exact schema and version; and a digest matching
    those exact live bytes.

    Every other condition — no receipt, malformed, wrong version, stale from a
    value since replaced, unreadable, a lost create race — is ``unproven``.
    Absence of evidence is never consent.

    ``unproven`` means Casa CANNOT PROVE it minted these bytes. It does not
    mean it did not: a mint whose receipt write failed reads identically, and
    that path is deliberate. Read as disproof it would say the opposite of
    what it knows, and a consumer acting on "these are not Casa's" is exactly
    the confident destruction this exists to prevent.

    Owner-AGNOSTIC by design: "did Casa generate these bytes" does not depend
    on how the declaration currently labels them. An owner-validated read
    would reintroduce exactly the owner-flip blindness that lets a casa token
    pass as a provider value.

    Total: never raises.
    """
    secrets_dir = Path(secrets_dir)
    try:
        raw = _read_raw(name, secrets_dir)
    except OSError:
        return "unproven"
    if raw is None:
        # ENOENT is the ONLY absent condition. Unreadable, non-regular and
        # symlinked must not collapse into absent, or a transient /data fault
        # reads as "nothing here" to whatever later decides what to do.
        try:
            os.lstat(secrets_dir / name)
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "unproven"
        return "unproven"
    rec = _read_receipt(name, secrets_dir)
    if rec is None:
        return "unproven"
    # `_read_receipt` has already shape-checked the digest to 64 lowercase hex,
    # which is what makes this comparison safe to reach: `compare_digest`
    # raises on a non-ASCII str, and this function never raises.
    actual = hashlib.sha256(raw).hexdigest()
    return ("casa_minted" if hmac.compare_digest(rec["value_sha256"], actual)
            else "unproven")


def mint_resident_secret(name: str, *, secrets_dir: Path) -> bytes | None:
    """Mint a casa-owned RESIDENT slot and record value-bound provenance.

    The resident counterpart to :func:`ensure_secret`, which is deliberately
    left unchanged: that one also serves the plugin path through
    :func:`ensure_secret_for_identity`, whose provenance is the `.ident`
    consent binding and whose semantics are retire-and-rekey. A resident has
    neither, and a second differently-shaped provenance on that path would be
    redundant surface.

    A receipt is written ONLY when this call actually created the live name.
    `_publish` keeps a concurrent winner's file rather than clobbering it, and
    that winner can be an operator hand-placing a credential — certifying its
    bytes would be the precise mistake this mechanism exists to prevent.

    An EXISTING value is returned untouched and NEVER receipted. Backfilling
    would certify a 43-byte operator credential as Casa's, because nothing
    distinguishes the two by shape; the caller's own `probe_secret == absent`
    guard already means this normally sees no file at all.
    """
    secrets_dir = Path(secrets_dir)
    if secrets_dir.exists():
        _sweep_orphans(secrets_dir)
    existing = _read_final(name, "casa", secrets_dir)
    if existing is not None:
        return existing
    value = secrets.token_urlsafe(_CASA_TOKEN_NBYTES).encode("ascii")
    if _publish(name, value, secrets_dir):
        _write_mint_receipt(name, value, secrets_dir)
    return _read_final(name, "casa", secrets_dir)


def _ident_path(name: str, secrets_dir: Path) -> Path:
    return secrets_dir / f"{name}.ident"


def _write_ident(name: str, identity: str, secrets_dir: Path) -> bool:
    """Atomically persist the consent-identity binding; False on failure."""
    try:
        tmp = secrets_dir / f".ident-{os.getpid()}-{secrets.token_hex(8)}"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                     0o600)
        try:
            # #622 — a truncated `.ident` is worse than an absent one: it reads
            # as a MISMATCHED identity, and `ensure_secret_for_identity` retires
            # and re-mints on mismatch.
            buf = memoryview(identity.encode("ascii"))
            while buf:
                buf = buf[os.write(fd, buf):]
            os.fsync(fd)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
        os.replace(tmp, _ident_path(name, secrets_dir))
        _fsync_dir(secrets_dir)
        return True
    except (OSError, UnicodeEncodeError):
        return False


def secret_bound_to_identity(
    name: str, *, identity: str, secrets_dir: Path,
) -> bool:
    """Is a live casa-owned secret for ``name`` ALREADY minted under
    ``identity``? The read-only mirror of the reuse test inside
    :func:`ensure_secret_for_identity` (#453).

    False for every state that mint would treat as needing work: no secret at
    all (nothing minted yet), a secret whose ``.ident`` sidecar names a
    different consent identity (a re-approval after a revoke rekeys), and an
    unbound one (the webhook handler's lazy mint, or a crash mid-bind) — all of
    which the next reconcile REPLACES. A consumer that read the file first
    would provision an external service against a credential Casa is about to
    change, so this is the predicate the setup gate needs, not "a file
    exists". Total: any read failure is False."""
    secrets_dir = Path(secrets_dir)
    try:
        if _read_final(name, "casa", secrets_dir) is None:
            return False
        return _ident_path(name, secrets_dir).read_text(
            encoding="ascii").strip() == identity
    except (OSError, UnicodeDecodeError):
        return False


def ensure_secret_for_identity(
    name: str, *, identity: str, secrets_dir: Path,
) -> bytes | None:
    """The live casa-owned secret for ``name``, GUARANTEED minted under the
    consent *identity* (Terra shipB-r2: non-inheritance is enforced at
    ACTIVATION, not at retirement).

    A surviving secret whose ``.ident`` sidecar matches *identity* is reused
    (same approved tuple — continuity for the provisioned external service).
    A secret bound to a DIFFERENT identity, or with no binding at all
    (pre-binding mint, crash, lazy handler mint), is retired and re-minted —
    so even a silently-failed earlier retirement can never leak the old
    credential into a new approval. If the stale secret cannot actually be
    removed, this returns ``None`` (the trigger stays unrouted with
    ``trigger_secret_missing``) rather than the surviving old value.
    """
    secrets_dir = Path(secrets_dir)
    existing = _read_final(name, "casa", secrets_dir)
    if existing is not None:
        try:
            bound = _ident_path(name, secrets_dir).read_text(
                encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            bound = None
        if bound == identity:
            return existing
        retire_secret(name, secrets_dir=secrets_dir)
        if _read_final(name, "casa", secrets_dir) is not None:
            return None  # stale credential survived — fail closed, no reuse
    got = ensure_secret(name, owner="casa", secrets_dir=secrets_dir)
    if got is None:
        return None
    if not _write_ident(name, identity, secrets_dir):
        # An unbound secret would be rekeyed on every reconcile (safe but
        # churn) — surface the problem instead: retire + unrouted.
        retire_secret(name, secrets_dir=secrets_dir)
        return None
    return got


# ---------------------------------------------------------------------------
# Secret rotation state machine (spec A2c) — persisted, crash-safe.
#
# Phases (state file ``<name>.rot.json``; absent = idle):
#   awaiting_next : provider rotation begun, waiting for the provider `.next`
#                   import (single-accept; live endpoint keeps working).
#   staged        : `.next` present + valid; verifier dual-accepts live + next.
#   promote       : persisted immediately before the live-replace rename, so a
#                   crash mid-rename is recoverable.
#
# `.next` is published with the same no-clobber staging primitive as the live
# secret; the state file is published with an atomic overwrite (os.replace).
# ---------------------------------------------------------------------------


def _state_path(name: str, secrets_dir: Path) -> Path:
    return secrets_dir / f"{name}.rot.json"


def _next_name(name: str) -> str:
    return f"{name}.next"


def _write_state(name: str, phase: str, owner: str, secrets_dir: Path) -> None:
    payload = json.dumps(
        {"phase": phase, "secret_owner": owner, "started_ts": int(time.time())}
    ).encode("ascii")
    tmp = secrets_dir / f".rot-{os.getpid()}-{secrets.token_hex(8)}"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        # #622 — same short-write hazard as `_publish`.
        buf = memoryview(payload)
        while buf:
            buf = buf[os.write(fd, buf):]
        os.fsync(fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)
    os.replace(tmp, _state_path(name, secrets_dir))
    dir_fd = os.open(secrets_dir, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _read_state(name: str, secrets_dir: Path) -> dict | None:
    """Parsed rotation state, or ``None`` if absent/malformed (fail-closed:
    a malformed state file is deleted)."""
    path = _state_path(name, secrets_dir)
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        st = json.loads(raw)
        if not isinstance(st, dict) or st.get("phase") not in (
            "awaiting_next", "staged", "promote"
        ):
            raise ValueError("bad phase")
        return st
    except (ValueError, TypeError):
        try:
            path.unlink()
        except OSError:
            pass
        return None


def _clear_state(name: str, secrets_dir: Path) -> None:
    try:
        _state_path(name, secrets_dir).unlink()
    except OSError:
        pass
    dir_fd = os.open(secrets_dir, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _fsync_dir(secrets_dir: Path) -> None:
    dir_fd = os.open(secrets_dir, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def rotation_begin(name: str, *, owner: str, secrets_dir: Path) -> str:
    """Begin a rotation. Returns the resolved phase.

    ``casa``: mint (or reuse an existing) ``.next`` and go straight to
    ``staged``. ``provider``: enter ``awaiting_next`` (the ``.next`` arrives
    later via :func:`rotation_import_next`).
    """
    secrets_dir = Path(secrets_dir)
    if owner == "casa":
        # Reuse an existing `.next` (prior unfinished rotation), else mint.
        if _read_final(_next_name(name), owner, secrets_dir) is None:
            _publish(_next_name(name),
                     secrets.token_urlsafe(_CASA_TOKEN_NBYTES).encode("ascii"),
                     secrets_dir)
        _write_state(name, "staged", owner, secrets_dir)
        return "staged"
    _write_state(name, "awaiting_next", owner, secrets_dir)
    return "awaiting_next"


def rotation_import_next(
    name: str, value: bytes, *, owner: str, secrets_dir: Path,
) -> str:
    """Import a provider-minted ``.next`` secret (slot=next). Transitions
    ``awaiting_next`` → ``staged``. Idempotent for an equal re-import; raises
    ``ValueError('secret_conflict')`` for an unequal one (spec A2b/Sol r5-1)."""
    secrets_dir = Path(secrets_dir)
    if not _valid_value(owner, value):
        raise ValueError("invalid_secret_value")
    existing = _read_final(_next_name(name), owner, secrets_dir)
    if existing is not None:
        if not hmac.compare_digest(existing, value):
            raise ValueError("secret_conflict")
        _write_state(name, "staged", owner, secrets_dir)
        return "staged"
    _publish(_next_name(name), value, secrets_dir)
    _write_state(name, "staged", owner, secrets_dir)
    return "staged"


def rotation_promote(name: str, *, secrets_dir: Path) -> str:
    """Promote ``.next`` to the live secret and clear rotation state.

    Persists ``promote`` before the rename so a crash mid-rename recovers.
    """
    secrets_dir = Path(secrets_dir)
    st = _read_state(name, secrets_dir)
    owner = (st or {}).get("secret_owner", "casa")
    _write_state(name, "promote", owner, secrets_dir)
    os.replace(secrets_dir / _next_name(name), secrets_dir / name)
    _fsync_dir(secrets_dir)
    _clear_state(name, secrets_dir)
    return "idle"


def rotation_recover(name: str, *, owner: str, secrets_dir: Path) -> str:
    """Reconcile persisted rotation state at boot. Returns the resolved phase.

    Every durable combination converges: ``awaiting_next`` keeps waiting;
    ``staged`` stays only with a valid ``.next`` (else reverts to idle);
    ``promote`` completes the rename if ``.next`` survives, else the live file
    already won.
    """
    secrets_dir = Path(secrets_dir)
    st = _read_state(name, secrets_dir)
    if st is None:
        return "idle"
    phase = st["phase"]
    st_owner = st.get("secret_owner", owner)
    if phase == "awaiting_next":
        return "awaiting_next"
    if phase == "staged":
        if _read_final(_next_name(name), st_owner, secrets_dir) is not None:
            return "staged"
        _clear_state(name, secrets_dir)
        return "idle"
    # promote
    next_path = secrets_dir / _next_name(name)
    if next_path.exists():
        os.replace(next_path, secrets_dir / name)
        _fsync_dir(secrets_dir)
    _clear_state(name, secrets_dir)
    return "idle"
