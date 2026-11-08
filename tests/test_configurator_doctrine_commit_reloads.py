"""The configurator doctrine teaches that a commit reloads what it changed
(INV-TOOL-014), so no recipe asks for a reload after committing.

The configurator used to be told commit -> reload -> emit_completion, and
skipped or mis-scoped the reload often enough that Casa force-reloaded at
completion. ``config_git_commit`` now runs the reload itself; a recipe that
still asked for one would cost a second reload of the same change, or a wrong
scope picked by the model on top of the right one the commit already ran.
The only reloads left to the configurator are ``plugin_env`` (its file is not
committed) and the retries a plugin tool's result asks for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_DOCTRINE_ROOT = (
    Path(__file__).resolve().parent.parent
    / "casa" / "rootfs" / "opt" / "casa" / "defaults" / "agents" / "executors"
    / "configurator" / "doctrine"
)

_RELOAD_CALL = re.compile(r"casa_reload(?:_triggers)?\(([^)]*)\)")


def _recipes() -> list[Path]:
    return sorted((_DOCTRINE_ROOT / "recipes").rglob("*.md"))


def test_completion_md_order_is_commit_then_emit():
    text = (_DOCTRINE_ROOT / "completion.md").read_text(encoding="utf-8")
    commit = text.find("**Commit.**")
    emit = text.find("**Emit completion.**")
    assert 0 <= commit < emit, (commit, emit)
    assert "**Reload" not in text
    step_one = text[commit:emit]
    assert "also reloads what it changed" in step_one


def test_reload_md_says_the_commit_reloads():
    text = (_DOCTRINE_ROOT / "reload.md").read_text(encoding="utf-8")
    assert "`config_git_commit` reloads what its commit changed" in text
    assert "You do not call a reload after committing." in text


@pytest.mark.parametrize("recipe_path", _recipes(),
                         ids=lambda p: str(p.relative_to(_DOCTRINE_ROOT)))
def test_a_recipe_calls_no_reload_but_plugin_env(recipe_path: Path) -> None:
    """Outside the plugin recipes — whose tools reload and verify their own
    targets, and whose results name the retry — a recipe's only reload call is
    ``plugin_env``."""
    rel = recipe_path.relative_to(_DOCTRINE_ROOT / "recipes")
    if rel.parts[0] == "plugin":
        pytest.skip("the plugin tools' own retries")
    text = recipe_path.read_text(encoding="utf-8")
    calls = [m.group(0) for m in _RELOAD_CALL.finditer(text)]
    others = [c for c in calls if "plugin_env" not in c]
    assert others == [], f"{rel}: reload calls a commit already makes: {others}"
