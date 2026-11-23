"""Matcher for CC CLI ``tools.allowed`` pattern strings.

Pattern grammar (mirrors CC CLI 2.1.x):
  * Bare tool name (``Read``, ``mcp__casa-framework__memory_read``):
    matches any invocation of that tool, ignoring input.
  * ``ToolName(spec)``: matches iff tool name == ToolName AND ``spec``
    matches the tool's identifying input field, using ``fnmatch`` glob
    semantics (``*`` and ``?``):
      - Bash:                 spec matches ``tool_input.command``
      - Edit/Write/Read/Glob/Grep: spec matches ``tool_input.file_path``
        (Glob falls back to ``tool_input.pattern`` when no file_path).
      - Anything else:        no spec support (bare name only)

This matcher is best-effort. Casa's allow-list semantics mirror CC's by
convention. Edge cases that diverge produce safe behaviour:
  * Under-match (Casa says NOT allowed, CC would auto-approve):
    operator sees redundant Telegram button.
  * Over-match (Casa says allowed, CC would gate): the
    ``disallowed_tools`` deny rule still applies after the hook.

See spec ``2026-05-13-c1-permission-relay-fix.md`` §4.1.
"""

from __future__ import annotations

import fnmatch
import re

# A pattern is either ``ToolName`` (no parens) or ``ToolName(spec)``.
_SPEC_RE = re.compile(r"^([A-Za-z0-9_]+(?:__[A-Za-z0-9_-]+)*)(?:\((.*)\))?$")


def matches(pattern: str, tool_name: str, tool_input: dict) -> bool:
    """Return True iff (tool_name, tool_input) satisfies ``pattern``."""
    m = _SPEC_RE.match(pattern or "")
    if not m:
        return False
    pat_tool, pat_spec = m.group(1), m.group(2)
    if pat_tool != tool_name:
        return False
    if pat_spec is None:
        return True  # bare match — ignore input
    target = _identifying_field(tool_name, tool_input or {})
    if target is None:
        return False
    return fnmatch.fnmatchcase(target, pat_spec)


def _identifying_field(tool_name: str, tool_input: dict) -> str | None:
    """Return the tool's primary spec-matching field, or None if unsupported."""
    if tool_name == "Bash":
        v = tool_input.get("command")
        return v if isinstance(v, str) else None
    if tool_name in ("Edit", "Write", "Read", "Glob", "Grep"):
        v = tool_input.get("file_path") or tool_input.get("pattern")
        return v if isinstance(v, str) else None
    return None
