"""H1 (v0.50.0): the generated nginx ingress server block must carry the
HA-mandated source restriction (allow 172.30.32.2; deny all;) so only the
Supervisor ingress proxy — not any peer container on the hassio bridge —
can reach the operator dashboard / proxied API / web terminal.

Static-source test: parse the emitted heredoc in setup-nginx.sh rather than
booting bashio/nginx, so it runs in the pure-unit tier.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "casa/rootfs/etc/s6-overlay/scripts/setup-nginx.sh"
)


def _ingress_block(text: str) -> str:
    """Isolate the ingress server block (marker → external API marker)."""
    start = text.index("# --- Ingress server")
    end = text.index("# --- External API server")
    return text[start:end]


def test_setup_nginx_ingress_restricts_to_supervisor():
    text = _SCRIPT.read_text()
    ingress = _ingress_block(text)
    assert "allow 172.30.32.2;" in ingress, "ingress server missing supervisor allow"
    assert "deny all;" in ingress, "ingress server missing deny all"
    # The filter must precede the proxy location so it applies to every
    # route, including /terminal/.
    assert ingress.index("deny all;") < ingress.index("location / {")


def test_external_api_server_unaffected():
    """The public 18065 server keeps its path-based gating and must NOT
    inherit the ingress source filter (it is reached from outside the
    bridge, not from 172.30.32.2)."""
    text = _SCRIPT.read_text()
    ext_start = text.index("# --- External API server")
    external = text[ext_start:]
    assert "allow 172.30.32.2;" not in external


def test_external_api_server_blocks_internal_mcp_and_hooks():
    """v0.97.0 SECURITY: the public 18065 server must NOT proxy the
    unauthenticated internal fallback endpoints /mcp/ and /hooks/ (they
    dispatch CASA_TOOLS — recall_memory returns private memory, plugin_add
    installs plugins). They must return 404 externally; loopback (in-container
    workspace subprocesses on 127.0.0.1:8099) is unaffected."""
    text = _SCRIPT.read_text()
    ext_start = text.index("# --- External API server")
    external = text[ext_start:]
    # Both deny blocks must appear BEFORE the catch-all proxy location so they
    # win (nginx longest-prefix match makes /mcp/ + /hooks/ beat /).
    assert "location /mcp/ {" in external
    assert "location /hooks/ {" in external
    catchall = external.index("proxy_pass http://127.0.0.1:8099;")
    assert external.index("location /mcp/ {") < catchall
    assert external.index("location /hooks/ {") < catchall
    # And they return 404, not proxy.
    mcp_block = external[external.index("location /mcp/ {"):]
    assert mcp_block[:mcp_block.index("}")].strip().endswith("return 404;")


# ---------------------------------------------------------------------------
# INV-CB-006: a callback query string (?code=...&state=...) must
# never reach nginx's access log — the third surface, alongside the aiohttp
# request-line redaction and log-path suppression.
# ---------------------------------------------------------------------------


def test_http_block_declares_callback_log_suppression_map():
    """The map is declared once, in the http {} context, before either
    server block — nginx requires map at http scope to be usable inside a
    server's access_log directive.

    The ingress server sits in the unquoted (variable-expanding) heredoc, so
    the SOURCE text escapes nginx's ``$`` as ``\\$`` — this asserts the
    source form, not the rendered nginx.conf."""
    text = _SCRIPT.read_text()
    http_idx = text.index("http {")
    map_idx = text.index(r"map \$uri \$casa_cb_log {")
    ingress_idx = text.index("# --- Ingress server")
    assert http_idx < map_idx < ingress_idx
    map_block = text[map_idx:text.index("}", map_idx) + 1]
    # The regex must match both the bare /callback path and its sub-paths
    # (/callback/<name>), while still excluding lookalikes such as
    # /callbackish — an end-of-string or slash anchor after the literal,
    # not a trailing-slash-only match.
    assert r"~^/callback(/|\$) 0;" in map_block
    assert "default          1;" in map_block or "default 1;" in map_block


def test_callback_log_suppression_regex_matches_bare_and_subpaths_only():
    """Behavioral check on the map regex (rendered form, backslash-escape
    stripped): a bare ``/callback`` (no trailing slash — what ``GET
    /callback?code=...&state=...`` presents as ``$uri``) and any
    ``/callback/<name>`` sub-path must be classified into the suppressed
    bucket, while a merely-prefixed path like ``/callbackish`` must not."""
    import re

    text = _SCRIPT.read_text()
    map_idx = text.index(r"map \$uri \$casa_cb_log {")
    map_block = text[map_idx:text.index("}", map_idx) + 1]
    match = re.search(r"~\^(/callback\(/\|\\\$\))\s+0;", map_block)
    assert match, "callback-suppression regex not found in map block"
    # Rendered nginx.conf form: \$ -> $ once bash expands the heredoc.
    rendered_pattern = match.group(1).replace(r"\$", "$")
    compiled = re.compile("^" + rendered_pattern)
    assert compiled.match("/callback")
    assert compiled.match("/callback/google")
    assert compiled.match("/callback/effective")
    assert not compiled.match("/callbackish")
    assert not compiled.match("/not-callback")


def test_ingress_server_suppresses_callback_query_from_access_log():
    """Ingress server block: still inside the unquoted heredoc — ``\\$``."""
    text = _SCRIPT.read_text()
    ingress = _ingress_block(text)
    directive = r"access_log /dev/stdout combined if=\$casa_cb_log;"
    assert directive in ingress
    # Must precede the proxy location so it governs every route in this
    # server, including /terminal/.
    assert ingress.index(directive) < ingress.index("location / {")


def test_external_api_server_suppresses_callback_query_from_access_log():
    """The 18065 server is the one an OAuth provider's redirect actually
    reaches — this is the surface INV-CB-006 exists to protect. This block
    is emitted from the QUOTED heredoc (``<<'NGINX'``), so the source form
    has no backslash before ``$``."""
    text = _SCRIPT.read_text()
    ext_start = text.index("# --- External API server")
    external = text[ext_start:]
    directive = "access_log /dev/stdout combined if=$casa_cb_log;"
    assert directive in external
    assert external.index(directive) < external.index("location = / {")


# ---------------------------------------------------------------------------
# #514: the web terminal (ttyd) is an unauthenticated root shell. It must be
# bound to a root-restricted UNIX socket, not TCP loopback, so a dropped-uid
# engagement cannot reach it by bypassing nginx. nginx proxies /terminal/ over
# that socket, and its worker identity (www-data) must be pinned so the socket
# owner can be matched to it.
# ---------------------------------------------------------------------------


def test_nginx_worker_user_pinned_to_www_data():
    """The worker user must be explicit — the ttyd socket is owned
    www-data:www-data, and leaving the worker user to nginx's compiled default
    would silently break the terminal proxy (or the DAC fence) on a base-image
    change."""
    text = _SCRIPT.read_text()
    # http-scope directive, before any server block.
    assert "\nuser www-data;\n" in text
    assert text.index("user www-data;") < text.index("# --- Ingress server")


def test_terminal_proxies_over_unix_socket_not_tcp():
    """The enabled /terminal/ location must proxy to the ttyd UNIX socket, and
    the old TCP 127.0.0.1:7681 target must be gone entirely."""
    text = _SCRIPT.read_text()
    assert "proxy_pass http://unix:/run/casa-term/ttyd.sock:/terminal/;" in text
    assert "7681" not in text, "TCP terminal port 7681 still referenced"
    assert "127.0.0.1:7681" not in text
