"""Tests for mcp_registry.py."""

import logging

from mcp_registry import McpServerRegistry


class TestMcpServerRegistry:
    def test_register_http(self):
        reg = McpServerRegistry()
        reg.register_http("ha", "http://localhost:8123/mcp", headers={"X-Key": "abc"})

        result = reg.resolve(["ha"])
        assert "ha" in result
        assert result["ha"]["type"] == "http"
        assert result["ha"]["url"] == "http://localhost:8123/mcp"
        assert result["ha"]["headers"] == {"X-Key": "abc"}

    def test_resolve_multiple(self):
        reg = McpServerRegistry()
        reg.register_http("a", "http://a")
        reg.register_http("b", "http://b")
        reg.register_http("c", "http://c")

        result = reg.resolve(["a", "c"])
        assert set(result.keys()) == {"a", "c"}

    def test_resolve_unknown_logs_warning(self, caplog):
        reg = McpServerRegistry()
        reg.register_http("known", "http://x")

        with caplog.at_level(logging.WARNING):
            result = reg.resolve(["known", "mystery"])

        assert "known" in result
        assert "mystery" not in result
        assert "mystery" in caplog.text

    def test_resolve_empty_list(self):
        reg = McpServerRegistry()
        assert reg.resolve([]) == {}
