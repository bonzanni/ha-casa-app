# casa/rootfs/opt/casa/rw_barrier.py
"""#578: the readers/writer core shared by the two process-wide barriers.

Two barriers in Casa have the same shape — many concurrent participants on the
SHARED side, one draining exclusive holder — over two different populations:

- :class:`memory_wipe.RetainFence` over long-term-bank WRITERS, plus a wipe
  generation that makes a pre-wipe writer discard.
- :class:`session_gate.TurnAdmission` over TURNS, so a retirement can drain
  every in-flight turn on any path (the client pool could only ever join the
  turns it owned).

The core lives here because of one scar, not for tidiness. ``RetainFence``
originally released its exclusive lock only on the timeout path, so a
CANCELLATION during the drain (shutdown killing the wipe task, an admin client
dropping) left the lock held forever and every later retain and wipe
deadlocked. Duplicating a readers/writer implementation duplicates the
opportunity to omit that release, so both barriers acquire exclusivity through
:meth:`RwBarrier.acquire_exclusive` and inherit the repair.

Leaf module: no Casa imports, so both sides can import it without the
lazy-import cycle dance ``agent``/``memory_wipe``/``session_saver`` already do.
"""
from __future__ import annotations

import asyncio


class RwBarrier:
    """Shared-side counter + exclusive lock, with the cancellation repair.

    Deliberately NOT a context manager: the two owners need different things
    around the critical section (a generation check, a domain-specific timeout
    exception), so they compose these primitives rather than subclassing a
    context manager and fighting its shape.
    """

    def __init__(self) -> None:
        self._active_shared = 0
        self._no_shared = asyncio.Event()
        self._no_shared.set()
        self._exclusive = asyncio.Lock()

    # --- shared side ------------------------------------------------------

    async def wait_for_exclusive_clear(self) -> None:
        """Block while an exclusive holder has, or is awaiting, the lock.

        Callers run their own admission check (a generation comparison, say)
        AFTER this and BEFORE :meth:`enter_shared`, with no await in between.
        """
        while self._exclusive.locked():
            async with self._exclusive:
                pass  # wait for the holder to finish, then release immediately

    def enter_shared(self) -> None:
        """Join the shared side. Synchronous BY CONTRACT: the increment must
        not follow an await inside the acquire, or a cancellation between the
        two leaves a phantom participant that no exclusive drain can ever
        wait out."""
        self._active_shared += 1
        self._no_shared.clear()

    def exit_shared(self) -> None:
        self._active_shared -= 1
        if self._active_shared <= 0:
            self._active_shared = 0
            self._no_shared.set()

    # --- exclusive side ---------------------------------------------------

    async def acquire_exclusive(self, *, drain_timeout_s: float) -> None:
        """Take exclusivity: block new shared entries, then drain the active
        ones within ``drain_timeout_s``.

        On ANY exceptional exit — the drain timing out, or the caller being
        cancelled mid-drain — the lock is released before the exception
        propagates. That release is the whole reason this class exists; see
        the module docstring. On success the caller owns exclusivity and MUST
        call :meth:`release_exclusive`.

        Raises :class:`asyncio.TimeoutError` on drain timeout; each owner
        translates that into its own domain exception.
        """
        await self._exclusive.acquire()
        try:
            await asyncio.wait_for(
                self._no_shared.wait(), timeout=drain_timeout_s,
            )
        except BaseException:
            self._exclusive.release()
            raise

    def release_exclusive(self) -> None:
        self._exclusive.release()
