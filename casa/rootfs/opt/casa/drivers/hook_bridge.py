"""Translate Casa executor hooks.yaml into Claude Code .claude/settings.json.

The CC hook shape is:
    {"hooks": {"PreToolUse": [{"matcher": "...", "hooks": [
        {"type": "command", "command": "<proxy-script> <policy-name>"}
    ]}]}}

Casa's hooks.yaml shape (per configurator + plugin-developer defaults):
    pre_tool_use:
      - policy: casa_config_guard
        matcher: Write|Edit
"""

from __future__ import annotations


def translate_hooks_to_settings(
    hooks_yaml: dict, *, proxy_script_path: str,
) -> dict:
    """Convert Casa hooks.yaml -> CC settings.json shape.

    Reads snake_case keys (``pre_tool_use``, ``post_tool_use``) per
    ``defaults/schema/hooks.v1.json``; emits PascalCase
    (``PreToolUse``, ``PostToolUse``) per CC settings.json shape.

    Round-4 (Terra P0): the emitted ``PreToolUse`` block ALWAYS carries a
    ``managed_component_guard`` entry, whatever the yaml declares. Both
    claude_code workspace settings writers (drivers.workspace legacy +
    template paths) route through this function, and definition.yaml's
    ``hooks_file:`` key is a config-editable POINTER — repointing it at a
    hollow yaml previously emitted ZERO hooks, shedding every policy for
    the next session. Yaml policies are additive-only.
    """
    # #354 (Sol r5-3): the document ROOT is attacker-editable too — valid
    # yaml like `[]` or a bare scalar crashed provisioning on .get(). A
    # non-mapping root reads as empty; the mandatory guard still emits.
    if not isinstance(hooks_yaml, dict):
        hooks_yaml = {}

    from hooks import (
        HOOK_POLICIES,
        REQUIRED_CLAUDE_CODE_POLICIES,
        canonical_matcher_for,
    )

    out: dict = {"hooks": {}}
    declared_pre_tool_use_policies: set = set()
    for snake, pascal in (
        ("pre_tool_use", "PreToolUse"),
        ("post_tool_use", "PostToolUse"),
    ):
        entries = hooks_yaml.get(snake, []) or []
        # #354: the yaml is a mutable trust surface — a non-list section or a
        # non-mapping member (``pre_tool_use: [null]``) is syntactically valid
        # yaml that passed boot/reload, then crashed engagement provisioning
        # on ``e.get()``. Treat malformed shapes as absent (the mandatory
        # guard below is appended regardless).
        if not isinstance(entries, list):
            entries = []
        if not entries:
            continue
        out_entries = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            policy = e.get("policy")
            if not policy:
                continue
            if snake == "pre_tool_use":
                declared_pre_tool_use_policies.add(policy)
            # Task 4 (#360, Sol r2 generalization): every registry-backed
            # policy's matcher is code-mandatory here, not just the floor.
            # hooks.yaml (definition.yaml's config-editable ``hooks_file:``
            # pointer) can declare ANY matcher on ANY policy — a bare
            # ``block_dangerous_bash`` with ``matcher: Read`` previously
            # loaded fine and silently never ran against Bash. Only the two
            # live non-registry policies (engagement_permission_relay,
            # engagement_buttons_reminder) keep their yaml-declared matcher —
            # there is no registry entry to override them with.
            if policy in HOOK_POLICIES:
                matcher = canonical_matcher_for(policy)
            else:
                matcher = e.get("matcher", ".*")
            cc_hook: dict = {
                "type": "command",
                "command": f"{proxy_script_path} {policy}",
            }
            # Pass-through optional per-hook timeout (seconds). CC's default
            # is 60s; engagement_permission_relay needs ~600s for the
            # operator-response window (C-1 spec §4.6). #354: an unparseable
            # timeout is dropped rather than crashing provisioning.
            if "timeout" in e and e["timeout"] is not None:
                try:
                    cc_hook["timeout"] = int(e["timeout"])
                except (TypeError, ValueError, OverflowError):
                    pass  # OverflowError: yaml `.inf` (Terra r7-1)
            out_entries.append({
                "matcher": matcher,
                "hooks": [cc_hook],
            })
        out["hooks"][pascal] = out_entries

    # Round-4 (Terra P0) mandatory guard entry. The canonical matcher comes
    # from HOOK_POLICIES so the two paths can't drift. Dedupe: skip only
    # when the yaml already emitted the policy with the canonical matcher —
    # since every registry policy above is now FORCED to its canonical
    # matcher, a yaml declaration of managed_component_guard can no longer
    # emit anything else, so ".*" can no longer occur here.
    canonical_matcher = HOOK_POLICIES["managed_component_guard"]["matcher"]
    guard_cmd = f"{proxy_script_path} managed_component_guard"
    pre = out["hooks"].setdefault("PreToolUse", [])
    already = any(
        e.get("matcher") == canonical_matcher
        and any(h.get("command") == guard_cmd for h in e.get("hooks", []))
        for e in pre
    )
    if not already:
        pre.append({
            "matcher": canonical_matcher,
            "hooks": [{"type": "command", "command": guard_cmd}],
        })

    # Task 4 (#360): both containment-floor policies (block_dangerous_bash,
    # path_scope) must be present in every emitted PreToolUse block, exactly
    # like the mandatory guard above — a hollowed or repointed hooks.yaml
    # must not shed the floor. For a claude_code executor the floor is
    # already load-enforced (Task 2/3: missing_required_cc_policies gates
    # loading), so this append is defense-in-depth for any document that
    # reaches this bridge by another route. Emitted with only the policy
    # name + canonical matcher — no synthesized params (path_scope's
    # writable/readable default to empty inside its factory, which is the
    # safe/deny-leaning default).
    for floor_policy in sorted(REQUIRED_CLAUDE_CODE_POLICIES):
        if floor_policy in declared_pre_tool_use_policies:
            continue
        floor_cmd = f"{proxy_script_path} {floor_policy}"
        already_floor = any(
            any(h.get("command") == floor_cmd for h in e.get("hooks", []))
            for e in pre
        )
        if already_floor:
            continue
        pre.append({
            "matcher": canonical_matcher_for(floor_policy),
            "hooks": [{"type": "command", "command": floor_cmd}],
        })
    return out
