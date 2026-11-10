"""The app's AppArmor profile: what it must keep granting Casa.

It must let Casa signal an engagement's processes, and it must keep its
network rules enforced without taking away a socket kind Casa uses.

#1382: Casa and s6 run as root, an engagement's CLI runs as its own uid, and
signalling another uid needs CAP_KILL. The profile did not grant it, so on
HAOS every kill of a running engagement was refused and swallowed; the quiesce
ladder could only report the uid NOT verified extinct. No unit or e2e run loads
the profile, so this pin is the only thing that notices the grant going away.
"""
from __future__ import annotations

import re
from pathlib import Path

PROFILE = Path(__file__).resolve().parents[1] / "casa" / "apparmor.txt"


def _rules() -> list[str]:
    return [line.split("#", 1)[0].strip()
            for line in PROFILE.read_text(encoding="utf-8").splitlines()]


def test_the_profile_lets_casa_signal_an_engagement_uid() -> None:
    rules = _rules()
    assert "capability kill," in rules
    # The signal rule must still allow the signals the ladder and s6 send.
    sig = [r for r in rules if r.startswith("signal")]
    assert sig, "the profile has no signal rule"
    allowed = set(re.findall(r"[a-z]+", sig[0].split("set=", 1)[1]))
    assert {"kill", "term", "cont"} <= allowed


def test_the_profile_enforces_network_rules_and_keeps_tcp_udp_open() -> None:
    # Without an abi line the kernel ignores every network rule, so the line
    # must stay first. With it, an inet, inet6 or netlink socket kind the
    # profile does not list is refused, so the kinds Casa needs must stay
    # listed: TCP and UDP over IPv4 and IPv6, and netlink raw for glibc's
    # address lookup inside getaddrinfo.
    lines = PROFILE.read_text(encoding="utf-8").splitlines()
    assert lines[0].strip() == "abi <abi/3.0>,"
    rules = set(_rules())
    for needed in ("network inet stream,", "network inet dgram,",
                   "network inet6 stream,", "network inet6 dgram,",
                   "network netlink raw,"):
        assert needed in rules, needed
    # A complain flag would log instead of refuse; the shipped profile enforces.
    header = next(r for r in _rules() if r.startswith("profile casa"))
    assert "complain" not in header


# --- the script runner's child profile -------------------------------------------------


def _child_rules() -> list[str]:
    """The rules between ``profile pyrun`` and its closing brace."""
    rules = _rules()
    start = next(i for i, r in enumerate(rules) if r.startswith("profile pyrun"))
    end = next(i for i in range(start + 1, len(rules)) if rules[i] == "}")
    return [r for r in rules[start + 1:end] if r]


def test_the_interpreter_copy_transitions_into_the_child_profile() -> None:
    """An exact-path cx rule outranks /opt/casa/** rix, so the one interpreter at that path
    runs under the child, and nothing else does."""
    rules = _rules()
    assert "/opt/casa/run/python3 cx -> pyrun," in rules
    assert "/opt/casa/** rix," in rules
    header = next(r for r in rules if r.startswith("profile pyrun"))
    assert "complain" not in header and "attach_disconnected" not in header


def test_the_child_profile_has_no_network_exec_mount_capability_or_write_rule() -> None:
    """The box's refusals rest on the absence of these rules: with the abi line a profile
    with no network rule refuses every socket family, no x rule means nothing runs, and
    no w anywhere means nothing is written. The prelude's intentional refusals must stay
    refused: never a rule for /etc/passwd, /proc/1/cmdline or a socket."""
    child = _child_rules()
    assert child, "no child profile"
    for rule in child:
        assert not rule.startswith(("network", "capability", "mount", "ptrace", "unix", "file,")), rule
        if rule.startswith("signal"):
            assert "receive" in rule and "send" not in rule
            continue
        perms = rule.rstrip(",").split()[-1]
        assert set(perms) <= set("rm"), rule
        assert "/etc/passwd" not in rule and "/proc/1/" not in rule
    assert "signal (receive) set=(kill, term)," in child


def test_the_child_profile_reads_only_the_interpreter_its_prelude_the_stdlib_and_public_data() -> None:
    child = _child_rules()
    paths = [r.split()[0] for r in child if r.startswith(("/", "@{"))]
    sentinel = [p for p in paths if p.startswith("/opt/casa/run/pyrun.")]
    assert len(sentinel) == 1 and re.fullmatch(r"/opt/casa/run/pyrun\.[0-9a-f]{12}", sentinel[0])
    assert set(paths) - set(sentinel) == {
        "/opt/casa/run/python3", "/opt/casa/run/child.py", "/etc/ld.so.cache",
        "/usr/lib/@{multiarch}/*.so*", "/usr/lib/python3.[0-9]*/", "/usr/lib/python3.[0-9]*/**",
        "/usr/lib/locale/**", "/usr/share/zoneinfo/**", "/etc/localtime", "/etc/ssl/openssl.cnf",
        "@{PROC}/@{pid}/attr/apparmor/current", "@{PROC}/@{pid}/attr/current",
        "@{PROC}/@{pid}/fd/"}
    for forbidden in ("/data", "/config", "/run/", "/opt/casa/**", "/proc/**", "/etc/**"):
        assert not any(p.startswith(forbidden) for p in paths), forbidden


def test_the_profile_compiles_and_names_exactly_the_two_profiles() -> None:
    """The parser, where the developer's machine has one, must accept the text with the
    abi it declares and list the parent and its one child."""
    import shutil
    import subprocess
    parser = shutil.which("apparmor_parser")
    base = Path("/etc/apparmor.d")
    if parser is None or not (base / "abi" / "3.0").exists():
        import pytest
        pytest.skip("no apparmor_parser with abi/3.0 on this machine")
    done = subprocess.run([parser, "-Q", "-N", "-M", str(base / "abi" / "3.0"), "--base", str(base),
                           str(PROFILE)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert sorted(done.stdout.split()) == ["casa", "casa//pyrun"]
