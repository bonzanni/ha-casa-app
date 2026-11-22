"""Sanity checks on the recorded `op` reference outputs."""
import json
import re
from pathlib import Path

import op_reference
from op_reference import FIXTURE_DIR

REPO = Path(__file__).resolve().parent.parent


def test_every_case_has_args_and_code():
    names = op_reference.names()
    assert names
    for name in names:
        for suffix in ("args", "code", "out", "err"):
            assert (FIXTURE_DIR / f"{name}.{suffix}").is_file(), (name, suffix)
        assert isinstance(op_reference.case(name).code, int)


def test_no_real_key_material():
    run = re.compile(r"[A-Za-z0-9+/=]{43,}")
    for path in FIXTURE_DIR.iterdir():
        assert not run.search(path.read_text(encoding="utf-8")), path.name


def test_json_outputs_parse():
    parsed = 0
    for path in FIXTURE_DIR.glob("*.out"):
        text = path.read_text(encoding="utf-8")
        if text.startswith(("[", "{")):
            json.loads(text)
            parsed += 1
    assert parsed


def test_the_recorded_version_is_the_pinned_one():
    dockerfile = (REPO / "casa" / "Dockerfile").read_text(encoding="utf-8")
    pinned = re.search(r"^ARG OP_CLI_VERSION=(\S+)$", dockerfile, re.M).group(1)
    assert op_reference.case("01-version").out.strip() == pinned
