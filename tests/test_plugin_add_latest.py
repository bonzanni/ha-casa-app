"""``plugin_add(ref="latest")`` / ``plugin_update(new_ref="latest")``: the
literal resolves to a release tag, the TAG is what the registry stores and
the result reports, and a repo with no release refuses before anything moves.

Design 2026-09-15 §2.C. The registry never stores the word "latest": a later
update, rollback or ``expected_revision`` check reads ``source.ref`` as an
exact ref, and "latest" would poison all three.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from test_plugin_tools import _State, _entry, _pr, _wire

SHA = "b" * 40
OTHER = "c" * 40


def _latest(monkeypatch, tools_mod, st, *, tag="v0.7.0", sha=SHA, exc=None):
    def fake_latest(repo):
        st.log.append("resolve_latest")
        if exc is not None:
            raise exc
        return tag, sha
    monkeypatch.setattr(tools_mod.plugin_store, "resolve_latest_release", fake_latest)


async def test_latest_installs_the_release_tag_and_stores_it(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _latest(monkeypatch, tools_mod, st)
    r = await tools_mod.plugin_add.handler({
        "name": "gmail", "repo": "bonzanni/casa-plugin-gmail", "ref": "latest",
        "targets": ["resident:assistant"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True, payload
    assert payload["resolved_ref"] == "v0.7.0"
    assert payload["revision"] == "git:" + SHA
    entry = st.raw["plugins"][0]
    assert entry["source"]["ref"] == "v0.7.0"          # the tag, never "latest"
    assert "latest" not in json.dumps(st.raw)
    # the literal never reaches the exact-ref resolver
    assert "resolve" not in st.log and "resolve_latest" in st.log


async def test_an_exact_ref_still_reports_itself_as_resolved_ref(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1.2.0",
        "targets": ["resident:assistant"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["resolved_ref"] == "v1.2.0"
    assert "resolve" in st.log and "resolve_latest" not in st.log


async def test_no_release_refuses_before_anything_moves(monkeypatch, tmp_path):
    from plugin_store import NoReleaseFound
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _latest(monkeypatch, tools_mod, st, exc=NoReleaseFound("o/r has no release"))
    r = await tools_mod.plugin_add.handler({
        "name": "gmail", "repo": "o/r", "ref": "latest",
        "targets": ["resident:assistant"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["kind"] == "no_release_found"
    assert payload["activation_committed"] is False
    assert st.raw["plugins"] == []
    assert st.log == ["resolve_latest"]            # nothing after the refusal


def test_the_tag_version_guard_runs_on_the_resolved_tag(monkeypatch, tmp_path):
    """A release tag whose peel carries a manifest of another version is the
    same tag_version_mismatch an exact tag gets."""
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.6.9"))
    _latest(monkeypatch, tools_mod, st, tag="v0.7.0")
    core = tools_mod._plugin_add_sync(
        name="gmail", repo="o/r", ref="latest", targets=["resident:assistant"],
        expected_revision=None)
    assert core["kind"] == "tag_version_mismatch"
    assert core["ref"] == "v0.7.0"
    assert st.raw["plugins"] == []


def test_expected_revision_is_compared_with_the_peel(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _latest(monkeypatch, tools_mod, st, sha=OTHER)
    core = tools_mod._plugin_add_sync(
        name="gmail", repo="o/r", ref="latest", targets=["resident:assistant"],
        expected_revision=SHA)
    assert core["kind"] == "revision_mismatch"
    assert core["resolved_revision"] == OTHER
    assert st.log == ["resolve_latest"]


async def test_plugin_update_to_latest_repoints_to_the_tag(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"] = [_entry(name="gmail", version="0.5.6")]
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _latest(monkeypatch, tools_mod, st)
    r = await tools_mod.plugin_update.handler({"name": "gmail", "new_ref": "latest"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True, payload
    assert payload["resolved_ref"] == "v0.7.0"
    assert st.raw["plugins"][0]["source"]["ref"] == "v0.7.0"
    assert "latest" not in json.dumps(st.raw)


@pytest.mark.parametrize("tool_name", ["plugin_add", "plugin_update"])
def test_the_tool_descriptions_offer_latest(tool_name):
    import tools as tools_mod
    desc = getattr(tools_mod, tool_name).description
    assert "latest" in desc


# --- doctrine: the configurator passes "latest", never browses, never a branch

_ADD = (Path(__file__).resolve().parents[1]
        / "casa/rootfs/opt/casa/defaults/agents/executors/configurator/doctrine/recipes/plugin/add.md")


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_add_recipe_routes_latest_through_the_tool():
    text = _collapse_ws(_ADD.read_text(encoding="utf-8"))
    assert 'pass `ref="latest"`' in text
    assert "You have no web tool" in text
    assert "never choose a branch on your own" in text
    assert "resolve 'latest' yourself" not in text
    assert "`resolved_ref`" in text
