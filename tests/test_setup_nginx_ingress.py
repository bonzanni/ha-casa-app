"""H1 (v0.50.0): the generated nginx ingress server block must carry the
HA-mandated source restriction (allow 172.30.32.2; deny all;) so only the
Supervisor ingress proxy — not any peer container on the hassio bridge —
can reach the operator dashboard / proxied API / web terminal.

Static-source test: parse the emitted heredoc in setup-nginx.sh rather than
booting bashio/nginx, so it runs in the pure-unit tier.
"""

import re
import shutil
import subprocess
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
    dispatch CASA_TOOLS — search_memory returns private memory, plugin_add
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


def test_phone_webhook_is_refused_on_ingress_only():
    """The call webhook is reached from outside on the external listener;
    the ingress server refuses the path, the external one proxies it."""
    text = _SCRIPT.read_text()
    ingress = _ingress_block(text)
    assert "location = /phone/incoming {" in ingress
    block = ingress[ingress.index("location = /phone/incoming {"):]
    assert block[:block.index("}")].strip().endswith("return 404;")
    external = text[text.index("# --- External API server"):]
    assert "/phone/incoming" not in external


# ---------------------------------------------------------------------------
# The secrets page: served on ingress only when the install keeps its secrets
# in Casa's built-in store, and never on the external listener. The script
# runs here for real, under bash with stand-ins for the bashio functions it
# calls, writing to a fixture path instead of /etc/nginx/nginx.conf and
# reading a fixture backend file through a copy of the shipped helper.
# ---------------------------------------------------------------------------

_LIB = _SCRIPT.parents[3] / "opt/casa/scripts/secrets-backend-lib.sh"
_LIB_IMAGE_PATH = "/opt/casa/scripts/secrets-backend-lib.sh"
_BACKEND_FILE = "/run/casa-vault/backend"
_CONF = "/etc/nginx/nginx.conf"

_STATIC = (
    'location /vault/static/ { alias /opt/casa/vault_static/; '
    'types { text/css css; application/javascript js; } '
    'default_type application/octet-stream; '
    'add_header Cache-Control "no-store" always; '
    'add_header X-Content-Type-Options "nosniff" always; }')
_PROXY = (
    'location /vault/ { proxy_pass http://unix:/run/casa-vault-page/page.sock:/; '
    'proxy_set_header Host $host; '
    'proxy_set_header X-Ingress-Path $http_x_ingress_path; '
    'proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto; '
    'proxy_set_header X-Forwarded-Host $http_x_forwarded_host; '
    'proxy_read_timeout 75; client_max_body_size 300k; }')
_NOT_SERVED = "location /vault/ { return 404; }"

# The options set the real bashio puts the script under.
_BASHIO = """set -o errexit -o nounset -o pipefail
bashio::app.ingress_port() { echo 8099; }
bashio::config.true() { [ "${K_TERMINAL:-false}" = true ]; }
bashio::config() { echo "${K_TERMINAL:-false}"; }
bashio::log.info() { printf '%s\\n' "$*" >> "$K_LOG"; }
"""

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _render(tmp_path, backend: bytes | None, *, terminal: bool = False) -> str:
    """The nginx.conf setup-nginx.sh writes when the backend file holds BACKEND
    (None: no file at all)."""
    backend_file = tmp_path / "backend"
    if backend is not None:
        backend_file.write_bytes(backend)
    lib_text = _LIB.read_text()
    assert lib_text.count(_BACKEND_FILE) == 2
    lib = tmp_path / "secrets-backend-lib.sh"
    lib.write_text(lib_text.replace(_BACKEND_FILE, str(backend_file)))
    conf = tmp_path / "nginx.conf"
    text = _SCRIPT.read_text()
    assert text.count(_LIB_IMAGE_PATH) == 1
    script = tmp_path / "setup-nginx.sh"
    script.write_text(_BASHIO + text.replace(_LIB_IMAGE_PATH, str(lib))
                      .replace(_CONF, str(conf)))
    log = tmp_path / "log"
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "K_LOG": str(log),
                            "K_TERMINAL": "true" if terminal else "false"},
                       timeout=30)
    assert (r.returncode, r.stderr) == (0, ""), r.stderr
    return conf.read_text()


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _servers(conf: str) -> tuple[str, str]:
    """The rendered ingress server and external server, whitespace collapsed."""
    ingress_at = conf.index("# --- Ingress server")
    external_at = conf.index("# --- External API server")
    return _flat(conf[ingress_at:external_at]), _flat(conf[external_at:])


@needs_bash
@pytest.mark.parametrize("terminal", [False, True], ids=["terminal-off", "terminal-on"])
def test_the_secrets_page_is_served_on_ingress_for_the_built_in_store(tmp_path, terminal):
    conf = _render(tmp_path, b"builtin\n", terminal=terminal)
    ingress, external = _servers(conf)
    assert _STATIC in ingress
    assert _PROXY in ingress
    assert _NOT_SERVED not in ingress
    assert ingress.count("location /vault/ {") == 1
    # never on the listener outside Home Assistant
    assert external.count("location /vault/") == 1
    assert _NOT_SERVED in external
    assert "casa-vault-page" not in external and "vault_static" not in external


@needs_bash
def test_the_static_files_are_served_with_their_two_types_only(tmp_path):
    # Casa's nginx.conf includes no mime.types: without this block the files
    # go out as text/plain, and under nosniff browsers refuse both.
    ingress, _ = _servers(_render(tmp_path, b"builtin\n"))
    static = ingress[ingress.index("location /vault/static/ {"):]
    types = re.search(r"types \{ ([^}]*)\}", static).group(1)
    assert sorted(entry.strip() for entry in types.split(";") if entry.strip()) == \
        ["application/javascript js", "text/css css"]


@needs_bash
@pytest.mark.parametrize("backend", [b"1password\n", None, b"builtin \n"],
                         ids=["1password", "no-file", "not-the-word"])
def test_the_secrets_page_answers_404_everywhere_otherwise(tmp_path, backend):
    conf = _render(tmp_path, backend)
    ingress, external = _servers(conf)
    assert _NOT_SERVED in ingress and _NOT_SERVED in external
    assert ingress.count("location /vault/") == external.count("location /vault/") == 1
    assert "casa-vault-page" not in conf
    assert "vault_static" not in conf


@needs_bash
def test_the_log_line_says_whether_the_page_is_served(tmp_path):
    _render(tmp_path, b"builtin\n")
    assert (tmp_path / "log").read_text() == \
        "Nginx configured (terminal: false, secrets page: served)\n"
