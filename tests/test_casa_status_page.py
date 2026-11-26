"""Casa's status page, the first thing Home Assistant's "Open web UI" shows.

When the install keeps its secrets in Casa's built-in store, the page links to
the secrets page beside its Terminal and Health links; otherwise there is no
such link, since the secrets page answers 404 there. Which store is used is
read from the file written at container start, the way the start scripts read
it: only the exact word `builtin` on the first line counts.
"""
import re

import pytest

import casa_core

PREFIX = "/api/hassio_ingress/abc"
LINK = f'<a class="btn primary" href="{PREFIX}/vault/">Secrets</a>'


def render():
    return casa_core._status_page(agent_rows="", channel_rows="", system_rows="",
                                  terminal_enabled=False, ingress_path=PREFIX,
                                  version="1.2.3")


@pytest.fixture
def backend(tmp_path, monkeypatch):
    path = tmp_path / "backend"
    monkeypatch.setattr(casa_core, "SECRETS_BACKEND_FILE", str(path))
    return path


def test_the_secrets_link_sits_with_the_other_links_for_the_built_in_store(backend):
    backend.write_text("builtin\n")
    page = render()
    assert page.count(LINK) == 1
    actions = re.search(r'<div class="actions">(.*?)</div>', page, re.DOTALL).group(1)
    assert LINK in actions
    assert f'href="{PREFIX}/terminal/">Terminal</a>' in actions
    assert f'href="{PREFIX}/healthz">Health Check</a>' in actions


def test_a_backend_file_without_a_final_newline_still_counts(backend):
    backend.write_text("builtin")
    assert LINK in render()


@pytest.mark.parametrize("content", [None, b"1password\n", b"", b"builtin \n", b"builtin\r\n",
                                     b"BUILTIN\n", b"\nbuiltin\n", b"\xff\xfe\n", "directory"],
                         ids=["no-file", "1password", "empty", "trailing-space", "crlf",
                              "upper-case", "second-line", "not-utf8", "directory"])
def test_no_secrets_link_otherwise(backend, content):
    if content == "directory":
        backend.mkdir()
    elif content is not None:
        backend.write_bytes(content)
    page = render()
    assert "/vault/" not in page and "Secrets" not in page
    assert "Health Check" in page and "Casa v1.2.3" in page


def test_the_backend_file_is_the_one_the_start_scripts_write():
    assert casa_core.SECRETS_BACKEND_FILE == "/run/casa-vault/backend"
