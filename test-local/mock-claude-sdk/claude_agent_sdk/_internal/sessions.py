"""Offline shim for ``claude_agent_sdk._internal.sessions``.

Casa's engagement transcript reaper imports ``_canonicalize_path`` and
``_find_project_dir`` from here (lazily, per pass). This file is a verbatim
copy of those two functions and the helpers they call, taken from the REAL
claude-agent-sdk 0.2.153 (the version casa/requirements.txt pins), so the e2e
image resolves the same project dir the production SDK does and the reaper
really runs there. Re-sync it when the SDK pin moves:
tests/test_mock_sdk_surface.py checks the names resolve, and
tests/test_engagement_transcript_reaper_pins.py checks the mapping still
agrees with the real SDK's public ``project_key_for_directory``.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

# Maximum length for a single filesystem path component. Most filesystems
# limit individual components to 255 bytes. We use 200 to leave room for
# the hash suffix and separator.
MAX_SANITIZED_LENGTH = 200

_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")


def _simple_hash(s: str) -> str:
    """32-bit integer hash to base36, matching the CLI's directory naming."""
    h = 0
    for ch in s:
        char = ord(ch)
        h = (h << 5) - h + char
        # Emulate JS `hash |= 0` (coerce to 32-bit signed int)
        h = h & 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    h = abs(h)
    # JS toString(36)
    if h == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    n = h
    while n > 0:
        out.append(digits[n % 36])
        n //= 36
    return "".join(reversed(out))


def _sanitize_path(name: str) -> str:
    """Makes a string safe for use as a directory name.

    Replaces all non-alphanumeric characters with hyphens. For paths
    exceeding MAX_SANITIZED_LENGTH, truncates and appends a hash suffix.
    """
    sanitized = _SANITIZE_RE.sub("-", name)
    if len(sanitized) <= MAX_SANITIZED_LENGTH:
        return sanitized
    h = _simple_hash(name)
    return f"{sanitized[:MAX_SANITIZED_LENGTH]}-{h}"


def _get_claude_config_home_dir() -> Path:
    """Returns the Claude config directory (respects CLAUDE_CONFIG_DIR)."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(unicodedata.normalize("NFC", config_dir))
    return Path(unicodedata.normalize("NFC", str(Path.home() / ".claude")))


def _get_projects_dir(env_override: dict[str, str] | None = None) -> Path:
    """Returns the projects directory.

    ``env_override`` is consulted before ``os.environ`` so callers that pass
    ``CLAUDE_CONFIG_DIR`` to the subprocess via ``options.env`` resolve the
    same directory the subprocess will write to.
    """
    if env_override:
        override = env_override.get("CLAUDE_CONFIG_DIR")
        if override:
            return Path(unicodedata.normalize("NFC", override)) / "projects"
    return _get_claude_config_home_dir() / "projects"


def _get_project_dir(project_path: str) -> Path:
    return _get_projects_dir() / _sanitize_path(project_path)


def _canonicalize_path(d: str) -> str:
    """Resolves a directory path to its canonical form using realpath + NFC."""
    try:
        resolved = os.path.realpath(d)
        return unicodedata.normalize("NFC", resolved)
    except OSError:
        return unicodedata.normalize("NFC", d)


def _find_project_dir(project_path: str) -> Path | None:
    """Finds the project directory for a given path.

    Tolerates hash mismatches for long paths (>200 chars). The CLI uses
    Bun.hash while the SDK under Node.js uses simpleHash — for paths that
    exceed MAX_SANITIZED_LENGTH, these produce different directory suffixes.
    This function falls back to prefix-based scanning when the exact match
    doesn't exist.
    """
    exact = _get_project_dir(project_path)
    if exact.is_dir():
        return exact

    # Exact match failed — for short paths this means no sessions exist.
    # For long paths, try prefix matching to handle hash mismatches.
    sanitized = _sanitize_path(project_path)
    if len(sanitized) <= MAX_SANITIZED_LENGTH:
        return None

    prefix = sanitized[:MAX_SANITIZED_LENGTH]
    projects_dir = _get_projects_dir()
    try:
        for entry in projects_dir.iterdir():
            if entry.is_dir() and entry.name.startswith(prefix + "-"):
                return entry
    except OSError:
        pass
    return None
