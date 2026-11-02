"""POST /admin/memory/wipe (the casactl terminal door). Root gating itself is
the shared peercred middleware, covered by test_admin_peercred_gate.py; here:
the explicit confirm, and that consent records the intent, replies, and asks
for the app restart at once (INV-MEM-038)."""
from __future__ import annotations

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
    if maintenance._expiry is not None:
        maintenance._expiry.cancel()


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
    supervisor["answer"] = {"supervisor_status": 502}
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/admin/memory/wipe", json={"confirm": True})
        body = await resp.json()
    assert resp.status == 200
    assert body["message"] == (maintenance.FORGETTING_LINE + " "
                               + maintenance.RESTART_FAILED_LINE)
    assert maintenance.read_intent()["kind"] == "wipe"
    assert not maintenance.restart_pending()


async def test_a_second_request_answers_already_scheduled(supervisor) -> None:
    async with TestClient(TestServer(_make_app())) as client:
        await client.post("/admin/memory/wipe", json={"confirm": True})
        resp = await client.post("/admin/memory/wipe", json={"confirm": True})
        body = await resp.json()
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
