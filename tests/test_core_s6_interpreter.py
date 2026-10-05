"""#1268: the core s6 scripts run no program from the plugin tools directory
before their final hand-off.

`setup-configs.sh` prepends `/config/tools/bin` — where a plugin's
`verify_bin` executables are published, under any name — to the PATH of every
s6 service. The twelve `#!/command/with-contenv …` scripts under
`casa/rootfs/etc/s6-overlay` used to name `bashio` by bare name, so the
interpreter, the `bash` that bashio's own `#!/usr/bin/env bash` line looks up,
the launcher's `readlink`/`dirname`, the library's `jq`/`curl` and the bodies'
bare programs were all looked up with that directory first, as root.

They now name one Casa wrapper, `/opt/casa/scripts/core-bashio.sh`, which
filters PATH by `drivers.workspace._root_path_fragment()` (the rule #1248's
engagement scripts use) before it runs `/bin/bash /usr/bin/bashio`.

The chain test runs each REAL file's interpreter word the way with-contenv's
`exec $@` does — a real `/bin/sh` execvp — against executables in a stand-in
tools directory (injected through `s6_rc.PLUGIN_TOOLS_BIN`) and a stand-in image
PATH. Every stub records one line, so a wrong lookup shows up as a COUNT. The
real bashio exists only in the image; a fake launcher reproduces its mechanics
(`#!/usr/bin/env bash`, bare `readlink`/`dirname`, a sourced library that calls
`jq`/`curl`, then `source "$0"`).
"""
from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from drivers import s6_rc
from drivers.workspace import _root_path_fragment

REPO = Path(__file__).resolve().parents[1]
OVERLAY = REPO / "casa" / "rootfs" / "etc" / "s6-overlay"
WRAPPER = REPO / "casa" / "rootfs" / "opt" / "casa" / "scripts" / "core-bashio.sh"
WRAPPER_IMAGE_PATH = "/opt/casa/scripts/core-bashio.sh"
SHEBANG = "#!/command/with-contenv " + WRAPPER_IMAGE_PATH
BASHIO_PATHS = ("/usr/bin/bashio", "/usr/lib/bashio/bashio")

CORE_SCRIPTS = (
    "s6-rc.d/svc-casa/run",
    "s6-rc.d/svc-casa/finish",
    "s6-rc.d/svc-casa-mcp/run",
    "s6-rc.d/svc-casa-mcp/finish",
    "s6-rc.d/svc-nginx/run",
    "s6-rc.d/svc-nginx/finish",
    "s6-rc.d/svc-ttyd/run",
    "s6-rc.d/svc-ttyd/finish",
    "scripts/setup-configs.sh",
    "scripts/setup-nginx.sh",
    "scripts/setup-plugin-store.sh",
    "scripts/validate-config.sh",
)

# Rendered at import, before any test monkeypatches PLUGIN_TOOLS_BIN: the block
# the shipped wrapper must carry, byte for byte.
DEFAULT_BLOCK = _root_path_fragment()

STUBBED = ("bash", "bashio", "env", "readlink", "dirname", "jq", "curl", "nginx")
FORWARDED = ("bash", "env", "readlink", "dirname")
HOST = {name: shutil.which(name) for name in FORWARDED}

pytestmark = pytest.mark.skipif(
    not all(HOST.values()) or not os.path.exists("/bin/sh")
    or not os.path.exists("/bin/bash") or not os.path.exists("/usr/bin/env"),
    reason="needs /bin/sh, /bin/bash, /usr/bin/env and coreutils on the host")


def _first_line(path: Path) -> str:
    return path.read_bytes().split(b"\n", 1)[0].decode("utf-8", "replace")


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _stub(directory: Path, name: str, tag: str, log: Path, *,
          forward: str | None = None, rc: int = 0) -> None:
    """Record `<tag> <name>` with shell builtins, then forward or exit."""
    tail = f'exec "{forward}" "$@"\n' if forward else f"exit {rc}\n"
    _write_exec(directory / name,
                "#!/bin/sh\n"
                f'printf "%s %s\\n" "{tag}" "{name}" >> "{log}"\n' + tail)


def _populate(directory: Path, tag: str, log: Path, launcher: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in FORWARDED:
        _stub(directory, name, tag, log, forward=HOST[name])
    _stub(directory, "bashio", tag, log, forward=str(launcher))
    _stub(directory, "jq", tag, log)
    _stub(directory, "curl", tag, log)
    _stub(directory, "nginx", tag, log, rc=37)


def _fake_bashio(directory: Path, log: Path) -> Path:
    """The launcher and library layout of /usr/lib/bashio, reduced to the
    lookups the real ones make."""
    directory.mkdir(parents=True, exist_ok=True)
    launcher = directory / "bashio"
    _write_exec(launcher,
                "#!/usr/bin/env bash\n"
                "set -o errexit -o nounset -o pipefail\n"
                f'printf "launcher\\n" >> "{log}"\n'
                '__BASHIO_BIN=$(readlink -f "${BASH_SOURCE[0]}")\n'
                '__BASHIO_LIB_DIR=$(dirname "${__BASHIO_BIN}")\n'
                'source "${__BASHIO_LIB_DIR}/bashio.sh"\n'
                'BASH_ARGV0=${1:?script}\n'
                "shift\n"
                'source "$0" "$@"\n')
    (directory / "bashio.sh").write_text(
        f'printf "library\\n" >> "{log}"\n'
        "jq >/dev/null\n"
        "curl >/dev/null\n"
        f'bashio::log.info() {{ printf "log.info\\n" >> "{log}"; }}\n')
    return launcher


class Chain:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.log = tmp_path / "ran.log"
        self.tools = tmp_path / "tools"
        self.trusted = tmp_path / "trusted"
        self.launcher = _fake_bashio(tmp_path / "launcher", self.log)
        _populate(self.tools, "shadow", self.log, self.launcher)
        _populate(self.trusted, "trusted", self.log, self.launcher)
        self.path = f"{self.tools}:{self.trusted}:/usr/bin:/bin"
        self.filtered = f"{self.trusted}:/usr/bin:/bin"

    def wrapper_copy(self) -> Path:
        """The shipped wrapper with fixture paths only: the default block
        swapped for the one rendered for the stand-in tools dir, and bashio's
        path for the fake launcher. Nothing is added if either is missing."""
        text = WRAPPER.read_text()
        text = text.replace(DEFAULT_BLOCK, _root_path_fragment())
        for p in BASHIO_PATHS:
            text = text.replace(p, str(self.launcher))
        copy = self.tmp / "core-bashio.sh"
        _write_exec(copy, text)
        return copy

    def resolve(self, word: str) -> str:
        if word == WRAPPER_IMAGE_PATH:
            return str(self.wrapper_copy())
        if word in BASHIO_PATHS:
            return str(self.launcher)
        return word          # a bare name is left to the shell's execvp

    def lines(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def count(self, tag: str, name: str) -> int:
        return sum(1 for line in self.lines() if line.split(" ") == [tag, name])


@pytest.fixture
def chain(tmp_path, monkeypatch):
    c = Chain(tmp_path)
    monkeypatch.setattr(s6_rc, "PLUGIN_TOOLS_BIN", str(c.tools))
    return c


def _interpreter_word(rel: str) -> str:
    first = _first_line(OVERLAY / rel)
    assert first.startswith("#!/command/with-contenv "), first
    return first.split()[1]


# --- K1: the interpreter chain each real first line selects -------------------

@pytest.mark.parametrize("rel", CORE_SCRIPTS)
def test_core_interpreter_chain(chain, rel):
    probe = chain.tmp / "probe"
    probe.write_text(
        f'printf "body 0=%s n=%s 1=%s PATH=%s\\n" "$0" "$#" "${{1-}}" "$PATH" >> "{chain.log}"\n'
        "exec nginx\n")
    interp = chain.resolve(_interpreter_word(rel))

    r = subprocess.run(["/bin/sh", "-c", 'exec "$@"', "with-contenv-probe",
                        interp, str(probe), "256"],
                       env={"PATH": chain.path}, cwd=chain.tmp,
                       capture_output=True, text=True)

    lines = chain.lines()
    assert [line for line in lines if line.startswith("shadow ")] == [], lines
    for name in ("readlink", "dirname", "jq", "curl", "nginx"):
        assert chain.count("trusted", name) == 1, (name, lines)
    assert chain.count("trusted", "bash") == 0, lines
    assert lines.count("launcher") == 1 and lines.count("library") == 1, lines
    bodies = [line for line in lines if line.startswith("body ")]
    assert bodies == [f"body 0={probe} n=1 1=256 PATH={chain.filtered}"], lines
    assert r.returncode == 37, r.stderr


# --- K4: every core shebang names exactly the wrapper ------------------------

def test_core_shebang_inventory():
    assert len(CORE_SCRIPTS) == len(set(CORE_SCRIPTS)) == 12
    found = {str(p.relative_to(OVERLAY)) for p in OVERLAY.rglob("*")
             if p.is_file() and _first_line(p).startswith("#!/command/with-contenv")}
    assert found == set(CORE_SCRIPTS)


@pytest.mark.parametrize("rel", CORE_SCRIPTS)
def test_core_shebang(rel):
    assert _first_line(OVERLAY / rel) == SHEBANG
    assert _first_line(WRAPPER) == "#!/bin/sh"


# --- K3: the terminal's shell binary is absolute ------------------------------

def test_terminal_command_shell_is_absolute():
    text = (OVERLAY / "s6-rc.d" / "svc-ttyd" / "run").read_text()
    joined = text.replace("\\\n", " ")
    invocations = [shlex.split(line, comments=True) for line in joined.splitlines()
                   if line.strip().startswith(("exec ttyd", "exec /usr/bin/ttyd"))]
    assert len(invocations) == 1, invocations
    assert invocations[0][-1] == "/bin/bash", invocations
