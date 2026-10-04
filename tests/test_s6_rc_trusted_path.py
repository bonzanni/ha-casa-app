"""#987: a file published into the plugin tools directory does not change
which program the s6 driver runs.

`setup-configs.sh` prepends `/config/tools/bin` — where a plugin's `verify_bin`
executables land — to the PATH of every s6 service, casa-core included. The
driver runs `s6-rc-compile`, `s6-rc-update`, `s6-rc`, `s6-svc` and `s6-svstat`
by bare name, so before this fix a same-named file there was what those
root-run supervision calls executed.

These tests use the REAL `subprocess.run` and real executables in two
directories — `trusted/` (stands for the image PATH) and `tools/` (the plugin
tools directory, prepended ahead of it) — and assert which one ran. A stubbed
`subprocess.run` would prove nothing here: the lookup happens inside CPython's
subprocess, against the PATH of the env it is given.

Every call site is covered by construction: `tests/test_launch_path_guard.py`
reports any `subprocess.run` in `drivers/s6_rc.py` outside `_run` as
unsupported, so each site either goes through `_run` (pinned here) or turns
that guard red.
"""
from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from drivers import s6_rc

PROGRAMS = ("s6-rc-compile", "s6-rc-update", "s6-rc", "s6-svc", "s6-svstat")


def _stub(directory: Path, name: str, tag: str, log: Path, out: str = "") -> None:
    path = directory / name
    path.write_text(
        "#!/bin/sh\n"
        f'printf "%s %s\\n" "{tag}" "${{0##*/}} $*" >> "{log}"\n'
        + (f"printf '%s\\n' '{out}'\n" if out else "")
        # What s6-rc itself runs by name inherits the env: resolve a child.
        + ('[ -n "$CASA_TEST_CHILD" ] && "$CASA_TEST_CHILD"\n'
           if name == "s6-rc" else "")
        + "exit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    # raising=False: on a tree without the fix the end-to-end case still runs
    # and reports the shadow, rather than erroring on a missing attribute.
    trusted, tools = tmp_path / "trusted", tmp_path / "tools"
    trusted.mkdir()
    tools.mkdir()
    log = tmp_path / "ran.log"
    log.touch()
    for name in PROGRAMS + ("child-prog",):
        _stub(trusted, name, "real", log, out="true true 4242")
        _stub(tools, name, "shadow", log, out="true true 4242")
    monkeypatch.setattr(s6_rc, "PLUGIN_TOOLS_BIN", str(tools), raising=False)
    return trusted, tools, log


def _ran(log: Path) -> list[str]:
    return [line.split(" ", 1)[0] for line in log.read_text().splitlines()]


def _spellings(tools: Path, tmp_path: Path) -> list[str]:
    link = tmp_path / "tools-link"
    link.symlink_to(tools)
    return [str(tools), str(tools) + "/", "/" + str(tools),
            str(tools).replace("/tools", "/./tools"), str(link)]


@pytest.mark.parametrize("program", PROGRAMS)
@pytest.mark.parametrize("spelling", range(5))
def test_run_resolves_past_every_spelling_of_the_tools_dir(
        dirs, tmp_path, monkeypatch, program, spelling):
    trusted, tools, log = dirs
    entry = _spellings(tools, tmp_path)[spelling]
    monkeypatch.setenv("PATH", f"{entry}{os.pathsep}{trusted}")

    s6_rc._run([program, "-x"], check=True, capture_output=True, text=True)

    assert _ran(log) == ["real"], log.read_text()


def test_what_s6_rc_runs_by_name_resolves_the_same_way(dirs, monkeypatch):
    trusted, tools, log = dirs
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{trusted}")
    monkeypatch.setenv("CASA_TEST_CHILD", "child-prog")

    s6_rc._run(["s6-rc", "-u", "change", "x"], check=True)

    assert _ran(log) == ["real", "real"], log.read_text()


def test_other_path_entries_and_env_are_kept_in_order(dirs, monkeypatch):
    trusted, tools, _ = dirs
    monkeypatch.setenv("PATH", os.pathsep.join(
        ["/a", str(tools), "/b", str(tools) + "/", str(trusted)]))
    monkeypatch.setenv("CASA_TEST_KEEP", "1")
    before = dict(os.environ)

    env = s6_rc._trusted_env()

    # Only PATH differs, and only by the tools entries; os.environ untouched.
    assert env == {**before,
                   "PATH": os.pathsep.join(["/a", "/b", str(trusted)])}
    assert dict(os.environ) == before


def test_driver_helpers_run_the_image_programs(dirs, tmp_path, monkeypatch):
    """End to end through two public helpers, one direct and one threaded."""
    trusted, tools, log = dirs
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{trusted}")
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(tmp_path / "svc"))
    (tmp_path / "svc").mkdir()
    monkeypatch.setattr(s6_rc, "LIVE_DB_SYMLINK", str(tmp_path / "live"))

    asyncio.run(s6_rc.start_service(engagement_id="e1"))
    try:
        asyncio.run(s6_rc.compile_and_update())
    except Exception:  # noqa: BLE001 — only which program ran is asserted
        pass

    ran = log.read_text().splitlines()
    assert ran and all(line.startswith("real ") for line in ran), ran
    assert {line.split()[1] for line in ran} >= {"s6-rc", "s6-rc-compile"}, ran
