# tests/test_svc_ttyd_socket.py
"""#514: svc-ttyd must bind ttyd to a root-restricted UNIX socket.

Static-source test over the run script (no bashio/s6 boot): the once-exposed
TCP `127.0.0.1:7681` root shell must be gone, replaced by a filesystem UNIX
socket fenced by DAC — a 0700 dir owned by the nginx worker user (www-data)
plus a www-data-owned socket — so a dropped-uid engagement cannot reach it.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_RUN = (
    Path(__file__).resolve().parents[1]
    / "casa/rootfs/etc/s6-overlay/s6-rc.d/svc-ttyd/run"
)


def _code_lines(text: str) -> str:
    """The script's executable lines, comments stripped — so assertions about
    what ttyd is *invoked* with can't be satisfied by prose in a comment."""
    return "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )


def test_ttyd_binds_unix_socket_not_tcp():
    text = _RUN.read_text()
    code = _code_lines(text)
    assert "--interface" in code
    # The socket path is assembled from TERM_DIR + a .sock leaf.
    assert "TERM_DIR=/run/casa-term" in code
    assert 'TERM_SOCK="${TERM_DIR}/ttyd.sock"' in code
    assert '--interface "${TERM_SOCK}"' in code
    # The old TCP exposure must be fully removed from the invocation.
    assert "127.0.0.1" not in code
    assert "7681" not in code
    assert "--port" not in code


def test_ttyd_socket_owned_by_www_data():
    text = _RUN.read_text()
    assert "--socket-owner www-data:www-data" in text


def test_terminal_dir_created_0700_owned_www_data_before_exec():
    text = _RUN.read_text()
    assert "install -d -m 0700 -o www-data -g www-data" in text
    # The dir must be prepared before ttyd is exec'd (exec replaces the shell).
    assert text.index("install -d") < text.index("exec ttyd")


def test_disabled_branch_still_sleeps():
    """When the terminal is disabled the service must idle, not exit-and-respawn."""
    text = _RUN.read_text()
    assert "exec sleep infinity" in text
