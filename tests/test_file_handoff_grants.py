"""S6 §2.3 — having an inbox grants the path AND the two inbound-file tools, from one
constructor, to the resident builder and the delegated builder alike (INV-FILE-002)."""
from __future__ import annotations

import pytest

import agent_inbox as ai
import tools as tools_mod
from plugin_registry import ResolutionResult
from test_inbound_files import _path_scope, _read
from test_delegate_to_agent import _specialist_cfg as _base_cfg


def _specialist_cfg(role, kind="specialist"):
    cfg = _base_cfg(role)
    cfg.kind = kind
    return cfg
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


def _listed_tool_names(server_cfg):
    """The tools the REAL SDK server answers tools/list with (not the allowed list)."""
    import asyncio
    from mcp import types as mt
    server = server_cfg["instance"]
    handler = server.request_handlers[mt.ListToolsRequest]
    res = asyncio.run(handler(mt.ListToolsRequest(method="tools/list")))
    return {t.name for t in res.root.tools}


def test_a_delegated_inbox_specialist_gets_the_framework_server_exposing_both_tools(tmp_path, bound, inbox_root, monkeypatch):
    """Diff round 1, Astra: granting the names is not enough — a specialist whose config omits
    `mcp_server_names` had both names allowed and NO server exposing them."""
    from mcp_registry import McpServerRegistry
    reg = McpServerRegistry()
    reg.register_sdk_factory("casa-framework", lambda _role, grants: tools_mod.create_casa_tools(grants))
    monkeypatch.setattr(tools_mod, "_mcp_registry", reg)
    _wire("finance", inbox_root)
    cfg = _specialist_cfg("finance")
    cfg.mcp_server_names = []
    res = ResolutionResult(registry_valid=True, plugins=[_plugin(tmp_path, "plain")])
    opts = tools_mod._build_specialist_options(cfg, resolution=res)
    assert "casa-framework" in opts.mcp_servers
    assert {"list_inbound_files", "share_inbound_file"} <= _listed_tool_names(opts.mcp_servers["casa-framework"])
    assert cfg.mcp_server_names == []                                # the config itself is untouched


def test_an_engagement_build_offers_no_inbox_tools_and_no_inbox_read(tmp_path, bound, inbox_root):
    """the coordinator's ruling R-D1 (a): only a specialist's desk and delegated turns get its inbox;
    its jobs and engagements (the builds that carry the launch grants) are offered neither
    tool nor the read path, and the #541 ceiling is unchanged."""
    inbox = _wire("finance", inbox_root)
    res = ResolutionResult(registry_valid=True, plugins=[_plugin(tmp_path, "plain")])
    opts = tools_mod._build_specialist_options(_specialist_cfg("finance"), resolution=res,
                                               extra_casa_tools=tools_mod.SPECIALIST_CASA_GRANTS)
    assert LIST_T not in opts.allowed_tools and SHARE_T not in opts.allowed_tools
    cb = _path_scope(opts.hooks)
    import asyncio
    assert not asyncio.run(_read(cb, inbox.ready_dir + "/x.pdf"))
    assert {"list_inbound_files", "share_inbound_file"}.isdisjoint(tools_mod._SPECIALIST_DISPATCH_CEILING)



def test_a_delegated_resident_keeps_its_read_path_but_takes_no_inbox_tools(tmp_path, bound, inbox_root):
    """Diff round 3, Astra (third round on this grant — generalised): the delegated builder
    also builds a RESIDENT delegated by another resident; only a specialist's desk or
    delegated build takes the two tools from `grants_for`, a resident's stay what its own
    configuration lists. Its read path still comes from the one constructor."""
    inbox = _wire("assistant", inbox_root)
    res = ResolutionResult(registry_valid=True, plugins=[_plugin(tmp_path, "plain")])
    opts = tools_mod._build_specialist_options(_specialist_cfg("assistant", kind="resident"), resolution=res)
    assert LIST_T not in opts.allowed_tools and SHARE_T not in opts.allowed_tools
    cb = _path_scope(opts.hooks)
    import asyncio
    assert asyncio.run(_read(cb, inbox.ready_dir + "/x.pdf"))
