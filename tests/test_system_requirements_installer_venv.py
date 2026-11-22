"""venv install strategy for casa.systemRequirements (§4.3.1)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from system_requirements.venv import install_venv

pytestmark = pytest.mark.unit


@pytest.mark.slow
def test_install_happy(tmp_path: Path) -> None:
    # Pick a tiny PyPI package that's stable + small (pip itself).
    result = install_venv(
        plugin_name="pip-probe",
        spec={
            "type": "venv",
            "package": "pip==24.0",
            "python": sys.executable,
            "verify_bin": "pip",
        },
        tools_root=tmp_path / "tools",
    )
    assert result.ok
    assert (tmp_path / "tools" / "bin" / "pip").is_symlink()


def _fake_venv_run(fail_pip: bool = False):
    """Stand-in for subprocess.run: ``-m venv DIR`` creates DIR/bin and
    ``pip install PKG`` writes DIR/bin/tool naming PKG — no network, no pip."""
    def run(cmd, check, timeout):
        if cmd[1:3] == ["-m", "venv"]:
            (Path(cmd[3]) / "bin").mkdir(parents=True, exist_ok=True)
            return
        if fail_pip:
            raise subprocess.CalledProcessError(1, cmd)
        (Path(cmd[0]).parent / "tool").write_text(f"#!/bin/sh\necho {cmd[-1]}\n")
    return run


def _working_install(tools: Path, plugin: str) -> Path:
    """A working install as earlier releases laid it out: the venv IS
    tools/venv-<plugin>, and tools/bin/tool points straight into it."""
    venv_bin = tools / f"venv-{plugin}" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "tool").write_text("#!/bin/sh\necho old\n")
    (tools / "bin").mkdir(parents=True)
    os.symlink(venv_bin / "tool", tools / "bin" / "tool")
    return tools / "bin" / "tool"


_SPEC = {"type": "venv", "package": "new", "verify_bin": "tool"}


def test_failed_update_keeps_the_working_install(tmp_path, monkeypatch):
    """#1649: a pip failure during an update must leave the version still in
    use working — its tools/bin link resolves to the old binary, as before."""
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH",
                        tmp_path / "sysreq.yaml")
    tools = tmp_path / "tools"
    link = _working_install(tools, "plug")
    monkeypatch.setattr("system_requirements.venv.subprocess.run",
                        _fake_venv_run(fail_pip=True))

    with pytest.raises(subprocess.CalledProcessError):
        install_venv(plugin_name="plug", spec=_SPEC, tools_root=tools)

    assert link.resolve().is_file()
    assert link.read_text() == "#!/bin/sh\necho old\n"
    # Nothing half-built is left next to the old install.
    assert sorted(p.name for p in (tools / "venv-plug").iterdir()) == ["bin"]


def test_successful_update_switches_the_link_and_removes_the_old_tree(
        tmp_path, monkeypatch):
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH",
                        tmp_path / "sysreq.yaml")
    tools = tmp_path / "tools"
    link = _working_install(tools, "plug")
    monkeypatch.setattr("system_requirements.venv.subprocess.run",
                        _fake_venv_run())

    first = install_venv(plugin_name="plug", spec=_SPEC, tools_root=tools)
    assert first.ok and first.verify_bin_resolves
    assert link.read_text() == "#!/bin/sh\necho new\n"
    # The old venv is gone; only the new one remains, inside the plugin's own
    # venv-<plugin> directory.
    assert list((tools / "venv-plug").iterdir()) == [first.install_dir]

    second = install_venv(plugin_name="plug", spec=_SPEC, tools_root=tools)
    assert second.ok and second.install_dir != first.install_dir
    assert list((tools / "venv-plug").iterdir()) == [second.install_dir]
    assert link.resolve().is_file()
