"""#1139 red case (C12-2): no configurator recipe tells the configurator to call
``specialist_install_commit`` to change an installed specialist's settings.

The install commit refuses any slug that already has an active tuple
(``active_present``, ``specialist_install.py`` bundle arm, INV-SPEC-020), so
such an instruction can only fail. At the base the retired ``update.md`` stub
said exactly that ("re-run ``specialist_install_commit`` with the new
``config`` for the installed component"). The ruled route is a same-version
``specialist_upgrade``.

This is a lexical check over recipe text, not a natural-language verifier:
it collects, per paragraph or list item, an affirmative call/re-run/invoke/use
instruction naming ``specialist_install_commit`` inside an item that talks
about settings/config of an installed/active specialist. Directly negated
instructions are exempt; a refusal sentence elsewhere in the item is not.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RECIPES_DIR = (
    REPO_ROOT
    / "casa/rootfs/opt/casa/defaults/agents/executors/configurator/doctrine/recipes"
)

_ITEM_START = re.compile(r"^\s*(?:[-*]|\d+\.)\s")
_TOPIC = re.compile(r"\b(?:config|configuration|settings)\b", re.I)
_SUBJECT = re.compile(r"\b(?:installed|active)\b", re.I)
_INSTRUCTION = re.compile(
    r"(?P<neg>\b(?:do not|don't|never)\s+)?"
    r"\b(?:re-run|rerun|call|invoke|use)\s+(?:the\s+)?specialist_install_commit\b",
    re.I)


def _items(text: str) -> list[str]:
    """Paragraphs split into list items; soft-wrapped lines stay with their item."""
    items: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        current: list[str] = []
        for line in para.splitlines():
            if _ITEM_START.match(line) and current:
                items.append(" ".join(current))
                current = []
            current.append(line.strip())
        if current:
            items.append(" ".join(current))
    return [re.sub(r"\s+", " ", i.replace("`", "")) for i in items]


def violations_in(name: str, text: str) -> list[str]:
    found = []
    for item in _items(text):
        if not (_TOPIC.search(item) and _SUBJECT.search(item)):
            continue
        for m in _INSTRUCTION.finditer(item):
            if m.group("neg"):
                continue
            found.append(f"{name}: {item[:200]}")
    return found


def _all_violations() -> list[str]:
    out = []
    for p in sorted(RECIPES_DIR.rglob("*.md")):
        out += violations_in(str(p.relative_to(RECIPES_DIR)),
                             p.read_text(encoding="utf-8"))
    return out


def test_no_install_commit_instruction_for_installed_settings():
    assert sum(1 for _ in RECIPES_DIR.rglob("*.md")) > 0
    violations = _all_violations()
    assert len(violations) == 0, violations


# The collector itself, against the mutants the specifier named: a check that
# only looked at update.md, or exempted any item that mentions a refusal, would
# let these through.
_BAD = ("- Config changes (model tier, memory budget, secrets): re-run\n"
        "  `specialist_install_commit` with the new `config` for the installed component\n"
        "  (see `recipes/specialist/install.md`, step 5).\n")


def test_collector_catches_the_instruction_in_any_recipe():
    for name in ("specialist/update.md", "specialist/upgrade.md",
                 "specialist/install.md", "resident/update.md"):
        assert len(violations_in(name, _BAD)) == 1, name


def test_collector_catches_rewordings_and_refusal_camouflage():
    reworded = ("For an active specialist, call `specialist_install_commit` "
                "with the new settings.\n")
    camouflaged = ("4. `specialist_install_commit` refuses `active_present` on an "
                   "installed slug.\n   To change an installed specialist's config, "
                   "use specialist_install_commit with the new values.\n")
    assert len(violations_in("x.md", reworded)) == 1
    assert len(violations_in("x.md", camouflaged)) == 1


def test_collector_exempts_refusals_and_negations():
    refusal = ("- `specialist_install_commit` refuses an installed slug with "
               "`active_present`; change its settings with `specialist_upgrade`.\n")
    negated = ("- Never call `specialist_install_commit` to change an installed "
               "specialist's config — it refuses `active_present`.\n")
    assert violations_in("x.md", refusal) == []
    assert violations_in("x.md", negated) == []
