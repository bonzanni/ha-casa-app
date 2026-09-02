"""Shared test fixtures and path setup for Casa tests.

Also installs the `telegram.*` package stubs needed by the Telegram
channel tests. Installing here (once, at session start) guarantees
that every test file sees the SAME `NetworkError` / `TimedOut` /
`TelegramError` class objects, so `except NetworkError:` in
`channels.telegram` catches exceptions raised in tests via these
names. If each test file installed its own stubs instead, pytest's
alphabetical discovery order would decide which file's class "wins",
and later files' locally-defined `_FakeNetworkError` would diverge
from the one production code catches.
"""

import sys
import types
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

# Ensure the Casa package root is importable.
_casa_root = str(Path(__file__).resolve().parent.parent / "casa" / "rootfs" / "opt" / "casa")
if _casa_root not in sys.path:
    sys.path.insert(0, _casa_root)


# ---------------------------------------------------------------------------
# Memory cage, invoker-independent half. The documented systemd-run cage
# (CLAUDE.md) only protects runs that remember to use it; on 2026-07-24 an
# uncaged pytest (different repo, same machine) hit 22 GB anon RSS and took
# down the WSL VM. Cap this process's address space so a runaway test fails
# inside pytest (usually MemoryError; native allocators may abort) instead
# of taking the VM. The full unit gate peaks well under 1 GiB RSS; 12 GiB VA
# is pure backstop. Override with PYTEST_RLIMIT_AS_GB (<=0 or a malformed
# value disables). Keep the external systemd-run cage too — RLIMIT_AS is
# per-process, not per-tree, and only guards from conftest import onward.
import math as _math
import os as _os
import resource as _resource

try:
    _cap_gb = float(_os.environ.get("PYTEST_RLIMIT_AS_GB", "12"))
except ValueError:
    _cap_gb = 0.0
if not _math.isfinite(_cap_gb):
    _cap_gb = 0.0
# The cap is PER PROCESS, so running under xdist multiplies the total
# allowance by the worker count — twelve workers would turn a 12 GiB backstop
# into 144 GiB, in a repo that has twice lost the VM to a runaway pytest.
# Divide it across the workers, with a floor, because this limit is a blunt
# instrument: it bounds ADDRESS SPACE, not resident memory, and CPython
# reserves far more VA than it ever touches. Real aggregate memory is bounded
# by the systemd cage the Makefile now applies automatically; this stays a
# per-process backstop for anyone who bypasses it.
if _cap_gb > 0:
    try:
        _workers = int(_os.environ.get("PYTEST_XDIST_WORKER_COUNT", "1"))
    except ValueError:
        _workers = 1
    if _workers > 1:
        # Floor at 6 GiB: 2 GiB proved too tight (a worker hit MemoryError in
        # the full parallel run — RLIMIT_AS bounds ADDRESS SPACE, and CPython
        # reserves far more VA than it uses). Real aggregate memory is the
        # cage's job, not this one's.
        _cap_gb = max(6.0, _cap_gb / _workers)
if _cap_gb > 0:
    try:
        _cap_bytes = int(_cap_gb * 1024**3)
        _soft, _hard = _resource.getrlimit(_resource.RLIMIT_AS)
        if _cap_bytes > 0 and (_soft == _resource.RLIM_INFINITY
                               or _soft > _cap_bytes):
            _resource.setrlimit(_resource.RLIMIT_AS, (_cap_bytes, _hard))
    except (OverflowError, ValueError, OSError):
        pass  # unrepresentable override — run uncaged rather than not at all


# ---------------------------------------------------------------------------
# telegram.* stubs — shared canonical exception classes across all tests.
# ---------------------------------------------------------------------------


class _FakeTelegramError(Exception):
    pass


class _FakeNetworkError(_FakeTelegramError):
    pass


class _FakeTimedOut(_FakeNetworkError):
    pass


class _FakeBadRequest(_FakeNetworkError):
    # Mirrors real PTB 22.7: BadRequest -> NetworkError -> TelegramError
    # (verified empirically 2026-07-12).
    pass


class _FakeForbidden(_FakeTelegramError):
    # Real PTB 22.7: Forbidden -> TelegramError.
    pass


class _FakeRetryAfter(_FakeTelegramError):
    # Real PTB 22.7: RetryAfter -> TelegramError, carrying a .retry_after attr
    # (topic_ledger's sweep reads exc.retry_after to size its backoff).
    def __init__(self, retry_after=0, *a, **k):
        super().__init__(f"Flood control exceeded. Retry in {retry_after} seconds")
        self.retry_after = retry_after


class _FakeInvalidToken(_FakeTelegramError):
    # Real PTB 22.7: InvalidToken -> TelegramError (verified 2026-08-14).
    # A SERVER REFUSAL for INV-TG-006's purposes: PTB raises it from a 401/404
    # response, so the call was evaluated and declined.
    def __init__(self, *a, **k):
        super().__init__("Invalid token")


class _FakeChatMigrated(_FakeTelegramError):
    # Real PTB 22.7: ChatMigrated -> TelegramError, carrying .new_chat_id.
    def __init__(self, new_chat_id=0, *a, **k):
        super().__init__(f"Group migrated to supergroup. New chat id: {new_chat_id}")
        self.new_chat_id = new_chat_id


class _FakeConflict(_FakeTelegramError):
    # Real PTB 22.7: Conflict -> TelegramError, built from an HTTP 409 response.
    # A SERVER REFUSAL for INV-TG-006 (Sol + Terra, diff r3).
    pass


class _FakeInputFile:
    """Value-shape stand-in for telegram.InputFile — captures the wrapped bytes
    and filename so send_media tests can assert on them."""

    def __init__(self, obj, filename=None, **kw):
        self.data = obj.getvalue() if hasattr(obj, "getvalue") else obj
        self.filename = filename


class _FakeMessageEntity:
    """Value-shape stand-in for telegram.MessageEntity.

    Carries the type constants the parser uses and a codepoint→UTF-16 offset
    adjustment matching PTB's ``adjust_message_entities_to_utf_16`` for BMP and
    astral characters. Equality is (type, offset, length) so tests can compare.
    """

    BOLD = "bold"
    ITALIC = "italic"
    CODE = "code"
    PRE = "pre"
    TEXT_LINK = "text_link"

    def __init__(self, type, offset, length, url=None, **kwargs):
        self.type = type
        self.offset = offset
        self.length = length
        self.url = url

    def __eq__(self, other):
        return (
            isinstance(other, _FakeMessageEntity)
            and (self.type, self.offset, self.length)
            == (other.type, other.offset, other.length)
        )

    def __repr__(self):
        return f"_FakeMessageEntity({self.type!r}, {self.offset}, {self.length})"

    @staticmethod
    def adjust_message_entities_to_utf_16(text, entities):
        out = []
        for e in entities:
            off16 = len(text[:e.offset].encode("utf-16-le")) // 2
            len16 = len(text[e.offset:e.offset + e.length].encode("utf-16-le")) // 2
            out.append(_FakeMessageEntity(e.type, off16, len16, url=e.url))
        return out


def _install_telegram_stubs() -> None:
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
    tg_err.BadRequest = _FakeBadRequest
    tg_err.Forbidden = _FakeForbidden
    tg_err.RetryAfter = _FakeRetryAfter
    tg_err.InvalidToken = _FakeInvalidToken
    tg_err.ChatMigrated = _FakeChatMigrated
    tg_err.Conflict = _FakeConflict
    tg.error = tg_err

    tg.MessageEntity = _FakeMessageEntity
    tg.InputFile = _FakeInputFile

    # R5 (v0.89.0): reaction primitive for the `react` framework tool.
    class _FakeReactionTypeEmoji:
        def __init__(self, emoji):
            self.emoji = emoji

        def __repr__(self):
            return f"_FakeReactionTypeEmoji({self.emoji!r})"

    tg.ReactionTypeEmoji = _FakeReactionTypeEmoji

    tg_ext = types.ModuleType("telegram.ext")
    tg_ext.Application = MagicMock()
    tg_ext.ContextTypes = MagicMock()
    tg_ext.MessageHandler = MagicMock()
    tg_ext.filters = MagicMock()
    tg.ext = tg_ext

    # Simple shim classes so `from telegram import BotCommand, BotCommandScopeChat` works
    class _FakeBotCommand:
        def __init__(self, command, description):
            self.command = command
            self.description = description

    class _FakeBotCommandScopeChat:
        def __init__(self, chat_id):
            self.chat_id = chat_id
            self.type = "chat"

    tg.BotCommand = _FakeBotCommand
    tg.BotCommandScopeChat = _FakeBotCommandScopeChat

    # v0.37.0 E-12 (Phase 2): inline-keyboard primitives — minimal value-shape
    # stubs that preserve the constructor kwargs so tests can inspect them.
    class _FakeInlineKeyboardButton:
        def __init__(self, text, *, callback_data=None, url=None, **kw):
            self.text = text
            self.callback_data = callback_data
            self.url = url

    class _FakeInlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    tg.InlineKeyboardButton = _FakeInlineKeyboardButton
    tg.InlineKeyboardMarkup = _FakeInlineKeyboardMarkup

    # python-telegram-bot also surfaces CallbackQueryHandler from telegram.ext;
    # ext stubs are a MagicMock above, so this just lives there too.
    tg_ext.CallbackQueryHandler = MagicMock()

    sys.modules["telegram"] = tg
    sys.modules["telegram.constants"] = tg_const
    sys.modules["telegram.error"] = tg_err
    sys.modules["telegram.ext"] = tg_ext


_install_telegram_stubs()


# ---------------------------------------------------------------------------
# Forum supergroup fakes — used by telegram engagement tests.
# ---------------------------------------------------------------------------


@dataclass
class _FakeTopicState:
    thread_id: int
    name: str
    icon_emoji: str | None = None
    closed: bool = False


@dataclass
class _FakeForumSupergroup:
    chat_id: int
    topics: dict = field(default_factory=dict)
    _next_thread_id: int = 1001
    messages_by_thread: dict = field(default_factory=lambda: defaultdict(list))
    my_commands_by_scope: dict = field(default_factory=dict)
    bot_can_manage_topics: bool = True


class _FakeTelegramBot:
    def __init__(self):
        self.messages: list = []
        self._supergroups: dict = {}

    def _require_supergroup(self, chat_id):
        if chat_id not in self._supergroups:
            self._supergroups[chat_id] = _FakeForumSupergroup(chat_id=chat_id)
        return self._supergroups[chat_id]

    async def create_forum_topic(self, chat_id, name, icon_custom_emoji_id=None, **kw):
        sg = self._require_supergroup(chat_id)
        tid = sg._next_thread_id
        sg._next_thread_id += 1
        sg.topics[tid] = _FakeTopicState(thread_id=tid, name=name, icon_emoji=icon_custom_emoji_id)
        return MagicMock(message_thread_id=tid)

    async def edit_forum_topic(self, chat_id, message_thread_id, name=None, icon_custom_emoji_id=None, **kw):
        sg = self._require_supergroup(chat_id)
        topic = sg.topics[message_thread_id]
        if name is not None:
            topic.name = name
        if icon_custom_emoji_id is not None:
            topic.icon_emoji = icon_custom_emoji_id
        return True

    async def close_forum_topic(self, chat_id, message_thread_id, **kw):
        sg = self._require_supergroup(chat_id)
        sg.topics[message_thread_id].closed = True
        return True

    async def send_message(self, chat_id, text, message_thread_id=None, **kw):
        # Always register the supergroup so tests can inspect _supergroups[chat_id].
        sg = self._require_supergroup(chat_id)
        if message_thread_id is not None:
            sg.messages_by_thread[message_thread_id].append(text)
        else:
            self.messages.append((chat_id, text))
        return MagicMock(message_id=1)

    async def set_my_commands(self, commands, scope=None, **kw):
        chat_id = getattr(scope, "chat_id", None) if scope is not None else None
        if chat_id is None and self._supergroups:
            chat_id = next(iter(self._supergroups))
        if chat_id is None:
            return True
        sg = self._require_supergroup(chat_id)
        scope_key = repr(scope) if scope is not None else "default"
        sg.my_commands_by_scope[scope_key] = [
            {"command": c.command, "description": c.description} for c in commands
        ]
        return True

    async def get_chat_member(self, chat_id, user_id, **kw):
        sg = self._require_supergroup(chat_id)
        m = MagicMock()
        m.can_manage_topics = sg.bot_can_manage_topics
        return m

    async def get_me(self):
        m = MagicMock()
        m.id = 4242
        return m


@pytest.fixture
def fake_telegram_bot():
    return _FakeTelegramBot()


@pytest_asyncio.fixture
async def engagement_fixture(tmp_path):
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="t", origin={"role": "assistant"}, topic_id=555,
    )

    class _Fx:
        registry = reg
        active_record = rec

    return _Fx()


# ---------------------------------------------------------------------------
# Ask validation-gate isolation (v0.84.0 round 4, Task A5).
# ---------------------------------------------------------------------------
# ``channels.channel_handlers`` keeps process-global ``ASK_GATES`` +
# ``_ASK_VALIDATION_OWNERS`` maps. In PRODUCTION request_ids are unique, so a
# resolved gate lingering for its reattach window never collides. Tests, by
# contrast, reuse fixed request_ids ("r1", "fu", ...) across files, so a
# resolved gate left by one test would be read by an unrelated same-id test in
# another file. Clear both maps around every test (production-inert — nothing
# imports this fixture; it only resets in-memory test state).
@pytest.fixture(autouse=True)
def _isolate_engagement_control_root(tmp_path_factory, monkeypatch):
    """Containment stage 2, Task 4: ``drivers.workspace.CONTROL_ROOT``
    defaults to the real ``/data/engagement-ctl`` in production. Without
    this, any test that provisions a workspace (or exercises the
    session-id/spool/stream-cursor/stderr-ring/casa-meta control-dir paths)
    would try to mkdir/write under that real path — unwritable in CI,
    cross-test-polluting locally, and liable to silently create a real
    ``/data`` tree on a dev machine that happens to be running as root.
    Redirected to a per-test tmp dir; a test that specifically needs to
    assert the real production default (e.g. the control_dir() shape test)
    restores it via its own ``monkeypatch.setattr`` — the fixture instance is
    shared within one test, so that composes cleanly.

    Deliberately a SIBLING of (not nested inside) the test's own ``tmp_path``:
    several existing tests point ``engagements_root``/``_ENGAGEMENTS_ROOT`` at
    ``tmp_path`` itself and then ``os.scandir`` its direct children as "every
    workspace" — nesting the control root inside ``tmp_path`` would show up
    as a spurious extra entry in exactly those scans."""
    try:
        import drivers.workspace as _ws
    except Exception:  # pragma: no cover — module import is universal in tests
        yield
        return
    ctl_root = tmp_path_factory.mktemp("engagement-ctl")
    monkeypatch.setattr(_ws, "CONTROL_ROOT", str(ctl_root))
    yield


@pytest.fixture(autouse=True)
def _isolate_engagement_outbox_root(tmp_path_factory, monkeypatch):
    """Containment stage 2, Task 11: ``plugin_outbox.ENGAGEMENT_OUTBOX_ROOT``
    defaults to the real ``/data/plugin-outbox-eng`` in production. Without
    this, any test that provisions a workspace (or renders a run script) for
    a real uid would try to mkdir under that real path — unwritable in CI,
    same rationale as ``_isolate_engagement_control_root`` above. Also clears
    the process-global per-uid ``PluginOutbox`` cache both before and after
    the test so a leaked cached instance (and its open dir-FDs) from one test
    can never leak into another."""
    try:
        import plugin_outbox as _pob
    except Exception:  # pragma: no cover — module import is universal in tests
        yield
        return
    for _ob in _pob._engagement_outboxes.values():
        try:
            _ob.close()
        except Exception:  # noqa: BLE001 — best-effort pre-test cleanup
            pass
    _pob._engagement_outboxes.clear()
    out_root = tmp_path_factory.mktemp("plugin-outbox-eng")
    monkeypatch.setattr(_pob, "ENGAGEMENT_OUTBOX_ROOT", str(out_root))
    yield
    for _ob in _pob._engagement_outboxes.values():
        try:
            _ob.close()
        except Exception:  # noqa: BLE001 — best-effort post-test cleanup
            pass
    _pob._engagement_outboxes.clear()


@pytest.fixture(autouse=True)
def _isolate_resident_bindings(tmp_path, monkeypatch):
    """Personality Phase A, Task 8: point the resident instance-tuple root
    (``CASA_BINDINGS_DIR``, consumed by agent_loader's boot-time binding
    reconciliation) at a per-test tmp dir. Without this, any test that loads a
    resident agent would try to write its active/desired binding tuple under the
    real ``/config/bindings`` tree (unwritable in CI, cross-test-polluting
    locally). Production leaves the env var unset and uses ``/config/bindings``.
    Harmless for tests that never load a resident — it only sets an env var."""
    monkeypatch.setenv("CASA_BINDINGS_DIR", str(tmp_path / "casa-bindings"))
    yield


@pytest.fixture(autouse=True)
def _fresh_operator_notice_lock():
    """Rebind ``casa_core._OPERATOR_NOTICE_LOCK`` per test.

    It is a module-level ``asyncio.Lock()`` created at import (#556), so it
    binds to the FIRST running loop that acquires it. Every pytest-asyncio test
    gets its own loop, so the second file in a worker to use an operator
    notifier raised "is bound to a different event loop" — and worse, a loop
    that died holding it left it locked for the rest of the process. The suite
    stayed green only because ``--dist loadfile`` usually scattered those files
    across workers; run `tests/test_placeholder_rewrite_notice.py` and
    `tests/test_plugin_health_notify.py` in one process on v0.196.0 and it
    fails. Reproduced at 978f812 before this batch — not caused by it.

    Test-only: production has exactly one loop for the process's life, so the
    lock there is bound once and correctly. Nothing about the guarantee it
    provides (at most one operator notice in flight) is relaxed here — each
    test simply gets its own instance of it."""
    try:
        import asyncio as _asyncio

        import casa_core as _cc
    except Exception:  # pragma: no cover — import is universal in tests
        yield
        return
    _cc._OPERATOR_NOTICE_LOCK = _asyncio.Lock()
    yield


@pytest.fixture(autouse=True)
def _fresh_reload_locks(monkeypatch):
    """Give every test its own reload lock cache and reader/writer lock.

    ``reload._LOCKS`` caches one ``asyncio.Lock`` per scope key for the
    process's life and ``reload._GLOBAL_RW`` is created once; both are correct
    in production, where there is one event loop. An ``asyncio.Lock`` binds to
    the loop of its FIRST contended acquire, and pytest-asyncio gives each test
    a fresh loop — so a test that genuinely contends on ``agent:<role>`` leaves
    a lock bound to a dead loop for the next test to trip over ("is bound to a
    different event loop"). Several files already reset both by hand; this
    makes the reset the default. Same defect class as the two fixtures below.

    Test-only: nothing about the lock discipline reload enforces is relaxed —
    each test simply gets its own instances of the same locks."""
    try:
        import reload as _reload
    except Exception:  # pragma: no cover — import is universal in tests
        yield
        return
    monkeypatch.setattr(_reload, "_LOCKS", {})
    monkeypatch.setattr(_reload, "_GLOBAL_RW", None)
    # The remembered incomplete retirements are process state of the same
    # kind (#786): one test's failed teardown must not make the next test's
    # sweep retry a role it never touched.
    monkeypatch.setattr(_reload, "_INCOMPLETE_RETIREMENTS", set())
    yield


@pytest.fixture(autouse=True)
def _restore_active_runtime():
    """#818: snapshot ``agent.active_runtime`` at every test's setup and
    restore THE SNAPSHOT at its teardown.

    The module global (``agent.py``) is ``None`` until ``casa_core.main`` binds
    the runtime once per process; every consumer reads it at call time. Tests
    that drive the reload tool handlers bare-assign a ``CasaRuntime`` to it and
    nothing restored it, so the leaked runtime — whose ``trigger_registry`` is a
    ``MagicMock`` — made two truthiness probes fire in later, unpatched tests
    (``callback_reconcile``'s routing row and ``_tool_plugin_status``'s
    ``routing_unavailable`` key), breaking exact-shape assertions elsewhere.
    ``--dist loadfile`` hid it by placing the leaker and its victims on different
    workers; the default serial order was red.

    Restore-to-snapshot, never force-``None``: a module-scoped baseline bound
    before this function-scoped fixture runs must come back exactly (a
    ``monkeypatch.setattr`` inside a test is fine — it restores first, to the
    same value). Same defect class and the same shape as the broker fixture
    below (#783); pinned by ``tests/test_active_runtime_isolation.py``.

    Test-only: production binds the global once and never rebinds it."""
    import agent as _agent

    snapshot = _agent.active_runtime
    try:
        yield
    finally:
        _agent.active_runtime = snapshot


@pytest.fixture(autouse=True)
def _isolate_verdict_broker_and_challenges():
    """#783: clear the two process-global request registries at every test
    boundary, so a request one test registers cannot reach the next one.

    ``verdict_broker.BROKER`` (`verdict_broker.py`) and
    ``authz_grants.CHALLENGES`` (`authz_grants.py`) are module-level singletons
    created at import. A ``PendingRequest`` registered inside a test holds a
    future minted by ``asyncio.get_running_loop().create_future()``, i.e. bound
    to that test's pytest-asyncio loop, and the registries kept it after the
    loop closed. Two things then happened to later tests in the same worker:

    * a sweep — ``cancel_all`` / ``cancel`` / ``cancel_scope`` /
      ``cancel_where`` / ``unregister`` — reached ``_finish``, called
      ``set_result`` on the stale future, and because the future carries a
      done-callback, ``Future.__schedule_callbacks`` reached ``call_soon`` on
      the closed loop: ``RuntimeError: Event loop is closed``. That is #783,
      whose victims were ``test_graceful_shutdown_engagement_launch.py`` and
      ``test_plugin_triggers_reconcile.py`` — neither of which leaked anything;
    * more quietly, a leftover ``CHALLENGES._entries`` row deduplicated a later
      test's registration away (no keyboard posted at all) and a leftover
      ``BROKER._retired`` tombstone reattached a later same-key registration to
      the earlier test's outcome.

    Same defect class as ``_fresh_operator_notice_lock`` above, and the same
    reason the suite stayed green for so long: ``--dist loadfile`` decides which
    files share a worker, so co-residency — not load — is the die roll. It
    reproduces serially and deterministically; see
    ``tests/test_global_broker_isolation.py``, which pins it.

    Test-only. Production has exactly one loop for the process's life, so a
    broker future is always resolved on the loop that created it, and nothing
    here relaxes any guarantee the broker or the coordinator makes.

    Three implementation points, each load-bearing:

    * **The containers are CLEARED IN PLACE; the singletons are never rebound.**
      ``casa_core`` and ``tools`` do ``from authz_grants import CHALLENGES`` at
      module import, so those aliases would keep pointing at the polluted
      original — and ``tools``' alias is on the sweep path that produced the
      second victim. The canonical objects are captured at setup so the
      teardown reset reaches them whatever a test's own ``monkeypatch`` rebound
      in between.
    * **Nothing here resolves a future or cancels a task or a timer.**
      Resolving the leaked future is precisely the operation that detonates,
      and ``Task.cancel()`` can reach ``call_soon`` on the same closed loop.
      Dropping the reference is enough: nothing else holds the entry. (Dead
      pending tasks may then be reported by the GC as "Task was destroyed but
      it is pending!" — a stderr note from a task whose loop is already gone.)
    * **It runs only at the two test boundaries**, never mid-test, so
      ``drain_hooks``'s 3.12+ eager-gather livelock guard never sees
      ``_hook_tasks``/``_setup_tasks`` mutated underneath it.

    Imports and attribute access are deliberately UNGUARDED, unlike the
    ``try/except ImportError`` in the neighbouring fixtures: an isolation guard
    that silently degraded after a rename would restore this defect invisibly,
    which is the one failure mode it exists to prevent.

    ``_hook_tasks``, ``_setup_tasks`` and ``_drivers`` are reference hygiene
    rather than behavioural guards — the owning loop cancels its pending tasks
    before closing, and ``drain_hooks`` sync-discards done ones — but they are
    reset with the rest so no container of this class is left accumulating for
    the life of the worker."""
    import authz_grants as _ag
    import verdict_broker as _vb

    broker = _vb.BROKER
    challenges = _ag.CHALLENGES

    def _reset() -> None:
        broker._live.clear()
        broker._retired.clear()
        broker._hook_tasks.clear()
        broker._setup_tasks.clear()
        challenges._entries.clear()
        challenges._drivers.clear()

    _reset()
    try:
        yield
    finally:
        _reset()


@pytest.fixture(autouse=True)
def _reset_ask_validation_gates():
    try:
        from channels import channel_handlers as _ch
    except Exception:  # pragma: no cover — module import is universal in tests
        yield
        return
    _ch.ASK_GATES.clear()
    _ch._ASK_VALIDATION_OWNERS.clear()
    yield
    _ch.ASK_GATES.clear()
    _ch._ASK_VALIDATION_OWNERS.clear()


# ---------------------------------------------------------------------------
# event_reconcile published-routing isolation (Task 10, plugin-events).
# ---------------------------------------------------------------------------
# ``event_reconcile`` keeps a process-global published routing map that
# defaults to (and fails back to) ``event_spool.ROUTING_UNAVAILABLE`` — the
# fail-closed sentinel that licenses no destructive worker action (decision
# 26). Left alone across tests, a PRIOR test that reconciled events for real
# would leak its published map into a later, unrelated test — so this resets
# it before every test. Minor-2: it resets to the SENTINEL, the actual
# PRODUCTION default/fail-back value — not an authoritative ``{}``, which
# would make ``event_reconcile.get_routed()`` look authoritatively-routeless
# by default across the WHOLE suite (a global accidental authoritativeness
# that could mask a real "must treat as unavailable" bug anywhere a test
# happens to touch event-worker code without realizing it). A handful of
# health-report tests assert an EXACT issue list/count and would otherwise
# pick up the sentinel's own ``event_routing_unavailable`` row; those opt
# into an authoritative empty map explicitly via the ``event_routing_ok``
# fixture below, naming the need rather than inheriting it silently.
# ``tests/test_event_reconcile.py`` has its OWN autouse fixture that
# (redundantly, for clarity/independence from fixture ordering) also pins
# the sentinel per-test via ``monkeypatch``; the two compose cleanly — a
# conftest-level autouse fixture is instantiated before a same-scope one
# declared in the test module.
@pytest.fixture(autouse=True)
def _reset_event_routing(monkeypatch):
    try:
        import event_reconcile
        import event_spool
    except Exception:  # pragma: no cover — module import is universal in tests
        yield
        return
    monkeypatch.setattr(event_reconcile, "_routed", event_spool.ROUTING_UNAVAILABLE)
    yield


@pytest.fixture
def event_routing_ok(monkeypatch):
    """Opt-in fixture (Minor-2) for a test whose health-report assertion is
    an EXACT issue list/count that the sentinel's own
    ``event_routing_unavailable`` row would otherwise pollute — publishes an
    authoritative empty routing map instead of the sentinel
    ``_reset_event_routing`` defaults to."""
    import event_reconcile
    monkeypatch.setattr(event_reconcile, "_routed", {})
    yield
