"""Which operation a plugin's callback answers.

A plugin's callback answers a sign-in that a capability delivered as an
operator link. The delivery records the agent that executes the operation and
where the link was delivered (:func:`note_link`); the callback ingress asks
:func:`operation_for` before composing the callback's turn. The latest
unexpired link of a plugin decides.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from channels.tg_address import TgAddress


def note_link(plugin: str, executor: str, delivery: "TgAddress", ttl_s: int) -> None:
    """A capability of *plugin* run by *executor* delivered an operator link
    at *delivery*; it answers a callback for *ttl_s* seconds."""
    raise NotImplementedError


def operation_for(plugin: str) -> "tuple[str, TgAddress] | None":
    """The executor and delivery of *plugin*'s latest unexpired link, else ``None``."""
    raise NotImplementedError
