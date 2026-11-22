"""Integration tests for Telegram reconnect wiring (spec 5.2 §4).

We stub the telegram package so channels.telegram imports cleanly
without python-telegram-bot installed. Mirrors the pattern used by
test_telegram_split.py.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# telegram.* module stubs — installed by tests/conftest.py at session start
# so all test files share the same NetworkError / TimedOut / TelegramError
# class identities (see conftest.py docstring). We ALIAS the local
# `_FakeNetworkError` names to whatever conftest registered in
# sys.modules["telegram.error"], ensuring that the exceptions raised in
# our AsyncMock side_effects are the same class objects that
# channels.telegram's `except NetworkError:` clauses catch.
# ---------------------------------------------------------------------------


_FakeNetworkError = sys.modules["telegram.error"].NetworkError
_FakeTimedOut = sys.modules["telegram.error"].TimedOut
_FakeTelegramError = sys.modules["telegram.error"].TelegramError


def _install_telegram_stubs() -> None:
    """Idempotently install fake telegram.* modules."""
    if "telegram" in sys.modules and getattr(
        sys.modules["telegram"], "_casa_stub", False,
    ):
        return

    tg = types.ModuleType("telegram")
    tg._casa_stub = True  # type: ignore[attr-defined]
    tg.Update = MagicMock()

    tg_const = types.ModuleType("telegram.constants")
    tg_const.ChatAction = MagicMock()
    tg.constants = tg_const

    tg_err = types.ModuleType("telegram.error")
    tg_err.TelegramError = _FakeTelegramError
    tg_err.NetworkError = _FakeNetworkError
    tg_err.TimedOut = _FakeTimedOut
    tg.error = tg_err

    tg_ext = types.ModuleType("telegram.ext")
    tg_ext.Application = MagicMock()
    tg_ext.ContextTypes = MagicMock()
    tg_ext.MessageHandler = MagicMock()
    tg_ext.filters = MagicMock()
    tg.ext = tg_ext

    sys.modules["telegram"] = tg
    sys.modules["telegram.constants"] = tg_const
    sys.modules["telegram.error"] = tg_err
    sys.modules["telegram.ext"] = tg_ext


_install_telegram_stubs()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_fake_application() -> MagicMock:
    """Return a MagicMock that shape-matches telegram.ext.Application."""
    app = MagicMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.add_handler = MagicMock()
    app.add_error_handler = MagicMock()
    app.process_update = AsyncMock()

    app.bot = MagicMock()
    # initialize() runs getMe: a started bot knows its own id.
    app.bot.id = 4242
    app.bot.set_webhook = AsyncMock()
    app.bot.delete_webhook = AsyncMock()
    app.bot.get_me = AsyncMock(return_value=MagicMock(username="casabot"))
    app.bot.send_message = AsyncMock()
    app.bot.edit_message_text = AsyncMock()
    app.bot.send_chat_action = AsyncMock()

    app.updater = MagicMock()
    app.updater.start_polling = AsyncMock()
    app.updater.stop = AsyncMock()

    return app


@pytest.fixture
def patched_application_builder(monkeypatch):
    """Patch telegram.ext.Application.builder() to return fresh fakes.

    Yields a list; each call to builder() appends a new fake to it, so
    the test can assert how many Applications were built (e.g., after
    a rebuild).
    """
    from telegram.ext import Application  # the stub we installed above

    built: list[MagicMock] = []

    def fake_builder():
        chain = MagicMock()
        chain.token = MagicMock(return_value=chain)

        def build_once():
            app = _make_fake_application()
            built.append(app)
            return app

        chain.build = build_once
        return chain

    monkeypatch.setattr(Application, "builder", fake_builder)
    return built


@pytest.fixture
def mock_bus():
    bus = MagicMock()
    bus.send = AsyncMock()
    bus.request = AsyncMock()
    return bus


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.asyncio


class TestInitialSetWebhookFailure:
    async def test_network_error_on_initial_set_webhook_triggers_supervisor(
        self, patched_application_builder, mock_bus, caplog,
    ):
        caplog.set_level(logging.DEBUG)
        from channels.telegram import TelegramChannel

        # First application: set_webhook raises NetworkError.
        # Second application (after rebuild): set_webhook succeeds.
        def builder_override():
            chain = MagicMock()
            chain.token = MagicMock(return_value=chain)

            def build_once():
                app = _make_fake_application()
                if len(patched_application_builder) == 0:
                    app.bot.set_webhook = AsyncMock(
                        side_effect=_FakeNetworkError("dns lookup failed"),
                    )
                patched_application_builder.append(app)
                return app

            chain.build = build_once
            return chain

        from telegram.ext import Application
        Application.builder = builder_override  # type: ignore[assignment]

        ch = TelegramChannel(
            bot_token="t",
            chat_id="123",
            default_agent="assistant",
            bus=mock_bus,
            webhook_url="https://casa.example",
        )
        # Short backoff for the test.
        import channels.telegram as telegram_mod
        telegram_mod._RECONNECT_INITIAL_MS = 1
        telegram_mod._RECONNECT_CAP_MS = 4

        await ch.start()
        # Let the supervisor complete one retry cycle.
        for _ in range(200):
            await asyncio.sleep(0.005)
            if len(patched_application_builder) >= 2:
                break
        await ch.stop()

        assert len(patched_application_builder) >= 2, \
            "supervisor should have rebuilt the Application at least once"
        # First app's set_webhook was called once (failed). Second app's
        # set_webhook was called once (succeeded).
        assert patched_application_builder[0].bot.set_webhook.await_count == 1
        assert patched_application_builder[1].bot.set_webhook.await_count == 1


def _builder_failing_first_initialize(built: list, exc: BaseException):
    """A builder whose first Application's ``initialize`` (``getMe``) raises
    *exc*; every later one comes up."""
    def builder():
        chain = MagicMock()
        chain.token = MagicMock(return_value=chain)

        def build_once():
            app = _make_fake_application()
            if not built:
                app.initialize = AsyncMock(side_effect=exc)
            built.append(app)
            return app

        chain.build = build_once
        return chain
    return builder


async def _boot_casa_bot(mock_bus):
    """Casa's own bot registered and started the way the boot does it:
    the Telegram transport inside the channel manager, ``start_all()``."""
    from channels import ChannelManager
    from channels.telegram import TelegramChannel
    from channels.telegram_transport import TelegramTransport
    from telegram_channel_double import MemoryBotIdStore

    ch = TelegramChannel(
        bot_token="op://Casa/Telegram/credential", chat_id="123",
        default_agent="assistant", bus=mock_bus,
    )
    transport = TelegramTransport(casa_bot_ids=MemoryBotIdStore())
    transport.add_endpoint(ch, casa=True)
    cm = ChannelManager()
    cm.register(transport)
    await cm.start_all()
    return cm, transport


class TestBootBringUpFailure:
    """#1652: a failure of Casa's own bot to come up at boot never escapes
    ``start_all()`` (which would stop the whole app); the endpoint stays not
    ready."""

    async def test_rejected_token_leaves_the_bot_down_and_the_boot_running(
        self, patched_application_builder, mock_bus, caplog, monkeypatch,
    ):
        caplog.set_level(logging.DEBUG, logger="channels.telegram")
        import channels.telegram as telegram_mod
        monkeypatch.setattr(telegram_mod, "_RECONNECT_INITIAL_MS", 1)
        monkeypatch.setattr(telegram_mod, "_RECONNECT_CAP_MS", 4)
        invalid_token = sys.modules["telegram.error"].InvalidToken
        monkeypatch.setattr(
            telegram_mod.Application, "builder",
            _builder_failing_first_initialize(
                patched_application_builder, invalid_token()))

        cm, transport = await _boot_casa_bot(mock_bus)   # must not raise
        # Time for several reconnect cycles, had one been started.
        for _ in range(20):
            await asyncio.sleep(0.005)
        try:
            assert not transport.ready_event.is_set()
            # A token Telegram rejected cannot become valid without a restart
            # (which reads it again): no retry is made with it.
            assert len(patched_application_builder) == 1
            errors = [r.getMessage() for r in caplog.records
                      if r.levelno == logging.ERROR]
            assert len(errors) == 1, errors
            assert "rejected" in errors[0] and "op://" in errors[0]
        finally:
            await cm.stop_all()

    async def test_other_bring_up_failure_is_retried_until_the_bot_is_up(
        self, patched_application_builder, mock_bus, monkeypatch,
    ):
        import channels.telegram as telegram_mod
        monkeypatch.setattr(telegram_mod, "_RECONNECT_INITIAL_MS", 1)
        monkeypatch.setattr(telegram_mod, "_RECONNECT_CAP_MS", 4)
        retry_after = sys.modules["telegram.error"].RetryAfter
        monkeypatch.setattr(
            telegram_mod.Application, "builder",
            _builder_failing_first_initialize(
                patched_application_builder, retry_after(1)))

        cm, transport = await _boot_casa_bot(mock_bus)   # must not raise
        try:
            await asyncio.wait_for(transport.ready_event.wait(), timeout=2)
            assert len(patched_application_builder) == 2
        finally:
            await cm.stop_all()

    async def test_an_agent_bot_rejected_token_still_reaches_its_starter(
        self, patched_application_builder, mock_bus, monkeypatch,
    ):
        """The own-chat lifecycle reads an agent bot's failed start from
        ``start()`` raising; only Casa's own bot absorbs it."""
        import channels.telegram as telegram_mod
        from channels.telegram import TelegramChannel
        invalid_token = sys.modules["telegram.error"].InvalidToken
        monkeypatch.setattr(
            telegram_mod.Application, "builder",
            _builder_failing_first_initialize(
                patched_application_builder, invalid_token()))
        ch = TelegramChannel(
            bot_token="t", chat_id="123", default_agent="finance",
            bus=mock_bus, is_casa=False, known_bot_id=777)
        try:
            with pytest.raises(invalid_token):
                await ch.start()
        finally:
            await ch.stop()


class TestHealthProbeLoop:
    async def test_probe_failure_triggers_supervisor(
        self, patched_application_builder, mock_bus,
    ):
        from channels.telegram import TelegramChannel
        import channels.telegram as telegram_mod
        telegram_mod._RECONNECT_INITIAL_MS = 1
        telegram_mod._RECONNECT_CAP_MS = 4
        telegram_mod._PROBE_INTERVAL = 0.05
        telegram_mod._PROBE_TIMEOUT = 0.05

        ch = TelegramChannel(
            bot_token="t", chat_id="123",
            default_agent="assistant", bus=mock_bus,
        )
        await ch.start()

        # First app: get_me raises NetworkError on the next probe.
        first_app = patched_application_builder[0]
        first_app.bot.get_me = AsyncMock(
            side_effect=_FakeNetworkError("connection reset"),
        )

        # Wait for probe → trigger → rebuild.
        for _ in range(300):
            await asyncio.sleep(0.01)
            if len(patched_application_builder) >= 2:
                break

        await ch.stop()
        assert len(patched_application_builder) >= 2


class TestPtbErrorHandler:
    async def test_network_error_surfaced_via_error_handler_triggers_supervisor(
        self, patched_application_builder, mock_bus,
    ):
        from channels.telegram import TelegramChannel
        import channels.telegram as telegram_mod
        telegram_mod._RECONNECT_INITIAL_MS = 1
        telegram_mod._RECONNECT_CAP_MS = 4

        ch = TelegramChannel(
            bot_token="t", chat_id="123",
            default_agent="assistant", bus=mock_bus,
        )
        await ch.start()

        # add_error_handler was called with the channel's handler.
        first_app = patched_application_builder[0]
        handler = first_app.add_error_handler.call_args[0][0]

        # Simulate PTB calling the handler with a NetworkError.
        ctx = MagicMock()
        ctx.error = _FakeNetworkError("upstream 502")
        await handler(update=None, context=ctx)

        for _ in range(300):
            await asyncio.sleep(0.01)
            if len(patched_application_builder) >= 2:
                break

        await ch.stop()
        assert len(patched_application_builder) >= 2

    async def test_non_network_error_does_not_trigger_supervisor(
        self, patched_application_builder, mock_bus,
    ):
        from channels.telegram import TelegramChannel
        import channels.telegram as telegram_mod
        telegram_mod._RECONNECT_INITIAL_MS = 1
        telegram_mod._RECONNECT_CAP_MS = 4

        ch = TelegramChannel(
            bot_token="t", chat_id="123",
            default_agent="assistant", bus=mock_bus,
        )
        await ch.start()

        first_app = patched_application_builder[0]
        handler = first_app.add_error_handler.call_args[0][0]

        # ValueError is not a transport concern — must NOT trigger rebuild.
        ctx = MagicMock()
        ctx.error = ValueError("some handler bug")
        await handler(update=None, context=ctx)

        await asyncio.sleep(0.1)
        await ch.stop()
        # No rebuild — only the initial Application was built.
        assert len(patched_application_builder) == 1


class TestTeardownOnRebuild:
    async def test_old_application_is_torn_down_before_new_one_is_built(
        self, patched_application_builder, mock_bus,
    ):
        from channels.telegram import TelegramChannel
        import channels.telegram as telegram_mod
        telegram_mod._RECONNECT_INITIAL_MS = 1
        telegram_mod._RECONNECT_CAP_MS = 4

        ch = TelegramChannel(
            bot_token="t", chat_id="123",
            default_agent="assistant", bus=mock_bus,
        )
        await ch.start()

        first_app = patched_application_builder[0]
        # Trigger via error handler.
        handler = first_app.add_error_handler.call_args[0][0]
        ctx = MagicMock()
        ctx.error = _FakeNetworkError("blip")
        await handler(update=None, context=ctx)

        for _ in range(300):
            await asyncio.sleep(0.01)
            if len(patched_application_builder) >= 2:
                break

        await ch.stop()

        # First app was stopped + shut down.
        assert first_app.updater.stop.await_count >= 1
        assert first_app.stop.await_count >= 1
        assert first_app.shutdown.await_count >= 1


class TestLogOnceAtChannelLevel:
    async def test_outage_then_recovery_emits_exactly_one_error_and_one_info(
        self, patched_application_builder, mock_bus, caplog,
    ):
        caplog.set_level(logging.DEBUG, logger="channels.telegram")
        from channels.telegram import TelegramChannel
        import channels.telegram as telegram_mod
        telegram_mod._RECONNECT_INITIAL_MS = 1
        telegram_mod._RECONNECT_CAP_MS = 4

        ch = TelegramChannel(
            bot_token="t", chat_id="123",
            default_agent="assistant", bus=mock_bus,
        )
        await ch.start()

        first_app = patched_application_builder[0]
        # First rebuild attempt fails, second succeeds — accomplished by
        # making the NEXT builder produce an app whose initialize raises.
        original_builder = telegram_mod.Application.builder
        call_count = [0]

        def fail_once_builder():
            chain = MagicMock()
            chain.token = MagicMock(return_value=chain)

            def build_once():
                app = _make_fake_application()
                call_count[0] += 1
                if call_count[0] == 1:  # first rebuild fails
                    app.initialize = AsyncMock(
                        side_effect=_FakeNetworkError("first-retry-fails"),
                    )
                patched_application_builder.append(app)
                return app

            chain.build = build_once
            return chain

        telegram_mod.Application.builder = fail_once_builder

        # Trigger the outage.
        handler = first_app.add_error_handler.call_args[0][0]
        ctx = MagicMock()
        ctx.error = _FakeNetworkError("outage")
        await handler(update=None, context=ctx)

        for _ in range(500):
            await asyncio.sleep(0.01)
            if call_count[0] >= 2:
                break

        # Restore builder, stop channel.
        telegram_mod.Application.builder = original_builder
        await ch.stop()

        channel_logger = "channels.telegram"
        errors = [
            r for r in caplog.records
            if r.levelno == logging.ERROR and r.name == channel_logger
        ]
        infos = [
            r for r in caplog.records
            if r.levelno == logging.INFO and r.name == channel_logger
            and "recover" in r.message.lower()
        ]
        assert len(errors) == 1, [r.message for r in errors]
        assert len(infos) == 1, [r.message for r in infos]


class TestTeardownPerStep:
    """M8 (v0.52.0): a failing teardown step must not skip the others, and a
    partial bring-up in _rebuild must roll back the started Application."""

    @pytest.mark.unit
    async def test_teardown_stops_and_shuts_down_app_even_when_delete_webhook_raises(
        self,
    ):
        """delete_webhook failing (network down) must not skip stop()/shutdown()."""
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(
            bot_token="t", chat_id="1", webhook_url="https://example.test",
        )
        app = _make_fake_application()
        app.bot.delete_webhook = AsyncMock(
            side_effect=_FakeNetworkError("down"),
        )
        ch._app = app

        await ch._teardown_app()

        assert app.stop.await_count == 1
        assert app.shutdown.await_count == 1
        assert ch._app is None

    @pytest.mark.unit
    async def test_rebuild_rolls_back_started_app_when_set_webhook_raises(
        self, monkeypatch,
    ):
        """A started-but-unpublished app must be stopped/shut down before the
        supervisor retries, not silently dropped."""
        from channels.telegram import TelegramChannel
        from telegram.ext import Application

        app = _make_fake_application()
        app.running = True  # after start()
        app.bot.set_webhook = AsyncMock(
            side_effect=_FakeNetworkError("blip"),
        )

        def fake_builder():
            chain = MagicMock()
            chain.token = MagicMock(return_value=chain)
            chain.build = MagicMock(return_value=app)
            return chain

        monkeypatch.setattr(Application, "builder", fake_builder)

        ch = TelegramChannel(
            bot_token="t", chat_id="1", webhook_url="https://example.test",
        )

        with pytest.raises(_FakeNetworkError):
            await ch._rebuild()

        assert app.stop.await_count == 1
        assert app.shutdown.await_count == 1
        assert ch._app is None


class TestRebuildCancellation:
    """#347: ``_rebuild``'s rollback ran only under ``except Exception`` —
    a cancellation during ``set_webhook()``/``start_polling()`` skipped the
    stop()/shutdown() rollback, and since the app was never published to
    ``self._app``, channel ``stop()`` could not tear it down either: the
    running updater/fetcher leaked."""

    @pytest.mark.unit
    async def test_rebuild_cancelled_during_set_webhook_rolls_back(
        self, monkeypatch,
    ):
        from channels.telegram import TelegramChannel
        from telegram.ext import Application

        app = _make_fake_application()
        app.running = True  # after start()
        app.bot.set_webhook = AsyncMock(
            side_effect=asyncio.CancelledError(),
        )

        def fake_builder():
            chain = MagicMock()
            chain.token = MagicMock(return_value=chain)
            chain.build = MagicMock(return_value=app)
            return chain

        monkeypatch.setattr(Application, "builder", fake_builder)

        ch = TelegramChannel(
            bot_token="t", chat_id="1", webhook_url="https://example.test",
        )

        with pytest.raises(asyncio.CancelledError):
            await ch._rebuild()

        assert app.stop.await_count == 1
        assert app.shutdown.await_count == 1
        assert ch._app is None

    @pytest.mark.unit
    async def test_rebuild_cancelled_during_start_polling_stops_updater(
        self, monkeypatch,
    ):
        """Sol+Terra r1 (P1): in polling mode a cancellation inside
        ``start_polling()`` leaves ``updater.running`` set; ``app.stop()``
        does NOT stop the updater and ``app.shutdown()`` then raises
        (swallowed) — the polling task survived unpublished. The rollback
        must stop the updater FIRST, mirroring ``_teardown_app``."""
        from channels.telegram import TelegramChannel
        from telegram.ext import Application

        app = _make_fake_application()
        app.running = True  # after start()
        app.updater.running = True  # start_polling marked itself running
        app.updater.start_polling = AsyncMock(
            side_effect=asyncio.CancelledError(),
        )

        def fake_builder():
            chain = MagicMock()
            chain.token = MagicMock(return_value=chain)
            chain.build = MagicMock(return_value=app)
            return chain

        monkeypatch.setattr(Application, "builder", fake_builder)

        ch = TelegramChannel(bot_token="t", chat_id="1")  # polling mode

        with pytest.raises(asyncio.CancelledError):
            await ch._rebuild()

        assert app.updater.stop.await_count == 1
        assert app.stop.await_count == 1
        assert app.shutdown.await_count == 1
        assert ch._app is None


class TestRequestBoundaryWiring:
    """INV-TG-009 (#846): every Application the channel builds — the first and
    each reconnect rebuild — installs the surrogate-replacing request class on
    the ordinary Bot API pool, exactly once, and leaves the getUpdates pool
    alone. This is the one place the request-layer boundary can silently
    regress to the base (a builder that forgets ``.request(...)``), so it is
    pinned in-process; what the request class DOES is pinned against the real
    library by tests/test_telegram_surrogate_boundary.py."""

    async def test_every_built_application_installs_the_request_boundary_once(
        self, patched_application_builder, mock_bus,
    ):
        from channels.telegram import TelegramChannel, _SurrogateSafeRequest
        import channels.telegram as telegram_mod
        telegram_mod._RECONNECT_INITIAL_MS = 1
        telegram_mod._RECONNECT_CAP_MS = 4
        telegram_mod._PROBE_INTERVAL = 0.05
        telegram_mod._PROBE_TIMEOUT = 0.05

        chains: list[MagicMock] = []

        def builder_override():
            chain = MagicMock()
            chain.token = MagicMock(return_value=chain)

            def build_once():
                app = _make_fake_application()
                patched_application_builder.append(app)
                return app

            chain.build = build_once
            chains.append(chain)
            return chain

        from telegram.ext import Application
        Application.builder = builder_override  # type: ignore[assignment]

        ch = TelegramChannel(
            bot_token="t", chat_id="123",
            default_agent="assistant", bus=mock_bus,
        )
        await ch.start()
        # Force one reconnect rebuild: the probe's get_me fails on app 1.
        patched_application_builder[0].bot.get_me = AsyncMock(
            side_effect=_FakeNetworkError("connection reset"),
        )
        for _ in range(300):
            await asyncio.sleep(0.01)
            if len(patched_application_builder) >= 2:
                break
        await ch.stop()

        assert len(chains) >= 2, "the supervisor should have rebuilt at least once"
        for chain in chains:
            assert chain.request.call_count == 1
            (installed,), kwargs = chain.request.call_args
            assert isinstance(installed, _SurrogateSafeRequest)
            assert kwargs == {}
            chain.get_updates_request.assert_not_called()
