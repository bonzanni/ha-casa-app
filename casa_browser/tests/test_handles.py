"""Task 7: opaque handles bound to their run and page generation."""
from __future__ import annotations

import pytest

from casa_browser.handles import HandleRegistry, PrivateTarget, StaleHandle
from casa_browser.protocol import ID_PATTERN


def test_handles_are_opaque_ids():
    reg = HandleRegistry()
    t = PrivateTarget("link", "link", object(), "https://e.example/inv/SECRETTOKEN", "https://e.example/")
    h = reg.issue("r1", "s1", 7, t)
    assert ID_PATTERN.fullmatch(h)
    assert "SECRETTOKEN" not in h and "SECRETTOKEN" not in repr(t)
    assert reg.resolve(h, "r1", 7) is t


def test_stale_handle_other_run_or_generation():
    reg = HandleRegistry()
    h = reg.issue("r1", "s1", 7, PrivateTarget("element"))
    for run, gen in [("r2", 7), ("r1", 8), ("r1", 6)]:
        with pytest.raises(StaleHandle):
            reg.resolve(h, run, gen)
    with pytest.raises(StaleHandle):
        reg.resolve("hunknown", "r1", 7)
    with pytest.raises(StaleHandle):
        reg.resolve(None, "r1", 7)


def test_invalidate_page_drops_older_generations():
    reg = HandleRegistry()
    old = reg.issue("r1", "s1", 3, PrivateTarget("element"))
    new = reg.issue("r1", "s1", 4, PrivateTarget("element"))
    reg.invalidate_page(3)
    with pytest.raises(StaleHandle):
        reg.resolve(old, "r1", 3)
    assert reg.resolve(new, "r1", 4)
    reg.drop_run("r1")
    with pytest.raises(StaleHandle):
        reg.resolve(new, "r1", 4)


def test_duplicate_targets_get_distinct_handles():
    reg = HandleRegistry()
    t = PrivateTarget("element", "button")
    assert len({reg.issue("r1", None, 1, t) for _ in range(50)}) == 50
