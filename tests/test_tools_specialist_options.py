"""Task N1b Step 23: the system-prompt seam _build_specialist_options gained
in Step 22 — a specialist with an ACTIVE compiled binding is served the
compiled bundle's text projection; a specialist with no binding (still
bundled-in-image, or pending-configuration) falls back unchanged to the
legacy cfg.system_prompt (_compose_prompt's output)."""


def test_build_specialist_options_prefers_compiled_bundle_when_present() -> None:
    """Pins INV-PERS-001. Red case demonstrated: forcing the no-bundle branch (compiled_bundle -> None) fails this test."""
    from tools import _build_specialist_options
    from prompt_compiler import CompiledPromptBundle, CompiledProjection

    class _FakeCfg:
        role = "mtg"
        model = "claude-sonnet-4-6"
        system_prompt = "LEGACY — should not be used"
        cwd = ""
        hooks = type("H", (), {"pre_tool_use": []})()
        tools = type("T", (), {"allowed": [], "disallowed": [], "permission_mode": "dontAsk",
                                 "max_turns": 8, "skills": "none"})()
        mcp_server_names = []
        compiled_prompt_bundle = CompiledPromptBundle(
            role_id="specialist:mtg", resolved_model="claude-sonnet-4-6",
            text=CompiledProjection(system_prompt="COMPILED TEXT\n", digest="sha256:" + "0" * 64,
                                     estimated_tokens=10),
            voice=CompiledProjection(system_prompt="COMPILED VOICE\n", digest="sha256:" + "1" * 64,
                                      estimated_tokens=10),
            restricted_webhook=CompiledProjection(system_prompt="COMPILED RW\n",
                                                    digest="sha256:" + "2" * 64, estimated_tokens=5),
            binding_digest="sha256:" + "3" * 64,
        )

    opts = _build_specialist_options(_FakeCfg())
    assert opts.system_prompt == "COMPILED TEXT\n"


def test_build_specialist_options_falls_back_to_legacy_system_prompt_when_no_bundle() -> None:
    from tools import _build_specialist_options

    class _FakeCfg:
        role = "finance"
        model = "claude-sonnet-4-6"
        system_prompt = "LEGACY PROMPT\n"
        cwd = ""
        hooks = type("H", (), {"pre_tool_use": []})()
        tools = type("T", (), {"allowed": [], "disallowed": [], "permission_mode": "dontAsk",
                                 "max_turns": 8, "skills": "none"})()
        mcp_server_names = []
        compiled_prompt_bundle = None

    opts = _build_specialist_options(_FakeCfg())
    assert opts.system_prompt == "LEGACY PROMPT\n"


def test_build_specialist_options_logs_the_effective_capability_set(
    tmp_path, caplog,
) -> None:
    """#459: the boot `agent_capabilities` line reports only the role.yaml
    declaration — a specialist whose tools come from its owned plugin's
    server-level grant logs `tool_count=0` there and looks broken. The
    EFFECTIVE set exists only here, after the resolution is applied, so this
    builder must emit it: the grant-derived tool and the plugin name."""
    import json
    import logging

    from plugin_registry import ResolutionResult, ResolvedPlugin
    from tools import _build_specialist_options

    plug = tmp_path / "plug"
    plug.mkdir()
    (plug / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"mtg": {"command": "serve"}}}))

    class _FakeCfg:
        role = "mtg"
        model = "claude-sonnet-4-6"
        system_prompt = "P\n"
        cwd = ""
        hooks = type("H", (), {"pre_tool_use": []})()
        tools = type("T", (), {"allowed": [], "disallowed": [],
                               "permission_mode": "dontAsk",
                               "max_turns": 8, "skills": "none"})()
        mcp_server_names = []
        compiled_prompt_bundle = None

    res = ResolutionResult(registry_valid=True, plugins=[ResolvedPlugin(
        name="mtg.mtg", artifact_id="art-1", path=str(plug), version="1.0.0",
        manifest={}, manifest_name="mtg")])

    with caplog.at_level(logging.INFO, logger="tools"):
        _build_specialist_options(_FakeCfg(), resolution=res)

    lines = [r.getMessage() for r in caplog.records
             if "agent_capabilities_effective" in r.getMessage()]
    assert lines, "no agent_capabilities_effective line from the options builder"
    line = lines[0]
    assert "role=mtg" in line
    # The server-level grant composed from the owned plugin must be visible —
    # this is exactly what the boot line cannot show.
    assert "mcp__plugin_mtg_mtg" in line
    assert "tool_count=1" in line
    assert "plugins=['mtg.mtg']" in line


def test_build_specialist_options_effective_line_with_no_plugins(caplog) -> None:
    """#459 companion: with an empty resolution the effective line still
    emits, showing a genuinely tool-less build for what it is."""
    import logging

    from plugin_registry import ResolutionResult
    from tools import _build_specialist_options

    class _FakeCfg:
        role = "finance"
        model = "claude-sonnet-4-6"
        system_prompt = "P\n"
        cwd = ""
        hooks = type("H", (), {"pre_tool_use": []})()
        tools = type("T", (), {"allowed": [], "disallowed": [],
                               "permission_mode": "dontAsk",
                               "max_turns": 8, "skills": "none"})()
        mcp_server_names = []
        compiled_prompt_bundle = None

    with caplog.at_level(logging.INFO, logger="tools"):
        _build_specialist_options(
            _FakeCfg(), resolution=ResolutionResult(registry_valid=True))

    lines = [r.getMessage() for r in caplog.records
             if "agent_capabilities_effective" in r.getMessage()]
    assert lines
    assert "role=finance" in lines[0]
    assert "tool_count=0" in lines[0]


def test_effective_line_excludes_disallowed_tools(caplog) -> None:
    """#459 / Sol r1 S2: a tool that is both granted and disallowed is denied
    by the CLI, so the effective line must NOT count it as usable — otherwise
    the oracle claims a capability the specialist does not have."""
    import logging

    from plugin_registry import ResolutionResult
    from tools import _build_specialist_options

    class _FakeCfg:
        role = "finance"
        model = "claude-sonnet-4-6"
        system_prompt = "P\n"
        cwd = ""
        hooks = type("H", (), {"pre_tool_use": []})()
        tools = type("T", (), {"allowed": ["Read", "Write"],
                               "disallowed": ["Write"],
                               "permission_mode": "dontAsk",
                               "max_turns": 8, "skills": "none"})()
        mcp_server_names = []
        compiled_prompt_bundle = None

    with caplog.at_level(logging.INFO, logger="tools"):
        opts = _build_specialist_options(
            _FakeCfg(), resolution=ResolutionResult(registry_valid=True))

    line = [r.getMessage() for r in caplog.records
            if "agent_capabilities_effective" in r.getMessage()][0]
    assert "tools=['Read']" in line
    assert "Write" not in line.split("mcp_servers=")[0]
    assert "tool_count=1" in line
    # And the options genuinely deny it at the CLI layer.
    assert "Write" in opts.disallowed_tools
