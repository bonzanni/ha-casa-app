"""The Stop key at the keyboard: one red reply-keyboard key per piece of stoppable work.

A card's inline Stop scrolls up the chat with the card; a reply keyboard stays at the
operator's keyboard for as long as the work runs. Two kinds of work have a key, both
delivered to the operator's private chat with one bot:

* a background job the operator started, while its record is live and its start line is
  filed (``EngagementRegistry`` ``ui_posts``, kind ``start``) at that bot and chat;
* a browsing request, while its admission is ``admitted``, still holds its card at that
  bot and chat (``run_admission.Confirmation.card``; a stop or a natural end forgets it)
  and its binding still exists.

Nothing here is stored: the live set is read from those records each time a keyboard is
sent or a key is pressed. A reply keyboard belongs to one bot's chat, so everything is per
``(bot, chat)``. A key's text is its label, ``⏹ Stop «title»``; a press arrives as the
operator's own text message equal to it, and is matched by exact equality against the
labels of the live items. Live items with the same title are told apart by the time each
started (`` · 19:42``). A label is worked out from the records alone, so the keyboard a
chat was last sent names the same items after a restart. A message that has a key's own
form and names no live item is a key whose work is gone, whenever it was shown and
whether or not Casa restarted since: it is answered, and the keyboard it was pressed on
is replaced.

A Telegram message carries an inline keyboard or a reply keyboard, never both, and an edit
cannot raise or remove a reply keyboard: the keyboard rides on the messages Casa posts for
this purpose — the start message, and one short line when an item ends.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any

from channels.tg_address import TgAddress

logger = logging.getLogger(__name__)

LABEL = "⏹ Stop «{title}»"
TITLE_CHARS = 40
#: what every label starts with and, after its title, goes on with
_LABEL_HEAD, _LABEL_CLOSE = LABEL.split("{title}")
#: the line a press of a key whose work is gone gets (``plugin_stop``'s own wording)
ENDED_LINE = "It had already ended."


@dataclasses.dataclass(frozen=True)
class Item:
    """One piece of stoppable work in a chat: its kind (``job`` or ``run``), its id (the
    engagement id, or the admission's cid), its title, when it started (the order labels
    are given in) and the message that started it (``None`` while that is being sent)."""
    kind: str
    key: str
    title: str
    started: float
    start: "TgAddress | None" = None


def _clip(title: str) -> str:
    title = " ".join(str(title).split()) or "work"
    return title if len(title) <= TITLE_CHARS else title[:TITLE_CHARS - 1].rstrip() + "…"


def job_title(rec: Any) -> str:
    from channels.telegram import TelegramChannel
    return TelegramChannel._job_title(rec)


def job_item(rec: Any, start: "TgAddress | None" = None) -> Item:
    return Item("job", str(rec.id), job_title(rec), float(getattr(rec, "started_at", 0.0) or 0.0),
                start)


def run_item(c: Any) -> Item:
    """A browsing request's item, titled by what it does (``run_admission.key_title``,
    stored with the request)."""
    return Item("run", str(c.cid), str(getattr(c, "title", "") or "browsing"),
                float(getattr(c, "created_at", 0.0) or 0.0), c.card_address())


def _live_jobs(bot_id: int, chat_id: int) -> list[Item]:
    import tools as tools_mod
    from channels.telegram_transport import _post_parts
    from engagement_registry import JOB_POST_START
    reg = getattr(tools_mod, "_engagement_registry", None)
    if reg is None:
        return []
    out = []
    for rec in reg.active_and_idle():
        for post in getattr(rec, "ui_posts", None) or ():
            parts = _post_parts(post)
            if (parts is not None and parts[1] == JOB_POST_START
                    and parts[0].bot_id == bot_id and parts[0].chat_id == chat_id):
                out.append(job_item(rec, parts[0]))
                break
    return out


def _live_runs(bot_id: int, chat_id: int) -> list[Item]:
    import run_admission
    return [run_item(c) for c in run_admission.CONFIRMATIONS.open_cards(bot_id)
            if c.card_address().chat_id == chat_id]


def _clock(started: float, fmt: str) -> str:
    import datetime
    import timekeeping
    return datetime.datetime.fromtimestamp(started, timekeeping.resolve_tz()).strftime(fmt)


def _labelled(bot_id: int, chat_id: int, starting: "Item | None") -> list[tuple[str, Item]]:
    """The live items of the chat, oldest first, each with its label — *starting* among them
    when its own record does not yet show it. A title only one live item has is the whole
    label; items that share a title each add the time they started, to the second when the
    minute is shared too, and a count only for items started in the same second. Every part
    is read from the records, so the same live set always gives the same labels."""
    items = _live_jobs(bot_id, chat_id) + _live_runs(bot_id, chat_id)
    if starting is not None and not any((i.kind, i.key) == (starting.kind, starting.key)
                                        for i in items):
        items.append(starting)
    items.sort(key=lambda i: (i.started, i.kind, i.key))
    labels = {id(i): LABEL.format(title=_clip(i.title)) for i in items}
    for fmt in ("%H:%M", "%H:%M:%S"):
        shared = _shared(items, labels)
        for i in shared:
            labels[id(i)] = f"{LABEL.format(title=_clip(i.title))} · {_clock(i.started, fmt)}"
    seen: dict[str, int] = {}
    for i in _shared(items, labels):
        seen[labels[id(i)]] = n = seen.get(labels[id(i)], 0) + 1
        if n > 1:
            labels[id(i)] = f"{labels[id(i)]} ({n})"
    return [(labels[id(i)], i) for i in items]


def _shared(items: list[Item], labels: dict[int, str]) -> list[Item]:
    """The items whose label another item has too, in order."""
    count: dict[str, int] = {}
    for i in items:
        count[labels[id(i)]] = count.get(labels[id(i)], 0) + 1
    return [i for i in items if count[labels[id(i)]] > 1]


def keyboard(bot_id: int, chat_id: int, *, starting: "Item | None" = None) -> Any:
    """The reply keyboard for a message Casa is about to send in the chat: one red key per
    live item, one row each (*starting* included), or the keyboard's removal when none is
    live."""
    from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
    labels = [label for label, _ in _labelled(int(bot_id), int(chat_id), starting)]
    if not labels:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup([[KeyboardButton(text=label, style="danger")] for label in labels],
                               resize_keyboard=True, is_persistent=True)


def pressed(bot_id: int, chat_id: int, text: str) -> "tuple[str, Item | None] | None":
    """What a message *text* in the chat presses: ``("live", item)`` for the label of a live
    item, ``("ended", None)`` for any other text of a key's form (``⏹ Stop «…»…``) — a key
    whose work is gone — else ``None``: the message is not a key."""
    if not text:
        return None
    for label, item in _labelled(int(bot_id), int(chat_id), None):
        if label == text:
            return "live", item
    if text.startswith(_LABEL_HEAD) and _LABEL_CLOSE in text[len(_LABEL_HEAD):]:
        return "ended", None
    return None
