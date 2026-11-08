"""Tests for the delegate_to_agent delegation ACL (spec A1)."""
from __future__ import annotations
import json
from unittest.mock import AsyncMock, MagicMock
import pytest
from config import AgentConfig, CharacterConfig, DelegateEntry, ToolsConfig

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _cfg(role, delegates=()):
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role)
    cfg.character = CharacterConfig(name=role.title(), archetype=role, card="", prompt=f"You are {role}.")
    cfg.enabled = True; cfg.model = "haiku"
    cfg.tools = ToolsConfig(allowed=["Read"], disallowed=[], permission_mode="acceptEdits", max_turns=5)
    cfg.delegates = [DelegateEntry(agent=d, purpose="p", when="w") for d in delegates]
    cfg.system_prompt = f"You are {role}."
    return cfg


def _full_specialist_reg():
    reg = MagicMock()
    reg.get.return_value = None
    reg.register_delegation = AsyncMock()
    reg.cancel_delegation = AsyncMock()
    reg.fail_delegation = AsyncMock()
    reg.complete_delegation = AsyncMock()
    return reg


def _init(agent_role_map):
    from tools import init_tools
    init_tools(channel_manager=MagicMock(), bus=MagicMock(),
               specialist_registry=_full_specialist_reg(), mcp_registry=MagicMock(),
               trigger_registry=MagicMock(), engagement_registry=MagicMock(),
               agent_role_map=agent_role_map)


async def _call(target, *, role="", execution_role=None, mode="sync",
                monkeypatch=None, extra_origin=None, extra_args=None):
    """Invoke delegate_to_agent with a synthetic origin.

    Caller identity is set ONLY via origin (role / execution_role) — the
    handler must never read identity from tool args. ``extra_args`` lets a
    test plant decoy role-like fields in the args to prove they're ignored.
    """
    import agent as agent_mod
    from tools import delegate_to_agent
    if monkeypatch is not None:
        import tools as tm
        async def _never_run(*a, **k):  # guard: ACL denials must not launch
            raise AssertionError("delegated runner reached despite ACL")
        monkeypatch.setattr(tm, "_run_delegated_agent", _never_run)
    origin = {"role": role, "channel": "telegram",
              "chat_id": "c1", "cid": "t", "user_text": "hi"}
    if execution_role is not None:
        origin["execution_role"] = execution_role
    if extra_origin:
        origin.update(extra_origin)
    args = {"agent": target, "task": "t", "context": "", "mode": mode}
    if extra_args:
        args.update(extra_args)
    token = agent_mod.origin_var.set(origin)
    try:
        res = await delegate_to_agent.handler(args)
    finally:
        agent_mod.origin_var.reset(token)
    return json.loads(res["content"][0]["text"])


class TestDelegationACL:
    async def test_undeclared_target_denied(self, monkeypatch):
        _init({"concierge": _cfg("concierge", ["mtg"]), "assistant": _cfg("assistant", ["finance"])})
        p = await _call("assistant", role="concierge", monkeypatch=monkeypatch)
        assert p["kind"] == "delegation_not_declared"

    async def test_undeclared_absent_target_denied_before_lookup(self, monkeypatch):
        # The undeclared target is ABSENT from the role map. An impl that
        # resolved the target before the ACL would still deny (unknown_agent)
        # — so we ALSO assert the registry fallback was never consulted,
        # proving the ACL fires strictly before any lookup.
        import tools as tm
        _init({"concierge": _cfg("concierge", ["mtg"])})
        p = await _call("ghosttarget", role="concierge", monkeypatch=monkeypatch)
        assert p["kind"] == "delegation_not_declared"
        tm._specialist_registry.get.assert_not_called()

    async def test_unknown_caller_denied(self, monkeypatch):
        _init({"assistant": _cfg("assistant", ["finance"])})
        assert (await _call("assistant", role="ghost", monkeypatch=monkeypatch))["kind"] == "delegation_not_declared"

    async def test_missing_caller_role_denied(self, monkeypatch):
        _init({"assistant": _cfg("assistant", ["finance"])})
        assert (await _call("assistant", role="", monkeypatch=monkeypatch))["kind"] == "delegation_not_declared"

    async def test_missing_role_at_depth1_denied_not_depth_exceeded(self, monkeypatch):
        # A role-less caller ALREADY at depth 1: the ACL must fire before the
        # depth-cap branch, so the denial is delegation_not_declared (caller
        # identity), NOT delegation_depth_exceeded.
        _init({"assistant": _cfg("assistant", ["finance"])})
        p = await _call("assistant", role="", monkeypatch=monkeypatch,
                        extra_origin={"delegation_depth": 1})
        assert p["kind"] == "delegation_not_declared"

    async def test_declared_but_absent_target_reaches_lookup(self):
        _init({"assistant": _cfg("assistant", ["finance"])})
        assert (await _call("finance", role="assistant"))["kind"] == "unknown_agent"

    async def test_reload_synced_map_enforced(self, monkeypatch):
        # After sync_agent_role_map rebuilds the map, the ACL uses the NEW
        # delegates (forged/removed declarations take effect immediately).
        import tools as tm
        _init({"assistant": _cfg("assistant", ["finance"])})
        runtime = MagicMock()
        runtime.role_configs = {"assistant": _cfg("assistant", [])}  # finance removed
        runtime.specialist_registry = MagicMock(all_configs=MagicMock(return_value={}))
        tm.sync_agent_role_map(runtime)
        assert (await _call("finance", role="assistant", monkeypatch=monkeypatch))["kind"] == "delegation_not_declared"

    async def test_delegated_specialist_judged_by_own_delegates(self, monkeypatch):
        # THE fix: in a delegated turn origin["role"] is the PARENT
        # (assistant, which DECLARES finance) but execution_role is the
        # running delegate (mtg, which declares nothing). The ACL must key on
        # execution_role, so mtg->finance is DENIED even though the parent
        # would have been allowed. An impl keyed on `role` would wrongly PASS.
        _init({"assistant": _cfg("assistant", ["finance"]),
               "mtg": _cfg("mtg", []),
               "finance": _cfg("finance", [])})
        p = await _call("finance", role="assistant", execution_role="mtg",
                        monkeypatch=monkeypatch)
        assert p["kind"] == "delegation_not_declared"

    async def test_arg_role_spoof_ignored(self, monkeypatch):
        # Caller identity must come only from trusted origin. An attacker who
        # plants privileged role-like fields in the TOOL ARGS while running as
        # an unprivileged delegate (execution_role=mtg, declares nothing) is
        # still denied — the args are never consulted for identity.
        _init({"assistant": _cfg("assistant", ["finance"]),
               "mtg": _cfg("mtg", []),
               "finance": _cfg("finance", [])})
        p = await _call("finance", role="mtg", execution_role="mtg",
                        monkeypatch=monkeypatch,
                        extra_args={"role": "assistant",
                                    "execution_role": "assistant",
                                    "caller_role": "assistant"})
        assert p["kind"] == "delegation_not_declared"

    @pytest.mark.parametrize("mode", ["sync", "async"])
    async def test_denial_uniform_across_modes(self, monkeypatch, mode):
        # The ACL denies before any mode-specific gate, so an undeclared
        # target is refused identically regardless of requested mode.
        _init({"concierge": _cfg("concierge", ["mtg"]), "assistant": _cfg("assistant", ["finance"])})
        p = await _call("assistant", role="concierge", mode=mode, monkeypatch=monkeypatch)
        assert p["kind"] == "delegation_not_declared"


def _named(role, name, delegates=()):
    """A config whose persona display name DIFFERS from its role id — the
    #433 shape. ``_cfg`` uses ``role.title()``, which differs only by case
    and so cannot tell alias resolution apart from a casefold."""
    cfg = _cfg(role, delegates)
    cfg.character.name = name
    return cfg


class TestDelegationAliasResolution:
    """#433: a persona display name addresses a delegate the caller already
    declares. Resolution is scoped to the CALLER'S OWN declared set, so the
    resolved value is inside the ACL by construction and no display-name
    collision elsewhere in the deployment can widen it."""

    async def _launched_role(self, target, monkeypatch):
        """The role actually LAUNCHED for *target* — the canonical value the
        rest of the pipeline keys on — or the denial kind if a gate refused."""
        import tools as tm
        seen = {}

        async def _capture(cfg, *a, **k):
            seen["role"] = cfg.role
            return tm.DelegatedOutput(text="ok", structured=None)

        monkeypatch.setattr(tm, "_run_delegated_agent_bounded", _capture)
        monkeypatch.setattr(tm, "_run_delegated_agent", _capture)
        p = await _call(target, role="assistant")
        return seen.get("role", p.get("kind"))

    async def test_persona_name_addresses_the_same_delegate_as_its_role_id(
            self, monkeypatch):
        _init({"assistant": _named("assistant", "Ada", ["finance"]),
               "finance": _named("finance", "Alex")})
        assert await self._launched_role("Alex", monkeypatch) == "finance"

    async def test_persona_name_is_case_and_space_insensitive(self, monkeypatch):
        _init({"assistant": _named("assistant", "Ada", ["finance"]),
               "finance": _named("finance", "Alex")})
        assert await self._launched_role("  aLeX ", monkeypatch) == "finance"

    async def test_undeclared_persona_name_still_denied(self, monkeypatch):
        # Alex exists and is addressable BY NAME, but this caller does not
        # declare finance. Resolution must not reach outside `declared`.
        _init({"concierge": _named("concierge", "Cleo", ["mtg"]),
               "mtg": _named("mtg", "Morgan"),
               "finance": _named("finance", "Alex")})
        p = await _call("Alex", role="concierge", monkeypatch=monkeypatch)
        assert p["kind"] == "delegation_not_declared"

    async def test_ambiguous_persona_name_denied(self, monkeypatch):
        # Two DECLARED delegates share a display name: there is no single
        # right answer, so an authorization boundary must refuse rather than
        # pick. `setdefault`-style first-wins is exactly what we avoid.
        _init({"assistant": _named("assistant", "Ada", ["finance", "billing"]),
               "finance": _named("finance", "Alex"),
               "billing": _named("billing", "Alex")})
        p = await _call("Alex", role="assistant", monkeypatch=monkeypatch)
        assert p["kind"] == "delegation_ambiguous_name"
        assert sorted(p["candidates"]) == ["billing", "finance"]

    async def test_exact_role_id_wins_over_another_delegates_name(self):
        # `billing`'s display name is the literal string "finance". An exact
        # role-id match must be taken FIRST, or addressing the finance role
        # would silently land on billing. `finance` is declared but absent
        # from the role map, so the two outcomes are distinguishable: exact
        # match falls through to unknown_agent NAMING finance, while alias
        # resolution would find billing and sail past target resolution.
        _init({"assistant": _named("assistant", "Ada", ["finance", "billing"]),
               "billing": _named("billing", "finance")})
        p = await _call("finance", role="assistant")
        assert p["kind"] == "unknown_agent"
        assert "'finance'" in p["message"]


class TestDelegationNonStringTarget:
    """`args` can carry any JSON type on the paths that do not run the schema
    validator first — the same condition the `mode` coercion is explicitly
    TOTAL over. A non-string `agent` must reach the uniform denial, never
    raise: alias resolution calls `.strip()`, and a bare `7` would otherwise
    `AttributeError` BEFORE the ACL, escaping the gate entirely."""

    @pytest.mark.parametrize("hostile", [7, True, ["finance"], {"a": 1}, None])
    async def test_non_string_target_is_denied_not_raised(
            self, monkeypatch, hostile):
        _init({"assistant": _named("assistant", "Ada", ["finance"]),
               "finance": _named("finance", "Alex")})
        p = await _call(hostile, role="assistant", monkeypatch=monkeypatch)
        assert p["kind"] == "delegation_not_declared"


class TestDelegationNonStringPayload:
    """The `agent` coercion has a parallel path: `task` and `context` reach
    `len()` in the input-bounds gate with no type normalization, so a
    schema-bypassing `{"task": 7}` raised TypeError out of the gate instead
    of denying. Pre-existing, not introduced by the alias work — found while
    auditing the same arbitrary-JSON class."""

    @pytest.mark.parametrize("field", ["task", "context"])
    @pytest.mark.parametrize("hostile", [7, ["x"], {"a": 1}])
    async def test_non_string_payload_field_is_denied_not_raised(
            self, monkeypatch, field, hostile):
        _init({"assistant": _named("assistant", "Ada", ["finance"]),
               "finance": _named("finance", "Alex")})
        p = await _call("finance", role="assistant", monkeypatch=monkeypatch,
                        extra_args={field: hostile})
        assert p["kind"] == "invalid_argument"
        assert p["field"] == field


class TestDelegationDenialDiagnostic:
    """#433: the refusal must distinguish 'not wired' from 'wrong key for a
    wired delegate', and name what it would accept."""

    async def test_denial_enumerates_accepted_values(self, monkeypatch):
        _init({"assistant": _named("assistant", "Ada", ["finance"]),
               "finance": _named("finance", "Alex")})
        p = await _call("nonesuch", role="assistant", monkeypatch=monkeypatch)
        assert p["kind"] == "delegation_not_declared"
        assert p["declared_delegates"] == [{"role": "finance", "name": "Alex"}]

    async def test_denial_delegate_list_survives_a_reload(self, monkeypatch):
        # `sync_agent_role_map` refreshes `_agent_role_map` on reload but NOT
        # `_agent_registry`, which `init_tools` captures once at boot. Sourcing
        # callability from the registry meant a specialist disabled at runtime
        # kept being advertised as a valid retry target for the rest of the
        # process. The list is therefore derived from the SAME map the next
        # gate resolves against, at the point of use.
        import tools as tm
        _init({"assistant": _named("assistant", "Ada", ["finance", "mtg"]),
               "finance": _named("finance", "Alex"),
               "mtg": _named("mtg", "Morgan")})
        runtime = MagicMock()
        runtime.role_configs = {"assistant": _named("assistant", "Ada",
                                                    ["finance", "mtg"]),
                                "finance": _named("finance", "Alex")}
        runtime.specialist_registry = MagicMock(
            all_configs=MagicMock(return_value={}))  # mtg disabled/removed
        tm.sync_agent_role_map(runtime)
        p = await _call("nonesuch", role="assistant", monkeypatch=monkeypatch)
        assert p["declared_delegates"] == [{"role": "finance", "name": "Alex"}]

    async def test_denial_omits_declared_but_unavailable_delegates(
            self, monkeypatch):
        # A delegates entry may point at a specialist that is now disabled or
        # removed. `_render_delegates_block` already hides those (they are NOT
        # callable — the tool rejects them with unknown_agent). Advertising
        # one here would send the model to a target that fails at the very
        # next gate — another dead end, which is the bug class #433 is about.
        import tools as tm
        from agent_registry import AgentRegistry
        _init({"assistant": _named("assistant", "Ada", ["finance", "ghost"]),
               "finance": _named("finance", "Alex")})
        tm._agent_registry = AgentRegistry.build(
            residents={"assistant": _named("assistant", "Ada")},
            specialists={"finance": _named("finance", "Alex")},
        )
        try:
            p = await _call("nonesuch", role="assistant", monkeypatch=monkeypatch)
        finally:
            tm._agent_registry = None
        assert p["declared_delegates"] == [{"role": "finance", "name": "Alex"}]

    async def test_absent_delegate_contributes_no_alias_candidate(
            self, monkeypatch):
        # `ghost` is declared but has no config, so it has no persona name.
        # Its role id must not act as a display name, or an absent delegate
        # could collide with a real one and force a false ambiguity refusal.
        _init({"assistant": _named("assistant", "Ada", ["finance", "ghost"]),
               "finance": _named("finance", "ghost")})
        # "ghost" resolves by EXACT role id, never ambiguously.
        p = await _call("ghost", role="assistant", monkeypatch=monkeypatch)
        assert p["kind"] == "unknown_agent"

    async def test_denial_is_logged(self, monkeypatch, caplog):
        # The denial was retroactively undetectable before #433 — no log line
        # and no telemetry — so an operator could not tell how often a
        # delegation had already failed over a naming mismatch.
        _init({"assistant": _named("assistant", "Ada", ["finance"])})
        with caplog.at_level("WARNING", logger="tools"):
            await _call("nonesuch", role="assistant", monkeypatch=monkeypatch)
        assert any("delegation_denied" in r.message
                   and "kind=delegation_not_declared" in r.message
                   for r in caplog.records)

    async def test_denial_log_does_not_echo_the_rejected_token(
            self, monkeypatch, caplog):
        # The rejected token is untrusted model output. `_known_role`
        # collapses an unregistered value to the literal `<other>`.
        _init({"assistant": _named("assistant", "Ada", ["finance"])})
        with caplog.at_level("WARNING", logger="tools"):
            await _call("Robert'); DROP TABLE", role="assistant",
                        monkeypatch=monkeypatch)
        line = next(r.message for r in caplog.records
                    if "delegation_denied" in r.message)
        assert "target=<other>" in line
        assert "DROP TABLE" not in line

    async def test_denial_moves_no_target_keyed_counter(self, monkeypatch):
        # The ACL denial is PRE-AUTHORIZATION and the target is
        # caller-supplied, so it must not move per-role telemetry — an
        # unauthorized caller would otherwise drive another role's counters
        # and grow the stats dict without bound. Pinned here too because
        # #433's own issue text proposed `record_denial` on this exact path;
        # the existing guards live in tests/test_specialist_limits.py.
        import tools as tm
        _init({"assistant": _named("assistant", "Ada", ["finance"]),
               "finance": _named("finance", "Alex")})
        tm._specialist_telemetry = MagicMock()
        try:
            # A REGISTERED role the caller does not declare — the case that
            # `_known_role` does NOT collapse to `<other>`.
            await _call("finance", role="finance", monkeypatch=monkeypatch)
            tm._specialist_telemetry.record_denial.assert_not_called()
        finally:
            tm._specialist_telemetry = None
