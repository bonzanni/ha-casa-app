"""Casa Browser ships its own copy of the built-in store module.

The browser suite runs in a tree without Casa's code, so the check that the two
copies are the same file lives here, where both are always present.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_two_copies_are_identical():
    casa = REPO_ROOT / "casa/rootfs/opt/casa/vault_store.py"
    browser = REPO_ROOT / "casa_browser/rootfs/opt/casa_browser/vault_store.py"
    assert casa.read_bytes() == browser.read_bytes()
