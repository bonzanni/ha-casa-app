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

That accusation is opt-in per module, and it only ever covered LITERAL callers.
:func:`casa_logging_containment` (#911) covers the rest: ``tests/conftest.py``
imports it, which makes it autouse for the whole suite, and it restores — without
accusing — whatever a test changed, including a test that reaches the installer
transitively through ``casa_core.main()``. The two nest, deliberately and
explicitly: the guard takes containment as a parameter, so the accusation is
computed and raised while containment is still open.

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
    """Exactly what ``install_logging`` mutates, and nothing else.

    ``handlers`` carries the ``_casa_owned`` root handlers as ``(index, handler)``
    pairs — by OBJECT IDENTITY and by position in ``logging.getLogger().handlers``.
    Identity, because a same-count replacement is a change that a count cannot
    see; position, because restoring a handler somewhere else changes the order
    records are emitted in. Never the whole handler list: pytest adds and removes
    its own ``LogCaptureHandler``/``_LiveLoggingNullHandler`` around every test,
    and this must not fight that lifecycle.
    """

    root_level: int
    factory: Any
    pinned: tuple[tuple[str, int], ...]
    handlers: tuple[tuple[int, logging.Handler], ...] = ()


def casa_handlers() -> list[logging.Handler]:
    """The ``_casa_owned`` handlers currently on the root logger."""
    return [
        h for h in logging.getLogger().handlers
        if getattr(h, "_casa_owned", False)
    ]


def snapshot() -> LoggingState:
    """The current value of every field ``install_logging`` writes."""
    root = logging.getLogger()
    return LoggingState(
        root_level=root.level,
        factory=logging.getLogRecordFactory(),
        pinned=tuple(
            (name, logging.getLogger(name).level) for name in PINNED_LOGGERS
        ),
        handlers=tuple(
            (i, h) for i, h in enumerate(root.handlers)
            if getattr(h, "_casa_owned", False)
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

    Every arm is a DIFFERENCE against ``before``, the handler arm included
    (#911). It used to be an absolute count, and that asymmetry is how a test
    that added nothing came to be told, in print, that it "left Casa logging
    residue": it had merely INHERITED a handler from a test on the same worker
    that reached ``install_logging`` through ``casa_core.main()``.
    """
    now = snapshot()
    left: list[str] = []
    was = [h for _, h in before.handlers]
    have = [h for _, h in now.handlers]
    added = [h for h in have if not any(h is b for b in was)]
    gone = [b for b in was if not any(b is h for h in have)]
    if added or gone:
        left.append(
            f"{len(added)} _casa_owned handler(s) added to and {len(gone)} "
            f"removed from the root logger since the snapshot (install_logging "
            f"adds one at log_cid.py:230 and removes it only on a later call). "
            f"Compared by identity, so a same-count replacement counts as both")
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
    """Put back exactly the state ``before`` describes — never a default.

    Both directions, because ``before`` is a snapshot and not an assumption that
    the world started clean: a ``_casa_owned`` handler that was NOT in the
    snapshot is removed, and one that WAS in it and has gone is reinstated at the
    position it held. Handlers and filters that are not ``_casa_owned`` are never
    touched; a suite-wide restorer that swept them would be deleting state that
    belongs to whoever installed it.
    """
    root = logging.getLogger()
    keep = [h for _, h in before.handlers]
    for h in casa_handlers():
        if not any(h is k for k in keep):
            root.removeHandler(h)
    for idx, h in before.handlers:
        if any(h is existing for existing in root.handlers):
            continue
        root.addHandler(h)                # locked append, and dedups
        if root.handlers and root.handlers[-1] is h and idx < len(root.handlers) - 1:
            # Position matters and ``addHandler`` can only append. Rebuild the
            # list and REBIND it in one assignment rather than mutating in
            # place: a concurrent ``callHandlers`` iterating the old list keeps
            # its own reference and cannot see a half-moved handler.
            ordered = [x for x in root.handlers if x is not h]
            ordered.insert(min(idx, len(ordered)), h)
            root.handlers = ordered
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
def casa_logging_containment() -> Iterator[LoggingState]:
    """Give back the process-global logging state this test changed (#911).

    Made autouse for the WHOLE suite by one import in ``tests/conftest.py``, and
    it is deliberately not the guard below: it establishes no distinctive state
    and it never asserts. Containment and accusation are separate jobs.

    It exists because the hazard is REACHABILITY, not a literal call.
    ``casa_core.main()``'s first statement is ``install_logging`` and there is no
    uninstall, so ``tests/test_ingress_identity_boot_check.py`` — which runs the
    real ``main()`` early and unconditionally, as a declared INV-HTTP-005
    binding — left a handler, a wrapped ``LogRecord`` factory, a root level and
    two pinned levels for whichever test the scheduler ran next on that worker.
    The AST scan in ``tests/test_logging_state_hygiene.py`` cannot see that: it
    enumerates ``install_logging(...)`` call nodes, and no scan decides
    reachability. Making the polluter import the guard is not the fix either —
    the guard asserts, so the polluter would simply go red at a new node id.

    Test-only, and it changes no production behaviour: ``install_logging`` still
    removes only its own handlers and still wraps the factory at most once, on
    the single boot call production makes.
    """
    with casa_logging_restored() as before:
        yield before


@pytest.fixture(autouse=True)
def casa_logging_guard(casa_logging_containment) -> Iterator[LoggingState]:
    """Fail any test in the importing module that leaves logging residue.

    Autouse applies only where this name is imported, which is the three
    modules that call ``install_logging``. It establishes a distinctive state
    first (see :data:`GUARD_ROOT_LEVEL`) so that an unrestored effect cannot
    hide behind an ambient value that happens to match, and it restores that
    state before asserting, so one leaking test fails alone instead of turning
    every later test in the file into a confusing failure.

    It takes :func:`casa_logging_containment` as a parameter, and that parameter
    is the whole point rather than a formality: two restorers of the same
    process-global state now sit side by side, and the guard MUST compute its
    residue and raise while containment is still open. Declaring the dependency
    makes that nesting a fact of pytest's fixture graph instead of an inherited
    conftest-before-module ordering — reversed, containment would restore the
    ambient state first and the guard would find a world already clean, retiring
    #898's detector in silence. Pinned by ``TestContainmentNestsOutsideTheGuard``.
    """
    ambient = snapshot()
    established = LoggingState(
        root_level=GUARD_ROOT_LEVEL,
        factory=ambient.factory,
        pinned=tuple(GUARD_PINNED_LEVELS.items()),
        handlers=ambient.handlers,
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
