"""H3 (v0.53.0): executor hooks.yaml params must reach the /hooks/resolve
HTTP path so a claude_code executor's configured path_scope / commit_size_guard
is enforced instead of the deny-all factory defaults.

Pre-fix, _build_cc_hook_policies produced only default-configured callbacks, so
path_scope defaulted to writable=[]/readable=[] and denied EVERY Read/Write/Edit
for a plugin-developer engagement.
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ENG_ID = "a" * 32

# #366: executor-param selection binds only to the AUTHENTICATED engagement —
# requests present the credential pair the workspace .mcp.json carries.
_CREDS = {"engagement_id": ENG_ID, "engagement_token": "tok-a"}


class _Rec:
    status = "active"
    role_or_type = "plugin-developer"
    auth_token = "tok-a"


class _Registry:
    def get(self, eng_id):
        return _Rec() if eng_id == ENG_ID else None


def _handler():
    from casa_core import _build_cc_hook_policies
    from hooks import HOOK_POLICIES, build_policy_callbacks_from_hooks_yaml
    from internal_handlers import _make_internal_hooks_resolve_handler
    hooks_yaml = {"pre_tool_use": [
        {"policy": "path_scope",
         "writable": ["/data/engagements/"],
         "readable": ["/data/engagements/", "/config/plugins/store"]},
        {"policy": "commit_size_guard", "max_files": 50},
    ]}
    return _make_internal_hooks_resolve_handler(
        hook_policies=_build_cc_hook_policies(HOOK_POLICIES),
        executor_hook_policies={
            "plugin-developer": build_policy_callbacks_from_hooks_yaml(
                hooks_yaml
            )},
        engagement_registry=_Registry(),
    )


async def test_write_inside_declared_writable_prefix_is_allowed():
    app = web.Application()
    app.router.add_post("/hooks/resolve", _handler())
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post("/hooks/resolve", json={
            "policy": "path_scope", **_CREDS,
            "payload": {"tool_name": "Write",
                        "cwd": f"/data/engagements/{ENG_ID}",
                        "tool_input": {
                            "file_path":
                                f"/data/engagements/{ENG_ID}/plugin/skill.md"}}})
        assert await resp.json() == {}  # RED pre-fix: deny "writable prefixes []"


async def test_read_inside_declared_readable_prefix_is_allowed():
    app = web.Application()
    app.router.add_post("/hooks/resolve", _handler())
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post("/hooks/resolve", json={
            "policy": "path_scope", **_CREDS,
            "payload": {"tool_name": "Read",
                        "cwd": f"/data/engagements/{ENG_ID}",
                        "tool_input": {"file_path": "/config/plugins/store/x.md"}}})
        assert await resp.json() == {}


async def test_write_outside_declared_prefix_still_denied():
    app = web.Application()
    app.router.add_post("/hooks/resolve", _handler())
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post("/hooks/resolve", json={
            "policy": "path_scope", **_CREDS,
            "payload": {"tool_name": "Write",
                        "cwd": f"/data/engagements/{ENG_ID}",
                        "tool_input": {"file_path": "/etc/passwd"}}})
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_unknown_engagement_falls_back_to_default_policies():
    app = web.Application()
    app.router.add_post("/hooks/resolve", _handler())
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post("/hooks/resolve", json={
            "policy": "path_scope",
            "payload": {"tool_name": "Write",
                        "cwd": "/somewhere/else",
                        "tool_input": {
                            "file_path": "/data/engagements/x/f"}}})
        body = await resp.json()
        # default writable=[] -> deny
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_commit_size_guard_uses_declared_max_files(monkeypatch):
    import hooks as hooks_mod
    monkeypatch.setattr(
        hooks_mod, "_git_porcelain_count", lambda repo_dir="/config": 30,
    )
    app = web.Application()
    app.router.add_post("/hooks/resolve", _handler())
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post("/hooks/resolve", json={
            "policy": "commit_size_guard", **_CREDS,
            "payload": {"tool_name": "Write",
                        "cwd": f"/data/engagements/{ENG_ID}",
                        "tool_input": {
                            "file_path": f"/data/engagements/{ENG_ID}/f"}}})
        # 30 < declared max_files=50 -> allow; default max=20 would deny.
        assert await resp.json() == {}


# ---------------------------------------------------------------------------
# #313: duplicate policy declarations must ALL run on the HTTP path.
# ---------------------------------------------------------------------------


async def test_duplicate_policy_declarations_all_enforced():
    """#313: the SDK path registers one matcher per declaration and runs all
    of them, so duplicate declarations enforce their INTERSECTION. Pre-fix,
    the HTTP builder stored ``out[name] = ...`` — last-writer-wins — so a
    write refused by the first declaration but permitted by the last was
    allowed on the HTTP path."""
    from hooks import build_policy_callbacks_from_hooks_yaml
    built = build_policy_callbacks_from_hooks_yaml({"pre_tool_use": [
        {"policy": "path_scope",
         "writable": ["/data/engagements/"],
         "readable": ["/data/engagements/"]},
        {"policy": "path_scope",
         "writable": ["/config/"],
         "readable": ["/config/"]},
    ]})
    _matcher, cb = built["path_scope"]
    # Permitted by the LAST declaration alone, refused by the first: the
    # composite must deny (SDK parity).
    result = await cb(
        {"tool_name": "Write", "tool_input": {"file_path": "/config/x"}},
        None, {},
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_duplicate_declarations_intersection_still_allows():
    """A path inside EVERY duplicate declaration's scope stays allowed."""
    from hooks import build_policy_callbacks_from_hooks_yaml
    built = build_policy_callbacks_from_hooks_yaml({"pre_tool_use": [
        {"policy": "path_scope",
         "writable": ["/data/"], "readable": ["/data/"]},
        {"policy": "path_scope",
         "writable": ["/data/engagements/"],
         "readable": ["/data/engagements/"]},
    ]})
    _matcher, cb = built["path_scope"]
    result = await cb(
        {"tool_name": "Write",
         "tool_input": {"file_path": "/data/engagements/e/f"}},
        None, {},
    )
    assert not result


# ---------------------------------------------------------------------------
# #315: disabled-but-resumable executors keep their configured hook params.
# ---------------------------------------------------------------------------


async def test_disabled_executor_hook_params_still_built(tmp_path):
    """#315: boot replay resumes brief-bearing engagements of DISABLED
    executors via definition_any(); the HTTP hook-policy map must carry their
    declared parameters too. Pre-fix it iterated list_types()/get() (enabled
    only), so a resumed engagement fell back to the default empty path_scope
    — every workspace Read/Write/Edit denied."""
    from types import SimpleNamespace
    from casa_core import _build_executor_cc_hook_policies
    from executor_registry import ExecutorRegistry

    hooks_path = tmp_path / "hooks.yaml"
    hooks_doc = {
        "schema_version": 1,
        "pre_tool_use": [
            {"policy": "path_scope",
             "writable": ["/data/engagements/"],
             "readable": ["/data/engagements/"]},
        ],
    }
    hooks_path.write_text(
        "schema_version: 1\n"
        "pre_tool_use:\n"
        "  - policy: path_scope\n"
        "    writable: [/data/engagements/]\n"
        "    readable: [/data/engagements/]\n"
    )
    reg = ExecutorRegistry(str(tmp_path / "executors"))
    reg._disabled.add("plugin-developer")
    reg._disabled_defs["plugin-developer"] = SimpleNamespace(
        driver="claude_code", hooks_path=str(hooks_path),
        hooks_document=hooks_doc,
    )
    built = _build_executor_cc_hook_policies(reg)
    assert "plugin-developer" in built
    assert "path_scope" in built["plugin-developer"]
    _matcher, cb = built["plugin-developer"]["path_scope"]
    result = await cb(
        {"tool_name": "Write",
         "tool_input": {"file_path": "/data/engagements/e/f"}},
        None, {},
    )
    assert not result  # declared scope applies — not the deny-all default


async def test_duplicate_declarations_with_differing_matchers_refused():
    """Sol r2-1: the wire names only the POLICY, so per-declaration matcher
    scoping of duplicates is unenforceable server-side — refuse the config
    loudly (which #312's load-time constructibility check turns into a
    fail-closed executor load/commit failure)."""
    import pytest as _pytest
    from hooks import UnknownPolicyError, build_policy_callbacks_from_hooks_yaml
    with _pytest.raises(UnknownPolicyError, match="differing matchers"):
        build_policy_callbacks_from_hooks_yaml({"pre_tool_use": [
            {"policy": "path_scope", "matcher": "Read",
             "readable": ["/a/"]},
            {"policy": "path_scope", "matcher": "Write",
             "writable": ["/b/"]},
        ]})


async def test_duplicate_declarations_same_matcher_still_compose():
    from hooks import build_policy_callbacks_from_hooks_yaml
    built = build_policy_callbacks_from_hooks_yaml({"pre_tool_use": [
        {"policy": "path_scope", "writable": ["/data/"],
         "readable": ["/data/"]},
        {"policy": "path_scope", "writable": ["/data/engagements/"],
         "readable": ["/data/engagements/"]},
    ]})
    assert "path_scope" in built


# ---------------------------------------------------------------------------
# Task 5 (#360): the HTTP hook-policy map builds from the load-time snapshot
# (ExecutorDefinition.hooks_document), never by re-reading hooks_path.
# ---------------------------------------------------------------------------


async def test_post_load_hooks_yaml_mutation_does_not_reach_the_http_map(
    tmp_path,
):
    """TOCTOU red case: pin a deny-everything path_scope snapshot as the
    load-time ``hooks_document``, then widen the ON-DISK file afterward (as a
    config-editable ``hooks_file:`` repoint would) and rebuild the HTTP map
    WITHOUT a reload. The rebuilt map must still enforce the narrow snapshot,
    not the mutated file — reload (Task 3's load path) is the only supported
    way to pick up a change."""
    from types import SimpleNamespace
    from casa_core import _build_executor_cc_hook_policies
    from executor_registry import ExecutorRegistry

    hooks_path = tmp_path / "hooks.yaml"
    narrow_snapshot = {
        "pre_tool_use": [
            {"policy": "path_scope", "writable": [], "readable": []},
        ],
    }
    hooks_path.write_text(
        "pre_tool_use:\n"
        "  - policy: path_scope\n"
        "    writable: []\n"
        "    readable: []\n",
        encoding="utf-8",
    )
    reg = ExecutorRegistry(str(tmp_path / "executors"))
    reg._disabled.add("plugin-developer")
    reg._disabled_defs["plugin-developer"] = SimpleNamespace(
        driver="claude_code", hooks_path=str(hooks_path),
        hooks_document=narrow_snapshot,
    )

    # Post-load mutation: the file now grants broad write access.
    hooks_path.write_text(
        "pre_tool_use:\n"
        "  - policy: path_scope\n"
        "    writable: ['/']\n"
        "    readable: ['/']\n",
        encoding="utf-8",
    )

    built = _build_executor_cc_hook_policies(reg)
    _matcher, cb = built["plugin-developer"]["path_scope"]
    result = await cb(
        {"tool_name": "Write",
         "tool_input": {"file_path": "/data/engagements/e/f"}},
        None, {},
    )
    assert result and result["hookSpecificOutput"]["permissionDecision"] == (
        "deny"
    ), (
        "path_scope must enforce the load-time snapshot (empty writable), "
        "not the post-load widened on-disk file"
    )
