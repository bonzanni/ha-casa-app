"""The operator's replies to a running task reach its live session, in order.

Pending queues; attaching flushes the queue, oldest first, then later replies
are written directly; an ended (or never opened) task answers ``"ended"``.
The client is a recording fake of the SDK client's ``query``; the write's
payload is the background job's own (``task_input.write``).
"""
from __future__ import annotations

import asyncio

import pytest

import task_input

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class _Client:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.written: list[dict] = []
        self.fail_on = fail_on

    async def query(self, stream) -> None:
        async for payload in stream:
            if payload["message"]["content"] == self.fail_on:
                raise RuntimeError("the CLI went away")
            self.written.append(payload)

    def texts(self) -> list[str]:
        return [p["message"]["content"] for p in self.written]


@pytest.fixture(autouse=True)
def _fresh():
    task_input.reset()
    yield
    task_input.reset()


async def _settle(client: _Client, n: int) -> None:
    for _ in range(200):
        if len(client.written) >= n:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"wrote {client.texts()}, expected {n}")


async def test_pending_queues_then_attach_flushes_in_order():
    task_input.open_pending("t1")
    assert task_input.submit("t1", "first") == "queued"
    assert task_input.submit("t1", "second") == "queued"
    assert task_input.state("t1") == "pending"
    client = _Client()
    task_input.attach("t1", client)
    assert task_input.state("t1") == "live"
    await _settle(client, 2)
    assert client.texts() == ["first", "second"]


async def test_live_replies_are_written_directly_after_the_flush():
    task_input.open_pending("t1")
    task_input.submit("t1", "queued one")
    client = _Client()
    task_input.attach("t1", client)
    assert task_input.submit("t1", "live one") == "written"
    await _settle(client, 2)
    assert client.texts() == ["queued one", "live one"]


async def test_each_write_is_one_stamped_user_message():
    task_input.open_pending("t1")
    client = _Client()
    task_input.attach("t1", client)
    task_input.submit("t1", "hello")
    await _settle(client, 1)
    [payload] = client.written
    assert payload["type"] == "user"
    assert payload["message"] == {"role": "user", "content": "hello"}
    assert isinstance(payload["uuid"], str) and payload["uuid"]


async def test_pending_is_never_ended():
    task_input.open_pending("t1")
    for _ in range(3):
        assert task_input.submit("t1", "x") == "queued"
    assert task_input.state("t1") == "pending"


async def test_closed_and_unknown_tasks_are_ended():
    task_input.open_pending("t1")
    task_input.close("t1")
    assert task_input.submit("t1", "late") == "ended"
    assert task_input.submit("never-opened", "x") == "ended"
    assert task_input.state("t1") == "ended"
    task_input.attach("t1", _Client())           # nothing to attach to
    assert task_input.state("t1") == "ended"


async def test_close_drops_what_was_never_written():
    task_input.open_pending("t1")
    task_input.submit("t1", "unwritten")
    task_input.close("t1")
    client = _Client()
    task_input.attach("t1", client)
    await asyncio.sleep(0)
    assert client.written == []


async def test_a_failed_write_does_not_stop_the_next():
    task_input.open_pending("t1")
    client = _Client(fail_on="bad")
    task_input.attach("t1", client)
    task_input.submit("t1", "bad")
    task_input.submit("t1", "good")
    await _settle(client, 1)
    assert client.texts() == ["good"]


async def test_reopening_keeps_the_queue():
    task_input.open_pending("t1")
    task_input.submit("t1", "kept")
    task_input.open_pending("t1")
    client = _Client()
    task_input.attach("t1", client)
    await _settle(client, 1)
    assert client.texts() == ["kept"]


async def test_close_lets_the_worker_return():
    task_input.open_pending("t1")
    task_input.attach("t1", _Client())
    worker = task_input._destinations["t1"].worker
    task_input.close("t1")
    await asyncio.wait_for(worker, 1)
    assert worker.done() and not worker.cancelled()


async def test_the_operators_words_outlive_the_input_and_are_taken_once():
    task_input.open_pending("t1")
    task_input.submit("t1", "[replying to …] only Lidl", said="only Lidl")
    task_input.close("t1")              # the reply is dropped unwritten, the words stay
    assert task_input.take_said("t1") == ["only Lidl"]
    assert task_input.take_said("t1") == []
    assert task_input.take_said("never-opened") == []


async def test_words_are_kept_per_task_and_without_said_nothing_is_recorded():
    task_input.open_pending("t1")
    task_input.open_pending("t2")
    task_input.submit("t1", "a", said="for one")
    task_input.submit("t2", "b", said="for two")
    task_input.submit("t2", "c")
    task_input.submit("t2", "d", said="   ")
    assert task_input.take_said("t1") == ["for one"]
    assert task_input.take_said("t2") == ["for two"]


async def test_words_are_bounded():
    for i in range(task_input.SAID_TASKS + 1):
        task_input.open_pending(f"t{i}")
        task_input.submit(f"t{i}", "x", said=f"words {i}")
    assert task_input.take_said("t0") == []
    assert task_input.take_said("t1") == ["words 1"]
    assert task_input.take_said(f"t{task_input.SAID_TASKS}") == [
        f"words {task_input.SAID_TASKS}"]

    task_input.open_pending("many")
    for i in range(task_input.SAID_PER_TASK + 1):
        task_input.submit("many", "x", said=f"entry {i}")
    assert task_input.take_said("many") == [
        f"entry {i}" for i in range(1, task_input.SAID_PER_TASK + 1)]

    task_input.open_pending("long")
    task_input.submit("long", "x", said="word\n  " + "a" * 400)
    [entry] = task_input.take_said("long")
    assert len(entry) == task_input.SAID_CHARS
    assert entry.startswith("word a") and entry.endswith("…")


async def test_an_ended_submit_records_nothing():
    assert task_input.submit("never-opened", "x", said="only Lidl") == "ended"
    task_input.open_pending("t1")
    task_input.close("t1")
    assert task_input.submit("t1", "x", said="only Lidl") == "ended"
    assert task_input.take_said("never-opened") == []
    assert task_input.take_said("t1") == []


async def test_reset_forgets_the_words():
    task_input.open_pending("t1")
    task_input.submit("t1", "x", said="only Lidl")
    task_input.reset()
    assert task_input.take_said("t1") == []
