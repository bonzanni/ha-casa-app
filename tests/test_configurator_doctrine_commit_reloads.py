"""The configurator doctrine teaches that a commit reloads what it changed
(INV-TOOL-014), so no recipe asks for a reload after committing.

The configurator used to be told commit -> reload -> emit_completion, and
skipped or mis-scoped the reload often enough that Casa force-reloaded at
completion. ``config_git_commit`` now runs the reload itself; a recipe that
still asked for one would cost a second reload of the same change, or a wrong
scope picked by the model on top of the right one the commit already ran.
The only reloads left to the configurator are ``plugin_env`` (its file is not
committed) and the retry of a reload a tool ran and reported failed; every
other write a commit cannot see reloads in the tool that makes it.
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


# The only reloads a recipe may still ask for: `plugin_env`, because
# plugin-env.conf is gitignored and no commit sees it; and the retry of a reload
# a TOOL ran and reported failed — a committed-but-not-ready `plugin_update`, a
# specialist `persona_apply` whose own reload did not succeed. Every other
# commit-invisible write reloads in the tool that makes it (reload.md, "Changes
# no commit can see").
_ALLOWED_CALLS = {
    ("plugin/update.md", 'casa_reload(scope="agent",\n     role=<affected role>)'),
    ("persona/apply.md", 'casa_reload(scope="agent", role="<slug>")'),
}


def _calls(text: str) -> list[str]:
    return [m.group(0) for m in _RELOAD_CALL.finditer(text)]


@pytest.mark.parametrize("recipe_path", _recipes(),
                         ids=lambda p: str(p.relative_to(_DOCTRINE_ROOT)))
def test_a_recipe_calls_no_reload_but_the_allowed_ones(recipe_path: Path) -> None:
    rel = recipe_path.relative_to(_DOCTRINE_ROOT / "recipes").as_posix()
    calls = _calls(recipe_path.read_text(encoding="utf-8"))
    others = [c for c in calls
              if "plugin_env" not in c and (rel, c) not in _ALLOWED_CALLS]
    assert others == [], f"{rel}: reload calls nothing needs: {others}"


def test_every_allowed_retry_is_still_there():
    """The allowlist cannot outlive the text it allows."""
    for rel, call in _ALLOWED_CALLS:
        text = (_DOCTRINE_ROOT / "recipes" / rel).read_text(encoding="utf-8")
        assert call in _calls(text), (rel, call)


def test_reload_md_names_who_reloads_each_change_no_commit_sees():
    text = (_DOCTRINE_ROOT / "reload.md").read_text(encoding="utf-8")
    section = text[text.index("## Changes no commit can see"):
                   text.index("## Reading the commit's reloads")]
    for tool in ("persona_apply", "specialist_upgrade", "specialist_rollback",
                 "specialist_uninstall", "specialist_install_commit", "plugin_add",
                 "set_plugin_env_reference", "persona_install_commit"):
        assert f"`{tool}`" in section, tool
    assert 'casa_reload(scope="plugin_env")' in section
