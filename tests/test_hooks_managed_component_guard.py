"""Tests for managed_component_guard hook policy (#210, v0.101.0).

On a fresh install the configurator hand-authored specialist files under
/config/agents/specialists/ via Bash instead of the typed
specialist_install_* pipeline; its path_scope makes /config/agents writable
and only /config/plugins/ Bash-writes were specially denied. The policy
denies hand-edits (Write/Edit by normalized path, Bash by write-shaped
commands) of the managed component trees and routes the model to the typed
tool for the matched tree; reads stay allowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

# Since v0.351.0 the configurator's hooks ship in its role artifact
# (``hooks.pre_tool_use``), not in a config-editable hooks.yaml.
_ROLES_EXECUTOR_DIR = (
    Path(__file__).resolve().parents[1]
    / "casa/rootfs/opt/casa/defaults/roles/executor"
)


def _decision(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecision"]


def _reason(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _hook():
    from hooks import make_managed_component_guard
    return make_managed_component_guard()


# ------------------------------------------------------------------
# Write/Edit deny under each managed prefix (incl. traversal and //)
# ------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
@pytest.mark.parametrize("path,route_marker", [
    ("/config/agents/specialists/finance/runtime.yaml",
     "specialist_install_inspect"),
    ("/config/specialists/finance/0.1.0/spec.yaml",
     "specialist_install_inspect"),
    ("/config/bindings/ellen.yaml", "resident_persona_swap"),
    ("/config/plugins/registry.json", "plugin_add"),
    # `..` traversal and `//` collapse must resolve to the managed prefix
    # (same normalization contract as path_scope / casa_config_guard).
    ("/config/agents/other/../specialists/finance/runtime.yaml",
     "specialist_install_inspect"),
    ("//config//plugins/registry.json", "plugin_add"),
    ("/config/policies/../bindings/ellen.yaml", "resident_persona_swap"),
])
async def test_write_edit_denied_under_managed_prefix(
        tool_name, path, route_marker):
    out = await _hook()(
        {"tool_name": tool_name, "tool_input": {"file_path": path}},
        None, {},
    )
    assert _decision(out) == "deny", path
    # The deny reason must ROUTE the model to the typed pipeline.
    assert route_marker in _reason(out), path
    assert "hand-editing is forbidden" in _reason(out)


@pytest.mark.parametrize("path", [
    # NOT managed: ordinary resident config the configurator legitimately edits.
    # NOTE (#403): deliberately NOT triggers.yaml — that file joined this
    # classifier under its own kind, and the guard now denies it with the
    # trigger-file reason (see test_resident_trigger_file_is_denied_with_its_own_reason).
    "/config/agents/assistant/delegates.yaml",
    "/config/policies/x.yaml",
    "/config/workspace/scratch.md",
    # hooks.yaml OUTSIDE /config/agents/ is not a policy-file self-edit.
    "/config/hooks.yaml",
])
async def test_write_allowed_outside_managed_prefixes(path):
    out = await _hook()(
        {"tool_name": "Write", "tool_input": {"file_path": path}},
        None, {},
    )
    assert out == {}, path


# ------------------------------------------------------------------
# hooks.yaml policy-file self-editing (any agent under /config/agents/)
# ------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/config/agents/ellen/hooks.yaml",
    "/config/agents/executors/configurator/hooks.yaml",
    "/config/agents/executors/plugin-developer/hooks.yaml",
    "/config/agents/ellen/../ellen/hooks.yaml",  # traversal spelling
])
async def test_hooks_yaml_self_edit_denied(path):
    out = await _hook()(
        {"tool_name": "Edit", "tool_input": {"file_path": path}},
        None, {},
    )
    assert _decision(out) == "deny", path
    assert "hook-policy file" in _reason(out)


# ------------------------------------------------------------------
# Bash write-forms into managed prefixes deny
# ------------------------------------------------------------------


@pytest.mark.parametrize("cmd,route_marker", [
    ("echo 'name: x' > /config/agents/specialists/x/runtime.yaml",
     "specialist_install_inspect"),
    ("cat /tmp/f | tee /config/specialists/x/spec.yaml", "specialist_install_inspect"),
    ("cp /tmp/x /config/bindings/ellen.yaml", "resident_persona_swap"),
    ("mv /tmp/x /config/plugins/registry.json", "plugin_add"),
    ("mkdir -p /config/agents/specialists/newbot", "specialist_install_inspect"),
    ("rm -rf /config/specialists/finance", "specialist_install_inspect"),
    ("sed -i 's/a/b/' /config/bindings/ellen.yaml", "resident_persona_swap"),
    ("ln -s /tmp/tgt /config/plugins/store/x", "plugin_add"),
    # Heredoc into a managed path.
    ("cat > /config/agents/specialists/x/agent.yaml <<'EOF'\nname: x\nEOF",
     "specialist_install_inspect"),
    # bash -c wrapper: the raw string still carries the path token, so no
    # argv unwrapping is needed to catch it.
    ("bash -c 'echo x > /config/plugins/registry.json'", "plugin_add"),
    # Traversal / double-slash spellings resolve to the managed prefix.
    ("echo x > /config/agents/specialists/../../plugins/registry.json",
     "plugin_add"),
    ("echo x > //config//plugins/registry.json", "plugin_add"),
    ("touch /config/bindings/gary.yaml", "resident_persona_swap"),
    ("rsync -a /tmp/stage/ /config/specialists/x/", "specialist_install_inspect"),
    # #348 fail-closed pins: a MANAGED -t target is still a write, and
    # rsync's -t is preserve-times (a flag, not a target) — the managed
    # destination operand must still deny.
    ("cp -t /config/plugins/store/x /tmp/evil.json", "plugin_add"),
    ("cp --target-directory=/config/bindings /tmp/evil.yaml",
     "resident_persona_swap"),
    ("rsync -t /tmp/src.yaml /config/specialists/x/dst.yaml",
     "specialist_install_inspect"),
    ("chmod 755 /config/agents/specialists/x/run.sh", "specialist_install_inspect"),
    ("git checkout -- /config/bindings/ellen.yaml", "resident_persona_swap"),
    ("tar -xf /tmp/a.tar -C /config/specialists/", "specialist_install_inspect"),
    ("find /config/plugins/store -name '*.pyc' -delete", "plugin_add"),
])
async def test_bash_write_forms_denied(cmd, route_marker):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert _decision(out) == "deny", cmd
    assert route_marker in _reason(out), cmd


# ------------------------------------------------------------------
# Bash read-forms pass (the configurator legitimately READS these trees)
# ------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    "cat /config/agents/specialists/finance/runtime.yaml",
    "ls -la /config/specialists",
    "grep -r persona /config/bindings",
    "git -C /config log --oneline -5",
    "git -C /config status",
    "git -C /config diff",
    "find /config/plugins/store -name plugin.json",
    # Benign stderr redirects must not read as writes.
    "cat /config/plugins/store/x/plugin.json 2>/dev/null",
    "ls /config/bindings 2>&1",
    # No managed prefix mentioned at all.
    "echo hello > /tmp/scratch.txt",
    # #348: an explicit UNMANAGED -t/--target-directory leaves only source
    # operands — copying an inspectable artifact OUT is the documented
    # read allowance and must not be marked ambiguous.
    "cp -t /tmp /config/plugins/store/x/plugin.json",
    "cp --target-directory=/tmp /config/plugins/store/x/plugin.json",
    "install -t /tmp /config/plugins/store/x/tool.bin",
])
async def test_bash_read_forms_pass(cmd):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert out == {}, cmd


# ------------------------------------------------------------------
# Fail-closed: an internal exception returns the deny shape, never escapes
# ------------------------------------------------------------------


class TestFailClosed:
    async def test_write_internal_error_denies(self, monkeypatch):
        import hooks as hooks_mod

        def _boom(norm):
            raise RuntimeError("synthetic internal failure")

        monkeypatch.setattr(hooks_mod, "_managed_prefix_route", _boom)
        out = await _hook()(
            {"tool_name": "Write",
             "tool_input": {"file_path":
                 "/config/agents/specialists/x/runtime.yaml"}},
            None, {},
        )
        assert _decision(out) == "deny"
        assert "failing closed" in _reason(out)

    async def test_bash_internal_error_denies(self, monkeypatch):
        import hooks as hooks_mod

        def _boom(command):
            raise RuntimeError("synthetic internal failure")

        monkeypatch.setattr(hooks_mod, "_bash_managed_prefix_route", _boom)
        out = await _hook()(
            {"tool_name": "Bash",
             "tool_input": {"command": "echo x > /config/plugins/r.json"}},
            None, {},
        )
        assert _decision(out) == "deny"
        assert "failing closed" in _reason(out)


# ------------------------------------------------------------------
# No-op paths return {} exactly (H-2 contract — mirror
# TestHookNoopReturnsEmptyDict in test_hooks_policies.py)
# ------------------------------------------------------------------


class TestNoopReturnsEmptyDict:
    async def test_non_matching_tool(self):
        out = await _hook()(
            {"tool_name": "Read",
             "tool_input": {"file_path": "/config/plugins/registry.json"}},
            None, {},
        )
        assert out == {}, f"expected empty dict, got {out!r}"

    async def test_allowed_write(self):
        out = await _hook()(
            {"tool_name": "Write",
             "tool_input": {"file_path": "/config/agents/assistant/delegates.yaml"}},
            None, {},
        )
        assert out == {}, f"expected empty dict, got {out!r}"

    async def test_safe_bash(self):
        out = await _hook()(
            {"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}},
            None, {},
        )
        assert out == {}, f"expected empty dict, got {out!r}"


# ------------------------------------------------------------------
# Registry shape + factory conventions
# ------------------------------------------------------------------


async def test_registered_in_hook_policies():
    from hooks import HOOK_POLICIES
    assert "managed_component_guard" in HOOK_POLICIES
    assert HOOK_POLICIES["managed_component_guard"]["matcher"] == "Write|Edit|Bash"


async def test_factory_returns_hookcallback():
    import inspect
    from hooks import HOOK_POLICIES
    cb = HOOK_POLICIES["managed_component_guard"]["factory"]()
    assert inspect.iscoroutinefunction(cb)


async def test_factory_rejects_params():
    from hooks import HOOK_POLICIES, UnknownPolicyError
    with pytest.raises(UnknownPolicyError, match="bogus"):
        HOOK_POLICIES["managed_component_guard"]["factory"](bogus=True)


# ------------------------------------------------------------------
# Shipped hooks wiring — the policy must be declared in, and built from, the
# hooks the shipped configurator role artifact carries
# ------------------------------------------------------------------


def _shipped_hooks_yaml(executor: str) -> dict:
    role = yaml.safe_load(
        (_ROLES_EXECUTOR_DIR / executor / "role.yaml").read_text(encoding="utf-8"))
    return {"pre_tool_use": role["hooks"]["pre_tool_use"]}


async def test_shipped_yaml_declares_policy_for_both_executors():
    """The shipped configurator's hooks carry the policy, and
    build_policy_callbacks_from_hooks_yaml (the agent loader's load-time
    constructibility check) materializes it under its canonical matcher."""
    from hooks import build_policy_callbacks_from_hooks_yaml
    for executor in ("configurator",):
        data = _shipped_hooks_yaml(executor)
        declared = [e.get("policy") for e in data["pre_tool_use"]]
        assert "managed_component_guard" in declared, executor
        built = build_policy_callbacks_from_hooks_yaml(data)
        assert "managed_component_guard" in built, executor
        matcher, _cb = built["managed_component_guard"]
        assert matcher == "Write|Edit|Bash"


async def test_shipped_configurator_yaml_resolves_on_sdk_path():
    """resolve_hooks (SDK path) accepts the shipped configurator hooks and
    the resolved stack denies a specialist hand-write."""
    from config import HooksConfig
    from hooks import resolve_hooks

    data = _shipped_hooks_yaml("configurator")
    resolved = resolve_hooks(
        HooksConfig(pre_tool_use=data["pre_tool_use"]), default_cwd="/config")
    payload = {"tool_name": "Write",
               "tool_input": {"file_path":
                   "/config/agents/specialists/x/runtime.yaml"}}
    reasons = []
    for matcher in resolved["PreToolUse"]:
        for cb in matcher.hooks:
            out = await cb(payload, None, {})
            if out and _decision(out) == "deny":
                reasons.append(_reason(out))
    assert any("specialist_install_inspect" in r for r in reasons), reasons


# ------------------------------------------------------------------
# Round-2 F2: RELATIVE paths (executor cwd is /config) resolve against
# /config before the prefix test — Write/Edit file_path and Bash tokens
# ------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
@pytest.mark.parametrize("path,route_marker", [
    ("agents/specialists/x/runtime.yaml", "specialist_install_inspect"),
    ("specialists/x/0.1.0/spec.yaml", "specialist_install_inspect"),
    ("./bindings/ellen.yaml", "resident_persona_swap"),
    ("plugins/registry.json", "plugin_add"),
])
async def test_relative_write_edit_denied(tool_name, path, route_marker):
    out = await _hook()(
        {"tool_name": tool_name, "tool_input": {"file_path": path}},
        None, {},
    )
    assert _decision(out) == "deny", path
    assert route_marker in _reason(out), path


async def test_relative_hooks_yaml_write_denied():
    out = await _hook()(
        {"tool_name": "Edit",
         "tool_input": {"file_path": "agents/executors/configurator/hooks.yaml"}},
        None, {},
    )
    assert _decision(out) == "deny"
    assert "hook-policy file" in _reason(out)


async def test_relative_write_outside_managed_allowed():
    out = await _hook()(
        {"tool_name": "Write", "tool_input": {"file_path": "workspace/notes.md"}},
        None, {},
    )
    assert out == {}


@pytest.mark.parametrize("cmd,route_marker", [
    ("echo 'name: x' > agents/specialists/x/runtime.yaml",
     "specialist_install_inspect"),
    ("mkdir -p specialists/newbot", "specialist_install_inspect"),
    ("echo x > ./bindings/gary.yaml", "resident_persona_swap"),
    # cwd=/ spelling resolves via the "/"-join candidate.
    ("echo x > config/plugins/registry.json", "plugin_add"),
    # Bare managed-root word: `rm -rf plugins` from /config kills the store.
    ("rm -rf plugins", "plugin_add"),
])
async def test_relative_bash_write_forms_denied(cmd, route_marker):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert _decision(out) == "deny", cmd
    assert route_marker in _reason(out), cmd


async def test_relative_bash_read_passes():
    out = await _hook()(
        {"tool_name": "Bash",
         "tool_input": {"command": "cat agents/specialists/finance/runtime.yaml"}},
        None, {},
    )
    assert out == {}


# ------------------------------------------------------------------
# Round-2 F1: Bash write-forms against hooks.yaml under /config/agents/
# deny with the hook-policy message (self-disarm was the P0 bypass)
# ------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    "sed -i 's/x/y/' /config/agents/executors/configurator/hooks.yaml",
    "sed -i 's/x/y/' agents/executors/configurator/hooks.yaml",
    "echo '' > /config/agents/ellen/hooks.yaml",
    "rm agents/executors/plugin-developer/hooks.yaml",
])
async def test_bash_hooks_yaml_write_denied(cmd):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert _decision(out) == "deny", cmd
    assert "hook-policy file" in _reason(out), cmd


async def test_bash_hooks_yaml_read_passes():
    out = await _hook()(
        {"tool_name": "Bash",
         "tool_input": {
             "command": "cat /config/agents/executors/configurator/hooks.yaml"}},
        None, {},
    )
    assert out == {}


# ------------------------------------------------------------------
# Round-2 F3: symlink resolution. Strongest testable form: full hook
# invocation over a REAL on-disk symlink in tmp_path, with only the
# managed-prefix table repointed at the tmp layout (monkeypatched
# _MANAGED_PREFIX_ROUTES — _managed_prefix_route reads the module global
# at call time, so the production realpath+lexical composition runs
# unmodified end to end).
# ------------------------------------------------------------------


class TestSymlinkResolution:
    def _layout(self, tmp_path, monkeypatch):
        import hooks as hooks_mod
        managed = tmp_path / "config" / "plugins"
        managed.mkdir(parents=True)
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "link").symlink_to(managed, target_is_directory=True)
        monkeypatch.setattr(
            hooks_mod, "_MANAGED_PREFIX_ROUTES",
            ((str(managed), hooks_mod._MANAGED_ROUTE_PLUGINS),))
        return ws

    async def test_write_through_symlink_denied(self, tmp_path, monkeypatch):
        ws = self._layout(tmp_path, monkeypatch)
        out = await _hook()(
            {"tool_name": "Write",
             "tool_input": {"file_path": str(ws / "link" / "evil.json")}},
            None, {},
        )
        assert _decision(out) == "deny"
        assert "plugin_add" in _reason(out)

    async def test_bash_through_symlink_denied(self, tmp_path, monkeypatch):
        ws = self._layout(tmp_path, monkeypatch)
        out = await _hook()(
            {"tool_name": "Bash",
             "tool_input": {"command": f"echo x > {ws}/link/evil.json"}},
            None, {},
        )
        assert _decision(out) == "deny"
        assert "plugin_add" in _reason(out)

    async def test_write_beside_symlink_allowed(self, tmp_path, monkeypatch):
        ws = self._layout(tmp_path, monkeypatch)
        out = await _hook()(
            {"tool_name": "Write",
             "tool_input": {"file_path": str(ws / "direct.json")}},
            None, {},
        )
        assert out == {}


# ------------------------------------------------------------------
# Round-2 F3 fail-closed: OSError during realpath resolution denies
# ------------------------------------------------------------------


class TestRealpathOSErrorFailsClosed:
    def _patch(self, monkeypatch):
        import hooks as hooks_mod
        real = hooks_mod.os.path.realpath

        def _boom(path, *args, **kwargs):
            # Conditional on a sentinel so incidental realpath users
            # elsewhere in the process are unaffected.
            if "sentinel-oserr" in str(path):
                raise OSError("synthetic realpath failure")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(hooks_mod.os.path, "realpath", _boom)

    async def test_write_denies(self, monkeypatch):
        self._patch(monkeypatch)
        out = await _hook()(
            {"tool_name": "Write",
             "tool_input": {"file_path": "/tmp/sentinel-oserr/x.yaml"}},
            None, {},
        )
        assert _decision(out) == "deny"
        assert "failing closed" in _reason(out)

    async def test_bash_denies(self, monkeypatch):
        self._patch(monkeypatch)
        out = await _hook()(
            {"tool_name": "Bash",
             "tool_input": {"command": "echo x > /tmp/sentinel-oserr/f"}},
            None, {},
        )
        assert _decision(out) == "deny"
        assert "failing closed" in _reason(out)


# ------------------------------------------------------------------
# Round-2 F4: inline-interpreter escape denies when a managed token
# appears anywhere in the command, regardless of write verbs
# ------------------------------------------------------------------


@pytest.mark.parametrize("cmd,route_marker", [
    ("python3 -c \"open('/config/plugins/registry.json','w').write('x')\"",
     "plugin_add"),
    ("python3 -c \"open('agents/specialists/x/runtime.yaml','w')\"",
     "specialist_install_inspect"),
    ("perl -e 'unlink q{/config/bindings/ellen.yaml}'",
     "resident_persona_swap"),
    ("node --eval \"require('fs').rmSync('/config/specialists/x',"
     "{recursive:true})\"", "specialist_install_inspect"),
    # Wrapped invocation still detected.
    ("bash -c 'python3 -c \"open(\\\"plugins/registry.json\\\",\\\"w\\\")\"'",
     "plugin_add"),
])
async def test_inline_interpreter_with_managed_token_denied(cmd, route_marker):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert _decision(out) == "deny", cmd
    assert route_marker in _reason(out), cmd


@pytest.mark.parametrize("cmd", [
    # No managed token — inline code alone is not a deny.
    "python3 -c \"print('hello world')\"",
    # Managed token but NO inline-code flag and no write shape (plain
    # script execution / read) stays allowed.
    "python3 /config/plugins/store/x/main.py",
])
async def test_interpreter_forms_without_both_conditions_pass(cmd):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert out == {}, cmd


# ------------------------------------------------------------------
# Round-2 F5: write VERBS only count in command position — verbs inside
# argument text (grep patterns/args) no longer false-deny
# ------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    "grep -r install /config/plugins/store",
    "grep tar /config/plugins/x",
    "grep -r 'touch' /config/bindings",
    "git -C /config log -- agents/specialists",
])
async def test_write_verbs_in_argument_text_pass(cmd):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert out == {}, cmd


@pytest.mark.parametrize("cmd,route_marker", [
    ("install -m 644 f /config/plugins/x", "plugin_add"),
    ("find /config/specialists -delete", "specialist_install_inspect"),
    # Wrapper prefixes still expose the command-position verb.
    ("sudo cp /tmp/x /config/bindings/ellen.yaml", "resident_persona_swap"),
    ("timeout 5 rm -rf /config/specialists/x", "specialist_install_inspect"),
    ("find /tmp -name '*.yaml' | xargs cp -t /config/bindings",
     "resident_persona_swap"),
])
async def test_command_position_verbs_still_deny(cmd, route_marker):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert _decision(out) == "deny", cmd
    assert route_marker in _reason(out), cmd


# ------------------------------------------------------------------
# Round-2 W2 (Sol): /config/personas is managed — persona installs are
# consent-gated exactly like specialists
# ------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
@pytest.mark.parametrize("path", [
    "/config/personas/warm-helper/0.1.0/persona.yaml",
    "personas/warm-helper/0.1.0/persona.yaml",   # relative (F2 applies too)
])
async def test_personas_write_edit_denied(tool_name, path):
    out = await _hook()(
        {"tool_name": tool_name, "tool_input": {"file_path": path}},
        None, {},
    )
    assert _decision(out) == "deny", path
    assert "persona_install_inspect" in _reason(out), path


@pytest.mark.parametrize("cmd", [
    "cp /tmp/p.yaml /config/personas/warm-helper/0.1.0/persona.yaml",
    "echo x > personas/warm-helper/0.1.0/persona.yaml",
    "rm -rf /config/personas/warm-helper",
])
async def test_personas_bash_write_denied(cmd):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert _decision(out) == "deny", cmd
    assert "persona_install_inspect" in _reason(out), cmd


async def test_personas_bash_read_passes():
    out = await _hook()(
        {"tool_name": "Bash",
         "tool_input": {"command": "ls -la /config/personas"}}, None, {},
    )
    assert out == {}


# ------------------------------------------------------------------
# Round-2 W3 (Sol): target-aware redirects and cp/rsync/install
# destinations — reading FROM a managed tree is not a write
# ------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    # Copy FROM managed = read of managed, write elsewhere.
    "cp /config/plugins/store/x/file /tmp/out",
    # stderr redirect to a NON-managed target is not a managed write.
    "grep pat /config/plugins/store/x 2>/tmp/error",
    "grep pat /config/specialists/x 2>&1 | head",
])
async def test_managed_source_read_shapes_pass(cmd):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert out == {}, cmd


# ------------------------------------------------------------------
# Round-4 (Terra P0), re-pointed in v0.351.0: the configurator
# session Casa builds carries managed_component_guard. Its hooks ship with the
# image (the role artifact's ``hooks.pre_tool_use``) and nothing in /config can
# repoint or hollow them, so the guard is listed once and run as written; the
# session is built through ``tools._build_specialist_options``.
# ------------------------------------------------------------------


_SPECIALIST_WRITE = {
    "tool_name": "Write",
    "tool_input": {"file_path": "/config/agents/specialists/x/runtime.yaml"},
}


async def _managed_denies(opts) -> int:
    """How many PreToolUse callbacks in built ClaudeAgentOptions return a
    managed deny for a specialist hand-write."""
    n = 0
    for matcher in opts.hooks["PreToolUse"]:
        for cb in matcher.hooks:
            out = await cb(dict(_SPECIALIST_WRITE), None, {})
            if out and _decision(out) == "deny" \
                    and "managed_component_guard" in _reason(out):
                n += 1
    return n


async def _stack_denies_managed(opts) -> bool:
    return await _managed_denies(opts) > 0


class TestGuardIsCodeMandatoryInCasa:
    def _build(self, monkeypatch):
        import agent_loader
        import tools
        monkeypatch.setattr(tools, "_mcp_registry", None, raising=False)
        monkeypatch.setattr(tools, "_agent_registry", None, raising=False)
        return tools._build_specialist_options(agent_loader.load_configurator())

    async def test_declared_guard_not_double_appended(self, monkeypatch):
        """The shipped configurator declares the policy once, and the session
        Casa builds for it carries it exactly once — and denies."""
        opts = self._build(monkeypatch)
        assert await _managed_denies(opts) == 1
        assert await _stack_denies_managed(opts)


class TestTheLoadTimeBuildOfTheGuard:
    async def test_a_declared_guard_builds_under_its_canonical_matcher(self):
        """build_policy_callbacks_from_hooks_yaml (the agent loader's
        load-time constructibility check) builds a declared guard under the
        HOOK_POLICIES matcher, never the config-editable declared one, and
        the built callback denies a specialist hand-write."""
        from hooks import build_policy_callbacks_from_hooks_yaml
        built = build_policy_callbacks_from_hooks_yaml({"pre_tool_use": [
            {"policy": "managed_component_guard", "matcher": "Read"}]})
        assert "managed_component_guard" in built
        matcher, cb = built["managed_component_guard"]
        assert matcher == "Write|Edit|Bash"
        out = await cb(dict(_SPECIALIST_WRITE), None, {})
        assert _decision(out) == "deny"


@pytest.mark.parametrize("cmd,route_marker", [
    ("cp /tmp/x /config/plugins/store/y", "plugin_add"),
    ("cp /config/specialists/a /config/specialists/b",
     "specialist_install_inspect"),
    ("echo x > /config/specialists/f", "specialist_install_inspect"),
    # mv mutates its SOURCE too (removes it) — stays blanket, not
    # destination-only (deviation from the literal W3 family list; see
    # the _MANAGED_BLANKET_WRITE_VERBS comment).
    ("mv /config/plugins/store/x /tmp/", "plugin_add"),
    # Option-terminated operand shape is ambiguous -> any managed operand
    # denies (fail-closed).
    ("cp /config/plugins/store/x /tmp/y -S .bak", "plugin_add"),
    # Runtime-expanded redirect target is unknowable -> ambiguous -> deny.
    ("cat /config/bindings/ellen.yaml > $OUT", "resident_persona_swap"),
])
async def test_destination_aware_write_shapes_deny(cmd, route_marker):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert _decision(out) == "deny", cmd
    assert route_marker in _reason(out), cmd


# ------------------------------------------------------------------
# Round-3 S2: GNU attached-form `-tDIR` target directory (cp/install)
# ------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "cp -t/config/plugins /tmp/x",
    "install -t/config/agents/specialists/finance /tmp/payload",
    "cp -t/config/personas/pack /tmp/manifest.yaml",
])
async def test_bash_attached_target_directory_denied(command):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": command}}, None, {})
    assert _decision(out) == "deny", command


async def test_bash_rsync_times_cluster_does_not_downgrade_dest_check():
    # rsync's -t is preserve-times, NOT a target directory: a cluster like
    # `-tv` must not be misread as target "v" (which would turn the managed
    # last operand into an ignored "source").
    out = await _hook()({"tool_name": "Bash", "tool_input": {
        "command": "rsync -tv /tmp/evil /config/plugins/registry.json"}},
        None, {})
    assert _decision(out) == "deny"


async def test_bash_attached_target_nonmanaged_copy_still_reads_ok():
    # Attached -t pointing OUTSIDE managed trees with a managed SOURCE:
    # single-operand shape is ambiguous -> fail-closed deny is accepted; the
    # unambiguous separate-form read (two operands, non-managed dest) passes.
    out = await _hook()({"tool_name": "Bash", "tool_input": {
        "command": "cp /config/plugins/store/x/manifest.json /tmp/out.json"}},
        None, {})
    assert out == {}


# ------------------------------------------------------------------
# Round-3 S4: bindings route names the FULL lifecycle set
# ------------------------------------------------------------------


async def test_bindings_route_names_all_lifecycle_tools():
    out = await _hook()(
        {"tool_name": "Write",
         "tool_input": {"file_path": "/config/bindings/ellen.yaml"}},
        None, {},
    )
    for marker in ("resident_persona_swap", "resident_persona_reset",
                   "persona_apply"):
        assert marker in _reason(out), marker


# ------------------------------------------------------------------
# Round-7 (Terra): git subcommands allowlisted read-only, not denylisted
# ------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "git -C /config/agents/specialists apply /tmp/patch",
    # NOTE: a `git apply` whose -C target is a NON-managed parent (e.g.
    # /config or the executors dir) with the mutation hidden in the patch
    # CONTENT is the opaque-content residual (same class as script files /
    # stdin) — closed for the configurator at the capability layer (no
    # shell).
    "git -C /config/specialists am /tmp/series.mbox",
    "git -C /config/plugins stash pop",
    "git -C /config/bindings mv a.yaml b.yaml",
    "git -C /config/personas rm -r pack",
    "git -C /config/agents/specialists checkout -- finance/runtime.yaml",
])
async def test_bash_git_mutating_subcommands_denied(command):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": command}}, None, {})
    assert _decision(out) == "deny", command


@pytest.mark.parametrize("command", [
    "git -C /config log --oneline -5",
    "git -C /config/agents/specialists status",
    "git -C /config diff HEAD~1 -- agents/specialists/finance/runtime.yaml",
    "git -C /config show HEAD:agents/specialists/finance/runtime.yaml",
    "git -C /config/plugins ls-files",
])
async def test_bash_git_readonly_subcommands_pass(command):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": command}}, None, {})
    assert out == {}, command


async def test_bash_git_unknown_subcommand_fails_closed():
    out = await _hook()({"tool_name": "Bash", "tool_input": {
        "command": "git -C /config/specialists filter-branch --force"}},
        None, {})
    assert _decision(out) == "deny"


# ------------------------------------------------------------------
# Round-8 (Sol): --output on read-only git subcommands is a write
# ------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "git -C /config log --output=/config/plugins/stolen.txt",
    "git -C /config show HEAD --output /config/agents/specialists/finance/x",
    "git -C /config diff --output=$OUT agents/specialists/finance/runtime.yaml",
    "git log --output /config/plugins/exfil.json",
])
async def test_bash_git_output_option_write_denied(command):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": command}}, None, {})
    assert _decision(out) == "deny", command


async def test_bash_git_output_to_nonmanaged_from_managed_read_passes():
    out = await _hook()({"tool_name": "Bash", "tool_input": {
        "command": "git -C /config log --output=/tmp/export.txt -- "
                   "agents/specialists/finance/runtime.yaml"}}, None, {})
    assert out == {}


async def test_bash_git_config_injection_pre_subcommand_denied():
    # Round-8 (Terra): -c/--paginate/--exec-path before the subcommand can
    # execute arbitrary commands with the payload argv-visible.
    out = await _hook()({"tool_name": "Bash", "tool_input": {
        "command": "git -c core.pager='sh -c \"touch /config/plugins/x\"' "
                   "--paginate log"}}, None, {})
    assert _decision(out) == "deny"


async def test_bash_git_no_pager_read_still_passes():
    out = await _hook()({"tool_name": "Bash", "tool_input": {
        "command": "git --no-pager -C /config log --oneline -- "
                   "agents/specialists/finance/runtime.yaml"}}, None, {})
    assert out == {}


# ------------------------------------------------------------------
# Round-9 (Sol): post-subcommand option allowlist on read-only git
# ------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    ("git -C /config grep --open-files-in-pager="
     "'cp /tmp/x /config/plugins/registry.json' pattern"),
    "git -C /config grep -Ovim pattern -- agents/specialists/",
    "git -C /config/specialists fsck --lost-found",
    "git -C /config/agents/specialists reflog expire --expire=now --all",
    "git -C /config log --exec-path=/tmp/evil -- agents/specialists/x",
])
async def test_bash_git_unsafe_postsubcommand_options_denied(command):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": command}}, None, {})
    assert _decision(out) == "deny", command


@pytest.mark.parametrize("command", [
    "git -C /config log --oneline --graph -5 -- agents/specialists/finance",
    "git -C /config grep -n -i persona -- bindings/",
    "git -C /config diff --stat --name-only HEAD~1 -- agents/specialists/",
    "git -C /config log --pretty=format:%H --max-count=3 -- plugins/",
    "git -C /config show --name-status HEAD -- agents/specialists/finance",
])
async def test_bash_git_safe_postsubcommand_options_pass(command):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": command}}, None, {})
    assert out == {}, command


# ------------------------------------------------------------------
# Round-10 (Sol): env-assignment-prefixed commands are write-shaped
# ------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    ("GIT_EXTERNAL_DIFF='sh -c \"cp /tmp/x /config/plugins/registry.json\"' "
     "git -C /config diff HEAD~1 HEAD -- agents/specialists/finance"),
    "LD_PRELOAD=/tmp/evil.so cat /config/specialists/finance/active.yaml",
    "PATH=/tmp/bin:$PATH ls /config/bindings",
    "cd /tmp; GIT_PAGER='tee /config/plugins/x' git -C /config log -- plugins/",
])
async def test_bash_env_prefixed_commands_denied(command):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": command}}, None, {})
    assert _decision(out) == "deny", command


async def test_bash_env_shape_inside_args_not_confused():
    # X=1 as an ARGUMENT (not segment-leading) must not trip the env rule.
    out = await _hook()({"tool_name": "Bash", "tool_input": {
        "command": "grep 'MODE=1' /config/plugins/store/x/plugin.json"}},
        None, {})
    assert out == {}


# ---------------------------------------------------------------------------
# #403 — a resident's triggers.yaml shares this classifier under its own kind
# ---------------------------------------------------------------------------


async def test_resident_trigger_file_is_denied_with_its_own_reason():
    """It is NOT managed component state — it is the operator's file — but it
    is a file no agent may write, because the resident's reminder tools write
    it from inside Casa. It joins the classifier so the Bash half (redirect
    targets, destination-aware cp/rsync, git --output, wrapper unwrapping,
    symlink resolution) is not written twice and cannot drift; the KIND stays
    distinct so each guard says why in its own words."""
    out = await _hook()(
        {"tool_name": "Write",
         "tool_input": {"file_path": "/config/agents/assistant/triggers.yaml"}},
        None, {},
    )
    assert _decision(out) == "deny"
    assert "config_trigger_upsert" in _reason(out)
    assert "managed component state" not in _reason(out)


async def test_resident_trigger_file_bash_redirect_is_denied():
    """The reason the classifier is shared rather than copied: this needed the
    redirect-target resolution the managed rules already own."""
    out = await _hook()(
        {"tool_name": "Bash",
         "tool_input": {"command":
                        "echo x > /config/agents/assistant/triggers.yaml"}},
        None, {},
    )
    assert _decision(out) == "deny"
    assert "config_trigger_upsert" in _reason(out)
