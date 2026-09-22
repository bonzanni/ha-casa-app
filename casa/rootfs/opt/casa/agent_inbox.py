"""Inbound files for the Telegram default agent (#1036).

A PDF, image or text file the operator sends in the Telegram DM is downloaded,
checked, and published into a per-role ``ready/`` directory that the agent's own
``Read`` tool may open. Arrival runs no agent turn: Casa stores the file and
acknowledges it; the agent reads it later, when asked.

Layout, per role::

    <root>/<role>/
        staging/   in-flight ``.part-*`` downloads   (never readable)
        meta/      one JSON per published file        (never readable)
        ready/     published files                    (the readable prefix)

The one guarantee this module exists to keep: **``ready/`` contains only
regular, single-link files that Casa wrote, under a name Casa generated.** It is
established at publication (the file is created ``O_EXCL|O_NOFOLLOW``, flushed,
``fstat``-checked and renamed through a pinned directory descriptor) and
re-checked by the sweep, which removes anything else and logs at WARN. A
read-only grant over ``ready/`` is safe only while that holds: the prefix check
in ``hooks.py`` is lexical and does not resolve links.

What this module deliberately does NOT do:

* evict to make room — a full folder refuses the next upload; the only deletion
  of an acknowledged file is age;
* sweep ``staging/`` by age — an unlinked ``.part`` still open keeps its inode
  and fails at rename. Instead a download is held in memory (bounded by the cap)
  under a deadline, so a hung download leaves nothing on disk; a ``.part`` exists
  only during the bounded publication step, whose own ``finally`` removes it;
  and boot reclaims whatever a crash left;
* bound page count or read cost — only bytes.
"""
from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import re
import secrets
import shutil
import stat
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

import atomic_io
from media_policies import _accepts_pdf, _accepts_photo, _accepts_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — no app option: pre-1.0, a constant beats an option nobody tunes.
# ---------------------------------------------------------------------------

_MB = 1024 * 1024

#: Per-file cap. A read puts the file into the model request as a base64
#: document block, so the binding limit is the API request (32 MB), not
#: Telegram's 20 MB download cap; 8 MB base64-encodes to ~10.7 MB.
CAP_BYTES = 8 * _MB
#: Retention of an acknowledged file. The acknowledgement promises it.
RETENTION_S = 7 * 24 * 3600
#: Per-role capacity, enforced at publication. Full means refuse, never evict.
MAX_FILES = 50
MAX_BYTES = 200 * _MB
#: Deadline on one download. The bytes are in memory until then, so hitting it
#: leaves nothing on disk.
UPLOAD_DEADLINE_S = 300
#: Sweep cadence for ``ready/``.
SWEEP_INTERVAL_H = 1

STAGING = "staging"
READY = "ready"
META = "meta"

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

#: Allowed extensions → the content predicate that must accept the bytes. The
#: predicates are imported from ``media_policies`` (total over content, already
#: reviewed); the TABLE is not, because that one is keyed by outbound send kind.
#: ``_accepts_photo`` accepts JPEG and PNG alike, so ``.jpg``/``.png`` are one
#: family here.
INBOUND_POLICIES: dict[str, Callable[[bytes], bool]] = {
    ".pdf": _accepts_pdf,
    ".png": _accepts_photo,
    ".jpg": _accepts_photo,
    ".jpeg": _accepts_photo,
    ".txt": _accepts_text,
    ".md": _accepts_text,
    ".csv": _accepts_text,
}

#: The only names ``ready/`` may hold. Anything else is not Casa's and is removed.
_NAME_RE = re.compile(r"^(\d{13})-([0-9a-f]{16})(\.[a-z]{2,4})$")


def _new_name(ext: str, now_ms: int | None = None) -> str:
    """Casa's name for a published file. No operator byte reaches it: the
    extension is the allowlist key that matched, never a substring of what the
    operator sent."""
    if ext not in INBOUND_POLICIES:
        raise ValueError(f"not an allowed extension: {ext!r}")
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    return f"{ms:013d}-{secrets.token_hex(8)}{ext}"


def extension_of(file_name: str | None) -> str | None:
    """The allowlist key for an operator-supplied filename, or ``None``.

    Only the final ASCII suffix of the final path component is considered, and it
    is returned only if it is itself a key of ``INBOUND_POLICIES`` — so the value
    is always drawn from Casa's closed vocabulary."""
    if not file_name or not isinstance(file_name, str):
        return None
    base = file_name.replace("\\", "/").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    if dot <= 0:
        return None
    ext = base[dot:].lower()
    return ext if ext in INBOUND_POLICIES else None


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


class Outcome(Enum):
    """Every way an upload ends. Each maps to exactly one operator-facing line
    in the channel; none of them is silent."""

    STORED = "stored"
    TOO_LARGE = "too_large"
    MISMATCH = "mismatch"
    FULL = "full"
    DOWNLOAD_FAILED = "download_failed"
    LOCAL_PATH = "local_path"
    STORAGE_FAILED = "storage_failed"   # nothing was published
    UNCERTAIN = "uncertain"             # published, but durability unconfirmed


@dataclass(frozen=True)
class Receipt:
    outcome: Outcome
    name: str = ""          # Casa's published name, when STORED
    size: int = 0           # bytes received


@dataclass(frozen=True)
class InboundFile:
    """One entry of ``ready/``, as ``list_inbound_files`` reports it."""

    name: str
    path: str
    display_name: str
    ext: str
    size: int
    published_at: float     # epoch seconds, from the name


class DownloadError(Exception):
    """The transport could not deliver the file."""


class TooLarge(Exception):
    """More bytes arrived than the cap allows."""


class LocalPathRefused(Exception):
    """``getFile`` returned something other than a URL under the configured
    Bot API file endpoint — most notably a local path, which PTB would copy
    from Casa's own filesystem."""


# ---------------------------------------------------------------------------
# The inbox
# ---------------------------------------------------------------------------


class Inbox:
    """One role's inbound-file folder. Construct with :func:`open_inbox`."""

    def __init__(self, role: str, base: str) -> None:
        self.role = role
        self.base = base
        self.staging_dir = os.path.join(base, STAGING)
        self.ready_dir = os.path.join(base, READY)
        self.meta_dir = os.path.join(base, META)
        self._staging_fd: int | None = None
        self._ready_fd: int | None = None
        self._meta_fd: int | None = None
        # Created lazily so it binds to the loop that first uses it.
        self._publish_lock: asyncio.Lock | None = None

    # -- lifecycle --------------------------------------------------------

    def _provision(self) -> None:
        for d in (self.base, self.staging_dir, self.ready_dir, self.meta_dir):
            os.makedirs(d, mode=0o700, exist_ok=True)
            st = os.lstat(d)
            if not stat.S_ISDIR(st.st_mode):
                raise RuntimeError(f"inbox path is not a real directory: {d}")
            if st.st_mode & 0o077:
                os.chmod(d, 0o700)
        self._staging_fd = os.open(self.staging_dir, _DIR_FLAGS)
        self._ready_fd = os.open(self.ready_dir, _DIR_FLAGS)
        self._meta_fd = os.open(self.meta_dir, _DIR_FLAGS)

    def close(self) -> None:
        for attr in ("_staging_fd", "_ready_fd", "_meta_fd"):
            fd = getattr(self, attr)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, attr, None)

    def reclaim_staging(self) -> int:
        """Remove every ``staging/.part-*``. Called at boot, before the handler
        that writes them is registered: after a restart no upload is in flight,
        because the tasks and descriptors that owned them are gone."""
        removed = 0
        assert self._staging_fd is not None
        for entry in os.listdir(self._staging_fd):
            try:
                st = os.lstat(entry, dir_fd=self._staging_fd)
                if stat.S_ISDIR(st.st_mode):
                    shutil.rmtree(os.path.join(self.staging_dir, entry))
                else:
                    os.unlink(entry, dir_fd=self._staging_fd)
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("inbox %s: could not reclaim staging entry: %s",
                               self.role, exc)
        if removed:
            logger.info("inbox %s: reclaimed %d staging entr%s at boot",
                        self.role, removed, "y" if removed == 1 else "ies")
        return removed

    # -- sweep ------------------------------------------------------------

    def sweep(self, now: float | None = None) -> dict[str, int]:
        """Enforce the ``ready/`` invariant and retention.

        * Anything that is not a regular, single-link file under a Casa name is
          removed and logged at WARN — the invariant is loud when it breaks.
        * A Casa file older than :data:`RETENTION_S` is removed with its meta.
          Age comes from the name, not ``mtime``: nothing can re-date it.
        * A meta file whose published file is gone is removed.

        Never removes a file for capacity."""
        assert self._ready_fd is not None and self._meta_fd is not None
        now_s = time.time() if now is None else now
        stats = {"foreign": 0, "expired": 0, "orphan_meta": 0}
        present: set[str] = set()
        for entry in os.listdir(self._ready_fd):
            try:
                st = os.lstat(entry, dir_fd=self._ready_fd)
            except FileNotFoundError:
                continue
            m = _NAME_RE.match(entry)
            regular = stat.S_ISREG(st.st_mode) and st.st_nlink == 1
            if not regular or m is None or m.group(3) not in INBOUND_POLICIES:
                logger.warning(
                    "inbox %s: removing an entry that is not a Casa-written "
                    "regular file (mode=%o nlink=%d)",
                    self.role, st.st_mode, st.st_nlink)
                self._remove_entry(self._ready_fd, self.ready_dir, entry, st)
                stats["foreign"] += 1
                continue
            published_s = int(m.group(1)) / 1000.0
            if now_s - published_s >= RETENTION_S:
                self._remove_entry(self._ready_fd, self.ready_dir, entry, st)
                self._drop_meta(entry)
                stats["expired"] += 1
                continue
            present.add(entry)
        for entry in os.listdir(self._meta_fd):
            if entry.endswith(".json") and entry[:-5] not in present:
                try:
                    os.unlink(entry, dir_fd=self._meta_fd)
                    stats["orphan_meta"] += 1
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.warning("inbox %s: meta cleanup failed: %s", self.role, exc)
        return stats

    @staticmethod
    def _remove_entry(dir_fd: int, dir_path: str, entry: str, st: os.stat_result) -> None:
        try:
            if stat.S_ISDIR(st.st_mode):
                # rmtree does not follow symlinks inside the tree.
                shutil.rmtree(os.path.join(dir_path, entry))
            else:
                os.unlink(entry, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("inbox: could not remove %r: %s", entry, exc)

    def _drop_meta(self, name: str) -> None:
        assert self._meta_fd is not None
        try:
            os.unlink(f"{name}.json", dir_fd=self._meta_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("inbox %s: meta removal failed: %s", self.role, exc)

    # -- listing ----------------------------------------------------------

    def list_files(self) -> list[InboundFile]:
        """Published files, newest first. Only Casa-named regular files appear;
        the display name comes from meta and falls back to Casa's name."""
        assert self._ready_fd is not None
        out: list[InboundFile] = []
        for entry in os.listdir(self._ready_fd):
            m = _NAME_RE.match(entry)
            if m is None or m.group(3) not in INBOUND_POLICIES:
                continue
            try:
                st = os.lstat(entry, dir_fd=self._ready_fd)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                continue
            out.append(InboundFile(
                name=entry,
                path=os.path.join(self.ready_dir, entry),
                display_name=self._display_name(entry),
                ext=m.group(3),
                size=st.st_size,
                published_at=int(m.group(1)) / 1000.0,
            ))
        out.sort(key=lambda f: f.published_at, reverse=True)
        return out

    def display_name_for(self, name: str) -> str:
        """The name the operator gave a published file, or Casa's name when no
        meta was written — what the #1038 disclosure calls the file."""
        return self._display_name(name)

    def _display_name(self, name: str) -> str:
        try:
            with open(os.path.join(self.meta_dir, f"{name}.json"),
                      encoding="utf-8") as fh:
                meta = json.load(fh)
            dn = meta.get("display_name")
            if isinstance(dn, str) and dn:
                return dn
        except (OSError, ValueError):
            pass
        return name

    # -- publication ------------------------------------------------------

    def _lock(self) -> asyncio.Lock:
        if self._publish_lock is None:
            self._publish_lock = asyncio.Lock()
        return self._publish_lock

    def _write_part(self, part: str, data: bytes) -> None:
        """Create ``part`` fresh and make its bytes durable. Strict: any failure
        raises and nothing is published. The caller chose the name, so it can
        clean up whatever this leaves."""
        assert self._staging_fd is not None
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o600, dir_fd=self._staging_fd)
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            self._unlink_part(part)
            raise
        os.close(fd)

    def _unlink_part(self, part: str) -> None:
        if self._staging_fd is None:
            return
        try:
            os.unlink(part, dir_fd=self._staging_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("inbox %s: could not remove %s: %s", self.role, part, exc)

    def _capacity_ok(self, incoming: int) -> bool:
        """Counts every entry in ``ready/`` — a foreign entry the sweep has not
        removed yet still occupies space. ``lstat`` only: bounded work, fit to
        run inside the publication lock."""
        assert self._ready_fd is not None
        count = 0
        total = 0
        for entry in os.listdir(self._ready_fd):
            try:
                total += os.lstat(entry, dir_fd=self._ready_fd).st_size
            except FileNotFoundError:
                continue
            count += 1
        return count < MAX_FILES and total + incoming <= MAX_BYTES

    def _publish_locked(self, part: str, size: int, ext: str) -> tuple[Outcome, str]:
        """The critical section: final quota scan, final ``fstat``, rename.
        Bounded metadata work only — the file flush happened before the lock
        was taken and the directory flush happens after it is released.

        The name — and with it the epoch retention is measured from — is minted
        here, immediately before the rename, so the seven days the
        acknowledgement promises start at publication, not before the flush."""
        assert self._staging_fd is not None and self._ready_fd is not None
        if not self._capacity_ok(size):
            return Outcome.FULL, ""
        fd = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=self._staging_fd)
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
                or st.st_size != size):
            raise OSError(errno.EIO, "staged file changed before publication")
        name = _new_name(ext)
        os.rename(part, name, src_dir_fd=self._staging_fd,
                  dst_dir_fd=self._ready_fd)
        return Outcome.STORED, name

    async def publish(self, data: bytes, ext: str, display_name: str) -> Receipt:
        """Validate, flush and publish ``data``. Every path out of here either
        publishes, or leaves nothing in ``staging/``.

        Order: write and flush the ``.part`` outside the lock; take
        the lock for the final quota scan, ``fstat`` and rename only; release;
        flush the directory. Display metadata is written after publication and
        is best-effort — a missing display name is cosmetic, never a failure."""
        size = len(data)
        if size > CAP_BYTES:
            return Receipt(Outcome.TOO_LARGE, size=size)
        predicate = INBOUND_POLICIES.get(ext)
        if predicate is None or not predicate(data):
            return Receipt(Outcome.MISMATCH, size=size)

        part: str | None = f".part-{secrets.token_hex(12)}"
        try:
            await asyncio.to_thread(self._write_part, part, data)
            async with self._lock():
                outcome, name = await asyncio.to_thread(
                    self._publish_locked, part, size, ext)
            if outcome is not Outcome.STORED:
                return Receipt(outcome, size=size)
            part = None  # renamed into ready/; nothing left in staging
            try:
                await asyncio.to_thread(os.fsync, self._ready_fd)
            except OSError as exc:
                logger.warning("inbox %s: directory flush failed after "
                               "publication: %s", self.role, exc)
                await asyncio.to_thread(self._best_effort_retract, name)
                return Receipt(Outcome.UNCERTAIN, size=size)
        except OSError as exc:
            logger.warning("inbox %s: storage failed: %s", self.role, exc)
            return Receipt(Outcome.STORAGE_FAILED, size=size)
        finally:
            if part is not None:
                await asyncio.to_thread(self._unlink_part, part)
        try:
            await asyncio.to_thread(self._write_meta, name, display_name, ext, size)
        except OSError as exc:
            logger.warning("inbox %s: display metadata not written: %s",
                           self.role, exc)
        return Receipt(Outcome.STORED, name=name, size=size)

    def _write_meta(self, name: str, display_name: str, ext: str, size: int) -> None:
        atomic_io.atomic_write_json(
            os.path.join(self.meta_dir, f"{name}.json"),
            {"display_name": _bounded_display(display_name), "ext": ext,
             "size": size},
            mode=0o600,
        )

    def _best_effort_retract(self, name: str) -> None:
        """After a failed directory flush the outcome is unknown. Try to remove
        what was published; whatever happens, the caller says it is uncertain."""
        assert self._ready_fd is not None
        try:
            os.unlink(name, dir_fd=self._ready_fd)
        except OSError as exc:
            logger.warning("inbox %s: retract after failed flush did not "
                           "complete: %s", self.role, exc)

    # -- the whole upload -------------------------------------------------

    async def receive(
        self,
        fetch: Callable[[int], Awaitable[bytes]],
        *,
        ext: str,
        display_name: str,
        declared_size: int | None,
    ) -> Receipt:
        """Download through ``fetch`` (which must enforce the cap it is given) and
        publish.

        The :data:`UPLOAD_DEADLINE_S` deadline bounds the DOWNLOAD — the only
        phase that can hang. Bytes are held in memory, bounded by the cap, so a
        cancelled download leaves nothing on disk at all. A ``.part`` exists only
        during publication, which is bounded disk work and is never cancelled
        mid-write: cancelling a coroutine cannot stop a thread already writing,
        and would orphan the file it was creating."""
        if declared_size is not None and declared_size > CAP_BYTES:
            return Receipt(Outcome.TOO_LARGE, size=declared_size)
        # Early and non-authoritative: spares a download into a full folder. The
        # authoritative check is the one under the lock at publication.
        if not await asyncio.to_thread(self._capacity_ok, declared_size or 0):
            return Receipt(Outcome.FULL)
        try:
            async with asyncio.timeout(UPLOAD_DEADLINE_S):
                data = await fetch(CAP_BYTES)
        except TooLarge:
            return Receipt(Outcome.TOO_LARGE, size=CAP_BYTES + 1)
        except LocalPathRefused:
            return Receipt(Outcome.LOCAL_PATH)
        except (DownloadError, TimeoutError):
            return Receipt(Outcome.DOWNLOAD_FAILED)
        return await self.publish(data, ext, display_name)


def _bounded_display(name: str) -> str:
    """The operator's filename as display metadata: control characters removed,
    length bounded. It never reaches a path."""
    cleaned = "".join(ch for ch in (name or "") if ch.isprintable())
    return cleaned[:200]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


async def fetch_telegram_file(bot, file_id: str, cap: int, *,
                              transport=None) -> bytes:
    """Fetch a Telegram file, counting bytes as they arrive.

    Accepts only a ``file_path`` under the bot's configured file endpoint. PTB's
    ``get_file`` leaves the path bare when it names a file that exists on Casa's
    own filesystem — and its ``download_to_drive`` would then copy that path —
    so anything that is not under the endpoint is refused rather than handled.
    PTB's own download helpers buffer the whole response before writing, so
    they are not used. Nothing here logs the URL: it embeds the bot token."""
    import httpx

    try:
        tg_file = await bot.get_file(file_id)
    except Exception as exc:  # noqa: BLE001 — any failure is a download failure
        raise DownloadError(type(exc).__name__) from None
    url = getattr(tg_file, "file_path", None)
    prefix = str(getattr(bot, "base_file_url", "")).rstrip("/") + "/"
    if not isinstance(url, str) or not prefix.startswith(("https://", "http://")) \
            or not url.startswith(prefix):
        raise LocalPathRefused()
    buf = bytearray()
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30.0,
                                     transport=transport) as client:
            async with client.stream("GET", url,
                                     headers={"Accept-Encoding": "identity"}) as resp:
                if resp.status_code != 200:
                    raise DownloadError(f"http {resp.status_code}")
                async for chunk in resp.aiter_raw():
                    buf.extend(chunk)
                    if len(buf) > cap:
                        raise TooLarge()
    except (TooLarge, DownloadError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(type(exc).__name__) from None
    return bytes(buf)


# ---------------------------------------------------------------------------
# Module state and boot wiring
# ---------------------------------------------------------------------------

_inboxes: dict[str, Inbox] = {}


def open_inbox(role: str, root: str) -> Inbox:
    inbox = Inbox(role, os.path.join(root, role))
    try:
        inbox._provision()
    except Exception:
        # Provisioning opens three directory descriptors in turn; a failure
        # partway must not leak the ones already open.
        inbox.close()
        raise
    return inbox


def get_inbox(role: str) -> Inbox | None:
    return _inboxes.get(role)


def readable_prefixes(role: str) -> tuple[str, ...]:
    """The read grant for ``role``: exactly its ``ready/`` directory if it has an
    inbox, otherwise nothing. Every other agent keeps an empty readable list."""
    inbox = _inboxes.get(role)
    return (inbox.ready_dir,) if inbox is not None else ()


def sweep_all() -> None:
    for inbox in list(_inboxes.values()):
        try:
            inbox.sweep()
        except Exception:  # noqa: BLE001 — one inbox never stops another
            logger.warning("inbox %s: sweep failed", inbox.role, exc_info=True)


async def sweep_job() -> None:
    await asyncio.to_thread(sweep_all)


def register_sweep(scheduler) -> None:
    scheduler.add_job(
        sweep_job, trigger="interval", id="agent_inbox_sweep",
        hours=SWEEP_INTERVAL_H, replace_existing=True, coalesce=True,
        max_instances=1, misfire_grace_time=3600,
    )


async def wire(scheduler, root: str, *, role: str) -> None:
    """Boot wiring, called BEFORE agents are built and before channels go live:
    provision ``role``'s inbox, reclaim staging, sweep once, register the hourly
    sweep. A failure never blocks boot — the role simply has no inbox, so it gets
    no read grant and uploads are answered with the storage refusal."""
    inbox: Inbox | None = None
    try:
        inbox = await asyncio.to_thread(open_inbox, role, root)
        await asyncio.to_thread(inbox.reclaim_staging)
        await asyncio.to_thread(inbox.sweep)
        register_sweep(scheduler)
    except Exception:  # noqa: BLE001 — boot continues without an inbox
        logger.warning("agent inbox for %s could not be provisioned; inbound "
                       "files disabled", role, exc_info=True)
        if inbox is not None:
            inbox.close()
        return
    # Installed last: a failure at any step above leaves no inbox, so no grant.
    _inboxes[role] = inbox


def _reset_for_tests() -> None:
    for inbox in _inboxes.values():
        inbox.close()
    _inboxes.clear()
