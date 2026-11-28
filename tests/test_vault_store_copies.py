"""Casa Browser ships its own copies of the built-in store module and of its page.

The browser suite runs in a tree without Casa's code, so the check that the two
copies of each shared file are the same file lives here, where both are always
present. The script that copies the shared files is tested here too, on
temporary trees.
"""
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "sync_shared_vault.py"

_spec = importlib.util.spec_from_file_location("sync_shared_vault", SCRIPT)
sync_shared_vault = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_shared_vault)

STATIC = "vault_static"


@pytest.mark.parametrize("name", sync_shared_vault.SHARED_FILES)
def test_the_two_copies_are_identical(name):
    casa = sync_shared_vault.CASA_ROOT / name
    browser = sync_shared_vault.BROWSER_ROOT / name
    assert casa.is_file() and browser.is_file(), name
    assert casa.read_bytes() == browser.read_bytes(), name


def test_no_file_of_the_page_is_in_one_tree_only():
    """Each app serves its whole static directory, so a file added to one of the two and
    not named as shared would be served by that app alone and compared by nobody."""
    listed = {name for name in sync_shared_vault.SHARED_FILES if name.startswith(STATIC + "/")}
    assert listed
    for root in (sync_shared_vault.CASA_ROOT, sync_shared_vault.BROWSER_ROOT):
        found = {path.relative_to(root).as_posix()
                 for path in (root / STATIC).rglob("*") if path.is_file()}
        assert found == listed, root


def _source_tree(root: Path) -> Path:
    for index, name in enumerate(sync_shared_vault.SHARED_FILES):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"line {index}\nsecond line\r\n".encode())
    return root


def test_a_sync_into_an_empty_target_copies_every_file_exactly(tmp_path):
    source = _source_tree(tmp_path / "source")
    target = tmp_path / "target"

    sync_shared_vault.sync(source, target)

    for name in sync_shared_vault.SHARED_FILES:
        assert (target / name).read_bytes() == (source / name).read_bytes()
    assert sync_shared_vault.differing(source, target) == []


def test_a_changed_and_a_missing_copy_are_reported_by_name(tmp_path):
    source = _source_tree(tmp_path / "source")
    target = tmp_path / "target"
    sync_shared_vault.sync(source, target)
    changed, missing = sync_shared_vault.SHARED_FILES[1], sync_shared_vault.SHARED_FILES[-1]
    data = bytearray((target / changed).read_bytes())
    data[0] ^= 1
    (target / changed).write_bytes(bytes(data))
    (target / missing).unlink()

    assert sync_shared_vault.differing(source, target) == [changed, missing]


def test_every_shared_file_exists_in_casas_tree():
    for name in sync_shared_vault.SHARED_FILES:
        assert (sync_shared_vault.CASA_ROOT / name).is_file(), name
