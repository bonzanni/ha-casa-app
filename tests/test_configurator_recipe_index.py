"""The configurator prompt's recipe index must stay in lockstep with the
doctrine/recipes tree.

The index exists because recipe discovery used to be left entirely to the model
(observed failure: an explicit install-from-repo ask was handled via the retired
hand-authoring recipe). A stale index would recreate that failure mode with the
router itself pointing at the wrong doors, so drift is a test failure, not a
docs nit.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGURATOR = (
    REPO_ROOT
    / "casa/rootfs/opt/casa/defaults/agents/executors/configurator"
)
RECIPES_DIR = CONFIGURATOR / "doctrine/recipes"
PROMPT = CONFIGURATOR / "prompt.md"

RETIRED = {
    "specialist/create",
    "specialist/update",
    "specialist/delete",
    "resident/create",
    "resident/delete",
}


def _recipe_slugs_on_disk() -> set[str]:
    return {
        str(p.relative_to(RECIPES_DIR).with_suffix("")).replace("\\", "/")
        for p in RECIPES_DIR.rglob("*.md")
    }


def _index_section() -> str:
    text = PROMPT.read_text(encoding="utf-8")
    m = re.search(r"^## Recipe index.*?(?=^## |\Z)", text, re.M | re.S)
    assert m, "prompt.md lost its '## Recipe index' section"
    return m.group(0)


def _slugs_in_index() -> set[str]:
    slugs = set()
    for line in _index_section().splitlines():
        if not line.startswith("- "):
            continue
        # Backticked tokens with exactly one slash are recipe refs; prose
        # backticks (tool names, file names with dots) never match this shape.
        slugs.update(re.findall(r"`([a-z0-9-]+/[a-z0-9_-]+)`", line))
    return slugs


def test_every_recipe_on_disk_is_indexed():
    missing = _recipe_slugs_on_disk() - _slugs_in_index()
    assert not missing, f"recipes missing from the prompt.md index: {sorted(missing)}"


def test_every_indexed_recipe_exists_on_disk():
    phantom = _slugs_in_index() - _recipe_slugs_on_disk()
    assert not phantom, f"prompt.md index names nonexistent recipes: {sorted(phantom)}"


def test_index_carries_the_mandatory_rule():
    section = _index_section()
    assert "MANDATORY" in section
    assert "forbidden" in section


def test_retired_recipes_are_stubs_that_say_so():
    for slug in RETIRED:
        text = (RECIPES_DIR / f"{slug}.md").read_text(encoding="utf-8")
        first_line = text.splitlines()[0]
        assert "RETIRED" in first_line, f"{slug}.md first line must mark it RETIRED"
        # A stub must redirect somewhere real.
        refs = re.findall(r"recipes/([a-z0-9-]+/[a-z0-9_-]+)\.md", text)
        assert refs, f"{slug}.md redirects nowhere"


def test_cross_recipe_references_resolve():
    on_disk = _recipe_slugs_on_disk()
    for p in RECIPES_DIR.rglob("*.md"):
        for ref in re.findall(r"recipes/([a-z0-9-]+/[a-z0-9_-]+)\.md", p.read_text(encoding="utf-8")):
            assert ref in on_disk, f"{p.relative_to(RECIPES_DIR)} references missing recipe {ref}.md"


def test_lifecycle_recipes_order_commit_before_reload_before_emit():
    """completion.md's canonical order is commit -> reload -> emit_completion.
    Round-1 found the inversion in 4 recipes and round-3 found a 5th
    (uninstall.md) — so pin it mechanically for EVERY recipe that stages all
    three calls as numbered steps."""
    for p in RECIPES_DIR.rglob("*.md"):
        text = p.read_text(encoding="utf-8")
        positions = {}
        for marker in ("config_git_commit", "casa_reload", "emit_completion"):
            # First numbered step naming the call. m.end() (not the line
            # start) so three calls staged in-order on ONE step line — like
            # upgrade.md step 6 — compare correctly.
            m = re.search(rf"^\s*\d+\.\s[^\n]*?{marker}", text, re.M)
            if m:
                positions[marker] = m.end()
        if len(positions) == 3:
            assert (positions["config_git_commit"]
                    < positions["casa_reload"]
                    < positions["emit_completion"]), (
                f"{p.relative_to(RECIPES_DIR)} violates the canonical "
                "commit -> reload -> emit_completion order")


def test_specialist_recipes_reload_the_resident_whose_delegates_they_edit():
    """#1009: the install recipe wires the specialist into a resident's
    `delegates.yaml` (step 6) and used to finish with `scope="agents"` alone.
    That sweep adds and evicts roles; it never re-reads a live resident, so
    the resident kept its boot-time delegate list and refused the new
    specialist with `delegation_not_declared`, while the completion text
    claimed "wired for delegation". Pin, for install AND uninstall, that the
    per-role reload is staged AFTER the agents sweep and BEFORE
    emit_completion, and that the install's completion text no longer claims
    a wiring the sequence did not perform."""
    for name in ("specialist/install.md", "specialist/uninstall.md"):
        text = (RECIPES_DIR / name).read_text(encoding="utf-8")
        agents = re.search(r'casa_reload\(scope="agents"\)', text)
        per_role = re.search(
            r'casa_reload\(scope="agent",\s*role="<resident>"\)', text)
        emit = re.search(r"^\s*\d+\.\s[^\n]*?emit_completion", text, re.M)
        assert agents and per_role and emit, name
        assert agents.start() < per_role.start() < emit.start(), (
            f"{name}: the per-role reload must follow the agents sweep and "
            "precede emit_completion")
        # The per-role reload is a numbered STEP's instruction, not a
        # common-mistakes aside: it must sit on the same step as the sweep.
        step = re.search(
            r'^\s*\d+\.\s[^\n]*?casa_reload\(scope="agents"\)(?:.|\n)*?'
            r'casa_reload\(scope="agent",\s*role="<resident>"\)', text, re.M)
        assert step is not None, name
        assert "emit_completion" not in step.group(0), name
    # Whitespace-normalized: the shipped phrase wrapped across two lines, and
    # an un-normalized check passed against it.
    install = " ".join((RECIPES_DIR / "specialist/install.md")
                       .read_text(encoding="utf-8").split())
    assert "reloaded and wired for delegation" not in install


def test_the_topic_is_told_in_plain_words_not_recipe_steps():
    """casa-test, a Fido upgrade: the configurator wrote "Per recipe step 6: commit, then
    `casa_reload(scope="agents")`" to the operator's topic. The prompt's output discipline,
    which the configurator reads on every engagement, says the topic is the operator's and
    names what never goes in it."""
    text = PROMPT.read_text(encoding="utf-8")
    m = re.search(r"^## Output discipline.*?(?=^## |\Z)", text, re.M | re.S)
    assert m is not None
    section = " ".join(m.group(0).split())
    assert section.count(
        "The operator reads everything you write in this topic. Tell him what is happening in "
        "plain words: what you found, what you changed, what you need from him.") == 1
    assert "never cite them (\"per recipe step 6\"), and never write a tool name or a call " \
           "such as `casa_reload(...)` in the topic." in section
    assert "A result a recipe tells you to relay verbatim is still relayed as written." in section


def _output_discipline() -> str:
    text = PROMPT.read_text(encoding="utf-8")
    m = re.search(r"^## Output discipline.*?(?=^## |\Z)", text, re.M | re.S)
    assert m is not None
    return " ".join(m.group(0).split())


def test_the_topic_is_not_told_each_step_as_it_runs():
    """casa-test, a Fido upgrade: the topic got "Now let me read the specialist/upgrade
    recipe." and "Committed and reloaded. Finalizing with completion summary." A prompt
    line asking not to narrate did not hold for the text written before each tool call,
    so the driver now posts only a turn's closing text; the prompt says so and keeps
    the one instruction that matters to the operator — end the turn with the question."""
    section = _output_discipline()
    assert "One or two sentences of status update, then the tool call." not in section
    assert "Do not narrate your steps" not in section
    assert section.count(
        "Only the text you write after your last tool call in a turn reaches him; what "
        "you write before or between tool calls is not shown, and the topic already shows "
        "your steps. So end your turn with any question you have for him, and put there "
        "everything he should see: something you found that he should know, or a result "
        "you are told to relay. Be terse.") == 1


def test_the_completion_text_is_written_for_the_operator():
    """The same Fido upgrade closed its topic with "specialist_upgrade returned
    state=active ... Committed as SHA 9a44ae47..., then soft-reloaded scope=agents" — the
    completion text the prompt and completion.md had asked to carry the SHA and the
    reload."""
    section = _output_discipline()
    assert "what commit SHA, what reload was triggered" not in section
    assert section.count(
        "Write your emit_completion `text` for him too, in plain words: what you changed, "
        "whether you made it take effect, and anything he still has to do. The commit SHA "
        "and the files changed go in `artifacts`; the text names no SHA, tool, result field "
        "or reload scope.") == 1
    completion = " ".join(
        (CONFIGURATOR / "doctrine/completion.md").read_text(encoding="utf-8").split())
    assert "Committed SHA abc123" not in completion
    assert "`text` is what Ellen READS" not in completion
    assert ("Write `text` for the operator. Make it: - Factually complete. - Plain words: "
            "what you changed, whether you made it take effect") in completion
    assert ("no reload scopes — they are how you work, not news for him. The commit SHA "
            "and the files changed go in `artifacts` above.") in completion


def test_no_example_completion_text_carries_the_commit_or_the_reload():
    """Every recipe's example ``emit_completion(... text="...")`` is a model the
    configurator copies; the ones that said "committed SHA <sha>, called
    casa_reload(scope='agent')" put exactly that in the operator's topic."""
    docs = [CONFIGURATOR / "doctrine/completion.md", *sorted(RECIPES_DIR.rglob("*.md"))]
    examples = []
    for path in docs:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'emit_completion\([^)]*?text="([^"]*)"', text, re.S):
            examples.append((path.name, " ".join(m.group(1).split())))
    # The recipes that model a finished change all carry one (and completion.md's
    # payload skeleton), so the scan cannot pass by matching nothing.
    assert len(examples) >= 10, examples
    for name, example in examples:
        # <ref> may be a commit sha the operator named (specialist/install.md).
        for machinery in ("SHA", "sha>", "<ref>", "casa_reload", "reloaded triggers",
                          "scope="):
            assert machinery not in example, (name, example)
