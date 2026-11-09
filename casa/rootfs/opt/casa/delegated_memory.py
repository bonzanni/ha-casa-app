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
# memory on every path its own output takes there — its delegated answer and
# the delegation notice a resident's turn carries. The resident's own words
# relaying a result are captured as usual.
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
# v0.347.6 (operator decision 2026-11-04): a post that is his answer — a kind
# in ``result_broker.ANSWER_KINDS``, decided by the path that sent it — says
# so, since the neutral line rarely closed the errand; every other post keeps
# the neutral line.
# ---------------------------------------------------------------------------

POST_LINE = "{name} posted to your chat."
ANSWER_LINE = "{name} posted his answer to your chat."


def post_line(name: str, kind: str) -> str:
    """Casa's line for a post of *kind* by the specialist named *name*."""
    from result_broker import ANSWER_KINDS
    return (ANSWER_LINE if kind in ANSWER_KINDS else POST_LINE).format(name=name)


def capture_post_line(*, channel: str, chat_id, operator_chat, resident: str,
                      post) -> None:
    """Capture Casa's line for *post*, one message of which just landed in
    *chat_id*, when its poster keeps no memory and the chat is the
    configured *operator_chat*: :data:`ANSWER_LINE` when the post's kind is
    his answer's, else :data:`POST_LINE`. Detached, keyed by the post's
    identity (never its kind), so every page of it is one event. Mechanical
    facts only; never raises."""
    import logging
    try:
        role = getattr(post, "role", "") or ""
        if (not role or not str(operator_chat or "").strip()
                or str(chat_id) != str(operator_chat).strip()):
            return
        from channel_policy import writes_to_bank
        from memory import capture, eligibility, occurrence
        import tools as tools_mod
        text = post_line(tools_mod._display_name_for_role(role),
                         getattr(post, "kind", "") or "")
        rule = eligibility.admit(
            eligibility.Source.SPECIALIST_POST, text=text,
            channel_writes=writes_to_bank(channel),
            role_writes_memory=role_writes_memory(role))
        if rule is None:
            return
        item = capture.event(
            source_key=occurrence.post_key(
                channel, chat_id, getattr(post, "owner", ""),
                getattr(post, "tool_use_id", ""), getattr(post, "slot", "")),
            role=resident, channel=channel, speaker_kind="casa",
            origin_kind="system",
            text=text,
            eligibility=rule, origin_tier="private", speaker_id=role)
        capture.record_detached(item)
    except Exception:  # noqa: BLE001 — a memory line never fails a send
        logging.getLogger(__name__).exception(
            "memory: the line for a specialist's post was not captured")


# ---------------------------------------------------------------------------
# INV-MEM-043: a swipe-reply on such a specialist's post reaches his desk, not
# a resident turn, so no turn captured it, and memory kept only the line that
# his answer came. The operator's own words are his message like any other:
# captured once, as he typed them, and classified like any message of his.
# ---------------------------------------------------------------------------

def capture_desk_reply(*, channel, chat_id, user_id, user_name,
                       message_id, resident: str, desk_role: str,
                       text: str) -> None:
    """Capture the operator's own words *text*, sent as a reply on the desk
    of *desk_role* in *chat_id*, as his message: when that specialist keeps
    no memory and the chat is the channel's configured operator chat.
    Speaker ``operator``, origin ``operator``, no floor, the chat's
    *resident* as its role, keyed by the provider's message id. Detached,
    like Casa's post line: his words come from no run's transcript, so no
    run waits on the write. Never raises."""
    import logging
    try:
        operator_chat = str(getattr(channel, "chat_id", "") or "").strip()
        if not operator_chat or str(chat_id) != operator_chat:
            return
        name = str(getattr(channel, "name", "") or "")
        from channel_policy import writes_to_bank
        from memory import capture, eligibility, occurrence
        rule = eligibility.admit(
            eligibility.Source.DESK_REPLY, text=text,
            channel_writes=writes_to_bank(name),
            role_writes_memory=role_writes_memory(desk_role))
        if rule is None:
            return
        item = capture.event(
            source_key=(occurrence.provider_key(name, str(chat_id), str(message_id))
                        if message_id is not None else None),
            role=resident, channel=name, speaker_kind="operator",
            origin_kind="operator", text=text, eligibility=rule,
            origin_tier=None, speaker_id=str(user_id),
            speaker_name=user_name,
            provider_message_id=None if message_id is None else str(message_id))
        capture.record_detached(item)
    except Exception:  # noqa: BLE001 — memory never fails a desk turn
        logging.getLogger(__name__).exception(
            "memory: the operator's desk reply was not captured")


# ---------------------------------------------------------------------------
# INV-MEM-044: what the operator types to such a specialist's engagement — in
# its topic, or as a reply in their direct chat to one of its job's posts —
# reaches the specialist's turn, and that engagement captures nothing
# (INV-MEM-032), so their words were lost. They are the operator's message like any other:
# captured once, as they typed them, classified like any message of theirs.
# ---------------------------------------------------------------------------

def capture_topic_message(*, channel, engagement, user_id, user_name,
                          message_id, resident: str, text: str) -> None:
    """Capture the operator's own words *text*, sent to *engagement* (in its
    topic, or as a reply to one of its job's posts in their direct chat), as
    their message: when it is a specialist's engagement, that
    specialist keeps no memory and the sender is the channel's configured
    operator. Speaker ``operator``, origin ``operator``, no floor, the chat's
    *resident* as its role, keyed by their message (``engagement_key``,
    the key a memory-writing specialist's engagement gives it). Detached,
    like the desk reply: no run's transcript holds his words. Never raises."""
    import logging
    try:
        if (getattr(engagement, "kind", "") != "specialist"
                or not channel._user_id_is_operator(user_id)):
            return
        name = str(getattr(channel, "name", "") or "")
        from channel_policy import writes_to_bank
        from memory import capture, eligibility, occurrence
        rule = eligibility.admit(
            eligibility.Source.TOPIC_MESSAGE, text=text,
            channel_writes=writes_to_bank(name),
            role_writes_memory=role_writes_memory(engagement.role_or_type))
        if rule is None:
            return
        item = capture.event(
            source_key=(occurrence.engagement_key(name, engagement.id,
                                                  str(message_id))
                        if message_id is not None else None),
            role=resident, channel=name, speaker_kind="operator",
            origin_kind="operator", text=text, eligibility=rule,
            origin_tier=None, speaker_id=str(user_id),
            speaker_name=user_name,
            provider_message_id=None if message_id is None else str(message_id))
        capture.record_detached(item)
    except Exception:  # noqa: BLE001 — memory never fails a topic turn
        logging.getLogger(__name__).exception(
            "memory: the operator's topic message was not captured")
