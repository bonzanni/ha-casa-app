"""Tests for HOOK_POLICIES registry + resolve_hooks."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


CTX: dict = {"signal": None}


def _decision(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecision"]


class TestPathScopeV2:
    async def test_writable_allows_write_under_prefix(self):
        from hooks import make_path_scope_hook_v2
        hook = make_path_scope_hook_v2(
            writable=["/addon_configs/casa/workspace"],
            readable=["/addon_configs/casa/workspace"],
        )
        data = {"tool_name": "Write",
                "tool_input": {"file_path":
                    "/addon_configs/casa/workspace/note.txt"}}
        assert await hook(data, "tid", CTX) == {}

    async def test_writable_denies_write_outside_prefix(self):
        from hooks import make_path_scope_hook_v2
        hook = make_path_scope_hook_v2(
            writable=["/addon_configs/casa/workspace"],
            readable=["/addon_configs/casa/workspace"],
        )
        data = {"tool_name": "Write",
                "tool_input": {"file_path": "/etc/shadow"}}
        result = await hook(data, "tid", CTX)
        assert result is not None and _decision(result) == "deny"

    async def test_readable_allows_read_under_prefix(self):
        from hooks import make_path_scope_hook_v2
        hook = make_path_scope_hook_v2(
            writable=[],
            readable=["/addon_configs"],
        )
        data = {"tool_name": "Read",
                "tool_input": {"file_path": "/addon_configs/something"}}
        assert await hook(data, "tid", CTX) == {}

    async def test_readable_denies_read_outside_prefix(self):
        from hooks import make_path_scope_hook_v2
        hook = make_path_scope_hook_v2(writable=[], readable=["/data"])
        data = {"tool_name": "Read",
                "tool_input": {"file_path": "/etc/passwd"}}
        result = await hook(data, "tid", CTX)
        assert result is not None and _decision(result) == "deny"

    async def test_traversal_normalized(self):
        from hooks import make_path_scope_hook_v2
        hook = make_path_scope_hook_v2(
            writable=[], readable=["/addon_configs"],
        )
        data = {"tool_name": "Read",
                "tool_input": {"file_path":
                    "/addon_configs/../etc/passwd"}}
        result = await hook(data, "tid", CTX)
        assert result is not None and _decision(result) == "deny"

    async def test_path_scope_deny_payload_uses_single_slash(self):
        """L-2 (v0.34.2): deny string shows '/addon_configs/...' not '//addon_configs/...'."""
        from hooks import make_path_scope_hook_v2
        hook = make_path_scope_hook_v2(
            writable=["/addon_configs/foo"],
            readable=["/addon_configs/foo"],
        )
        result = await hook(
            {"tool_name": "Write",
             "tool_input": {"file_path": "/etc/something"}},
            tool_use_id=None, context={},
        )
        # Hook returned a deny dict; string contents must show single-slash prefixes.
        assert result is not None
        payload = str(result)
        assert "/addon_configs/foo" in payload
        assert "//addon_configs/foo" not in payload


class TestResolveHooks:
    def test_empty_hooks_config_resolves_empty_dict(self):
        from hooks import resolve_hooks
        from config import HooksConfig

        resolved = resolve_hooks(HooksConfig(), default_cwd="/cwd")
        # Default is block_dangerous_bash + path_scope scoped to cwd.
        assert "PreToolUse" in resolved
        assert len(resolved["PreToolUse"]) == 2

    def test_explicit_block_dangerous_bash(self):
        from hooks import resolve_hooks
        from config import HooksConfig

        cfg = HooksConfig(pre_tool_use=[{"policy": "block_dangerous_bash"}])
        resolved = resolve_hooks(cfg, default_cwd="/cwd")
        assert "PreToolUse" in resolved
        # Just one matcher when only block_dangerous_bash is listed.
        assert len(resolved["PreToolUse"]) == 1

    def test_explicit_path_scope_with_params(self):
        from hooks import resolve_hooks
        from config import HooksConfig

        cfg = HooksConfig(pre_tool_use=[
            {"policy": "path_scope",
             "writable": ["/workspace"],
             "readable": ["/workspace", "/addon_configs"]},
        ])
        resolved = resolve_hooks(cfg, default_cwd="/cwd")
        assert len(resolved["PreToolUse"]) == 1

    def test_unknown_policy_raises(self):
        from hooks import resolve_hooks, UnknownPolicyError
        from config import HooksConfig

        cfg = HooksConfig(pre_tool_use=[{"policy": "not_a_real_policy"}])
        with pytest.raises(UnknownPolicyError, match="not_a_real_policy"):
            resolve_hooks(cfg, default_cwd="/cwd")

    def test_path_scope_bad_params_raises(self):
        """Unknown parameters on a policy surface as load error."""
        from hooks import resolve_hooks, UnknownPolicyError
        from config import HooksConfig

        cfg = HooksConfig(pre_tool_use=[
            {"policy": "path_scope", "bogus_param": []},
        ])
        with pytest.raises(UnknownPolicyError, match="bogus_param"):
            resolve_hooks(cfg, default_cwd="/cwd")

    def test_stray_matcher_and_timeout_stripped_before_factory_call(self):
        """REVISION 3 (Terra plan-review r3, #360): resolve_hooks is the SDK
        path used by the in-casa _build_executor_options. A snapshot
        carrying the transport-only ``matcher``/``timeout`` keys on a floor
        policy (e.g. because hook_bridge's canonical-matcher force wrote a
        matcher back onto every registry entry, or the yaml itself declared
        one) loads/reloads fine but previously blew up every in-casa
        executor's START with UnknownPolicyError, since the factories reject
        unexpected kwargs. Both keys must be stripped before the factory
        call, exactly like build_policy_callbacks_from_hooks_yaml already
        does."""
        from hooks import resolve_hooks
        from config import HooksConfig

        cfg = HooksConfig(pre_tool_use=[
            {"policy": "block_dangerous_bash", "matcher": "Read"},
            {"policy": "path_scope", "matcher": "Read|Write|Edit",
             "timeout": 600,
             "writable": ["/workspace"], "readable": ["/workspace"]},
        ])
        resolved = resolve_hooks(cfg, default_cwd="/cwd")
        assert len(resolved["PreToolUse"]) == 2


# --- 0.13.1 — two-tier HOOK_POLICIES shape --------------------------------


def test_hook_policies_are_two_tier_dicts():
    """Each policy entry is {'matcher': regex, 'factory': callable}."""
    from hooks import HOOK_POLICIES

    for name, entry in HOOK_POLICIES.items():
        assert isinstance(entry, dict), f"{name}: must be dict"
        assert set(entry.keys()) == {"matcher", "factory"}, (
            f"{name}: keys must be exactly {{matcher, factory}}"
        )
        assert isinstance(entry["matcher"], str)
        assert callable(entry["factory"])


def test_factory_returns_hookcallback():
    """factory(**kwargs) must return an awaitable-callable, not a HookMatcher."""
    from hooks import HOOK_POLICIES

    entry = HOOK_POLICIES["casa_config_guard"]
    cb = entry["factory"](forbid_write_paths=["/data"])

    # HookCallback is (input, tool_use_id, context) -> Awaitable[dict | None]
    # We don't import HookCallback directly (it's a type alias); we just
    # assert callability + coroutinefunction-ness.
    import inspect
    assert inspect.iscoroutinefunction(cb), (
        f"factory returned {type(cb)!r}; must be async function"
    )


def test_resolve_hooks_still_builds_HookMatcher():
    """The SDK-path consumer (resolve_hooks) still produces HookMatcher objects."""
    from config import HooksConfig
    from hooks import resolve_hooks

    cfg = HooksConfig(pre_tool_use=[{"policy": "block_dangerous_bash"}])
    resolved = resolve_hooks(cfg, default_cwd="/tmp")
    assert "PreToolUse" in resolved
    assert len(resolved["PreToolUse"]) == 1
    # HookMatcher has .matcher and .hooks — duck-type check is fine.
    matcher = resolved["PreToolUse"][0]
    assert hasattr(matcher, "matcher") and hasattr(matcher, "hooks")
    assert matcher.matcher == "Bash"


# --- H-2 (v0.36.1) — no-op paths return empty dict, never None ---------------
#
# The SDK's `_convert_hook_output_for_cli` (claude_agent_sdk/_internal/query.py)
# calls `hook_output.items()` unconditionally. The typed contract is
# `HookCallback = Callable[..., Awaitable[HookJSONOutput]]` where
# `HookJSONOutput = AsyncHookJSONOutput | SyncHookJSONOutput` — both TypedDicts
# with all fields `NotRequired`. Returning `None` from a no-op branch violates
# the contract and produces 73+ `Error in hook callback hook_X: 'NoneType'
# object has no attribute 'items'` per ~30-min engagement window.
#
# Fix: every HookCallback no-op path returns `{}` instead of `None`. Operationally
# equivalent (the SDK treats both as "no decision") but type-compliant.
# -----------------------------------------------------------------------------


class TestHookNoopReturnsEmptyDict:
    """H-2 regression: every HookCallback returns {} (not None) on no-op paths."""

    async def test_block_dangerous_commands_safe_command(self):
        from hooks import block_dangerous_commands
        result = await block_dangerous_commands(
            {"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}},
            None, {},
        )
        assert result == {}, f"expected empty dict, got {result!r}"

    async def test_block_dangerous_commands_non_bash(self):
        from hooks import block_dangerous_commands
        result = await block_dangerous_commands(
            {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}},
            None, {},
        )
        assert result == {}, f"expected empty dict, got {result!r}"

    async def test_path_scope_allowed_write(self):
        from hooks import make_path_scope_hook_v2
        hook = make_path_scope_hook_v2(
            writable=["/addon_configs/casa/workspace"],
            readable=["/addon_configs/casa/workspace"],
        )
        result = await hook(
            {"tool_name": "Write",
             "tool_input": {"file_path":
                 "/addon_configs/casa/workspace/note.txt"}},
            None, {},
        )
        assert result == {}, f"expected empty dict, got {result!r}"

    async def test_path_scope_non_file_tool(self):
        from hooks import make_path_scope_hook_v2
        hook = make_path_scope_hook_v2(writable=[], readable=[])
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            None, {},
        )
        assert result == {}, f"expected empty dict, got {result!r}"

    async def test_casa_config_guard_allowed_write(self):
        from hooks import make_casa_config_guard_hook
        hook = make_casa_config_guard_hook(
            forbid_write_paths=["/data"],
            forbid_delete_residents=True,
        )
        result = await hook(
            {"tool_name": "Write",
             "tool_input": {"file_path":
                 "/addon_configs/casa/agents/specialists/x/character.yaml"}},
            None, {},
        )
        assert result == {}, f"expected empty dict, got {result!r}"

    async def test_casa_config_guard_safe_bash(self):
        from hooks import make_casa_config_guard_hook
        hook = make_casa_config_guard_hook(
            forbid_write_paths=[],
            forbid_delete_residents=True,
        )
        result = await hook(
            {"tool_name": "Bash",
             "tool_input": {"command": "ls /addon_configs"}},
            None, {},
        )
        assert result == {}, f"expected empty dict, got {result!r}"

    async def test_commit_size_guard_under_threshold(self):
        from unittest.mock import patch
        from hooks import make_commit_size_guard_hook
        hook = make_commit_size_guard_hook(max_files=20)
        with patch("hooks._git_porcelain_count", return_value=5):
            result = await hook(
                {"tool_name": "Write",
                 "tool_input": {"file_path":
                     "/addon_configs/casa/agents/x.yaml"}},
                None, {},
            )
        assert result == {}, f"expected empty dict, got {result!r}"

    async def test_commit_size_guard_non_write(self):
        from hooks import make_commit_size_guard_hook
        hook = make_commit_size_guard_hook(max_files=1)
        result = await hook(
            {"tool_name": "Read", "tool_input": {"file_path": "/x"}},
            None, {},
        )
        assert result == {}, f"expected empty dict, got {result!r}"

    async def test_self_containment_guard_non_bash(self):
        from hooks import make_self_containment_guard
        hook = make_self_containment_guard()
        result = await hook(
            {"tool_name": "Read", "tool_input": {"file_path": "/x"}},
            None, {},
        )
        assert result == {}, f"expected empty dict, got {result!r}"

    async def test_self_containment_guard_non_git_push(self):
        from hooks import make_self_containment_guard
        hook = make_self_containment_guard()
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "ls -la"}},
            None, {},
        )
        assert result == {}, f"expected empty dict, got {result!r}"
