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


def _shipped_resident_roles(yaml) -> set[str]:
    """The residents this image actually ships.

    Derived from the tree rather than listed here — a hardcoded set would pass
    by construction the day a resident is added or dropped, which is the drift
    this pin exists to catch — but INTERSECTED with the authoritative fixed
    slot set. A directory that merely claims `kind: resident` is not a shipped
    agent: `agent_loader` validates the resident set against
    `FIXED_RESIDENT_SLOTS` and refuses anything else (`agent_loader.py:1550-1560`),
    so trusting the claim alone would let a `defaults/agents/ghost/runtime.yaml`
    saying `kind: resident` legitimise `agent: ghost` here while boot rejects it.
    """
    from role_slot import FIXED_RESIDENT_SLOTS

    roles: set[str] = set()
    for runtime in (DEFAULTS_ROOT / "agents").glob("*/runtime.yaml"):
        data = yaml.safe_load(runtime.read_text(encoding="utf-8")) or {}
        if data.get("kind") == "resident":
            roles.add(runtime.parent.name)
    return roles & set(FIXED_RESIDENT_SLOTS)


def test_no_shipped_delegates_entry_names_an_unshipped_agent() -> None:
    """#525: a shipped `delegates.yaml` must not declare an agent the image
    does not contain (pre-fix: the assistant declared `finance`, a specialist
    that ships separately).

    This pins the CLASS, not the one file. A dead entry is not inert:
    `agent.py::_render_delegates_block` filters it out of the advertised block,
    so it silently becomes wiring that reads as done and never was — and
    `config_sync` re-seeds it into every live tree on every sync.
    """
    yaml = pytest.importorskip("yaml")
    shipped = _shipped_resident_roles(yaml)
    assert shipped, "no shipped residents found — the derivation is broken"

    offenders: list[str] = []
    for path in sorted((DEFAULTS_ROOT / "agents").glob("*/delegates.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for entry in data.get("delegates") or []:
            agent = entry.get("agent")
            if agent not in shipped:
                offenders.append(f"{path.relative_to(DEFAULTS_ROOT)}: {agent}")
    assert offenders == []


def test_gary_persona_pack_manifest_matches_content() -> None:
    """The gary pack was edited for neutrality; its manifest checksums must
    match the admitted file bytes or the concierge fails to boot."""
    from persona_pack import load_persona_pack

    pack_dir = DEFAULTS_ROOT / "personas" / "casa" / "gary" / "0.1.0"
    pack = load_persona_pack(pack_dir / "pack", pack_dir / "manifest.json")
    assert pack.persona_id == "casa/gary"
