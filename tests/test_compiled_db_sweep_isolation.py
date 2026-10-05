"""Pins `conftest._isolate_compiled_db_sweep`: a test that reaches boot
replay's argument-less compiled-db sweep never removes a real
`/tmp/s6-casa-db-*` directory, which another pytest worker may own."""
from __future__ import annotations

import tempfile
from pathlib import Path

from drivers import s6_rc


def test_the_default_sweep_leaves_the_real_tmp_alone():
    planted = Path(tempfile.mkdtemp(prefix="s6-casa-db-", dir="/tmp"))
    try:
        removed = s6_rc.sweep_orphan_compiled_dbs()
        assert planted.is_dir(), "the sweep reached the real /tmp"
        assert str(planted) not in removed
    finally:
        planted.rmdir()


def test_an_explicit_root_is_still_swept(tmp_path):
    stale = tmp_path / "s6-casa-db-stale"
    stale.mkdir()
    assert s6_rc.sweep_orphan_compiled_dbs(tmp_root=str(tmp_path)) == [str(stale)]
    assert not stale.exists()
