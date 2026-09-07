"""Save, restore and audit the process-global logging state ``install_logging``
mutates — the shared half of the fix for #898.

``log_cid.install_logging`` is production code and its effects are
process-global: it adds a ``_casa_owned`` :class:`logging.StreamHandler` over
``stream or sys.stdout`` to the ROOT logger, sets the root level, wraps the
``LogRecord`` factory, and pins the ``httpx`` and ``opentelemetry`` loggers to
WARNING (``log_cid.py:223-234``). Its own self-cleaning arm (``:200-204``)
removes the previous handler only on a SUBSEQUENT call, so a test worker that
calls it once and never again keeps the handler for the rest of its life.

A test that calls it under ``capsys`` therefore leaves a handler bound to a
capture object pytest then closes. Every later record on that worker raises
``ValueError: I/O operation on closed file`` inside ``StreamHandler.emit``, and
because ``logging.raiseExceptions`` is true each one prints an error block plus
a full call stack onto whichever test is running at the time — measured to
inflate one test's report section from 27,616 B to 2,948,376 B, and on CI past
the 6 GiB per-worker address-space cap ``tests/conftest.py`` sets, aborting a
required check.

Three test modules call the installer (``tests/test_callback_http.py``,
``tests/test_log_cid.py``, ``tests/test_casa_access_logger.py``). Each imports
:func:`casa_logging_guard` from here, which makes the residue a FAILURE of the
test that left it; ``tests/test_logging_state_hygiene.py`` refuses a fourth
caller that does not.

Only ``_casa_owned`` markers and the named pinned loggers are inspected — never
the root handler list as a whole — so this cannot fight pytest's own
``LogCaptureHandler``/``_LiveLoggingNullHandler`` lifecycle, which adds and
removes handlers around every test.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

import pytest

#: The loggers ``install_logging`` pins to WARNING (``log_cid.py:233-234``).
PINNED_LOGGERS = ("httpx", "opentelemetry")

#: The state the guard establishes before each guarded test. Every value is
#: distinct from the one ``install_logging`` would leave (root DEBUG or INFO,
#: both pinned loggers WARNING) AND from the interpreter default (root WARNING,
#: pinned NOTSET), so a failure to restore any single effect is visible even
#: when the ambient value happens to coincide with the installed one.
GUARD_ROOT_LEVEL = logging.ERROR
GUARD_PINNED_LEVELS = {"httpx": logging.DEBUG, "opentelemetry": logging.CRITICAL}


@dataclass(frozen=True)
class LoggingState:
    """Exactly what ``install_logging`` mutates, and nothing else."""

    root_level: int
    factory: Any
    pinned: tuple[tuple[str, int], ...]


def casa_handlers() -> list[logging.Handler]:
    """The ``_casa_owned`` handlers currently on the root logger."""
    return [
        h for h in logging.getLogger().handlers
        if getattr(h, "_casa_owned", False)
    ]


def snapshot() -> LoggingState:
    """The current value of every field ``install_logging`` writes."""
    return LoggingState(
        root_level=logging.getLogger().level,
        factory=logging.getLogRecordFactory(),
        pinned=tuple(
            (name, logging.getLogger(name).level) for name in PINNED_LOGGERS
        ),
    )


def _apply(state: LoggingState) -> None:
    logging.getLogger().setLevel(state.root_level)
    logging.setLogRecordFactory(state.factory)
    for name, level in state.pinned:
        logging.getLogger(name).setLevel(level)


def residue(before: LoggingState) -> list[str]:
    """What a test has left behind, one human-readable line per difference.

    Empty means the process-global logging state is as ``before`` describes it.
    The factory is compared by IDENTITY, not by its ``_casa_owned`` flag: a
    wrapper that forgot to mark itself is still a wrapper.
    """
    now = snapshot()
    left: list[str] = []
    n = len(casa_handlers())
    if n:
        left.append(
            f"{n} _casa_owned handler(s) still on the root logger "
            f"(install_logging adds one at log_cid.py:230 and removes it only "
            f"on a later call)")
    if now.factory is not before.factory:
        left.append(
            f"the LogRecord factory is {now.factory!r}, not the "
            f"{before.factory!r} it was before the test")
    if now.root_level != before.root_level:
        left.append(
            f"the root logger level is {logging.getLevelName(now.root_level)}, "
            f"not the {logging.getLevelName(before.root_level)} it was before "
            f"the test")
    was = dict(before.pinned)
    for name, level in now.pinned:
        if level != was[name]:
            left.append(
                f"logger {name!r} is at {logging.getLevelName(level)}, not the "
                f"{logging.getLevelName(was[name])} it was before the test")
    return left


def restore(before: LoggingState) -> None:
    """Undo every effect ``install_logging`` has, back to ``before``."""
    root = logging.getLogger()
    for h in casa_handlers():
        root.removeHandler(h)
    for f in list(root.filters):          # belt, from the helper this replaces
        if getattr(f, "_casa_owned", False):
            root.removeFilter(f)
    _apply(before)


@contextmanager
def casa_logging_restored() -> Iterator[LoggingState]:
    """Run a block that calls ``install_logging`` and leave nothing behind."""
    before = snapshot()
    try:
        yield before
    finally:
        restore(before)


@pytest.fixture(autouse=True)
def casa_logging_guard() -> Iterator[LoggingState]:
    """Fail any test in the importing module that leaves logging residue.

    Autouse applies only where this name is imported, which is the three
    modules that call ``install_logging``. It establishes a distinctive state
    first (see :data:`GUARD_ROOT_LEVEL`) so that an unrestored effect cannot
    hide behind an ambient value that happens to match, and it restores that
    state before asserting, so one leaking test fails alone instead of turning
    every later test in the file into a confusing failure.
    """
    ambient = snapshot()
    established = LoggingState(
        root_level=GUARD_ROOT_LEVEL,
        factory=ambient.factory,
        pinned=tuple(GUARD_PINNED_LEVELS.items()),
    )
    _apply(established)
    try:
        yield established
    finally:
        left = residue(established)
        restore(ambient)
        assert not left, (
            "this test left Casa logging residue for the next test on its "
            "worker (#898): " + "; ".join(left))
