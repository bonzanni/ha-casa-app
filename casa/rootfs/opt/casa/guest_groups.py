"""Which groups the operator allowed a specialist's bot to answer in.

A specialist bot can be summoned in a group it is not a member of. That is
only honoured for groups recorded here, and only for the specialist that was
installed when the group was allowed: an entry remembers the component id, so
a replacement specialist in the same role starts without the allowance.

The file is ``{"<role>": {"<chat_id>": {"title", "type", "component_id",
"allowed_at"}}}``, saved whole after each change (atomically, owner-only). A
file that does not hold that shape is moved aside to ``.corrupt`` and the store
starts empty.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import specialist_install
import specialist_registry
from atomic_io import PRIVATE, atomic_write_json

logger = logging.getLogger(__name__)

# How often a guest reply's progress message is refreshed, in seconds.
GUEST_TICK_S = 2.0
# How long a guest turn may take to reach a final answer, in seconds.
GUEST_TERMINAL_BUDGET_S = 120.0
# The longest guest reply text sent in one message (Telegram's message limit).
GUEST_TEXT_LIMIT = 4096
# Words shown while a guest reply is being prepared.
THINKING_WORDS = ("Thinking", "Working on it", "Looking into it", "Almost there")

_ENTRY_KEYS = {"title", "type", "component_id", "allowed_at"}
_DEFAULT_PATH = Path("/data/agent-voices-guests.json")
_FILE_NAME = _DEFAULT_PATH.name


@dataclass(frozen=True)
class ChatRef:
    id: int
    title: str
    type: str


@dataclass(frozen=True)
class GuestEntry:
    role: str
    chat: ChatRef
    component_id: str
    allowed_at: str  # ISO 8601


def _is_int_string(key: object) -> bool:
    if not isinstance(key, str):
        return False
    try:
        int(key)
    except ValueError:
        return False
    return True


def _valid(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    for role, chats in data.items():
        if not isinstance(role, str) or not isinstance(chats, dict):
            return False
        for key, rec in chats.items():
            if not _is_int_string(key) or not isinstance(rec, dict):
                return False
            if set(rec) != _ENTRY_KEYS or not all(
                    isinstance(rec[k], str) for k in _ENTRY_KEYS):
                return False
            try:
                datetime.fromisoformat(rec["allowed_at"])
            except ValueError:
                return False
    return True


class GuestStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[int, GuestEntry]] = {}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not _valid(data):
                raise ValueError("not a guest-group record set")
        except (OSError, ValueError):
            logger.error("%s is corrupt or unreadable; moving it to .corrupt and "
                         "starting empty", self.path)
            try:
                os.replace(self.path, f"{self.path}.corrupt")
            except OSError:
                pass
            return
        for role, chats in data.items():
            for key, rec in chats.items():
                chat = ChatRef(int(key), rec["title"], rec["type"])
                self._data.setdefault(role, {})[chat.id] = GuestEntry(
                    role, chat, rec["component_id"], rec["allowed_at"])

    def _snapshot(self) -> dict:
        return {
            role: {str(cid): {"title": e.chat.title, "type": e.chat.type,
                              "component_id": e.component_id,
                              "allowed_at": e.allowed_at}
                   for cid, e in chats.items()}
            for role, chats in self._data.items() if chats
        }

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, data, mode=PRIVATE)

    async def save(self) -> None:
        async with self._lock:
            data = self._snapshot()
            await asyncio.to_thread(self._write, data)

    def allow(self, role: str, chat: ChatRef, component_id: str) -> None:
        """Record the allowance in memory; the caller awaits ``save()``."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._data.setdefault(role, {})[chat.id] = GuestEntry(
            role, chat, component_id, now)

    def revoke(self, role: str, chat_id: int) -> bool:
        chats = self._data.get(role)
        if not chats or chat_id not in chats:
            return False
        del chats[chat_id]
        if not chats:
            del self._data[role]
        return True

    def allowed(self, role: str, chat_id: int, component_id: str | None) -> bool:
        """True only for an entry made for this very component."""
        entry = self._data.get(role, {}).get(chat_id)
        return (entry is not None and component_id is not None
                and entry.component_id == component_id)

    def entries(self, role: str | None = None) -> list[GuestEntry]:
        roles = [role] if role is not None else list(self._data)
        return [e for r in roles for e in self._data.get(r, {}).values()]

    def status(self, role: str, chat_id: int) -> Literal[
            "allowed", "previous_specialist", "none"]:
        """``allowed`` for the current specialist, ``previous_specialist`` when
        the entry was made for a different one, ``none`` when there is none.
        Without a way to tell the current component, use ``allowed()``."""
        entry = self._data.get(role, {}).get(chat_id)
        if entry is None:
            return "none"
        return "allowed" if entry.component_id == component_of(role) \
            else "previous_specialist"


def component_of(role: str) -> str | None:
    """The component id of the specialist installed in *role*, or ``None``."""
    instance = specialist_registry.get_installed_instance(role)
    active = getattr(instance, "active", None)
    if active is None:
        return None
    try:
        return specialist_install.parse_component_root(active.root)[0]
    except (ValueError, AttributeError, TypeError):
        return None


_root: Path | None = None
_store: GuestStore | None = None


def configure(root: Path | None) -> None:
    """Keep the store under *root* instead of ``/data`` (``None`` resets)."""
    global _root, _store  # noqa: PLW0603
    _root = Path(root) if root is not None else None
    _store = None


def store() -> GuestStore:
    """The process's store, created on first use."""
    global _store  # noqa: PLW0603
    if _store is None:
        _store = GuestStore((_root / _FILE_NAME) if _root else _DEFAULT_PATH)
    return _store
