"""#427: the shipped defaults tree is NEUTRAL — a fresh install must not
carry any household-specific (MTG) doctrine, delegate routing, or persona.
That content belongs with the separately supplied specialist component, not
in the image defaults every user receives."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DEFAULTS_ROOT = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa" / "defaults"

_MTG_MARKERS = re.compile(r"\bmtg\b|gathering", re.IGNORECASE)


def _default_files():
    return sorted(p for p in DEFAULTS_ROOT.rglob("*") if p.is_file())


def test_defaults_tree_carries_no_mtg_doctrine() -> None:
    offenders: list[str] = []
    for path in _default_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _MTG_MARKERS.search(text):
            offenders.append(str(path.relative_to(DEFAULTS_ROOT)))
    assert offenders == []


def test_defaults_ship_no_judge_persona() -> None:
    assert not (DEFAULTS_ROOT / "personas" / "casa" / "judge").exists()


def test_concierge_ships_no_delegates() -> None:
    """The shipped concierge must not advertise or route to a delegate that
    the image does not ship (pre-fix: a mandatory `mtg` delegate whose
    invocation returned unknown_agent on a fresh install)."""
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(
        (DEFAULTS_ROOT / "agents" / "concierge" / "delegates.yaml").read_text(encoding="utf-8"))
    assert data.get("delegates") == []


def test_gary_persona_pack_manifest_matches_content() -> None:
    """The gary pack was edited for neutrality; its manifest checksums must
    match the admitted file bytes or the concierge fails to boot."""
    from persona_pack import load_persona_pack

    pack_dir = DEFAULTS_ROOT / "personas" / "casa" / "gary" / "0.1.0"
    pack = load_persona_pack(pack_dir / "pack", pack_dir / "manifest.json")
    assert pack.persona_id == "casa/gary"
