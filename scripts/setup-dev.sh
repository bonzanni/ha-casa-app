#!/usr/bin/env bash
# One-time developer setup for Casa, for WSL2 / Linux. Idempotent — safe to re-run.
#
#   ./scripts/setup-dev.sh   (or: make setup)
#
# 1. Installs the shared git hooks (the publication leak guard and push gate).
# 2. Builds a Linux test venv at venv_test/ (replacing any stale Windows-layout venv);
#    falls back to uv's managed CPython 3.12 when system python3 can't make a venv.
# 3. Installs runtime + test dependencies into it.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> 1/3 git hooks (publication leak guard + push gate)"
git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true

echo "==> 2/3 Linux test venv at venv_test/"
# A venv created on Windows has Scripts/ + Activate.ps1 and no bin/python — unusable on WSL.
if [ ! -x venv_test/bin/python ]; then
  rm -rf venv_test
  python3 -m venv venv_test >/dev/null 2>&1 || true
fi
# No usable pip (system python lacks ensurepip/python3-venv, or venv creation failed
# partway): rebuild with uv's managed CPython 3.12, which seeds pip.
if ! venv_test/bin/python -m pip --version >/dev/null 2>&1 \
   && command -v uv >/dev/null 2>&1; then
  echo "    python3 venv unusable — rebuilding with uv (CPython 3.12 + pip)"
  rm -rf venv_test
  uv venv --seed --python 3.12 venv_test
fi
if ! venv_test/bin/python -m pip --version >/dev/null 2>&1; then
  echo "ERROR: could not build a venv with pip." >&2
  echo "       Install uv (https://docs.astral.sh/uv/), or:  sudo apt install python3-venv python3-pip" >&2
  exit 1
fi

echo "==> 3/3 dependencies"
venv_test/bin/python -m pip install --quiet --upgrade pip
venv_test/bin/python -m pip install --quiet -r casa/requirements.txt -r casa/requirements-test.txt

cat <<'DONE'

Setup complete.
  make test-unit     # fast unit tests
  make test-docker   # docker-backed unit tests
DONE
