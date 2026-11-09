"""A session surface for the turn-scripted fake SDK clients.

The job driver writes each message as a stream-json line carrying a uuid
(``client.query(<async iterable of dicts>)``) and reads one continuous
stream (``client.receive_messages()``). Many older fakes script one turn per
``receive_response()`` call after a string ``query()``. ``TurnScriptedSession``
gives such a fake the session surface by modelling the CLI's timing for the
simplest case: every written message runs as its own turn, and the CLI echoes
it (a ``UserMessage`` with its uuid, as ``--replay-user-messages`` does at
take-up) before that turn's scripted frames. A scripted turn with no
``ResultMessage`` is a CLI that ended mid-turn: the stream ends there.

Put it FIRST in the fake's bases; a fake that defines its own ``query``
records through ``record_query`` instead of overriding ``query``."""

from __future__ import annotations

import asyncio

from claude_agent_sdk import UserMessage

_END = object()


class TurnScriptedSession:
    def _session_turns(self) -> asyncio.Queue:
        q = self.__dict__.get("_turn_queue")
        if q is None:
            q = self.__dict__["_turn_queue"] = asyncio.Queue()
        return q

    async def record_query(self, text: str) -> None:
        """What the fake does with one written text (its old ``query``)."""
        parent = super()
        if hasattr(parent, "query"):
            await parent.query(text)

    async def query(self, prompt) -> None:
        if isinstance(prompt, str):
            await self.record_query(prompt)
            return
        async for line in prompt:
            await self.record_query(line["message"]["content"])
            self._session_turns().put_nowait(line)

    async def receive_messages(self):
        turns = self._session_turns()
        while True:
            line = await turns.get()
            if line is _END:
                return
            yield UserMessage(content=line["message"]["content"], uuid=line["uuid"])
            finished = False
            async for frame in self.receive_response():
                finished = finished or type(frame).__name__ == "ResultMessage"
                yield frame
            if not finished:
                return          # a turn with no ResultMessage: the CLI ended

    async def disconnect(self) -> None:
        self._session_turns().put_nowait(_END)
        parent = super()
        if hasattr(parent, "close"):
            await parent.close()
        elif hasattr(parent, "__aexit__"):
            await parent.__aexit__(None, None, None)


def attach_session(driver, rec, client):
    """Register *client* as *rec*'s live job session on *driver* — what
    ``InCasaDriver.open`` does once the client is entered — starting its
    reader and the job's delivery worker. For rigs that inject a fake client
    instead of opening one. Needs a running loop."""
    from drivers.in_casa_driver import _EngagementTurn, _JobSession

    session = _JobSession(client=client, ctx=client, holder=_EngagementTurn())
    driver._sessions[rec.id] = session
    session.reader = driver._spawn(driver._read(session, rec))
    driver._ensure_worker(rec)
    return session
