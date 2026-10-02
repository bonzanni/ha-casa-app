"""Pinned Claude CLI identity used by Casa's in-process SDK agents."""

from __future__ import annotations

import json
import subprocess


CLAUDE_CLI_PATH = "/usr/local/bin/claude"
CLAUDE_CLI_VERSION = "2.1.273"

# #1111: the SDK fails the whole query on any stream-json line longer than
# ``max_buffer_size`` (1 MiB when unset). A built-in ``Read`` of a PDF carries
# the file's base64 twice on one line (the tool result's document block and
# ``tool_use_result``), so a ~400 KB PDF already overflowed the default. The
# pinned CLI 2.1.273 inlines a whole PDF up to 20 MiB and refuses a larger one
# (above 3 MiB it first tries ``pdftoppm`` page rendering, absent from this
# image, and falls back to inlining). Two base64 copies of 20 MiB are ~53.4 MiB,
# so 64 MiB leaves margin for the envelope. Page-range reads always render
# through ``pdftoppm`` and so fail here; adding poppler to the image would need
# this value re-sized (up to 20 pages x 5 MiB base64). The bound only caps one
# line — nothing is preallocated.
SDK_MAX_BUFFER_SIZE = 64 * 1024 * 1024

# The pinned CLI's cross-session messaging surface. ``SendMessage`` and
# ``ListAgents`` list and message the other Claude Code sessions running as the
# same OS user on this machine (per-user inbox sockets, on by default), and
# ``PushNotification`` sends a notification outside Casa's channels. None of
# them asks for permission, so the fail-closed ``can_use_tool`` never sees them,
# and every Casa CLI session runs as the same user in one container: reaching
# another agent must go through Casa's delegation, never around it. Every
# options builder merges these into its ``disallowed_tools`` (the CLI removes
# a disallowed tool from the surface), whatever the agent's own config says.
CROSS_SESSION_TOOLS = ("SendMessage", "ListAgents", "PushNotification")

# The inbound half: ``crossSessionInbound: "refuse"`` makes the CLI drop every
# message another session delivers to this one. Passed through ``--settings``
# (the SDK's ``settings=`` option), the source the CLI reads right after
# managed settings, and ``refuse`` is the strictest value, so no project, local
# or user file can relax it.
CROSS_SESSION_INBOUND = "refuse"


def with_cross_session_tools_denied(disallowed) -> list[str]:
    """Return ``disallowed`` (any iterable) plus :data:`CROSS_SESSION_TOOLS`,
    de-duplicated, order-stable."""
    out = list(disallowed)
    for t in CROSS_SESSION_TOOLS:
        if t not in out:
            out.append(t)
    return out


def cli_session_settings(extra: dict | None = None) -> str:
    """The ``settings=`` JSON every Casa CLI session is started with: *extra*
    plus ``crossSessionInbound: "refuse"``, which no caller can override."""
    return json.dumps({**(extra or {}),
                       "crossSessionInbound": CROSS_SESSION_INBOUND})


def verify_effective_cli() -> str:
    """Return the effective CLI version string or fail closed on mismatch."""
    proc = subprocess.run(
        [CLAUDE_CLI_PATH, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    rendered = (proc.stdout or proc.stderr or "").strip()
    observed_version = rendered.split(maxsplit=1)[0] if rendered else ""
    if proc.returncode != 0 or observed_version != CLAUDE_CLI_VERSION:
        raise RuntimeError(
            f"effective Claude CLI mismatch: expected {CLAUDE_CLI_VERSION}, "
            f"path={CLAUDE_CLI_PATH}, returncode={proc.returncode}"
        )
    return rendered
