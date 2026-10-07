"""Pin for the #1148 split: the open-conversations rule leaves the bundle
transactions document.

`architecture/specialist-bundle-transactions.md`'s Scope named, as a clause of
its own, "what each change does to the specialist's open conversations — the
warning before it, and the close after an uninstall" (`:11-15` at 814ce23c),
and its manifest row declared INV-SPEC-022 beside INV-SPEC-003/011/012/013/014
(`architecture-s.yaml:107` at 814ce23c). Over the document ceiling, INV-DOC-007
owes the split. The split separates the open-conversations rule from the
bundle transaction mechanics:

- partition, resolved through the manifest: INV-SPEC-022's declaring document
  differs from the one document declaring the five retained ids;
- payload location: each of INV-SPEC-022's four passages, identified by a lead
  phrase quoted from the base bytes, occurs exactly once across the corpus's
  Markdown, in INV-SPEC-022's declaring document.

Red case demonstrated: at 814ce23c the partition fails — all six ids share the
transactions document. The payload arms already hold there (the passages sit
in that same document) and guard the move against leaving prose behind.

The predicate is also run over a synthetic, passing split and three named
mutants of it (INV-SPEC-022 left on the retained row; INV-SPEC-014 moved; one
passage left behind as a copy), each of which it must reject. It establishes
partition and lead location only — not byte-identical preservation of the
passages, nor the destination's path or shard.
"""
from test_pin_doc_corpus_shape import DOCS, _declaring_document, _entries

MOVED = "INV-SPEC-022"
RETAINED = ("INV-SPEC-003", "INV-SPEC-011", "INV-SPEC-012",
            "INV-SPEC-013", "INV-SPEC-014")

# Lead phrases of INV-SPEC-022's four passages, quoted from
# specialist-bundle-transactions.md:255, :257, :278 and :294 at 814ce23c. The
# first keeps the bold declaration marker: the generated invariant index
# repeats the statement but never renders the id in bold.
LEADS = (
    "**INV-SPEC-022**: While a specialist has open conversations",
    "A specialist's open conversations are its active or idle "
    "specialist-kind engagements",
    "Enforced by one listing (`tools.py::_open_specialist_engagements`) "
    "and two placements.",
    "What it does not cover. The model can acknowledge without asking",
)


def _normalized(text):
    return " ".join(text.split())


def _owner(entries, inv):
    owners = [e["doc"] for e in entries
              if inv in (e.get("defines_invariants") or [])]
    assert len(owners) == 1, (inv, owners)
    return owners[0]


def _split_problems(entries, texts):
    """Every way (entries, texts) fails the split; empty when it holds.

    `texts` maps a corpus-relative path to its Markdown text.
    """
    problems = []
    retained = {_owner(entries, inv) for inv in RETAINED}
    moved = _owner(entries, MOVED)
    if (len(retained), len(retained | {moved})) != (1, 2):
        problems.append(("partition", sorted(retained), moved))
    corpus = {path: _normalized(text) for path, text in texts.items()}
    for lead in LEADS:
        lead = _normalized(lead)
        counts = (sum(t.count(lead) for t in corpus.values()),
                  corpus.get(moved, "").count(lead))
        if counts != (1, 1):
            problems.append(("payload", lead, counts))
    return problems


def _corpus_texts():
    return {str(p.relative_to(DOCS)): p.read_text()
            for p in sorted(DOCS.rglob("*.md"))}


def test_the_split_separates_the_open_conversations_rule_from_the_transactions():
    entries = _entries()
    assert _owner(entries, MOVED) == _declaring_document(MOVED)
    assert _split_problems(entries, _corpus_texts()) == []


RETAINED_DOC = "architecture/retained.md"
MOVED_DOC = "architecture/moved.md"


def _synthetic_split():
    entries = [
        {"doc": RETAINED_DOC, "defines_invariants": list(RETAINED)},
        {"doc": MOVED_DOC, "defines_invariants": [MOVED]},
    ]
    texts = {
        RETAINED_DOC: "Transactions.\n\nThe journal and its locks.\n",
        # Re-wrapped on purpose: the leads are matched whitespace-normalized.
        MOVED_DOC: "\n\n".join(lead.replace(" ", "\n", 2) + " ..."
                               for lead in LEADS),
    }
    return entries, texts


def test_the_predicate_accepts_a_synthetic_split():
    entries, texts = _synthetic_split()
    assert _split_problems(entries, texts) == []


def test_mutant_the_moved_id_left_on_the_retained_row_is_rejected():
    entries, texts = _synthetic_split()
    entries[0]["defines_invariants"].append(MOVED)
    entries[1]["defines_invariants"].remove(MOVED)
    texts[RETAINED_DOC] += texts.pop(MOVED_DOC)
    problems = _split_problems(entries, texts)
    assert [p[0] for p in problems] == ["partition"]


def test_mutant_a_retained_id_moved_is_rejected():
    entries, texts = _synthetic_split()
    entries[0]["defines_invariants"].remove("INV-SPEC-014")
    entries[1]["defines_invariants"].append("INV-SPEC-014")
    problems = _split_problems(entries, texts)
    assert [p[0] for p in problems] == ["partition"]


def test_mutant_one_passage_left_behind_as_a_copy_is_rejected():
    entries, texts = _synthetic_split()
    texts[RETAINED_DOC] += "\n" + LEADS[2] + "\n"
    problems = _split_problems(entries, texts)
    assert problems == [("payload", _normalized(LEADS[2]), (2, 1))]


def test_mutant_one_passage_left_behind_alone_is_rejected():
    entries, texts = _synthetic_split()
    texts[MOVED_DOC] = texts[MOVED_DOC].replace(
        LEADS[2].replace(" ", "\n", 2), "")
    texts[RETAINED_DOC] += "\n" + LEADS[2] + "\n"
    problems = _split_problems(entries, texts)
    assert problems == [("payload", _normalized(LEADS[2]), (1, 0))]
