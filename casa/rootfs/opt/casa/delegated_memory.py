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
