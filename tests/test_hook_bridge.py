"""Tests for drivers.hook_bridge -- Casa hook policy -> CC settings.json."""

from __future__ import annotations

import json

import pytest

PROXY = "/opt/casa/scripts/hook_proxy.sh"


class TestHookBridgeTranslate:
    def test_emits_pretooluse_block_for_each_policy(self):
        from drivers.hook_bridge import translate_hooks_to_settings

        hooks_yaml = {
            "pre_tool_use": [
                {"policy": "casa_config_guard", "matcher": "Write|Edit"},
                {"policy": "commit_size_guard", "matcher": "Bash"},
            ],
        }

        settings = translate_hooks_to_settings(
            hooks_yaml, proxy_script_path="/opt/casa/scripts/hook_proxy.sh",
        )

        assert "hooks" in settings
        pre = settings["hooks"]["PreToolUse"]
        # Two declared entries + the code-mandatory managed_component_guard
        # (round-4 Terra P0: yaml policies are additive-only) + the two
        # containment-floor policies (block_dangerous_bash, path_scope —
        # Task 4 #360), neither of which was declared here.
        # #631: +1 — resident_prompt_write_guard is code-mandatory in this
        # bridge too (file tools only; path_scope's writable prefixes are the
        # executor's own declaration, so a declaration admitting /config/agents
        # would otherwise leave a resident's inert prompt file refused by
        # nothing).
        assert len(pre) == 6

        first = pre[0]
        # Task 4 (#360): matcher is forced to the registry's canonical value
        # regardless of the yaml-declared "Write|Edit" — casa_config_guard's
        # canonical matcher is "Write|Edit|Bash".
        assert first["matcher"] == "Write|Edit|Bash"
        assert first["hooks"][0]["type"] == "command"
        assert first["hooks"][0]["command"].endswith(
            "hook_proxy.sh casa_config_guard"
        )
        commands = [e["hooks"][0]["command"] for e in pre]
        assert any(c.endswith("hook_proxy.sh managed_component_guard")
                    for c in commands)
        assert any(c.endswith("hook_proxy.sh block_dangerous_bash")
                    for c in commands)
        assert any(c.endswith("hook_proxy.sh path_scope")
                    for c in commands)

    def test_empty_hooks_yaml_still_carries_managed_guard(self):
        """Round-4 Terra P0 regression: a hollow hooks.yaml (definition.yaml's
        `hooks_file:` repointed at an empty file) used to emit ZERO hooks —
        the next session then loaded no pre_tool_use policies at all. The
        managed guard entry is now code-mandatory."""
        from drivers.hook_bridge import translate_hooks_to_settings
        settings = translate_hooks_to_settings(
            {}, proxy_script_path="/opt/casa/scripts/hook_proxy.sh",
        )
        pre = settings["hooks"]["PreToolUse"]
        # Task 4 (#360): the empty document also gets both containment-floor
        # policies appended alongside the managed-component guard, and #631's
        # resident_prompt_write_guard — code-mandatory here because
        # `path_scope`'s writable prefixes are declared by the executor, so
        # nothing else refuses a resident's inert prompt file to a declaration
        # that admits it.
        assert len(pre) == 4
        guard = next(
            e for e in pre
            if e["hooks"][0]["command"].endswith(
                "hook_proxy.sh managed_component_guard")
        )
        assert guard["matcher"] == "Write|Edit|Bash"
        commands = [e["hooks"][0]["command"] for e in pre]
        assert any(c.endswith("hook_proxy.sh block_dangerous_bash")
                    for c in commands)
        assert any(c.endswith("hook_proxy.sh path_scope")
                    for c in commands)

    def test_malformed_list_members_skipped_not_crash(self):
        """#354: a syntactically valid ``pre_tool_use: [null]`` (or a scalar
        member) passed boot/reload, then raised AttributeError on ``e.get()``
        when the first engagement provisioned its workspace. Non-mapping
        members are skipped; valid members and the mandatory guard survive."""
        from drivers.hook_bridge import translate_hooks_to_settings

        settings = translate_hooks_to_settings(
            {"pre_tool_use": [None, "junk", 7,
                              {"policy": "casa_config_guard",
                               "matcher": "Write"}]},
            proxy_script_path=PROXY,
        )
        pre = settings["hooks"]["PreToolUse"]
        commands = [e["hooks"][0]["command"] for e in pre]
        assert f"{PROXY} casa_config_guard" in commands
        assert f"{PROXY} managed_component_guard" in commands
        # Task 4 (#360): the two floor policies are also appended (neither
        # was declared) alongside the one valid member + managed guard.
        assert f"{PROXY} block_dangerous_bash" in commands
        assert f"{PROXY} path_scope" in commands
        assert len(pre) == 5   # skipped members emit nothing; +1 = #631

    def test_non_mapping_document_root_treated_as_empty(self):
        """#354 (Sol r5-3): a hooks file whose ROOT is valid yaml but not a
        mapping (`[]`, a scalar, a list of policies) must not crash
        provisioning — it reads as empty and the mandatory guard is emitted."""
        from drivers.hook_bridge import translate_hooks_to_settings

        for bad_root in ([], "oops", 7, [{"policy": "x"}], None):
            settings = translate_hooks_to_settings(
                bad_root, proxy_script_path=PROXY,
            )
            pre = settings["hooks"]["PreToolUse"]
            # Task 4 (#360): managed guard + both floor policies.
            assert len(pre) == 4
            commands = [e["hooks"][0]["command"] for e in pre]
            assert any(c.endswith("managed_component_guard") for c in commands)
            assert any(c.endswith("block_dangerous_bash") for c in commands)
            assert any(c.endswith("path_scope") for c in commands)

    def test_non_list_hook_section_treated_as_empty(self):
        """#354 companion: a scalar/mapping ``pre_tool_use:`` value must not
        crash provisioning either — only the mandatory guard is emitted."""
        from drivers.hook_bridge import translate_hooks_to_settings

        for bad in ("oops", {"policy": "x"}, 3):
            settings = translate_hooks_to_settings(
                {"pre_tool_use": bad}, proxy_script_path=PROXY,
            )
            pre = settings["hooks"]["PreToolUse"]
            # Task 4 (#360): managed guard + both floor policies.
            assert len(pre) == 4
            commands = [e["hooks"][0]["command"] for e in pre]
            assert any(c.endswith("managed_component_guard") for c in commands)
            assert any(c.endswith("block_dangerous_bash") for c in commands)
            assert any(c.endswith("path_scope") for c in commands)

    def test_unparseable_timeout_skipped_not_crash(self):
        """#354 companion: ``timeout: abc`` passed boot validation, then
        ``int()`` raised at provisioning. An unparseable timeout is dropped;
        the hook entry itself survives."""
        from drivers.hook_bridge import translate_hooks_to_settings

        for bad_timeout in ("abc", float("inf"), float("nan"), [60]):
            settings = translate_hooks_to_settings(
                {"pre_tool_use": [{"policy": "casa_config_guard",
                                   "matcher": "Write",
                                   "timeout": bad_timeout}]},
                proxy_script_path=PROXY,
            )
            entry = settings["hooks"]["PreToolUse"][0]
            assert entry["hooks"][0]["command"].endswith("casa_config_guard")
            assert "timeout" not in entry["hooks"][0]

    def test_translates_bundled_plugin_developer_hooks_yaml(self):
        """L-1b regression: bundled snake_case hooks.yaml must translate."""
        import yaml
        from pathlib import Path
        from drivers.hook_bridge import translate_hooks_to_settings

        here = Path(__file__).resolve().parent.parent
        hooks_path = (
            here / "casa" / "rootfs" / "opt" / "casa" / "defaults"
            / "agents" / "executors" / "plugin-developer" / "hooks.yaml"
        )
        raw = yaml.safe_load(hooks_path.read_text(encoding="utf-8")) or {}
        settings = translate_hooks_to_settings(
            raw, proxy_script_path="/opt/casa/scripts/hook_proxy.sh",
        )
        assert "PreToolUse" in settings["hooks"]
        assert len(settings["hooks"]["PreToolUse"]) >= 1


class TestCanonicalMatcherForced:
    """Task 4 (#360, Sol r2 generalization): every registry-backed policy's
    matcher is forced to HOOK_POLICIES' canonical value, not just the floor
    — a yaml-supplied matcher on ANY registry policy is misroutable
    (definition.yaml's hooks_file: is a config-editable pointer)."""

    def test_block_dangerous_bash_matcher_forced_to_canonical(self):
        from drivers.hook_bridge import translate_hooks_to_settings

        settings = translate_hooks_to_settings(
            {"pre_tool_use": [
                {"policy": "block_dangerous_bash", "matcher": "Read"},
                {"policy": "path_scope", "matcher": "Read"},
            ]},
            proxy_script_path=PROXY,
        )
        pre = settings["hooks"]["PreToolUse"]
        entry = next(
            e for e in pre
            if e["hooks"][0]["command"].endswith("block_dangerous_bash")
        )
        assert entry["matcher"] == "Bash"

    def test_self_containment_guard_matcher_forced_to_canonical(self):
        """Verified against the registry: HOOK_POLICIES['self_containment_guard']
        ['matcher'] is 'Bash'."""
        from drivers.hook_bridge import translate_hooks_to_settings
        from hooks import HOOK_POLICIES

        expected = HOOK_POLICIES["self_containment_guard"]["matcher"]
        assert expected == "Bash"

        settings = translate_hooks_to_settings(
            {"pre_tool_use": [
                {"policy": "self_containment_guard", "matcher": "Read"},
            ]},
            proxy_script_path=PROXY,
        )
        pre = settings["hooks"]["PreToolUse"]
        entry = next(
            e for e in pre
            if e["hooks"][0]["command"].endswith("self_containment_guard")
        )
        assert entry["matcher"] == expected

    def test_non_registry_policy_keeps_yaml_matcher(self):
        """engagement_permission_relay has no HOOK_POLICIES entry — its
        yaml-declared matcher is the only source of truth and must survive."""
        from drivers.hook_bridge import translate_hooks_to_settings

        settings = translate_hooks_to_settings(
            {"pre_tool_use": [
                {"policy": "engagement_permission_relay", "matcher": ".*",
                 "timeout": 600},
            ]},
            proxy_script_path=PROXY,
        )
        pre = settings["hooks"]["PreToolUse"]
        entry = next(
            e for e in pre
            if e["hooks"][0]["command"].endswith("engagement_permission_relay")
        )
        assert entry["matcher"] == ".*"

    def test_missing_path_scope_appended_with_canonical_matcher(self):
        """Defense-in-depth (#360): a doc missing path_scope entirely still
        gets it appended with the registry's canonical matcher."""
        from drivers.hook_bridge import translate_hooks_to_settings

        settings = translate_hooks_to_settings(
            {"pre_tool_use": [
                {"policy": "block_dangerous_bash"},
            ]},
            proxy_script_path=PROXY,
        )
        pre = settings["hooks"]["PreToolUse"]
        entry = next(
            e for e in pre
            if e["hooks"][0]["command"].endswith("hook_proxy.sh path_scope")
        )
        assert entry["matcher"] == "Read|Write|Edit"


class TestTimeoutPassthrough:
    """C-1 follow-up: per-hook ``timeout`` must propagate to CC settings.

    CC's hook-runner default is 60s; engagement_permission_relay needs
    ~600s for the operator-response window (C-1 spec section 4.6).
    Without pass-through CC kills the hook before the operator can reply.
    """

    def test_timeout_emitted_when_present(self):
        from drivers.hook_bridge import translate_hooks_to_settings

        hooks_yaml = {
            "pre_tool_use": [
                {"policy": "foo", "matcher": ".*", "timeout": 600},
            ],
        }
        out = translate_hooks_to_settings(
            hooks_yaml, proxy_script_path=PROXY,
        )
        entry = out["hooks"]["PreToolUse"][0]
        assert entry["matcher"] == ".*"
        cc_hook = entry["hooks"][0]
        assert cc_hook["type"] == "command"
        assert cc_hook["command"].endswith("hook_proxy.sh foo")
        assert cc_hook["timeout"] == 600

    def test_timeout_omitted_when_absent(self):
        from drivers.hook_bridge import translate_hooks_to_settings

        hooks_yaml = {
            "pre_tool_use": [
                {"policy": "foo", "matcher": ".*"},
            ],
        }
        out = translate_hooks_to_settings(
            hooks_yaml, proxy_script_path=PROXY,
        )
        cc_hook = out["hooks"]["PreToolUse"][0]["hooks"][0]
        assert "timeout" not in cc_hook

    def test_timeout_coerced_to_int(self):
        """YAML may parse numeric strings or floats; we want int seconds."""
        from drivers.hook_bridge import translate_hooks_to_settings

        hooks_yaml = {
            "pre_tool_use": [
                {"policy": "foo", "matcher": ".*", "timeout": "600"},
            ],
        }
        out = translate_hooks_to_settings(
            hooks_yaml, proxy_script_path=PROXY,
        )
        cc_hook = out["hooks"]["PreToolUse"][0]["hooks"][0]
        assert cc_hook["timeout"] == 600
        assert isinstance(cc_hook["timeout"], int)

    def test_none_timeout_omitted(self):
        """Explicit None should not emit a bogus 0 or null timeout."""
        from drivers.hook_bridge import translate_hooks_to_settings

        hooks_yaml = {
            "pre_tool_use": [
                {"policy": "foo", "matcher": ".*", "timeout": None},
            ],
        }
        out = translate_hooks_to_settings(
            hooks_yaml, proxy_script_path=PROXY,
        )
        cc_hook = out["hooks"]["PreToolUse"][0]["hooks"][0]
        assert "timeout" not in cc_hook

    def test_bundled_engagement_permission_relay_has_600s_timeout(self):
        """Bundled C-1 policy must surface timeout=600 in CC settings.

        v0.37.4: only claude_code-driver executors carry this policy.
        Configurator was reverted (driver: in_casa — the hook's cwd
        resolver can't match its tool calls; see spec §4.6).
        """
        import yaml
        from pathlib import Path
        from drivers.hook_bridge import translate_hooks_to_settings

        here = Path(__file__).resolve().parent.parent
        for executor in ("plugin-developer",):
            hooks_path = (
                here / "casa" / "rootfs" / "opt" / "casa" / "defaults"
                / "agents" / "executors" / executor / "hooks.yaml"
            )
            raw = yaml.safe_load(hooks_path.read_text(encoding="utf-8")) or {}
            settings = translate_hooks_to_settings(
                raw, proxy_script_path=PROXY,
            )
            relay_entries = [
                entry
                for entry in settings["hooks"].get("PreToolUse", [])
                if any(
                    "engagement_permission_relay" in h["command"]
                    for h in entry["hooks"]
                )
            ]
            assert relay_entries, (
                f"{executor} hooks.yaml missing engagement_permission_relay"
            )
            cc_hook = relay_entries[0]["hooks"][0]
            assert cc_hook.get("timeout") == 600, (
                f"{executor}: expected timeout=600, got "
                f"{cc_hook.get('timeout')!r}"
            )

    def test_configurator_does_not_carry_relay_policy(self):
        """v0.37.4: configurator's bundled defaults must NOT include
        engagement_permission_relay — its driver: in_casa setup means
        the hook's cwd-based engagement resolver cannot match its tool
        calls, and including it would deny every configurator tool
        call. See memory project_v037_2_v037_3_c1_shipped.md
        follow-up #2."""
        import yaml
        from pathlib import Path

        here = Path(__file__).resolve().parent.parent
        hooks_path = (
            here / "casa" / "rootfs" / "opt" / "casa" / "defaults"
            / "agents" / "executors" / "configurator" / "hooks.yaml"
        )
        raw = yaml.safe_load(hooks_path.read_text(encoding="utf-8")) or {}
        policies = [
            entry.get("policy")
            for entry in raw.get("pre_tool_use", [])
        ]
        assert "engagement_permission_relay" not in policies, (
            "configurator must not carry engagement_permission_relay — "
            "wrong driver (in_casa vs claude_code); see spec §4.6"
        )
