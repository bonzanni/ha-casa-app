"""#1111: every in-process SDK client raises the SDK's per-line stream limit.

The SDK fails the whole query on one stream-json line longer than
``max_buffer_size`` (1 MiB when unset). A built-in ``Read`` of a PDF puts the
file's base64 on one line twice — the tool result's document block and
``tool_use_result`` — so a 458,486-byte PDF killed three delegations in a row.

The transport tests drive the REAL pinned SDK's subprocess transport against a
stand-in CLI (a local script on pipes — no socket, no network) that emits one
such line. The sweep pins that every production construction passes the shared
constant, so a new builder cannot silently fall back to the SDK default.
"""
from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
APP_ROOT = REPO / "casa" / "rootfs" / "opt" / "casa"

# The pinned CLI (2.1.273) inlines a whole PDF up to this size in ``Read``.
_CLI_MAX_PDF_BYTES = 20 * 1024 * 1024
# The size observed in production on #1111.
_OBSERVED_PDF_BYTES = 458_486

_FAKE_CLI = textwrap.dedent('''\
    import base64, json, os, sys
    if "--version" in sys.argv or "-v" in sys.argv:
        print("2.1.273 (Claude Code)"); sys.exit(0)
    n = int(os.environ["FAKE_PDF_BYTES"])
    b64 = base64.b64encode(b"%PDF-" + b"\\0" * (n - 5)).decode()
    msg = {"type": "user", "message": {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "toolu_x", "content": [
            {"type": "text", "text": "PDF file read"},
            {"type": "document", "source": {"type": "base64",
             "media_type": "application/pdf", "data": b64}}]}]},
        "parent_tool_use_id": None, "session_id": "s", "uuid": "u",
        "tool_use_result": {"type": "pdf", "file": {
            "filePath": "/x.pdf", "base64": b64, "originalSize": n}}}
    for raw in sys.stdin:
        try:
            req = json.loads(raw)
        except Exception:
            continue
        if req.get("type") == "control_request":
            sys.stdout.write(json.dumps({"type": "control_response",
                "response": {"subtype": "success",
                             "request_id": req["request_id"],
                             "response": {}}}) + "\\n")
            sys.stdout.flush()
            if req.get("request", {}).get("subtype") == "initialize":
                break
    sys.stdout.write(json.dumps(msg) + "\\n")
    sys.stdout.write(json.dumps({"type": "result", "subtype": "success",
        "duration_ms": 1, "duration_api_ms": 1, "is_error": False,
        "num_turns": 1, "session_id": "s", "total_cost_usd": 0}) + "\\n")
    sys.stdout.flush()
''')


def _fake_cli(tmp_path: Path) -> str:
    path = tmp_path / "fake_claude"
    path.write_text(f"#!{sys.executable}\n{_FAKE_CLI}", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


async def _read_pdf_turn(tmp_path, monkeypatch, *, pdf_bytes, max_buffer_size):
    """Run one query through the real SDK transport; return the message
    types received, or raise what the SDK raised."""
    from claude_agent_sdk import ClaudeAgentOptions, query

    monkeypatch.setenv("FAKE_PDF_BYTES", str(pdf_bytes))
    options = ClaudeAgentOptions(
        cli_path=_fake_cli(tmp_path), max_buffer_size=max_buffer_size,
    )
    got = []
    async for message in query(prompt="x", options=options):
        got.append(type(message).__name__)
    return got


async def test_default_limit_fails_the_observed_read(tmp_path, monkeypatch):
    """The red case: the stand-in reaches the SDK's guard exactly as the
    production failure did — so the passing cases below are evidence."""
    from claude_agent_sdk import CLIJSONDecodeError

    with pytest.raises(CLIJSONDecodeError, match="1048576 bytes"):
        await _read_pdf_turn(tmp_path, monkeypatch,
                             pdf_bytes=_OBSERVED_PDF_BYTES,
                             max_buffer_size=None)


@pytest.mark.parametrize("pdf_bytes", [_OBSERVED_PDF_BYTES, _CLI_MAX_PDF_BYTES])
async def test_shared_limit_carries_the_largest_whole_pdf_the_cli_inlines(
    tmp_path, monkeypatch, pdf_bytes,
):
    from claude_runtime import SDK_MAX_BUFFER_SIZE

    got = await _read_pdf_turn(tmp_path, monkeypatch, pdf_bytes=pdf_bytes,
                               max_buffer_size=SDK_MAX_BUFFER_SIZE)
    assert got == ["UserMessage", "ResultMessage"]


def test_every_production_claude_options_sets_the_shared_line_buffer():
    observed: dict[str, str | None] = {}
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            ctor = node.func
            if not ((isinstance(ctor, ast.Name)
                     and ctor.id == "ClaudeAgentOptions")
                    or (isinstance(ctor, ast.Attribute)
                        and ctor.attr == "ClaudeAgentOptions")):
                continue
            kw = next((k for k in node.keywords
                       if k.arg == "max_buffer_size"), None)
            observed[f"{path.relative_to(APP_ROOT)}:{node.lineno}"] = (
                ast.unparse(kw.value) if kw is not None else None)

    assert len(observed) >= 8, observed
    unset = {loc: v for loc, v in observed.items()
             if v != "SDK_MAX_BUFFER_SIZE"}
    assert not unset, (
        "every production ClaudeAgentOptions construction must pass the "
        f"shared SDK_MAX_BUFFER_SIZE; missing or different: {unset}")
