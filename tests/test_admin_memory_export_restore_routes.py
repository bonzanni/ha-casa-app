"""POST /admin/memory/export and /admin/memory/restore (``casactl
memory-export`` and ``memory-restore``). Root gating is the shared peercred
middleware (test_admin_peercred_gate.py); here: the export publishes a
verified copy of the live store, and the restore is validated at once,
needs an explicit confirm, and on consent stages the export, records the
intent and asks for the app restart."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import memory_restore_support as r
import supervisor_restart
from memory import capture, export, maintenance, restore

pytestmark = [pytest.mark.unit]

CASACTL = (Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "usr"
           / "local" / "bin" / "casactl")


@pytest.fixture
def tree(monkeypatch, tmp_path):
    root = tmp_path / "root"
    monkeypatch.setattr(maintenance, "INTENT_PATH",
                        str(root / "data/memory/maintenance.json"))
    monkeypatch.setattr(maintenance, "restart_accepted_at", None)
    monkeypatch.setattr(maintenance, "RESTART_GRACE_S", 3600.0)
    real_stage = restore.stage
    monkeypatch.setattr(restore, "stage", lambda path, **kw: real_stage(
        path, **{**kw, "root": str(root)}))
    state = {"answer": {"supervisor_status": 200}, "calls": 0,
             "root": root, "store": r.store_at(root)}
    r.populate(state["store"])

    async def answer():
        state["calls"] += 1
        return state["answer"]

    monkeypatch.setattr(supervisor_restart, "request_app_restart", answer)
    capture.configure(store=state["store"])
    yield state
    capture.configure(store=None)
    if maintenance._expiry is not None:
        maintenance._expiry.cancel()


def _app() -> web.Application:
    from internal_handlers import (build_admin_memory_export_handler,
                                   build_admin_memory_restore_handler)
    app = web.Application()
    app.router.add_post("/admin/memory/export",
                        build_admin_memory_export_handler())
    app.router.add_post("/admin/memory/restore",
                        build_admin_memory_restore_handler())
    return app


async def _post(path: str, body) -> tuple[int, dict]:
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post(path, json=body)
        return resp.status, await resp.json()


async def test_export_publishes_a_verified_copy(tree) -> None:
    status, body = await _post("/admin/memory/export", {})
    assert status == 200 and body["status"] == "ok"
    assert body["counts"]["events"] == 4
    assert os.path.basename(body["path"]).startswith("export-")
    export.verify_files(body["path"], export.read_manifest(body["path"]))


async def test_export_of_an_unavailable_store_says_so(tree) -> None:
    capture.configure(store=None)
    status, body = await _post("/admin/memory/export", {})
    assert status == 503 and body["kind"] == "unavailable"


async def test_restore_needs_a_confirm_and_schedules_nothing_without(
        tree) -> None:
    _, made = await _post("/admin/memory/export", {})
    status, body = await _post("/admin/memory/restore",
                               {"path": made["path"]})
    assert status == 400 and body["status"] == "confirm_required"
    assert body["message"] == restore.CONFIRMATION
    assert r.nothing_scheduled(tree["root"]) and tree["calls"] == 0


async def test_a_refused_restore_names_the_reason(tree) -> None:
    status, body = await _post("/admin/memory/restore",
                               {"path": str(tree["root"] / "nowhere"),
                                "confirm": True})
    assert status == 409 and body["kind"] == "refused"
    assert body["message"].startswith("The restore was refused: ")
    assert body["message"].endswith("Nothing was scheduled.")
    assert r.nothing_scheduled(tree["root"]) and tree["calls"] == 0
    for bad in ({}, {"path": ""}, None):
        status, _ = await _post("/admin/memory/restore", bad)
        assert status == 400


async def test_consent_stages_records_the_intent_and_requests_the_restart(
        tree) -> None:
    _, made = await _post("/admin/memory/export", {})
    status, body = await _post("/admin/memory/restore",
                               {"path": os.path.basename(made["path"]),
                                "confirm": True})
    assert status == 200
    assert body == {"status": "ok", "state": "staged",
                    "message": restore.STAGED_LINE}
    intent = maintenance.read_intent(str(tree["root"]))
    assert (intent["kind"], intent["phase"]) == ("restore", "staged")
    assert tree["calls"] == 1 and maintenance.restart_pending()
    assert maintenance.courtesy_line() == maintenance.COURTESY_LINES["restore"]


async def test_a_failed_restart_request_says_the_restore_waits(tree) -> None:
    tree["answer"] = {"supervisor_status": 502}
    _, made = await _post("/admin/memory/export", {})
    _, body = await _post("/admin/memory/restore",
                          {"path": made["path"], "confirm": True})
    assert body["message"] == (restore.STAGED_LINE + " "
                               + maintenance.RESTART_FAILED_LINES["restore"])
    assert maintenance.read_intent(str(tree["root"]))["kind"] == "restore"


def test_casactl_lists_the_memory_export_and_restore_commands() -> None:
    out = subprocess.run([sys.executable, str(CASACTL), "--help"],
                         capture_output=True, text=True, check=True).stdout
    assert "memory-export" in out and "memory-restore" in out
    refused = subprocess.run(
        [sys.executable, str(CASACTL), "memory-restore"],
        capture_output=True, text=True)
    assert refused.returncode == 2 and "export" in refused.stderr
