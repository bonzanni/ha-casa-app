"""Guard: the configurator carry-over recipe ships and references the
report path + git-diff carry-over mechanism."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

RECIPE = Path(
    "casa/rootfs/opt/casa/defaults/configurator/"
    "doctrine/recipes/config/reconcile-defaults.md"
)


def test_recipe_exists_and_covers_carryover() -> None:
    assert RECIPE.is_file(), "reconcile-defaults recipe missing"
    text = RECIPE.read_text(encoding="utf-8")
    assert "/data/config-sync-report.json" in text
    assert "git" in text and "diff" in text
    assert ".casabak" in text
