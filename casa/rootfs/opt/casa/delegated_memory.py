# casa/rootfs/opt/casa/delegated_memory.py
"""Which roles' output memory keeps out.

A specialist whose role reads no memory (``memory.token_budget: 0``) is kept
out of memory on every path its own output takes there; the capture seams ask
these two predicates (``memory/eligibility.py``).
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Browser executor §5.4 (v9.1, operator decision 2026-10-18): a specialist
# whose role reads no memory (``memory.token_budget: 0``) is kept out of
# memory on every path its own output takes there — its delegated answer, the
# delegation notice a resident's turn carries, and a specialist engagement's
# summary (``tools._finalize_engagement``). The resident's own words relaying
# a result are captured as usual.
# ---------------------------------------------------------------------------

# Casa's own notice header, every branch of ``Agent._synthesize_delegation_turn``
# (read AFTER the time envelope is split off). The role id is a token without
# spaces; ``(id `` follows it on every branch.
_NOTICE_ROLE_RE = re.compile(
    r"^\[System notification: (?:your delegation to|the launch turn of your "
    r"engagement with) (?P<role>\S+) \(id ")


def role_writes_memory(role: str) -> bool:
    """Whether output authored by *role* may be kept in memory.

    ``False`` for a loaded SPECIALIST whose ``memory.token_budget`` is 0, and
    for a role not loaded at all (its switch cannot be read, so it is kept
    out). A loaded resident or executor writes (``True``)."""
    import tools as tools_mod
    roles = getattr(tools_mod, "_agent_role_map", None) or {}
    cfg = roles.get(role)
    if cfg is None:
        # a role Casa cannot see declares nothing it can read: it writes
        # nothing (the safe direction — p3r2a, p3r3c); an empty map is a
        # Casa whose tools were never initialised, where nothing is gated
        return not roles
    if getattr(cfg, "kind", "") != "specialist":
        return True
    budget = getattr(getattr(cfg, "memory", None), "token_budget", None)
    return not (isinstance(budget, int) and budget <= 0)


def notice_from_excluded_role(text: str) -> bool:
    """Is *text* (a user turn, envelope removed) a delegation or engagement
    notice whose result was written by a role that writes no memory — or by a
    role no longer loaded, whose switch Casa cannot read? The notice embeds
    that role's own answer, so capturing it would keep the output the role's
    switch keeps out."""
    m = _NOTICE_ROLE_RE.match((text or "").lstrip())
    return m is not None and not role_writes_memory(m.group("role"))


# ---------------------------------------------------------------------------
# INV-MEM-040 (operator ruling 2026-11-03, option a): what such a specialist
# posts to the operator's chat stays out of memory, so the writer saw a request
# handed to it and then nothing, and kept the errand open. Casa keeps one line
# of its own per post instead: that the specialist posted, never what.
# ---------------------------------------------------------------------------

POST_LINE = "{name} posted to your chat."


def capture_post_line(*, channel: str, chat_id, operator_chat, resident: str,
                      post) -> None:
    """Capture Casa's line for *post*, one message of which just landed in
    *chat_id*, when its poster keeps no memory and the chat is the
    configured *operator_chat*. Detached, keyed by the post's identity, so
    every page of it is one event. Mechanical facts only; never raises."""
    import logging
    try:
        role = getattr(post, "role", "") or ""
        if (not role or not str(operator_chat or "").strip()
                or str(chat_id) != str(operator_chat).strip()):
            return
        from channel_policy import writes_to_bank
        from memory import capture, eligibility, occurrence
        rule = eligibility.admit(
            eligibility.Source.SPECIALIST_POST, text=POST_LINE,
            channel_writes=writes_to_bank(channel),
            role_writes_memory=role_writes_memory(role))
        if rule is None:
            return
        import tools as tools_mod
        item = capture.event(
            source_key=occurrence.post_key(
                channel, chat_id, getattr(post, "owner", ""),
                getattr(post, "tool_use_id", ""), getattr(post, "slot", "")),
            role=resident, channel=channel, speaker_kind="casa",
            origin_kind="system",
            text=POST_LINE.format(name=tools_mod._display_name_for_role(role)),
            eligibility=rule, origin_tier="private", speaker_id=role)
        capture.record_detached(item)
    except Exception:  # noqa: BLE001 — a memory line never fails a send
        logging.getLogger(__name__).exception(
            "memory: the line for a specialist's post was not captured")
