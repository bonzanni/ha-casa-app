"""#631 red case — INV-PERS-012 (re-specified under D36).

`agents/<role>/prompts/system.md` is a committed, schema-pointed,
reload-documented config file that is an input to nothing a persona-bound
resident is ever served: `character.yaml`'s `prompt_file:` pointer feeds
`agent_loader._compose_prompt`, whose output is `cfg.system_prompt`, which
`Agent._build_options` uses only on the no-bundle arm — and every resident is
bundle-bound from its first boot. The file IS read at load (`_resolve_prose`
opens it unconditionally); what is true is that its bytes reach only the
COMPOSED fallback prompt, which is not what a bundle-bound resident is served.
Meanwhile five configurator doctrine sites and `casa/DOCS.md` used to route
behavioural instructions into it and assert the edit was live on the next turn.

This is `response_shape.yaml`/#610 one file over, and this suite is that suite's
argument one file over. It asserts COUNTS, because the failure it exists to
catch is silent: an edit that is written, committed and reported live while the
served prompt is byte-identical.

The invariant is NARROWED to the four file primitives — `Write`, `Edit`,
`MultiEdit`, `NotebookEdit`. `Bash` is deliberately NOT routed to this guard
and its callback classifies no command text: a text predicate over an
unexecuted shell command was measured wrong in both directions (it refused
reads the invariant promises to allow, and missed writes), and the operator
ruled it out (D36). A shell-capable agent can therefore still make the edit;
it is inert for a bundle-bound resident, and no shipped resident holds `Bash`.

Re-specified by the red-case reviewer (Terra), 2026-09-01, before any production
change; the previous accepted version of this file routed `Bash` as a fifth
primitive. Two deliberate departures from the ORIGINAL specification are kept,
both stated so the acceptor can rule on them:

  1. The builder-wiring assertions invoke the built option stack and count
     DENIALS rather than monkeypatching a counting sentinel over the matcher
     factory. It yields the same counts, it cannot drift from the real callback,
     and it still fails for the specified reason pre-fix (nothing in the stack
     denies) rather than on an absent symbol.
  2. `_guard_callbacks` returns `[]` when the policy is unregistered, for the
     same reason.
"""

from __future__ import annotations

import asyncio
import builtins
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

GUARD = "resident_prompt_write_guard"
# The four file-writing primitives, and ONLY those: the guard's whole contract.
PRIMITIVES = ("Write", "Edit", "MultiEdit", "NotebookEdit")
BASH = "Bash"
ROLES = ("assistant", "butler", "concierge")


def _decision(out: dict) -> str | None:
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision")


def _guard_callbacks() -> list:
    """The registered guard, or an empty list when it is not registered.

    Deliberately tolerant of absence: on the pre-fix tree this suite must fail
    because nothing REFUSES the write, not because a symbol is missing.
    """
    from hooks import HOOK_POLICIES
    policy = HOOK_POLICIES.get(GUARD)
    return [] if policy is None else [policy["factory"]()]


def _registry_matcher() -> str:
    from hooks import HOOK_POLICIES
    policy = HOOK_POLICIES.get(GUARD)
    return "" if policy is None else policy["matcher"]


def _routes(matcher: str | None, tool_name: str) -> bool:
    """Whether an SDK/CC matcher string would ROUTE this tool to its hook.

    Invoking a callback directly proves nothing about production: a callback
    registered under matcher `Read` never sees a `Write`. The acceptor
    demonstrated exactly that false green — with both the client and resolver
    matchers set to `Read`, direct invocation still produced every asserted
    count. Every assertion below therefore routes first.
    """
    if matcher is None:
        return True          # a matcher of None routes every tool call
    return re.fullmatch(matcher, tool_name) is not None


async def _denies(callbacks, payload: dict, matcher: str | None) -> bool:
    if not _routes(matcher, payload["tool_name"]):
        return False
    for cb in callbacks:
        out = await cb(dict(payload), None, {})
        if out and _decision(out) == "deny":
            return True
    return False


# The four file-writing primitives and the argument each carries its target in.
FILE_PRIMITIVES = (
    ("Write", "file_path"),
    ("Edit", "file_path"),
    ("MultiEdit", "file_path"),
    ("NotebookEdit", "notebook_path"),
)


def _equivalent_spellings(rel: str) -> list[str]:
    """Every POSIX-equivalent rewriting of *rel* this grammar can produce.

    GENERATED at every segment boundary, not tabulated. Three productions —
    a `.` segment, a `<bogus>/..` round trip, and a doubled separator — are
    applied at each of the path's boundaries in turn. The result is closed
    under position, which is what an enumerated table was not: rounds 2, 3 and
    4 were each returned because the table had omitted one cell, and the fifth
    cell would have been omitted too.

    Combined with `test_the_classifier_resolves_before_it_decides` below, this
    is the acceptor's own resolution of the fork it named at round 4: a bounded
    transformation grammar, plus a direct check that the implementation
    NORMALIZES rather than matching spellings. A finite black-box set can
    always be memorised by a fixture-specialized guard; a normalization proof
    cannot be.
    """
    segs = rel.split("/")
    out = {rel}
    # An ABSOLUTE path splits with a leading empty segment; inserting before
    # it turns the path relative (`.//config/...` resolves to
    # `/config/config/...`), which names a different file. Start after it.
    first = 1 if segs and segs[0] == "" else 0
    for i in range(first, len(segs) + 1):
        out.add("/".join(segs[:i] + ["."] + segs[i:]))
        out.add("/".join(segs[:i] + ["zz", ".."] + segs[i:]))
        if 0 < i < len(segs):
            out.add("/".join(segs[:i]) + "//" + "/".join(segs[i:]))
    return sorted(out)


def _target_spellings(role: str) -> list[tuple[str, str]]:
    """(cwd, raw_path) pairs that ALL name one resident's own prompt file.

    Three anchorings — an executor's absolute path from `/config`, a
    resident's relative path from its own agent home, and a bare basename from
    inside the prompts directory — each expanded through the grammar above.
    """
    home = f"/config/agents/{role}"
    pairs: list[tuple[str, str]] = []
    for cwd, rel in (("/config", f"{home}/prompts/system.md"),
                     (home, "prompts/system.md"),
                     (f"{home}/prompts", "system.md")):
        pairs += [(cwd, raw) for raw in _equivalent_spellings(rel)]
    # And one anchoring that reaches the file from a SIBLING resident's home,
    # which no rewriting of the three above produces.
    other = "butler" if role != "butler" else "concierge"
    pairs.append((f"/config/agents/{other}", f"../{role}/prompts/system.md"))
    return pairs


def _calls_for(role: str) -> list[dict]:
    """Every (file primitive x equivalent spelling) pair for one resident.

    File primitives ONLY. The shell forms that used to be appended here are
    the mechanism D36 cut; they now live in `_bash_calls_for`, where the
    assertion on them is that they are ALLOWED.
    """
    return [
        {"tool_name": tool, "cwd": cwd, "tool_input": {arg: raw}}
        for tool, arg in FILE_PRIMITIVES
        for cwd, raw in _target_spellings(role)
    ]


def _bash_calls_for(role: str) -> list[dict]:
    """The three canonical shell spellings the CUT Bash half used to deny.

    Kept verbatim from the previous accepted version of this file, so that the
    assertion that they are now allowed is made against the very payloads that
    were denied before — a resident's redirect with the evidence in the cwd,
    the quote splice, a `sed -i`, and an executor's absolute form.
    """
    home = f"/config/agents/{role}"
    target = f"{home}/prompts/system.md"
    return [
        {"tool_name": BASH, "cwd": home,
         "tool_input": {"command": 'printf x > prompts/"system.md"'}},
        {"tool_name": BASH, "cwd": home,
         "tool_input": {"command": "sed -i s/a/b/ ./prompts/system.md"}},
        {"tool_name": BASH, "cwd": "/config",
         "tool_input": {"command": f"printf x > {target}"}},
    ]


PER_RESIDENT_CALLS = len(_calls_for("assistant"))
ALL_CALLS = PER_RESIDENT_CALLS * len(ROLES)
BASH_CALLS = sum(len(_bash_calls_for(r)) for r in ROLES)


class TestTheGuardIsRegisteredAndRefuses:

    def test_registry_primitives_and_denial_counts(self):
        """(registry_entries, routed_primitives, denied_calls) == (1, 4, 384).

        Four routed primitives — and `routed_primitives` is counted over the
        four file tools PLUS `Bash`, so a matcher that routes the shell reads
        5 here and fails. 384 = 4 primitives x 32 generated spellings x 3
        residents.

        At this file's parent (the Bash half present) the tuple is
        (1, 5, 384): Bash is routed as a fifth primitive. At the base
        `418c2e59` it is (0, 0, 0): no resident-prompt policy is registered,
        so every write goes unrefused. Neither is an import error; both are
        the intended failures.
        """
        cbs = _guard_callbacks()
        matcher = _registry_matcher()

        registry_entries = 1 if cbs else 0
        routed_primitives = sum(
            1 for p in PRIMITIVES + (BASH,) if _routes(matcher, p))

        async def run() -> int:
            denied = 0
            for role in ROLES:
                for payload in _calls_for(role):
                    if await _denies(cbs, payload, matcher):
                        denied += 1
            return denied

        denied_calls = asyncio.run(run())
        assert (registry_entries, routed_primitives, denied_calls) == (1, 4, ALL_CALLS)
        assert ALL_CALLS == 384

    def test_the_shell_is_not_classified_at_all(self):
        """(registry_entries, bash_denials, bash_allows) == (1, 0, 9).

        The callback is invoked DIRECTLY, bypassing matcher routing on purpose:
        a narrowed matcher would hide a Bash branch that is still in the
        callback, and the invariant says the shell is not classified, not
        merely not routed. Every one of the nine canonical spellings the cut
        half used to deny must come back as an allow (`{}`), including the
        redirect and `sed -i` forms.

        At this file's parent the tuple is (1, 9, 0): the `elif tool_name ==
        "Bash"` branch still denies each spelling. At the base it is
        (0, 0, 0): nothing is registered.
        """
        cbs = _guard_callbacks()

        async def run() -> tuple[int, int]:
            denied = allowed = 0
            for role in ROLES:
                for payload in _bash_calls_for(role):
                    for cb in cbs:
                        out = await cb(dict(payload), None, {})
                        if out and _decision(out) == "deny":
                            denied += 1
                        else:
                            allowed += 1
            return denied, allowed

        bash_denials, bash_allows = asyncio.run(run())
        assert (1 if cbs else 0, bash_denials, bash_allows) == (1, 0, BASH_CALLS)
        assert BASH_CALLS == 9


    @pytest.mark.parametrize("tool,arg", FILE_PRIMITIVES)
    def test_the_decision_follows_the_resolved_path_not_the_spelling(
        self, tool, arg, monkeypatch,
    ):
        """The guard's decision must be a function of the RESOLVED path.

        This is the property no finite equivalence set can carry, and it took
        the acceptor five rounds to pin down precisely: a set can always be
        memorised, and merely OBSERVING a call to the normalizer proves only
        that it ran, not that its result decided anything — a guard that
        normalizes and discards the result passes that weaker check.

        So the normalizer is REPLACED, in both directions, and the decision is
        read off:

          (a) resolve everything TO the resident's file: a payload naming a
              wholly unrelated path must DENY;
          (b) resolve everything AWAY from it: a payload naming the resident's
              real absolute path must ALLOW;
          (c) LEXICAL resolution yields an innocent alias while `realpath`
              yields the resident's file: the write must DENY — for an
              ABSOLUTE alias and for a RELATIVE one, because a guard that
              consults `realpath` only for absolute inputs passed (c) while
              allowing `alias.md` from inside the prompts directory.

        (c) is the symlink re-ask, and it was returned as missing: a guard that
        normalizes lexically for all four primitives and never consumes
        `os.path.realpath` passed every other assertion in this file while
        allowing `Write(file_path=".../prompts/alias.md")` — a symlink whose
        target is `system.md`, so the operation changes the resident's inert
        prompt anyway.

        A guard that decides by spelling fails (a) — the raw path is in no
        fixture set — and fails (b) — the raw path is in every one. A guard
        that ignores the resolver fails both. Only a guard whose classifier
        consumes the resolver's OUTPUT satisfies both.

        Run for EVERY file primitive, with that primitive's own path argument.
        A single-primitive version was returned: a guard that resolved
        correctly for `Write` and matched raw fixtures for the other three
        passed the whole file while allowing
        `Edit(file_path="/config/./agents/./assistant/prompts/system.md")` —
        two simultaneous rewrites, which the one-rewrite-at-a-time grammar
        does not generate.

        The clause is about the FILE tools because those are the only tools
        this guard is routed; a `Bash` call never reaches it, by ruling (D36),
        and `test_the_shell_is_not_classified_at_all` pins that separately.
        """
        import hooks as hooks_mod

        cbs = _guard_callbacks()
        matcher = _registry_matcher() or None
        target = "/config/agents/assistant/prompts/system.md"

        unrelated = {"tool_name": tool, "cwd": "/tmp",
                     "tool_input": {arg: "/tmp/unrelated.txt"}}
        real = {"tool_name": tool, "cwd": "/config",
                "tool_input": {arg: target}}

        monkeypatch.setattr(hooks_mod, "_normalize_path", lambda _raw: target)
        # `realpath` is consulted on the allow path; keep it from turning an
        # ALLOW into a filesystem-dependent answer.
        monkeypatch.setattr(hooks_mod.os.path, "realpath", lambda pth: pth)
        resolved_to_it = asyncio.run(_denies(cbs, unrelated, matcher))

        monkeypatch.setattr(hooks_mod, "_normalize_path",
                            lambda _raw: "/tmp/elsewhere.md")
        resolved_away = asyncio.run(_denies(cbs, real, matcher))

        alias = "/config/agents/assistant/prompts/alias.md"
        monkeypatch.setattr(hooks_mod, "_normalize_path", lambda raw: raw)
        monkeypatch.setattr(hooks_mod.os.path, "realpath",
                            lambda pth: target if pth == alias else pth)
        via_symlink = asyncio.run(_denies(
            cbs, {"tool_name": tool, "cwd": "/config",
                  "tool_input": {arg: alias}}, matcher))
        via_relative_symlink = asyncio.run(_denies(
            cbs, {"tool_name": tool,
                  "cwd": "/config/agents/assistant/prompts",
                  "tool_input": {arg: "alias.md"}}, matcher))

        assert (int(resolved_to_it), int(resolved_away), int(via_symlink),
                int(via_relative_symlink)) == (1, 0, 1, 1)

    def test_nothing_outside_a_resident_prompt_file_is_claimed(self):
        """Denial count 0 over everything the guard must NOT claim.

        Reads stay allowed — #610's guard says why in its own words, and
        `resident/grant_ha_tools.md` tells the model to READ butler's copy.
        A specialist's materialized copy belongs to `managed_component_guard`;
        an executor has `prompt.md`, not this file; a resident's
        `prompts/<trigger>.md` IS served (stale until `casa_reload_triggers`),
        so refusing it would be a false claim.
        """
        cbs = _guard_callbacks()
        allowed = [
            {"tool_name": "Read", "cwd": "/config",
             "tool_input": {"file_path": "/config/agents/butler/prompts/system.md"}},
            {"tool_name": "Write", "cwd": "/config",
             "tool_input": {"file_path": "/config/agents/specialists/x/prompts/system.md"}},
            {"tool_name": "Write", "cwd": "/config",
             "tool_input": {"file_path": "/config/agents/executors/configurator/prompt.md"}},
            {"tool_name": "Write", "cwd": "/config",
             "tool_input": {"file_path": "/config/agents/assistant/prompts/morning-briefing.md"}},
            {"tool_name": "Write", "cwd": "/config",
             "tool_input": {"file_path": "/config/agents/assistant/response_shape.yaml"}},
            # The plugin-developer's own workspace: a relative
            # `prompts/system.md` there resolves under /data, not under the
            # resident tree, so it must not be claimed.
            {"tool_name": "Write", "cwd": "/data/engagements/" + "a" * 32,
             "tool_input": {"file_path": "prompts/system.md"}},
        ]
        # The same relative spellings a resident uses for the files it MAY
        # edit, crossed over every write primitive — the mirror of the
        # positive cross product, so a guard that over-claims by matching on
        # the basename from the agent home is caught here rather than in
        # production.
        for tool, arg in FILE_PRIMITIVES:
            for rel in ("prompts/morning-briefing.md", "./prompts/nightly.md",
                        "prompts/../prompts/weekly.md"):
                allowed.append({"tool_name": tool,
                                "cwd": "/config/agents/assistant",
                                "tool_input": {arg: rel}})
            # A SPECIALIST's own prompt file, across the same transformations:
            # managed state, denied by managed_component_guard in its own
            # words, and deliberately not claimed here.
            for raw in ("/config/agents/specialists/x/prompts/system.md",
                        "/config/agents/specialists/./x/prompts/system.md",
                        "specialists/x/prompts/system.md"):
                allowed.append({"tool_name": tool, "cwd": "/config/agents",
                                "tool_input": {arg: raw}})

        matcher = _registry_matcher() or None

        async def run() -> int:
            denied = 0
            for payload in allowed:
                if await _denies(cbs, payload, matcher):
                    denied += 1
            return denied

        assert asyncio.run(run()) == 0


# ---------------------------------------------------------------------------
# Wiring. The guard must be CODE-MANDATORY on every session Casa builds hooks
# for. `hooks_file:` / `hooks.yaml` is a config-editable pointer, so a
# yaml-only policy can be shed by an edit the configurator is entitled to make.


# `_build_specialist_options` appends only the agent-home settings guard, and
# its own comment records that `delegate_to_agent` routes RESIDENTS through it.
# It is also why the wiring must be TIER-AWARE — the coarse Bash halves of the
# neighbouring guards match a bare basename anywhere in a command, so an actual
# specialist that inherited them would start being refused for writing its own
# engagement artifacts.
# ---------------------------------------------------------------------------

_RESIDENT_PROMPT_WRITE = {
    "tool_name": "Write", "cwd": "/config",
    "tool_input": {"file_path": "/config/agents/assistant/prompts/system.md"},
}


async def _stack_denies(opts) -> bool:
    """True iff a PreToolUse callback in built options refuses the write AS
    THIS GUARD.

    The reason string is matched, not merely the decision, for a measured
    reason: a hand-built cfg whose `hooks.pre_tool_use` is empty resolves to a
    deny-everything `path_scope` (empty writable prefixes), so a bare
    decision check reported the specialist stack as refusing the write on the
    PRE-FIX tree — a false green for the wiring this test exists to prove.
    Same discrimination `test_hooks_managed_component_guard` makes.
    """
    for matcher in (opts.hooks or {}).get("PreToolUse", []):
        # ROUTE first. A callback registered under a matcher that does not
        # route `Write` never sees this payload in production, and the
        # acceptor demonstrated that invoking it anyway produces a green
        # count for a guard that could never fire.
        if not _routes(getattr(matcher, "matcher", None),
                       _RESIDENT_PROMPT_WRITE["tool_name"]):
            continue
        for cb in matcher.hooks:
            out = await cb(dict(_RESIDENT_PROMPT_WRITE), None, {})
            if not out or _decision(out) != "deny":
                continue
            reason = out["hookSpecificOutput"].get(
                "permissionDecisionReason", "")
            if GUARD in reason:
                return True
    return False


def _executor_defn(hooks_document: dict | None = None):
    return SimpleNamespace(
        hooks_path=None, hooks_document=hooks_document or {},
        mcp_server_names=[], tools_allowed=["Read"],
        model="sonnet", permission_mode="acceptEdits",
        tools_disallowed=[], driver="in_casa",
    )


class _SpecialistCfg:
    model = "claude-sonnet-4-6"
    system_prompt = "P\n"
    cwd = ""
    hooks = type("H", (), {"pre_tool_use": []})()
    tools = type("T", (), {"allowed": [], "disallowed": [],
                           "permission_mode": "dontAsk", "max_turns": 8,
                           "skills": "none"})()
    mcp_server_names = []
    compiled_prompt_bundle = None

    def __init__(self, role: str):
        self.role = role


class TestTheGuardIsCodeMandatoryEverywhereCasaBuildsHooks:

    def test_four_builder_paths_deny_or_abstain_exactly(self, tmp_path,
                                                        monkeypatch):
        """(executor, resident, delegated-resident, specialist) denials
        == (1, 1, 1, 0).

        Pre-fix all four are 0: no builder appends the guard, so nothing in any
        stack refuses. The fourth entry is 0 BY DESIGN, not by omission — an
        actual specialist keeps today's settings-guard-only stack.
        """
        import tools as tools_mod
        from test_agent_plugin_binding import _make_agent

        async def run() -> tuple[int, int, int, int]:
            ex = tools_mod._build_executor_options(
                _executor_defn(), executor_type="configurator",
                plugin_paths=[])
            agent = _make_agent(tmp_path, role="assistant")
            res_opts = await agent._build_options(
                channel="telegram", channel_key="k", is_fresh=True,
                resume_sid=None, user_text="hi")

            monkeypatch.setattr(
                tools_mod, "_agent_registry",
                SimpleNamespace(tier_for_role=lambda r: (
                    "resident" if r == "assistant" else "specialist")),
                raising=False)
            deleg_resident = tools_mod._build_specialist_options(
                _SpecialistCfg("assistant"))
            specialist = tools_mod._build_specialist_options(
                _SpecialistCfg("finance"))

            return (
                int(await _stack_denies(ex)),
                int(await _stack_denies(res_opts)),
                int(await _stack_denies(deleg_resident)),
                int(await _stack_denies(specialist)),
            )

        assert asyncio.run(run()) == (1, 1, 1, 0)

    def test_the_claude_code_transport_carries_it_on_both_halves(self):
        """(settings_proxy_entries, resolved_callbacks, http_denials)
        == (1, 1, 128).

        Each of the first two counts requires the emitted/resolved matcher to
        ROUTE every file primitive AND NOT route `Bash` — not merely to exist.
        128 = 4 file primitives x 32 generated spellings for one resident.

        Both halves are required and neither is sufficient:
        `translate_hooks_to_settings` emits the settings.json entry that makes
        Claude Code invoke `hook_proxy.sh <policy>`, and
        `build_policy_callbacks_from_hooks_yaml` is what resolves that policy
        NAME to a callback server-side. An entry for a policy the resolver does
        not know resolves to nothing.

        Why the transport is wired at all, given `path_scope`: `path_scope`'s
        PRESENCE is load-enforced on a claude_code executor, but its
        `writable:` prefixes are declared by that executor's own hooks.yaml —
        an executor whose declaration admits `/config/agents` would be refused
        by nothing else, and the refusal here names the corrective recipe where
        a scope denial cannot.

        At this file's parent the tuple is (0, 0, 128): both halves still
        route `Bash`, so neither counts. At the base it is (0, 0, 0).
        """
        from drivers.hook_bridge import translate_hooks_to_settings
        from hooks import build_policy_callbacks_from_hooks_yaml

        pd_hooks = yaml.safe_load(Path(
            "casa/rootfs/opt/casa/defaults/agents/executors/plugin-developer"
            "/hooks.yaml").read_text(encoding="utf-8")) or {}

        def _routes_exactly_the_file_tools(matcher) -> bool:
            return (all(_routes(matcher, p) for p in PRIMITIVES)
                    and not _routes(matcher, BASH))

        settings = translate_hooks_to_settings(
            pd_hooks, proxy_script_path="/proxy")
        settings_proxy_entries = sum(
            1
            for entry in settings["hooks"].get("PreToolUse", [])
            for hook in entry.get("hooks", [])
            if hook.get("command") == f"/proxy {GUARD}"
            and _routes_exactly_the_file_tools(entry.get("matcher"))
        )

        resolved = build_policy_callbacks_from_hooks_yaml(pd_hooks)
        resolved_matcher = resolved[GUARD][0] if GUARD in resolved else None
        resolved_callbacks = (
            1 if GUARD in resolved
            and _routes_exactly_the_file_tools(resolved_matcher) else 0
        )

        async def run() -> int:
            if GUARD not in resolved:
                return 0
            _matcher, cb = resolved[GUARD]
            denied = 0
            for payload in _calls_for("assistant"):
                if not _routes(_matcher, payload["tool_name"]):
                    continue
                out = await cb(dict(payload), None, {})
                if out and _decision(out) == "deny":
                    denied += 1
            return denied

        assert (settings_proxy_entries, resolved_callbacks,
                asyncio.run(run())) == (1, 1, PER_RESIDENT_CALLS)
        assert PER_RESIDENT_CALLS == 128



class TestTheFileIsReadButNotServed:
    """The premise INV-PERS-012's first clause rests on, pinned in both halves.

    Green at the base — deliberately. INV-PERS-012's RED half is the unrefused
    write above; this is the reason the refusal is honest rather than merely
    restrictive. Clause 1 says the file "is read only into the composed
    fallback prompt and is not served when its compiled bundle is active", and
    that is TWO facts: the loader really does open the file (an earlier
    wording, "is not read", was false — `_resolve_prose` opens it
    unconditionally and `LoadError`s if it is missing), and the bytes it reads
    reach no served projection. Each half has its own count.
    """

    def test_the_loader_opens_the_file_into_the_composed_prompt(
        self, tmp_path, monkeypatch,
    ):
        """(open_calls, returned_marker_count) == (1, 1).

        Mutation: a loader that stopped reading the file (returning "" for a
        `prompt_file:` pointer) gives (0, 0); one that read it twice gives
        (2, 1).
        """
        from agent_loader import _resolve_prose

        marker = "ZZ-RESIDENT-PROMPT-FILE-MARKER-ZZ"
        agent_dir = tmp_path / "assistant"
        (agent_dir / "prompts").mkdir(parents=True)
        (agent_dir / "prompts" / "system.md").write_text(
            f"You are helpful. {marker}\n", encoding="utf-8")

        real_open = builtins.open
        opened: list[str] = []

        def counting_open(file, *args, **kwargs):
            if str(file).endswith("prompts/system.md"):
                opened.append(str(file))
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", counting_open)
        prose = _resolve_prose(
            {"prompt_file": "prompts/system.md"}, field="prompt",
            agent_dir=str(agent_dir), source_label="character.yaml",
        )
        assert (len(opened), prose.count(marker)) == (1, 1)

    def test_no_served_projection_carries_the_composed_prompt(self, tmp_path):
        """served_marker_count == 0 over the three surfaces.

        Mutation: return `cfg.system_prompt` instead of the bundle projection
        and the marker count becomes 3.
        """
        marker = "ZZ-COMPOSED-PROMPT-MARKER-ZZ"
        from prompt_compiler import CompiledProjection, CompiledPromptBundle
        from test_agent_plugin_binding import _make_agent

        agent = _make_agent(tmp_path, role="assistant")
        agent.config.system_prompt = f"You are helpful. {marker}\n"
        agent.config.kind = "resident"
        agent.config.compiled_prompt_bundle = CompiledPromptBundle(
            role_id="resident:assistant", resolved_model="claude-sonnet-4-6",
            text=CompiledProjection(system_prompt="COMPILED TEXT\n",
                                    digest="sha256:" + "0" * 64,
                                    estimated_tokens=10),
            voice=CompiledProjection(system_prompt="COMPILED VOICE\n",
                                     digest="sha256:" + "1" * 64,
                                     estimated_tokens=10),
            restricted_webhook=CompiledProjection(
                system_prompt="COMPILED RW\n",
                digest="sha256:" + "2" * 64, estimated_tokens=5),
            binding_digest="sha256:" + "3" * 64,
        )

        from agent import origin_var

        async def run() -> int:
            total = 0
            for channel, route in (("telegram", None), ("voice", None),
                                   ("webhook", "webhook_trigger")):
                token = origin_var.set({"_origin_route": route})
                try:
                    opts = await agent._build_options(
                        channel=channel, channel_key="k", is_fresh=True,
                        resume_sid=None, user_text="hi")
                finally:
                    origin_var.reset(token)
                total += (opts.system_prompt or "").count(marker)
            return total

        assert asyncio.run(run()) == 0
