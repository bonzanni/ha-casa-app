"""#1139 red case (C12-2): no configurator recipe tells the configurator to use
``specialist_install_commit`` to change an installed specialist's settings.

The install commit refuses any slug that already has an active tuple
(``active_present``, ``specialist_install.py`` bundle arm, INV-SPEC-020), so
such an instruction can only fail. At the base the retired ``update.md`` stub
said exactly that ("re-run ``specialist_install_commit`` with the new
``config`` for the installed component"). The ruled route is a same-version
``specialist_upgrade``.

The collector (as specified, after re-specification): every recipe item --
a paragraph split at list starts, soft wraps joined -- that mentions
config/configuration/settings AND installed/active is in scope. In it, EVERY
``specialist_install_commit`` token is a violation unless one of its next six
tokens starts with ``refus`` (the tool is the subject of a refusal) or its
previous six tokens hold ``never``, ``don't`` or the pair ``do not``. Each
mention is judged on its own, so a refusal of one never exempts another.

Bounded lexical pin, with known limits: it does not parse grammatical
subjects or negation scope. "For installed settings, call
specialist_install_commit; if it refuses, retry." yields 0 although it
instructs a call; an unrelated nearby negation can suppress a mention; context
split across items or expressed by synonyms escapes qualification; and a
purely descriptive mention can be flagged.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RECIPES_DIR = (
    REPO_ROOT
    / "casa/rootfs/opt/casa/defaults/agents/executors/configurator/doctrine/recipes"
)

TOOL = "specialist_install_commit"
WINDOW = 6

_ITEM_START = re.compile(r"^\s*(?:[-*]|\d+\.)\s")
_TOPIC = re.compile(r"\b(?:config|configuration|settings)\b", re.I)
_SUBJECT = re.compile(r"\b(?:installed|active)\b", re.I)
_TOKEN = re.compile(r"[a-z0-9_]+(?:'[a-z0-9_]+)?")


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


def _exempt(tokens: list[str], i: int) -> bool:
    after = tokens[i + 1:i + 1 + WINDOW]
    if any(t.startswith("refus") for t in after):
        return True
    before = tokens[max(0, i - WINDOW):i]
    if "never" in before or "don't" in before:
        return True
    return any(a == "do" and b == "not" for a, b in zip(before, before[1:]))


def violations_in(name: str, text: str) -> list[str]:
    found = []
    for item in _items(text):
        if not (_TOPIC.search(item) and _SUBJECT.search(item)):
            continue
        tokens = _TOKEN.findall(item.lower())
        for i, tok in enumerate(tokens):
            if tok == TOOL and not _exempt(tokens, i):
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


# The collector itself, against the specifier's mutants and controls.
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
    verbless = ("For an installed specialist, settings changes require\n"
                "`specialist_install_commit`.\n")
    camouflaged = ("4. `specialist_install_commit` refuses `active_present` on an "
                   "installed slug.\n   To change an installed specialist's config, "
                   "use specialist_install_commit with the new values.\n")
    one_sentence = ("- specialist_install_commit refuses an installed slug, so to "
                    "change its config call specialist_install_commit again.\n")
    two_affirmative = ("- To change an installed specialist's config, call "
                       "specialist_install_commit with the new values, then call "
                       "specialist_install_commit once more to confirm.\n")
    negated_then_affirmative = (
        "- Never call specialist_install_commit before the tap lands; once it has, "
        "for an installed specialist's config call specialist_install_commit.\n")
    assert len(violations_in("x.md", reworded)) == 1
    assert len(violations_in("x.md", verbless)) == 1
    assert len(violations_in("x.md", camouflaged)) == 1
    assert len(violations_in("x.md", one_sentence)) == 1
    assert len(violations_in("x.md", two_affirmative)) == 2
    assert len(violations_in("x.md", negated_then_affirmative)) == 1


def test_collector_exempts_refusals_and_negations():
    refusal = ("- `specialist_install_commit` refuses an installed slug with "
               "`active_present`; change its settings with `specialist_upgrade`.\n")
    controls = [
        refusal,
        "- Never call specialist_install_commit for an installed specialist's config.\n",
        "- Do not call specialist_install_commit for an installed specialist's config.\n",
        "- Don't call specialist_install_commit for an installed specialist's config.\n",
        # A fresh install: config, but nothing installed or active.
        "4. Once approved: `specialist_install_commit(slug=..., config={...})` using "
        "the EXACT values the inspect returned.\n",
    ]
    for text in controls:
        assert violations_in("x.md", text) == [], text
    for name in ("specialist/install.md", "specialist/upgrade.md"):
        base_text = (RECIPES_DIR / name).read_text(encoding="utf-8")
        hits = [v for v in violations_in(name, base_text)]
        assert hits == [], hits


def test_collector_window_boundaries():
    lead = "- For an installed specialist's config: "
    # `refuses` as the 6th / 7th token after the mention.
    six_after = lead + "specialist_install_commit a b c d e refuses.\n"
    seven_after = lead + "specialist_install_commit a b c d e f refuses.\n"
    assert len(violations_in("x.md", six_after)) == 0
    assert len(violations_in("x.md", seven_after)) == 1
    # `never` at distance 6 / 7 before the mention.
    six_before = lead + "never a b c d e specialist_install_commit.\n"
    seven_before = lead + "never a b c d e f specialist_install_commit.\n"
    assert len(violations_in("x.md", six_before)) == 0
    assert len(violations_in("x.md", seven_before)) == 1
