"""Telegram message addresses, turn routes, and the errors of resolving them.

Telegram numbers ``update_id`` and ``message_id`` per bot, and a private
chat's id equals the user's id. So once Casa runs more than one bot, a chat id
and a message id no longer name a message: two bots' private chats with the
operator share the chat id, and both count message ids up from 1. Anything
that outlives a turn therefore names the bot too.

Two values carry the bot, and they are kept apart on purpose:

* A **route** (the reserved context key ``_tg_bot``) is what a turn or a post
  is *going to* deliver through. It is either a verified bot id or
  :data:`CASA`, which means "Casa's own endpoint, whatever id it verifies".
  ``CASA`` exists because a turn can be born before Casa's bot has completed
  its first ``getMe``; it resolves to that endpoint at delivery time.
* An **address** (:class:`TgAddress`) is a message that was actually sent or
  received. It always carries the integer id of the bot that sent or received
  it, so :data:`CASA` can never reach one, nor any identity key built by
  :func:`scope`.

This module is leaf-level: it imports nothing from Casa.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

#: The reserved route value naming Casa's own endpoint, whatever id it verifies.
CASA: Final = "casa"

#: What ``_tg_bot`` holds: a verified bot id or :data:`CASA`. Never ``None``.
Route = int | Literal["casa"]

#: The broker scope kinds a Telegram chat lane is keyed by.
_SCOPE_KINDS: Final = frozenset({"dm", "authz", "ask", "proposal"})


class AddressError(ValueError):
    """A bot, chat or route value that cannot name a Telegram endpoint or
    message: a non-int bot or chat in an address or key, or a present
    ``_tg_bot`` that is neither a bot id nor :data:`CASA`. A malformed route
    is a programming error, never a reason to pick another endpoint."""


class EndpointUnavailable(RuntimeError):
    """A bot id with no endpoint registered under it (a stored address or an
    int route whose bot is not, or no longer, configured). Never answered by
    another endpoint."""

    def __init__(self, bot_id: int, message: str | None = None) -> None:
        self.bot_id = bot_id
        super().__init__(message or f"no Telegram endpoint for bot {bot_id}")


class EndpointNotReady(RuntimeError):
    """An endpoint's ``bot_id`` read before the endpoint has any id: its
    token has never completed ``getMe`` and no id was persisted for it.
    Only a writer that must form an identity before it posts can meet it."""


class DuplicateEndpoint(RuntimeError):
    """A bot id that already belongs to another endpoint. One bot configured
    twice is a configuration error; the second endpoint stays unready."""

    def __init__(self, bot_id: int, message: str | None = None) -> None:
        self.bot_id = bot_id
        super().__init__(
            message or f"bot {bot_id} is already served by another endpoint")


class RouteMissing(RuntimeError):
    """Route-bound delivery from a context with no ``_tg_bot`` key at all.

    Delivery never guesses a bot from an empty context, a ``cid``-only
    context, a bare ``chat_id`` or the agent's role: a builder that dropped
    the route fails loudly here instead of being misrouted."""

    def __init__(self, site: str) -> None:
        self.site = site
        super().__init__(f"no Telegram route in the delivery context ({site})")


def _is_id(v: object) -> bool:
    """A real ``int`` (a ``bool`` is never an id)."""
    return isinstance(v, int) and not isinstance(v, bool)


def _require_bot(v: object) -> int:
    if not _is_id(v) or v <= 0:
        raise AddressError(f"not a Telegram bot id: {v!r}")
    return v


def is_route(v: object) -> bool:
    """True for a valid route: a positive int (not a bool) or :data:`CASA`."""
    if isinstance(v, str):
        return v == CASA
    return _is_id(v) and v > 0


@dataclass(frozen=True)
class TgAddress:
    """A Telegram message (or, with no ``message_id``, a chat) as seen by one
    bot: the integer id of the endpoint that sent or received it, the chat,
    and the message."""

    bot_id: int
    chat_id: int
    message_id: int | None = None

    def __post_init__(self) -> None:
        _require_bot(self.bot_id)
        if not _is_id(self.chat_id):
            raise AddressError(f"not a Telegram chat id: {self.chat_id!r}")
        if self.message_id is not None and not _is_id(self.message_id):
            raise AddressError(
                f"not a Telegram message id: {self.message_id!r}")

    def to_json(self) -> list:
        """``[bot_id, chat_id, message_id]``."""
        return [self.bot_id, self.chat_id, self.message_id]

    @classmethod
    def from_json(cls, v: object) -> "TgAddress":
        """The inverse of :meth:`to_json`. Strict: there is no default bot, so
        a stored record written without one fails here, loudly, rather than
        being assigned to some bot."""
        if not isinstance(v, (list, tuple)) or len(v) != 3:
            raise AddressError(f"not a stored Telegram address: {v!r}")
        return cls(v[0], v[1], v[2])


def scope(kind: str, bot_id: int, chat_id: int) -> str:
    """The broker scope of one chat lane on one bot: ``"<kind>:<bot>:<chat>"``.

    Two bots' private chats with the operator share the chat id, so a lane
    keyed by the chat alone would let one bot's question, challenge or
    proposal block, or ``/new`` cancel, the other's."""
    if kind not in _SCOPE_KINDS:
        raise ValueError(f"unknown scope kind: {kind!r}")
    return f"{kind}:{_require_bot(bot_id)}:{chat_id}"
