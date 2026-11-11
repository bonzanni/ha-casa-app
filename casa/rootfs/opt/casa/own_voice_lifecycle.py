"""The lifecycle of an agent's own Telegram bot: offer, creation, binding,
self-setup, opening, token changes, dormancy and release.

A specialist that declares its own chat (``own_chat`` in its manifest) gets a
bot of its own, created by the operator from a link Casa posts in the
resident's chat and managed through the resident's bot. This module keeps the
record of every such bot and drives it through its phases.

Three rules shape every transition:

* **Durable before effect.** Every transition is a sequence of named phases.
  Before any external effect (a Telegram call, a 1Password write, a card
  post) the phase that names the effect is persisted, every effect is
  idempotent or reconciled by lookup, and every phase has a boot
  reconciliation that completes or rolls the transition back
  (:data:`BOOT_STEPS`). A phase stays put after a restart only to wait on a
  named condition: an unbound bot waits on the operator's answer, a
  recovering bot on a successful token fetch, and every fetch on the
  resident's bot being authorized. The token's source of truth is Telegram:
  while the resident's bot manages a bot, ``getManagedBotToken`` returns its
  current token, so a lost stored token is fetched again, never given up on.
* **Held updates leave the journal only through intake.** An update the bot
  cannot hand to a running agent yet (unbound, mid-transition, no queue for
  its agent) is appended to the bot's journal, with fsync, before Telegram
  is acknowledged. A release hands the journal's records, in arrival order,
  to the endpoint's normal intake; a record is marked released once intake
  accepted it, and a refused one keeps itself and every later record held.
  After that a message has exactly the guarantee of ordinary intake: a crash
  during a release delivers the records still in the journal again (at
  least once), and a turn lost after acceptance is lost as any other.
* **The committed declaration is truth.** A role is eligible for its own
  chat when it is installed and its committed active component declares one.
  A bound bot whose role stops being eligible holds its intake and goes
  dormant; one whose role becomes eligible again is bound again.

Persisted state: the records in ``agent-voices.json`` (one per agent, one per
bot), the held journals in ``agent-voices/held/<bot_id>.jsonl``, and each
agent's token in its own 1Password item (``Telegram <Role>``, created once and
then edited in place) or, without 1Password, in
``agent-voices/tokens/<role>``. Before a bot is bound to a role its token is
kept in memory only and fetched again at every boot.

The Telegram side lives behind :class:`TelegramHost`, so the transitions
themselves are plain code over a host object.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import quote_plus

from atomic_io import PRIVATE, atomic_write_json, fsync_directory
from channels.tg_address import AddressError, TgAddress

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

SEEN = "seen"
UNBOUND = "unbound"
BINDING = "binding"
BOUND = "bound"
RECOVERING = "recovering"
DORMANT = "dormant"
RELEASING = "releasing"
RELEASED = "released"
SWITCHING = "switching"
PHASES = (SEEN, UNBOUND, BINDING, BOUND, RECOVERING, DORMANT, RELEASING, RELEASED,
          SWITCHING)

#: Each phase's boot reconciliation, by method name. Every phase has one; a
#: phase added without one fails the crash sweep that iterates this table.
BOOT_STEPS: dict[str, str] = {
    SEEN: "_step_seen",
    UNBOUND: "_step_unbound",
    BINDING: "_step_binding",
    BOUND: "_step_bound",
    RECOVERING: "_step_recovering",
    DORMANT: "_step_dormant",
    RELEASING: "_step_releasing",
    RELEASED: "_step_released",
    SWITCHING: "_step_switching",
}

#: The fetch failures that establish a bot is no longer managed by the
#: resident's bot, so its record may be released: ``getManagedBotToken``
#: answering 403 Forbidden with "user is deactivated", which is what Telegram
#: answers for a managed bot its owner deleted in BotFather. The match is the
#: exception class and a case-insensitive substring of its message. Every
#: other failure keeps the bot recovering, retried with backoff: a 400 "user
#: is not a bot", a manager token that is not authorized, a network error.
RELEASE_ON: tuple[tuple[str, str], ...] = (("Forbidden", "user is deactivated"),)

ASK_NONE = "None — I'll delete it"
BOTFATHER_DELETE = "https://t.me/BotFather?start=deletebot"
COMMANDS = (("new", "Start a fresh conversation"),)
SETUP_REPLY = "Setting up this chat — I'll answer here as soon as it's ready."
TOKEN_TAG = "casa-agent-bot"
RETRY_DELAYS_S = (30.0, 60.0, 120.0, 300.0, 900.0, 1800.0, 3600.0)
_OP_TIMEOUT_S = 30
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def establishes_deletion(exc: BaseException) -> bool:
    """Whether a ``getManagedBotToken`` failure is one of :data:`RELEASE_ON`:
    an instance of the named ``telegram.error`` class whose message holds the
    text, in any case."""
    import telegram.error as tg_error
    text = str(exc).lower()
    for name, needle in RELEASE_ON:
        cls = getattr(tg_error, name, None)
        if isinstance(cls, type) and isinstance(exc, cls) and needle in text:
            return True
    return False


def token_belongs_to(token: str, bot_id: int) -> bool:
    """Whether *token* is *bot_id*'s: a bot token begins with its bot's id
    and a colon."""
    return isinstance(token, str) and token.split(":", 1)[0] == str(bot_id)


def is_unauthorized(exc: BaseException) -> bool:
    """A token Telegram does not accept (401/404 on any method)."""
    from telegram.error import InvalidToken
    return isinstance(exc, InvalidToken)


# ---------------------------------------------------------------------------
# The suggestion: the agent's bot named after the resident's own
# ---------------------------------------------------------------------------

def _fold(text: str) -> str:
    """ASCII folding, keeping only ``[A-Za-z0-9]``."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]", "", folded)


def valid_username(name: str) -> bool:
    """Telegram's rule for a bot username: 5–32 of ``[A-Za-z0-9_]``, ending
    in ``bot`` in any case."""
    return bool(_USERNAME_RE.match(name or "")) and name.lower().endswith("bot")


def suggest(*, resident_name: str, resident_first_name: str, resident_username: str,
            agent_name: str, role: str, taken: "set[str] | frozenset[str]" = frozenset(),
            ) -> tuple[str, str]:
    """The suggested ``(name, username)`` of *agent_name*'s bot, derived from
    the resident's bot by replacing the resident's name with the agent's —
    a string operation that compares strings and nothing else.

    *taken* holds the usernames (lower-cased) suggested to other agents still
    waiting for a bot; a clash gets ``_<role>`` before the ``bot`` suffix. An
    empty username means the operator types one."""
    e, a = resident_name, agent_name
    folded = _fold(a)
    name = resident_first_name.replace(e, a) if e and e in resident_first_name else a
    if e and e in (resident_username or ""):
        username = resident_username.replace(e, folded)
    else:
        username = folded + "Bot"
    if not valid_username(username):
        username = folded + "Bot"
    if not valid_username(username):
        return name, ""
    if username.lower() in {t.lower() for t in taken}:
        slug = re.sub(r"[^A-Za-z0-9_]", "", role)
        username = f"{username[:-3]}_{slug}{username[-3:]}"
        if not valid_username(username) or username.lower() in {t.lower() for t in taken}:
            return name, ""
    return name, username


def create_link(resident_username: str, name: str, username: str) -> str:
    """The link that opens Telegram's pre-filled "Create Bot" form."""
    path = f"https://t.me/newbot/{resident_username}"
    if username:
        path += f"/{username}"
    return f"{path}?name={quote_plus(name)}"


# ---------------------------------------------------------------------------
# The records
# ---------------------------------------------------------------------------

_AGENT_KEYS = {"offered_at", "offer_card", "bot_id", "chat_open_at", "label",
               "suggestion", "token_item", "handles", "change", "moved_from"}
_CHANGE_KEYS = {"new_bot", "old_bot", "old_username"}
_BOT_KEYS = {"phase", "role", "username", "name", "ask", "setup_digest",
             "created_at", "photo_set"}


def _valid_addr(v: Any) -> bool:
    if v is None:
        return True
    try:
        TgAddress.from_json(v)
    except AddressError:
        return False
    return True


def _valid_store(data: Any) -> bool:
    if not isinstance(data, dict) or set(data) != {"agents", "bots"}:
        return False
    agents, bots = data["agents"], data["bots"]
    if not isinstance(agents, dict) or not isinstance(bots, dict):
        return False
    for role, rec in agents.items():
        if not isinstance(role, str) or not isinstance(rec, dict) or set(rec) != _AGENT_KEYS:
            return False
        if not _valid_addr(rec["offer_card"]):
            return False
        if rec["bot_id"] is not None and not (
                isinstance(rec["bot_id"], int) and not isinstance(rec["bot_id"], bool)):
            return False
        handles = rec["handles"]
        if not isinstance(handles, list) or not all(isinstance(h, str) for h in handles):
            return False
        change = rec["change"]
        if change is not None and not (isinstance(change, dict) and set(change) == _CHANGE_KEYS):
            return False
    for key, rec in bots.items():
        if not (isinstance(key, str) and key.isdigit()) or not isinstance(rec, dict):
            return False
        if set(rec) != _BOT_KEYS or rec["phase"] not in PHASES:
            return False
        ask = rec["ask"]
        if ask is not None and not (
                isinstance(ask, dict) and set(ask) == {"state", "card", "rid", "candidates"}
                and ask["state"] in ("posting", "posted") and _valid_addr(ask["card"])
                and isinstance(ask["candidates"], list)):
            return False
    return True


class VoiceStore:
    """``agent-voices.json``: the agent and bot records, in memory, saved
    whole after each change (atomically, owner-only). A file that does not
    hold exactly this shape is moved aside to ``.corrupt`` and the store
    starts empty."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.agents: dict[str, dict] = {}
        self.bots: dict[int, dict] = {}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not _valid_store(data):
                raise ValueError("not an agent-voices record set")
        except (OSError, ValueError):
            logger.error("%s is corrupt or unreadable; moving it to .corrupt and "
                         "starting empty", self.path)
            try:
                os.replace(self.path, f"{self.path}.corrupt")
            except OSError:
                pass
            return
        self.agents = {role: dict(rec) for role, rec in data["agents"].items()}
        self.bots = {int(key): dict(rec) for key, rec in data["bots"].items()}

    def snapshot(self) -> dict:
        return {"agents": {r: dict(a) for r, a in self.agents.items()},
                "bots": {str(b): dict(r) for b, r in self.bots.items()}}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, data, mode=PRIVATE)

    async def save(self) -> None:
        async with self._lock:
            data = self.snapshot()
            await asyncio.to_thread(self._write, data)

    def agent(self, role: str, *, create: bool = False) -> "dict | None":
        rec = self.agents.get(role)
        if rec is None and create:
            rec = {"offered_at": None, "offer_card": None, "bot_id": None,
                   "chat_open_at": None, "label": role, "suggestion": None,
                   "token_item": None, "handles": [], "change": None,
                   "moved_from": None}
            self.agents[role] = rec
        return rec

    def bot_of(self, role: str) -> "tuple[int, dict] | None":
        agent = self.agents.get(role)
        bot_id = agent.get("bot_id") if agent else None
        if bot_id is None or bot_id not in self.bots:
            return None
        return bot_id, self.bots[bot_id]


# ---------------------------------------------------------------------------
# The held journal
# ---------------------------------------------------------------------------

class HeldJournal:
    """One append-only file per bot: ``{"update": <raw update>}`` lines and
    ``{"released": <update_id>}`` lines. Appends are fsynced (and the
    directory, when the file is new); a line torn by a crash is skipped, and
    the next append starts on a fresh line. The file is deleted once every
    update in it is released."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def path(self, bot_id: int) -> Path:
        return self.dir / f"{int(bot_id)}.jsonl"

    def _append_line(self, bot_id: int, record: dict) -> None:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.path(bot_id)
        created = not path.exists()
        line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                line = b"\n" + line
            view = memoryview(line)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            fsync_directory(self.dir)

    def append(self, bot_id: int, update: dict) -> None:
        self._append_line(bot_id, {"update": update})

    def pending(self, bot_id: int) -> list[dict]:
        """The updates not yet released, in arrival order, each once."""
        try:
            raw = self.path(bot_id).read_bytes()
        except FileNotFoundError:
            return []
        updates: list[dict] = []
        seen: set[int] = set()
        released: set[int] = set()
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # a line torn by a crash
            if not isinstance(rec, dict):
                continue
            if "released" in rec:
                released.add(rec["released"])
                continue
            upd = rec.get("update")
            uid = upd.get("update_id") if isinstance(upd, dict) else None
            if not isinstance(uid, int) or isinstance(uid, bool) or uid in seen:
                continue
            seen.add(uid)
            updates.append(upd)
        return [u for u in updates if u["update_id"] not in released]

    def mark_released(self, bot_id: int, update_id: int) -> None:
        self._append_line(bot_id, {"released": update_id})
        if not self.pending(bot_id):
            self.drop(bot_id)

    def drop(self, bot_id: int) -> None:
        try:
            self.path(bot_id).unlink()
        except FileNotFoundError:
            return
        fsync_directory(self.dir)


# ---------------------------------------------------------------------------
# The token store
# ---------------------------------------------------------------------------

class TokenStoreError(RuntimeError):
    """A token could not be stored; the message never carries the token."""


def item_title(role: str) -> str:
    """The 1Password item of *role*'s bot token (a simple name: an
    ``op://`` reference rejects a colon in the item segment)."""
    return f"Telegram {role[:1].upper()}{role[1:]}"


class TokenStore:
    """Each bound agent's bot token: in its own 1Password item when Casa has
    a vault and a service account, else in a file of its own.

    The item is created once and then edited in place from a template piped
    on stdin, so the token never appears in ``op``'s argv and the item id
    stays stable. When the stored id is missing the item is found by its
    title and :data:`TOKEN_TAG`; a create whose outcome is unknown (a crash,
    a timeout) is therefore reconciled by that lookup, never by deleting.
    Blocking: call it off the event loop."""

    def __init__(self, directory: Path, *, env: "dict | None" = None,
                 run: "Callable[..., Any] | None" = None) -> None:
        self.dir = Path(directory)
        self._env = env
        self._run_fn = run

    def _environ(self) -> dict:
        return os.environ if self._env is None else self._env

    def onepassword(self) -> bool:
        env = self._environ()
        return bool(env.get("ONEPASSWORD_DEFAULT_VAULT")) and bool(
            env.get("OP_SERVICE_ACCOUNT_TOKEN"))

    def _vault(self) -> str:
        return self._environ()["ONEPASSWORD_DEFAULT_VAULT"]

    def _op(self, args: list[str], stdin: "str | None" = None):
        run = self._run_fn or subprocess.run
        return run(["op", *args], input=stdin, capture_output=True, text=True,
                   timeout=_OP_TIMEOUT_S,
                   **({} if stdin is not None else {"stdin": subprocess.DEVNULL}))

    def _template(self, role: str, token: str) -> str:
        return json.dumps({
            "title": item_title(role), "category": "PASSWORD", "tags": [TOKEN_TAG],
            "fields": [{"id": "password", "type": "CONCEALED", "purpose": "PASSWORD",
                        "label": "password", "value": token}]})

    def _lookup(self, role: str) -> "str | None":
        r = self._op(["item", "list", "--vault", self._vault(), "--tags", TOKEN_TAG,
                      "--format", "json"])
        if r.returncode != 0:
            raise TokenStoreError(f"op item list failed ({r.returncode})")
        try:
            rows = json.loads(r.stdout or "[]")
        except ValueError as exc:
            raise TokenStoreError("op item list was unreadable") from exc
        title = item_title(role)
        ids = sorted(row["id"] for row in rows if isinstance(row, dict)
                     and row.get("title") == title and isinstance(row.get("id"), str)
                     and TOKEN_TAG in (row.get("tags") or []))
        return ids[0] if ids else None

    def _file(self, role: str) -> Path:
        if not role or "/" in role or role.startswith("."):
            raise TokenStoreError("not a role name")
        return self.dir / role

    def store(self, role: str, token: str, item_id: "str | None") -> "str | None":
        """Write *token* for *role*, in place. Returns the 1Password item id
        (``None`` for the file store). Raises :class:`TokenStoreError`."""
        if not self.onepassword():
            path = self._file(role)
            self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
            try:
                os.write(fd, token.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            fsync_directory(self.dir)
            return None
        template = self._template(role, token)
        try:
            if item_id:
                r = self._op(["item", "edit", item_id, "--vault", self._vault(),
                              "--format", "json"], stdin=template)
                if r.returncode == 0:
                    return item_id
            found = self._lookup(role)
            if found:
                r = self._op(["item", "edit", found, "--vault", self._vault(),
                              "--format", "json"], stdin=template)
                if r.returncode != 0:
                    raise TokenStoreError(f"op item edit failed ({r.returncode})")
                return found
            try:
                r = self._op(["item", "create", "--vault", self._vault(),
                              "--format", "json", "-"], stdin=template)
            except subprocess.TimeoutExpired:
                found = self._lookup(role)
                if found:
                    return found
                raise TokenStoreError("op item create timed out") from None
            if r.returncode != 0:
                raise TokenStoreError(f"op item create failed ({r.returncode})")
            try:
                created = json.loads(r.stdout)["id"]
            except (ValueError, KeyError, TypeError):
                created = self._lookup(role)
            if not isinstance(created, str):
                raise TokenStoreError("op item create gave no item id")
            return created
        except subprocess.TimeoutExpired:
            raise TokenStoreError("op timed out") from None
        except OSError as exc:
            raise TokenStoreError(f"op did not start ({type(exc).__name__})") from None

    def load(self, role: str, item_id: "str | None") -> "str | None":
        """The stored token of *role*, or ``None`` when it is missing or
        cannot be read (it is then fetched from Telegram again)."""
        try:
            if not self.onepassword():
                try:
                    return self._file(role).read_text(encoding="utf-8").strip() or None
                except FileNotFoundError:
                    return None
            target = item_id or self._lookup(role)
            if not target:
                return None
            r = self._op(["item", "get", target, "--vault", self._vault(),
                          "--fields", "label=password", "--reveal"])
            if r.returncode != 0:
                return None
            return (r.stdout or "").strip() or None
        except (TokenStoreError, OSError, subprocess.TimeoutExpired):
            return None


# ---------------------------------------------------------------------------
# The lifecycle
# ---------------------------------------------------------------------------

class Lifecycle:
    """Every own-chat bot and agent record, and the transitions between
    their phases. One per process (:func:`current`).

    Each bot has a per-bot lock that covers its intake (hold or deliver),
    every phase change and its journal release. A registry lock covers facts
    that span bots ("is this agent still waiting for one"). The order is
    always per-bot, then registry."""

    def __init__(self, *, root: "Path | str" = "/data", host: Any = None,
                 tokens: "TokenStore | None" = None,
                 retry_delays: "tuple[float, ...] | None" = RETRY_DELAYS_S) -> None:
        root = Path(root)
        self.root = root
        self.host = host if host is not None else TelegramHost()
        self.store = VoiceStore(root / "agent-voices.json")
        self.journal = HeldJournal(root / "agent-voices" / "held")
        self.tokens = tokens if tokens is not None else TokenStore(root / "agent-voices" / "tokens")
        self.retry_delays = retry_delays
        self.sleep = asyncio.sleep
        self._bot_locks: dict[int, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()
        self._tokens: dict[int, str] = {}
        self._reconciled: set[int] = set()
        self._told_setup: set[int] = set()
        self._live_asks: set[str] = set()
        self._retry_tasks: dict[int, asyncio.Task] = {}
        self._attempts: dict[int, int] = {}
        self._tasks: set[asyncio.Task] = set()
        self._manager_down = False
        self._manager_told = False
        self._avatar_told: set[str] = set()

    # -- reads ------------------------------------------------------------

    def _configs(self) -> dict:
        try:
            return dict(self.host.configs() or {})
        except Exception:  # noqa: BLE001 — no registry yet means nobody is installed
            logger.debug("own chats: no installed agents readable", exc_info=True)
            return {}

    def declared(self, role: str) -> bool:
        cfg = self._configs().get(role)
        return cfg is not None and getattr(cfg, "own_chat", None) is not None

    def _label(self, role: str) -> str:
        cfg = self._configs().get(role)
        name = getattr(getattr(cfg, "character", None), "name", "") if cfg else ""
        if name:
            return name
        agent = self.store.agents.get(role)
        return (agent or {}).get("label") or role

    def _resident_name(self) -> str:
        try:
            return self.host.resident_name() or ""
        except Exception:  # noqa: BLE001
            return ""

    def _resident(self) -> str:
        return self._resident_name() or "the assistant"

    def state(self, role: str) -> str:
        found = self.store.bot_of(role)
        phase = found[1]["phase"] if found else None
        if phase == DORMANT:
            return "dormant"
        if not self.declared(role):
            return "not_declared"
        if phase != BOUND:
            return "no_bot"
        return "open" if self.store.agents[role].get("chat_open_at") else "not_open"

    def is_recovering(self, role: str) -> bool:
        found = self.store.bot_of(role)
        return bool(found) and found[1]["phase"] == RECOVERING

    def bot_id_for(self, role: str) -> "int | None":
        found = self.store.bot_of(role)
        return found[0] if found else None

    def link_for(self, role: str) -> "str | None":
        found = self.store.bot_of(role)
        username = found[1].get("username") if found else None
        return f"https://t.me/{username}" if username else None

    # -- small helpers ----------------------------------------------------

    def _lock(self, bot_id: int) -> asyncio.Lock:
        lock = self._bot_locks.get(bot_id)
        if lock is None:
            lock = self._bot_locks[bot_id] = asyncio.Lock()
        return lock

    def _waiting(self, role: str) -> bool:
        """*role* declares its own chat and has no bot."""
        agent = self.store.agents.get(role)
        return self.declared(role) and (agent is None or agent.get("bot_id") is None)

    def _waiting_roles(self) -> list[str]:
        return sorted(r for r in self._configs() if self._waiting(r))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _schedule_retry(self, bot_id: int, why: str) -> None:
        if self.retry_delays is None:
            logger.info("own chat bot %s: %s; retried at the next trigger", bot_id, why)
            return
        task = self._retry_tasks.get(bot_id)
        if task is not None and not task.done():
            return
        n = self._attempts.get(bot_id, 0)
        self._attempts[bot_id] = n + 1
        delay = self.retry_delays[min(n, len(self.retry_delays) - 1)]
        logger.info("own chat bot %s: %s; retrying in %.0f s", bot_id, why, delay)

        async def _retry() -> None:
            await self.sleep(delay)
            self._retry_tasks.pop(bot_id, None)
            async with self._lock(bot_id):
                await self._reconcile_locked(bot_id)

        self._retry_tasks[bot_id] = self._spawn(_retry())

    async def close(self) -> None:
        """Cancel pending retries (shutdown, tests)."""
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _manager_unauthorized(self) -> None:
        self._manager_down = True
        if not self._manager_told:
            self._manager_told = True
            logger.error(
                "The resident's Telegram bot is not authorized: no agent bot "
                "can get its token until a valid token is set in the app "
                "options and Casa restarts")

    async def _fetch(self, bot_id: int) -> "tuple[str | None, str]":
        """``getManagedBotToken`` through the resident's bot:
        ``(token, "ok")``, ``(None, "deleted")`` for a failure in
        :data:`RELEASE_ON`, ``(None, "wait")`` while the resident's bot is
        unauthorized, ``(None, "retry")`` for anything else."""
        if self._manager_down:
            return None, "wait"
        try:
            token = await self.host.fetch_token(bot_id)
        except Exception as exc:  # noqa: BLE001 — classified below
            if establishes_deletion(exc):
                logger.warning("own chat bot %s is no longer managed: %s", bot_id,
                               type(exc).__name__)
                return None, "deleted"
            if is_unauthorized(exc):
                self._manager_unauthorized()
                return None, "wait"
            logger.warning("own chat bot %s: token fetch failed (%s)", bot_id,
                           type(exc).__name__)
            return None, "retry"
        if not isinstance(token, str) or not token:
            return None, "retry"
        self._register_secret(token)
        self._attempts.pop(bot_id, None)
        return token, "ok"

    @staticmethod
    def _register_secret(token: str) -> None:
        try:
            import log_redact
            log_redact.register_secret(token)
        except Exception:  # noqa: BLE001 — redaction is best effort
            pass

    async def _store_token(self, role: str, token: str) -> bool:
        agent = self.store.agent(role, create=True)
        try:
            item = await asyncio.to_thread(self.tokens.store, role, token,
                                           agent.get("token_item"))
        except TokenStoreError as exc:
            logger.warning("own chat %s: token not stored (%s)", role, exc)
            return False
        if item != agent.get("token_item"):
            agent["token_item"] = item
            await self.store.save()
        return True

    async def _load_token(self, role: str, bot_id: int) -> "str | None":
        """*role*'s stored token, when it is *bot_id*'s. A stored token of
        another bot is never used: the caller fetches the right one."""
        agent = self.store.agents.get(role) or {}
        token = await asyncio.to_thread(self.tokens.load, role, agent.get("token_item"))
        if not token:
            return None
        self._register_secret(token)
        if not token_belongs_to(token, bot_id):
            logger.warning("own chat %s: the stored token is not bot %s's; fetching "
                           "its own", role, bot_id)
            return None
        return token

    # -- cards ------------------------------------------------------------

    async def _edit(self, addr_json: Any, text: str,
                    buttons: "list[tuple[str, str]] | None" = None) -> None:
        if addr_json is None:
            return
        try:
            await self.host.edit_card(TgAddress.from_json(addr_json), text, buttons or [])
        except Exception:  # noqa: BLE001 — a card edit is best effort
            logger.warning("own chats: card edit failed", exc_info=True)

    async def _post(self, text: str, buttons: list[tuple[str, str]]) -> "TgAddress | None":
        try:
            return await self.host.post_card(text, buttons)
        except Exception:  # noqa: BLE001
            logger.warning("own chats: card post failed", exc_info=True)
            return None

    def _bound_card(self, role: str) -> tuple[str, list[tuple[str, str]]]:
        label = self._label(role)
        agent = self.store.agents.get(role) or {}
        if agent.get("chat_open_at"):
            return f"{label}'s chat is open.", []
        found = self.store.bot_of(role)
        username = found[1].get("username") if found else ""
        return (f"{label}'s chat is ready: @{username}. Open it and press Start.",
                [(f"Open {label}'s chat", f"https://t.me/{username}")])

    async def _card_bound(self, role: str) -> None:
        agent = self.store.agent(role, create=True)
        text, buttons = self._bound_card(role)
        if agent.get("offer_card") is not None:
            await self._edit(agent["offer_card"], text, buttons)
            return
        addr = await self._post(text, buttons)
        if addr is not None:
            agent["offer_card"] = addr.to_json()
            await self.store.save()

    async def _mark_open(self, role: str) -> None:
        agent = self.store.agent(role, create=True)
        if agent.get("chat_open_at"):
            return
        agent["chat_open_at"] = _now_iso()
        await self.store.save()
        await self._edit(agent.get("offer_card"), f"{self._label(role)}'s chat is open.")

    def chat_opening(self, bot_id: int) -> bool:
        """Whether a turn delivered on *bot_id* now is the one that opens its
        agent's chat: the bot is its agent's and that chat has not opened yet.
        Read from the records, not :meth:`state`, which says ``no_bot`` while a
        binding releases what was held for it."""
        role = (self.store.bots.get(bot_id) or {}).get("role")
        return bool(role) and not (self.store.agents.get(role) or {}).get("chat_open_at")

    async def mark_open(self, bot_id: int) -> None:
        """The agent's queue took the turn that opened *bot_id*'s chat: the
        chat is open. Takes no lock: the endpoint calls it while a release
        may hold this bot's."""
        role = (self.store.bots.get(bot_id) or {}).get("role")
        if role:
            await self._mark_open(role)

    async def post_offer(self, role: str) -> dict:
        """Post a new offer for *role* (the previous card, if any, is marked
        replaced). ``offered_at`` and the suggestion are persisted before
        the card is posted."""
        label = self._label(role)
        try:
            me = await self.host.manager_me()
        except Exception as exc:  # noqa: BLE001
            logger.warning("own chat %s: no offer, the resident's bot is not "
                           "reachable (%s)", role, type(exc).__name__)
            return {"status": "error", "kind": "delivery_failed",
                    "message": "the resident's bot is not reachable right now"}
        async with self._registry_lock:
            taken = {
                (a.get("suggestion") or {}).get("username", "").lower()
                for r, a in self.store.agents.items()
                if r != role and a.get("bot_id") is None and a.get("suggestion")}
            name, username = suggest(
                resident_name=self._resident_name(),
                resident_first_name=me.get("first_name") or "",
                resident_username=me.get("username") or "",
                agent_name=label, role=role, taken=taken - {""})
            agent = self.store.agent(role, create=True)
            previous = agent.get("offer_card")
            agent.update(offered_at=_now_iso(), label=label,
                         suggestion={"name": name, "username": username})
            agent["offer_card"] = None
            await self.store.save()
        if previous is not None:
            await self._edit(previous, "Replaced by the offer below.")
        url = create_link(me.get("username") or "", name, username)
        addr = await self._post(
            f"{label} can get a separate chat with you. Tap below, check the name, then Create.",
            [(f"Create {label}'s chat", url)])
        if addr is None:
            return {"status": "error", "kind": "delivery_failed",
                    "message": "the offer could not be posted"}
        agent["offer_card"] = addr.to_json()
        await self.store.save()
        return {"status": "offered", "suggested_name": name, "suggested_username": username}

    # -- creation, match, ask ---------------------------------------------

    async def on_created(self, bot: dict, from_user_id: int) -> None:
        operator = self.host.operator_id()
        if operator is None or from_user_id != operator:
            logger.warning("own chats: a bot creation from someone other than "
                           "the operator was ignored")
            return
        try:
            bot_id = int(bot["id"])
        except (KeyError, TypeError, ValueError):
            logger.warning("own chats: a bot creation without a bot id was ignored")
            return
        async with self._lock(bot_id):
            if bot_id in self.store.bots:
                return  # a redelivered creation
            self.store.bots[bot_id] = {
                "phase": SEEN, "role": None,
                "username": str(bot.get("username") or ""),
                "name": str(bot.get("first_name") or ""),
                "ask": None, "setup_digest": None, "created_at": _now_iso(),
                "photo_set": False}
            await self.store.save()
            await self._step_seen(bot_id)

    async def on_updated(self, bot: dict, owner_id: int) -> None:
        operator = self.host.operator_id()
        if operator is None or owner_id != operator:
            logger.warning("own chats: a managed-bot change for another owner was ignored")
            return
        try:
            bot_id = int(bot["id"])
        except (KeyError, TypeError, ValueError):
            return
        async with self._lock(bot_id):
            rec = self.store.bots.get(bot_id)
            if rec is None or rec["phase"] not in (BOUND, DORMANT, RECOVERING):
                return  # unknown, released, or still being created
            rec["phase"] = RECOVERING
            await self.store.save()
            await self._step_recovering(bot_id)

    def _match(self, rec: dict) -> "str | None":
        hits = []
        for role in self._waiting_roles():
            sugg = (self.store.agents.get(role) or {}).get("suggestion") or {}
            if (sugg.get("username") and sugg.get("name") == rec["name"]
                    and sugg["username"].lower() == rec["username"].lower()):
                hits.append(role)
        return hits[0] if len(hits) == 1 else None

    async def _enter_binding(self, bot_id: int, role: str) -> bool:
        """Under the registry lock: one bot per agent, never rebound."""
        rec = self.store.bots[bot_id]
        agent = self.store.agent(role, create=True)
        if agent.get("bot_id") not in (None, bot_id) or rec.get("role") not in (None, role):
            return False
        rec.update(phase=BINDING, role=role)
        agent["bot_id"] = bot_id
        agent["label"] = self._label(role)
        await self.store.save()
        return True

    async def _match_or_ask(self, bot_id: int) -> None:
        rec = self.store.bots[bot_id]
        async with self._registry_lock:
            role = self._match(rec)
            bound = role is not None and await self._enter_binding(bot_id, role)
        if bound:
            await self._edit_ask(rec, f"@{rec['username']} is {self._label(role)}'s.")
            await self._step_binding(bot_id)
            return
        if self._manager_down:
            return  # the question goes out once the resident's bot is back
        ask = rec.get("ask")
        if ask is not None and ask["state"] == "posted" and ask["rid"] in self._live_asks:
            return  # answerable: posted by this process
        if ask is not None and ask.get("card") is not None:
            await self._edit(ask["card"], "Asked again below.")
        await self._post_ask(bot_id)

    async def _edit_ask(self, rec: dict, text: str,
                        buttons: "list[tuple[str, str]] | None" = None) -> None:
        ask = rec.get("ask")
        if ask is not None and ask.get("card") is not None:
            await self._edit(ask["card"], text, buttons)

    async def _post_ask(self, bot_id: int, exclude: tuple[str, ...] = ()) -> None:
        rec = self.store.bots[bot_id]
        candidates = [r for r in self._waiting_roles() if r not in exclude]
        rid = uuid.uuid4().hex
        rec["ask"] = {"state": "posting", "card": None, "rid": rid,
                      "candidates": candidates}
        await self.store.save()
        text = (f'A new bot @{rec["username"]} ("{rec["name"]}") was just created. '
                "Which agent is it for?")
        options = [self._label(r) for r in candidates] + [ASK_NONE]

        async def _answer(idx: "int | None") -> None:
            await self.on_ask_answer(bot_id, rid, idx)

        try:
            addr = await self.host.post_ask(rid, text, options, _answer)
        except Exception:  # noqa: BLE001
            logger.warning("own chat bot %s: the question could not be posted",
                           bot_id, exc_info=True)
            addr = None
        if addr is None:
            self._schedule_retry(bot_id, "the question was not posted")
            return
        rec["ask"] = {"state": "posted", "card": addr.to_json(), "rid": rid,
                      "candidates": candidates}
        self._live_asks.add(rid)
        await self.store.save()

    async def on_ask_answer(self, bot_id: int, rid: str, idx: "int | None") -> None:
        """The operator's answer to the question about a new bot; ``None``
        when the question ended without one (it is asked again)."""
        async with self._lock(bot_id):
            rec = self.store.bots.get(bot_id)
            ask = (rec or {}).get("ask") or {}
            if rec is None or rec["phase"] != UNBOUND or ask.get("rid") != rid:
                return
            self._live_asks.discard(rid)
            if idx is None:
                await self._post_ask(bot_id)
                return
            candidates = list(ask.get("candidates") or [])
            if idx == len(candidates):
                rec["phase"] = RELEASING
                await self.store.save()
                await self._step_releasing(bot_id)
                return
            if not 0 <= idx < len(candidates):
                return
            role = candidates[idx]
            async with self._registry_lock:
                ok = self._waiting(role) and await self._enter_binding(bot_id, role)
            if ok:
                await self._edit_ask(rec, f"@{rec['username']} is {self._label(role)}'s.")
                await self._step_binding(bot_id)
                return
            other = self.store.bot_of(role)
            other_name = f"@{other[1]['username']}" if other else "a bot"
            await self._edit_ask(rec, f"{self._label(role)} already has {other_name}.")
            await self._post_ask(bot_id, exclude=(role,))

    # -- self-setup -------------------------------------------------------

    def _avatar(self, role: str, cfg: Any) -> "bytes | None":
        pack = getattr(cfg, "persona_pack", None)
        path = getattr(pack, "avatar_path", None) if pack is not None else None
        if path is None:
            return None
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            if role not in self._avatar_told:
                self._avatar_told.add(role)
                logger.warning("own chat %s: persona avatar unreadable (%s); "
                               "the bot keeps no picture", role, type(exc).__name__)
            return None
        self._avatar_told.discard(role)
        return data

    async def _self_setup(self, bot_id: int, role: str) -> None:
        """Describe the bot, set its commands, restrict it to its owner and
        give it the persona's picture — each call idempotent, each failure
        logged and retried at the next trigger. Runs when the declared
        values' digest differs from the one last applied."""
        cfg = self._configs().get(role)
        own = getattr(cfg, "own_chat", None)
        if own is None:
            return
        rec = self.store.bots[bot_id]
        photo = self._avatar(role, cfg)
        digest = hashlib.sha256(json.dumps([
            own.description, own.short_description, list(COMMANDS), "owner-only",
            hashlib.sha256(photo).hexdigest() if photo is not None else None,
        ]).encode("utf-8")).hexdigest()
        if rec.get("setup_digest") == digest:
            return
        token = self._tokens.get(bot_id)
        if token is None:
            return
        ok = True
        calls = (
            ("description", lambda: self.host.set_description(token, own.description)),
            ("short description",
             lambda: self.host.set_short_description(token, own.short_description)),
            ("commands", lambda: self.host.set_commands(token, list(COMMANDS))),
            ("access", lambda: self.host.restrict_access(bot_id)),
        )
        for what, call in calls:
            try:
                await call()
            except Exception as exc:  # noqa: BLE001 — retried at the next trigger
                ok = False
                logger.warning("own chat %s: bot %s not set (%s)", role, what,
                               type(exc).__name__)
        if photo is not None:
            try:
                await self.host.set_photo(token, photo)
                rec["photo_set"] = True
            except Exception as exc:  # noqa: BLE001 — no picture, binding goes on
                logger.warning("own chat %s: the persona's picture was refused (%s); "
                               "the bot keeps no picture", role, type(exc).__name__)
        elif rec.get("photo_set"):
            try:
                await self.host.remove_photo(token)
                rec["photo_set"] = False
            except Exception as exc:  # noqa: BLE001
                logger.warning("own chat %s: old picture not removed (%s)", role,
                               type(exc).__name__)
        if ok:
            rec["setup_digest"] = digest
        await self.store.save()

    # -- endpoint, release, intake ----------------------------------------

    async def _start(self, bot_id: int, token: str, role: "str | None") -> bool:
        try:
            await self.host.start_endpoint(bot_id, token, role)
        except Exception:  # noqa: BLE001
            logger.warning("own chat bot %s: endpoint not started", bot_id, exc_info=True)
            return False
        return True

    def _is_operator_message(self, update: dict) -> bool:
        msg = update.get("message") if isinstance(update, dict) else None
        sender = (msg or {}).get("from") or {}
        return msg is not None and sender.get("id") == self.host.operator_id()

    async def _release_locked(self, bot_id: int) -> None:
        """Hand the bot's held updates, in order, to its endpoint's normal
        intake. A refused one keeps itself and every later one held. The
        endpoint's turn opens the chat (:meth:`mark_open`), never the release."""
        for update in await asyncio.to_thread(self.journal.pending, bot_id):
            try:
                outcome = await self.host.release_update(bot_id, update)
            except Exception:  # noqa: BLE001 — stays held for the next trigger
                logger.warning("own chat bot %s: a held update was not released",
                               bot_id, exc_info=True)
                return
            if outcome != "accepted":
                return
            await asyncio.to_thread(self.journal.mark_released, bot_id, update["update_id"])

    async def _dormant_replies(self, bot_id: int) -> None:
        for update in await asyncio.to_thread(self.journal.pending, bot_id):
            if self._is_operator_message(update):
                await self._notify(bot_id, self._dormant_text(bot_id))
        await asyncio.to_thread(self.journal.drop, bot_id)

    def _dormant_text(self, bot_id: int) -> str:
        role = self.store.bots[bot_id].get("role") or ""
        label = self._label(role)
        if role in self._configs():
            return f"{label} no longer has a separate chat — talk to {self._resident()}."
        return f"{label} isn't installed right now."

    async def _notify(self, bot_id: int, text: str) -> None:
        try:
            await self.host.notify(bot_id, text)
        except Exception:  # noqa: BLE001
            logger.warning("own chat bot %s: reply not sent", bot_id, exc_info=True)

    async def intake(self, bot_id: int, update: dict) -> str:
        """What the endpoint does with *update*: ``"deliver"`` it through
        its normal intake; ``"held"`` — it is journaled (or, for a released
        bot, ignored) and acknowledged; ``"dormant_reply"`` — the endpoint
        answers with the dormant line and does nothing else. An agent with no
        registered queue is never ``"deliver"``, so an endpoint whose delivery
        found no queue may ask again and gets ``"held"``."""
        async with self._lock(bot_id):
            rec = self.store.bots.get(bot_id)
            if rec is None or rec["phase"] in (RELEASING, RELEASED):
                return "held"
            if rec["phase"] == DORMANT:
                return "dormant_reply"   # the endpoint sends the dormant line
            role = rec.get("role")
            deliverable = (rec["phase"] == BOUND and bot_id in self._reconciled
                           and role is not None and self.declared(role)
                           and self.host.queue_registered(role))
            if not deliverable or self.journal.pending(bot_id):
                await asyncio.to_thread(self.journal.append, bot_id, update)
                if rec["phase"] in (SEEN, UNBOUND) and bot_id not in self._told_setup:
                    self._told_setup.add(bot_id)
                    await self._notify(bot_id, SETUP_REPLY)
                if deliverable:
                    await self._release_locked(bot_id)
                return "held"
            return "deliver"

    # -- the phases -------------------------------------------------------

    async def _reconcile_locked(self, bot_id: int) -> None:
        rec = self.store.bots.get(bot_id)
        if rec is None:
            return
        await getattr(self, BOOT_STEPS[rec["phase"]])(bot_id)

    async def _token_for(self, bot_id: int) -> "tuple[str | None, str]":
        token = self._tokens.get(bot_id)
        if token:
            return token, "ok"
        token, outcome = await self._fetch(bot_id)
        if token:
            self._tokens[bot_id] = token
        return token, outcome

    async def _step_seen(self, bot_id: int) -> None:
        token, outcome = await self._fetch(bot_id)
        if token is None:
            if outcome != "wait":
                self._schedule_retry(bot_id, "token not fetched")
            return
        self._tokens[bot_id] = token
        self.store.bots[bot_id]["phase"] = UNBOUND
        await self.store.save()
        await self._start(bot_id, token, None)
        await self._match_or_ask(bot_id)

    async def _step_unbound(self, bot_id: int) -> None:
        token, outcome = await self._token_for(bot_id)
        if token is not None:
            await self._start(bot_id, token, None)
        elif outcome != "wait":
            self._schedule_retry(bot_id, "token not fetched")
        await self._match_or_ask(bot_id)

    async def _step_binding(self, bot_id: int) -> None:
        rec = self.store.bots[bot_id]
        role = rec["role"]
        if not self.declared(role):
            rec["phase"] = DORMANT
            await self.store.save()
            await self._step_dormant(bot_id)
            return
        token, outcome = await self._token_for(bot_id)
        if token is None:
            if outcome == "deleted":
                rec["phase"] = RECOVERING
                await self.store.save()
                await self._step_recovering(bot_id)
            elif outcome != "wait":
                self._schedule_retry(bot_id, "token not fetched")
            return
        if not await self._store_token(role, token):
            self._schedule_retry(bot_id, "token not stored")
            return
        await self._self_setup(bot_id, role)
        if not await self._start(bot_id, token, role):
            self._schedule_retry(bot_id, "endpoint not started")
            return
        self._reconciled.add(bot_id)
        await self._release_locked(bot_id)
        await self._card_bound(role)
        rec["phase"] = BOUND
        await self.store.save()

    async def _step_bound(self, bot_id: int) -> None:
        rec = self.store.bots[bot_id]
        role = rec["role"]
        if not self.declared(role):
            await self._go_dormant(bot_id)
            return
        token = self._tokens.get(bot_id) or await self._load_token(role, bot_id)
        if token is None:
            token, outcome = await self._fetch(bot_id)
            if token is None:
                rec["phase"] = RECOVERING
                await self.store.save()
                if outcome == "deleted":
                    await self._step_recovering(bot_id)
                elif outcome != "wait":
                    self._schedule_retry(bot_id, "token not fetched")
                return
            if not await self._store_token(role, token):
                self._schedule_retry(bot_id, "token not stored")
        if await self.host.probe(token) == "unauthorized":
            rec["phase"] = RECOVERING
            await self.store.save()
            await self._step_recovering(bot_id)
            return
        self._tokens[bot_id] = token
        await self._start(bot_id, token, role)
        await self._self_setup(bot_id, role)
        self._reconciled.add(bot_id)
        await self._release_locked(bot_id)

    async def _go_dormant(self, bot_id: int) -> None:
        """Hold first (the endpoint keeps no agent), then ``dormant``."""
        self._reconciled.discard(bot_id)
        token = self._tokens.get(bot_id)
        if token is not None:
            await self._start(bot_id, token, None)
        self.store.bots[bot_id]["phase"] = DORMANT
        await self.store.save()
        await self._step_dormant(bot_id)

    async def _step_recovering(self, bot_id: int) -> None:
        rec = self.store.bots[bot_id]
        token, outcome = await self._fetch(bot_id)
        if outcome == "deleted":
            rec["phase"] = RELEASING
            await self.store.save()
            await self._step_releasing(bot_id)
            return
        if token is None:
            if outcome != "wait":
                self._schedule_retry(bot_id, "token not fetched")
            return
        if await self.host.probe(token) == "unauthorized":
            self._schedule_retry(bot_id, "the fetched token is not accepted")
            return
        role = rec["role"]
        if (await self._load_token(role, bot_id) != token
                and not await self._store_token(role, token)):
            self._schedule_retry(bot_id, "token not stored")
            return
        self._tokens[bot_id] = token
        if self.declared(role):
            await self._start(bot_id, token, role)
            rec["phase"] = BOUND
            await self.store.save()
            await self._self_setup(bot_id, role)
            self._reconciled.add(bot_id)
            await self._release_locked(bot_id)
        else:
            await self._start(bot_id, token, None)
            rec["phase"] = DORMANT
            await self.store.save()
            await self._dormant_replies(bot_id)

    async def _step_dormant(self, bot_id: int) -> None:
        rec = self.store.bots[bot_id]
        role = rec["role"]
        if self.declared(role):
            rec["phase"] = BINDING
            await self.store.save()
            await self._step_binding(bot_id)
            return
        self._reconciled.discard(bot_id)
        token = self._tokens.get(bot_id) or await self._load_token(role, bot_id)
        if token is None:
            token, outcome = await self._fetch(bot_id)
            if token is None:
                if outcome == "deleted":
                    rec["phase"] = RECOVERING
                    await self.store.save()
                    await self._step_recovering(bot_id)
                elif outcome != "wait":
                    self._schedule_retry(bot_id, "token not fetched")
                return
        if await self.host.probe(token) == "unauthorized":
            rec["phase"] = RECOVERING
            await self.store.save()
            await self._step_recovering(bot_id)
            return
        self._tokens[bot_id] = token
        await self._start(bot_id, token, None)
        await self._dormant_replies(bot_id)

    async def _step_releasing(self, bot_id: int) -> None:
        rec = self.store.bots[bot_id]
        try:
            await self.host.stop_endpoint(bot_id)
        except Exception:  # noqa: BLE001
            logger.warning("own chat bot %s: endpoint not stopped", bot_id, exc_info=True)
        await asyncio.to_thread(self.journal.drop, bot_id)
        self._tokens.pop(bot_id, None)
        self._reconciled.discard(bot_id)
        role = rec.get("role")
        if role is None:
            await self._edit_ask(rec, f"@{rec['username']} is not used. Delete it in BotFather.",
                                 [("Delete it in BotFather", BOTFATHER_DELETE)])
        else:
            agent = self.store.agent(role, create=True)
            if agent.get("bot_id") == bot_id:
                agent["bot_id"] = None
                agent["chat_open_at"] = None
            await self._edit(agent.get("offer_card"),
                             f"{self._label(role)}'s chat was deleted — ask "
                             f"{self._resident()} for a new one.")
        rec["phase"] = RELEASED
        await self.store.save()

    async def _step_released(self, bot_id: int) -> None:
        return

    async def _step_switching(self, bot_id: int) -> None:
        """A new bot waiting for the restart that moves its agent to it: its
        endpoint holds what it receives, and it is never bound here."""
        token, outcome = await self._token_for(bot_id)
        if token is not None:
            await self._start(bot_id, token, None)
        elif outcome != "wait":
            self._schedule_retry(bot_id, "token not fetched")

    # -- triggers ---------------------------------------------------------

    async def boot(self) -> None:
        """Boot order: the resident's bot first, then every bot's phase
        completed or rolled back — a bound or dormant bot against its role's
        committed declaration first, and an agent endpoint is built, started
        and delivers only once its own reconcile is done — then the one offer
        owed to an eligible agent with no record at all."""
        if self.host.operator_id() is None:
            return
        status = await self.host.manager_check()
        if status == "unauthorized":
            self._manager_unauthorized()
        for bot_id in sorted(self.store.bots):
            async with self._lock(bot_id):
                try:
                    await self._reconcile_locked(bot_id)
                except Exception:  # noqa: BLE001 — one bot never blocks the others
                    logger.exception("own chat bot %s: boot reconcile failed", bot_id)
                    self._schedule_retry(bot_id, "boot reconcile failed")
        for role in sorted(self._configs()):
            if self.declared(role) and role not in self.store.agents:
                await self.post_offer(role)

    async def reconcile_role(self, role: str) -> None:
        """An install, upgrade, rollback or uninstall committed for *role*:
        its bot follows the committed declaration."""
        found = self.store.bot_of(role)
        if found is None:
            return
        bot_id = found[0]
        async with self._lock(bot_id):
            rec = self.store.bots[bot_id]
            if rec["phase"] == BOUND and not self.declared(role):
                await self._go_dormant(bot_id)
            elif rec["phase"] == DORMANT and self.declared(role):
                rec["phase"] = BINDING
                await self.store.save()
                await self._step_binding(bot_id)
            elif rec["phase"] == BOUND:
                await self._self_setup(bot_id, role)

    async def on_install(self, role: str) -> None:
        await self.reconcile_role(role)
        if self.declared(role) and self.bot_id_for(role) is None:
            await self.post_offer(role)

    async def on_upgrade(self, role: str) -> None:
        await self.reconcile_role(role)
        if self.declared(role) and role not in self.store.agents:
            await self.post_offer(role)

    async def on_persona(self, role: str) -> None:
        found = self.store.bot_of(role)
        if found is None:
            return
        async with self._lock(found[0]):
            if self.store.bots[found[0]]["phase"] == BOUND:
                await self._self_setup(found[0], role)

    async def on_queue(self, role: str) -> None:
        found = self.store.bot_of(role)
        if found is None:
            return
        bot_id = found[0]
        async with self._lock(bot_id):
            rec = self.store.bots[bot_id]
            if rec["phase"] == BOUND and bot_id in self._reconciled:
                await self._release_locked(bot_id)

    async def offer(self, role: str) -> dict:
        """The resident's ``offer_own_chat``."""
        if not self.declared(role):
            return {"status": "error", "kind": "not_declared",
                    "message": f"{self._label(role)} does not have a chat of its own"}
        state = self.state(role)
        if state == "open":
            return {"status": "already_open", "link": self.link_for(role)}
        if state == "not_open":
            agent = self.store.agent(role, create=True)
            previous = agent.get("offer_card")
            text, buttons = self._bound_card(role)
            addr = await self._post(text, buttons)
            if addr is None:
                return {"status": "error", "kind": "delivery_failed",
                        "message": "the card could not be posted"}
            agent["offer_card"] = addr.to_json()
            await self.store.save()
            await self._edit(previous, "Replaced by the card below.")
            return {"status": "open_card_posted", "link": self.link_for(role)}
        if self.bot_id_for(role) is not None:
            return {"status": "error", "kind": "being_set_up",
                    "message": f"{self._label(role)}'s chat is being set up"}
        return await self.post_offer(role)

    def disclosure(self, role: str) -> "dict | None":
        found = self.store.bot_of(role)
        if found is None or not found[1].get("username"):
            return None
        return {"bot": f"@{found[1]['username']}", "delete_link": BOTFATHER_DELETE}


# ---------------------------------------------------------------------------
# The Telegram side
# ---------------------------------------------------------------------------

class TelegramHost:
    """The lifecycle's view of Telegram and of the rest of Casa: the
    resident's bot (Casa's endpoint), the agent endpoints, the cards, the
    installed agents and the bus."""

    # -- Casa -------------------------------------------------------------

    @staticmethod
    def _transport():
        from channels.telegram_transport import telegram_transport
        return telegram_transport()

    def _casa(self):
        transport = self._transport()
        if transport is None:
            raise RuntimeError("Telegram is not configured")
        return transport.casa_endpoint()

    def operator_id(self) -> "int | None":
        transport = self._transport()
        return transport.operator_user_id() if transport is not None else None

    def resident_role(self) -> str:
        try:
            return getattr(self._casa(), "default_agent", "") or "assistant"
        except RuntimeError:
            return "assistant"

    def resident_name(self) -> str:
        import tools
        cfg = (getattr(tools, "_agent_role_map", None) or {}).get(self.resident_role())
        return getattr(getattr(cfg, "character", None), "name", "") or ""

    def configs(self) -> dict:
        import tools
        registry = getattr(tools, "_specialist_registry", None)
        return dict(registry.all_configs()) if registry is not None else {}

    def queue_registered(self, role: str) -> bool:
        import tools
        bus = getattr(tools, "_bus", None)
        return bus is not None and role in getattr(bus, "queues", {})

    async def manager_me(self) -> dict:
        me = await self._casa().bot.get_me()
        return {"first_name": me.first_name or "", "username": me.username or ""}

    async def manager_check(self) -> str:
        try:
            await self._casa().bot.get_me()
        except Exception as exc:  # noqa: BLE001
            return "unauthorized" if is_unauthorized(exc) else "error"
        return "ok"

    async def fetch_token(self, bot_id: int) -> str:
        """The managed bot's token, from ``getManagedBotToken`` on the
        resident's bot.

        The call goes straight to the bot's request object rather than
        through ``Bot.get_managed_bot_token``: the Bot method logs every
        call's return value at DEBUG, and here that value is the token,
        which the log redaction cannot know yet. The request layer below it
        logs the request URL, never the response body. Failures raise the
        same ``TelegramError`` subclasses as the Bot method; the caller
        registers the token for redaction before anything else uses it."""
        from telegram.request import RequestData
        from telegram.request._requestparameter import RequestParameter
        bot = self._casa().bot
        return await bot.request.post(
            url=f"{bot.base_url}/getManagedBotToken",
            request_data=RequestData(
                parameters=[RequestParameter.from_input("user_id", int(bot_id))]))

    async def restrict_access(self, bot_id: int) -> None:
        await self._casa().bot.set_managed_bot_access_settings(
            user_id=bot_id, is_access_restricted=True)

    # -- one agent's bot --------------------------------------------------

    @staticmethod
    def _bot(token: str):
        from telegram import Bot
        return Bot(token)

    async def probe(self, token: str) -> str:
        try:
            async with self._bot(token) as bot:
                await bot.get_me()
        except Exception as exc:  # noqa: BLE001
            return "unauthorized" if is_unauthorized(exc) else "error"
        return "ok"

    async def set_description(self, token: str, text: str) -> None:
        async with self._bot(token) as bot:
            await bot.set_my_description(text)

    async def set_short_description(self, token: str, text: str) -> None:
        async with self._bot(token) as bot:
            await bot.set_my_short_description(text)

    async def set_commands(self, token: str, commands: list) -> None:
        from telegram import BotCommand
        async with self._bot(token) as bot:
            await bot.set_my_commands([BotCommand(c, d) for c, d in commands])

    async def set_photo(self, token: str, photo: bytes) -> None:
        from telegram import InputProfilePhotoStatic
        async with self._bot(token) as bot:
            await bot.set_my_profile_photo(InputProfilePhotoStatic(photo))

    async def remove_photo(self, token: str) -> None:
        async with self._bot(token) as bot:
            await bot.remove_my_profile_photo()

    # -- endpoints --------------------------------------------------------

    def endpoint(self, bot_id: int):
        from channels.tg_address import EndpointUnavailable
        transport = self._transport()
        if transport is None:
            return None
        try:
            return transport.endpoint(bot_id)
        except (EndpointUnavailable, AddressError):
            return None

    def make_endpoint(self, bot_id: int, token: str, role: "str | None"):
        """A new endpoint for an agent's bot, configured like Casa's own."""
        from channels.telegram import TelegramChannel
        casa = self._casa()
        ep = TelegramChannel(
            bot_token=token, chat_id=casa.chat_id, default_agent=role or "",
            bus=casa._bus, webhook_url=casa._webhook_url,
            rate_limiter=casa._rate_limiter, is_casa=False, known_bot_id=bot_id,
            webhook_secret_for=casa._webhook_secret_for)
        for attr in ("_engagement_registry", "_engagement_driver",
                     "_session_registry", "_finalize_cancel"):
            setattr(ep, attr, getattr(casa, attr, None))
        return ep

    async def start_endpoint(self, bot_id: int, token: str, role: "str | None") -> None:
        transport = self._transport()
        if transport is None:
            raise RuntimeError("Telegram is not configured")
        ep = self.endpoint(bot_id)
        if ep is not None and ep.bot_token == token:
            transport.set_endpoint_role(ep, role)
            return
        if ep is not None:
            await ep.stop()
            transport.remove_endpoint(ep)
        ep = self.make_endpoint(bot_id, token, role)
        transport.add_agent_endpoint(ep, bot_id, role)
        await ep.start()

    async def stop_endpoint(self, bot_id: int) -> None:
        ep = self.endpoint(bot_id)
        if ep is None:
            return
        await ep.stop()
        self._transport().remove_endpoint(ep)

    async def notify(self, bot_id: int, text: str) -> bool:
        ep = self.endpoint(bot_id)
        return bool(ep is not None and await ep.notify_operator(text))

    async def release_update(self, bot_id: int, update: dict) -> str:
        """The endpoint's normal intake of a held update: ``"accepted"`` once
        it is on its agent's queue, ``"no_target"`` otherwise."""
        ep = self.endpoint(bot_id)
        if ep is None:
            return "no_target"
        return await ep.release_update(update)

    # -- cards in the resident's chat (Casa's own cards) ------------------

    async def post_card(self, text: str, buttons: list[tuple[str, str]]) -> "TgAddress | None":
        from output_boundary import casa_text
        casa = self._casa()
        operator = self.operator_id()
        if operator is None:
            return None
        mid = await casa.post_dm_keyboard(
            chat_id=operator, request_id=uuid.uuid4().hex, text=casa_text(text),
            options=[label for label, _ in buttons], urls=[url for _, url in buttons])
        return TgAddress(casa.bot_id, operator, mid) if isinstance(mid, int) else None

    async def edit_card(self, addr: TgAddress, text: str,
                        buttons: list[tuple[str, str]]) -> bool:
        ep = self.endpoint(addr.bot_id)
        if ep is None:
            return False
        return await ep.edit_dm_message(
            addr.chat_id, addr.message_id, text,
            buttons=[(label, "noop") for label, _ in buttons] or None,
            urls=[url for _, url in buttons] or None)

    async def post_ask(self, rid: str, text: str, options: list[str],
                       on_answer: "Callable[[int | None], Any]") -> "TgAddress | None":
        from channels.tg_address import scope as tg_scope
        from output_boundary import casa_text
        from verdict_broker import BROKER

        casa = self._casa()
        operator = self.operator_id()
        if operator is None:
            return None
        bot_id = casa.bot_id
        scope = tg_scope("ask", bot_id, operator)
        req, _created = BROKER.register(
            namespace="resident_ask", scope=scope, request_id=rid,
            timeout_s=365 * 86400.0, detached=True, supersede=False,
            meta={"options": list(options), "bot_id": bot_id, "chat_id": operator,
                  "operator_id": operator, "kind": "own_chat_ask", "_scope": scope})

        async def _post():
            return await casa.post_dm_keyboard(
                chat_id=operator, request_id=rid, text=casa_text(text),
                options=list(options))

        def _finish_factory(message_id: int):
            async def _finish(outcome: dict) -> None:
                if outcome.get("outcome") == "answered":
                    await on_answer(outcome.get("option_index"))
                elif outcome.get("reason") != "casa_shutdown":
                    await on_answer(None)
            return _finish

        await BROKER.ensure_posted(req, _post, _finish_factory)
        mid = req.meta.get("message_id")
        return TgAddress(bot_id, operator, mid) if isinstance(mid, int) else None


# ---------------------------------------------------------------------------
# The process's lifecycle
# ---------------------------------------------------------------------------

_CURRENT: "Lifecycle | None" = None


def current() -> Lifecycle:
    """The process's lifecycle, created on first use over ``/data``."""
    global _CURRENT  # noqa: PLW0603
    if _CURRENT is None:
        _CURRENT = Lifecycle()
    return _CURRENT


def install(lifecycle: "Lifecycle | None") -> None:
    """Use *lifecycle* as the process's (``None`` forgets it)."""
    global _CURRENT  # noqa: PLW0603
    _CURRENT = lifecycle


async def close_current() -> None:
    """Graceful stop: cancel the process's pending retries and in-flight
    reconciles, before the endpoints stop, so none wakes to start or edit
    anything during the teardown. Nothing when no lifecycle was used."""
    if _CURRENT is not None:
        await _CURRENT.close()
