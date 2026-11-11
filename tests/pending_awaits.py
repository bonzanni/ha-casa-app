"""What a timed-out test was waiting on, for pytest-timeout's failure report.

A test that hangs on an ``await`` is a suspended coroutine: its frames are on no
thread's stack, so a thread dump shows only the event loop idling in ``select()``.
``pending_awaits`` follows each pending task's await chain instead (coroutine ->
the coroutine it awaits -> ...) and names the innermost line each one is parked on.
``wrap_timeout_handler`` appends that text to the timeout's failure message, which
pytest-xdist carries back from the worker like any other failure.
"""
from __future__ import annotations

import asyncio
import linecache
import signal

import pytest


def _await_chain(coro) -> list[str]:
    lines = []
    while coro is not None:
        frame = (getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None)
                 or getattr(coro, "ag_frame", None))
        if frame is None:
            break
        code = frame.f_code
        lines.append(f'  File "{code.co_filename}", line {frame.f_lineno}, in {code.co_name}')
        source = linecache.getline(code.co_filename, frame.f_lineno).strip()
        if source:
            lines.append(f"    {source}")
        coro = getattr(coro, "cr_await", None) or getattr(coro, "gi_yieldfrom", None)
    return lines


def pending_awaits() -> str:
    """Every task of the running event loop with the await chain it is parked on."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return "No event loop was running when the test timed out."
    blocks = [f"Pending task {task.get_name()}:\n" + "\n".join(_await_chain(task.get_coro()))
              for task in asyncio.all_tasks(loop)]
    return "\n\n".join(blocks) or "The event loop had no pending task."


def wrap_timeout_handler(settings) -> None:
    """Wrap the SIGALRM handler pytest-timeout has just installed (its ``signal``
    method) so the failure it raises also lists the pending awaits."""
    timed_out = signal.getsignal(signal.SIGALRM)
    if settings.method != "signal" or not callable(timed_out):
        return

    def report_pending_awaits(signum, frame):
        __tracebackhide__ = True
        try:
            timed_out(signum, frame)
        except pytest.fail.Exception as failed:
            message = failed.msg
        else:
            return      # pytest-timeout let the test run on (a debugger is attached)
        pytest.fail(f"{message}\n\n{pending_awaits()}")

    signal.signal(signal.SIGALRM, report_pending_awaits)
