"""Builders for tests in which an agent other than the resident posts.

An agent's surfaces (a delivered capability slot, a run card, a Stop) post
only while its own chat is open, and only at the call's delivery
(``GrantIdentity.delivery``). Tests that exercise what such a post does —
not whether the agent may post — open the agent's chat with
:func:`open_own_chats` and give the call's identity a delivery with
:func:`with_delivery`.
"""
from __future__ import annotations

from dataclasses import replace

import own_voice
from channels.tg_address import TgAddress

RESIDENT = "assistant"


def open_own_chats(monkeypatch, *roles: str) -> set[str]:
    """Patch the capability rule's one question (``own_voice.require_open_chat``)
    so the resident's chat and each of *roles*' own chats are open; any other
    agent has no chat of its own. Returns the mutable set of open roles, so a
    test can close or open one mid-run."""
    open_roles = set(roles)

    def require_open_chat(role, *, label):
        if role == RESIDENT or role in open_roles:
            return None
        return own_voice.Refusal(
            "own_chat_missing", f"{label} has no chat of its own: return what you have as text.")

    monkeypatch.setattr(own_voice, "require_open_chat", require_open_chat)
    return open_roles


def delivery_for(bot_id: int, chat_id: int) -> TgAddress:
    """The delivery of a call whose posts go to *chat_id* through *bot_id*."""
    return TgAddress(int(bot_id), int(chat_id))


def with_delivery(identity):
    """*identity* with the delivery an ordinary (non-task) turn gets: its own
    chat, through its own bot. An identity that already names one is kept."""
    if identity.delivery is not None:
        return identity
    return replace(identity, delivery=delivery_for(identity.bot_id, identity.chat_id))
