#!/command/with-contenv bashio
# 5.5 item 3: strip ANSI from bashio output for clean docker logs.
export BASHIO_LOG_NO_COLORS=true
export NO_COLOR=1

INGRESS_PORT=$(bashio::addon.ingress_port)

cat > /etc/nginx/nginx.conf <<NGINX
worker_processes 1;
error_log /dev/stdout warn;
pid /tmp/nginx.pid;

# #514: pin the worker identity explicitly. The master runs as root and binds
# the listen ports; workers drop to www-data. This is the identity that must be
# able to reach the ttyd UNIX socket (owned www-data:www-data) while the
# dropped-uid engagement cannot — so it must not be left to nginx's compiled
# default.
user www-data;

events { worker_connections 128; }

http {
    map \$http_upgrade \$connection_upgrade {
        default upgrade;
        ''      close;
    }

    # INV-CB-006: the third surface — a callback query string (the
    # provider's ?code=...&state=...) must never reach nginx's access log,
    # matching the redaction/suppression already applied at the aiohttp
    # layer. \$uri excludes the query string by definition (unlike
    # \$request or \$request_uri), so classifying purely on it is sufficient.
    map \$uri \$casa_cb_log {
        ~^/callback(/|\$) 0;
        default          1;
    }

    # --- Ingress server (HA-authenticated) ---
    server {
        listen ${INGRESS_PORT} default_server;
        server_name _;

        # No access_log directive here previously meant the compiled-in
        # nginx default (on) applied — this makes the callback suppression
        # explicit rather than relying on \$casa_cb_log alone ever being
        # consulted. combined is nginx's stock format.
        access_log /dev/stdout combined if=\$casa_cb_log;

        # HA ingress hardening (developers.home-assistant.io): only the
        # Supervisor ingress proxy may reach this port. Deny every other
        # peer on the hassio bridge (172.30.32.0/23). Placed at server
        # scope so it filters every route, including /terminal/.
        allow 172.30.32.2;
        deny all;

        location / {
            proxy_pass http://127.0.0.1:8099;
            proxy_http_version 1.1;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Ingress-Path \$http_x_ingress_path;
            # WebSocket upgrade is benign for non-WS requests: the map above
            # yields an empty \$connection_upgrade when no Upgrade header arrives.
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection \$connection_upgrade;
            proxy_read_timeout 300;
        }
NGINX

# Terminal on ingress
if bashio::config.true 'enable_terminal'; then
    cat >> /etc/nginx/nginx.conf <<NGINX
        location /terminal/ {
            # #514: ttyd listens on a root-restricted UNIX socket, not TCP.
            proxy_pass http://unix:/run/casa-term/ttyd.sock:/terminal/;
            proxy_http_version 1.1;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection \$connection_upgrade;
            proxy_read_timeout 86400;
            proxy_send_timeout 86400;
        }
NGINX
else
    cat >> /etc/nginx/nginx.conf <<'NGINX'
        location /terminal/ {
            default_type text/html;
            return 200 '<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Casa Terminal</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; display: flex;
         justify-content: center; align-items: center; min-height: 80vh;
         margin: 0; background: #1e293b; color: #e2e8f0; }
  .card { text-align: center; max-width: 420px; padding: 2rem; }
  h1 { font-size: 1.4rem; margin-bottom: 0.5rem; }
  p { color: #94a3b8; line-height: 1.6; }
  code { background: #334155; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }
</style></head>
<body><div class="card">
  <h1>Web Terminal is disabled</h1>
  <p>To enable it, go to <strong>Settings &rarr; Add-ons &rarr; Casa &rarr; Configuration</strong>
     and set <code>Enable Web Terminal</code> to on, then restart the add-on.</p>
</div></body></html>';
        }
NGINX
fi

# Close ingress server block
cat >> /etc/nginx/nginx.conf <<'NGINX'
    }

    # --- External API server (no terminal) ---
    server {
        listen 18065;
        server_name _;

        # See the ingress server's identical directive above (INV-CB-006)
        # — this is the surface that actually matters here, since
        # this server is the one the callback provider's redirect reaches.
        access_log /dev/stdout combined if=$casa_cb_log;

        # 5.7: the public hostname is not a front door for the dashboard.
        # Exact-match on / only; deeper paths fall through to the catch-all
        # below and keep their existing gates.
        location = / {
            return 404;
        }

        # v0.97.0 SECURITY, retained as defense in depth: the MCP + hooks
        # endpoints (/mcp/casa-framework, /hooks/resolve) are UNAUTHENTICATED
        # and exist for in-container workspace subprocesses only (served by
        # svc-casa-mcp on loopback 8100 since v0.164.0 — no longer registered
        # on the 8099 app at all). When they were proxied publicly, an
        # attacker could POST a JSON-RPC tools/call for recall_memory
        # (returns PRIVATE memory) or plugin_add (arbitrary plugin install),
        # bypassing webhook auth + origin containment entirely. Keep these
        # 404s so a future re-registration on 8099 stays unexposed.
        location /mcp/ {
            return 404;
        }
        location /hooks/ {
            return 404;
        }

        location / {
            proxy_pass http://127.0.0.1:8099;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            # WebSocket upgrade is benign for non-WS requests: the map above
            # yields an empty $connection_upgrade when no Upgrade header arrives.
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_read_timeout 300;
        }

        location /terminal/ {
            return 404;
        }
    }
}
NGINX

bashio::log.info "Nginx configured (terminal: $(bashio::config 'enable_terminal'))"
