#!/usr/bin/env bash
# Casa Browser's real-Chrome passkey pin (slice 5v): local and opt-in, never in CI.
#
#   <one JSON line> | bash casa_browser/test/run-live-passkey.sh
#
# The JSON line (read from stdin, held in memory, never written or printed) names a real
# 1Password guest account and its browsing vault: {"op_token", "vault", "guest_sign_in_address",
# "guest_email", "guest_password", "guest_secret_key", "item_id"}, optionally "password_item_id";
# item_id is the vault's webauthn.io item, whose passkey must have been registered or used at
# webauthn.io in the last 24 hours; password_item_id, the vault's the-internet.herokuapp.com
# item (without it that site's run has no linked item). Needs the internet. Builds the app and test images, starts the app container with its
# own s6 services (the enforcing proxy, the launcher, Chrome) and options holding only the account
# host, runs tests/integration/livepasskey.py, then scans the container's log for every value.
set -euo pipefail
cd "$(dirname "$0")/.."
IFS= read -r secrets
[ -n "$secrets" ] || { echo "run-live-passkey: no JSON line on stdin" >&2; exit 2; }

app_image="casa-browser:${CASA_IT_TAG:-live}-app"
test_image="casa-browser:${CASA_IT_TAG:-live}-test"
tag="casa-browser-live-$$"
cleanup() {
  docker rm -f "$tag-app" >/dev/null 2>&1 || true
  docker volume rm "$tag-data" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build ${DOCKER_PULL:-} -t "$app_image" .
docker build -t "$test_image" -f test/Dockerfile --build-arg APP_IMAGE="$app_image" .

host="$(printf '%s\n' "$secrets" | python3 -c 'import json,sys; print(json.loads(sys.stdin.readline())["guest_sign_in_address"])')"
docker volume create "$tag-data" >/dev/null
docker run --rm --entrypoint /bin/sh -v "$tag-data:/data" "$test_image" \
  -c "printf '%s' '{\"retention_days\": 30, \"log_level\": \"info\", \"guest_sign_in_address\": \"$host\"}' > /data/options.json"
docker run -d --name "$tag-app" --cap-add NET_ADMIN --security-opt seccomp=unconfined \
  -e TZ=UTC -v "$tag-data:/data" "$test_image" >/dev/null
for _ in $(seq 1 60); do
  if docker exec "$tag-app" test -f /run/casa-browser/netfloor.ok -a -f /run/casa-browser/ca/ca.pem; then
    break
  fi
  sleep 1
done

status=0
printf '%s\n' "$secrets" | docker exec -i -w /opt/casa_browser_tests -e LIVE_DEBUG="${LIVE_DEBUG:-0}" \
  "$tag-app" /opt/venv/bin/python tests/integration/livepasskey.py || status=$?

# The container's own log (every s6 service) holds none of the values.
{ printf '%s\n' "$secrets"; docker logs "$tag-app" 2>&1; } | python3 -c '
import json, sys
values = [v for k, v in json.loads(sys.stdin.readline()).items()
          if k not in ("vault", "guest_sign_in_address", "item_id", "password_item_id") and v]
log = sys.stdin.read()
leaked = any(v in log for v in values)
print("run-live-passkey: container log", "LEAKED A VALUE" if leaked else "clean")
sys.exit(1 if leaked else 0)' || status=1
exit "$status"
