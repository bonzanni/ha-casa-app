"""Safety hooks: command blocking and parameterized path-scope enforcement.

Per-agent hook wiring is driven by each agent's ``hooks.yaml`` file,
resolved through :func:`resolve_hooks` and the :data:`HOOK_POLICIES`
registry. Payload shape follows the SDK's
``PreToolUseHookSpecificOutput``: ``hookEventName`` +
``permissionDecision`` (allow | deny | ask) + reason.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable


_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Forbidden shell commands (argv-aware)
#
# History: pre-v0.14.6 this was a flat list of regex patterns matched against
# the raw command string. That trivially missed equivalents like
# `rm -r -f`, `rm --recursive --force`, `rm -rfv`, etc. The argv-aware
# matcher below splits the command on shell separators (;, &&, ||, |, &,
# and newlines), shlex'es each piece into argv tokens, and inspects
# argv[0] + argv[1:]. It also recurses into `bash -c <str>` / `sh -c <str>`
# so that wrapper shells don't bypass the check.
# FORBIDDEN_PATTERNS is kept as a deprecated alias of the legacy regex list
# so existing imports don't break; the live matcher is _command_is_dangerous.
#
# SCOPE & ACCEPTED RESIDUALS (v0.50.0 security review):
# block_dangerous_commands and casa_config_guard are DEFENSE-IN-DEPTH argv
# inspectors, not a bash sandbox — the real security boundaries are the SDK
# permission system and workspace isolation. Known residuals, inherent to
# argv-level inspection, accepted and documented rather than chased with
# ever-more regex:
#   * command substitution ``$(...)`` / backticks and process substitution
#     ``<(...)`` / ``>(...)``: scanned best-effort by span regexes (see
#     _SUBSTITUTION_SPAN_RE below); adversarial quoting/nesting inside a
#     span can still evade.
#   * destructive verbs outside the modeled set: ``find -delete`` /
#     ``find ... -exec rm ...``, ``truncate``, ``shred``, ``tee /target``,
#     ``> file`` clobbering, non-``rm`` deletion of residents, etc. — a
#     deletion hidden behind a verb we do not model is not decomposed.
#   * anything requiring evaluation of attacker-controlled data: variable
#     indirection (``X='rm -rf /'; $X``), ``env -S 'rm ...'``, paths built
#     from shell variables (``rm${IFS}-rf${IFS}/``), xargs arguments
#     arriving via stdin.
#   * exec-wrapper prefixes outside the modeled set: the shell builtins
#     ``command``/``exec`` (builtins, not external programs, so absent from
#     _EXEC_WRAPPER_ARG_FLAGS), and wrapper nests deeper than the recursion
#     bound (``_depth > 3`` — e.g. four stacked wrappers like
#     ``sudo nohup setsid timeout 5 rm -rf /``): the innermost command is
#     not unwrapped/re-scanned.
# Do NOT present these guards as complete command filtering; strengthen the
# outer boundaries instead. Honesty over false assurance.
# ---------------------------------------------------------------------------

# Programs that are denied outright (no allow-listed flag set).
_DENY_PROGRAMS = frozenset({
    "shutdown", "reboot", "halt", "poweroff", "ssh", "scp",
})

# Wrapper shells whose -c argument we should re-scan.
_WRAPPER_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ash", "ksh"})

# Shell-control operators on which we split the command into pipeline pieces.
# Order matters in the regex: longer operators first so we don't double-split.
# ``[\r\n]+`` makes newlines (LF/CRLF) first-class command separators — an
# LLM-issued multi-line command is exactly as dangerous as its ``;``-joined
# equivalent. Only used by the legacy quote-blind fallback below.
_PIPELINE_SPLIT_RE = re.compile(r"&&|\|\||;|\||&|[\r\n]+")

# Pipeline separators as *tokens* (for the quote-aware shlex splitter).
# v0.50.0 round 2: bash's '|&' (pipe stdout+stderr) and the case-branch
# terminators ';&' / ';;&' each begin a new simple command, and shlex's
# punctuation-run tokenizer emits each as ONE token — absent from this set
# they fell through to the _SHLEX_PUNCT skip below and the two stages
# merged, so the RHS program was never scanned as argv[0].
# True redirections ('>', '>>', '<', '>&', '&>', and the '>&' inside
# '2>&1') must stay OUT of this set: they do not start a new command, and
# splitting on them would promote a redirection target to argv[0].
_PIPELINE_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "|&", ";&", ";;&"})

# shlex ``punctuation_chars=True`` emits these as standalone operator tokens.
# Non-pipeline operators (redirections ``>`` ``<`` ``>>``, subshell ``(`` ``)``)
# are skipped so a redirection target isn't promoted to argv[0] of a new piece.
_SHLEX_PUNCT = frozenset("();<>|&")

# Env-var assignment prefix (FOO=bar cmd) — skipped when locating argv[0].
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Kept for back-compat with any external code that imported the old name.
# Not used by block_dangerous_commands — the argv matcher is authoritative.
FORBIDDEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r"\bcurl\b.*-X\s*POST\b"),
    re.compile(r"\bcurl\b.*--data\b"),
    re.compile(r"\bssh\b"),
    re.compile(r"\bscp\b"),
]


def _split_pipeline_fallback(command: str) -> list[list[str]]:
    """Legacy quote-blind split; used only when the whole command has
    mismatched quotes and the quote-aware tokenizer cannot parse it.

    Splits on the raw operator regex, then runs shlex.split on each piece
    with a whitespace-split fallback. Leading FOO=bar assignments are
    stripped so argv[0] is the real program name. Permissive by intent: a
    malformed command is the user's problem; we still want to *try* to
    detect dangerous primitives in it.
    """
    pieces = _PIPELINE_SPLIT_RE.split(command)
    out: list[list[str]] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        try:
            argv = shlex.split(piece, posix=True)
        except ValueError:
            argv = piece.split()
        while argv and _ENV_ASSIGN_RE.match(argv[0]):
            argv = argv[1:]
        if argv:
            out.append(argv)
    return out


def _split_pipeline(command: str) -> list[list[str]]:
    """Split a shell command line into a list of argv lists, quote-aware.

    Splits on ;, &&, ||, |, & AND newlines. Uses shlex with
    ``punctuation_chars`` so those operators are NOT treated as pipeline
    boundaries when they appear inside quoted strings (e.g.
    ``git commit -m "a && b"`` stays one argv). Leading FOO=bar
    assignments are stripped so argv[0] is the real program name.
    Falls back to the legacy quote-blind split on mismatched quotes.
    """
    # Shell line-continuation: backslash-newline joins physical lines into
    # one logical command. Collapse it BEFORE newline splitting so
    # ``curl \<newline> -X POST ...`` stays a single argv.
    command = command.replace("\\\r\n", " ").replace("\\\n", " ")
    # Newlines are command separators equivalent to ';'. Rewriting them to
    # ' ; ' is quote-safe: a newline INSIDE quotes becomes a ';' that the
    # tokenizer keeps as literal data (the surrounding quotes are preserved),
    # while a bare newline becomes a real separator token.
    command = re.sub(r"[\r\n]+", " ; ", command)
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        # Disable '#' comment handling (shlex.shlex defaults commenters='#',
        # unlike shlex.split(comments=False)). Otherwise a '#' would swallow
        # the rest of the (newline-collapsed) command — e.g.
        # ``ls # note\nrm -rf /`` would hide the rm and bypass the guard.
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        # Mismatched quotes — permissive best-effort, same as before.
        return _split_pipeline_fallback(command)
    out: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in _PIPELINE_SEPARATORS:
            if cur:
                out.append(cur)
                cur = []
        elif tok and all(ch in _SHLEX_PUNCT for ch in tok):
            # Non-pipeline operator token (>, <, >>, (, ) ...): skip it so
            # a redirection target stays inside the current argv instead of
            # being promoted to argv[0] of a new piece.
            continue
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    result: list[list[str]] = []
    for argv in out:
        while argv and _ENV_ASSIGN_RE.match(argv[0]):
            argv = argv[1:]
        if argv:
            result.append(argv)
    return result


def _rm_has_recursive_and_force(argv: list[str]) -> bool:
    """True iff `rm` argv contains both a recursive AND a force flag.

    Recognises -r / -R / --recursive and -f / --force in any order, and
    short-flag clusters (-rf, -fr, -rfv, -rfd, -fRv, ...).
    """
    has_recursive = False
    has_force = False
    for arg in argv[1:]:
        if arg == "--":
            break  # everything after -- is positional
        if arg in ("--recursive",):
            has_recursive = True
        elif arg == "--force":
            has_force = True
        elif arg.startswith("--"):
            continue
        elif arg.startswith("-") and len(arg) > 1:
            for ch in arg[1:]:
                if ch in ("r", "R"):
                    has_recursive = True
                elif ch == "f":
                    has_force = True
    return has_recursive and has_force


def _dd_writes_block_device(argv: list[str]) -> bool:
    """True iff dd has if= or of= argument (block-level read or write)."""
    return any(a.startswith("if=") or a.startswith("of=") for a in argv[1:])


def _curl_sends_data(argv: list[str]) -> bool:
    """True iff curl is doing a write request (-X POST/PUT/DELETE/PATCH or -d/--data*)."""
    args = argv[1:]
    write_methods = {"POST", "PUT", "DELETE", "PATCH"}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-X", "--request") and i + 1 < len(args):
            if args[i + 1].upper() in write_methods:
                return True
        if a in ("-d", "--data", "--data-raw", "--data-binary",
                 "--data-urlencode", "-T", "--upload-file"):
            return True
        # Combined short forms: -XPOST, -dfoo
        if a.startswith("-X") and len(a) > 2 and a[2:].upper() in write_methods:
            return True
        if a.startswith("-d") and len(a) > 2 and not a.startswith("-d="):
            return True
        i += 1
    return False


# v0.50.0 (security-review follow-up, H8 class): command/process
# substitution and backticks execute their contents without the inner
# program ever appearing as argv[0] of a pipeline piece, so the argv
# scanner alone never saw `echo $(rm -rf /)` or ``x=`rm -rf /` ``.
# The spans are extracted from the RAW command string (quote-blind by
# design: `"$(...)"` still executes, and after shlex de-quoting single
# and double quotes are indistinguishable — deny both). Only fires when
# the *inner* content is itself dangerous, so `awk '{print $(NF-1)}'`
# and `echo $((1+2))` stay allowed.
_SUBSTITUTION_SPAN_RE = re.compile(r"[$<>]\((.*)\)", re.DOTALL)
_BACKTICK_SPAN_RE = re.compile(r"`([^`]+)`")

# xargs flags that consume the FOLLOWING token as their argument (GNU
# xargs flags whose argument must be separate or may be separate).
# Attached forms (-n1, -I{}, --max-args=1) are skipped by the generic
# leading-dash test. -e/-i/-l take only attached optional args in GNU
# xargs, so they are deliberately NOT in this set — consuming the next
# token for them could swallow the wrapped command (a false negative).
_XARGS_ARG_FLAGS = frozenset({
    "-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s",
    "--arg-file", "--delimiter", "--eof", "--max-lines", "--max-args",
    "--max-procs", "--max-chars",
})


def _xargs_wrapped_argv(argv: list[str]) -> list[str]:
    """Return the argv of the command ``xargs`` will exec, or ``[]``.

    Skips xargs' own flags (consuming separate arguments where the flag
    requires one); the first non-flag token starts the wrapped command.
    """
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in _XARGS_ARG_FLAGS:
            i += 2
            continue
        if a.startswith("-") and a != "-":
            i += 1
            continue
        return argv[i:]
    return []


# v0.50.0 round 2: exec-wrapper prefixes. Each of these programs runs its
# tail as the real command (``nohup rm -rf /``, ``timeout 5 rm -rf /``,
# ``sudo rm ...``) — pre-fix argv[0] was the benign wrapper name and the
# tail was never inspected. Map: wrapper name -> its flags that consume the
# FOLLOWING token as a separate argument (attached forms like ``-n5`` /
# ``--adjustment=5`` are skipped by the generic leading-dash test — same
# convention as _XARGS_ARG_FLAGS above).
_EXEC_WRAPPER_ARG_FLAGS: dict[str, frozenset[str]] = {
    "nohup": frozenset(),
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "setsid": frozenset(),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata",
                         "-p", "--pid", "-P", "--pgid", "-u", "--uid"}),
    "chrt": frozenset({"-T", "--sched-runtime", "-P", "--sched-period",
                       "-D", "--sched-deadline", "-p", "--pid"}),
    "taskset": frozenset(),
    "unbuffer": frozenset(),
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-h", "--host",
                       "-p", "--prompt", "-C", "--close-from",
                       "-D", "--chdir", "-R", "--chroot",
                       "-T", "--command-timeout", "-U", "--other-user",
                       "-r", "--role", "-t", "--type"}),
    "doas": frozenset({"-u", "-C", "-a"}),
}

# Wrappers with a MANDATORY positional before the command: ``timeout
# DURATION cmd``, ``chrt PRIORITY cmd``, ``taskset MASK cmd``. That many
# non-flag tokens are skipped before the tail starts.
_EXEC_WRAPPER_POSITIONALS: dict[str, int] = {
    "timeout": 1, "chrt": 1, "taskset": 1,
}


def _exec_wrapper_tail(argv: list[str]) -> list[str]:
    """Return the argv of the command an exec-wrapper prefix will run, or ``[]``.

    Same pattern as :func:`_xargs_wrapped_argv`: skip the wrapper name, its
    option flags (consuming the following token where the flag takes a
    separate argument), ``NAME=VALUE`` assignments (``env A=B cmd``,
    ``sudo VAR=x cmd``), and any mandatory positional (timeout's DURATION,
    chrt's PRIORITY, taskset's MASK). The first remaining token starts the
    wrapped command. Conservative by design: when a flag's arity is not in
    the table, the tail starts at the first non-flag/non-assignment token.
    """
    prog = os.path.basename(argv[0])
    arg_flags = _EXEC_WRAPPER_ARG_FLAGS.get(prog)
    if arg_flags is None:
        return []
    skip_positionals = _EXEC_WRAPPER_POSITIONALS.get(prog, 0)
    i = 1
    opts_done = False
    while i < len(argv):
        a = argv[i]
        if not opts_done and a == "--":
            opts_done = True
            i += 1
            continue
        if not opts_done and a in arg_flags:
            i += 2
            continue
        if not opts_done and a.startswith("-") and a != "-":
            i += 1
            continue
        if _ENV_ASSIGN_RE.match(a):
            i += 1
            continue
        if skip_positionals > 0:
            skip_positionals -= 1
            i += 1
            continue
        return argv[i:]
    return []


def _argv_is_dangerous(argv: list[str], *, _depth: int = 0) -> str | None:
    """Inspect a single pipeline piece (argv list) for dangerous primitives."""
    if _depth > 3 or not argv:
        return None
    # Strip absolute path: /usr/bin/rm -> rm.
    prog = os.path.basename(argv[0])

    # Wrapper-shell recursion: bash -c "<cmd>" / sh -c "<cmd>".
    if prog in _WRAPPER_SHELLS:
        for j in range(1, len(argv) - 1):
            if argv[j] == "-c":
                inner_reason = _command_is_dangerous(
                    argv[j + 1], _depth=_depth + 1,
                )
                if inner_reason:
                    return f"{prog} -c wrapping {inner_reason}"
                break  # don't double-scan the same -c
        # Fall through — the outer wrapper shell itself isn't denied.

    # eval concatenates its arguments and executes them as a shell command.
    if prog == "eval" and len(argv) > 1:
        inner_reason = _command_is_dangerous(
            " ".join(argv[1:]), _depth=_depth + 1,
        )
        if inner_reason:
            return f"eval wrapping {inner_reason}"

    # xargs execs its trailing argv (with stdin-derived arguments appended).
    if prog == "xargs":
        wrapped = _xargs_wrapped_argv(argv)
        if wrapped:
            inner_reason = _argv_is_dangerous(wrapped, _depth=_depth + 1)
            if inner_reason:
                return f"xargs wrapping {inner_reason}"

    # v0.50.0 round 2: exec-wrapper prefixes (nohup, timeout, env, sudo, ...)
    # run their tail as the real command — unwrap and re-scan it as its own
    # argv, so `timeout 5 rm -rf /` resolves like `rm -rf /`.
    if prog in _EXEC_WRAPPER_ARG_FLAGS:
        wrapped = _exec_wrapper_tail(argv)
        if wrapped:
            inner_reason = _argv_is_dangerous(wrapped, _depth=_depth + 1)
            if inner_reason:
                return f"{prog} wrapping {inner_reason}"

    if prog == "rm" and _rm_has_recursive_and_force(argv):
        return f"rm with recursive+force flags: {shlex.join(argv)!r}"
    if prog == "dd" and _dd_writes_block_device(argv):
        return f"dd with if=/of= argument: {shlex.join(argv)!r}"
    if prog == "curl" and _curl_sends_data(argv):
        return f"curl sending data: {shlex.join(argv)!r}"
    if prog in _DENY_PROGRAMS:
        return f"{prog} is not allowed"
    return None


def _command_is_dangerous(command: str, *, _depth: int = 0) -> str | None:
    """Return a human-readable reason if the command is dangerous, else None.

    Recurses up to 3 levels deep into wrapper shells (bash -c, sh -c, ...),
    ``eval``, ``xargs``, and command/process-substitution spans.
    """
    if _depth > 3:
        return None
    for argv in _split_pipeline(command):
        reason = _argv_is_dangerous(argv, _depth=_depth)
        if reason:
            return reason
    # Command substitution $(...), process substitution <(...)/>(...), and
    # backticks all execute their contents; scan those spans recursively.
    for span_re in (_SUBSTITUTION_SPAN_RE, _BACKTICK_SPAN_RE):
        for m in span_re.finditer(command):
            inner_reason = _command_is_dangerous(
                m.group(1), _depth=_depth + 1,
            )
            if inner_reason:
                return f"substitution wrapping {inner_reason}"
    return None


HookCallback = Callable[
    [dict[str, Any], str | None, dict[str, Any]],
    Awaitable[dict[str, Any]],
]
# H-2 (v0.36.1): no-op paths return ``{}`` not ``None``. The SDK's
# ``_convert_hook_output_for_cli`` calls ``hook_output.items()``
# unconditionally; ``None`` violates the typed ``HookJSONOutput`` contract
# and emits 73+ ``Error in hook callback`` lines per ~30-min engagement.
# Operationally equivalent (SDK treats ``{}`` the same as ``None`` for
# decision purposes) but type-compliant.


def _deny(reason: str) -> dict[str, Any]:
    """Return a PreToolUse payload that denies the tool call.

    Shape is defined by the SDK's ``PreToolUseHookSpecificOutput``. The older
    ``{"decision": "deny"}`` shape is silently ignored by the CLI Zod
    validator, which is why per-role enforcement appeared broken.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _normalize_path(raw: str) -> str:
    """Resolve ``..`` segments using PurePosixPath splitting (no OS calls)."""
    # v0.50.0 (security-review must-fix): collapse redundant slashes FIRST.
    # POSIX leaves a leading '//' implementation-defined and PurePosixPath
    # preserves it as a distinct root ('//config' -> parts ('//', 'config')),
    # so '//config/agents/x' normalized to the malformed
    # '///config/agents/x' and slipped past every prefix check — while the
    # Linux kernel resolves '//' as '/', making the command effective.
    raw = re.sub(r"/{2,}", "/", raw)
    parts = PurePosixPath(raw).parts
    resolved: list[str] = []
    for part in parts:
        if part == "..":
            if resolved and resolved[-1] != "/":
                resolved.pop()
        elif part != ".":
            resolved.append(part)
    # L-2 (v0.34.2): when the input was an absolute path, parts[0] == "/".
    # Naive "/".join produces "//rest" — return "/rest" instead.
    if resolved and resolved[0] == "/":
        return "/" + "/".join(resolved[1:])
    return "/".join(resolved)


# ---------------------------------------------------------------------------
# Hook implementations
# ---------------------------------------------------------------------------


async def block_dangerous_commands(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Block Bash commands that contain dangerous primitives.

    Uses an argv-aware matcher (see ``_command_is_dangerous``) so that
    flag variations (``-r -f`` vs ``-rf``, ``--recursive --force``,
    short-flag clusters) and wrapper shells (``bash -c "..."``) all
    resolve to the same decision.
    """
    tool_name = input_data.get("tool_name", "")
    if tool_name != "Bash":
        return {}

    command = input_data.get("tool_input", {}).get("command", "")
    reason = _command_is_dangerous(command)
    if reason is not None:
        return _deny(f"Blocked by safety hook: {reason}")
    # Sol #5: also deny a Bash write into /config/plugins (registry/store).
    # path_scope ignores Bash, so an executor (configurator) could otherwise
    # `echo > /config/plugins/registry.json`, bypassing plugin_add validation
    # + §3.9 sequencing — the same regex the resident/specialist settings
    # guard uses.
    if _PLUGINS_WRITE_RE.search(command):
        return _deny(_PLUGINS_DENY_MSG)
    return {}


# ---------------------------------------------------------------------------
# Parameterized path_scope + HOOK_POLICIES registry.
# ---------------------------------------------------------------------------


class UnknownPolicyError(Exception):
    """Raised when a hooks.yaml policy or parameter is not recognised."""


def _require_prefix_list(
    policy: str, param: str, value: Any,
) -> list[str] | None:
    """Return ``value`` as a list of path prefixes, or raise.

    #442: the hooks schema leaves per-policy parameters open
    (``additionalProperties``), so a mistyped value reaches the factory
    intact. A bare string is iterable, and every consumer here iterates —
    ``writable: /config`` (no list dash) became the prefix set
    ``['/', 'c', 'o', 'n', ...]``, whose lone ``/`` prefix-matches every
    absolute path. The guard then failed OPEN: it admitted precisely the
    writes it was configured to refuse. A type error must fail the BUILD,
    where ``UnknownPolicyError`` already fail-closes the executor at load
    (see ``agent_loader._resolve_hooks_file``), never enforcement.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise UnknownPolicyError(
            f"{policy}: {param} must be a list of path prefixes, got "
            f"{type(value).__name__} ({value!r})"
        )
    bad = [p for p in value if not isinstance(p, str)]
    if bad:
        raise UnknownPolicyError(
            f"{policy}: every {param} entry must be a string; got "
            f"{bad!r}"
        )
    return value


def _require_int(policy: str, param: str, value: Any) -> int:
    """Return ``value`` as an int, or raise (#442 r2).

    ``int(...)`` coerces rather than checks: it turned ``true`` into the
    limit 1 and ``"5"`` into 5. Neither fails open, but a coercion is a
    guess at what the author meant, and INV-MCP-007 is the stronger, simpler
    rule — a parameter of the wrong type never builds. ``bool`` is excluded
    explicitly because it is a subclass of ``int``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnknownPolicyError(
            f"{policy}: {param} must be a whole number, got "
            f"{type(value).__name__} ({value!r})"
        )
    return value


def _require_bool(policy: str, param: str, value: Any) -> bool:
    """Return ``value`` as a bool, or raise (#442).

    Truthiness is not a type check: a falsy non-bool (``0``, ``[]``, ``""``)
    silently DISABLES the guard it gates, and a truthy one (``"false"``,
    ``"no"``) silently enables it against the author's intent.
    """
    if not isinstance(value, bool):
        raise UnknownPolicyError(
            f"{policy}: {param} must be true or false, got "
            f"{type(value).__name__} ({value!r})"
        )
    return value


def make_path_scope_hook_v2(
    *,
    writable: list[str] | None = None,
    readable: list[str] | None = None,
    extra_read_check: Callable[[Path], bool] | None = None,
) -> HookCallback:
    """Return a PreToolUse hook that enforces absolute-path prefixes.

    The per-agent ``hooks.yaml`` supplies the prefix lists.
    ``writable`` applies to Write/Edit. ``readable`` applies to
    Read/Write/Edit. Anything outside the allowed set denies;
    exact-match or prefix-match.

    ``extra_read_check`` is asked about a Read the readable prefixes deny,
    at that Read's decision: a path it accepts is allowed. It is consulted
    live, so a file granted after the hook was built is readable at once.
    """
    # #442: type-check BEFORE the comprehension — a bare string would
    # otherwise expand per character into a '/' prefix that matches
    # everything.
    writable = [
        _normalize_path(p)
        for p in (_require_prefix_list("path_scope", "writable", writable)
                  or [])
    ]
    readable = [
        _normalize_path(p)
        for p in (_require_prefix_list("path_scope", "readable", readable)
                  or [])
    ]

    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        if tool_name not in ("Read", "Write", "Edit"):
            return {}
        raw = input_data.get("tool_input", {}).get("file_path", "")
        norm = _normalize_path(raw)

        if tool_name in ("Write", "Edit"):
            if not _has_prefix(norm, writable):
                return _deny(
                    f"path_scope: {tool_name} denied — {raw!r} outside "
                    f"writable prefixes {writable}"
                )
        else:  # Read
            if not (_has_prefix(norm, readable)
                    or _is_own_tool_result(norm, input_data)
                    or (extra_read_check is not None and extra_read_check(Path(norm)))):
                return _deny(
                    f"path_scope: Read denied — {raw!r} outside "
                    f"readable prefixes {readable}"
                )
        return {}

    return _hook


def _is_own_tool_result(norm: str, input_data: dict[str, Any]) -> bool:
    """Is ``norm`` a file in the calling session's own ``tool-results/``?

    #1082: Claude Code diverts an oversized tool result to
    ``<claude home>/projects/<project>/<session>/tool-results/`` and tells the
    model to Read it. The projects root comes from the ``transcript_path`` the
    runtime reports on this very call (``<root>/<project>/<session>.jsonl``);
    the session segment must be this session's own id — the ``session_id``
    the runtime reports, or the transcript's stem. The project segment is left
    free on purpose: a session resumed under another working directory writes
    its results under that directory's project folder (review of #1082), and
    re-deriving Claude Code's folder naming here would be a copy that drifts.
    Nothing else in the private Claude home matches — not a transcript, not
    another session's results, not the directory itself.
    """
    tp = input_data.get("transcript_path")
    if not isinstance(tp, str) or not tp:
        return False
    tp = _normalize_path(tp)
    if not tp.startswith("/") or not tp.endswith(".jsonl"):
        return False
    tp_parts = PurePosixPath(tp).parts
    if len(tp_parts) < 3:
        return False
    root = tp_parts[:-2]
    # A normalised segment is never empty, "." or "..", and never equals a
    # non-string, so a malformed ``session_id`` simply matches nothing.
    ids = (tp_parts[-1][: -len(".jsonl")], input_data.get("session_id"))
    parts = PurePosixPath(norm).parts
    return (len(parts) >= len(root) + 4
            and parts[: len(root)] == root
            and parts[len(root) + 1] in ids
            and parts[len(root) + 2] == "tool-results")


def _has_prefix(norm: str, prefixes: list[str]) -> bool:
    return any(norm == p or norm.startswith(p.rstrip("/") + "/")
               for p in prefixes)


# ---------------------------------------------------------------------------
# read_evidence — #1038 §6.2: did this turn open an inbound file?
# ---------------------------------------------------------------------------

def read_evidence_matchers(role: str) -> dict[str, list[Any]]:
    """PostToolUse / PostToolUseFailure matchers on ``Read`` that record, into
    the running turn's scope, whether an inbox file was opened.

    The runtime's own call, not an inference: the SDK fires PostToolUse only
    when the tool ran and returned, PostToolUseFailure when it raised, and
    neither when the ``path_scope`` PreToolUse guard denied it (measured
    2026-09-22). Both complete BEFORE the tool result reaches the model, so an
    in-process tool the model calls next cannot race them.

    Only a path under ``agent_inbox.readable_prefixes(role)`` is evidence — the
    same lexical normalisation the scope guard applies. A read aimed at an
    inbox file the turn never listed makes the turn a file turn too (a path
    copied from an earlier listing). A role without an inbox records nothing;
    a call with no scope bound records nothing. Never breaks the tool: any
    failure here is logged and the hook returns ``{}``.
    """
    from claude_agent_sdk import HookMatcher

    def _record(input_data: dict[str, Any], *, ok: bool) -> None:
        import agent as agent_mod
        import agent_inbox

        origin = agent_mod.origin_var.get(None) or {}
        scope = origin.get("turn_scope")
        if scope is None:
            return
        raw = (input_data.get("tool_input") or {}).get("file_path", "")
        if not isinstance(raw, str) or not raw:
            return
        norm = _normalize_path(raw)
        prefixes = [_normalize_path(p) for p in agent_inbox.readable_prefixes(role)]
        if not prefixes or not _has_prefix(norm, prefixes):
            return
        inbox = agent_inbox.get_inbox(role)
        name = os.path.basename(norm)
        display = inbox.display_name_for(name) if inbox is not None else name
        scope.note_read_attempt(norm, display_name=display)
        if ok:
            scope.note_read_ok(norm)
        else:
            scope.note_read_failed(norm)

    async def _post(input_data: dict[str, Any], tool_use_id: str | None,
                    context: dict[str, Any]) -> dict[str, Any]:
        try:
            _record(input_data, ok=True)
        except Exception:  # noqa: BLE001 — evidence must never break the tool
            _logger.warning("read_evidence: PostToolUse recording failed",
                            exc_info=True)
        return {}

    async def _fail(input_data: dict[str, Any], tool_use_id: str | None,
                    context: dict[str, Any]) -> dict[str, Any]:
        try:
            _record(input_data, ok=False)
        except Exception:  # noqa: BLE001
            _logger.warning("read_evidence: PostToolUseFailure recording failed",
                            exc_info=True)
        return {}

    return {
        "PostToolUse": [HookMatcher(matcher="Read", hooks=[_post])],
        "PostToolUseFailure": [HookMatcher(matcher="Read", hooks=[_fail])],
    }


# ---------------------------------------------------------------------------
# casa_config_guard - Plan 3 (blocks /data/, schema/, resident deletions)
# ---------------------------------------------------------------------------


_RESIDENT_ROOT = "/config/agents"
_RESIDENT_EXEMPT_SUBTREES = ("specialists", "executors")


def _argv_deletes_resident(argv: list[str], *, _depth: int = 0) -> bool:
    """One pipeline piece of :func:`_deletes_resident` (argv level)."""
    if _depth > 3 or not argv:
        return False
    prog = os.path.basename(argv[0])
    if prog in _WRAPPER_SHELLS:
        for j in range(1, len(argv) - 1):
            if argv[j] == "-c":
                if _deletes_resident(argv[j + 1], _depth=_depth + 1):
                    return True
                break
    # v0.50.0: eval concatenates its arguments and executes them as a
    # shell command — same wrapper class as bash -c.
    if prog == "eval" and len(argv) > 1:
        if _deletes_resident(" ".join(argv[1:]), _depth=_depth + 1):
            return True
    # v0.50.0 round 2: exec-wrapper prefixes (nohup, timeout, env, sudo,
    # ...) run their tail as the real command — unwrap and re-scan, same
    # as in _argv_is_dangerous.
    if prog in _EXEC_WRAPPER_ARG_FLAGS:
        wrapped = _exec_wrapper_tail(argv)
        if wrapped and _argv_deletes_resident(wrapped, _depth=_depth + 1):
            return True
    if prog != "rm":
        return False
    seen_ddash = False
    for a in argv[1:]:
        if not seen_ddash:
            if a == "--":
                seen_ddash = True
                continue
            if a.startswith("-") and a != "-":
                continue  # short or long flag
        norm = _normalize_path(a)
        if norm == _RESIDENT_ROOT:
            return True  # rm of the whole agents dir kills every resident
        if norm.startswith(_RESIDENT_ROOT + "/"):
            rest = norm[len(_RESIDENT_ROOT) + 1:]
            head, _sep, tail = rest.partition("/")
            # Exempt only paths INSIDE specialists/ or executors/;
            # deleting those subtree roots themselves still denies
            # (matches the old regex's lookahead semantics).
            if head in _RESIDENT_EXEMPT_SUBTREES and tail:
                continue
            return True
    return False


def _deletes_resident(command: str, *, _depth: int = 0) -> bool:
    """True iff any ``rm`` in the command (or behind a ``bash``/``sh -c``
    wrapper, ``eval``, or an exec-wrapper prefix like ``nohup``/``timeout``/
    ``sudo``) targets ``/config/agents`` itself or a
    non-specialist/non-executor child of it.

    Argv-aware (via :func:`_split_pipeline`), so it is immune to the
    bypasses that defeated the old regex: quoted paths
    (``rm -r "/config/agents/assistant"``), long flags (``--recursive``), the
    ``--`` end-of-options marker, wrapper shells, exec-wrapper prefixes,
    and ``..`` traversal (paths are normalised with
    :func:`_normalize_path`).
    """
    if _depth > 3:
        return False
    for argv in _split_pipeline(command):
        if _argv_deletes_resident(argv, _depth=_depth):
            return True
    return False


def make_casa_config_guard_hook(
    *,
    forbid_write_paths: list[str] | None = None,
    forbid_delete_residents: bool = True,
) -> HookCallback:
    """Return a PreToolUse hook that guards Casa-specific destructive ops."""
    # #442: see _require_prefix_list — a bare string fails this guard OPEN.
    forbid_write = [
        _normalize_path(p)
        for p in (_require_prefix_list(
            "casa_config_guard", "forbid_write_paths", forbid_write_paths)
            or [])
    ]
    forbid_delete_residents = _require_bool(
        "casa_config_guard", "forbid_delete_residents",
        forbid_delete_residents,
    )

    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        if tool_name in ("Write", "Edit"):
            raw = input_data.get("tool_input", {}).get("file_path", "")
            norm = _normalize_path(raw)
            if _has_prefix(norm, forbid_write):
                return _deny(
                    f"casa_config_guard: {tool_name} blocked - {raw!r} is "
                    f"in a forbidden prefix ({forbid_write}). This path "
                    f"holds runtime state or authoritative schema; editing "
                    f"it would break Casa. Ask the user if you believe "
                    f"this is necessary."
                )
        elif tool_name == "Bash":
            command = input_data.get("tool_input", {}).get("command", "")
            if forbid_delete_residents and _deletes_resident(command):
                return _deny(
                    "casa_config_guard: Bash blocked - command looks like "
                    "a resident agent deletion. Residents are very "
                    "destructive to remove; ask the user explicitly and "
                    "retry only if they say yes."
                )
        return {}

    return _hook


# ---------------------------------------------------------------------------
# I-2 (v0.69.8): agent-home settings.json self-grant guard
# ---------------------------------------------------------------------------


_SETTINGS_JSON_SUFFIX = "/.claude/settings.json"

_SETTINGS_DENY_MSG = (
    "settings_guard: editing .claude/settings.json is not permitted — plugin "
    "grants are configurator-managed. Ask the configurator to install/enable a "
    "plugin instead of editing settings.json directly."
)

# A Bash command with a write operator (redirect / tee / dd / cp / mv /
# install / sed -i / truncate) appearing BEFORE a .claude/settings.json path —
# i.e. writing INTO settings.json. A bare `cat …/settings.json` (read) has no
# preceding write op and is allowed. Best-effort — see the Bash branch of
# make_agent_home_settings_guard.
_SETTINGS_JSON_WRITE_RE = re.compile(
    r"(?:>>?|\btee\b|\bdd\b|\bcp\b|\bmv\b|\binstall\b|sed\s+-i|\btruncate\b)"
    r".*?\.claude/settings\.json",
    re.IGNORECASE | re.DOTALL,
)

# Unified plugin architecture (§3.11/§3.13): /config/plugins/ (registry.json +
# the content-addressed store + staging) is the single plugin-assignment
# authority. An engagement with Write/Edit/Bash could self-assign plugins by
# editing the registry directly, bypassing plugin_add's validation + §3.9
# sequencing. Deny direct writes under it (same residual as I-2: an obfuscated
# `permission_mode: auto` Bash command can still slip through — the complete
# boundary is sandbox enforcement).
_PLUGINS_DIR_PREFIX = "/config/plugins/"
_PLUGINS_DENY_MSG = (
    "Direct writes under /config/plugins/ are refused. The plugin registry and "
    "store are the single assignment authority — mutate them via the "
    "configurator's plugin_add / plugin_update / plugin_assign / "
    "plugin_unassign / plugin_remove tools, never by hand."
)
_PLUGINS_WRITE_RE = re.compile(
    # A write-ish verb followed (anywhere) by the plugins path. Sol round-3 B3a:
    # broadened with chmod/chown/ln/touch (mode/link tamper) + a trailing-slash-
    # or word-boundary match so exact `/config/plugins` is covered too.
    r"(?:>>?|\btee\b|\bdd\b|\bcp\b|\bmv\b|\bln\b|\binstall\b|sed\s+-i|"
    r"\btruncate\b|\brm\b|\bmkdir\b|\bchmod\b|\bchown\b|\btouch\b|\bcd\b)"
    r".*?/config/plugins(?:/|\b)",
    re.IGNORECASE | re.DOTALL,
)
# Language-runtime write to the path (python/node/perl `open(...,'w')`, etc.).
# Targeted at WRITE modes so a plain READ of /config/plugins/store (the
# agent's legitimate access) is not denied. Best-effort — a
# determined obfuscated command still needs the filesystem/privilege boundary
# (spec integrity = content-addressing + checksum DETECTION; tracked backlog).
_PLUGINS_CODE_WRITE_RE = re.compile(
    r"/config/plugins\b[^\n]{0,80}?['\"][wax]\+?b?['\"]"
    r"|['\"][wax]\+?b?['\"][^\n]{0,80}?/config/plugins\b",
    re.IGNORECASE | re.DOTALL,
)


def make_agent_home_settings_guard() -> HookCallback:
    """Deny hand-edits to any ``.claude/settings.json`` (I-2) OR anything under
    ``/config/plugins/`` (unified plugin architecture §3.11/§3.13).

    settings.json is configurator-managed; no agent should hand-edit it. The
    plugin registry + store (``/config/plugins/``) is the single plugin-
    assignment authority (§3.13); a resident/executor with Write/Edit/Bash
    could otherwise self-assign a plugin by editing the registry directly,
    bypassing plugin_add's validation + §3.9 sequencing. Both guards match by
    normalized path, so ``..`` traversal can't slip a write through (see
    `_normalize_path`)."""
    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            ti = input_data.get("tool_input", {})
            raw = ti.get("file_path") or ti.get("notebook_path") or ""
            norm = _normalize_path(raw)
            if norm.endswith(_SETTINGS_JSON_SUFFIX):
                return _deny(_SETTINGS_DENY_MSG)
            # Sol round-3 B3a: cover the exact dir too (not just the trailing-
            # slash prefix).
            if norm.startswith(_PLUGINS_DIR_PREFIX) or norm == "/config/plugins":
                return _deny(_PLUGINS_DENY_MSG)
        elif tool_name == "Bash":
            # Finding 1 (codex review v0.69.10): residents with Bash (the assistant)
            # could bypass the file-tool guard with `echo … >
            # .claude/settings.json`. Deny a Bash command that names a
            # settings.json path AND looks like a write. This is best-effort
            # (a determined obfuscated command can still slip through — the
            # complete boundary is filesystem/sandbox enforcement or removing
            # residents' broad Bash; tracked in ROADMAP-backlog), but it
            # catches the realistic prompt-injection form (a plain redirect).
            command = input_data.get("tool_input", {}).get("command", "")
            if _SETTINGS_JSON_WRITE_RE.search(command):
                return _deny(_SETTINGS_DENY_MSG)
            if (_PLUGINS_WRITE_RE.search(command)
                    or _PLUGINS_CODE_WRITE_RE.search(command)):
                return _deny(_PLUGINS_DENY_MSG)
        return {}

    return _hook


def agent_home_settings_guard_matcher():
    """A ``HookMatcher`` wrapping :func:`make_agent_home_settings_guard`,
    injected code-side into every resident's PreToolUse hooks (I-2, v0.69.8)
    so the self-grant guard is an always-on invariant, not config-removable."""
    from claude_agent_sdk import HookMatcher
    return HookMatcher(
        # Sol round-3 B3a: include NotebookEdit — the hook body already handles
        # it, but the matcher must route it or NotebookEdit writes bypass entirely.
        matcher="Write|Edit|MultiEdit|NotebookEdit|Bash",
        hooks=[make_agent_home_settings_guard()],
    )


def profile_guard_matcher(plan):
    """S8: the ``HookMatcher`` that enforces a plugin access profile — deny
    unless the exact tool name is in the profile's ``allowed_names``, over
    every guarded namespace of the plan (``plugin_grants.ProfilePlan``).
    Injected code-side in every options builder beside
    :func:`agent_home_settings_guard_matcher`, so it holds whatever the
    allow/deny lists say and whatever the CLI's precedence between them is:
    a PreToolUse deny is evaluated before permission rules. ``None`` for an
    empty plan, so an unprofiled build is byte-identical to today's."""
    if plan is None or plan.empty:
        return None
    from claude_agent_sdk import HookMatcher
    by_prefix = {pre: entry for entry in plan.entries for pre in entry.prefixes}
    pattern = "|".join(re.escape(pre) + ".*" for pre in by_prefix)

    async def _hook(input_data: dict[str, Any], tool_use_id, context) -> dict[str, Any]:
        name = str(input_data.get("tool_name") or "")
        for pre, entry in by_prefix.items():
            if name.startswith(pre):
                if name in entry.allowed_names:
                    return {}
                return _deny(
                    f"{name} is outside the `{entry.profile}` profile of "
                    f"{entry.name} this agent was assigned")
        return {}

    return HookMatcher(matcher=pattern, hooks=[_hook])


def guest_pin_matcher(allowed: frozenset[str]):
    """The first PreToolUse matcher of a guest run: deny every tool whose
    name is not in *allowed* — built-ins, Casa's own tools and plugin tools
    alike (the pin of a stored-call turn lets plugin tools through to the
    broker; this one does not). The result broker's admission hook judges
    the same list as its own first check, because the CLI runs matching
    hooks concurrently and one hook's deny does not stop its siblings."""
    from claude_agent_sdk import HookMatcher
    names = frozenset(allowed)

    async def _hook(input_data: dict[str, Any], tool_use_id, context) -> dict[str, Any]:
        name = str((input_data or {}).get("tool_name") or "")
        if name in names:
            return {}
        return _deny(f"{name} is not available here")

    return HookMatcher(matcher=None, hooks=[_hook])


# ---------------------------------------------------------------------------
# managed_component_guard — #210 (v0.101.0): typed-pipeline-only state
#
# On a fresh install the configurator hand-authored specialist files under
# /config/agents/specialists/ via Bash instead of the typed
# specialist_install_* pipeline. Nothing blocked it: its path_scope makes
# /config/agents writable, and only /config/plugins/ Bash-writes were
# specially denied (the settings guard above). This policy denies ANY
# hand-edit — Write/Edit by normalized path, Bash by write-shaped commands —
# of the managed component trees, and ROUTES the model to the typed tool
# for the matched tree. Reads stay allowed: the configurator legitimately
# inspects these trees to verify pipeline outcomes.
#
# Fail-closed (#210, same rationale as make_resident_authz_hook in
# authz_grants.py): an ESCAPED callback exception becomes an SDK error
# control response, NOT a deny — so the whole body runs inside try/except
# and any unexpected Exception returns the deny shape (CancelledError
# re-raises). Same accepted residual as the other Bash guards: argv-level
# inspection, not a sandbox — obfuscated writes (source'd scripts, busybox
# applets, git-apply, interpreters reading code from files/stdin, and
# variable-built destinations like `d=/config; printf x > "$d/plugins/…"`)
# need the outer boundary. Round-2 W1 (ratified): for
# the CONFIGURATOR that Bash
# vector is now closed at the capability layer — Bash was removed from its
# toolset (role.yaml + definition.yaml). The Bash branch below stays fully
# intact: an operator who deliberately re-adds Bash to an executor still
# gets this guard as the inner layer.
#
# Round-2 hardening (Sol+Terra adversarial review):
#   F1  Bash commands also enforce the hooks.yaml policy-file rule.
#   F2  RELATIVE paths (cwd is /config — tools.py ClaudeAgentOptions) are
#       resolved against /config before the prefix test, for Write/Edit
#       file_path and for Bash path tokens.
#   F3  In addition to the lexical test, paths are os.path.realpath-resolved
#       so a symlink in a writable area can't tunnel into a managed tree;
#       OSError during resolution denies (fail-closed).
#   F4  Inline-interpreter invocations (python/perl/ruby/node -c/-e) that
#       mention a managed token deny regardless of write verbs.
#   F5  Write VERBS must sit in command position (wrapper prefixes allowed);
#       only true operators (>, >>, sed -i, perl -i, -delete, -exec) match
#       anywhere — `grep -r install /config/plugins/store` passes.
# ---------------------------------------------------------------------------


# Round-2 W5 (Sol): routes name the FULL lifecycle toolset per prefix, not
# just the install/add door — a deny on an upgrade/removal attempt must
# route to the matching typed tool, not send the model back to install.
_MANAGED_ROUTE_SPECIALISTS = (
    "Specialists: use specialist_install_inspect / specialist_install_commit, "
    "specialist_upgrade, specialist_rollback, specialist_uninstall "
    "(doctrine/recipes/specialist/)."
)
_MANAGED_ROUTE_PLUGINS = (
    "Plugins: use plugin_add / plugin_update / plugin_assign / "
    "plugin_unassign / plugin_remove (doctrine/recipes/plugin/)."
)
_MANAGED_ROUTE_BINDINGS = (
    "Bindings/personas: use resident_persona_swap / resident_persona_reset "
    "/ persona_apply (doctrine/recipes/persona/)."
)
_MANAGED_ROUTE_PERSONAS = (
    "Personas: use persona_install_inspect / persona_install_commit / "
    "persona_apply / persona_list / persona_remove / persona_prune / "
    "persona_ack_revoke (doctrine/recipes/persona/)."
)

# Managed prefix -> routing sentence. Prefixes are matched against
# _normalize_path output (so `..` traversal and `//` collapse can't slip
# through); first match wins (the prefixes are disjoint after normalization).
# Round-2 W2 (Sol): /config/personas added — persona installs are
# consent-gated exactly like specialists, so their tree is managed too.
_MANAGED_PREFIX_ROUTES: tuple[tuple[str, str], ...] = (
    ("/config/agents/specialists", _MANAGED_ROUTE_SPECIALISTS),
    ("/config/specialists", _MANAGED_ROUTE_SPECIALISTS),
    ("/config/bindings", _MANAGED_ROUTE_BINDINGS),
    ("/config/personas", _MANAGED_ROUTE_PERSONAS),
    ("/config/plugins", _MANAGED_ROUTE_PLUGINS),
)

_MANAGED_HOOKS_YAML_DENY = (
    "managed_component_guard: {tool} blocked — {path!r} is a hook-policy file "
    "(hooks.yaml under /config/agents/). An executor must never edit hook "
    "policy files; ask the user if a policy change is truly needed."
)

_MANAGED_INTERNAL_DENY = (
    "managed_component_guard: internal error — failing closed; the call was "
    "not executed."
)

_MANAGED_RESOLVE_DENY = (
    "managed_component_guard: {tool} blocked — could not resolve {path!r} "
    "(realpath error); failing closed. Use the typed pipeline tools for "
    "managed component state."
)


_TRIGGER_FILE_NAME = "triggers.yaml"

_TRIGGER_WRITE_DENY = (
    "trigger_file_write_guard: {tool} blocked — {path!r} is a resident's "
    "trigger file, and it has a second writer: the resident's own reminder "
    "tools, inside the Casa process. Editing it from here silently discards "
    "any reminder written since you read it. Use "
    "config_trigger_upsert(role=..., name=..., type=..., ...) to add or "
    "replace one trigger, or config_trigger_delete(role=..., name=...) to "
    "remove one; both leave every other entry's content unchanged. Then commit "
    "and casa_reload_triggers as usual. If you are a resident rather than the "
    "configurator, this file is not yours to edit at all: set_reminder and "
    "cancel_reminder manage your own reminders in it, and everything else in "
    "it is the operator's."
)

_TRIGGER_RESOLVE_DENY = (
    "trigger_file_write_guard: {tool} blocked — could not resolve {path!r} "
    "(realpath error); failing closed. Use config_trigger_upsert / "
    "config_trigger_delete for a resident's triggers.yaml."
)

_TRIGGER_UNRESOLVABLE_DENY = (
    "trigger_file_write_guard: {tool} blocked — {path!r} is a relative path and "
    "this call reported no working directory, so where it lands cannot be "
    "established; failing closed. Give an absolute path, or use "
    "config_trigger_upsert / config_trigger_delete for a resident's "
    "triggers.yaml."
)

_TRIGGER_INTERNAL_DENY = (
    "trigger_file_write_guard: internal error — failing closed; the call was "
    "not executed."
)


def _is_resident_trigger_file(norm: str) -> bool:
    """True iff ``norm`` (already normalized, absolute) names
    ``/config/agents/<role>/triggers.yaml`` for a single ``<role>`` segment.

    Scoped to that ONE depth on purpose. ``/config/agents/specialists/x/
    triggers.yaml`` is not a resident's file — the loader forbids the file
    there outright — and it is already managed state, denied by
    ``managed_component_guard``.
    """
    p = PurePosixPath(norm)
    if p.name != _TRIGGER_FILE_NAME:
        return False
    parent = p.parent
    return (str(parent.parent) == _RESIDENT_ROOT
            and parent.name not in ("", "specialists"))


def _trigger_file_real_hit(norm: str) -> "str | None":
    """``"hit"``, ``"resolve_error"`` or ``None`` — this guard's slice of the
    one classifier. Delegating to :func:`_managed_real_hit` is what buys the
    lexical-then-symlink resolution (a symlink from a writable area defeats the
    lexical test; realpath alone misses a not-yet-existing path whose lexical
    form is already the target) without a second copy of it.

    Another kind of hit is NOT this guard's business and reads as ``None``:
    ``/config/plugins/x/triggers.yaml`` is managed state, and the guard that
    owns managed state says so in its own words."""
    hit = _managed_real_hit(norm)
    if hit is None:
        return None
    if hit[0] == "resolve_error":
        return "resolve_error"
    return "hit" if hit[0] == "trigger_file" else None


def _managed_prefix_route(norm: str) -> tuple[str, str] | None:
    """Return ``(prefix, route)`` for the managed prefix containing ``norm``,
    or ``None``. ``norm`` must already be ``_normalize_path``-normalized."""
    for prefix, route in _MANAGED_PREFIX_ROUTES:
        if _has_prefix(norm, [prefix]):
            return prefix, route
    return None


def _managed_path_hit(norm: str) -> tuple[str, str, str] | None:
    """Lexical managed-state test for one normalized absolute path.

    Returns ``("prefix", <prefix>, <route>)`` when ``norm`` is under a
    managed prefix, ``("hooks_yaml", norm, "")`` when it names a hook-policy
    file under /config/agents/ (round-2 F1 shares this with the Bash
    branch), else ``None``.
    """
    hit = _managed_prefix_route(norm)
    if hit is not None:
        prefix, route = hit
        return ("prefix", prefix, route)
    if (norm.startswith(_RESIDENT_ROOT + "/")
            and PurePosixPath(norm).name == "hooks.yaml"):
        return ("hooks_yaml", norm, "")
    # #403: a resident's triggers.yaml is not managed component state — it is
    # the OPERATOR's file — but it IS a file no agent may write, for a
    # different reason: the resident's reminder tools write it from inside
    # Casa. It joins this classifier rather than getting a second one so the
    # normalize-then-realpath resolution is written once; the KIND is distinct,
    # so each guard says WHY in its own words. Only the file-tool paths reach
    # here — `trigger_file_write_guard`'s Bash half deliberately resolves
    # nothing (see it for why).
    if _is_resident_trigger_file(norm):
        return ("trigger_file", norm, "")
    return None


def _managed_real_hit(norm: str) -> tuple[str, str, str] | None:
    """Lexical test PLUS symlink resolution (round-2 F3).

    The lexical test alone is defeated by a symlink in a writable area that
    points into a managed tree; ``os.path.realpath`` alone would miss
    not-yet-existing paths whose lexical form is managed (realpath resolves
    only EXISTING ancestor components). So: lexical first, then realpath on
    a lexical miss. Any ``OSError`` during resolution returns a
    ``("resolve_error", ...)`` hit — the caller denies (fail-closed).
    """
    hit = _managed_path_hit(norm)
    if hit is not None:
        return hit
    try:
        real = os.path.realpath(norm)
    except OSError:
        return ("resolve_error", norm, "")
    if real != norm:
        return _managed_path_hit(_normalize_path(real))
    return None


# Path-shaped tokens in a Bash command (start at '/', stop at whitespace,
# quotes, backticks, and shell operators). Each token is normalized before
# the prefix test, so `//config//plugins/x` and
# `/config/agents/specialists/../../plugins/x` both resolve — and a
# `bash -c '...'` wrapper needs no unwrapping (the raw string still carries
# the path token).
_MANAGED_PATH_TOKEN_RE = re.compile(r"/[^\s'\"`;|&<>()]*")

# Round-2 F2: executors run with cwd=/config (tools.py sets
# ClaudeAgentOptions cwd="/config"), so `echo x > agents/specialists/x/
# runtime.yaml` names managed state without a single absolute token.
# Relative tokens whose FIRST path segment is a managed root name
# (optionally ./ or ../ prefixed) are extracted and resolved against
# /config. `config` is in the set for the cwd=/ spelling
# (`config/plugins/x`), covered by the "/"-join candidate. A BARE root word
# (`rm -rf plugins` from /config) also matches — fail-closed: a prose word
# like `plugins` in a write-shaped command is an accepted false positive.
_MANAGED_REL_TOKEN_RE = re.compile(
    r"(?<![\w./-])"
    r"(?:\.{1,2}/)*"
    r"(?:agents|specialists|bindings|personas|plugins|config)"
    r"(?:/[^\s'\"`;|&<>()]*|(?![\w.-]))"
)


def _token_candidates(tok: str) -> list[str]:
    """Normalized absolute candidates for one path token: an absolute token
    as-is; a relative token joined against /config (the executor cwd),
    against / (the ``config/...`` spelling), and with any leading ./ or ../
    segments stripped then joined against /config. The stripped candidate
    covers the deeper-cwd shape (``../specialists/x`` typed from
    /config/agents lands in a managed tree even though the same token from
    /config does not) — the fail-closed bias governs the tie."""
    if tok.startswith("/"):
        return [_normalize_path(tok)]
    out = [_normalize_path("/config/" + tok), _normalize_path("/" + tok)]
    stripped = re.sub(r"^(?:\.{1,2}/)+", "", tok)
    if stripped and stripped != tok:
        out.append(_normalize_path("/config/" + stripped))
    return out


def _token_resolves_managed(tok: str) -> bool:
    """True iff any resolution candidate of ``tok`` hits managed state
    (lexical or via symlink), the resolution errors, or the token is
    runtime-expanded (``$var`` / backtick) and therefore unknowable —
    ambiguity denies (round-2 W3, fail-closed)."""
    if not tok or "$" in tok or "`" in tok:
        return True
    return any(_managed_real_hit(c) is not None for c in _token_candidates(tok))


def _managed_candidate_paths(command: str) -> list[str]:
    """Normalized absolute candidates for every path-shaped token in a Bash
    command — absolute tokens plus relative tokens whose first segment is a
    managed root name (see ``_token_candidates`` for the join rules)."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(path: str) -> None:
        if path not in seen:
            seen.add(path)
            out.append(path)

    for tok in _MANAGED_PATH_TOKEN_RE.findall(command):
        for cand in _token_candidates(tok):
            _add(cand)
    for tok in _MANAGED_REL_TOKEN_RE.findall(command):
        for cand in _token_candidates(tok):
            _add(cand)
    return out

# Benign redirects, stripped BEFORE the write-operator test so the common
# verification forms (`cat x 2>/dev/null`, `cmd 2>&1 | head`, `>&2`) don't
# read as writes. Writing to /dev/null or duplicating an fd mutates nothing.
_MANAGED_BENIGN_REDIRECT_RE = re.compile(r"[0-9]*>>?\s*/dev/null\b|[0-9]*>&[0-9]+")

# Round-2 F5 restructure (Sol): the old single ANYWHERE regex matched write
# VERBS inside argument text — `grep -r install /config/plugins/store`
# false-denied on \binstall\b. Split into (a) operand-mutating operators
# that are write-shaped anywhere in the raw text — in-place edit flags and
# find's action predicates — and (b) verbs that must sit in COMMAND
# POSITION (argv[0] of a pipeline segment via _split_pipeline, allowing
# wrapper prefixes: sudo/nohup/timeout <arg>/env/command/xargs and the git
# subcommand shape). Benign redirects are stripped before the operator
# test. `find <managed> -delete` still denies (path-first, operator
# anywhere); the fail-closed bias keeps the operator class order-blind.
#
# Round-2 W3 refinement (Sol): redirects and the cp/rsync/install family
# are TARGET-aware — a redirect denies only when its target resolves
# managed or is unresolvable ($var, substitution, missing), and
# cp/rsync/install deny on a managed DESTINATION (copying FROM a managed
# tree elsewhere is a legitimate read). Blanket any-operand treatment
# stays for verbs that mutate their operands (rm/rmdir/ln/touch/mkdir/
# chmod/chown/truncate/shred/tee/dd/tar/unzip, sed -i/perl -i,
# -delete/-exec) AND for mv — mv also mutates its SOURCE (removes it), so
# destination-only checking would let `mv /config/plugins/x /tmp` gut a
# managed tree. Ambiguous shapes deny (fail-closed).
_MANAGED_WRITE_ANYWHERE_RE = re.compile(
    r"(?:\bsed\s+-i|\bperl\s+-i|-delete\b|-exec\b)",
    re.IGNORECASE | re.DOTALL,
)

# A redirect and its target token (scanned AFTER the benign strip). An
# empty/unparseable target (quoted, `$var`, `>(cmd)`, end-of-string) is
# ambiguous -> treated as managed by _token_resolves_managed.
_MANAGED_REDIRECT_RE = re.compile(r"[0-9]*>>?\s*([^\s'\"`;|&<>()]*)")

_MANAGED_BLANKET_WRITE_VERBS = frozenset({
    "tee", "dd", "mv", "mkdir", "rm", "rmdir", "ln", "touch",
    "chmod", "chown", "truncate", "shred", "tar", "unzip",
})
_MANAGED_DEST_WRITE_VERBS = frozenset({"cp", "rsync", "install"})
# Round-7 (Terra): git subcommands are ALLOWLISTED read-only, not
# denylisted write — git has dozens of mutating subcommands (apply, am,
# stash, mv, rm, worktree, config, checkout-index, ...) and enumerating
# them is a losing game. Any subcommand not in this set counts as
# write-shaped when a managed token is present (fail-closed).
# Round-9 (Sol): multi-action subcommands whose DEFAULT action is a read
# but which mutate via sub-actions or options (reflog expire/delete,
# fsck --lost-found) are excluded outright — the option allowlist below
# cannot see sub-action operands.
_MANAGED_GIT_READONLY_SUBCMDS = frozenset({
    "log", "show", "diff", "status", "blame", "shortlog", "describe",
    "rev-parse", "rev-list", "ls-files", "ls-tree", "ls-remote",
    "cat-file", "grep", "name-rev", "merge-base",
    "help", "version", "var", "check-ignore",
    "check-attr", "show-ref",
})
# Round-9 (Sol): the same inversion at OPTION level — even read-only
# subcommands carry command-executing or writing options (`grep
# --open-files-in-pager=<cmd>` / `-O<pager>`). Post-subcommand options
# must match this conservative safe set (exact name, `name=`-prefixed
# value form, or a bare `-<digits>` count); ANYTHING else is
# write-shaped when a managed token is present. Operands (revisions,
# paths, everything after `--`) are not options and pass freely.
_MANAGED_GIT_SAFE_OPTS = frozenset({
    "-p", "-n", "-s", "-u", "-w", "-c", "-r", "-t", "-z", "-l",
    "--oneline", "--stat", "--numstat", "--shortstat", "--name-only",
    "--name-status", "--graph", "--decorate", "--all", "--short",
    "--long", "--porcelain", "--cached", "--staged", "--no-color",
    "--follow", "--reverse", "--patch", "--no-patch", "--raw",
    "--first-parent", "--merges", "--no-merges", "--line-number",
    "--count", "--ignore-case", "-i", "--fixed-strings", "-F",
    "--extended-regexp", "-E", "--recursive", "--no-pager",
})
_MANAGED_GIT_SAFE_OPT_PREFIXES = (
    "--pretty=", "--format=", "--color=", "--abbrev=", "--max-count=",
    "--date=", "--since=", "--until=", "--grep=", "--author=",
    "--diff-filter=", "--relative-date",
)


def _dest_family_writes_managed(argv: list[str]) -> bool:
    """cp/rsync/install mutate their DESTINATION (round-2 W3): True iff the
    last path operand resolves managed, an explicit ``-t`` /
    ``--target-directory`` target resolves managed, or the operand shape is
    ambiguous (options after operands, ``--remove-source*``, a single
    operand, no parseable target) AND any operand resolves managed.
    Copying FROM a managed tree elsewhere is a legitimate read.

    #348: ``-t``/``--target-directory`` designates the destination for
    cp/install ONLY (rsync's ``-t`` is preserve-times, a plain flag). When
    cp/install carry an explicit UNMANAGED target, every remaining operand
    is a SOURCE — ``cp -t /tmp <managed-file>`` copies an inspectable
    artifact OUT, the documented read allowance, and must not be flagged
    just because a sources-only remainder has fewer than two operands."""
    prog = os.path.basename(argv[0])
    t_takes_target = prog in ("cp", "install")
    dest_unmanaged = False
    args = argv[1:]
    operands: list[str] = []
    ambiguous = False
    seen_ddash = False
    i = 0
    while i < len(args):
        a = args[i]
        if not seen_ddash and a == "--":
            seen_ddash = True
            i += 1
            continue
        if not seen_ddash and a.startswith("-") and a != "-":
            if operands:
                ambiguous = True  # option-terminated / interleaved options
            if t_takes_target and a in ("-t", "--target-directory"):
                if i + 1 < len(args):
                    if _token_resolves_managed(args[i + 1]):
                        return True
                    dest_unmanaged = True
                    i += 2
                    continue
                ambiguous = True
            elif t_takes_target and a.startswith("--target-directory="):
                if _token_resolves_managed(a.split("=", 1)[1]):
                    return True
                dest_unmanaged = True
            elif t_takes_target and a.startswith("-t") and len(a) > 2:
                # GNU cp/install attached form ``-tDIR`` (round-3 S2). Only
                # a slash-bearing suffix is treated as a target; a bare
                # suffix could be a cluster (``-tv``) and marks the shape
                # ambiguous (fail-closed).
                suffix = a[2:]
                if "/" in suffix:
                    if _token_resolves_managed(suffix):
                        return True
                    dest_unmanaged = True
                else:
                    ambiguous = True
            elif a.startswith("--remove-source"):
                ambiguous = True  # rsync mutates its SOURCE too
            i += 1
            continue
        operands.append(a)
        i += 1
    if not operands:
        return False
    if dest_unmanaged and not any(
            x.startswith("--remove-source") for x in args):
        # Explicit unmanaged cp/install target: remaining operands are all
        # sources; reading them out of a managed tree is legitimate.
        return False
    if len(operands) < 2:
        ambiguous = True  # cannot tell source from destination
    targets = operands if ambiguous else operands[-1:]
    return any(_token_resolves_managed(t) for t in targets)


def _argv_managed_write(argv: list[str], *, _depth: int = 0) -> bool:
    """True iff this pipeline segment's command-position program writes into
    managed state: blanket operand-mutating verbs (the caller holds the
    managed-token precondition), destination-aware cp/rsync/install, and
    git write subcommands — unwrapping wrapper prefixes (``sudo``,
    ``nohup``, ``timeout <arg>``, ``env``, ``command``, ``xargs``, ...),
    wrapper shells (``bash -c '...'``) and ``eval``."""
    if _depth > 3 or not argv:
        return False
    prog = os.path.basename(argv[0])
    if prog in _MANAGED_BLANKET_WRITE_VERBS:
        return True
    if prog in _MANAGED_DEST_WRITE_VERBS:
        return _dest_family_writes_managed(argv)
    if prog == "git":
        # Round-8 (Sol): even read-only subcommands write via
        # `--output[=]<file>` — resolve the destination like a redirect
        # target: managed or unresolvable denies; writing elsewhere from a
        # managed read stays legal.
        for k, a in enumerate(argv[1:], start=1):
            if a.startswith("--output="):
                if _token_resolves_managed(a.split("=", 1)[1]):
                    return True
            elif a == "--output":
                if k + 1 >= len(argv) or _token_resolves_managed(argv[k + 1]):
                    return True
        j = 1
        while j < len(argv):
            a = argv[j]
            if a == "-C":  # takes a separate argument
                j += 2
                continue
            if a == "--no-pager":
                j += 1
                continue
            if a.startswith("-"):
                # Round-8 (Terra): any OTHER pre-subcommand option is
                # write-shaped — `-c core.pager='sh -c ...'`, `--paginate`,
                # `--exec-path`, `--config-env` etc. can execute arbitrary
                # commands, and the payload is argv-visible (NOT the
                # opaque-content residual). Only -C <dir> and --no-pager
                # are recognized read-safe here.
                return True
            if a not in _MANAGED_GIT_READONLY_SUBCMDS:
                return True
            # Round-9 (Sol): post-subcommand options must ALSO be
            # allowlisted — `grep --open-files-in-pager=<cmd>` / `-O<pager>`
            # execute arbitrary commands from a "read-only" subcommand.
            # Everything after `--` is operands (paths/revisions), never
            # options. `--output` was resolved above.
            past_ddash = False
            for opt in argv[j + 1:]:
                if opt == "--":
                    past_ddash = True
                    continue
                if past_ddash or not opt.startswith("-") or opt == "-":
                    continue
                if opt in _MANAGED_GIT_SAFE_OPTS:
                    continue
                if opt.startswith(_MANAGED_GIT_SAFE_OPT_PREFIXES):
                    continue
                if re.fullmatch(r"-\d+", opt):  # `git log -5`
                    continue
                if opt == "--output" or opt.startswith("--output="):
                    continue  # already resolved above
                return True
            return False
        # Bare `git` / options-only: no subcommand reached — treat as
        # write-shaped only if we couldn't classify (fail-closed keeps
        # parity with the unparseable-shape rule elsewhere); a bare `git`
        # mutates nothing, so allow.
        return False
    if prog == "command":
        j = 1
        while j < len(argv) and argv[j].startswith("-"):
            j += 1
        return _argv_managed_write(argv[j:], _depth=_depth + 1)
    if prog == "xargs":
        wrapped = _xargs_wrapped_argv(argv)
        return bool(wrapped) and _argv_managed_write(wrapped, _depth=_depth + 1)
    if prog in _EXEC_WRAPPER_ARG_FLAGS:
        wrapped = _exec_wrapper_tail(argv)
        return bool(wrapped) and _argv_managed_write(wrapped, _depth=_depth + 1)
    if prog in _WRAPPER_SHELLS:
        for j in range(1, len(argv) - 1):
            if argv[j] == "-c":
                return _managed_write_shape(argv[j + 1], _depth=_depth + 1)
        return False
    if prog == "eval" and len(argv) > 1:
        return _managed_write_shape(" ".join(argv[1:]), _depth=_depth + 1)
    return False


def _managed_write_shape(command: str, *, _depth: int = 0) -> bool:
    """True iff the command writes into managed state, given the caller's
    managed-token precondition: an operand-mutating OPERATOR anywhere
    (after stripping benign /dev/null and fd-dup redirects), a redirect
    whose TARGET resolves managed or is unresolvable, or a write VERB in
    command position of any pipeline segment (destination-aware for the
    cp/rsync/install family — round-2 W3)."""
    if _depth > 3:
        return False
    scan = _MANAGED_BENIGN_REDIRECT_RE.sub(" ", command)
    if _MANAGED_WRITE_ANYWHERE_RE.search(scan):
        return True
    for m in _MANAGED_REDIRECT_RE.finditer(scan):
        if _token_resolves_managed(m.group(1)):
            return True
    # Round-10 (Sol): env-assignment-prefixed commands are write-shaped —
    # `GIT_EXTERNAL_DIFF='sh -c "cp ... <managed>"' git diff ...` (or
    # LD_PRELOAD/PATH injection) modifies EXECUTION, and _split_pipeline
    # strips the assignments before classification ever sees them. One
    # rule ends the variable-enumeration game; a rare read like
    # `LANG=C grep ... <managed>` false-denies only when a managed token
    # is present (fail-closed; the configurator has no shell at all).
    if _MANAGED_ENV_PREFIX_RE.search(command):
        return True
    return any(_argv_managed_write(argv, _depth=_depth)
               for argv in _split_pipeline(command))


# Segment-leading VAR=... (start of command or right after ; | & && ||).
# Anchored to segment starts so `grep "X=1" <path>` never matches.
_MANAGED_ENV_PREFIX_RE = re.compile(
    r"(?:^|[;|&]\s*)[A-Za-z_][A-Za-z0-9_]*=")


# Round-2 F4 (Sol): interpreter escape — `python3 -c "open('agents/...',
# 'w')..."` mutates managed state only inside inline code, where the write
# operator/verb tests cannot see it. When an interpreter runs INLINE CODE
# (-c/-e/-E/--eval style) AND any managed token appears anywhere in the
# command, deny regardless of write verbs — the code body is opaque, so a
# read-only inline one-liner over a managed path is also denied (accepted
# false positive; fail-closed). Accepted residual, unchanged in kind from
# round 1: this is argv-level inspection, not a sandbox — an interpreter
# reading code from stdin or a script file, or assembling paths at runtime
# inside the code, still needs the outer filesystem/privilege boundary.
_MANAGED_INTERPRETER_RE = re.compile(r"python[0-9.]*|perl|ruby|node|nodejs")


def _argv_is_inline_interpreter(argv: list[str]) -> bool:
    """True iff argv invokes python/perl/ruby/node with an inline-code
    flag (-c, -e, -E, --eval, --print, or a short-flag cluster carrying
    one, e.g. ``perl -we``)."""
    if not argv:
        return False
    if _MANAGED_INTERPRETER_RE.fullmatch(os.path.basename(argv[0])) is None:
        return False
    for a in argv[1:]:
        if a in ("--eval", "--print"):
            return True
        if (a.startswith("-") and not a.startswith("--")
                and any(ch in a[1:] for ch in "ceE")):
            return True
    return False


def _managed_inline_interpreter(command: str, *, _depth: int = 0) -> bool:
    """True iff any pipeline segment (unwrapping wrapper shells, ``eval``,
    ``xargs``, and exec-wrapper prefixes) invokes an inline interpreter."""
    if _depth > 3:
        return False
    for argv in _split_pipeline(command):
        if _argv_is_inline_interpreter(argv):
            return True
        prog = os.path.basename(argv[0]) if argv else ""
        if prog in _WRAPPER_SHELLS:
            for j in range(1, len(argv) - 1):
                if argv[j] == "-c" and _managed_inline_interpreter(
                        argv[j + 1], _depth=_depth + 1):
                    return True
        elif prog == "eval" and len(argv) > 1:
            if _managed_inline_interpreter(
                    " ".join(argv[1:]), _depth=_depth + 1):
                return True
        elif prog == "xargs":
            wrapped = _xargs_wrapped_argv(argv)
            if wrapped and _argv_is_inline_interpreter(wrapped):
                return True
        elif prog in _EXEC_WRAPPER_ARG_FLAGS:
            wrapped = _exec_wrapper_tail(argv)
            if wrapped and _argv_is_inline_interpreter(wrapped):
                return True
    return False


def _bash_managed_prefix_route(command: str) -> tuple[str, str, str] | None:
    """Return the first managed hit — ``("prefix", prefix, route)``,
    ``("hooks_yaml", path, "")`` or ``("resolve_error", path, "")`` — for
    any path-shaped token (absolute or relative, lexical or
    symlink-resolved) in the Bash command, else ``None``. A prefix or
    hooks_yaml hit alone is not a deny — the caller also requires a write
    shape or an inline-interpreter invocation; resolve_error always
    denies (fail-closed)."""
    for norm in _managed_candidate_paths(command):
        hit = _managed_real_hit(norm)
        if hit is not None:
            return hit
    return None


def make_managed_component_guard() -> HookCallback:
    """Deny hand-edits of managed component state (#210, round-2 hardened).

    Write/Edit: denied when the ``file_path`` — resolved against /config
    when relative (F2), normalized, and symlink-resolved (F3) — is under
    any managed prefix or names a ``hooks.yaml`` under ``/config/agents/``
    (policy-file self-editing). Bash: denied when the command carries a
    managed path token (absolute or relative, lexical or symlink-resolved;
    hooks.yaml included — F1) AND is write-shaped (F5: operators anywhere,
    verbs in command position) or invokes an inline interpreter (F4);
    read-only mentions pass. A realpath OSError denies (fail-closed).
    """
    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            tool_name = input_data.get("tool_name", "")
            if tool_name in ("Write", "Edit"):
                raw = input_data.get("tool_input", {}).get("file_path", "")
                # Round-2 F2: cwd is /config (tools.py ClaudeAgentOptions),
                # so a RELATIVE file_path can name managed state.
                norm = _normalize_path(
                    raw if raw.startswith("/") else "/config/" + raw)
                hit = _managed_real_hit(norm)
                if hit is not None:
                    kind, _subject, route = hit
                    if kind == "hooks_yaml":
                        return _deny(_MANAGED_HOOKS_YAML_DENY.format(
                            tool=tool_name, path=raw))
                    if kind == "resolve_error":
                        return _deny(_MANAGED_RESOLVE_DENY.format(
                            tool=tool_name, path=raw))
                    if kind == "trigger_file":
                        # Shares the classifier, not the reason (#403).
                        return _deny(_TRIGGER_WRITE_DENY.format(
                            tool=tool_name, path=raw))
                    return _deny(
                        f"managed_component_guard: {tool_name} blocked — "
                        f"{raw!r} is managed component state; hand-editing "
                        f"is forbidden. {route}"
                    )
            elif tool_name == "Bash":
                command = input_data.get("tool_input", {}).get("command", "")
                hit = _bash_managed_prefix_route(command)
                if hit is not None:
                    kind, subject, route = hit
                    if kind == "resolve_error":
                        return _deny(_MANAGED_RESOLVE_DENY.format(
                            tool="Bash", path=subject))
                    write_shape = _managed_write_shape(command)
                    if write_shape or _managed_inline_interpreter(command):
                        if kind == "hooks_yaml":
                            return _deny(_MANAGED_HOOKS_YAML_DENY.format(
                                tool="Bash", path=subject))
                        if kind == "trigger_file":
                            return _deny(_TRIGGER_WRITE_DENY.format(
                                tool="Bash", path=subject))
                        verb = ("writes into" if write_shape
                                else "runs inline interpreter code against")
                        return _deny(
                            f"managed_component_guard: Bash blocked — this "
                            f"command {verb} managed component state "
                            f"({subject}/); hand-editing is forbidden. "
                            f"{route}"
                        )
            return {}
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — fail closed, never let it escape
            _logger.exception(
                "managed_component_guard internal error — denying")
            return _deny(_MANAGED_INTERNAL_DENY)

    return _hook


def managed_component_guard_matcher():
    """A ``HookMatcher`` wrapping :func:`make_managed_component_guard`,
    injected CODE-SIDE into every executor session (round-4 Terra P0) the
    same way :func:`agent_home_settings_guard_matcher` is (I-2, v0.69.8).

    Rationale: definition.yaml editing is a legitimate configurator recipe
    (executor/enable|disable|edit-definition), and definition.yaml's
    ``hooks_file:`` key is a config-editable POINTER to the hook-policy
    file. hooks.yaml itself is guard-protected, but repointing ``hooks_file``
    at a hollow yaml would shed every yaml-declared policy on the next
    session. Code-side injection makes the yaml additive-only: no
    definition/hooks-file manipulation can remove this guard."""
    from claude_agent_sdk import HookMatcher
    policy = HOOK_POLICIES["managed_component_guard"]
    return HookMatcher(
        matcher=policy["matcher"],
        hooks=[policy["factory"]()],
    )


# ---------------------------------------------------------------------------
# trigger_file_write_guard (#403) — agents/<role>/triggers.yaml has TWO writers
# ---------------------------------------------------------------------------
#
# The other one is the resident's own reminder tools, which read-modify-write
# the whole document inside the Casa process. An executor is a separate CLI
# child process, so its Read→Edit spans model thinking time: a reminder set in
# that window is silently discarded by the stale rewrite, and config_git_commit's
# `git add -A` commits the loss. No lock can fence that — the only fix is one
# writer, so the edit is denied here and routed to config_trigger_upsert /
# config_trigger_delete, which perform it in-process on the loop.
#
# triggers.yaml is the OPERATOR's file — hand-editable by the operator and
# readable by every agent — so this is a separate policy from
# managed_component_guard rather than a widening of it. They share the path
# classifier and each denies in its own words; what is forbidden is an AGENT
# writing it.

# Shell quoting characters, stripped from the WHOLE command before it is
# searched for the filename. `triggers"."yaml` is one word to bash and a
# different word to a naive parser (Sol r1-2); the concatenation is invisible
# unless the quotes come off first.
_SHELL_QUOTE_CHARS = str.maketrans("", "", "'\"\\")

def _command_mentions_a_trigger_file(command: str) -> bool:
    """True iff *command*'s text names ``triggers.yaml``, with shell quoting
    removed from the WHOLE command first.

    Stripping the quotes globally rather than per token is what makes the
    splice forms one case instead of many: ``triggers.""yaml``,
    ``triggers"."yaml``, ``\'triggers.yaml\'`` and ``tri\'\'ggers.yaml`` all
    reduce to the same text, and a redirect target, a verb operand and an
    interpreter body are no longer different parsing problems.
    """
    return _TRIGGER_FILE_NAME in command.translate(_SHELL_QUOTE_CHARS)


# Commands that read a file and cannot write one. An ALLOWLIST, and that is the
# whole point: the write side is an open namespace — a denylist of write verbs
# still missed `command cp`, `env cp` and `bash -c "cp …"`, because a wrapper
# can put any program in argv[0]. Anything not named here
# is treated as a write, so an unfamiliar verb costs a refusal rather than a
# lost reminder. Kept small on purpose: it only has to cover what the recipes
# actually tell an agent to do with this file, which is look at it.
# Deliberately ABSENT: `sed` (`-i` edits in place), `awk`/`yq`/`jq` (all can
# write), every interpreter, and `find` (`-delete`, `-exec`).
# Round 3 pruned three of these after Sol demonstrated each writing a file:
# `xxd infile OUTFILE` takes a second operand, `rg --pre=<prog>` executes an
# arbitrary preprocessor (`--pre=tee` rewrites the file it is reading), and
# `file -C` compiles a magic database. An allowlist is only as good as its
# audit, which is the price of it being the safe direction.
_TRIGGER_READ_ONLY_VERBS = frozenset({
    "cat", "head", "tail", "wc", "nl", "od",
    "grep", "egrep", "fgrep", "zgrep",
    "ls", "stat", "readlink", "realpath", "basename", "dirname",
    "cmp", "md5sum", "sha1sum", "sha256sum", "cksum",
    "cd", "pwd", "echo", "printf", "true", "test", "[",
})

# Any substitution makes a segment's argv[0] a lie about what runs:
# `echo $(cp /tmp/x triggers.yaml)` is headed by an allowlisted program and
# writes the file (Sol r3-2). None of the reads above needs one, so their
# presence simply ends the read-only claim.
_SUBSTITUTION_RE = re.compile(r"\$\(|`|<\(|>\(")


def _provably_read_only(command: str) -> bool:
    """True only when every segment of *command* is a recognised read-only
    program AND the command contains no redirect.

    The inverse of the question the previous three rounds asked. "Does this
    write" is undecidable over an open set of programs and spellings; "is every
    program here one of these twenty, and is there no `>`" is decidable over a
    closed one. Unknown reads as a write, which is the fail-closed direction.

    Benign redirects (`2>/dev/null`, `>&2`) are stripped first, so the ordinary
    verification forms are not mistaken for writes — the same courtesy the
    managed classifier extends. A command substitution, backtick or process
    substitution anywhere ends the claim outright: it makes argv[0] a lie about
    what runs, and no read this set covers needs one.
    """
    if _SUBSTITUTION_RE.search(command):
        return False
    scan = _MANAGED_BENIGN_REDIRECT_RE.sub(" ", command)
    if _MANAGED_REDIRECT_RE.search(scan):
        return False
    segments = _split_pipeline(command)
    if not segments:
        return False
    for argv in segments:
        if not argv:
            return False
        prog = argv[0]
        if "/" in prog:
            # A PATH-QUALIFIED program is never allowlisted. The set names
            # programs, not files, and `/data/engagements/e1/cat` is an
            # executable an agent wrote itself (Terra r3-2) — basename matching
            # alone would let it inherit `cat`'s reputation. This does not close
            # the class (a shadowing `cat` earlier in PATH still runs), which is
            # part of why the Bash half is a backstop and not a boundary (#460).
            return False
        if prog == "git":
            # `git` earns an exception because the read/write split for it is
            # already decided, audited and tested next door: the managed
            # guard's `_MANAGED_GIT_READONLY_SUBCMDS` allowlist plus its
            # option handling. Without it `git diff -- triggers.yaml` — a normal
            # read-only action on an unrelated file — is refused with no
            # equivalent (Terra r3-3), since a file tool cannot produce a diff.
            if _argv_managed_write(argv):
                return False
            continue
        if prog not in _TRIGGER_READ_ONLY_VERBS:
            return False
    return True


def make_trigger_file_write_guard() -> HookCallback:
    """Deny an agent's direct write of a resident's ``triggers.yaml``.

    **Write/Edit/MultiEdit/NotebookEdit is decidable for a path that RESOLVES,
    and this half is sound for exactly that.** A hard link is the same inode
    under another name and ``realpath`` reports the alias; a symlink retargeted
    between this check and the write is a race no pre-tool hook can close. Both
    are #460's family, not defects here.
    The path is a literal, resolved against the SESSION's own working directory
    — residents run from their agent home, not from ``/config``, so a fixed
    ``/config`` base made `../../agents/<role>/triggers.yaml` resolve to
    something that does not exist and read as allowed (Sol r1-1). Then
    normalized and symlink-resolved; a realpath ``OSError`` denies.

    **Bash asks two deliberately coarse questions, because the precise ones have
    no answer.** Four review rounds tried to decide, from the command text,
    WHICH path a shell writes to and WHETHER it writes. Each round closed the
    previous round's spelling and the next found another — a bare basename after
    a ``cd``, a quote-spliced name, an opaque interpreter body, a redirect target
    whose quotes the parser stopped at, a ``$PWD`` only bash expands,
    ``command cp`` / ``env cp`` / ``bash -c "cp …"`` past a write-verb list.
    That is not a run of bugs; both questions range over open sets — a shell
    computes its destination at runtime, and a wrapper can put any program in
    argv[0]. So both judgments are gone, and what is left is decidable over
    CLOSED sets:

    * does the command text name ``triggers.yaml`` at all, with shell quoting
      stripped from the whole command first (so every splice form is one case);
    * is it *provably read-only* — no substitution of any kind, no redirect,
      and every pipeline segment a program named WITHOUT a path in
      :data:`_TRIGGER_READ_ONLY_VERBS`, with ``git`` admitted by subcommand
      through the managed guard's own audited read-only set.

    Named and not provably read-only ⇒ deny. Unknown reads as a write, which is
    the fail-closed direction and the reason this is an allowlist: the write side
    cannot be enumerated, the read side can, and it only has to cover what the
    recipes tell an agent to do with this file — look at it.

    Deliberately over-broad, and the breadth is the cheap direction: a shell
    write to ANY file of that name is refused wherever it lives, and an
    unrecognised read verb is refused too. The way through is the file tools,
    whose paths are literal and resolved exactly, or the typed
    ``config_trigger_*`` tools for a resident's own file.

    **The Bash half is a backstop, NOT a boundary, and nothing should be built
    on it.** Four review rounds produced seventeen bypasses; the last one that
    cannot be closed is bash's own quoting: ``tri$\'\'ggers.yaml`` and a
    backslash-newline continuation both name the file to the shell while naming
    something else to any parser, and ANSI-C quoting can encode any character.
    Nor is the read allowlist truly closed while an agent can put its own ``cat``
    earlier in ``PATH``.
    A script file, or a path assembled from parts, does not name it at all.
    So this half catches the *accidental* form — the one a model following an
    old recipe would actually type — and claims nothing more; INV-TRIG-011 is
    scoped to the file-tool half for exactly that reason. The real boundary for
    an agent with broad shell access is filesystem enforcement or not having a
    shell over ``/config/agents`` (#460); this is the same residual
    ``make_agent_home_settings_guard`` records for ``settings.json``.
    """
    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            tool_name = input_data.get("tool_name", "")
            # The session's real cwd. Executors run from /config; residents run
            # from their agent home. Never assume one of them.
            reported_cwd = input_data.get("cwd")
            cwd = str(reported_cwd or "/config")
            if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                ti = input_data.get("tool_input", {})
                raw = ti.get("file_path") or ti.get("notebook_path") or ""
                if not reported_cwd and not raw.startswith("/"):
                    # `cwd` is a REQUIRED field of the SDK's hook input, so its
                    # absence means the payload did not come from the CLI.
                    # Without it a relative path cannot be resolved AT ALL, and
                    # the /config fallback below would resolve a resident's
                    # `../../agents/<role>/triggers.yaml` to a path that does not
                    # exist and read as allowed. Every relative path denies, not
                    # just one whose basename matches: a relative SYMLINK names
                    # nothing recognisable and still resolves to the file
                    # (Terra r2-3). Naming it absolutely is the way through.
                    return _deny(_TRIGGER_UNRESOLVABLE_DENY.format(
                        tool=tool_name, path=raw))
                norm = _normalize_path(
                    raw if raw.startswith("/") else cwd.rstrip("/") + "/" + raw)
                hit = _trigger_file_real_hit(norm)
                if hit == "resolve_error":
                    return _deny(_TRIGGER_RESOLVE_DENY.format(
                        tool=tool_name, path=raw))
                if hit == "hit":
                    return _deny(_TRIGGER_WRITE_DENY.format(
                        tool=tool_name, path=raw))
            elif tool_name == "Bash":
                command = input_data.get("tool_input", {}).get("command", "")
                if (_command_mentions_a_trigger_file(command)
                        and not _provably_read_only(command)):
                    return _deny(_TRIGGER_WRITE_DENY.format(
                        tool="Bash", path=_TRIGGER_FILE_NAME))
            return {}
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — fail closed, never let it escape
            _logger.exception(
                "trigger_file_write_guard internal error — denying")
            return _deny(_TRIGGER_INTERNAL_DENY)

    return _hook


# ---------------------------------------------------------------------------
# response_shape_write_guard (#610) — the file is read into a fallback prompt
# that no bundle-bound resident is served
# ---------------------------------------------------------------------------
#
# `agents/<role>/response_shape.yaml` is read on every load and renders only
# into `_compose_prompt`, whose output is `cfg.system_prompt` — the composed
# FALLBACK, which `agent.py` serves ONLY when there is no
# compiled bundle (INV-PERS-001). All three resident role artifacts declare
# `persona.policy: required`, so a resident is bundle-bound from its first boot
# and no part of the fallback reaches the model while that bundle is active.
# #549 made the ROLE ARTIFACT's
# `response:` block the live source; it did not retire this file, and the
# configurator's recipe still pointed at it.
#
# So an edit here is written, committed to the config repo, and reported live
# with an explicit "no reload needed" justification — while the prompt digest is
# byte-identical before and after. That is the defect: not a lost update (which
# is triggers.yaml's problem), but a change that cannot take effect being
# reported as done. The way to actually change how a resident expresses itself
# is its persona pack, which the denial names.
#
# ONE directory depth, deliberately. `agents/specialists/**` is managed state
# that `managed_component_guard` already denies in its own words, and
# `TIER_FILES["executor"]` FORBIDS this file outright, so neither is claimed
# here — two guards on one path, and neither on the reason, is the shape the
# trigger guard documents avoiding.

_RESPONSE_SHAPE_FILE_NAME = "response_shape.yaml"

_RESPONSE_SHAPE_WRITE_DENY = (
    "response_shape_write_guard: {tool} blocked — {path!r} is read and "
    "rendered into the composed FALLBACK prompt, and for a persona-bound "
    "resident that fallback is not served: its base prompt is its COMPILED "
    "BUNDLE (the persona plus the role artifact's own response block) for as "
    "long as that bundle is active. So editing this file would be committed "
    "and reported live while changing nothing the model sees. To change how a "
    "resident writes or speaks, change its PERSONA: install the pack that "
    "says it (doctrine/recipes/persona/install.md) and apply it "
    "(doctrine/recipes/persona/apply.md), then restart that resident. Reading "
    "this file is fine — it is only the edit that would be a lie."
)

_RESPONSE_SHAPE_RESOLVE_DENY = (
    "response_shape_write_guard: {tool} blocked — could not resolve {path!r} "
    "(realpath error); failing closed. A resident's response shape comes from "
    "its persona pack; see doctrine/recipes/persona/."
)

_RESPONSE_SHAPE_UNRESOLVABLE_DENY = (
    "response_shape_write_guard: {tool} blocked — {path!r} is a relative path "
    "and this call reported no working directory, so where it lands cannot be "
    "established; failing closed. Give an absolute path."
)

_RESPONSE_SHAPE_INTERNAL_DENY = (
    "response_shape_write_guard: internal error — failing closed; the call was "
    "not executed."
)


def _is_resident_response_shape_file(norm: str) -> bool:
    """True iff ``norm`` (normalized, absolute) names
    ``/config/agents/<role>/response_shape.yaml`` for a SINGLE ``<role>``
    segment — the resident tier only. Mirrors
    :func:`_is_resident_trigger_file`, including its exclusion of the
    ``specialists`` subtree, for the reasons in the block comment above.
    """
    p = PurePosixPath(norm)
    if p.name != _RESPONSE_SHAPE_FILE_NAME:
        return False
    parent = p.parent
    return (str(parent.parent) == _RESIDENT_ROOT
            and parent.name not in ("", "specialists", "executors"))


def _command_mentions_a_response_shape_file(command: str) -> bool:
    """True iff *command*'s text names ``response_shape.yaml``, with shell
    quoting stripped from the WHOLE command first — so ``response_shape"."yaml``
    and friends reduce to one case rather than many spellings."""
    return _RESPONSE_SHAPE_FILE_NAME in command.translate(_SHELL_QUOTE_CHARS)


def make_response_shape_write_guard() -> HookCallback:
    """Deny an agent's write of a resident's ``response_shape.yaml``.

    The file-tool half is precise: the literal path is resolved against the
    session's own working directory (residents run from their agent home, not
    ``/config``), normalized and symlink-resolved, and denied only when it
    names a resident's own copy.

    The Bash half is the same coarse, decidable pair the trigger guard uses —
    does the command text name the file, and is every segment provably
    read-only — and carries the same caveat: it is a BACKSTOP, not a boundary.
    It catches the accidental form (a `sed -i` from a model following the old
    recipe), which is the entire threat model here: this guard exists to stop
    an honest agent reporting an inert edit as done, not to contain a hostile
    one. A path assembled from parts, or a script file, does not name the file
    at all and is not caught.
    """
    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            tool_name = input_data.get("tool_name", "")
            reported_cwd = input_data.get("cwd")
            cwd = str(reported_cwd or "/config")
            if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                ti = input_data.get("tool_input", {})
                raw = ti.get("file_path") or ti.get("notebook_path") or ""
                if not reported_cwd and not raw.startswith("/"):
                    return _deny(_RESPONSE_SHAPE_UNRESOLVABLE_DENY.format(
                        tool=tool_name, path=raw))
                norm = _normalize_path(
                    raw if raw.startswith("/") else cwd.rstrip("/") + "/" + raw)
                if _is_resident_response_shape_file(norm):
                    return _deny(_RESPONSE_SHAPE_WRITE_DENY.format(
                        tool=tool_name, path=raw))
                # A symlink whose lexical form is innocent but whose target is
                # the file: resolve and re-ask. An unresolvable path that could
                # be it fails closed.
                try:
                    real = os.path.realpath(norm)
                except OSError:
                    return _deny(_RESPONSE_SHAPE_RESOLVE_DENY.format(
                        tool=tool_name, path=raw))
                if _is_resident_response_shape_file(_normalize_path(real)):
                    return _deny(_RESPONSE_SHAPE_WRITE_DENY.format(
                        tool=tool_name, path=raw))
            elif tool_name == "Bash":
                command = input_data.get("tool_input", {}).get("command", "")
                if (_command_mentions_a_response_shape_file(command)
                        and not _provably_read_only(command)):
                    return _deny(_RESPONSE_SHAPE_WRITE_DENY.format(
                        tool="Bash", path=_RESPONSE_SHAPE_FILE_NAME))
            return {}
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — fail closed, never let it escape
            _logger.exception(
                "response_shape_write_guard internal error — denying")
            return _deny(_RESPONSE_SHAPE_INTERNAL_DENY)

    return _hook


def _response_shape_write_guard_factory(**kwargs: Any) -> HookCallback:
    if kwargs:
        raise ValueError(
            f"response_shape_write_guard: unknown parameter(s) {list(kwargs)}; "
            f"this policy takes none"
        )
    return make_response_shape_write_guard()


def response_shape_write_guard_matcher():
    """A ``HookMatcher`` wrapping :func:`make_response_shape_write_guard`,
    injected CODE-SIDE into every executor session AND every resident's, for
    the same reason :func:`trigger_file_write_guard_matcher` is: ``hooks_file:``
    is a config-editable pointer, so a yaml-only policy can be shed by an edit
    the configurator is otherwise entitled to make. Residents are included
    because the shipped assistant carries broad shell access over its own
    agent home, which is where this file lives."""
    from claude_agent_sdk import HookMatcher
    policy = HOOK_POLICIES["response_shape_write_guard"]
    return HookMatcher(
        matcher=policy["matcher"],
        hooks=[policy["factory"]()],
    )


# ---------------------------------------------------------------------------
# #631: agents/<role>/prompts/system.md
#
# The same defect as response_shape.yaml above, one file over, with a wider
# steering surface. `character.yaml`'s `prompt_file:` pointer is read once at
# load (`agent_loader._resolve_prose`), lands in `CharacterConfig.prompt`, and
# becomes the FIRST part of `_compose_prompt`'s output — which is
# `cfg.system_prompt`, and which a resident is served only on the no-bundle
# arm. Every resident is bundle-bound from its first boot, because all three
# role artifacts declare `persona.policy: required`. So the file is an input
# to nothing a resident ever receives, and an edit here is committed and
# reported live while the served prompt's digest is byte-identical.
#
# TWO DIRECTORY DEPTHS, not one — and this is the trap a verbatim copy of the
# guard above falls into. `response_shape.yaml` sits directly in the role
# directory, so its predicate is `p.parent.parent == _RESIDENT_ROOT`. This file
# is `/config/agents/<role>/prompts/system.md`: the role directory is
# `p.parent.parent` and the root is `p.parent.parent.parent`. A copied
# two-parent predicate matches nothing at all, silently.
#
# `prompts/<trigger>.md` is deliberately NOT claimed. A scheduled trigger's
# prose IS served — `agent_loader._build_triggers` resolves it at load and
# `trigger_registry` captures it in the job closure — so an edit there is not
# inert, only stale until `casa_reload_triggers`. Refusing it would be a false
# claim, and the configurator's reload table now says so instead.
#
# Specialists and executors are not claimed either, for the reasons the guard
# above gives in its own words: `agents/specialists/**` is managed state that
# `managed_component_guard` already denies, and `TIER_FILES["executor"]`
# FORBIDS this file outright (an executor's prose is its `prompt.md`).

_RESIDENT_PROMPT_FILE_NAME = "system.md"
_RESIDENT_PROMPTS_DIR_NAME = "prompts"
_RESIDENT_PROMPT_WRITE_DENY = (
    "resident_prompt_write_guard: {tool} blocked — {path!r} is not served to a "
    "persona-bound resident (the loader reads it only into a composed fallback "
    "prompt that the compiled bundle replaces), so editing it would be "
    "committed and reported live while changing nothing the model sees. A "
    "resident's base prompt is its COMPILED BUNDLE (the persona plus the role "
    "artifact's doctrine and response block), not the composed prompt this "
    "file feeds. What to do "
    "instead depends on what was asked for: how it SOUNDS is its persona "
    "(doctrine/recipes/persona/install.md then apply.md, then restart it); "
    "what it can DO is a grant (doctrine/recipes/resident/grant_ha_tools.md, "
    "recipes/plugin/, recipes/delegate/wire.md); and a standing BEHAVIOURAL "
    "rule has no configuration surface at all — a resident's instructions are "
    "its role doctrine, which ships inside the image and is never synced into "
    "/config. Say that plainly rather than writing this file. Reading it is "
    "fine — it is only the edit that would be a lie. If you meant a "
    "SPECIALIST's copy, that tree is managed state and "
    "managed_component_guard owns it."
)

_RESIDENT_PROMPT_RESOLVE_DENY = (
    "resident_prompt_write_guard: {tool} blocked — could not resolve {path!r} "
    "(realpath error); failing closed. A resident's instructions come from its "
    "persona pack and its role doctrine; see doctrine/recipes/prompt/resident.md."
)

_RESIDENT_PROMPT_UNRESOLVABLE_DENY = (
    "resident_prompt_write_guard: {tool} blocked — {path!r} is a relative path "
    "and this call reported no working directory, so where it lands cannot be "
    "established; failing closed. Give an absolute path."
)

_RESIDENT_PROMPT_INTERNAL_DENY = (
    "resident_prompt_write_guard: internal error — failing closed; the call "
    "was not executed."
)


def _is_resident_prompt_file(norm: str) -> bool:
    """True iff ``norm`` (normalized, absolute) names
    ``/config/agents/<role>/prompts/system.md`` for a SINGLE ``<role>``
    segment — the resident tier only.

    THREE parents, not two: ``p.parent`` is the prompts directory,
    ``p.parent.parent`` is the role directory, and only ``p.parent.parent
    .parent`` is ``_RESIDENT_ROOT``. The ``specialists``/``executors``
    exclusion cannot fire at this depth (their layouts put another segment
    between the root and the role directory, so the root test already fails);
    it is kept for parity with the two guards above, and stated here as
    redundant so a later reader does not take it for the thing that excludes
    them.
    """
    p = PurePosixPath(norm)
    if p.name != _RESIDENT_PROMPT_FILE_NAME:
        return False
    prompts = p.parent
    if prompts.name != _RESIDENT_PROMPTS_DIR_NAME:
        return False
    role = prompts.parent
    return (str(role.parent) == _RESIDENT_ROOT
            and role.name not in ("", "specialists", "executors"))


def make_resident_prompt_write_guard() -> HookCallback:
    """Deny an agent's write of a resident's ``prompts/system.md``.

    The file-tool half RESOLVES before it decides: the literal path is joined
    against the session's own working directory (residents run from their agent
    home, not ``/config``), lexically normalized, and — whatever that produced —
    re-asked through ``realpath``, so a symlink whose own name is innocent is
    caught by what it points at. That re-ask runs for a relative path exactly as
    for an absolute one; a red-case round measured a guard that consulted
    ``realpath`` only for absolute inputs and let ``alias.md`` through from
    inside the prompts directory.

    There is NO Bash half, and that is a ruling rather than an omission (D36,
    2026-08-31). Its two neighbours decide a shell command by matching text in
    it; a first cut of this guard did the same and was measured wrong in both
    directions — it refused reads the invariant promises to allow (`cp` source,
    `tar -cf` member, `<` redirection, `sed --file=`, `diff` operand) and
    missed writes (an interior `..`, a `cp` destination composed from a
    directory operand, `tar -xf -C`, a glob) — because what a shell command
    writes to, and whether it writes, are not decidable from its text. So the
    matcher routes the four file primitives only, this callback classifies no
    command text, and a `Bash` payload that reaches it (it cannot, in
    production) falls through to allow. The residual is exactly that: a
    shell-capable agent can still make the edit, which is inert for a
    bundle-bound resident. No shipped agent holds `Bash` (the configurator
    has none), and an execution boundary over that tree is a separate
    decision.
    """
    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            tool_name = input_data.get("tool_name", "")
            reported_cwd = input_data.get("cwd")
            cwd = str(reported_cwd or "/config")
            if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                ti = input_data.get("tool_input", {})
                raw = ti.get("file_path") or ti.get("notebook_path") or ""
                if not reported_cwd and not raw.startswith("/"):
                    return _deny(_RESIDENT_PROMPT_UNRESOLVABLE_DENY.format(
                        tool=tool_name, path=raw))
                norm = _normalize_path(
                    raw if raw.startswith("/") else cwd.rstrip("/") + "/" + raw)
                if _is_resident_prompt_file(norm):
                    return _deny(_RESIDENT_PROMPT_WRITE_DENY.format(
                        tool=tool_name, path=raw))
                # A symlink whose lexical form is innocent but whose target is
                # the file: resolve and re-ask. An unresolvable path that could
                # be it fails closed.
                try:
                    real = os.path.realpath(norm)
                except OSError:
                    return _deny(_RESIDENT_PROMPT_RESOLVE_DENY.format(
                        tool=tool_name, path=raw))
                if _is_resident_prompt_file(_normalize_path(real)):
                    return _deny(_RESIDENT_PROMPT_WRITE_DENY.format(
                        tool=tool_name, path=raw))
            return {}
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — fail closed, never let it escape
            _logger.exception(
                "resident_prompt_write_guard internal error — denying")
            return _deny(_RESIDENT_PROMPT_INTERNAL_DENY)

    return _hook


def _resident_prompt_write_guard_factory(**kwargs: Any) -> HookCallback:
    if kwargs:
        raise ValueError(
            f"resident_prompt_write_guard: unknown parameter(s) "
            f"{list(kwargs)}; this policy takes none"
        )
    return make_resident_prompt_write_guard()


def resident_prompt_write_guard_matcher():
    """A ``HookMatcher`` wrapping :func:`make_resident_prompt_write_guard`,
    injected CODE-SIDE into every executor session, every resident's, and every
    DELEGATED resident's, for the reason its two neighbours give: ``hooks_file:``
    is a config-editable pointer, so a yaml-only policy can be shed by an edit
    the configurator is otherwise entitled to make."""
    from claude_agent_sdk import HookMatcher
    policy = HOOK_POLICIES["resident_prompt_write_guard"]
    return HookMatcher(
        matcher=policy["matcher"],
        hooks=[policy["factory"]()],
    )


def _trigger_file_write_guard_factory(**kwargs: Any) -> HookCallback:
    if kwargs:
        raise UnknownPolicyError(
            f"trigger_file_write_guard: unknown parameter(s) {list(kwargs)}; "
            f"it takes none"
        )
    return make_trigger_file_write_guard()


def trigger_file_write_guard_matcher():
    """A ``HookMatcher`` wrapping :func:`make_trigger_file_write_guard`,
    injected CODE-SIDE into every executor session (``tools``) AND every
    resident's (``agent``) for the same reason
    :func:`managed_component_guard_matcher` is: ``hooks_file:`` is a
    config-editable pointer, so a yaml-only policy can be shed by an edit the
    configurator is otherwise entitled to make. Residents are included because
    the shipped assistant carries broad shell access and its own reminder tools
    write the very file this covers."""
    from claude_agent_sdk import HookMatcher
    policy = HOOK_POLICIES["trigger_file_write_guard"]
    return HookMatcher(
        matcher=policy["matcher"],
        hooks=[policy["factory"]()],
    )


# ---------------------------------------------------------------------------
# commit_size_guard - Plan 3 (asks user before batch commits > N files)
# ---------------------------------------------------------------------------


def _git_porcelain_count(repo_dir: str = "/config") -> int:
    """Return the number of lines in ``git status --porcelain``.

    Isolated for testability - tests monkeypatch this function.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir, capture_output=True, text=True, check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return sum(1 for line in out.stdout.splitlines() if line.strip())


def make_commit_size_guard_hook(*, max_files: int) -> HookCallback:
    """Deny Write/Edit when >= max_files are already uncommitted.

    Forces the agent to emit_completion + config_git_commit before
    piling on more changes.
    """
    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        if tool_name not in ("Write", "Edit"):
            return {}
        # M17: `git status --porcelain` is a blocking subprocess (up to 5s).
        # Offload it so a Write/Edit on any agent doesn't freeze the shared
        # event loop. _git_porcelain_count stays a sync module-level function
        # so existing patch("hooks._git_porcelain_count", ...) tests still work.
        count = await asyncio.to_thread(_git_porcelain_count)
        # #324: >= — the docstring's contract. At count == max_files another
        # write would produce max_files+1 uncommitted files.
        if count >= max_files:
            return _deny(
                f"commit_size_guard: {count} files already uncommitted "
                f"(max={max_files}). Call config_git_commit to stage your "
                f"current batch, then continue. If you must commit more "
                f"than {max_files} files atomically, ask the user first."
            )
        return {}

    return _hook


# ---------------------------------------------------------------------------
# HOOK_POLICIES — two-tier registry (Plan 4a.1).
#
# Each entry is {"matcher": regex, "factory": fn(**kwargs) -> HookCallback}.
# The "matcher" regex names the CC tool names the policy applies to;
# resolve_hooks (below) passes it to HookMatcher(matcher=...).
# The "factory" returns a raw async HookCallback — the same coroutine shape
# produced by make_*_hook_* helpers above.
# ---------------------------------------------------------------------------


def _block_dangerous_bash_factory(**kwargs: Any) -> HookCallback:
    if kwargs:
        raise UnknownPolicyError(
            f"block_dangerous_bash takes no parameters; got {list(kwargs)}"
        )
    return block_dangerous_commands


def _path_scope_factory(**kwargs: Any) -> HookCallback:
    writable = kwargs.pop("writable", None)
    readable = kwargs.pop("readable", None)
    if kwargs:
        raise UnknownPolicyError(
            f"path_scope: unknown parameter(s) {list(kwargs)}; "
            f"supported: writable, readable"
        )
    return make_path_scope_hook_v2(writable=writable, readable=readable)


def _casa_config_guard_factory(**kwargs: Any) -> HookCallback:
    forbid_write_paths = kwargs.pop("forbid_write_paths", None)
    forbid_delete_residents = kwargs.pop("forbid_delete_residents", True)
    if kwargs:
        raise UnknownPolicyError(
            f"casa_config_guard: unknown parameter(s) {list(kwargs)}; "
            f"supported: forbid_write_paths, forbid_delete_residents"
        )
    return make_casa_config_guard_hook(
        forbid_write_paths=forbid_write_paths,
        forbid_delete_residents=forbid_delete_residents,
    )


def _managed_component_guard_factory(**kwargs: Any) -> HookCallback:
    if kwargs:
        raise UnknownPolicyError(
            f"managed_component_guard takes no parameters; got {list(kwargs)}"
        )
    return make_managed_component_guard()


def _commit_size_guard_factory(**kwargs: Any) -> HookCallback:
    max_files = _require_int(
        "commit_size_guard", "max_files", kwargs.pop("max_files", 20))
    if kwargs:
        raise UnknownPolicyError(
            f"commit_size_guard: unknown parameter(s) {list(kwargs)}; "
            f"supported: max_files"
        )
    return make_commit_size_guard_hook(max_files=max_files)


HOOK_POLICIES: dict[str, dict[str, Any]] = {
    "block_dangerous_bash": {
        "matcher": "Bash",
        "factory": _block_dangerous_bash_factory,
    },
    "path_scope": {
        "matcher": "Read|Write|Edit",
        "factory": _path_scope_factory,
    },
    "casa_config_guard": {
        "matcher": "Write|Edit|Bash",
        "factory": _casa_config_guard_factory,
    },
    "managed_component_guard": {
        "matcher": "Write|Edit|Bash",
        "factory": _managed_component_guard_factory,
    },
    "commit_size_guard": {
        "matcher": "Write|Edit",
        "factory": _commit_size_guard_factory,
    },
    "trigger_file_write_guard": {
        # Every write-capable primitive, not just the two obvious ones — the
        # settings guard learned this the same way (Sol round-3 B3a): a matcher
        # that does not ROUTE MultiEdit/NotebookEdit lets those bypass entirely.
        "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
        "factory": _trigger_file_write_guard_factory,
    },
    "response_shape_write_guard": {
        # #610. Same routing rule as its neighbour above, for the same reason.
        "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
        "factory": _response_shape_write_guard_factory,
    },
    "resident_prompt_write_guard": {
        # #631. The four file primitives, for the same routing reason as its
        # two neighbours (a matcher that does not ROUTE MultiEdit/NotebookEdit
        # lets those bypass the hook entirely) — and, unlike them, NOT Bash:
        # the shell half was cut by ruling (D36); see the guard's docstring.
        "matcher": "Write|Edit|MultiEdit|NotebookEdit",
        "factory": _resident_prompt_write_guard_factory,
    },
}


def resolve_hooks(
    config: "HooksConfig",
    *,
    default_cwd: str,
    extra_readable: tuple[str, ...] = (),
    extra_read_check: Callable[[Path], bool] | None = None,
) -> dict[str, list[Any]]:
    """Turn a HooksConfig into ``{"PreToolUse": [HookMatcher, ...]}``.

    Builds SDK HookMatcher objects from the two-tier HOOK_POLICIES shape.
    The factory returns a raw HookCallback; HookMatcher wraps it with the
    policy's matcher regex.

    ``extra_readable`` (#1036) is appended to the ``readable`` list of EVERY
    ``path_scope`` entry built here — the default bundle and any entry from an
    explicit hooks file alike, so an agent that later ships a hooks file does
    not silently lose it. It never touches ``writable``. Only ``Agent`` passes
    it, and only for the role with an inbound-file folder
    (``agent_inbox.readable_prefixes``); the in-casa executor resynthesis
    passes nothing, so it can never resynthesize a wider scope than its
    snapshot.

    ``extra_read_check`` is given to EVERY ``path_scope`` entry built here,
    the same way: a Read its readable prefixes deny is allowed when the check
    accepts the path, asked at that Read's decision. Code passes it (a
    task's own files, ``agent_inbox.delegated_build_grants``); a hooks file
    cannot declare it.
    """
    from claude_agent_sdk import HookMatcher

    matchers: list[Any] = []
    entries = list(config.pre_tool_use or [])

    if not entries:
        entries = [
            {"policy": "block_dangerous_bash"},
            {"policy": "path_scope",
             "writable": [default_cwd] if default_cwd else [],
             "readable": [default_cwd] if default_cwd else []},
        ]

    for entry in entries:
        policy_name = entry.get("policy")
        policy = HOOK_POLICIES.get(policy_name)
        if policy is None:
            raise UnknownPolicyError(
                f"unknown hook policy {policy_name!r}; "
                f"available: {sorted(HOOK_POLICIES)}"
            )
        # REVISION 3 (Terra plan-review r3, #360): strip the transport-only
        # ``matcher``/``timeout`` keys before the factory call, exactly like
        # build_policy_callbacks_from_hooks_yaml already does above. Without
        # this, a snapshot carrying a stray ``matcher`` (e.g. because an
        # executor's yaml declared one) made every factory call raise
        # TypeError on an unexpected kwarg — turning a config-editable
        # cosmetic key into a START-time UnknownPolicyError for every
        # in-casa executor.
        params = {
            k: v for k, v in entry.items()
            if k not in ("policy", "matcher", "timeout")
        }
        if policy_name == "path_scope" and extra_readable:
            params["readable"] = [*(params.get("readable") or ()),
                                  *extra_readable]
        callback = policy["factory"](**params)
        if policy_name == "path_scope" and extra_read_check is not None:
            # the factory above validated the declared parameters
            callback = make_path_scope_hook_v2(
                writable=params.get("writable"), readable=params.get("readable"),
                extra_read_check=extra_read_check)
        matchers.append(HookMatcher(
            matcher=policy["matcher"],
            hooks=[callback],
        ))

    return {"PreToolUse": matchers}


def _compose_hook_callbacks(
    callbacks: list["HookCallback"],
) -> "HookCallback":
    """#313: one callback running every duplicate declaration of a policy.

    Runs each callback in declaration order and returns the FIRST non-allow
    result (allow is ``None``/``{}``; the shipped non-allow shapes are
    ``_deny`` and the relay's ask). Matches the SDK path, where every
    declaration is its own matcher and any deny blocks the call.
    """
    async def _composed(input_data, tool_use_id, context):
        for cb in callbacks:
            result = await cb(input_data, tool_use_id, context)
            if result:
                return result
        return {}
    return _composed


def build_policy_callbacks_from_hooks_yaml(
    hooks_yaml_data: dict,
) -> dict[str, tuple[str, "HookCallback"]]:
    """Build ``{policy_name: (matcher, callback)}`` from an executor hooks.yaml.

    The agent loader calls this at load for every executor as a
    constructibility check (#312): a declaration whose factory raises fails
    the executor at load rather than at enforcement time.

    Policies with no HOOK_POLICIES factory are skipped here. The per-entry
    ``matcher``/``timeout`` keys are not factory parameters, so they are
    stripped before the factory call.
    """
    grouped: dict[str, tuple[str, list["HookCallback"]]] = {}
    declared_matchers: dict[str, Any] = {}
    for entry in (hooks_yaml_data.get("pre_tool_use") or []):
        name = entry.get("policy")
        policy = HOOK_POLICIES.get(name)
        if policy is None:
            continue
        # Sol r2-1: duplicate declarations of one policy must agree on their
        # declared ``matcher`` (including all leaving it absent). The SDK
        # path deliberately ignores declared matchers (they are
        # config-editable; HOOK_POLICIES matchers are the trust anchor) — so
        # per-declaration matcher scoping of duplicates is unenforceable and
        # the composite would surprise (intersection under the canonical
        # matcher). Refuse the config loudly instead:
        # with #312's load-time constructibility check this fail-closes the
        # executor at load/commit, not silently at enforcement time.
        if name in declared_matchers:
            if declared_matchers[name] != entry.get("matcher"):
                raise UnknownPolicyError(
                    f"policy {name!r} is declared more than once with "
                    f"differing matchers ({declared_matchers[name]!r} vs "
                    f"{entry.get('matcher')!r}); duplicate declarations "
                    f"must agree on their matcher"
                )
        else:
            declared_matchers[name] = entry.get("matcher")
        params = {
            k: v for k, v in entry.items()
            if k not in ("policy", "matcher", "timeout")
        }
        _matcher, callbacks = grouped.setdefault(
            name, (policy["matcher"], []))
        callbacks.append(policy["factory"](**params))
    # #313: a policy declared MORE THAN ONCE must enforce every declaration.
    # The SDK path registers one HookMatcher per declaration and runs all of
    # them (any deny blocks); this dict is keyed by policy name, so a plain
    # ``out[name] = ...`` would be last-writer-wins.
    # Duplicates compose into ONE callback that runs each declaration in
    # order and returns the first non-allow result (deny/ask), restoring the
    # SDK's intersection semantics.
    out: dict[str, tuple[str, "HookCallback"]] = {
        name: (
            matcher,
            callbacks[0] if len(callbacks) == 1
            else _compose_hook_callbacks(callbacks),
        )
        for name, (matcher, callbacks) in grouped.items()
    }
    return out
