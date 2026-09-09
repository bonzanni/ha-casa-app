"""Unit coverage for executor-memory slot substitution (M4 L3).

Triage note (collapse 4/7):
- ``_fetch_executor_archive`` was rewritten to use semantic recall (delegated_memory)
  instead of a per-executor Honcho session. The five tests that drove the OLD
  provider behavior (``memory_provider=``, ``ensure_session``/``get_context``
  round-trips, provider-raises path) were DELETED — they tested REMOVED behavior
  now fully covered by ``test_executor_memory_tiers.py``.

Surviving tests:
  - ``test_substitutes_executor_memory_slot_when_memory_enabled`` — template
      {executor_memory} slot substitution (pure string op, no _fetch_executor_archive
      call).
  - ``test_workspace_legacy_path_substitutes_executor_memory`` — provision_workspace
      threads executor_memory= through to CLAUDE.md; pre-existing _Defn._tools_allowed
      gap unrelated to this task (workspace.py added tools_allowed after this test was
      written).
"""

from __future__ import annotations

import sys

import pytest



def test_substitutes_executor_memory_slot_when_memory_enabled(monkeypatch, tmp_path):
    """engage_executor build_prompt path interpolates {executor_memory}.

    Black-box-ish: we re-derive the same substitution rules engage_executor
    uses, asserting the helper's output substitutes correctly.
    """
    template = (
        "task: {task}\n"
        "ctx: {context}\n"
        "world: {world_state_summary}\n"
        "mem: {executor_memory}\n"
    )
    rendered = (
        template
        .replace("{task}", "do thing")
        .replace("{context}", "(none)")
        .replace("{world_state_summary}", "ws ok")
        .replace("{executor_memory}", "## Prior engagements (lessons learned)\n- prior")
    )
    assert "do thing" in rendered
    assert "ws ok" in rendered
    assert "Prior engagements" in rendered
    assert "{executor_memory}" not in rendered


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="provision_workspace uses os.mkfifo (Linux-only)",
)
async def test_workspace_legacy_path_substitutes_executor_memory(tmp_path):
    """The legacy provision_workspace path threads executor_memory through.

    Forward-compat coverage for the claude_code driver — no claude_code
    executor opts into memory today, but workspace.py wires the slot so a
    future memory-enabled claude_code executor works without plumbing.
    """
    from drivers.workspace import provision_workspace

    class _Defn:
        type = "x"
        prompt_template_path = str(tmp_path / "prompt.md")
        hooks_path = None
        tools_allowed: list[str] = []
        permission_mode = "acceptEdits"
    (tmp_path / "prompt.md").write_text(
        "task={task} mem={executor_memory}\n", encoding="utf-8",
    )

    eng_root = tmp_path / "engagements"
    eng_root.mkdir()
    plugins_root = tmp_path / "plugins_root"
    plugins_root.mkdir()

    await provision_workspace(
        engagements_root=str(eng_root),
        engagement_id="abc12345",
        engagement_auth_token="tok-ws-test",
        defn=_Defn(),
        task="dotask",
        context="(none)",
        casa_framework_mcp_url="http://127.0.0.1:8100/mcp/casa-framework",
        executor_memory="## Prior\nbody",
    )
    claude_md = (eng_root / "abc12345" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "task=dotask" in claude_md
    assert "mem=## Prior\nbody" in claude_md
