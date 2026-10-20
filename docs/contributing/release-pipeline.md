---
last_reviewed: 2026-10-20
---

# How apps are built, tested and released

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How the repository's app folders become published images, tags and GitHub Releases, and
which test lane runs for which app. It covers `.github/workflows/deploy.yml`,
`.github/workflows/deploy-app.yml`, `scripts/browser-release-matrix.py`, the per-app
lint and unit lanes, and the `make` targets that run them. What a document must say about
an app's code is [`doc-contract.md`](doc-contract.md); what may be published at all is
[`../doctrine/publishing.md`](../doctrine/publishing.md).

## Mental model

An **app** is a top-level folder with a `config.yaml`. There are two app names: `casa`, and
`casa_browser`, the optional Casa Browser app, which builds, tests and publishes nothing
until its folder exists. `scripts/browser-release-matrix.py` holds the list (`APPS`), and
every other per-app list in the repository is pinned to it by a test.

**Selection.** On every push to `main` and every pull request that touches an app folder or
the shared release pipeline (`SHARED_PATHS`: the two deploy workflows and the selection
script), `deploy.yml` asks the script which apps the change touches. A change inside an
app's folder selects that app; a change to the shared pipeline selects every app, so each
is at least validated; a base the script cannot diff (a push whose `before` is all zeros)
counts as a shared change. An app whose folder has no `config.yaml` at the head commit is
never selected.

**One invocation per app.** `deploy.yml` runs `deploy-app.yml` once per selected app, as a
matrix. That reusable workflow reads the app's own manifest and produces that app's
architecture matrix, image name, version, tag and release notes; nothing one app computes
is visible to the other's invocation.

**Publication.** A pull request builds every selected app's images and publishes nothing. A
push to `main` publishes an app — per-arch images, Cosign-signed by the Home Assistant
builder actions, the multi-arch manifest, then the tag and GitHub Release — only when that
app's release tag does not exist yet. When it already exists, the push builds to validate
and publishes nothing, so a released version's image and tag are never overwritten. The
release job tags the exact commit the run built, and takes its notes from the matching
section of the app's changelog (`casa/CHANGELOG.md` for Casa).

| | Casa | Casa Browser |
|---|---|---|
| Folder, manifest | `casa/`, `casa/config.yaml` | `casa_browser/`, `casa_browser/config.yaml` |
| Release tag | `vX.Y.Z` | `casa_browser-vX.Y.Z` |
| GitHub "Latest" release | decided by GitHub, as for any release | never (`--latest=false`) |
| Builder cache scope | the builder's per-arch default | its own (`casa_browser`) |

**Concurrency.** One group per app and version on `main`, so a second run for the same
version waits rather than racing it, and different versions never replace each other's
pending run. A release in progress is never cancelled; a pull request's superseded
validation build is.

**Test lanes.** Each app runs its own unit lane in its own environment. Casa's is
`tier2-functional` in `.github/workflows/qa.yml` (Python 3.11, `venv_test/` locally). The
browser app's is the `browser-unit` job (Python 3.13, `venv_browser/` from
`scripts/setup-browser-dev.sh`), with the same opt-out selection — everything except
`docker` and `slow`. `make test-unit` and `make test-unit-serial` run Casa's suite and then
`make test-browser`, as two separate invocations under the suite lock and memory cage, so
the lock is never taken while held. `make test-browser-integration` runs the browser app's
integration tests in its built test image. The app linter in `.github/workflows/lint.yml`
runs once per app. Until `casa_browser/` exists, every browser lane and target states that
there is nothing to run and succeeds; once it exists, a lane that collects no test fails.

## Failure behavior

**What "already released" means.** Only the release tag is consulted. A run that pushed
images and then failed before tagging leaves the version untagged, and the next push or a
re-run of that version publishes it again — the same images are rebuilt and re-pushed,
then tagged. A failure to ask the remote whether the tag exists stops the run rather than
being read as "not released".

**What selection does not cover.** Selection is by path. A change outside an app folder
and outside `SHARED_PATHS` — a test, a document, another workflow — selects no app and
builds nothing. The version-only exemption in `scripts/docs_impact.sh` is judged per app
manifest: a manifest whose diff is only its `version:` line impacts no document, and any
other line in it, including a deleted option, does.

**Signing identity.** Images are signed keylessly by the job that builds them, which is a
job of `deploy-app.yml`; a verifier that pins the signing workflow's path must name that
file.

## Extension points

Adding an app is an entry in the script's `APPS` (its tag prefix, Latest flag and cache
scope) and, in the same change, the app's roots in `scripts/coverage_ledger.py`'s `APPS`
— where its non-path keys take its folder as a namespace, `option:<app>:<key>` (see
[`doc-contract.md`](doc-contract.md)) — its manifest in the version-only loop of
`scripts/docs_impact.sh`, a leg in the lint matrix, and its folder in both trigger path
lists of `deploy.yml`. Tests in `tests/test_browser_release_matrix.py` fail while any of
those lists disagrees with the script's.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `scripts/browser-release-matrix.py::select_apps`
- `scripts/browser-release-matrix.py::release_settings`
- `.github/workflows/deploy.yml`
- `.github/workflows/deploy-app.yml`
- `scripts/setup-browser-dev.sh`

**Tests**
- `tests/test_browser_release_matrix.py`

**Related**
- [`contributing/doc-contract.md`](../contributing/doc-contract.md)
- [`doctrine/publishing.md`](../doctrine/publishing.md)
<!-- END SOURCEMAP -->
