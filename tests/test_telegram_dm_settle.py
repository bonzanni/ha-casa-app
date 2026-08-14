"""INV-TG-007 — a settled DM keyboard message drops its buttons.

``edit_dm_message`` is the settle path for every DM question Casa posts:
``ask_user`` and ``wipe_memory`` (``tools.py``), the protected-action challenge
(``authz_grants.py``), and callback / event / trigger / persona / specialist
install consent. Every one of those calls it from a broker FINISH hook, i.e.
only at a terminal outcome, so the edit must retire the keyboard as well as the
text.

A bare ``edit_message_text`` does NOT do that: PTB drops an omitted
``reply_markup``, so ``editMessageText`` never touches the markup and the
buttons stay tappable under the settled text (the same defect v0.79.0 fixed for
the TOPIC path in ``edit_topic_message(clear_keyboard=True)`` — see
``test_telegram_ask_settle.py``; the DM path was left behind). A tap on a
settled keyboard is refused by the broker (``stale``/``duplicate``) so nothing
wrong commits, but the operator keeps a live-looking control for a closed
question.

The clear is UNCONDITIONAL here rather than an opt-in flag: every caller in the
tree is a terminal settle, and a flag is a thing the next call site forgets.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest

pytestmark = pytest.mark.asyncio

CHAT = 4242
MESSAGE_ID = 7007

# No markdown markers -> render() returns entities=None -> the plain branch.
PLAIN_TEXT = "Invoice ready?\n\n(this question has expired)"
# A bold span -> render() returns entities -> the rich branch.
RICH_TEXT = "Invoice ready?\n\n*Answered: Confirm*"


def _mk_channel():
    from channels.telegram import TelegramChannel

    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    ch = TelegramChannel(bot=bot, chat_id=CHAT)
    return ch, bot


def _assert_empty_markup(kwargs: dict) -> None:
    """The markup must be PRESENT (not dropped by PTB) and EMPTY."""
    assert "reply_markup" in kwargs, (
        "reply_markup absent — PTB drops the None and the keyboard survives"
    )
    markup = kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert list(markup.inline_keyboard) == []


async def test_pin_inv_tg_007_plain_settle_clears_the_keyboard():
    ch, bot = _mk_channel()

    ok = await ch.edit_dm_message(CHAT, MESSAGE_ID, PLAIN_TEXT)

    assert ok is True
    assert bot.edit_message_text.await_count == 1
    _, kwargs = bot.edit_message_text.call_args
    _assert_empty_markup(kwargs)
    assert kwargs["text"] == PLAIN_TEXT
    assert kwargs["chat_id"] == CHAT
    assert kwargs["message_id"] == MESSAGE_ID


async def test_pin_inv_tg_007_rich_settle_clears_the_keyboard():
    ch, bot = _mk_channel()

    ok = await ch.edit_dm_message(CHAT, MESSAGE_ID, RICH_TEXT)

    assert ok is True
    assert bot.edit_message_text.await_count == 1
    _, kwargs = bot.edit_message_text.call_args
    # Precondition: this text really does take the rich branch, else the test
    # would silently be a second copy of the plain one.
    assert kwargs.get("entities"), "expected the entity-bearing branch"
    _assert_empty_markup(kwargs)


async def test_pin_inv_tg_007_entity_fallback_retry_clears_the_keyboard():
    """The rich edit can fail on entities; the plain retry must still clear."""
    ch, bot = _mk_channel()
    bot.edit_message_text = AsyncMock(
        side_effect=[BadRequest("Can't parse entities"), True],
    )

    ok = await ch.edit_dm_message(CHAT, MESSAGE_ID, RICH_TEXT)

    assert ok is True
    assert bot.edit_message_text.await_count == 2
    for call in bot.edit_message_text.call_args_list:
        _assert_empty_markup(call.kwargs)


# Exit 3 means ONLY "the library is absent" — the one condition that may skip.
# Any other import failure (a moved internal module, a broken install) must fail
# the test, or the invariant quietly stops being checked.
_WIRE_PROBE = """
import sys
try:
    import telegram  # noqa: F401
except ModuleNotFoundError as exc:
    if exc.name == "telegram":
        sys.exit(3)
    raise

from telegram import InlineKeyboardMarkup
from telegram.request._requestdata import RequestData
from telegram.request._requestparameter import RequestParameter

omitted = RequestData([RequestParameter.from_input("reply_markup", None)]).parameters
cleared = RequestData(
    [RequestParameter.from_input("reply_markup", InlineKeyboardMarkup([]))],
).parameters
assert "reply_markup" not in omitted, omitted
assert "reply_markup" in cleared, cleared
assert cleared["reply_markup"] == {}, cleared
print("OK")
"""


def test_pin_inv_tg_007_empty_markup_survives_to_the_wire():
    """The clear must survive SERIALIZATION, not merely reach the client.

    The tests above stop at a mock — and this suite stubs the whole ``telegram``
    package (``conftest.py``), so they cannot see the real library at all. They
    pin the argument, not the request. This one pins what the invariant actually
    rests on, in a subprocess that imports the REAL client:

    * a ``None`` markup is dropped from the request entirely — that is the bug;
    * an empty ``InlineKeyboardMarkup`` still emits a present ``reply_markup``.

    It serializes to ``{}`` rather than ``{"inline_keyboard": []}`` (the library
    strips empty sequences), and the platform accepts that as "no keyboard" —
    verified end-to-end against the real Bot API: a posted two-button keyboard,
    settled through ``edit_dm_message``, came back cleared. Pinned because a
    library change to either half would silently restore the bug, and every
    other test here would stay green.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", _WIRE_PROBE],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode == 3:
        pytest.skip("python-telegram-bot not installed in this environment")
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


async def test_identical_re_edit_still_reports_success():
    """"Message is not modified" means the desired end state already holds —
    tolerated as success, unchanged by the markup clear."""
    ch, bot = _mk_channel()
    bot.edit_message_text = AsyncMock(
        side_effect=BadRequest("Message is not modified"),
    )

    assert await ch.edit_dm_message(CHAT, MESSAGE_ID, PLAIN_TEXT) is True


async def test_edit_failure_still_reports_false():
    ch, bot = _mk_channel()
    bot.edit_message_text = AsyncMock(side_effect=BadRequest("MESSAGE_ID_INVALID"))

    assert await ch.edit_dm_message(CHAT, MESSAGE_ID, PLAIN_TEXT) is False
