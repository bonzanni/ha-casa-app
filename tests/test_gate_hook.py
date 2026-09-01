"""The pre-push hook and the attestation it requires.

These were verified by hand at the shell but had no automated test, which is exactly the
kind of gap that rots: the hook is the only thing standing between a local mistake and an
irreversible publication.
"""
import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".githooks" / "pre-push"
ATTEST = ROOT / "scripts" / "attest.sh"
ZERO = "0" * 40


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    # -b main: attest.sh records the CURRENT branch into the receipt and
    # pre-push compares it to the pushed ref. The tests push refs/heads/main,
    # so the fixture must be on main regardless of the host's
    # init.defaultBranch — CI runners default to master, which made the
    # attest-then-push test fail only in CI.
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    # attest.sh sweeps the branch name, and every policy file fails closed when absent.
    hooks = repo / ".githooks"
    hooks.mkdir()
    (hooks / "deny-patterns.txt").write_text(
        "[paths]\n(^|/)zzforbidden-\n[content]\nZZ-DENIED-LITERAL-ZZ\n[allow-content]\nZZ-NEVER-ZZ\n"
    )
    (hooks / "root-allowlist.txt").write_text("")
    (hooks / "gitleaks-allow-sites.txt").write_text("")
    return repo


def _commit(repo: Path, rel: str) -> str:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("body\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", rel], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _push(repo: Path, sha: str, env: dict | None = None, *,
          remote_sha: str = ZERO, branch: str = "main"):
    """Feed the hook one stdin line as git would. `remote_sha` is what the destination
    currently holds for the ref: ZERO selects the new-ref arm, a real sha the
    existing-ref arm. `$1` is the destination remote name, as git passes it."""
    return subprocess.run(
        ["bash", str(HOOK), "origin"], cwd=repo, capture_output=True, text=True,
        input=f"refs/heads/{branch} {sha} refs/heads/{branch} {remote_sha}\n",
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo), **(env or {})},
    )


def _receipt(repo: Path, name: str, body: str) -> None:
    (repo / ".git" / name).write_text(body)


def _reviewed(repo: Path, sha: str, extra: list[str] | None = None) -> str:
    """Record the commit SET the gate swept, as scripts/gate.sh does; return its digest."""
    shas = subprocess.run(
        ["git", "-C", str(repo), "rev-list", sha], capture_output=True, text=True, check=True
    ).stdout.split()
    keep = shas if extra is None else extra
    body = "".join(f"{c}\n" for c in keep)
    (repo / ".git" / "casa-gate-commits").write_text(body)
    return hashlib.sha256(body.encode()).hexdigest()


def _approve(repo: Path, sha: str, branch: str = "main",
             extra: list[str] | None = None) -> None:
    """A complete approved receipt: tip, set digest, branch, claims."""
    digest = _reviewed(repo, sha, extra=extra)
    _receipt(repo, "casa-gate-approved",
             f"{sha}\n{digest}\n{branch}\nread-in-full; reviewers=a,b\n")


def test_a_gated_push_without_an_attestation_is_refused(tmp_path):
    """Pins INV-PUB-003 (the mechanism: nothing publishes without an attested human read). Red case demonstrated: skipping the pre-push receipt check fails this test."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    result = _push(repo, sha)
    assert result.returncode == 1
    assert "not attested" in result.stderr


def test_a_push_touching_only_the_guard_is_gated(tmp_path):
    """PR-0's shape exactly: publication machinery and boundary prose, no corpus."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "CLAUDE.md")
    result = _push(repo, sha)
    assert result.returncode == 1
    assert "not attested" in result.stderr


def test_an_ordinary_source_push_is_gated_too(tmp_path):
    """This used to assert the opposite, which enshrined a real hole: a push adding
    private prose or an address to casa/ was an ordinary, fully-verified path to a public
    ref, gated by nothing. Every push to a public repo publishes."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "casa/rootfs/opt/casa/thing.py")
    result = _push(repo, sha)
    assert result.returncode == 1
    assert "not attested" in result.stderr


def test_an_empty_commit_is_gated(tmp_path):
    """It changes no file, so a path-based trigger let it through — but its MESSAGE,
    author and committer are published. A test previously asserted this was allowed."""
    repo = _repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "private text here"],
        check=True,
    )
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    result = _push(repo, sha)
    assert result.returncode == 1
    assert "not attested" in result.stderr


def test_an_empty_tip_does_not_hide_an_earlier_commit(tmp_path):
    """What the removed test was reaching for: the hook enumerates the range, so a push
    whose TIP is empty is still gated for the commits behind it."""
    repo = _repo(tmp_path)
    _commit(repo, "docs/architecture/a.md")
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "empty tip"],
                   check=True)
    tip = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    # A receipt for the right BRANCH but the wrong tip, so the run reaches the enumeration
    # rather than stopping at the earlier unattested-branch check.
    _receipt(repo, "casa-gate-approved", "f" * 40 + "\ndigest\nmain\nclaims\n")
    result = _push(repo, tip)
    assert result.returncode == 1
    # Prove BOTH commits were enumerated: a hook that inspected only the empty tip would
    # also return 1, so the count is what distinguishes them.
    assert "2 commit(s)" in result.stderr


def test_a_non_branch_namespace_is_refused(tmp_path):
    """An arbitrary namespace can carry a tag object whose target is already reachable, so
    it introduces no commits and would slip past the enumeration entirely."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    result = _tag_push(repo, "refs/heads/main", sha, remote_ref="refs/archive/x")
    assert result.returncode == 1
    assert "only refs/heads/* is allowed" in result.stderr


def test_the_automated_receipt_alone_does_not_authorise_a_push(tmp_path):
    """gate.sh proves the automated half only; the read and the review are separate, and
    an earlier design let the automated receipt stand in for both."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-automated", sha + "\n")
    assert _push(repo, sha).returncode == 1


def test_an_attested_tip_is_allowed(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _approve(repo, sha)
    assert _push(repo, sha).returncode == 0


def test_an_attested_tip_does_not_authorise_unreviewed_commits(tmp_path):
    """The receipt names a TIP; the gate swept a RANGE. A tip gated against one base,
    pushed to a destination that has less history, introduces commits the review never
    covered — and matching the tip alone let them through."""
    repo = _repo(tmp_path)
    _commit(repo, "docs/architecture/older.md")
    sha = _commit(repo, "docs/architecture/a.md")
    _approve(repo, sha, extra=[sha])           # only the tip was reviewed
    result = _push(repo, sha)
    assert result.returncode == 1
    assert "never in" in result.stderr


def test_a_missing_reviewed_set_is_refused(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-approved", f"{sha}\ndigest\nmain\nread-in-full\n")
    result = _push(repo, sha)
    assert result.returncode == 1
    assert "no reviewed commit set" in result.stderr


def test_a_stale_attestation_is_refused(tmp_path):
    """Applying a review finding makes a new commit; the old approval must not carry."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-approved", "f" * 40 + "\ndigest\nmain\nold\n")
    assert _push(repo, sha).returncode == 1


def test_a_branch_deletion_is_not_gated(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs/architecture/a.md")
    result = subprocess.run(
        ["bash", str(HOOK)], cwd=repo, capture_output=True, text=True,
        input=f"(delete) {ZERO} refs/heads/main {ZERO}\n",
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo)},
    )
    assert result.returncode == 0


def test_an_explicit_override_is_allowed_and_announced(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    result = _push(repo, sha, env={"CASA_GATE_OVERRIDE": "reason recorded in the PR"})
    assert result.returncode == 0
    assert "overridden" in result.stderr


# --- the attestation itself ----------------------------------------------------------

def _attest(repo: Path, *args):
    return subprocess.run(
        ["bash", str(ATTEST), *args], cwd=repo, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo)},
    )


def test_attest_refuses_a_bare_invocation(tmp_path):
    """`attest.sh x` used to mint the receipt pre-push honours."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-automated", sha + "\n")
    result = _attest(repo)
    assert result.returncode == 1
    assert "--read-in-full" in result.stderr
    assert not (repo / ".git" / "casa-gate-approved").exists()


def test_attest_refuses_a_partial_claim(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-automated", sha + "\n")
    result = _attest(repo, "--read-in-full", "--reviewers", "a,b")
    assert result.returncode == 1
    assert not (repo / ".git" / "casa-gate-approved").exists()


def test_attest_refuses_without_a_matching_automated_receipt(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs/architecture/a.md")
    result = _attest(repo, "--read-in-full", "--reviewers", "a,b",
                     "--findings-applied", "--re-reviewed")
    assert result.returncode == 1
    assert not (repo / ".git" / "casa-gate-approved").exists()


def test_attest_refuses_when_the_automated_receipt_is_for_another_commit(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-automated", "f" * 40 + "\n")
    assert _attest(repo, "--read-in-full", "--reviewers", "a,b",
                   "--findings-applied", "--re-reviewed").returncode == 1


def test_attest_writes_the_receipt_for_the_repo_it_is_run_in(tmp_path):
    """It must touch the throwaway repo's receipt, never the real checkout's."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    digest = _reviewed(repo, sha)
    _receipt(repo, "casa-gate-automated", f"{sha}\n{digest}\n")
    result = _attest(repo, "--read-in-full", "--reviewers", "a,b",
                     "--findings-applied", "--re-reviewed")
    assert result.returncode == 0, result.stderr
    receipt = (repo / ".git" / "casa-gate-approved").read_text()
    assert receipt.splitlines()[0] == sha
    assert receipt.splitlines()[1] == digest
    assert "reviewers=a,b" in receipt
    assert _push(repo, sha).returncode == 0, "the receipt it wrote is accepted"


def test_attest_refuses_on_a_dirty_tree(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-automated", sha + "\n")
    (repo / "docs" / "architecture" / "a.md").write_text("changed\n")
    assert _attest(repo, "--read-in-full", "--reviewers", "a,b",
                   "--findings-applied", "--re-reviewed").returncode == 1


# --- tags are refused outright ---------------------------------------------------------

def _tag_push(repo: Path, local_ref: str, sha: str, remote_ref: str | None = None,
              env: dict | None = None):
    remote_ref = remote_ref or local_ref
    return subprocess.run(
        ["bash", str(HOOK)], cwd=repo, capture_output=True, text=True,
        input=f"{local_ref} {sha} {remote_ref} {ZERO}\n",
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo), **(env or {})},
    )


def test_an_annotated_tag_push_is_refused(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs/architecture/a.md")
    subprocess.run(["git", "-C", str(repo), "tag", "-a", "v1", "-m", "release"], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "v1"],
                         capture_output=True, text=True, check=True).stdout.strip()
    result = _tag_push(repo, "refs/tags/v1", sha)
    assert result.returncode == 1
    assert "only refs/heads/* is allowed" in result.stderr


def test_a_lightweight_tag_push_is_refused(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    assert _tag_push(repo, "refs/tags/light", sha).returncode == 1


def test_a_branch_source_pushed_to_a_tag_destination_is_refused(tmp_path):
    """`main:refs/tags/x` publishes a tag while the SOURCE ref is a branch — a
    local-ref-only test waved this through."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    result = _tag_push(repo, "refs/heads/main", sha, remote_ref="refs/tags/sneaky")
    assert result.returncode == 1
    assert "only refs/heads/* is allowed" in result.stderr


def test_a_tag_name_with_regex_metacharacters_is_still_refused(tmp_path):
    """The removed binding interpolated the name into a grep expression, so a name like
    `v1|.*` matched any receipt line."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    assert _tag_push(repo, "refs/tags/v1|.*", sha).returncode == 1


def test_an_attested_commit_receipt_does_not_authorise_a_tag(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _approve(repo, sha)
    assert _push(repo, sha).returncode == 0, "the commit push is fine"
    assert _tag_push(repo, "refs/tags/v1", sha).returncode == 1, "the tag is not"


def test_a_tag_push_can_be_overridden_with_a_reason(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    result = _tag_push(repo, "refs/tags/v1", sha,
                       env={"CASA_GATE_OVERRIDE": "documented exception"})
    assert result.returncode == 0
    assert "overridden" in result.stderr


def test_a_swapped_reviewed_set_is_refused(tmp_path):
    """Re-running the gate at the same HEAD with a wider base rewrites the set; the older
    approval must not still authorise it."""
    repo = _repo(tmp_path)
    _commit(repo, "docs/architecture/older.md")
    sha = _commit(repo, "docs/architecture/a.md")
    _approve(repo, sha)
    _reviewed(repo, sha, extra=[sha, "0" * 40])      # set rewritten after approval
    result = _push(repo, sha)
    assert result.returncode == 1
    assert "changed since it was attested" in result.stderr


def test_a_push_to_a_different_branch_than_attested_is_refused(tmp_path):
    """A branch name is published metadata; the attestation names the swept one."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _approve(repo, sha, branch="main")
    ok = subprocess.run(
        ["bash", str(HOOK)], cwd=repo, capture_output=True, text=True,
        input=f"refs/heads/main {sha} refs/heads/private-client-name {ZERO}\n",
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo)},
    )
    assert ok.returncode == 1
    assert "published text" in ok.stderr


def test_republishing_the_same_objects_under_a_new_branch_name_is_refused(tmp_path):
    """Introduces NO commits — every objects-based check short-circuits — but the branch
    name is newly published. A check placed after the commit enumeration never ran here;
    an end-to-end push proved it silently succeeded."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _approve(repo, sha, branch="main")
    result = subprocess.run(
        ["bash", str(HOOK)], cwd=repo, capture_output=True, text=True,
        # remote_sha is zero (new ref) and the objects are already reachable there
        input=f"refs/heads/main {sha} refs/heads/leaky-client-name {ZERO}\n",
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo)},
    )
    assert result.returncode == 1
    assert "introduces no commits" in result.stderr


def test_publishing_a_branch_name_with_no_receipt_at_all_is_refused(tmp_path):
    """`git push <published-sha>:refs/heads/private-client-name` adds no objects, so the
    commit enumeration `continue`d — and the branch check was conditional on a receipt
    existing, so with none it never ran. Both reviewers found it; the comment claiming the
    commit check would report it was wrong."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    result = subprocess.run(
        ["bash", str(HOOK)], cwd=repo, capture_output=True, text=True,
        input=f"refs/heads/main {sha} refs/heads/private-client-name {ZERO}\n",
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo)},
    )
    assert result.returncode == 1
    assert "published text even" in result.stderr


def test_an_unattested_branch_publication_can_be_overridden(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    result = subprocess.run(
        ["bash", str(HOOK)], cwd=repo, capture_output=True, text=True,
        input=f"refs/heads/main {sha} refs/heads/other {ZERO}\n",
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo),
             "CASA_GATE_OVERRIDE": "documented exception"},
    )
    assert result.returncode == 0
    assert "overridden" in result.stderr


# --- coverage is relative to the DESTINATION, for an existing ref too --------------------
#
# Every case above pushes a NEW ref with no remote configured, so `ls-remote` fails and
# the hook enumerates the whole ancestry. These cases give the hook a real destination: a
# bare repository it can ask, and a real `remote_sha` for the ref being replaced.

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    ).stdout.strip()


def _remote(repo: Path) -> Path:
    """A bare destination named `origin`, holding whatever `repo` pushes to it.

    Fixture pushes use --no-verify: the hook under test is run explicitly, never as a
    side effect of building the graph."""
    bare = repo.parent / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    return bare


def _fixture_push(repo: Path, *refspecs: str) -> None:
    _git(repo, "push", "-q", "--no-verify", "origin", *refspecs)


def _moved_main(repo: Path) -> dict[str, str]:
    """M0 on main (published); cand = C1 on M0 (published as a new ref); main advances to
    M1 (published); cand is rebased onto M1, giving C1r — the byte-identical re-cut."""
    _remote(repo)
    m0 = _commit(repo, "docs/architecture/m0.md")
    _fixture_push(repo, "main")
    _git(repo, "checkout", "-q", "-b", "cand")
    c1 = _commit(repo, "docs/architecture/c1.md")
    _fixture_push(repo, "cand")
    _git(repo, "checkout", "-q", "main")
    m1 = _commit(repo, "docs/architecture/m1.md")
    _fixture_push(repo, "main")
    _git(repo, "checkout", "-q", "cand")
    _git(repo, "rebase", "-q", "main")
    c1r = _git(repo, "rev-parse", "HEAD")
    assert c1r != c1
    return {"M0": m0, "C1": c1, "M1": m1, "C1r": c1r}


def _bare_ref(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(repo.parent / "origin.git"), "rev-parse", ref],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_a_re_cut_onto_a_moved_main_pushes_over_its_own_ref(tmp_path):
    """Pins INV-PUB-004 (the accept half): a commit the destination already reaches on
    ANY branch is not introduced, so the reviewed set of a re-cut is the re-cut alone.

    Red case demonstrated: enumerating an existing ref as `remote_sha..local_sha` counts
    the destination's own `main` commit as introduced and refuses the attested re-cut,
    naming M1 as "never in the reviewed range"."""
    repo = _repo(tmp_path)
    g = _moved_main(repo)
    # The shape, asserted before the hook runs: the replaced tip lacks TWO commits, the
    # destination as a whole lacks ONE.
    assert set(_git(repo, "rev-list", f"{g['C1']}..{g['C1r']}").split()) == {g["M1"], g["C1r"]}
    assert _git(repo, "rev-list", g["C1r"], "--not", g["C1"], g["M1"]).split() == [g["C1r"]]
    _approve(repo, g["C1r"], branch="cand", extra=[g["C1r"]])   # exactly what gate.sh writes
    assert (repo / ".git" / "casa-gate-commits").read_text().split() == [g["C1r"]]

    result = _push(repo, g["C1r"], remote_sha=g["C1"], branch="cand")
    assert result.returncode == 0, result.stderr
    assert g["M1"] not in result.stderr


def test_a_real_force_push_of_a_re_cut_is_gated_and_then_accepted(tmp_path):
    """The same shape through `git push` itself, with the repository's hooks installed for
    that one command, so the stdin line the direct tests feed is proven to be git's.

    First WITHOUT a receipt — refused, the bare ref unmoved — proving the hook ran at all;
    then approved — accepted, the bare ref at the re-cut."""
    repo = _repo(tmp_path)
    g = _moved_main(repo)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(repo)}
    push = ["git", "-c", f"core.hooksPath={HOOK.parent}", "push", "--force-with-lease",
            "origin", "cand"]

    refused = subprocess.run(push, cwd=repo, capture_output=True, text=True, env=env)
    assert refused.returncode == 1, refused.stderr
    assert "not attested" in refused.stderr
    assert _bare_ref(repo, "refs/heads/cand") == g["C1"]

    _approve(repo, g["C1r"], branch="cand", extra=[g["C1r"]])
    accepted = subprocess.run(push, cwd=repo, capture_output=True, text=True, env=env)
    assert accepted.returncode == 0, accepted.stderr
    assert _bare_ref(repo, "refs/heads/cand") == g["C1r"]
