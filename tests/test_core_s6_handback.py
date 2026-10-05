"""#1268: what the core s6 scripts hand on once their interpreter,
`/opt/casa/scripts/core-bashio.sh`, has filtered PATH.

The real script bodies run here through a copy of the shipped wrapper (fixture
paths only, see `test_core_s6_interpreter.Chain.wrapper_copy`) and a fake bashio
launcher whose library stubs the `bashio::*` functions the bodies call. The
program each body execs last is replaced by a recorder at its ONE absolute
occurrence, so a body that execs something else, or by bare name, fails the
replacement count instead of passing.

- svc-casa, svc-casa-mcp and svc-ttyd's ttyd get the inherited PATH back, tools
  directory first, and no `_casa_cli_path`.
- svc-ttyd's disabled branch, `install` and the finish scripts stay filtered.
- the wrapper's filter is `_root_path_fragment()`'s, once, and agrees with
  `drivers.s6_rc._trusted_env()` on #1248's ten PATH spellings.
- setup-configs.sh publishes the same container PATH as before the filter.
- the wrapper execs: the service is the process s6-supervise started.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from drivers import s6_rc
from drivers.workspace import _root_path_fragment
from test_core_s6_interpreter import (
    DEFAULT_BLOCK, OVERLAY, WRAPPER, Chain, _populate, _stub, _write_exec)
from test_core_s6_interpreter import pytestmark  # noqa: F401 — same host needs
from test_engagement_root_path import _CASES, _case_path

_LIBRARY = """printf "library\\n" >> "{log}"
jq >/dev/null
curl >/dev/null
_k_rec() {{ printf "%s\\n" "$*" >> "{log}"; }}
bashio::log.info() {{ _k_rec log.info; }}
bashio::log.warning() {{ _k_rec log.warning "$*"; }}
bashio::log.error() {{ _k_rec log.error "$*"; }}
bashio::config() {{ _k_rec config "$1"; }}
bashio::config.true() {{ _k_rec config.true "$1"; [ "${{K_TERMINAL:-false}}" = true ]; }}
bashio::config.has_value() {{ _k_rec config.has_value "$1"; }}
bashio::addon.version() {{ echo 0.0.0; }}
bashio::addon.ingress_port() {{ echo 8099; }}
bashio::addon.stop() {{ _k_rec addon.stop; }}
bashio::net.wait_for() {{ _k_rec net.wait_for "$*"; }}
bashio::exit.nok() {{ _k_rec exit.nok; exit 1; }}
"""

FINAL_PROGRAMS = {
    "s6-rc.d/svc-casa/run": "/opt/casa/venv/bin/python3",
    "s6-rc.d/svc-casa-mcp/run": "/opt/casa/venv/bin/python3",
    "s6-rc.d/svc-ttyd/run": "/usr/bin/ttyd",
}


class Body(Chain):
    def __init__(self, tmp_path: Path):
        super().__init__(tmp_path)
        (self.launcher.parent / "bashio.sh").write_text(_LIBRARY.format(log=self.log))
        self.env_out = tmp_path / "final.env"
        self.argv_out = tmp_path / "final.argv"
        for d, tag in ((self.tools, "shadow"), (self.trusted, "trusted")):
            _stub(d, "install", tag, self.log)

    def recorder(self) -> Path:
        rec = self.tmp / "final-program"
        _write_exec(rec,
                    "#!/bin/sh\n"
                    f'printf "%s\\n" "$@" > "{self.argv_out}"\n'
                    f'exec /usr/bin/env > "{self.env_out}"\n')
        return rec

    def script(self, rel: str) -> Path:
        text = (OVERLAY / rel).read_text()
        final = FINAL_PROGRAMS.get(rel)
        if final is not None:
            assert text.count(final) == 1, (rel, final)
            text = text.replace(final, str(self.recorder()))
        copy = self.tmp / rel.replace("/", "_")
        copy.write_text(text)
        return copy

    def run(self, rel: str, *, env_extra: dict | None = None, argv=(),
            interpreter: Path | None = None, path: str | None = None):
        env = {"PATH": path or self.path, **(env_extra or {})}
        interp = interpreter or self.wrapper_copy()
        return subprocess.run(["/bin/sh", "-c", 'exec "$@"', "with-contenv-probe",
                               str(interp), str(self.script(rel)), *argv],
                              env=env, cwd=self.tmp, capture_output=True, text=True)

    def final_env(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in self.env_out.read_text().splitlines())

    def shadow(self) -> list[str]:
        return [line for line in self.lines() if line.startswith("shadow ")]


@pytest.fixture
def body(tmp_path, monkeypatch):
    b = Body(tmp_path)
    monkeypatch.setattr(s6_rc, "PLUGIN_TOOLS_BIN", str(b.tools))
    return b


def _last_code_line(rel: str) -> str:
    code = [ln.strip() for ln in (OVERLAY / rel).read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    return code[-1]


# --- K2: the two Python services get the inherited PATH back ------------------

@pytest.mark.parametrize("rel", ["s6-rc.d/svc-casa/run", "s6-rc.d/svc-casa-mcp/run"])
def test_python_service_gets_the_inherited_path_back(body, rel):
    assert _last_code_line(rel).startswith("exec /opt/casa/venv/bin/python3 /opt/casa/")

    r = body.run(rel)

    assert r.returncode == 0, r.stderr
    assert body.shadow() == [], body.lines()
    env = body.final_env()
    assert env["PATH"] == body.path
    assert [k for k in env if k.startswith("_casa")] == []


# --- K3: the terminal ----------------------------------------------------------

def test_terminal_gets_the_inherited_path_and_an_absolute_shell(body):
    r = body.run("s6-rc.d/svc-ttyd/run", env_extra={"K_TERMINAL": "true"})

    assert r.returncode == 0, r.stderr
    assert body.shadow() == [], body.lines()
    assert body.count("trusted", "install") == 1
    assert body.argv_out.read_text().splitlines()[-1] == "/bin/bash"
    env = body.final_env()
    assert env["PATH"] == body.path
    # The whole environment: what the wrapper was given, the script's two
    # exports, and what bash itself always exports. Nothing of the wrapper's.
    assert set(env) == {"PATH", "K_TERMINAL", "BASHIO_LOG_NO_COLORS", "NO_COLOR",
                        "PWD", "SHLVL"}, sorted(env)


def test_disabled_terminal_keeps_the_filtered_path(body):
    for d, tag in ((body.tools, "shadow"), (body.trusted, "trusted")):
        _write_exec(d / "sleep",
                    "#!/bin/sh\n"
                    f'printf "%s sleep %s PATH=%s\\n" "{tag}" "$*" "$PATH" >> "{body.log}"\n')

    r = body.run("s6-rc.d/svc-ttyd/run", env_extra={"K_TERMINAL": "false"})

    assert r.returncode == 0, r.stderr
    assert body.shadow() == [], body.lines()
    sleeps = [ln for ln in body.lines() if ln.startswith("trusted sleep")]
    assert sleeps == [f"trusted sleep infinity PATH={body.filtered}"]
    assert body.count("trusted", "install") == 0
    assert not body.argv_out.exists()


# --- finish scripts: same $1 handling and status as bashio run directly --------

def _without_stubs(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.split(" ", 1)[0] not in ("trusted", "shadow")]


@pytest.mark.parametrize("code", ["0", "1", "256"])
@pytest.mark.parametrize("rel", ["s6-rc.d/svc-casa/finish", "s6-rc.d/svc-casa-mcp/finish",
                                 "s6-rc.d/svc-nginx/finish", "s6-rc.d/svc-ttyd/finish"])
def test_finish_script_sees_the_same_arguments(body, rel, code):
    direct = body.run(rel, argv=[code], interpreter=body.launcher, path=body.filtered,
                      env_extra={"K_TERMINAL": "true"})
    direct_lines = _without_stubs(body.lines())
    body.log.unlink()

    wrapped = body.run(rel, argv=[code], env_extra={"K_TERMINAL": "true"})

    assert body.shadow() == [], body.lines()
    assert (wrapped.returncode, _without_stubs(body.lines())) == \
        (direct.returncode, direct_lines)
    assert wrapped.stderr == direct.stderr


# --- K5: one rule ----------------------------------------------------------------

def test_wrapper_carries_the_fragment_once_then_hands_off():
    text = WRAPPER.read_text()
    assert text.count(DEFAULT_BLOCK) == 1
    assert text.endswith(DEFAULT_BLOCK + "export _casa_cli_path\n"
                         'exec /bin/bash /usr/bin/bashio "$@"\n')


def _probe(chain: Chain) -> Path:
    probe = chain.tmp / "probe"
    probe.write_text(f'printf "body PATH=%s\\n" "$PATH" >> "{chain.log}"\n')
    return probe


@pytest.mark.parametrize("case", _CASES)
def test_wrapper_filter_and_trusted_path_agree(tmp_path, monkeypatch, case):
    chain = Chain(tmp_path)
    monkeypatch.setattr(s6_rc, "PLUGIN_TOOLS_BIN", str(chain.tools))
    ws = tmp_path / "ws"
    _populate(ws, "cwd", chain.log, chain.launcher)
    _populate(ws / "rel", "rel", chain.log, chain.launcher)
    path = _case_path(case, chain.tools, chain.trusted, tmp_path)
    expected = f"{chain.trusted}:/usr/bin"

    r = subprocess.run(["/bin/sh", "-c", 'exec "$@"', "with-contenv-probe",
                        str(chain.wrapper_copy()), str(_probe(chain))],
                       env={"PATH": path}, cwd=ws, capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    assert [ln for ln in chain.lines()
            if ln.split(" ", 1)[0] in ("shadow", "cwd", "rel")] == []
    assert [ln for ln in chain.lines() if ln.startswith("body ")] == [f"body PATH={expected}"]
    monkeypatch.setenv("PATH", path)
    monkeypatch.chdir(ws)
    assert s6_rc._trusted_env()["PATH"] == expected


def test_wrapper_filter_with_a_space_in_the_tools_path(tmp_path, monkeypatch):
    chain = Chain(tmp_path)
    chain.tools = tmp_path / "x y" / "tools"
    _populate(chain.tools, "shadow", chain.log, chain.launcher)
    chain.path = f"{chain.tools}:{chain.trusted}:/usr/bin:/bin"
    monkeypatch.setattr(s6_rc, "PLUGIN_TOOLS_BIN", str(chain.tools))
    assert "'" in _root_path_fragment()          # rendered shell-quoted

    r = subprocess.run(["/bin/sh", "-c", 'exec "$@"', "with-contenv-probe",
                        str(chain.wrapper_copy()), str(_probe(chain))],
                       env={"PATH": chain.path}, cwd=chain.tmp, capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    assert [ln for ln in chain.lines() if ln.startswith("shadow ")] == []
    assert [ln for ln in chain.lines() if ln.startswith("body ")] == \
        [f"body PATH={chain.filtered}"]


def test_wrapper_with_no_trusted_entry_runs_nothing(chain):
    ws = chain.tmp / "ws"
    _populate(ws, "cwd", chain.log, chain.launcher)
    _populate(ws / "rel", "rel", chain.log, chain.launcher)

    r = subprocess.run(["/bin/sh", "-c", 'exec "$@"', "with-contenv-probe",
                        str(chain.wrapper_copy()), str(_probe(chain))],
                       env={"PATH": f"{chain.tools}::rel:{chain.tools}/"}, cwd=ws,
                       capture_output=True, text=True)

    assert r.returncode == 111
    assert "refusing to start" in r.stderr
    assert chain.lines() == []


@pytest.fixture
def chain(tmp_path, monkeypatch):
    c = Chain(tmp_path)
    monkeypatch.setattr(s6_rc, "PLUGIN_TOOLS_BIN", str(c.tools))
    return c


# --- the wrapper execs: the service keeps the supervised process -------------

def test_body_runs_in_the_process_the_supervisor_started(chain):
    probe = chain.tmp / "probe"
    out = chain.tmp / "pid"
    probe.write_text(f'printf "%s" "$$" > "{out}"\n')

    p = subprocess.Popen(["/bin/sh", "-c", 'exec "$@"', "with-contenv-probe",
                          str(chain.wrapper_copy()), str(probe)],
                         env={"PATH": chain.path}, cwd=chain.tmp)
    assert p.wait(timeout=30) == 0
    assert int(out.read_text()) == p.pid


# --- setup-configs.sh publishes the container PATH it did before -------------

def _writer_block() -> str:
    text = (OVERLAY / "scripts" / "setup-configs.sh").read_text()
    start = text.index('CURRENT_PATH="')
    end = text.index("\nfi\n", start) + len("\nfi\n")
    return text[start:end]


@pytest.mark.parametrize("given", ["plain", "relative-and-empty", "tools-present"])
def test_setup_configs_publishes_the_path_it_was_given(body, given):
    published = body.tmp / "container_environment_PATH"
    block = _writer_block()
    assert block.count("/run/s6/container_environment/PATH") == 1
    script = body.tmp / "writer"
    script.write_text(f'TOOLS_BIN="{body.tools}"\n'
                      + block.replace("/run/s6/container_environment/PATH", str(published)))
    path = {"plain": f"{body.trusted}:/usr/bin:/bin",
            "relative-and-empty": f"{body.trusted}:/usr/bin:/bin:rel::",
            "tools-present": body.path}[given]

    r = subprocess.run(["/bin/sh", "-c", 'exec "$@"', "with-contenv-probe",
                        str(body.wrapper_copy()), str(script)],
                       env={"PATH": path}, cwd=body.tmp, capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    if given == "tools-present":
        assert not published.exists()
    else:
        assert published.read_text() == f"{body.tools}:{path}"
