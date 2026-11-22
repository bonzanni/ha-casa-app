"""``resolve_latest_release``: "latest" is a published release tag or nothing.

Design 2026-09-15 §2.C: the configurator has no web tool and its brief said
"resolve 'latest' yourself"; on the production box (2026-09-15) it spent 90 s failing
and then installed ``main``. Now ``plugin_add(ref="latest")`` resolves
server-side: GitHub's latest release, accepted only when its name is a release
tag (``v<semver>``); else the highest release tag by numeric order; else
``no_release_found`` — never a branch. The chosen name is resolved in the TAG
namespace (``git/ref/tags/<name>``, an exact-ref JSON object, annotated tags
peeled), never through ``commits/<name>``, which would silently resolve a
same-named branch when the release metadata is stale (Astra, design round 1).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from unittest.mock import patch

import pytest

import plugin_store
from plugin_store import (NoReleaseFound, RefNotFound, ResolveUnavailable,
                          resolve_latest_release)

SHA = "a" * 40
TAG_OBJ = "b" * 40
PEELED = "c" * 40


class _Proc:
    def __init__(self, code, out, err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def _resp(status: int, body) -> str:
    text = body if isinstance(body, str) else json.dumps(body)
    return f"HTTP/2.0 {status} X\ncontent-type: application/json\n\n{text}"


def _gh(routes: dict, calls=None):
    """A fake ``gh api -i <path> [--jq …]``: ``routes[path]`` is
    ``(status, body)``; an unknown path is a 404."""
    def _run(argv, **_kw):
        if calls is not None:
            calls.append(list(argv))
        path = argv[3]
        status, body = routes.get(path, (404, {"message": "Not Found"}))
        return _Proc(0 if 200 <= status < 300 else 1, _resp(status, body))
    return _run


def _tag_ref(name: str, sha: str, kind: str) -> dict:
    return {"ref": f"refs/tags/{name}", "object": {"type": kind, "sha": sha}}


# --- the happy paths ----------------------------------------------------------

def test_latest_release_with_a_lightweight_tag():
    routes = {
        "repos/o/r/releases/latest": (200, {"tag_name": "v0.7.0"}),
        "repos/o/r/git/ref/tags/v0.7.0": (200, _tag_ref("v0.7.0", SHA, "commit")),
    }
    calls: list = []
    with patch("plugin_store.subprocess.run", _gh(routes, calls)):
        assert resolve_latest_release("o/r") == ("v0.7.0", SHA)
    assert all("commits/" not in c[3] for c in calls), calls


def test_latest_release_with_an_annotated_tag_is_peeled():
    routes = {
        "repos/o/r/releases/latest": (200, {"tag_name": "v0.7.0"}),
        "repos/o/r/git/ref/tags/v0.7.0": (200, _tag_ref("v0.7.0", TAG_OBJ, "tag")),
        f"repos/o/r/git/tags/{TAG_OBJ}": (200, {"object": {"type": "commit", "sha": PEELED}}),
    }
    with patch("plugin_store.subprocess.run", _gh(routes)):
        assert resolve_latest_release("o/r") == ("v0.7.0", PEELED)


def test_no_releases_falls_back_to_the_highest_release_tag_numerically():
    routes = {
        "repos/o/r/tags?per_page=100": (200, [
            {"name": "v0.5.6"}, {"name": "v0.10.0"}, {"name": "v0.5.10"},
            {"name": "feature-x"}, {"name": "v1"}, {"name": "0.11.0"}]),
        "repos/o/r/git/ref/tags/v0.10.0": (200, _tag_ref("v0.10.0", SHA, "commit")),
    }
    with patch("plugin_store.subprocess.run", _gh(routes)):
        assert resolve_latest_release("o/r") == ("v0.10.0", SHA)


def test_a_release_named_like_a_branch_is_not_latest():
    """releases/latest → "main": not a release tag; the tags fallback decides."""
    routes = {
        "repos/o/r/releases/latest": (200, {"tag_name": "main"}),
        "repos/o/r/tags?per_page=100": (200, [{"name": "v0.5.6"}]),
        "repos/o/r/git/ref/tags/v0.5.6": (200, _tag_ref("v0.5.6", SHA, "commit")),
    }
    with patch("plugin_store.subprocess.run", _gh(routes)):
        assert resolve_latest_release("o/r") == ("v0.5.6", SHA)


# --- fail closed ---------------------------------------------------------------

def test_no_release_and_no_release_tag_is_no_release_found():
    routes = {"repos/o/r/tags?per_page=100": (200, [{"name": "main"}, {"name": "v1"}])}
    with patch("plugin_store.subprocess.run", _gh(routes)):
        with pytest.raises(NoReleaseFound) as ei:
            resolve_latest_release("o/r")
    assert ei.value.reason_code == "no_release_found"


def test_a_stale_release_naming_a_missing_tag_is_no_release_found():
    """Astra design r1 Q5: release metadata names v1.2.0, only a BRANCH of that
    name exists. commits/v1.2.0 would resolve the branch; the tag namespace
    says 404, and that is no release."""
    routes = {
        "repos/o/r/releases/latest": (200, {"tag_name": "v1.2.0"}),
        "repos/o/r/commits/v1.2.0": (200, SHA),      # the trap, never consulted
        "repos/o/r/tags?per_page=100": (200, []),
    }
    calls: list = []
    with patch("plugin_store.subprocess.run", _gh(routes, calls)):
        with pytest.raises(NoReleaseFound):
            resolve_latest_release("o/r")
    assert all("commits/" not in c[3] for c in calls), calls


def test_a_prefix_match_list_is_not_the_tag():
    """GitHub answers a prefix match with a LIST; only an exact-ref object
    counts (Terra design r2 R5)."""
    routes = {
        "repos/o/r/releases/latest": (200, {"tag_name": "v0.7.0"}),
        "repos/o/r/git/ref/tags/v0.7.0": (200, [_tag_ref("v0.7.0-rc1", SHA, "commit")]),
        "repos/o/r/tags?per_page=100": (200, []),
    }
    with patch("plugin_store.subprocess.run", _gh(routes)):
        with pytest.raises(NoReleaseFound):
            resolve_latest_release("o/r")


def test_an_object_whose_ref_is_a_prefix_hit_is_not_the_tag():
    routes = {
        "repos/o/r/releases/latest": (200, {"tag_name": "v0.7.0"}),
        "repos/o/r/git/ref/tags/v0.7.0": (200, _tag_ref("v0.7.0-rc1", SHA, "commit")),
        "repos/o/r/tags?per_page=100": (200, []),
    }
    with patch("plugin_store.subprocess.run", _gh(routes)):
        with pytest.raises(NoReleaseFound):
            resolve_latest_release("o/r")


def test_an_unpeelable_annotated_tag_is_no_release_found():
    routes = {
        "repos/o/r/releases/latest": (200, {"tag_name": "v0.7.0"}),
        "repos/o/r/git/ref/tags/v0.7.0": (200, _tag_ref("v0.7.0", TAG_OBJ, "tag")),
        f"repos/o/r/git/tags/{TAG_OBJ}": (404, {"message": "Not Found"}),
    }
    with patch("plugin_store.subprocess.run", _gh(routes)):
        with pytest.raises(NoReleaseFound):
            resolve_latest_release("o/r")


# --- transport failures keep the resolver's existing taxonomy -----------------

def test_a_server_error_on_the_listing_is_resolve_unavailable():
    routes = {"repos/o/r/releases/latest": (502, {"message": "bad gateway"})}
    with patch("plugin_store.subprocess.run", _gh(routes)):
        with pytest.raises(ResolveUnavailable):
            resolve_latest_release("o/r")


def test_a_missing_repository_is_ref_not_found():
    routes = {
        "repos/o/r/releases/latest": (404, {"message": "Not Found"}),
        "repos/o/r/tags?per_page=100": (404, {"message": "Not Found"}),
    }
    with patch("plugin_store.subprocess.run", _gh(routes)):
        with pytest.raises(RefNotFound):
            resolve_latest_release("o/r")


def test_release_tuple_orders_numerically():
    assert plugin_store._release_tuple("v0.10.0") > plugin_store._release_tuple("v0.9.9")
    assert plugin_store._release_tuple("v1.0.0") > plugin_store._release_tuple("v0.99.99")
    assert plugin_store._release_tuple("main") is None


def test_a_nested_annotated_tag_is_peeled_to_the_commit():
    """Terra batch-2 r1 J1: tag A -> tag B -> commit C must store C."""
    A, B, C = "d" * 40, "e" * 40, "f" * 40
    routes = {
        "repos/o/r/releases/latest": (200, {"tag_name": "v1.2.3"}),
        "repos/o/r/git/ref/tags/v1.2.3": (200, _tag_ref("v1.2.3", A, "tag")),
        f"repos/o/r/git/tags/{A}": (200, {"object": {"type": "tag", "sha": B}}),
        f"repos/o/r/git/tags/{B}": (200, {"object": {"type": "commit", "sha": C}}),
    }
    calls: list = []
    with patch("plugin_store.subprocess.run", _gh(routes, calls)):
        assert resolve_latest_release("o/r") == ("v1.2.3", C)
    assert any(c[3] == f"repos/o/r/git/tags/{B}" for c in calls)


def test_a_tag_chain_that_never_reaches_a_commit_is_no_release_found():
    A = "d" * 40
    routes = {
        "repos/o/r/releases/latest": (200, {"tag_name": "v1.2.3"}),
        "repos/o/r/git/ref/tags/v1.2.3": (200, _tag_ref("v1.2.3", A, "tag")),
        f"repos/o/r/git/tags/{A}": (200, {"object": {"type": "tag", "sha": A}}),   # a loop
        "repos/o/r/tags?per_page=100": (200, []),
    }
    with patch("plugin_store.subprocess.run", _gh(routes)):
        with pytest.raises(NoReleaseFound):
            resolve_latest_release("o/r")


def test_a_tag_pointing_at_a_non_commit_object_is_no_release_found():
    routes = {
        "repos/o/r/releases/latest": (200, {"tag_name": "v1.2.3"}),
        "repos/o/r/git/ref/tags/v1.2.3": (200, _tag_ref("v1.2.3", SHA, "blob")),
        "repos/o/r/tags?per_page=100": (200, []),
    }
    with patch("plugin_store.subprocess.run", _gh(routes)):
        with pytest.raises(NoReleaseFound):
            resolve_latest_release("o/r")


# --- no GitHub token ----------------------------------------------------------

_GH_LOGIN_HINT = ("To get started with GitHub CLI, please run:  gh auth login\n"
                  "Alternatively, populate the GH_TOKEN environment variable "
                  "with a GitHub API authentication token.\n")


class _Resp:
    def __init__(self, status, body, headers=None):
        self.status, self._body = status, body
        self.headers = {"Content-Type": "application/json", **(headers or {})}

    def read(self):
        return json.dumps(self._body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _anonymous(routes: dict, seen: list):
    """A fake ``urllib.request.urlopen`` over ``routes[path]``."""
    import urllib.error

    def _open(_self, req, timeout=None):
        seen.append(req)
        path = req.full_url.removeprefix("https://api.github.com/")
        status, body = routes.get(path, (404, {"message": "Not Found"}))
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "X",
                                         {"X-RateLimit-Remaining": "0"},
                                         io.BytesIO(json.dumps(body).encode()))
        return _Resp(status, body)
    return _open


def _gh_without_a_token(argv, **_kw):
    return _Proc(4, "", _GH_LOGIN_HINT)


def test_without_a_token_a_public_repository_still_resolves():
    """gh sends nothing when no token is set (exit 4, no HTTP status line);
    the same paths are then asked of GitHub without a credential."""
    routes = {
        "repos/o/r/releases/latest": (200, {"tag_name": "v0.7.0"}),
        "repos/o/r/git/ref/tags/v0.7.0": (200, _tag_ref("v0.7.0", SHA, "commit")),
        "repos/o/r/commits/v0.7.0": (200, {"sha": SHA}),
    }
    seen: list = []
    with patch("plugin_store.subprocess.run", _gh_without_a_token), \
         patch("urllib.request.OpenerDirector.open", _anonymous(routes, seen)):
        assert resolve_latest_release("o/r") == ("v0.7.0", SHA)
        assert plugin_store.resolve_ref("o/r", "v0.7.0") == SHA
    assert len(seen) == 3
    assert all(r.get_header("Authorization") is None for r in seen)


def test_without_a_token_a_repository_that_is_not_public_is_not_found():
    with patch("plugin_store.subprocess.run", _gh_without_a_token), \
         patch("urllib.request.OpenerDirector.open", _anonymous({}, [])):
        with pytest.raises(plugin_store.RefNotFound):
            plugin_store.resolve_ref("o/private", "v1.0.0")


def test_without_a_token_a_rate_limit_keeps_its_classification():
    routes = {"repos/o/r/releases/latest": (403, {"message": "API rate limit exceeded"})}
    with patch("plugin_store.subprocess.run", _gh_without_a_token), \
         patch("urllib.request.OpenerDirector.open", _anonymous(routes, [])):
        with pytest.raises(plugin_store.ResolveUnavailable):
            resolve_latest_release("o/r")


def test_without_a_token_a_network_failure_is_unavailable():
    def _down(_self, req, timeout=None):
        raise OSError("network is unreachable")
    with patch("plugin_store.subprocess.run", _gh_without_a_token), \
         patch("urllib.request.OpenerDirector.open", _down):
        with pytest.raises(plugin_store.ResolveUnavailable):
            plugin_store.resolve_ref("o/r", "v1.0.0")


def test_a_gh_failure_that_is_not_the_missing_token_is_not_retried_anonymously():
    def _broken(argv, **_kw):
        return _Proc(1, "", "gh: something else went wrong")

    def _never(_self, req, timeout=None):
        raise AssertionError("an anonymous request was made")
    with patch("plugin_store.subprocess.run", _broken), \
         patch("urllib.request.OpenerDirector.open", _never):
        with pytest.raises(plugin_store.ResolveUnavailable):
            plugin_store.resolve_ref("o/r", "v1.0.0")


@pytest.mark.skipif(shutil.which("gh") is None, reason="the gh binary is not installed")
def test_the_real_gh_exits_4_without_contacting_github_when_no_token_is_set(tmp_path):
    """The fallback rests on this: with no credential anywhere, gh prints a
    login hint, no HTTP status line, and exits 4."""
    proc = subprocess.run(
        ["gh", "api", "-i", "repos/o/r/releases/latest"],
        capture_output=True, text=True, timeout=30,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
             "GH_CONFIG_DIR": str(tmp_path)})
    assert proc.returncode == 4
    assert "HTTP/" not in proc.stdout


def test_without_a_token_a_redirect_to_another_host_is_not_followed():
    """A local server answers every path with a redirect to another host; the
    probe returns that 3xx and asks the other host nothing."""
    import http.server
    import threading

    asked: list = []

    class _Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            asked.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"sha": SHA}).encode())

        def log_message(self, *_a):
            pass

    other = http.server.HTTPServer(("127.0.0.1", 0), _Other)

    class _Api(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{other.server_port}/x")
            self.end_headers()

        def log_message(self, *_a):
            pass

    api = http.server.HTTPServer(("127.0.0.1", 0), _Api)
    for srv in (other, api):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with patch("plugin_store.subprocess.run", _gh_without_a_token), \
             patch("plugin_store._GITHUB_API", f"http://127.0.0.1:{api.server_port}/"):
            status, _headers, _body = plugin_store.gh_api_probe("repos/o/r/commits/v1")
            with pytest.raises(plugin_store.ResolveUnavailable):
                plugin_store.resolve_ref("o/r", "v1")
    finally:
        for srv in (other, api):
            srv.shutdown()
            srv.server_close()
    assert status == 302 and asked == []
