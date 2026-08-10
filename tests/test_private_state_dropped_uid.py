"""The inventory, verified BY ACTUALLY DROPPING TO AN UNPRIVILEGED UID.

GHSA-569r-7crq-xr43's guarantee is a statement about what a per-engagement uid
can read. Every other test in this release asserts modes, which is a proxy: it
assumes the mapping from mode bits to reachability, assumes no other name reaches
the file, and assumes the directory-only tightening in ``private_state`` really
does make the contents unreachable. This test removes those assumptions by
forking, calling ``setuid``, and trying the open.

That distinction is not pedantic. During the investigation that produced this
advisory, three probes returned confident verdicts in BOTH directions while
actually testing nothing — a missing ``ps`` binary read as "no survivors", every
container process sharing ``pgid=1`` read as a process-group match, and zombies
counted as live survivors. Assert the outcome, never the arrangement.

Requires root (to ``setuid`` away from it), so it runs in the docker tier. The
unit-tier tests in ``test_private_state.py`` cover the same inventory through
modes, which is what keeps the fast gate meaningful.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

import private_state

pytestmark = [pytest.mark.unit, pytest.mark.docker]

# nobody. Any uid that is not root and owns none of the fixture works; using a
# uid that exists avoids depending on NSS for a synthetic one.
DROPPED_UID = 65534
DROPPED_GID = 65534

pytestmark_root_only = pytest.mark.skipif(
    os.geteuid() != 0 if hasattr(os, "geteuid") else True,
    reason="dropping to an unprivileged uid requires starting as root",
)


def _readable_as_dropped(path: str) -> bool:
    """True iff *path* can be opened for reading as DROPPED_UID.

    Runs in a forked child so the uid drop cannot leak into the test process
    (setuid is irreversible). The child communicates through its exit status:
    0 = the open succeeded, 1 = it was refused. A directory is "read" by
    listing it, which is the operation that matters for a log or `.git` dir.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        code = 1
        try:
            os.close(read_fd)
            os.setgroups([])
            os.setgid(DROPPED_GID)
            os.setuid(DROPPED_UID)
            assert os.getuid() == DROPPED_UID, "uid drop did not take effect"
            try:
                if os.path.isdir(path):
                    os.listdir(path)
                else:
                    with open(path, "rb") as fh:
                        fh.read(1)
                code = 0
            except OSError:
                code = 1
        except BaseException:
            code = 2  # the harness itself failed — never read as "refused"
        finally:
            os.close(write_fd)
            os._exit(code)

    os.close(write_fd)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status), f"probe child did not exit cleanly for {path}"
    code = os.WEXITSTATUS(status)
    assert code in (0, 1), (
        f"probe harness failed for {path} (exit {code}) — this is NOT evidence "
        "that the path is protected"
    )
    return code == 0


def _fixture_tree(root: Path) -> None:
    """A miniature of the real layout: traversable roots, private state inside."""
    for d in ("data", "config", "var/log", "run/s6/container_environment"):
        (root / d).mkdir(parents=True, exist_ok=True)
        os.chmod(root / d, 0o755)

    for rel in (
        "run/s6/container_environment/SUPERVISOR_TOKEN",
        "run/s6/container_environment/HASSIO_TOKEN",
        "data/webhook_secret",
        "data/sessions.json",
        "data/topic-ledger.json",
        "data/jobs.json",
        "data/plugin-health.json",
        "data/plugin-setup-episodes.json",
        "data/callback_acks.json",
        "data/event_acks.json",
        "data/webhook_trigger_acks.json",
        "data/persona_install_acks.json",
        "data/specialist_install_acks.json",
        "data/sessions.json.corrupt",
        "data/topic-ledger.json.casabak",
    ):
        p = root / rel
        p.write_text("sensitive", encoding="utf-8")
        os.chmod(p, 0o644)

    for rel in ("data/cold-retain-retry", "config/cc-home", "config/.git",
                "var/log/casa-engagement-aaaaaaaa"):
        d = root / rel
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o755)
        inner = d / "inside.txt"
        inner.write_text("sensitive", encoding="utf-8")
        os.chmod(inner, 0o644)

    # The control set: things the dropped uid MUST still reach.
    report = root / "data/config-sync-report.json"
    report.write_text("{}", encoding="utf-8")
    os.chmod(report, 0o644)
    store = root / "config/plugins/store/x/abc"
    store.mkdir(parents=True, exist_ok=True)
    os.chmod(root / "config/plugins", 0o755)
    os.chmod(root / "config/plugins/store", 0o755)
    os.chmod(root / "config/plugins/store/x", 0o755)
    os.chmod(store, 0o755)
    manifest = store / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    os.chmod(manifest, 0o644)


@pytest.fixture
def enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _fixture_tree(tmp_path)
    monkeypatch.setattr(
        private_state, "_resolve",
        lambda root, declared: os.path.join(str(tmp_path), declared.lstrip("/")),
    )
    # tmp_path itself must be traversable, or every probe "passes" for the wrong
    # reason — the classic false negative this whole file exists to avoid.
    for parent in (tmp_path, *tmp_path.parents):
        try:
            os.chmod(parent, os.stat(parent).st_mode | 0o005)
        except OSError:
            break
    private_state.enforce()
    return tmp_path


@pytestmark_root_only
def test_the_probe_can_detect_a_readable_file(enforced: Path) -> None:
    """Prove the harness works before trusting a single refusal.

    Without this, a broken probe (wrong uid, unreachable parent, child crashing
    early) reports "refused" for everything and the whole file passes while
    verifying nothing.
    """
    canary = enforced / "data/config-sync-report.json"
    assert _readable_as_dropped(str(canary)), (
        "the probe cannot read a deliberately world-readable file — the harness "
        "is broken, so no refusal below is meaningful"
    )


@pytestmark_root_only
def test_no_declared_private_file_is_readable(enforced: Path) -> None:
    exposed = [
        e.path for e in private_state.FILES
        if _readable_as_dropped(private_state._resolve("/", e.path))
    ]
    assert exposed == [], f"still readable by uid {DROPPED_UID}: {exposed}"


@pytestmark_root_only
def test_no_declared_private_directory_is_readable(enforced: Path) -> None:
    exposed = [
        e.path for e in private_state.DIRS
        if _readable_as_dropped(private_state._resolve("/", e.path))
    ]
    assert exposed == [], f"still listable by uid {DROPPED_UID}: {exposed}"


@pytestmark_root_only
def test_contents_of_a_tightened_directory_are_unreachable(
    enforced: Path,
) -> None:
    """The load-bearing claim behind not recursing: the FILE inside keeps mode
    0644, and is unreachable anyway because its parent lost `x` for others."""
    inner = enforced / "config/cc-home/inside.txt"
    assert stat.S_IMODE(os.stat(inner).st_mode) == 0o644
    assert not _readable_as_dropped(str(inner))


@pytestmark_root_only
def test_sibling_engagement_log_is_unreachable(enforced: Path) -> None:
    """The advisory's worst finding: ~21 MB of another engagement's stdout."""
    sibling = enforced / "var/log/casa-engagement-aaaaaaaa/inside.txt"
    assert not _readable_as_dropped(str(sibling))


@pytestmark_root_only
def test_quarantine_aliases_are_unreachable(enforced: Path) -> None:
    for rel in ("data/sessions.json.corrupt", "data/topic-ledger.json.casabak"):
        assert not _readable_as_dropped(str(enforced / rel)), rel


@pytestmark_root_only
def test_the_traversal_roots_are_still_traversable(enforced: Path) -> None:
    """Over-closure guard. The dropped uid reaches its own workspace and its
    assigned plugin artifacts THROUGH /data and /config; tightening either one
    breaks every engagement, which is worse than the exposure."""
    assert _readable_as_dropped(str(enforced / "data"))
    assert _readable_as_dropped(str(enforced / "config"))


@pytestmark_root_only
def test_the_control_set_is_still_readable(enforced: Path) -> None:
    """Everything the dropped process legitimately needs. The config-sync report
    is allow-listed for the configurator executor's Read tool; the plugin store
    artifact is loaded by the dropped CLI itself."""
    assert _readable_as_dropped(str(enforced / "data/config-sync-report.json"))
    assert _readable_as_dropped(
        str(enforced / "config/plugins/store/x/abc/manifest.json"))


@pytestmark_root_only
def test_a_regressed_mode_is_caught(enforced: Path) -> None:
    """The red case for this file itself: put one file back to 0644 and the
    probe must see it. A suite that cannot fail here proves nothing."""
    secret = enforced / "data/webhook_secret"
    assert not _readable_as_dropped(str(secret))
    os.chmod(secret, 0o644)
    assert _readable_as_dropped(str(secret))
