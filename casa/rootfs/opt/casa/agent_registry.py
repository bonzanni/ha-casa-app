"""Bidirectional name↔role map across residents and specialists.

Built once at boot from the loaded AgentConfig dicts and rebuilt on
casa_reload. Read-only at runtime. Used by:

- `agent.py` to render `<delegates>` / `<executors>` system-prompt blocks
  with display names (e.g. "Tina (role: butler)").
- Future code paths that need programmatic name resolution.

Internal addressing in tools and registries stays role-keyed; this
module is a rendering / utility layer only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from config import AgentConfig

logger = logging.getLogger(__name__)


Tier = Literal["resident", "specialist"]


@dataclass(frozen=True)
class KnownAgent:
    role: str
    name: str
    card: str
    tier: Tier


class AgentRegistry:
    """Read-only lookup. Build via :meth:`AgentRegistry.build`."""

    def __init__(
        self,
        *,
        role_to_known: dict[str, KnownAgent],
        name_lower_to_role: dict[str, str],
    ) -> None:
        self._role_to_known = role_to_known
        self._name_lower_to_role = name_lower_to_role

    # --- factory --------------------------------------------------------

    @classmethod
    def build(
        cls,
        *,
        residents: dict[str, AgentConfig],
        specialists: dict[str, AgentConfig],
    ) -> "AgentRegistry":
        role_to_known: dict[str, KnownAgent] = {}
        name_lower_to_role: dict[str, str] = {}

        def _register(role: str, cfg: AgentConfig, tier: Tier) -> None:
            name = (getattr(cfg.character, "name", "") or role).strip()
            card = getattr(cfg.character, "card", "") or ""
            role_to_known[role] = KnownAgent(
                role=role, name=name, card=card, tier=tier,
            )
            name_lower_to_role.setdefault(name.lower(), role)

        for role, cfg in residents.items():
            _register(role, cfg, "resident")
        for role, cfg in specialists.items():
            # #343: never let a specialist silently shadow a same-named
            # resident. Boot refuses the collision outright
            # (casa_core._build_role_registry raises); on a reload path
            # that reaches here anyway, resident wins — matching
            # tools.sync_agent_role_map — so every component agrees
            # about the role's tier.
            if role in role_to_known:
                logger.warning(
                    "AgentRegistry.build: role %r exists in both tiers — "
                    "resident entry wins", role,
                )
                continue
            _register(role, cfg, "specialist")

        return cls(
            role_to_known=role_to_known,
            name_lower_to_role=name_lower_to_role,
        )

    # --- queries --------------------------------------------------------

    def role_to_name(self, role: str) -> str:
        known = self._role_to_known.get(role)
        return known.name if known else role

    def name_to_role(self, name: str) -> str | None:
        return self._name_lower_to_role.get((name or "").strip().lower())

    def tier_for_role(self, role: str) -> str | None:
        """'resident' | 'specialist' for a known role, else None (unified
        plugin architecture — the resolver targets a tier:role)."""
        known = self._role_to_known.get(role)
        return known.tier if known is not None else None

    def is_known(self, role: str) -> bool:
        """True if ``role`` is a currently-known agent — a resident or an
        ENABLED specialist. A bundled-but-disabled (``enabled: false``) or
        removed specialist is absent here, so callers can use this to avoid
        advertising / acting on an agent that is not actually callable."""
        return role in self._role_to_known

    def all_known(self) -> list[KnownAgent]:
        return list(self._role_to_known.values())


def keeps_own_conversation(role: str) -> bool:
    """Whether the loaded specialist *role* keeps a conversation of its own:
    its committed component declares its own chat (``own_voice.declared``).
    Such an agent is on the bus as an ``Agent`` for its role, at boot and on
    every reload, so the operator's direct messages in its chat reach it.

    A declaration that cannot be read counts as none, logged: the agent then
    stays behind the resident until the next boot or reload reads it."""
    import own_voice
    try:
        return bool(own_voice.declared(role))
    except Exception:  # noqa: BLE001 — a declaration read never fails a caller
        logger.warning("own chat declaration of %s not read", role,
                       exc_info=True)
        return False


def own_conversation_roles(specialists: dict[str, AgentConfig]) -> list[str]:
    """The loaded specialists that keep a conversation of their own, in
    load order (:func:`keeps_own_conversation`)."""
    return [role for role, cfg in specialists.items()
            if getattr(cfg, "kind", "specialist") == "specialist"
            and keeps_own_conversation(role)]
