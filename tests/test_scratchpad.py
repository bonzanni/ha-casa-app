"""The pad and its operations, reached as the CLI reaches them: the SDK's real tool server,
through the SDK's real JSON-RPC bridge. Its lifetime in Casa's delegated runner is
``tests/test_scratchpad_lifetime.py``."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

import scratchpad
from scratchpad import GRANT, MAX_BYTES, PROMPT_LINE, SERVER, attach

MODULE = Path(scratchpad.__file__)
CLI_SAVE_TO_FILE_CHARS = 50_000


@pytest.fixture(autouse=True)
def pads(monkeypatch):
    store = scratchpad.PadStore()
    monkeypatch.setattr(scratchpad, "PADS", store)
    return store


def _options(**kw) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(**kw)


async def _rpc(options: ClaudeAgentOptions, method: str, params: dict) -> dict:
    """One request over the SDK's own bridge, after the handshake the CLI performs."""
    bridge = SdkMcpBridge(SERVER, options.mcp_servers[SERVER]["instance"])
    try:
        await bridge.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}})
        await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return await bridge.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                                    "params": params})
    finally:
        await bridge.aclose()


async def _call(options: ClaudeAgentOptions, **args) -> tuple[str, bool]:
    """(the reply's text, whether it is an error), as the model receives them."""
    reply = await _rpc(options, "tools/call", {"name": "scratchpad", "arguments": args})
    result = reply["result"]
    return result["content"][0]["text"], result.get("isError", False)


def _client(key: str = "S1", **kw) -> ClaudeAgentOptions:
    return attach(_options(), key, **kw)


async def _pad(text: str, key: str = "S1") -> ClaudeAgentOptions:
    client = _client(key)
    _, error = await _call(client, op="write", text=text)
    assert error is False
    return client


def _body(reply: str) -> str:
    """What a reply carries below its header."""
    return reply.split("\n\n", 1)[1]


# --- whose pad ---------------------------------------------------------------------------


async def test_no_argument_selects_a_pad(pads):
    a, b = await _pad("mine", "S1"), await _pad("theirs", "S2")
    listed = await _rpc(a, "tools/list", {})
    (tool,) = listed["result"]["tools"]
    schema = tool["inputSchema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"op", "text", "section", "find", "line", "limit",
                                         "chars", "char_offset", "skip"}
    for wrong in ("key", "pad", "session_id", "id", "conversation"):
        reply, error = await _call(a, op="read", **{wrong: "S2"})
        assert error is True and "theirs" not in reply
    # every string the tool does take is pad content or a section, never a pad: naming the
    # other pad's key in any of them still answers from this client's own
    for field in ("section", "text"):
        reply, _ = await _call(a, op="search", find="theirs", **{field: "S2"})
        assert "theirs" not in reply
    assert pads.lines("S1") == ["mine"] and pads.lines("S2") == ["theirs"]
    assert (await _call(b, op="read"))[0].endswith("theirs")


async def test_a_clients_pad_is_the_key_it_was_built_with(pads):
    client = _client("S1")
    await _call(client, op="write", text="note")
    assert pads.keys() == ["S1"]
    # another client of the same conversation (a resumed turn) finds it
    assert _body((await _call(_client("S1"), op="read"))[0]) == "note"


async def test_two_clients_have_two_pads(pads):
    a, b = _client("S1"), _client("S2")
    await _call(a, op="write", text="one")
    await _call(b, op="write", text="two\nthree")
    assert pads.lines("S1") == ["one"] and pads.lines("S2") == ["two", "three"]
    pads.discard("S1")
    assert pads.keys() == ["S2"]
    assert (await _call(a, op="read"))[1] is True
    assert (await _call(b, op="read"))[1] is False


async def test_only_the_four_line_operations_remain():
    """What the pad no longer does is gone from its schema, so the model is never offered it."""
    client = await _pad("b\na\nb")
    (tool,) = (await _rpc(client, "tools/list", {}))["result"]["tools"]
    assert tool["inputSchema"]["properties"]["op"]["enum"] == ["read", "write", "append", "search"]
    for gone in ("other", "new", "all"):
        assert gone not in tool["inputSchema"]["properties"]
    for op in ("replace", "count", "sort", "dedupe", "compare", "delete"):
        assert op not in tool["description"]
        reply, error = await _call(client, op=op, find="a", new="x", section="")
        assert error is True
    assert scratchpad.PADS.lines("S1") == ["b", "a", "b"]


async def test_attach_adds_a_server_and_a_grant_on_a_copy():
    other = {"type": "http", "url": "http://x"}
    options = _options(mcp_servers={"other": other}, allowed_tools=["Read"],
                       disallowed_tools=["Bash"], session_id="s")
    attached = attach(options, "S1")
    assert options.mcp_servers == {"other": other} and options.allowed_tools == ["Read"]
    assert attached.mcp_servers["other"] is other
    assert attached.allowed_tools == ["Read", GRANT]
    assert attached.disallowed_tools == ["Bash"] and attached.session_id == "s"


@pytest.mark.parametrize("given, told", [
    (None, PROMPT_LINE), ("", PROMPT_LINE), ("Role prompt.", f"Role prompt.\n\n{PROMPT_LINE}")])
def test_attach_tells_the_client_in_its_system_prompt(given, told):
    options = _options(system_prompt=given)
    assert attach(options, "S1").system_prompt == told
    assert options.system_prompt == given


def test_the_line_reaches_the_cli_as_its_system_prompt():
    options = attach(_options(cli_path="/bin/true", system_prompt="Role prompt."), "S1")
    command = SubprocessCLITransport("x", options)._build_command()
    assert command[command.index("--system-prompt") + 1] == f"Role prompt.\n\n{PROMPT_LINE}"


def test_the_prompt_line_is_short_and_names_no_pad():
    assert len(PROMPT_LINE) < 200 and "\n" not in PROMPT_LINE
    assert "S1" not in attach(_options(), "S1").system_prompt


@pytest.mark.parametrize("eager, always", [(False, None), (True, True)])
def test_only_an_eager_server_asks_to_load_up_front(eager, always):
    command = SubprocessCLITransport("x", attach(_options(cli_path="/bin/true"), "S1", eager=eager)
                                     )._build_command()
    config = json.loads(command[command.index("--mcp-config") + 1])["mcpServers"][SERVER]
    assert config.get("alwaysLoad") is always
    assert config["type"] == "sdk" and "instance" not in config


def test_the_brief_is_written_at_the_first_attach_and_never_again(pads):
    attach(_options(), "S1", task="find\n## them", words="his list")
    first = ["## brief", "Task:", "find", " ## them", "The operator's words:", "his list"]
    assert pads.lines("S1") == first
    attach(_options(), "S1", task="another task", words="other words")
    assert pads.lines("S1") == first
    pads.put("S2", ["kept"])
    attach(_options(), "S2", task="late")
    assert pads.lines("S2") == ["kept"]
    attach(_options(), "S3")
    assert pads.lines("S3") is None
    attach(_options(disallowed_tools=[GRANT]), "S4", task="denied")
    assert pads.lines("S4") is None


def test_the_module_uses_no_regex():
    tree = ast.parse(MODULE.read_text())
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not imported & {"re", "regex", "fnmatch", "glob"}


# --- a reply stays in its window ---------------------------------------------------------


async def test_a_read_defaults_to_a_hundred_lines_and_says_what_comes_next():
    client = await _pad("\n".join(f"item {i}" for i in range(1, 351)))
    reply, _ = await _call(client, op="read")
    assert len(_body(reply).split("\n")) == 100
    assert "Lines 1-100 of 350; next: line=101." in reply
    reply, _ = await _call(client, op="read", line=301)
    assert len(_body(reply).split("\n")) == 50 and "Lines 301-350 of 350." in reply
    assert "next" not in reply


async def test_a_read_defaults_to_eight_thousand_characters():
    client = await _pad("\n".join("x" * 99 for _ in range(200)))
    body = _body((await _call(client, op="read", limit=200))[0])
    assert len(body.split("\n")) == 80 and len(body) <= 8000


async def test_no_reply_reaches_the_clis_save_to_file_threshold():
    """A full pad of each hard shape: no reply is over the threshold, whatever the op."""
    shapes = ["x" * (MAX_BYTES - 1),
              "\n".join("y" * 1500 for _ in range(MAX_BYTES // 1502)),
              "\n".join(f"## {i}" for i in range(MAX_BYTES // 11))]
    for i, text in enumerate(shapes):
        client = await _pad(text, f"S{i}")
        for args in ({"op": "read", "chars": 40_000, "limit": 100_000},
                     {"op": "read", "chars": 40_000, "char_offset": 3},
                     {"op": "search", "find": "y"}, {"op": "search", "find": "#"},
                     {"op": "read", "line": 1, "limit": 1}):
            reply, _ = await _call(client, **args)
            assert len(reply) < CLI_SAVE_TO_FILE_CHARS, args
        assert "and " in (await _call(client, op="read", limit=1))[0] or i < 2


async def test_a_long_name_or_find_never_comes_back_whole():
    """A section name and a find are the caller's text, and neither may carry a reply past the
    window: each reply that quotes one quotes it cut."""
    long = "n" * (MAX_BYTES - 100)
    client = await _pad(f"## {long}\nbody")
    for args in ({"op": "read", "section": long}, {"op": "search", "section": long, "find": "b"},
                 {"op": "search", "find": "f" * 100_000},
                 {"op": "read", "section": "m" * 100_000},
                 {"op": "search", "section": "m" * 100_000, "find": "x"},
                 {"op": "append", "section": long, "text": "x"}):
        reply, _ = await _call(client, **args)
        assert len(reply) < 2_000, args["op"]
    other = _client("S2")
    for op in ("write", "append"):
        reply, error = await _call(other, op=op, section="m" * 100_000, text="x")
        assert error is False and len(reply) < 2_000
    half = long[:MAX_BYTES // 2 - 100]
    scratchpad.PADS.put("S3", [f"## {half}", f"## {half}"])
    reply, error = await _call(_client("S3"), op="read", section=half)
    assert error is True and "2 sections are named" in reply and len(reply) < 2_000
    reply, error = await _call(client, op="append", text=f"## {long}")
    assert error is True and "would make 2 sections named" in reply and len(reply) < 2_000


async def test_chars_above_the_maximum_is_refused():
    client = await _pad("x")
    assert (await _call(client, op="read", chars=40_001))[1] is True


async def test_a_long_line_is_cut_with_an_offset_and_reads_back_whole():
    line = "".join(chr(0x61 + i % 26) for i in range(5_000))
    client = await _pad(f"before\n{line}\nafter")
    body = _body((await _call(client, op="read"))[0])
    (first, cut, last) = body.split("\n")
    assert (first, last) == ("before", "after")
    assert cut.startswith(line[:2000]) and not cut.startswith(line[:2001])
    assert "line 2 has 5000 characters" in cut and "line=2, char_offset=2000" in cut
    got, offset = line[:2000], 2000
    while offset is not None:
        reply, _ = await _call(client, op="read", line=2, char_offset=offset)
        got += _body(reply)
        header = reply.split("\n\n", 1)[0]
        offset = (int(header.rsplit("char_offset=", 1)[1].rstrip("."))
                  if "char_offset=" in header else None)
    assert got == line


async def test_a_search_gives_forty_lines_cut_at_300_and_the_true_total():
    client = await _pad("\n".join(f"hit {i} " + "z" * 400 for i in range(90)))
    reply, _ = await _call(client, op="search", find="HIT")
    assert "90 matching lines; showing 1-40 of 90; next: skip=40." in reply
    shown = _body(reply).split("\n")
    assert len(shown) == 40 and all(len(s) < 360 for s in shown)
    assert shown[0].startswith("1: hit 0 z") and "cut: line 1 has 406 characters" in shown[0]
    reply, _ = await _call(client, op="search", find="hit", skip=80)
    assert len(_body(reply).split("\n")) == 10 and "next" not in reply


async def test_every_reply_opens_with_the_shape():
    client = await _pad("top\n## A\none\ntwo\n## B\nthree")
    expected = 'Pad: 6 lines, 27 bytes, 2 sections: top 1 line; "A" at line 2, 2 lines; ' \
               '"B" at line 5, 1 line.'
    for args in ({"op": "read"}, {"op": "search", "find": "one"}, {"op": "read", "section": "A"},
                 {"op": "search", "find": "a\nb"}):
        assert (await _call(client, **args))[0].split("\n", 1)[0] == expected


async def test_the_shape_names_a_bounded_number_of_sections():
    client = await _pad("\n".join(f"## {i:03} " + "n" * 200 for i in range(30)))
    shape = (await _call(client, op="read", limit=1))[0].split("\n", 1)[0]
    assert shape.count(" at line ") == 20 and shape.endswith("and 10 more.")
    assert len(shape) < 2000


@pytest.mark.parametrize("op, noted", [("read", True), ("search", True), ("append", False)])
async def test_only_a_read_or_a_search_carries_the_page_text_sentence(op, noted):
    client = await _pad("x")
    reply, _ = await _call(client, op=op, **({"read": {}, "search": {"find": "x"},
                                              "append": {"text": "y"}}[op]))
    assert (scratchpad.PAGE_NOTE in reply) is noted


async def test_a_write_past_the_cap_is_refused_and_truncates_nothing(pads):
    client = await _pad("keep")
    reply, error = await _call(client, op="write", text="x" * (MAX_BYTES + 1))
    assert error is True and str(MAX_BYTES + 1) in reply and str(MAX_BYTES) in reply
    assert pads.lines("S1") == ["keep"]
    assert (await _call(client, op="write", text="x" * MAX_BYTES))[1] is False
    assert (await _call(client, op="append", text="y"))[1] is True
    assert pads.lines("S1") == ["x" * MAX_BYTES]


async def test_the_cap_counts_bytes_not_characters(pads):
    client = _client()
    assert (await _call(client, op="write", text="é" * (MAX_BYTES // 2 + 1)))[1] is True
    assert (await _call(client, op="write", text="é" * (MAX_BYTES // 2)))[1] is False


# --- line counts on awkward input --------------------------------------------------------


async def test_a_pasted_section_line_is_a_section_and_the_reply_says_so(pads):
    client = await _pad("## List\none")
    reply, error = await _call(client, op="append", section="List",
                               text="two\n## Not mine\nthree\nfour")
    assert error is False
    assert 'Appended 4 lines to "List". Lines 2 -> 6. Sections 1 -> 2:' in reply
    assert '"Not mine" at line 4, 2 lines' in reply
    assert pads.lines("S1") == ["## List", "one", "two", "## Not mine", "three", "four"]
    # the split is what a later read finds: only two of the appended lines are in List
    assert _body((await _call(client, op="read", section="List"))[0]) == "one\ntwo"


@pytest.mark.parametrize("op, extra", [
    ("read", {}), ("append", {"text": "x"}), ("write", {"text": "x"}), ("search", {"find": "one"})])
async def test_a_duplicate_section_name_refuses_and_changes_nothing(pads, op, extra):
    text = "## Dup\none\n## B\ntwo\n## Dup\none"
    pads.put("S1", text.split("\n"))     # no write makes this pad any more; an older one could
    client = _client()
    reply, error = await _call(client, op=op, section="Dup", **extra)
    assert error is True
    assert '2 sections are named "Dup", at lines 1 and 5' in reply and "Nothing changed" in reply
    assert pads.lines("S1") == text.split("\n")


@pytest.mark.parametrize("op, section, text", [
    ("append", None, "## A\nmore"), ("append", "B", "x\n## A"), ("write", "B", "## A"),
    ("write", None, "## A\n## A"), ("append", "", "## B"), ("write", "", "## A")])
async def test_a_change_that_would_make_a_second_section_of_a_name_is_refused(
        pads, op, section, text):
    before = ["## A", "one", "## B", "two"]
    pads.put("S1", before)
    args = {} if section is None else {"section": section}
    reply, error = await _call(_client(), op=op, text=text, **args)
    assert error is True and "Nothing changed" in reply
    assert "would make 2 sections named" in reply
    assert pads.lines("S1") == before


@pytest.mark.parametrize("op, before, after", [
    ("write", ["## A", "one"], ["## A", "x", "y"]),
    ("write", ["## B", "two"], ["## B", "two", "## A", "x", "y"]),
    ("append", ["## A", "one"], ["## A", "one", "x", "y"])])
async def test_text_that_opens_with_its_own_sections_mark_is_that_sections_text(
        pads, op, before, after):
    """A model that puts the section's mark both in ``section`` and atop the text (measured on
    casa-test, job a, twice) gets the section written once, not a refusal."""
    pads.put("S1", before)
    reply, error = await _call(_client(), op=op, section="## A", text="## A\nx\ny")
    assert error is False, reply
    assert pads.lines("S1") == after


async def test_a_name_used_once_stays_free_to_reuse_by_its_own_section(pads):
    client = await _pad("## A\none\n## B\ntwo")
    assert (await _call(client, op="write", section="A", text="new"))[1] is False
    assert (await _call(client, op="write", text="## A\nx\n## C\ny"))[1] is False
    assert pads.lines("S1") == ["## A", "x", "## C", "y"]


async def test_search_is_plain_and_ignores_case():
    client = await _pad("A.c\nabc\n.*\n## .*")
    assert "1 matching line;" in (await _call(client, op="search", find="a.c"))[0]
    assert "2 matching lines;" in (await _call(client, op="search", find=".*"))[0]
    reply, error = await _call(client, op="search", find="a\nb")
    assert error is True and "one line" in reply
    assert (await _call(client, op="search", find=""))[1] is True


async def test_search_stays_in_its_section():
    client = await _pad("hit\n## A\nhit\n## B\nhit")
    reply, _ = await _call(client, op="search", section="A", find="hit")
    assert "1 matching line;" in reply and _body(reply) == "3: hit"


@pytest.mark.parametrize("text, lines", [
    ("a\nb\n", ["a", "b"]), ("a\n\n", ["a", ""]), ("a\r\nb\rc", ["a", "b", "c"]),
    ("a b\x0cc\x85d", ["a b\x0cc\x85d"]), ("", []), ("\n", [""]),
])
async def test_only_a_newline_ends_a_line(pads, text, lines):
    client = _client()
    await _call(client, op="write", text=text)
    assert pads.lines("S1") == lines


async def test_a_section_starts_only_at_a_line_beginning_the_mark(pads):
    client = await _pad("##\n##no\n ## no\n### no\n## yes\n## ")
    assert "2 sections" in (await _call(client, op="read"))[0]


async def test_a_section_name_with_its_mark_is_the_same_name(pads):
    client = await _pad("## A\none")
    assert _body((await _call(client, op="read", section="## A"))[0]) == "one"
    assert _body((await _call(client, op="read", section=" A "))[0]) == "one"


async def test_a_section_is_created_by_writing_or_appending_to_it(pads):
    client = _client()
    assert (await _call(client, op="append", section="New", text="x"))[1] is False
    assert (await _call(client, op="write", section="Other", text="y\nz"))[1] is False
    assert pads.lines("S1") == ["## New", "x", "## Other", "y", "z"]
    assert (await _call(client, op="write", section="New", text="w"))[1] is False
    assert pads.lines("S1") == ["## New", "w", "## Other", "y", "z"]
    assert (await _call(client, op="append", section="", text="top"))[1] is False
    assert pads.lines("S1") == ["top", "## New", "w", "## Other", "y", "z"]
    reply, error = await _call(client, op="append", section="a\nb", text="x")
    assert error is True and pads.lines("S1")[0] == "top"


async def test_a_read_outside_its_section_is_refused():
    client = await _pad("## A\nx\n## B\ny")
    reply, error = await _call(client, op="read", section="A", line=4)
    assert error is True and "lines 2-2" in reply
    assert _body((await _call(client, op="read", section="A"))[0]) == "x"


async def test_reading_an_empty_section_says_so():
    client = await _pad("## A\n## B\ny")
    reply, error = await _call(client, op="read", section="A")
    assert error is False and "No lines" in reply


async def test_no_pad_says_why_and_keeps_nothing(pads):
    client = _client()
    for args in ({"op": "read"}, {"op": "read", "section": "A"}, {"op": "search", "find": "x"}):
        reply, error = await _call(client, **args)
        assert error is True and reply.startswith("No pad for this conversation:")
        assert "restarted at " in reply
    assert pads.keys() == []


async def test_an_empty_named_header_is_not_the_top_of_the_pad(pads):
    client = await _pad("## \nKEEP\n## target\nRIGHT\n")
    assert (await _call(client, op="write", section="", text="top"))[1] is False
    assert pads.lines("S1") == ["top", "## ", "KEEP", "## target", "RIGHT"]
    headed = await _pad("## \nKEEP", "S2")
    assert "No lines in the top of the pad" in (await _call(headed, op="read", section=""))[0]
