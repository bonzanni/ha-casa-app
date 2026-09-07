"""Unit tests for log_cid (spec 5.2 §7)."""

from __future__ import annotations

import json
import logging
import re
import sys
from io import StringIO

from log_cid import (
    CidFilter,
    HumanFormatter,
    JsonFormatter,
    cid_var,
    install_logging,
    new_cid,
)

# #898: install_logging mutates process-global logging state; the guard fails
# any test in this module that leaves that state altered for the next test on
# its xdist worker. Autouse applies only where the name is imported.
from logging_state import casa_logging_guard  # noqa: F401


# ---------------------------------------------------------------------------
# cid_var + new_cid
# ---------------------------------------------------------------------------


class TestCidVar:
    def test_default_is_dash(self):
        # A fresh context var must read as "-" so records emitted outside
        # any dispatch (startup, shutdown, sweepers) render as cid=-.
        assert cid_var.get() == "-"

    def test_set_and_get_roundtrip(self):
        token = cid_var.set("abcd1234")
        try:
            assert cid_var.get() == "abcd1234"
        finally:
            cid_var.reset(token)
        assert cid_var.get() == "-"


class TestNewCid:
    def test_is_8_hex_chars(self):
        for _ in range(50):
            cid = new_cid()
            assert re.fullmatch(r"[0-9a-f]{8}", cid), cid

    def test_is_unique_across_calls(self):
        cids = {new_cid() for _ in range(200)}
        # 200 samples from a 32-bit space — collision probability negligible.
        assert len(cids) == 200


# ---------------------------------------------------------------------------
# CidFilter
# ---------------------------------------------------------------------------


def _make_record(msg: str = "hi") -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__,
        lineno=1, msg=msg, args=(), exc_info=None,
    )


class TestCidFilter:
    def test_injects_current_cid(self):
        token = cid_var.set("deadbeef")
        try:
            rec = _make_record()
            f = CidFilter()
            assert f.filter(rec) is True
            assert rec.cid == "deadbeef"
        finally:
            cid_var.reset(token)

    def test_injects_dash_when_unset(self):
        rec = _make_record()
        f = CidFilter()
        f.filter(rec)
        assert rec.cid == "-"

    def test_filter_returns_true(self):
        # Filters that return False drop records; ours must always pass.
        assert CidFilter().filter(_make_record()) is True


# ---------------------------------------------------------------------------
# JsonFormatter
# ---------------------------------------------------------------------------


class TestJsonFormatter:
    def test_emits_valid_json(self):
        fmt = JsonFormatter()
        rec = _make_record("hello")
        rec.cid = "aaaa1111"
        line = fmt.format(rec)
        payload = json.loads(line)  # does not raise
        assert payload["msg"] == "hello"
        assert payload["cid"] == "aaaa1111"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "test"

    def test_timestamp_is_iso_utc_with_z(self):
        fmt = JsonFormatter()
        rec = _make_record()
        rec.cid = "-"
        payload = json.loads(fmt.format(rec))
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", payload["ts"],
        ), payload["ts"]

    def test_includes_exc_when_present(self):
        fmt = JsonFormatter()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            rec = logging.LogRecord(
                name="test", level=logging.ERROR, pathname=__file__,
                lineno=1, msg="oops", args=(), exc_info=sys.exc_info(),
            )
        rec.cid = "-"
        payload = json.loads(fmt.format(rec))
        assert "exc" in payload
        assert "RuntimeError: boom" in payload["exc"]

    def test_falls_back_to_exc_text_when_exc_info_cleared(self):
        """A filter (e.g. ``callback_http``'s aiohttp.server redactor) may
        PRE-RENDER a redacted traceback onto ``record.exc_text`` and clear
        ``exc_info``. The stdlib Formatter and HumanFormatter both reuse that
        cache; JsonFormatter must too, or the ``exc`` field vanishes entirely
        for such records instead of being redacted."""
        fmt = JsonFormatter()
        rec = logging.LogRecord(
            name="test", level=logging.ERROR, pathname=__file__,
            lineno=1, msg="oops", args=(), exc_info=None,
        )
        rec.cid = "-"
        rec.exc_text = "Traceback (most recent call last):\n  RuntimeError: x"
        payload = json.loads(fmt.format(rec))
        assert "exc" in payload
        assert "RuntimeError: x" in payload["exc"]

    def test_no_exc_field_without_exc_info_or_text(self):
        fmt = JsonFormatter()
        rec = _make_record("plain")
        rec.cid = "-"
        payload = json.loads(fmt.format(rec))
        assert "exc" not in payload

    def test_missing_cid_attr_defaults_to_dash(self):
        # If a record slips through without CidFilter, formatter must
        # still produce a valid line (defensive — spec §7.2 fallback).
        fmt = JsonFormatter()
        rec = _make_record("no-cid-attr")
        # Deliberately do not set rec.cid.
        payload = json.loads(fmt.format(rec))
        assert payload["cid"] == "-"


# ---------------------------------------------------------------------------
# install_logging
# ---------------------------------------------------------------------------


class TestInstallLogging:
    def _casa_handlers(self) -> list[logging.Handler]:
        return [
            h for h in logging.getLogger().handlers
            if getattr(h, "_casa_owned", False)
        ]

    def _cleanup_casa(self) -> None:
        """Restore the process-global logging state so one test cannot
        contaminate the next: drop Casa-owned handlers, restore the
        original LogRecord factory if install_logging wrapped it, and
        (belt-and-braces) strip any Casa-owned root filters left by
        earlier plan iterations."""
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, "_casa_owned", False):
                root.removeHandler(h)
        factory = logging.getLogRecordFactory()
        if getattr(factory, "_casa_owned", False):
            logging.setLogRecordFactory(factory._wrapped)
        for f in list(root.filters):
            if getattr(f, "_casa_owned", False):
                root.removeFilter(f)

    def test_human_format_default(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "human")
        buf = StringIO()
        install_logging(stream=buf)

        try:
            token = cid_var.set("1234abcd")
            try:
                logging.getLogger("unit").info("hello world")
            finally:
                cid_var.reset(token)
        finally:
            self._cleanup_casa()

        line = buf.getvalue().strip()
        # Spec §7.2 format: ts [LEVEL] name cid=XX: msg
        assert " cid=1234abcd:" in line
        assert "[INFO]" in line
        assert "hello world" in line
        assert re.search(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", line,
        )

    def test_json_format_env_toggle(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "json")
        buf = StringIO()
        install_logging(stream=buf)

        try:
            token = cid_var.set("beef5678")
            try:
                logging.getLogger("unit").info("payload")
            finally:
                cid_var.reset(token)
        finally:
            self._cleanup_casa()
            monkeypatch.delenv("LOG_FORMAT", raising=False)

        line = buf.getvalue().strip()
        payload = json.loads(line)
        assert payload["msg"] == "payload"
        assert payload["cid"] == "beef5678"
        assert payload["level"] == "INFO"

    def test_idempotent(self, monkeypatch):
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        buf = StringIO()
        try:
            install_logging(stream=buf)
            install_logging(stream=buf)
            install_logging(stream=buf)
            assert len(self._casa_handlers()) == 1
        finally:
            self._cleanup_casa()

    def test_redaction_still_applied(self, monkeypatch):
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        buf = StringIO()
        install_logging(stream=buf)
        try:
            logging.getLogger("unit").info(
                "Raw sk-abcdefghijklmnopqrstuvwxyz1234567890",
            )
        finally:
            self._cleanup_casa()

        line = buf.getvalue()
        assert "sk-abcdefghijklmnopqrst" in line
        assert "uvwxyz1234567890" not in line
        assert "***" in line  # redaction marker present, not just truncation

    def test_otel_logger_quieted_to_warning(self, monkeypatch):
        """Bug 7: install_logging must set the `opentelemetry` logger to
        WARNING so that the SDK's DEBUG-level 'OTEL trace context
        injection failed' traceback (logged inside
        claude_agent_sdk._internal.transport.subprocess_cli) is never
        emitted at Casa's prod DEBUG level (spec phase4 §5.2)."""
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        # Pre-state: ensure logger is at default level (or below WARNING).
        otel_logger = logging.getLogger("opentelemetry")
        otel_logger.setLevel(logging.NOTSET)

        try:
            install_logging(stream=StringIO())
            # The exact contract: a DEBUG log call on the OTEL logger
            # must NOT propagate to handlers because the logger's
            # effective level is WARNING.
            assert otel_logger.getEffectiveLevel() == logging.WARNING, (
                f"opentelemetry logger level not quieted: "
                f"got {logging.getLevelName(otel_logger.getEffectiveLevel())}"
            )
        finally:
            self._cleanup_casa()
            # Restore default for subsequent tests.
            otel_logger.setLevel(logging.NOTSET)


class TestFormatDefaultIsJson:
    """5.5 item 4 — LOG_FORMAT unset now means JSON."""

    def test_unset_env_yields_json_formatter(
        self, monkeypatch, capsys
    ):
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        install_logging(level=logging.INFO)
        logging.getLogger("casa.test").info("hello")
        line = capsys.readouterr().out.strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["level"] == "INFO"
        assert payload["msg"] == "hello"

    def test_log_format_human_yields_human_formatter(
        self, monkeypatch, capsys
    ):
        monkeypatch.setenv("LOG_FORMAT", "human")
        install_logging(level=logging.INFO)
        logging.getLogger("casa.test").info("hello")
        line = capsys.readouterr().out.strip().splitlines()[-1]
        # human format starts with an ISO timestamp and has bracketed level
        assert "[INFO]" in line
        assert "hello" in line


class TestExtrasFlatten:
    def _make_record(self, extras):
        rec = logging.LogRecord(
            name="t", level=logging.INFO, pathname=__file__,
            lineno=1, msg="evt", args=(), exc_info=None,
        )
        rec.cid = "ab12"
        for k, v in extras.items():
            setattr(rec, k, v)
        return rec

    def test_json_formatter_flattens_extras(self):
        from log_cid import JsonFormatter
        rec = self._make_record({"channel": "telegram", "winner": "house"})
        out = json.loads(JsonFormatter().format(rec))
        assert out["msg"] == "evt"
        assert out["channel"] == "telegram"
        assert out["winner"] == "house"

    def test_json_formatter_ignores_standard_attrs(self):
        from log_cid import JsonFormatter
        rec = self._make_record({})
        out = json.loads(JsonFormatter().format(rec))
        for k in ("pathname", "lineno", "funcName", "args", "module"):
            assert k not in out

    def test_human_formatter_appends_extras_as_key_val_suffix(self):
        from log_cid import HumanFormatter
        fmt = HumanFormatter(
            "%(asctime)s [%(levelname)s] %(name)s cid=%(cid)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        rec = self._make_record({"channel": "voice", "winner_score": 0.61})
        out = fmt.format(rec)
        assert "evt" in out
        assert "channel=voice" in out
        assert "winner_score=0.61" in out

    def test_human_formatter_no_extras_no_trailing_space(self):
        from log_cid import HumanFormatter
        fmt = HumanFormatter(
            "%(asctime)s [%(levelname)s] %(name)s cid=%(cid)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        rec = self._make_record({})
        out = fmt.format(rec)
        assert out.rstrip() == out
        assert out.endswith("evt")
