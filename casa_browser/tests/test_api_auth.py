"""Task 3: authentication, protocol header, wire bounds and the route table."""
from __future__ import annotations

import hashlib
import json
import logging
import re


from casa_browser import __version__
from casa_browser.api import make_app
from casa_browser.protocol import ERROR_STATUS, PROTOCOL_HEADER

from apihelpers import Clock, headers, make_fake, pair, paired_client, run_body
from fake_controller import PNG

ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

EXPECTED_ROUTES = {
    ("GET", "/v1/hello"), ("POST", "/v1/pair/claim"), ("GET", "/v1/sites"),
    ("POST", "/v1/control/sites"), ("POST", "/v1/runs"),
    ("GET", "/v1/runs"), ("GET", "/v1/runs/{run}"), ("GET", "/v1/runs/{run}/events"),
    ("GET", "/v1/control/runs/{run}/screenshots/{screenshot}"),
    ("POST", "/v1/control/runs/{run}/screenshots"),
    ("GET", "/v1/control/runs/{run}/sources"), ("POST", "/v1/runs/{run}/observe"),
    ("POST", "/v1/runs/{run}/action"), ("POST", "/v1/runs/{run}/download"),
    ("POST", "/v1/runs/{run}/search"), ("POST", "/v1/runs/{run}/fetch"),
    ("POST", "/v1/runs/{run}/archive"),
    ("POST", "/v1/runs/{run}/login-code"),
    ("POST", "/v1/runs/{run}/sign-in"),
    ("POST", "/v1/runs/{run}/look"), ("POST", "/v1/runs/{run}/read"),
    ("GET", "/v1/control/runs/{run}/judgements/{judgement}/picture"),
    ("POST", "/v1/runs/{run}/live"),
    ("POST", "/v1/control/runs/{run}/live-complete"), ("POST", "/v1/runs/{run}/finish"),
    ("POST", "/v1/runs/{run}/cancel"), ("POST", "/v1/requests/cancel"),
    ("POST", "/v1/control/sites/{site}/forget"),
    ("GET", "/v1/records"), ("GET", "/v1/files/{file}"),
    ("POST", "/v1/control/files/{file}/delivery"), ("GET", "/v1/notices"),
    ("GET", "/v1/control/runs/{run}/judgements/pending"),
    ("POST", "/v1/control/runs/{run}/judgements/{judgement}"),
    ("POST", "/v1/control/runs/{run}/questions/{question}/answer"),
    ("GET", "/v1/control/runs/{run}/questions/{question}"),
    ("POST", "/v1/control/runs/{run}/questions/{question}/live"),
}


def registered(app) -> set[tuple[str, str]]:
    out = set()
    for route in app.router.routes():
        if route.method == "*":  # the catch-all: any other method or path → not_found
            continue
        out.add((route.method, route.resource.canonical))
    return out


def fill(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: f"{m.group(1)}_x", path)


async def test_all_routes_require_auth(aiohttp_client, tmp_path):
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    routes = registered(client.app)
    assert routes == EXPECTED_ROUTES
    bad_auth = [None, "Bearer wrong", f"bearer {secret}", f"Bearer {secret} x", f"Basic {secret}",
                f"Bearer {secret[:-1]}"]
    for method, path in routes:
        for auth in bad_auth:
            hdrs = {PROTOCOL_HEADER: "1"}
            if auth is not None:
                hdrs["Authorization"] = auth
            resp = await client.request(method, fill(path), json={}, headers=hdrs)
            assert resp.status == 401, (method, path, auth)
            assert await resp.json() == {"error": "unauthorized"}
        # The secret is never accepted from a query string.
        resp = await client.request(method, fill(path) + f"?token={secret}", json={},
                                    headers={PROTOCOL_HEADER: "1"})
        assert resp.status == 401
    # Unknown paths need auth too, then answer not_found.
    resp = await client.get("/v1/nope")
    assert resp.status == 401
    resp = await client.get("/v1/nope", headers=headers(secret))
    assert resp.status == 404 and await resp.json() == {"error": "not_found"}


async def test_hello_members_exact(aiohttp_client, tmp_path):
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    resp = await client.get("/v1/hello", headers={"Authorization": f"Bearer {secret}"})
    body = await resp.json()
    assert body == {"protocol_version": 1, "app_version": __version__, "configured": True,
                    "needs_locale": False, "language_source": "home_assistant"}
    fake.needs_locale = True
    fake.language_source = None
    body = await (await client.get("/v1/hello", headers=headers(secret, None))).json()
    assert body["needs_locale"] is True and body["language_source"] is None


async def test_bad_version_before_run(aiohttp_client, tmp_path):
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    for protocol in (None, "2", "01", "1.0"):
        resp = await client.post("/v1/runs", json=run_body(), headers=headers(secret, protocol))
        assert resp.status == 400 and await resp.json() == {"error": "protocol_mismatch"}
    assert fake.store.list_runs(("queued", "running", "starting")) == []
    resp = await client.post("/v1/runs", json=run_body(), headers=headers(secret))
    assert resp.status == 201


async def test_error_bodies_and_statuses(aiohttp_client, tmp_path):
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    resp = await client.get("/v1/runs/run_missing", headers=headers(secret))
    assert (resp.status, await resp.json()) == (404, {"error": "not_found"})
    resp = await client.post("/v1/runs", data=b"{not json", headers=headers(secret))
    assert (resp.status, await resp.json()) == (400, {"error": "unsupported"})
    run = await (await client.post("/v1/runs", json=run_body(), headers=headers(secret))).json()
    changed = run_body(task_text="something else")
    resp = await client.post("/v1/runs", json=changed, headers=headers(secret))
    assert (resp.status, await resp.json()) == (409, {"error": "conflict"})
    resp = await client.post(f"/v1/runs/{run['run_id']}/action",
                             json={"kind": "click", "handle": "el_1", "generation": 99},
                             headers=headers(secret))
    assert (resp.status, await resp.json()) == (409, {"error": "stale_handle"})
    for code, status in ERROR_STATUS.items():
        assert 400 <= status < 600, code


async def test_control_never_accepts_model_operator_boolean(aiohttp_client, tmp_path):
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    run = await (await client.post("/v1/runs", json=run_body(), headers=headers(secret))).json()
    good = {"revision": 1, "digest": "a" * 64, "choice": "confirm", "operator_ref": "tg-cb-1"}
    for bad in ({**good, "operator": True}, {**good, "operator_ref": True},
                {k: v for k, v in good.items() if k != "operator_ref"},
                {**good, "approved_by_operator": "yes"}):
        resp = await client.post(f"/v1/control/runs/{run['run_id']}/questions/q_1/answer",
                                 json=bad, headers=headers(secret))
        assert resp.status == 400
    for bad in ({"display_name": "X", "origins": ["https://x.example"], "login_origins": [],
                 "operator": True},
                {"display_name": "X", "origins": ["https://x.example"], "login_origins": []}):
        resp = await client.post("/v1/control/sites", json=bad, headers=headers(secret))
        assert resp.status == 400
    resp = await client.post(f"/v1/run/{run['run_id']}/finish", json={}, headers=headers(secret))
    assert resp.status == 404


async def test_admission_refusals(aiohttp_client, tmp_path):
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    body = run_body()
    body["admission"]["origin_kind"] = "model"
    resp = await client.post("/v1/runs", json=body, headers=headers(secret))
    assert resp.status == 400
    body = run_body()
    del body["admission"]
    assert (await client.post("/v1/runs", json=body, headers=headers(secret))).status == 400
    body = run_body()
    body["admission"]["origin_ref"] = ""
    assert (await client.post("/v1/runs", json=body, headers=headers(secret))).status == 400
    assert fake.store.list_runs(("queued", "running")) == []


BOUNDS = [
    ("task_kind", 60), ("task_text", 2000), ("result_expectation", 500),
]


async def test_wire_bounds(aiohttp_client, tmp_path):
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    n = 0
    for member, limit in BOUNDS:
        n += 1
        ok = run_body(f"ok-{n}", **{member: "x" * limit})
        assert (await client.post("/v1/runs", json=ok, headers=headers(secret))).status == 201
        over = run_body(f"over-{n}", **{member: "x" * (limit + 1)})
        resp = await client.post("/v1/runs", json=over, headers=headers(secret))
        assert (resp.status, await resp.json()) == (400, {"error": "unsupported"})
        assert fake.store.get_run is not None
    for member in ("request_id", "requester_id"):
        n += 1
        at = "r" * 128 if member == "request_id" else "q" * 128
        ok = run_body(at if member == "request_id" else f"ok-{n}", **({member: at} if member != "request_id" else {}))
        assert (await client.post("/v1/runs", json=ok, headers=headers(secret))).status == 201
        over_val = "r" * 129
        over = run_body(over_val if member == "request_id" else f"over-{n}",
                        **({member: over_val} if member != "request_id" else {}))
        assert (await client.post("/v1/runs", json=over, headers=headers(secret))).status == 400
        spaced = run_body("has space" if member == "request_id" else f"sp-{n}",
                          **({member: "has space"} if member != "request_id" else {}))
        assert (await client.post("/v1/runs", json=spaced, headers=headers(secret))).status == 400
    # Unknown member anywhere → 400 before side effects.
    before = len(fake.store.list_runs(("queued", "running", "starting")))
    body = run_body("unknown-member")
    body["extra"] = 1
    assert (await client.post("/v1/runs", json=body, headers=headers(secret))).status == 400
    body = run_body("unknown-site-member", sites=[{"site_id": "site_a", "access": "account", "x": 1}])
    assert (await client.post("/v1/runs", json=body, headers=headers(secret))).status == 400
    assert len(fake.store.list_runs(("queued", "running", "starting"))) == before
    # App-issued ids match the id pattern.
    for r in fake.store.list_runs(("queued", "running", "starting")):
        assert ID.fullmatch(r.run_id)
    # A running run for the remaining bounds.
    run_id = fake.store.list_runs(("running",))[0].run_id
    resp = await client.post(f"/v1/runs/{run_id}/search", json={"query": "q" * 300}, headers=headers(secret))
    assert resp.status == 200
    resp = await client.post(f"/v1/runs/{run_id}/search", json={"query": "q" * 301}, headers=headers(secret))
    assert resp.status == 400
    answer = {"revision": 1, "digest": "a" * 64, "choice": "not_now", "operator_ref": "o" * 129}
    assert (await client.post(f"/v1/control/runs/{run_id}/questions/q_1/answer", json=answer,
                              headers=headers(secret))).status == 400
    finish = {"summary_masked": "s" * 2001, "file_ids": [], "source_ids": [],
              "expectation_status": "matched"}
    assert (await client.post(f"/v1/runs/{run_id}/finish", json=finish,
                              headers=headers(secret))).status == 400
    finish["summary_masked"] = "s" * 2000
    # (the bound check passes)
    resp = await client.post(f"/v1/runs/{run_id}/finish", json=finish, headers=headers(secret))
    assert resp.status != 400


async def test_claim_label_bound(aiohttp_client, tmp_path):
    clock = Clock()
    fake = make_fake(tmp_path, clock)
    client = await aiohttp_client(make_app(fake))
    fake.pairing.arm()
    resp = await client.post("/v1/pair/claim", json={"caller_label": "l" * 65}, headers=headers(None))
    assert resp.status == 400
    assert fake.pairing.window_open()  # a refused body does not spend the window
    resp = await client.post("/v1/pair/claim", json={"caller_label": "l" * 64}, headers=headers(None))
    assert resp.status == 200


async def test_scroll_members(aiohttp_client, tmp_path):
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    run = await (await client.post("/v1/runs", json=run_body(), headers=headers(secret))).json()
    url = f"/v1/runs/{run['run_id']}/action"
    base = {"kind": "scroll", "handle": "pg_1", "generation": 1}
    for bad in (base, {**base, "direction": "down"}, {**base, "amount": "page"},
                {**base, "direction": "left", "amount": "page"},
                {"kind": "click", "handle": "el_1", "generation": 1, "direction": "down"},
                {"kind": "click", "handle": "el_1", "generation": 1, "amount": "step"},
                {"kind": "type", "handle": "el_1", "generation": 1},
                {"kind": "click", "handle": "el_1", "generation": 1, "text": "x"},
                {"kind": "select", "handle": "el_1", "generation": 1},
                {"kind": "click", "handle": "el_1", "generation": 1, "url": "https://x"}):
        resp = await client.post(url, json=bad, headers=headers(secret))
        assert (resp.status, await resp.json()) == (400, {"error": "unsupported"}), bad
    ok = {**base, "direction": "down", "amount": "page"}
    assert (await client.post(url, json=ok, headers=headers(secret))).status == 200
    # Fido sends every member, absent ones as null.
    nulls = {"kind": "click", "handle": "el_1", "generation": 2, "text": None,
             "choice_handle": None, "direction": None, "amount": None}
    assert (await client.post(url, json=nulls, headers=headers(secret))).status == 200


async def test_new_routes_registered(aiohttp_client, tmp_path):
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    routes = registered(client.app)
    for need in (("GET", "/v1/runs"),
                 ("GET", "/v1/control/runs/{run}/screenshots/{screenshot}"),
                 ("GET", "/v1/control/runs/{run}/sources")):
        assert need in routes
    # Slice 7: the approval model's routes are gone (hints, proposals, refine, decision).
    assert not {r for r in routes if "/hints" in r[1] or "/proposals" in r[1]}
    a = await (await client.post("/v1/runs", json=run_body("a"), headers=headers(secret))).json()
    b = await (await client.post("/v1/runs", json=run_body("b", mode="open_web",
                                                            sites=[{"site_id": "site_pub", "access": "public"}]),
                                 headers=headers(secret))).json()
    fake.screenshots[(a["run_id"], "shot_1")] = PNG
    resp = await client.get(f"/v1/control/runs/{a['run_id']}/screenshots/shot_1", headers=headers(secret))
    assert resp.status == 200 and resp.content_type == "image/png" and await resp.read() == PNG
    resp = await client.get(f"/v1/control/runs/{b['run_id']}/screenshots/shot_1", headers=headers(secret))
    assert resp.status == 404
    # Run b is queued behind a; finish a so b runs and records a source.
    await client.post(f"/v1/runs/{a['run_id']}/cancel", json={}, headers=headers(secret))
    fake.store.lease_next_run()
    fake.store.transition(b["run_id"], "starting", "running")
    await client.post(f"/v1/runs/{b['run_id']}/observe", json={}, headers=headers(secret))
    src_b = await (await client.get(f"/v1/control/runs/{b['run_id']}/sources", headers=headers(secret))).json()
    src_a = await (await client.get(f"/v1/control/runs/{a['run_id']}/sources", headers=headers(secret))).json()
    assert [s["source_id"] for s in src_b["sources"]] == ["src_1"] and src_a == {"sources": []}
    resp = await client.get("/v1/runs?state=queued", headers=headers(secret))
    assert resp.status == 400
    resp = await client.get("/v1/runs", headers=headers(secret))
    assert resp.status == 400


async def test_fake_serves_every_route(aiohttp_client, tmp_path):
    """Every route answers 2xx through FakeController on a happy path (Plan 2's contract
    test depends on it)."""
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    h = headers(secret)
    seen: set[tuple[str, str]] = set()

    async def call(method, template, path, body=None, expect=None):
        resp = await client.request(method, path, json=body if method == "POST" else None, headers=h)
        assert 200 <= resp.status < 300, (template, resp.status, await resp.text())
        seen.add((method, template))
        if resp.content_type == "application/json":
            return await resp.json()
        return await resp.read()

    await call("GET", "/v1/hello", "/v1/hello")
    await call("GET", "/v1/sites", "/v1/sites")
    site = await call("POST", "/v1/control/sites", "/v1/control/sites",
                      {"display_name": "New", "origins": ["https://n.example"], "login_origins": [],
                       "operator_ref": "tg-1"})
    run = await call("POST", "/v1/runs", "/v1/runs", run_body())
    rid = run["run_id"]
    await call("GET", "/v1/runs", "/v1/runs?state=active")
    await call("GET", "/v1/runs/{run}", f"/v1/runs/{rid}")
    await call("GET", "/v1/runs/{run}/events", f"/v1/runs/{rid}/events?after=0")
    obs = await call("POST", "/v1/runs/{run}/observe", f"/v1/runs/{rid}/observe", {})
    assert await call("GET", "/v1/control/runs/{run}/judgements/pending",
                      f"/v1/control/runs/{rid}/judgements/pending") == {"judgements": []}
    judged = await call("POST", "/v1/runs/{run}/action", f"/v1/runs/{rid}/action",
                        {"kind": "click", "handle": "el_1", "generation": obs["generation"]})
    assert judged["outcome"] == "judgement_required"
    sha = hashlib.sha256(judged["judgement"]["packet"].encode("utf-8")).hexdigest()
    jpath = f"/v1/control/runs/{rid}/judgements/{judged['judgement']['id']}"
    await call("GET", "/v1/control/runs/{run}/judgements/{judgement}/picture", jpath + "/picture")
    assert (await call("POST", "/v1/runs/{run}/look", f"/v1/runs/{rid}/look",
                       {"handle": "el_1"}))["picture"]
    assert (await call("POST", "/v1/runs/{run}/read", f"/v1/runs/{rid}/read",
                       {"offset": 5}))["text"] == "text"
    template = "/v1/control/runs/{run}/judgements/{judgement}"
    ask = {"verdict": "ask", "category": "account_change", "reason": "saves a setting",
           "question": {"text": "Save it?", "confirm_label": "Save", "extra_options": [],
                        "screenshot": "none", "highlight": [], "text_incomplete": False}}
    asked = await call("POST", template, jpath, {"packet_sha256": sha, "verdict": ask})
    qid = asked["question_id"]
    q = await call("GET", "/v1/control/runs/{run}/questions/{question}",
                   f"/v1/control/runs/{rid}/questions/{qid}")
    live = await call("POST", "/v1/control/runs/{run}/questions/{question}/live",
                      f"/v1/control/runs/{rid}/questions/{qid}/live",
                      {"revision": q["revision"], "digest": q["digest"], "n": 1,
                       "operator_ref": "tg-cb-0"})
    assert live["outcome"] == "live" and live["grant"]["grant_id"]
    fake.store.transition(rid, "waiting_live", "waiting_approval")    # the operator's Done
    tap = await call("POST", "/v1/control/runs/{run}/questions/{question}/answer",
                     f"/v1/control/runs/{rid}/questions/{qid}/answer",
                     {"revision": q["revision"], "digest": q["digest"], "choice": "confirm",
                      "operator_ref": "tg-cb-0"})
    rj = tap["judgement"]
    done = await call("POST", template, f"/v1/control/runs/{rid}/judgements/{rj['id']}",
                      {"packet_sha256": hashlib.sha256(rj["packet"].encode()).hexdigest(),
                       "verdict": {"verdict": "same", "reason": "unchanged",
                                   "text_incomplete": False}})
    assert done["outcome"] == "done"
    await call("POST", "/v1/runs/{run}/search", f"/v1/runs/{rid}/search", {"query": "invoices"})
    fetched = await call("POST", "/v1/runs/{run}/fetch", f"/v1/runs/{rid}/fetch",
                         {"url": "https://public.example/page", "fields": ["a.b"]})
    assert fetched["outcome"] == "not_run" and fetched["reason"] == "http_403"
    fake.code_sites[rid] = "site_a"
    assert (await call("POST", "/v1/runs/{run}/login-code", f"/v1/runs/{rid}/login-code",
                       {"code": "482915", "site_id": "site_a"}))["outcome"] == "done"
    signin = await call("POST", "/v1/runs/{run}/sign-in", f"/v1/runs/{rid}/sign-in",
                        {"site_name": "Enable Banking"})
    assert signin["outcome"] == "not_configured" and fake.sign_ins == [(rid, "Enable Banking")]
    dl = await call("POST", "/v1/runs/{run}/download", f"/v1/runs/{rid}/download", {"handle": "el_2"})
    go = {"verdict": "go", "category": "reading", "reason": "downloads an invoice"}
    got = await call("POST", template, f"/v1/control/runs/{rid}/judgements/{dl['judgement']['id']}",
                     {"packet_sha256": hashlib.sha256(dl["judgement"]["packet"].encode()).hexdigest(),
                      "verdict": go})
    f1 = got["file"]
    await call("POST", "/v1/runs/{run}/archive", f"/v1/runs/{rid}/archive", {"file_ids": [f1["file_id"]]})
    await call("POST", "/v1/runs/{run}/live", f"/v1/runs/{rid}/live", {"purpose": "login"})
    await call("POST", "/v1/control/runs/{run}/live-complete", f"/v1/control/runs/{rid}/live-complete",
               {"operator_ref": "tg-cb-2"})
    fake.screenshots[(rid, "shot_1")] = PNG
    await call("GET", "/v1/control/runs/{run}/screenshots/{screenshot}",
               f"/v1/control/runs/{rid}/screenshots/shot_1")
    await call("POST", "/v1/control/runs/{run}/screenshots", f"/v1/control/runs/{rid}/screenshots",
               {})
    await call("GET", "/v1/control/runs/{run}/sources", f"/v1/control/runs/{rid}/sources")
    await call("GET", "/v1/files/{file}", f"/v1/files/{f1['file_id']}")
    await call("POST", "/v1/control/files/{file}/delivery", f"/v1/control/files/{f1['file_id']}/delivery",
               {"requester_id": "finance", "delivery_id": "d-1", "outcome": "deposited"})
    await call("POST", "/v1/runs/{run}/finish", f"/v1/runs/{rid}/finish",
               {"summary_masked": "Got it", "file_ids": [f1["file_id"]], "source_ids": [],
                "expectation_status": "matched"})
    await call("GET", "/v1/records", "/v1/records?requester_id=finance&since=2026-01-01T00:00:00Z")
    run2 = await call("POST", "/v1/runs", "/v1/runs", run_body("req-2"))
    await call("POST", "/v1/runs/{run}/cancel", f"/v1/runs/{run2['run_id']}/cancel", {})
    await call("POST", "/v1/requests/cancel", "/v1/requests/cancel", {"request_ids": ["req-3"]})
    await call("POST", "/v1/control/sites/{site}/forget", f"/v1/control/sites/{site['site_id']}/forget",
               {"operator_ref": "tg-cb-3"})
    await call("GET", "/v1/notices", "/v1/notices?after=0")
    pairing_route = ("POST", "/v1/pair/claim")
    assert seen | {pairing_route} == EXPECTED_ROUTES


async def test_secret_absent_from_responses_and_logs(aiohttp_client, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    fake = make_fake(tmp_path, clock)
    client = await aiohttp_client(make_app(fake))
    fake.pairing.arm()
    resp = await client.post("/v1/pair/claim", json={"caller_label": "Casa"}, headers=headers(None))
    secret = (await resp.json())["secret"]
    bodies = []
    for method, path in (("GET", "/v1/hello"), ("GET", "/v1/sites"), ("GET", "/v1/notices"),
                         ("GET", "/v1/runs?state=active")):
        r = await client.request(method, path, headers=headers(secret))
        bodies.append(await r.text())
    r = await client.post("/v1/runs", json=run_body(), headers=headers(secret))
    bodies.append(await r.text())
    r = await client.post("/v1/pair/claim", json={"caller_label": "Casa"}, headers=headers(secret))
    bodies.append(await r.text())
    joined = "\n".join(bodies) + caplog.text + json.dumps(fake.pairing.status_view(), default=str)
    assert secret not in joined
    assert len(secret) >= 43  # 256 bits, urlsafe base64


async def test_api_logs_no_query_or_header(aiohttp_client, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="aiohttp.access")
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    await client.get("/v1/records?requester_id=finance&since=2026-01-01T00:00:00Z",
                     headers=headers(secret))
    # serve() runs with access_log=None; the test server's own access log line, if any,
    # never carries the bearer secret.
    assert secret not in caplog.text


async def test_contract_edges_from_review(aiohttp_client, tmp_path):
    """Review r3-c1-r1 (Astra b1/b2): each answers its documented code, no state change."""
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    # A bearer with a non-ASCII byte is just a wrong secret.
    resp = await client.get("/v1/hello", headers={"Authorization": "Bearer \xff"})
    assert (resp.status, await resp.json()) == (401, {"error": "unauthorized"})
    # A wrong method on a known path, HEAD, and an unknown path are all not_found.
    resp = await client.post("/v1/hello", json={}, headers=headers(secret))
    assert (resp.status, await resp.json()) == (404, {"error": "not_found"})
    resp = await client.head("/v1/hello", headers=headers(secret))
    assert resp.status == 404
    # A cursor that is a Unicode digit but not an ASCII number.
    resp = await client.get("/v1/notices?after=%C2%B2", headers=headers(secret))
    assert (resp.status, await resp.json()) == (400, {"error": "unsupported"})
    # An open-web run starts with no site; an account run needs one.
    resp = await client.post("/v1/runs", json=run_body("ow", mode="open_web", sites=[]),
                             headers=headers(secret))
    assert resp.status == 201
    resp = await client.post("/v1/runs", json=run_body("acct-empty", sites=[]), headers=headers(secret))
    assert resp.status == 400
    assert (await resp.json()) == {"error": "unsupported"}


async def test_request_framing_edges_from_review(aiohttp_client, tmp_path):
    """Review r3-c1-r2: Expect values, undecodable bodies, encoded claim paths and repeated
    JSON members all get the guard's typed answers, with no state change."""
    from yarl import URL

    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    expect = {"Expect": "unexpected"}
    resp = await client.post("/v1/runs", data=b"{}", headers=expect)
    assert (resp.status, await resp.json()) == (401, {"error": "unauthorized"})
    resp = await client.post("/v1/runs", data=b"{}", headers={**expect, **headers(secret, None)})
    assert (resp.status, await resp.json()) == (400, {"error": "protocol_mismatch"})
    gz = {"Content-Encoding": "gzip"}
    resp = await client.post("/v1/runs", data=b"not-gzip", headers=gz)
    assert (resp.status, await resp.json()) == (401, {"error": "unauthorized"})
    resp = await client.post("/v1/runs", data=b"not-gzip", headers={**gz, **headers(secret)})
    assert (resp.status, await resp.json()) == (400, {"error": "unsupported"})
    fake.pairing.arm()
    resp = await client.post(URL("/v1%2fpair/claim", encoded=True), json={"caller_label": "x"},
                             headers=headers(None))
    assert (resp.status, await resp.json()) == (401, {"error": "unauthorized"})
    for spelling in ("/%761/pair/claim", "/v1/p%61ir/claim", "/v1/pair/%63laim"):
        resp = await client.post(URL(spelling, encoded=True), json={"caller_label": "x"},
                                 headers=headers(None))
        assert (resp.status, await resp.json()) == (401, {"error": "unauthorized"}), spelling
    assert fake.pairing.window_open()
    secret = fake.pairing.claim("Casa", None)
    raw = (b'{"request_id":"dup","requester_id":"finance","mode":"account",'
           b'"task_text":"NOT-ADMITTED","task_text":"Download the March invoice",'
           b'"task_kind":"invoice","sites":[{"site_id":"site_a","access":"account"}],'
           b'"result_expectation":"one PDF","admission":{"origin_kind":"operator_telegram",'
           b'"origin_ref":"tg-1","admitted_spec_digest":"'
           + run_body("dup")["admission"]["admitted_spec_digest"].encode() + b'"}}')
    resp = await client.post("/v1/runs", data=raw,
                             headers={**headers(secret), "Content-Type": "application/json"})
    assert (resp.status, await resp.json()) == (400, {"error": "unsupported"})
    assert fake.store.list_runs(("queued", "starting", "running")) == []


async def test_blank_names_and_ports(aiohttp_client, tmp_path):
    """Review r3-c1-r2: a blank name or an out-of-range port is refused before it is stored."""
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    before = len(fake.store.list_sites())
    for bad in ({"display_name": " ", "origins": ["https://x.example"], "login_origins": [],
                 "operator_ref": "tg-1"},
                {"display_name": "X", "origins": ["https://x.example:99999"], "login_origins": [],
                 "operator_ref": "tg-1"},
                {"display_name": "X", "origins": ["https://x.example:0"], "login_origins": [],
                 "operator_ref": "tg-1"}):
        resp = await client.post("/v1/control/sites", json=bad, headers=headers(secret))
        assert (resp.status, await resp.json()) == (400, {"error": "unsupported"})
    assert len(fake.store.list_sites()) == before


async def test_malformed_http_never_logs_request_bytes(aiohttp_server, tmp_path, caplog):
    """Operator ruling 2026-10-20 (option A): aiohttp's HTTP parser answers malformed requests
    itself, outside the /v1 contract; no log record carries their bytes (a secret-shaped
    header included)."""
    import asyncio
    import logging as _logging

    caplog.set_level(_logging.DEBUG)
    fake = make_fake(tmp_path)
    secret = pair(fake)
    server = await aiohttp_server(make_app(fake))
    canary = "SECRETCANARY" + secret
    raw_requests = [
        f"GET /v1/hello HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {canary}\x01\r\n\r\n".encode("latin-1"),
        f"BREW /v1/hello HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {canary}\r\n\r\n".encode(),
        f"GET /v1/hello HTTP/1.1\r\nHost: x\r\nX-Probe: {canary}{'a' * 20000}\r\n\r\n".encode(),
        f"GET /v1/{canary}\x7f HTTP/1.1\r\nHost: x\r\n\r\n".encode("latin-1"),
        f"GET /v1/hello HTTP/9.9\r\nAuthorization: Bearer {canary}\r\n\r\n".encode(),
    ]
    for raw in raw_requests:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        writer.write(raw)
        await writer.drain()
        try:
            await asyncio.wait_for(reader.read(), 5)
        except (asyncio.TimeoutError, ConnectionError):
            pass
        writer.close()
    await asyncio.sleep(0.05)
    assert caplog.records, "the probes produced no log records at all (harness check)"
    formatter = _logging.Formatter("%(message)s")
    for record in caplog.records:
        text = formatter.format(record) + str(record.args)
        assert canary not in text and secret not in text, record.name


async def test_login_code_is_never_echoed_or_logged(aiohttp_client, tmp_path, caplog):
    """Slice 6 (S6-3): the sign-in code reaches the controller and nothing else the API says:
    neither a reply, a refusal, nor a log line."""
    caplog.set_level(logging.DEBUG)
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    rid = (await (await client.post("/v1/runs", json=run_body(), headers=headers(secret))).json())["run_id"]
    url = f"/v1/runs/{rid}/login-code"
    code = "K4-" + "93x7"
    bodies = []
    r = await client.post(url, json={"code": code, "site_id": "site_a"}, headers=headers(secret))
    bodies.append((r.status, await r.text()))                  # not waiting for a code
    fake.code_sites[rid] = "site_a"
    for bad in ({"code": code + "xxxxxxx", "site_id": "site_a"}, {"code": code},
                {"code": code, "site_id": "site_a", "note": code}):
        r = await client.post(url, json=bad, headers=headers(secret))
        bodies.append((r.status, await r.text()))
    r = await client.post(url, json={"code": code, "site_id": "site_a"}, headers=headers(secret))
    bodies.append((r.status, await r.text()))
    assert [b[0] for b in bodies] == [503, 400, 400, 400, 200], bodies
    assert fake.codes == [code]
    assert code not in json.dumps(bodies) and code not in caplog.text
    events = await (await client.get(f"/v1/runs/{rid}/events?after=0", headers=headers(secret))).text()
    assert code not in events


async def test_request_cancel_is_the_paired_requesters_only(aiohttp_client, tmp_path):
    """The Stop of one request takes the paired requester's credential and nothing else; with
    it, a run's request is cancelled and a request id with no run yet is recorded, in one call."""
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    run = await (await client.post("/v1/runs", json=run_body("req-a"),
                                   headers=headers(secret))).json()
    body = {"request_ids": ["req-a", "req-none"]}
    for auth in ("Bearer wrong", f"Bearer {secret[:-1]}", None):
        hdrs = {PROTOCOL_HEADER: "1", **({"Authorization": auth} if auth else {})}
        resp = await client.post("/v1/requests/cancel", json=body, headers=hdrs)
        assert resp.status == 401
    assert fake.store.get_run(run["run_id"]).state not in ("cancelled", "finishing")
    resp = await client.post("/v1/requests/cancel", json=body, headers=headers(secret))
    assert resp.status == 200 and await resp.json() == {"requests": [
        {"request_id": "req-a", "run_id": run["run_id"], "stopped": True, "state": "cancelled"},
        {"request_id": "req-none", "run_id": None, "stopped": True, "state": "cancelled"}]}
    got = await (await client.get(f"/v1/runs/{run['run_id']}", headers=headers(secret))).json()
    assert got["stopped"] is True and got["state"] == "cancelled"
    for bad in ({}, {"request_ids": []}, {"request_ids": ["a", "a"]},
                {"request_ids": [f"r{i}" for i in range(9)]}, {"request_ids": ["a"], "x": 1}):
        resp = await client.post("/v1/requests/cancel", json=bad, headers=headers(secret))
        assert resp.status == 400, bad
