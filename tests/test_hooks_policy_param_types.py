"""#442: hook policy parameters are type-checked at BUILD time.

``hooks.yaml`` is schema-validated with ``additionalProperties`` open, so a
mistyped parameter reaches the policy factory intact. Before this, a bare
string was accepted where a list of prefixes was expected and then iterated
CHARACTER by character: ``writable: /config`` became the prefix set
``['/', 'c', 'o', 'n', 'f', 'i', 'g']``, and the lone ``/`` prefix-matches
every absolute path — the guard failed OPEN, admitting exactly the writes it
existed to refuse.

The rule these tests pin: a mistyped parameter raises ``UnknownPolicyError``
out of the factory. ``agent_loader._resolve_hooks_file`` turns that into a
``LoadError`` and the executor fails closed at load, the same way an unknown
parameter name already did.
"""

from __future__ import annotations

from pathlib import Path

import pytest


CTX: dict = {"signal": None}


def _decision(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecision"]


class TestPathScopeParamTypes:
    def test_writable_string_is_refused(self):
        from hooks import UnknownPolicyError, make_path_scope_hook_v2
        with pytest.raises(UnknownPolicyError) as exc:
            make_path_scope_hook_v2(writable="/config", readable=[])
        assert "writable" in str(exc.value)

    def test_readable_string_is_refused(self):
        from hooks import UnknownPolicyError, make_path_scope_hook_v2
        with pytest.raises(UnknownPolicyError) as exc:
            make_path_scope_hook_v2(writable=[], readable="/config")
        assert "readable" in str(exc.value)

    def test_writable_non_string_element_is_refused(self):
        from hooks import UnknownPolicyError, make_path_scope_hook_v2
        with pytest.raises(UnknownPolicyError):
            make_path_scope_hook_v2(writable=["/config", 7], readable=[])

    def test_writable_mapping_is_refused(self):
        from hooks import UnknownPolicyError, make_path_scope_hook_v2
        with pytest.raises(UnknownPolicyError):
            make_path_scope_hook_v2(writable={"/config": True}, readable=[])

    def test_valid_lists_still_build(self):
        from hooks import make_path_scope_hook_v2
        assert make_path_scope_hook_v2(
            writable=["/config"], readable=["/config"]) is not None

    def test_none_still_means_empty(self):
        from hooks import make_path_scope_hook_v2
        assert make_path_scope_hook_v2() is not None


class TestCasaConfigGuardParamTypes:
    def test_forbid_write_paths_string_is_refused(self):
        from hooks import UnknownPolicyError, make_casa_config_guard_hook
        with pytest.raises(UnknownPolicyError) as exc:
            make_casa_config_guard_hook(forbid_write_paths="/data")
        assert "forbid_write_paths" in str(exc.value)

    def test_forbid_write_paths_non_string_element_is_refused(self):
        from hooks import UnknownPolicyError, make_casa_config_guard_hook
        with pytest.raises(UnknownPolicyError):
            make_casa_config_guard_hook(forbid_write_paths=["/data", None])

    def test_forbid_delete_residents_non_bool_is_refused(self):
        from hooks import UnknownPolicyError, make_casa_config_guard_hook
        with pytest.raises(UnknownPolicyError) as exc:
            make_casa_config_guard_hook(forbid_delete_residents="no")
        assert "forbid_delete_residents" in str(exc.value)

    def test_forbid_delete_residents_falsy_non_bool_is_refused(self):
        """The dangerous shape: a falsy non-bool silently DISABLED the guard."""
        from hooks import UnknownPolicyError, make_casa_config_guard_hook
        with pytest.raises(UnknownPolicyError):
            make_casa_config_guard_hook(forbid_delete_residents=0)

    def test_valid_params_still_build(self):
        from hooks import make_casa_config_guard_hook
        assert make_casa_config_guard_hook(
            forbid_write_paths=["/data"], forbid_delete_residents=True,
        ) is not None


class TestCommitSizeGuardParamTypes:
    """r2 (both reviewers): `int(...)` coerced rather than type-checked.

    `max_files: true` built a limit of 1 and `max_files: "5"` a limit of 5 —
    neither fails open, but INV-MCP-007 claims EVERY policy parameter of the
    wrong type fails the build, and those two contradicted it. A list or a
    mapping already raised.
    """

    def test_bool_is_refused(self):
        from hooks import UnknownPolicyError, HOOK_POLICIES
        with pytest.raises(UnknownPolicyError) as exc:
            HOOK_POLICIES["commit_size_guard"]["factory"](max_files=True)
        assert "max_files" in str(exc.value)

    def test_numeric_string_is_refused(self):
        from hooks import UnknownPolicyError, HOOK_POLICIES
        with pytest.raises(UnknownPolicyError):
            HOOK_POLICIES["commit_size_guard"]["factory"](max_files="5")

    def test_float_is_refused(self):
        from hooks import UnknownPolicyError, HOOK_POLICIES
        with pytest.raises(UnknownPolicyError):
            HOOK_POLICIES["commit_size_guard"]["factory"](max_files=5.9)

    def test_list_is_refused(self):
        from hooks import UnknownPolicyError, HOOK_POLICIES
        with pytest.raises(UnknownPolicyError):
            HOOK_POLICIES["commit_size_guard"]["factory"](max_files=[1])

    def test_int_still_builds(self):
        from hooks import HOOK_POLICIES
        assert HOOK_POLICIES["commit_size_guard"]["factory"](
            max_files=50) is not None

    def test_omitted_still_builds(self):
        from hooks import HOOK_POLICIES
        assert HOOK_POLICIES["commit_size_guard"]["factory"]() is not None


class TestMistypedParamFailsClosedThroughTheConfigPath:
    """The two real build paths must both refuse, not just the make_* helper."""

    def test_resolve_hooks_refuses_string_writable(self):
        from hooks import UnknownPolicyError, resolve_hooks
        from config import HooksConfig
        config = HooksConfig(pre_tool_use=[
            {"policy": "path_scope", "writable": "/config", "readable": []},
        ])
        with pytest.raises(UnknownPolicyError):
            resolve_hooks(config, default_cwd="/config/workspace")

    def test_executor_hooks_yaml_refuses_string_writable(self):
        from hooks import (
            UnknownPolicyError, build_policy_callbacks_from_hooks_yaml,
        )
        with pytest.raises(UnknownPolicyError):
            build_policy_callbacks_from_hooks_yaml({"pre_tool_use": [
                {"policy": "path_scope", "writable": "/config"},
            ]})

    def test_executor_hooks_yaml_refuses_string_forbid_write_paths(self):
        from hooks import (
            UnknownPolicyError, build_policy_callbacks_from_hooks_yaml,
        )
        with pytest.raises(UnknownPolicyError):
            build_policy_callbacks_from_hooks_yaml({"pre_tool_use": [
                {"policy": "casa_config_guard", "forbid_write_paths": "/data"},
            ]})


@pytest.mark.asyncio
class TestABrokenHooksDocumentNeverFallsBackToDefaults:
    """Sol/Terra r1 P1: refusing to BUILD is only half of failing closed.

    The HTTP hook path's per-executor map is built separately, at boot, from a
    second read of the same operator-editable file. That builder caught every
    exception, omitted the executor, and the resolver then answered from the
    DEFAULT-configured policies — which for ``casa_config_guard`` means no
    forbidden write paths at all. So the type check alone turned a fail-open
    guard into a *differently* fail-open one: refuse the declared policy,
    inherit a weaker default. The builder now installs a deny-all map for that
    executor instead.
    """

    def _registry_with_hooks(self, tmp_path, body: str):
        import yaml
        from types import SimpleNamespace
        from executor_registry import ExecutorRegistry
        hooks_path = tmp_path / "hooks.yaml"
        hooks_path.write_text(body)
        reg = ExecutorRegistry(str(tmp_path / "executors"))
        reg._disabled.add("plugin-developer")
        reg._disabled_defs["plugin-developer"] = SimpleNamespace(
            driver="claude_code", hooks_path=str(hooks_path),
            # Task 5 (#360): the builder now reads the load-time snapshot,
            # not the file — populate it exactly as agent_loader's reader
            # would have, for these well-formed (no ${VAR}) fixture bodies.
            hooks_document=yaml.safe_load(body) or {},
        )
        return reg

    async def test_mistyped_param_denies_instead_of_inheriting_defaults(
        self, tmp_path,
    ):
        from casa_core import _build_executor_cc_hook_policies
        from hooks import HOOK_POLICIES
        reg = self._registry_with_hooks(tmp_path, (
            "schema_version: 1\n"
            "pre_tool_use:\n"
            "  - policy: casa_config_guard\n"
            "    forbid_write_paths: /data\n"
        ))
        built = _build_executor_cc_hook_policies(reg)
        assert "plugin-developer" in built, (
            "a broken hooks.yaml omitted the executor, so the resolver falls "
            "back to the default policy map — casa_config_guard's default "
            "forbids NO write path at all"
        )
        entry = built["plugin-developer"]
        for policy_name in HOOK_POLICIES:
            assert policy_name in entry, policy_name
        _matcher, cb = entry["casa_config_guard"]
        result = await cb(
            {"tool_name": "Write", "tool_input": {"file_path": "/data/x"}},
            None, {},
        )
        assert result and _decision(result) == "deny"

    async def test_deny_all_covers_a_policy_the_document_never_named(
        self, tmp_path,
    ):
        """The resolver falls back per POLICY, not per executor."""
        from casa_core import _build_executor_cc_hook_policies
        reg = self._registry_with_hooks(tmp_path, (
            "schema_version: 1\n"
            "pre_tool_use:\n"
            "  - policy: path_scope\n"
            "    writable: /config\n"
        ))
        built = _build_executor_cc_hook_policies(reg)
        _matcher, cb = built["plugin-developer"]["commit_size_guard"]
        result = await cb(
            {"tool_name": "Write", "tool_input": {"file_path": "/tmp/x"}},
            None, {},
        )
        assert result and _decision(result) == "deny"

    async def test_a_valid_document_is_unaffected(self, tmp_path):
        from casa_core import _build_executor_cc_hook_policies
        reg = self._registry_with_hooks(tmp_path, (
            "schema_version: 1\n"
            "pre_tool_use:\n"
            "  - policy: path_scope\n"
            "    writable: [/data/engagements/]\n"
            "    readable: [/data/engagements/]\n"
        ))
        built = _build_executor_cc_hook_policies(reg)
        _matcher, cb = built["plugin-developer"]["path_scope"]
        assert not await cb(
            {"tool_name": "Write",
             "tool_input": {"file_path": "/data/engagements/e/f"}},
            None, {},
        )


@pytest.mark.asyncio
class TestUnrepresentedExecutorDeniesAtTheResolver:
    """r3: stop enumerating failure modes; decide at the point of USE.

    Three review rounds produced the same finding in a different arm each
    time — the file failed to build; the file failed to LOAD, so the type was
    never published; the whole executor directory failed to scan, so no type
    name survived to be marked failed at all. The ways a type can go missing
    from the per-executor map are not enumerable at the builder, and every
    miss fell back **per policy** to defaults whose ``casa_config_guard``
    forbids no write path at all.

    So the resolver no longer reads absence as "use the defaults". An
    authenticated request naming an executor the map does not represent is
    refused. Executors that legitimately carry no hooks document are
    represented POSITIVELY by a marker the builder records, so "known to need
    no parameters" is a statement rather than a silence.
    """

    async def test_an_unrepresented_executor_is_refused(self):
        from casa_core import _build_executor_cc_hook_policies
        from executor_registry import ExecutorRegistry
        # The collection-level failure mode: nothing scanned, so no type name
        # survives to be marked failed anywhere.
        built = _build_executor_cc_hook_policies(
            ExecutorRegistry("/nonexistent/executors"))
        assert "ptype" not in built
        decision, reason = await _resolve_guarded_write(built)
        assert decision == "deny", (
            "an authenticated engagement of an executor the map does not "
            "represent resolved against the permissive defaults"
        )
        assert "hook" in reason.lower()

    async def test_an_executor_with_no_hooks_document_still_uses_defaults(
        self, tmp_path,
    ):
        """The carve-out: no PARAMETERS is not the same as no such executor."""
        from types import SimpleNamespace
        from casa_core import _build_executor_cc_hook_policies
        from executor_registry import ExecutorRegistry
        reg = ExecutorRegistry(str(tmp_path / "executors"))
        reg._disabled.add("ptype")
        reg._disabled_defs["ptype"] = SimpleNamespace(
            driver="claude_code", hooks_path=None,
        )
        built = _build_executor_cc_hook_policies(reg)
        assert "ptype" in built
        decision, _reason = await _resolve_guarded_write(built)
        assert decision != "deny"

    async def test_a_real_load_failure_reaches_the_resolver_as_a_deny(
        self, tmp_path,
    ):
        """r3/r4: pin loader -> registry -> builder -> resolver, as a PAIR.

        A single broken fixture proves nothing: an incomplete executor
        directory fails to load for its own reasons, and the resolver would
        deny it under the general unrepresented-executor rule while the
        loader's type check was quietly broken. So this copies a REAL shipped
        executor and changes exactly one thing — the same `writable:` line,
        valid in the control and mistyped in the subject. The control must
        load; the subject must not.
        """
        import shutil
        from casa_core import _build_executor_cc_hook_policies
        from executor_registry import ExecutorRegistry

        HOOKS = (
            "schema_version: 1\n"
            "pre_tool_use:\n"
            "  - policy: path_scope\n"
            "    writable: {value}\n"
            "    readable: [/config]\n"
        )
        src = (Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt"
               / "casa" / "defaults" / "agents")

        def _registry(case: str, writable: str):
            agents = tmp_path / case / "agents"
            shutil.copytree(src, agents)
            # copytree preserves source modes, so a read-only checkout would
            # leave this private copy unwritable and break the edits below.
            for entry in [agents, *agents.rglob("*")]:
                entry.chmod(entry.stat().st_mode | 0o200)
            for child in (agents / "executors").iterdir():
                if child.name != "configurator":
                    shutil.rmtree(child)
            (agents / "executors" / "configurator" / "hooks.yaml").write_text(
                HOOKS.format(value=writable))
            reg = ExecutorRegistry(str(agents / "executors"))
            reg.load()
            return reg

        control = _registry("control", "[/config]")
        assert "configurator" not in control.failed_types
        assert control.definition_any("configurator") is not None

        subject = _registry("subject", "/config")
        assert "configurator" in subject.failed_types, (
            "the mistyped parameter did not fail the executor's LOAD — the "
            "loader's constructibility check is not doing the work claimed"
        )
        assert subject.definition_any("configurator") is None

        built = _build_executor_cc_hook_policies(subject)
        assert "configurator" not in built
        decision, _reason = await _resolve_guarded_write(
            built, role="configurator")
        assert decision == "deny"

    async def test_the_relay_is_not_refused_for_an_unrepresented_executor(
        self,
    ):
        """r4/r5 (both): refusing to ENFORCE must not refuse the ability to ASK.

        engagement_permission_relay has no factory — casa_core wires it
        separately with live dependencies, so it is absent from every
        per-executor map by construction. Denying it alongside the guards
        would leave a broken configuration unable to ask the operator as well
        as unable to act.

        The relay reached here is the REAL one, wired by the real helper.
        Two negatives are asserted, and both are load-bearing: the answer is
        not the *unrepresented-executor* refusal (the resolver stopped short
        of pre-empting the relay), and it is not ``unknown policy`` (the
        relay is genuinely installed under the name the resolver looks up, so
        a wiring that stopped installing it would fail here rather than pass
        by returning a different refusal).

        What the relay then DOES is deliberately not asserted: it is its own
        rule (INV-MCP-006), tested with it, and it fails closed on this
        inactive stand-in record — which is correct behaviour, not the
        permission-request path. This test says only that the resolver hands
        the decision to the relay instead of taking it away.
        """
        from casa_core import _build_executor_cc_hook_policies
        from executor_registry import ExecutorRegistry
        built = _build_executor_cc_hook_policies(
            ExecutorRegistry("/nonexistent/executors"))
        _decision, reason = await _resolve_guarded_write(
            built, policy="engagement_permission_relay", live_relay=True)
        assert _UNREPRESENTED not in reason, (
            "the resolver refused engagement_permission_relay for an "
            "executor it does not represent — a broken configuration can no "
            "longer even ask"
        )
        assert "unknown policy" not in reason.lower(), (
            "the relay never reached the resolver's lookup at all — the real "
            "wiring did not install it, so the assertion above passed "
            "vacuously"
        )


_UNREPRESENTED = "hook enforcement is unavailable"


async def _resolve_guarded_write(
    executor_hook_policies, role="ptype", policy="casa_config_guard",
    live_relay=False,
):
    """POST a guarded Write to the REAL resolve handler as ``role``."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from internal_handlers import _make_internal_hooks_resolve_handler
    from hooks import HOOK_POLICIES

    eng_id = "c" * 32

    class _Rec:
        role_or_type = role
        auth_token = "tok"

    class _Registry:
        def get(self, _eng_id):
            return _Rec() if _eng_id == eng_id else None

    defaults = {
        name: (entry["matcher"], entry["factory"]())
        for name, entry in HOOK_POLICIES.items()
    }

    async def _allow(_input, _tid, _ctx):
        return None

    # casa_core wires these two into the DEFAULT map with live deps; they have
    # no factory, so they are absent from every per-executor map.
    if live_relay:
        # r5 (both): use the REAL wiring, so a break in it is visible here.
        from casa_core import _wire_engagement_permission_relay
        _wire_engagement_permission_relay(
            defaults,
            engagement_registry=_Registry(),
            telegram_channel=object(),
        )
    else:
        defaults["engagement_permission_relay"] = (r".*", _allow)
    defaults["engagement_buttons_reminder"] = (r"Skill", _allow)

    handler = _make_internal_hooks_resolve_handler(
        hook_policies=defaults,
        executor_hook_policies=executor_hook_policies,
        engagement_registry=_Registry(),
    )
    app = web.Application()
    app.router.add_post("/hooks/resolve", handler)
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post("/hooks/resolve", json={
            "policy": policy,
            "payload": {"tool_name": "Write",
                        "tool_input": {"file_path": "/data/x"},
                        "cwd": f"/data/engagements/{eng_id}"},
            "engagement_id": eng_id, "engagement_token": "tok",
        })
        body = await resp.json()
    out = body.get("hookSpecificOutput") or {}
    return out.get("permissionDecision"), out.get("permissionDecisionReason") or ""


class TestTheDenyAllMapIsMarked:
    def test_a_deny_all_map_is_marked_so_reload_can_recognise_it(
        self, tmp_path,
    ):
        from types import SimpleNamespace
        from casa_core import _build_executor_cc_hook_policies
        from executor_registry import ExecutorRegistry
        from hooks import DenyAllPolicyMap
        hooks_path = tmp_path / "hooks.yaml"
        hooks_path.write_text(
            "schema_version: 1\n"
            "pre_tool_use:\n"
            "  - policy: path_scope\n"
            "    writable: /config\n"
        )
        reg = ExecutorRegistry(str(tmp_path / "executors"))
        reg._disabled.add("plugin-developer")
        reg._disabled_defs["plugin-developer"] = SimpleNamespace(
            driver="claude_code", hooks_path=str(hooks_path),
        )
        built = _build_executor_cc_hook_policies(reg)
        assert isinstance(built["plugin-developer"], DenyAllPolicyMap)


@pytest.mark.asyncio
class TestReloadPrefersAKnownGoodSetOverDenyAll:
    """Sol r2 P1: deny-all must not evict a pre-reload set that still works.

    Deny-all is the right answer when there is nothing better. A pre-reload
    policy set built from the operator's last valid file IS something better,
    and evicting it takes live engagements down for a file edit that has not
    even been accepted.
    """

    async def test_stale_callbacks_survive_a_deny_all_rebuild(
        self, tmp_path, monkeypatch,
    ):
        from types import SimpleNamespace
        import tools as tools_mod
        from executor_registry import ExecutorRegistry
        from reload import reload_executors
        from test_reload import _make_runtime

        monkeypatch.setattr(
            tools_mod, "_regenerate_plugin_health", lambda extra: None)

        async def _noop():
            return None

        monkeypatch.setattr(
            tools_mod, "_notify_plugin_health_if_possible", _noop)

        # Terra r4: the earlier form only populated failed_types, so the
        # builder produced NO fresh entry and this exercised the pre-existing
        # missing-entry arm, not the new DenyAllPolicyMap one. Give the type a
        # published definition pointing at a mistyped file, so the builder
        # really does return a deny-all map for it.
        hooks_path = tmp_path / "hooks.yaml"
        hooks_path.write_text(
            "schema_version: 1\n"
            "pre_tool_use:\n"
            "  - policy: path_scope\n"
            "    writable: /config\n"
        )
        reg = ExecutorRegistry(str(tmp_path / "executors"))

        def _fake_load():
            reg._disabled.add("broken-exec")
            reg._disabled_defs["broken-exec"] = SimpleNamespace(
                driver="claude_code", hooks_path=str(hooks_path),
            )

        monkeypatch.setattr(reg, "load", _fake_load)
        from casa_core import _build_executor_cc_hook_policies
        from hooks import DenyAllPolicyMap
        _fake_load()
        assert isinstance(
            _build_executor_cc_hook_policies(reg)["broken-exec"],
            DenyAllPolicyMap,
        ), "this test must drive the deny-all branch, not the missing one"

        runtime = _make_runtime()
        runtime.executor_registry = reg
        tight = {"commit_size_guard": ("Write|Edit", object())}
        runtime.executor_cc_policies = {"broken-exec": tight}

        actions = await reload_executors(runtime)

        assert runtime.executor_cc_policies["broken-exec"] is tight
        assert "executor_hook_policies_kept_stale:broken-exec" in actions


class TestAMistypedParamFailsTheExecutorLoad:
    """Sol r1 P2: nothing pinned the LOAD boundary the invariant claims.

    Deleting agent_loader's constructibility check left every other test in
    this file green, so this one calls the loader.
    """

    def test_executor_with_mistyped_param_fails_to_load(self, tmp_path):
        import agent_loader
        exec_dir = tmp_path / "executors" / "broken"
        exec_dir.mkdir(parents=True)
        (exec_dir / "hooks.yaml").write_text(
            "schema_version: 1\n"
            "pre_tool_use:\n"
            "  - policy: path_scope\n"
            "    writable: /config\n"
        )
        with pytest.raises(agent_loader.LoadError) as exc:
            agent_loader._resolve_executor_hooks(
                str(exec_dir), "broken", {}, {"hooks.yaml"},
            )
        assert "writable" in str(exc.value)


@pytest.mark.asyncio
class TestTheFailOpenIsGone:
    """The behavioural half: no build may yield a guard that admits anything.

    Pre-fix, ``writable="/config"`` built a hook whose prefix list contained
    ``/`` — this Write to ``/etc/shadow`` was ALLOWED.
    """

    async def test_string_writable_never_yields_an_admitting_hook(self):
        from hooks import UnknownPolicyError, make_path_scope_hook_v2
        try:
            hook = make_path_scope_hook_v2(writable="/config", readable=[])
        except UnknownPolicyError:
            return  # refused at build — fail-closed, nothing to enforce
        result = await hook(
            {"tool_name": "Write", "tool_input": {"file_path": "/etc/shadow"}},
            "tid", CTX,
        )
        assert result and _decision(result) == "deny", (
            "path_scope built from a string `writable` admitted a write "
            "outside every declared prefix — the guard failed OPEN"
        )
