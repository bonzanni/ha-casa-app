"""#1294 controls: a specialist whose role lists or already denies the shell
tool is unaffected by the Bash clamp.

The clamp treats any form the role schema admits as declared — a bare
``Bash`` or a scoped ``Bash(<pattern>)`` — and never adds a second denial.
"""
from types import SimpleNamespace

import pytest

import tools


def _options(monkeypatch, allowed, disallowed):
    from config import HooksConfig
    from plugin_registry import ResolutionResult

    monkeypatch.setattr(tools, "_mcp_registry", None)
    cfg = SimpleNamespace(
        role="finance", model="claude-haiku-4-5", system_prompt="You are Alex.",
        tools=SimpleNamespace(allowed=list(allowed), disallowed=list(disallowed),
                              permission_mode="acceptEdits", max_turns=10),
        mcp_server_names=[], hooks=HooksConfig(), cwd="",
    )
    return tools._build_specialist_options(
        cfg, resolution=ResolutionResult(registry_valid=True))


@pytest.mark.parametrize("grant", ["Bash", "Bash(git status:*)"])
def test_a_role_that_lists_bash_keeps_it(monkeypatch, grant):
    opts = _options(monkeypatch, ["Read", grant], [])
    assert opts.allowed_tools == ["Read", grant]
    assert opts.disallowed_tools.count("Bash") == 0


def test_a_role_that_denies_bash_denies_it_once(monkeypatch):
    opts = _options(monkeypatch, ["Read"], ["Bash"])
    assert opts.disallowed_tools.count("Bash") == 1
    assert opts.disallowed_tools[0] == "Bash"
