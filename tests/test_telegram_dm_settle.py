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


# ---------------------------------------------------------------------------
# INV-JOB-013 — the crash window between the terminal CAS and the keyboard edit
# ---------------------------------------------------------------------------
#
# Everything above drives `edit_dm_message` directly. This drives it the way a
# scheduled ask does — through `_settle` and then through the BOOT RECONCILER —
# because nothing else in the tree does, and the gap is exactly where #635's
# reported picture (buttons intact, tap says "expired") is reachable from code.


class _GatedStore:
    """A `ScheduledAskStore` whose settling CAS blocks AFTER its durable write.

    The process is "killed" while `_settle` is suspended there: the record is on
    disk reading `settling`, and not one byte has reached Telegram.
    """

    def __new__(cls, path):
        import scheduled_asks

        class _Impl(scheduled_asks.ScheduledAskStore):
            def __init__(self, path):
                super().__init__(path)
                self.cas_done = None
                self.release = None

            async def set_state(self, rid, state, **fields):
                ok = await super().set_state(rid, state, **fields)
                if state == scheduled_asks.STATE_SETTLING:
                    self.cas_done.set()
                    await self.release.wait()
                return ok

        return _Impl(path)


def _crash_record() -> dict:
    return {
        "rid": "rid-crash", "state": "live",
        "role": "assistant", "session_scope": "cron-invoices",
        "scope": f"dm:{CHAT}", "chat_id": CHAT, "operator_id": CHAT,
        "message_id": MESSAGE_ID,
        "options": ["Confirm", "Wrong"], "body": "Invoice ready?",
        "epoch": "0:0", "created_at": 0.0, "expires_at": 1_000.0,
    }


async def test_crash_after_settling_cas_replays_exact_edit_once_without_dispatch(
    tmp_path, monkeypatch,
):
    """INV-JOB-013 through the REAL `TelegramChannel.edit_dm_message`.

    A terminal outcome is decided, the CAS persists `settling` together with the
    exact text it decided, and the process dies before the keyboard is touched.
    The next boot replays that one edit — with the buttons cleared, INV-TG-007 —
    and dispatches NOTHING: at-most-once binds the continuation, not the edit.
    """
    import asyncio
    import contextlib

    import scheduled_asks
    from broker_helpers import wait_until

    monkeypatch.setattr(scheduled_asks, "_BOOT_REVOCATIONS", [])
    monkeypatch.setattr(scheduled_asks, "_BOOT_RECONCILED", False)
    path = str(tmp_path / "scheduled_asks.json")
    store = _GatedStore(path)
    store.cas_done = asyncio.Event()
    store.release = asyncio.Event()
    monkeypatch.setattr(scheduled_asks, "STORE", store)

    rec = _crash_record()
    await store.put(rec)

    ch, bot = _mk_channel()
    ch._dispatch_scheduled_continuation = AsyncMock(return_value=True)
    task = asyncio.create_task(scheduled_asks._settle(
        ch, rec, kind="cancelled", reason="trigger_cancelled", chosen=None,
        edit_text=PLAIN_TEXT,
    ))
    await wait_until(store.cas_done.is_set)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # The process died here: decided and persisted, nothing sent.
    assert bot.edit_message_text.await_count == 0
    assert ch._dispatch_scheduled_continuation.await_count == 0

    # A fresh process reads the same file.
    reopened = scheduled_asks.ScheduledAskStore(path)
    monkeypatch.setattr(scheduled_asks, "STORE", reopened)
    records = reopened.all()
    assert sum(r.get("state") == scheduled_asks.STATE_SETTLING
               for r in records) == 1
    assert sum(r.get("terminal_edit") == PLAIN_TEXT for r in records) == 1

    boot_ch, boot_bot = _mk_channel()
    boot_ch._dispatch_scheduled_continuation = AsyncMock(return_value=True)
    counts = await scheduled_asks.reconcile_at_boot(boot_ch, now=0.0)

    assert counts.get("settled_before_crash", 0) == 1
    assert boot_bot.edit_message_text.await_count == 1
    _, kwargs = boot_bot.edit_message_text.call_args
    assert sum(kwargs.get(k) == v for k, v in
               (("text", PLAIN_TEXT), ("chat_id", CHAT),
                ("message_id", MESSAGE_ID))) == 3
    _assert_empty_markup(kwargs)
    assert boot_ch._dispatch_scheduled_continuation.await_count == 0
    assert len(reopened.all()) == 0
