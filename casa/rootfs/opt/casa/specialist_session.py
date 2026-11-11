"""What a specialist's session carries beyond its options.

A specialist's session — one delegated run, or an agent's own conversation in
its chat — gets three things its options builder does not give it:

* **The long-run compaction window.** A role whose requests are long
  conversations (``session.long_runs: true`` in its manifest) gets the
  CLI's auto-compaction window widened, so a long browsing run is not
  compacted at the default size.
* **Its pad** (:mod:`scratchpad`), keyed by the session: the pad tool, its
  grant and its prompt line. A stored-call turn runs Casa's one pinned call,
  so it has none. A long-run role keeps the pad's tool loaded up front.
* **Its run in the agent's log** (a specialist's or the configurator's).

:func:`session_env_and_pad` decides all three once, for one session; the
caller applies them to its options (:meth:`SessionExtras.apply`) and runs
``cleanup`` when the session's conversation ends.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable

import scratchpad

# The CLI's auto-compaction window for a role whose requests are long conversations.
LONG_RUN_ENV = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}


def long_runs(cfg: Any) -> bool:
    """A mechanical stand-in for "this role's requests are one long conversation"."""
    return getattr(getattr(cfg, "session", None), "long_runs", False) is True


@dataclasses.dataclass(frozen=True)
class PadHandle:
    """The pad a session attaches: its key (the session) and whether its tool
    is loaded up front."""
    key: str
    eager: bool


@dataclasses.dataclass(frozen=True)
class SessionExtras:
    """One session's extras: the env overrides for its options, its pad
    (``None`` when it has none), its run in the agent's log, and the cleanup
    that lets its pad go once nothing can resume the session."""
    env: dict
    pad: "PadHandle | None"
    cleanup: Callable[[], None]
    log: Any

    def apply(self, options: Any, *, task: "str | None" = None,
              words: "str | None" = None) -> Any:
        """*options* with the env overrides and the pad. A conversation's
        first attach passes its *task*, and the operator's *words* when the
        turn has them: they open a pad that does not exist yet as its brief."""
        if self.env:
            options = dataclasses.replace(options, env={**options.env, **self.env})
        if self.pad is not None:
            from plugin_bindings import BINDINGS
            brief = {"task": task, "words": words} if task is not None else {}
            options = scratchpad.attach(options, self.pad.key, eager=self.pad.eager, **brief)
            scratchpad.PADS.expire(BINDINGS.holds_session)
        return options


def release_pad(session_id: str) -> None:
    """The pad of *session_id* goes, unless a binding can still resume the
    session (a binding minted during the run names it from then on)."""
    from plugin_bindings import BINDINGS
    if not BINDINGS.holds_session(session_id):
        scratchpad.PADS.discard(session_id)


def session_env_and_pad(cfg: Any, origin: dict, *, run_id: str) -> SessionExtras:
    """The extras of the session *run_id* of the agent *cfg*, for a turn
    whose origin is *origin*. The agent-log run starts here."""
    import tools as tools_mod
    log = tools_mod._DelegatedRunLog(cfg)
    log.start()
    long = long_runs(cfg)
    pad = None if (origin or {}).get("stored_call") else PadHandle(run_id, eager=long)
    return SessionExtras(env=dict(LONG_RUN_ENV) if long else {}, pad=pad,
                         cleanup=lambda: release_pad(run_id), log=log)
