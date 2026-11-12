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
self-setup, a move to a new bot, recovery and dormancy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    from channels.telegram import TelegramChannel

logger = logging.getLogger(__name__)

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

    Only an open chat admits a surface, and one whose agent's greeting is on
    its way: the chat opens with that greeting, and the agent was told the
    operator opened it. The resident's chat is Casa's own and always open;
    every other agent without a chat of its own has no surface at all."""
    lc = _lifecycle()
    state = lc.state(role)
    if state == "open" or (state == "not_open" and lc.greeting_on_its_way(role)):
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


def greeting_started(bot_id: int) -> None:
    """The agent of own bot *bot_id* is about to say hello by itself."""
    _lifecycle().greeting_started(bot_id)


def greeting_ended(bot_id: int) -> None:
    """That greeting reached the operator, or will not. Creates no
    lifecycle: without one, nothing was on its way."""
    import own_voice_lifecycle
    lc = own_voice_lifecycle._CURRENT
    if lc is not None:
        lc.greeting_ended(bot_id)


async def mark_open(bot_id: int) -> None:
    """The turn that opened *bot_id*'s chat was taken by the agent's queue (the
    operator's) or reached Telegram (the agent's greeting): it is open."""
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


# ---------------------------------------------------------------------------
# What is still waiting on a bot
# ---------------------------------------------------------------------------

#: what :func:`open_on_bot` says when a store could not be read: the change waits
OPEN_UNCHECKED = "something Casa could not check"


def _delegations_on(role: str, bot_id: int) -> bool:
    """A delegation asked on the bot or delivering there that is still under way:
    accepted or running, waiting for input it can still take, or still owing its
    requester an announcement."""
    import tools
    from job_registry import ExecutionState, JobRegistry
    registry = tools._specialist_registry
    if registry is None:
        return False
    jobs = registry.job_registry
    now = jobs._now()
    for job in jobs.all():
        if not (job.route.get("_tg_bot") == bot_id
                or (job.delivery is not None and job.delivery.bot_id == bot_id)):
            continue
        if (job.execution_state in (ExecutionState.ACCEPTED, ExecutionState.RUNNING)
                or (job.awaiting_input and job.continuable_until is not None
                    and job.continuable_until > now)
                or JobRegistry._owes_announcement_still(job)):
            return True
    return False


def _jobs_on(role: str, bot_id: int) -> bool:
    """A live or idle job started on the bot or posting there."""
    import tools
    registry = tools._engagement_registry
    if registry is None:
        return False
    return any(rec.origin.get("_tg_bot") == bot_id
               or (rec.delivery is not None and rec.delivery.bot_id == bot_id)
               for rec in registry.active_and_idle())


def _job_cards_on(role: str, bot_id: int) -> bool:
    """An ended job whose keyboard post on the bot is not yet retired."""
    import tools
    registry = tools._engagement_registry
    if registry is None:
        return False
    return any(addr.bot_id == bot_id
               for rec in registry.records_with_ui_posts()
               for addr, _kind in rec.ui_posts)


def _scheduled_asks_on(role: str, bot_id: int) -> bool:
    """A scheduled question stored as posted through the bot."""
    import scheduled_asks
    store = scheduled_asks.STORE
    if store is None:
        return False
    return any(rec.get("bot_id") == bot_id for rec in store.all())


def _run_cards_on(role: str, bot_id: int) -> bool:
    """A browsing request's card on the bot whose Stop still works."""
    import run_admission
    return run_admission.CONFIRMATIONS.open_cards_on_bot(bot_id) > 0


def _held_on(role: str, bot_id: int) -> bool:
    """An update the bot received and holds, not yet delivered."""
    return bool(_lifecycle().journal.pending(bot_id))


def _questions_on(role: str, bot_id: int) -> bool:
    """A question or card on the bot still waiting for its answer (in memory)."""
    import verdict_broker
    return verdict_broker.BROKER.live_on_bot(bot_id) > 0


def _continuations_on(role: str, bot_id: int) -> bool:
    """A continuation queued on the bot and not yet started (in memory)."""
    import plugin_continuations
    return plugin_continuations.CONTINUATIONS.pending_on_bot(bot_id) > 0


def _answering(role: str, bot_id: int) -> bool:
    """A turn of the agent being answered now (in memory). Turns are not tracked
    per bot, so any of the agent's turns counts."""
    import tools
    bus = tools._bus
    if bus is None:
        return False
    return any(not task.done() for task in bus.dispatch_tasks_for(role))


#: (phrase, check, durable) in the order :func:`open_on_bot` names them
_OPEN_KINDS: "tuple[tuple[str, Callable[[str, int], bool], bool], ...]" = (
    ("a delegated task", _delegations_on, True),
    ("a background job", _jobs_on, True),
    ("a job's card", _job_cards_on, True),
    ("a scheduled question", _scheduled_asks_on, True),
    ("a run card with Stop", _run_cards_on, True),
    ("a message held for it", _held_on, True),
    ("a question or card awaiting an answer", _questions_on, False),
    ("a continuation", _continuations_on, False),
    ("a message it is answering now", _answering, False),
)


def open_on_bot(role: str, bot_id: int, *, durable_only: bool = False) -> list[str]:
    """What Casa still holds that waits on *role*'s bot *bot_id*: one short
    phrase per kind found, in a fixed order; empty when nothing does.

    An agent moves to a new bot only when this is empty. *durable_only* reads
    only what survives a restart (at boot, nothing else exists yet). A store
    that is not there holds nothing; one that fails to read is logged and
    reported as :data:`OPEN_UNCHECKED`, so the change waits. Never raises.
    Reminders and triggers are not counted: they pick their bot when they fire."""
    found: list[str] = []
    unchecked = False
    for phrase, check, durable in _OPEN_KINDS:
        if durable_only and not durable:
            continue
        try:
            if check(role, bot_id):
                found.append(phrase)
        except Exception:  # noqa: BLE001 — an unreadable store holds the change
            logger.warning("own chat of %s: could not check for %s on bot %s",
                           role, phrase, bot_id, exc_info=True)
            unchecked = True
    if unchecked:
        found.append(OPEN_UNCHECKED)
    return found


#: what :func:`offer_own_chat` can be asked to do
OFFER_MODES = ("check", "offer", "replace", "drop", "rename")


async def offer_own_chat(role: str, *, mode: str = "offer", username: "str | None" = None,
                         candidates: "list[str] | None" = None,
                         name: "str | None" = None) -> dict:
    """The resident's tool, for *role*'s own chat. *mode*:

    ``"check"`` reports on the *candidates* handles and what the resident
    needs to choose one; ``"offer"`` posts the card that creates the chat
    with the handle *username* (or, once created, the card that opens it);
    ``"replace"`` offers a new bot with the handle *username* in place of the
    current one; ``"drop"`` keeps the current bot after all. For the handle
    of a bot *role* was moved away from, ``"offer"`` and ``"replace"`` move
    it back to that bot. ``"rename"`` gives *role*'s bot the display name
    *name*."""
    lc = _lifecycle()
    if mode == "check":
        return await lc.check_handles(role, list(candidates or []))
    if mode == "offer":
        return await lc.offer(role, username or "")
    if mode == "replace":
        return await lc.replace(role, username or "")
    if mode == "drop":
        return await lc.drop_change(role)
    if mode == "rename":
        return await lc.rename(role, name or "")
    return {"status": "error", "kind": "bad_mode",
            "message": f"mode must be one of {', '.join(OFFER_MODES)}"}


async def apply_switches_at_boot() -> None:
    """Early at boot, before any bot is started: move each agent whose new
    bot exists to it, unless something durable still waits on the old one.
    Needs only Casa's records and the token store."""
    await _lifecycle().apply_switches()


def chat_moved(bot_id: int) -> bool:
    """Whether a turn on own bot *bot_id* opens an agent's new chat after a
    handle change, rather than its first one."""
    return _lifecycle().chat_moved(bot_id)


def own_chat_disclosure(role: str) -> "dict | None":
    """For an uninstall's result: *role*'s bot and the BotFather link to
    delete it, or ``None`` when it has no bot."""
    return _lifecycle().disclosure(role)
