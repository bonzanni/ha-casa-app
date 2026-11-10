"""Casa Browser is generic: no production file and no browser document names a site or a product
someone used it on. A term here is a substring, ignoring case, except that a short one (``\\b``) must
stand as a word. Tests and fixtures stay out of the scan. A new mention fails the build; a term that
turns out to be fine in one place is a decision to raise, not a line to allow here."""
from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
REPO = APP.parent
TERMS = re.compile("|".join((
    "cardmarket", "moxfield", "scryfall", "magic the gathering", "magic: the gathering",
    r"\bmtg\b", "secret lair", "wishlist", "wish list", "wants list", "wants-list", "decklist",
    r"\bfoil\b", "booster pack",
)), re.I)
SKIP_DIRS = {"__pycache__", "tests", "test"}


def _files() -> list[Path]:
    """Every file of the app's image (its rootfs, the Dockerfile's neighbours and translations) and
    the browser documents: the app's own and the corpus's reference pages."""
    app = [f for f in APP.rglob("*")
           if f.is_file() and not SKIP_DIRS.intersection(f.relative_to(APP).parts)]
    # The corpus's pages ship with the repository, not the image: the gate runs this suite
    # without Casa's roots (docs/ among them), CI with them.
    return app + sorted((REPO / "docs" / "reference").glob("casa-browser*.md"))


def _hits(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in TERMS.finditer(text)})


def test_the_term_rule_recognises_what_it_is_for():
    assert _hits("Open CardMarket and MTG, a Secret Lair wishlist") == [
        "cardmarket", "mtg", "secret lair", "wishlist"]
    assert _hits("the prompt was smtg, a tinfoil hat") == []


def test_the_scan_covers_what_ships_and_nothing_of_the_tests():
    rel = {f.relative_to(REPO).as_posix() for f in _files()}
    shipped = ["casa_browser/Dockerfile", "casa_browser/config.yaml", "casa_browser/DOCS.md",
               "casa_browser/rootfs/opt/casa_browser/controller.py",
               "casa_browser/rootfs/opt/casa_browser/locales/en.json"]
    if (REPO / "docs").is_dir():
        shipped.append("docs/reference/casa-browser-actions.md")
    for path in shipped:
        assert path in rel, path
    assert any(r.startswith("casa_browser/rootfs/opt/casa_browser/static/") for r in rel)
    assert not [r for r in rel if "/tests/" in r or r.startswith("casa_browser/test/")]


def test_nothing_that_ships_names_a_site_or_a_product():
    found = {}
    for path in _files():
        got = _hits(path.read_text("utf-8", "replace"))
        if got:
            found[path.relative_to(REPO).as_posix()] = got
    assert found == {}
