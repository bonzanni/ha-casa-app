"""The one constructor of every ``source_key`` (INV-MEM-026).

A ``source_key`` names the thing that occurred, never a position: nothing here
counts, and the short log correlation id is never an input. There are three
live forms.

* ``provider:<h>`` — an operator message that carries a provider message id
  (a Telegram DM, or a message in an engagement topic). ``h`` is the sha256 of
  the channel, the chat and the provider's message id, joined by NUL bytes and
  truncated to 128 bits, so the same message delivered twice collapses into
  one event.
* ``event:<id>`` — every other live item: an operator turn with no provider id
  (a scheduled, reminder or trigger turn), each resident or specialist
  message, a delegated request and answer, an engagement summary. ``id`` is a
  random 128-bit value minted once, when capture builds the item. The spool
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


def provider_key(channel: str, chat: str, provider_message_id: str) -> str:
    """The key of an operator message that carries a provider message id."""
    return f"{PROVIDER}:{_digest(channel, chat, provider_message_id)}"


def engagement_key(channel: str, engagement_id: str,
                   provider_message_id: str) -> str:
    """The key of an operator message in an engagement's topic: one key per
    topic message, whichever seam captures it."""
    return provider_key(channel, f"engagement:{engagement_id}",
                        provider_message_id)


def post_key(channel: str, chat: str, owner: str, tool_use_id: str,
             slot: str) -> str:
    """The key of Casa's line for one post Casa delivered for a specialist."""
    return f"{POST}:{_digest(channel, chat, owner, tool_use_id, slot)}"


def event_key() -> str:
    """A fresh key for one live emission, minted when its item is built."""
    return f"{EVENT}:{uuid.uuid4().hex}"
