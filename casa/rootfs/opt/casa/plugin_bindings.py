"""Browser executor §5.6 (Plan 3 C1): who a plugin's work serves, kept for as
long as the work takes.

A plugin whose runs outlive one turn (the browsing specialist's runs wait on the
operator, then carry on in fresh turns) must know, in every later turn, whom a run
serves and where its result goes. Casa mints a **binding** the first time a
``callsCasa`` call asks for its requester (``POST /internal/broker/requester``):
an opaque reference standing for the operator, the chat, the chain of requesters
and the origin markers of the turn that asked. The plugin stores only the
reference with its run; a continuation turn Casa starts for that run
(``plugin_continuations``) is built from the binding, never from anything the
plugin or a model says.

Bindings are durable (``/data/plugin-bindings.json``, ``0600``) because a run can
outlive a Casa restart and the driver sweep resumes it afterwards. A binding is
kept for ``BINDING_TTL_S`` after it was last used and the store holds at most
``MAX_BINDINGS``; the oldest go first. One binding per (plugin, turn): asking twice
in one turn returns the same reference.

A binding also keeps its request's conversation: the session its continuation turns
resume (``plugin_continuations``), how many of them raised in a row, and the session's
cumulative cost when last counted. It therefore owns that session's pad (``scratchpad``): the pad
follows a session the binding adopts, stays while the binding is kept, and goes when the binding
is dropped, unless another binding still names the session.

A binding keeps the Telegram route of the turn that asked (``origin._tg_bot``),
which may be ``CASA`` when Casa's bot had not verified its id yet: the route its
continuations run on, so their results reach whoever asked. It also keeps the
**delivery** of the call that asked (``GrantIdentity.delivery``): where every
post of the work goes — its cards, its run card, its Stop line, its
continuations' deliveries — or ``None`` when the work posts nothing. A task's
delivery is decided once, when the task is born, and a binding carries it
unchanged for the life of the work. Every per-bot fact a binding's work keys
(a run admission, a queued continuation, a Stop) names the delivery's bot, or
the route's bot for work that posts nothing (:func:`binding_target`). A stored
binding without a route, or without a valid delivery, is refused at load."""
from __future__ import annotations

import dataclasses
import logging
import os
import secrets
import threading
import time
from typing import Any

import scratchpad
from atomic_io import PRIVATE, atomic_write_json
from channels.tg_address import (
    AddressError, EndpointNotReady, EndpointUnavailable, TgAddress, is_route)
from provenance import ROUTING_KEYS

logger = logging.getLogger(__name__)

BINDINGS_PATH = "/data/plugin-bindings.json"
BINDING_TTL_S = 30 * 24 * 3600.0
MAX_BINDINGS = 500
REF_PREFIX = "casa-bind-"

# The origin markers a continuation turn is rebuilt from — plain, persistable
# values only. Never the operator's words: a continuation has none
# (``handoff.operator_words``), so a run it starts goes to his tap.
_ORIGIN_KEYS = ("role", "channel", "chat_id", "user_id", "user_name",
                "_origin_route", "_origin_clearance", *ROUTING_KEYS)


@dataclasses.dataclass
class Binding:
    ref: str
    plugin: str                 # the plugin segment (runtime name) it was minted for
    operator_id: int
    chat_id: int
    requester: str
    requester_kind: str         # "operator_resident" | "specialist"
    chain: list
    origin: dict
    turn_key: str
    created_at: float
    last_used: float
    # where the work's posts go, as ``TgAddress.to_json()``, or None: nothing
    delivery: "list | None"
    session_id: "str | None" = None
    resume_failures: int = 0
    cost_seen: float = 0.0


class ChannelUnavailable(RuntimeError):
    """The binding's bot cannot be named now: Telegram is not configured, the
    route's bot has no endpoint, or Casa's endpoint has not verified its id
    yet (a first boot, or a replaced token)."""


def route_target(context: dict) -> "tuple[Any, int]":
    """The endpoint the route in *context* resolves to, and that endpoint's
    bot id, read now. Raises :class:`ChannelUnavailable` when there is none
    to read; the caller then answers as when its post cannot be made."""
    from channels.telegram_transport import telegram_transport
    transport = telegram_transport()
    if transport is None:
        raise ChannelUnavailable("Telegram is not configured")
    try:
        ep = transport.endpoint_for_route(context, site="plugin binding")
        return ep, ep.bot_id
    except (EndpointUnavailable, EndpointNotReady) as exc:
        raise ChannelUnavailable(str(exc)) from exc


def delivery_of(binding: Binding) -> "TgAddress | None":
    """Where *binding*'s work posts, or ``None`` when it posts nothing."""
    raw = binding.delivery
    return None if raw is None else TgAddress.from_json(raw)


def delivery_target(delivery: TgAddress) -> "tuple[Any, int]":
    """The endpoint of *delivery*'s bot and that bot's id. Raises
    :class:`ChannelUnavailable` when there is none: no other bot is tried."""
    from channels.telegram_transport import telegram_transport
    transport = telegram_transport()
    if transport is None:
        raise ChannelUnavailable("Telegram is not configured")
    try:
        ep = transport.endpoint(delivery.bot_id)
        return ep, ep.bot_id
    except (EndpointUnavailable, EndpointNotReady, AddressError) as exc:
        raise ChannelUnavailable(str(exc)) from exc


def binding_target(binding: Binding) -> "tuple[Any, int]":
    """The endpoint *binding*'s work posts through and keys by: its
    delivery's bot (:func:`delivery_target`), or, for work that posts
    nothing, its route's (:func:`route_target`)."""
    delivery = delivery_of(binding)
    if delivery is not None:
        return delivery_target(delivery)
    return route_target(binding.origin)


def route_of(origin: dict, identity: Any) -> dict:
    """The route a binding minted from *origin* keeps: the turn's own, or, on
    a turn that carries none (an engagement's), the bot the call's grant
    identity was resolved on."""
    if "_tg_bot" in origin:
        return {"_tg_bot": origin["_tg_bot"]}
    return {"_tg_bot": int(identity.bot_id)}


def _route_ok(entry: Any) -> bool:
    origin = entry.get("origin") if isinstance(entry, dict) else None
    if not (isinstance(origin, dict) and is_route(origin.get("_tg_bot"))):
        return False
    if "delivery" not in entry:
        return False
    if entry["delivery"] is None:
        return True
    try:
        TgAddress.from_json(entry["delivery"])
    except AddressError:
        return False
    return True


def requester_kind(chain: tuple | list) -> str:
    """``operator_resident`` when the run serves the chain's root resident
    directly (the operator's own request to the resident, or his desk reply),
    else ``specialist``."""
    return "operator_resident" if len(chain) <= 2 else "specialist"


class BindingStore:
    def __init__(self, path: str = BINDINGS_PATH, *, now=time.time) -> None:
        self._path = path
        self._now = now
        self._lock = threading.Lock()
        self._loaded = False
        self._items: dict[str, Binding] = {}

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            import json
            with open(self._path, encoding="utf-8") as fh:
                raw = json.load(fh)
            refused = 0
            for entry in raw if isinstance(raw, list) else []:
                if not _route_ok(entry):
                    refused += 1
                    continue
                try:
                    b = Binding(**entry)
                except TypeError:
                    continue
                if isinstance(b.ref, str) and b.ref.startswith(REF_PREFIX):
                    self._items[b.ref] = b
            if refused:
                logger.error("plugin bindings: %d binding(s) without a Telegram route "
                             "or a delivery refused", refused)
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001 — a corrupt store loses its bindings, loudly
            logger.warning("plugin bindings: unreadable store %s ignored", self._path,
                           exc_info=True)

    def _prune_and_save_locked(self) -> None:
        now = self._now()
        dropped: list[Binding] = []
        for ref, b in list(self._items.items()):
            if b.last_used + BINDING_TTL_S <= now:
                dropped.append(self._items.pop(ref))
        if len(self._items) > MAX_BINDINGS:
            for b in sorted(self._items.values(), key=lambda b: b.last_used)[
                    :len(self._items) - MAX_BINDINGS]:
                dropped.append(self._items.pop(b.ref))
        kept_sessions = {b.session_id for b in self._items.values()}
        for b in dropped:
            if b.session_id and b.session_id not in kept_sessions:
                scratchpad.PADS.discard(b.session_id)
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            atomic_write_json(self._path, [dataclasses.asdict(b) for b in self._items.values()],
                              mode=PRIVATE)
        except OSError:
            logger.warning("plugin bindings: could not write %s", self._path, exc_info=True)

    def for_turn(self, *, plugin: str, turn_key: str, identity: Any, chain: tuple,
                 origin: dict) -> Binding:
        """The binding of this plugin's call in this turn — minted once. Its
        route is the turn's; a turn that carries none (an engagement's) takes
        the bot the call's grant identity was resolved on. Its delivery is the
        call's (``identity.delivery``), ``None`` included."""
        with self._lock:
            self._load_locked()
            for b in self._items.values():
                if b.plugin == plugin and b.turn_key == turn_key and turn_key:
                    b.last_used = self._now()
                    self._prune_and_save_locked()
                    return b
            now = self._now()
            b = Binding(
                ref=REF_PREFIX + secrets.token_hex(16), plugin=plugin,
                operator_id=int(identity.operator_id), chat_id=int(identity.chat_id),
                requester=chain[-2] if len(chain) >= 2 else (chain[0] if chain else ""),
                requester_kind=requester_kind(chain), chain=list(chain),
                origin=_kept_origin(origin, identity),
                turn_key=turn_key, created_at=now, last_used=now,
                delivery=_delivery_json(identity),
                session_id=origin.get("_delegated_session_id"))
            self._items[b.ref] = b
            self._prune_and_save_locked()
            return b

    def get(self, ref: Any, *, plugin: str, operator_id: int) -> "Binding | None":
        """The binding *ref* names, only for the plugin and operator it was
        minted for; touching it extends its life."""
        if not isinstance(ref, str) or not ref.startswith(REF_PREFIX):
            return None
        with self._lock:
            self._load_locked()
            b = self._items.get(ref)
            if b is None or b.plugin != plugin or b.operator_id != int(operator_id):
                return None
            b.last_used = self._now()
            self._prune_and_save_locked()
            return b

    def peek(self, ref: Any) -> "Binding | None":
        """Casa's own read (the driver sweep): no plugin or operator check —
        the sweep compares the plugin itself."""
        with self._lock:
            self._load_locked()
            return self._items.get(ref) if isinstance(ref, str) else None

    def adopt(self, ref: str, session_id: str) -> None:
        """A continuation that started fresh: its session is the request's conversation
        from now on, with its own count and its own cost, and it takes the request's pad along:
        a fresh session is where the model needs its notes most. Another binding of the old
        session keeps its own copy."""
        with self._lock:
            self._load_locked()
            b = self._items.get(ref)
            if b is not None:
                old = b.session_id
                b.session_id, b.resume_failures, b.cost_seen = session_id, 0, 0.0
                if old and old != session_id:
                    scratchpad.PADS.move(old, session_id, keep=any(
                        other.session_id == old for other in self._items.values()))
                self._prune_and_save_locked()

    def turn_ended(self, ref: str, *, raised: bool) -> None:
        """A resumed turn ended: one more raised in a row, or none."""
        with self._lock:
            self._load_locked()
            b = self._items.get(ref)
            if b is not None:
                b.resume_failures = b.resume_failures + 1 if raised else 0
                self._prune_and_save_locked()

    def holds_session(self, session_id: "str | None") -> bool:
        """Whether a kept binding names this session, so a later turn can resume it. A binding
        past its life counts until a write prunes it: ``get`` can still revive it until then."""
        with self._lock:
            self._load_locked()
            return bool(session_id) and any(b.session_id == session_id
                                            for b in self._items.values())

    def note_cost(self, session_id: "str | None", cumulative: float) -> float:
        """What this turn cost: a resumed session reports its running total, so the
        difference; the whole value when no binding holds that session."""
        with self._lock:
            self._load_locked()
            for b in self._items.values():
                if b.session_id == session_id and session_id:
                    spent = max(cumulative - b.cost_seen, 0.0)
                    b.cost_seen = max(cumulative, b.cost_seen)
                    self._prune_and_save_locked()
                    return spent
        return cumulative


def _delivery_json(identity: Any) -> "list | None":
    delivery = getattr(identity, "delivery", None)
    return None if delivery is None else delivery.to_json()


def _kept_origin(origin: dict, identity: Any) -> dict:
    return {**{k: origin[k] for k in _ORIGIN_KEYS if k in origin},
            **route_of(origin, identity)}


BINDINGS = BindingStore()
