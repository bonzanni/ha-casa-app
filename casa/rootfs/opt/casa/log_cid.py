"""Correlation-id logging support (spec 5.2 §7).

Every inbound message gets an 8-char cid at ingress. The bus dispatcher
sets :data:`cid_var` from ``msg.context["cid"]`` before calling the
handler, and :func:`install_logging` installs a LogRecord factory that
tags every record with ``record.cid = cid_var.get()`` at creation time.
Records emitted outside any dispatch (startup, shutdown, sweepers) read
the default value ``"-"``.

``LOG_FORMAT=human`` switches the root handler to the human-readable
format; any other value (including unset) uses :class:`JsonFormatter`.
This is a 5.5 change — prior Casa versions defaulted to human.
"""

from __future__ import annotations

import codecs
import json
import logging
import os
import select
import sys
import time
import uuid
from contextvars import ContextVar
from typing import IO

from log_redact import RedactingFilter, redact, redact_extras

# Module-level constant — Python's stock LogRecord attributes. Anything
# else attached to a record (via logger.*("msg", extra={...}) or by a
# LogRecordFactory) is treated as a structured extra by both formatters
# below. Keep in sync if upstream Python adds attributes (3.12 added
# taskName).
STANDARD_LOGRECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "cid", "asctime", "taskName",
})


def _record_extras(record: logging.LogRecord) -> dict:
    """Return non-standard LogRecord attrs as a flat dict."""
    return {
        k: v for k, v in record.__dict__.items()
        if k not in STANDARD_LOGRECORD_ATTRS and not k.startswith("_")
    }


# ---------------------------------------------------------------------------
# Context var + cid helpers
# ---------------------------------------------------------------------------

cid_var: ContextVar[str] = ContextVar("cid", default="-")


def new_cid() -> str:
    """Return a fresh 8-char lowercase-hex correlation id."""
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Filter — injects cid_var.get() onto every record
# ---------------------------------------------------------------------------


class CidFilter(logging.Filter):
    """Inject ``record.cid`` from the current :data:`cid_var`.

    Standalone utility. :func:`install_logging` does NOT attach this to
    the root logger — it uses :func:`logging.setLogRecordFactory`
    instead, which tags records at creation (before handlers or
    filters run). ``CidFilter`` remains available for callers that
    construct records manually and want to re-inject the current cid.
    Always returns ``True`` — this filter only mutates, it never drops
    records.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.cid = str(cid_var.get())
        return True


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

_HUMAN_FORMAT = "%(asctime)s [%(levelname)s] %(name)s cid=%(cid)s: %(message)s"
_ISO_UTC_DATEFMT = "%Y-%m-%dT%H:%M:%SZ"


class _RedactingRenderMixin:
    """Redact exception/stack text at the point it becomes a string (#285).

    The :class:`~log_redact.RedactingFilter` runs before formatting and
    only sees ``record.msg``/``record.args`` — a secret inside an
    exception message or traceback would otherwise reach the log at any
    level. Redacting here, in the formatter, covers both formatters with
    one mechanism and keeps the filter's per-value arg walk (which must
    not see rendered ``key=value`` text) unchanged.
    """

    def formatException(self, ei) -> str:
        return redact(super().formatException(ei))

    def formatStack(self, stack_info: str) -> str:
        return redact(super().formatStack(stack_info))


class HumanFormatter(_RedactingRenderMixin, logging.Formatter):
    """Human-readable formatter that appends LogRecord extras as
    `key=val` suffix. Mirrors `JsonFormatter`'s extras-merging so a
    single `logger.info("evt", extra={...})` call renders coherently
    in both modes."""

    def format(self, record: logging.LogRecord) -> str:
        # A foreign formatter (another handler running first) may have
        # cached unredacted exception text on the record; the base class
        # reuses that cache instead of calling formatException, so scrub
        # it here. Idempotent — re-redacting redacted text is a no-op.
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        base = super().format(record)
        extras = redact_extras(_record_extras(record))
        if not extras:
            return base
        suffix = " ".join(f"{k}={v}" for k, v in extras.items())
        return f"{base} {suffix}"


def _human_formatter() -> HumanFormatter:
    """ISO-UTC human format: ``2026-04-18T14:32:01Z [INFO] name cid=X: msg [extras]``."""
    fmt = HumanFormatter(_HUMAN_FORMAT, datefmt=_ISO_UTC_DATEFMT)
    fmt.converter = time.gmtime
    return fmt


class JsonFormatter(_RedactingRenderMixin, logging.Formatter):
    """One-line JSON with fields ``ts, level, logger, cid, msg[, exc]``."""

    def __init__(self) -> None:
        super().__init__(datefmt=_ISO_UTC_DATEFMT)
        self.converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        payload: dict[str, object] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "cid": getattr(record, "cid", "-"),
            "msg": record.message,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            # A filter or a foreign formatter may have PRE-RENDERED (and
            # possibly redacted) the traceback onto ``record.exc_text`` and
            # cleared ``exc_info`` — the stdlib Formatter and HumanFormatter
            # both fall back to that cache, so JsonFormatter must too, or the
            # exc field vanishes entirely for such records (e.g. the
            # ``callback_http`` aiohttp.server redactor). Redact on the way
            # out for the same reason ``formatException`` does.
            payload["exc"] = redact(record.exc_text)
        # Flatten any extras (e.g. logger.info("evt", extra={"channel": "x"})).
        payload.update(redact_extras(_record_extras(record)))
        return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Log stream that survives a non-blocking pipe (#1384)
# ---------------------------------------------------------------------------


class WaitingStreamHandler(logging.StreamHandler):
    """A :class:`logging.StreamHandler` that waits out a full pipe.

    Casa's stdout and stderr are a pipe shared with every other process in
    the container, and a Node child (the Claude CLI, an MCP server) may set
    ``O_NONBLOCK`` on it — a flag of the shared open file, so it applies to
    Casa too. A long line then meets ``EAGAIN`` once the pipe is full, and the
    stock handler loses it to ``BlockingIOError``. This handler writes the
    encoded line straight to the stream's descriptor and, on ``EAGAIN``,
    waits until the pipe can take more — what a blocking pipe would do.
    A stream without a descriptor (``StringIO`` in tests), or one in an
    encoding other than UTF-8, takes the stock path.
    """

    def emit(self, record: logging.LogRecord) -> None:
        stream = self.stream
        try:
            fd = stream.fileno()
            encoding = codecs.lookup(stream.encoding).name
        except (AttributeError, LookupError, OSError, TypeError, ValueError):
            fd = encoding = None
        # Only plain UTF-8 (Casa's stdout) goes straight to the descriptor;
        # any other codec may carry state (a BOM) only the stream knows.
        if encoding != "utf-8":
            super().emit(record)
            return
        try:
            data = (self.format(record) + self.terminator).encode(
                "utf-8", getattr(stream, "errors", None) or "strict",
            )
            try:
                stream.flush()  # keep order with anything else written to it
            except BlockingIOError:
                pass
            view = memoryview(data)
            while view:
                try:
                    view = view[os.write(fd, view):]
                except BlockingIOError:
                    select.select([], [fd], [])
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
# Root-logger setup
# ---------------------------------------------------------------------------


def install_logging(
    *, stream: IO[str] | None = None, level: int = logging.INFO,
) -> None:
    """Idempotent root-logger setup.

    - Installs a LogRecord factory that tags every record with
      ``record.cid = cid_var.get()`` at creation. Works for records
      from any logger (Casa, httpx, caplog, …) because the factory
      runs inside ``Logger.makeRecord``, before any handler or filter.
    - Attaches exactly one Casa-owned :class:`WaitingStreamHandler`;
      repeated calls remove the previous Casa handler before adding
      the new one.
    - Attaches :class:`log_redact.RedactingFilter` to the new handler
      (not to the root logger — filters on the root logger do NOT run
      for records propagated from descendants).
    - Chooses the formatter from ``LOG_FORMAT`` env: ``json`` → JSON,
      anything else (incl. unset) → human.
    - Quiets the ``httpx`` logger to WARNING (Telegram polling emits a
      line every ~10 s on INFO — retained from the prior basicConfig
      block for behaviour parity).

    Safe to call from tests; the ``_casa_owned`` flag prevents
    duplication across repeated invocations.
    """
    root = logging.getLogger()

    # Remove only Casa-owned handlers so we don't disturb pytest's
    # caplog handler or user-configured handlers.
    for h in list(root.handlers):
        if getattr(h, "_casa_owned", False):
            root.removeHandler(h)

    # Install the factory wrapper exactly once. Repeated install_logging
    # calls must not double-wrap — a double-wrapped factory would
    # mutate record.cid twice (harmless) and risk unbounded nesting on
    # future refactors.
    current_factory = logging.getLogRecordFactory()
    if not getattr(current_factory, "_casa_owned", False):
        orig_factory = current_factory

        def _casa_record_factory(*args, **kwargs):
            record = orig_factory(*args, **kwargs)
            record.cid = str(cid_var.get())
            return record

        _casa_record_factory._casa_owned = True  # type: ignore[attr-defined]
        _casa_record_factory._wrapped = orig_factory  # type: ignore[attr-defined]
        logging.setLogRecordFactory(_casa_record_factory)

    handler = WaitingStreamHandler(stream if stream is not None else sys.stdout)
    handler._casa_owned = True  # type: ignore[attr-defined]
    handler.addFilter(RedactingFilter())
    if os.environ.get("LOG_FORMAT", "").strip().lower() == "human":
        handler.setFormatter(_human_formatter())
    else:
        handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("opentelemetry").setLevel(logging.WARNING)
    # markdown-it-py traces every block rule per line at DEBUG; one persona
    # reload is ~9k lines, enough to trip journald's rate limit on HA OS (#1421).
    logging.getLogger("markdown_it").setLevel(logging.INFO)
