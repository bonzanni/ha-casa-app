"""Structural guards for the init-plugin-store s6 oneshot + the marketplace
strip from setup-configs.sh (spec §3.6)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent / "casa" / "rootfs"
_S6 = _ROOT / "etc" / "s6-overlay" / "s6-rc.d"
_SCRIPTS = _ROOT / "etc" / "s6-overlay" / "scripts"


def test_oneshot_service_files_exist_with_exact_contents():
    d = _S6 / "init-plugin-store"
    assert (d / "type").read_text() == "oneshot\n"
    assert (d / "up").read_text() == \
        "/etc/s6-overlay/scripts/setup-plugin-store.sh\n"
    assert (d / "dependencies.d" / "init-setup-configs").is_file()


def test_svc_deps_and_bundle_membership():
    # svc-casa + svc-casa-mcp start AFTER the plugin store; enrolled in bundle.
    assert (_S6 / "svc-casa" / "dependencies.d" / "init-plugin-store").is_file()
    assert (_S6 / "svc-casa-mcp" / "dependencies.d" / "init-plugin-store").is_file()
    assert (_S6 / "user" / "contents.d" / "init-plugin-store").is_file()


def test_setup_plugin_store_script():
    text = (_SCRIPTS / "setup-plugin-store.sh").read_text()
    assert text.split("\n", 1)[0] == "#!/command/with-contenv /opt/casa/scripts/core-bashio.sh"
    assert "/opt/casa/plugin_boot.py" in text
    assert text.rstrip().endswith("exit 0")
    assert "\r" not in text                          # LF only


def test_setup_configs_marketplace_machinery_removed():
    text = (_SCRIPTS / "setup-configs.sh").read_text()
    assert "claude plugin marketplace add" not in text
    assert 'claude -p "noop"' not in text
    assert "seed-copy: begin" not in text
    assert "marketplace-user/.claude-plugin" not in text   # no user-mkt seed
    # cc-home HOME setup is preserved (casa-main + CC CLI still need it).
    assert "export HOME=/config/cc-home" in text
