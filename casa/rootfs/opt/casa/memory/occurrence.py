"""The one constructor of every ``source_key`` (INV-MEM-026).

A ``source_key`` names the thing that occurred, never a position: nothing here
counts, and the short log correlation id is never an input. There are three
live forms.

* ``provider:<h>`` — an operator message that carries a provider message id
  (a Telegram DM, or the operator's reply into a job). ``h`` is the sha256 of
  the channel, the bot that received the message, the chat and the provider's
  message id, joined by NUL bytes and truncated to 128 bits, so the same
  message delivered twice collapses into one event. The bot is part of the
  key because Telegram numbers messages per bot, and two bots' private chats
  with the operator share the chat id: without it, one bot's message N would
  collapse into the other's.
* ``event:<id>`` — every other live item: an operator turn with no provider id
  (a scheduled, reminder or trigger turn), each resident or specialist
  message, a delegated request and answer. ``id`` is a random 128-bit value
  minted once, when capture builds the item. The spool
  line carries it, so a drain that is cut off and re-run adds the item once,
  while a regenerated answer or a retry's output is a new emission with a new
  id and is kept.
* ``post:<h>`` — Casa's line that a specialist which keeps no memory posted
  to the operator's chat (INV-MEM-040). ``h`` hashes the channel, the chat and
  the post's own identity (its owner, tool call and slot), so every page of
  one post, and the same post landing again, is one event.

No key has to match anything in a transcript.
"""
from __future__ import annotations

import hashlib
import uuid

PROVIDER = "provider"
EVENT = "event"
POST = "post"


def _digest(*parts) -> str:
    return hashlib.sha256("\0".join(str(p) for p in parts).encode(
        "utf-8")).hexdigest()[:32]


def _bot(bot: object) -> int:
    """The receiving bot's id. Only an int names a bot: a route value such as
    ``"casa"`` (an endpoint not yet verified) can never become part of a key."""
    if not isinstance(bot, int) or isinstance(bot, bool) or bot <= 0:
        # Imported only here: the memory package is also imported by the
        # container-start maintenance step, which loads no Casa channel code.
        from channels.tg_address import AddressError
        raise AddressError(f"not a Telegram bot id: {bot!r}")
    return bot


def message_key(channel: str, bot: int, chat, provider_message_id) -> str:
    """The key of an operator message that carries a provider message id,
    received by *bot* in *chat*."""
    return f"{PROVIDER}:{_digest(channel, _bot(bot), chat, provider_message_id)}"


def engagement_key(channel: str, bot: int, engagement_id: str,
                   provider_message_id) -> str:
    """The key of an operator message handed to an engagement, received by
    *bot*: one key per message, whichever seam captures it."""
    return message_key(channel, bot, f"engagement:{engagement_id}",
                       provider_message_id)


def post_key(channel: str, chat: str, owner: str, tool_use_id: str,
             slot: str) -> str:
    """The key of Casa's line for one post Casa delivered for a specialist."""
    return f"{POST}:{_digest(channel, chat, owner, tool_use_id, slot)}"


def event_key() -> str:
    """A fresh key for one live emission, minted when its item is built."""
    return f"{EVENT}:{uuid.uuid4().hex}"
