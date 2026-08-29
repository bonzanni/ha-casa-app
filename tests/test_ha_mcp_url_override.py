"""Contracts for the HA MCP URL and Tina facade boot wiring.

The URL tests inline the env-read pattern that `casa_core.main` uses. Facade
tests exercise the extracted boot/lifecycle helpers without booting the full
app, while a narrow source assertion pins their ownership and order in main.
The Phase F e2e
(`test-local/e2e/test_ha_delegation.sh`) covers the live wiring by
asserting the addon log line `Registered Home Assistant MCP server (url=...)`
contains the override URL."""
from __future__ import annotations

import inspect
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
HA_E2E_SCRIPT = REPO_ROOT / "test-local/e2e/test_ha_delegation.sh"
E2E_PYTHON_RESOLVER = REPO_ROOT / "test-local/e2e/resolve_python.sh"


def _write_fake_python(path: Path, body: str = "exit 0") -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run_python_resolver(
    shared_root: Path,
    *,
    env: dict[str, str],
    timeout: float = 3,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(E2E_PYTHON_RESOLVER), str(shared_root)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_ha_mock_e2e_pins_eager_facade_contract():
    script = HA_E2E_SCRIPT.read_text(encoding="utf-8")

    assert "MOCK_HA_MALFORMED_TOOL=1" in script
    assert "HomeAssistantFacade" in script
    assert "tools/list after resident connect" in script
    assert '"name": "GetLiveContext", "arguments": {}' in script
    assert "guard bound exceeded" in script


def test_e2e_python_resolver_prefers_explicit_override(tmp_path):
    shared_root = tmp_path / "shared"
    shared_python = shared_root / "venv_test/bin/python3"
    shared_python.parent.mkdir(parents=True)
    _write_fake_python(shared_python)
    override = tmp_path / "override-python"
    _write_fake_python(override)
    env = {**os.environ, "E2E_PYTHON": str(override)}

    result = _run_python_resolver(shared_root, env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(override)
    assert result.stderr == ""


def test_e2e_python_resolver_falls_back_to_path_without_shared_venv(tmp_path):
    shared_root = tmp_path / "shared-without-venv"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    path_python = fake_bin / "python3"
    _write_fake_python(path_python)
    env = dict(os.environ)
    env.pop("E2E_PYTHON", None)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = _run_python_resolver(shared_root, env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(path_python)
    assert result.stderr == ""


def test_e2e_python_resolver_rejects_nonexecutable_override(tmp_path):
    override = tmp_path / "not-executable"
    override.write_text("not executable\n", encoding="utf-8")
    env = {**os.environ, "E2E_PYTHON": str(override)}

    result = _run_python_resolver(tmp_path / "shared", env=env)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "not executable" in result.stderr


def test_e2e_python_resolver_reports_aiohttp_dependency_without_detail(
    tmp_path,
):
    override = tmp_path / "python-without-aiohttp"
    _write_fake_python(
        override,
        'printf "SECRET_DEPENDENCY_DETAIL" >&2; exit 7',
    )
    env = {**os.environ, "E2E_PYTHON": str(override)}

    result = _run_python_resolver(tmp_path / "shared", env=env)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "cannot import aiohttp" in result.stderr
    assert "SECRET_DEPENDENCY_DETAIL" not in result.stderr


def test_e2e_python_resolver_bounds_aiohttp_import_check(tmp_path):
    override = tmp_path / "hanging-python"
    _write_fake_python(override, "sleep 10")
    env = {
        **os.environ,
        "E2E_PYTHON": str(override),
        "E2E_PYTHON_CHECK_TIMEOUT": "0.1",
    }

    result = _run_python_resolver(
        tmp_path / "shared",
        env=env,
        timeout=2,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "aiohttp check timed out after 0.1" in result.stderr


def test_ha_mock_e2e_bounds_every_mock_reset_and_history_curl():
    normalized = " ".join(HA_E2E_SCRIPT.read_text(encoding="utf-8").split())

    history = (
        'curl -sf --max-time 2 '
        '"http://localhost:${MOCK_HA_PORT}/_calls"'
    )
    reset = (
        'curl -sf --max-time 2 -X POST '
        '"http://localhost:${MOCK_HA_PORT}/_reset"'
    )
    assert normalized.count(history) == 4
    assert normalized.count(reset) == 2


@pytest.fixture
def recording_facade(monkeypatch):
    import casa_core

    class RecordingFacade:
        instances = []
        start_error = None

        def __init__(self, url, headers, on_schema_change=None):
            self.url = url
            self.headers = dict(headers)
            self.on_schema_change = on_schema_change
            self.server_config = {"type": "sdk", "instance": self}
            self.started = False
            self.closed = False
            self.__class__.instances.append(self)

        async def start(self):
            self.started = True
            if self.__class__.start_error is not None:
                raise self.__class__.start_error

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(
        casa_core, "HomeAssistantFacade", RecordingFacade, raising=False,
    )
    return RecordingFacade


def test_default_ha_mcp_url(monkeypatch):
    """Without env override, the canonical supervisor URL is used."""
    from mcp_registry import McpServerRegistry

    monkeypatch.delenv("CASA_HA_MCP_URL", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

    reg = McpServerRegistry()
    ha_url = os.environ.get("CASA_HA_MCP_URL", "http://supervisor/core/api/mcp")
    reg.register_http(
        name="homeassistant",
        url=ha_url,
        headers={"Authorization": "Bearer test-token"},
    )
    resolved = reg.resolve(["homeassistant"])
    assert resolved["homeassistant"]["url"] == "http://supervisor/core/api/mcp"


def test_ha_mcp_url_override(monkeypatch):
    """With CASA_HA_MCP_URL set, that URL is used instead."""
    from mcp_registry import McpServerRegistry

    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    monkeypatch.setenv("CASA_HA_MCP_URL", "http://mock-ha:8200/")

    reg = McpServerRegistry()
    ha_url = os.environ.get("CASA_HA_MCP_URL", "http://supervisor/core/api/mcp")
    reg.register_http(
        name="homeassistant",
        url=ha_url,
        headers={"Authorization": "Bearer test-token"},
    )
    resolved = reg.resolve(["homeassistant"])
    assert resolved["homeassistant"]["url"] == "http://mock-ha:8200/"


async def test_start_tina_facade_uses_supervisor_auth_and_butler_override(
    recording_facade,
):
    from casa_core import _start_tina_ha_facade
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    registry.register_http(
        "homeassistant", "http://raw",
        headers={"Authorization": "Bearer secret-token"},
    )

    facade = await _start_tina_ha_facade(
        registry,
        {"butler": SimpleNamespace(channels=["ha_voice"])},
        {},
        ha_mcp_url="http://raw",
        supervisor_token="secret-token",
    )

    assert facade is recording_facade.instances[0]
    assert facade.started
    assert facade.url == "http://raw"
    assert facade.headers == {"Authorization": "Bearer secret-token"}
    assert callable(facade.on_schema_change)
    assert registry.resolve(
        ["homeassistant"], role="butler",
    )["homeassistant"] is facade.server_config
    assert registry.resolve(
        ["homeassistant"], role="assistant",
    )["homeassistant"]["url"] == "http://raw"


async def test_tina_facade_callback_uses_current_agents_and_refreshed_config(
    recording_facade,
):
    from casa_core import _start_tina_ha_facade
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    registry.register_http("homeassistant", "http://raw")
    agents = {}
    facade = await _start_tina_ha_facade(
        registry,
        {"butler": SimpleNamespace(channels=["ha_voice"])},
        agents,
        ha_mcp_url="http://raw",
        supervisor_token="secret-token",
    )

    class ObservingAgent:
        def __init__(self):
            self.seen = []

        async def invalidate_tool_surface(self):
            self.seen.append(
                registry.resolve(["homeassistant"], role="butler")[
                    "homeassistant"
                ]
            )

    butler = ObservingAgent()
    agents["butler"] = butler
    refreshed = {"type": "sdk", "instance": object()}
    facade.server_config = refreshed

    await facade.on_schema_change()

    assert butler.seen == [refreshed]
    assert registry.resolve(
        ["homeassistant"], role="butler",
    )["homeassistant"] is refreshed
    assert registry.resolve(
        ["homeassistant"], role="assistant",
    )["homeassistant"]["url"] == "http://raw"


@pytest.mark.parametrize(
    ("supervisor_token", "role_configs"),
    [
        ("", {"butler": SimpleNamespace(channels=["ha_voice"])}),
        ("secret-token", {}),
        ("secret-token", {"butler": SimpleNamespace(channels=["telegram"])}),
    ],
    ids=["no-token", "no-butler", "butler-without-ha-voice"],
)
async def test_tina_facade_requires_token_and_ha_voice_butler(
    recording_facade, supervisor_token, role_configs,
):
    from casa_core import _start_tina_ha_facade
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    registry.register_http("homeassistant", "http://raw")

    facade = await _start_tina_ha_facade(
        registry,
        role_configs,
        {},
        ha_mcp_url="http://raw",
        supervisor_token=supervisor_token,
    )

    assert facade is None
    assert recording_facade.instances == []
    assert registry.resolve(
        ["homeassistant"], role="butler",
    )["homeassistant"]["url"] == "http://raw"


async def test_tina_facade_initial_discovery_failure_is_sanitized_degraded(
    recording_facade, caplog,
):
    """Pins INV-HA-002. Red case demonstrated: unregistering the raw homeassistant entry in the facade-failure handler fails this test."""
    from casa_core import _start_tina_ha_facade
    from mcp_registry import McpServerRegistry

    recording_facade.start_error = RuntimeError(
        "private-token failed at http://private-ha",
    )
    registry = McpServerRegistry()
    registry.register_http("homeassistant", "http://raw")

    with caplog.at_level(logging.WARNING):
        facade = await _start_tina_ha_facade(
            registry,
            {"butler": SimpleNamespace(channels=["ha_voice"])},
            {},
            ha_mcp_url="http://private-ha",
            supervisor_token="private-token",
        )

    assert facade is None
    assert recording_facade.instances[0].closed
    assert [
        record.getMessage() for record in caplog.records
        if "ha_facade" in record.getMessage()
    ] == ["ha_facade_initialization_failed status=degraded"]
    assert "private-token" not in caplog.text
    assert "private-ha" not in caplog.text
    assert registry.resolve(
        ["homeassistant"], role="butler",
    )["homeassistant"]["url"] == "http://raw"


def test_main_owns_tina_facade_boot_and_shutdown_lifecycle():
    """#698: the CLOSE half now lives in the graceful-stop cleanup coroutine.

    ``main``'s stop block was extracted into ``casa_core._shutdown_cleanup`` so
    a red case could execute the whole stop sequence rather than re-implement
    it. The facade's lifecycle is unchanged — it is still booted by ``main``
    after the agents are built, and still closed exactly once on the way down,
    in the same position in the same statement order — so this case follows the
    close to where it now stands rather than claiming a lifecycle that moved.
    ``main`` awaits that coroutine, so "main owns the lifecycle" is still what
    is asserted; it now spans the two functions the sequence spans."""
    from casa_core import _shutdown_cleanup, main

    source = inspect.getsource(main)
    assert "ha_facade = await _start_tina_ha_facade(" in source
    assert "await _shutdown_cleanup(" in source
    assert "ha_facade=ha_facade," in source
    assert source.index("agents[role] = agent") < source.index(
        "ha_facade = await _start_tina_ha_facade(",
    )
    assert "await _close_tina_ha_facade(ha_facade)" in inspect.getsource(
        _shutdown_cleanup,
    )
