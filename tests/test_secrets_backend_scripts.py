"""Where an install keeps its secrets is decided once, at container start.

`scripts/secrets-backend.sh` writes one word to a file under `/run`: `builtin`
when no 1Password service-account token is configured, `1password` when one
is. Everything else reads that file and nothing else:

- `/usr/local/bin/op`, the front every `op` call goes through, runs the
  built-in store's client on `builtin` and the 1Password program, moved to
  `/usr/local/bin/op-1password`, on anything else;
- `secrets-backend-lib.sh` tells the start scripts what the two 1Password
  options mean under that backend;
- `svc-vault` serves the store on `builtin` and idles on `1password`.

An install that uses 1Password must not notice any of this, so the tests of
that side compare what reaches the 1Password program with what was passed.

The real files run here: each is copied with fixture paths in place of the
image's (every replacement is counted, so a file that stops naming a path
fails the copy), and the programs they hand over to are recorders. The s6
scripts run through the shipped interpreter and a stand-in bashio with the
shell options the real one sets, as in `test_core_s6_handback`.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from test_core_s6_handback import Body
from test_core_s6_interpreter import OVERLAY, REPO, _stub, _write_exec
from test_core_s6_interpreter import pytestmark  # noqa: F401 — same host needs
from test_s6_memory_maintenance_graph import graph, reaches

ROOTFS = REPO / "casa" / "rootfs"
FRONT = ROOTFS / "usr" / "local" / "bin" / "op"
LIB = ROOTFS / "opt" / "casa" / "scripts" / "secrets-backend-lib.sh"
SETUP_CONFIGS = OVERLAY / "scripts" / "setup-configs.sh"

RUN_DIR = "/run/casa-vault"
BACKEND_FILE = RUN_DIR + "/backend"
LIB_IMAGE_PATH = "/opt/casa/scripts/secrets-backend-lib.sh"
VENV_PYTHON = "/opt/casa/venv/bin/python3"
REAL_OP = "/usr/local/bin/op-1password"
MARKER = "casa-builtin-store"

# The image's /bin/sh is dash. What a shell does to the environment it passes
# on is its own, so the tests that hold the environment to the byte speak for
# the image only under dash.
SH_IS_DASH = os.path.basename(os.path.realpath("/bin/sh")) == "dash"
dash_only = pytest.mark.skipif(
    not SH_IS_DASH, reason="the image's /bin/sh is dash; this host's is not")

# Contents of the backend file that are not the exact word, and so mean
# 1Password. None stands for no file at all.
NOT_BUILTIN = {
    "no-file": None,
    "empty": b"",
    "garbage": b"garbage\n",
    "trailing-space": b"builtin \n",
    "leading-space": b" builtin\n",
    "carriage-return": b"builtin\r\n",
    "upper-case": b"BUILTIN\n",
    "second-line": b"\nbuiltin\n",
    "the-other-word": b"1password\n",
}


def _replaced(text: str, replacements: dict[str, tuple[str, int]]) -> str:
    for old, (new, count) in replacements.items():
        assert text.count(old) == count, (old, text.count(old), count)
        text = text.replace(old, new)
    return text


def _recorder(directory: Path, name: str) -> tuple[Path, Path]:
    """A program that writes down what it was started with: its arguments,
    the environment exactly as the kernel handed it over, its standard input,
    what it can read from descriptor 9 and whether descriptor 3 is open."""
    out = directory / f"{name}.json"
    exe = directory / name
    _write_exec(exe, f"#!{sys.executable} -I\n" + f"""\
import json, os, sys
record = {{"argv": sys.argv[1:],
          "environ": open("/proc/self/environ", "rb").read().decode().split("\\0")[:-1],
          "stdin": sys.stdin.buffer.read().decode("latin-1")}}
try:
    record["fd9"] = os.read(9, 65536).decode()
except OSError:
    record["fd9"] = None
try:
    os.fstat(3)
    record["fd3"] = True
except OSError:
    record["fd3"] = False
with open({str(out)!r}, "w") as f:
    json.dump(record, f)
""")
    return exe, out


def _environ(record: dict) -> dict[str, str]:
    entries = [entry.split("=", 1) for entry in record["environ"]]
    assert len({name for name, _ in entries}) == len(entries), record["environ"]
    return dict(entries)


# Starts a program with exactly this argv, environment, working directory and,
# when asked, a file open for reading on descriptor 9. A fresh interpreter
# does it so that nothing of the test process is inherited.
_LAUNCH = """\
import json, os, sys
spec = json.load(open(sys.argv[1]))
if spec["fd9"] is not None:
    fd = os.open(spec["fd9"], os.O_RDONLY)
    os.dup2(fd, 9)
    os.close(fd)
os.chdir(spec["cwd"])
os.execve(spec["argv"][0], spec["argv"], spec["env"])
"""


class Front:
    """The shipped front, with the backend file, the client's interpreter and
    the 1Password program at fixture paths."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.backend = tmp_path / "backend"
        self.cwd = tmp_path / "cwd"
        self.cwd.mkdir()
        self.client, self.client_out = _recorder(tmp_path, "client-python")
        self.real, self.real_out = _recorder(tmp_path, "op-real")
        self.exe = tmp_path / "op"
        _write_exec(self.exe, _replaced(FRONT.read_text(), {
            BACKEND_FILE: (str(self.backend), 2),
            VENV_PYTHON: (str(self.client), 1),
            REAL_OP: (str(self.real), 1),
        }))

    def set_backend(self, content: bytes | None) -> None:
        if content is not None:
            self.backend.write_bytes(content)

    def run(self, args, env: dict[str, str], *, stdin: bytes = b"",
            fd9: Path | None = None) -> subprocess.CompletedProcess:
        spec = self.tmp / "launch.json"
        spec.write_text(json.dumps({
            "argv": [str(self.exe), *args], "env": env, "cwd": str(self.cwd),
            "fd9": str(fd9) if fd9 else None}))
        return subprocess.run([sys.executable, "-I", "-c", _LAUNCH, str(spec)],
                              input=stdin, capture_output=True, timeout=30)

    def ran(self) -> list[str]:
        return [name for name, out in (("client", self.client_out), ("real", self.real_out))
                if out.exists()]


@pytest.fixture
def front(tmp_path):
    return Front(tmp_path)


AWKWARD_ARGS = ["item", "get", "two words", "", "-leading-dash", "--", "a'b\"c", "$HOME", "*"]


# --- the front ------------------------------------------------------------------

def test_front_on_builtin_runs_the_client_with_the_same_arguments(front):
    front.set_backend(b"builtin\n")

    r = front.run(AWKWARD_ARGS, {"PATH": "/usr/bin:/bin"}, stdin=b"a template\n")

    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")
    assert front.ran() == ["client"]
    record = json.loads(front.client_out.read_text())
    # Isolated mode, the client by its absolute path, then the caller's words.
    assert record["argv"] == ["-I", "/opt/casa/vault_op_client.py", *AWKWARD_ARGS]
    assert record["stdin"] == "a template\n"


@pytest.mark.parametrize("content,ran", [(None, "real"), (b"1password\n", "real"),
                                         (b"builtin\n", "client")])
def test_front_takes_no_backend_from_the_environment(front, content, ran):
    """Only the file decides, whatever the caller's environment says."""
    front.set_backend(content)
    other = "1password" if ran == "client" else "builtin"

    token = MARKER if other == "builtin" else "ops_token"

    r = front.run(["--version"], {"PATH": "/usr/bin:/bin", "backend": other,
                                  "BACKEND": other, "CASA_SECRETS_BACKEND": other,
                                  "OP_SERVICE_ACCOUNT_TOKEN": token})

    assert (r.returncode, r.stderr) == (0, b"")
    assert front.ran() == [ran]


@dash_only
def test_front_on_1password_execs_the_real_binary_untouched(front):
    front.set_backend(b"1password\n")
    inherited = front.tmp / "inherited"
    inherited.write_text("read from descriptor nine\n")
    env = {"PATH": "/usr/bin:/bin", "PWD": str(front.cwd), "HOME": "/nonexistent",
           "OP_SERVICE_ACCOUNT_TOKEN": "ops_a token, with spaces", "EMPTY": "",
           "TWO_LINES": "one\ntwo", "ENV": "/nonexistent/rc", "lower_case": "$HOME *"}

    r = front.run(AWKWARD_ARGS, env, stdin=b"standard input\n\x00\xff", fd9=inherited)

    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")
    assert front.ran() == ["real"]
    record = json.loads(front.real_out.read_text())
    assert record["argv"] == AWKWARD_ARGS
    assert _environ(record) == env
    assert record["stdin"].encode("latin-1") == b"standard input\n\x00\xff"
    assert record["fd9"] == "read from descriptor nine\n"
    assert record["fd3"] is False


@dash_only
@pytest.mark.parametrize("given", ["no-pwd", "stale-pwd", "odd-names", "named-backend"])
def test_front_on_1password_changes_only_what_a_shell_script_owns(front, given):
    """The front is a shell script, so the environment that reaches the
    1Password program is the one the shell (dash) passes on. It differs from
    the caller's in these ways and no other:

    - `PWD` is the directory the call runs in, also when the caller's `PWD`
      was missing or named another one;
    - an entry whose name is not a shell identifier is not carried over;
    - a variable the caller happens to call `backend`, the front's one
      variable, leaves with what the front read from the file.

    (dash also gives its own `IFS`, `OPTIND` and `PPID` to a caller that
    exported one of those; not held here, `PPID` being a process id.)"""
    front.set_backend(b"1password\n")
    base = {"PATH": "/usr/bin:/bin", "OP_SERVICE_ACCOUNT_TOKEN": "ops_token"}
    here = {**base, "PWD": str(front.cwd)}
    env, arrives = {
        "no-pwd": (base, here),
        "stale-pwd": ({**base, "PWD": "/nonexistent/elsewhere"}, here),
        "odd-names": ({**here, "dotted.name": "1", "dashed-name": "2"}, here),
        "named-backend": ({**here, "backend": "the caller's"},
                          {**here, "backend": "1password"}),
    }[given]

    r = front.run(["read", "op://v/i/f"], env)

    assert (r.returncode, r.stderr) == (0, b"")
    record = json.loads(front.real_out.read_text())
    assert record["argv"] == ["read", "op://v/i/f"]
    assert _environ(record) == arrives


def test_front_with_no_file_execs_the_real_binary(front):
    r = front.run(["--version"], {"PATH": "/usr/bin:/bin"})

    assert (r.returncode, r.stderr) == (0, b"")
    assert not front.backend.exists()
    assert front.ran() == ["real"]
    assert json.loads(front.real_out.read_text())["argv"] == ["--version"]


@pytest.mark.parametrize("case", [c for c in NOT_BUILTIN if c != "no-file"] + ["unreadable"])
def test_front_with_anything_but_the_exact_word_execs_the_real_binary(front, case):
    if case == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root reads a file of any mode")
        front.set_backend(b"builtin\n")
        front.backend.chmod(0)
    else:
        front.set_backend(NOT_BUILTIN[case])

    r = front.run(["read", "op://v/i/f"], {"PATH": "/usr/bin:/bin"})

    assert (r.returncode, r.stderr) == (0, b"")
    assert front.ran() == ["real"]
    assert json.loads(front.real_out.read_text())["argv"] == ["read", "op://v/i/f"]


@pytest.mark.parametrize("content,ran", [(b"builtin\n", "client"), (b"builtin", "client"),
                                         (b"1password\n", "real"), (None, "real")])
def test_front_ignores_programs_named_cat_read_python3_sh_earlier_on_path(
        front, content, ran):
    shadow = front.tmp / "shadow"
    shadow.mkdir()
    log = front.tmp / "shadow.log"
    for name in ("cat", "read", "python3", "sh", "op", "op-1password", "[", "test",
                 "exec", "printf", "echo"):
        _stub(shadow, name, "shadow", log, rc=99)
    front.set_backend(content)

    r = front.run(["read", "op://v/i/f"], {"PATH": f"{shadow}:/usr/bin:/bin"})

    assert (r.returncode, r.stderr) == (0, b"")
    assert not log.exists(), log.read_text()
    assert front.ran() == [ran]


_SEPARATORS = {";", "&&", "||", "|", "&"}
_KEYWORDS = {"if", "then", "elif", "else", "fi", "{", "}", "!"}
# What dash and bash both run without looking anything up.
_BUILTINS = {":", "[", "exec", "read", "printf"}


def _command_words(text: str) -> list[str]:
    """The word in command position of every simple command in a shell file,
    those inside `$(...)` too, and the program each `exec` names."""
    lines = []
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        lines.append(raw)
        lines += re.findall(r"\$\(([^()]*)\)", raw)
    words = []
    for line in lines:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        command = True
        for token in lexer:
            if token in _SEPARATORS:
                command = True
            elif not command or token in _KEYWORDS:
                pass
            elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
                pass                                  # an assignment before a command
            elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\(\)", token):
                pass                                  # a function definition
            else:
                words.append(token)
                command = token == "exec"
    return words


def test_command_words_sees_a_bare_program():
    """The static check below is only worth something if it would see one."""
    assert _command_words('x=1 cat f; if [ -r f ]; then\n  y="$(tr a b)" || :\nfi\n'
                          'exec env "$@"\n# sed\nf() {\n printf x | grep x\n}\n') == \
        ["cat", "[", ":", "tr", "exec", "env", "printf", "grep"]


def test_front_has_no_path_lookups():
    assert FRONT.read_text().startswith("#!/bin/sh\n")
    words = _command_words(FRONT.read_text())
    assert sorted(set(words)) == sorted({"[", "read", ":", "exec", VENV_PYTHON, REAL_OP})
    assert [w for w in words if w not in _BUILTINS and not w.startswith("/")] == []


# --- the helper the start scripts source ---------------------------------------

LIB_FUNCTIONS = {"casa_secrets_backend", "casa_op_token", "casa_op_vault"}


def test_helper_has_no_path_lookups():
    text = LIB.read_text()
    assert set(re.findall(r"^(\w+)\(\) \{$", text, re.M)) == LIB_FUNCTIONS
    assert [w for w in _command_words(text) if w not in _BUILTINS | LIB_FUNCTIONS] == []


def _call_helper(tmp_path: Path, content: bytes | None, function: str,
                 *args: str, env_extra: dict[str, str] | None = None,
                 ) -> subprocess.CompletedProcess:
    backend = tmp_path / "backend"
    if content is not None:
        backend.write_bytes(content)
    lib = tmp_path / "lib.sh"
    lib.write_text(_replaced(LIB.read_text(), {BACKEND_FILE: (str(backend), 2)}))
    # Nothing on PATH but programs that fail: the helper is builtins only.
    shadow = tmp_path / "shadow"
    shadow.mkdir(exist_ok=True)
    for name in ("cat", "read", "printf", "echo", "[", "test", "tr", "sed"):
        _stub(shadow, name, "shadow", tmp_path / "shadow.log", rc=99)
    return subprocess.run(
        ["/bin/sh", "-c", 'set -eu; . "$1"; shift; "$@"', "helper", str(lib), function, *args],
        env={"PATH": str(shadow), **(env_extra or {})},
        capture_output=True, text=True, timeout=30)


_VALUES = ["", "null", "ops_a-real.token", "two words", "-n", "a\\tb", "*", "nullable"]
# What the three options the start scripts read can hold.
_TOKENS = ["ops_a-real.token", "", "null"]
_VAULTS = ["Family", "", "null"]
_OAUTHS = ["a-raw-token", "op://Family/Claude/credential", "", "null"]


@pytest.mark.parametrize("case", ["builtin", "builtin-no-newline", *NOT_BUILTIN])
def test_helper_backend(tmp_path, case):
    content = {"builtin": b"builtin\n", "builtin-no-newline": b"builtin"}.get(
        case, NOT_BUILTIN.get(case))

    r = _call_helper(tmp_path, content, "casa_secrets_backend")

    expected = "builtin" if case.startswith("builtin") else "1password"
    assert (r.returncode, r.stdout, r.stderr) == (0, expected + "\n", "")


def test_helper_takes_no_backend_from_the_environment(tmp_path):
    env = {"_casa_backend": "builtin", "backend": "builtin", "CASA_SECRETS_BACKEND": "builtin"}

    r = _call_helper(tmp_path, None, "casa_secrets_backend", env_extra=env)

    assert (r.returncode, r.stdout, r.stderr) == (0, "1password\n", "")


@pytest.mark.parametrize("value", _VALUES)
@pytest.mark.parametrize("case", ["builtin", "the-other-word", "no-file", "trailing-space"])
def test_helper_token(tmp_path, case, value):
    content = b"builtin\n" if case == "builtin" else NOT_BUILTIN[case]

    r = _call_helper(tmp_path, content, "casa_op_token", value)

    if case == "builtin":
        expected = MARKER
    else:
        expected = "" if value == "null" else value
    assert (r.returncode, r.stdout.rstrip("\n"), r.stderr) == (0, expected, "")
    assert not (tmp_path / "shadow.log").exists()


@pytest.mark.parametrize("value", _VALUES)
@pytest.mark.parametrize("case", ["builtin", "the-other-word", "no-file", "trailing-space"])
def test_helper_vault(tmp_path, case, value):
    content = b"builtin\n" if case == "builtin" else NOT_BUILTIN[case]

    r = _call_helper(tmp_path, content, "casa_op_vault", value)

    expected = "" if value == "null" else value
    if case == "builtin" and not expected:
        expected = "Casa"
    assert (r.returncode, r.stdout.rstrip("\n"), r.stderr) == (0, expected, "")
    assert not (tmp_path / "shadow.log").exists()


@pytest.mark.parametrize("function", ["casa_op_token", "casa_op_vault"])
def test_helper_without_an_argument_is_the_empty_value(tmp_path, function):
    r = _call_helper(tmp_path, b"1password\n", function)

    assert (r.returncode, r.stdout.rstrip("\n"), r.stderr) == (0, "", "")


# --- the s6 scripts, through the shipped interpreter and a stand-in bashio -------

# The shell options are the ones the real bashio sets before it sources a
# script (the launcher of the fixture sets errexit, nounset and pipefail).
_LIBRARY = """set -o errtrace
shopt -s inherit_errexit
_k_rec() {{ printf "%s\\n" "$*" >> "{log}"; }}
bashio::log.info() {{ _k_rec log.info "$*"; }}
bashio::log.warning() {{ _k_rec log.warning "$*"; }}
bashio::log.error() {{ _k_rec log.error "$*"; }}
bashio::config() {{
    _k_rec config "$1"
    case "$1" in
        onepassword_service_account_token) printf "%s" "${{K_OP_TOKEN-}}" ;;
        onepassword_default_vault) printf "%s" "${{K_OP_VAULT-}}" ;;
        claude_oauth_token) printf "%s" "${{K_CC_OAUTH-}}" ;;
    esac
}}
bashio::app.version() {{ echo 0.0.0; }}
"""


class Start(Body):
    """`Body`, with a bashio whose `config` answers the options these scripts
    read (from `K_*` variables) and a stand-in for `/run/casa-vault`."""

    def __init__(self, tmp_path: Path):
        super().__init__(tmp_path)
        (self.launcher.parent / "bashio.sh").write_text(_LIBRARY.format(log=self.log))
        self.run_dir = tmp_path / "run-casa-vault"
        self.backend = self.run_dir / "backend"
        self.lib = tmp_path / "secrets-backend-lib.sh"
        self.lib.write_text(_replaced(LIB.read_text(), {BACKEND_FILE: (str(self.backend), 2)}))

    def set_backend(self, content: bytes | None) -> None:
        if content is not None:
            self.run_dir.mkdir(exist_ok=True)
            self.backend.write_bytes(content)

    def copy(self, name: str, text: str, replacements: dict[str, tuple[str, int]]) -> Path:
        script = self.tmp / name
        script.write_text(_replaced(text, replacements))
        return script

    def start(self, script: Path, *, options: dict[str, str] | None = None,
              env_extra: dict[str, str] | None = None, argv=(),
              ready: Path | None = None) -> subprocess.CompletedProcess:
        """Run a script the way s6 does: through its interpreter, and with
        `ready` open for writing on descriptor 3 when it is given."""
        env = {"PATH": self.path, **(options or {}), **(env_extra or {})}
        shell = 'exec "$@"'
        words = [str(self.wrapper_copy()), str(script), *argv]
        if ready is not None:
            shell = 'exec 3>"$1"; shift; ' + shell
            words.insert(0, str(ready))
        return subprocess.run(["/bin/sh", "-c", shell, "with-contenv-probe", *words],
                              env=env, cwd=self.tmp, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60)

    def logged(self, kind: str) -> list[str]:
        return [ln for ln in self.lines() if ln.split(" ", 1)[0] == kind]


@pytest.fixture
def start(tmp_path, monkeypatch):
    import trusted_path
    s = Start(tmp_path)
    monkeypatch.setattr(trusted_path, "PLUGIN_TOOLS_BIN", str(s.tools))
    return s


# --- scripts/secrets-backend.sh --------------------------------------------------

@pytest.mark.parametrize("token,word", [("", "builtin"), ("null", "builtin"),
                                        ("ops_a-real.token", "1password"),
                                        ("anything else", "1password")])
@pytest.mark.parametrize("before", ["nothing", "other-word-and-mode"])
def test_backend_script_writes_builtin_for_empty_and_null_and_1password_otherwise(
        start, token, word, before):
    script = start.copy("secrets-backend.sh",
                        (OVERLAY / "scripts" / "secrets-backend.sh").read_text(),
                        {RUN_DIR: (str(start.run_dir), 5)})
    if before == "other-word-and-mode":
        start.set_backend(b"1password\n" if word == "builtin" else b"builtin\n")
        start.run_dir.chmod(0o700)

    r = start.start(script, options={"K_OP_TOKEN": token})

    assert (r.returncode, r.stderr) == (0, ""), r.stderr
    assert start.backend.read_bytes() == word.encode() + b"\n"
    assert stat.S_IMODE(start.run_dir.stat().st_mode) == 0o755
    # Written under another name and renamed: nothing else is left behind.
    assert sorted(p.name for p in start.run_dir.iterdir()) == ["backend"]
    assert start.logged("config") == ["config onepassword_service_account_token"]
    # One line, naming the backend, and never the token.
    assert len(start.logged("log.info")) == 1 and word in start.logged("log.info")[0]
    assert start.shadow() == [], start.lines()
    if token and token != "null":
        assert token not in start.log.read_text() + r.stdout + r.stderr


def test_backend_script_unit_runs_the_script():
    unit = OVERLAY / "s6-rc.d" / "init-secrets-backend"
    assert (unit / "type").read_text() == "oneshot\n"
    assert (unit / "up").read_text() == "/etc/s6-overlay/scripts/secrets-backend.sh\n"


# --- svc-vault/run ------------------------------------------------------------------

def _svc_vault_run(start: Start) -> tuple[Path, Path]:
    service, out = _recorder(start.tmp, "service-python")
    script = start.copy("svc-vault-run", (OVERLAY / "s6-rc.d/svc-vault/run").read_text(), {
        LIB_IMAGE_PATH: (str(start.lib), 1),
        VENV_PYTHON: (str(service), 1),
    })
    return script, out


@pytest.mark.parametrize("case", ["the-other-word", "no-file", "trailing-space"])
def test_svc_vault_run_idles_on_1password_and_signals_ready(start, case):
    script, service_out = _svc_vault_run(start)
    start.set_backend(NOT_BUILTIN[case])
    ready = start.tmp / "ready"
    for d, tag in ((start.tools, "shadow"), (start.trusted, "trusted")):
        _write_exec(d / "sleep",
                    "#!/bin/sh\n"
                    'if (: >&3) 2>/dev/null; then fd3=open; else fd3=closed; fi\n'
                    f'printf "%s sleep %s PATH=%s fd3=%s\\n" "{tag}" "$*" "$PATH" "$fd3"'
                    f' >> "{start.log}"\n')

    r = start.start(script, ready=ready,
                    options={"K_OP_TOKEN": "ops_token", "K_OP_VAULT": "Family"})

    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    # Ready at once, then nothing but a sleep: no store process, no option read,
    # no log line.
    assert ready.read_bytes() == b"\n"
    assert [ln for ln in start.lines() if " sleep " in ln] == \
        [f"trusted sleep infinity PATH={start.filtered} fd3=closed"]
    assert not service_out.exists()
    assert start.logged("config") == [] and start.logged("log.info") == []
    assert start.shadow() == [], start.lines()


@pytest.mark.parametrize("option,name", [("", "Casa"), ("null", "Casa"),
                                         ("Family Vault", "Family Vault")])
def test_svc_vault_run_starts_the_service_on_builtin(start, option, name):
    script, service_out = _svc_vault_run(start)
    start.set_backend(b"builtin\n")
    ready = start.tmp / "ready"

    r = start.start(script, ready=ready, options={"K_OP_VAULT": option},
                    env_extra={"CASA_VAULT_KEY": "a key from outside"})

    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    record = json.loads(service_out.read_text())
    assert record["argv"] == ["/opt/casa/vault_service.py", "--dir", "/data/vault",
                              "--socket", "/run/casa-vault/store.sock",
                              "--vault-name", name, "--ready-fd", "3"]
    # The service says when it is ready, on the descriptor it was left.
    assert record["fd3"] is True and ready.read_bytes() == b""
    env = _environ(record)
    assert env["PATH"] == start.filtered
    assert env["CASA_VAULT_KEY"] == "a key from outside"
    assert start.shadow() == [], start.lines()


# --- svc-casa/run: what Casa itself is started with ---------------------------------

_ODD = "My Vault -n a\\tb *"


@pytest.mark.parametrize("content,token,vault,exported", [
    (b"1password\n", "ops_a-real.token", "Family", ("ops_a-real.token", "Family")),
    (None, "ops_a-real.token", _ODD, ("ops_a-real.token", _ODD)),
    (b"1password\n", "ops_a-real.token", "", ("ops_a-real.token", "")),
    (b"1password\n", "null", "null", ("", "")),
    (None, "", "Family", ("", "Family")),
    (b"builtin\n", "", "", (MARKER, "Casa")),
    (b"builtin\n", "null", "null", (MARKER, "Casa")),
    (b"builtin\n", "", "Family", (MARKER, "Family")),
])
def test_svc_casa_run_exports_follow_the_backend(start, content, token, vault, exported):
    casa, casa_out = _recorder(start.tmp, "casa-python")
    script = start.copy("svc-casa-run", (OVERLAY / "s6-rc.d/svc-casa/run").read_text(), {
        LIB_IMAGE_PATH: (str(start.lib), 1),
        VENV_PYTHON: (str(casa), 1),
    })
    start.set_backend(content)

    r = start.start(script, options={"K_OP_TOKEN": token, "K_OP_VAULT": vault})

    assert (r.returncode, r.stderr) == (0, "")
    env = _environ(json.loads(casa_out.read_text()))
    assert (env["OP_SERVICE_ACCOUNT_TOKEN"], env["ONEPASSWORD_DEFAULT_VAULT"]) == exported
    assert [name for name in env if "backend" in name.lower()] == []


@pytest.mark.parametrize("case", ["the-other-word", "no-file"])
def test_svc_casa_run_on_1password_starts_casa_as_it_did_without_the_helper(start, case):
    """The whole environment Casa is started with, and its arguments, against
    the same script with the two plain exports it had before the helper."""
    text = (OVERLAY / "s6-rc.d/svc-casa/run").read_text()
    casa, casa_out = _recorder(start.tmp, "casa-python")
    after = start.copy("svc-casa-run-after", text, {
        LIB_IMAGE_PATH: (str(start.lib), 1), VENV_PYTHON: (str(casa), 1)})
    before = start.copy("svc-casa-run-before", text, {
        f". {LIB_IMAGE_PATH}\n": ("", 1), VENV_PYTHON: (str(casa), 1),
        '"$(casa_op_token "$_op_token")"': ('"$_op_token"', 1),
        '"$(casa_op_vault "$_op_vault")"': ('"$_op_vault"', 1)})
    assert "casa_op_" not in "\n".join(
        ln for ln in before.read_text().splitlines() if not ln.startswith("#"))
    start.set_backend(NOT_BUILTIN[case])

    for token in _TOKENS:
        for vault in _VAULTS:
            options = {"K_OP_TOKEN": token, "K_OP_VAULT": vault}
            seen = []
            for script in (after, before):
                r = start.start(script, options=options)
                assert (r.returncode, r.stderr) == (0, ""), options
                record = json.loads(casa_out.read_text())
                casa_out.unlink()
                seen.append((record["argv"], _environ(record)))
            assert seen[0] == seen[1], options
            assert seen[0][1]["OP_SERVICE_ACCOUNT_TOKEN"] == ("" if token == "null" else token)


def test_svc_vault_declares_readiness_on_fd_3():
    unit = OVERLAY / "s6-rc.d" / "svc-vault"
    assert (unit / "type").read_text() == "longrun\n"
    assert (unit / "notification-fd").read_text() == "3\n"
    run = (unit / "run").read_text().replace("\\\n", " ")
    assert re.search(r"--ready-fd 3\s*$", run, re.M), run
    # On 1Password the script itself is what says ready.
    assert "echo >&3\n" in run


# --- the three blocks of setup-configs.sh that read the 1Password options ---------

GITHUB_FILE = "/run/s6/container_environment/GITHUB_TOKEN"
CLAUDE_FILE = "/run/s6/container_environment/CLAUDE_CODE_OAUTH_TOKEN"
_HELPER_CALL = re.compile(r'^\s*\w+="\$\(casa_op_(token|vault) "\$\w+"\)"\n', re.M)


def _blocks() -> dict[str, str]:
    text = SETUP_CONFIGS.read_text()

    def between(begin: str, end: str) -> str:
        assert text.count(begin) == 1 and text.count(end) == 1, (begin, end)
        return text[text.index(begin):text.index(end) + len(end)]

    return {
        "discovery": between('_op_tok_discovery="$(bashio::config', "unset _op_tok_discovery\n"),
        "github": between("# === github-token: begin", "# === github-token: end"),
        "claude": between("# === claude-oauth-token: begin", "# === claude-oauth-token: end"),
    }


class Blocks:
    """The three blocks, run in the order the script has them, after the line
    with which the script sources the helper."""

    def __init__(self, start: Start, *, without_the_helper: bool = False):
        self.start = start
        self.tag = "before" if without_the_helper else "after"
        self.env_dir = start.tmp / f"container_environment-{self.tag}"
        self.env_dir.mkdir()
        self.calls = start.tmp / f"calls-{self.tag}"
        text = SETUP_CONFIGS.read_text()
        source = f". {LIB_IMAGE_PATH}\n"
        assert text.count(source) == 1
        assert text.index(source) < text.index('_op_tok_discovery="$(bashio::config')
        blocks = _blocks()
        if without_the_helper:
            # What the blocks were before they asked the helper: the same
            # text less the lines that call it.
            removed = {name: len(_HELPER_CALL.findall(block)) for name, block in blocks.items()}
            assert removed == {"discovery": 1, "github": 2, "claude": 1}, removed
            blocks = {name: _HELPER_CALL.sub("", block) for name, block in blocks.items()}
            assert "casa_op_" not in "".join(blocks.values())
            source = ""
        body = source + "\n".join(blocks.values()) + "\n"
        self.script = start.copy(f"blocks-{self.tag}", body, {
            **({} if without_the_helper else {LIB_IMAGE_PATH: (str(start.lib), 1)}),
            GITHUB_FILE: (str(self.env_dir / "GITHUB_TOKEN"), 2),
            CLAUDE_FILE: (str(self.env_dir / "CLAUDE_CODE_OAUTH_TOKEN"), 2),
            "/opt/casa/supervisor_discovery.py": ("discovery", 1),
        })

    def run(self, options: dict[str, str]) -> dict:
        """What the blocks did that anything else can see."""
        for d, tag in ((self.start.tools, "shadow"), (self.start.trusted, "trusted")):
            _write_exec(d / "op",
                        "#!/bin/sh\n"
                        f'printf "%s op [%s] token=[%s]\\n" "{tag}" "$*"'
                        f' "${{OP_SERVICE_ACCOUNT_TOKEN-unset}}" >> "{self.calls}"\n'
                        'printf "resolved:%s\\n" "$2"\n')
            _write_exec(d / "python3",
                        "#!/bin/sh\n"
                        f'printf "%s python3 [%s] token=[%s] auth=[%s]\\n" "{tag}" "$*"'
                        f' "${{OP_SERVICE_ACCOUNT_TOKEN-unset}}"'
                        f' "${{CASA_DISCOVERY_AUTH_ENABLED-unset}}" >> "{self.calls}"\n')
        for name in ("GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            (self.env_dir / name).write_text("left by an earlier start")
            (self.env_dir / name).chmod(0o600)
        if self.start.log.exists():
            self.start.log.unlink()
        if self.calls.exists():
            self.calls.unlink()

        r = self.start.start(self.script, options=options)

        files = {p.name: (p.read_text(), stat.S_IMODE(p.stat().st_mode))
                 for p in sorted(self.env_dir.iterdir())}
        return {"status": r.returncode, "stdout": r.stdout, "stderr": r.stderr,
                "calls": self.calls.read_text().splitlines() if self.calls.exists() else [],
                "files": files,
                "log": [ln for ln in self.start.lines() if ln.startswith("log.")]}


@pytest.mark.parametrize("token", ["", "null"])
@pytest.mark.parametrize("vault_option,vault", [("", "Casa"), ("null", "Casa"),
                                                ("Family", "Family")])
def test_setup_configs_blocks_on_builtin_call_op_read_with_the_marker(
        start, token, vault_option, vault):
    start.set_backend(b"builtin\n")

    seen = Blocks(start).run({"K_OP_TOKEN": token, "K_OP_VAULT": vault_option,
                              "K_CC_OAUTH": "op://Casa/Claude/credential"})

    assert (seen["status"], seen["stderr"]) == (0, "")
    assert seen["calls"] == [
        f"trusted python3 [discovery] token=[{MARKER}] auth=[true]",
        f"trusted op [read op://{vault}/GitHub/credential] token=[{MARKER}]",
        f"trusted op [read op://Casa/Claude/credential] token=[{MARKER}]",
    ]
    assert seen["files"] == {
        "GITHUB_TOKEN": (f"resolved:op://{vault}/GitHub/credential", 0o600),
        "CLAUDE_CODE_OAUTH_TOKEN": ("resolved:op://Casa/Claude/credential", 0o600),
    }


@pytest.mark.parametrize("case", ["the-other-word", "no-file"])
def test_setup_configs_blocks_on_1password_are_unchanged(start, case):
    """On 1Password the blocks do what they did before they asked the helper:
    for every combination of the three options, the same `op` calls with the
    same token, the same files and the same log lines as the same text
    without the helper's lines."""
    start.set_backend(NOT_BUILTIN[case])
    after, before = Blocks(start), Blocks(start, without_the_helper=True)
    called = 0
    for token in _TOKENS:
        for vault in _VAULTS:
            for oauth in _OAUTHS:
                options = {"K_OP_TOKEN": token, "K_OP_VAULT": vault, "K_CC_OAUTH": oauth}
                seen = after.run(options)
                assert seen == before.run(options), options
                assert (seen["status"], seen["stderr"]) == (0, ""), options
                assert MARKER not in json.dumps(seen), options
                called += len(seen["calls"])
    # 36 runs: discovery every time; GitHub with a token and a vault (4);
    # the Claude reference with a token (3).
    assert called == 36 + 4 + 3

    seen = after.run({"K_OP_TOKEN": "ops_a-real.token", "K_OP_VAULT": "Family",
                      "K_CC_OAUTH": "op://Family/Claude/credential"})
    assert seen["calls"] == [
        "trusted python3 [discovery] token=[ops_a-real.token] auth=[true]",
        "trusted op [read op://Family/GitHub/credential] token=[ops_a-real.token]",
        "trusted op [read op://Family/Claude/credential] token=[ops_a-real.token]",
    ]


# --- the order s6 starts them in ---------------------------------------------------

def test_graph_backend_before_store_before_setup_configs():
    g = graph(OVERLAY)
    assert g["init-secrets-backend"] == {"base"}
    assert g["svc-vault"] == {"init-secrets-backend", "init-memory-maintenance"}
    assert "svc-vault" in g["init-setup-configs"]
    # Everything that reads a secret at start, or serves the ones who do, comes
    # after the store has said it is ready.
    for unit in ("init-setup-configs", "init-plugin-store", "svc-casa", "svc-ttyd"):
        assert reaches(g, unit, "svc-vault"), unit
        assert reaches(g, unit, "init-secrets-backend"), unit
    # And the two new units wait for none of those.
    for unit in ("init-secrets-backend", "svc-vault"):
        assert not reaches(g, unit, "init-setup-configs"), unit
        assert (OVERLAY / "user-bundles.d/user/contents.d" / unit).is_file(), unit


# --- the image -----------------------------------------------------------------------

def _instructions(path: Path) -> list[str]:
    out, current = [], ""
    for raw in path.read_text().splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        current += raw.rstrip().removesuffix("\\") + " "
        if not raw.rstrip().endswith("\\"):
            out.append(" ".join(current.split()))
            current = ""
    return out


def test_image_moves_the_real_binary_aside_before_the_front_lands_on_its_name():
    steps = _instructions(REPO / "casa" / "Dockerfile")
    install = next(i for i, s in enumerate(steps) if "unzip /tmp/op.zip" in s)
    copy = steps.index("COPY rootfs /")
    chmod = next(i for i, s in enumerate(steps) if "chmod +x /usr/local/bin/casactl" in s)
    check = next(i for i, s in enumerate(steps) if "/usr/local/bin/op --version" in s)

    assert re.search(r"unzip /tmp/op\.zip -d /usr/local/bin; .*"
                     r"mv /usr/local/bin/op /usr/local/bin/op-1password; .*"
                     r"/usr/local/bin/op-1password --version$", steps[install]), steps[install]
    # The front arrives with the application, not executable, and is made so;
    # only then can the build ask it for the version of the program behind it.
    assert install < copy < chmod < check
    assert re.search(r"chmod \+x /usr/local/bin/casactl /usr/local/bin/op$", steps[chmod])
    assert '"$(/usr/local/bin/op --version)" = "${OP_CLI_VERSION}"' in steps[check]
    assert not [s for s in steps if re.search(r"(^|[ ;&|])op --version", s)], steps


def test_e2e_image_leaves_the_store_out():
    """The e2e image replaces the start scripts with its own and installs no
    `op`, so it runs neither backend: the two units, the edge to them and the
    front are taken out of it."""
    steps = _instructions(REPO / "test-local" / "Dockerfile.test")
    copy = steps.index("COPY casa/rootfs /")
    removal = next(i for i, s in enumerate(steps) if "svc-vault" in s)
    assert removal > copy
    removed = set(steps[removal].replace("RUN rm -rf ", "").split())
    base = "/etc/s6-overlay"
    assert removed == {
        f"{base}/s6-rc.d/init-secrets-backend",
        f"{base}/s6-rc.d/svc-vault",
        f"{base}/s6-rc.d/init-setup-configs/dependencies.d/svc-vault",
        f"{base}/user-bundles.d/user/contents.d/init-secrets-backend",
        f"{base}/user-bundles.d/user/contents.d/svc-vault",
        "/usr/local/bin/op",
    }
    # Nothing left in the tree names a unit that image does not have.
    for unit in ("init-secrets-backend", "svc-vault"):
        for found in (ROOTFS / "etc" / "s6-overlay").rglob(unit):
            path = "/" + str(found.relative_to(ROOTFS))
            assert any(path == r or path.startswith(r + "/") for r in removed), path
