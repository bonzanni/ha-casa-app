# Run from the repo root: `make -C test-local <target>`
SHELL := /usr/bin/env bash
IMAGE ?= casa-test
ROOT  := $(shell git rev-parse --show-toplevel)

# DOCKER_PULL (#942): Dockerfile.test's base is a floating tag, so a warm local
# image store decides which base a tier certifies unless the tag is resolved.
# `--pull` resolves it. It is here and not in e2e/common.sh's build_image
# because CI never invokes make (README.md "CI does NOT invoke `make`"), so
# this flag's failure mode is strictly local -- it cannot red the push gate.
# A pull FAILS the build when the base cannot be resolved from a registry, even
# with the image in the store, so the flag is overridable: `make build
# DOCKER_PULL=` builds from cache, offline, exactly as before.
DOCKER_PULL ?= --pull

.PHONY: build test test-smoke test-runtime test-voice \
        test-tier1 test-tier2 test-tier3 test-all clean help

help:
	@echo "Tiered targets (preferred):"
	@echo "  test-tier1      Tier 1 smoke (~3 min): smoke + voice"
	@echo "  test-tier2      Tier 2 functional (~10 min): unit pytest + invoke + memory + delegation + scheduling + engagement E-block"
	@echo "  test-tier3      Tier 3 hardening (nightly): slow pytest + concurrency + MCP restart"
	@echo "  test-all        tier1 -> tier2 -> tier3 sequentially"
	@echo ""
	@echo "Legacy targets:"
	@echo "  build           Build the test image"
	@echo "  test            Smoke + runtime + voice (default)"
	@echo "  test-smoke      Image builds + /healthz + dashboard"
	@echo "  test-runtime    /invoke session isolation + concurrency"
	@echo "  test-voice      Voice pipeline SSE + WS smoke scenarios"
	@echo "  clean           Remove dangling test containers"
	@echo ""
	@echo "Every build refreshes the floating base (#942). Offline, or during a"
	@echo "registry outage, build from the local store with DOCKER_PULL= :"
	@echo "  make test-tier1 DOCKER_PULL="

build:
	cd $(ROOT) && docker build $(DOCKER_PULL) -f test-local/Dockerfile.test -t $(IMAGE) .

test-smoke: build
	cd $(ROOT) && bash test-local/e2e/test_smoke.sh

test-runtime: build
	cd $(ROOT) && bash test-local/e2e/test_invoke_sessions.sh
	cd $(ROOT) && bash test-local/e2e/test_concurrency.sh

test-voice: build
	cd $(ROOT) && bash test-local/e2e/test_voice_sse.sh
	cd $(ROOT) && bash test-local/e2e/test_voice_ws.sh

test: test-smoke test-runtime test-voice

test-tier1: build
	cd $(ROOT) && bash test-local/e2e/test_smoke.sh
	cd $(ROOT) && bash test-local/e2e/test_voice_sse.sh
	cd $(ROOT) && bash test-local/e2e/test_voice_ws.sh

test-tier2: test-tier1
	cd $(ROOT) && $(MAKE) suite-run PYTEST_ARGS='-m "unit and not docker and not slow"'
	cd $(ROOT) && bash test-local/e2e/test_invoke_sessions.sh
	cd $(ROOT) && bash test-local/e2e/test_external_surface.sh
	cd $(ROOT) && bash test-local/e2e/test_specialist_delegation.sh
	cd $(ROOT) && bash test-local/e2e/test_scheduling.sh
	cd $(ROOT) && bash test-local/e2e/test_engagement_E.sh

# `build` is a prerequisite here for the same reason it is on tier1 (#942):
# without it tier3's image came only from build_image_with_mock_cli, which
# resolves nothing -- so the one tier whose two wasted runs were measured in
# #942 was also the only tier that never refreshed its base.
test-tier3: build
	cd $(ROOT) && $(MAKE) suite-run PYTEST_ARGS='-m "slow"' || true
	cd $(ROOT) && bash test-local/e2e/test_concurrency.sh
	cd $(ROOT) && CASA_USE_MOCK_CLAUDE=1 bash test-local/e2e/test_mcp_restart_survival.sh

test-all: test-tier1 test-tier2 test-tier3

clean:
	-docker ps -a --filter 'name=casa-' --format '{{.ID}}' | xargs -r docker rm -f
