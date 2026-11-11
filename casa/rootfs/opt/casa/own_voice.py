"""Agents that speak for themselves, from their own Telegram bot.

A specialist may declare its own chat in its manifest. Once the operator has
created its bot and Casa has bound and opened it, the agent's chat holds its
conversation with the operator and every surface it shows him: capability
deliveries, stored-call buttons, files, run cards, Stop, job lines. Every
other agent stays behind the resident and has no surface of its own.

This module answers two questions for the rest of Casa: what state an agent's
own chat is in (:func:`own_chat_state`), and whether one of its surfaces may
post now (:func:`require_open_chat`, the capability rule's one function). The
entry points below drive the chat's lifecycle: offer, creation, binding,
self-setup, recovery and dormancy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from channels.telegram import TelegramChannel

OwnChatState = Literal["not_declared", "no_bot", "not_open", "open", "dormant"]


@dataclass(frozen=True)
class Refusal:
    """A surface that may not post: *kind* is one of ``own_chat_missing``,
    ``own_chat_not_open`` or ``own_chat_unreachable``; *text* is the line the
    caller is told."""
    kind: str
    text: str


def declared(role: str) -> bool:
    """Whether the committed active component of *role* declares its own chat."""
    raise NotImplementedError


def own_chat_state(role: str) -> OwnChatState:
    """The state of *role*'s own chat, now."""
    raise NotImplementedError


def endpoint_for(role: str) -> "TelegramChannel | None":
    """The endpoint bound to *role*'s own bot, in any phase, else ``None``."""
    raise NotImplementedError


def require_open_chat(role: str, *, label: str) -> "Refusal | None":
    """``None`` when a surface of *role* may post now, else the refusal."""
    raise NotImplementedError


async def on_managed_bot_created(bot: dict, from_user_id: int) -> None:
    raise NotImplementedError


async def on_managed_bot_updated(bot: dict, owner_id: int) -> None:
    raise NotImplementedError


async def intake(bot_id: int, update_json: dict) -> Literal["deliver", "held", "dormant_reply"]:
    """What to do with an update that reached an own bot's endpoint."""
    raise NotImplementedError


async def on_install_committed(role: str) -> None:
    raise NotImplementedError


async def on_upgrade_committed(role: str) -> None:
    raise NotImplementedError


async def on_rollback_committed(role: str) -> None:
    raise NotImplementedError


async def on_uninstall_committed(role: str) -> None:
    raise NotImplementedError


async def on_persona_applied(role: str) -> None:
    """The bot's self-setup runs again for *role*'s new persona."""
    raise NotImplementedError


async def on_queue_registered(role: str) -> None:
    """*role*'s conversation is on the bus: updates held for it are released."""
    raise NotImplementedError


async def boot_reconcile() -> None:
    raise NotImplementedError


async def offer_own_chat(role: str) -> dict:
    """The resident's tool: offer *role* its own chat."""
    raise NotImplementedError
