"""#610: a persona-bound resident's `response_shape.yaml` reaches nothing, so
writing it is refused rather than committed and reported live.

The reported defect: the configurator edits `agents/<role>/response_shape.yaml`,
commits it, and tells the operator the change "is already live for the agent's
next turn" — correctly, per its own recipe. For a persona-bound resident the
file is not read on any turn. The `static_prompt_digest` is byte-identical
before and after, and the next reply is often shorter by chance, so nothing
surfaces the drop.

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
    pinned, not assumed."""

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
