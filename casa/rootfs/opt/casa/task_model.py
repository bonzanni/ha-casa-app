"""A task's destinations: where its posts go, and who receives its result.

A **task** is work that does not belong to an agent's conversation: a
delegation, a plugin job, or a plugin continuation. It has two destinations,
kept apart:

* its **delivery** — where every post of the task physically goes: its start
  line, its plugin posts, media, job lines, its answer, its run card, Stop.
  Decided once, when the task is born (:func:`delivery_for`), and never
  recomputed. ``None`` means the task posts nothing, for its whole life, even
  if its executor's chat opens while it runs;
* its **requester** — who receives the result (:class:`Requester`): the
  resident for work the resident started, or the executor's own conversation
  for work that conversation started.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from channels.tg_address import AddressError, EndpointNotReady, TgAddress, is_route
from provenance import ROUTING_KEYS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Requester:
    """Who receives a task's result: the agent *role*, on *route* — the route
    of the turn that started the task (``provenance.carry_route`` of its
    origin)."""
    role: str
    route: dict

    def to_json(self) -> dict:
        return {"role": self.role, "route": dict(self.route)}

    @classmethod
    def from_json(cls, value: Any) -> "Requester":
        """Strict: a role and a route holding only routing keys, each a valid route."""
        if not isinstance(value, dict) or set(value) != {"role", "route"}:
            raise ValueError(f"not a stored requester: {value!r}")
        role, route = value["role"], value["route"]
        if not isinstance(role, str) or not role or not isinstance(route, dict):
            raise ValueError(f"not a stored requester: {value!r}")
        if set(route) - set(ROUTING_KEYS) or not all(is_route(v) for v in route.values()):
            raise ValueError(f"not a stored requester route: {route!r}")
        return cls(role, dict(route))


def delivery_for(executor: str, requester_route: dict) -> "TgAddress | None":
    """The delivery of a task the agent *executor* runs, born now for a
    requester on *requester_route*:

    * the executor is the resident whose chat is Casa's own: that chat;
    * the executor declares its own chat and that chat is open at this moment:
      the executor's own endpoint and the operator's chat there;
    * anything else — an agent behind the resident, or an own chat not open
      now: ``None``.

    Called once, by the task's launcher, at the task's birth."""
    import own_voice
    from channels.telegram_transport import telegram_transport

    transport = telegram_transport()
    if transport is None:
        return None
    operator = transport.operator_user_id()
    if operator is None:
        return None
    try:
        casa = transport.casa_endpoint()
    except RuntimeError:
        return None
    if executor == getattr(casa, "default_agent", None):
        # the resident's chat is its own: Casa's endpoint, or the one the
        # requester's route names when that endpoint is the resident's too
        ep = casa
        try:
            routed = transport.endpoint_for_route(requester_route, site="task_delivery")
            if getattr(routed, "default_agent", None) == executor:
                ep = routed
        except (AddressError, RuntimeError):
            pass
        try:
            return TgAddress(ep.bot_id, operator)
        except EndpointNotReady:
            logger.warning("task for %s born before its bot had an id: it posts nothing",
                           executor)
            return None
    if own_voice.own_chat_state(executor) != "open":
        return None
    ep = own_voice.endpoint_for(executor)
    if ep is None:
        return None
    try:
        return TgAddress(ep.bot_id, operator)
    except (EndpointNotReady, AddressError):
        return None
