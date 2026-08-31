"""Unit test for the v0.15.1 mock-SDK tool-invoke hook.

The hook lives in `test-local/mock-claude-sdk/claude_agent_sdk/__init__.py`
and is only loaded inside the `casa-test` Docker image (which pip-installs
the mock SDK over the real one). This host test imports the mock package
directly from its source path so we can verify the file-driven behaviour
without booting Docker.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

pytestmark = pytest.mark.unit


def _load_mock_sdk():
    """Load the mock SDK package from its in-repo source path under a
    private alias so we don't shadow the real claude_agent_sdk on the host.
    """
    pkg_root = (
        Path(__file__).resolve().parents[1]
        / "test-local" / "mock-claude-sdk" / "claude_agent_sdk"
    )
    spec = importlib.util.spec_from_file_location(
        "_casa_mock_sdk_under_test",
        pkg_root / "__init__.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_casa_mock_sdk_under_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _RecordingHaApp:
    """Minimal aiohttp server that records POST bodies for assertion."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.calls.append(body)
        return web.json_response({
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": {"content": [{"type": "text", "text": "ok"}]},
        })


class TestMockSdkToolInvoke(AioHTTPTestCase):
    @pytest.fixture(autouse=True)
    def _tmp_root(self, tmp_path):
        """#778: pytest injects no `tmp_path` into a `unittest.TestCase` test
        METHOD — and `AioHTTPTestCase` is one — but a class-local AUTOUSE
        fixture may request one, which is what this is. The test below used to
        call `tempfile.mkdtemp()` and leak the directory on every run."""
        self.tmp_root = tmp_path

    async def get_application(self):
        self.recorder = _RecordingHaApp()
        app = web.Application()
        app.router.add_post("/", self.recorder.handle)
        return app

    async def test_invokes_http_tool_when_file_present(self):
        # The dead `tmp_path=None` parameter this method used to carry could
        # never be filled (see `_tmp_root`); the fixture supplies the root.
        invoke_file = self.tmp_root / "mock_sdk_tool_invoke.json"
        invoke_file.write_text(json.dumps([{
            "server": "homeassistant",
            "tool": "HassTurnOff",
            "args": {"name": "kitchen"},
        }]))

        mock_sdk = _load_mock_sdk()
        url = str(self.server.make_url("/"))
        opts = mock_sdk.ClaudeAgentOptions(
            mcp_servers={
                "homeassistant": {"type": "http", "url": url},
            },
        )

        import os
        os.environ["MOCK_SDK_TOOL_INVOKE_FILE"] = str(invoke_file)
        try:
            await mock_sdk._maybe_invoke_mcp_tool(opts)
        finally:
            os.environ.pop("MOCK_SDK_TOOL_INVOKE_FILE", None)

        # Tool call landed on the recording server.
        assert len(self.recorder.calls) == 1
        body = self.recorder.calls[0]
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "HassTurnOff"
        assert body["params"]["arguments"] == {"name": "kitchen"}

        # Entry was popped from the file.
        remaining = json.loads(invoke_file.read_text())
        assert remaining == []

        # #778: the invoke file is the ONLY thing this test created, and it is
        # inside the managed root — nothing is left in the system tmpdir.
        assert [q.name for q in self.tmp_root.iterdir()] == [
            "mock_sdk_tool_invoke.json"]

    async def test_no_op_when_file_missing(self):
        mock_sdk = _load_mock_sdk()
        opts = mock_sdk.ClaudeAgentOptions(
            mcp_servers={"homeassistant": {"type": "http", "url": "http://x/"}},
        )

        import os
        os.environ["MOCK_SDK_TOOL_INVOKE_FILE"] = "/nonexistent/path.json"
        try:
            await mock_sdk._maybe_invoke_mcp_tool(opts)  # must not raise
        finally:
            os.environ.pop("MOCK_SDK_TOOL_INVOKE_FILE", None)

        assert len(self.recorder.calls) == 0
