"""The wipe's doors in casa-main: the restart request, the consent tap's
update confirmed before it (Q20), the courtesy flag and its two readers, and
what happens when no restart comes (§14 tests 8 and 9c2).

The window between consent and the restart has no admission guarantee: the
flag is a message nicety read on the operator DM path and the
engagement-topic path only, and nothing else in Casa reads the intent."""
from __future__ import annotations

import ast
import asyncio
import types
from pathlib import Path

import pytest

import supervisor_restart
from memory import maintenance

pytestmark = [pytest.mark.unit]

CODE = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"


@pytest.fixture(autouse=True)
def supervisor(monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, "INTENT_PATH",
                        str(tmp_path / "memory" / "maintenance.json"))
    monkeypatch.setattr(maintenance, "restart_accepted_at", None)
    monkeypatch.setattr(maintenance, "_resume_updates", None)
    monkeypatch.setattr(maintenance, "_notify", None)
    state = {"answers": [], "calls": 0}

    async def answer():
        state["calls"] += 1
        return (state["answers"].pop(0) if state["answers"]
                else {"supervisor_status": 200})

    monkeypatch.setattr(supervisor_restart, "request_app_restart", answer)
    yield state
    for task in (maintenance._expiry, maintenance._request):
        if task is not None:
            task.cancel()


async def _answered(**kw) -> bool:
    """Request the restart and wait for the Supervisor's answer, which in
    production arrives only after the container has stopped; whether the
    courtesy flag survived it."""
    await maintenance.request_restart(**kw)
    await asyncio.wait_for(maintenance._request, 2)
    return maintenance.restart_accepted_at is not None


class Lines:
    def __init__(self):
        self.sent: list[str] = []

    async def __call__(self, text):
        self.sent.append(text)


# --- the restart request ---------------------------------------------------------

async def test_the_flag_is_set_at_once_and_does_not_wait_for_the_answer(
        supervisor, monkeypatch):
    """The Supervisor answers a restart only after it has stopped this
    container, so the request returns before the answer and the courtesy
    flag is already set."""
    never = asyncio.Event()

    async def held():
        await never.wait()
        return {"supervisor_status": 200}

    monkeypatch.setattr(supervisor_restart, "request_app_restart", held)
    await asyncio.wait_for(maintenance.request_restart(), 1)
    assert maintenance.restart_pending()
    assert not maintenance._request.done()


async def test_a_failed_answer_clears_the_flag_and_says_so(supervisor):
    say = Lines()
    for answer in ({"status": "error", "kind": "no_supervisor_token"},
                   {"status": "error", "kind": "supervisor_error",
                    "message": "boom"},
                   {"supervisor_status": 500}, {"supervisor_status": 401}):
        supervisor["answers"].append(answer)
        assert await _answered(notify=say) is False
        assert maintenance.restart_accepted_at is None
    assert say.sent == [maintenance.RESTART_FAILED_LINE] * 4
    assert await _answered(notify=say) is True
    assert maintenance.restart_pending()
    assert len(say.sent) == 4


async def test_a_failed_request_resumes_the_paused_updates(supervisor):
    order: list[str] = []

    async def pause():
        order.append("pause")
        return True

    async def resume():
        order.append("resume")

    supervisor["answers"].append({"supervisor_status": 503})
    say = Lines()
    assert not await _answered(
        notify=say, pause_updates=pause, resume_updates=resume)
    assert order == ["pause", "resume"]
    assert say.sent == [maintenance.RESTART_FAILED_LINE]


async def test_an_accepted_restart_that_never_comes_tells_once_and_clears(
        supervisor, monkeypatch):
    monkeypatch.setattr(maintenance, "RESTART_GRACE_S", 0.05)
    said, resumed = Lines(), []
    maintenance.configure(notify=said)

    async def pause():
        return True

    async def resume():
        resumed.append(1)

    assert await _answered(pause_updates=pause,
                                             resume_updates=resume)
    assert maintenance.restart_pending()
    await asyncio.wait_for(maintenance._expiry, 2)
    assert not maintenance.restart_pending()
    assert maintenance.restart_accepted_at is None
    assert said.sent == [maintenance.RESTART_FAILED_LINE]
    assert resumed == [1]
    # the intent was never touched by any of this
    assert maintenance.read_intent() is None


async def test_a_second_acceptance_replaces_the_first_grace(supervisor,
                                                            monkeypatch):
    monkeypatch.setattr(maintenance, "RESTART_GRACE_S", 0.2)
    said = Lines()
    maintenance.configure(notify=said)
    assert await _answered()
    first = maintenance._expiry
    await asyncio.sleep(0.1)
    assert await _answered()
    await asyncio.sleep(0.15)
    assert first.cancelled() or first.done()
    assert maintenance.restart_pending() and said.sent == []
    await asyncio.wait_for(maintenance._expiry, 2)
    assert said.sent == [maintenance.RESTART_FAILED_LINE]


async def test_the_three_doors_reply_before_they_request_the_restart(
        supervisor):
    order: list[str] = []

    async def reply(line):
        order.append(f"reply:{line}")

    async def pause():
        order.append(f"pause@{supervisor['calls']}")
        return True

    assert await maintenance.request_wipe(reply=reply,
                                          pause_updates=pause) == "written"
    await asyncio.wait_for(maintenance._request, 2)
    assert order == [f"reply:{maintenance.FORGETTING_LINE}", "pause@0"]
    assert supervisor["calls"] == 1
    assert maintenance.read_intent()["attempts"] == []


# --- Q20: the consent tap is confirmed before the restart ---------------------------

_Q20_CHILD = """
import asyncio, json, types
from telegram import Update
from telegram.ext import Updater
from channels.telegram import TelegramChannel

class ServerBot:
    # A stand-in Bot API server: getUpdates returns every pending update at
    # or above the offset, and an offset confirms (drops) everything below
    # it, as Telegram does.
    def __init__(self, ids):
        self.pending = [Update(update_id=i) for i in ids]
        self.offsets = []
    async def initialize(self): pass
    async def shutdown(self): pass
    async def delete_webhook(self, **kw): return True
    async def get_updates(self, offset=None, timeout=None, **kw):
        self.offsets.append(offset)
        if offset is not None:
            self.pending = [u for u in self.pending if u.update_id >= offset]
        if not self.pending:
            await asyncio.sleep(0.01)
        return list(self.pending)

async def main():
    bot = ServerBot([41])                    # the consent tap
    queue = asyncio.Queue()
    updater = Updater(bot=bot, update_queue=queue)
    await updater.initialize()
    await updater.start_polling(poll_interval=0.0)
    tap = await asyncio.wait_for(queue.get(), 5)
    channel = TelegramChannel(known_bot_id=111, bot_token="T", chat_id="500",
                              default_agent="assistant", bus=None)
    channel._app = types.SimpleNamespace(updater=updater, bot=None)
    paused = await channel.pause_updates_for_restart()
    # after the restart, a fresh poller asks from the start
    fresh = await bot.get_updates(offset=None, timeout=0)
    print(json.dumps({"tap": tap.update_id, "paused": paused,
                      "running": updater.running,
                      "fresh": [u.update_id for u in fresh],
                      "confirmed": 42 in bot.offsets}))
    await updater.shutdown()

asyncio.run(main())
"""


def test_pausing_updates_confirms_the_consent_tap():
    """Q20, with the real PTB updater (in a fresh interpreter: the suite
    stubs ``telegram``): stopping it confirms the tap's update, so a poller
    started after the restart never receives it again."""
    import json
    import memory_wipe_support as w

    child = w.run_child(_Q20_CHILD)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout.strip().splitlines()[-1]) == {
        "tap": 41, "paused": True, "running": False, "fresh": [],
        "confirmed": True}


async def test_webhook_mode_and_a_stopped_updater_pause_nothing():
    from channels.telegram import TelegramChannel

    channel = TelegramChannel(known_bot_id=111, bot_token="T", chat_id="500",
                              default_agent="assistant", bus=None,
                              webhook_url="https://casa.example.com")
    channel._app = types.SimpleNamespace(
        updater=types.SimpleNamespace(running=True), bot=None)
    assert await channel.pause_updates_for_restart() is False
    polling = TelegramChannel(known_bot_id=111, bot_token="T", chat_id="500",
                              default_agent="assistant", bus=None)
    polling._app = types.SimpleNamespace(
        updater=types.SimpleNamespace(running=False), bot=None)
    assert await polling.pause_updates_for_restart() is False


# --- the courtesy line on the two operator paths ------------------------------------

class _Bot:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send_message(self, **kw):
        self.sent.append((kw.get("chat_id"), kw.get("text")))
        return types.SimpleNamespace(message_id=1)


def _dm_channel():
    from bus import MessageBus
    from channels.telegram import TelegramChannel

    async def _noop(_m):
        return None

    bus = MessageBus()
    bus.register("assistant", _noop)
    channel = TelegramChannel(known_bot_id=111, bot_token="T", chat_id="42",
                              default_agent="assistant", bus=bus)
    channel._start_typing = lambda *a, **k: None
    channel._app = types.SimpleNamespace(bot=_Bot())
    return channel, bus


def _dm_update(text="hello"):
    user = types.SimpleNamespace(first_name="Op", id=42)
    message = types.SimpleNamespace(text=text, message_id=9)
    chat = types.SimpleNamespace(id="42")
    return types.SimpleNamespace(message=message, effective_chat=chat,
                                 effective_user=user)


def _queued(bus):
    q = bus.queues.get("assistant")
    return 0 if q is None else q.qsize()


async def test_an_operator_dm_after_acceptance_gets_the_line_and_no_turn(
        supervisor):
    channel, bus = _dm_channel()
    await channel._handle(_dm_update(), None)
    assert _queued(bus) == 1                       # an ordinary turn
    assert await _answered()
    await channel._handle(_dm_update("are you there"), None)
    assert _queued(bus) == 1                       # no second turn
    assert channel._app.bot.sent == [("42", maintenance.COURTESY_LINE)]


async def test_a_dm_after_a_failed_request_is_an_ordinary_turn(supervisor):
    channel, bus = _dm_channel()
    supervisor["answers"].append({"supervisor_status": 500})
    assert not await _answered(notify=Lines())
    await channel._handle(_dm_update(), None)
    assert _queued(bus) == 1
    assert channel._app.bot.sent == []


async def test_a_reply_into_a_job_gets_the_line_and_no_turn(supervisor):
    from channels.telegram import TelegramChannel, _NoticeTarget

    channel = TelegramChannel(known_bot_id=111, bot_token="T", chat_id="42",
                              default_agent="assistant", bus=None)
    notices, turns = [], []

    async def reply(chat_id, message_id, text):
        notices.append(text)

    class _Driver:
        def submit_operator(self, rec, text, **kw):
            turns.append(text)

    channel._send_dm_reply = reply
    channel._engagement_driver = _Driver()
    rec = types.SimpleNamespace(id="e" * 32, origin={})
    target = _NoticeTarget(address=channel.address(42, 7))

    assert await channel._deliver_to_job(rec, "first",
                                         notice_target=target) is True
    assert (turns, notices) == (["first"], [])
    assert await _answered()
    assert await channel._deliver_to_job(rec, "second",
                                         notice_target=target) is False
    assert turns == ["first"]
    assert notices == [maintenance.COURTESY_LINE]
    # a system turn into the engagement is not the operator's message
    assert await channel.deliver_system_turn(rec, "continue") is True
    assert turns == ["first", "continue"]
    assert notices == [maintenance.COURTESY_LINE]


# --- nothing else reads the flag or the intent -------------------------------------

#: Every function outside ``memory/maintenance.py`` that may name the flag
#: or the intent's reader: the two courtesy readers, boot, and the doors.
ALLOWED = {
    ("channels/telegram.py", "_dispatch_dm_turn"): {"restart_pending"},
    ("channels/telegram.py", "_deliver_to_job"): {"restart_pending"},
    ("casa_core.py", "main"): {"read_intent"},
    # the size meter reports whether one is pending; it refuses nothing
    ("memory/meter.py", "measure"): {"maintenance.json"},
    # a restore request is refused while a wipe or restore is pending (it
    # never replaces one); this refuses only the request, never a turn
    ("memory/restore.py", "_refuse_if_pending"): {"read_intent"},
}
WATCHED = {"restart_pending", "restart_accepted_at", "read_intent",
           "maintenance.json"}


def readers(code: Path) -> dict[tuple[str, str], set[str]]:
    found: dict[tuple[str, str], set[str]] = {}
    for path in sorted(code.rglob("*.py")):
        rel = path.relative_to(code).as_posix()
        if rel == "memory/maintenance.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = set()
            for node in ast.walk(fn):
                name = (getattr(node, "attr", None) or getattr(node, "id", None)
                        or (node.value if isinstance(node, ast.Constant)
                            and isinstance(node.value, str) else None))
                if isinstance(name, str):
                    names |= {w for w in WATCHED if w == name
                              or (w == "maintenance.json" and w in name)}
            if names:
                found.setdefault((rel, fn.name), set()).update(names)
    # A nested function's names also count for the one enclosing it; keep
    # only the outermost reader of each file.
    return found


def test_nothing_but_the_two_courtesy_paths_and_boot_reads_the_flag():
    found = readers(CODE)
    assert found == ALLOWED


def test_the_reader_scan_reports_a_new_reader(tmp_path):
    (tmp_path / "reminders.py").write_text(
        "from memory import maintenance\n"
        "def fire():\n"
        "    if maintenance.restart_pending():\n"
        "        return\n")
    assert readers(tmp_path) == {("reminders.py", "fire"): {"restart_pending"}}


async def test_a_request_during_the_expiry_recovery_does_not_cancel_it(
        supervisor, monkeypatch):
    """The grace ran out and intake is being resumed when a new request
    comes in: the new request starts its own grace and the resume still
    completes."""
    monkeypatch.setattr(maintenance, "RESTART_GRACE_S", 0.05)
    maintenance.configure(notify=Lines())
    resuming, release, resumed = asyncio.Event(), asyncio.Event(), []

    async def pause():
        return True

    async def resume():
        resuming.set()
        await release.wait()
        resumed.append(1)

    await maintenance.request_restart(pause_updates=pause,
                                      resume_updates=resume)
    expiry = maintenance._expiry
    await asyncio.wait_for(resuming.wait(), 2)
    await maintenance.request_restart()
    release.set()
    await asyncio.wait_for(expiry, 2)
    assert resumed == [1]
    assert maintenance.restart_pending()
