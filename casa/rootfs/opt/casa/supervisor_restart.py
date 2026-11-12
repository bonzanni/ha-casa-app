"""Ask the Home Assistant Supervisor to restart this app.

A full app restart (``POST /addons/self/restart``) stops the whole container
and starts a fresh one, so every container-start step runs again. The
Supervisor answers only after the restart has stopped this container, so a
caller sees an answer only when the request was refused, and the awaiting
task must not be one Casa's own stop waits for.

A leaf module: it imports nothing from the running Casa process, so the
memory package can use it as well as ``tools``.
"""
from __future__ import annotations

import os

RESTART_URL = "http://supervisor/addons/self/restart"


async def request_app_restart() -> dict:
    """POST the restart request. Returns ``{"supervisor_status": <code>}``
    when the Supervisor answered, or an error-shaped dict; never raises."""
    import aiohttp

    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return {
            "status": "error",
            "kind": "no_supervisor_token",
            "message": "SUPERVISOR_TOKEN not set - cannot restart addon",
        }
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.post(RESTART_URL) as resp:
                return {"supervisor_status": resp.status}
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        return {
            "status": "error",
            "kind": "supervisor_error",
            "message": str(exc),
        }


def accepted(result: dict) -> bool:
    """Whether *result* is the Supervisor's success answer."""
    status = result.get("supervisor_status")
    return isinstance(status, int) and 200 <= status < 300
