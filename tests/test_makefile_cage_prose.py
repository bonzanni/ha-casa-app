"""The Makefile's prose about the memory cage does not contradict the cage.

Nothing else in this repository reads the `Makefile` for factual claims. No
`covers` anchor in `docs/manifest.yaml` or any `docs/manifest.d` shard names it,
`docs/coverage.yaml` does not list it, `scripts/verify_docs.py`,
`scripts/coverage_ledger.py` and `scripts/docs_impact.sh` contain no `Makefile`
token, and `docs.yml`'s only repo-root step reads `CLAUDE.md` and `AGENTS.md`.
So a false statement in that file has no detector, which is how issue #906's
three false claims survived.

These two tests are a BOUNDED REGRESSION DETECTOR, not a proof that the file's
prose is correct. They close exactly the three historical false claims, each
against a fact DERIVED from the file (or from the repository) rather than
restated here:

* `test_makefile_does_not_promise_an_uncaged_fallback` derives that the suite
  entry points reach a `$(error ...)` when the cage is unavailable, then
  requires that the file no longer tells the reader they degrade to running
  uncaged instead.
* `test_makefile_names_no_caller_that_sets_the_uncaged_opt_out` derives that no
  tracked file outside this `Makefile` sets `CASA_ALLOW_UNCAGED`, then requires
  that the file no longer names CI as a caller that does.

What they deliberately do NOT do: recognise a novel contradictory paraphrase.
A block-scoped word-coexistence predicate was specified and rejected — measured
on the pre-fix file it flagged the historical `LOCK` block, which describes a
past failure rather than promising a present fallback, and it was cleared by
appending a refusal word without correcting anything. Closing the grammar over
the specific retired claims buys an honest regression detector; it does not buy
a universal guarantee, and nothing here should be read as claiming one.

Where a derivation cannot be made — the guard's shape is not recognised, no
suite recipe reaches it — these tests FAIL with a diagnostic rather than
silently passing with the assertion disabled.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"

# The claims retired by the change that added this file, normalised exactly as
# `_prose()` normalises the file: whitespace collapsed, comment markers gone.
# Re-wrapping any of them still matches; correcting them does not.
RETIRED_FALLBACK_CLAIM = (
    "Degrades to running uncaged where systemd-run is absent (CI images)."
)
RETIRED_CALLER_CLAIMS = (
    "(CI does this)",
    "CI images legitimately have no user bus and opt out explicitly rather "
    "than degrading silently.",
)

# A real assignment, not a mention. `CASA_ALLOW_UNCAGED=1` inside a sentence is
# prose; these are the forms that actually put it in a callee's environment.
SETTER_RE = re.compile(
    r"(?:^|[;&|]|\bexport\s+|\benv\s+)\s*CASA_ALLOW_UNCAGED\s*(?::|\??\+?=)")

# Directories that cannot contain a caller: build output, VCS internals, and
# `tests/` itself. Test fixtures must never count as evidence that something
# sets the opt-out — a string literal in this very file would otherwise make
# the caller assertion vacuous.
SKIP_DIRS = {".git", "tests", "venv_test", "venv", "__pycache__",
             "node_modules", ".pytest_cache", ".mypy_cache"}


def _logical_lines(text: str) -> list[str]:
    """Backslash continuations joined, so a wrapped definition is one line."""
    out, buf = [], ""
    for line in text.splitlines():
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _collapse(s: str) -> str:
    return " ".join(s.split())


def _prose(text: str) -> str:
    """Every comment in the file, joined and whitespace-collapsed.

    Re-wrapping a sentence across different line boundaries leaves this
    unchanged, which is the point: the pin is over the claim, not the layout.
    """
    said = [ln.lstrip()[1:].strip()
            for ln in text.splitlines() if ln.lstrip().startswith("#")]
    return _collapse(" ".join(said))


def _call_args(expr: str) -> tuple[str, list[str]]:
    """Split one `$(fn arg,arg,...)` into its name and top-level arguments.

    Commas nested inside a `$(...)` are not separators. Returns ("", []) when
    `expr` is not a single balanced call, so callers can report the shape they
    did not recognise instead of guessing.
    """
    expr = expr.strip()
    if not (expr.startswith("$(") and expr.endswith(")")):
        return "", []
    depth, body = 0, expr[2:-1]
    for i, ch in enumerate(expr):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and i != len(expr) - 1:
                return "", []          # not a single call
        if depth < 0:
            return "", []              # unbalanced
    head, _, rest = body.partition(" ")
    args, depth, cur = [], 0, ""
    for ch in rest:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur)
            cur = ""
            continue
        cur += ch
    args.append(cur)
    return head, args


def _definition(lines: list[str], name: str) -> tuple[str, str]:
    """The operator and right-hand side of `name`'s definition."""
    pat = re.compile(rf"^{re.escape(name)}\s*(=|:=|::=|\?=|\+=)\s*(.*)$")
    for ln in lines:
        m = pat.match(ln)
        if m:
            return m.group(1), m.group(2)
    return "", ""


def _guard_error_text() -> str:
    """The `$(error ...)` message CAGE_GUARD raises, structurally derived.

    Every step is asserted rather than pattern-sniffed: finding the token
    `$(error` somewhere in the file would be satisfied by a comment.
    """
    lines = _logical_lines(MAKEFILE.read_text())
    op, body = _definition(lines, "CAGE_GUARD")
    assert op == "=", (
        "CAGE_GUARD must stay a RECURSIVE (`=`) variable so its $(error ...) "
        f"fires at use and not at parse (a `:=` aborts `make help`); found {op!r}")
    fn, args = _call_args(body)
    assert (fn, len(args)) == ("if", 3), (
        "CAGE_GUARD's shape is not recognised, so this test cannot tell "
        f"whether the suite refuses or degrades: {body!r}")
    assert _collapse(args[0]) == "$(CAGE)" and _collapse(args[1]) == "", (
        f"CAGE_GUARD's outer $(if ...) does not test CAGE: {args[:2]!r}")
    fn, inner = _call_args(args[2])
    assert (fn, len(inner)) == ("if", 3), (
        f"CAGE_GUARD's inner conditional is not recognised: {args[2]!r}")
    assert _collapse(inner[0]) == "$(CASA_ALLOW_UNCAGED)" and _collapse(inner[1]) == "", (
        f"CAGE_GUARD's inner $(if ...) does not test the opt-out: {inner[:2]!r}")
    fn, msg = _call_args(inner[2])
    assert fn == "error", (
        "CAGE_GUARD does not raise when the cage is unavailable and the "
        f"opt-out is unset — it expands to {inner[2]!r}, so nothing refuses")
    return _collapse(",".join(msg))


def _targets_reaching_the_guard() -> set[str]:
    """Targets whose recipe expands $(SUITE), and so the guard."""
    text = MAKEFILE.read_text()
    op, suite = _definition(_logical_lines(text), "SUITE")
    assert op and "$(CAGE_GUARD)" in suite, (
        "SUITE does not interpolate CAGE_GUARD, so expanding it no longer "
        f"reaches the refusal: {suite!r}")
    targets, current = set(), None
    for ln in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", ln)
        if m:
            current = m.group(1)
        elif ln.startswith("\t") and current and "$(SUITE)" in ln:
            targets.add(current)
    return targets


def _opt_out_setters() -> list[str]:
    """Tracked paths outside this Makefile that really set the opt-out."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                             capture_output=True, text=True, timeout=120)
        paths = [p for p in out.stdout.split("\0") if p] if out.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        paths = []
    if not paths:                      # no git available: walk the tree instead
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                paths.append(str(Path(dirpath, fn).relative_to(ROOT)))
    found = []
    for rel in paths:
        parts = Path(rel).parts
        if rel == "Makefile" or (parts and parts[0] in SKIP_DIRS):
            continue
        p = ROOT / rel
        try:
            if not p.is_file() or p.stat().st_size > 512_000:
                continue
            text = p.read_text(errors="replace")
        except OSError:
            continue
        if any(SETTER_RE.search(ln) for ln in text.splitlines()):
            found.append(rel)
    return sorted(found)


def test_makefile_does_not_promise_an_uncaged_fallback():
    """Red case demonstrated: restoring `Makefile`'s retired sentence
    "Degrades to running uncaged where systemd-run is absent (CI images)."
    fails this test, because the derivation below shows the suite entry points
    refuse instead."""
    _guard_error_text()                     # derives that something refuses
    reached = _targets_reaching_the_guard()
    assert {"test-unit", "test-unit-serial"} <= reached, (
        "the documented suite targets no longer expand $(SUITE), so this test "
        f"cannot show that they refuse; reached: {sorted(reached)}")
    prose = _prose(MAKEFILE.read_text())
    assert RETIRED_FALLBACK_CLAIM not in prose, (
        "Makefile tells the reader the cage degrades to an uncaged run, while "
        f"{sorted(reached)} expand a CAGE_GUARD that raises: "
        f"{RETIRED_FALLBACK_CLAIM!r}")


def test_makefile_names_no_caller_that_sets_the_uncaged_opt_out():
    """Red case demonstrated: restoring either retired CI attribution — the
    "(CI does this)" tail of CAGE_GUARD's error text, or the comment sentence
    above it — fails this test, because no tracked file outside the Makefile
    sets CASA_ALLOW_UNCAGED."""
    setters = _opt_out_setters()
    assert setters == [], (
        "something in the repository now sets CASA_ALLOW_UNCAGED: "
        f"{setters}. That may be legitimate — but the Makefile's prose about "
        "who opts out was written when nothing did, so re-read it before "
        "widening this test")
    haystack = _prose(MAKEFILE.read_text()) + " " + _guard_error_text()
    named = [c for c in RETIRED_CALLER_CLAIMS if c in haystack]
    assert named == [], (
        "Makefile names a caller that sets CASA_ALLOW_UNCAGED while no tracked "
        f"file outside it does: {named}")
