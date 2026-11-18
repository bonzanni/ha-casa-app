"""``TelegramChannel._handle`` reuses a bound cid or allocates a fresh one."""

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
        known_bot_id=111,
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

    async def test_allocates_cid_when_var_holds_no_string(self, telegram_channel):
        # Inside an SDK tool handler the variable holds the pooled client's
        # cid box — an object the pool rewrites for every turn, not a cid.
        import re

        from log_cid import cid_var
        from sdk_client_pool import _CidBox

        box = _CidBox()
        box.value = "0badcafe"
        token = cid_var.set(box)
        try:
            await _invoke_handle(telegram_channel, text="hello")
        finally:
            cid_var.reset(token)

        msgs = await _drain(telegram_channel)
        assert msgs, "expected at least one bus message"
        cid = msgs[-1].context["cid"]
        assert type(cid) is str, cid
        assert re.match(r"^[0-9a-f]{8}$", cid), cid
        assert cid != "0badcafe"
