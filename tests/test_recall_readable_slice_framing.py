# tests/test_recall_readable_slice_framing.py
"""#581: a NON-EMPTY recall result must scope itself the way the empty ones do.

#472 taught the empty arms to say "not proof of absence" and stopped there.
But emptiness was never the property that needed framing — *boundedness* is,
and it holds of a large, useful result exactly as much as of an empty one. The
recall request sends the caller's readable tiers as a server-side tag filter,
so an above-clearance hit is dropped BY THE BACKEND and leaves no client-visible
trace: no count, no flag, nothing. The only observable is what the model then
says to a person.

Observed live on v0.206.1: a `private` fact was stored; on the voice surface
(clearance `friends`) recall logged `outcome=hits hits=33` — thirty-three
unrelated readable memories, the on-topic one dropped server-side — and the
butler answered "No record of a wall safe combination hint." A well-formed,
non-empty, entirely honest result, read as an inventory of Casa's memory.

FOUR model-facing consumers rendered a slice; every one of them was unframed on
its non-empty path, and the suite that shipped with #472 actively pinned the
defect. One remains a slice of this kind:

  4. `_fetch_executor_archive`'s block   → instruction line under the heading

The resident's auto-recall (2) and the specialist's pre-delegation recall (3)
were replaced by the served memory view, which is a whole ledger (or its
index), not a slice of one; the recall tool (1) by the memory tools, whose
results carry their own constant note (`tests/test_memory_search_tool.py`).

The site is asserted in ITS OWN emitted artifact, so deleting the framing from
it fails its test. For the same reason
the expected wording is written out LITERALLY here rather than imported from
production: a test that reads the constant it is meant to pin asserts nothing.

`test_every_slice_consumer_is_declared` is the guard against the failure mode
that produced this issue — a consumer added later that nobody remembers to
frame. It parses the tree for `render_recall` / `delegated_recall` call sites
and fails on any that this file has not classified, counts included. Its reach
is direct calls by name: an alias or other indirection escapes it, so it is
regression coverage, not a completeness proof.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

import tools

pytestmark = [pytest.mark.unit]

CASA_ROOT = (
    Path(__file__).resolve().parent.parent / "casa" / "rootfs" / "opt" / "casa"
)

# The wording, written out here independently of the production constant.
EXPECTED_NOTE = (
    "Use relevant entries normally. This is the bounded view readable at this "
    "surface, not a complete inventory of Casa's memory. If it does not answer "
    "the request, do not say Casa has no record of it or does not know it — "
    "say you have nothing you can share on that here. Do not repeat or "
    "paraphrase this guidance to the user."
)
EXPECTED_PROMPT_LINE = f"[memory-use instruction: {EXPECTED_NOTE}]"


def _text(res: dict) -> str:
    return res["content"][0]["text"]


def _payload(res: dict) -> dict:
    """The tool result decoded. ``_result`` serialises with the default
    ``ensure_ascii``, so the note's em dash arrives as ``\\u2014`` in the raw
    text — assert against the decoded fields, as the model's own parse does
    (the sibling empty-arm messages have always been escaped the same way)."""
    import json
    return json.loads(_text(res))


def _hit(text: str = "Nicola keeps the thermostat at 20C."):
    from personality_types import RecallHit
    return RecallHit(
        text=text, memory_type="world", sensitivity="friends",
        application_tags=(), provenance=None, backend_id="b1",
        document_id=None, chunk_id=None, source_fact_ids=None,
        metadata=None, context=None, score=None,
    )


# ---------------------------------------------------------------------------
# The wording itself
# ---------------------------------------------------------------------------


class TestTheNoteItself:
    def test_wording_is_pinned(self):
        """Anchored outside the module under test: the literal above is the
        contract, and production is checked against it (not the reverse)."""
        import recall_renderer
        assert recall_renderer.READABLE_SLICE_NOTE == EXPECTED_NOTE
        assert recall_renderer.READABLE_SLICE_PROMPT_LINE == EXPECTED_PROMPT_LINE

    def test_the_note_never_confirms_a_hidden_record(self):
        """It must scope the slice without ever conceding that the asked-about
        record exists — the one thing the framing may not disclose."""
        low = EXPECTED_NOTE.lower()
        for leak in ("hidden", "above your clearance", "filtered out",
                     "withheld", "someone else can see", "there are entries"):
            assert leak not in low, f"the note hints at a specific record: {leak!r}"

    def test_the_note_authorises_ordinary_use(self):
        """The opposite failure to the one #581 reports: a caveat that makes
        the agent hedge over memories it can plainly read. The wording has to
        say 'use these' before it says 'but'."""
        assert "use relevant entries normally" in EXPECTED_NOTE.lower()

    def test_the_note_is_not_repeated_aloud(self):
        assert "do not repeat" in EXPECTED_NOTE.lower()

    def test_the_prompt_line_is_marked_as_an_instruction(self):
        """It sits beside rendered '- X said: ...' bullets; unmarked, it reads
        as one more recalled fact."""
        assert EXPECTED_PROMPT_LINE.startswith("[memory-use instruction:")
        assert EXPECTED_PROMPT_LINE.endswith("]")


# ---------------------------------------------------------------------------
# Consumer 4 — the executor archive's lessons block
# ---------------------------------------------------------------------------


class TestExecutorArchiveBlock:
    async def test_lessons_block_carries_the_note(self, monkeypatch, tmp_path):
        """Sol, design round 1: this block reaches BOTH executor drivers
        through this one helper, and it is filtered twice over — by the
        launching turn's clearance and by the doctrine epoch."""
        from memory import capture
        from memory_serving_support import ledger_store, wire
        store = ledger_store(tmp_path)
        wire(monkeypatch, store)
        await capture.record(capture.summary(
            role="configurator", procedural_epoch="e1", tier="private",
            text="always run make setup first", engagement_id="eng-1"))

        out = await tools._fetch_executor_archive(
            role="configurator", procedural_epoch="e1",
            origin_channel="telegram", token_budget=2000,
        )
        assert "Prior engagements" in out
        assert "make setup" in out, "the summary must still be usable"
        assert EXPECTED_PROMPT_LINE in out

    async def test_empty_archive_stays_silent(self, monkeypatch, tmp_path):
        """The framing must not resurrect the empty case: "" renders as no
        block at all, and absence of a block is not a claim of absence (#201).
        A bare instruction line with no lessons under it would be exactly the
        fabricated header this helper's docstring forbids."""
        from memory_serving_support import ledger_store, wire
        wire(monkeypatch, ledger_store(tmp_path))
        assert await tools._fetch_executor_archive(
            role="configurator", procedural_epoch="e1",
            origin_channel="telegram", token_budget=2000) == ""


# ---------------------------------------------------------------------------
# Caller inventory — the guard against the fifth consumer
# ---------------------------------------------------------------------------

# Every call site of a slice-producing function, keyed by (module, enclosing
# function) so it survives renumbering, with how that site frames a NON-EMPTY
# result. A new call site fails the test below until it is declared here —
# which is the point: #581 existed because three consumers were added over time
# and none of them inherited the framing the first one grew.
DECLARED_SLICE_CALLERS: dict[tuple[str, str], tuple[int, str]] = {
    # render_recall — the typed renderer itself. delegated_recall has no
    # caller: a new one must be declared here with how it frames its slice.
    ("delegated_memory.py", "delegated_recall"): (
        1, "renders only; its callers frame (or are exempt) individually"),
    # A DIFFERENT render_recall — semantic_memory's legacy flat "- {text}"
    # renderer for the untyped recall() seam, which no consumer calls. Declared
    # rather than filtered out by name, so that wiring a consumer to the legacy
    # path has to pass through this table too.
    ("hindsight_memory.py", "recall"): (
        1, "exempt: legacy untyped recall(); no model-facing consumer calls it"),
}

_SLICE_FUNCS = frozenset({"render_recall", "delegated_recall"})


def _call_sites(path: Path) -> Counter:
    """(file, enclosing function) -> call count, for every call to a
    slice-producing function.

    Line numbers are deliberately not part of the key — this guard must survive
    ordinary edits and fail only on genuinely NEW work. The COUNT is part of the
    value (Sol, diff round 1): keyed on the pair alone, a second
    ``render_recall`` added inside an already-declared function — the cheapest
    way to grow a new unframed consumer — would collapse into the existing
    entry and change nothing.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: Counter = Counter()

    class _V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _enter(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _enter
        visit_AsyncFunctionDef = _enter

        def visit_Call(self, node):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name in _SLICE_FUNCS and self.stack:
                found[(path.name, self.stack[-1])] += 1
            self.generic_visit(node)

    _V().visit(tree)
    return found


def _all_call_sites() -> Counter:
    sites: Counter = Counter()
    for path in sorted(CASA_ROOT.rglob("*.py")):
        sites.update(_call_sites(path))
    return sites


def test_every_slice_consumer_is_declared():
    """What this does and does not prove (Sol + Terra, diff round 1, both named
    it the weakest part): it resolves DIRECT calls by name, so a new call site,
    or one more call inside an existing one, fails here. It cannot see a call
    made through an alias (``r = render_recall; r(...)``) or any other
    indirection. It is regression coverage against the way this defect actually
    arose — consumers accreting one at a time — not a proof that every slice
    reaching a model is framed."""
    sites = _all_call_sites()
    undeclared = {k: v for k, v in sites.items() if k not in DECLARED_SLICE_CALLERS}
    assert not undeclared, (
        "a new caller renders a clearance-filtered memory slice and has not "
        "declared how it frames a NON-EMPTY result (#581). Add it to "
        f"DECLARED_SLICE_CALLERS with its disposition: {sorted(undeclared)}"
    )
    miscounted = {
        k: (v, DECLARED_SLICE_CALLERS[k][0])
        for k, v in sites.items()
        if v != DECLARED_SLICE_CALLERS[k][0]
    }
    assert not miscounted, (
        "an already-declared function gained or lost a slice-producing call "
        "(#581). A NEW one there is a new consumer and needs its own framing "
        "decision; confirm it, then update the count in "
        f"DECLARED_SLICE_CALLERS. found vs declared: {miscounted}"
    )


def test_the_inventory_is_not_vacuous():
    """The guard above passes trivially if the parse finds nothing. Pin that
    it actually resolves the known sites."""
    sites = _all_call_sites()
    for expected in (
        ("delegated_memory.py", "delegated_recall"),
        ("hindsight_memory.py", "recall"),
    ):
        assert expected in sites, f"caller scan missed {expected}"
