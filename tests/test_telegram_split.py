"""Tests for Telegram message splitting.

We import the splitting logic directly to avoid pulling in
python-telegram-bot (not installed locally).
"""

import importlib
import sys
import types
from unittest.mock import MagicMock

# Stub out the telegram package so channels.telegram can be imported
_telegram_stub = types.ModuleType("telegram")
_telegram_stub.Update = MagicMock()
_telegram_constants = types.ModuleType("telegram.constants")
_telegram_constants.ChatAction = MagicMock()
_telegram_stub.constants = _telegram_constants
_telegram_error = types.ModuleType("telegram.error")
_telegram_error.TelegramError = type("TelegramError", (Exception,), {})
_telegram_error.NetworkError = type("NetworkError", (Exception,), {})
_telegram_error.TimedOut = type("TimedOut", (Exception,), {})
_telegram_stub.error = _telegram_error
_telegram_ext = types.ModuleType("telegram.ext")
_telegram_ext.Application = MagicMock()
_telegram_ext.ContextTypes = MagicMock()
_telegram_ext.MessageHandler = MagicMock()
_telegram_ext.filters = MagicMock()
_telegram_stub.ext = _telegram_ext
sys.modules.setdefault("telegram", _telegram_stub)
sys.modules.setdefault("telegram.constants", _telegram_constants)
sys.modules.setdefault("telegram.error", _telegram_error)
sys.modules.setdefault("telegram.ext", _telegram_ext)

from channels.telegram import _split_message, _TG_MAX_LENGTH


class TestSplitMessage:
    def test_short_message_unchanged(self):
        result = _split_message("Hello world")
        assert result == ["Hello world"]

    def test_empty_message_yields_nothing_sendable(self):
        # #305 (Sol r1): the old fast path returned [""] and send() then
        # attempted an empty Bot API message (rejected). Whitespace-only
        # input — including "" — now yields no chunks at all.
        assert _split_message("") == []
        assert _split_message("\n\n  \n") == []

    def test_exact_limit(self):
        text = "a" * _TG_MAX_LENGTH
        result = _split_message(text)
        assert result == [text]

    def test_splits_at_newline(self):
        # Build a message that's slightly over the limit with a newline
        first_part = "x" * (_TG_MAX_LENGTH - 10)
        second_part = "y" * 20
        text = first_part + "\n" + second_part

        result = _split_message(text)
        assert len(result) == 2
        assert result[0] == first_part
        assert result[1] == second_part

    def test_hard_split_when_no_newline(self):
        text = "a" * (_TG_MAX_LENGTH + 100)
        result = _split_message(text)
        assert len(result) == 2
        assert len(result[0]) == _TG_MAX_LENGTH
        assert len(result[1]) == 100

    def test_multiple_splits(self):
        text = "a" * (_TG_MAX_LENGTH * 3)
        result = _split_message(text)
        assert len(result) == 3

    def test_preserves_content(self):
        lines = [f"Line {i}: " + "x" * 100 for i in range(100)]
        text = "\n".join(lines)
        result = _split_message(text)
        # #305: every split lands on a newline here, and the splitter consumes
        # EXACTLY the one split newline — so rejoining with "\n" reconstructs
        # the original byte-for-byte (stronger than the old "each line appears
        # somewhere" check, which passed even when blank lines were eaten).
        assert "\n".join(result) == text


class TestSplitMessageUtf16:
    """#305: Telegram's 4096 limit counts UTF-16 code units, not code points.
    Astral chars (most emoji) are 2 units; the splitter must measure what the
    platform measures or an under-4096-``len()`` chunk still gets rejected."""

    async def test_astral_heavy_message_splits_within_utf16_limit(self):
        from text_util import utf16_len
        text = "\U0001F389" * 3000  # len() 3000, but 6000 UTF-16 units
        result = _split_message(text)
        assert len(result) >= 2
        for chunk in result:
            assert utf16_len(chunk) <= _TG_MAX_LENGTH
        # No newlines involved: hard splits must reassemble exactly.
        assert "".join(result) == text

    async def test_hard_split_never_lands_inside_an_astral_pair(self):
        # 4095 ASCII then an astral char: the emoji (2 units) does not fit the
        # first chunk's remaining 1 unit — it must move whole to chunk 2.
        text = "a" * 4095 + "\U0001F389" + "b" * 10
        result = _split_message(text)
        assert result[0] == "a" * 4095
        assert result[1].startswith("\U0001F389")
        assert "".join(result) == text

    async def test_blank_lines_preserved_at_split_boundary(self):
        # "...\n\n\nImportant": the split consumes exactly ONE newline, so the
        # blank-line separation survives into the next chunk (the old
        # lstrip("\n") deleted it — content mutation, not partitioning).
        first = "x" * (_TG_MAX_LENGTH - 2)
        text = first + "\n\n\nImportant"
        result = _split_message(text)
        assert len(result) == 2
        assert "\n".join(result) == text

    async def test_whitespace_only_chunk_is_never_emitted(self):
        # A pure-newline tail would render as an empty Telegram message (the
        # API rejects it) — it is dropped, not sent.
        text = "a" * 4095 + "\n" + "\n" * 4
        result = _split_message(text)
        assert result == ["a" * 4095]


# ---------------------------------------------------------------------------
# Helpers + fixtures for _handle cid tests
# ---------------------------------------------------------------------------

import types as _types

import pytest

from bus import BusMessage, MessageBus
from channels.telegram import TelegramChannel


def _fake_update(text: str = "hello") -> object:
    user = _types.SimpleNamespace(first_name="User", id=1)
    message = _types.SimpleNamespace(text=text, message_id=42)
    chat = _types.SimpleNamespace(id="123")
    return _types.SimpleNamespace(
        message=message,
        effective_chat=chat,
        effective_user=user,
    )


async def _noop_handler(_msg: BusMessage) -> None:
    return None


class _FakeApp:
    class bot:
        @staticmethod
        async def send_chat_action(**kwargs):  # noqa: ARG004
            pass

        @staticmethod
        async def send_message(**kwargs):  # noqa: ARG004
            pass


@pytest.fixture
def telegram_channel():
    bus = MessageBus()
    bus.register("assistant", _noop_handler)
    channel = TelegramChannel(
        bot_token="T",
        chat_id="123",
        default_agent="assistant",
        bus=bus,
    )
    channel._start_typing = lambda *a, **k: None  # type: ignore[assignment]
    channel._app = _FakeApp()  # type: ignore[assignment]
    # Expose bus on the channel so tests can drain it.
    channel._bus = bus  # type: ignore[attr-defined]
    return channel


async def _invoke_handle(channel: TelegramChannel, text: str = "hello") -> None:
    await channel._handle(_fake_update(text), None)


async def _drain(channel) -> list[BusMessage]:
    q = channel._bus.queues.get("assistant")
    if q is None:
        return []
    out: list[BusMessage] = []
    while not q.empty():
        _p, _s, msg = q.get_nowait()
        out.append(msg)
        q.task_done()
    return out


# ---------------------------------------------------------------------------
# TestInheritOrAllocateCid — 5.5 §3.2.4a
# ---------------------------------------------------------------------------


class TestInheritOrAllocateCid:
    """_handle reuses a pre-bound cid (webhook mode via middleware) or
    allocates a fresh one (polling mode, no HTTP ingress)."""

    async def test_inherits_cid_when_var_is_bound(self, telegram_channel):
        from log_cid import cid_var

        token = cid_var.set("fedcba98")
        try:
            await _invoke_handle(telegram_channel, text="hello")
        finally:
            cid_var.reset(token)

        msgs = await _drain(telegram_channel)
        assert msgs, "expected at least one bus message"
        assert msgs[-1].context["cid"] == "fedcba98"

    async def test_allocates_cid_when_var_is_default(self, telegram_channel):
        # cid_var default is "-"
        await _invoke_handle(telegram_channel, text="hi")

        msgs = await _drain(telegram_channel)
        assert msgs, "expected at least one bus message"
        cid = msgs[-1].context["cid"]
        import re
        assert re.match(r"^[0-9a-f]{8}$", cid), cid
        assert cid != "-"
