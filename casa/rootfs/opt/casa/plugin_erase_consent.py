"""#1046: the one question an uninstall asks when a plugin declares an eraser —
Keep data / Erase data, keep sign-ins / Erase everything / Cancel, offering
only the erase options the plugins declare (#1067) — posted by Casa on the
operator's DM through the same ChallengeCoordinator the install-consent
keyboards use.

The tap IS the authorization: an Erase tap records a single-use choice grant,
of the kind tapped, that ``plugin_remove`` / ``specialist_uninstall`` consume
before any eraser runs, so the model can never assert "erase" — or pick which
eraser — on the operator's behalf. Keep and Cancel record nothing (keeping
the data is today's removal). Every tap continues the configurator engagement
through the caller's callback."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from authz_grants import DEFAULT_GRANT_TTL_S

logger = logging.getLogger(__name__)

# Choice ids. ERASE is "Erase everything" (casa.eraseTool); ERASE_DATA_ONLY is
# "Erase data, keep sign-ins" (casa.eraseDataOnlyTool, #1067). A question
# offers a subset, in DEFAULT_ORDER; its buttons map index -> choice id.
KEEP, ERASE, CANCEL, ERASE_DATA_ONLY = 0, 1, 2, 3
LABELS = {KEEP: "Keep data", ERASE_DATA_ONLY: "Erase data, keep sign-ins",
          ERASE: "Erase everything", CANCEL: "Cancel"}
DEFAULT_ORDER = (KEEP, ERASE_DATA_ONLY, ERASE, CANCEL)
OPTIONS = [LABELS[c] for c in (KEEP, ERASE, CANCEL)]
CHOICE_TTL_S = DEFAULT_GRANT_TTL_S
# How long the question stays answerable.
QUESTION_TTL_S = 600


@dataclass(frozen=True)
class EraseChoiceKey:
    """One uninstall question: who may answer it, what it uninstalls
    (``plugin:<name>`` / ``specialist:<slug>``), and the artifacts of the
    erasing plugins at question time — a choice made for one version does
    not authorize erasing another."""
    operator_id: int
    chat_id: int
    subject: str
    artifacts: tuple
    question: str = ""        # plugin_erasure.QUESTIONS id of this question


class ChoiceGrants:
    """Single-use, TTL-bound Erase choices."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: dict[tuple[EraseChoiceKey, int], float] = {}

    def mint(self, key: EraseChoiceKey, choice: int) -> None:
        with self._lock:
            self._rows[(key, choice)] = self._clock() + CHOICE_TTL_S

    def consume(self, key: EraseChoiceKey, choice: int) -> bool:
        with self._lock:
            expires = self._rows.pop((key, choice), None)
        return expires is not None and self._clock() <= expires

    def consume_erase(self, key: EraseChoiceKey) -> "int | None":
        """The erase choice recorded for *key* (ERASE or ERASE_DATA_ONLY),
        consumed; ``None`` when there is none. A question is answered once,
        so at most one is ever recorded per key."""
        for choice in (ERASE, ERASE_DATA_ONLY):
            if self.consume(key, choice):
                return choice
        return None


CHOICES = ChoiceGrants()


_EXPLAIN = {
    KEEP: "Keep data: uninstall and keep the data (a reinstall picks it up again).",
    ERASE_DATA_ONLY: ("Erase data, keep sign-ins: erase the data first but keep "
                      "the sign-ins, so a reinstall carries on without signing "
                      "in again."),
    ERASE: ("Erase everything: erase all of it first, sign-ins included, and "
            "clear Casa's references to its secrets — as if it had never been "
            "installed."),
}


def render_erase_choice(what: str,
                        erasers: "list[tuple[str, str, str | None]]",
                        choices: "tuple[int, ...]" = (KEEP, ERASE, CANCEL),
                        ) -> str:
    """The question. *erasers* is ``(plugin, tool, summary)`` per eraser that
    one of the offered choices would run; the summary is the plugin's own
    protected-tool copy, if any. *choices* are the offered buttons."""
    lines = [f"\U0001F5D1 Uninstalling {what}", "",
             "Also erase its data first? Erasing runs the plugin's own eraser "
             "before anything is removed:"]
    for plugin, tool, summary in erasers:
        lines.append(f"• {plugin}: {tool}" + (f" — {summary}" if summary else ""))
    lines.append("")
    lines += [_EXPLAIN[c] for c in choices if c in _EXPLAIN]
    if not any(c in (ERASE, ERASE_DATA_ONLY) for c in choices):
        lines.append("No erase option is offered: the plugins being "
                     "uninstalled do not all declare the same kind of eraser.")
    else:
        lines.append("An erase goes ahead with the uninstall only if the "
                     "plugin reports the erasure complete.")
    lines.append("Home Assistant backups taken before now still contain the "
                 "data either way.")
    return "\n".join(lines)


_EDITS = {
    KEEP: "Keeping the data — the uninstall continues in the configurator topic.",
    ERASE: "Erase everything — the plugin's eraser runs first; the "
           "configurator topic reports what it says.",
    ERASE_DATA_ONLY: "Erase data, keep sign-ins — the plugin's eraser runs "
                     "first; the configurator topic reports what it says.",
    CANCEL: "Cancelled — nothing was removed.",
}


def prompt_erase_choice(
    *, coordinator: Any, channel: Any, key: EraseChoiceKey, text: str,
    continue_cb: "Callable[[int], Awaitable[bool]]",
    grants: "ChoiceGrants | None" = None,
    inbound_reservation: Any | None = None,
    choices: "tuple[int, ...]" = (KEEP, ERASE, CANCEL),
) -> Any:
    """Post the question with *choices* as its buttons, in order; every
    callback below receives the choice id, never the button index."""
    grants = grants if grants is not None else CHOICES
    choices = tuple(choices)

    def _choice(idx: Any) -> "int | None":
        return choices[idx] if isinstance(idx, int) and 0 <= idx < len(choices) \
            else None

    def _on_commit_sync(idx: int, meta: dict) -> None:
        # Runs in the Telegram callback right after the commit: the record
        # step. Only an Erase tap authorizes anything, and it records its kind.
        choice = _choice(idx)
        if choice in (ERASE, ERASE_DATA_ONLY):
            grants.mint(key, choice)
        if inbound_reservation is not None:
            inbound_reservation.take()

    def _finish_factory(message_id: int, req: Any) -> Callable[[dict], Any]:
        async def _finish(outcome: dict) -> None:
            try:
                await _finish_inner(outcome)
            finally:
                if inbound_reservation is not None:
                    inbound_reservation.release()

        async def _finish_inner(outcome: dict) -> None:
            o = outcome.get("outcome") if isinstance(outcome, dict) else None
            if o != "answered":
                await channel.edit_dm_message(
                    key.chat_id, message_id,
                    "This uninstall question is no longer open — nothing was "
                    "removed. Ask the configurator again to uninstall.")
                return
            idx = _choice(outcome.get("option_index"))
            if idx not in _EDITS:
                return
            continued: object = False
            try:
                continued = await continue_cb(idx)
            except Exception:  # noqa: BLE001 — never raise into the tap hook
                logger.exception("erase-choice continuation raised (%s)",
                                 key.subject)
            text_out = _EDITS[idx]
            if continued is not True:
                text_out += (" (The configurator was not resumed automatically — "
                             "ask it to continue.)")
            await channel.edit_dm_message(key.chat_id, message_id, text_out)

        return _finish

    return coordinator.register_challenge(
        key, chat_id=key.chat_id, operator_id=key.operator_id, channel=channel,
        challenge_text=text, options=[LABELS[c] for c in choices],
        on_commit_sync=_on_commit_sync, finish_factory=_finish_factory,
        kind="plugin_erase_choice", meta_extra={"subject": key.subject},
        timeout_s=QUESTION_TTL_S,
    )
