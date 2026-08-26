"""in_casa driver — embedded claude_agent_sdk engagement runtime."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from channels import DeliveryOutcome
from drivers.driver_protocol import DriverProtocol, StaleLaunchError
from error_kinds import (
    ApiErrorTurn, ErrorKind, api_error_kind, result_api_error_kind,
)
from engagement_registry import EngagementRecord
import sdk_logging

if TYPE_CHECKING:
    from channels.telegram import TopicStreamHandle

logger = logging.getLogger(__name__)


TopicStreamFactory = Callable[[int], "TopicStreamHandle"]
"""(topic_id) → TopicStreamHandle — channel-side per-turn streaming primitive.

Returned handle exposes async ``emit(accumulated_text)`` and async
``finalize(full_text) -> DeliveryOutcome`` (#665 — the driver reads the
outcome to see a wholly undelivered turn; a handle off the contract returns
``None``, which the driver coerces to ``UNKNOWN``). See
channels.telegram.TopicStreamHandle."""

ResultObserver = Callable[[EngagementRecord, ResultMessage], None]
"""(engagement, ResultMessage) → None — Task 6 (spec §4.6) per-turn cost/usage
observer. Called synchronously for every ``ResultMessage`` seen on an
engagement turn; casa_core wires it to feed interactive specialist cost into
``SpecialistTelemetry`` (filtering by ``engagement.kind``). Must not raise."""

SessionIdPersister = Callable[[str, str], Awaitable[None]]
"""(engagement_id, session_id) → None — registry persist hook.

Matches engagement_registry.persist_session_id's bound-method signature."""


def _session_id_from_message(sdk_msg: Any) -> str | None:
    """Extract the CLI session id from an SDK message (v0.69.11).

    The pinned Agent SDK's ``ClaudeSDKClient`` exposes no ``session_id``
    attribute; the id arrives in the stream — on the ``SystemMessage`` whose
    ``subtype == "init"`` (``data["session_id"]``) and on every
    ``ResultMessage`` (``.session_id``). Same source ``sdk_client_pool`` uses.
    """
    if isinstance(sdk_msg, SystemMessage):
        if getattr(sdk_msg, "subtype", None) == "init":
            data = getattr(sdk_msg, "data", None) or {}
            sid = data.get("session_id") if isinstance(data, dict) else None
            if isinstance(sid, str) and sid:
                return sid
    elif isinstance(sdk_msg, ResultMessage):
        sid = getattr(sdk_msg, "session_id", None)
        if isinstance(sid, str) and sid:
            return sid
    return None


class DriverNotAliveError(RuntimeError):
    """Raised when a turn is fed to a driver that has no open client."""


LAUNCH_MISSING_RESULT = "missing_result_message"
"""#678: the launch turn's response stream ended with no ``ResultMessage``.

The pinned SDK makes this unambiguous. ``ClaudeSDKClient.receive_response``
returns AT the ``ResultMessage`` and otherwise iterates forever
(``client.py`` — "If no ResultMessage is received, the iterator continues
indefinitely"), and the underlying ``Query.receive_messages`` ends only on the
``{"type": "end"}`` sentinel its read task sends from its ``finally`` — i.e.
transport EOF, CLI subprocess exit, or the read task being cancelled — or on
``EndOfStream`` after ``Query._close_impl`` closes the send side. A turn that
ran to its end always yields one, so its absence means the turn was CUT OFF
in flight, however many frames were seen first."""

FOLLOWUP_MISSING_RESULT = "followup_missing_result"
"""#692/#678: a ticketed FOLLOW-UP turn's response stream ended with no
``ResultMessage`` — the same cut-off fact as :data:`LAUNCH_MISSING_RESULT`,
one turn over, and recorded in its own slot because the two have different
owners and must not clobber each other.

There is no follow-up analogue of :data:`LAUNCH_NO_VISIBLE_OUTPUT`. A
follow-up turn that ran to its end and posted nothing is an ordinary quiet
turn — the agent may legitimately have done tool work and had nothing to say —
whereas a LAUNCH turn that posted nothing is an engagement nobody can see.
That covers QUIET turns only: a follow-up turn that PRODUCED text which never
arrived is not a quiet turn — it gets
:data:`FOLLOWUP_TEXT_NOT_DELIVERED` (#665)."""

LAUNCH_NO_VISIBLE_OUTPUT = "no_visible_output"
"""#678: the launch turn ran to its end but nothing about it is visible.

A ``ResultMessage`` arrived, the engagement is not terminal (no
``emit_completion``), and no assistant text was handed to the topic stream.
An interactive engagement that ENDS its launch turn to await the operator has
posted text and is legitimately unreported; one that posted nothing has left
the operator with a topic containing no evidence that anything happened."""

LAUNCH_TEXT_NOT_DELIVERED = "text_not_delivered"
"""#665: the launch turn produced text but none of it reached the topic.

The stream handle's ``finalize`` ESTABLISHED ``NOT_DELIVERED`` — every
Telegram operation was positively refused (or no bot existed to attempt
one), so the head of the turn's output is certainly not on the operator's
screen. The operator-visible situation is identical to
:data:`LAUNCH_NO_VISIBLE_OUTPUT` — a topic showing nothing — and the launch
owners react identically. A lost acknowledgement (``UNKNOWN``) records
NOTHING: the text may be on screen, and reporting a death over it would
kill a delivered turn."""

FOLLOWUP_TEXT_NOT_DELIVERED = "followup_text_not_delivered"
"""#665: a ticketed FOLLOW-UP turn's streamed text was wholly undelivered.

Same established-``NOT_DELIVERED`` fact as
:data:`LAUNCH_TEXT_NOT_DELIVERED`, one turn over, recorded in the
per-ticket slot because the two arms have different owners. The
missing-result case keeps its own reason
(:data:`FOLLOWUP_MISSING_RESULT`) — this one requires the turn to have
FINISHED (``result_msg`` present) with its text refused."""


class EmptyTurnError(RuntimeError):
    """#649: an ADMITTED inbound turn whose response stream ended with no
    model-evidence frame and no API fault. The client accepted the prompt but
    nothing shows the model took the turn up — raised so the delivery owner
    surfaces a visible failure instead of a silent success. The launch
    prompt (no admission token) keeps the historical warn-only behavior."""


class InCasaDriver(DriverProtocol):
    """Holds one ClaudeSDKClient per active engagement.

    ``topic_stream_factory`` is the channel-side factory that, given a
    topic_id, returns a TopicStreamHandle. Each ``_deliver_turn`` builds
    a fresh handle and emits AssistantMessage chunks progressively
    (Phase 3b — Bug 1). Injected rather than imported from
    ``channels.telegram`` to keep the driver pure/testable.
    """

    def __init__(
        self,
        *,
        topic_stream_factory: TopicStreamFactory,
        persist_session_id: SessionIdPersister | None = None,
        result_observer: "ResultObserver | None" = None,
        record_lookup: Callable[[str], Any] | None = None,
    ) -> None:
        self._topic_stream_factory = topic_stream_factory
        self._persist_session_id = persist_session_id
        # Task 6 (spec §4.6): optional per-turn cost/usage observer.
        self._result_observer = result_observer
        # #369: live registry lookup (engagement_id -> record | None) consulted
        # at the LAST suspension point before the initial prompt is delivered —
        # a clearance clamp can land during __aenter__, after the launcher's
        # own checks. None-safe for tests and legacy wiring.
        self._record_lookup = record_lookup
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._ctx_stack: dict[str, Any] = {}
        # Per-engagement asyncio.Lock guards query/receive_response sequencing:
        # ClaudeSDKClient is single-threaded per connection.
        self._locks: dict[str, asyncio.Lock] = {}
        # v0.69.11: the CLI's session id, captured from the MESSAGE STREAM
        # (SystemMessage init `data["session_id"]` / ResultMessage.session_id) —
        # ClaudeSDKClient has NO `session_id` attribute on the pinned Agent
        # SDK (0.2.114; still true on 0.2.128), so getattr(client,
        # "session_id") was always None and persistence silently never
        # fired. Same source the warm pool uses.
        self._session_ids: dict[str, str] = {}
        # #649 admission-ticket ledger (INV-ENG-003 for this driver). Two
        # in-memory, loop-confined populations of opaque token -> exact text,
        # insertion-ordered per engagement:
        #   unread   — admitted for delivery, not yet handed to the client.
        #              Vetoes a successful completion (inbound_unread_depth)
        #              and is disclosed on terminalization.
        #   accepted — client.query() took the text, no model-evidence frame
        #              seen yet. Disclosure-only (inbound_in_flight_texts);
        #              it must never veto: the only party able to emit a
        #              completion in that window is the very turn carrying
        #              the ticket. Non-durable BY DESIGN — this driver has no
        #              spool; a ticket alive at process death dies with it.
        self._inbound_unread: dict[str, dict[object, str]] = {}
        self._inbound_accepted: dict[str, dict[object, str]] = {}
        # #678: per-engagement observation of the LAUNCH turn's terminal
        # artifacts, written once by _deliver_turn (launch path only, so a
        # follow-up turn can never overwrite it) and POPPED by the launch
        # owner. The driver only OBSERVES: it does not read the engagement's
        # status and does not raise. A lock-free status read here could
        # observe the uncommitted window of a strict terminal transition that
        # a persist failure then rolls back (engagement_registry.py's strict
        # path commits in memory before awaiting the tombstone write), so
        # adjudication belongs to the registry's own single transactional
        # question, asked by the owner. Values: "" | LAUNCH_MISSING_RESULT |
        # LAUNCH_NO_VISIBLE_OUTPUT | LAUNCH_TEXT_NOT_DELIVERED.
        self._launch_incomplete: dict[str, str] = {}
        # #692/#678: the same observation for a ticketed FOLLOW-UP turn,
        # keyed by the exact admission TICKET rather than by engagement.
        # Per-engagement keying is not merely coarse, it is wrong: the
        # observation is written AFTER ``_deliver_turn`` releases the
        # per-engagement turn lock, so turn T1 can write its reason and T2 can
        # then acquire the lock, run and overwrite or consume it before T1's
        # delivery task reads — losing one notice or attributing it to the
        # wrong turn. Both design reviewers reached that interleaving
        # independently. eid -> {ticket: reason}.
        self._followup_incomplete: dict[str, dict[object, str]] = {}

    # -- lifecycle --------------------------------------------------------

    async def start(
        self,
        engagement: EngagementRecord,
        prompt: str,
        options: ClaudeAgentOptions,
        expected_generation: int | None = None,
    ) -> None:
        # E-E (v0.29.0): bind engagement_var BEFORE ClaudeSDKClient.__aenter__
        # so the SDK's inner Query._read_task — created via loop.create_task
        # in claude_agent_sdk._internal.query.Query.start — captures the
        # engagement in its ContextVar copy. Tool callbacks dispatched from
        # that inner task (and any spawn_task descendants) inherit the
        # binding. Without this, _effective_caller_role() reads None and
        # falls through to origin_var.role ("assistant" — Ellen's bus role
        # via origin_var inherited along the parent task), every privileged
        # tool refuses, and the engagement orphans. Lazy-imported to avoid
        # circular import (tools imports engagement_registry).
        from tools import engagement_var

        assert engagement.topic_id is not None, (
            "in_casa driver requires a topic_id (got None)"
        )
        client = ClaudeSDKClient(
            sdk_logging.with_stderr_callback(
                options, engagement_id=engagement.id[:8],
            ),
        )
        token = engagement_var.set(engagement)
        try:
            ctx = client.__aenter__()
            entered = await ctx if asyncio.iscoroutine(ctx) else ctx
            # #369 (Sol design r2 + Terra diff-gate r2): LAST-instant gate,
            # BEFORE this launch registers anything — a clearance clamp that
            # landed while __aenter__ was suspended means `prompt` was
            # rendered from pre-clamp materials, and a clamp→rebuild cycle
            # that COMPLETED in that window has already registered a fresh
            # floor client that this stale launch must not overwrite. The
            # pending flag alone cannot see the completed cycle (the rebuild
            # cleared it), so the monotonic generation captured at
            # prompt-source time is compared too. Stale → close the just-
            # entered client directly, touching no driver state.
            latest = (
                self._record_lookup(engagement.id)
                if self._record_lookup is not None else None
            )
            stale = latest is not None and (
                getattr(latest, "context_rebuild_pending", False)
                or (expected_generation is not None
                    and getattr(latest, "context_generation", 0)
                    != expected_generation)
            )
            if stale:
                try:
                    if hasattr(entered or client, "close"):
                        await (entered or client).close()
                    else:
                        await client.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001 — best-effort close of a
                    logger.warning(  # client that never carried state
                        "stale-launch client close failed for %s",
                        engagement.id[:8], exc_info=True)
                raise StaleLaunchError(
                    f"engagement {engagement.id[:8]} was clearance-clamped "
                    "during launch; aborting the pre-clamp prompt",
                    # Terra r5: pending cleared + generation moved = a rebuild
                    # COMPLETED while we were suspended — the engagement is
                    # alive on its floor client and must not be errored.
                    record_live=not getattr(
                        latest, "context_rebuild_pending", False))
            self._clients[engagement.id] = entered or client
            self._ctx_stack[engagement.id] = client  # for __aexit__
            self._locks[engagement.id] = asyncio.Lock()
            logger.info(
                "Engagement %s driver=in_casa client opened",
                engagement.id[:8],
            )
            try:
                await self._deliver_turn(engagement, prompt)
            except BaseException:
                # M14: Bug-13-style rollback (claude_code got this in v0.14.6).
                # engage_executor marks the record error, but error records are
                # excluded from active_and_idle() so no sweeper ever tears this
                # client down, and the topic stops routing — the opened claude
                # subprocess leaks until Casa restarts. Close + deregister here,
                # then re-raise so the caller's mark_error path still runs.
                # cancel() pops _clients/_ctx_stack/_locks and swallows close
                # errors, so the original exception is never masked.
                # #344: BaseException, not Exception — a CANCELLED initial
                # delivery took the same leak path (client registered,
                # never closed, record still "alive" to later turns). The
                # rollback runs as its own task under shield so a
                # re-cancellation interrupts our wait, not the close.
                logger.warning(
                    "Engagement %s first turn failed; rolling back client",
                    engagement.id[:8],
                )
                cleanup = asyncio.ensure_future(self.cancel(engagement))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    pass  # cleanup completes in background; re-raise below
                raise
        finally:
            # Clear from the parent task. The SDK inner task already
            # captured its own snapshot at __aenter__ time and is
            # unaffected by this reset.
            engagement_var.reset(token)

    async def send_user_turn(
        self, engagement: EngagementRecord, text: str,
        *, tg_message_id: int | None = None,
        inbound_token: object | None = None,
    ) -> None:
        # tg_message_id is part of the uniform driver interface (v0.79
        # reply-threading); in_casa turns have no topic-stream threading,
        # so it is accepted and ignored.
        #
        # #649: ``inbound_token`` is a seam-created admission ticket (the
        # Telegram entry points admit synchronously before their first await
        # and thread the token here). A direct caller gets a self-admitted
        # ticket at entry. Failure ownership: a SEAM ticket is deliberately
        # LEFT ledgered on an exception exit — the owning delivery task posts
        # its visible outcome first and discharges afterwards, so a terminal
        # transition racing that telling still snapshots the text. A
        # self-admitted ticket has no outer owner and is discharged here.
        self_owned = inbound_token is None
        token = (inbound_token if inbound_token is not None
                 else self.admit_inbound(engagement.id, text))
        try:
            if not self.is_alive(engagement):
                raise DriverNotAliveError(
                    f"engagement {engagement.id[:8]} has no live client"
                )
            await self._deliver_turn(engagement, text, inbound_token=token)
        except BaseException:
            if self_owned:
                self.discharge_inbound(engagement.id, token)
            raise
        else:
            # Clean turn end — normally a no-op (the evidence frame already
            # discharged it); the safety net for an evidence-less clean end
            # is EmptyTurnError, which exits through the branch above.
            self.discharge_inbound(engagement.id, token)

    # -- #649 inbound admission ledger ------------------------------------

    def admit_inbound(self, engagement_id: str, text: str) -> object:
        """SYNCHRONOUS: register one admitted inbound turn; returns the
        opaque ticket its owner later discharges. Exact-token semantics —
        duplicate texts stay independent."""
        token = object()
        self._inbound_unread.setdefault(engagement_id, {})[token] = str(text)
        return token

    def discharge_inbound(self, engagement_id: str, token: object) -> None:
        """SYNCHRONOUS + IDEMPOTENT: remove one ticket from whichever
        population holds it; a second discharge is a no-op."""
        for pop in (self._inbound_unread, self._inbound_accepted):
            bucket = pop.get(engagement_id)
            if bucket is not None and bucket.pop(token, None) is not None:
                if not bucket:
                    pop.pop(engagement_id, None)
                return

    def inbound_token_held(self, engagement_id: str, token: object) -> bool:
        """SYNCHRONOUS peek: is this exact ticket still ledgered? The
        failure owner asks before its one bounded notice attempt."""
        return any(
            token in pop.get(engagement_id, ())
            for pop in (self._inbound_unread, self._inbound_accepted))

    def _accept_inbound(self, engagement_id: str, token: object) -> None:
        """Move unread -> accepted, synchronously, immediately BEFORE the
        client hand-off: once the per-turn lock is held the executing turn
        is the only possible completion emitter, so an unread veto spanning
        the transport write could only self-veto (the SDK dispatches the
        emit_completion callback from its reader while ``query()`` awaits)."""
        bucket = self._inbound_unread.get(engagement_id)
        if bucket is None:
            return
        text = bucket.pop(token, None)
        if not bucket:
            self._inbound_unread.pop(engagement_id, None)
        if text is not None:
            self._inbound_accepted.setdefault(engagement_id, {})[token] = text

    def inbound_unread_depth(self, engagement_id: str) -> int:
        """G4 gate read (tools.py emit_completion): admitted turns not yet
        handed to this engagement's client. > 0 vetoes a successful
        completion."""
        return len(self._inbound_unread.get(engagement_id, ()))

    def inbound_unread_texts(self, engagement_id: str) -> list[str]:
        """Terminal-hook read: the unread population's exact texts, for the
        veto re-check inside the transition and the dying-message
        disclosure."""
        return list(self._inbound_unread.get(engagement_id, {}).values())

    def launch_turn_incomplete(self, engagement_id: str) -> str:
        """SYNCHRONOUS: POP the #678 launch-turn observation.

        Returns ``LAUNCH_MISSING_RESULT``, ``LAUNCH_NO_VISIBLE_OUTPUT``,
        ``LAUNCH_TEXT_NOT_DELIVERED`` or
        ``""``. Read exactly once, by the launch owner, immediately after
        :meth:`start` returns; popping means a re-read cannot report the same
        death twice. Not part of ``DriverProtocol`` — the launch owners reach
        it through ``getattr`` inside their in_casa branches only, so a
        claude_code or legacy driver reads as "nothing to report" instead of
        raising ``AttributeError`` into a generic handler that would mark a
        HEALTHY engagement errored.
        """
        return self._launch_incomplete.pop(engagement_id, "")

    def followup_turn_incomplete(
        self, engagement_id: str, inbound_token: object,
    ) -> str:
        """SYNCHRONOUS: POP the #692/#678 FOLLOW-UP turn observation for one
        exact admission ticket.

        Returns ``FOLLOWUP_MISSING_RESULT``,
        ``FOLLOWUP_TEXT_NOT_DELIVERED`` or ``""``. Read once, by the
        delivery task that owns that ticket, immediately after its
        ``send_user_turn`` returns; popping means the task-end cleanup
        backstop that runs after it cannot tell the operator a second time.

        Keyed by TICKET, not by engagement — see ``_followup_incomplete``.

        Not part of ``DriverProtocol``, for the same reason
        :meth:`launch_turn_incomplete` is not: the owner reaches it through
        ``getattr`` inside its in_casa branch only, so a ``claude_code``
        engagement reads as "nothing to report" instead of raising
        ``AttributeError`` into a handler that would surface a failure for a
        perfectly healthy turn.
        """
        bucket = self._followup_incomplete.get(engagement_id)
        if bucket is None:
            return ""
        reason = bucket.pop(inbound_token, "")
        if not bucket:
            self._followup_incomplete.pop(engagement_id, None)
        return reason

    def inbound_in_flight_texts(self, engagement_id: str) -> list[str]:
        """Disclosure-only population: client accepted the text, no
        model-evidence frame processed yet. ``inbound_in_flight_blocking``
        is deliberately NOT implemented — a blocking count here could only
        veto the carrying turn's own completion."""
        return list(self._inbound_accepted.get(engagement_id, {}).values())

    async def cancel(self, engagement: EngagementRecord) -> None:
        client = self._clients.pop(engagement.id, None)
        ctx = self._ctx_stack.pop(engagement.id, None)
        self._locks.pop(engagement.id, None)
        self._session_ids.pop(engagement.id, None)
        self._launch_incomplete.pop(engagement.id, None)  # #678 map hygiene
        self._followup_incomplete.pop(engagement.id, None)  # #692 same
        if client is None and ctx is None:
            return
        try:
            # Prefer close() on the entered client; fall back to __aexit__ on
            # the original context manager object if close() is absent.
            if client is not None and hasattr(client, "close"):
                await client.close()
            elif ctx is not None and hasattr(ctx, "__aexit__"):
                await ctx.__aexit__(None, None, None)
        except Exception as exc:
            logger.warning(
                "Engagement %s cancel: client close raised %s",
                engagement.id[:8], exc,
            )

    async def resume(
        self, engagement: EngagementRecord, session_id: str,
    ) -> None:
        """Reopen a ClaudeSDKClient with resume=session_id.

        Caller (telegram routing path, after user turn in a suspended topic)
        handles retry + error surfacing. This method raises on failure.
        """
        # E-E (v0.29.0): same propagation requirement as start() — the
        # resumed client also creates a fresh _read_task during __aenter__.
        from tools import engagement_var

        if self.is_alive(engagement):
            logger.warning(
                "resume() called on engagement %s that is already alive",
                engagement.id[:8],
            )
            return
        # Finding 2 (codex review v0.69.10): rebuild the FULL option set (a
        # bare ClaudeAgentOptions(resume=) drops disallowed_tools/Agent+Task,
        # the fail-closed callback, hooks, skills, MCP restrictions — running
        # the resumed engagement unrestricted). Off-loop: the builder reads the
        # registry + hooks.yaml. Fails closed if the config is gone (§3.8: an
        # executor resumes from its recorded plugin artifacts, never re-resolved).
        from tools import build_engagement_resume_options
        options = await asyncio.to_thread(
            build_engagement_resume_options, engagement, session_id,
        )
        client = ClaudeSDKClient(
            sdk_logging.with_stderr_callback(
                options, engagement_id=engagement.id[:8],
            ),
        )
        token = engagement_var.set(engagement)
        try:
            entered = await client.__aenter__()
            self._clients[engagement.id] = entered or client
            self._ctx_stack[engagement.id] = client
            self._locks[engagement.id] = asyncio.Lock()
            logger.info(
                "Engagement %s resumed (session=%s)",
                engagement.id[:8], session_id,
            )
        finally:
            engagement_var.reset(token)

    async def invalidate_session(self, engagement: EngagementRecord) -> None:
        """#369 (Sol diff-gate r1+r2): teardown for a clearance downgrade —
        unlike :meth:`cancel` (terminal path, best-effort close), a FAILED
        client close here PROPAGATES, and the stale client is RETAINED in the
        maps until the close succeeds: popping first would make the retry see
        an empty map and report teardown "confirmed" over a surviving
        subprocess. Deliveries into the retained client are refused meanwhile
        by the context_rebuild_pending fence, which is set before any caller
        reaches this method."""
        client = self._clients.get(engagement.id)
        ctx = self._ctx_stack.get(engagement.id)
        if client is not None or ctx is not None:
            if client is not None and hasattr(client, "close"):
                await client.close()
            elif ctx is not None and hasattr(ctx, "__aexit__"):
                await ctx.__aexit__(None, None, None)
        self._clients.pop(engagement.id, None)
        self._ctx_stack.pop(engagement.id, None)
        self._locks.pop(engagement.id, None)
        self._session_ids.pop(engagement.id, None)
        self._launch_incomplete.pop(engagement.id, None)  # #678 map hygiene
        self._followup_incomplete.pop(engagement.id, None)  # #692 same

    async def open_fresh(self, engagement: EngagementRecord) -> None:
        """#369: open a NEW session for an engagement whose context was
        invalidated by a clearance downgrade — the same fully-restricted
        option set as :meth:`resume`, but with no ``resume=`` sid, so the
        CLI starts a fresh conversation holding nothing fetched at the old
        tier. Raises on failure (the caller fails closed and retries on the
        next turn)."""
        from tools import build_engagement_resume_options, engagement_var

        if self.is_alive(engagement):
            logger.warning(
                "open_fresh() called on engagement %s that is already alive",
                engagement.id[:8],
            )
            return
        options = await asyncio.to_thread(
            build_engagement_resume_options, engagement, None,
        )
        client = ClaudeSDKClient(
            sdk_logging.with_stderr_callback(
                options, engagement_id=engagement.id[:8],
            ),
        )
        token = engagement_var.set(engagement)
        try:
            entered = await client.__aenter__()
            self._clients[engagement.id] = entered or client
            self._ctx_stack[engagement.id] = client
            self._locks[engagement.id] = asyncio.Lock()
            logger.info(
                "Engagement %s reopened FRESH after clearance downgrade",
                engagement.id[:8],
            )
        finally:
            engagement_var.reset(token)

    def get_session_id(self, engagement: EngagementRecord) -> str | None:
        """Return the session id captured from this engagement's message stream
        (v0.69.11), falling back to the record's persisted value. The live
        ``ClaudeSDKClient`` has no ``session_id`` attribute (pinned SDK), so the
        stream-sourced value is authoritative."""
        return (
            self._session_ids.get(engagement.id)
            or getattr(engagement, "sdk_session_id", None)
        )

    def is_alive(self, engagement: EngagementRecord) -> bool:
        return engagement.id in self._clients

    # -- internal ---------------------------------------------------------

    async def _deliver_turn(
        self, engagement: EngagementRecord, prompt: str,
        *, inbound_token: object | None = None,
    ) -> None:
        # Lazy import: tools imports engagement_registry; doing this at
        # module top-level would create a circular import.
        from tools import engagement_var

        client = self._clients[engagement.id]
        lock = self._locks[engagement.id]
        assert engagement.topic_id is not None
        # Phase 3b: stream per-AssistantMessage rather than buffer the
        # entire turn.
        stream = self._topic_stream_factory(engagement.topic_id)
        accumulated = ""
        # Task 6 (spec §4.6): per-turn output bound for INTERACTIVE SPECIALIST
        # engagements — the streamed assistant text was otherwise unbounded
        # before emit_completion. Once the accumulator crosses the cap it is
        # frozen with a marker and further assistant text is skipped (the
        # stream still drains). Executor engagements are out of scope.
        cap_output = getattr(engagement, "kind", "") == "specialist"
        stream_truncated = False
        idx = 0  # Phase 4b: per-turn AssistantMessage counter.
        started_ms = time.monotonic() * 1000  # Phase 4b: turn duration anchor.
        # #595: the kind of API-level fault that ENDED this turn, if any, and
        # the terminal result that may carry it instead. Collected here and
        # raised after the stream drains — see the raise below for why.
        api_error: ErrorKind | None = None
        result_msg: Any = None
        # #649: has any model-evidence frame been processed this turn?
        evidence_seen = False
        # Per-call tool name lookup so log_tool_result can render name=.
        tool_names_by_id: dict[str, str] = {}
        token = engagement_var.set(engagement)
        try:
            async with lock:
                # #649: unread -> accepted, synchronously, before the
                # hand-off — see _accept_inbound for why not after query().
                if inbound_token is not None:
                    self._accept_inbound(engagement.id, inbound_token)
                await client.query(prompt)
                async for sdk_msg in client.receive_response():
                    sid = _session_id_from_message(sdk_msg)
                    if sid:
                        self._session_ids[engagement.id] = sid
                    if (
                        sid
                        and self._persist_session_id is not None
                        and engagement.sdk_session_id != sid
                    ):
                        # #302: mark the ID persisted ONLY after the durable
                        # write succeeded. Setting it on failure defeated the
                        # same-sid retry guard above — the in-memory record
                        # looked current while the registry never received
                        # the ID, and a restart could not resume the session.
                        # On failure the next message carrying the same sid
                        # retries the persist.
                        try:
                            await self._persist_session_id(engagement.id, sid)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "Engagement %s persist_session_id failed "
                                "(retried on the next message): %s",
                                engagement.id[:8], exc,
                            )
                        else:
                            engagement.sdk_session_id = sid
                    # Phase 4b dispatch — wrapped in try/except so a
                    # malformed block does not abort the rest of the turn.
                    try:
                        if isinstance(sdk_msg, SystemMessage):
                            sdk_logging.log_system_init(sdk_msg)
                        elif isinstance(sdk_msg, AssistantMessage):
                            idx += 1
                            sdk_logging.log_assistant_message(sdk_msg, idx=idx)
                            for block in getattr(sdk_msg, "content", []) or []:
                                if isinstance(block, ToolUseBlock):
                                    tool_names_by_id[
                                        getattr(block, "id", "")
                                    ] = getattr(block, "name", "?")
                                    sdk_logging.log_tool_use(
                                        block,
                                        idx=idx,
                                        started_ms=started_ms,
                                    )
                        elif isinstance(sdk_msg, UserMessage):
                            for block in getattr(sdk_msg, "content", []) or []:
                                if isinstance(block, ToolResultBlock):
                                    name = tool_names_by_id.get(
                                        getattr(block, "tool_use_id", ""),
                                        "",
                                    )
                                    sdk_logging.log_tool_result(
                                        block, idx=idx, started_ms=started_ms,
                                        name=name,
                                    )
                        elif isinstance(sdk_msg, ResultMessage):
                            sdk_logging.log_turn_done(
                                sdk_msg, started_ms=started_ms,
                            )
                            # Task 6 (spec §4.6): feed interactive specialist
                            # cost/usage to the telemetry observer. Guarded —
                            # an observability hook must never abort the turn.
                            if self._result_observer is not None:
                                try:
                                    self._result_observer(engagement, sdk_msg)
                                except Exception:  # noqa: BLE001
                                    logger.warning(
                                        "result_observer raised for engagement %s",
                                        engagement.id[:8], exc_info=True,
                                    )
                    except Exception as dispatch_exc:  # noqa: BLE001
                        logger.warning(
                            "phase4b dispatch failed: %s", dispatch_exc,
                            exc_info=True,
                        )
                    # #568: an API-level fault (a safety refusal included)
                    # arrives as an assistant message whose text block is the
                    # CLI's own error prose — a request id and terminal-UI
                    # advice. Streaming it would post that into the
                    # engagement's topic as the agent's words, so it is never
                    # streamed. #595: it is also RECORDED, so the turn can end
                    # with the kind the fault actually was.
                    #
                    # Detected outside the ``not stream_truncated`` branch on
                    # purpose (Sol design r2): once a specialist's output has
                    # been clipped that branch stops running, and a fault after
                    # the clip would have been invisible — the turn would end
                    # as a truncated success. Main-loop faults only, exactly as
                    # ``sdk_client_pool.run_turn_locked``: one scoped to a
                    # sub-agent (``parent_tool_use_id`` set) is that
                    # sub-agent's problem and the turn can still answer, though
                    # its prose is not an answer either and is still skipped.
                    # First one wins — the fault that ended the turn is the
                    # earliest, not the last thing on the wire.
                    if isinstance(sdk_msg, AssistantMessage):
                        _api_kind = api_error_kind(sdk_msg)
                        if _api_kind is not None:
                            if (api_error is None and getattr(
                                    sdk_msg, "parent_tool_use_id", None) is None):
                                api_error = _api_kind
                            logger.warning(
                                "engagement %s turn ended by an API error "
                                "kind=%s", engagement.id[:8], _api_kind.value,
                            )
                            continue
                    elif isinstance(sdk_msg, ResultMessage):
                        result_msg = sdk_msg
                    # #649: an accepted ticket is discharged on the first
                    # MODEL-EVIDENCE frame — an Assistant frame with no
                    # main-loop fault (fault frames `continue`d above), a
                    # User (tool-result) frame, or a fault-free Result. The
                    # check consults the CUMULATIVE fault latch, not the
                    # frame alone: a clean-looking Result after a fault
                    # Assistant must not convert a doomed turn into a
                    # consumed one (the same frame chain ends in
                    # ApiErrorTurn, and the ticket must survive into the
                    # failure owner's telling / the terminal snapshot).
                    if (inbound_token is not None and api_error is None
                            and isinstance(sdk_msg, (
                                AssistantMessage, UserMessage))):
                        evidence_seen = True
                        self.discharge_inbound(engagement.id, inbound_token)
                    elif (inbound_token is not None and api_error is None
                            and isinstance(sdk_msg, ResultMessage)
                            and result_api_error_kind(sdk_msg) is None):
                        evidence_seen = True
                        self.discharge_inbound(engagement.id, inbound_token)
                    # Phase 3b streaming — Task 6 output bound for specialists.
                    if isinstance(sdk_msg, AssistantMessage) and not stream_truncated:
                        msg_text = "".join(
                            b.text for b in getattr(sdk_msg, "content", [])
                            if isinstance(b, TextBlock)
                        )
                        if msg_text:
                            candidate = (
                                f"{accumulated}\n\n{msg_text}"
                                if accumulated else msg_text
                            )
                            import specialist_limits
                            cap = specialist_limits._MAX_OUTPUT_CHARS
                            if cap_output and len(candidate) > cap:
                                accumulated = candidate[:cap] + " … [truncated]"
                                stream_truncated = True
                                # Persist a flag so the finalize/notification
                                # path can disclose the clipped stream.
                                engagement.origin["stream_output_truncated"] = True
                                logger.warning(
                                    "engagement %s specialist stream output "
                                    "truncated at %d chars (spec §4.6)",
                                    engagement.id[:8], cap,
                                )
                            else:
                                accumulated = candidate
                            await stream.emit(accumulated)
        finally:
            engagement_var.reset(token)
        final = accumulated.strip()
        # #665: UNKNOWN before any finalize — a quiet turn (no text) makes no
        # delivery claim, and a handle off the contract (returns None, or a
        # test fake returning a truthy MagicMock) coerces to UNKNOWN by the
        # enum's own documented rule, hence isinstance and not truthiness.
        delivery_outcome = DeliveryOutcome.UNKNOWN
        if final:
            res = await stream.finalize(final)
            if isinstance(res, DeliveryOutcome):
                delivery_outcome = res

        # #595: the result's own stop_reason is the second carrier — read so a
        # refusal reported ONLY there (no synthesized assistant message) still
        # ends the turn honestly rather than as an empty success. Same pair of
        # carriers every other read loop in the codebase handles.
        if api_error is None:
            api_error = result_api_error_kind(result_msg)
        if api_error is not None:
            # Raised AFTER the stream is drained and whatever legitimate text
            # preceded the fault has been finalized: that text has already been
            # posted progressively and cannot be retracted, so leaving it
            # un-finalized would strand a partial edit in the topic. Raising at
            # all is the point of #595 — without it the turn returns normally
            # having produced nothing, and the engagement's terminal record
            # says `driver_start_failed` (or nothing), making a safety refusal
            # indistinguishable from a crash. The kind travels on the exception
            # so classification happens once, where the evidence is.
            logger.warning(
                "engagement %s: turn ended by an API fault kind=%s",
                engagement.id[:8], api_error.value,
            )
            raise ApiErrorTurn(api_error)

        # G-4 (v0.33.0): exploration2 found a configurator engagement
        # that finalized outcome=error 24s after system_init with zero
        # tool_uses inside the subprocess and no log evidence of why.
        # When the SDK loop completes without producing any
        # AssistantMessage frames (idx never incremented), surface the
        # empty turn as a structured warning so operators have a
        # starting signal. Causes include: hook payload synthesis denial
        # (G-1 class), model refusal at system-prompt time, or
        # subprocess crash between init and first message.
        if idx == 0:
            logger.warning(
                "Engagement %s subprocess_terminated "
                "reason=no_assistant_message",
                engagement.id[:8],
            )
        # #649: an ADMITTED turn that produced no evidence at all (clean
        # exhaustion, zero model frames, no API fault) must not end as a
        # silent success — its ticket would be discharged with the message
        # unaccounted. Raise so the delivery owner posts a visible failure.
        # Launch prompts (no token) keep the warn-only G-4 behavior above.
        if inbound_token is not None and not evidence_seen:
            raise EmptyTurnError(
                f"engagement {engagement.id[:8]}: admitted turn ended with "
                "no model-evidence frame"
            )

        # #692/#678: OBSERVE whether a ticketed FOLLOW-UP turn left the turn's
        # own terminal artifact, in its OWN slot keyed by its OWN ticket.
        #
        # Reached only after the ``ApiErrorTurn`` and ``EmptyTurnError``
        # raises above, so a faulted turn and an evidence-less turn keep their
        # existing owners and never write here. ``evidence_seen`` is
        # deliberately NOT the predicate — it latches on the FIRST frame, so a
        # turn cut off mid-tool-loop is indistinguishable from a finished one
        # on it. The honest predicate is the missing result frame.
        #
        # RECORDED, never raised. A raise here would be a new failure mode on
        # the path every follow-up turn in the tree takes, and it cannot tell
        # a cutoff from a HEALTHY in_casa self-emit completion — where
        # ``_finalize_engagement_tail`` closes this engagement's own SDK
        # client and the response iterator can end with no ``ResultMessage``
        # on the happy path. The delivery task adjudicates instead, against
        # the registry's SETTLED status, which is the one place that
        # distinction can be made.
        if inbound_token is not None and result_msg is None:
            self._followup_incomplete.setdefault(
                engagement.id, {})[inbound_token] = FOLLOWUP_MISSING_RESULT
            logger.warning(
                "Engagement %s follow-up turn ended with no ResultMessage "
                "(frames=%d) — its delivery task reports it",
                engagement.id[:8], idx,
            )
        elif (inbound_token is not None
                and delivery_outcome is DeliveryOutcome.NOT_DELIVERED):
            # #665: the turn FINISHED (result_msg present — the elif keeps
            # the cut-off fact as the stronger reason) but its streamed text
            # was wholly refused. Recorded only on established NOT_DELIVERED;
            # an UNKNOWN lost-ack may be on the operator's screen.
            self._followup_incomplete.setdefault(
                engagement.id, {})[inbound_token] = FOLLOWUP_TEXT_NOT_DELIVERED
            logger.warning(
                "Engagement %s follow-up turn's streamed text was not "
                "delivered (frames=%d) — its delivery task reports it",
                engagement.id[:8], idx,
            )

        # #678: OBSERVE whether this LAUNCH turn left a terminal artifact.
        # Launch turns only (``inbound_token is None``); the ticketed arm is
        # directly above and writes a DIFFERENT slot, so neither can overwrite
        # an observation the other's owner has not read yet.
        #
        # Recorded AFTER ``stream.finalize(final)`` above, deliberately: the
        # ``no_visible_output`` arm is a statement about text that reached the
        # topic, so deciding it before the finalize would judge a turn on
        # output it had not yet posted. Recorded, never raised — a raise here
        # would take ``start()``'s M14 rollback and log "first turn failed" on
        # a successful tool-only launch whose completion the owner is about to
        # discover. ``evidence_seen`` is deliberately NOT consulted: it
        # latches on the FIRST frame, so a turn cut off mid-tool-loop is
        # indistinguishable from a finished one on it.
        #
        # NOTE (#692): this block used to justify its launch-only scope by
        # saying a follow-up turn's failure owner "is the Telegram delivery
        # task's 'Turn failed' notice (#649), which needs no help from here".
        # That was FALSE and it is why the follow-up cutoff was silent: that
        # notice lives inside ``except Exception`` around
        # ``_driver_send_user_turn``, and this delivery does not raise. The
        # ticketed arm above is the help it needed.
        if inbound_token is None:
            reason = ""
            if result_msg is None:
                reason = LAUNCH_MISSING_RESULT
            elif not final:
                reason = LAUNCH_NO_VISIBLE_OUTPUT
            elif delivery_outcome is DeliveryOutcome.NOT_DELIVERED:
                # #665: strongest-known-first — a cut-off turn keeps its
                # cut-off reason and a quiet turn its no-output reason; this
                # arm is a turn that finished WITH text, all of it refused.
                reason = LAUNCH_TEXT_NOT_DELIVERED
            if reason:
                self._launch_incomplete[engagement.id] = reason
                logger.warning(
                    "Engagement %s launch turn left no terminal artifact "
                    "(reason=%s frames=%d) — the launch owner reports it",
                    engagement.id[:8], reason, idx,
                )
