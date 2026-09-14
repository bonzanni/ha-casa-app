"""Guard: the release build's launch-path exec probe names exactly the programs
an engagement launch executes (#957).

`casa/rootfs/opt/casa/drivers/s6_rc.py` runs every s6 program by BARE NAME
through `subprocess.run`, directly or via `asyncio.to_thread(subprocess.run,
[...])`. `casa/Dockerfile` carries one build-time probe that launches each named
program with that same primitive in the image's own `python3`, and fails the
build when a launch raises. The build runs natively in every arm of
`deploy.yml`'s matrix, so an image in which one of these programs cannot be
launched on one architecture is refused before anything publishes.

This test keeps the probe's list equal to the call sites'. A guard naming one
program leaves the others, which is how #957 was filed. Adding a sixth program
to the driver without extending the probe turns this red.

**What this asserts and what it cannot.** It is a TEXT pin of list equality and
uniqueness. It does not establish that a launched program then works: a program
that starts and exits non-zero passes the probe. It does not establish anything
about an image as built on any architecture; that is what a lane runs, not what
a test asserts. It does not reach programs outside `drivers/s6_rc.py` either:
the run template's `setpriv`/`claude`, the `with-contenv`/`bashio` shebangs, and
the base image's own boot programs.

Nor does the probe it pins establish that a runtime launch resolves these bare
names to the programs the image installed: `setup-configs.sh` prepends
`/config/tools/bin`, which an installed plugin publishes binaries into, ahead of
the entire image PATH for every s6-supervised service (#987). That is a property
of the running system rather than of the image, so the probe neither covers it
nor is weakened by it.
"""
from __future__ import annotations

import ast
import shlex
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "casa" / "rootfs" / "opt" / "casa" / "drivers" / "s6_rc.py"
DOCKERFILE = REPO / "casa" / "Dockerfile"

# The probe's Python program, compared by AST so that spacing is free and the
# mechanism is not: a bash `"$prog"` loop, or a `test -x`, is not this probe.
PROBE_CODE = (
    "import subprocess, sys; "
    "[subprocess.run([p], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
    "stderr=subprocess.DEVNULL) for p in sys.argv[1:]]"
)


def _is_subprocess_run(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "run"
            and isinstance(node.value, ast.Name) and node.value.id == "subprocess")


def _is_to_thread(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "to_thread"
            and isinstance(node.value, ast.Name) and node.value.id == "asyncio")


def _argv_of(call: ast.Call, positional: int) -> ast.AST | None:
    if len(call.args) > positional:
        return call.args[positional]
    for kw in call.keywords:
        if kw.arg == "args":
            return kw.value
    return None


def driver_programs(source: str) -> tuple[list[str], int, int, list[str]]:
    """argv[0] of every `subprocess.run` call, in both call forms.

    Returns (programs, direct_count, threaded_count, unsupported). A call whose
    argv is not a literal list/tuple starting with a bare-name string literal is
    reported as unsupported rather than skipped: an extractor that silently
    reaches nothing is a test that pins nothing.
    """
    programs: list[str] = []
    unsupported: list[str] = []
    direct = threaded = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if _is_subprocess_run(node.func):
            argv = _argv_of(node, 0)
            direct += 1
        elif (_is_to_thread(node.func) and node.args
              and _is_subprocess_run(node.args[0])):
            argv = _argv_of(node, 1)
            threaded += 1
        else:
            continue
        first = (argv.elts[0] if isinstance(argv, (ast.List, ast.Tuple))
                 and argv.elts else None)
        if (isinstance(first, ast.Constant) and isinstance(first.value, str)
                and first.value and "/" not in first.value):
            programs.append(first.value)
        else:
            unsupported.append(f"line {node.lineno}")
    return programs, direct, threaded, unsupported


def _logical_instructions(text: str) -> list[str]:
    """Dockerfile instructions with backslash continuations joined; blank lines
    and whole-line comments are dropped, as the Dockerfile parser drops them."""
    out: list[str] = []
    current = ""
    for raw in text.split("\n"):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw.rstrip().endswith("\\"):
            current += raw.rstrip()[:-1] + " "
            continue
        out.append(current + raw)
        current = ""
    if current:
        out.append(current)
    return out


def _same_program(code: str) -> bool:
    try:
        return ast.dump(ast.parse(code)) == ast.dump(ast.parse(PROBE_CODE))
    except SyntaxError:
        return False


def probe_instructions(dockerfile_text: str) -> list[list[str]]:
    """For every RUN instruction that invokes `python3 -c <PROBE_CODE>`, the
    program arguments after the code and before the `||` failure handler."""
    found: list[list[str]] = []
    for instr in _logical_instructions(dockerfile_text):
        parts = instr.strip().split(None, 1)
        if len(parts) != 2 or parts[0].upper() != "RUN":
            continue
        lexer = shlex.shlex(parts[1], posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
        for i in range(len(tokens) - 2):
            if (tokens[i] == "python3" and tokens[i + 1] == "-c"
                    and _same_program(tokens[i + 2])):
                args: list[str] = []
                for tok in tokens[i + 3:]:
                    if tok == "||":
                        break
                    args.append(tok)
                found.append(args)
    return found


def test_launch_probe_matches_driver_programs() -> None:
    programs, direct, threaded, unsupported = driver_programs(
        DRIVER.read_text(encoding="utf-8"))
    assert direct > 0, "extractor reached no direct subprocess.run call"
    assert threaded > 0, "extractor reached no asyncio.to_thread(subprocess.run) call"
    assert unsupported == [], (
        f"subprocess.run argv this pin cannot read: {unsupported}")

    probes = probe_instructions(DOCKERFILE.read_text(encoding="utf-8"))
    assert len(probes) == 1, (
        f"casa/Dockerfile has {len(probes)} launch-path exec probe instructions; "
        "expected exactly one")
    listed = probes[0]
    assert len(listed) > 0, listed
    assert len(listed) == len(set(listed)), f"duplicate names in the probe: {listed}"
    assert set(listed) == set(programs), (
        f"probe lists {sorted(set(listed))}, drivers/s6_rc.py executes "
        f"{sorted(set(programs))}")
