# Casa — contributor & AI-assistant guide

Casa is a **Home Assistant app** (formerly "add-on"): a fleet of Claude-powered agents
reachable over Telegram and a voice (SSE/WebSocket) channel, packaged as an HA app.
Python + `aiohttp`, built on the **Claude Agent SDK**, Hindsight memory, the **MCP**
protocol, and **APScheduler**, all **s6-overlay**–supervised inside the container.

## Where things are
- **Application code:** `casa/rootfs/opt/casa/` (~45 modules — this is the deep
  HA-rootfs path; the add-on copies `rootfs/` into the image root).
- **App manifest:** `casa/config.yaml` (version lives here). User-facing app
  docs: `casa/DOCS.md`. App changelog: `casa/CHANGELOG.md`.
- **Tests:** `tests/` (173 files). Container/e2e harness: `test-local/`. CI: `.github/workflows/qa.yml`.
- **Agent-facing docs corpus:** `docs/` — the canonical, CI-enforced current-state
  documentation. **Before changing a subsystem, read the document that
  `docs/README.md`'s routing table names for it.** See the boundary note below.

## Build & test
Run once on a fresh checkout:
```bash
make setup        # builds a WSL/Linux venv at venv_test/ + installs the git hooks
```
Then:
```bash
make test-unit    # fast unit tests, PARALLEL + memory-caged (~25s for ~7700)
make test-unit-serial  # same suite, one process — for debugging a failure
make test-docker  # docker-backed unit tests
```
`test-unit` runs `-n auto --maxprocesses=12 --dist loadfile`; file-scoped
distribution keeps a module's tests on one worker, which is what the suites that
monkeypatch module-level state require. Iterate on targeted files
(`venv_test/bin/pytest tests/test_x.py`) and save the full suite for before the
gate — at ~25s it is cheap, but not free, and it is not a substitute for
thinking about which tests your change can break.
CI runs four tiers (tier1-smoke, tier2-functional, baseline-runtime, tier3-hardening);
tier2 is the unit gate. The gate is **opt-out** (v0.64.2): unmarked tests run by
default; mark `docker` or `slow` to exclude (`unit` is legacy/optional). Markers
in `pytest.ini`.
The `tests/conftest.py` auto-adds the code root to `sys.path`.

> **⚠️ Memory cage for pytest (2026-07-11, leak FIXED in v0.66.0 — cap stays):**
> a ~23 GB pytest blow-up OOM-killed the entire WSL VM twice. Root cause:
> `patch("retry.asyncio.sleep", …)` patches the **global** `asyncio.sleep`, which
> made the SDK-pool sweeper's `while True: await asyncio.sleep(...)` spin at CPU
> speed under an AsyncMock (unbounded `call_args_list`). Fixed by scoping those
> patches to retry's module-local `asyncio` (see `patch_retry_sleep` in
> `tests/test_agent_process.py`). Two standing rules: (1) **never patch
> `<module>.asyncio.sleep`** — it is the shared module attribute, not a local;
> (2) keep running pytest under the hard cap as belt-and-suspenders, since a cap
> kills only the runaway pytest, not the VM. **`make test-unit` now applies that
> cage automatically** — it probes that `systemd-run --user --scope` actually
> works (in a container or a non-login session the binary exists but the user bus
> does not) and degrades to an uncaged run rather than failing. Invoking pytest
> directly still needs it by hand:
> `systemd-run --user --scope -p MemoryMax=8G -p MemorySwapMax=2G venv_test/bin/pytest …`
> Note the cage matters MORE under parallelism, not less: `RLIMIT_AS` in
> `conftest.py` is per process, so only the cgroup bounds the workers in
> aggregate. That limit also bounds ADDRESS SPACE, not resident memory — CPython
> reserves far more VA than it touches, which is why the per-worker floor is
> 6 GiB and why 2 GiB broke the suite outright.
>
> **Not every WSL freeze is this bug.** Establish first whether an OOM kill
> happened at all, using the current boot: `journalctl -k -b | grep -i "killed
> process"` for an entry naming pytest, plus `/proc/vmstat`'s `oom_kill`. That
> counter totals every OOM kill of the running kernel, so a non-zero value can
> belong to an unrelated earlier one, and a zero value says nothing about
> previous boots — correlate the journal entry's timestamp and process name
> before concluding. If neither shows a kernel OOM kill, the freeze was not
> caused by one, and the cage did not trigger one; the cage bounds a single
> pytest process tree, not the machine. Check `/proc/pressure/memory` for memory
> stalls: a rising `full` total measures time in which every non-idle task was
> stalled on memory, but does not identify the source of the pressure. Do not
> reach for the suite as the default suspect: in one 2026-08-02 measurement,
> sampled once per second during a 21 s run, the lowest sampled `MemAvailable`
> was 19.15 GiB, the lowest sampled `SwapFree` was 8.00 GiB, and the largest
> sampled `/tmp` usage was 85.6 MB.

## Release flow
1. Branch `feat/vX.Y.Z-<desc>` off `main`.
2. Bump `version:` in `casa/config.yaml` and prepend a `casa/CHANGELOG.md` entry.
3. Commit `release: vX.Y.Z (<summary>)`, push, open a PR, **squash-merge** once CI is green.
4. Merging to main auto-publishes the GHCR images and creates the `vX.Y.Z` tag +
   GitHub Release from the changelog entry (`deploy.yml`) — no manual tagging.
- **Removing an app option?** The stored value stays behind and draws a boot
  WARN from HA until the stored options are cleaned by hand (Casa ignores
  unknown keys). Pre-1.0 that is accepted; clean the one prod install manually.

## Environment (WSL)
- Develop on **WSL2 on the native ext4 filesystem** (not `/mnt/c`) — needed for perf and
  correct exec bits.
- **Container-bound files must be LF.** `.gitattributes` enforces `eol=lf` on `*.sh`,
  `Dockerfile`, and all of `casa/rootfs/**` — CRLF breaks shebangs and s6. Don't
  fight it; new `rootfs/**` or `*.sh` files must be LF.
- `ls` may show `-rwxr-xr-x` on plain files — that's a WSL mount display artifact; git
  tracks `100644`. Don't "fix" it or commit mode changes.
- `venv_test/` must be a **Linux venv** (run `make setup`); any pre-existing Windows-layout
  venv (`Scripts/`, `Activate.ps1`) won't run under WSL.

## The publication boundary — READ THIS
**Internal engineering material is never committed here.** Design specs, plans, roadmaps,
reviews and captured transcripts belong outside this repository. The rule for what may go
in is one line: *a fact belongs in this repo only if it is verifiable from the public
commit alone* — no operator, no production box, no private repository.

`docs/` holds the public, curated, agent-facing corpus — **anything committed there is
published.** It is canonical and CI-enforced: every anchor must resolve against tracked
code, every declared invariant must carry a tracked test binding (the red-case discipline
in `docs/contributing/doc-contract.md` is what makes it a genuine pinning test), and a
code-derived coverage ledger (`docs/coverage.yaml`) fails the build when a substantial
module (≥100 lines), an option, a tool, a route or an s6 unit is neither assigned to a
document nor excluded with a reason. Consult `docs/README.md`'s routing table before
changing a subsystem, and update the corpus in the same change — the verifier
(`python -m scripts.verify_docs .`) and `scripts/coverage_ledger.py check .` must stay
green.

Three controls enforce this, and `make setup` installs the hooks:

- `.githooks/pre-commit` refuses staged paths and added lines matching
  `.githooks/deny-patterns.txt` — generic rules only, since this repo is public and a
  pattern written here is published by the commit that adds it.
- `scripts/gate.sh` is the pre-push gate: it evaluates `HEAD` on a clean tree, sweeps the
  endpoint tree, every unpublished commit and their messages, then runs a pinned secret
  scanner over both tree and history.
- `.githooks/pre-push` refuses a push whose commits touch a gated path without an
  attestation from `scripts/attest.sh`. **The first push of anything is irreversible** —
  objects stay fetchable by SHA after any branch deletion or force-push.

Contributors with additional local tooling supply an exact-literal supplement through
`CASA_DENY_SUPPLEMENT`; CI runs the generic rules and the scanner.

## Working norms
- **Verify against whole files, not thin grep slices** — read around a symbol before
  asserting behaviour.
- Don't commit or push unless asked; if on `main`, branch first.

## Repo hygiene & publishing norms
This public repo is the storefront for the Casa app (HA renamed "add-ons" → "apps"
mid-2026). Keep it publish-ready at all times:
- **Green main.** Ship-fast doesn't wait for CI, but after pushing check the previous
  QA run (`gh run list --workflow qa.yml --branch main --limit 1`); a red main is
  stop-the-line before the next release — the e2e tiers cover what the local unit gate
  can't (see the v0.52–v0.57 red streak).
- **One check is NOT covered by "don't wait for CI": `docs.yml` → "Corpus + publication
  guard".** It finishes in well under a minute and it is the only thing that catches
  documentation drift, so **wait for it and merge only when it is green**:
  `gh pr checks <pr> --watch --required` or, at minimum,
  `gh run list --workflow docs.yml --branch <branch> --limit 1`. It fired on v0.145.0
  (PR #383), naming six documents that needed auditing — five of which did turn out to
  need prose changes; that PR was squash-merged with `--admin` about a minute after
  opening, before the guard reported, and the drift shipped. The guard detects
  *potential* impact, not proven staleness: the claim is file-level, so a change can
  easily miss what a document actually quotes. When a named document is genuinely still
  accurate, acknowledge it per document rather than making a cosmetic edit —
  `Docs-impact: architecture/tools-interface.md — none (claimed symbols unchanged)` —
  **in the tip commit's message**, at column zero. A later commit voids it, exactly as
  it voids a `scripts/attest.sh` receipt: the acknowledgement is a statement about the
  diff as it finally stands. **Carry the line into the squash-merge message** — a
  waiver that only ever existed on a deleted branch is not the audit trail it claims to
  be. (The repo's squash default is `COMMIT_MESSAGES`, which carries it automatically;
  passing a hand-built `--body` to `gh pr merge` overrides that, so if you do, include
  the line.) **Put waiver lines in the TIP commit and nowhere else** — the squash
  concatenates every commit's message, so a line in an intermediate commit reaches
  `main` waiving a diff it never saw, and the gate refuses one (#685). **Every tip
  carries a line**, `Docs-impact: none — <reason>` when nothing is affected; the gate
  refuses a tip with none. And the claim is now checked against the diff: waiving a
  document the same change edits, or one the change does not impact, or waiving one
  twice, is refused. Since 2026-08-02 `main` is PROTECTED: this guard is a required check with
  admin enforcement and strict mode, merges must go through a pull request, and force
  pushes and branch deletion are blocked. Disabling protection to get out of a jam is a
  break-glass action — say so when you do it. The gate cannot check that a waiver is sincere, and deliberately does not try;
  it exists so that no batch merges without someone naming each impacted document and
  saying why, on the record.
- **Branches die on merge.** GitHub auto-deletes the remote head; delete the local
  branch too. No stray branches on origin.
- **Every release**: bump `casa/config.yaml` version + a user-facing CHANGELOG
  entry (keepachangelog tone; deep engineering detail belongs in the PR body) + a
  `translations/en.yaml` entry for any new/changed option + DOCS.md accuracy.
- **Nothing internal on any pushed ref**: no design specs, plans, reviews, audit or
  diagnosis ledgers, no `.claude/`. Internal artifacts live in a private repository
  outside this checkout; the public `docs/` corpus carries only what passes the
  publication rule. Force-pushed-away commits stay fetchable by SHA on GitHub —
  prevention is the only cure.
- **Public identity**: commits AND public-facing files (repository.yaml maintainer,
  README) use `3899230+bonzanni@users.noreply.github.com`, never a personal address.
- **Clean tree between sessions**: commit or revert stragglers. New files have a home
  (eval scripts → `test-local/eval/`, e2e → `test-local/e2e/`, internal specs → the
  private repository outside this checkout); nothing parked at the repo root.
- **AI attribution**: commits end with `Assisted-by: Claude Code` (kernel/Fedora-style
  disclosure — not `Co-Authored-By`, which implies authorship and suffers GitHub
  email-squatting); PR bodies carry no vendor footer. Configured in
  `.claude/settings.json` (`attribution`); the README's "AI-assisted development" note
  is the canonical disclosure. Never strip or rewrite historical trailers.
