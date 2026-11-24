"""The OpenAI live client, against the local fake of the service.

No network: every call goes to ``FakeOpenAI`` on localhost, which speaks the
service's REST and WebSocket shapes. The key and session ids are invented."""
from __future__ import annotations

import asyncio
import json
import socket
import time

import aiohttp
import pytest

from live_voice.api import ApiError, LiveApi
from phone_fakes import FakeOpenAI, function_call, message, tool_call_event

KEY = "sk-test-not-a-real-key"
SID = "live_fake_1"


@pytest.fixture
async def fake():
    async with FakeOpenAI() as server:
        yield server


@pytest.fixture
async def api(fake):
    async with aiohttp.ClientSession() as http:
        yield LiveApi(key=KEY, http=http, timeout_s=2.0, base_url=fake.base_url)


def test_base_defaults_to_the_public_host() -> None:
    plain = LiveApi(key=KEY, http=None)  # type: ignore[arg-type]
    eu = LiveApi(key=KEY, host="eu.api.openai.com", http=None)  # type: ignore[arg-type]
    assert plain.base == "https://api.openai.com"
    assert eu.base == "https://eu.api.openai.com"
    assert KEY not in repr(plain)


async def test_accept_posts_the_session_with_the_bearer_key(fake, api) -> None:
    session = {"type": "live", "model": "m", "instructions": "hi"}
    await api.accept(SID, session)
    assert fake.requests == [("accept", SID, {"session": session})]
    assert fake.auth == [f"Bearer {KEY}"]


async def test_reject_refer_hangup_bodies(fake, api) -> None:
    await api.reject(SID)
    await api.refer(SID, "tel:+15550100")
    await api.hangup(SID)
    assert fake.requests == [
        ("reject", SID, {"status_code": 486}),
        ("refer", SID, {"target_uri": "tel:+15550100"}),
        ("hangup", SID, {}),
    ]


async def test_refer_error_status_raises(fake, api) -> None:
    fake.status_for["refer"] = 500
    with pytest.raises(ApiError) as caught:
        await api.refer(SID, "tel:+15550100")
    assert caught.value.status == 500
    assert caught.value.code == "fake_500"
    assert KEY not in str(caught.value)


async def test_a_server_that_never_answers_times_out(fake) -> None:
    fake.hang_for.add("accept")
    async with aiohttp.ClientSession() as http:
        api = LiveApi(key=KEY, http=http, timeout_s=0.2, base_url=fake.base_url)
        started = time.monotonic()
        with pytest.raises(ApiError) as caught:
            await api.accept(SID, {"type": "live"})
    assert caught.value.status == 0
    assert time.monotonic() - started < 1.5


async def test_a_refused_connection_is_status_zero() -> None:
    with socket.socket() as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    async with aiohttp.ClientSession() as http:
        api = LiveApi(key=KEY, http=http, timeout_s=5.0,
                      base_url=f"http://127.0.0.1:{port}")
        with pytest.raises(ApiError) as caught:
            await api.hangup(SID)
    assert (caught.value.status, caught.value.code) == (0, "connection")


async def test_an_attach_that_never_completes_can_be_timed_out(fake, api) -> None:
    fake.attach_refuse = "hang"
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            async with api.attach(SID):
                pass
    assert fake.attach_count == 1


async def test_attach_receives_events_and_sends_client_events(fake, api) -> None:
    async with api.attach(SID) as ws:
        await fake.emit(SID, tool_call_event("transfer", {"why": "x"}, "call_9"))
        msg = await asyncio.wait_for(ws.receive_json(), 2)
        assert msg["event"]["item"]["name"] == "transfer"
        await ws.send_json({"type": "response.create", "event_id": "c_call_9"})
        await fake.wait_sent("response.create")
        await fake.close(SID, "remote_hangup")
        closed = await asyncio.wait_for(ws.receive_json(), 2)
        assert closed == {"type": "session.closed", "reason": "remote_hangup"}
    assert {"type": "response.create", "event_id": "c_call_9"} in fake.ws_sent
    assert fake.attach_count == 1
    assert fake.auth == [f"Bearer {KEY}"]


async def test_attach_refused_raises_a_client_error(fake, api) -> None:
    fake.attach_refuse = True
    with pytest.raises(aiohttp.ClientError):
        async with api.attach(SID):
            pass
    assert fake.attach_count == 1


async def test_drop_ws_closes_without_session_closed(fake, api) -> None:
    async with api.attach(SID) as ws:
        await fake.drop_ws(SID)
        msg = await asyncio.wait_for(ws.receive(), 2)
        assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)


async def test_emit_after_a_dropped_socket_reaches_the_next_attach(fake, api) -> None:
    async with api.attach(SID) as ws:
        await fake.drop_ws(SID)
        await asyncio.wait_for(ws.receive(), 2)
    emitted = asyncio.create_task(fake.emit(SID, {"type": "session.started"}))
    await asyncio.sleep(0.05)
    assert not emitted.done()  # waits for the client to attach again
    async with api.attach(SID) as ws:
        await asyncio.wait_for(emitted, 2)
        assert await asyncio.wait_for(ws.receive_json(), 2) == {"type": "session.started"}
    assert fake.attach_count == 2


async def test_respond_returns_function_calls(fake, api) -> None:
    fake.responses_queue = [
        [message("thinking"), function_call("transfer", {"why": "x"})],
        [message("nothing to do")],
        [function_call("end_call", "{not json")],
    ]
    tools = [{"type": "function", "name": "transfer", "parameters": {}}]
    first = await api.respond(model="m", instructions="i", input="call so far",
                              tools=tools, timeout_s=2)
    second = await api.respond(model="m", instructions="i", input="x", tools=tools,
                               timeout_s=2)
    third = await api.respond(model="m", instructions="i", input="x", tools=tools,
                              timeout_s=2)
    assert first == [{"name": "transfer", "arguments": {"why": "x"}}]
    assert second == []
    assert third == [{"name": "end_call", "arguments": {}}]
    verb, sid, body = fake.requests[0]
    assert (verb, sid) == ("responses", "")
    assert body == {"model": "m", "instructions": "i", "input": "call so far",
                    "tools": tools, "tool_choice": "auto"}


async def test_respond_errors(fake, api) -> None:
    fake.responses_queue = [RuntimeError("boom")]
    with pytest.raises(ApiError) as caught:
        await api.respond(model="m", instructions="i", input="x", tools=[], timeout_s=2)
    assert caught.value.status == 500
    fake.hang_for.add("responses")
    with pytest.raises(ApiError) as caught:
        await api.respond(model="m", instructions="i", input="x", tools=[], timeout_s=0.2)
    assert caught.value.status == 0


async def test_respond_waits_for_a_gated_answer(fake, api) -> None:
    gate = asyncio.Event()
    fake.responses_queue = [("wait", gate, [function_call("end_call", {})])]
    call = asyncio.create_task(
        api.respond(model="m", instructions="i", input="x", tools=[], timeout_s=2))
    await asyncio.sleep(0.05)
    assert not call.done()
    gate.set()
    assert await call == [{"name": "end_call", "arguments": {}}]


async def test_respond_json_returns_the_object(fake, api) -> None:
    schema = {"type": "object", "properties": {"summary": {"type": "string"}},
              "required": ["summary"], "additionalProperties": False}
    fake.responses_queue = [[message('{"summary": '), message('"a call"}')]]
    got = await api.respond_json(model="m", instructions="i", input="x",
                                 schema=schema, timeout_s=2)
    assert got == {"summary": "a call"}
    body = fake.requests[0][2]
    assert body["text"] == {"format": {"type": "json_schema", "name": "record",
                                       "schema": schema, "strict": True}}
    assert "tools" not in body


async def test_respond_json_reads_a_top_level_output_text(fake, api) -> None:
    fake.responses_queue = [{"output": [], "output_text": '{"ok": true}'}]
    got = await api.respond_json(model="m", instructions="i", input="x",
                                 schema={}, timeout_s=2)
    assert got == {"ok": True}


async def test_respond_json_raises_on_text_that_is_not_json(fake, api) -> None:
    fake.responses_queue = [[message("sorry, no")], [message(json.dumps([1, 2]))]]
    for _ in range(2):
        with pytest.raises(ApiError) as caught:
            await api.respond_json(model="m", instructions="i", input="x",
                                   schema={}, timeout_s=2)
        assert caught.value.code == "invalid_output"
