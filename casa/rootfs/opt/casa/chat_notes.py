"""Casa's notes for the agent that owns a chat.

A body-free line about something that happened in a chat — a task the agent
ran for someone else, and how it ended — kept for the agent whose endpoint
owns that chat, and drained into its next turn. Keyed by the bot and the chat:
the operator's private chat has the same id on every bot. In memory, like the
result echo: a restart loses notes not yet drained.
"""
from __future__ import annotations

from typing import Any

LINE_MAX = 300          # one note
MAX_EVENTS = 64         # kept per chat, oldest dropped
MAX_LINES = 10          # drained into one turn, then a count of the rest
CLIP = "[…]"

_LEDGER = None


def _ledger():
    global _LEDGER
    if _LEDGER is None:
        import result_broker as rb
        _LEDGER = rb.PostLedger(max_events=MAX_EVENTS)
    return _LEDGER


def _positive_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _key(bot_id: int, chat_id: int) -> str:
    return f"{bot_id}:{chat_id}"


def record(bot_id: int, chat_id: int, line: str) -> None:
    """Keep *line* for the agent of the chat *chat_id* on the bot *bot_id*,
    clipped to ``LINE_MAX``; nothing for a bot or chat id that is not a
    positive int, or an empty line."""
    if not _positive_int(bot_id) or not _positive_int(chat_id):
        return
    text = str(line or "").strip()
    if not text:
        return
    if len(text) > LINE_MAX:
        text = text[:LINE_MAX - len(CLIP)] + CLIP
    _ledger().record(_key(bot_id, chat_id), text)


def drain(bot_id: int, chat_id: int) -> list[str]:
    """Read and clear the chat's notes, oldest first: at most ``MAX_LINES``,
    then ``…and N more.``."""
    if not _positive_int(bot_id) or not _positive_int(chat_id):
        return []
    events = _ledger().drain(_key(bot_id, chat_id))
    lines = [str(e) for e in events[:MAX_LINES]]
    if len(events) > MAX_LINES:
        lines.append(f"…and {len(events) - MAX_LINES} more.")
    return lines


def reset() -> None:
    """Drop every note."""
    global _LEDGER
    _LEDGER = None
