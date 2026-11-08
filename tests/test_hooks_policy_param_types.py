"""#442: hook policy parameters are type-checked at BUILD time.

``hooks.yaml`` is schema-validated with ``additionalProperties`` open, so a
mistyped parameter reaches the policy factory intact. Before this, a bare
string was accepted where a list of prefixes was expected and then iterated
CHARACTER by character: ``writable: /config`` became the prefix set
``['/', 'c', 'o', 'n', 'f', 'i', 'g']``, and the lone ``/`` prefix-matches
every absolute path — the guard failed OPEN, admitting exactly the writes it
existed to refuse.

The rule these tests pin: a mistyped parameter raises ``UnknownPolicyError``
out of the factory, so a session build fails closed, the same way an unknown
parameter name already did. (The executor load boundary this used to pin went
in v0.351.0: the configurator's hooks ship with Casa, and
``test_hooks_managed_component_guard.py`` resolves the shipped ones.)
"""

from __future__ import annotations


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
