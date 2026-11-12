"""Pinned Claude CLI identity used by Casa's in-process SDK agents."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


CLAUDE_CLI_PATH = "/usr/local/bin/claude"
CLAUDE_CLI_VERSION = "2.1.293"

# #1111: the SDK fails the whole query on any stream-json line longer than
# ``max_buffer_size`` (1 MiB when unset). A built-in ``Read`` of a PDF carries
# the file's base64 twice on one line (the tool result's document block and
# ``tool_use_result``), so a ~400 KB PDF already overflowed the default. The
# CLI inlines a whole PDF up to 20 MiB and refuses a larger one (measured on
# 2.1.273; the CLI changelog through the pinned 2.1.293 records no change to
# either limit)
# (above 3 MiB it first tries ``pdftoppm`` page rendering, absent from this
# image, and falls back to inlining). Two base64 copies of 20 MiB are ~53.4 MiB,
# so 64 MiB leaves margin for the envelope. Page-range reads always render
# through ``pdftoppm`` and so fail here; adding poppler to the image would need
# this value re-sized (up to 20 pages x 5 MiB base64). The bound only caps one
# line — nothing is preallocated.
SDK_MAX_BUFFER_SIZE = 64 * 1024 * 1024

# Auto-compaction for long sessions. The CLI compacts a session's context on
# its own when it crosses a threshold; this env lowers the window the threshold
# is computed from (threshold = window - 33k; measured on the pinned CLI, with a
# floor of a 100k window). Without it the job model's 1M window compacts only
# at about 967k tokens. Compaction fires between steps, mid-turn, and the turn
# carries on. One shared value for every session meant to run long: background
# jobs, and roles that hold one long conversation.
AUTO_COMPACT_WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
LONG_SESSION_COMPACT_WINDOW = "200000"


def long_session_env() -> dict[str, str]:
    """The env entries every long-running session is started with."""
    return {AUTO_COMPACT_WINDOW_ENV: LONG_SESSION_COMPACT_WINDOW}


# ``--replay-user-messages``: the CLI echoes each user message written to its
# stdin back on its stdout, once, carrying the uuid the host wrote it with, and
# only at the moment it takes the message into a turn (never at write time;
# measured on the pinned CLI). The SDK keeps the ``uuid`` on the echoed
# ``UserMessage``, which is how a driver learns that a message was read.
REPLAY_USER_MESSAGES_ARG = "replay-user-messages"


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

# #1258: the pinned CLI's self-scheduling built-ins, a different reason for the
# same lever. ``ScheduleWakeup`` and ``CronCreate`` queue a prompt in the CLI's
# own scheduler, outside Casa's (``set_reminder`` is the agent's way to
# schedule). Neither asks for permission, and a wake-up is not inert: measured
# on 2.1.273, the CLI runs an unprompted turn at the due time while its process
# lives, which a warm client or an engagement can mistake for the next turn's
# reply. ``CronCreate`` can also persist its jobs (bundle default
# ``durable: true``). ``CronDelete`` and ``CronList`` manage those jobs.
# ``Monitor`` (gated by a CLI flag) and ``RemoteTrigger`` (claude.ai remote
# sessions only) may be absent from a given session's surface; denying them
# regardless is harmless, since the CLI accepts a disallowed name it does not
# offer. ``CLAUDE_CODE_DISABLE_CRON``
# is no substitute: it removes the ``Cron*`` tools but leaves ``ScheduleWakeup``.
SELF_SCHEDULING_TOOLS = ("ScheduleWakeup", "CronCreate", "CronDelete",
                         "CronList", "Monitor", "RemoteTrigger")

# #1353: a built-in that reaches outside Casa for a third reason. CLI 2.1.293
# offers ``ShareOnboardingGuide``, which uploads ``ONBOARDING.md`` from the
# working directory to a claude.ai share link under the session's login.
# Measured on 2.1.293: it runs without a permission prompt, so the fail-closed
# ``can_use_tool`` never sees it. Denied with the rest.
OUTBOUND_SHARE_TOOLS = ("ShareOnboardingGuide",)

# The inbound half: ``crossSessionInbound: "refuse"`` makes the CLI drop every
# message another session delivers to this one. Passed through ``--settings``
# (the SDK's ``settings=`` option), the source the CLI reads right after
# managed settings, and ``refuse`` is the strictest value, so no project, local
# or user file can relax it.
CROSS_SESSION_INBOUND = "refuse"


def with_cross_session_tools_denied(disallowed) -> list[str]:
    """Return ``disallowed`` (any iterable) plus :data:`CROSS_SESSION_TOOLS`,
    :data:`SELF_SCHEDULING_TOOLS` and :data:`OUTBOUND_SHARE_TOOLS`,
    de-duplicated, order-stable."""
    out = list(disallowed)
    for t in (*CROSS_SESSION_TOOLS, *SELF_SCHEDULING_TOOLS,
              *OUTBOUND_SHARE_TOOLS):
        if t not in out:
            out.append(t)
    return out


def cli_session_settings(extra: dict | None = None) -> str:
    """The ``settings=`` JSON every Casa CLI session is started with: *extra*
    plus ``crossSessionInbound: "refuse"``, which no caller can override."""
    return json.dumps({**(extra or {}),
                       "crossSessionInbound": CROSS_SESSION_INBOUND})


def ephemeral_setting_sources() -> list[str]:
    """The ``setting_sources`` of a utility one-shot (the tier classifier,
    query_engager's synthesis): none at all (INV-MEM-021).

    Left unset, the SDK loads the user source, and with it the pinned CLI
    enables its own age-based cleanup, which walks the whole projects root
    and deletes any transcript older than ``cleanupPeriodDays`` (default 30)
    — a transcript INV-MEM-031 keeps included. The CLI enables that
    cleanup only when the user source is loaded or a loaded source sets
    ``cleanupPeriodDays``; a project source is no better, since its file could
    carry the key, and a one-shot has no project of its own. Never put
    ``cleanupPeriodDays`` in :func:`cli_session_settings`: passing it as a
    flag enables the cleanup for every launch."""
    return []


def ephemeral_extra_args() -> dict[str, str | None]:
    """The ``extra_args`` of a utility one-shot: ``--no-session-persistence``
    (INV-ENG-028). Nothing resumes or names a one-shot's session afterwards,
    so it writes no transcript at all. Measured on the pinned CLI under the
    SDK's stream-json transport: a completed turn returns its reply and
    leaves nothing under the projects root."""
    return {"no-session-persistence": None}


# #1570: the SDK hands a string system prompt to the CLI as ONE argv entry
# (``--system-prompt <text>``), and Linux refuses an exec whose single argument
# reaches 128 KiB (MAX_ARG_STRLEN, measured in the container: 131,071 bytes
# start, 131,072 fail with E2BIG). A resident's prompt carries its memory block,
# which grows with the bank: the assistant's reached 128,696 bytes and the next
# cold start failed, retry after retry. So every exec passes the prompt as a
# file (``--system-prompt-file``), which the pinned CLI reads once, at startup.
# Files are content-addressed, so an unchanged prompt is written once, and any
# file not used for an hour is pruned: by then its CLI has long read it.
SYSTEM_PROMPT_DIR = Path(tempfile.gettempdir()) / "casa-system-prompts"
SYSTEM_PROMPT_FILE_TTL_S = 3600


def with_system_prompt_file(options):
    """Return *options* with a non-empty string system prompt moved to a file.

    Apply at the exec site only, on the copy handed to the SDK: the options
    builders and their readers keep working with the string. Preset, file and
    empty prompts pass through unchanged."""
    prompt = getattr(options, "system_prompt", None)
    if not isinstance(prompt, str) or not prompt:
        return options
    path = _system_prompt_file(prompt)
    return dataclasses.replace(
        options, system_prompt={"type": "file", "path": str(path)})


def _system_prompt_file(prompt: str) -> Path:
    data = prompt.encode("utf-8")
    SYSTEM_PROMPT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = SYSTEM_PROMPT_DIR / f"{hashlib.sha256(data).hexdigest()}.md"
    try:
        os.utime(path)                     # reused: keep it out of the prune
    except FileNotFoundError:
        fd, tmp = tempfile.mkstemp(dir=SYSTEM_PROMPT_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    _prune_system_prompt_files(keep=path)
    return path


def _prune_system_prompt_files(*, keep: Path) -> None:
    cutoff = time.time() - SYSTEM_PROMPT_FILE_TTL_S
    for entry in SYSTEM_PROMPT_DIR.iterdir():
        try:
            if entry != keep and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


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
