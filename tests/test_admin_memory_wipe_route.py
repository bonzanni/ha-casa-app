"""POST /admin/memory/wipe (the casactl terminal door). Root gating itself is
the shared peercred middleware, covered by test_admin_peercred_gate.py; here:
the explicit confirm, and that consent records the intent, replies, and asks
for the app restart at once (INV-MEM-038)."""
from __future__ import annotations

import asyncio

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import supervisor_restart
from memory import maintenance

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def supervisor(monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, "INTENT_PATH",
                        str(tmp_path / "memory" / "maintenance.json"))
    monkeypatch.setattr(maintenance, "restart_accepted_at", None)
    monkeypatch.setattr(maintenance, "RESTART_GRACE_S", 3600.0)
    state = {"answer": {"supervisor_status": 200}, "calls": 0}

    async def answer():
        state["calls"] += 1
        return state["answer"]

    monkeypatch.setattr(supervisor_restart, "request_app_restart", answer)
    yield state
    maintenance.configure(notify=None)
    for task in (maintenance._expiry, maintenance._request):
        if task is not None:
            task.cancel()


def _make_app() -> web.Application:
    from internal_handlers import build_admin_memory_wipe_handler
    app = web.Application()
    app.router.add_post(
        "/admin/memory/wipe", build_admin_memory_wipe_handler(),
    )
    return app


async def test_requires_explicit_confirm(supervisor) -> None:
    async with TestClient(TestServer(_make_app())) as client:
        for body in ({}, {"confirm": False}, {"confirm": "yes"}, None):
            resp = await client.post("/admin/memory/wipe", json=body)
            assert resp.status == 400
            assert (await resp.json())["kind"] == "confirm_required"
    assert maintenance.read_intent() is None and supervisor["calls"] == 0


async def test_consent_records_the_intent_and_requests_the_restart(
        supervisor) -> None:
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/admin/memory/wipe", json={"confirm": True})
        body = await resp.json()
    assert resp.status == 200
    assert body == {"status": "ok", "state": "written",
                    "message": maintenance.FORGETTING_LINE}
    intent = maintenance.read_intent()
    assert (intent["kind"], intent["attempts"]) == ("wipe", [])
    assert supervisor["calls"] == 1
    assert maintenance.restart_pending()


async def test_a_failed_restart_request_says_so_and_keeps_the_intent(
        supervisor) -> None:
    """The terminal is answered before the Supervisor is (it answers only
    after stopping the container); a refusal that comes back is told on
    Telegram."""
    supervisor["answer"] = {"supervisor_status": 502}
    told: list[str] = []

    async def telegram(line):
        told.append(line)

    maintenance.configure(notify=telegram)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/admin/memory/wipe", json={"confirm": True})
        body = await resp.json()
    assert resp.status == 200
    assert body["message"] == maintenance.FORGETTING_LINE
    await asyncio.wait_for(maintenance._request, 2)
    assert told == [maintenance.RESTART_FAILED_LINE]
    assert maintenance.read_intent()["kind"] == "wipe"
    assert not maintenance.restart_pending()


async def test_a_second_request_answers_already_scheduled(supervisor) -> None:
    async with TestClient(TestServer(_make_app())) as client:
        await client.post("/admin/memory/wipe", json={"confirm": True})
        resp = await client.post("/admin/memory/wipe", json={"confirm": True})
        body = await resp.json()
    await asyncio.wait_for(maintenance._request, 2)
    assert (body["state"], body["message"]) == (
        "already", maintenance.ALREADY_SCHEDULED_LINE)
    assert supervisor["calls"] == 2


async def test_a_pending_restore_is_not_replaced(supervisor) -> None:
    maintenance.write_intent({"kind": "restore", "requested_at": "t",
                              "attempts": [], "notified_container_ids": []})
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/admin/memory/wipe", json={"confirm": True})
        body = await resp.json()
    assert resp.status == 409 and body["kind"] == "restore_pending"
    assert maintenance.read_intent()["kind"] == "restore"
    assert supervisor["calls"] == 0


async def test_an_intent_that_cannot_be_written_is_reported(
        supervisor, monkeypatch) -> None:
    def refuse(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(maintenance, "write_intent", refuse)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/admin/memory/wipe", json={"confirm": True})
        body = await resp.json()
    assert resp.status == 500 and body["kind"] == "not_scheduled"
    assert "nothing was deleted" in body["message"]
    assert supervisor["calls"] == 0
    assert json.dumps(body).count("Forgetting") == 0


async def test_a_failure_before_telegram_is_wired_is_still_told(
        supervisor, monkeypatch) -> None:
    """A wipe asked for while the channels are still starting (no notifier
    yet) that the Supervisor refuses is told with one raw Bot API line."""
    supervisor["answer"] = {"supervisor_status": 503}
    raw: list[str] = []

    async def bot_api(line):
        raw.append(line)
        return True

    monkeypatch.setattr(maintenance, "send_operator_line", bot_api)
    maintenance.configure(notify=None)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/admin/memory/wipe", json={"confirm": True})
    assert resp.status == 200
    await asyncio.wait_for(maintenance._request, 2)
    assert raw == [maintenance.RESTART_FAILED_LINE]


async def test_a_notifier_that_cannot_send_falls_back_to_the_raw_line(
        supervisor, monkeypatch) -> None:
    """Telegram configured but not ready (its notifier answers False): the
    refusal still reaches the operator through one raw Bot API line."""
    supervisor["answer"] = {"supervisor_status": 503}
    raw: list[str] = []
    unready: list[str] = []

    async def bot_api(line):
        raw.append(line)
        return True

    async def not_ready(line):
        unready.append(line)
        return False

    monkeypatch.setattr(maintenance, "send_operator_line", bot_api)
    maintenance.configure(notify=not_ready)
    async with TestClient(TestServer(_make_app())) as client:
        await client.post("/admin/memory/wipe", json={"confirm": True})
    await asyncio.wait_for(maintenance._request, 2)
    assert unready == raw == [maintenance.RESTART_FAILED_LINE]
