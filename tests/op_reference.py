"""Recorded outputs of the real `op` CLI, one case per command line.

Each case is a set of files in ``tests/fixtures/op_reference``:
``NN-name.args`` (one argument per line), ``.stdin`` (only when the command
read input), ``.out``, ``.err`` and ``.code`` (exit status).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "op_reference"


@dataclass(frozen=True)
class Case:
    name: str
    args: list[str]
    stdin: str | None
    out: str
    err: str
    code: int


def names() -> list[str]:
    return sorted(p.stem for p in FIXTURE_DIR.glob("*.args"))


def case(name: str) -> Case:
    def read(suffix: str) -> str:
        return (FIXTURE_DIR / f"{name}.{suffix}").read_text(encoding="utf-8")

    stdin_path = FIXTURE_DIR / f"{name}.stdin"
    return Case(
        name=name,
        args=read("args").splitlines(),
        stdin=stdin_path.read_text(encoding="utf-8") if stdin_path.exists() else None,
        out=read("out"),
        err=read("err"),
        code=int(read("code").strip()),
    )
