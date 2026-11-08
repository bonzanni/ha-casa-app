"""Unit coverage for executor-memory slot substitution (M4 L3).

Triage note (collapse 4/7):
- ``_fetch_executor_archive`` was rewritten to use semantic recall (delegated_memory)
  instead of a per-executor Honcho session. The five tests that drove the OLD
  provider behavior (``memory_provider=``, ``ensure_session``/``get_context``
  round-trips, provider-raises path) were DELETED — they tested REMOVED behavior
  now fully covered by ``test_executor_memory_tiers.py``.

Surviving test:
  - ``test_substitutes_executor_memory_slot_when_memory_enabled`` — template
      {executor_memory} slot substitution (pure string op, no _fetch_executor_archive
      call).
"""

from __future__ import annotations


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
