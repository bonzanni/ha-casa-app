"""Two Telegram endpoints behind one transport: the harness for the bot
boundary.

Production runs one bot, so this is the place the boundary between bots is
exercised. ``two_endpoints`` builds a transport with bot 111 (Casa's own
endpoint, role ``assistant``) and bot 222 (role ``finance``), both serving
operator 100. A test can feed the same ``update_id``, chat, ``message_id`` and
``media_group_id`` to both, and count what each bot sent with ``calls``.

The endpoints are :class:`FakeEndpoint` by default: duck-typed stand-ins that
record every method the transport calls and behave, when not ready, the way an
unready ``TelegramChannel`` does (``send`` reports not delivered, ``send_media``
raises, ``create_on_token`` still returns a callable). ``build_two_endpoints``
accepts real ``TelegramChannel`` instances in their place.

Usage::

    from two_endpoints import two_endpoints, feed_update, calls  # noqa: F401
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from channels import DeliveryOutcome
from channels.tg_address import DuplicateEndpoint, EndpointNotReady

CASA_BOT = 111
FINANCE_BOT = 222
OPERATOR = 100
CASA_TOKEN = "111:casa-token"


class FakeEndpoint:
    """A Telegram endpoint stand-in. ``bot_id`` is the id ``getMe`` verified
    (see :meth:`initialize`), else the known id it was built with, else it
    raises ``EndpointNotReady``, as the real endpoint does."""

    def __init__(self, role: str, *, bot_id: "int | None", is_casa: bool = False,
                 ready: bool = True, operator: int = OPERATOR) -> None:
        self.default_agent = role
        self.is_casa = is_casa
        self.chat_id = operator
        self.on_identity = None
        self.calls: list[tuple[str, tuple]] = []
        self.ready_event = asyncio.Event()
        self.stopped = False
        self.identity_seen_ready: list[bool] = []
        self._known = bot_id
        self._verified: "int | None" = bot_id if ready else None
        self._ready = ready and bot_id is not None
        if self._ready:
            self.ready_event.set()
        self._seen_update_ids: set[int] = set()
        self._next_message_id = 1000
        # The exact error an unready ``send_media`` raises, so a test can
        # assert the transport passed this very object through.
        self.media_unavailable = RuntimeError("Telegram channel not started")

    # -- identity --------------------------------------------------------
    @property
    def bot_id(self) -> int:
        if self._verified is not None:
            return self._verified
        if self._known is not None:
            return self._known
        raise EndpointNotReady(f"endpoint for {self.default_agent!r} has no bot id")

    @property
    def has_bot_id(self) -> bool:
        return self._verified is not None or self._known is not None

    @property
    def is_ready(self) -> bool:
        return self._ready

    def initialize(self, bot_id: int) -> bool:
        """A successful ``getMe`` returning *bot_id*: the id is recorded and
        reported through ``on_identity`` BEFORE the endpoint becomes ready,
        as the real bring-up does. A ``DuplicateEndpoint`` stops it, unready.
        Returns whether the endpoint became ready."""
        self._verified = bot_id
        if self.on_identity is not None:
            self.identity_seen_ready.append(self._ready)
            try:
                self.on_identity(bot_id)
            except DuplicateEndpoint:
                self._verified = None
                self.stopped = True
                return False
        self._ready = True
        self.ready_event.set()
        return True

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def _mid(self) -> int:
        self._next_message_id += 1
        return self._next_message_id

    # -- inbound ---------------------------------------------------------
    async def process_webhook_update(self, payload: dict) -> str:
        if not self._ready:
            return "ignored"
        uid = payload.get("update_id")
        if uid in self._seen_update_ids:
            return "duplicate"
        self._seen_update_ids.add(uid)
        self._record("update", payload)
        return "accepted"

    # -- route-bound reply methods ---------------------------------------
    async def send(self, message: str, context: dict) -> DeliveryOutcome:
        self._record("send", message, context)
        return DeliveryOutcome.DELIVERED if self._ready else DeliveryOutcome.NOT_DELIVERED

    async def send_media(self, content: bytes, kind: str, filename: str,
                         context: dict, *, caption: "str | None" = None,
                         post=None) -> None:
        self._record("send_media", kind, filename, context, caption)
        if not self._ready:
            raise self.media_unavailable

    async def send_response(self, message: str, context: dict) -> DeliveryOutcome:
        self._record("send_response", message, context)
        return DeliveryOutcome.DELIVERED if self._ready else DeliveryOutcome.NOT_DELIVERED

    async def finalize_response_stream(self, full_text: str, context: dict,
                                       on_token: Any) -> DeliveryOutcome:
        self._record("finalize_response_stream", full_text, context, on_token)
        return DeliveryOutcome.DELIVERED if self._ready else DeliveryOutcome.NOT_DELIVERED

    async def turn_finished(self, context: dict) -> None:
        self._record("turn_finished", context)

    def create_on_token(self, context: dict) -> Any:
        self._record("create_on_token", context)

        async def on_token(text: str) -> None:
            self._record("on_token", text)

        self.last_on_token = on_token
        return on_token

    async def finalize_stream(self, full_text: str, context: dict,
                              on_token: Any) -> DeliveryOutcome:
        self._record("finalize_stream", full_text, context, on_token)
        return DeliveryOutcome.DELIVERED if self._ready else DeliveryOutcome.NOT_DELIVERED

    # -- job records -----------------------------------------------------
    async def post_job_start_line(self, rec: Any) -> "int | None":
        self._record("post_job_start_line", rec)
        return self._mid() if self._ready else None

    async def post_job_parked(self, rec: Any, text: str) -> "int | None":
        self._record("post_job_parked", rec, text)
        return self._mid() if self._ready else None

    async def post_job_question(self, rec: Any, qid: str, body: Any,
                                options: list, captions: list) -> "int | None":
        self._record("post_job_question", rec, qid, body, options, captions)
        return self._mid() if self._ready else None

    async def _retire_job_posts(self, rec: Any, posts: list) -> None:
        self._record("retire_job_posts", rec, list(posts))

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        self._record("start")

    async def stop(self) -> None:
        self._record("stop")

    async def pause_updates_for_restart(self) -> bool:
        self._record("pause_updates_for_restart")
        return True

    async def resume_updates(self) -> None:
        self._record("resume_updates")


@dataclass
class TwoEndpoints:
    transport: Any
    manager: Any
    casa: Any
    finance: Any
    store: Any = None
    ep: dict = field(default_factory=dict)


def build_two_endpoints(casa: Any = None, finance: Any = None, *,
                        store: Any = None) -> TwoEndpoints:
    """The transport over Casa's endpoint (111, ``assistant``) and a second
    one (222, ``finance``), registered in a ``ChannelManager`` the way
    ``casa_core`` registers it. Either endpoint may be passed in (a real
    ``TelegramChannel`` included); *store* defaults to an in-memory one."""
    from telegram_channel_double import MemoryBotIdStore, make_transport

    casa = casa if casa is not None else FakeEndpoint(
        "assistant", bot_id=CASA_BOT, is_casa=True)
    finance = finance if finance is not None else FakeEndpoint(
        "finance", bot_id=FINANCE_BOT)
    store = store if store is not None else MemoryBotIdStore(CASA_BOT)
    manager = make_transport(casa, finance, casa=casa, casa_bot_ids=store)
    transport = manager.get("telegram")
    return TwoEndpoints(transport=transport, manager=manager, casa=casa,
                        finance=finance, store=store,
                        ep={CASA_BOT: casa, FINANCE_BOT: finance})


@pytest.fixture
def two_endpoints(tmp_path: Path) -> TwoEndpoints:
    """Bots 111 (Casa, ``assistant``) and 222 (``finance``), both ready, both
    serving operator 100, over a real ``CasaBotIdStore`` in *tmp_path*."""
    from channels.telegram_transport import CasaBotIdStore

    store = CasaBotIdStore(tmp_path / "telegram-casa-bot.json", CASA_TOKEN)
    return build_two_endpoints(store=store)


def update_payload(update_id: int, message_id: int, text: str, *,
                   bot_id: int = 0, media_group_id: "str | None" = None,
                   reply_to: "int | None" = None,
                   operator: int = OPERATOR) -> dict:
    """A Bot API ``Update`` (as JSON) carrying the operator's private
    message; ``reply_to`` quotes a message the bot *bot_id* sent."""
    chat = {"id": operator, "type": "private", "first_name": "Operator"}
    sender = {"id": operator, "is_bot": False, "first_name": "Operator"}
    message: dict[str, Any] = {
        "message_id": message_id, "date": 0, "chat": chat, "from": sender,
    }
    if media_group_id is not None:
        message["media_group_id"] = media_group_id
        message["caption"] = text
    else:
        message["text"] = text
    if reply_to is not None:
        message["reply_to_message"] = {
            "message_id": reply_to, "date": 0, "chat": chat,
            "from": {"id": bot_id, "is_bot": True, "first_name": "Bot"},
            "text": "",
        }
    return {"update_id": update_id, "message": message}


async def feed_update(ep: Any, update_id: int, message_id: int, text: str,
                      media_group_id: "str | None" = None,
                      reply_to: "int | None" = None) -> str:
    """Deliver one operator update to *ep* through its webhook entry, as
    Telegram would on that bot's route. Returns the entry's outcome
    (``"accepted"``, ``"duplicate"`` or ``"ignored"``)."""
    bot_id = ep.bot_id if ep.has_bot_id else 0
    return await ep.process_webhook_update(update_payload(
        update_id, message_id, text, bot_id=bot_id,
        media_group_id=media_group_id, reply_to=reply_to))


def calls(ep: Any) -> list[tuple[str, Any]]:
    """What went out through *ep*, in order, as ``(name, args)``: the methods
    a :class:`FakeEndpoint` was called with (inbound ``update`` entries
    excluded), or, for a real ``TelegramChannel`` over a fake bot, the Bot API
    calls its bot received."""
    if isinstance(ep, FakeEndpoint):
        return [c for c in ep.calls if c[0] != "update"]
    bot = ep._app.bot if getattr(ep, "_app", None) is not None else getattr(ep, "_bot", None)
    if bot is None:
        return []
    return [(c[0], c[2]) for c in bot.mock_calls if c[0] and "." not in c[0]]
