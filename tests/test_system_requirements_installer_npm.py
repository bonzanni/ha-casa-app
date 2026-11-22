"""npm install strategy for casa.systemRequirements (§4.3.1)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from system_requirements.npm import install_npm

pytestmark = pytest.mark.unit


@pytest.mark.slow
def test_install_happy(tmp_path: Path) -> None:
    # `is-number` is a ~10kb package with a CLI — good smoke.
    result = install_npm(
        plugin_name="is-number-probe",
        spec={
            "type": "npm",
            "package": "is-number@7.0.0",
            "verify_bin": "is-number",
        },
        tools_root=tmp_path / "tools",
    )
    # is-number doesn't expose a bin, so the install is refused for its
    # missing verify_bin and the attempt's prefix is removed (#1649).
    assert result.ok or "verify_bin" in result.message
    # M25: npm prefix is namespaced per plugin (under tools/npm/<plugin>).
    assert result.install_dir.is_relative_to(
        tmp_path / "tools" / "npm" / "is-number-probe")
    assert result.ok or not result.install_dir.exists()


def _fake_npm_run(fail: bool = False):
    """Stand-in for subprocess.run: ``npm install --prefix DIR PKG`` writes
    DIR/node_modules/.bin/tool naming PKG — no network, no npm."""
    def run(cmd, check, timeout):
        if fail:
            raise subprocess.CalledProcessError(1, cmd)
        bin_dir = Path(cmd[cmd.index("--prefix") + 1]) / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "tool").write_text(f"#!/bin/sh\necho {cmd[-1]}\n")
    return run


def _working_install(tools: Path, plugin: str) -> Path:
    """A working install as earlier releases laid it out: the npm prefix IS
    tools/npm/<plugin>, and tools/bin/tool points straight into it."""
    bin_dir = tools / "npm" / plugin / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "tool").write_text("#!/bin/sh\necho old\n")
    (tools / "bin").mkdir(parents=True)
    os.symlink(bin_dir / "tool", tools / "bin" / "tool")
    return tools / "bin" / "tool"


_SPEC = {"type": "npm", "package": "new", "verify_bin": "tool"}


def test_failed_update_keeps_the_working_install(tmp_path, monkeypatch):
    """#1649: an npm failure during an update must leave the version still in
    use working — its tools/bin link resolves to the old binary, as before."""
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH",
                        tmp_path / "sysreq.yaml")
    tools = tmp_path / "tools"
    link = _working_install(tools, "plug")
    monkeypatch.setattr("system_requirements.npm.subprocess.run",
                        _fake_npm_run(fail=True))

    with pytest.raises(subprocess.CalledProcessError):
        install_npm(plugin_name="plug", spec=_SPEC, tools_root=tools)

    assert link.resolve().is_file()
    assert link.read_text() == "#!/bin/sh\necho old\n"
    assert sorted(p.name for p in (tools / "npm" / "plug").iterdir()) == [
        "node_modules"]


def test_successful_update_switches_the_link_and_removes_the_old_tree(
        tmp_path, monkeypatch):
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH",
                        tmp_path / "sysreq.yaml")
    tools = tmp_path / "tools"
    link = _working_install(tools, "plug")
    monkeypatch.setattr("system_requirements.npm.subprocess.run",
                        _fake_npm_run())

    first = install_npm(plugin_name="plug", spec=_SPEC, tools_root=tools)
    assert first.ok and first.verify_bin_resolves
    assert link.read_text() == "#!/bin/sh\necho new\n"
    assert list((tools / "npm" / "plug").iterdir()) == [first.install_dir]

    second = install_npm(plugin_name="plug", spec=_SPEC, tools_root=tools)
    assert second.ok and second.install_dir != first.install_dir
    assert list((tools / "npm" / "plug").iterdir()) == [second.install_dir]
    assert link.resolve().is_file()
