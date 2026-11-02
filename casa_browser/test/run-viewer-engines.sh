#!/usr/bin/env bash
# The live view's viewer in WebKit and Chromium with touch (make test-browser-viewer-engines):
# casa_browser/test/viewer_engines.py in Playwright's own Python image, which carries both
# engines' builds; its Python package, pinned to the same version, is installed first.
set -euo pipefail
cd "$(dirname "$0")/.."
image="mcr.microsoft.com/playwright/python:v1.64.0-noble"
docker run --rm --ipc=host -v "$PWD:/app:ro" -w /app "$image" sh -c \
  'pip install -q --root-user-action=ignore --break-system-packages --disable-pip-version-check playwright==1.64.0 \
   && python3 test/viewer_engines.py'
