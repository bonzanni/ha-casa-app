"""S6 §2.3 — having an inbox grants the path AND the two inbound-file tools, from one
constructor, to the resident builder and the delegated builder alike (INV-FILE-002)."""
from __future__ import annotations

import pytest

import agent_inbox as ai
import tools as tools_mod
from plugin_registry import ResolutionResult
from test_inbound_files import _path_scope, _read
from test_delegate_to_agent import _specialist_cfg
from test_pinned_wiring import _plugin, bound  # noqa: F401 — the fixture that unbinds the registries

LIST_T = "mcp__casa-framework__list_inbound_files"
SHARE_T = "mcp__casa-framework__share_inbound_file"


@pytest.fixture
def inbox_root(tmp_path, monkeypatch):
    root = tmp_path / "inbox"
    monkeypatch.setattr(ai, "_inboxes", {})
    yield root


def _wire(role, root):
    """What ``agent_inbox.wire`` does synchronously: provision and register."""
    inbox = ai.open_inbox(role, str(root))
    ai._inboxes[role] = inbox
    return inbox


def test_grants_for_names_the_ready_dir_and_both_tools_for_an_inbox_role_and_nothing_otherwise(inbox_root):
    inbox = _wire("finance", inbox_root)
    prefixes, tool_names = ai.grants_for("finance")
    assert prefixes == (inbox.ready_dir,) and set(tool_names) == {LIST_T, SHARE_T}
    assert ai.grants_for("records") == ((), ())
    assert ai.readable_prefixes("finance") == prefixes          # the old name stays a thin alias


def test_the_delegated_builder_grants_path_and_tools_to_an_inbox_specialist(tmp_path, bound, inbox_root):
    inbox = _wire("finance", inbox_root)
    res = ResolutionResult(registry_valid=True, plugins=[_plugin(tmp_path, "plain")])
    opts = tools_mod._build_specialist_options(_specialist_cfg("finance"), resolution=res)
    assert LIST_T in opts.allowed_tools and SHARE_T in opts.allowed_tools
    selected = {t.name for t in tools_mod.select_casa_tools(frozenset(opts.allowed_tools))}
    assert {"list_inbound_files", "share_inbound_file"} <= selected   # what the real server registers
    cb = _path_scope(opts.hooks)                                       # the resolved path_scope hook itself
    import asyncio
    assert asyncio.run(_read(cb, inbox.ready_dir + "/x.pdf"))          # a read under its ready/ is admitted
    assert not asyncio.run(_read(cb, str(inbox_root / "records" / "ready" / "y.pdf")))


def test_the_delegated_builder_grants_nothing_to_a_specialist_without_an_inbox(tmp_path, bound, inbox_root):
    res = ResolutionResult(registry_valid=True, plugins=[_plugin(tmp_path, "plain")])
    opts = tools_mod._build_specialist_options(_specialist_cfg("records"), resolution=res)
    assert LIST_T not in opts.allowed_tools and SHARE_T not in opts.allowed_tools
    selected = {t.name for t in tools_mod.select_casa_tools(frozenset(opts.allowed_tools))}
    assert not ({"list_inbound_files", "share_inbound_file"} & selected)
    cb = _path_scope(opts.hooks)
    import asyncio
    assert not asyncio.run(_read(cb, str(inbox_root / "finance" / "ready" / "x.pdf")))


def test_the_fail_closed_callback_denies_either_tool_for_a_specialist_without_an_inbox():
    import asyncio
    import plugin_grants
    cb = plugin_grants.make_fail_closed_can_use_tool("records")
    for name in (LIST_T, SHARE_T):
        verdict = asyncio.run(cb(name, {}, None))
        assert "deny" in type(verdict).__name__.lower() or getattr(verdict, "behavior", "") == "deny"
