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


def _lifecycle():
    import own_voice_lifecycle
    return own_voice_lifecycle.current()


def _is_resident(role: str) -> bool:
    """The resident whose chat is Casa's own: its chat always exists."""
    return role == _lifecycle().host.resident_role()


def declared(role: str) -> bool:
    """Whether the committed active component of *role* declares its own chat."""
    return _lifecycle().declared(role)


def own_chat_state(role: str) -> OwnChatState:
    """The state of *role*'s own chat, now."""
    return _lifecycle().state(role)


def endpoint_for(role: str) -> "TelegramChannel | None":
    """The endpoint bound to *role*'s own bot, in any phase, else ``None``."""
    lc = _lifecycle()
    bot_id = lc.bot_id_for(role)
    return lc.host.endpoint(bot_id) if bot_id is not None else None


def require_open_chat(role: str, *, label: str) -> "Refusal | None":
    """``None`` when a surface of *role* may post now, else the refusal.

    Only an open chat admits a surface. The resident's chat is Casa's own
    and always open; every other agent without a chat of its own has no
    surface at all."""
    lc = _lifecycle()
    state = lc.state(role)
    if state == "open":
        return None
    resident = lc._resident()
    if state == "not_declared":
        if _is_resident(role):
            return None
        return Refusal("own_chat_missing",
                       f"{label} has no chat of its own: return what you have as text.")
    if state == "dormant":
        return Refusal("own_chat_missing", f"{label}'s chat is closed right now.")
    if state == "not_open":
        return Refusal("own_chat_not_open",
                       f"{label}'s chat isn't open yet — tap Open {label}'s chat "
                       f"in {resident}'s chat.")
    if lc.is_recovering(role):
        return Refusal("own_chat_unreachable", f"{label}'s chat isn't reachable right now.")
    return Refusal("own_chat_missing",
                   f"{label}'s chat isn't set up yet — ask {resident} to create it.")


async def on_managed_bot_created(bot: dict, from_user_id: int) -> None:
    """A ``managed_bot_created`` service message reached Casa's bot: the only
    way a new bot is registered."""
    await _lifecycle().on_created(bot, from_user_id)


async def on_managed_bot_updated(bot: dict, owner_id: int) -> None:
    """A ``managed_bot`` update: a known bot's token is fetched again."""
    await _lifecycle().on_updated(bot, owner_id)


async def intake(bot_id: int, update_json: dict) -> Literal["deliver", "held", "dormant_reply"]:
    """What to do with an update that reached an own bot's endpoint:
    ``"deliver"`` through the endpoint's normal intake; ``"held"`` — it is
    kept for later (or ignored) and needs nothing more; ``"dormant_reply"`` —
    the bot has already answered that its agent has no chat now."""
    return await _lifecycle().intake(bot_id, update_json)


def chat_opening(bot_id: int) -> bool:
    """Whether a turn delivered on own bot *bot_id* now is the one that opens
    its agent's chat (the chat has not opened yet)."""
    return _lifecycle().chat_opening(bot_id)


async def mark_open(bot_id: int) -> None:
    """The agent's queue took the turn that opened *bot_id*'s chat: it is open."""
    await _lifecycle().mark_open(bot_id)


async def on_install_committed(role: str) -> None:
    await _lifecycle().on_install(role)


async def on_upgrade_committed(role: str) -> None:
    await _lifecycle().on_upgrade(role)


async def on_rollback_committed(role: str) -> None:
    await _lifecycle().on_upgrade(role)


async def on_uninstall_committed(role: str) -> None:
    await _lifecycle().reconcile_role(role)


async def on_persona_applied(role: str) -> None:
    """The bot's self-setup runs again for *role*'s new persona."""
    await _lifecycle().on_persona(role)


async def on_queue_registered(role: str) -> None:
    """*role*'s conversation is on the bus: updates held for it are released."""
    await _lifecycle().on_queue(role)


async def boot_reconcile() -> None:
    await _lifecycle().boot()


async def offer_own_chat(role: str) -> dict:
    """The resident's tool: offer *role* its own chat."""
    return await _lifecycle().offer(role)


def own_chat_disclosure(role: str) -> "dict | None":
    """For an uninstall's result: *role*'s bot and the BotFather link to
    delete it, or ``None`` when it has no bot."""
    return _lifecycle().disclosure(role)
