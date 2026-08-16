"""VoiceChannel — dual-ingress (SSE + WS) channel for the butler agent.

Unlike Telegram, VoiceChannel does not own an IO loop. It mounts HTTP
routes on the aiohttp app already created by casa_core.py. The Channel
start()/stop() hooks exist for lifecycle (sweeper task), not transport.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from aiohttp import web

from agent import _classify_error
from bus import BusMessage, MessageBus, MessageType
from channel_authz import agent_allowed_on
from ingress_identity import ingress_identity
from channels import Channel
from log_cid import new_cid
from provenance import sanitize_external_context
from rate_limit import RateLimiter
from channels.voice.catalog import (
    VOICE_AGENT_CATALOG_PATH,
    VoiceAgentCatalogError,
    build_voice_agent_catalog,
)
from channels.voice.prosodic import Block, ProsodicSplitter
from channels.voice.routes import VoiceRouteRegistry, VoiceWsConnection
from channels.voice.session import VoiceSessionPool
from channels.voice.tts_adapter import TagDialectAdapter, has_speech
import voice_phrases
from job_registry import JobTransitionError, VoiceJob
from semantic_memory import SemanticMemory

logger = logging.getLogger(__name__)

# Concierge specialist hand-off wire protocol. The client must echo this (and
# the job id) in its `handoff_received` receipt; see VoiceHandoff.frame and
# VoiceHandoffCoordinator.handle. Named rather than repeated as a literal so
# the offer and the acceptance test can never drift apart again.
_HANDOFF_PROTOCOL = 2

# Per-utterance delivery offer (#233/#224). The client tells Casa how the
# device that ASKED can receive a deferred answer, so Casa promises only what
# that device can do. Route capabilities could never answer this: the socket is
# shared by every device on the Home Assistant.
_DELIVERY_OFFER_PROTOCOL = 3
_DELIVERY_MODALITIES = frozenset({"audio", "text"})
_DELIVERY_RECEIPTS = frozenset({"playback_complete", "accepted"})


def sanitize_delivery_offer(raw: Any) -> dict | None:
    """Accept a client's delivery offer, or reject it.

    **Trust boundary.** Casa cannot see Home Assistant's entity registry, so it
    cannot independently verify that a device can be announced to or notified.
    The authenticated integration is the authority on that, by design: it is
    the component that can see the registry, and it holds the route's HMAC
    secret. What this function guarantees is narrower — that the value Casa
    acts on has a known shape, comes from the FRAME on that authenticated
    route, and can never be a claim a turn's context made about itself
    (``_voice_delivery_offer`` is in ``provenance.RESERVED_CONTEXT_KEYS``).

    A client holding the route secret can therefore state a capability it does
    not have. The blast radius is bounded to that client's own devices and ends
    in a failed delivery, not in disclosure: the answer still has to survive
    clearance, and delivery still has to find a real endpoint.

    An unrecognised receipt degrades to the weakest one rather than the frame
    being dropped — receipt strength affects wording, not authorization.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("protocol") != _DELIVERY_OFFER_PROTOCOL:
        return None
    modality = raw.get("modality")
    if modality not in _DELIVERY_MODALITIES:
        return None
    receipt = raw.get("receipt")
    if receipt not in _DELIVERY_RECEIPTS:
        receipt = "accepted"
    return {"modality": modality, "receipt": receipt}


@dataclass(frozen=True)
class VoiceHandoff:
    """The sole foreground frame data produced from a durable voice job."""

    utterance_id: str
    handoff_id: str
    text: str
    job_id: str = ""

    @classmethod
    def from_job(cls, utterance_id: str, job: VoiceJob) -> "VoiceHandoff":
        """Select the fixed acknowledgement without exposing job content.

        The wording SETS AN EXPECTATION (#233): the caller is about to wait
        tens of seconds for a specialist, and "I will ask X." alone left them
        with no idea whether an answer was still coming. Channel-owned fixed
        text — never the model's — so the promise is identical every time.
        """
        return cls(
            utterance_id=utterance_id,
            handoff_id=job.handoff_id or "",
            job_id=job.id,
            # Rendered from the modality this endpoint actually offered, so the
            # promise cannot outrun what the device can do. Varied wording,
            # fixed commitment — and channel-owned, never the model's.
            text=voice_phrases.acknowledgement(
                job.specialist_display_name,
                job.delivery_modality,
                voice_phrases.seed_for(job.id),
            ),
        )

    def frame(self) -> dict[str, str]:
        # `job_id` + `protocol` are what the client echoes back in its
        # `handoff_received` receipt. Casa's coordinator binds that receipt to
        # a job AND a route; without these fields the client could not produce
        # an acceptable receipt at all, so every handoff stayed PENDING and the
        # finished answer expired unspoken (the #233/#224 delivery failure).
        return {
            "type": "handoff",
            "protocol": _HANDOFF_PROTOCOL,
            "utterance_id": self.utterance_id,
            "handoff_id": self.handoff_id,
            "job_id": self.job_id,
            "text": self.text,
        }


class VoiceHandoffReservation:
    """Request-local foreground ownership for one potential WS handoff.

    The reservation has no task, context, routing, or user data.  Tools can
    synchronously reserve/release it while the channel owns the one-way commit
    callback that resolves the foreground handoff future after durability.
    """

    def __init__(self) -> None:
        self._held = False
        self._speech_sent = False
        self._committed = False
        self._commit_callback: Callable[[VoiceJob], None] | None = None

    @property
    def held(self) -> bool:
        return self._held

    @property
    def committed(self) -> bool:
        return self._committed

    def bind_commit(self, callback: Callable[[VoiceJob], None]) -> None:
        """Install the channel-owned completion callback exactly once."""
        if self._commit_callback is not None:
            raise RuntimeError("voice handoff commit callback already bound")
        self._commit_callback = callback

    def reserve(self) -> bool:
        """Suppress foreground output, unless this turn has already spoken."""
        if self._speech_sent:
            return False
        self._held = True
        return True

    def release(self) -> None:
        """Let a typed prelaunch failure resume the ordinary response turn."""
        if not self._committed:
            self._held = False

    def mark_speech_sent(self) -> None:
        """Close the handoff path once a real speech block reached the wire."""
        self._speech_sent = True

    def commit(self, job: VoiceJob) -> None:
        """Resolve the foreground owner once Task 3 made the job durable."""
        if self._committed:
            return
        callback = self._commit_callback
        if callback is None:
            raise RuntimeError("voice handoff commit callback is not bound")
        callback(job)
        self._committed = True


class VoiceHandoffCoordinator:
    """Send and acknowledge durable handoffs on authenticated routes only."""

    def __init__(self, registry: Any, route_registry: Any | None = None) -> None:
        self._registry = registry
        self._route_registry = route_registry

    @staticmethod
    def _frame(job: VoiceJob) -> dict[str, Any]:
        """Build the intentionally metadata-only coordinator frame."""
        return {
            "type": "voice_handoff",
            "protocol": _HANDOFF_PROTOCOL,
            "job_id": job.id,
            "handoff_id": job.handoff_id,
            "specialist_display_name": job.specialist_display_name,
        }

    async def route_connected(self, route: Any) -> None:
        """Reoffer only this route's persisted pending acknowledgements."""
        route_id = _nonempty_identifier(getattr(route, "route_id", None))
        if route_id is None:
            return
        for job in self._registry.pending_handoffs_for_route(route_id):
            if not self._is_current(route_id, route):
                # #329: a concurrent re-registration of the same route id
                # superseded this socket mid-replay — job/handoff metadata
                # must not reach the stale socket. The current binding got
                # (or will get) its own route_connected replay.
                return
            # The guard rides inside the connection's serialized send (see
            # VoiceWsConnection.send_json): a supersession that lands while
            # this write waits on the send lock still suppresses the frame.
            await route.send_json(
                self._frame(job),
                allow=lambda: self._is_current(route_id, route),
            )

    def _is_current(self, route_id: str, route: Any) -> bool:
        if self._route_registry is None:
            return True
        return self._route_registry.get_connected(route_id) is route

    async def handle(self, route: Any, frame: Mapping[str, Any]) -> None:
        """Accept a receipt only for the server-bound route that owns it.

        EVERY rejection is logged with a reason (#233/#224). These branches
        used to `return` silently, and a client that sent a receipt without
        `protocol`/`job_id` was indistinguishable from a client that sent
        nothing: the hand-off stayed PENDING, the finished answer expired
        unspoken, and NOTHING appeared in the logs on either side. A refused
        receipt is now always visible. Reasons only — never frame content.
        """
        if frame.get("type") != "handoff_received":
            return          # not addressed to us; the WS carries other types
        if frame.get("protocol") != _HANDOFF_PROTOCOL:
            logger.warning(
                "voice handoff receipt REFUSED reason=protocol_mismatch "
                "expected=%s got=%r", _HANDOFF_PROTOCOL,
                frame.get("protocol"),
            )
            return
        job_id = _nonempty_identifier(frame.get("job_id"))
        handoff_id = _nonempty_identifier(frame.get("handoff_id"))
        route_id = _nonempty_identifier(getattr(route, "route_id", None))
        if job_id is None or handoff_id is None or route_id is None:
            logger.warning(
                "voice handoff receipt REFUSED reason=missing_identifier "
                "job_id=%s handoff_id=%s route_id=%s",
                job_id is not None, handoff_id is not None,
                route_id is not None,
            )
            return
        job = self._registry.get(job_id)
        if job is None:
            logger.warning(
                "voice handoff receipt REFUSED reason=unknown_job")
            return
        if job.origin_route_id != route_id:
            logger.warning(
                "voice handoff receipt REFUSED reason=route_mismatch")
            return
        try:
            await self._registry.acknowledge_handoff(job_id, handoff_id)
        except JobTransitionError as exc:
            # A duplicate receipt is idempotent in the registry; mismatched
            # IDs and stale lifecycle rows are not errors, but they are no
            # longer invisible.
            logger.info(
                "voice handoff receipt not applied reason=%s",
                type(exc).__name__,
            )
            return
        logger.info("voice handoff ACKNOWLEDGED job=%s", job_id[:8])


def _nonempty_identifier(value: Any) -> str | None:
    """Normalize one trusted voice identifier without logging its value."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 512:
        return None
    return normalized


def _connection_voice_route(
    connection: Any,
) -> tuple[str | None, frozenset[str], str | None]:
    """Read route identity only from the server-owned WS connection object.

    Task 4 replaces the raw aiohttp socket with ``VoiceWsConnection``.  The
    two accepted attribute spellings keep this trust seam compatible with
    that wrapper while direct Task-3 tests can use a minimal connection
    double.  Utterance/context fields are intentionally never consulted.
    """
    route_id = _nonempty_identifier(
        getattr(connection, "voice_route_id", None)
    ) or _nonempty_identifier(getattr(connection, "route_id", None))
    raw_capabilities = getattr(
        connection,
        "voice_route_capabilities",
        getattr(
            connection,
            "accepted_capabilities",
            getattr(connection, "capabilities", ()),
        ),
    )
    if not isinstance(raw_capabilities, (set, frozenset, list, tuple)):
        return route_id, frozenset(), _nonempty_identifier(
            getattr(connection, "voice_job_control_id", None),
        )
    capabilities = frozenset(
        item for item in raw_capabilities
        if isinstance(item, str) and item
    )
    return (
        route_id,
        capabilities,
        _nonempty_identifier(
            getattr(connection, "voice_job_control_id", None),
        ),
    )

_DEFAULT_ERROR_LINES = {
    "timeout":       "[flat] That took too long.",
    "rate_limit":    "[flat] I'm busy — try again shortly.",
    "sdk_error":     "[flat] I couldn't reach my brain.",
    "memory_error":  "",
    "channel_error": "[flat] Something went wrong.",
    # #568: a refused or API-faulted turn reaches this table like any other
    # kind. Without a line the sink speaks an empty string — silence is the
    # worst outcome on the voice channel, and it is exactly what the raw CLI
    # error text must NOT be replaced by.
    "refusal":       "[flat] That one was declined. Try asking it differently.",
    "api_error":     "[flat] Claude returned an error. Try again in a moment.",
    "unknown":       "[flat] Sorry, something went wrong.",
    # S-1 (2026-07-15, cid 93f501bb): a turn that completes with ZERO spoken
    # output (e.g. max_turns exhausted on ToolSearch round-trips) must never
    # end as a bare `done` — a voice user just hears silence.
    "empty_turn":    "[apologetic] Sorry, I lost my train of thought — "
                     "could you ask that again?",
    # #594: NOT an error kind — the sentence prepended to whichever error line
    # above is spoken, when this turn already voiced real speech. A voice write
    # is irreversible, so a fault after a partial answer otherwise leaves the
    # listener holding a statement Casa never stood behind. Overridable per
    # persona through `voice_errors` like any other line.
    "retraction":    "[flat] Disregard that —",
}


class _SpeechDelivered:
    """#594: whether real speech actually REACHED the listener this turn.

    Deliberately NOT ``speech_block_sent``, which answers "was speech
    SELECTED". The socket handler sets that flag before starting its write, on
    purpose, so a tool cannot claim a handoff while speech is in flight; SSE,
    which has no handoff reservation to protect, sets it after. Selection is
    the right question for handoff exclusion and the wrong one for a
    retraction, and both halves of that mismatch were reproduced: a socket
    write the transport rejects selects speech the listener never hears, and
    on either transport the final tail block delivers speech without changing
    selection at all. Three review rounds found the same shape of defect while
    the retraction borrowed that flag, so it now has one that answers its own
    question — recorded only by a site that has COMPLETED a speech write, and
    read only by the retraction.
    """

    __slots__ = ("_delivered",)

    def __init__(self) -> None:
        self._delivered = False

    def record(self) -> None:
        self._delivered = True

    def delivered(self) -> bool:
        return self._delivered

    def __bool__(self) -> bool:
        return self._delivered

# A4 (spec A4): voice turn-budget envelope. INTEGRATION_TIMEOUT_TOTAL is
# the total wall-clock time the voice transport (SSE/WS plus any fronting
# proxy) gives one turn before ITS OWN timeout would fire — a synchronous
# specialist delegation must always leave that much room. The hard cap
# holds even if INTEGRATION_TIMEOUT_TOTAL is raised later.
INTEGRATION_TIMEOUT_TOTAL: float = 30.0
_VOICE_TURN_BUDGET_HARD_CAP_S: float = 27.0


def _voice_turn_budget_s() -> float:
    """Effective per-turn delegation budget (spec A4).

    ``min(INTEGRATION_TIMEOUT_TOTAL - 3, 27)``. This is derived, not
    configured: the 3s reserve is what the companion integration needs to
    return a turn before ITS OWN timeout fires, so the only honest budget is
    the one the transport actually allows.

    v0.125.0 (#228) removed the ``voice_turn_budget_seconds`` option and its
    ``VOICE_TURN_BUDGET_SECONDS`` env var. It offered no operator decision:
    the value was already hard-capped at 27 here, so every setting other than
    the default could only shorten a turn and starve delegations — and the
    sub-10s floor, NaN rejection and schema rail all existed solely to defend
    against that option being set badly.
    """
    return min(INTEGRATION_TIMEOUT_TOTAL - 3.0, _VOICE_TURN_BUDGET_HARD_CAP_S)


def _compose_block(
    carry_sep: str,
    block: Block,
    adapter: TagDialectAdapter,
    *,
    fallback_sep: str = "",
) -> tuple[str, str]:
    """Render one splitter block into wire text (issue #257).

    Returns ``(wire_text, carry_sep)``. The companion integration streams
    every block straight into HA's delta stream and concatenates them
    verbatim, so the separator the splitter carries on ``block.sep`` has to
    ride ON the wire text — there is no second field HA would join with.

    Three things must not lose a separator:

    * ``TagDialectAdapter.render`` lstrips (dialect ``none``), so the
      separator is prepended AFTER rendering, never passed through it.
    * A block whose text renders to nothing (a tag-only block under dialect
      ``none``) emits no frame at all — the integration drops empty frames,
      which would take the separator with it. Its separator is carried
      forward onto the next frame instead, which is why two whitespace runs
      can legitimately meet here: with the tag deleted, the source really
      did have whitespace on both sides of it.
    * ``fallback_sep`` covers a frame this turn wrote OUTSIDE the splitter
      (the synthetic progress line): nothing downstream carries a separator
      against it. It applies only when the stream itself supplies none —
      a fallback, never a summand, or a progress line followed by text that
      already starts on a new paragraph would gain a stray leading space.
    """
    rendered = adapter.render(block.text)
    stream_sep = carry_sep + block.sep
    if not rendered:
        return "", stream_sep
    return (stream_sep or fallback_sep) + rendered, ""


class VoiceChannel(Channel):
    name: str = "voice"

    def __init__(
        self,
        bus: MessageBus,
        default_agent: str,
        webhook_secret: str,
        sse_path: str,
        ws_path: str,
        agent_configs: Mapping[str, Any],
        memory: SemanticMemory,
        idle_timeout: int,
        sse_enabled: bool = True,
        ws_enabled: bool = True,
        rate_limiter: RateLimiter | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        route_registry: VoiceRouteRegistry | None = None,
        delivery_coordinator: Any | None = None,
        handoff_coordinator: VoiceHandoffCoordinator | None = None,
    ) -> None:
        self._bus = bus
        self.default_agent = default_agent
        self._webhook_secret = webhook_secret
        self._sse_path = sse_path
        self._ws_path = ws_path
        self._sse_enabled = sse_enabled
        self._ws_enabled = ws_enabled
        self._agent_configs = agent_configs
        self._memory = memory
        self.pool = VoiceSessionPool(idle_timeout=idle_timeout)
        self._sweeper: asyncio.Task | None = None
        # Per-scope_id rate limit (spec 5.2 §8). None = unlimited.
        self._rate_limiter = rate_limiter
        self._monotonic = monotonic
        self.routes = route_registry or VoiceRouteRegistry(
            secret_present=bool(webhook_secret),
            agent_configs=agent_configs,
        )
        self._delivery = delivery_coordinator
        self._handoff = handoff_coordinator

    # --- Channel ABC --------------------------------------------------

    async def start(self) -> None:
        if self._delivery is not None:
            await self._delivery.start()
        self._sweeper = asyncio.create_task(self.pool.run_sweeper())
        logger.info(
            "Voice channel active (sse=%s, ws=%s, sse_path=%s, ws_path=%s)",
            self._sse_enabled, self._ws_enabled, self._sse_path, self._ws_path,
        )

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
            self._sweeper = None
        if self._delivery is not None:
            await self._delivery.stop()

    # Voice never delivers an agent's FINAL text out-of-band (send() below is
    # a no-op) — agent.handle_message reads this to keep one-shot prepends
    # (the §3.10 plugin-health notice) pending for a channel that can show
    # them, instead of counting the no-op as a confirmed delivery (#349).
    delivers_final_text = False

    async def send(self, message: str, context: dict) -> None:
        # Voice has no out-of-band send path — responses are delivered
        # inline on the request's transport. No-op for the ChannelManager's
        # outbound registration (kept so the Channel ABC is satisfied).
        return None

    # --- create_on_token: adapter for the production Agent path -------

    def create_on_token(self, context: dict) -> Callable[[str], Awaitable[None]]:
        """Return the per-utterance streaming callback stored in context.

        The SSE/WS handler stashes the real callback in
        ``context["_on_token"]`` before dispatching on the bus. The
        production ``Agent._process`` resolves it via
        ``channel_manager.get("voice").create_on_token(msg.context)`` —
        so both the test stub (which reads context directly) and the
        production agent (which goes through this method) converge on
        the same callable.
        """
        cb = context.get("_on_token")
        if cb is None:
            async def _noop(_text: str) -> None:
                return None
            return _noop
        return cb

    # --- Route registration -------------------------------------------

    def register_routes(self, app: web.Application) -> None:
        if not (self._sse_enabled or self._ws_enabled):
            return
        app.router.add_get(
            VOICE_AGENT_CATALOG_PATH,
            self._voice_agent_catalog_handler,
        )
        if self._sse_enabled:
            app.router.add_post(self._sse_path, self._sse_handler)
        if self._ws_enabled:
            app.router.add_get(self._ws_path, self._ws_handler)

    async def _voice_agent_catalog_handler(
        self,
        request: web.Request,
    ) -> web.Response:
        # _verify is fail-closed on a missing secret and a non-ASCII
        # signature header (#287) — one mechanism for all three routes.
        if not self._verify(request, b""):
            return web.json_response(
                {"error": "invalid signature"}, status=401,
            )
        try:
            payload = build_voice_agent_catalog(self._agent_configs)
        except VoiceAgentCatalogError as err:
            logger.error(
                "Voice agent catalog unavailable reason=%s", err.args[0],
            )
            return web.json_response(
                {"error": "voice catalog unavailable"},
                status=503,
            )
        return web.json_response(
            payload,
            headers={"Cache-Control": "no-store"},
        )

    # --- HMAC ---------------------------------------------------------

    def _verify(self, request: web.Request, body: bytes) -> bool:
        """HMAC-SHA256 of *body* vs ``X-Webhook-Signature``. FAIL-CLOSED.

        #193 (v0.117.0): with no configured secret this returned ``True``, so
        the SSE turn path and the WS upgrade accepted UNSIGNED requests. The
        external ``:18065`` server block proxies these routes, so with webhook
        auth off an attacker could POST an arbitrary prompt to ``/api/converse``
        and reach the butler (which drives Home Assistant). No secret now means
        the voice routes are OFF, matching ``/invoke``, ``/telegram/update`` and
        the voice-agent catalog (which was already fail-closed here).

        Safe for the first-party LAN path: the companion `ha-casa-integration`
        signs EVERY request and cannot even be configured without a secret —
        both of its config flows require one and validate it against the
        already-fail-closed ``/api/voice/agents`` catalog, so a working voice
        install always has a secret.
        """
        if not self._webhook_secret:
            return False
        sig = request.headers.get("X-Webhook-Signature", "")
        # #287: compare_digest raises TypeError on a non-ASCII str — a
        # malformed header must be a 401, not a 500, on every route.
        if not sig.isascii():
            return False
        expected = hmac.new(
            self._webhook_secret.encode(), body, hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    # --- SSE ----------------------------------------------------------

    async def _sse_handler(self, request: web.Request) -> web.StreamResponse:
        ingress_started_ms = self._monotonic() * 1000
        # A4: capture the deadline at TRUE ingress — the first line of the
        # handler, BEFORE body-read/HMAC/JSON validation — so those (I/O-
        # bound, potentially slow) steps are counted against the 27s window
        # rather than silently extending it past HA's ~30s transport
        # timeout. Monotonic (loop.time()).
        voice_deadline = asyncio.get_running_loop().time() + _voice_turn_budget_s()

        body = await request.read()
        if not self._verify(request, body):
            return web.json_response({"error": "invalid signature"}, status=401)
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        # #287: valid JSON with a non-object top level (array/string/number)
        # would raise AttributeError on .get below — refuse it as a 400.
        if not isinstance(payload, dict):
            return web.json_response({"error": "invalid JSON"}, status=400)

        # #287 r2: field-level shape checks — a non-str prompt/agent_role from
        # an authenticated caller raised (unhashable dict key, str ops) → 500.
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            return web.json_response({"error": "missing 'prompt'"}, status=400)

        agent_role = payload.get("agent_role", self.default_agent)
        if not isinstance(agent_role, str):
            # Same body as an unknown role — no shape oracle.
            return web.json_response({"error": "unknown agent_role"}, status=404)
        cfg = self._agent_configs.get(agent_role)
        # Fail-closed channel-capability gate (spec A3): unknown role and a
        # role that never declared ha_voice get the SAME 404 body — no
        # existence oracle for residents that exist but aren't voice-reachable.
        if cfg is None or not agent_allowed_on("voice", cfg):
            return web.json_response({"error": "unknown agent_role"}, status=404)

        scope_id = self._resolve_scope_id(payload)
        self.pool.ensure(scope_id, role=agent_role)
        self.pool.touch(scope_id, role=agent_role)

        # Rate limit BEFORE opening the SSE stream (spec 5.2 §8).
        if self._rate_limiter is not None and self._rate_limiter.enabled:
            decision = self._rate_limiter.check((agent_role, scope_id))
            if not decision.allowed:
                logger.info(
                    "Voice SSE rate limit hit for role=%s scope_id=%s",
                    agent_role, scope_id,
                )
                response = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
                await response.prepare(request)
                adapter = TagDialectAdapter(cfg.tts.tag_dialect)
                line = VoiceChannel._error_line_for_kind(cfg, "rate_limit")
                await _write_sse(response, "error", {
                    "kind": "rate_limit",
                    "spoken": adapter.render(line) if line else "",
                })
                return response

        utterance_id = str(uuid.uuid4())

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        splitter = ProsodicSplitter()
        adapter = TagDialectAdapter(cfg.tts.tag_dialect)
        last_text = ""
        # A4: write_lock serializes real SDK-streamed blocks (on_token)
        # against the synthetic progress block (_progress_sink below). The
        # flag CHECK, the wire WRITE, and the flag MUTATION all happen under
        # the SAME held lock in both closures, so there is no window where
        # the progress sink can observe speech_block_sent=False, queue
        # behind an in-flight on_token write, and then emit progress AFTER
        # real speech. speech_block_sent flips ONLY after a real block is
        # actually written (not on any token/last_text update); progress_sent
        # flips ONLY after the progress block is actually written.
        write_lock = asyncio.Lock()
        speech_block_sent = False
        progress_sent = False
        spoke = _SpeechDelivered()   # #594 — delivery, not selection
        first_block_logged = False
        # #257: two debts to the NEXT wire frame, both mutated only under
        # write_lock. `carry_sep` is whitespace the CURRENT splitter epoch
        # owes, taken verbatim from the stream (a block that rendered to
        # nothing hands its separator on). `fallback_gap` is a gap owed by
        # something ALREADY ON THE WIRE that no stream whitespace answers
        # for — the synthetic progress line, or a superseded epoch — and it
        # applies only when the stream supplies no separator of its own, so
        # gaps are never summed into a double space.
        carry_sep = ""
        fallback_gap = ""

        def _log_first_block() -> None:
            nonlocal first_block_logged
            if first_block_logged:
                return
            logger.info(
                "voice_first_block role=%s transport=sse ms=%d",
                agent_role,
                int(self._monotonic() * 1000 - ingress_started_ms),
            )
            first_block_logged = True

        async def on_token(accumulated: str) -> None:
            nonlocal last_text, splitter, speech_block_sent
            nonlocal carry_sep, fallback_gap
            if not accumulated.startswith(last_text):
                # AR-B (2026-07-11 design §2 point 3): a mid-turn SDK retry
                # or a divergent canonical correction breaks the
                # "accumulated always extends last_text" assumption the
                # delta slice below depends on. Reset to a fresh splitter
                # so the new cumulative re-renders cleanly from its own
                # start instead of computing a bogus delta (mid-word
                # garbage, or a silently swallowed restart).
                logger.debug(
                    "voice sse on_token non-prefix cumulative "
                    "(len=%d vs last_text len=%d); resetting splitter "
                    "scope_id=%s",
                    len(accumulated), len(last_text), scope_id,
                )
                # #257: the discarded epoch's owed whitespace must not be
                # prefixed verbatim onto the corrected reply (it can stack
                # onto a gap already owed), but it must not simply vanish
                # either — if a frame was already written, that whitespace
                # is what separates it from whatever the new epoch says
                # ("Old.Hi."). Demote it to the fallback slot: one gap,
                # applied only if the new epoch brings no separator itself,
                # and only when something of this turn actually reached the
                # wire — a gap with no predecessor to anchor it separates
                # nothing, and would just open the reply with whitespace.
                # The gap may be owed by the channel (a block that rendered
                # to nothing) AND still held inside the splitter we are
                # about to throw away — around a suppressed block the two
                # runs are adjacent, so they concatenate exactly as they do
                # in _compose_block. Read the splitter's half BEFORE
                # replacing it.
                if progress_sent or speech_block_sent:
                    fallback_gap = fallback_gap or (
                        carry_sep + splitter.pending_sep
                    )
                carry_sep = ""
                last_text = ""
                splitter = ProsodicSplitter()
            delta = accumulated[len(last_text):]
            last_text = accumulated
            for block in splitter.feed(delta):
                async with write_lock:
                    text, carry_sep = _compose_block(
                        carry_sep, block, adapter,
                        fallback_sep=fallback_gap,
                    )
                    if not text:
                        continue
                    await _write_sse(response, "block", {
                        "text": text,
                        "final": False,
                    })
                    fallback_gap = ""
                    _log_first_block()
                    speech_block_sent = True
                    spoke.record()

        async def _progress_sink(text: str) -> None:
            # A4: deterministic "still working" block for a mid-turn
            # specialist delegation. Exactly once per outer voice turn
            # (progress_sent) and suppressed once the turn has spoken any
            # REAL content (speech_block_sent). The check + write + mutation
            # are ALL under the lock — see the write_lock comment above.
            # Writes a real wire `block` — NOT via on_token, whose
            # cumulative-prefix `last_text` bookkeeping would corrupt on a
            # manually-injected block not part of the accumulated SDK text.
            nonlocal progress_sent, fallback_gap
            async with write_lock:
                if progress_sent or speech_block_sent:
                    return
                await _write_sse(response, "block", {
                    "text": adapter.render(text), "final": False,
                })
                progress_sent = True
                # #257: this line never went through the splitter, so no
                # block carries a separator against it. Real speech that
                # follows would otherwise join straight onto its full stop.
                fallback_gap = " "

        error_emitted = False

        async def _error_sink(kind: str, spoken: str) -> None:
            nonlocal error_emitted
            await _write_sse(response, "error", {
                "kind": kind, "spoken": spoken,
            })
            error_emitted = True

        external_context = sanitize_external_context(payload.get("context"))
        msg = BusMessage(
            type=MessageType.REQUEST,
            source="voice",
            target=agent_role,
            content=prompt,
            channel="voice",
            context={
                # Sanitize-and-preserve (A:§3.5): payload["context"] is
                # caller-supplied (the SSE POST body) — strip Casa-reserved
                # provenance keys before Casa's own keys are merged in below.
                **external_context,
                "chat_id": scope_id,
                "utterance_id": utterance_id,
                "cid": request["cid"],
                "_on_token": on_token,
                "_error_sink": _error_sink,
                # #594: read at error time, and answering "did the listener
                # hear anything", not "was speech selected".
                "_spoken_any": spoke.delivered,
                "_voice_deadline": voice_deadline,
                "_progress_sink": _progress_sink,
                # SSE can complete only the live request. It never advertises
                # an out-of-band delivery route, even when its external
                # context contains route-shaped spoof fields.
                "_voice_transport": "sse",
            },
            # Task 9: anonymous but trusted voice speaker — server-created
            # ingress identity (never decoded from the SSE payload), resolved
            # through the declarative ingress table (#203).
            trusted_user_origin=ingress_identity("voice_sse"),
        )

        try:
            result = await self._bus.request(msg, timeout=300)
            if error_emitted:
                return response
            tail = splitter.flush_tail()
            tail_text = ""
            if tail is not None:
                tail_text, carry_sep = _compose_block(
                    carry_sep, tail, adapter, fallback_sep=fallback_gap,
                )
            if tail_text:
                await _write_sse(response, "block", {
                    "text": tail_text,
                    "final": True,
                })
                fallback_gap = ""
                _log_first_block()
                # #594: for an answer with no sentence boundary the splitter
                # held ALL of it until this flush, so this is the only speech
                # the listener gets — and the terminal `done` write after it
                # can still fail. Selection is untouched: this is delivery.
                spoke.record()
            elif not speech_block_sent:
                # S-1: zero spoken output for the whole turn — emit a typed
                # empty_turn error line instead of a silent bare `done`
                # (mirrors every other error path: error frame, no done).
                line = self._error_line_for_kind(cfg, "empty_turn")
                await _write_sse(response, "error", {
                    "kind": "empty_turn",
                    "spoken": adapter.render(line) if line else "",
                })
                return response
            await _write_sse(response, "done", {})
        except asyncio.CancelledError:
            # Client disconnect mid-stream — do NOT emit `event: done`.
            # Pool entry already created above, stays alive per spec §10.3.
            raise
        except Exception as exc:
            # #594 (Sol + Terra diff-r1, S1): a fault that never reaches
            # `Agent.handle_message`'s classified branch lands here — the
            # reachable one is the 300s `MessageBus.request` timeout, which is
            # exactly a fault arriving AFTER partial speech. This path composed
            # its own frame and so skipped the retraction, leaving the listener
            # holding a half-answer Casa did not stand behind. It still writes
            # unconditionally — this is the last resort, and a silent failure
            # here is worse than a late one — but the text now goes through the
            # same composition the sink uses.
            line = self._error_line(cfg, exc)
            await _write_sse(response, "error", {
                "kind": _classify_error(exc).value,
                "spoken": self._with_retraction(
                    cfg, adapter, line, already_spoke=bool(spoke)),
            })

        return response

    # --- helpers ------------------------------------------------------

    @staticmethod
    def _resolve_scope_id(payload: dict) -> str:
        # #287 r2: only a non-empty str may become a scope id — a list/dict
        # here raised later as an unhashable session key (500 on SSE), and a
        # non-dict ``context`` raised on ``.get``. Malformed values fall
        # through to the next candidate / "anon" instead.
        sid = payload.get("scope_id")
        if isinstance(sid, str) and sid:
            return sid
        ctx = payload.get("context")
        if not isinstance(ctx, dict):
            ctx = {}
        for key in ("user_id", "device_id", "conversation_id"):
            value = ctx.get(key)
            if isinstance(value, str) and value:
                return value
        return "anon"

    @staticmethod
    def _error_line(cfg: Any, exc: Exception) -> str:
        kind = _classify_error(exc).value
        return VoiceChannel._error_line_for_kind(cfg, kind)

    @staticmethod
    def _error_line_for_kind(cfg: Any, kind: str) -> str:
        lines = getattr(cfg, "voice_errors", {}) or {}
        return lines.get(kind) or _DEFAULT_ERROR_LINES.get(kind, "")

    @staticmethod
    def _with_retraction(
        cfg: Any, adapter: Any, line: str, *, already_spoke: bool,
    ) -> str:
        """Render *line* (canonical form), prefixed by a retraction when this
        turn already put speech in the listener's ear (#594).

        Composition ONLY — it never decides who writes the frame or whether
        one is written. That separation is the point: the two transports'
        last-resort error paths must keep writing unconditionally (a handoff
        whose own write failed still owes the caller an error frame — see
        ``test_failed_handoff_write_does_not_fake_a_terminal_success``),
        while the ordinary sink keeps its write lock and handoff suppression.
        Routing those paths through the sink to reuse this text was tried and
        silently swallowed both cases.

        Takes the CANONICAL line rather than a rendered one so both guards can
        ask whether something is actually *spoken*, which a rendered string
        cannot answer under a tag-preserving dialect (:func:`has_speech`):

        - an error line with nothing speakable in it is never retracted — a
          retraction with no reason after it is precisely the outcome this
          change exists to prevent;
        - a retraction with nothing speakable in it is not prefixed, and an
          explicitly empty ``voice_errors.retraction`` switches retractions
          off entirely, while an ABSENT key takes the default.
          ``_error_line_for_kind``'s ``get(kind) or default`` cannot tell an
          absent key from an empty one, and here that difference is a decision
          the persona made.
        """
        spoken = adapter.render(line) if line else ""
        if not already_spoke or not has_speech(line):
            return spoken
        lines = getattr(cfg, "voice_errors", {}) or {}
        retraction = (
            lines["retraction"] if "retraction" in lines
            else _DEFAULT_ERROR_LINES["retraction"]
        )
        if not has_speech(retraction):
            return spoken
        return f"{adapter.render(retraction)} {spoken}"

    async def emit_error_line(
        self, kind: str, context: dict, agent_cfg: Any,
    ) -> bool:
        """Emit a persona-voice error line via the per-request sink.

        Called by Agent.handle_message's error branch. Returns True if
        the error was delivered to the client (caller should suppress
        normal text delivery). Returns False if no sink is present
        (e.g. this was called outside a live SSE/WS request).

        #594: when this turn has ALREADY put real speech on the wire, the
        error line is a correction of something the listener heard, so it is
        prefixed with a retraction. The predicate is the transport's own
        ``_spoken_any`` — its :class:`_SpeechDelivered` witness, recorded only
        by a COMPLETED speech write, never by the "still working" progress
        notice. Composed here rather than in the two sinks so SSE and WS cannot
        drift, and emitted as ONE frame: a retraction and its reason must not
        be two writes that can split, leaving a listener with a retraction of
        nothing (Sol/Terra design round, D3-S2).
        """
        sink = context.get("_error_sink")
        if sink is None:
            return False
        line = VoiceChannel._error_line_for_kind(agent_cfg, kind)
        adapter = TagDialectAdapter(agent_cfg.tts.tag_dialect)
        spoken_any = context.get("_spoken_any")
        spoken = VoiceChannel._with_retraction(
            agent_cfg, adapter, line,
            already_spoke=bool(callable(spoken_any) and spoken_any()),
        )
        await sink(kind, spoken)
        return True

    # --- WS ------------------------------------------------------------

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        if not self._verify(request, b""):
            return web.json_response({"error": "invalid signature"}, status=401)

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        connection = VoiceWsConnection(ws)

        # Per-utterance task map so `cancel` frames can target them.
        tasks: dict[str, asyncio.Task] = {}
        # #303: originals displaced by a duplicate utterance_id. Cancelled
        # on replacement, but cancellation is cooperative — they stay here
        # so teardown re-cancels and awaits them like every other task.
        orphans: set[asyncio.Task] = set()

        try:
            async for msg in ws:
                if msg.type.name != "TEXT":
                    continue
                try:
                    frame = json.loads(msg.data)
                except Exception:
                    continue
                if not isinstance(frame, dict):
                    continue
                t = frame.get("type")

                self.routes.touch(connection)

                if t == "voice_route_register":
                    # Bindings this frame may displace: this connection's
                    # own current binding, and another socket's binding for
                    # the requested route id (#304, review round 4).
                    displaceable: list[Any] = []
                    previous = None
                    if connection.voice_route_id is not None:
                        candidate = self.routes.get_connected(
                            connection.voice_route_id,
                        )
                        if (
                            candidate is not None
                            and candidate.connection is connection
                        ):
                            previous = candidate
                            displaceable.append(candidate)
                    requested_id = _nonempty_identifier(
                        frame.get("route_id"),
                    )
                    if requested_id is not None:
                        holder = self.routes.get_connected(requested_id)
                        if (
                            holder is not None
                            and holder.connection is not connection
                        ):
                            displaceable.append(holder)
                    try:
                        bound = await self.routes.register(connection, frame)
                    except BaseException:
                        # #304: register() mutates bindings before its ack
                        # write. If that write fails, teardown reports only
                        # this connection's CURRENT binding — any binding
                        # this frame displaced must be reported here or its
                        # offered attempts stay pinned to it. A refused
                        # frame mutates nothing (each candidate is still
                        # the registry's current), so this fires only when
                        # displacement actually happened.
                        if self._delivery is not None:
                            for displaced in displaceable:
                                if (
                                    self.routes.get_connected(
                                        displaced.route_id,
                                    )
                                    is not displaced
                                ):
                                    await self._delivery.route_disconnected(
                                        displaced,
                                    )
                        raise
                    if bound is not None and self._delivery is not None:
                        if (
                            previous is not None
                            and previous.route_id != bound.route_id
                        ):
                            # #304: re-registering under a new route id
                            # displaces the old binding — emit the same
                            # notification socket teardown emits, or an
                            # offered attempt for the old id is never
                            # re-offered.
                            await self._delivery.route_disconnected(previous)
                        await self._delivery.route_connected(bound)
                    if bound is not None and self._handoff is not None:
                        await self._handoff.route_connected(bound)
                    continue

                if isinstance(t, str) and t.startswith("job_"):
                    if self._delivery is not None:
                        await self._delivery.handle(connection, frame)
                    continue

                if t == "handoff_received":
                    route_id = _nonempty_identifier(
                        getattr(connection, "voice_route_id", None)
                    )
                    bound = (
                        self.routes.get_connected(route_id)
                        if route_id is not None else None
                    )
                    if (
                        bound is not None
                        and bound.connection is connection
                        and self._handoff is not None
                    ):
                        await self._handoff.handle(bound, frame)
                    continue

                if t == "stt_start":
                    scope_id = _nonempty_identifier(frame.get("scope_id"))
                    agent_role = _nonempty_identifier(frame.get("agent_role"))
                    cfg = (
                        self._agent_configs.get(agent_role)
                        if agent_role is not None else None
                    )
                    if (
                        scope_id is not None
                        and agent_role is not None
                        and cfg is not None
                        and agent_allowed_on("voice", cfg)
                    ):
                        # Pool metadata only. SDK prewarm remains the separate,
                        # conditional Tina T2 optimization.
                        self.pool.ensure(scope_id, role=agent_role)
                    continue

                if t == "stage":
                    # Hints only — no-op in 2.3.
                    continue

                if t == "cancel":
                    # #287 r2: a non-str utterance_id raised as an unhashable
                    # dict key and closed the socket — skip the frame instead.
                    uid = frame.get("utterance_id")
                    task = (tasks.get(uid)
                            if isinstance(uid, str) and uid else None)
                    if task is not None and not task.done():
                        task.cancel()
                    continue

                if t == "utterance":
                    uid = frame.get("utterance_id")
                    if not isinstance(uid, str) or not uid:
                        # #287 r2: non-str ids (unhashable → closed socket)
                        # get a server-minted id like absent ones.
                        uid = str(uuid.uuid4())
                    # Server-owned anchors: overwrite any identically named
                    # client fields before handing the frame to the task.
                    frame["_casa_ingress_started_ms"] = self._monotonic() * 1000
                    # #329: pin the route binding AT FRAME RECEIPT. The task
                    # may not run for an unbounded interval, and a
                    # registration frame processed first must not rebind an
                    # already-received utterance (and its deferred answer /
                    # handoff) to the new route.
                    frame["_casa_route_snapshot"] = _connection_voice_route(
                        connection,
                    )
                    # A4: capture the deadline at TRUE ingress — the moment the
                    # utterance frame is RECEIVED here, not inside the separately-
                    # scheduled _run_ws_utterance task (which may not run for an
                    # unbounded interval under load). Monotonic (loop.time()).
                    voice_deadline = (
                        asyncio.get_running_loop().time()
                        + _voice_turn_budget_s()
                    )
                    existing = tasks.get(uid)
                    if existing is not None and not existing.done():
                        # #303: a retransmitted utterance_id must not orphan
                        # the in-flight original — once the map entry is
                        # replaced, no cancel frame or teardown can ever
                        # reach it again.
                        existing.cancel()
                        orphans.add(existing)
                        existing.add_done_callback(orphans.discard)
                    task = asyncio.create_task(
                        self._run_ws_utterance(
                            connection, frame, uid, voice_deadline,
                        ),
                    )
                    tasks[uid] = task

                    def _reap(
                        done_task: asyncio.Task, uid: str = uid,
                    ) -> None:
                        # Prune only if this entry wasn't overwritten by a
                        # duplicate uid. `done_task` (the callback's own arg)
                        # IS the finished task, so this closure never holds a
                        # separate strong reference to it beyond the callback's
                        # own (transient) invocation.
                        if tasks.get(uid) is done_task:
                            tasks.pop(uid, None)
                        if done_task.cancelled():
                            return
                        # Retrieve so GC never logs 'never retrieved'.
                        exc = done_task.exception()
                        if exc is not None:
                            logger.warning(
                                "Voice WS utterance task failed "
                                "(utterance_id=%s): %s",
                                uid, exc,
                            )

                    task.add_done_callback(_reap)
                    # Drop the frame-local reference so a finished task is not
                    # kept alive by this coroutine's own suspended stack frame
                    # while it awaits the next WS frame.
                    del task
                    continue
        finally:
            # Clear the server-bound writer even when a handler or server
            # shutdown aborts the reader loop.
            pending = [
                task for task in (*tasks.values(), *orphans)
                if not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            disconnected = await self.routes.disconnect(connection)
            if disconnected is not None and self._delivery is not None:
                await self._delivery.route_disconnected(disconnected)
        return ws

    async def _run_ws_utterance(
        self, ws: VoiceWsConnection, frame: dict, uid: str,
        voice_deadline: float,
    ) -> None:
        # A4: `voice_deadline` (monotonic loop.time()) is captured by the
        # caller at utterance-frame RECEIPT — see _ws_handler — so any delay
        # between receipt and this task actually running is counted against
        # the budget rather than silently extending it.
        agent_role = frame.get("agent_role", self.default_agent)
        if not isinstance(agent_role, str):
            # #287 r2: a non-str role raised on the config lookup; treat it
            # as unknown so the client gets the error frame below.
            agent_role = ""
        cfg = self._agent_configs.get(agent_role)
        # Fail-closed channel-capability gate (spec A3): a 404 can't follow
        # the WS upgrade, so an unknown role AND a role that never declared
        # ha_voice both get the same `unknown_agent` error frame, emitted
        # BEFORE any bus dispatch.
        if cfg is None or not agent_allowed_on("voice", cfg):
            await ws.send_json({
                "type": "error", "utterance_id": uid,
                "kind": "unknown_agent", "spoken": "",
            })
            return

        scope_id = self._resolve_scope_id({
            "scope_id": frame.get("scope_id"),
            "context": frame.get("context") or {},
        })
        self.pool.ensure(scope_id, role=agent_role)
        self.pool.touch(scope_id, role=agent_role)

        # Rate limit BEFORE dispatching to the agent (spec 5.2 §8).
        if self._rate_limiter is not None and self._rate_limiter.enabled:
            decision = self._rate_limiter.check((agent_role, scope_id))
            if not decision.allowed:
                logger.info(
                    "Voice WS rate limit hit for role=%s scope_id=%s "
                    "utterance_id=%s",
                    agent_role, scope_id, uid,
                )
                adapter = TagDialectAdapter(cfg.tts.tag_dialect)
                line = VoiceChannel._error_line_for_kind(cfg, "rate_limit")
                await ws.send_json({
                    "type": "error", "utterance_id": uid,
                    "kind": "rate_limit",
                    "spoken": adapter.render(line) if line else "",
                })
                return

        splitter = ProsodicSplitter()
        adapter = TagDialectAdapter(cfg.tts.tag_dialect)
        last_text = ""
        error_emitted = False
        # A4: mirrors the SSE handler's write_lock/speech_block_sent —
        # see its on_token for the full rationale.
        write_lock = asyncio.Lock()
        speech_block_sent = False
        progress_sent = False
        spoke = _SpeechDelivered()   # #594 — delivery, not selection
        # #257: see the SSE handler — epoch-owned vs progress-owed whitespace.
        carry_sep = ""
        fallback_gap = ""
        handoff: asyncio.Future[VoiceHandoff] = (
            asyncio.get_running_loop().create_future()
        )
        reservation = VoiceHandoffReservation()

        def commit_handoff(job: VoiceJob) -> None:
            if not handoff.done():
                handoff.set_result(VoiceHandoff.from_job(uid, job))

        reservation.bind_commit(commit_handoff)
        first_block_logged = False
        ingress_started_ms = frame.get("_casa_ingress_started_ms")
        if ingress_started_ms is None:
            # Direct internal callers (tests/helpers) bypass _ws_handler.
            ingress_started_ms = self._monotonic() * 1000

        def _log_first_block() -> None:
            nonlocal first_block_logged
            if first_block_logged:
                return
            logger.info(
                "voice_first_block role=%s transport=ws ms=%d",
                agent_role,
                int(self._monotonic() * 1000 - ingress_started_ms),
            )
            first_block_logged = True

        async def on_token(accumulated: str) -> None:
            nonlocal last_text, splitter, speech_block_sent
            nonlocal carry_sep, fallback_gap
            async with write_lock:
                # A handoff reserve happens before any async prelaunch gate.
                # Do not mutate prefix state while held: a later typed failure
                # releases the reservation and the next cumulative token can
                # resume normal speech from the previous real prefix.
                if reservation.held:
                    return
                if not accumulated.startswith(last_text):
                    # AR-B — see the SSE handler's on_token for rationale.
                    logger.debug(
                        "voice ws on_token non-prefix cumulative "
                        "(len=%d vs last_text len=%d); resetting splitter "
                        "utterance_id=%s scope_id=%s",
                        len(accumulated), len(last_text), uid, scope_id,
                    )
                    # Demote the epoch's owed whitespace to the fallback
                    # slot rather than dropping it, only when this turn has
                    # already written something, and reading the splitter's
                    # own held gap before discarding it — see the SSE
                    # handler for the full rationale.
                    if progress_sent or speech_block_sent:
                        fallback_gap = fallback_gap or (
                            carry_sep + splitter.pending_sep
                        )
                    carry_sep = ""
                    last_text = ""
                    splitter = ProsodicSplitter()
                delta = accumulated[len(last_text):]
                last_text = accumulated
                for block in splitter.feed(delta):
                    text, carry_sep = _compose_block(
                        carry_sep, block, adapter,
                        fallback_sep=fallback_gap,
                    )
                    if not text:
                        # Rendered to nothing; its separator is now owed to
                        # the next frame (#257). Nothing was spoken, so the
                        # handoff reservation must not be closed out either.
                        continue
                    # Select speech before starting the asynchronous write.
                    # `reserve()` is synchronous, so this prevents a tool
                    # call from claiming a handoff while this write is in
                    # flight and later producing both terminal paths.
                    speech_block_sent = True
                    reservation.mark_speech_sent()
                    await ws.send_json({
                        "type": "block", "utterance_id": uid,
                        "text": text, "final": False,
                    })
                    # AFTER the write: `speech_block_sent` above is set before
                    # it on purpose (handoff selection), this is not.
                    spoke.record()
                    fallback_gap = ""
                    _log_first_block()

        async def _progress_sink(text: str) -> None:
            # A4: see the SSE handler's _progress_sink for the full
            # exactly-once / suppress-after-real-speech rationale — the
            # check + write + mutation all happen under the held lock.
            nonlocal progress_sent, fallback_gap
            async with write_lock:
                if reservation.held or progress_sent or speech_block_sent:
                    return
                await ws.send_json({
                    "type": "block", "utterance_id": uid,
                    "text": adapter.render(text), "final": False,
                })
                progress_sent = True
                # #257: see the SSE handler — this synthetic line owes the
                # next real block a separator.
                fallback_gap = " "

        async def _error_sink(kind: str, spoken: str) -> None:
            nonlocal error_emitted
            async with write_lock:
                # A Task-3 commit has already selected the terminal handoff.
                # Never append an ordinary foreground error to that frame.
                if handoff.done() or reservation.committed:
                    return
                await ws.send_json({
                    "type": "error", "utterance_id": uid,
                    "kind": kind, "spoken": spoken,
                })
                error_emitted = True

        external_context = sanitize_external_context(frame.get("context"))
        snapshot = frame.get("_casa_route_snapshot")
        if isinstance(snapshot, tuple) and len(snapshot) == 3:
            # #329: stamped by _ws_handler at frame receipt (server-owned —
            # any client-supplied value was overwritten there). Direct
            # internal callers without a snapshot read the live binding.
            route_id, route_capabilities, job_control_id = snapshot
        else:
            route_id, route_capabilities, job_control_id = (
                _connection_voice_route(ws)
            )
        # The integration frame is authenticated by the WS route.  Ordinary
        # external context remains available to the agent but must never be
        # promoted into trusted job-delivery provenance.
        origin_device_id = _nonempty_identifier(frame.get("device_id"))
        trusted_route_context: dict[str, Any] = {
            "_voice_transport": "ws",
        }
        if route_id is not None:
            trusted_route_context["_voice_route_id"] = route_id
        if route_capabilities:
            trusted_route_context["_voice_route_capabilities"] = (
                route_capabilities
            )
        if origin_device_id is not None:
            trusted_route_context["_origin_device_id"] = origin_device_id
        delivery_offer = sanitize_delivery_offer(frame.get("delivery_offer"))
        if delivery_offer is not None:
            trusted_route_context["_voice_delivery_offer"] = delivery_offer
        if job_control_id is not None:
            trusted_route_context["_voice_job_control_id"] = job_control_id

        bus_msg = BusMessage(
            type=MessageType.REQUEST, source="voice", target=agent_role,
            content=frame.get("text", ""),
            channel="voice",
            context={
                # Sanitize-and-preserve (A:§3.5): frame["context"] is
                # caller-supplied (the WS utterance frame) — strip
                # Casa-reserved provenance keys before Casa's own keys are
                # merged in below.
                **external_context,
                "chat_id": scope_id, "utterance_id": uid,
                "cid": new_cid(),
                "_on_token": on_token,
                "_error_sink": _error_sink,
                # #594: read at error time, and answering "did the listener
                # hear anything", not "was speech selected".
                "_spoken_any": spoke.delivered,
                "_voice_deadline": voice_deadline,
                "_progress_sink": _progress_sink,
                "_voice_handoff_reservation": reservation,
                **trusted_route_context,
            },
            # Task 9: anonymous but trusted voice speaker — server-created
            # ingress identity (never decoded from the WS frame), resolved
            # through the declarative ingress table (#203).
            trusted_user_origin=ingress_identity("voice_ws"),
        )

        request_task = asyncio.create_task(
            self._bus.request(bus_msg, timeout=300),
            name=f"voice-request-{uid}",
        )
        try:
            done, _ = await asyncio.wait(
                {request_task, handoff},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if handoff in done:
                try:
                    foreground_handoff = handoff.result()
                    async with write_lock:
                        await ws.send_json(foreground_handoff.frame())
                except BaseException:
                    # The pending latch remains durable for Task 3's reconnect
                    # reoffer.  Tear down only this foreground request before
                    # taking the ordinary connection/error path below.
                    if not request_task.done():
                        request_task.cancel()
                    await asyncio.gather(request_task, return_exceptions=True)
                    raise
                if not request_task.done():
                    request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
                return

            # The normal request won.  Its handler has either released a
            # prelaunch reservation or never reserved it, so retain all prior
            # streaming/done behaviour and consume the unused callback waiter.
            handoff.cancel()
            await asyncio.gather(handoff, return_exceptions=True)
            await request_task
            if error_emitted:
                return
            tail = splitter.flush_tail()
            tail_text = ""
            if tail is not None:
                tail_text, carry_sep = _compose_block(
                    carry_sep, tail, adapter, fallback_sep=fallback_gap,
                )
            if tail_text:
                await ws.send_json({
                    "type": "block", "utterance_id": uid,
                    "text": tail_text, "final": True,
                })
                fallback_gap = ""
                _log_first_block()
                # #594 — the socket twin of the SSE tail write.
                spoke.record()
            elif not speech_block_sent:
                # S-1: zero spoken output — typed empty_turn error, never a
                # silent bare `done`. See the SSE handler for rationale.
                line = self._error_line_for_kind(cfg, "empty_turn")
                await ws.send_json({
                    "type": "error", "utterance_id": uid,
                    "kind": "empty_turn",
                    "spoken": adapter.render(line) if line else "",
                })
                return
            await ws.send_json({"type": "done", "utterance_id": uid})
        except asyncio.CancelledError:
            # Cancellation from a `cancel` frame — drop partial state; do
            # not emit `done`. Pool stays alive per spec §10.3.
            if not request_task.done():
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
            if not handoff.done():
                handoff.cancel()
            raise
        except Exception as exc:
            # #594 — the socket twin of the SSE branch above. Routing this
            # through `emit_error_line`'s sink instead (as both reviewers
            # prescribed) was tried and reverted: the sink suppresses an
            # ordinary foreground error once a handoff is committed, which is
            # right for the agent's error branch and wrong here — a handoff
            # whose own write FAILED still owes the caller an error frame
            # (`test_failed_handoff_write_does_not_fake_a_terminal_success`).
            # So this keeps writing unconditionally and borrows only the text.
            line = self._error_line(cfg, exc)
            await ws.send_json({
                "type": "error", "utterance_id": uid,
                "kind": _classify_error(exc).value,
                "spoken": self._with_retraction(
                    cfg, adapter, line, already_spoke=bool(spoke)),
            })

async def _write_sse(response: web.StreamResponse, event: str, data: dict) -> None:
    payload = (
        f"event: {event}\n"
        f"data: {json.dumps(data)}\n\n"
    )
    await response.write(payload.encode("utf-8"))
