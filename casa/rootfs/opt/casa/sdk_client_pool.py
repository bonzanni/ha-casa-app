"""Warm SDK-client pool for resident turns (spec 2026-07-11, AR-1..AR-10).

One ``ManagedSdkClient`` == one live conversation (subprocess + MCP
handshake kept warm across turns). ``SdkClientPool`` caches at most one
per ``channel_key`` per Agent, reconciled against the SessionRegistry —
the registry stays the sole source of truth for which conversation a key
is on; the pool only caches a live client *for* that conversation.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, NamedTuple

logger = logging.getLogger(__name__)


class _CidBox:
    """Mutable cid holder. Bound into ``log_cid.cid_var`` in the client's
    connect context so log records created inside the SDK read task carry
    the *current turn's* cid (the read task snapshots contextvars at
    connect — F7); ``run_turn_locked`` rewrites ``value`` per turn."""

    def __init__(self) -> None:
        self.value = "-"

    def __str__(self) -> str:
        return self.value


class SdkTurnError(Exception):
    """CLI returned an is_error ResultMessage whose text classifies as a
    retryable fault (AR-5). Message text == the result text so
    error_kinds._classify_error routes it to RETRY_KINDS."""


class PoolUnavailable(Exception):
    """Pool is closing or the entry vanished twice — caller must run the
    turn on the per-turn bypass path instead (AR-7)."""


def _default_make_client(options):
    from claude_agent_sdk import ClaudeSDKClient
    return ClaudeSDKClient(options)


class ManagedSdkClient:
    """One warm conversation: a live ClaudeSDKClient + the contextvar
    bindings its read task snapshotted at connect (F7).

    Locking contract: callers hold ``self.lock`` around
    ``run_turn_locked`` (the pool does; the bypass path does too).
    ``open()``/``aclose()`` manage their own consistency.
    """

    def __init__(self, options, *, origin_ctxvar, cid_ctxvar,
                 engagement_ctxvar, make_client=None,
                 monotonic=time.monotonic, binding_digest: str = "") -> None:
        self.options = options
        self._origin_ctxvar = origin_ctxvar
        self._cid_ctxvar = cid_ctxvar
        self._engagement_ctxvar = engagement_ctxvar
        self._make_client = make_client or _default_make_client
        self._monotonic = monotonic
        # Task 9: the identity this warm conversation was built for. A warm
        # client is only reusable when the incoming turn's binding_digest
        # matches (defense-in-depth on top of the registry-level resume gate).
        # Never mutated to a new identity in place — an identity change forces
        # a fresh ManagedSdkClient.
        self.binding_digest = binding_digest
        self.origin_holder: dict = {}
        self.cid_box = _CidBox()
        self.lock = asyncio.Lock()
        self.state = "new"
        self.sid: str | None = None
        self.created_at = monotonic()
        self.last_used = monotonic()
        self._client: Any = None

    async def open(self) -> None:
        """Connect under a context that binds origin/cid holders.

        The SDK's read task is created inside connect() and snapshots the
        ambient context (F7) — so the holders MUST be bound before the
        connect call, and engagement_var MUST be None (resident turns
        never run inside an engagement binding — spec Q7)."""
        assert self._engagement_ctxvar.get(None) is None, (
            "ManagedSdkClient.open() inside an engagement binding"
        )
        assert self.state == "new", f"open() on state={self.state}"
        self._client = self._make_client(self.options)

        ctx = contextvars.copy_context()

        def _bind() -> None:
            self._origin_ctxvar.set(self.origin_holder)
            self._cid_ctxvar.set(self.cid_box)

        ctx.run(_bind)
        # Run connect inside the prepared context so the read task
        # inherits the holder bindings.
        task = asyncio.get_running_loop().create_task(
            self._connect(), context=ctx,
        )
        try:
            await task
        except BaseException:
            # Finding 3 (final-review): don't leave self._client set with no
            # disconnect on a failed connect — best-effort disconnect + null
            # it via the same path a mid-turn failure uses.
            await self._invalidate()
            raise
        self.state = "warm"

    async def _connect(self) -> None:
        connect = getattr(self._client, "connect", None)
        if connect is not None:
            await connect()
        else:  # pragma: no cover — ClaudeSDKClient always has connect()
            await self._client.__aenter__()

    async def run_turn_locked(
        self, prompt: str, *, origin: dict, cid: str,
        on_message: Callable[[Any], Awaitable[None]],
    ) -> str | None:
        """Run one turn on the warm client. Caller holds ``self.lock``.

        Rewrites the origin holder + cid box IN PLACE (read-task
        visibility — spec Q7), queries, iterates receive_response()
        forwarding every message to ``on_message``, captures the sid,
        and enforces the AR-5 error-result and AR-1 cancellation
        contracts."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage

        from error_kinds import ApiErrorTurn, ErrorKind, api_error_kind

        assert self.state == "warm", f"run_turn on state={self.state}"
        self.state = "in_turn"
        self.origin_holder.clear()
        self.origin_holder.update(origin)
        self.cid_box.value = cid or "-"
        self.last_used = self._monotonic()
        result_msg = None
        api_error: ErrorKind | None = None
        try:
            await self._client.query(prompt)
            async for sdk_msg in self._client.receive_response():
                if isinstance(sdk_msg, SystemMessage):
                    if getattr(sdk_msg, "subtype", None) == "init":
                        data = getattr(sdk_msg, "data", {}) or {}
                        if "session_id" in data:
                            self.sid = data["session_id"]
                elif isinstance(sdk_msg, AssistantMessage):
                    # #568: the CLI reports an API-level fault (a refusal
                    # included) as an ordinary assistant message carrying its
                    # own error prose. Only a MAIN-LOOP message is the turn's
                    # verdict — one scoped to a sub-agent
                    # (``parent_tool_use_id`` set) is that sub-agent's
                    # problem, and the turn can still answer. First one wins:
                    # the fault that ended the turn is the earliest, not the
                    # last thing on the wire.
                    if (
                        api_error is None
                        and getattr(sdk_msg, "parent_tool_use_id", None) is None
                    ):
                        api_error = api_error_kind(sdk_msg)
                elif isinstance(sdk_msg, ResultMessage):
                    result_msg = sdk_msg
                    s = getattr(sdk_msg, "session_id", None)
                    if s:
                        self.sid = s
                await on_message(sdk_msg)
        except asyncio.CancelledError:
            await self._cleanup_after_cancel()
            raise
        except BaseException:
            await self._invalidate()
            raise
        self.last_used = self._monotonic()
        # #568: the result's own stop_reason is the second carrier — read so a
        # refusal reported ONLY there (no synthesized assistant message) still
        # ends the turn honestly rather than as an empty success.
        if api_error is None and getattr(
            result_msg, "stop_reason", None,
        ) == "refusal":
            api_error = ErrorKind.REFUSAL
        # #568: raise BEFORE the AR-5 text classification below — this carrier
        # NAMES the fault, where AR-5 can only pattern-match the result prose.
        # Raising here (rather than after ``turn()`` returns) is what keeps the
        # refused session from being published and the entry from going back to
        # warm: the CLI's own refusal advice is to start a new session.
        if api_error is not None:
            logger.info("turn ended by api error kind=%s", api_error.value)
            await self._invalidate()
            raise ApiErrorTurn(api_error)
        # AR-5: never leave an error-result entry warm; raise retryables.
        if result_msg is not None and getattr(result_msg, "is_error", False):
            await self._invalidate()
            text = str(getattr(result_msg, "result", "") or "")
            from error_kinds import _classify_error
            from retry import RETRY_KINDS
            if _classify_error(SdkTurnError(text)) in RETRY_KINDS:
                raise SdkTurnError(text)
            self.state = "invalid"  # non-retryable: surfaced text, dead entry
            return self.sid
        self.state = "warm"
        return self.sid

    async def _cleanup_after_cancel(self) -> None:
        """AR-1/AR-10: interrupt (≤2 s) then drain the aborted turn's
        buffered messages through its ResultMessage (≤5 s), shielded from
        further cancellation; ANY failure (incl. a second CancelledError)
        invalidates instead of returning the entry to warm."""
        try:
            await asyncio.shield(
                asyncio.wait_for(self._interrupt_and_drain(), timeout=7.0)
            )
            self.state = "warm"
        except BaseException:  # noqa: BLE001 — second cancel included (AR-10)
            await self._invalidate()

    async def _interrupt_and_drain(self) -> None:
        from claude_agent_sdk import ResultMessage
        await asyncio.wait_for(self._client.interrupt(), timeout=2.0)
        async for sdk_msg in self._client.receive_response():
            if isinstance(sdk_msg, ResultMessage):
                s = getattr(sdk_msg, "session_id", None)
                if s:
                    self.sid = s
                return

    async def _invalidate(self) -> None:
        self.state = "invalid"
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning("pool client disconnect failed: %s", exc)

    async def aclose(self) -> None:
        if self.state == "closed":
            return
        client, self._client = self._client, None
        self.state = "closed"
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.warning("pool client close failed: %s", exc)


def _env_int(name: str, default: int, *, min_value: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default
    return max(value, min_value)


class PoolTurnResult(NamedTuple):
    sid: str | None
    resume_sid: str | None
    is_fresh: bool


class SdkClientPool:
    """Per-Agent cache of warm conversation clients, keyed by channel_key.

    The SessionRegistry stays authoritative: the resume decision is derived
    INSIDE turn(), under the entry's serialization (AR-3), from a fresh
    registry read — so /new, freshness expiry, sid drift, and interleaved
    turns can never fork a conversation or reuse a stale client."""

    _instances: "list[SdkClientPool]" = []   # fleet accounting (Task 6)

    def __init__(self, session_registry, *, decide, origin_ctxvar, cid_ctxvar,
                 engagement_ctxvar, freshness=None, make_client=None,
                 monotonic=time.monotonic, wall_now=None) -> None:
        if freshness is None:
            from session_saver import freshness_window as freshness
        self._registry = session_registry
        self._decide = decide
        self._origin_ctxvar = origin_ctxvar
        self._cid_ctxvar = cid_ctxvar
        self._engagement_ctxvar = engagement_ctxvar
        self._freshness = freshness
        self._make_client = make_client
        self._monotonic = monotonic
        self._wall_now = wall_now or (lambda: datetime.now(timezone.utc))
        self._entries: dict[str, ManagedSdkClient] = {}
        self._invalidation_barriers: dict[
            str, asyncio.Future[None]
        ] = {}
        self._invalidation_groups: set[asyncio.Future[Any]] = set()
        # #319: per-key in-flight invalidation close workers. close_key (the
        # /new reset hook) awaits these so a reset joins the old generation's
        # FULL close — the disconnect is what flushes the transcript (AR-4) —
        # not just its turn-lock handoff barrier. A set per key tolerates
        # overlapping invalidate_all() generations.
        self._invalidation_closes: dict[str, set[asyncio.Task]] = {}
        self._pool_lock = asyncio.Lock()
        self._closing = False
        self._sweeper: asyncio.Task | None = None
        self.max_per_agent = _env_int("SDK_POOL_MAX_PER_AGENT", 4)
        self.idle_seconds = float(_env_int("SDK_POOL_IDLE_SECONDS", 1800))
        self.max_age_seconds = float(_env_int("SDK_POOL_MAX_AGE_SECONDS", 43200))
        SdkClientPool._instances.append(self)

    def stats(self) -> dict:
        return {"entries": len(self._entries), "closing": self._closing}

    async def turn(self, *, channel_key: str, channel: str, prompt: str,
                   origin: dict, cid: str, build_options, on_stale_old,
                   on_message,
                   on_success: Callable[[str], Awaitable[None]] | None = None,
                   on_decision: Callable[[str | None, bool, int | None], None] | None = None,
                   binding_digest: str = "",
                   ) -> PoolTurnResult:
        """Run one serialized turn and publish its returned session id.

        When provided, ``on_success`` is awaited exactly once when the SDK
        turn returns a non-None session id. It runs while the
        per-key entry lock is still held so publication completes before
        turn/invalidation handoff. The callback must therefore be fast and
        non-reentrant into this pool (a same-key turn would deadlock).
        Callback exceptions and cancellation propagate and drop the
        unpublished client generation.
        """
        self._ensure_sweeper()
        for _attempt in (1, 2):                      # AR-7: one silent retry
            if self._closing:
                raise PoolUnavailable("pool closing")
            entry = await self._entry_stub(channel_key)
            result: PoolTurnResult | None = None
            async with entry.lock:
                if self._entries.get(channel_key) is not entry:
                    continue                          # replaced/evicted; retry
                if entry.state not in ("new", "warm"):
                    async with self._pool_lock:
                        if self._entries.get(channel_key) is entry:
                            del self._entries[channel_key]
                    continue
                # --- decision UNDER the entry lock (AR-3) ---
                reg_entry = self._registry.get(channel_key)
                # #526: the registration generation belonging to exactly this
                # entry snapshot — same no-await block, so a caller's guarded
                # clear can tell a later (even same-sid) re-registration apart.
                reg_generation = self._registry.generation(channel_key)
                decision = self._decide(
                    channel, reg_entry, self._wall_now(),
                )
                # #290/#411: while a reset/wipe holds a retirement claim on
                # this key, steer the turn to a FRESH session instead of the
                # dying sid, and DON'T retain the old snapshot here — the
                # retiring caller owns that retain (a second one would only
                # be duplicate work). Checked in the pool, not the decide
                # callback, so no pool user can exist without the steer;
                # duck-typed so registry fakes without claims are unaffected.
                _retiring = getattr(self._registry, "retirement_pending", None)
                if _retiring is not None and _retiring(channel_key):
                    decision = dataclasses.replace(
                        decision, action="new", resume_sid=None,
                        retain_old=False, old=None, reason="retiring",
                    )
                resume_sid = (
                    decision.resume_sid if decision.action == "resume" else None
                )
                is_fresh = resume_sid is None
                # Finding 2 (final-review): record the decision even when the
                # turn is a warm reuse (which skips build_options / _build
                # below) — otherwise a caller-side "last resume sid" tracker
                # fed only from build_options misses warm-reuse turns, and a
                # non-retryable failure on one can't tell the stale-resume
                # fallback which sid was in play.
                if on_decision is not None:
                    on_decision(resume_sid, is_fresh, reg_generation)
                # Task 9: a warm client is reusable only when the incoming
                # turn's binding_digest matches the identity it was built for —
                # defense-in-depth on top of the registry-level resume gate.
                reusable = (
                    entry.state == "warm"
                    and not is_fresh
                    and entry.sid == resume_sid
                    and entry.binding_digest == binding_digest
                )
                if not reusable:
                    if entry.state == "warm":
                        await entry.aclose()          # flush BEFORE retain (AR-4)
                    # Task 9: hand the resume gate's own immutable snapshot to
                    # the retain callback (sourced from the registry read under
                    # the entry lock, AR-3) — never a sid reconstructed from the
                    # pool's own possibly-stale ``entry.sid``. #411: the second
                    # argument is the retain-fence generation the DECISION was
                    # made under (captured by the decide callback in the same
                    # no-await block as the registry read) — the aclose() await
                    # above sits between decision and this call, and a memory
                    # wipe completing inside it must make the spawned retain
                    # discard rather than restore pre-wipe content.
                    if decision.retain_old and decision.old is not None:
                        on_stale_old(
                            decision.old,
                            getattr(decision, "fence_generation", None),
                        )
                    options = await build_options(is_fresh, resume_sid)
                    fresh_client = ManagedSdkClient(
                        options,
                        origin_ctxvar=self._origin_ctxvar,
                        cid_ctxvar=self._cid_ctxvar,
                        engagement_ctxvar=self._engagement_ctxvar,
                        make_client=self._make_client or _default_make_client,
                        monotonic=self._monotonic,
                        binding_digest=binding_digest,
                    )
                    fresh_client.lock = entry.lock    # keep the held lock
                    fresh_client.sid = resume_sid
                    connect_started_ms = self._monotonic() * 1000
                    await fresh_client.open()
                    # Finding 4 (final-review): one INFO per successful cold
                    # connect; warm reuse stays silent (hot path).
                    logger.info(
                        "pool cold connect key=%s resume=%s ms=%d",
                        channel_key,
                        bool(resume_sid),
                        int(self._monotonic() * 1000 - connect_started_ms),
                    )
                    async with self._pool_lock:
                        self._entries[channel_key] = fresh_client
                    entry = fresh_client
                if decision.action == "resume":
                    await self._registry.touch(channel_key)
                publishing = False
                try:
                    sid = await entry.run_turn_locked(
                        prompt, origin=origin, cid=cid, on_message=on_message,
                    )
                    if sid is not None and on_success is not None:
                        # Publish the result before the entry lock can hand off
                        # to another turn or an invalidation generation.
                        publishing = True
                        publish_started_ms = self._monotonic() * 1000
                        publish_ok = False
                        try:
                            await on_success(sid)
                            publish_ok = True
                        finally:
                            logger.info(
                                "pool session publish ok=%s ms=%d",
                                publish_ok,
                                int(
                                    self._monotonic() * 1000
                                    - publish_started_ms
                                ),
                            )
                        publishing = False
                except asyncio.CancelledError:
                    if publishing or entry.state != "warm":
                        # A warm client whose sid was not successfully
                        # published is unsafe to reuse. Cancellation inside
                        # run_turn_locked retains its existing warm-reuse
                        # behavior when the SDK interrupt/drain succeeds.
                        await self._drop(channel_key, entry)
                    raise
                except BaseException:
                    await self._drop(channel_key, entry)
                    raise
                if entry.state != "warm":             # AR-5 non-retryable path
                    await self._drop(channel_key, entry)
                result = PoolTurnResult(sid=sid, resume_sid=resume_sid,
                                        is_fresh=is_fresh)
            if result is not None:
                await self._enforce_caps(channel_key)  # outside entry.lock
                return result
        raise PoolUnavailable("entry unstable after retry")

    async def _entry_stub(self, channel_key: str) -> ManagedSdkClient:
        while True:
            async with self._pool_lock:
                if self._closing:
                    raise PoolUnavailable("pool closing")
                barrier = self._invalidation_barriers.get(channel_key)
                if barrier is None:
                    entry = self._entries.get(channel_key)
                    if entry is None:
                        entry = ManagedSdkClient(
                            None,
                            origin_ctxvar=self._origin_ctxvar,
                            cid_ctxvar=self._cid_ctxvar,
                            engagement_ctxvar=self._engagement_ctxvar,
                            make_client=(
                                self._make_client or _default_make_client
                            ),
                            monotonic=self._monotonic,
                        )
                        self._entries[channel_key] = entry
                    return entry
            # Never hold the global pool lock while an old same-key turn
            # drains. Other keys remain free to construct/reuse entries. The
            # Future is shared, so one cancelled waiter must not cancel the
            # handoff signal for every other waiter.
            await asyncio.shield(barrier)

    async def _release_invalidation_barrier(
        self,
        channel_key: str,
        barrier: asyncio.Future[None],
    ) -> None:
        """Resolve only the invalidation generation that owns ``barrier``."""
        release = False
        async with self._pool_lock:
            if self._invalidation_barriers.get(channel_key) is barrier:
                del self._invalidation_barriers[channel_key]
                release = True
        if release and not barrier.done():
            barrier.set_result(None)

    async def _drop(self, channel_key: str, entry: ManagedSdkClient) -> None:
        """Generation-checked invalidate: only removes THIS entry object.

        Locking contract: caller already holds ``entry.lock`` (the only
        callers are the in-turn failure/non-retryable paths inside
        ``turn()``) — this must NOT try to acquire it again, that would
        deadlock against itself."""
        async with self._pool_lock:
            if self._entries.get(channel_key) is entry:
                del self._entries[channel_key]
        await entry.aclose()

    async def _evict(self, channel_key: str, entry: ManagedSdkClient) -> None:
        """Generation-checked pop + lock-guarded close (AR-7).

        Locking contract: caller must NOT hold ``entry.lock`` — this is
        the eviction path for callers outside the entry's own turn (LRU
        cap enforcement, idle/max-age sweep). It removes the entry from
        the dict under the pool lock first (so no new turn can pick it
        up), then acquires the entry's own lock before closing — this
        blocks until any in-flight turn holding the lock (e.g. the
        warm-window between resume-touch and run_turn_locked flipping
        the state to "in_turn") has released it, so a close can never
        race a turn already using this entry."""
        async with self._pool_lock:
            if self._entries.get(channel_key) is not entry:
                return
            del self._entries[channel_key]
        async with entry.lock:
            await entry.aclose()

    def _discard_invalidation_close(self, key: str, task: asyncio.Task) -> None:
        tasks = self._invalidation_closes.get(key)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                del self._invalidation_closes[key]

    async def close_key(self, channel_key: str) -> None:
        """AR-4 reset-hook target: close (flush) the key's warm client.

        #319: joins any in-flight ``invalidate_all()`` generation for this key
        before returning. The old contract popped the entry and returned
        instantly when invalidation had already removed it — so a /new reset
        could save/remove the session BEFORE the old client's turn ended and
        its transport closed (the disconnect is what flushes the transcript,
        AR-4), letting the finishing turn resurrect the just-reset session.
        Unlike a replacement turn (which only waits for the lock-handoff
        barrier), a reset waits for the old generation's close to COMPLETE."""
        while True:
            closing = tuple(self._invalidation_closes.get(channel_key) or ())
            if closing:
                # wait() never cancels the shared close workers, and the
                # shield keeps a cancelled caller's CancelledError from
                # propagating into them. It does NOT make a cancelled caller
                # wait out the flush — cancellation aborts close_key
                # immediately, which aborts the whole reset (reset_channel
                # reads the transcript only after this returns), so an
                # unflushed transcript is never read on that path.
                await asyncio.shield(asyncio.wait(closing))
                continue
            async with self._pool_lock:
                if self._invalidation_barriers.get(channel_key) is None:
                    entry = self._entries.pop(channel_key, None)
                    break
            # A barrier without a close worker is only the synchronous window
            # inside invalidate_all(); loop back and pick up its close task.
            await asyncio.sleep(0)
        if entry is not None:
            async with entry.lock:
                await entry.aclose()

    async def invalidate_all(self) -> None:
        """Drop the current entry generation without shutting down the pool.

        A per-key handoff barrier prevents a replacement client from starting
        while the removed generation still owns its turn lock. The barrier is
        released as soon as that lock transfers to this invalidation, before
        transport close, so a slow disconnect stays off the new turn's path.
        """
        async with self._pool_lock:
            entries = dict(self._entries)
            self._entries.clear()
            loop = asyncio.get_running_loop()
            barriers = {}
            for key in entries:
                barrier = loop.create_future()
                self._invalidation_barriers[key] = barrier
                barriers[key] = barrier

        async def _close(
            key: str,
            entry: ManagedSdkClient,
            barrier: asyncio.Future[None],
        ) -> None:
            try:
                async with entry.lock:
                    # The prior turn has ended. Allow the replacement to
                    # connect now; it need not wait for a slow disconnect.
                    await self._release_invalidation_barrier(key, barrier)
                    await entry.aclose()
            finally:
                # Cancellation/close races must never strand future turns.
                await asyncio.shield(
                    self._release_invalidation_barrier(key, barrier)
                )

        # #319: register each close worker per key so close_key can join the
        # full flush. Created synchronously after the barrier install above
        # (no suspension between), so a reset that saw the barrier always
        # finds the close task on its next loop iteration.
        close_tasks = []
        for key, entry in entries.items():
            task = asyncio.create_task(_close(key, entry, barriers[key]))
            self._invalidation_closes.setdefault(key, set()).add(task)
            task.add_done_callback(
                lambda t, _key=key: self._discard_invalidation_close(_key, t)
            )
            close_tasks.append(task)
        close_group = asyncio.gather(*close_tasks, return_exceptions=True)
        # Caller cancellation must not cancel the lock-handoff workers: doing
        # so would either strand the barrier or release it while the old turn
        # still owns the lock. Keep the shielded group strongly referenced
        # until every old generation has transferred ownership and closed.
        self._invalidation_groups.add(close_group)
        close_group.add_done_callback(self._invalidation_groups.discard)
        await asyncio.shield(close_group)

    async def aclose(self, *, drain_timeout: float = 120.0) -> None:
        self._closing = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None
        async with self._pool_lock:
            entries = dict(self._entries)
            self._entries.clear()
            barriers = list(self._invalidation_barriers.values())
            self._invalidation_barriers.clear()
            invalidation_groups = tuple(self._invalidation_groups)
        # Wake same-key waiters only after closing is visible. Their next
        # _entry_stub loop raises PoolUnavailable instead of constructing a
        # post-shutdown generation.
        for barrier in barriers:
            if not barrier.done():
                barrier.set_result(None)
        for key, entry in entries.items():
            try:
                await asyncio.wait_for(entry.lock.acquire(), timeout=drain_timeout)
                try:
                    await entry.aclose()
                finally:
                    entry.lock.release()
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning("pool aclose: drain timeout on %s; force close", key)
                await entry.aclose()
        if invalidation_groups:
            # These workers own entries already removed from _entries, so
            # normal shutdown must drain them too. Shield preserves their
            # lock-handoff invariant if an outer shutdown timeout cancels us.
            await asyncio.shield(asyncio.gather(
                *invalidation_groups,
                return_exceptions=True,
            ))
        if self in SdkClientPool._instances:
            SdkClientPool._instances.remove(self)

    def _channel_of(self, channel_key: str) -> str:
        return channel_key.partition("-")[0]

    async def _enforce_caps(self, protect: str) -> None:
        """Caller holds no locks. LRU-close overage; never the protected key."""
        fleet_cap = _env_int("SDK_POOL_FLEET_CAP", 8)

        def _lru(pools):
            candidates = [
                (e.last_used, p, k, e)
                for p in pools
                for k, e in p._entries.items()
                if e.state == "warm" and not (p is self and k == protect)
            ]
            return min(candidates, key=lambda c: c[0], default=None)

        while len(self._entries) > self.max_per_agent:
            victim = _lru([self])
            if victim is None:
                break
            _, p, k, e = victim
            logger.info("pool cap: LRU-closing %s", k)
            await p._evict(k, e)
        while sum(len(p._entries) for p in SdkClientPool._instances) > fleet_cap:
            victim = _lru(SdkClientPool._instances)
            if victim is None:
                break
            _, p, k, e = victim
            logger.info("fleet cap: LRU-closing %s", k)
            await p._evict(k, e)

    async def _sweep_once(self) -> None:
        now = self._monotonic()
        doomed: list[tuple[str, ManagedSdkClient]] = []
        async with self._pool_lock:
            for key, e in list(self._entries.items()):
                if e.state != "warm":
                    continue
                bound = min(
                    self._freshness(self._channel_of(key)).total_seconds(),
                    self.idle_seconds,
                )
                if (now - e.last_used) > bound or \
                        (now - e.created_at) > self.max_age_seconds:
                    doomed.append((key, e))
        for key, e in doomed:
            logger.info("pool sweep: closing %s (idle/max-age)", key)
            await self._evict(key, e)
        if doomed:
            # Finding 4 (final-review): post-sweep depth, so an operator can
            # see the pool actually shrank without instrumenting further.
            logger.info("pool depth=%d", len(self._entries))

    async def _run_sweeper(self, interval: float = 60.0) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self._sweep_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("pool sweep failed; retrying next tick")
        except asyncio.CancelledError:
            return

    def _ensure_sweeper(self) -> None:
        if self._closing:
            return
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(
                self._run_sweeper(), name="sdk-pool-sweeper",
            )
