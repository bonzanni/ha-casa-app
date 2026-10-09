"""Git-backed history for ``/config/``.

Local-only repo: no remote, no push. The builder agent in Spec Y uses
``commit_config`` on every write and ``restore_file`` to roll back.
Casa boot uses ``init_repo`` (idempotent) and ``snapshot_manual_edits``
(records uncommitted human edits before the builder can trip over them).

Wraps the ``git`` CLI via :mod:`subprocess` — keeps the dependency
footprint zero (no libgit2, no dulwich).
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
from typing import Callable, Sequence

logger = logging.getLogger(__name__)

# #351: serialize EVERY in-process git mutation in this module (checked
# commits, plain commits, boot snapshots, rollbacks) — Terra/Sol v0.142.0
# r6: the shared-index read-decide-write sequences are only safe against
# writers that share this lock. A threading (not asyncio) lock: every
# caller runs in a worker thread.
#
# Cross-PROCESS writers (an SSH operator's own git commands) cannot share
# it; there the guarantee is bounded deliberately: each individual git
# command serializes on git's index.lock, and the residual lost-update
# window between two adjacent commands here is the same semantics plain
# git gives any two concurrent processes — stock git has no per-entry
# compare-and-swap (verified: `read-tree -m` refuses whenever worktree
# differs from index, i.e. always in the gate's success state).
_COMMIT_LOCK = threading.Lock()


_GITIGNORE_CONTENT = """\
# Casa config repo — track configs only.
*
!agents/
!agents/**
!policies/
!policies/**
!bindings/
!bindings/**
!schema/
!schema/**
# Unified plugin architecture (v0.71.0): the registry is config — the single
# plugin-assignment authority — and versioning it gives an audit trail.
# ONLY registry.json: the artifact store and staging under plugins/ are
# content-addressed binaries, never tracked.
!plugins/
!plugins/registry.json
plugins/store/
plugins/.staging/
# Nothing under specialists/ is tracked (#1097): an instance tuple can hold
# legacy plaintext the boot scrub has not yet removed, or a value a later
# upgrade reclassifies secret, and history is irreversible. An install,
# upgrade or rollback commit records agents/specialists/<slug>/ and
# plugins/registry.json instead.
!.gitignore
"""


# #278: single source of truth for the human-readable tracked-path summary
# used by config_git_commit's tool description and its no-op warning. Must
# stay in step with _GITIGNORE_CONTENT above — the pinning tests parse the
# whitelist and measure the admitted set through real git (the pre-fix
# strings had drifted: bindings/ (v0.100.0) was missing, sending agents that
# wrote there hunting for a nonexistent gitignore rule when they got an empty
# SHA; #1097: the specialists/ tuples it once named were never admitted).
TRACKED_PATHS_SUMMARY = (
    "agents/, policies/, bindings/, schema/, and plugins/registry.json"
)


def _run(cwd: str, args: Sequence[str], *, check: bool = True,
         env: dict | None = None) -> str:
    """Run ``git`` under *cwd*. Returns stripped stdout."""
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=check,
        capture_output=True, text=True, env=env,
    )
    return completed.stdout.strip()


def init_repo(config_dir: str) -> None:
    """Initialize *config_dir* as a local git repo if not already one.

    Idempotent: on an already-initialized repo the only action is the
    ``.gitignore`` reconcile below. Writes ``.gitignore`` to restrict
    tracking to ``TRACKED_PATHS_SUMMARY``'s set. Makes one initial commit so
    ``HEAD`` resolves.
    """
    gitignore = os.path.join(config_dir, ".gitignore")

    if os.path.isdir(os.path.join(config_dir, ".git")):
        # P-3 (v0.69.1): existing deployments carry the whitelist their repo
        # was initialized with — reconcile .gitignore on every boot so
        # whitelist changes (e.g. marketplace.json) reach them without a
        # fresh install. snapshot_manual_edits runs right after and commits
        # any newly-tracked files as the boot snapshot.
        try:
            with open(gitignore, "r", encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            current = ""
        if current != _GITIGNORE_CONTENT:
            logger.info("Refreshing config-repo .gitignore whitelist")
            with open(gitignore, "w", encoding="utf-8") as fh:
                fh.write(_GITIGNORE_CONTENT)
            _run(config_dir, ["add", ".gitignore"], check=False)
            _run(config_dir, ["commit", "-qm",
                              "update .gitignore whitelist"], check=False)
        return

    logger.info("Initializing config git repo at %s", config_dir)
    _run(config_dir, ["init", "-q"])
    _run(config_dir, ["config", "user.email", "casa@local"])
    _run(config_dir, ["config", "user.name",  "Casa"])

    with open(gitignore, "w", encoding="utf-8") as fh:
        fh.write(_GITIGNORE_CONTENT)

    # add -A honors the .gitignore whitelist and — unlike explicit pathspecs —
    # cannot abort the whole add when a whitelisted dir doesn't exist yet
    # (git rejects unmatched pathspecs wholesale; marketplace/ is absent on a
    # fresh install).
    _run(config_dir, ["add", "-A"], check=False)
    _run(config_dir, ["commit", "-qm", "initial config snapshot"],
         check=False)


def commit_config(config_dir: str, message: str,
                  paths: Sequence[str] = ()) -> str:
    """Stage + commit any tracked-file changes. Returns the new sha, or
    an empty string if there were no changes to commit.

    *paths* (#1391) scopes the commit to those paths, so a boot write is
    recorded under its own message without sweeping in anything else.
    """
    spec = ["--", *paths] if paths else []
    with _COMMIT_LOCK:
        status = _run(config_dir, ["status", "--porcelain", *spec])
        if not status:
            return ""

        _run(config_dir, ["add", "-A", *spec])
        _run(config_dir, ["commit", "-qm", message, *spec])
        return _run(config_dir, ["rev-parse", "HEAD"])


def commit_config_checked(
    config_dir: str,
    message: str,
    validate: Callable[[str], list[str]],
) -> tuple[str, list[str]]:
    """Stage all tracked changes, validate EXACTLY the staged tree, then
    commit the index. Returns ``(sha, errors)``.

    #351 (validate→commit TOCTOU): the tool used to validate the worktree,
    then stage-and-commit as a separate step — an edit landing in between
    (an SSH operator writing invalid YAML) was committed unvalidated and
    failed the next boot. Here the sequence is: ``git add -A`` freezes the
    snapshot in the index; ``git checkout-index`` exports that exact
    snapshot to a temp dir; ``validate`` runs over the export; only a clean
    result commits the index. A worktree edit after staging can neither
    enter the commit (``git commit`` commits the index) nor influence
    validation (the export is already taken). On refusal the staging is
    reset and ``(\"\", errors)`` returned; error strings have the temp
    export path rewritten to *config_dir* so refusals read as real paths.

    No-op (clean tree) returns ``("", [])`` without calling ``validate``.

    Sol r1-1/2/3 + Terra r1-1: the whole sequence runs against a PRIVATE
    temporary index (``GIT_INDEX_FILE``), never the repository's real one.
    Consequences, each previously a finding: a refusal or an unexpected
    exception leaves the real index byte-for-byte untouched (an operator's
    intentional manual staging survives); a concurrent ``git add`` by
    another process (boot's snapshot, an SSH operator) cannot inject
    content into this commit, because the commit is built with
    ``commit-tree`` from the validated private tree, not from the shared
    index. After a successful commit the real index is refreshed to the new
    HEAD (exactly what a normal ``git commit`` leaves behind).
    """
    with _COMMIT_LOCK:
        status = _run(config_dir, ["status", "--porcelain"])
        if not status:
            return "", []
        with tempfile.TemporaryDirectory(prefix="casa-commit-gate-") as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index")}
            # Pin the base commit once: it is the private index's seed, the
            # new commit's parent, AND the compare-and-swap expectation for
            # the ref update below.
            head = _run(config_dir, ["rev-parse", "HEAD"])
            # Private index := HEAD, then stage the worktree into it.
            _run(config_dir, ["read-tree", head], env=env)
            _run(config_dir, ["add", "-A"], env=env)
            export = os.path.join(tmp, "export")
            os.makedirs(export)
            # Trailing separator is required: checkout-index treats the
            # prefix as a literal string prepended to each path.
            _run(config_dir, ["checkout-index", "-a", "-f",
                              f"--prefix={export}{os.sep}"], env=env)
            errors = [
                e.replace(export, config_dir)
                for e in (validate(export) or [])
            ]
            if errors:
                return "", errors     # private index discarded; repo untouched
            tree = _run(config_dir, ["write-tree"], env=env)
            if tree == _run(config_dir, ["rev-parse", f"{head}^{{tree}}"]):
                return "", []         # staged snapshot identical to HEAD
            sha = _run(config_dir,
                       ["commit-tree", tree, "-p", head, "-m", message])
            # CAS ref update: refuses (raises) if HEAD moved since `head` —
            # a plain overwrite would silently ORPHAN a commit an external
            # writer (SSH operator) landed mid-window, which even the old
            # add-then-commit flow could never do. The caller surfaces the
            # git error; a retry re-runs the gate on the new HEAD.
            # Refresh the real index ONLY for the paths this commit touched
            # (Terra/Sol r2-1: a full `git reset` would also drop an
            # operator's concurrently staged unrelated entry). Unchanged
            # paths keep index entries identical to the new HEAD, so no
            # phantom diffs appear; a concurrent stage of a path we DID
            # commit is superseded (its content survives in the worktree).
            #
            # Sol r3-1: entries carry `:(literal)` pathspec magic — a bare
            # name from diff-tree would be interpreted as a GLOB by reset
            # (`foo[1].yaml` matching `foo1.yaml`, refreshing the wrong
            # entry and dropping unrelated staging).
            #
            # Sol r3-2 (ordering): the refresh targets the NEW commit and
            # runs BEFORE the ref update. With the old order there was a
            # window (new HEAD, old index) where an external `git commit`
            # would build a child tree from the stale index, silently
            # reverting every Casa-touched path. With this order the same
            # external commit either lands before the CAS (carrying the
            # already-refreshed validated content) and the CAS refuses —
            # caller retries and no-ops — or lands after and is untouched.
            changed = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "--name-only",
                 "-z", "-r", sha],
                cwd=config_dir, capture_output=True, text=True, check=True,
            ).stdout
            committed_paths = [p for p in changed.split("\0") if p]
            # Terra r5-1: skip any committed path the operator has staged
            # different from the base commit — refreshing it would DESTROY
            # their staged version (its content lives only in the index).
            # Their entry survives, visible as staged-vs-new-HEAD.
            pre_staged = subprocess.run(
                ["git", "diff-index", "--cached", "--name-only", "-z", head],
                cwd=config_dir, capture_output=True, text=True, check=True,
            ).stdout
            staged_set = {p for p in pre_staged.split("\0") if p}
            refresh_paths = [
                p for p in committed_paths if p not in staged_set
            ]
            spec = "\0".join(f":(literal){p}" for p in refresh_paths)
            if spec:
                refresh = subprocess.run(
                    ["git", "reset", "-q", sha, "--pathspec-file-nul",
                     "--pathspec-from-file=-"],
                    cwd=config_dir, capture_output=True, text=True,
                    input=spec,
                )
                if refresh.returncode != 0:
                    # Sol r2-1b: surface, don't swallow — the commit is
                    # still valid; stale index entries for these paths
                    # self-heal on the next stage (every Casa flow stages
                    # with add -A).
                    logger.warning(
                        "config commit %s: index refresh failed: %s",
                        sha[:8], refresh.stderr.strip(),
                    )
            try:
                _run(config_dir, ["update-ref", "HEAD", sha, head])
            except Exception:
                # Sol r4-1: HEAD moved between our snapshot and the CAS —
                # the refresh above staged OUR content for the touched
                # paths; left in place, a later ordinary commit would
                # silently revert the external work. Undo by resetting to
                # the CURRENT HEAD — but only entries STILL HOLDING OUR
                # content (Sol r5-2): an operator who staged newer content
                # for a touched path after the refresh keeps their entry
                # (it no longer matches the candidate commit's version).
                if spec:
                    ours = subprocess.run(
                        ["git", "diff-index", "--cached", "--name-only",
                         "-z", sha],
                        cwd=config_dir, capture_output=True, text=True,
                    ).stdout
                    changed_since = {p for p in ours.split("\0") if p}
                    undo_spec = "\0".join(
                        f":(literal){p}" for p in refresh_paths
                        if p not in changed_since
                    )
                    if undo_spec:
                        undo = subprocess.run(
                            ["git", "reset", "-q", "HEAD",
                             "--pathspec-file-nul",
                             "--pathspec-from-file=-"],
                            cwd=config_dir, capture_output=True, text=True,
                            input=undo_spec,
                        )
                        if undo.returncode != 0:
                            logger.warning(
                                "config commit %s: CAS refused and the "
                                "index undo also failed: %s",
                                sha[:8], undo.stderr.strip(),
                            )
                raise
        return sha, []


def changed_paths(config_dir: str, sha: str) -> list[str]:
    """Return the repo-relative paths a commit touched (vs its first parent).

    Used by the G-2 reload guard (#231/#222) to tell a plugin-registry-only
    persist commit — already activated in-process — from a commit that also
    edits agents/ or policies/ and therefore genuinely owes a reload, and
    (#1086) to record which paths the obligation owes. Returns an empty list
    on any git error (fail-safe: the caller then arms the reload obligation
    as usual rather than wrongly suppressing it). NUL-separated (``-z``) so a
    name git would C-quote (non-ASCII, a quote, a newline) comes back
    literally, as the path the reload scopes know it by.
    """
    try:
        out = _run(config_dir,
                   ["diff-tree", "-z", "--no-commit-id", "--name-only", "-r", sha])
    except Exception:  # noqa: BLE001 — never let a git hiccup break a commit
        return []
    return [p for p in out.split("\0") if p.strip()]


def snapshot_manual_edits(config_dir: str) -> str | None:
    """Commit any uncommitted changes found in tracked paths. Returns
    the new sha if a commit was made, else None.

    Runs at Casa boot so human edits via SSH land as proper commits
    before the builder agent can race against them.
    """
    with _COMMIT_LOCK:
        status = _run(config_dir, ["status", "--porcelain"])
        if not status:
            return None
        _run(config_dir, ["add", "-A"])
        _run(config_dir, ["commit", "-qm", "manual edit (boot-time snapshot)"])
        return _run(config_dir, ["rev-parse", "HEAD"])


def restore_file(config_dir: str, sha: str, relpath: str) -> None:
    """Restore *relpath* to its content at *sha* and commit the restore.

    #351 (low): a path that did not exist at *sha* (a file ADDED after the
    target commit) cannot be checked out from it — ``git checkout`` errors
    and the rollback failed. Restoring to "absent" means removing the file,
    so that case becomes ``git rm`` + commit. Sol r1-4: that branch is taken
    only for a VERIFIED target commit — a malformed/unknown *sha* must
    raise, not silently delete the live file.
    """
    with _COMMIT_LOCK:
        _run(config_dir,
             ["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"])
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}:{relpath}"],
            cwd=config_dir, capture_output=True, text=True,
        )
        if probe.returncode == 0:
            _run(config_dir, ["checkout", sha, "--", relpath])
            _run(config_dir, ["add", relpath])
            _run(config_dir, ["commit", "-qm",
                              f"restore {relpath} to {sha[:8]}"])
        else:
            _run(config_dir, ["rm", "-f", "--ignore-unmatch", "--", relpath])
            _run(config_dir, ["commit", "-qm",
                              f"remove {relpath} (absent at {sha[:8]})"])
