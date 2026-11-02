#!/bin/sh
# Test override: generates nginx config with hardcoded port instead of bashio
OPTIONS=/data/options.json
INGRESS_PORT=8080
TERMINAL_ENABLED=$(jq -r '.enable_terminal // false' "$OPTIONS")

cat > /etc/nginx/nginx.conf <<NGINX
worker_processes 1;
error_log /dev/stdout info;
pid /tmp/nginx.pid;

events { worker_connections 128; }

http {
    map \$http_upgrade \$connection_upgrade {
        default upgrade;
        ''      close;
    }

    # --- Ingress server ---
    server {
        listen ${INGRESS_PORT} default_server;
        server_name _;

        location / {
            proxy_pass http://127.0.0.1:8099;
            proxy_http_version 1.1;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection \$connection_upgrade;
            proxy_read_timeout 300;
        }
NGINX

if [ "$TERMINAL_ENABLED" = "true" ]; then
    cat >> /etc/nginx/nginx.conf <<NGINX
        location /terminal/ {
            proxy_pass http://127.0.0.1:7681/terminal/;
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

# Close ingress server, add external API server, close http
cat >> /etc/nginx/nginx.conf <<'NGINX'
    }

    # --- External API server (no terminal) ---
    server {
        listen 18065;
        server_name _;

        # 5.7: the public hostname is not a front door for the dashboard.
        # Exact-match on / only; deeper paths fall through to the catch-all
        # below and keep their existing gates.
        location = / {
            return 404;
        }

        # v0.97.0 SECURITY (must mirror rootfs setup-nginx.sh): never proxy the
        # unauthenticated internal MCP/hooks fallback endpoints on the public
        # port — they dispatch CASA_TOOLS (search_memory→private memory,
        # plugin_add). Loopback (127.0.0.1:8099) is unaffected.
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

echo "[INFO] Nginx configured (terminal: ${TERMINAL_ENABLED}, local test mode)."
