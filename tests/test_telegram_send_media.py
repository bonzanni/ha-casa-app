"""TelegramChannel.send_media — per-kind positional dispatch (v0.73.0, spec §3.1)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from output_boundary_testing import admitted

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _mk_channel():
    from channels.telegram import TelegramChannel

    fake_bot = MagicMock()
    for m in ("send_document", "send_photo", "send_audio", "send_voice"):
        setattr(fake_bot, m, AsyncMock(return_value=MagicMock(message_id=7)))
    fake_app = MagicMock()
    fake_app.bot = fake_bot
    ch = TelegramChannel(bot_token="x:y", chat_id=100, default_agent="assistant")
    ch._app = fake_app
    ch._stop_typing = MagicMock()          # avoid touching the typing machinery
    return ch, fake_bot


# #482/#565: several KINDS share one method. Keyed by kind, not by method, so
# the matrix keeps covering every kind the table declares. A named constant
# rather than an inline literal: the coverage guard below consumes THIS, and
# reaching into the decorator's metadata instead would silently inspect the
# wrong mark the day a second one is added to the test.
_DISPATCH_MATRIX = [
    ("document", "send_document"), ("photo", "send_photo"),
    ("audio", "send_audio"), ("voice", "send_voice"),
    ("zip", "send_document"), ("text", "send_document"),
]


@pytest.mark.parametrize("kind,method", _DISPATCH_MATRIX)
async def test_send_media_dispatches_per_kind(kind, method):
    ch, bot = _mk_channel()
    await ch.send_media(b"BYTES", kind, "f.ext", {"chat_id": 555}, caption=admitted("hi"))
    target = getattr(bot, method)
    target.assert_awaited_once()
    for other in ("send_document", "send_photo", "send_audio", "send_voice"):
        if other != method:
            getattr(bot, other).assert_not_awaited()
    args, kwargs = target.await_args
    assert args[0] == 555                     # resolved chat id (1st positional)
    inp = args[1]                             # InputFile (2nd positional)
    assert inp.data == b"BYTES"
    assert inp.filename == "f.ext"
    assert kwargs["caption"] == "hi"


async def test_dispatch_matrix_covers_every_declared_kind():
    """The matrix above is hand-written; this fails the day a kind is added to
    MEDIA_POLICIES without a row, instead of silently under-covering it."""
    from media_policies import MEDIA_POLICIES

    assert {kind for kind, _ in _DISPATCH_MATRIX} == set(MEDIA_POLICIES)


async def test_send_media_uses_default_chat_when_context_lacks_numeric():
    ch, bot = _mk_channel()
    await ch.send_media(b"B", "document", "f.pdf", {"chat_id": "not-numeric"})
    assert bot.send_document.await_args.args[0] == 100   # falls back to self.chat_id


async def test_send_media_raises_when_app_none():
    ch, _ = _mk_channel()
    ch._app = None
    with pytest.raises(RuntimeError):
        await ch.send_media(b"B", "document", "f.pdf", {"chat_id": 5})


# #1661: an agent writes its caption in Markdown, as everywhere else; the file
# arrives with the formatting shown, not the asterisks and link syntax.
async def test_send_media_renders_markdown_caption():
    ch, bot = _mk_channel()
    await ch.send_media(b"B", "document", "inv.pdf", {"chat_id": 5},
                        caption=admitted("**March invoice** is attached"))
    kwargs = bot.send_document.await_args.kwargs
    assert kwargs["caption"] == "March invoice is attached"
    [bold] = kwargs["caption_entities"]
    assert (bold.type, bold.offset, bold.length) == ("bold", 0, len("March invoice"))


async def test_send_media_caption_refused_entities_resends_plain():
    from telegram.error import BadRequest

    ch, bot = _mk_channel()
    bot.send_document.side_effect = [BadRequest("Can't parse entities"),
                                     MagicMock(message_id=8)]
    await ch.send_media(b"B", "document", "inv.pdf", {"chat_id": 5},
                        caption=admitted("**March invoice** is attached"))
    assert bot.send_document.await_count == 2
    kwargs = bot.send_document.await_args.kwargs
    assert kwargs["caption"] == "March invoice is attached"
    assert kwargs.get("caption_entities") is None


async def test_base_channel_send_media_not_implemented():
    from channels import Channel

    class _Bare(Channel):
        name = "bare"
        default_agent = "assistant"

        async def start(self):
            ...

        async def send(self, message, context):
            ...

        async def stop(self):
            ...

    with pytest.raises(NotImplementedError):
        await _Bare().send_media(b"B", "document", "f.pdf", {"chat_id": 5})
