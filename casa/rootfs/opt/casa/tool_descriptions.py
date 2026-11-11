"""The real descriptions of protected plugin tools, listed ahead of time.

An approval card is written by a model that is better for knowing what the tool
says about itself: its MCP description and input schema. Those come from the
plugin's own server, which is never started on the approval path. Instead, when a
role's plugins are resolved, :func:`schedule_fill` lists each protected-tool plugin's
tools in the background and keeps the protected ones here, under the artifact id
they were listed from, so a replacement artifact never reads another's entry.

The cache is a small in-memory dict. A miss is normal (not listed yet, a server that
would not list, a plugin with several servers) and the card simply goes without.
Nothing here ever raises into its caller.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# One listing starts the plugin's server, so a plugin that failed is not started
# again by every session build: it is tried again after this many seconds.
RETRY_AFTER_S = 300.0

_loop: "asyncio.AbstractEventLoop | None" = None
_cache: dict[tuple[str, str], dict] = {}
_listed: set[str] = set()          # artifact ids listed, or being listed
_failed_at: dict[str, float] = {}  # artifact id -> monotonic time of its last failure
_tasks: set["asyncio.Task"] = set()  # strong references while a fill is in flight


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Name the loop the fills run on: Casa's main loop, once at boot."""
    global _loop
    _loop = loop


def get(artifact_id: str, tool: str) -> "dict | None":
    """``{"description": str | None, "inputSchema": dict | None}`` for the protected
    tool *tool* (its bare name, as the plugin declares it) of that artifact, or None."""
    entry = _cache.get((artifact_id, tool))
    return dict(entry) if entry is not None else None


def schedule_fill(resolution) -> None:
    """List the protected tools of every plugin in *resolution* in the background.

    Safe from any thread (the protected-tool maps are built on worker threads): the
    work is handed to the bound loop and this never touches the loop itself. A no-op
    before :func:`bind_loop`. Once per artifact id; a failed listing is tried again
    only after ``RETRY_AFTER_S``."""
    loop = _loop
    if loop is None:
        return
    try:
        loop.call_soon_threadsafe(_start, resolution)
    except Exception:  # noqa: BLE001 — a closed loop must not reach a session build
        logger.info("tool descriptions: no fill scheduled, the loop is unavailable")


def _start(resolution) -> None:
    """On the loop: one task per resolved plugin that declares protected tools and has
    one stdio server to ask."""
    try:
        from plugin_continuations import sweep_server
        from plugin_store import StoreError, manifest_protected_tools
        for rp in getattr(resolution, "plugins", None) or []:
            aid = rp.artifact_id
            if aid in _listed:
                continue
            failed = _failed_at.get(aid)
            if failed is not None and time.monotonic() - failed < RETRY_AFTER_S:
                continue
            try:
                names = [e["name"] for e in manifest_protected_tools(rp.manifest)]
            except StoreError:
                continue
            if not names:
                continue
            server = sweep_server(rp)
            if server is None:
                logger.info("tool descriptions: %s (artifact_id=%s) not listed: it has no "
                            "single stdio server", rp.name, aid)
                continue
            _listed.add(aid)
            task = asyncio.get_running_loop().create_task(_fill(rp, server, names))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
    except Exception:  # noqa: BLE001 — never into the caller's loop callback
        logger.info("tool descriptions: scheduling failed", exc_info=True)


async def _fill(rp, server: dict, names: list[str]) -> None:
    import plugin_continuations
    aid = rp.artifact_id
    try:
        listed = await plugin_continuations.list_plugin_tools(server)
        wanted = set(names)
        found = {t["name"]: t for t in listed if t.get("name") in wanted}
        for name, t in found.items():
            _cache[(aid, name)] = {"description": t.get("description"),
                                   "inputSchema": t.get("inputSchema")}
        _failed_at.pop(aid, None)
    except Exception as exc:  # noqa: BLE001 — a miss, logged; the card goes without
        _listed.discard(aid)
        _failed_at[aid] = time.monotonic()
        logger.info("tool descriptions: listing %s (artifact_id=%s) failed: %s: %s",
                    rp.name, aid, type(exc).__name__, exc)


def _reset_for_tests() -> None:
    global _loop
    _loop = None
    _cache.clear()
    _listed.clear()
    _failed_at.clear()
    _tasks.clear()
