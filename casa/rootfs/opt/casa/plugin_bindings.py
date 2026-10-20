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
in one turn returns the same reference."""
from __future__ import annotations

import dataclasses
import logging
import os
import secrets
import threading
import time
from typing import Any

from atomic_io import PRIVATE, atomic_write_json

logger = logging.getLogger(__name__)

BINDINGS_PATH = "/data/plugin-bindings.json"
BINDING_TTL_S = 30 * 24 * 3600.0
MAX_BINDINGS = 500
REF_PREFIX = "casa-bind-"

# The origin markers a continuation turn is rebuilt from — plain, persistable
# values only. Never the operator's words: a continuation has none
# (``handoff.operator_words``), so a run it starts goes to his tap.
_ORIGIN_KEYS = ("role", "channel", "chat_id", "user_id", "user_name",
                "_origin_route", "_origin_clearance")


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
            for entry in raw if isinstance(raw, list) else []:
                try:
                    b = Binding(**entry)
                except TypeError:
                    continue
                if isinstance(b.ref, str) and b.ref.startswith(REF_PREFIX):
                    self._items[b.ref] = b
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001 — a corrupt store loses its bindings, loudly
            logger.warning("plugin bindings: unreadable store %s ignored", self._path,
                           exc_info=True)

    def _prune_and_save_locked(self) -> None:
        now = self._now()
        for ref, b in list(self._items.items()):
            if b.last_used + BINDING_TTL_S <= now:
                del self._items[ref]
        if len(self._items) > MAX_BINDINGS:
            for b in sorted(self._items.values(), key=lambda b: b.last_used)[
                    :len(self._items) - MAX_BINDINGS]:
                del self._items[b.ref]
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            atomic_write_json(self._path, [dataclasses.asdict(b) for b in self._items.values()],
                              mode=PRIVATE)
        except OSError:
            logger.warning("plugin bindings: could not write %s", self._path, exc_info=True)

    def for_turn(self, *, plugin: str, turn_key: str, identity: Any, chain: tuple,
                 origin: dict) -> Binding:
        """The binding of this plugin's call in this turn — minted once."""
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
                origin={k: origin[k] for k in _ORIGIN_KEYS if k in origin},
                turn_key=turn_key, created_at=now, last_used=now)
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


BINDINGS = BindingStore()
