"""MCP (Model Context Protocol) server registry."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

SdkFactory = Callable[[str, frozenset[str]], dict[str, Any]]


class McpServerRegistry:
    """Registry of named MCP server configurations."""

    def __init__(self) -> None:
        self._servers: dict[str, dict[str, Any]] = {}
        self._role_servers: dict[tuple[str, str], dict[str, Any]] = {}
        # #1091: the identity of the tool surface each role override was
        # published with, stored beside it under the same key.
        self._role_surface_digests: dict[tuple[str, str], str] = {}
        self._sdk_factories: dict[str, SdkFactory] = {}

    def register_http(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Register an HTTP-based MCP server."""
        self._servers[name] = {
            "type": "http",
            "url": url,
            "headers": headers,
        }

    def register_sdk(self, name: str, server_config: dict[str, Any]) -> None:
        """Register an MCP server from a raw config dict."""
        self._servers[name] = server_config

    def register_sdk_factory(self, name: str, factory: SdkFactory) -> None:
        """Register a role-aware SDK server configuration factory."""
        self._sdk_factories[name] = factory

    def register_role_sdk(
        self,
        name: str,
        role: str,
        server_config: dict[str, Any],
        *,
        surface_digest: str = "",
    ) -> None:
        """Register an SDK server override for one role.

        ``surface_digest`` names the tool surface this config was published
        with (#1091). The config and its digest are replaced together, so a
        reader never pairs one publication's tools with another's identity."""
        self._role_servers[(name, role)] = server_config
        if surface_digest:
            self._role_surface_digests[(name, role)] = surface_digest
        else:
            self._role_surface_digests.pop((name, role), None)

    def unregister_role_sdk(self, name: str, role: str) -> None:
        """Remove a role-specific SDK server override if present."""
        self._role_servers.pop((name, role), None)
        self._role_surface_digests.pop((name, role), None)

    def role_surfaces(
        self,
        names: Iterable[str],
        *,
        role: str,
    ) -> tuple[tuple[str, dict[str, Any], str], ...]:
        """The role overrides among *names* that carry a surface digest.

        ``(name, server_config, surface_digest)`` triples, sorted by name —
        the snapshot a turn arms, so the session is built with exactly the
        tools its recorded identity describes (#1091). Empty when no override
        for *role* was published with a digest."""
        surfaces = []
        for name in sorted(set(names)):
            digest = self._role_surface_digests.get((name, role))
            config = self._role_servers.get((name, role))
            if digest and config is not None:
                surfaces.append((name, config, digest))
        return tuple(surfaces)

    def resolve(
        self,
        names: list[str],
        *,
        role: str = "",
        allowed_tools: Iterable[str] = (),
    ) -> dict[str, dict[str, Any]]:
        """Return configs for all *names* that are registered.

        Logs a warning for any name that is not found.
        """
        grants = frozenset(allowed_tools)
        result: dict[str, dict[str, Any]] = {}
        for name in names:
            override = self._role_servers.get((name, role))
            if override is not None:
                result[name] = override
            elif name in self._sdk_factories:
                result[name] = self._sdk_factories[name](role, grants)
            elif name in self._servers:
                result[name] = self._servers[name]
            else:
                logger.warning("MCP server '%s' is not registered", name)
        return result
