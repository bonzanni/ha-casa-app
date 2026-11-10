"""The Telegram transport: every bot endpoint Casa runs, behind one channel.

Each Telegram bot is one endpoint (a ``TelegramChannel``) that owns its own
Application, update stream and transport-local state. The transport holds the
endpoints by bot id and is the only object registered as the ``"telegram"``
channel. It resolves every outbound call to exactly one endpoint, by one of
three named rules, and never by guessing:

* **A, address-bound** — :meth:`TelegramTransport.endpoint`: the bot of a
  stored :class:`~channels.tg_address.TgAddress` (or a bare bot id). A bot with
  no endpoint raises :class:`~channels.tg_address.EndpointUnavailable`; it is
  never answered by another endpoint.
* **B, route-bound** — :meth:`TelegramTransport.endpoint_for_route`: the route
  a turn carries in its context (``_tg_bot``). A context with no route raises
  :class:`~channels.tg_address.RouteMissing`; there is no fallback to the role
  or to Casa's endpoint, so a builder that dropped the route fails loudly.
* **C, Casa-bound** — :meth:`TelegramTransport.casa_endpoint`: Casa's own
  cards and notices, which always go through Casa's bot.

Resolution returns an endpoint *object* and the endpoint's own method runs
exactly as it would alone. An endpoint that is not ready yet behaves as it
always has, per method; the transport adds no outcome of its own.

A route is decided once, where a turn is born. A turn born with no parent asks
:meth:`TelegramTransport.route_for`. Casa's endpoint may not know its bot id
yet (a first boot, or a replaced token), in which case its route is
:data:`~channels.tg_address.CASA`, which resolves to that endpoint at delivery.
The id Casa's bot verified is persisted by :class:`CasaBotIdStore`, so on a
normal restart every turn is born with the integer route even before the bot
has reconnected.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from atomic_io import PRIVATE, atomic_write_json
from channels import Channel, DeliveryOutcome
from channels.tg_address import (
    CASA,
    AddressError,
    DuplicateEndpoint,
    EndpointUnavailable,
    Route,
    RouteMissing,
    TgAddress,
    is_route,
)

if TYPE_CHECKING:
    from channels.telegram import TelegramChannel

logger = logging.getLogger(__name__)

__all__ = [
    "CasaBotIdStore",
    "TelegramTransport",
    "telegram_transport",
    "token_digest",
    "webhook_secret_for",
]


def token_digest(token: str) -> str:
    """A short fingerprint of a bot token: ``sha256(token)`` in hex, first 16
    characters. Stored beside a bot id so a replaced token is noticed without
    storing the token itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def webhook_secret_for(root_secret: str, bot_id: int) -> str:
    """The webhook secret of one bot's update route, derived from the one
    configured secret: ``HMAC-SHA256(root_secret, "telegram-update:<bot_id>")``
    in hex (64 characters, inside Telegram's ``secret_token`` alphabet). A
    request signed for one bot cannot enter another bot's route.

    ``""`` when no root secret is configured, so the route fails closed the
    way it does without a secret."""
    if not root_secret:
        return ""
    return hmac.new(root_secret.encode("utf-8"),
                    f"telegram-update:{bot_id}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


class CasaBotIdStore:
    """The bot id Casa's own endpoint last verified, persisted with the digest
    of the token it was verified for.

    It lets a restart know Casa's bot id before the bot reconnects, so a turn
    born in that window gets the integer route. An id stored for another
    token is not trusted: a replaced token may belong to another bot, so until
    the new token's ``getMe`` succeeds the route stays
    :data:`~channels.tg_address.CASA`."""

    def __init__(self, path: Path, token: str) -> None:
        self._path = Path(path)
        self._digest = token_digest(token)
        self._loaded = False
        self._stored: tuple[int, str] | None = None

    def _load(self) -> tuple[int, str] | None:
        if not self._loaded:
            self._loaded = True
            self._stored = self._read()
        return self._stored

    def _read(self) -> tuple[int, str] | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("Casa bot id not read from %s: %s", self._path,
                           type(exc).__name__)
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("Casa bot id file %s is not JSON; ignored", self._path)
            return None
        bot_id = data.get("bot_id") if isinstance(data, dict) else None
        digest = data.get("token_digest") if isinstance(data, dict) else None
        if not is_route(bot_id) or bot_id == CASA or not isinstance(digest, str):
            logger.warning("Casa bot id file %s is malformed; ignored", self._path)
            return None
        return bot_id, digest

    def known_id(self) -> int | None:
        """The stored bot id when it was stored for the configured token;
        ``None`` when nothing is stored or it belongs to another token. Never
        a route by itself: the endpoint is built with it as its known id."""
        stored = self._load()
        if stored is None or stored[1] != self._digest:
            return None
        return stored[0]

    def observe(self, bot_id: int) -> None:
        """Record that Casa's endpoint verified *bot_id* for the configured
        token. Writes only on a change. A failed write is logged and changes
        nothing else: the file serves the next boot only."""
        current = (bot_id, self._digest)
        if self._load() == current:
            return
        try:
            atomic_write_json(
                self._path, {"bot_id": bot_id, "token_digest": self._digest},
                mode=PRIVATE)
        except OSError:
            logger.error("Casa bot id not persisted to %s", self._path,
                         exc_info=True)
            return
        self._stored = current


def _operator_id(raw: object) -> int | None:
    """The operator's user id from the configured chat: a private chat's id
    equals its user's id. A group id (negative) or anything unparsable yields
    ``None``, so nobody is the operator rather than the wrong someone."""
    if isinstance(raw, bool):
        return None
    try:
        cid = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return cid if cid > 0 else None


def _post_parts(post: Any) -> tuple[TgAddress, str] | None:
    """A job keyboard post as ``(address, kind)``, or ``None`` for an entry
    that names no address. Accepts the in-memory ``(TgAddress, kind)`` pair
    and the stored ``[bot, chat, message, kind]`` row."""
    if not isinstance(post, (list, tuple)):
        return None
    try:
        if len(post) == 2 and isinstance(post[0], TgAddress):
            return post[0], post[1]
        if len(post) == 4:
            return TgAddress.from_json(list(post[:3])), post[3]
    except AddressError:
        return None
    return None


class TelegramTransport(Channel):
    """Every Telegram endpoint, registered as the one ``"telegram"`` channel.

    The duck-typed reply methods (``send``, ``send_response``, the streaming
    trio, ``send_media``, ``turn_finished``) keep the channel signatures and
    are route-bound: each resolves its endpoint from ``context["_tg_bot"]`` and
    calls that endpoint's method of the same name, returning or raising
    exactly what the endpoint does."""

    name = "telegram"

    def __init__(self, *, casa_bot_ids: CasaBotIdStore) -> None:
        self._casa_bot_ids = casa_bot_ids
        self._endpoints: list[TelegramChannel] = []
        self._by_bot: dict[int, TelegramChannel] = {}
        self._by_role: dict[str, TelegramChannel] = {}
        self._casa: TelegramChannel | None = None
        # Readiness before any Casa endpoint is added: never set.
        self._no_casa_ready = asyncio.Event()
        # Strong references to the tasks retiring ended jobs' keyboards.
        self._job_ui_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def add_endpoint(self, ep: "TelegramChannel", *, casa: bool) -> None:
        """Register *ep*. One endpoint per agent role and one per bot: a
        second endpoint for a role, or for a bot id already served, is
        refused, as is a second Casa endpoint. Only Casa's endpoint may be
        added before its bot id is known (its route is then ``CASA``); any
        other endpoint must already know its id.

        Installs ``ep.on_identity``, through which the endpoint reports each
        id its ``getMe`` verifies."""
        if ep in self._endpoints:
            raise ValueError("this Telegram endpoint is already registered")
        if casa and self._casa is not None:
            raise ValueError("the transport already has a Casa endpoint")
        role = getattr(ep, "default_agent", "")
        if role in self._by_role:
            raise ValueError(f"a Telegram endpoint already serves role {role!r}")
        bot_id = ep.bot_id if ep.has_bot_id else None
        if bot_id is None and not casa:
            raise ValueError(
                "a Telegram endpoint other than Casa's needs a known bot id")
        if bot_id is not None and bot_id in self._by_bot:
            raise DuplicateEndpoint(bot_id)
        self._endpoints.append(ep)
        self._by_role[role] = ep
        if bot_id is not None:
            self._by_bot[bot_id] = ep
        if casa:
            self._casa = ep
        ep.on_identity = partial(self.observe_endpoint_identity, ep)

    def observe_endpoint_identity(self, ep: "TelegramChannel", bot_id: int) -> None:
        """Record that *ep*'s ``getMe`` verified *bot_id*.

        The endpoint calls this after each successful ``getMe`` and before it
        marks itself ready. It is synchronous, so from the moment it returns a
        turn born for this endpoint gets the integer route. For Casa's endpoint
        the id is also persisted for the next boot. Observing the id the
        endpoint already has is a no-op. An id that another endpoint already
        serves is refused with :class:`DuplicateEndpoint`, and nothing
        changes: that endpoint must stop and stay unready."""
        if ep not in self._endpoints:
            raise ValueError("not an endpoint of this transport")
        if not is_route(bot_id) or bot_id == CASA:
            raise AddressError(f"not a Telegram bot id: {bot_id!r}")
        holder = self._by_bot.get(bot_id)
        if holder is not None and holder is not ep:
            logger.error(
                "Telegram bot %s is configured twice; the second endpoint "
                "(role %r) stays stopped", bot_id,
                getattr(ep, "default_agent", ""))
            raise DuplicateEndpoint(bot_id)
        if holder is None:
            for stale in [b for b, e in self._by_bot.items() if e is ep]:
                del self._by_bot[stale]
            self._by_bot[bot_id] = ep
        if ep is self._casa:
            self._casa_bot_ids.observe(bot_id)

    def endpoint(self, bot: "int | TgAddress") -> "TelegramChannel":
        """Rule A: the endpoint registered under *bot* (a bot id or an
        address's bot), whether or not it is ready. A bot with no endpoint
        raises :class:`EndpointUnavailable`."""
        bot_id = bot.bot_id if isinstance(bot, TgAddress) else bot
        if not is_route(bot_id) or bot_id == CASA:
            raise AddressError(f"not a Telegram bot id: {bot_id!r}")
        ep = self._by_bot.get(bot_id)
        if ep is None:
            raise EndpointUnavailable(bot_id)
        return ep

    def endpoint_for_route(self, context: Mapping | None, *, site: str) -> "TelegramChannel":
        """Rule B: the endpoint of the route *context* carries. No
        ``_tg_bot`` key raises :class:`RouteMissing`; ``CASA`` is Casa's
        endpoint; a bot id is :meth:`endpoint`; any other value raises
        :class:`AddressError`."""
        if not isinstance(context, Mapping) or "_tg_bot" not in context:
            raise RouteMissing(site)
        route = context["_tg_bot"]
        if not is_route(route):
            raise AddressError(f"not a Telegram route ({site}): {route!r}")
        if route == CASA:
            return self.casa_endpoint()
        return self.endpoint(route)

    def casa_endpoint(self) -> "TelegramChannel":
        """Rule C: Casa's own endpoint, ready or not."""
        if self._casa is None:
            raise RuntimeError("the Telegram transport has no Casa endpoint")
        return self._casa

    def casa_route(self) -> Route:
        """Casa's bot id when it is known (verified in this process, or
        persisted for the configured token), else ``CASA``."""
        ep = self._casa
        if ep is not None and ep.has_bot_id:
            return ep.bot_id
        return CASA

    def route_for(self, role: str) -> Route:
        """The route a turn born with no parent gets: the bot of the
        endpoint bound to *role*, else Casa's route. Called once, at the
        turn's creation; delivery never re-derives a route from a role."""
        ep = self._by_role.get(role)
        if ep is None or ep is self._casa:
            return self.casa_route()
        return ep.bot_id

    def operator_user_id(self) -> int | None:
        """The operator's Telegram user id: one person per deployment, the
        same on every bot. ``None`` when no valid private chat is
        configured."""
        if self._casa is None:
            return None
        return _operator_id(getattr(self._casa, "chat_id", None))

    def casa_operator_address(self) -> TgAddress:
        """The operator's private chat on Casa's bot, for an identity formed
        before a Casa card is posted (a consent key, a card's scope). Reads
        the endpoint's ``bot_id``, so it raises ``EndpointNotReady`` before
        Casa's bot has any id, and :class:`AddressError` when no operator
        chat is configured. A plain notice needs no address: it calls
        :meth:`casa_endpoint` with the operator chat in its context."""
        bot_id = self.casa_endpoint().bot_id
        operator = self.operator_user_id()
        if operator is None:
            raise AddressError("no operator chat is configured")
        return TgAddress(bot_id, operator)

    # ------------------------------------------------------------------
    # Route-bound reply methods (rule B)
    # ------------------------------------------------------------------

    async def send(self, message: str, context: dict[str, Any]) -> DeliveryOutcome:
        ep = self.endpoint_for_route(context, site="send")
        return await ep.send(message, context)

    async def send_media(
        self, content: bytes, kind: str, filename: str, context: dict[str, Any],
        *, caption: str | None = None,
    ) -> None:
        ep = self.endpoint_for_route(context, site="send_media")
        return await ep.send_media(content, kind, filename, context,
                                   caption=caption)

    async def send_response(self, message: str,
                            context: dict[str, Any]) -> DeliveryOutcome:
        ep = self.endpoint_for_route(context, site="send_response")
        return await ep.send_response(message, context)

    async def finalize_response_stream(
        self, full_text: str, context: dict[str, Any], on_token: Any,
    ) -> DeliveryOutcome:
        ep = self.endpoint_for_route(context, site="finalize_response_stream")
        return await ep.finalize_response_stream(full_text, context, on_token)

    async def turn_finished(self, context: dict[str, Any]) -> None:
        ep = self.endpoint_for_route(context, site="turn_finished")
        return await ep.turn_finished(context)

    def create_on_token(self, context: dict[str, Any]) -> Any:
        ep = self.endpoint_for_route(context, site="create_on_token")
        return ep.create_on_token(context)

    async def finalize_stream(
        self, full_text: str, context: dict[str, Any], on_token: Any,
    ) -> DeliveryOutcome:
        ep = self.endpoint_for_route(context, site="finalize_stream")
        return await ep.finalize_stream(full_text, context, on_token)

    # ------------------------------------------------------------------
    # Job records: a new post goes through the job's route, an edit through
    # the bot that posted the message being edited
    # ------------------------------------------------------------------

    def _job_endpoint(self, rec: Any, site: str) -> "TelegramChannel":
        return self.endpoint_for_route(getattr(rec, "origin", None) or {},
                                       site=site)

    async def post_job_start_line(self, rec: Any) -> "int | None":
        ep = self._job_endpoint(rec, "post_job_start_line")
        return await ep.post_job_start_line(rec)

    async def refresh_job_start_line(self, rec: Any) -> None:
        """Edit the job's start line through the bot that posted it. A job
        with no start line filed has nothing to edit."""
        from engagement_registry import JOB_POST_START
        for post in getattr(rec, "ui_posts", None) or ():
            parts = _post_parts(post)
            if parts is not None and parts[1] == JOB_POST_START:
                await self.endpoint(parts[0]).refresh_job_start_line(rec)
                return

    async def post_job_parked(self, rec: Any, text: str) -> "int | None":
        ep = self._job_endpoint(rec, "post_job_parked")
        return await ep.post_job_parked(rec, text)

    async def post_job_question(self, rec: Any, qid: str, body: Any,
                                options: list, captions: list) -> "int | None":
        ep = self._job_endpoint(rec, "post_job_question")
        return await ep.post_job_question(rec, qid, body, options, captions)

    def _engagement_registry(self) -> Any:
        return getattr(self._casa, "_engagement_registry", None) if self._casa else None

    def retire_job_keyboards(self, rec: Any) -> None:
        """Take a terminal job's keyboard posts off the record, synchronously
        so each is retired by one owner, and edit them away on tasks of their
        own, each through the bot that posted it. Never awaits: the registry's
        terminal observer calls it.

        Posts whose bot has no endpoint keep their keyboards (a tap on one
        reads the terminal record); once every other post is scheduled, the
        first such bot is raised as :class:`EndpointUnavailable`."""
        reg = self._engagement_registry()
        if reg is None:
            return
        posts = reg.take_ui_posts(rec.id)
        if not posts:
            return
        by_endpoint: dict[int, tuple[Any, list]] = {}
        missing: int | None = None
        for post in posts:
            parts = _post_parts(post)
            if parts is None:
                logger.warning("job %s keyboard post without an address: %r",
                               str(rec.id)[:8], post)
                continue
            addr, kind = parts
            try:
                ep = self.endpoint(addr)
            except EndpointUnavailable:
                if missing is None:
                    missing = addr.bot_id
                continue
            by_endpoint.setdefault(id(ep), (ep, []))[1].append((addr, kind))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop: nothing can be edited now
            loop = None
        if loop is not None:
            for ep, ep_posts in by_endpoint.values():
                task = loop.create_task(ep._retire_job_posts(rec, ep_posts))
                self._job_ui_tasks.add(task)
                task.add_done_callback(self._job_ui_tasks.discard)
        if missing is not None:
            raise EndpointUnavailable(missing)

    def retire_ended_job_keyboards(self) -> int:
        """Boot: retire the keyboards of every terminal job that still
        carries some. Returns how many records it scheduled."""
        reg = self._engagement_registry()
        lister = getattr(reg, "records_with_ui_posts", None) if reg is not None else None
        if lister is None:
            return 0
        records = list(lister())
        for rec in records:
            try:
                self.retire_job_keyboards(rec)
            except EndpointUnavailable as exc:
                logger.warning("job %s keyboards on bot %s left as they are: "
                               "no endpoint", str(rec.id)[:8], exc.bot_id)
        return len(records)

    # ------------------------------------------------------------------
    # Lifecycle: every endpoint
    # ------------------------------------------------------------------

    @property
    def ready_event(self) -> asyncio.Event:
        """Set once Casa's endpoint is ready (its first successful bring-up)."""
        if self._casa is None:
            return self._no_casa_ready
        return self._casa.ready_event

    async def start(self) -> None:
        """Start every endpoint, Casa's first. Casa's own bring-up behaves as
        it does alone; another bot's failure to start is logged and never
        keeps Casa's bot or the rest from starting."""
        if self._casa is not None:
            await self._casa.start()
        for ep in self._endpoints:
            if ep is self._casa:
                continue
            try:
                await ep.start()
            except Exception:  # noqa: BLE001 — one bot never blocks the others
                logger.exception("Telegram endpoint for role %r failed to start",
                                 getattr(ep, "default_agent", ""))

    async def stop(self) -> None:
        """Stop every endpoint; one failing to stop never keeps the others
        running."""
        for ep in self._endpoints:
            try:
                await ep.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Telegram endpoint for role %r failed to stop",
                                 getattr(ep, "default_agent", ""))

    async def pause_updates_for_restart(self) -> bool:
        """Stop taking updates on every endpoint before an app restart.
        Returns whether any endpoint's intake was paused."""
        paused = False
        for ep in self._endpoints:
            if await ep.pause_updates_for_restart():
                paused = True
        return paused

    async def resume_updates(self) -> None:
        """Undo :meth:`pause_updates_for_restart` on every endpoint."""
        for ep in self._endpoints:
            await ep.resume_updates()


def telegram_transport(channel_manager: Any = None) -> "TelegramTransport | None":
    """The registered Telegram channel, or ``None`` when Telegram is not
    configured. The one place Casa looks it up by name; *channel_manager*
    defaults to the one ``tools`` was initialised with."""
    if channel_manager is None:
        import tools as tools_mod
        channel_manager = getattr(tools_mod, "_channel_manager", None)
    if channel_manager is None:
        return None
    return channel_manager.get("telegram")
