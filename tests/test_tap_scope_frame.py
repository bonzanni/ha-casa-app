"""#1308: a tap's pinned one-call turn is framed as the operator's tap on the
specialist's own proposal, never as a delegation from the resident, and its
system prompt says the call is not to be judged against the role's scope. A
role that answers only its own kind of delegation refused the stored call when
the turn read as one ("falls outside my remit … I won't place that call").

The prompt is the one the REAL ``handle_tap`` sends through the REAL runner
(``test_redcase_1282``'s harness); the system prompt is the REAL builder's,
pinned and ordinary side by side. A turn without a stored call keeps its
delegation block (``test_pinned_prompt_controls`` pins that prompt exactly).
"""
from __future__ import annotations

import pytest

import tools as tools_mod
from plugin_registry import ResolutionResult
from test_delegate_to_agent import _specialist_cfg
from test_desk_tap import env  # noqa: F401
from test_pinned_wiring import _build_input, _owner, _pinned, bound  # noqa: F401
from test_redcase_1282 import _pinned_prompt

ROLE_TEXT = "Answer only finance-scoped delegations."


@pytest.mark.asyncio
async def test_the_tap_is_framed_as_the_operators_tap_not_a_delegation(env, bound, tmp_path,
                                                                       monkeypatch):
    prompt, _ = await _pinned_prompt(env, tmp_path, monkeypatch)
    assert "<delegation_context>" not in prompt and "caller_role:" not in prompt, prompt
    assert prompt.startswith(tools_mod.STORED_CALL_CONTEXT), prompt
    assert "authorisation: the operator's tap" in prompt
    assert prompt.count("[casa stored call]") == 1


def test_the_pinned_system_prompt_rules_out_the_scope_judgement_after_the_role_text():
    cfg = _specialist_cfg()
    cfg.system_prompt = ROLE_TEXT
    build = _build_input(cfg, ResolutionResult(registry_valid=True, plugins=[]))
    pinned = _pinned(_owner(build), tools_mod._build_specialist_options, cfg).system_prompt
    ordinary = tools_mod._build_specialist_options(cfg, resolution=build.resolution).system_prompt
    assert ordinary == ROLE_TEXT
    assert pinned == f"{ROLE_TEXT}\n\n{tools_mod.STORED_CALL_SYSTEM_SECTION}"
    section = tools_mod.STORED_CALL_SYSTEM_SECTION
    assert "a plugin assigned to you" in section
    assert "operator's tap is the authorisation" in section
    assert "not a request to judge against your role's scope or remit" in section
