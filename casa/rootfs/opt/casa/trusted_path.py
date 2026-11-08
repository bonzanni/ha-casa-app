"""The PATH rule that keeps a plugin-published program from running as Casa.

``setup-configs.sh`` puts the plugin tools directory first on every s6
service's PATH, and a plugin publishes executables there under any name. The
rule here removes that directory (and every empty or relative entry) before a
program is looked up: ``_trusted_env``/``trusted_path`` for programs Casa runs
from Python, ``_root_path_fragment`` for the same rule written as POSIX shell
(``scripts/core-bashio.sh`` carries it byte for byte).
"""

from __future__ import annotations

import os
import shlex

# #987: the directory plugin ``verify_bin`` executables are published into.
# ``setup-configs.sh`` prepends it to the PATH of every s6 service, casa-core
# included, ahead of the whole image PATH — so a bare ``s6-rc`` resolved there
# runs whatever a plugin published under that name. Every program
# ``drivers.s6_rc`` runs is resolved, and runs, under ``_trusted_env()`` instead.
PLUGIN_TOOLS_BIN = "/config/tools/bin"


def _trusted_env() -> dict[str, str]:
    """``os.environ`` with every PATH entry that names the plugin tools
    directory removed, the rest kept in order. Entries are compared by
    ``realpath``, so another spelling of the same directory (``//config/...``,
    a trailing slash, a symlink to it) is removed too. An empty or relative
    entry is removed as well (#1248): it means the current directory, which
    can be the tools directory, and the image PATH never has one. The
    engagement scripts' root phase filters its PATH by the same rule
    (``_root_path_fragment``). Passed as ``env=``:
    CPython's POSIX ``subprocess`` resolves a bare program name against the
    PATH of the env it is given, and the program inherits that env, so what
    s6-rc in turn runs by name is resolved the same way. No absolute program
    path is hard-coded: the image PATH stays the source of truth across base
    bumps (#925)."""
    env = dict(os.environ)
    path = env.get("PATH")
    if path is None:
        return env
    tools = os.path.realpath(PLUGIN_TOOLS_BIN)
    env["PATH"] = os.pathsep.join(
        entry for entry in path.split(os.pathsep)
        if os.path.isabs(entry) and os.path.realpath(entry) != tools)
    return env


def trusted_path() -> str | None:
    """The PATH ``_trusted_env`` hands a program (``None`` when there is no
    PATH at all). Both ``setpriv`` preflights resolve ``setpriv`` on it, as
    the engagement run script's root phase does (#1248)."""
    return _trusted_env().get("PATH")


def _root_path_fragment() -> str:
    """#1248: the shell lines both engagement service scripts run before any
    other program, as root. ``setup-configs.sh`` puts the plugin tools
    directory first on every s6 service's PATH, and a plugin publishes its
    ``verify_bin`` executables there under any name, so a bare ``cat``,
    ``mkdir``, ``s6-log`` or ``setpriv`` would run what a plugin published.
    The fragment saves the inherited PATH in ``_casa_cli_path`` (the run
    script hands it back to the dropped CLI) and rebuilds PATH from the
    ABSOLUTE entries that are not the tools directory (string-equal, or the
    same directory by ``-ef``) — the rule ``_trusted_env``
    applies, written in POSIX-shell builtins so bash and dash run it alike.
    If no entry is left it exits before running anything: an empty PATH
    would search the working directory. The variables are lowercase, so no
    ``extra_env`` key (upper-snake only) can name them. Its last line is the
    marker ``drivers.s6_rc.run_script_is_stale`` requires."""
    tools = shlex.quote(PLUGIN_TOOLS_BIN)
    return (
        "_casa_cli_path=$PATH\n"
        "_casa_root_path=\n"
        "_casa_rest=$PATH:\n"
        'while [ -n "$_casa_rest" ]; do\n'
        "  _casa_e=${_casa_rest%%:*}; _casa_rest=${_casa_rest#*:}\n"
        "  case $_casa_e in /*) ;; *) continue ;; esac\n"
        f'  if [ "$_casa_e" = {tools} ] || [ "$_casa_e" -ef {tools} ]; then continue; fi\n'
        "  _casa_root_path=${_casa_root_path:+$_casa_root_path:}$_casa_e\n"
        "done\n"
        '[ -n "$_casa_root_path" ] || { echo "casa: no PATH entry outside the'
        ' plugin tools dir; refusing to start" >&2; exit 111; }\n'
        "PATH=$_casa_root_path\n"
    )
