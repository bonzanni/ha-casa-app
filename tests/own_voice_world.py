"""A fake Telegram world for the own-chat lifecycle: the resident's managing
bot, the agents' bots, the cards in the resident's chat, the bus queues and a
fake ``op`` — kept across simulated restarts, while each process gets a new
host and a new :class:`own_voice_lifecycle.Lifecycle` over the same disk."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError

import own_voice_lifecycle as ovl
from channels.tg_address import TgAddress
from config import OwnChatConfig

OPERATOR = 1197017861
CASA_BOT = 7000000001
ALEX_BOT = 9115462040
FIDO_BOT = 9115462041


class Crash(BaseException):
    """The process dies here: nothing in the lifecycle catches it."""


def specialist(name: str, *, own: bool = True, avatar: "Path | None" = None,
               short: str = "Keeps the household's finances",
               description: str = "Your finances, in one chat.") -> Any:
    return SimpleNamespace(
        kind="specialist",
        character=SimpleNamespace(name=name),
        own_chat=OwnChatConfig(short_description=short, description=description) if own else None,
        persona_pack=SimpleNamespace(avatar_path=avatar))


@dataclass
class World:
    """Telegram's and 1Password's side of things: survives a restart."""
    manager_ok: bool = True
    manager_first_name: str = "EllenTest"
    manager_username: str = "HeyEllenTestBot"
    tokens: dict = field(default_factory=dict)        # bot_id -> current token
    deleted: set = field(default_factory=set)         # bots deleted in BotFather
    fetch_errors: dict = field(default_factory=dict)  # bot_id -> exception to raise
    cards: dict = field(default_factory=dict)         # message_id -> (text, buttons)
    next_mid: int = 100
    queues: set = field(default_factory=set)
    configs: dict = field(default_factory=dict)
    released: list = field(default_factory=list)      # (bot_id, update_id)
    openings: list = field(default_factory=list)      # each operator turn: did it open the chat
    notices: list = field(default_factory=list)       # (bot_id, text)
    setup: list = field(default_factory=list)         # (what, token, value)
    fetches: list = field(default_factory=list)
    effects: list = field(default_factory=list)
    crash_at: "int | None" = None
    ticks: int = 0
    op_items: dict = field(default_factory=dict)      # id -> {"title", "tags", "token"}
    op_argv: list = field(default_factory=list)
    op_create_timeout_once: bool = False
    fail_post: bool = False
    taken_handles: set = field(default_factory=set)   # usernames in use on Telegram
    probe_down: bool = False                          # t.me cannot be reached
    open: dict = field(default_factory=dict)          # bot_id -> what still waits on it
    restarts: int = 0                                 # app restarts requested
    restart_ok: bool = True

    def tick(self, what: str) -> None:
        self.effects.append(what)
        self.ticks += 1
        if self.crash_at is not None and self.ticks == self.crash_at:
            raise Crash(what)

    def card_text(self, addr: "TgAddress | list | None") -> "str | None":
        if addr is None:
            return None
        mid = addr.message_id if isinstance(addr, TgAddress) else addr[2]
        return self.cards[mid][0]


class FakeEndpoint:
    def __init__(self, bot_id: int, token: str, role: "str | None") -> None:
        self.bot_id, self.bot_token, self.default_agent = bot_id, token, role or ""
        self.running = True


class FakeHost:
    """One process's view of :class:`World`. Its endpoints act on an update
    the way the Telegram channel does: an operator's message is a turn, which
    is asked whether it opens the agent's chat when it is built, and marks the
    chat open once its agent's queue took it."""

    def __init__(self, world: World) -> None:
        self.w = world
        self.endpoints: dict[int, FakeEndpoint] = {}
        self.asks: dict[str, Any] = {}
        self.lifecycle: "ovl.Lifecycle | None" = None   # set by make_lifecycle

    def _operator_turn(self, update: dict) -> bool:
        """Whether *update* is a turn the channel builds for the operator: his
        text, or a file's caption (a captionless file starts no turn)."""
        msg = update.get("message") if isinstance(update, dict) else None
        if not msg or (msg.get("from") or {}).get("id") != self.operator_id():
            return False
        return bool(msg.get("text") or msg.get("caption"))

    def turn_opening(self, bot_id: int, update: dict) -> bool:
        """The channel building *update*'s turn: whether it opens the chat."""
        if not self._operator_turn(update):
            return False
        opening = self.lifecycle.chat_opening(bot_id)
        self.w.openings.append(opening)
        return opening

    async def turn_accepted(self, bot_id: int, opening: bool) -> None:
        """The agent's queue took the turn: one that opened the chat marks it."""
        if opening:
            await self.lifecycle.mark_open(bot_id)

    def operator_id(self):
        return OPERATOR

    def resident_role(self):
        return "assistant"

    def resident_name(self):
        return "Ellen"

    def configs(self):
        return dict(self.w.configs)

    def queue_registered(self, role):
        return role in self.w.queues

    async def manager_me(self):
        if not self.w.manager_ok:
            raise InvalidToken("Unauthorized")
        return {"first_name": self.w.manager_first_name, "username": self.w.manager_username}

    async def handle_taken(self, username):
        if self.w.probe_down:
            return None
        return username.lower() in {t.lower() for t in self.w.taken_handles}

    async def request_restart(self):
        self.w.restarts += 1
        return self.w.restart_ok

    def open_items(self, role, bot_id, durable_only):
        return list(self.w.open.get(bot_id, []))

    async def manager_check(self):
        return "ok" if self.w.manager_ok else "unauthorized"

    async def fetch_token(self, bot_id):
        self.w.fetches.append(bot_id)
        if not self.w.manager_ok:
            raise InvalidToken("Unauthorized")
        if bot_id in self.w.fetch_errors:
            raise self.w.fetch_errors[bot_id]
        if bot_id in self.w.deleted:
            raise Forbidden("Forbidden: user is deactivated")
        token = self.w.tokens[bot_id]
        self.w.tick(f"fetch:{bot_id}")
        return token

    async def probe(self, token):
        live = set(self.w.tokens.values())
        ok = token in live and not any(
            self.w.tokens.get(b) == token for b in self.w.deleted)
        return "ok" if ok else "unauthorized"

    async def restrict_access(self, bot_id):
        self.w.setup.append(("access", bot_id, True))
        self.w.tick("access")

    async def set_description(self, token, text):
        self.w.setup.append(("description", token, text))
        self.w.tick("description")

    async def set_short_description(self, token, text):
        self.w.setup.append(("short", token, text))
        self.w.tick("short")

    async def set_commands(self, token, commands):
        self.w.setup.append(("commands", token, tuple(commands)))
        self.w.tick("commands")

    async def set_photo(self, token, photo):
        self.w.setup.append(("photo", token, photo))
        self.w.tick("photo")

    async def remove_photo(self, token):
        self.w.setup.append(("remove_photo", token, None))
        self.w.tick("remove_photo")

    def endpoint(self, bot_id):
        return self.endpoints.get(bot_id)

    async def start_endpoint(self, bot_id, token, role):
        ep = self.endpoints.get(bot_id)
        if ep is not None and ep.bot_token == token:
            ep.default_agent = role or ""
            return
        self.endpoints[bot_id] = FakeEndpoint(bot_id, token, role)
        self.w.tick(f"start:{bot_id}")

    async def stop_endpoint(self, bot_id):
        ep = self.endpoints.pop(bot_id, None)
        if ep is not None:
            ep.running = False
            self.w.tick(f"stop:{bot_id}")

    async def notify(self, bot_id, text):
        self.w.notices.append((bot_id, text))
        return True

    async def release_update(self, bot_id, update):
        opening = self.turn_opening(bot_id, update)
        role = self.endpoints[bot_id].default_agent if bot_id in self.endpoints else ""
        if role not in self.w.queues:
            return "no_target"
        self.w.released.append((bot_id, update["update_id"]))
        self.w.tick(f"release:{update['update_id']}")
        await self.turn_accepted(bot_id, opening)
        return "accepted"

    def _new_card(self, text, buttons):
        mid = self.w.next_mid
        self.w.next_mid += 1
        self.w.cards[mid] = (text, list(buttons))
        return TgAddress(CASA_BOT, OPERATOR, mid)

    async def post_card(self, text, buttons):
        if self.w.fail_post or not self.w.manager_ok:
            return None
        addr = self._new_card(text, buttons)
        self.w.tick("post_card")
        return addr

    async def edit_card(self, addr, text, buttons):
        self.w.cards[addr.message_id] = (text, list(buttons))
        return True

    async def post_ask(self, rid, text, options, on_answer):
        if self.w.fail_post or not self.w.manager_ok:
            return None
        addr = self._new_card(text, [(o, f"ask:{rid}:{i}") for i, o in enumerate(options)])
        self.asks[rid] = (list(options), on_answer)
        self.w.tick("post_ask")
        return addr

    def latest_ask(self) -> tuple[str, list, Any]:
        rid = list(self.asks)[-1]
        options, on_answer = self.asks[rid]
        return rid, options, on_answer

    async def answer(self, label: str) -> None:
        _rid, options, on_answer = self.latest_ask()
        await on_answer(options.index(label))


class FakeOp:
    """An in-process ``op``: the item commands the token store uses."""

    def __init__(self, world: World) -> None:
        self.w = world

    def __call__(self, cmd, input=None, **_kw):
        assert cmd[0] == "op"
        args = cmd[1:]
        self.w.op_argv.append(list(args))
        ok = lambda out="": subprocess.CompletedProcess(cmd, 0, out, "")  # noqa: E731
        if args[:2] == ["item", "list"]:
            tags = args[args.index("--tags") + 1]
            rows = [{"id": i, "title": it["title"], "tags": it["tags"]}
                    for i, it in self.w.op_items.items() if tags in it["tags"]]
            return ok(json.dumps(rows))
        if args[:2] == ["item", "create"]:
            tpl = json.loads(input)
            item_id = f"item{len(self.w.op_items) + 1}"
            self.w.op_items[item_id] = {"title": tpl["title"], "tags": tpl["tags"],
                                        "token": tpl["fields"][0]["value"]}
            self.w.tick("op_create")
            if self.w.op_create_timeout_once:
                self.w.op_create_timeout_once = False
                raise subprocess.TimeoutExpired(cmd, 30)
            return ok(json.dumps({"id": item_id}))
        if args[:2] == ["item", "edit"]:
            item_id = args[2]
            if item_id not in self.w.op_items:
                return subprocess.CompletedProcess(cmd, 1, "", "not found")
            tpl = json.loads(input)
            self.w.op_items[item_id]["token"] = tpl["fields"][0]["value"]
            self.w.tick("op_edit")
            return ok("{}")
        if args[:2] == ["item", "get"]:
            item = self.w.op_items.get(args[2])
            if item is None:
                return subprocess.CompletedProcess(cmd, 1, "", "not found")
            return ok(item["token"] + "\n")
        raise AssertionError(f"unexpected op call {args}")


OP_ENV = {"ONEPASSWORD_DEFAULT_VAULT": "Casa", "OP_SERVICE_ACCOUNT_TOKEN": "ops_x"}


def make_lifecycle(world: World, root: Path, *, onepassword: bool = True,
                   counting: bool = True, channel: bool = True,
                   ) -> tuple[ovl.Lifecycle, FakeHost]:
    """A fresh process over *root*: a new host, a new lifecycle, the same world.

    With *channel*, an update the lifecycle's intake lets through is then the
    endpoint's turn, as on the Telegram channel (an intake that delivers has
    found the agent's queue registered, so the turn is taken); without it,
    ``intake`` is the lifecycle's alone."""
    host = FakeHost(world)
    tokens = ovl.TokenStore(root / "agent-voices" / "tokens",
                            env=OP_ENV if onepassword else {}, run=FakeOp(world))
    lc = ovl.Lifecycle(root=root, host=host, tokens=tokens, retry_delays=None)
    host.lifecycle = lc
    if channel:
        intake = lc.intake

        async def _intake(bot_id, update):
            decision = await intake(bot_id, update)
            if decision == "deliver":
                await host.turn_accepted(bot_id, host.turn_opening(bot_id, update))
            return decision
        lc.intake = _intake
    if counting:
        save = lc.store.save

        async def _save() -> None:
            await save()
            world.tick("save")
        lc.store.save = _save
        journal = lc.journal
        append, mark = journal.append, journal.mark_released

        def _append(bot_id, update):
            append(bot_id, update)
            world.tick("journal_append")

        def _mark(bot_id, update_id):
            mark(bot_id, update_id)
            world.tick("journal_release")
        journal.append, journal.mark_released = _append, _mark
    return lc, host


def update(uid: int, text: str = "hi", *, sender: int = OPERATOR) -> dict:
    return {"update_id": uid, "message": {
        "message_id": uid, "text": text, "chat": {"id": OPERATOR, "type": "private"},
        "from": {"id": sender, "is_bot": False}}}


def created(bot_id: int, name: str, username: str) -> dict:
    return {"id": bot_id, "first_name": name, "username": username, "is_bot": True}


__all__ = ["ALEX_BOT", "BadRequest", "CASA_BOT", "Crash", "FIDO_BOT", "FakeHost", "FakeOp",
           "Forbidden", "InvalidToken", "NetworkError", "OPERATOR", "OP_ENV", "World",
           "created", "make_lifecycle", "specialist", "update"]
