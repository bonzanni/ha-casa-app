"""The pad route: a plugin's own code puts text into its conversation's pad and reads it back,
with no model in between, during one of its calls. The pad is found by the broker client id
``attach`` registered, through the handler Casa mounts and the call record the admission hook
keeps; the model's side is the SDK's real tool server, as in ``tests/test_scratchpad.py``."""
from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import make_mocked_request
from claude_agent_sdk import ClaudeAgentOptions

import operator_calls
import result_broker as rb
import scratchpad
from authz_grants import GrantIdentity
from scratchpad import GRANT, MAX_BYTES, attach
from test_scratchpad import _body, _call, pads  # noqa: F401 — fixture

ORIGIN = {"role": "assistant", "channel": "telegram", "chat_id": 42, "user_id": 100,
          "_operator_turn": True, "_handoff_chain": ("assistant", "browser")}


@pytest.fixture(autouse=True)
def calls(monkeypatch):
    store = operator_calls.OperatorCalls()
    monkeypatch.setattr(operator_calls, "CALLS", store)
    return store


def _attached(key: str, client: str, **kw) -> ClaudeAgentOptions:
    """A client built as the delegated runner builds one: the broker's own env, then the pad."""
    return attach(ClaudeAgentOptions(env=rb.broker_env(client), **kw), key)


def _open(client: str, tu: str = "tu") -> None:
    operator_calls.CALLS.open(
        client_id=client, tool_use_id=tu, tool_name="mcp__plugin_browser_api__run_read",
        role="browser", plugin="browser",
        identity=GrantIdentity(operator_id=100, bot_id=111, chat_id=42, enforcement_role="browser",
                               artifact_id="1" * 64, engagement_id=""),
        origin=dict(ORIGIN))


async def _post(body) -> dict:
    req = make_mocked_request("POST", "/internal/broker/pad")

    async def _json():
        return body
    req.json = _json
    resp = await operator_calls.build_pad_handler()(req)
    return json.loads(resp.body)


def _refused(reply: dict, code: str) -> bool:
    return (set(reply) == {"error", "message"} and reply["error"] == code
            and isinstance(reply["message"], str) and reply["message"])


# --- whose pad, and when --------------------------------------------------------------------


async def test_the_route_answers_only_during_the_clients_call_in_flight(pads):  # noqa: F811
    _attached("S1", "c1")
    write = {"client": "c1", "op": "write", "section": "page", "text": "row"}
    assert _refused(await _post(write), "no_call_in_flight")
    assert _refused(await _post({**write, "op": "read"}), "no_call_in_flight")
    assert pads.lines("S1") is None
    _open("c1")
    assert await _post(write) == {"lines": 1, "bytes": 3}
    assert await _post({**write, "op": "read"}) == {"text": "row", "next": None}
    operator_calls.CALLS.close("c1", "tu")
    assert _refused(await _post({**write, "text": "late"}), "no_call_in_flight")
    assert pads.lines("S1") == ["## page", "row"]


async def test_the_route_reaches_only_that_clients_pad(pads):  # noqa: F811
    model = _attached("S1", "c1")
    _attached("S2", "c2")
    _open("c1")
    await _post({"client": "c1", "op": "write", "section": "page", "text": "mine"})
    # another conversation's client has no call in flight, so its id reaches nothing
    assert _refused(await _post({"client": "c2", "op": "read", "section": "page"}),
                    "no_call_in_flight")
    _open("c2")
    assert _refused(await _post({"client": "c2", "op": "read", "section": "page"}), "no_pad")
    assert pads.lines("S2") is None
    # the model's own tool reads what the plugin's code put in its pad
    assert _body((await _call(model, op="read", section="page"))[0]) == "mine"


async def test_a_call_in_flight_without_a_pad_reaches_none(pads):  # noqa: F811
    attach(ClaudeAgentOptions(env=rb.broker_env("denied"), disallowed_tools=[GRANT]), "S1")
    _open("denied")
    _open("bare")                    # a client no attach saw: a resident's, an engagement's
    for client in ("denied", "bare"):
        reply = await _post({"client": client, "op": "write", "section": "page", "text": "x"})
        assert _refused(reply, "no_pad")
    assert pads.keys() == []


async def test_a_client_id_registered_for_two_pads_reaches_neither(pads):  # noqa: F811
    _attached("S1", "c1")
    _attached("S2", "c1")
    _attached("S1", "c1")
    _open("c1")
    assert _refused(await _post({"client": "c1", "op": "write", "section": "p", "text": "x"}),
                    "no_pad")
    assert pads.keys() == []


async def test_two_conversations_pads_stay_apart_through_the_route(pads):  # noqa: F811
    a, b = _attached("S1", "c1"), _attached("S2", "c2")
    _open("c1")
    _open("c2")
    for client, text in (("c1", "one\ntwo"), ("c2", "three")):
        await _post({"client": client, "op": "write", "section": "page", "text": text})
    await _post({"client": "c2", "op": "append", "section": "page", "text": "four"})
    assert (await _post({"client": "c1", "op": "read", "section": "page"}))["text"] == "one\ntwo"
    assert (await _post({"client": "c2", "op": "read", "section": "page"}))["text"] == \
        "three\nfour"
    assert pads.lines("S1") == ["## page", "one", "two"]
    assert pads.lines("S2") == ["## page", "three", "four"]
    assert _body((await _call(a, op="read", section="page"))[0]) == "one\ntwo"
    assert _body((await _call(b, op="read", section="page"))[0]) == "three\nfour"


async def test_no_field_names_a_pad(pads):  # noqa: F811
    _attached("S1", "c1")
    _attached("S2", "c2")
    pads.put("S2", ["## page", "theirs"])
    _open("c1")
    for wrong in ("key", "pad", "session_id", "session", "conversation", "id"):
        reply = await _post({"client": "c1", "op": "write", "section": "page", "text": "mine",
                             wrong: "S2"})
        assert reply == {"lines": 1, "bytes": 4}
        reply = await _post({"client": "c1", "op": "read", "section": "page", wrong: "S2"})
        assert reply == {"text": "mine", "next": None}
    # a section named after the other pad's key is a section of this one
    await _post({"client": "c1", "op": "write", "section": "S2", "text": "still mine"})
    assert pads.lines("S2") == ["## page", "theirs"]
    assert pads.lines("S1") == ["## page", "mine", "## S2", "still mine"]


# --- what it reads and writes ---------------------------------------------------------------


async def test_a_read_pages_at_thirty_thousand_characters_with_next(pads):  # noqa: F811
    _attached("S1", "c1")
    _open("c1")
    text = "\n".join(f"{i:05} " + "r" * 94 for i in range(700))       # 70,699 characters
    assert (await _post({"client": "c1", "op": "write", "section": "page", "text": text}))[
        "lines"] == 700
    got, offset, sizes = "", 0, []
    while offset is not None:
        reply = await _post({"client": "c1", "op": "read", "section": "page", "offset": offset})
        assert set(reply) == {"text", "next"}
        sizes.append(len(reply["text"]))
        got += reply["text"]
        offset = reply["next"]
    assert sizes == [30_000, 30_000, len(text) - 60_000] and got == text
    first = await _post({"client": "c1", "op": "read", "section": "page"})
    assert first["next"] == 30_000 and first["text"] == text[:30_000]
    end = await _post({"client": "c1", "op": "read", "section": "page", "offset": len(text)})
    assert end == {"text": "", "next": None}
    for bad in (len(text) + 1, -1, "3", True, 1.5):
        reply = await _post({"client": "c1", "op": "read", "section": "page", "offset": bad})
        assert _refused(reply, "bad_offset"), bad


async def test_a_write_replaces_its_section_and_an_append_adds(pads):  # noqa: F811
    _attached("S1", "c1")
    pads.put("S1", ["note", "## page", "old", "## list", "a"])
    _open("c1")
    assert await _post({"client": "c1", "op": "write", "section": "page",
                        "text": "new 1\nnew 2\n"}) == {"lines": 2, "bytes": 11}
    assert await _post({"client": "c1", "op": "write", "section": "page",
                        "text": "new 1\nnew 2\n"}) == {"lines": 2, "bytes": 11}
    assert await _post({"client": "c1", "op": "append", "section": "list",
                        "text": "b\r\nc"}) == {"lines": 3, "bytes": 5}
    assert pads.lines("S1") == ["note", "## page", "new 1", "new 2", "## list", "a", "b", "c"]


async def test_a_route_line_beginning_the_mark_does_not_create_a_section(pads):  # noqa: F811
    model = _attached("S1", "c1")
    _open("c1")
    text = "a\n## Not a section\n##x\n ## b\n## "
    assert (await _post({"client": "c1", "op": "write", "section": "page", "text": text}))[
        "lines"] == 5
    await _post({"client": "c1", "op": "append", "section": "page", "text": "## page"})
    assert pads.lines("S1") == ["## page", "a", " ## Not a section", "##x", " ## b", " ## ",
                                " ## page"]
    shape = (await _call(model, op="read"))[0].split("\n", 1)[0]
    assert '1 section: "page" at line 1, 6 lines.' in shape
    assert (await _post({"client": "c1", "op": "read", "section": "page"}))["text"] == \
        "a\n ## Not a section\n##x\n ## b\n ## \n ## page"


async def test_a_name_two_sections_share_is_refused_on_the_route(pads):  # noqa: F811
    _attached("S1", "c1")
    before = ["## Dup", "one", "## B", "two", "## Dup", "three"]
    pads.put("S1", before)
    _open("c1")
    for op, extra in (("write", {"text": "x"}), ("append", {"text": "x"}), ("read", {})):
        reply = await _post({"client": "c1", "op": op, "section": "Dup", **extra})
        assert _refused(reply, "duplicate_section"), op
        assert '2 sections are named "Dup"' in reply["message"]
    assert pads.lines("S1") == before


async def test_a_write_past_the_cap_is_refused_on_the_route(pads):  # noqa: F811
    _attached("S1", "c1")
    pads.put("S1", ["## page", "keep"])
    _open("c1")
    reply = await _post({"client": "c1", "op": "append", "section": "page",
                         "text": "x" * MAX_BYTES})
    assert _refused(reply, "too_large") and str(MAX_BYTES) in reply["message"]
    assert pads.lines("S1") == ["## page", "keep"]


@pytest.mark.parametrize("body, code", [
    ({"op": "delete", "section": "p"}, "bad_op"), ({"op": "run", "section": "p"}, "bad_op"),
    ({"op": "write", "text": "x"}, "bad_section"), ({"op": "write", "section": "", "text": "x"},
                                                    "bad_section"),
    ({"op": "write", "section": "   ", "text": "x"}, "bad_section"),
    ({"op": "write", "section": "a\nb", "text": "x"}, "bad_section"),
    ({"op": "read", "section": 3}, "bad_section"),
    ({"op": "write", "section": "p"}, "bad_text"), ({"op": "append", "section": "p", "text": 1},
                                                    "bad_text"),
    ({"op": "read", "section": "absent"}, "no_section")])
async def test_a_request_the_route_cannot_do_is_refused_with_a_reason(pads, body, code):  # noqa: F811
    _attached("S1", "c1")
    pads.put("S1", ["## p", "x"])
    _open("c1")
    assert _refused(await _post({"client": "c1", **body}), code)
    assert pads.lines("S1") == ["## p", "x"]


async def test_a_read_of_a_pad_that_is_gone_says_so(pads):  # noqa: F811
    _attached("S1", "c1")
    _open("c1")
    assert _refused(await _post({"client": "c1", "op": "read", "section": "p"}), "no_pad")
    for body in (None, [], "x"):
        assert _refused(await _post(body), "bad_json")
    assert _refused(await _post({"op": "read", "section": "p"}), "bad_client")


async def test_a_pads_end_forgets_its_clients(pads):  # noqa: F811
    _attached("S1", "c1")
    _open("c1")
    await _post({"client": "c1", "op": "write", "section": "p", "text": "x"})
    pads.discard("S1")
    reply = await _post({"client": "c1", "op": "write", "section": "p", "text": "x"})
    assert _refused(reply, "no_pad") and pads.keys() == []


def test_the_route_is_mounted_beside_the_other_broker_routes():
    import ast
    from pathlib import Path
    core = Path(scratchpad.__file__).with_name("casa_core.py").read_text()
    routes = {node.args[0].value: ast.unparse(node.args[1]) for node in ast.walk(ast.parse(core))
              if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_post"
              and node.args and isinstance(node.args[0], ast.Constant)}
    assert routes["/internal/broker/pad"] == "_ops.build_pad_handler()"

