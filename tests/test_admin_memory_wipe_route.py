"""#411 — POST /admin/memory/wipe (the casactl terminal door). Root gating
itself is the shared peercred middleware, covered by
test_admin_peercred_gate.py; here: confirm gating, single-flight, and the
report passthrough."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import memory_wipe

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_wipe_state(monkeypatch):
    monkeypatch.setattr(memory_wipe, "_wipe_task", None)
    monkeypatch.setattr(memory_wipe, "_wipes_frozen", False)


def _make_app() -> web.Application:
    from internal_handlers import build_admin_memory_wipe_handler
    app = web.Application()
    app.router.add_post(
        "/admin/memory/wipe", build_admin_memory_wipe_handler(),
    )
    return app


def _bind_backends(monkeypatch):
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_session_registry", MagicMock(), raising=False)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", MagicMock(), raising=False)


async def test_requires_explicit_confirm(monkeypatch) -> None:
    _bind_backends(monkeypatch)
    app = _make_app()
    async with TestClient(TestServer(app)) as client:
        for body in ({}, {"confirm": False}, {"confirm": "yes"}, None):
            resp = await client.post("/admin/memory/wipe", json=body)
            assert resp.status == 400
            assert (await resp.json())["kind"] == "confirm_required"


async def test_happy_path_returns_report(monkeypatch) -> None:
    _bind_backends(monkeypatch)

    async def fake_wipe(**kwargs):
        return memory_wipe.WipeReport(
            spool_records_dropped=3, session_entries_dropped=2,
            bank_deleted=True,
        )

    monkeypatch.setattr(memory_wipe, "wipe_long_term_memory", fake_wipe)
    app = _make_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/admin/memory/wipe", json={"confirm": True})
        assert resp.status == 200
        payload = await resp.json()
        assert payload["status"] == "ok"
        assert payload["bank_deleted"] is True
        assert payload["spool_records_dropped"] == 3
        assert payload["session_entries_dropped"] == 2


async def test_second_wipe_409s_while_running(monkeypatch) -> None:
    import asyncio
    _bind_backends(monkeypatch)
    release = asyncio.Event()

    async def slow_wipe(**kwargs):
        await release.wait()
        return memory_wipe.WipeReport()

    monkeypatch.setattr(memory_wipe, "wipe_long_term_memory", slow_wipe)
    app = _make_app()
    async with TestClient(TestServer(app)) as client:
        first = asyncio.create_task(
            client.post("/admin/memory/wipe", json={"confirm": True}),
        )
        await asyncio.sleep(0.05)
        second = await client.post("/admin/memory/wipe", json={"confirm": True})
        assert second.status == 409
        assert (await second.json())["kind"] == "already_running"
        release.set()
        assert (await first).status == 200


async def test_abort_maps_to_503(monkeypatch) -> None:
    _bind_backends(monkeypatch)

    async def aborting_wipe(**kwargs):
        raise memory_wipe.WipeAborted("writers did not drain")

    monkeypatch.setattr(memory_wipe, "wipe_long_term_memory", aborting_wipe)
    app = _make_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/admin/memory/wipe", json={"confirm": True})
        assert resp.status == 503
        assert (await resp.json())["kind"] == "aborted"


async def test_uninitialized_backend_500s(monkeypatch) -> None:
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_session_registry", None, raising=False)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", None, raising=False)
    app = _make_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/admin/memory/wipe", json={"confirm": True})
        assert resp.status == 500
        assert (await resp.json())["kind"] == "not_initialized"
