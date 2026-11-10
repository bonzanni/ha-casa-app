"""The script runner's profile digest (``scripts/pyrun_digest.py``): the sentinel the box
opens is named by the child profile text's own digest, and ``check`` refuses every way the
three copies of that value can come apart.

The fixture texts here stand in for ``casa/apparmor.txt``; the tree's own files are checked
by ``tests/test_run_child.py``.
"""
from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "casa" / "rootfs" / "opt" / "casa" / "scripts" / "pyrun_digest.py"

_spec = importlib.util.spec_from_file_location("pyrun_digest", SCRIPT)
pyrun_digest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pyrun_digest)

CHILD_BODY = """  profile pyrun flags=(mediate_deleted) {
    /opt/casa/run/python3 rm,
    /opt/casa/run/child.py r,
    /etc/ld.so.cache r,
    /usr/lib/@{multiarch}/*.so* rm,
    /usr/lib/python3.[0-9]*/** rm,
    @{PROC}/@{pid}/attr/apparmor/current r,
    @{PROC}/@{pid}/fd/ r,
    signal (receive) set=(kill, term),
  }
"""
PARENT_HEAD = """abi <abi/3.0>,

#include <tunables/global>

profile casa flags=(attach_disconnected,mediate_deleted) {
  file,
  /opt/casa/** rix,
  /opt/casa/run/python3 cx -> pyrun,

"""
PARENT_TAIL = "}\n"


def _expected(child_body: str = CHILD_BODY) -> str:
    return hashlib.sha256(child_body.encode()).hexdigest()[:12]


def _profile(digest: str, child_body: str = CHILD_BODY, extra: str = "") -> str:
    sentinel = f"    /opt/casa/run/pyrun.{digest} r,\n"
    body = child_body.replace("    /etc/ld.so.cache r,\n", sentinel + extra + "    /etc/ld.so.cache r,\n")
    return PARENT_HEAD + body + PARENT_TAIL


def _child_py(digest: str) -> str:
    return f'import os\nPROFILE_DIGEST = "{digest}"\nSENTINEL = "/opt/casa/run/pyrun." + PROFILE_DIGEST\n'


def test_the_digest_is_sha256_of_the_child_text_without_its_sentinel_line():
    """The convention: the lines from ``profile pyrun`` to its closing brace, newlines
    kept, the one sentinel line left out, first 12 hex of sha256; and the value is the same
    whether the text carries the sentinel line yet or not."""
    expected = _expected()
    assert pyrun_digest.digest(_profile(expected)) == expected
    assert pyrun_digest.digest(_profile("000000000000")) == expected
    assert pyrun_digest.digest(PARENT_HEAD + CHILD_BODY + PARENT_TAIL) == expected
    assert len(expected) == 12


def test_the_closing_newline_is_part_of_the_text():
    """Red case for the convention: a digest over the text without its final newline is
    another value, which is why the convention lives in one script."""
    expected = _expected()
    assert pyrun_digest.digest(PARENT_HEAD + CHILD_BODY.rstrip("\n") + "\n" + PARENT_TAIL) == expected
    assert hashlib.sha256(CHILD_BODY.rstrip("\n").encode()).hexdigest()[:12] != expected


def test_only_the_child_block_is_digested():
    """A change to the parent profile moves nothing; a change to any child line does."""
    expected = _expected()
    assert pyrun_digest.digest(_profile(expected).replace("  file,\n", "  file,\n  /x r,\n")) == expected
    changed = CHILD_BODY.replace("signal (receive) set=(kill, term)", "signal (receive) set=(kill)")
    assert pyrun_digest.digest(_profile(expected, changed)) != expected


def test_check_passes_when_the_three_agree():
    expected = _expected()
    assert pyrun_digest.check(_profile(expected), _child_py(expected)) == expected


def test_check_refuses_a_child_text_edited_without_regenerating():
    expected = _expected()
    edited = CHILD_BODY.replace("/etc/ld.so.cache r,", "/etc/ld.so.cache r,\n    /data/** r,")
    with pytest.raises(pyrun_digest.DigestError, match="sentinel rule names"):
        pyrun_digest.check(_profile(expected, edited), _child_py(expected))


def test_check_refuses_a_child_py_constant_that_differs():
    expected = _expected()
    with pytest.raises(pyrun_digest.DigestError, match="PROFILE_DIGEST is"):
        pyrun_digest.check(_profile(expected), _child_py("0123456789ab"))
    with pytest.raises(pyrun_digest.DigestError, match="no PROFILE_DIGEST"):
        pyrun_digest.check(_profile(expected), "import os\n")


def test_check_refuses_a_rule_that_names_another_file():
    expected = _expected()
    with pytest.raises(pyrun_digest.DigestError, match="sentinel rule names"):
        pyrun_digest.check(_profile("0123456789ab"), _child_py(expected))
    with pytest.raises(pyrun_digest.DigestError, match="0 sentinel rules"):
        pyrun_digest.check(PARENT_HEAD + CHILD_BODY + PARENT_TAIL, _child_py(expected))


def test_check_refuses_two_sentinel_rules():
    expected = _expected()
    text = _profile(expected, extra="    /opt/casa/run/pyrun.0123456789ab r,\n")
    with pytest.raises(pyrun_digest.DigestError, match="2 sentinel lines"):
        pyrun_digest.check(text, _child_py(expected))


@pytest.mark.parametrize("rule", [
    "/opt/casa/run/** r,",
    "/opt/casa/run/pyrun.* r,",
    "/opt/casa/run/pyrun.{a,b} r,",
    "/opt/casa/run/pyrun.ee2f3570?b81 r,",
    "/opt/casa/run/pyrun.ee2f35704b8[0-9] r,",
    "/opt/casa/** r,",
    "/** r,",
    "/opt/casa/run/py* r,",
    "/opt/casa/run/@{x} r,",
    "@{x}/pyrun.0 r,",
    "owner /opt/casa/run/** r,",
    "/opt/casa/run/pyrun.0123456789abc r,",
])
def test_check_refuses_any_other_rule_whose_path_can_match_a_sentinel(rule):
    """Red case for the stale-child guard: with such a rule in the text, every later
    sentinel would open under a child of this text, and the digest would prove nothing."""
    expected = pyrun_digest.digest(_profile("000000000000", extra=f"    {rule}\n"))
    text = _profile(expected, extra=f"    {rule}\n")
    with pytest.raises(pyrun_digest.DigestError, match="can match /opt/casa/run/pyrun"):
        pyrun_digest.check(text, _child_py(expected))


@pytest.mark.parametrize("rule", [
    "/opt/casa/run/python3 rm,",
    "/opt/casa/run/child.py r,",
    "/opt/casa/runs/** r,",
    "/opt/casa/ru r,",
    "@{PROC}/@{pid}/fd/ r,",
    "@{PROC}/** r,",
    "/usr/lib/@{multiarch}/*.so* rm,",
    "signal (receive) set=(kill, term),",
    "network inet stream,",
])
def test_check_accepts_rules_that_cannot_name_a_sentinel(rule):
    expected = pyrun_digest.digest(_profile("000000000000", extra=f"    {rule}\n"))
    text = _profile(expected, extra=f"    {rule}\n")
    assert pyrun_digest.check(text, _child_py(expected)) == expected


def test_a_text_with_no_child_block_or_an_unclosed_one_is_refused():
    with pytest.raises(pyrun_digest.DigestError, match="no 'profile pyrun'"):
        pyrun_digest.digest(PARENT_HEAD + PARENT_TAIL)
    with pytest.raises(pyrun_digest.DigestError, match="never closes"):
        pyrun_digest.digest(PARENT_HEAD + CHILD_BODY.replace("  }\n", ""))


def test_the_command_line_prints_and_checks(tmp_path):
    expected = _expected()
    profile = tmp_path / "apparmor.txt"
    child = tmp_path / "child.py"
    profile.write_text(_profile(expected))
    child.write_text(_child_py(expected))
    out = subprocess.run([sys.executable, str(SCRIPT), "print", str(profile)],
                         capture_output=True, text=True)
    assert (out.returncode, out.stdout) == (0, expected + "\n")
    out = subprocess.run([sys.executable, str(SCRIPT), "check", str(profile), str(child)],
                         capture_output=True, text=True)
    assert out.returncode == 0 and expected in out.stdout
    child.write_text(_child_py("0123456789ab"))
    out = subprocess.run([sys.executable, str(SCRIPT), "check", str(profile), str(child)],
                         capture_output=True, text=True)
    assert out.returncode == 1 and "PROFILE_DIGEST is 0123456789ab" in out.stderr
    out = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert out.returncode == 2
    out = subprocess.run([sys.executable, str(SCRIPT), "print", str(tmp_path / "missing")],
                         capture_output=True, text=True)
    assert out.returncode == 1
