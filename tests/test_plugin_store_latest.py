"""``resolve_latest_release``: "latest" is a published release tag or nothing.

Design 2026-09-15 §2.C: the configurator has no web tool and its brief said
"resolve 'latest' yourself"; on the N150 (2026-09-15) it spent 90 s failing
and then installed ``main``. Now ``plugin_add(ref="latest")`` resolves
server-side: GitHub's latest release, accepted only when its name is a release
tag (``v<semver>``); else the highest release tag by numeric order; else
``no_release_found`` — never a branch. The chosen name is resolved in the TAG
namespace (``git/ref/tags/<name>``, an exact-ref JSON object, annotated tags
peeled), never through ``commits/<name>``, which would silently resolve a
same-named branch when the release metadata is stale (Astra, design round 1).
"""
from __future__ import annotations

import json
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
