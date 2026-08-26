"""The docs-corpus verifier.

Every case here was first a manual probe against the real corpus. A clean pass proves
nothing on its own — the point of these is that each check demonstrably bites.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# Loaded by explicit path, not `from scripts import verify_docs`: tests/conftest.py inserts
# the application code root at sys.path[0], and that root contains its OWN `scripts/`
# directory, which shadows the repo-root package. `python -m scripts.verify_docs` from the
# repo root is unaffected — but a test must not depend on sys.path ordering to import the
# thing it is testing.
_spec = importlib.util.spec_from_file_location(
    "casa_verify_docs", Path(__file__).resolve().parents[1] / "scripts" / "verify_docs.py"
)
verify_docs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_docs)

SOURCEMAP = "\n## Source & test map\n\n<!-- BEGIN SOURCEMAP -->\n<!-- END SOURCEMAP -->\n"
CODE_WINS = "> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.\n"

ENTRY = """
- doc: architecture/turn-loop.md
  summary: How one inbound message becomes one agent turn.
  when_changing: the turn lifecycle or turn timeouts
  covers: [casa/a.py::A.b]
  tests: [tests/test_a.py::test_b]
  related: [doctrine/publishing.md]
"""

SKELETON_MANIFEST = """
- doc: manifest.yaml
  kind: meta
  summary: The publication allowlist.
- doc: README.md
  kind: index
  summary: Routing map.
- doc: llms.txt
  kind: generated
  summary: Generated index.
- doc: doctrine/invariants.md
  kind: generated
  summary: Generated invariant index (families A-M).
- doc: doctrine/invariants-n-z.md
  kind: generated
  summary: Generated invariant index (families N-Z).
- doc: doctrine/publishing.md
  summary: What may be written down here.
  when_changing: anything published
- doc: contributing/doc-contract.md
  summary: How to keep this corpus true.
  when_changing: the documentation rules themselves
"""

SKELETON_FILES = {
    "README.md": "# Docs\n\n<!-- BEGIN ROUTING -->\n<!-- END ROUTING -->\n",
    "llms.txt": "",
    "doctrine/invariants.md": "",
    "doctrine/invariants-n-z.md": "",
    "doctrine/publishing.md": CODE_WINS + SOURCEMAP,
    "contributing/doc-contract.md": CODE_WINS + SOURCEMAP,
}

DOC = {"architecture/turn-loop.md": "# Turn loop\n" + CODE_WINS + SOURCEMAP}


def _corpus(tmp_path: Path, manifest: str = ENTRY, docs: dict[str, str] | None = None,
            *, skeleton: bool = True, stage: bool = True) -> Path:
    """A miniature repo. Real git, because the allowlist's ground truth is git ls-files."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(tmp_path), "config", key, value], check=True)

    files = {**(SKELETON_FILES if skeleton else {}), **(DOC if docs is None else docs)}
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manifest.yaml").write_text(
        manifest + (SKELETON_MANIFEST if skeleton else "")
    )
    for rel, body in files.items():
        target = tmp_path / "docs" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)

    (tmp_path / "casa").mkdir()
    (tmp_path / "casa" / "a.py").write_text("class A:\n    def b(self):\n        pass\n")
    (tmp_path / "casa" / "conf.yaml").write_text("schema:\n  foo: str\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_b():\n    pass\n")
    if stage:
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


# --- anchor primitives ---------------------------------------------------------------

def test_parse_anchor_splits_symbol():
    assert verify_docs.parse_anchor("casa/a.py::C.d") == ("casa/a.py", "C.d")
    assert verify_docs.parse_anchor("casa/a.py") == ("casa/a.py", None)


def test_symbol_exists_resolves_a_nested_method(tmp_path):
    src = tmp_path / "m.py"
    src.write_text("class A:\n    async def b(self):\n        pass\n")
    assert verify_docs.symbol_exists(src, "A.b") is True
    assert verify_docs.symbol_exists(src, "A.c") is False


def test_yaml_key_exists(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("schema:\n  foo: str\n")
    assert verify_docs.yaml_key_exists(cfg, "schema.foo") is True
    assert verify_docs.yaml_key_exists(cfg, "schema.bar") is False


# --- the corpus is clean, and each check bites ----------------------------------------

def test_a_clean_corpus_verifies(tmp_path):
    assert verify_docs.verify(_corpus(tmp_path)) == []


def test_a_dead_symbol_anchor_is_caught(tmp_path):
    root = _corpus(tmp_path, ENTRY.replace("A.b", "A.gone"))
    assert any("A.gone" in p and "does not resolve" in p for p in verify_docs.verify(root))


def test_a_line_number_anchor_is_rejected(tmp_path):
    """Pins INV-DOC-005's enforced core. Red case demonstrated: disabling the line-anchor rejection fails this test."""
    root = _corpus(tmp_path, ENTRY.replace("casa/a.py::A.b", "casa/a.py:12"))
    assert any("line-number anchor" in p for p in verify_docs.verify(root))


def test_an_anchor_outside_the_repository_is_rejected(tmp_path):
    root = _corpus(tmp_path, ENTRY.replace("casa/a.py::A.b", "/etc/passwd"))
    assert any("outside the repository" in p for p in verify_docs.verify(root))


def test_an_untracked_anchor_is_rejected(tmp_path):
    """It would pass locally while being absent from the published commit."""
    root = _corpus(tmp_path, ENTRY.replace("casa/a.py::A.b", "casa/ignored.py"))
    (root / "casa" / "ignored.py").write_text("x = 1\n")
    assert any("not tracked by git" in p for p in verify_docs.verify(root))


def test_a_tracked_file_with_no_manifest_entry_is_caught(tmp_path):
    root = _corpus(tmp_path, docs={**DOC, "architecture/stray.md": "# Stray\n" + SOURCEMAP})
    assert any("stray.md" in p and "not in the manifest" in p for p in verify_docs.verify(root))


def test_a_manifest_entry_git_does_not_track_is_caught(tmp_path):
    """One-directional exactness lets the verifier see a file the pushed commit lacks."""
    root = _corpus(tmp_path)
    ghost = root / "docs" / "architecture" / "ghost.md"
    ghost.write_text("# Ghost\n" + SOURCEMAP)          # created but never staged
    manifest = root / "docs" / "manifest.yaml"
    manifest.write_text(
        manifest.read_text()
        + "\n- doc: architecture/ghost.md\n  summary: s\n  when_changing: w\n"
    )
    assert any("ghost.md" in p and "does not track it" in p for p in verify_docs.verify(root))


def test_a_disallowed_extension_is_rejected(tmp_path):
    root = _corpus(tmp_path, docs={**DOC, "architecture/diagram.png": "\x89PNG\r\n"})
    assert any("diagram.png" in p and "not admitted" in p for p in verify_docs.verify(root))


def test_a_generated_index_gets_the_larger_budget(tmp_path):
    root = _corpus(tmp_path, docs={**DOC, "llms.txt": "x" * 30_000})
    assert verify_docs.verify(root) == []


def test_a_missing_sourcemap_marker_is_caught(tmp_path):
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": "# Turn loop\n" + CODE_WINS})
    assert any("SOURCEMAP" in p and "exactly one" in p for p in verify_docs.verify(root))


def test_reversed_sourcemap_markers_are_caught(tmp_path):
    body = "# T\n<!-- END SOURCEMAP -->\n<!-- BEGIN SOURCEMAP -->\n"
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": body})
    assert any("reversed" in p for p in verify_docs.verify(root))


def test_a_symlinked_doc_is_rejected(tmp_path):
    root = _corpus(tmp_path, docs={})
    target = root / "docs" / "architecture"
    target.mkdir(parents=True, exist_ok=True)
    (target / "turn-loop.md").symlink_to("/etc/passwd")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    assert any("symlink" in p for p in verify_docs.verify(root))


def test_path_traversal_in_the_manifest_is_rejected(tmp_path):
    root = _corpus(tmp_path, ENTRY.replace("architecture/turn-loop.md", "../../etc/passwd"))
    assert any("outside" in p for p in verify_docs.verify(root))


def test_a_duplicate_manifest_entry_is_caught(tmp_path):
    assert any("listed twice" in p for p in verify_docs.verify(_corpus(tmp_path, ENTRY + ENTRY)))


def test_required_fields_are_enforced(tmp_path):
    manifest = "\n- doc: architecture/turn-loop.md\n  covers: []\n"
    problems = verify_docs.verify(_corpus(tmp_path, manifest))
    assert any("`summary` is required" in p for p in problems)
    assert any("`when_changing` is required" in p for p in problems)


def test_a_pipe_in_a_table_rendered_field_is_rejected(tmp_path):
    root = _corpus(tmp_path, ENTRY.replace("the turn lifecycle or turn timeouts", "a | b"))
    assert any("free of `|`" in p for p in verify_docs.verify(root))


def test_a_malformed_kind_does_not_crash(tmp_path):
    """`kind: []` is unhashable and verify() builds sets before per-entry validation."""
    root = _corpus(tmp_path, ENTRY + "  kind: []\n")
    assert any("`kind` must be a string" in p for p in verify_docs.verify(root))


def test_invalid_yaml_is_a_finding_not_a_traceback(tmp_path):
    root = _corpus(tmp_path, "- doc: [unclosed\n", skeleton=False)
    assert any("not valid YAML" in p for p in verify_docs.verify(root))


def test_a_missing_skeleton_is_caught(tmp_path):
    """A manifest holding only its own entry would otherwise pass every other check."""
    root = _corpus(tmp_path, skeleton=False)
    assert any("required by the corpus contract" in p for p in verify_docs.verify(root))


def test_related_must_name_a_document_not_an_index(tmp_path):
    root = _corpus(tmp_path, ENTRY.replace("related: [doctrine/publishing.md]", "related: [llms.txt]"))
    assert any("not a manifested document" in p for p in verify_docs.verify(root))


# --- invariants ----------------------------------------------------------------------

def test_an_invariant_defined_twice_is_caught(tmp_path):
    """Pins INV-DOC-004's enforced core (define-once). Red case demonstrated: disabling the duplicate-definition check fails this test."""
    inv = "**INV-X-001**: one statement.\n"
    manifest = (
        ENTRY.replace("  related: [doctrine/publishing.md]",
                      "  related: []\n  defines_invariants: [INV-X-001]")
        + "\n- doc: architecture/other.md\n  summary: Other.\n  when_changing: else\n"
        + "  defines_invariants: [INV-X-001]\n"
    )
    root = _corpus(tmp_path, manifest, docs={
        "architecture/turn-loop.md": "# T\n" + CODE_WINS + inv + SOURCEMAP,
        "architecture/other.md": "# O\n" + CODE_WINS + inv + SOURCEMAP,
    })
    assert any("defined 2 times" in p for p in verify_docs.verify(root))


def test_an_undefined_invariant_reference_is_caught(tmp_path):
    root = _corpus(tmp_path, docs={
        "architecture/turn-loop.md": "# T\n" + CODE_WINS + "See INV-GHOST-009.\n" + SOURCEMAP,
    })
    assert any("INV-GHOST-009" in p and "never defined" in p for p in verify_docs.verify(root))


def test_a_declaration_mismatch_is_caught_both_ways(tmp_path):
    manifest = ENTRY.replace("  related: [doctrine/publishing.md]",
                             "  related: []\n  defines_invariants: [INV-X-002]")
    root = _corpus(tmp_path, manifest, docs={
        "architecture/turn-loop.md":
            "# T\n" + CODE_WINS + "**INV-X-001**: one statement.\n" + SOURCEMAP,
    })
    problems = verify_docs.verify(root)
    assert any("declares INV-X-002" in p for p in problems)
    assert any("does not declare" in p and "INV-X-001" in p for p in problems)


def test_a_wrapped_invariant_statement_is_caught(tmp_path):
    """It renders TRUNCATED into the generated index — only the definition line is captured.
    Found by reading the generated table, not by reasoning about it."""
    manifest = ENTRY.replace("  related: [doctrine/publishing.md]",
                             "  related: []\n  defines_invariants: [INV-X-001]")
    wrapped = "**INV-X-001**: this statement continues on\nthe following line.\n"
    root = _corpus(tmp_path, manifest, docs={
        "architecture/turn-loop.md": "# T\n" + CODE_WINS + wrapped + SOURCEMAP,
    })
    assert any("complete on ONE line" in p for p in verify_docs.verify(root))


# --- an invariant id cited in tracked Python prose (INV-DOC-009) ----------------------

def test_a_python_docstring_citing_an_undefined_id_in_a_defined_family_is_caught(
    tmp_path,
):
    """Pins INV-DOC-009. Specified by Sol, drive run 2026-08-25, cluster C7.

    Red case demonstrated: at the base commit the verifier had no
    tracked-prose pass at all and returned zero problems for this tree,
    exactly as it returned zero for the live citation of the retired
    OBS 002 id in `tests/test_pin_g5_invariants.py`.
    """
    manifest = _inv_manifest(
        "\n  invariant_tests:\n"
        "    INV-X-001: [tests/test_a.py::test_b]"
    )
    root = _corpus(tmp_path, manifest, docs=INV_DOC)

    fixture = (
        b"def test_b():\n"
        b'    """INV-X-001 replaces INV-X-002; INV-X-002 and '
        b'INV-GHOST-009 remain fixture prose."""\n'
        b"    pass\n"
    )
    (root / "tests" / "test_a.py").write_bytes(fixture)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)

    problems = verify_docs.verify(root)
    expected = (
        "tests/test_a.py:2: INV-X-002 is not defined; "
        "family X defines INV-X-001"
    )
    assert problems.count(expected) == 1
    assert len(problems) == 1



def _code(root: Path, files: dict[str, bytes]) -> list[str]:
    """Add tracked files to a miniature corpus and verify it.

    The corpus defines INV-X-001, so family X is defined INSIDE this fixture and
    undefined in the repository that holds these literals — which is why the same
    tokens are checked there and inert here.
    """
    for rel, body in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return verify_docs.verify(root)


def _prose_corpus(tmp_path: Path) -> Path:
    manifest = _inv_manifest(
        "\n  invariant_tests:\n    INV-X-001: [tests/test_a.py::test_b]"
    )
    return _corpus(tmp_path, manifest, docs=INV_DOC)


def test_an_undefined_id_in_a_python_comment_is_caught(tmp_path):
    problems = _code(_prose_corpus(tmp_path), {"casa/b.py": b"# INV-X-999\nx = 1\n"})
    assert problems == ["casa/b.py:1: INV-X-999 is not defined; family X defines INV-X-001"]


def test_a_defined_id_in_a_python_comment_passes(tmp_path):
    assert _code(_prose_corpus(tmp_path), {"casa/b.py": b"# INV-X-001 holds\n"}) == []


def test_an_ordinary_literal_is_not_prose_even_beside_a_comment(tmp_path):
    """The comment TOKEN is scanned, not the physical line. Reviewers reproduced that a
    whole-line scan turns an unrelated trailing comment into a refusal of fixture data."""
    files = {"casa/b.py": b'payload = "INV-X-999"  # fixture value\n'}
    assert _code(_prose_corpus(tmp_path), files) == []


def test_a_citation_deep_in_a_docstring_reports_its_own_physical_line(tmp_path):
    body = b'def f():\n    """one\n    two\n    INV-X-999 here\n    """\n'
    problems = _code(_prose_corpus(tmp_path), {"casa/b.py": body})
    assert problems == ["casa/b.py:4: INV-X-999 is not defined; family X defines INV-X-001"]


def test_two_ids_on_one_line_are_two_problems_and_a_repeat_is_one(tmp_path):
    body = b'# INV-X-999 INV-X-998 INV-X-999\n'
    problems = _code(_prose_corpus(tmp_path), {"casa/b.py": body})
    assert problems == [
        "casa/b.py:1: INV-X-998 is not defined; family X defines INV-X-001",
        "casa/b.py:1: INV-X-999 is not defined; family X defines INV-X-001",
    ]


def test_an_attribute_docstring_is_prose_at_module_class_and_init_scope(tmp_path):
    body = (b'A = 1\n"""INV-X-999 module attribute."""\n\n\n'
            b'class C:\n    B: int = 2\n    """INV-X-998 class attribute."""\n\n'
            b'    def __init__(self):\n        self.d = 3\n'
            b'        """INV-X-997 init attribute."""\n')
    problems = _code(_prose_corpus(tmp_path), {"casa/b.py": body})
    assert problems == [
        "casa/b.py:2: INV-X-999 is not defined; family X defines INV-X-001",
        "casa/b.py:7: INV-X-998 is not defined; family X defines INV-X-001",
        "casa/b.py:11: INV-X-997 is not defined; family X defines INV-X-001",
    ]


def test_a_string_after_an_assignment_in_an_ordinary_function_is_not_prose(tmp_path):
    """PEP 258 places attribute docstrings at module scope, class scope and a class's
    `__init__` — nowhere else. A no-op literal in an ordinary function is data, and
    refusing it would fail a correct tree."""
    body = (b'def work():\n    payload = 1\n    "INV-X-999"\n\n'
            b'def __init__(self):\n    x = 1\n    "INV-X-998"\n')
    assert _code(_prose_corpus(tmp_path), {"casa/b.py": body}) == []


def test_a_bare_string_that_is_not_a_docstring_is_not_prose(tmp_path):
    body = (b'def f(flag):\n    if flag:\n        "INV-X-999"\n\n'
            b'def g():\n    work()\n    "INV-X-998"\n\n'
            b'def h():\n    work()\n    "fixture"\n    "INV-X-997"\n')
    assert _code(_prose_corpus(tmp_path), {"casa/b.py": body}) == []


def test_a_second_docstring_after_the_first_is_prose(tmp_path):
    body = b'"""primary."""\n"""INV-X-999 secondary."""\n'
    problems = _code(_prose_corpus(tmp_path), {"casa/b.py": body})
    assert problems == ["casa/b.py:2: INV-X-999 is not defined; family X defines INV-X-001"]


def test_an_unknown_family_is_never_checked(tmp_path):
    """The verifier's own fixtures cite INV-X-* and INV-GHOST-*; a gate that fired on
    the corpus machinery's own test data would be waved through."""
    assert _code(_prose_corpus(tmp_path), {"casa/b.py": b"# INV-GHOST-009\n"}) == []


def test_an_extensionless_python_script_is_checked_by_its_shebang(tmp_path):
    files = {"bin/casactl": b"#!/usr/bin/env python3\n# INV-X-999\n",
             "bin/other": b"#!/usr/bin/python3.11 -u\n# INV-X-998\n"}
    problems = _code(_prose_corpus(tmp_path), files)
    assert problems == [
        "bin/casactl:2: INV-X-999 is not defined; family X defines INV-X-001",
        "bin/other:2: INV-X-998 is not defined; family X defines INV-X-001",
    ]


def test_a_non_python_comment_is_a_named_residual_not_a_refusal(tmp_path):
    """DELIBERATE (#679, D33 Part 1): the surface is Python. A `#`-line rule over shell
    and YAML was refuted — the repository's own shell citations live inside heredocs,
    and YAML block scalars and fenced Markdown are data. Closing this needs per-language
    comment grammars and is filed, not smuggled in."""
    files = {"run.sh": b"# INV-X-999\n", "c.yaml": b"# INV-X-998\n"}
    assert _code(_prose_corpus(tmp_path), files) == []


def test_an_untracked_python_file_is_not_checked(tmp_path):
    root = _prose_corpus(tmp_path)
    (root / "casa" / "loose.py").write_bytes(b"# INV-X-999\n")
    assert verify_docs.verify(root) == []


def test_a_python_file_with_an_encoding_cookie_is_decoded_as_python_would(tmp_path):
    body = ("# -*- coding: latin-1 -*-\n# caf\N{LATIN SMALL LETTER E WITH ACUTE}"
            " INV-X-999\n").encode("latin-1")
    problems = _code(_prose_corpus(tmp_path), {"casa/b.py": body})
    assert problems == ["casa/b.py:2: INV-X-999 is not defined; family X defines INV-X-001"]


def test_an_uninspectable_python_file_is_a_problem_not_an_exemption(tmp_path):
    files = {"casa/b.py": b"def (:\n# INV-X-999\n", "casa/c.py": b"x = 1\n\x00\n"}
    problems = _code(_prose_corpus(tmp_path), files)
    assert len(problems) == 2
    assert all("cannot be inspected" in p for p in problems)
    assert [p.split(":")[0] for p in problems] == ["casa/b.py", "casa/c.py"]


def test_a_tracked_symlink_is_a_problem_not_a_followed_path(tmp_path):
    root = _prose_corpus(tmp_path)
    (root / "casa" / "real.py").write_bytes(b"# INV-X-001\n")
    (root / "casa" / "link.py").symlink_to("real.py")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    problems = verify_docs.verify(root)
    assert problems == ["casa/link.py: a tracked symlink is not read — its target is not "
                        "the committed blob, so prose inside it cannot be checked"]


def test_a_tracked_file_missing_from_the_worktree_is_a_problem(tmp_path):
    root = _prose_corpus(tmp_path)
    (root / "casa" / "gone.py").write_bytes(b"# INV-X-001\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    (root / "casa" / "gone.py").unlink()
    assert verify_docs.verify(root) == [
        "casa/gone.py: cannot be read (FileNotFoundError) — the corpus gate must be "
        "able to see every tracked file"
    ]


def test_an_undefined_reference_inside_docs_is_reported_once(tmp_path):
    """`_check_invariants` owns docs/; the prose pass must not report it a second time."""
    root = _corpus(tmp_path, docs={
        "architecture/turn-loop.md": "# T\n" + CODE_WINS + "See INV-GHOST-009.\n" + SOURCEMAP,
    })
    problems = verify_docs.verify(root)
    assert [p for p in problems if "INV-GHOST-009" in p] == [
        "architecture/turn-loop.md: INV-GHOST-009 is referenced but never defined"
    ]


# --- invariant → pinning-test binding -------------------------------------------------

INV_DOC = {"architecture/turn-loop.md":
           "# T\n" + CODE_WINS + "**INV-X-001**: one statement.\n" + SOURCEMAP}


def _inv_manifest(binding: str = "") -> str:
    return ENTRY.replace(
        "  related: [doctrine/publishing.md]",
        "  related: []\n  defines_invariants: [INV-X-001]" + binding,
    )


def test_an_invariant_without_a_pinning_test_binding_is_refused(tmp_path):
    root = _corpus(tmp_path, _inv_manifest(), docs=INV_DOC)
    assert any(
        "INV-X-001" in p and "no pinning test" in p for p in verify_docs.verify(root)
    )


def test_a_binding_naming_an_untracked_test_file_is_refused(tmp_path):
    manifest = _inv_manifest("\n  invariant_tests:\n    INV-X-001: [tests/test_ghost.py]")
    root = _corpus(tmp_path, manifest, docs=INV_DOC)
    assert any(
        "INV-X-001" in p and "does not track" in p for p in verify_docs.verify(root)
    )


def test_a_binding_node_absent_from_the_file_is_refused(tmp_path):
    manifest = _inv_manifest(
        "\n  invariant_tests:\n    INV-X-001: [tests/test_a.py::test_vanished]"
    )
    root = _corpus(tmp_path, manifest, docs=INV_DOC)
    assert any(
        "test_vanished" in p and "does not appear" in p for p in verify_docs.verify(root)
    )


def test_a_correct_binding_passes(tmp_path):
    manifest = _inv_manifest("\n  invariant_tests:\n    INV-X-001: [tests/test_a.py::test_b]")
    root = _corpus(tmp_path, manifest, docs=INV_DOC)
    assert verify_docs.verify(root) == []


def test_a_class_qualified_binding_resolves_via_ast(tmp_path):
    """A pytest-runnable `Class::method` node id (a test nested inside a
    class, e.g. `pytest tests/test_a.py::TestC::test_b`) must verify —
    Python source never contains a literal '::' between a class and its
    method, so the plain substring search the bare-function case uses can
    never match this shape; it must resolve structurally via AST instead,
    exactly like a `covers:` entry's dotted symbol."""
    manifest = _inv_manifest(
        "\n  invariant_tests:\n    INV-X-001: [tests/test_a.py::TestC::test_b]"
    )
    root = _corpus(tmp_path, manifest, docs=INV_DOC)
    (root / "tests" / "test_a.py").write_text(
        "def test_b():\n    pass\n\n\nclass TestC:\n    def test_b(self):\n        pass\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    assert verify_docs.verify(root) == []


def test_a_class_qualified_binding_naming_a_missing_method_is_refused(tmp_path):
    manifest = _inv_manifest(
        "\n  invariant_tests:\n    INV-X-001: [tests/test_a.py::TestC::test_gone]"
    )
    root = _corpus(tmp_path, manifest, docs=INV_DOC)
    (root / "tests" / "test_a.py").write_text(
        "def test_b():\n    pass\n\n\nclass TestC:\n    def test_b(self):\n        pass\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    assert any(
        "TestC::test_gone" in p and "does not resolve" in p
        for p in verify_docs.verify(root)
    )


def test_the_pinning_sentinel_is_a_failure_naming_the_invariant(tmp_path):
    """The sentinel makes the missing-test backlog mechanical: the corpus is RED until
    every sentinel is replaced by a real, demonstrated-red pinning test."""
    manifest = _inv_manifest(
        "\n  invariant_tests:\n    INV-X-001: [tests/PINNING-TEST-MISSING]"
    )
    root = _corpus(tmp_path, manifest, docs=INV_DOC)
    assert any(
        "INV-X-001" in p and "PINNING-TEST-MISSING" in p for p in verify_docs.verify(root)
    )


def test_a_binding_for_an_undeclared_invariant_is_refused(tmp_path):
    """Bidirectional, like every other manifest check: a binding surviving its invariant's
    removal would keep a dead test looking load-bearing."""
    manifest = _inv_manifest(
        "\n  invariant_tests:\n"
        "    INV-X-001: [tests/test_a.py::test_b]\n"
        "    INV-X-009: [tests/test_a.py::test_b]"
    )
    root = _corpus(tmp_path, manifest, docs=INV_DOC)
    assert any(
        "INV-X-009" in p and "does not declare" in p for p in verify_docs.verify(root)
    )


def test_a_malformed_invariant_tests_field_is_a_finding_not_a_traceback(tmp_path):
    manifest = _inv_manifest("\n  invariant_tests: [not, a, mapping]")
    root = _corpus(tmp_path, manifest, docs=INV_DOC)
    assert any("invariant_tests" in p and "mapping" in p for p in verify_docs.verify(root))


# --- generated navigation ------------------------------------------------------------

def test_llms_links_resolve_relative_to_the_docs_directory(tmp_path):
    """Emitting `docs/...` from inside docs/ would resolve to docs/docs/..."""
    import re as _re
    root = _corpus(tmp_path)
    out = verify_docs.render_llms(root)
    targets = _re.findall(r"\]\(([^)]+)\)", out)
    assert targets
    for target in targets:
        assert not target.startswith("docs/")
        assert (root / "docs" / target).exists()


def test_routing_is_keyed_on_the_task_and_omits_indexes(tmp_path):
    out = verify_docs.render_routing(_corpus(tmp_path))
    assert "the turn lifecycle or turn timeouts" in out
    assert "llms.txt" not in out


def test_stale_nav_writes_nothing(tmp_path):
    root = _corpus(tmp_path)
    before = {p: p.read_bytes() for p in (root / "docs").rglob("*") if p.is_file()}
    assert verify_docs.stale_nav(root), "the seeded corpus is stale"
    after = {p: p.read_bytes() for p in (root / "docs").rglob("*") if p.is_file()}
    assert before == after, "--check-nav must not mutate what it inspects"


def test_write_nav_is_idempotent_and_keeps_handwritten_text(tmp_path):
    root = _corpus(tmp_path)
    readme = root / "docs" / "README.md"
    readme.write_text("# Docs\n\nRead me first.\n\n<!-- BEGIN ROUTING -->\nstale\n<!-- END ROUTING -->\n\nFooter.\n")
    verify_docs.write_nav(root)
    assert verify_docs.write_nav(root) == []
    assert verify_docs.stale_nav(root) == []
    text = readme.read_text()
    assert "Read me first." in text and "Footer." in text and "stale" not in text


def test_the_sourcemap_is_injected_from_the_manifest(tmp_path):
    root = _corpus(tmp_path)
    verify_docs.write_nav(root)
    text = (root / "docs" / "architecture" / "turn-loop.md").read_text()
    assert "casa/a.py::A.b" in text
    assert "tests/test_a.py::test_b" in text


# --- docs impact ---------------------------------------------------------------------

def test_impacted_docs_maps_a_changed_path_to_its_claimants(tmp_path):
    root = _corpus(tmp_path)
    assert verify_docs.impacted_docs(root, ["casa/a.py"]) == {"architecture/turn-loop.md"}
    assert verify_docs.impacted_docs(root, ["casa/unclaimed.py"]) == set()


def test_impacted_docs_honours_a_claim_deleted_in_the_same_change(tmp_path):
    """Dropping the covers anchor in the same PR must not drop the obligation."""
    root = _corpus(tmp_path, ENTRY.replace("  covers: [casa/a.py::A.b]\n", ""))
    assert verify_docs.impacted_docs(root, ["casa/a.py"]) == set()
    assert verify_docs.impacted_docs(root, ["casa/a.py"], base_manifest=ENTRY) == {
        "architecture/turn-loop.md"
    }


# --- round-9 findings ----------------------------------------------------------------

def test_an_anchor_through_a_tracked_symlink_is_rejected(tmp_path):
    """The lexical path is tracked, so the tracked-set check passes — and then symbol
    resolution reads the destination, which may not be in the commit at all."""
    root = _corpus(tmp_path, ENTRY.replace("casa/a.py::A.b", "casa/link.py::A.b"))
    (root / "casa" / "hidden.py").write_text("class A:\n    def b(self):\n        pass\n")
    (root / "casa" / "link.py").symlink_to("hidden.py")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    assert any("is a symlink" in p for p in verify_docs.verify(root))


def test_an_invariant_with_no_statement_is_caught(tmp_path):
    """It renders as a blank row; only the terminal-punctuation heuristic was checked."""
    manifest = ENTRY.replace("  related: [doctrine/publishing.md]",
                             "  related: []\n  defines_invariants: [INV-X-001]")
    root = _corpus(tmp_path, manifest, docs={
        "architecture/turn-loop.md": "# T\n" + CODE_WINS + "**INV-X-001**:\n" + SOURCEMAP,
    })
    assert any("no statement on its definition line" in p for p in verify_docs.verify(root))


def test_a_root_level_document_is_rejected(tmp_path):
    """It verifies cleanly but render_llms emits only the three configured prefixes, so it
    would be absent from the flat index it is supposed to appear in."""
    manifest = ENTRY + "\n- doc: stray.md\n  summary: s\n  when_changing: w\n"
    root = _corpus(tmp_path, manifest,
                   docs={**DOC, "stray.md": "# Stray\n" + CODE_WINS + SOURCEMAP})
    assert any("root-level document is omitted" in p for p in verify_docs.verify(root))


def test_reversed_routing_markers_are_reported_not_raised(tmp_path):
    root = _corpus(tmp_path)
    (root / "docs" / "README.md").write_text(
        "# Docs\n\n<!-- END ROUTING -->\n<!-- BEGIN ROUTING -->\n"
    )
    try:
        verify_docs.nav_targets(root)
    except SystemExit as exc:
        assert "reversed" in str(exc)
    else:
        raise AssertionError("reversed markers should be reported")


# --- prose must not name code that does not exist ---------------------------------------
#
# These exist because the check was added AFTER the failure it prevents had already been
# committed: a published sentence named `Agent._attempt_bypass_turn`, which is a closure
# nested inside `Agent._process` and not an attribute of Agent at all. The claim survived
# symbol-anchor verification, because the anchors were right and only the prose was wrong.

MODULES = {"agent.py", "agent_registry.py"}
NAMES = {
    "Agent",
    "Agent._process",              # a real method
    "_process",
    "_attempt_bypass_turn",        # a closure: defined, but not a method of anything
    "AgentRegistry",
    "AgentRegistry.tier_for_role",
    "tier_for_role",
    "build",                       # a method of some other class, i.e. inheritable
    "Base.build",
}


def _prose(text):
    return verify_docs._check_prose_code(text, "d.md", MODULES, NAMES)


def test_a_closure_named_as_a_method_is_refused():
    """The exact sentence that shipped wrong."""
    problems = _prose("see `Agent._attempt_bypass_turn` for the bypass")
    assert problems, "a closure dressed up as a method must not pass"
    assert "not a method of any class" in problems[0]


def test_a_real_method_passes():
    assert not _prose("`Agent._process` drives the turn")


def test_an_inherited_method_passes():
    """Only definition sites are recorded, so a subclass reference must not be refused."""
    assert not _prose("`AgentRegistry.build` assembles the index")


def test_a_retired_module_is_refused():
    problems = _prose("configured in `marketplace_ops.py`")
    assert problems and "does not exist" in problems[0]


def test_a_live_module_passes():
    assert not _prose("configured in `agent_registry.py`")


def test_an_invented_function_is_refused():
    problems = _prose("call `totally_made_up_thing()` first")
    assert problems and "does not exist" in problems[0]


def test_a_method_of_a_nested_class_is_not_flagged_as_a_closure():
    """`ast.walk` collects nested classes; if it did not, honest prose would be refused."""
    import ast as _ast
    tree = _ast.parse("class Outer:\n    class Inner:\n        def go(self): pass\n")
    found = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    found.add(f"{node.name}.{sub.name}")
    assert "Inner.go" in found


def test_a_reference_document_is_admitted(tmp_path):
    """reference/ is a corpus directory (operator reference docs); it renders
    into its own llms.txt section and passes the top-dir check."""
    manifest = ENTRY.replace("architecture/turn-loop.md", "reference/ops.md")
    root = _corpus(tmp_path, manifest, docs={"reference/ops.md": "# O\n" + CODE_WINS + SOURCEMAP})
    assert verify_docs.verify(root) == []
    assert "reference/ops.md" in verify_docs.render_llms(root)


def test_default_invocation_fails_on_stale_nav(tmp_path, monkeypatch, capsys):
    """The documented keep-me-green command is the BARE invocation; it must
    fail on stale generated navigation exactly like the gate does (which
    passes --check-nav). Pre-fix, the bare run printed a green corpus verdict
    over a stale routing table and the mismatch surfaced only at push time.

    Red case demonstrated: restoring the `if "--check-nav" in args:` guard
    around the staleness check fails this test.
    """
    root = _corpus(tmp_path)
    assert verify_docs.stale_nav(root), "the seeded corpus must start stale"
    monkeypatch.setattr(sys, "argv", ["verify_docs", str(root)])
    assert verify_docs.main() == 1
    out = capsys.readouterr().out
    assert "generated navigation is stale" in out


# --- manifest shards (#367) ----------------------------------------------------------

SHARD_META = """
- doc: manifest.d/architecture.yaml
  kind: meta
  summary: Manifest shard - architecture documents.
"""


def _sharded_corpus(tmp_path: Path, shard_body: str) -> Path:
    root = _corpus(tmp_path, manifest=SHARD_META, stage=False)
    shard_dir = tmp_path / "docs" / "manifest.d"
    shard_dir.mkdir()
    (shard_dir / "architecture.yaml").write_text(shard_body)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return root


def test_a_shard_under_manifest_d_contributes_entries(tmp_path):
    """#367: the manifest shards at its ceiling — entries in docs/manifest.d/*.yaml
    are loaded exactly like root entries, so a corpus whose documents are claimed
    only by a shard verifies clean."""
    root = _sharded_corpus(tmp_path, ENTRY)
    assert verify_docs.verify(root) == []


def test_a_dead_anchor_in_a_shard_is_caught(tmp_path):
    """Shard entries are VERIFIED, not merely admitted — a dead anchor in a
    shard must bite exactly as it does in the root manifest."""
    root = _sharded_corpus(tmp_path, ENTRY.replace("casa/a.py::A.b", "casa/a.py::A.zzz"))
    assert any("A.zzz" in p for p in verify_docs.verify(root))


def test_a_shard_over_the_index_ceiling_in_tree_mode_is_a_notice(tmp_path):
    """Index kinds follow the same trigger rule as documents: without a base
    there is nothing to measure a crossing against, so tree mode reports."""
    root = _sharded_corpus(tmp_path, ENTRY + ("# pad\n" * 7000))
    problems, notices = verify_docs.check_ceilings(root)
    assert problems == []
    assert any("architecture.yaml" in n and "40 KB index ceiling" in n for n in notices)
    assert not any("40 KB" in p for p in verify_docs.verify(root))


def test_a_document_may_not_live_under_manifest_d(tmp_path):
    """manifest.d/ holds manifest shards only — a kind:document entry whose file
    hides there escapes the routing/index rules and is refused."""
    rogue = (
        "- doc: manifest.d/rogue.md\n"
        "  summary: Sneaky.\n"
        "  when_changing: nothing\n"
    )
    root = _corpus(
        tmp_path, manifest=rogue,
        docs={"manifest.d/rogue.md": "# Rogue\n" + CODE_WINS + SOURCEMAP},
    )
    assert any("manifest.d" in p and "rogue.md" in p for p in verify_docs.verify(root))


def test_a_doc_duplicated_between_root_and_shard_is_caught(tmp_path):
    root = _corpus(tmp_path, manifest=SHARD_META + ENTRY, stage=False)
    shard_dir = tmp_path / "docs" / "manifest.d"
    shard_dir.mkdir()
    (shard_dir / "architecture.yaml").write_text(ENTRY)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert any("listed twice" in p for p in verify_docs.verify(root))


# --- the base-aware size trigger (#722) ----------------------------------------------
#
# The ceiling is a tripwire measured against a base, not a tree-state prohibition:
# the change that crosses it lands (visibly), the next change touching the
# over-ceiling document — its file or its manifest row — must split it first, and
# no document is born over the ceiling. Without a base, size is measured and
# reported, never failed; the enforcing caller is the pull-request check, which
# passes the merge-base.

BIG = "# Turn loop\n" + CODE_WINS + "x" * 26_000 + SOURCEMAP
SMALL = "# Turn loop\n" + CODE_WINS + SOURCEMAP


def _commit(root: Path, message: str = "c") -> str:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "commit.gpgsign=false", "commit", "-q",
         "--no-verify", "-m", message],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_tree_mode_reports_an_over_ceiling_document_without_failing(tmp_path):
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": BIG})
    assert verify_docs.verify(root) == []
    problems, notices = verify_docs.check_ceilings(root)
    assert problems == []
    assert any("turn-loop.md" in n and "25 KB ceiling" in n for n in notices)


def test_the_warn_tier_is_gone():
    """The 20 KB warn band was a second, stricter size regime holding previously
    split documents below everyone else's ceiling; the uniform ceiling is the
    only size rule left."""
    assert not hasattr(verify_docs, "WARN_BYTES")
    assert not hasattr(verify_docs, "warnings")


def test_a_document_under_the_ceiling_yields_no_size_finding(tmp_path):
    body = "# Turn loop\n" + CODE_WINS + "x" * 21_000 + SOURCEMAP
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": body})
    assert verify_docs.check_ceilings(root) == ([], [])


def test_a_first_crossing_lands_with_a_notice(tmp_path):
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": SMALL})
    base = _commit(root)
    (root / "docs" / "architecture" / "turn-loop.md").write_text(BIG)
    _commit(root)
    assert verify_docs.verify(root, base=base) == []
    problems, notices = verify_docs.check_ceilings(root, base=base)
    assert problems == []
    assert any("turn-loop.md" in n and "crossed" in n for n in notices)


def test_a_touched_document_past_the_ceiling_at_the_base_fails(tmp_path):
    """Pins INV-DOC-007's enforced core, with test_a_document_born_over_the_ceiling_fails.
    Red case demonstrated: disabling the touched-over-at-base arm fails this test."""
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": BIG})
    base = _commit(root)
    (root / "docs" / "architecture" / "turn-loop.md").write_text(BIG + "more\n")
    _commit(root)
    assert any(
        "turn-loop.md" in p and "split it first" in p
        for p in verify_docs.verify(root, base=base)
    )


def test_a_touched_document_brought_back_under_the_ceiling_passes(tmp_path):
    """The obligation is discharged by ending under the ceiling — whether that
    took a split or a trim is the reviewers' judgment, not the machine's."""
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": BIG})
    base = _commit(root)
    (root / "docs" / "architecture" / "turn-loop.md").write_text(SMALL)
    _commit(root)
    assert verify_docs.verify(root, base=base) == []
    assert verify_docs.check_ceilings(root, base=base) == ([], [])


def test_an_untouched_document_past_the_ceiling_at_the_base_is_a_notice(tmp_path):
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": BIG})
    base = _commit(root)
    (root / "casa" / "a.py").write_text(
        "class A:\n    def b(self):\n        pass\n# touched elsewhere\n"
    )
    _commit(root)
    problems, notices = verify_docs.check_ceilings(root, base=base)
    assert problems == []
    assert any("turn-loop.md" in n and "untouched" in n for n in notices)
    assert verify_docs.verify(root, base=base) == []


NEW_ROW = """
- doc: architecture/big.md
  summary: Newly added.
  when_changing: nothing yet
"""


def test_a_document_born_over_the_ceiling_fails(tmp_path):
    """Pins INV-DOC-007's enforced core, with test_a_touched_document_past_the_ceiling_at_the_base_fails.
    Red case demonstrated: disabling the born-over arm fails this test."""
    root = _corpus(tmp_path)
    base = _commit(root)
    (root / "docs" / "architecture" / "big.md").write_text(BIG)
    manifest = root / "docs" / "manifest.yaml"
    manifest.write_text(manifest.read_text() + NEW_ROW)
    _commit(root)
    assert any(
        "big.md" in p and "born over" in p for p in verify_docs.verify(root, base=base)
    )


def test_a_renamed_over_ceiling_document_fails_as_born_over(tmp_path):
    """A rename or copy is a NEW PATH: there is no lineage tracking anywhere in
    this mechanism, so over-ceiling debt cannot be laundered through history
    geometry — the renamed document must be split before it first lands."""
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": BIG})
    base = _commit(root)
    subprocess.run(
        ["git", "-C", str(root), "mv",
         "docs/architecture/turn-loop.md", "docs/architecture/renamed.md"],
        check=True,
    )
    manifest = root / "docs" / "manifest.yaml"
    manifest.write_text(
        manifest.read_text().replace("architecture/turn-loop.md", "architecture/renamed.md")
    )
    _commit(root)
    assert any(
        "renamed.md" in p and "born over" in p for p in verify_docs.verify(root, base=base)
    )


def test_a_manifest_row_change_touches_an_over_ceiling_document(tmp_path):
    """"Touched" is the document's file OR its manifest row: the row is the other
    half of a document's identity, and a row-only change must not slide past an
    inherited over-ceiling document as not-its-business."""
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": BIG})
    base = _commit(root)
    manifest = root / "docs" / "manifest.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "How one inbound message becomes one agent turn.",
            "How one inbound message becomes exactly one agent turn.",
        )
    )
    _commit(root)
    assert any(
        "turn-loop.md" in p and "split it first" in p
        for p in verify_docs.verify(root, base=base)
    )


def test_the_tree_kind_binds_the_ceiling_on_a_kind_flip(tmp_path):
    """A manifest-only kind flip must not land an oversized document: the flip
    changes the row (touched) and the TREE kind's ceiling is the one that binds,
    so a 30 KB index reclassified as a document fails against 25 KB."""
    flip = (
        "- doc: architecture/big.md\n"
        "  kind: index\n"
        "  summary: Big.\n"
    )
    root = _corpus(tmp_path, manifest=flip, docs={"architecture/big.md": "x" * 30_000})
    base = _commit(root)
    manifest = root / "docs" / "manifest.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "  kind: index\n  summary: Big.\n",
            "  summary: Big.\n  when_changing: big things\n",
        )
    )
    _commit(root)
    assert any(
        "big.md" in p and "25 KB ceiling" in p and "split it first" in p
        for p in verify_docs.verify(root, base=base)
    )


def test_an_index_born_over_the_index_ceiling_fails(tmp_path):
    root = _corpus(tmp_path)
    base = _commit(root)
    (root / "docs" / "manifest.d").mkdir()
    (root / "docs" / "manifest.d" / "architecture.yaml").write_text(
        "[]\n" + "# pad\n" * 8000
    )
    manifest = root / "docs" / "manifest.yaml"
    manifest.write_text(manifest.read_text() + SHARD_META)
    _commit(root)
    assert any(
        "architecture.yaml" in p and "born over" in p and "40 KB index ceiling" in p
        for p in verify_docs.verify(root, base=base)
    )


def test_an_unresolvable_base_is_a_refusal(tmp_path):
    """--base is validated eagerly: a wiring mistake in the enforcing caller must
    surface on the first pull request, not on the first crossing."""
    root = _corpus(tmp_path)
    _commit(root)
    assert any("does not resolve" in p for p in verify_docs.verify(root, base="0" * 40))


def test_an_unreadable_base_object_is_a_refusal_not_a_birth(tmp_path):
    """Absent and unreadable are different answers: a missing base object must
    refuse, never read as "the path is new at the base"."""
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": BIG})
    base = _commit(root)
    blob = subprocess.run(
        ["git", "-C", str(root), "rev-parse", f"{base}:docs/architecture/turn-loop.md"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (root / "docs" / "architecture" / "turn-loop.md").write_text(BIG + "more\n")
    _commit(root)
    loose = root / ".git" / "objects" / blob[:2] / blob[2:]
    assert loose.exists(), "the base blob must be loose for this construction"
    loose.chmod(0o644)
    loose.unlink()
    problems = verify_docs.verify(root, base=base)
    assert any("turn-loop.md" in p and "unreadable" in p for p in problems)
    assert not any("born over" in p for p in problems)


def test_an_unreadable_base_tree_is_a_refusal_not_a_birth(tmp_path):
    """The other unreadable shape: a missing TREE object makes ls-tree fail
    outright (a missing blob still lists, with an unparseable size — the case
    above). Both must refuse; neither may read as "the path is new at the base"."""
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": BIG})
    base = _commit(root)
    tree = subprocess.run(
        ["git", "-C", str(root), "rev-parse", f"{base}:docs/architecture"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (root / "docs" / "architecture" / "turn-loop.md").write_text(BIG + "more\n")
    _commit(root)
    loose = root / ".git" / "objects" / tree[:2] / tree[2:]
    assert loose.exists(), "the base tree must be loose for this construction"
    loose.unlink()
    problems = verify_docs.verify(root, base=base)
    assert any("turn-loop.md" in p and "unreadable" in p for p in problems)
    assert not any("born over" in p for p in problems)


def test_an_unparseable_base_manifest_is_a_refusal(tmp_path):
    """The manifest-row half of "touched" cannot be answered without the base
    rows; guessing either way would fail open or fail wrong."""
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": BIG})
    manifest = root / "docs" / "manifest.yaml"
    good = manifest.read_text()
    manifest.write_text("- doc: [unclosed\n")
    base = _commit(root)
    manifest.write_text(good)
    _commit(root)
    problems = verify_docs.verify(root, base=base)
    assert any("cannot tell" in p and "manifest row" in p for p in problems)


def test_an_unparseable_base_manifest_refuses_with_nothing_over_ceiling(tmp_path):
    """Eager, like the base-commit check: the refusal must fire on the first
    pull request that meets an unreadable base manifest, not only when an
    over-ceiling document happens to need its row compared."""
    root = _corpus(tmp_path)
    manifest = root / "docs" / "manifest.yaml"
    good = manifest.read_text()
    manifest.write_text("- doc: [unclosed\n")
    base = _commit(root)
    manifest.write_text(good)
    _commit(root)
    problems = verify_docs.verify(root, base=base)
    assert any("cannot tell" in p and "manifest row" in p for p in problems)


def test_a_duplicate_key_base_row_is_a_refusal_not_untouched(tmp_path):
    """The base manifest must parse under the SAME duplicate-refusing loader as
    the tree's: plain safe_load keeps the last duplicate key, so an invalid
    base row form could parse equal to a clean head row and read as untouched
    — a green run over base state the verifier itself rejects."""
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": BIG})
    manifest = root / "docs" / "manifest.yaml"
    good = manifest.read_text()
    manifest.write_text(
        good.replace(
            "  when_changing: the turn lifecycle or turn timeouts\n",
            "  when_changing: nonsense\n"
            "  when_changing: the turn lifecycle or turn timeouts\n",
        )
    )
    base = _commit(root)
    manifest.write_text(good)
    _commit(root)
    problems, notices = verify_docs.check_ceilings(root, base=base)
    assert any("cannot tell" in p and "manifest row" in p for p in problems)
    assert not any("untouched" in n for n in notices)


def test_a_representation_only_row_change_is_not_a_touch(tmp_path):
    """Row identity is what consumers PARSE: re-quoting a scalar changes no
    consumed semantic (the ceiling-binding kind included), so it must not turn
    an inherited over-ceiling document into this change's business. A semantic
    row change stays a touch — test_a_manifest_row_change_touches_an_over_ceiling_document
    pins that side."""
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": BIG})
    base = _commit(root)
    manifest = root / "docs" / "manifest.yaml"
    assert "  when_changing: the turn lifecycle or turn timeouts\n" in manifest.read_text()
    manifest.write_text(
        manifest.read_text().replace(
            "  when_changing: the turn lifecycle or turn timeouts\n",
            '  when_changing: "the turn lifecycle or turn timeouts"\n',
        )
    )
    _commit(root)
    problems, notices = verify_docs.check_ceilings(root, base=base)
    assert problems == []
    assert any("turn-loop.md" in n and "untouched" in n for n in notices)


def test_cli_base_equals_form_reaches_the_size_check(tmp_path, monkeypatch, capsys):
    """`--base=<sha>` must behave exactly like `--base <sha>` — a silently
    dropped flag would disarm the only enforcing size check with a green exit."""
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": BIG})
    verify_docs.write_nav(root)
    base = _commit(root)
    (root / "docs" / "architecture" / "turn-loop.md").write_text(BIG + "words\n")
    verify_docs.write_nav(root)
    _commit(root)
    monkeypatch.setattr(sys, "argv", ["verify_docs", str(root), f"--base={base}"])
    assert verify_docs.main() == 1
    assert "split it first" in capsys.readouterr().out


def test_cli_base_with_no_value_refuses(tmp_path, monkeypatch, capsys):
    root = _corpus(tmp_path)
    verify_docs.write_nav(root)
    monkeypatch.setattr(sys, "argv", ["verify_docs", str(root), "--base"])
    assert verify_docs.main() == 1
    assert "--base needs a value" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["verify_docs", str(root), "--base="])
    assert verify_docs.main() == 1
    assert "--base needs a value" in capsys.readouterr().out


def test_cli_base_flag_reaches_the_size_check(tmp_path, monkeypatch, capsys):
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": BIG})
    verify_docs.write_nav(root)
    base = _commit(root)
    (root / "docs" / "architecture" / "turn-loop.md").write_text(BIG + "words\n")
    verify_docs.write_nav(root)
    _commit(root)
    monkeypatch.setattr(sys, "argv", ["verify_docs", str(root), "--base", base])
    assert verify_docs.main() == 1
    assert "split it first" in capsys.readouterr().out


def test_cli_without_base_reports_and_passes(tmp_path, monkeypatch, capsys):
    root = _corpus(tmp_path, docs={"architecture/turn-loop.md": BIG})
    verify_docs.write_nav(root)
    monkeypatch.setattr(sys, "argv", ["verify_docs", str(root)])
    assert verify_docs.main() == 0
    assert "25 KB ceiling" in capsys.readouterr().out


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_impact_refuses_a_zero_byte_stdin():
    """A bare `--impact` (stdin /dev/null) used to print nothing and exit 0,
    recording an empty impact set as a clean one. Zero bytes is now a refusal
    that names the piped form."""
    r = subprocess.run(
        [sys.executable, "-m", "scripts.verify_docs", ".", "--impact"],
        cwd=REPO_ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "zero bytes" in r.stdout and "git diff --name-only" in r.stdout


def test_impact_accepts_an_empty_diff_piped_as_a_newline():
    """docs_impact.sh pipes at least one newline even when nothing changed —
    an empty DIFF must stay a clean empty impact set, not a refusal."""
    r = subprocess.run(
        [sys.executable, "-m", "scripts.verify_docs", ".", "--impact"],
        cwd=REPO_ROOT, input="\n", capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == ""



def test_engagements_is_named_when_tools_changes():
    """`engagements.md` already describes behaviour implemented in `tools.py` —
    a launch whose driver fails to start is marked errored, its topic cleanup
    attempted and its caller told — and its manifest row's `when_changing`
    names "engagement launch". Yet that row claimed no `tools.py` symbol, so a
    change to the launch-failure arms or to the silent topic close did not name
    the document that rules on them, and the ruling recorded there could go
    stale under a green corpus gate.
    """
    impact = sorted(verify_docs.impacted_docs(
        REPO_ROOT, ["casa/rootfs/opt/casa/tools.py"]))

    assert impact.count("architecture/engagements.md") == 1, impact


def test_engagement_finalization_is_named_when_job_registry_changes():
    """The terminal-authority ruling published in `engagement-finalization.md`
    is about BOTH persisting ledgers, and the job half is the one whose home is
    furthest from its subject. `related` links do not participate in impact
    resolution, so without a `covers` claim a job-terminal change could update
    or waive `jobs-and-delivery.md` alone while the ruling went stale.
    """
    impact = sorted(verify_docs.impacted_docs(
        REPO_ROOT, ["casa/rootfs/opt/casa/job_registry.py"]))

    assert impact.count("architecture/engagement-finalization.md") == 1, impact
    assert impact.count("architecture/jobs-and-delivery.md") == 1, impact
