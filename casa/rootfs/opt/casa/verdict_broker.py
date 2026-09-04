"""Casa-owned verdict broker — async request/answer registry (W5).

Design ref: docs/current-state-spec.md §W5 broker (Sol B1/B2/B3, r2-B1..r10-B3).

A `PendingRequest` is registered under a `(namespace, scope, request_id)` key
and awaited via a shared `asyncio.Future`. Exactly one of several finishers —
a delivered verdict, a timeout, a logical cancel, or an unregister (delivery
failure) — resolves that future exactly once. `_live` and `_retired` are
separate maps: on finish, the entry moves from `_live` to a `_RetiredEntry`
tombstone in `_retired` for `_RETIRE_S` seconds, so a same-id retry within
that window reattaches to the real outcome instead of creating a duplicate
ask or racing a lost HTTP response.

Two-phase claim/commit exists because the Telegram callback for
`engagement_ask` must persist `interaction_state=authorized` BETWEEN
reserving the winning tap and resolving the awaiting handler's future — see
`claim`/`commit`/`abort_claim` docstrings for the exact ordering guarantees.

Valid namespaces: "permission", "engagement_ask", "resident_ask".
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

_VALID_NAMESPACES = frozenset({"permission", "engagement_ask", "resident_ask"})

# Tombstone retention window (seconds). Tests monkeypatch this module
# attribute directly, so retirement always reads it as `_RETIRE_S` at fire
# time rather than capturing a default at registration.
_RETIRE_S = 60.0

_id_counter = itertools.count()

Key = tuple[str, str, str]


@dataclass
class PendingRequest:
    """A single outstanding (or pre-resolved tombstone) verdict request."""

    namespace: str
    scope: str
    request_id: str
    timeout_s: float
    detached: bool = False
    meta: dict = field(default_factory=dict)

    # internal state — not part of the public contract
    _future: asyncio.Future = field(default=None, repr=False)
    _timer: asyncio.TimerHandle | None = field(default=None, repr=False)
    _id: int = field(default=0, repr=False)
    _deadline: float = field(default=0.0, repr=False)
    _claimed: object | None = field(default=None, repr=False)
    _hook: Callable[[dict], Coroutine[Any, Any, None]] | None = field(default=None, repr=False)
    _hook_fired: bool = field(default=False, repr=False)
    _setup_task: "asyncio.Task | None" = field(default=None, repr=False)

    @property
    def key(self) -> Key:
        return (self.namespace, self.scope, self.request_id)


@dataclass
class _RetiredEntry:
    outcome: dict
    meta: dict
    entry_id: int


class _Claim:
    """Opaque token returned by `claim()`; passed to `commit()`/`abort_claim()`."""

    __slots__ = ("req", "token", "option_index", "actor_id", "option_indices")

    def __init__(
        self, req: PendingRequest, token: object, option_index: int,
        actor_id: int | None, option_indices: "list[int] | None" = None,
    ):
        self.req = req
        self.token = token
        self.option_index = option_index
        self.actor_id = actor_id
        # A5 · F-MULTI: the full sorted selection for a multi-select submit;
        # ``None`` for single-select / permission claims. ``option_index`` still
        # carries the FIRST selected index for downstream single-field compat.
        self.option_indices = option_indices


class VerdictBroker:
    """Casa-owned async request/answer registry.

    Designed for module-level singleton use — see `BROKER` at module bottom.
    """

    def __init__(self) -> None:
        self._live: dict[Key, PendingRequest] = {}
        self._retired: dict[Key, _RetiredEntry] = {}
        self._hook_tasks: set[asyncio.Task] = set()
        self._setup_tasks: set[asyncio.Task] = set()

    # -- registration ------------------------------------------------------

    def register(
        self,
        *,
        namespace: str,
        scope: str,
        request_id: str,
        timeout_s: float,
        detached: bool = False,
        meta: dict | None = None,
        supersede: bool = False,
        require_idle: bool = False,
        idle_scopes: "tuple[str, ...]" = (),
        idle_requires_delivered: bool = False,
    ) -> "tuple[PendingRequest | None, bool]":
        """Register (or reattach to) a request.

        ``require_idle`` (#573) refuses the registration — returning
        ``(None, False)`` — when ANY live request already occupies this
        (namespace, scope) or any of ``idle_scopes``. It is what lets a
        MACHINE-timed question decline the operator's attention lane instead
        of superseding a live human one, and it is decided here because
        ``register`` has no awaits: the check and the insert are one
        indivisible step against the tap handler, a typed-reply cancel and an
        authorization challenge's own registration. ``idle_scopes`` exists
        because that lane is not a single scope — a protected-action challenge
        lives in ``authz:<chat>`` while a plain ask lives in ``dm:<chat>``.

        ``idle_requires_delivered`` (#762) narrows what counts as occupancy to
        a request whose keyboard post has recorded a ``message_id``. It exists
        for ONE caller, ``scheduled_asks.reconcile_at_boot``, because that is
        the one place where a refusal is IRREVERSIBLE: the boot reconciler
        settles the refused record, editing its keyboard to expired and
        dispatching its terminal continuation, so a request that has merely
        registered would destroy a durable question for a keyboard that has not
        landed and may never land (INV-JOB-014). On the LIVE path the same
        refusal costs nothing — a machine question simply is not asked — which
        is why raw live membership stays the default and every other caller
        keeps it. The predicate is decided HERE, in the same no-await block as
        the insert, rather than composed by the caller from a separate read:
        two synchronous calls are equally indivisible today, and a function
        boundary is what stops a later edit putting an await between them.
        """
        if namespace not in _VALID_NAMESPACES:
            raise ValueError(f"invalid namespace: {namespace!r}")
        key: Key = (namespace, scope, request_id)

        if require_idle:
            occupied = {scope, *idle_scopes}
            for k, live in self._live.items():
                if k[0] != namespace or k[1] not in occupied:
                    continue
                if (idle_requires_delivered
                        and live.meta.get("message_id") is None):
                    # Registered, but its keyboard is not on screen. The id is
                    # written in exactly two places: `_run_setup` after a post
                    # returns one, and `scheduled_asks.broker_meta` for a
                    # record restored from disk, whose keyboard is still up
                    # from the previous process. Both are "the operator can see
                    # it"; neither is "someone means to ask".
                    continue
                return None, False

        existing = self._live.get(key)
        if existing is not None:
            return existing, False

        retired = self._retired.get(key)
        if retired is not None:
            tombstone = PendingRequest(
                namespace=namespace, scope=scope, request_id=request_id,
                timeout_s=timeout_s, detached=detached, meta=dict(retired.meta),
            )
            loop = asyncio.get_running_loop()
            tombstone._future = loop.create_future()
            tombstone._future.set_result(dict(retired.outcome))
            tombstone._id = next(_id_counter)
            tombstone._hook_fired = True
            return tombstone, False

        if supersede:
            for other_key in [k for k in self._live if k[0] == namespace and k[1] == scope]:
                self._finish(other_key, {"outcome": "cancelled", "reason": "superseded"})

        loop = asyncio.get_running_loop()
        req = PendingRequest(
            namespace=namespace, scope=scope, request_id=request_id,
            timeout_s=timeout_s, detached=detached, meta=dict(meta or {}),
        )
        req._future = loop.create_future()
        req._id = next(_id_counter)
        req._deadline = loop.time() + timeout_s
        req._timer = loop.call_at(req._deadline, self._on_timeout, key)
        self._live[key] = req
        return req, True

    # -- awaiting ------------------------------------------------------------

    async def await_result(self, req: PendingRequest) -> dict:
        # asyncio.shield: cancelling the awaiter must never cancel the shared
        # future (r3-B1) — a reattacher can still await the real outcome.
        return await asyncio.shield(req._future)

    # -- finishing -------------------------------------------------------

    def _cancel_timer(self, req: PendingRequest) -> None:
        if req._timer is not None:
            req._timer.cancel()
            req._timer = None

    def _finish(self, key: Key, outcome: dict) -> PendingRequest | None:
        req = self._live.pop(key, None)
        if req is None:
            return None
        self._cancel_timer(req)
        if not req._future.done():
            req._future.set_result(outcome)
        entry = _RetiredEntry(outcome=outcome, meta=dict(req.meta), entry_id=req._id)
        self._retired[key] = entry

        loop = asyncio.get_running_loop()

        def _pop_tombstone() -> None:
            if self._retired.get(key) is entry:
                del self._retired[key]

        if _RETIRE_S > 0:
            loop.call_later(_RETIRE_S, _pop_tombstone)
        else:
            # call_later(0, ...) requires TWO event-loop turns to become
            # ready (it still goes through the scheduled-callback heap);
            # call_soon fires on the very next turn, matching tests that
            # assert post-retirement staleness after a single `sleep(0)`.
            loop.call_soon(_pop_tombstone)
        self._fire_hook(req, outcome)
        return req

    def _on_timeout(self, key: Key) -> None:
        self._finish(key, {"outcome": "no_answer"})

    # -- claim / commit ----------------------------------------------------

    @staticmethod
    def _actor_is_bound(req: PendingRequest, actor_id: int | None) -> bool:
        """#469/#374: is *actor_id* the identity this request was bound to at
        registration (``meta["operator_id"]``)?

        Fail-closed on BOTH sides: the bound identity must be a positive int
        (a request registered without one — or with ``None``, the deliberate
        nobody-may-answer binding — is claimable by NO actor), and the actor
        must be that exact int (``bool`` is excluded: it is an ``int``
        subclass and ``True == 1`` would let a truthy flag impersonate user
        id 1). Absence is not consent — a register site that forgets the
        binding gets an unanswerable request, never an open one.
        """
        expected = req.meta.get("operator_id")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected <= 0:
            return False
        return (
            isinstance(actor_id, int)
            and not isinstance(actor_id, bool)
            and actor_id == expected
        )

    def claim(
        self, *, namespace: str, scope: str, request_id: str,
        option_index: int, actor_id: int | None,
        option_indices: "list[int] | None" = None,
    ) -> "_Claim | str":
        key: Key = (namespace, scope, request_id)
        req = self._live.get(key)
        if req is not None:
            # #469: the broker is the enforcement home for verdict authority —
            # actor binding is checked HERE, not only at the Telegram tap, so
            # no future delivery path can resolve a live request with an
            # unbound actor. Checked BEFORE the duplicate branch: a forbidden
            # actor learns nothing about claim state.
            if not self._actor_is_bound(req, actor_id):
                return "forbidden"
            if req._claimed is not None:
                return "duplicate"
            token = object()
            req._claimed = token
            self._cancel_timer(req)
            return _Claim(req, token, option_index, actor_id, option_indices)

        retired = self._retired.get(key)
        if retired is not None:
            return "duplicate" if retired.outcome.get("outcome") == "answered" else "stale"

        return "stale"

    def commit(self, claim: "_Claim") -> bool:
        req = claim.req
        if req._claimed is not claim.token or req._future.done():
            return False
        key = req.key
        outcome = {
            "outcome": "answered",
            "option_index": claim.option_index,
            "actor_id": claim.actor_id,
        }
        # A5 · F-MULTI: carry the full selection when this is a multi-select
        # submit (single-select outcomes stay byte-identical to pre-A5).
        if claim.option_indices is not None:
            outcome["option_indices"] = list(claim.option_indices)
        self._finish(key, outcome)
        return True

    def abort_claim(self, claim: "_Claim") -> None:
        req = claim.req
        if req._claimed is not claim.token or req._future.done():
            return
        req._claimed = None
        loop = asyncio.get_running_loop()
        remaining = req._deadline - loop.time()
        key = req.key
        if remaining <= 0:
            self._finish(key, {"outcome": "no_answer"})
            return
        req._timer = loop.call_later(remaining, self._on_timeout, key)

    # -- A5 · F-MULTI multi-select selection --------------------------------

    def toggle_selection(
        self, *, namespace: str, scope: str, request_id: str, idx: int,
    ) -> "list[int] | None":
        """Flip ``idx`` in a LIVE, UNCLAIMED request's ``meta["selected"]``
        (A5 · F-MULTI, Sol r1-7/r2-7 — broker-owned atomic toggle).

        Synchronous. Returns the NEW sorted selection list on success, or
        ``None`` when the request is retired / claimed / absent (the caller
        toasts "expired"). A terminal outcome that resolved before this call
        yields ``None`` — the finish hook stays the sole TERMINAL writer, and
        the caller's revalidated redraw is skipped."""
        req = self._live.get((namespace, scope, request_id))
        if req is None or req._claimed is not None:
            return None
        selected: list[int] = req.meta.setdefault("selected", [])
        if idx in selected:
            selected.remove(idx)
        else:
            selected.append(idx)
        selected.sort()
        return list(selected)

    def is_live_unclaimed(
        self, *, namespace: str, scope: str, request_id: str,
    ) -> bool:
        """True iff the request is still live in the broker AND unclaimed — the
        A5 toggle-redraw revalidation predicate (run under the sequencer lock
        immediately before the wire edit; a terminal outcome in between declines
        the redraw)."""
        req = self._live.get((namespace, scope, request_id))
        return req is not None and req._claimed is None

    # -- meta / introspection ---------------------------------------------

    def get_meta(self, *, namespace: str, scope: str, request_id: str) -> dict | None:
        key: Key = (namespace, scope, request_id)
        req = self._live.get(key)
        if req is not None:
            return req.meta
        retired = self._retired.get(key)
        if retired is not None:
            return retired.meta
        return None

    def pending(self, *, namespace: str, scope: str) -> list[str]:
        return [
            k[2] for k in self._live
            if k[0] == namespace and k[1] == scope
        ]

    # -- unregister / cancel ------------------------------------------------

    def unregister(self, *, namespace: str, scope: str, request_id: str) -> None:
        key: Key = (namespace, scope, request_id)
        req = self._live.pop(key, None)
        if req is None:
            return
        self._cancel_timer(req)
        if not req._future.done():
            req._future.set_result({"outcome": "delivery_failed"})
        # No tombstone: a genuine retry must be able to re-register fresh.

    def cancel(self, *, namespace: str, scope: str, request_id: str, reason: str) -> bool:
        key: Key = (namespace, scope, request_id)
        if key not in self._live:
            return False
        self._finish(key, {"outcome": "cancelled", "reason": reason})
        return True

    def cancel_scope(self, *, namespace: str, scope: str, reason: str) -> int:
        keys = [k for k in self._live if k[0] == namespace and k[1] == scope]
        for key in keys:
            self._finish(key, {"outcome": "cancelled", "reason": reason})
        return len(keys)

    def cancel_where(
        self, *, namespace: str, reason: str,
        predicate: Callable[[PendingRequest], bool],
    ) -> list[str]:
        """Cancel every LIVE request in *namespace* for which *predicate* holds.

        Synchronous, like every other finisher, and that is the point (#573):
        a caller deciding "cancel the machine-raised questions in this DM" must
        decide and act in ONE no-await block, against the live map itself.
        Selecting them from a durable record file instead loses to any ask that
        has registered but not yet persisted. Returns the cancelled request ids.
        """
        keys = [
            k for k, req in self._live.items()
            if k[0] == namespace and predicate(req)
        ]
        for key in keys:
            self._finish(key, {"outcome": "cancelled", "reason": reason})
        return [k[2] for k in keys]

    def cancel_all(self, *, reason: str) -> int:
        keys = list(self._live.keys())
        for key in keys:
            self._finish(key, {"outcome": "cancelled", "reason": reason})
        return len(keys)

    # -- finish hooks (broker-owned keyboard edit lifecycle) ----------------

    def set_finish_hook(
        self, req: PendingRequest,
        coro_factory: Callable[[dict], Coroutine[Any, Any, None]],
    ) -> None:
        req._hook = coro_factory
        if req._future.done() and not req._hook_fired:
            self._fire_hook(req, req._future.result())

    def _fire_hook(self, req: PendingRequest, outcome: dict) -> None:
        if req._hook_fired or req._hook is None:
            return
        req._hook_fired = True
        loop = asyncio.get_running_loop()
        task = loop.create_task(req._hook(outcome))
        self._hook_tasks.add(task)
        task.add_done_callback(self._on_hook_task_done)

    def _on_hook_task_done(self, task: asyncio.Task) -> None:
        self._hook_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception("verdict_broker: finish hook raised", exc_info=exc)

    async def drain_hooks(self) -> None:
        # Two-phase, setup-then-hooks. A completed setup task installs its
        # finish hook synchronously (in _run_setup, before it returns), so by
        # the time a setup task is done its hook already lives in _hook_tasks —
        # hence draining all setup tasks first, then hooks.
        #
        # 3.12+ livelock guard: on Python 3.12+ `await asyncio.gather(*tasks)`
        # completes EAGERLY WITHOUT YIELDING when every task is already done.
        # Our done-callbacks (`set.discard`) only run on a loop turn, so a
        # done-but-undiscarded task would keep the set non-empty forever and a
        # `while set: await gather(set)` loop would tight-spin, starving the
        # whole event loop (even same-loop timers never fire). Fix: sync-discard
        # already-done tasks BEFORE gathering, and only await genuinely-pending
        # ones (gather over a not-yet-done task always yields, terminating).
        while True:
            pending_setup = []
            for t in list(self._setup_tasks):
                if t.done():
                    self._setup_tasks.discard(t)
                else:
                    pending_setup.append(t)
            if pending_setup:
                results = await asyncio.gather(*pending_setup, return_exceptions=True)
                for r in results:
                    if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                        logger.exception("verdict_broker: setup task raised", exc_info=r)
                await asyncio.sleep(0)  # belt-and-suspenders yield
                continue  # re-drain setup (a completion may have added hooks)

            pending_hooks = []
            for t in list(self._hook_tasks):
                if t.done():
                    self._hook_tasks.discard(t)
                else:
                    pending_hooks.append(t)
            if pending_hooks:
                results = await asyncio.gather(*pending_hooks, return_exceptions=True)
                for r in results:
                    if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                        logger.exception("verdict_broker: finish hook raised", exc_info=r)
                await asyncio.sleep(0)  # belt-and-suspenders yield
                continue

            # Nothing pending in either set. If both are empty we're done;
            # otherwise a done-but-undiscarded task slipped in between the
            # snapshots — yield once to let its discard callback run, re-check.
            if not self._setup_tasks and not self._hook_tasks:
                return
            await asyncio.sleep(0)

    # -- keyboard-post lifecycle ---------------------------------------------

    async def ensure_posted(
        self, req: PendingRequest,
        post_coro_factory: Callable[[], Coroutine[Any, Any, Any]],
        finish_coro_factory: Callable[[int], Callable[[dict], Coroutine[Any, Any, None]]],
    ) -> None:
        if req._future.done() and req._setup_task is None:
            # Retired-key tombstone reattach: nothing to post.
            return

        if req._setup_task is None:
            req._setup_task = asyncio.ensure_future(
                self._run_setup(req, post_coro_factory, finish_coro_factory)
            )
            self._setup_tasks.add(req._setup_task)
            req._setup_task.add_done_callback(self._setup_tasks.discard)

        await asyncio.shield(req._setup_task)

    async def _run_setup(
        self, req: PendingRequest,
        post_coro_factory: Callable[[], Coroutine[Any, Any, Any]],
        finish_coro_factory: Callable[[int], Callable[[dict], Coroutine[Any, Any, None]]],
    ) -> None:
        try:
            mid = await post_coro_factory()
        except Exception:
            logger.exception("verdict_broker: keyboard post raised")
            self.unregister(namespace=req.namespace, scope=req.scope, request_id=req.request_id)
            return
        if not isinstance(mid, int):
            # r10-B3: both keyboard APIs return int | None; None means the
            # engagement/topic couldn't be resolved — treat as delivery failure.
            self.unregister(namespace=req.namespace, scope=req.scope, request_id=req.request_id)
            return
        req.meta["message_id"] = mid
        self.set_finish_hook(req, finish_coro_factory(mid))


BROKER = VerdictBroker()
