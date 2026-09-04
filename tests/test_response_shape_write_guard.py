"""#610: a persona-bound resident's `response_shape.yaml` reaches nothing, so
writing it is refused rather than committed and reported live.

The reported defect: the configurator edits `agents/<role>/response_shape.yaml`,
commits it, and tells the operator the change "is already live for the agent's
next turn" — correctly, per its own recipe. For a persona-bound resident the
file is read on every load and served on no turn. The `static_prompt_digest`
is byte-identical before and after, and the next reply is often shorter by
chance, so nothing surfaces the drop.

Why the file is dead for a resident: `agent.py` uses the compiled bundle's
projection as the base prompt whenever a bundle exists (INV-PERS-001), and all
three resident role artifacts carry `persona.policy: required`, so a resident is
bundle-bound from first boot. `response_shape.yaml` renders only into
`_compose_prompt`, whose output is `cfg.system_prompt` — the no-bundle fallback.
#549 made the ROLE ARTIFACT's `response:` block the live source; it did not
retire this file, and the recipe still pointed at it.

Scope, deliberately one directory depth (mirroring `_is_resident_trigger_file`):

* `agents/specialists/**` is NOT claimed — that subtree is managed state and
  `managed_component_guard` already denies it, in its own words. Two guards on
  one path and neither on the reason is the failure mode the trigger guard
  documents avoiding.
* executors are not claimed either: `agent_loader.TIER_FILES` FORBIDS
  `response_shape.yaml` for an executor, so there is no such file to protect.
"""
from __future__ import annotations

import pytest

CTX: dict = {}


def _decision(out):
    return out.get("hookSpecificOutput", {}).get("permissionDecision")


@pytest.mark.asyncio
class TestResponseShapeWriteGuard:
    def _guard(self):
        from hooks import make_response_shape_write_guard
        return make_response_shape_write_guard()

    async def _run(self, tool_name, tool_input, cwd="/config"):
        return await self._guard()({"tool_name": tool_name, "cwd": cwd,
                                    "tool_input": tool_input}, "tid", CTX)

    @pytest.mark.parametrize("tool,key", [
        ("Write", "file_path"), ("Edit", "file_path"),
        ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path"),
    ])
    @pytest.mark.parametrize("slot", ["assistant", "butler", "concierge"])
    async def test_denies_every_write_primitive_on_every_resident(self, tool, key, slot):
        """All four write-capable primitives: a matcher routing only Write|Edit
        lets MultiEdit/NotebookEdit through, which two earlier guards learned
        the hard way."""
        out = await self._run(tool, {key: f"/config/agents/{slot}/response_shape.yaml"})
        assert _decision(out) == "deny"

    async def test_the_denial_names_the_path_that_is_actually_live(self):
        """A refusal that does not say where to go instead just moves the dead
        end. The operator's ask ("be briefer") has a real home."""
        out = await self._run("Edit", {
            "file_path": "/config/agents/assistant/response_shape.yaml"})
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "persona" in reason.lower()

    async def test_denies_the_relative_spelling(self):
        out = await self._run("Edit", {"file_path": "agents/butler/response_shape.yaml"})
        assert _decision(out) == "deny"

    async def test_denies_a_traversal_spelling(self):
        out = await self._run("Write", {
            "file_path": "/config/policies/../agents/butler/response_shape.yaml"})
        assert _decision(out) == "deny"

    async def test_denies_a_relative_path_with_no_reported_cwd(self):
        """`cwd` is a required SDK hook-input field; without it a relative path
        cannot be resolved at all, and a /config fallback would let a
        resident's `../../agents/<role>/...` resolve to nothing and read as
        allowed. Fail closed."""
        guard = self._guard()
        out = await guard({"tool_name": "Edit",
                           "tool_input": {"file_path": "agents/butler/response_shape.yaml"}},
                          "tid", CTX)
        assert _decision(out) == "deny"

    async def test_allows_reading_it(self):
        """An agent must still be able to see the file to explain why it is
        inert — the recipe now tells it to say so."""
        assert await self._run(
            "Read", {"file_path": "/config/agents/butler/response_shape.yaml"}) == {}

    async def test_allows_the_other_files_in_the_same_directory(self):
        """The guard is about ONE file. Denying its neighbours would break
        every other configurator recipe in that directory."""
        for name in ("character.yaml", "voice.yaml", "runtime.yaml",
                     "delegates.yaml", "disclosure.yaml"):
            assert await self._run(
                "Edit", {"file_path": f"/config/agents/butler/{name}"}) == {}

    async def test_does_not_claim_the_specialists_subtree(self):
        """Managed state, already denied by managed_component_guard, which
        routes to the specialist pipeline rather than to the persona
        lifecycle. One guard per path, each in its own words."""
        assert await self._run("Edit", {
            "file_path": "/config/agents/specialists/mtg/response_shape.yaml"}) == {}

    async def test_does_not_claim_an_executor_path(self):
        """`TIER_FILES["executor"]` FORBIDS this file, so an executor has none
        to protect and a claim here would be a guard over an empty set."""
        assert await self._run("Edit", {
            "file_path": "/config/agents/executors/configurator/response_shape.yaml"}) == {}

    async def test_denies_a_bash_write_form(self):
        """The half that round 1 of the design review argued me into.

        `sed -i` on this file is committed, reported live, and inert — the
        exact reported defect — and `path_scope` does not match Bash at all.
        """
        out = await self._run("Bash", {
            "command": "sed -i s/2/1/ /config/agents/assistant/response_shape.yaml"})
        assert _decision(out) == "deny"

    async def test_denies_a_bash_redirect_form(self):
        out = await self._run("Bash", {
            "command": "printf 'x' > /config/agents/assistant/response_shape.yaml"})
        assert _decision(out) == "deny"

    async def test_denies_a_quote_spliced_bash_form(self):
        """`response_shape"."yaml` is one word to bash and a different word to a
        naive parser; the classifier strips shell quoting from the whole
        command first, so every splice form reduces to one case."""
        out = await self._run("Bash", {
            "command": 'printf x > /config/agents/assistant/response_shape"."yaml'})
        assert _decision(out) == "deny"

    async def test_allows_a_bash_read_form(self):
        assert await self._run("Bash", {
            "command": "cat /config/agents/assistant/response_shape.yaml"}) == {}


class TestResponseShapeGuardIsWired:
    def test_registered_in_hook_policies_for_every_write_primitive(self):
        """A guard nothing injects is a guard that never runs."""
        from hooks import HOOK_POLICIES

        policy = HOOK_POLICIES["response_shape_write_guard"]
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"):
            assert tool in policy["matcher"]

    def test_matcher_factory_builds(self):
        from hooks import response_shape_write_guard_matcher

        assert response_shape_write_guard_matcher() is not None

    def test_factory_refuses_unknown_parameters(self):
        """Config-declared params are a typo surface; an unknown one must fail
        loudly rather than silently produce an unconfigured guard."""
        from hooks import HOOK_POLICIES

        with pytest.raises(Exception):
            HOOK_POLICIES["response_shape_write_guard"]["factory"](nonsense=True)

    @pytest.mark.parametrize("module,needle", [
        # The executor session builder (tools.py) and the resident one
        # (agent.py). Injected CODE-SIDE in both: a yaml-only policy can be
        # shed by rewriting definition.yaml's `hooks_file:` pointer, which the
        # configurator is otherwise entitled to do.
        ("casa/rootfs/opt/casa/tools.py", "response_shape_write_guard_matcher()"),
        ("casa/rootfs/opt/casa/agent.py", "response_shape_write_guard_matcher()"),
    ])
    def test_injected_code_side_in_both_session_builders(self, module, needle):
        """A guard nothing injects never runs. Asserted against the source
        because both builders are async and require a live runtime to call."""
        import pathlib

        assert needle in pathlib.Path(module).read_text(), (
            f"{module} must inject the response-shape guard")


class TestTheFileIsActuallyDead:
    """The premise the guard rests on. If a resident's response_shape.yaml ever
    became live again, this guard would be actively harmful — so the premise is
    pinned, not assumed.

    "Dead" here means UNSERVED, not unread: the loader reads and renders the
    file on every load, into a composed prompt no bundle-bound resident is
    served. INV-PERS-017 states both halves, and
    `TestTheFileIsReadRenderedButNotServed` counts them.
    """

    def test_every_resident_role_requires_a_persona(self):
        """Bundle-bound from first boot is what makes the file unreachable;
        `persona.policy: required` on all three is why."""
        import pathlib

        import yaml

        roles = pathlib.Path("casa/rootfs/opt/casa/defaults/roles/resident")
        slots = sorted(p.name for p in roles.iterdir() if p.is_dir())
        assert slots == ["assistant", "butler", "concierge"]
        for slot in slots:
            doc = yaml.safe_load((roles / slot / "role.yaml").read_text())
            assert doc["persona"]["policy"] == "required", slot

    def test_executors_are_forbidden_the_file(self):
        from agent_loader import TIER_FILES

        assert "response_shape.yaml" in TIER_FILES["executor"]["forbidden"]
        assert "response_shape.yaml" in TIER_FILES["resident"]["required"]


# --- The read/render/not-served pair, and the corpus statement that names it ---
#
# Node ids here are UNPARAMETRISED on purpose: they are bound from
# `docs/manifest.d/architecture-n-r.yaml`, and a bracketed id does not resolve.

_DEFAULT_AGENTS = "casa/rootfs/opt/casa/defaults/agents"
_DISCLOSURE = "casa/rootfs/opt/casa/defaults/policies/disclosure.yaml"
_RESIDENTS = ("assistant", "butler", "concierge")
_GUARD_DOC = "docs/architecture/prompt-file-guards.md"

# The settled statement of the invariant this class pins. Byte-for-byte what
# `docs/architecture/prompt-file-guards.md` declares.
_EXPECTED_STATEMENT = (
    "A persona-bound resident's per-agent `response_shape.yaml` is read and "
    "rendered into the composed fallback prompt, and no part of it is served "
    "while its compiled bundle is active; and an agent's write to a "
    "resident's copy through `Write`, `Edit`, `MultiEdit` or `NotebookEdit` "
    "is refused on the executor, resident and delegated-resident hook paths."
)

def _declared_invariants(text):
    import re

    pattern = re.compile(r"^\*\*(?P<id>INV-[A-Z]+-\d+)\*\*: (?P<statement>.+)$")
    out = []
    for line in text.splitlines():
        m = pattern.fullmatch(line)
        if m:
            out.append((m["id"], m["statement"]))
    return out


def _statements_about_the_file(declared):
    """Select by the grammatical SUBJECT, never by id.

    Selecting by id would make this test fail at the base because
    `INV-PERS-017` is absent, not because the published statement contradicts
    the loader — a different failure wearing the same red. The subject regex
    admits the retired wording ("agent's") and the corrected one
    ("resident's") alike, and excludes INV-PERS-012, whose subject is
    `prompts/system.md` even though its statement also names the composed
    fallback.
    """
    import re

    subject = re.compile(
        r"^A persona-bound (?:agent|resident)'s per-agent "
        r"`response_shape\.yaml`(?:\s|$)")
    return [(i, s) for i, s in declared if subject.match(s)]


def _shape_doc(register="written", format_="plain", confirmation=2,
               status=4, rules=()):
    lines = ["schema_version: 1",
             f"max_sentences_confirmation: {confirmation}",
             f"max_sentences_status: {status}",
             f"register: {register}",
             f"format: {format_}"]
    if rules:
        lines.append("rules:")
        lines.extend(f'  - "{r}"' for r in rules)
    return "\n".join(lines) + "\n"


def _copy_residents(root, shape_for):
    """Copy the three shipped resident directories, rewriting each copy's
    `response_shape.yaml` with `shape_for(role)`."""
    import pathlib
    import shutil

    roots = {}
    for role in _RESIDENTS:
        dst = pathlib.Path(root) / role
        shutil.copytree(pathlib.Path(_DEFAULT_AGENTS) / role, dst)
        (dst / "response_shape.yaml").write_text(shape_for(role),
                                                 encoding="utf-8")
        roots[role] = dst
    return roots


def _load_residents(roots, bindings_dir, monkeypatch=None):
    """Real `load_agent_from_dir` on each copy. Returns (cfgs, opened).

    `opened` counts opens of exactly the three copied `response_shape.yaml`
    paths and is empty unless a monkeypatch is supplied. `binding_commit=False`
    keeps the reconciliation in memory — the loader still compiles and attaches
    the bundle, and nothing is written back to the copies.
    """
    import builtins
    import os
    import pathlib

    from agent_loader import load_agent_from_dir
    from policies import load_policies

    targets = {str((p / "response_shape.yaml").resolve()) for p in roots.values()}
    opened = []
    if monkeypatch is not None:
        real_open = builtins.open

        def counting_open(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)):
                try:
                    resolved = str(pathlib.Path(file).resolve())
                except OSError:
                    resolved = None
                if resolved in targets:
                    opened.append(resolved)
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", counting_open)

    policies = load_policies(_DISCLOSURE)
    cfgs = {
        role: load_agent_from_dir(
            str(path), policies=policies,
            bindings_dir=str(bindings_dir), binding_commit=False)
        for role, path in roots.items()
    }
    if monkeypatch is not None:
        monkeypatch.undo()
    return cfgs, opened


# The four served surfaces, keyed by route as well as channel: the two webhook
# routes take DIFFERENT arms — `webhook_trigger` the restricted one at
# `agent.py:1996`, `invoke` the ordinary projection at `:2144` — so keying by
# channel alone would let one overwrite the other and leave a route-specific
# regression green.
_SURFACES = (("telegram", None), ("voice", None),
             ("webhook", "webhook_trigger"), ("webhook", "invoke"))


def _served_prompts(cfgs, tmp_path):
    """`(role, channel, route) -> served system prompt`, over the four surfaces
    a resident is actually served: telegram (text), voice, an untrusted webhook
    origin (restricted) and a trusted `/invoke` one. Twelve in all."""
    import asyncio

    from agent import origin_var
    from test_agent_plugin_binding import _make_agent

    out = {}

    async def run():
        for role, cfg in cfgs.items():
            for channel, route in _SURFACES:
                seat = tmp_path / "seats" / f"{role}-{channel}-{route}"
                seat.mkdir(parents=True, exist_ok=True)
                agent = _make_agent(seat, role=role)
                agent.config = cfg
                token = origin_var.set({"_origin_route": route})
                try:
                    opts = await agent._build_options(
                        channel=channel, channel_key="k", is_fresh=False,
                        resume_sid=None, user_text="hi")
                finally:
                    origin_var.reset(token)
                out[(role, channel, route)] = opts.system_prompt or ""

    asyncio.run(run())
    return out


class TestTheFileIsReadRenderedButNotServed:
    """INV-PERS-017's first two clauses, and the published sentence that names
    them.

    The behavioural halves are green at the base by construction — the loader
    was always right; what was wrong was the corpus sentence describing it.
    They are MUTATION CHECKS, and each is mutated separately below. The RED
    case is the first method: it measures the loader and reads the published
    statement in one test, and fails while the two contradict each other.
    """

    def test_published_invariant_matches_the_measured_response_shape_path(
        self, tmp_path, monkeypatch,
    ):
        """RED at the base: `(1, 3, 3, 1)` against `(1, 3, 3, 0)`.

        The corpus declares one invariant whose subject is a persona-bound
        resident's `response_shape.yaml`; a real load of the three shipped
        residents opens all three copies and renders all three markers into
        the composed prompt; and the published statement claims the file "is
        not read". The fourth count is the contradiction, and it is the first
        assertion's only failing term at the base — the id assertions below it
        are never reached there.
        """
        import pathlib
        import re

        marker = "ZZ-RESPONSE-SHAPE-{}-ZZ"
        roots = _copy_residents(
            tmp_path / "agents",
            lambda role: _shape_doc(rules=(marker.format(role.upper()),)))
        cfgs, opened = _load_residents(
            roots, tmp_path / "bindings", monkeypatch=monkeypatch)

        rendered = sum(
            cfgs[role].system_prompt.count(marker.format(role.upper()))
            for role in _RESIDENTS)

        declared = _declared_invariants(
            pathlib.Path(_GUARD_DOC).read_text(encoding="utf-8"))
        matching = _statements_about_the_file(declared)
        denies_read = sum(
            1 for _, statement in matching
            if re.search(r"\bis\s+not\s+read\b", statement, re.IGNORECASE))

        assert (len(matching), len(opened), rendered, denies_read) == (1, 3, 3, 0)
        assert [i for i, _ in matching] == ["INV-PERS-017"]
        assert [s for _, s in matching] == [_EXPECTED_STATEMENT]

    def test_real_loader_renders_each_response_shape_field(
        self, tmp_path, monkeypatch,
    ):
        """Every one of the five rendered fields reaches the composed prompt,
        counted separately.

        A marker in `rules:` alone would leave `register`, `format` and the
        two sentence limits free to change behaviour untested. Mutation: drop
        any one field from `_render_response_shape_section` and that field's
        count falls to 0; skip the read and the open count falls to 0.
        """
        rule = "ZZ-RESPONSE-SHAPE-RULE-{}-ZZ"
        roots = _copy_residents(
            tmp_path / "agents",
            lambda role: _shape_doc(register="spoken", format_="markdown",
                                    confirmation=71, status=73,
                                    rules=(rule.format(role.upper()),)))
        cfgs, opened = _load_residents(
            roots, tmp_path / "bindings", monkeypatch=monkeypatch)

        sections = {}
        for role in _RESIDENTS:
            body = cfgs[role].system_prompt.split("### Response shape", 1)[1]
            sections[role] = body.split("\n###", 1)[0]

        def across(line_for):
            return sum(1 for role in _RESIDENTS
                       if line_for(role) in sections[role].splitlines())

        counts = (
            across(lambda r: "Register: spoken"),
            across(lambda r: "Format: markdown"),
            across(lambda r: "Max sentences (confirmation): 71"),
            across(lambda r: "Max sentences (status): 73"),
            across(lambda r: f"  - {rule.format(r.upper())}"),
        )
        assert (len(opened), *counts) == (3, 3, 3, 3, 3, 3)

    def test_each_response_shape_field_is_absent_from_all_served_projections(
        self, tmp_path,
    ):
        """No field of the file reaches any served projection — per field, by
        differential rather than by marker absence.

        `Register: spoken` legitimately occurs in a compiled projection, so a
        bare marker-absence check would false-positive. Instead each field is
        changed on its own and the twelve served prompts are compared
        byte-for-byte against the baseline's: three composed prompts differ,
        zero served prompts do. Mutation: serve `cfg.system_prompt` on the
        ordinary arm and the served difference count becomes 9 (telegram, voice
        and the trusted `/invoke` route); on the restricted-webhook arm, 3; on
        both, 12.
        """
        base_kwargs = dict(register="written", format_="plain",
                           confirmation=2, status=4,
                           rules=("baseline rule.",))
        mutations = {
            "register": dict(register="spoken"),
            "format": dict(format_="markdown"),
            "max_sentences_confirmation": dict(confirmation=12),
            "max_sentences_status": dict(status=14),
            "rules": dict(rules=("baseline rule.", "ZZ-EXTRA-RULE-ZZ")),
        }

        for field, override in mutations.items():
            arms = {}
            for arm, kwargs in (("base", base_kwargs),
                                ("mut", {**base_kwargs, **override})):
                root = tmp_path / field / arm
                roots = _copy_residents(root / "agents",
                                        lambda role, k=kwargs: _shape_doc(**k))
                cfgs, _ = _load_residents(roots, root / "bindings")
                arms[arm] = (cfgs, _served_prompts(cfgs, root))

            composed_differences = sum(
                1 for role in _RESIDENTS
                if arms["base"][0][role].system_prompt
                != arms["mut"][0][role].system_prompt)
            base_served, mut_served = arms["base"][1], arms["mut"][1]
            served_differences = sum(
                1 for key in base_served
                if base_served[key] != mut_served[key])

            assert (composed_differences, len(base_served), len(mut_served),
                    served_differences) == (3, 12, 12, 0), field


# --- the write clause, on each path Casa builds hooks for ------------------

_RESPONSE_SHAPE_WRITE = {
    "tool_name": "Write", "cwd": "/config",
    "tool_input": {"file_path": "/config/agents/assistant/response_shape.yaml"},
}


def _routes(matcher, tool_name):
    """Whether a matcher string would ROUTE this tool to its hook. A callback
    registered under `Read` never sees a `Write` in production, so routing is
    checked before the callback is invoked."""
    import re

    if matcher is None:
        return True
    return re.fullmatch(matcher, tool_name) is not None


async def _stack_denies_as_this_guard(opts):
    """True iff a PreToolUse callback in built options refuses the write AS
    THIS guard.

    The reason string is matched rather than the bare decision: a hand-built
    cfg with an empty `hooks.pre_tool_use` resolves to a deny-everything
    `path_scope`, which would report a stack carrying no response-shape guard
    as refusing.
    """
    for matcher in (opts.hooks or {}).get("PreToolUse", []):
        if not _routes(getattr(matcher, "matcher", None),
                       _RESPONSE_SHAPE_WRITE["tool_name"]):
            continue
        for cb in matcher.hooks:
            out = await cb(dict(_RESPONSE_SHAPE_WRITE), None, {})
            if not out or _decision(out) != "deny":
                continue
            if "response_shape_write_guard" in out["hookSpecificOutput"].get(
                    "permissionDecisionReason", ""):
                return True
    return False


class TestTheWriteClauseHoldsOnEveryPathItNames:
    """INV-PERS-017's second clause names three hook paths; this counts them.

    A statement that named every path Casa builds would be false — the
    `claude_code` transport's resolver deliberately omits this guard, because
    its Bash half matches a bare basename anywhere in a command and would
    refuse an executor writing its own `response_shape.yaml` under
    `/data/engagements`. That exclusion is a not-covered clause in the prose,
    not a count here: pinning a gap makes its eventual closure arrive as a
    broken invariant.
    """

    def test_three_builder_paths_deny_and_a_specialist_abstains(
        self, tmp_path, monkeypatch,
    ):
        """(executor, resident, delegated-resident, specialist) == (1, 1, 1, 0).

        The fourth is 0 BY DESIGN: an actual specialist writing its own
        engagement artifacts must not be refused by a basename match.
        Mutation: drop the guard from any one builder and that entry becomes 0.
        """
        import asyncio
        from types import SimpleNamespace

        import tools as tools_mod
        from test_agent_plugin_binding import _make_agent
        from test_resident_prompt_write_guard import (
            _SpecialistCfg, _executor_defn,
        )

        async def run():
            executor = tools_mod._build_executor_options(
                _executor_defn(), executor_type="configurator",
                plugin_paths=[])
            agent = _make_agent(tmp_path, role="assistant")
            resident = await agent._build_options(
                channel="telegram", channel_key="k", is_fresh=True,
                resume_sid=None, user_text="hi")

            monkeypatch.setattr(
                tools_mod, "_agent_registry",
                SimpleNamespace(tier_for_role=lambda r: (
                    "resident" if r == "assistant" else "specialist")),
                raising=False)
            delegated = tools_mod._build_specialist_options(
                _SpecialistCfg("assistant"))
            specialist = tools_mod._build_specialist_options(
                _SpecialistCfg("finance"))

            counts = []
            for opts in (executor, resident, delegated, specialist):
                counts.append(int(await _stack_denies_as_this_guard(opts)))
            return tuple(counts)

        assert asyncio.run(run()) == (1, 1, 1, 0)
