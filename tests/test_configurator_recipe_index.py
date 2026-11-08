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
    / "casa/rootfs/opt/casa/defaults/configurator"
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


def test_the_operator_is_told_in_plain_words_not_recipe_steps():
    """casa-test, a Fido upgrade: the configurator wrote "Per recipe step 6: commit, then
    `casa_reload(scope="agents")`" to the operator. The prompt's output discipline,
    which the configurator reads on every run, says the answer is the operator's and
    names what never goes in it."""
    section = _output_discipline()
    assert section.count(
        "Tell him what is happening in plain words: what you found, what you changed, what "
        "you need from him.") == 1
    assert "never cite them (\"per recipe step 6\"), and never write a tool name or a call " \
           "such as `casa_reload(...)` in your answer." in section
    assert "A result a recipe tells you to relay verbatim is still relayed as written." in section


def _output_discipline() -> str:
    text = PROMPT.read_text(encoding="utf-8")
    m = re.search(r"^## Output discipline.*?(?=^## |\Z)", text, re.M | re.S)
    assert m is not None
    return " ".join(m.group(0).split())


def test_the_operator_is_not_told_each_step_as_it_runs():
    """casa-test, a Fido upgrade: the operator got "Now let me read the specialist/upgrade
    recipe." and "Committed and reloaded. Finalizing with completion summary." A prompt
    line asking not to narrate did not hold for the text written before each tool call,
    so only a run's closing text reaches him; the prompt says so."""
    section = _output_discipline()
    assert "One or two sentences of status update, then the tool call." not in section
    assert "Do not narrate your steps" not in section
    assert section.count(
        "Only the text you write after your last tool call reaches him; what you write "
        "before or between tool calls is not shown. Put there everything he should see. "
        "Be terse.") == 1


def test_the_answer_is_written_for_the_operator():
    """The same Fido upgrade closed with "specialist_upgrade returned state=active ...
    Committed as SHA 9a44ae47..., then soft-reloaded scope=agents" — the text the prompt
    and completion.md had asked to carry the SHA and the reload."""
    section = _output_discipline()
    assert "what commit SHA, what reload was triggered" not in section
    assert section.count("Name no commit SHA, tool, result field or reload scope.") == 1
    completion = " ".join(
        (CONFIGURATOR / "doctrine/completion.md").read_text(encoding="utf-8").split())
    assert "Committed SHA abc123" not in completion
    assert "`text` is what Ellen READS" not in completion
    assert ("Write it for the operator. Make it: - Factually complete. - Plain words: "
            "what you found or changed, whether it is live") in completion
    assert ("Free of machinery: no commit SHA, no tool names, no result fields such as "
            "`state=active`, no reload scopes.") in completion


def test_an_information_task_completes_with_its_findings():
    """casa-test, 2026-11-04: "review Fido's configuration and its vault secrets and tell
    me what's wired; change nothing". The configurator reported its findings but ended
    with only a closing line, and that line was all Ellen received. A run's closing text
    is all that reaches the operator, so a task asking only for information ends with the
    findings themselves."""
    text = PROMPT.read_text(encoding="utf-8")
    m = re.search(r"^## How a run ends.*?(?=^## |\Z)", text, re.M | re.S)
    assert m is not None
    ending = " ".join(m.group(0).split())
    assert ending.count(
        "End every run with your answer or your question to the operator: that text, "
        "after your last tool call, is all that reaches him.") == 1
    completion = " ".join(
        (CONFIGURATOR / "doctrine/completion.md").read_text(encoding="utf-8").split())
    assert completion.count("## A task that only asks for information") == 1
    assert ("is finished once you have gathered the answer: there is nothing to commit, so "
            "step 1 is skipped. The answer carries the findings themselves, in plain "
            "words.") in completion
    # A findings report may need more room than a change's 2-5 sentences.
    assert "Terse. One paragraph, 2-5 sentences." not in completion
    assert ("Terse. A change takes one paragraph, 2-5 sentences; findings take the room "
            "they need and no more.") in completion


def test_no_example_answer_carries_the_commit_or_the_reload():
    """Every recipe's example answer (``e.g. "..."``) is a model the configurator
    copies; the ones that said "committed SHA <sha>, called casa_reload(scope='agent')"
    put exactly that in front of the operator."""
    docs = [CONFIGURATOR / "doctrine/completion.md", *sorted(RECIPES_DIR.rglob("*.md"))]
    examples = []
    for path in docs:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'(?:answer, e\.g\.|E\.g\.)\s+`?"([^"]*)"', text, re.S):
            examples.append((path.name, " ".join(m.group(1).split())))
    # The recipes that model a finished change all carry one (and completion.md's
    # own example), so the scan cannot pass by matching nothing.
    assert len(examples) >= 9, examples
    for name, example in examples:
        # <ref> may be a commit sha the operator named (specialist/install.md).
        for machinery in ("SHA", "sha>", "<ref>", "casa_reload", "reloaded triggers",
                          "scope="):
            assert machinery not in example, (name, example)
    assert not any("emit_completion" in p.read_text(encoding="utf-8") for p in docs)
