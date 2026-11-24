"""A fake of the OpenAI live-session API, served on localhost.

``FakeOpenAI`` is an ``aiohttp.web`` app under ``aiohttp.test_utils.TestServer``
that speaks the same REST and WebSocket shapes as the real service, so the
real client code runs against it:

- ``POST /v1/live/sessions/{sid}/{accept|reject|refer|hangup}``: 200, empty body.
- ``GET /v1/live/sessions/{sid}/attach``: the control WebSocket.
- ``POST /v1/responses``: answers from ``responses_queue``.

Use it as ``async with FakeOpenAI() as fake:`` and pass ``fake.base_url`` to
the client. Controls and records are plain attributes; see ``__init__``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

LIVE_VERBS = ("accept", "reject", "refer", "hangup")


def function_call(name: str, arguments: dict | str, call_id: str = "call_1") -> dict:
    """A ``function_call`` output item; *arguments* is sent as a JSON string."""
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return {"type": "function_call", "call_id": call_id, "name": name,
            "arguments": arguments}


def message(text: str) -> dict:
    """An assistant ``message`` output item holding one ``output_text`` part."""
    return {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}


def tool_call_event(name: str, arguments: dict | str, call_id: str = "call_1") -> dict:
    """The ``response.event`` that carries a delegated tool call on the socket."""
    return {"type": "response.event",
            "event": {"type": "response.output_item.done",
                      "item": function_call(name, arguments, call_id)}}


class FakeOpenAI:
    def __init__(self) -> None:
        # Records.
        #: (verb, sid, body) per REST call; ``responses`` calls have sid "".
        self.requests: list[tuple[str, str, dict]] = []
        #: The Authorization header of every request and attach, in order.
        self.auth: list[str] = []
        #: Client events received on any control socket, in order.
        self.ws_sent: list[dict] = []
        #: Attach attempts, refused ones included.
        self.attach_count = 0

        # Controls.
        #: A non-200 status per verb ("accept", ..., "responses").
        self.status_for: dict[str, int] = {}
        #: Verbs that never answer until the fake shuts down.
        self.hang_for: set[str] = set()
        #: Verbs whose answer waits until their event is set (or shutdown).
        self.hold_for: dict[str, asyncio.Event] = {}
        #: What successive ``/v1/responses`` calls answer, consumed in order:
        #: a list of output items -> 200 ``{"output": items}``; an Exception
        #: -> 500 with an error body; a dict -> that whole body as is;
        #: ``("wait", event, entry)`` -> entry once the asyncio.Event is set.
        #: An empty queue answers ``{"output": []}``.
        self.responses_queue: list[Any] = []
        #: True refuses the attach upgrade with 403; "hang" never completes it.
        self.attach_refuse: bool | str = False

        self._sockets: dict[str, web.WebSocketResponse] = {}
        self._attached: dict[str, asyncio.Event] = {}
        self._release = asyncio.Event()
        self._server: TestServer | None = None

        app = web.Application()
        app.router.add_post("/v1/live/sessions/{sid}/{verb}", self._live)
        app.router.add_get("/v1/live/sessions/{sid}/attach", self._attach)
        app.router.add_post("/v1/responses", self._responses)
        self.app = app

    # Lifecycle.

    async def start(self) -> None:
        self._server = TestServer(self.app)
        await self._server.start_server()

    async def stop(self) -> None:
        self._release.set()
        for ws in list(self._sockets.values()):
            if not ws.closed:
                await ws.close()
        if self._server is not None:
            await self._server.close()

    async def __aenter__(self) -> "FakeOpenAI":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    @property
    def base_url(self) -> str:
        assert self._server is not None, "the fake is not started"
        return str(self._server.make_url("")).rstrip("/")

    # Server-side actions on a control socket.

    async def _socket(self, sid: str) -> web.WebSocketResponse:
        event = self._attached.setdefault(sid, asyncio.Event())
        await asyncio.wait_for(event.wait(), 5)
        return self._sockets[sid]

    async def wait_sent(self, type_: str, count: int = 1, timeout: float = 5) -> list[dict]:
        """Wait until the client has sent *count* events of *type_*; return them.

        A client event still in flight when the fake closes the socket is lost
        (as it can be on the real service), so wait for it before closing."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            found = [e for e in self.ws_sent if e.get("type") == type_]
            if len(found) >= count:
                return found
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"the client sent {len(found)} {type_!r}, "
                                     f"expected {count}")
            await asyncio.sleep(0.01)

    async def emit(self, sid: str, event: dict) -> None:
        """Send a server event on *sid*'s socket (waits for the attach)."""
        await (await self._socket(sid)).send_json(event)

    async def close(self, sid: str, reason: str = "close_requested") -> None:
        """End the session the way the service does: ``session.closed``, then close."""
        ws = await self._socket(sid)
        self._forget(sid, ws)
        await ws.send_json({"type": "session.closed", "reason": reason})
        await ws.close()

    async def drop_ws(self, sid: str) -> None:
        """Close *sid*'s socket without sending ``session.closed``.

        A later ``emit``/``close`` on *sid* waits for the client to attach again."""
        ws = await self._socket(sid)
        self._forget(sid, ws)
        await ws.close()

    def _forget(self, sid: str, ws: web.WebSocketResponse) -> None:
        """Drop *ws* as *sid*'s socket, unless a newer attach replaced it."""
        if self._sockets.get(sid) is ws:
            del self._sockets[sid]
            self._attached[sid] = asyncio.Event()

    # Handlers.

    async def _hold(self, verb: str) -> None:
        if verb in self.hang_for:
            await self._release.wait()
        gate = self.hold_for.get(verb)
        if gate is not None:
            waits = [asyncio.ensure_future(gate.wait()),
                     asyncio.ensure_future(self._release.wait())]
            _, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()

    async def _live(self, request: web.Request) -> web.Response:
        verb, sid = request.match_info["verb"], request.match_info["sid"]
        if verb not in LIVE_VERBS:
            return web.json_response({"error": {"code": "not_found"}}, status=404)
        self.auth.append(request.headers.get("Authorization", ""))
        raw = await request.read()
        self.requests.append((verb, sid, json.loads(raw) if raw else {}))
        await self._hold(verb)
        status = self.status_for.get(verb, 200)
        if status != 200:
            return web.json_response(
                {"error": {"code": f"fake_{status}", "message": "fake error"}},
                status=status)
        return web.Response(status=200)

    async def _responses(self, request: web.Request) -> web.Response:
        self.auth.append(request.headers.get("Authorization", ""))
        self.requests.append(("responses", "", await request.json()))
        await self._hold("responses")
        status = self.status_for.get("responses", 200)
        if status != 200:
            return web.json_response(
                {"error": {"code": f"fake_{status}", "message": "fake error"}},
                status=status)
        entry: Any = self.responses_queue.pop(0) if self.responses_queue else []
        if isinstance(entry, tuple) and entry and entry[0] == "wait":
            _, gate, entry = entry
            waits = [asyncio.ensure_future(gate.wait()),
                     asyncio.ensure_future(self._release.wait())]
            _, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        if isinstance(entry, Exception):
            return web.json_response(
                {"error": {"code": "server_error", "message": str(entry)}}, status=500)
        if isinstance(entry, dict):
            return web.json_response(entry)
        return web.json_response({"id": "resp_fake", "output": entry})

    async def _attach(self, request: web.Request) -> web.StreamResponse:
        sid = request.match_info["sid"]
        self.attach_count += 1
        self.auth.append(request.headers.get("Authorization", ""))
        if self.attach_refuse == "hang":
            await self._release.wait()
            return web.Response(status=503)
        if self.attach_refuse:
            return web.json_response({"error": {"code": "forbidden"}}, status=403)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._sockets[sid] = ws
        self._attached.setdefault(sid, asyncio.Event()).set()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    self.ws_sent.append(json.loads(msg.data))
        finally:
            self._forget(sid, ws)
        return ws
