#!/usr/bin/env bash
# Casa Browser integration lane (make test-browser-integration [PYTEST_ARGS='…']).
#
# Builds the app image and the test image, starts the app container with the image's own
# s6 /init on a user-defined Docker network (Docker's embedded resolver, as under Home
# Assistant) with NET_ADMIN only, plus a private target container that is genuinely
# reachable on that network, and runs the `docker`-marked tests inside the app container.
# Fixtures are synthetic. Exits nonzero on any failure, on a run that collects nothing and on
# any skipped test (every image probe is mandatory); pytest prints the counts.
#
# CASA_IT_STORE says where the app in the container keeps site logins:
#   onepassword (the default)  its options hold a made-up token, so the app is on the 1Password
#                              path this lane has always tested (the forced extension and its
#                              hosts); tests/integration/test_builtin_mode.py is left out.
#   builtin                    no token, and a random key for the app's own store set on the
#                              container; without selectors only test_builtin_mode.py runs.
set -euo pipefail
cd "$(dirname "$0")/.."

store="${CASA_IT_STORE:-onepassword}"
case "$store" in
  onepassword) token_option=', "onepassword_service_account_token": "made-up"' ;;
  builtin) token_option='' ;;
  *) echo "run-integration: CASA_IT_STORE is onepassword or builtin" >&2; exit 2 ;;
esac

# CASA_IT_TAG names the image pair, so two checkouts can run the lane side by side.
app_image="casa-browser:${CASA_IT_TAG:-it}-app"
test_image="casa-browser:${CASA_IT_TAG:-it}-test"
tag="casa-browser-it-$$"

cleanup() {
  docker rm -f "$tag-app" "$tag-target" >/dev/null 2>&1 || true
  docker network rm "$tag" >/dev/null 2>&1 || true
  docker volume rm "$tag-data" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build ${DOCKER_PULL:-} -t "$app_image" .
docker build -t "$test_image" -f test/Dockerfile --build-arg APP_IMAGE="$app_image" .

docker network create "$tag" >/dev/null
docker run -d --name "$tag-target" --network "$tag" --entrypoint /opt/venv/bin/python \
  "$test_image" -m http.server 8080 --bind 0.0.0.0 >/dev/null
target_ip="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$tag-target")"

# /data is a volume: the app chowns its contents to its service users. Under either store the
# options name the same made-up team address as the 1Password guest address: the proxy's
# broker listener admits it when a token is set and refuses it when none is.
docker volume create "$tag-data" >/dev/null
printf '%s\n' '{"retention_days": 30, "log_level": "info", "guest_sign_in_address": "casa-it.1password.eu"'"$token_option"'}' \
  | docker run --rm -i --entrypoint /bin/sh -v "$tag-data:/data" "$test_image" \
      -c 'cat > /data/options.json'
# As Supervisor runs the app: NET_ADMIN only, and seccomp unconfined (measured on Home
# Assistant OS; Chrome's namespace sandbox needs user namespaces, which Docker's default
# seccomp profile refuses — Chrome then refuses to start, since its setuid helper is disarmed).
# SUPERVISOR_TOKEN is a canary here (Supervisor sets the real one): root bootstrap must
# remove it before any service starts, and the boundary tests look for it everywhere.
supervisor_canary="it-supervisor-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
# The key of the app's own store, set on the container as an operator sets it. Docker takes
# its value from the environment of this one command, so it is on no command line, and it is
# never printed.
store_key=""
store_env=()
if [ "$store" = builtin ]; then
  store_key="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
  store_env=(-e CASA_BROWSER_VAULT_KEY)
fi
CASA_BROWSER_VAULT_KEY="$store_key" docker run -d --name "$tag-app" --network "$tag" \
  --cap-add NET_ADMIN --security-opt seccomp=unconfined -e TZ=UTC \
  -e SUPERVISOR_TOKEN="$supervisor_canary" ${store_env[@]+"${store_env[@]}"} \
  -v "$tag-data:/data" "$test_image" >/dev/null

# Readiness: the floor marker and the proxy's exported CA (written once it listens).
for _ in $(seq 1 60); do
  if docker exec "$tag-app" test -f /run/casa-browser/netfloor.ok -a -f /run/casa-browser/ca/ca.pem; then
    break
  fi
  sleep 1
done
docker exec "$tag-app" test -f /run/casa-browser/ca/ca.pem || {
  echo "run-integration: the app's services did not come up" >&2
  docker logs "$tag-app" 2>&1 | tail -40 >&2
  exit 1
}

# Selectors arrive relative to the repository (casa_browser/tests/...); the container's tree
# is rooted at casa_browser/.
selectors=()
for arg in "$@"; do selectors+=("${arg#casa_browser/}"); done
if [ "$store" = builtin ]; then
  [ "${#selectors[@]}" -gt 0 ] || selectors=(tests/integration/test_builtin_mode.py)
else
  [ "${#selectors[@]}" -gt 0 ] || selectors=(tests/integration)
  # That file is about a container started without a token.
  selectors+=(--ignore=tests/integration/test_builtin_mode.py)
fi

docker exec -w /opt/casa_browser_tests -e PYTHONPATH=/opt -e CASA_IT_NO_SKIP=1 \
  -e CASA_IT_PRIVATE_TARGET="$target_ip:8080" -e CASA_IT_SUPERVISOR_CANARY="$supervisor_canary" "$tag-app" \
  /opt/venv/bin/python -m pytest -m docker -p no:cacheprovider -rA "${selectors[@]}"

# The app container's own log (every s6 service's output, as Supervisor collects it) holds no
# test secret — every canary the tests plant in a page or a request starts with "itsecret" —
# and not the Supervisor token or the store's key. A canary planted on that same log first
# must be found: the positive control that this scan reads it.
planted="itsecret$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
docker exec "$tag-app" sh -c "echo 'log scan control $planted' > /proc/1/fd/1"
sleep 1
container_log="$(docker logs "$tag-app" 2>&1)"
grep -q -- "$planted" <<<"$container_log" || {
  echo "run-integration: the planted log canary is missing: the container log scan is blind" >&2
  exit 1
}
leaked="$(grep -oE 'itsecret[0-9a-f]{32}' <<<"$container_log" | grep -vx -- "$planted" | sort -u || true)"
if [ -n "$leaked" ] || grep -q -- "$supervisor_canary" <<<"$container_log"; then
  echo "run-integration: a test secret or the Supervisor token reached the container log" >&2
  exit 1
fi
# The key is given to grep as a pattern file, not as an argument.
if [ -n "$store_key" ] && grep -qF -f <(printf '%s\n' "$store_key") <<<"$container_log"; then
  echo "run-integration: the store's key reached the container log" >&2
  exit 1
fi
echo "run-integration: container log scan clean (planted canary found)"
