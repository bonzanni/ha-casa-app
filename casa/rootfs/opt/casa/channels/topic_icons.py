"""Locked role → Telegram custom_emoji_id map for forum-topic bubble icons.

Evidence: 2026-05-13 live dump of ``bot.get_forum_topic_icon_stickers()``
from the production box's bundled PTB (addon ``91d4d4c8_casa``). The "Topics"
set is curated by Telegram (~111 stickers) and rotates rarely; the IDs
below are stable. ``verify_against_telegram`` provides a boot-time
diagnostic that warns if any ID rotates out.

Bubble = role (set once at engagement open, never edited).
State (active/awaiting/completed/failed/cancelled) lives in the topic
title TEXT — see ``channels.state_emoji.compose_topic_title``.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Display column is documentation; only the ID is sent to Telegram.
ROLE_CUSTOM_EMOJI_ID: dict[str, str] = {
    # Executors
    "configurator":     "5357315181649076022",  # 📁 folder
    # Specialists
    "finance":          "5350452584119279096",  # 💰 money bag
}

# Used for any role not in ROLE_CUSTOM_EMOJI_ID.
DEFAULT_ROLE_ID: str = "5309832892262654231"  # 🤖 robot


def icon_id_for_role(role: str) -> str:
    """Return the Telegram custom_emoji_id for ``role``.

    Never returns None; unknown / empty roles fall back to
    ``DEFAULT_ROLE_ID``. Callers always pass this through verbatim as
    ``icon_custom_emoji_id`` to ``create_forum_topic``.
    """
    return ROLE_CUSTOM_EMOJI_ID.get(role, DEFAULT_ROLE_ID)


async def verify_against_telegram(bot: Any) -> None:
    """Boot-time diagnostic: warn if any of our hardcoded IDs has
    rotated out of Telegram's curated "Topics" set.

    Non-fatal. If the bot call fails for any reason (network, auth,
    PTB version skew), we log a single info line and return — the
    static map continues to work; we just don't get the heads-up.
    """
    try:
        stickers = await bot.get_forum_topic_icon_stickers()
    except Exception as exc:  # noqa: BLE001 — diagnostic only
        logger.info(
            "topic_icons: get_forum_topic_icon_stickers() failed (%s); "
            "skipping verification", exc,
        )
        return

    available_ids: set[str] = {
        s.custom_emoji_id for s in stickers
        if getattr(s, "custom_emoji_id", None)
    }
    our_ids: set[str] = set(ROLE_CUSTOM_EMOJI_ID.values()) | {DEFAULT_ROLE_ID}
    missing = our_ids - available_ids
    if missing:
        # Reverse-map for human-readable warning.
        for role, eid in ROLE_CUSTOM_EMOJI_ID.items():
            if eid in missing:
                logger.warning(
                    "topic_icons: role=%s custom_emoji_id=%s no longer in "
                    "Telegram's curated set; bubble will fall back to "
                    "default chrome", role, eid,
                )
        if DEFAULT_ROLE_ID in missing:
            logger.warning(
                "topic_icons: DEFAULT_ROLE_ID=%s no longer in Telegram's "
                "curated set", DEFAULT_ROLE_ID,
            )
    else:
        logger.info(
            "topic_icons: verified %d role IDs present in Telegram set",
            len(our_ids),
        )
