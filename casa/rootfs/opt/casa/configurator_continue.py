"""Casa continues the configurator: one delegation on the resident's behalf,
on the configurator's lane — after an approval, or for an erase episode."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal

import approval_records

Outcome = Literal["started", "queued", "busy", "failed", "superseded"]
OnOutcome = "Callable[[str], Awaitable[Any]] | None"


def start(continuation: str, *, key: str,
          approval: "approval_records.ApprovalRecord | None" = None,
          asked: str = "", on_outcome: OnOutcome = None) -> Outcome:
    """Take the key's launch slot and spawn the one task that does the rest.
    Never awaits: a caller's cancellation cannot interrupt the launch.

    *key* is the approval's ``rid``, or ``erase:<question id>`` for an erase
    episode. Returns ``"started"`` (also for a duplicate start of an approval
    already launching), ``"queued"`` (an erase episode's follower, started when
    the live run ends) or ``"superseded"`` (the approval is no longer approved
    and current; nothing launched). *on_outcome*, when given, is spawned once
    with ``"started"`` when the run is registered, or ``"busy"``/``"failed"``
    when it was not."""
    raise NotImplementedError
