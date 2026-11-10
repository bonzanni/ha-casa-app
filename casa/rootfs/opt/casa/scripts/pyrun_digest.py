#!/usr/bin/env python3
"""The script runner's profile digest: the one place its convention lives.

The runner's AppArmor child profile ``pyrun`` (``casa/apparmor.txt``; the image keeps a
copy at ``/etc/casa-apparmor.txt``) names a sentinel file, ``/opt/casa/run/pyrun.<digest>``,
that is the only file of that name the profile lets the box read. The digest is the first 12
hex digits of sha256 over the child profile's text, from its ``profile pyrun`` line to the
``}`` that closes it, each line with its newline, with the sentinel's own line left out. The
Dockerfile creates the file at build time from ``print``; ``child.py`` carries the value as
``PROFILE_DIGEST`` and opens the file before it runs anything; so a child profile of any
other text loaded under the same label, wider or narrower, refuses the box.

``check`` holds the three together: the sentinel rule in the child text, ``child.py``'s
constant and the text's digest must agree, exactly one sentinel rule must exist, and no
other rule of the child text may have a path that can match ``/opt/casa/run/pyrun.*`` (a
``/opt/casa/run/** r,`` or ``pyrun.* r,`` rule would let every later sentinel open under a
stale child, and nothing could tell).

    pyrun_digest.py print <apparmor.txt>
    pyrun_digest.py check <apparmor.txt> <child.py>

Stdlib only: the Dockerfile runs it with the image's system interpreter.
"""
from __future__ import annotations

import hashlib
import re
import sys

SENTINEL_DIR = "/opt/casa/run/pyrun."
_PROFILE_LINE = re.compile(r"^\s*profile\s+pyrun\b")
_SENTINEL_LINE = re.compile(r"^\s*/opt/casa/run/pyrun\.([0-9a-f]{12}) r,\s*$")
_CONSTANT = re.compile(r'^PROFILE_DIGEST = "([0-9a-f]{12})"', re.MULTILINE)
_QUALIFIERS = ("deny", "audit", "owner", "allow")
# Tunables the child text may use before a literal path. Anything else in ``@{...}`` is
# treated as matching anything, which is the conservative reading.
_TUNABLES = {"@{PROC}": "/proc/", "@{sys}": "/sys/"}
_META = set("*?[{@")


class DigestError(Exception):
    """The texts disagree, or the convention is broken; the message says where."""


def child_lines(text: str) -> list[str]:
    """The child profile's lines, ``profile pyrun`` to its closing brace, each with its
    newline; the sentinel line, when one is present, is among them."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if _PROFILE_LINE.match(line)), None)
    if start is None:
        raise DigestError("no 'profile pyrun' block in the profile text")
    depth = 0
    for i in range(start, len(lines)):
        code = lines[i].split("#", 1)[0]
        depth += code.count("{") - code.count("}")
        if depth == 0:
            block = lines[start:i + 1]
            if not block[-1].endswith("\n"):
                block[-1] += "\n"
            return block
    raise DigestError("the 'profile pyrun' block never closes")


def digest(text: str) -> str:
    """The digest of the child profile in *text*: 12 hex of sha256 over its lines without
    the sentinel line. A text with two sentinel lines is refused, since one would be
    digested as an ordinary rule."""
    lines = child_lines(text)
    sentinels = [line for line in lines if _SENTINEL_LINE.match(line)]
    if len(sentinels) > 1:
        raise DigestError(f"{len(sentinels)} sentinel lines in the child profile; one at most")
    body = "".join(line for line in lines if not _SENTINEL_LINE.match(line))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _rule_path(line: str) -> str | None:
    """The path a file rule names, or None for any other line."""
    code = line.split("#", 1)[0].strip()
    tokens = code.split()
    while tokens and tokens[0] in _QUALIFIERS:
        tokens = tokens[1:]
    if not tokens:
        return None
    path = tokens[0]
    return path if path.startswith("/") or path.startswith("@{") else None


def _literal_prefix(path: str) -> tuple[str, bool]:
    """The characters a rule's path matches literally before its first glob or variable,
    and whether it has any."""
    for name, value in _TUNABLES.items():
        path = path.replace(name, value)
    for i, ch in enumerate(path):
        if ch in _META:
            return path[:i], True
    return path, False


def can_match_sentinel(path: str) -> bool:
    """Whether a rule's *path* can match some ``/opt/casa/run/pyrun.*``: a plain path when
    it is one; a pattern when its literal prefix and the sentinel prefix each start with
    the other."""
    prefix, pattern = _literal_prefix(path)
    if not pattern:
        return prefix.startswith(SENTINEL_DIR)
    return SENTINEL_DIR.startswith(prefix) or prefix.startswith(SENTINEL_DIR)


def check(profile_text: str, child_source: str) -> str:
    """The digest when the three agree; raises DigestError naming the disagreement."""
    value = digest(profile_text)
    lines = child_lines(profile_text)
    sentinels = [m.group(1) for m in map(_SENTINEL_LINE.match, lines) if m]
    if len(sentinels) != 1:
        raise DigestError(f"the child profile has {len(sentinels)} sentinel rules; it needs "
                          f"exactly one: /opt/casa/run/pyrun.{value} r,")
    if sentinels[0] != value:
        raise DigestError(f"the sentinel rule names pyrun.{sentinels[0]} but the child text's "
                          f"digest is {value}: regenerate the rule")
    for line in lines:
        if _SENTINEL_LINE.match(line):
            continue
        path = _rule_path(line)
        if path is not None and can_match_sentinel(path):
            raise DigestError(f"rule {line.strip()!r} can match {SENTINEL_DIR}*: only the "
                              "exact sentinel rule may")
    found = _CONSTANT.search(child_source)
    if found is None:
        raise DigestError('child.py has no PROFILE_DIGEST = "<12 hex>" line')
    if found.group(1) != value:
        raise DigestError(f"child.py's PROFILE_DIGEST is {found.group(1)} but the child "
                          f"text's digest is {value}: regenerate the constant")
    return value


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[1] == "print":
            with open(argv[2], encoding="utf-8") as fh:
                print(digest(fh.read()))
            return 0
        if len(argv) == 4 and argv[1] == "check":
            with open(argv[2], encoding="utf-8") as fh:
                profile_text = fh.read()
            with open(argv[3], encoding="utf-8") as fh:
                child_source = fh.read()
            print(f"pyrun digest {check(profile_text, child_source)}: the profile, the rule "
                  "and child.py agree")
            return 0
    except (OSError, DigestError) as exc:
        print(f"pyrun_digest: {exc}", file=sys.stderr)
        return 1
    print(__doc__.split("\n\n")[-2], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
