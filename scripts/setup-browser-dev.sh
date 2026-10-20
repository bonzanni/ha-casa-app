#!/usr/bin/env bash
# Developer and CI setup for the optional Casa Browser app (casa_browser/): builds
# venv_browser/ with Python 3.13 and installs the app's pinned runtime and test
# dependencies into it. Separate from venv_test/ on purpose — the two apps run on
# different Python versions and must not share an environment. Idempotent.
#
#   scripts/setup-browser-dev.sh
#
# BROWSER_PYTHON names the interpreter (CI passes setup-python's path); otherwise
# python3.13 from PATH, then uv's managed CPython 3.13. Until casa_browser/ exists in
# this tree there is nothing to set up, and the script says so and exits 0.
set -euo pipefail
cd "$(dirname "$0")/.."

app=casa_browser
if [ ! -f "$app/config.yaml" ]; then
  echo "setup-browser-dev: no $app/ app in this tree — nothing to set up"
  exit 0
fi

py="${BROWSER_PYTHON:-}"
if [ -z "$py" ] && command -v python3.13 >/dev/null 2>&1; then
  py="$(command -v python3.13)"
fi
if [ -z "$py" ] && command -v uv >/dev/null 2>&1; then
  py="$(uv python find 3.13 2>/dev/null || true)"
fi
if [ -z "$py" ] || ! "$py" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 13))'; then
  echo "setup-browser-dev: no Python 3.13 found (set BROWSER_PYTHON, or: uv python install 3.13)" >&2
  exit 1
fi

echo "==> venv_browser/ with $("$py" --version)"
if [ ! -x venv_browser/bin/python ] \
   || ! venv_browser/bin/python -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 13))'; then
  rm -rf venv_browser
  "$py" -m venv venv_browser
fi
venv_browser/bin/python -m pip install --quiet --upgrade pip
venv_browser/bin/python -m pip install --quiet \
  -r "$app/requirements.txt" -r "$app/requirements-test.txt"

echo "==> installed versions"
venv_browser/bin/python --version
venv_browser/bin/python -m pip freeze
