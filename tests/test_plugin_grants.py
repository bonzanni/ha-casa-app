"""P-5 (unified plugin arch): plugin MCP-tool grants + required env vars
derived from the RESOLVED artifact path; fail-closed can_use_tool.

Grant namespace per https://code.claude.com/docs/en/mcp.md ("Plugin MCP tool names"):
mcp__plugin_<plugin>_<server>; server-level (no __<tool> suffix). The
resident/specialist/executor option-builder integration tests live in
tests/test_agent_plugin_binding.py (Task 7)."""
from __future__ import annotations

import json
import logging

import pytest

from plugin_grants import (
    blocking_unresolved_env_vars_for_resolved,
    declared_absent_env_vars_for_resolved,
    env_remediation_hint,
    grants_for_resolved,
    grants_for_resolution,
    make_fail_closed_can_use_tool,
    protected_map,
    required_env_vars_for_resolved,
    sanitize_segment,
    sanitized_env_for_resolution,
    setup_secrets_ready,
    unresolved_env_vars_for_resolved,
    withhold_env_unresolved,
)
from plugin_registry import ResolutionResult, ResolvedPlugin

pytestmark = pytest.mark.unit


def _artifact(tmp_path, name="lesina-invoice", servers=None,
             manifest=None, artifact_id="0" * 64) -> ResolvedPlugin:
    """A store-artifact dir with an optional .mcp.json at its ROOT, wrapped as
    a ResolvedPlugin (the resolved object grants derive from)."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    if servers is not None:
        (root / ".mcp.json").write_text(
            json.dumps({"mcpServers": servers}), encoding="utf-8")
    return ResolvedPlugin(name=name, artifact_id=artifact_id, path=str(root),
                          version="1.0.0", manifest=manifest or {})


def test_sanitize_keeps_hyphens_and_underscores():
    assert sanitize_segment("lesina-invoice") == "lesina-invoice"
    assert sanitize_segment("a_b-c9") == "a_b-c9"


def test_sanitize_replaces_other_chars():
    assert sanitize_segment("my plugin.v2") == "my_plugin_v2"


def test_grants_for_resolved_live_pin(tmp_path):
    """Pin the exact live-verified string (guards format drift)."""
    rp = _artifact(tmp_path, "lesina-invoice",
                   {"lesina-invoice": {"command": "node"}})
    assert grants_for_resolved(rp) == ["mcp__plugin_lesina-invoice_lesina-invoice"]


def test_grants_for_resolved_multi_server(tmp_path):
    rp = _artifact(tmp_path, "multi", {"alpha": {}, "beta": {}})
    assert grants_for_resolved(rp) == [
        "mcp__plugin_multi_alpha", "mcp__plugin_multi_beta",
    ]


def test_grants_for_resolved_skill_only_is_empty(tmp_path):
    rp = _artifact(tmp_path, "skills-only", servers=None)  # no .mcp.json
    assert grants_for_resolved(rp) == []


def test_grants_for_resolved_sanitizes_segments(tmp_path):
    rp = _artifact(tmp_path, "weird-name", {"srv.one two": {}})
    assert grants_for_resolved(rp) == ["mcp__plugin_weird-name_srv_one_two"]


def test_grants_for_resolved_corrupt_mcp_json_degrades(tmp_path, caplog):
    rp = _artifact(tmp_path, "bad", servers={"srv": {}})
    (tmp_path / "bad" / ".mcp.json").write_text("{not json", encoding="utf-8")
    # The .mcp.json parsing now lives in plugin_store.mcp_servers_map (shared with
    # the build-time verifier); its DEBUG log records the unreadable path.
    with caplog.at_level(logging.DEBUG, logger="plugin_store"):
        assert grants_for_resolved(rp) == []
    assert any("bad" in r.message for r in caplog.records)


def test_grants_for_resolution_unions_and_sorts(tmp_path):
    a = _artifact(tmp_path, "aa", {"zeta": {}})
    b = _artifact(tmp_path, "bb", {"alpha": {}, "beta": {}})
    skill = _artifact(tmp_path, "cc", servers=None)
    res = ResolutionResult(registry_valid=True, plugins=[a, b, skill])
    assert grants_for_resolution(res) == [
        "mcp__plugin_aa_zeta", "mcp__plugin_bb_alpha", "mcp__plugin_bb_beta",
    ]


def test_required_env_vars_extracted(tmp_path):
    rp = _artifact(tmp_path, "needs-secret",
                   {"srv": {"env": {"KEY": "${MY_API_KEY}"}}})
    assert required_env_vars_for_resolved(rp) == ["MY_API_KEY"]


def test_required_env_vars_skill_only_returns_empty(tmp_path):
    """Sol F4: no .mcp.json → [] with no exception."""
    rp = _artifact(tmp_path, "skill", servers=None)
    assert required_env_vars_for_resolved(rp) == []


def test_required_env_vars_malformed_json_degrades(tmp_path):
    rp = _artifact(tmp_path, "corrupt", servers={"srv": {}})
    (tmp_path / "corrupt" / ".mcp.json").write_text("{broken", encoding="utf-8")
    assert required_env_vars_for_resolved(rp) == []


def test_unresolved_env_vars_flags_missing_empty_and_opref(tmp_path):
    # #424: a var is UNRESOLVED when it is absent from the effective
    # environment, empty, or still an op:// reference (boot/reload
    # resolution failed and fell back to the raw value) — the same rule
    # verify_plugin_state applies. A real value is resolved.
    rp = _artifact(tmp_path, "gmailish", servers={"srv": {"env": {
        "A": "${A_KEY}", "B": "${B_KEY}", "C": "${C_KEY}", "D": "${D_KEY}",
    }}})
    env = {"B_KEY": "", "C_KEY": "op://vault/item/field", "D_KEY": "real-value"}
    assert unresolved_env_vars_for_resolved(rp, env) == [
        "A_KEY", "B_KEY", "C_KEY"]


def test_unresolved_env_vars_malformed_mcp_json_degrades_open(tmp_path):
    # DELIBERATE fail-open (Sol 4 / Terra 2 rebuttal): an unparseable
    # declaration yields no servers from the shared parser, so the CLI can
    # spawn nothing from it — there is no placeholder-credential threat for
    # this gate to block. Malformed-ness gates READINESS on the verification
    # surface (parse_mcp_servers' malformed flag), not session admission.
    rp = _artifact(tmp_path, "corrupt2", servers={"srv": {}})
    (tmp_path / "corrupt2" / ".mcp.json").write_text("{broken",
                                                     encoding="utf-8")
    assert unresolved_env_vars_for_resolved(rp, {}) == []


def test_unresolved_env_vars_single_read_survives_concurrent_delete(tmp_path):
    # #424 r4 (Sol r4-3 / Terra r4-3): the check runs in a worker thread
    # while reload_plugin_env can pop keys from os.environ — the value must
    # be fetched ONCE, or a get-then-index pair raises KeyError mid-build.
    class _VanishingEnv(dict):
        def get(self, key, default=None):
            return "present-at-first-read"
        def __getitem__(self, key):
            raise KeyError(key)

    rp = _artifact(tmp_path, "racy", servers={"srv": {"env": {
        "K": "${RACY_KEY}"}}})
    assert unresolved_env_vars_for_resolved(rp, _VanishingEnv()) == []


def test_unresolved_env_vars_all_resolved_or_none_required(tmp_path):
    rp = _artifact(tmp_path, "wired", servers={"srv": {"env": {
        "K": "${WIRED_KEY}"}}})
    assert unresolved_env_vars_for_resolved(rp, {"WIRED_KEY": "v"}) == []
    skill_only = _artifact(tmp_path, "skillonly", servers=None)
    assert unresolved_env_vars_for_resolved(skill_only, {}) == []


async def test_fail_closed_callback_denies_with_log(caplog):
    from claude_agent_sdk import PermissionResultDeny
    cb = make_fail_closed_can_use_tool("finance")
    with caplog.at_level(logging.WARNING, logger="plugin_grants"):
        result = await cb("mcp__something__tool", {"x": 1}, None)
    assert isinstance(result, PermissionResultDeny)
    assert "mcp__something__tool" in result.message
    assert "finance" in result.message
    assert result.interrupt is False


def test_grants_from_top_level_mcp_json(tmp_path):
    """CI/real-world: a plugin .mcp.json may declare its server at the TOP LEVEL
    with no `mcpServers` wrapper (context7:
    {"context7": {"command": "npx", ...}}). The grant must still derive. Only
    the image build (which fetches the real context7) surfaced this."""
    art = tmp_path / "art"
    art.mkdir()
    (art / ".mcp.json").write_text(json.dumps(
        {"context7": {"command": "npx", "args": ["-y", "@upstash/context7-mcp"]}}),
        encoding="utf-8")
    rp = ResolvedPlugin(name="context7", artifact_id="a" * 64, path=str(art),
                        version="0.0.0", manifest={})
    assert grants_for_resolved(rp) == ["mcp__plugin_context7_context7"]
    # The `mcpServers` wrapper shape still works.
    (art / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"foo": {"command": "x"}}}), encoding="utf-8")
    rp2 = ResolvedPlugin(name="p", artifact_id="a" * 64, path=str(art),
                         version="0.0.0", manifest={})
    assert grants_for_resolved(rp2) == ["mcp__plugin_p_foo"]


def test_top_level_args_only_is_malformed_no_grant(tmp_path):
    """Sol CI-review HIGH: a top-level entry with `args` but NO command/url is not
    a runnable server → no grant AND flagged malformed (must not verify green)."""
    from plugin_grants import mcp_json_malformed
    art = tmp_path / "art"
    art.mkdir()
    (art / ".mcp.json").write_text(json.dumps({"svc": {"args": []}}),
                                   encoding="utf-8")
    rp = ResolvedPlugin(name="p", artifact_id="a" * 64, path=str(art),
                        version="0.0.0", manifest={})
    assert grants_for_resolved(rp) == []
    assert mcp_json_malformed(rp) is True


def _parse(tmp_path, data):
    from plugin_store import parse_mcp_servers
    p = tmp_path / ".mcp.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return parse_mcp_servers(p)


def test_parse_null_mcpservers_is_malformed(tmp_path):
    """Sol CI-review-2 HIGH #2a: `{"mcpServers": null}` must be caught by the
    wrapper branch on KEY presence (not fall through to top-level → false-green)."""
    servers, malformed = _parse(tmp_path, {"mcpServers": None})
    assert servers == {} and malformed is True


def test_parse_mixed_top_level_one_invalid_is_malformed(tmp_path):
    """Sol CI-review-2 HIGH #2b: a valid sibling must NOT suppress an invalid
    top-level server — malformed if ANY candidate lacks command/url."""
    servers, malformed = _parse(
        tmp_path, {"good": {"command": "x"}, "bad": {"args": []}})
    assert list(servers) == ["good"] and malformed is True


def test_parse_wrapper_nondict_entry_is_malformed(tmp_path):
    """Sol CI-review-2 HIGH #2c: a non-dict wrapper entry is dropped from grants
    but must set malformed (not silently removed → false-green)."""
    servers, malformed = _parse(tmp_path, {"mcpServers": {"svc": "notadict"}})
    assert servers == {} and malformed is True


def test_parse_wrapper_empty_config_grants_but_malformed(tmp_path):
    """Sol CI-review-2: a wrapper server with no command/url still yields its
    grant (from the key) but is malformed (can't run) → blocks readiness."""
    servers, malformed = _parse(tmp_path, {"mcpServers": {"probe": {}}})
    assert list(servers) == ["probe"] and malformed is True


def test_parse_empty_string_command_is_malformed(tmp_path):
    """Sol CI-review-2: command/url must be NON-empty strings."""
    servers, malformed = _parse(tmp_path, {"mcpServers": {"s": {"command": ""}}})
    assert list(servers) == ["s"] and malformed is True


def test_parse_url_only_server_is_valid(tmp_path):
    """A url-declaring (http/sse) server is runnable — not malformed."""
    servers, malformed = _parse(
        tmp_path, {"mcpServers": {"s": {"url": "http://x"}}})
    assert list(servers) == ["s"] and malformed is False


# --- A:§3.7 protected_map (v0.76.0) ------------------------------------------


def test_protected_map_absent_is_empty(tmp_path):
    rp = _artifact(tmp_path, "lesina-invoice",
                   {"lesina-invoice": {"command": "node"}})
    res = ResolutionResult(registry_valid=True, plugins=[rp])
    assert protected_map(res) == {}


def test_protected_map_single_server(tmp_path):
    rp = _artifact(
        tmp_path, "lesina-invoice", {"lesina-invoice": {"command": "node"}},
        manifest={"casa": {"protectedTools": ["invoice_reset"]}})
    res = ResolutionResult(registry_valid=True, plugins=[rp])
    assert protected_map(res) == {
        "mcp__plugin_lesina-invoice_lesina-invoice__invoice_reset":
            {"artifact_id": "0" * 64, "summary": None},
    }


def test_protected_map_expands_across_two_declared_servers(tmp_path):
    """A bare tool name protects that tool on EVERY server the plugin
    declares."""
    rp = _artifact(
        tmp_path, "multi", {"alpha": {"command": "x"}, "beta": {"command": "y"}},
        manifest={"casa": {"protectedTools": ["do_thing"]}},
        artifact_id="a" * 64)
    res = ResolutionResult(registry_valid=True, plugins=[rp])
    assert protected_map(res) == {
        "mcp__plugin_multi_alpha__do_thing": {"artifact_id": "a" * 64,
                                              "summary": None},
        "mcp__plugin_multi_beta__do_thing": {"artifact_id": "a" * 64,
                                             "summary": None},
    }


def test_protected_map_sanitizes_segments(tmp_path):
    rp = _artifact(
        tmp_path, "weird-name", {"srv.one two": {"command": "x"}},
        manifest={"casa": {"protectedTools": ["do the thing"]}})
    res = ResolutionResult(registry_valid=True, plugins=[rp])
    assert protected_map(res) == {
        "mcp__plugin_weird-name_srv_one_two__do_the_thing":
            {"artifact_id": "0" * 64, "summary": None},
    }


def test_protected_map_per_plugin_degradation(tmp_path, caplog):
    """A malformed casa.protectedTools in ONE resolved plugin excludes just
    that plugin's tools; a healthy sibling still contributes normally."""
    good = _artifact(
        tmp_path, "good", {"good": {"command": "x"}},
        manifest={"casa": {"protectedTools": ["safe_tool"]}},
        artifact_id="a" * 64)
    bad = _artifact(
        tmp_path, "bad", {"bad": {"command": "x"}},
        manifest={"casa": {"protectedTools": ["ok", 1]}},
        artifact_id="b" * 64)
    res = ResolutionResult(registry_valid=True, plugins=[good, bad])
    with caplog.at_level(logging.WARNING, logger="plugin_grants"):
        out = protected_map(res)
    assert out == {"mcp__plugin_good_good__safe_tool":
                   {"artifact_id": "a" * 64, "summary": None}}
    assert any("bad" in r.message for r in caplog.records)


def test_protected_map_object_entry_carries_summary(tmp_path):
    """v0.78.0 W1: an object-form protectedTools entry with a summary
    threads it into the map value alongside artifact_id."""
    rp = _artifact(
        tmp_path, "lesina-invoice", {"lesina-invoice": {"command": "node"}},
        manifest={"casa": {"protectedTools": [
            {"name": "invoice_reset",
             "summary": "Delete the invoice draft for {period}"},
        ]}})
    res = ResolutionResult(registry_valid=True, plugins=[rp])
    assert protected_map(res) == {
        "mcp__plugin_lesina-invoice_lesina-invoice__invoice_reset": {
            "artifact_id": "0" * 64,
            "summary": "Delete the invoice draft for {period}",
        },
    }


def test_protected_map_no_servers_contributes_nothing(tmp_path):
    """A skill-only plugin (no .mcp.json) declaring protectedTools has no
    server to qualify the tool name with — contributes nothing."""
    rp = _artifact(
        tmp_path, "skills-only", servers=None,
        manifest={"casa": {"protectedTools": ["x"]}})
    res = ResolutionResult(registry_valid=True, plugins=[rp])
    assert protected_map(res) == {}


def test_protected_map_empty_resolution():
    assert protected_map(ResolutionResult(registry_valid=True)) == {}


# ---------------------------------------------------------------------------
# #429: casa.setupProvides — breaking the setup-provisioning deadlock
# ---------------------------------------------------------------------------

_BANKFEED_SERVERS = {"bank-feed": {"command": "node", "env": {
    "KEY": "${CASA_PLUGIN_BANKFEED_PRIVATE_KEY}",
    "APP": "${CASA_PLUGIN_BANKFEED_APP_ID}",
    "CP": "${CASA_PLUGIN_BANKFEED_CP_TOKEN:-}",   # #431: never extracted
    "OP": "${OP_SERVICE_ACCOUNT_TOKEN}",
}}}

_BANKFEED_MANIFEST = {"casa": {
    "setupTool": "setup_bank_feed",
    "setupProvides": ["CASA_PLUGIN_BANKFEED_PRIVATE_KEY",
                      "CASA_PLUGIN_BANKFEED_APP_ID"],
}}


def _bankfeed(tmp_path, manifest=None):
    return _artifact(tmp_path, "bank-feed", _BANKFEED_SERVERS,
                     manifest=_BANKFEED_MANIFEST if manifest is None
                     else manifest)


def test_declared_absent_unions_both_fields(tmp_path):
    assert declared_absent_env_vars_for_resolved(_bankfeed(tmp_path)) == {
        "CASA_PLUGIN_BANKFEED_PRIVATE_KEY", "CASA_PLUGIN_BANKFEED_APP_ID"}


def test_declared_absent_undeclared_plugin_is_empty(tmp_path):
    rp = _artifact(tmp_path, "plain", {"srv": {"env": {"K": "${K_KEY}"}}})
    assert declared_absent_env_vars_for_resolved(rp) == set()


def test_declared_absent_fails_closed_on_malformed_declaration(
        tmp_path, caplog):
    """A declaration that does not parse must contribute NOTHING: it RELAXES
    the withholding gate, so reading it loosely would exempt a var the author
    never wrote. The plugin keeps the strict pre-#429 behaviour."""
    rp = _bankfeed(tmp_path, manifest={"casa": {
        "setupTool": "setup_bank_feed", "setupProvides": "not-a-list"}})
    with caplog.at_level(logging.WARNING, logger="plugin_grants"):
        assert declared_absent_env_vars_for_resolved(rp) == set()
    assert any("bank-feed" in r.message for r in caplog.records)


def test_declared_absent_ignores_setup_provides_without_setup_tool(tmp_path):
    """setupProvides without a setupTool is refused by plugin_store (it would
    be an undeclared 'optional' marker), so it exempts nothing here either."""
    rp = _bankfeed(tmp_path, manifest={"casa": {
        "setupProvides": ["CASA_PLUGIN_BANKFEED_PRIVATE_KEY"]}})
    assert declared_absent_env_vars_for_resolved(rp) == set()


def test_blocking_unresolved_excludes_declared_but_keeps_the_rest(tmp_path):
    """THE #429 BUG: four vars unresolved, three of them declared. Only the
    genuinely-missing operator secret may block."""
    rp = _bankfeed(tmp_path)
    # The CP token is a ${VAR:-} reference, so the extractor never sees it
    # at all (#431) — no declaration needed and nothing to withhold for.
    assert unresolved_env_vars_for_resolved(rp, {}) == [
        "CASA_PLUGIN_BANKFEED_APP_ID",
        "CASA_PLUGIN_BANKFEED_PRIVATE_KEY", "OP_SERVICE_ACCOUNT_TOKEN"]
    assert blocking_unresolved_env_vars_for_resolved(rp, {}) == [
        "OP_SERVICE_ACCOUNT_TOKEN"]


def test_blocking_unresolved_empty_once_undeclared_var_is_wired(tmp_path):
    rp = _bankfeed(tmp_path)
    assert blocking_unresolved_env_vars_for_resolved(
        rp, {"OP_SERVICE_ACCOUNT_TOKEN": "ops_tok"}) == []


def test_withhold_admits_the_setup_provisioning_plugin(tmp_path):
    """RED before #429: withhold_env_unresolved gated on the RAW unresolved
    set, so bank-feed was withheld from every session build and its setup
    tool could never run to create the credentials it was withheld for."""
    rp = _bankfeed(tmp_path)
    res = ResolutionResult(registry_valid=True, plugins=[rp])
    # The environment is STATED, never inherited. Read from `os.environ` this
    # case passes on a machine with the token exported and fails in CI without
    # it — which is exactly what happened, and main was red for two releases
    # behind a green local suite.
    kept, withheld = withhold_env_unresolved(
        res, context="test", environ={"OP_SERVICE_ACCOUNT_TOKEN": "ops_tok"})
    assert withheld == []
    assert [p.name for p in kept.plugins] == ["bank-feed"]


def test_withhold_still_blocks_an_undeclared_missing_secret(tmp_path):
    rp = _artifact(tmp_path, "gmailish",
                   {"srv": {"env": {"K": "${GMAIL_CLIENT_ID}"}}},
                   manifest={"casa": {"setupTool": "setup_gmail"}})
    res = ResolutionResult(registry_valid=True, plugins=[rp])
    kept, withheld = withhold_env_unresolved(res, context="test", environ={})
    assert [p.name for p in kept.plugins] == []
    assert [(p.name, m) for p, m in withheld] == [
        ("gmailish", ["GMAIL_CLIENT_ID"])]


def test_sanitized_env_empties_every_declared_unresolved_var(tmp_path):
    """The other half of the fix: an UNDEFINED ${VAR} reaches the MCP server
    as the LITERAL placeholder (#423), so every var the gate now exempts must
    be pinned to an empty string instead."""
    res = ResolutionResult(registry_valid=True, plugins=[_bankfeed(tmp_path)])
    assert sanitized_env_for_resolution(res, {}) == {
        "CASA_PLUGIN_BANKFEED_PRIVATE_KEY": "",
        "CASA_PLUGIN_BANKFEED_APP_ID": "",
    }


def test_sanitized_env_never_overwrites_a_wired_value(tmp_path):
    """Once setup has landed a credential the overlay must not empty it."""
    res = ResolutionResult(registry_valid=True, plugins=[_bankfeed(tmp_path)])
    env = {"CASA_PLUGIN_BANKFEED_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----",
           "CASA_PLUGIN_BANKFEED_APP_ID": "app-123"}
    assert sanitized_env_for_resolution(res, env) == {}


def test_sanitized_env_empties_an_unresolvable_op_reference(tmp_path):
    """A var left holding a raw op:// ref is not a credential — handing the
    literal to the server is the placeholder failure in another spelling."""
    res = ResolutionResult(registry_valid=True, plugins=[_bankfeed(tmp_path)])
    env = {"CASA_PLUGIN_BANKFEED_APP_ID": "op://Casa/bank-feed/app_id"}
    assert sanitized_env_for_resolution(res, env)["CASA_PLUGIN_BANKFEED_APP_ID"] == ""


def test_sanitized_env_omits_undeclared_vars(tmp_path):
    """An undeclared missing secret withholds the plugin; it must never be
    quietly emptied instead."""
    res = ResolutionResult(registry_valid=True, plugins=[_bankfeed(tmp_path)])
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in sanitized_env_for_resolution(
        res, {})


def test_sanitized_env_empty_for_a_plugin_with_no_declarations(tmp_path):
    rp = _artifact(tmp_path, "plain", {"srv": {"env": {"K": "${K_KEY}"}}})
    res = ResolutionResult(registry_valid=True, plugins=[rp])
    assert sanitized_env_for_resolution(res, {}) == {}


def test_remediation_hint_names_the_app_option_not_plugin_env():
    """OP_SERVICE_ACCOUNT_TOKEN comes from an app option exported by
    svc-casa/run — telling the operator to set_plugin_env_reference it is
    wrong advice (and an op:// entry would need the token to resolve)."""
    hint = env_remediation_hint(["OP_SERVICE_ACCOUNT_TOKEN"])
    assert "onepassword_service_account_token" in hint
    assert "set_plugin_env_reference" not in hint


def test_remediation_hint_splits_owned_and_plugin_wired():
    hint = env_remediation_hint(["OP_SERVICE_ACCOUNT_TOKEN", "SOME_API_KEY"])
    assert "onepassword_service_account_token" in hint
    assert "set_plugin_env_reference" in hint
    assert "SOME_API_KEY" in hint


def test_setup_secrets_ready_admits_the_deadlocked_plugin(tmp_path):
    """#429 END TO END at the gate: the settled episode must dispatch once
    only setup-provisioned/optional vars remain unresolved — otherwise setup
    cannot run until the credentials exist, and the credentials only exist
    after setup runs. Nothing re-kicks it: the retries fire on plugin_env and
    agent reloads, neither of which can supply a value only setup produces."""
    res = ResolutionResult(registry_valid=True, plugins=[_bankfeed(tmp_path)])
    assert setup_secrets_ready(
        res, "bank-feed", {"OP_SERVICE_ACCOUNT_TOKEN": "ops_tok"}) is True


def test_setup_secrets_ready_still_holds_on_an_undeclared_secret(tmp_path):
    res = ResolutionResult(registry_valid=True, plugins=[_bankfeed(tmp_path)])
    assert setup_secrets_ready(res, "bank-feed", {}) is False


def test_setup_secrets_ready_false_for_an_unresolvable_plugin(tmp_path):
    res = ResolutionResult(registry_valid=True, plugins=[])
    assert setup_secrets_ready(res, "bank-feed", {}) is False
