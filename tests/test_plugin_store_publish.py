"""§3.2 publish pipeline: ref resolution failure taxonomy, staging + atomic
rename, idempotent re-publish, corrupt-destination fail-closed, manifest
validation, bundle import."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import plugin_store
from plugin_registry import compute_artifact_id
from plugin_store import (
    METADATA_FILENAME,
    RefNotFound,
    ResolveUnavailable,
    StoreError,
    content_checksum,
    import_bundle,
    publish,
    publish_from_tree,
    resolve_ref,
    validate_artifact,
    validate_manifest,
)

pytestmark = pytest.mark.unit

SHA = "a" * 40


def _unfreeze(p: Path) -> None:
    """Restore write on a published artifact file — publish() now freezes files
    read-only (Sol #7). Tests that simulate corruption which BYPASSED the freeze
    (privileged process / disk error) must defeat it first; the artifact_verdict
    backstop must still catch the corruption."""
    import os
    import stat
    os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) | 0o200)


def _plugin_tree(tmp_path, name="probe", version="1.0.0") -> Path:
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": version}), encoding="utf-8")
    (root / "skills").mkdir()
    (root / "skills" / "s.md").write_text("skill", encoding="utf-8")
    return root


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _gh_response(status: int, body: str, headers: dict | None = None) -> str:
    """Render `gh api -i` stdout: status line + headers + blank + body."""
    hdrs = {"content-type": "application/json; charset=utf-8", **(headers or {})}
    head = "\n".join([f"HTTP/2.0 {status} X"] + [f"{k}: {v}" for k, v in hdrs.items()])
    return f"{head}\n\n{body}"


def _proc_for(status: int, body: str, headers: dict | None = None) -> "_Proc":
    return _Proc(0 if 200 <= status < 300 else 1,
                 _gh_response(status, body, headers), "")


def test_resolve_ref_happy_bare_sha_with_headers():
    """200 via `gh api -i … --jq .sha`: headers block + bare sha body."""
    with patch("plugin_store.subprocess.run",
               return_value=_proc_for(200, SHA + "\n")) as run:
        assert resolve_ref("o/r", "v1.0.0") == SHA
    argv = run.call_args[0][0]
    assert argv[:3] == ["gh", "api", "-i"]
    assert argv[3] == "repos/o/r/commits/v1.0.0"
    assert argv[-2:] == ["--jq", ".sha"]


def test_resolve_ref_happy_tolerates_json_body():
    """Belt-and-braces: a full-JSON 200 body still parses (jq not applied)."""
    with patch("plugin_store.subprocess.run",
               return_value=_proc_for(200, json.dumps({"sha": SHA}))):
        assert resolve_ref("o/r", "v1.0.0") == SHA


def test_resolve_ref_422_no_commit_is_ref_not_found():
    """THE primary fix: missing tag/sha/branch → 422 'No commit found for
    SHA' → hard ref_not_found, never retryable-unavailable."""
    body = json.dumps({"message": "No commit found for SHA: v9999.9.9",
                       "documentation_url": "https://docs.github.com/rest",
                       "status": "422"})
    with patch("plugin_store.subprocess.run", return_value=_proc_for(422, body)):
        with pytest.raises(RefNotFound):
            resolve_ref("o/r", "v9999.9.9")


def test_resolve_ref_422_other_is_resolve_unavailable():
    body = json.dumps({"message": "Validation Failed", "status": "422"})
    with patch("plugin_store.subprocess.run", return_value=_proc_for(422, body)):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "weird")


def test_resolve_ref_404_is_ref_not_found():
    body = json.dumps({"message": "Not Found", "status": "404"})
    with patch("plugin_store.subprocess.run", return_value=_proc_for(404, body)):
        with pytest.raises(RefNotFound) as ei:
            resolve_ref("o/r", "phantom-tag")
    assert "not visible" in str(ei.value)          # spec wording


def test_resolve_ref_401_is_resolve_auth_failed():
    from plugin_store import ResolveAuthFailed
    body = json.dumps({"message": "Bad credentials", "status": "401"})
    with patch("plugin_store.subprocess.run", return_value=_proc_for(401, body)):
        with pytest.raises(ResolveAuthFailed):
            resolve_ref("o/r", "v1.0.0")


def test_resolve_ref_403_not_ratelimited_is_resolve_auth_failed():
    from plugin_store import ResolveAuthFailed
    body = json.dumps({"message": "Resource not accessible by integration",
                       "status": "403"})
    with patch("plugin_store.subprocess.run",
               return_value=_proc_for(403, body,
                                      {"x-ratelimit-remaining": "42"})):
        with pytest.raises(ResolveAuthFailed):
            resolve_ref("o/r", "v1.0.0")


def test_resolve_ref_409_empty_repo_is_source_empty():
    from plugin_store import SourceEmpty
    body = json.dumps({"message": "Git Repository is empty.", "status": "409"})
    with patch("plugin_store.subprocess.run", return_value=_proc_for(409, body)):
        with pytest.raises(SourceEmpty):
            resolve_ref("o/r", "main")


def test_resolve_ref_409_other_is_resolve_unavailable():
    body = json.dumps({"message": "Conflict", "status": "409"})
    with patch("plugin_store.subprocess.run", return_value=_proc_for(409, body)):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "main")


def test_resolve_ref_5xx_is_resolve_unavailable():
    with patch("plugin_store.subprocess.run",
               return_value=_proc_for(503, '{"message": "Service Unavailable"}')):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "v1")


def test_resolve_ref_no_status_line_is_resolve_unavailable():
    """Tooling failure (no HTTP response on stdout) stays retryable."""
    with patch("plugin_store.subprocess.run",
               return_value=_Proc(1, "", "error connecting to api.github.com")):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "v1")


def test_resolve_ref_timeout_is_resolve_unavailable():
    with patch("plugin_store.subprocess.run",
               side_effect=subprocess.TimeoutExpired(["gh"], 20)):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "v1")


def test_resolve_ref_missing_gh_is_resolve_unavailable():
    with patch("plugin_store.subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "v1")


def test_normalize_revision():
    from plugin_store import normalize_revision
    assert normalize_revision("git:" + "A" * 40) == "a" * 40
    assert normalize_revision("a" * 40) == "a" * 40
    assert normalize_revision(" git:" + "b" * 40 + " ") == "b" * 40
    assert normalize_revision("g" * 40) is None          # not hex
    assert normalize_revision("abc") is None
    assert normalize_revision(None) is None
    assert normalize_revision(1234) is None


def test_resolve_ref_ratelimit_403_retries_then_succeeds():
    body = json.dumps({"message": "API rate limit exceeded", "status": "403"})
    responses = [
        _proc_for(403, body, {"x-ratelimit-remaining": "0", "retry-after": "1"}),
        _proc_for(200, SHA + "\n"),
    ]
    sleeps: list[float] = []
    with patch("plugin_store.subprocess.run", side_effect=responses):
        assert resolve_ref("o/r", "v1", _sleep=sleeps.append) == SHA
    assert sleeps == [1.0]           # Retry-After honored exactly


def test_resolve_ref_429_retries():
    responses = [
        _proc_for(429, '{"message": "too many requests"}', {"retry-after": "2"}),
        _proc_for(200, SHA + "\n"),
    ]
    sleeps: list[float] = []
    with patch("plugin_store.subprocess.run", side_effect=responses):
        assert resolve_ref("o/r", "v1", _sleep=sleeps.append) == SHA
    assert sleeps == [2.0]


def test_resolve_ref_ratelimit_exhaustion_bounded_and_carries_metadata():
    body = json.dumps({"message": "API rate limit exceeded", "status": "403"})
    proc = _proc_for(403, body, {"x-ratelimit-remaining": "0", "retry-after": "1"})
    sleeps: list[float] = []
    with patch("plugin_store.subprocess.run", return_value=proc) as run:
        with pytest.raises(ResolveUnavailable) as ei:
            resolve_ref("o/r", "v1", _sleep=sleeps.append)
    assert run.call_count == 3               # <=3 attempts (C.3)
    assert len(sleeps) == 2                  # waits only BETWEEN attempts
    assert ei.value.retry_after_s == 1.0     # latest Retry-After surfaced


def test_resolve_ref_retry_after_exceeding_budget_returns_immediately():
    """A Retry-After above the 60s budget is NEVER waited or truncated:
    immediate ResolveUnavailable carrying the server's requested delay."""
    body = json.dumps({"message": "API rate limit exceeded", "status": "403"})
    proc = _proc_for(403, body, {"x-ratelimit-remaining": "0",
                                 "retry-after": "3600"})
    sleeps: list[float] = []
    with patch("plugin_store.subprocess.run", return_value=proc) as run:
        with pytest.raises(ResolveUnavailable) as ei:
            resolve_ref("o/r", "v1", _sleep=sleeps.append)
    assert run.call_count == 1 and sleeps == []
    assert ei.value.retry_after_s == 3600.0


def test_resolve_ref_cumulative_budget_never_exceeded():
    """r2-B5: each delay individually < 60s but the SUM would exceed the 60s
    TOTAL budget — sleep only the first (40s), stop before the second, and
    surface the un-waited delay as retry metadata."""
    body = json.dumps({"message": "API rate limit exceeded", "status": "403"})
    responses = [
        _proc_for(403, body, {"x-ratelimit-remaining": "0", "retry-after": "40"}),
        _proc_for(403, body, {"x-ratelimit-remaining": "0", "retry-after": "30"}),
    ]
    sleeps: list[float] = []
    with patch("plugin_store.subprocess.run", side_effect=responses) as run:
        with pytest.raises(ResolveUnavailable) as ei:
            resolve_ref("o/r", "v1", _sleep=sleeps.append)
    assert run.call_count == 2               # 2nd response seen, 3rd never tried
    assert sleeps == [40.0]                  # 40+30 > 60 → second wait refused
    assert ei.value.retry_after_s == 30.0    # the refused delay is surfaced


def test_resolve_ref_secondary_ratelimit_recognized_by_body_only():
    """C.3: headers inconclusive -> body text recognizes a secondary limit."""
    body = json.dumps({"message":
                       "You have exceeded a secondary rate limit. "
                       "Please wait a few minutes before you try again."})
    responses = [
        _proc_for(403, body),        # NO rate-limit headers at all
        _proc_for(200, SHA + "\n"),
    ]
    sleeps: list[float] = []
    with patch("plugin_store.subprocess.run", side_effect=responses):
        assert resolve_ref("o/r", "v1", _sleep=sleeps.append) == SHA
    assert sleeps == [2.0]           # default backoff (no Retry-After)


def test_resolve_ref_non_ratelimit_transient_does_not_retry_in_function():
    """5xx is a retryable VERDICT for the caller, not an in-function loop."""
    with patch("plugin_store.subprocess.run",
               return_value=_proc_for(502, '{"message": "Bad gateway"}')) as run:
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "v1", _sleep=lambda s: (_ for _ in ()).throw(
                AssertionError("must not sleep")))
    assert run.call_count == 1


def test_publish_with_precommitted_sha_skips_resolve(tmp_path):
    """C.2: the identity guards resolve ONCE; publish(commit=) must not
    re-resolve (a tag moving between resolve and fetch would be a TOCTOU)."""
    import shutil
    src = _plugin_tree(tmp_path)

    def _no_resolve(*a, **k):
        raise AssertionError("resolve_ref must not be called")

    with patch("plugin_store.resolve_ref", side_effect=_no_resolve), \
         patch("plugin_store.fetch_commit_tree",
               side_effect=lambda repo, commit, subdir, dest, **k:
               shutil.copytree(src, dest, dirs_exist_ok=True)) as fct:
        res = publish(name="probe", repo="o/r", ref="v1.0.0",
                      store_root=tmp_path / "store",
                      staging_root=tmp_path / "staging", commit=SHA)
    assert res.revision == f"git:{SHA}"
    assert fct.call_args[0][1] == SHA


def test_validate_manifest_paths(tmp_path):
    root = _plugin_tree(tmp_path)
    assert validate_manifest(root, "probe")["version"] == "1.0.0"
    with pytest.raises(StoreError) as ei:
        validate_manifest(root, "other-name")
    assert ei.value.reason_code == "name_mismatch"
    (root / ".claude-plugin" / "plugin.json").write_text("{broken",
                                                         encoding="utf-8")
    with pytest.raises(StoreError) as ei:
        validate_manifest(root, "probe")
    assert ei.value.reason_code == "manifest_invalid"


def test_validate_manifest_defaults_missing_version(tmp_path):
    """CI/real-world: plugins like anthropics/claude-plugins-official ship NO
    top-level version. validate_manifest must default it (0.0.0), not reject —
    version is no longer identity-load-bearing. The unit gate's versioned
    fixtures masked this; only the image build caught it."""
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "probe"}), encoding="utf-8")   # no version
    assert validate_manifest(root, "probe")["version"] == "0.0.0"


def test_validate_manifest_tolerates_non_object_casa(tmp_path):
    root = _plugin_tree(tmp_path)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": "probe", "version": "1.0.0", "casa": "oops"}),
        encoding="utf-8")
    assert validate_manifest(root, "probe")["version"] == "1.0.0"


def test_validate_manifest_rejects_apt(tmp_path):
    root = _plugin_tree(tmp_path)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "probe", "version": "1.0.0",
        "casa": {"systemRequirements": [{"type": "apt", "package": "x"}]},
    }), encoding="utf-8")
    with pytest.raises(StoreError) as ei:
        validate_manifest(root, "probe")
    assert ei.value.reason_code == "apt_requirements_rejected"


# --- #354: casa.systemRequirements is guarded + STRICT -----------------------


def test_manifest_sysreqs_absent_is_empty():
    from plugin_store import manifest_sysreqs
    assert manifest_sysreqs({"name": "p"}) == []
    assert manifest_sysreqs({"name": "p", "casa": {}}) == []
    assert manifest_sysreqs({"name": "p", "casa": "oops"}) == []
    assert manifest_sysreqs({}) == []


@pytest.mark.parametrize("bad_value", [
    {"type": "npm", "package": "x"},   # object where a list is expected
    "npm",                             # scalar
    [{"type": "npm"}, None],           # non-object member
    [["npm"]],                         # list member
    [{"type": "npm"}, "x"],            # string member
])
def test_manifest_sysreqs_malformed_raises(bad_value):
    """#354: a PRESENT-but-malformed declaration must raise, not silently
    become "no requirements" (pre-fix the plugin activated with no install
    and its MCP server failed at runtime)."""
    from plugin_store import manifest_sysreqs
    manifest = {"name": "p", "casa": {"systemRequirements": bad_value}}
    with pytest.raises(StoreError) as ei:
        manifest_sysreqs(manifest)
    assert ei.value.reason_code == "system_requirements_invalid"


def test_validate_manifest_rejects_malformed_sysreqs(tmp_path):
    """#354: install/update of a plugin whose casa.systemRequirements is an
    object (not a list) is refused instead of proceeding requirement-less."""
    root = _plugin_tree(tmp_path)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "probe", "version": "1.0.0",
        "casa": {"systemRequirements": {"type": "npm", "package": "x"}},
    }), encoding="utf-8")
    with pytest.raises(StoreError) as ei:
        validate_manifest(root, "probe")
    assert ei.value.reason_code == "system_requirements_invalid"


def test_manifest_sysreqs_empty_list_is_valid():
    from plugin_store import manifest_sysreqs
    assert manifest_sysreqs(
        {"name": "p", "casa": {"systemRequirements": []}}) == []


@pytest.mark.parametrize("bad_req", [
    {"type": "tarball", "url": "https://x/y.tgz", "sha256": "0" * 64},  # absent
    {"type": "tarball", "url": "u", "sha256": "s", "verify_bin": ""},
    {"type": "tarball", "url": "u", "sha256": "s", "verify_bin": "../escape"},
    {"type": "npm", "package": "p", "verify_bin": "a/b"},
    {"type": "venv", "package": "p", "verify_bin": ".hidden"},
    {"type": "npm", "package": "p", "verify_bin": ["x"]},
])
def test_manifest_sysreqs_requires_safe_verify_bin(bad_req):
    """#354 (review round 2): a requirement without a safe verify_bin can
    never succeed (every strategy gates on the launcher resolving) and the
    name is joined into tools/bin/<name> — refuse at manifest level instead
    of running a whole install that ends in failure."""
    from plugin_store import manifest_sysreqs
    manifest = {"name": "p", "casa": {"systemRequirements": [bad_req]}}
    with pytest.raises(StoreError) as ei:
        manifest_sysreqs(manifest)
    assert ei.value.reason_code == "system_requirements_invalid"


def test_manifest_sysreqs_rejected_types_keep_their_own_kind():
    """The apt/dpkg/... refusal must keep its dedicated reason code — the
    verify_bin strictness skips package-manager rows so validate_manifest
    still raises apt_requirements_rejected for them (pinned above in
    test_validate_manifest_rejects_apt)."""
    from plugin_store import manifest_sysreqs
    # No verify_bin on an apt row: passes extraction; refusal happens in
    # validate_manifest with its own kind.
    reqs = manifest_sysreqs({"name": "p", "casa": {"systemRequirements": [
        {"type": "apt", "package": "x"}]}})
    assert reqs == [{"type": "apt", "package": "x"}]


# --- A:§3.7 casa.protectedTools (v0.76.0) ------------------------------------


def test_manifest_protected_tools_absent_is_empty():
    from plugin_store import manifest_protected_tools
    assert manifest_protected_tools({"name": "p", "version": "1.0.0"}) == []
    assert manifest_protected_tools({"name": "p", "casa": {}}) == []
    # Sol R2-3-style tolerance: a non-object casa degrades to [], not a raise.
    assert manifest_protected_tools({"name": "p", "casa": "oops"}) == []
    assert manifest_protected_tools({}) == []


def test_manifest_protected_tools_present_valid():
    """Legacy string form normalizes to {"name", "summary": None}."""
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": ["invoice_reset"]}}
    assert manifest_protected_tools(manifest) == [
        {"name": "invoice_reset", "summary": None}]


def test_manifest_protected_tools_present_empty_list_is_valid():
    """The key PRESENT but an empty list is a valid (vacuous) list of
    non-empty strings — not malformed."""
    from plugin_store import manifest_protected_tools
    assert manifest_protected_tools(
        {"name": "p", "casa": {"protectedTools": []}}) == []


@pytest.mark.parametrize("bad_value", [
    "invoice_reset",                 # str, not a list
    ["invoice_reset", ""],           # list-with-empty-string
    ["invoice_reset", 1],            # list-with-int
    {"invoice_reset": True},         # dict, not a list
])
def test_manifest_protected_tools_malformed_shapes_raise(bad_value):
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": bad_value}}
    with pytest.raises(StoreError) as ei:
        manifest_protected_tools(manifest)
    assert ei.value.reason_code == "protected_tools_invalid"


# --- v0.78.0 W1: object-form entries with summaries --------------------------


def test_manifest_protected_tools_object_entry_normalization():
    """An object entry with a summary normalizes to {"name", "summary"}."""
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [
        {"name": "invoice_reset", "summary": "Delete the draft for {period}"},
    ]}}
    assert manifest_protected_tools(manifest) == [
        {"name": "invoice_reset", "summary": "Delete the draft for {period}"}]


def test_manifest_protected_tools_object_entry_without_summary():
    """Object form WITHOUT a summary key normalizes summary to None, same as
    the legacy string form."""
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [
        {"name": "invoice_reset"},
    ]}}
    assert manifest_protected_tools(manifest) == [
        {"name": "invoice_reset", "summary": None}]


def test_manifest_protected_tools_mixed_string_and_object_entries():
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [
        "legacy_tool",
        {"name": "new_tool", "summary": "Does the new thing"},
    ]}}
    assert manifest_protected_tools(manifest) == [
        {"name": "legacy_tool", "summary": None},
        {"name": "new_tool", "summary": "Does the new thing"},
    ]


@pytest.mark.parametrize("bad_entry", [
    123,                                          # wrong type (not str/dict)
    None,                                         # wrong type
    [],                                           # wrong type (list)
    {"summary": "no name"},                       # missing name
    {"name": ""},                                 # empty name
    {"name": "t", "summary": ""},                 # empty summary
    {"name": "t", "summary": 5},                  # non-string summary
    {"name": "t", "summary": "x" * 201},          # oversized summary (>200)
    {"name": "t", "summary": "line one\nline two"},   # multiline (C0 \n)
    {"name": "t", "summary": "bad\x01char"},           # C0 control
    {"name": "t", "summary": "bad" + chr(0x9c) + "char"},  # C1 control
    {"name": "t", "summary": "line" + chr(0x2028) + "sep"},  # U+2028
    {"name": "t", "summary": "bidi" + chr(0x200e) + "mark"},  # U+200E LRM
    {"name": "t", "summary": "bidi" + chr(0x202a) + "embed"},  # U+202A
    {"name": "t", "unknown_key": "x"},            # unknown key
    {"name": "t", "summary": "ok", "extra": 1},   # unknown key alongside valid
])
def test_manifest_protected_tools_object_shapes_refuse(bad_entry):
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [bad_entry]}}
    with pytest.raises(StoreError) as ei:
        manifest_protected_tools(manifest)
    assert ei.value.reason_code == "protected_tools_invalid"


def test_manifest_protected_tools_object_summary_exactly_200_chars_ok():
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [
        {"name": "t", "summary": "x" * 200},
    ]}}
    assert manifest_protected_tools(manifest) == [
        {"name": "t", "summary": "x" * 200}]


# --- v0.78.0 W1: duplicate names after sanitize_segment ----------------------


def test_manifest_protected_tools_duplicate_string_string_refuses():
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": ["a_tool", "a_tool"]}}
    with pytest.raises(StoreError) as ei:
        manifest_protected_tools(manifest)
    assert ei.value.reason_code == "protected_tools_invalid"


def test_manifest_protected_tools_duplicate_object_object_refuses():
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [
        {"name": "a_tool", "summary": "first"},
        {"name": "a_tool", "summary": "second"},
    ]}}
    with pytest.raises(StoreError) as ei:
        manifest_protected_tools(manifest)
    assert ei.value.reason_code == "protected_tools_invalid"


def test_manifest_protected_tools_duplicate_mixed_string_object_refuses():
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [
        "a_tool",
        {"name": "a_tool", "summary": "obj form"},
    ]}}
    with pytest.raises(StoreError) as ei:
        manifest_protected_tools(manifest)
    assert ei.value.reason_code == "protected_tools_invalid"


def test_manifest_protected_tools_sanitized_collision_duplicate_refuses():
    """r3-1: 'do thing' and 'do_thing' sanitize to the same runtime tool id —
    no order-dependent last-wins summary semantics."""
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [
        "do thing", "do_thing",
    ]}}
    with pytest.raises(StoreError) as ei:
        manifest_protected_tools(manifest)
    assert ei.value.reason_code == "protected_tools_invalid"


def test_validate_manifest_accepts_absent_protected_tools(tmp_path):
    root = _plugin_tree(tmp_path)
    assert validate_manifest(root, "probe")["version"] == "1.0.0"


def test_validate_manifest_accepts_valid_protected_tools(tmp_path):
    root = _plugin_tree(tmp_path)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "probe", "version": "1.0.0",
        "casa": {"protectedTools": ["invoice_reset"]},
    }), encoding="utf-8")
    assert validate_manifest(root, "probe")["version"] == "1.0.0"


@pytest.mark.parametrize("bad_value", [
    "invoice_reset", ["invoice_reset", ""], ["invoice_reset", 1],
    {"invoice_reset": True},
])
def test_validate_manifest_rejects_malformed_protected_tools(tmp_path, bad_value):
    """A PRESENT but malformed casa.protectedTools FAILS validate_manifest —
    install/update refused (A:§3.7 B7 strict validator)."""
    root = _plugin_tree(tmp_path)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "probe", "version": "1.0.0",
        "casa": {"protectedTools": bad_value},
    }), encoding="utf-8")
    with pytest.raises(StoreError) as ei:
        validate_manifest(root, "probe")
    assert ei.value.reason_code == "protected_tools_invalid"


def test_artifact_verdict_flags_stored_malformed_protected_tools(tmp_path):
    """A:§3.7 (r2-B6/r3-4): a PRE-EXISTING stored artifact whose manifest is
    re-checked by artifact_verdict() (the shared strict extension runs
    inside deep validation too) yields protected_tools_invalid — excluding
    it from resolution, never a whole-role failure."""
    from plugin_store import artifact_verdict, content_checksum, write_metadata
    root = tmp_path / "art"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "probe", "version": "1.0.0",
        "casa": {"protectedTools": ["invoice_reset", 1]},   # malformed
    }), encoding="utf-8")
    write_metadata(root, name="probe", repo="o/r", ref="v1",
                   revision="git:" + SHA, subdir="", artifact_id="a" * 64,
                   version="1.0.0", checksum=content_checksum(root))
    verdict = artifact_verdict(root, name="probe", repo="o/r",
                               revision="git:" + SHA, subdir="",
                               artifact_id="a" * 64)
    assert verdict == "protected_tools_invalid"


def test_artifact_verdict_absent_protected_tools_is_valid(tmp_path):
    from plugin_store import artifact_verdict, content_checksum, write_metadata
    root = tmp_path / "art"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "probe", "version": "1.0.0"}), encoding="utf-8")
    write_metadata(root, name="probe", repo="o/r", ref="v1",
                   revision="git:" + SHA, subdir="", artifact_id="a" * 64,
                   version="1.0.0", checksum=content_checksum(root))
    verdict = artifact_verdict(root, name="probe", repo="o/r",
                               revision="git:" + SHA, subdir="",
                               artifact_id="a" * 64)
    assert verdict is None


def _wire_fetch(src_root):
    """publish() fetches into staging: fake fetch_commit_tree by copying."""
    import shutil

    def _fake(repo, commit, subdir, dest, **kw):
        shutil.copytree(src_root, dest, dirs_exist_ok=True, symlinks=True)
    return _fake


def test_publish_happy_atomic(tmp_path):
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        res = publish(name="probe", repo="o/r", ref="v1",
                      store_root=store, staging_root=staging)
    assert res.revision == f"git:{SHA}"
    assert res.version == "1.0.0"
    dest = store / "probe" / res.artifact_id
    assert Path(res.path) == dest and validate_artifact(dest)
    assert not any(staging.iterdir())          # staging cleaned


def test_publish_existing_valid_is_noop(tmp_path):
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        r1 = publish(name="probe", repo="o/r", ref="v1",
                     store_root=store, staging_root=staging)
        r2 = publish(name="probe", repo="o/r", ref="v1",
                     store_root=store, staging_root=staging)
    assert r1.artifact_id == r2.artifact_id


def test_publish_existing_corrupt_fails_closed(tmp_path):
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        r1 = publish(name="probe", repo="o/r", ref="v1",
                     store_root=store, staging_root=staging)
        # Tamper the published artifact (defeat the Sol #7 freeze to model
        # corruption that bypassed it — the verdict backstop must still catch it).
        _unfreeze(Path(r1.path) / "skills" / "s.md")
        (Path(r1.path) / "skills" / "s.md").write_text("evil", encoding="utf-8")
        with pytest.raises(StoreError) as ei:
            publish(name="probe", repo="o/r", ref="v1",
                    store_root=store, staging_root=staging)
    assert ei.value.reason_code == "corrupt_artifact"
    # Nothing swapped: tampered content still in place (operator/GC recovers).
    assert (Path(r1.path) / "skills" / "s.md").read_text(
        encoding="utf-8") == "evil"


def test_publish_existing_wrong_identity_metadata_fails_closed(tmp_path):
    """A destination whose checksum self-validates but whose metadata names a
    DIFFERENT identity is corrupt — never silently accepted."""
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        r1 = publish(name="probe", repo="o/r", ref="v1",
                     store_root=store, staging_root=staging)
        meta_path = Path(r1.path) / METADATA_FILENAME
        _unfreeze(meta_path)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["revision"] = "git:" + "b" * 40      # wrong identity
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        # re-fix the content checksum so ONLY identity is wrong
        meta["content_checksum"] = content_checksum(Path(r1.path))
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        with pytest.raises(StoreError) as ei:
            publish(name="probe", repo="o/r", ref="v1",
                    store_root=store, staging_root=staging)
    assert ei.value.reason_code == "corrupt_artifact"


def test_publish_failure_cleans_staging_store_unchanged(tmp_path):
    src = _plugin_tree(tmp_path, name="WRONG")  # name mismatch → validate fails
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        with pytest.raises(StoreError):
            publish(name="probe", repo="o/r", ref="v1",
                    store_root=store, staging_root=staging)
    assert not (store / "probe").exists()
    assert not staging.exists() or not any(staging.iterdir())


def test_publish_from_tree_excludes_git_and_uses_given_revision(tmp_path):
    src = _plugin_tree(tmp_path)
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: x", encoding="utf-8")
    store, staging = tmp_path / "store", tmp_path / "staging"
    rev = "legacy-content:" + "c" * 64
    res = publish_from_tree(name="probe", repo="o/r", ref="master",
                            revision=rev, subdir="", src_root=src,
                            store_root=store, staging_root=staging)
    assert res.revision == rev
    assert not (Path(res.path) / ".git").exists()
    expected = compute_artifact_id(repo="o/r", revision=rev, subdir="",
                                   name="probe")
    assert res.artifact_id == expected


def test_import_bundle_idempotent_and_fail_closed(tmp_path):
    src = _plugin_tree(tmp_path)
    bundle, store = tmp_path / "bundle", tmp_path / "store"
    res = publish_from_tree(name="probe", repo="o/r", ref="v1",
                            revision=f"git:{SHA}", subdir="", src_root=src,
                            store_root=bundle, staging_root=tmp_path / "stg")
    issues = import_bundle(bundle, store_root=store)
    assert issues == []
    dest = store / "probe" / res.artifact_id
    assert validate_artifact(dest)
    assert import_bundle(bundle, store_root=store) == []   # idempotent
    # Corrupt the store copy → issue raised, NOT silently replaced.
    _unfreeze(dest / "skills" / "s.md")
    (dest / "skills" / "s.md").write_text("evil", encoding="utf-8")
    issues = import_bundle(bundle, store_root=store)
    assert [i.reason_code for i in issues] == ["corrupt_artifact"]


def test_publish_freezes_artifact_files_readonly(tmp_path):
    """Sol #7: a published artifact's files are read-only (no write bit for any
    class) so in-place tampering can't defeat the cached deep-validation."""
    import os
    import stat
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        r = publish(name="probe", repo="o/r", ref="v1",
                    store_root=store, staging_root=staging)
    skill = Path(r.path) / "skills" / "s.md"
    mode = stat.S_IMODE(os.lstat(skill).st_mode)
    assert mode & 0o222 == 0, f"artifact file still writable: {oct(mode)}"
    # verify_bin backstop still readable (deep validation must pass).
    from plugin_store import validate_artifact
    assert validate_artifact(Path(r.path))


def test_gc_disabled_returns_candidates_without_deleting(tmp_path):
    src = _plugin_tree(tmp_path)
    store = tmp_path / "store"
    res = publish_from_tree(name="probe", repo="o/r", ref="v1",
                            revision=f"git:{SHA}", subdir="", src_root=src,
                            store_root=store, staging_root=tmp_path / "stg")
    cands = plugin_store.gc_sweep(store_root=store, referenced=set(),
                                  min_age_days=0, enabled=False)
    assert cands == [res.artifact_id] and Path(res.path).exists()


def test_publish_from_tree_rejects_escaping_symlink(tmp_path):
    """Sol round-3 H7: an offline-adopt tree with a symlink escaping the artifact
    root is rejected (unsafe_archive) — freezing/loading it must never touch or
    expose an external file."""
    import os
    src = _plugin_tree(tmp_path)
    os.symlink("/etc/passwd", src / "evil-link")      # escaping absolute symlink
    store, staging = tmp_path / "store", tmp_path / "staging"
    with pytest.raises(StoreError) as ei:
        publish_from_tree(name="probe", repo="o/r", ref="master",
                          revision="legacy-content:" + "c" * 64, subdir="",
                          src_root=src, store_root=store, staging_root=staging)
    assert ei.value.reason_code == "unsafe_archive"
    assert not (store / "probe").exists()             # nothing published


def test_publish_from_tree_allows_internal_symlink(tmp_path):
    """Sol round-3 H7: an in-artifact symlink (non-escaping) is allowed; freeze
    skips it without chmod-following."""
    import os
    src = _plugin_tree(tmp_path)
    (src / "skills" / "target.md").write_text("t", encoding="utf-8")
    os.symlink("target.md", src / "skills" / "link.md")   # internal, relative
    store, staging = tmp_path / "store", tmp_path / "staging"
    res = publish_from_tree(name="probe", repo="o/r", ref="master",
                            revision="legacy-content:" + "c" * 64, subdir="",
                            src_root=src, store_root=store, staging_root=staging)
    assert (Path(res.path) / "skills" / "link.md").is_symlink()  # preserved


def test_import_bundle_freezes_files(tmp_path):
    """Sol round-3 H7: imported bundle artifacts are frozen read-only too."""
    import os
    import stat
    src = _plugin_tree(tmp_path)
    bundle, store = tmp_path / "bundle", tmp_path / "store"
    res = publish_from_tree(name="probe", repo="o/r", ref="v1",
                            revision=f"git:{SHA}", subdir="", src_root=src,
                            store_root=bundle, staging_root=tmp_path / "stg")
    import_bundle(bundle, store_root=store)
    skill = store / "probe" / res.artifact_id / "skills" / "s.md"
    assert stat.S_IMODE(os.lstat(skill).st_mode) & 0o222 == 0


def test_publish_rejects_cyclic_symlink(tmp_path):
    """Sol round-4: a symlink LOOP raises unsafe_archive (RuntimeError from
    resolve() translated), not an uncaught error."""
    import os
    src = _plugin_tree(tmp_path)
    os.symlink("b", src / "a")           # a -> b
    os.symlink("a", src / "b")           # b -> a  (cycle)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with pytest.raises(StoreError) as ei:
        publish_from_tree(name="probe", repo="o/r", ref="master",
                          revision="legacy-content:" + "c" * 64, subdir="",
                          src_root=src, store_root=store, staging_root=staging)
    assert ei.value.reason_code == "unsafe_archive"


# --- G7 (v0.95.1, Sol-corrected): bytecode stays checksum-visible ----------


def _mk_minimal_artifact(tmp_path):
    import json as _json
    from plugin_store import content_checksum, write_metadata
    root = tmp_path / "artifact"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        _json.dumps({"name": "p", "version": "1.0.0"}), encoding="utf-8")
    (root / "server").mkdir()
    (root / "server" / "server.py").write_text("print('x')\n", encoding="utf-8")
    write_metadata(root, name="p", repo="o/r", ref="v1",
                   revision="git:" + "a" * 40, subdir="",
                   artifact_id="a" * 64, version="1.0.0",
                   checksum=content_checksum(root))
    return root


def _poison(root):
    pc = root / "server" / "__pycache__"
    pc.mkdir()
    (pc / "server.cpython-311.pyc").write_bytes(b"\x00fakebytecode")
    (root / "server" / "stray.pyc").write_bytes(b"\x00loose")


def _verdict(root):
    from plugin_store import artifact_verdict
    return artifact_verdict(root, name="p", repo="o/r",
                            revision="git:" + "a" * 40, subdir="",
                            artifact_id="a" * 64)


def test_bytecode_in_artifact_still_corrupts(tmp_path):
    """Checksums stay STRICT (a crafted header-valid .pyc shadows its .py at
    import time) — bytecode inside an artifact IS drift."""
    root = _mk_minimal_artifact(tmp_path)
    _poison(root)
    assert _verdict(root) == "corrupt_artifact"


def test_heal_removes_bytecode_only_drift(tmp_path):
    from plugin_store import heal_bytecode_poisoned_artifact
    root = _mk_minimal_artifact(tmp_path)
    _poison(root)
    assert heal_bytecode_poisoned_artifact(root) is True
    assert _verdict(root) is None
    assert not (root / "server" / "__pycache__").exists()


def test_heal_refuses_real_tampering(tmp_path):
    """Bytecode present AND a source modified: healing must refuse — the
    tree is tampered beyond the interpreter cache."""
    from plugin_store import heal_bytecode_poisoned_artifact
    root = _mk_minimal_artifact(tmp_path)
    _poison(root)
    (root / "server" / "server.py").chmod(0o644)
    (root / "server" / "server.py").write_text("print('evil')\n",
                                               encoding="utf-8")
    assert heal_bytecode_poisoned_artifact(root) is False
    assert _verdict(root) == "corrupt_artifact"


def test_heal_noop_on_clean_artifact(tmp_path):
    from plugin_store import heal_bytecode_poisoned_artifact
    root = _mk_minimal_artifact(tmp_path)
    assert heal_bytecode_poisoned_artifact(root) is False


def test_strip_bytecode_derivatives(tmp_path):
    from plugin_store import strip_bytecode_derivatives
    root = _mk_minimal_artifact(tmp_path)
    _poison(root)
    assert strip_bytecode_derivatives(root) >= 2
    assert not list(root.rglob("*.pyc"))


def test_foreign_file_still_corrupts_artifact(tmp_path):
    root = _mk_minimal_artifact(tmp_path)
    (root / "evil.md").write_text("tampered", encoding="utf-8")
    assert _verdict(root) == "corrupt_artifact"


def test_freeze_strips_directory_write_bits(tmp_path):
    import stat as _stat
    from plugin_store import _freeze_artifact_files
    root = _mk_minimal_artifact(tmp_path)
    _freeze_artifact_files(root)
    for d in (root, root / "server", root / ".claude-plugin"):
        assert _stat.S_IMODE(d.stat().st_mode) & 0o222 == 0, d


def test_boot_sweep_refreezes_legacy_artifacts(tmp_path):
    """Sol v0951b-2: a clean pre-v0.95.1 artifact (writable dirs) must come
    out of the ACTUAL boot sweep frozen even though nothing needed healing."""
    import logging
    import stat as _stat
    from plugin_boot import heal_and_freeze_store
    store = tmp_path / "store" / "p"
    store.mkdir(parents=True)
    root = _mk_minimal_artifact(tmp_path)
    dest = store / ("a" * 64)
    root.rename(dest)
    heal_and_freeze_store(tmp_path / "store", logging.getLogger("t"))
    assert _stat.S_IMODE(dest.stat().st_mode) & 0o222 == 0
    assert _stat.S_IMODE((dest / "server").stat().st_mode) & 0o222 == 0


def test_boot_sweep_skips_symlinked_artifact_root(tmp_path):
    """Sol v0951c-1: a symlinked store entry must not heal/freeze the
    EXTERNAL target tree."""
    import logging
    import stat as _stat
    from plugin_boot import heal_and_freeze_store
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    outside.chmod(0o777)
    (outside / "sub").chmod(0o777)
    store = tmp_path / "store" / "p"
    store.mkdir(parents=True)
    (store / ("b" * 64)).symlink_to(outside)
    heal_and_freeze_store(tmp_path / "store", logging.getLogger("t"))
    assert _stat.S_IMODE(outside.stat().st_mode) & 0o222 != 0
    assert _stat.S_IMODE((outside / "sub").stat().st_mode) & 0o222 != 0


def test_boot_sweep_skips_symlinked_name_dir(tmp_path):
    """Sol v0951d-1: a symlinked PLUGIN-NAME (first-level) dir must not be
    descended — glob-style traversal would freeze the external tree."""
    import logging
    import stat as _stat
    from plugin_boot import heal_and_freeze_store
    outside = tmp_path / "outside"
    (outside / ("c" * 64)).mkdir(parents=True)
    outside.chmod(0o777)
    (outside / ("c" * 64)).chmod(0o777)
    store = tmp_path / "store"
    store.mkdir()
    (store / "p").symlink_to(outside)
    heal_and_freeze_store(store, logging.getLogger("t"))
    assert _stat.S_IMODE(outside.stat().st_mode) & 0o222 != 0
    assert _stat.S_IMODE((outside / ("c" * 64)).stat().st_mode) & 0o222 != 0


def test_freeze_refuses_symlinked_root(tmp_path):
    import stat as _stat
    from plugin_store import _freeze_artifact_files
    outside = tmp_path / "target"
    outside.mkdir()
    outside.chmod(0o777)
    link = tmp_path / "link"
    link.symlink_to(outside)
    _freeze_artifact_files(link)
    assert _stat.S_IMODE(outside.stat().st_mode) & 0o222 != 0


def test_frozen_artifact_is_world_readable_and_traversable(tmp_path):
    """Containment stage 2, Task 11: a dropped-uid engagement CLI process
    (`--plugin-dir`) must be able to traverse+read an artifact it does not
    own. Freezing a 0700 dir / 0600 file must grant o+rx / o+r respectively
    (write bits still cleared) while preserving any exec bit the file
    already carried."""
    import stat as _stat
    from plugin_store import _freeze_artifact_files

    root = tmp_path / "artifact"
    (root / "sub").mkdir(parents=True)
    root.chmod(0o700)
    (root / "sub").chmod(0o700)
    plain = root / "sub" / "plain.md"
    plain.write_text("x", encoding="utf-8")
    plain.chmod(0o600)
    script = root / "sub" / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o700)          # owner-executable script

    _freeze_artifact_files(root)

    for d in (root, root / "sub"):
        mode = _stat.S_IMODE(d.stat().st_mode)
        assert mode & 0o222 == 0, f"{d} still writable: {oct(mode)}"
        assert mode & 0o055 == 0o055, f"{d} not o+rx/g+rx: {oct(mode)}"

    plain_mode = _stat.S_IMODE(plain.stat().st_mode)
    assert plain_mode & 0o222 == 0
    assert plain_mode & 0o044 == 0o044, f"file not o+r/g+r: {oct(plain_mode)}"
    assert plain_mode & 0o111 == 0, "read-only file must not gain exec bits"

    script_mode = _stat.S_IMODE(script.stat().st_mode)
    assert script_mode & 0o222 == 0
    assert script_mode & 0o044 == 0o044
    assert script_mode & 0o100, "owner exec bit must survive the freeze"


def test_publish_leaves_parent_chain_world_traversable(tmp_path):
    """Task 11: the preflight checks a --plugin-dir's OWN mode, but a
    dropped uid must also be able to TRAVERSE the plugin-store root and the
    plugin-name dir above it. publish() must leave the whole chain o+x."""
    import os as _os
    import stat as _stat
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    # Force a restrictive umask so mkdir's default mode would NOT already
    # grant o+x — the assertions below only prove something if the code
    # under test had to actively widen the mode.
    old_umask = _os.umask(0o077)
    try:
        with patch("plugin_store.resolve_ref", return_value=SHA), \
             patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
            r = publish(name="probe", repo="o/r", ref="v1",
                        store_root=store, staging_root=staging)
    finally:
        _os.umask(old_umask)
    dest = Path(r.path)
    # store_root and store_root/name must both grant o+x (traversal) even
    # though this test never widened their default creation mode.
    for d in (store, store / "probe"):
        mode = _stat.S_IMODE(d.stat().st_mode)
        assert mode & 0o001, f"{d} not world-traversable: {oct(mode)}"
    assert _stat.S_IMODE(dest.stat().st_mode) & 0o001


def test_s6_exports_pycache_prefix():
    from pathlib import Path as _P
    text = (_P(__file__).resolve().parent.parent / "casa" / "rootfs"
            / "etc" / "s6-overlay" / "scripts"
            / "setup-configs.sh").read_text(encoding="utf-8")
    assert "/run/s6/container_environment/PYTHONPYCACHEPREFIX" in text


def test_artifact_verdict_rejects_symlinked_path(tmp_path):
    """Sol v0951e-1: a symlinked artifact (or name-level) path must never
    validate — an external writable tree would be cached as immutable."""
    root = _mk_minimal_artifact(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(root)
    from plugin_store import artifact_verdict
    assert artifact_verdict(link, name="p", repo="o/r",
                            revision="git:" + "a" * 40, subdir="",
                            artifact_id="a" * 64) == "artifact_invalid"


def test_artifact_verdict_rejects_symlinked_parent(tmp_path):
    real_parent = tmp_path / "real-name-dir"
    real_parent.mkdir()
    root = _mk_minimal_artifact(tmp_path)
    dest = real_parent / ("a" * 64)
    root.rename(dest)
    linked_parent = tmp_path / "linked-name-dir"
    linked_parent.symlink_to(real_parent)
    from plugin_store import artifact_verdict
    assert artifact_verdict(linked_parent / ("a" * 64), name="p", repo="o/r",
                            revision="git:" + "a" * 40, subdir="",
                            artifact_id="a" * 64) == "artifact_invalid"


def _tree_with_casa(tmp_path, name, casa):
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "casa": casa}),
        encoding="utf-8")
    (root / "skills").mkdir()
    (root / "skills" / "s.md").write_text("s", encoding="utf-8")
    return root


class TestManifestTriggers:
    """Release B: casa.triggers is validated at publish/install time."""

    def test_validate_manifest_accepts_valid_triggers(self, tmp_path):
        root = _tree_with_casa(tmp_path, "elevenlabs", {"triggers": [
            {"name": "voicemail", "type": "webhook",
             "target": "resident:assistant", "auth": {"mode": "static_header"}}]})
        mf = validate_manifest(root, "elevenlabs")  # no raise
        assert mf["name"] == "elevenlabs"

    def test_validate_manifest_rejects_bad_target(self, tmp_path):
        root = _tree_with_casa(tmp_path, "p", {"triggers": [
            {"name": "x", "type": "webhook", "target": "specialist:finance",
             "auth": {"mode": "static_header"}}]})
        with pytest.raises(StoreError) as ei:
            validate_manifest(root, "p")
        assert ei.value.reason_code == "triggers_invalid"

    def test_validate_manifest_rejects_provider_owner(self, tmp_path):
        root = _tree_with_casa(tmp_path, "p", {"triggers": [
            {"name": "x", "type": "webhook", "target": "resident:assistant",
             "auth": {"mode": "timestamped_hmac", "secret_owner": "provider"}}]})
        with pytest.raises(StoreError) as ei:
            validate_manifest(root, "p")
        assert ei.value.reason_code == "triggers_invalid"

    def test_absent_triggers_ok(self, tmp_path):
        root = _tree_with_casa(tmp_path, "p", {})
        validate_manifest(root, "p")  # no raise

    def test_manifest_triggers_helper_uses_plugin_name(self):
        from plugin_store import manifest_triggers
        manifest = {"casa": {"triggers": [
            {"name": "vm", "type": "webhook", "target": "resident:assistant",
             "auth": {"mode": "static_header"}}]}}
        trigs = manifest_triggers(manifest, "el")
        assert trigs[0]["effective"] == "plg-el--vm"
