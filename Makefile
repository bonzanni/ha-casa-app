PY := venv_test/bin/python

.PHONY: help setup test-unit test-unit-serial test-docker test-image lint
.DEFAULT_GOAL := help

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-13s %s\n", $$1, $$2}'

setup: ## One-time WSL dev setup (Linux venv + git hooks)
	./scripts/setup-dev.sh

# -n auto --maxprocesses=12 --dist loadfile: 12 workers take this from ~185s to ~25s, and
# file-scoped distribution keeps every test in a module on one worker, so the
# module-level state a few suites monkeypatch cannot straddle processes.
# Measured identical results over repeated runs; see test-unit-serial when a
# failure needs readable, interleaving-free output.
# CAGE := the documented systemd-run memory cage, applied automatically when
# available. RLIMIT_AS in conftest bounds one process's ADDRESS SPACE; only this
# bounds REAL memory across all workers, which is what actually killed the VM
# twice. Degrades to running uncaged where systemd-run is absent (CI images).
# Probe that the cage actually WORKS, not merely that the binary exists: in WSL,
# containers and non-login SSH sessions /usr/bin/systemd-run is present while the
# user bus is not, and prepending it there would make pytest never run at all —
# taking the binding pre-push gate down with it. Both reviewers made this their
# top finding, and it passes on this machine, which is exactly why it needed a
# real probe rather than my judgement.
CAGE := $(shell systemd-run --user --scope -q true >/dev/null 2>&1 && echo "systemd-run --user --scope -q -p MemoryMax=8G -p MemorySwapMax=2G")

# LOCK := mutual exclusion for the memory-hungry unit suite. Two 8G-caged runs
# do not fit on a 23G developer box, and callers cannot be trusted to
# coordinate: parallel tooling and external review processes invoke this target
# directly. Exclusion therefore lives HERE, in the entry point, not in callers —
# putting it in one caller leaves every other caller unsafe. systemd-run
# --scope is synchronous, so the lock is held for the scope's lifetime.
SUITE_LOCK := /tmp/casa-suite.lock
# UNCONDITIONAL on purpose. A probed lock that expands to empty when flock is
# missing degrades silently to concurrent unlocked suites -- the same failure
# shape as the cage below, which is why both now fail loudly instead.
LOCK := flock -w 1800 $(SUITE_LOCK)

# The cage DEGRADES to an uncaged 12-worker run wherever systemd-run cannot
# reach a user bus. On a 23G box that is the OOM that has killed this VM twice,
# so refuse by default. CI images legitimately have no user bus and opt out
# explicitly rather than degrading silently.
CAGE_GUARD = $(if $(CAGE),,$(if $(CASA_ALLOW_UNCAGED),,$(error \
  refusing to run the unit suite uncaged: systemd-run --user --scope is \
  unavailable, and 12 uncaged workers have OOM-killed this machine. Run from a \
  session with a user bus, or set CASA_ALLOW_UNCAGED=1 if you know the host \
  can take it (CI does this).)))

# SUITE := the ONLY way to start the unit suite. Every protection lives here
# so that adding a suite target cannot forget one -- test-unit-serial ran
# uncaged, unlocked and unguarded precisely because each target carried its own
# decoration.
SUITE = $(CAGE_GUARD)$(LOCK) $(CAGE)

# RESIDUAL, declared rather than hidden: three separate suite entries were found
# unprotected in successive review rounds (test-unit, test-unit-serial, and
# test-local's test-tier2). Decorating call sites CANNOT be proven complete --
# any new Makefile or a hand-typed `pytest tests/` bypasses it. `suite-run` gives
# every caller one protected entry to route through, and the complete fix is to
# move the lock and cage probe into the pytest session itself (tests/conftest.py),
# where no caller can miss them. Filed separately.
suite-run: ## Protected entry point for any caller that needs the unit suite
	$(SUITE) $(PY) -m pytest tests/ $(PYTEST_ARGS) --tb=short

test-unit: ## Fast unit tests, parallel + memory-caged (except docker/slow)
	$(SUITE) $(PY) -m pytest tests/ -m "not docker and not slow" -n auto --maxprocesses=12 --dist loadfile --tb=short

test-unit-serial: ## Same suite, one process (for debugging a failure)
	$(SUITE) $(PY) -m pytest tests/ -m "not docker and not slow" --tb=short

# Deliberately NOT under $(SUITE): the docker tier is single-process and
# docker-backed, so it neither competes for the 8G cage nor benefits from
# serialising against the 12-worker unit suite. Declared rather than skipped
# silently, so the exclusion is a decision on the record.
test-docker: ## Docker-backed unit tests
	$(PY) -m pytest tests/ -m "docker and not slow" --tb=short

test-image: ## Build the e2e test image (mirrors CI tier1/baseline)
	docker build -f test-local/Dockerfile.test -t casa-test .

lint: ## (no linter configured yet)
	@echo "No linter configured. CI gate is pytest tier2 (see make test-unit)."
