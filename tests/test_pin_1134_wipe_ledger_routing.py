"""#1134: the wipe tool and the wipe route are routed, in ``docs/coverage.yaml``,
to the document that describes them.

The #894 split moved the wipe out of ``architecture/memory-lifecycle.md`` into
``architecture/memory-wipe.md`` and retargeted the ``memory_wipe.py`` module
row, because the #717 cross-check forced it. The ``tool:wipe_memory`` and
``POST /admin/memory/wipe`` rows were left behind: a namespaced row keys no
manifest ``covers`` path, so ``scripts/coverage_ledger.py`` checks only that its
document is manifested, never that the document is the one claiming the
surface. A reader routed by either row landed on a document that names
neither.

The expected owner is derived, not hard-coded: the one manifested document
whose ``covers`` hold the surface's own symbol, matched as the whole
``path::symbol`` string. A path-keyed match would return every document
covering any ``tools.py`` symbol. ``architecture/tools-interface.md`` also names
``wipe_memory``, so the identifier check alone would pass a misroute there; the
equality against the claimant is the assertion that carries the pin.

Scoped deliberately to these two rows. Widening the cross-check to namespaced
rows is a separate decision (#784).

Red case demonstrated at c3547ffd: both rows name memory-lifecycle.md, so each
parametrised case fails on the owner equality, independently.
"""

from pathlib import Path

import pytest

from scripts import coverage_ledger, verify_docs
from test_pin_doc_corpus_shape import _ledger_owner

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


@pytest.mark.parametrize(
    ("item", "anchor", "identifier"),
    [
        pytest.param(
            "tool:wipe_memory",
            "casa/rootfs/opt/casa/tools.py::wipe_memory",
            "wipe_memory",
            id="tool",
        ),
        pytest.param(
            "route:casa/rootfs/opt/casa/casa_core.py:POST:/admin/memory/wipe",
            "casa/rootfs/opt/casa/internal_handlers.py"
            "::build_admin_memory_wipe_handler",
            "/admin/memory/wipe",
            id="route",
        ),
    ],
)
def test_wipe_ledger_owner_matches_exact_claimant(item, anchor, identifier):
    assert item in coverage_ledger.enumerate_items(ROOT), (
        "Pinned surface disappeared; re-decide the pin",
        item,
    )

    entries, problems = verify_docs._load_entries(DOCS)
    assert len(problems) == 0, problems

    claimants = {
        entry["doc"]
        for entry in entries
        if anchor in (entry.get("covers") or [])
    }
    assert len(claimants) == 1, (
        "Owner derivation became ambiguous/empty (the claimant set changed); "
        "re-decide the pin rather than treating this as a routing break",
        anchor,
        sorted(claimants),
    )

    owner = _ledger_owner(item)
    assert owner == next(iter(claimants)), (item, owner, claimants)
    assert identifier in (DOCS / owner).read_text(), (item, owner, identifier)
