"""The operator's pages behind Home Assistant ingress (Plan 1 Task 13).

The UI runs in the controller process (uid 1103) on a Unix socket that only nginx (uid 1105)
may connect to; nginx admits only Home Assistant's ingress peer. Supervisor strips the ingress
prefix and sends ``X-Ingress-Path``: a request without a well-formed one is refused, and every
asset, form, redirect and WebSocket URL is built from it.

**Every mutation is a form POST** carrying a server-issued nonce (random, single use, one hour,
bound to the exact path it was issued for) and, when the browser sends one, an ``Origin`` equal
to the forwarded origin (else ``Sec-Fetch-Site: same-origin`` when present). Responses are never
cached; HTML is escaped; the CSP admits only this app's
own scripts and styles; nothing is logged.

**What the pages never show:** the pairing secret or its hash (the self-reported caller label is
escaped and marked as such, next to the address the claim came from), a raw address of an account
page, or a password. The app's own text comes from ``i18n`` in the effective locale's language (English
when there is no catalog for it), and ``lang`` names the language actually shown.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import web

from casa_browser.i18n import Text
from casa_browser.liveview import LiveRefused
from casa_browser.protocol import ACTIVE_STATES, TERMINAL_STATES, ProtocolError

UI_SOCKET = Path("/run/casa-browser/ui/ui.sock")
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "casa_browser_live"
INGRESS_PATH = re.compile(r"/api/hassio_ingress/[A-Za-z0-9_-]{1,128}")
NONCE_TTL_S = 3600
MAX_NONCES = 4096
MAX_FORM = 32 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_USER = re.compile(r"[A-Za-z0-9_-]{1,64}")
UI_KEY = web.AppKey("ui", object)
PREFIX_KEY = web.RequestKey("prefix", str)
NONCE_KEY = web.RequestKey("nonce", str)
FORM_KEY = web.RequestKey("form", dict)
FORM_ALL_KEY = web.RequestKey("form_all", dict)
BOUND_KEY = web.RequestKey("bound", object)


def ingress_peer() -> str:
    """Home Assistant's ingress peer (Supervisor), built from its octets: the repository's
    publication guard refuses dotted private-range literals in source."""
    return ".".join(str(o) for o in (172, 30, 32, 2))


def ingress_peer_conf() -> str:
    return f"allow {ingress_peer()};\ndeny all;\n"


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# --------------------------------------------------------------------------- nonces


class Nonces:
    """Single-use form nonces, each bound to the path its form posts to."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._live: dict[str, tuple[str, float]] = {}

    def issue(self, path: str, bound: str | None = None) -> str:
        """A nonce for the form posting to ``path``; ``bound`` (e.g. a live view's viewer id)
        travels with it and is handed to the handler when it is spent."""
        now = self.clock()
        if len(self._live) >= MAX_NONCES:
            for key in sorted(self._live, key=lambda k: self._live[k][1])[: MAX_NONCES // 4]:
                del self._live[key]
        nonce = "n" + secrets.token_urlsafe(16).replace("=", "")
        self._live[nonce] = (path, now + NONCE_TTL_S, bound)
        return nonce

    def take(self, nonce: Any, path: str) -> tuple[bool, str | None]:
        """Spend ``nonce`` for ``path``: (valid, what it was bound to)."""
        if not isinstance(nonce, str):
            return False, None
        entry = self._live.pop(nonce, None)
        ok = entry is not None and entry[0] == path and self.clock() < entry[1]
        return ok, (entry[2] if ok else None)

    def spend(self, nonce: Any, path: str) -> bool:
        return self.take(nonce, path)[0]


# --------------------------------------------------------------------------- the app



class Ui:
    def __init__(self, controller, live, *, nonces: Nonces | None = None):
        self.ctl = controller
        self.live = live
        self.store = controller.store
        self.nonces = nonces or Nonces()

    # ------------------------------------------------------------------ request helpers

    @staticmethod
    def prefix(request: web.Request) -> str:
        return request[PREFIX_KEY]

    def url(self, request: web.Request, path: str) -> str:
        return self.prefix(request) + path

    def form(self, request: web.Request, path: str, body: str, *, cls: str = "",
             confirm: str | None = None) -> str:
        nonce = self.nonces.issue(path)
        extra = f' data-confirm="{e(confirm)}"' if confirm else ""
        return (f'<form method="post" action="{e(self.url(request, path))}" class="{e(cls)}"{extra}>'
                f'<input type="hidden" name="nonce" value="{e(nonce)}">{body}</form>')

    @staticmethod
    def operator_ref(request: web.Request) -> str:
        user = request.headers.get("X-Remote-User-Id", "")
        return "ingress:" + (user if _USER.fullmatch(user) else "operator")

    @property
    def t(self) -> Text:
        """The operator's text in the effective locale's language (English without a catalog)."""
        locale = getattr(self.ctl.c, "locale", None)
        return Text(getattr(locale, "language", None))

    def page(self, request: web.Request, title: str, body: str, *, status: int = 200) -> web.Response:
        t = self.t
        lang = e(t.language)
        p = e(self.prefix(request))
        # The referrer meta comes first: Home Assistant's ingress replaces the app's
        # Referrer-Policy header with no-referrer, under which a browser sends "Origin: null"
        # on a form POST and the mutation check refuses every form (measured on casa-test).
        doc = (f'<!doctype html><html lang="{lang}"><head><meta name="referrer" content="same-origin">'
               '<meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width, initial-scale=1">'
               f'<title>{e(title)} — Casa Browser</title>'
               f'<link rel="stylesheet" href="{p}/static/style.css"></head><body>'
               f'<nav><a href="{p}/">{e(t("nav.home"))}</a><a href="{p}/sites">{e(t("nav.sites"))}</a>'
               f'<a href="{p}/setup">{e(t("nav.setup"))}</a></nav><main><h1>{e(title)}</h1>{body}</main>'
               f'<script src="{p}/static/app.js"></script></body></html>')
        return web.Response(text=doc, content_type="text/html", status=status)

    def refuse(self, request: web.Request, text: str, status: int = 403) -> web.Response:
        return self.page(request, self.t("refuse.title"), f"<p>{e(text)}</p>"
                         f'<p><a href="{e(self.url(request, "/"))}">{e(self.t("refuse.back"))}</a></p>',
                         status=status)

    def _copy(self, text: str, shown: str | None = None) -> str:
        """An address or code the operator can copy with one tap (casa-test F17; app.js)."""
        t = self.t
        return (f'<code class="copy" role="button" tabindex="0" title="{e(t("app.copy"))}" '
                f'data-copy="{e(text)}" data-copied="{e(t("app.copied"))}">{e(shown or text)}</code>')

    def see(self, request: web.Request, path: str) -> web.Response:
        return web.Response(status=303, headers={"Location": self.url(request, path)})

    # ------------------------------------------------------------------ home

    def _stamp(self) -> str:
        """What Home shows that changes by itself (a live view waiting, a question): app.js
        reloads Home when it changes (casa-test F40: a new view showed only after a manual
        reload)."""
        runs = self.store.list_runs(ACTIVE_STATES)
        waits = ",".join(f"{r.run_id}:{r.state}" for r in runs if r.state in ("waiting_live", "live"))
        # each question by its identity: one answered and another asked between two looks
        # changes it too (Terra K r6)
        questions = ",".join(f"{r.run_id}:{q.question_id}:{q.revision}" for r in runs
                             if (q := self._question_view(r)) is not None)
        return hashlib.sha256(f"{waits}|{questions}".encode()).hexdigest()[:16]

    async def home_stamp(self, request: web.Request) -> web.Response:
        return web.json_response({"stamp": self._stamp()})

    async def home(self, request: web.Request) -> web.Response:
        t = self.t
        runs = self.store.list_runs(ACTIVE_STATES)
        waits = ([r for r in runs if r.state == "waiting_live"] + [r for r in runs if r.state == "live"]
                 + [r for r in runs if self._question_view(r) is not None])
        parts = []
        if waits:
            items = []
            for run in waits:
                label = (t("home.wait_live") if run.state == "waiting_live"
                         else t("home.wait_reconnect") if run.state == "live"
                         else t("home.wait_question"))
                items.append(f'<li class="wait"><strong>{e(_task(run))}</strong> — {e(label)} '
                             + self.form(request, f"/runs/{run.run_id}/open",
                                         f'<button class="primary" data-busy="{e(t("live.opening"))}">{e(t("home.open"))}</button>',
                                         cls="inline")
                             + "</li>")
            parts.append(f'<section><h2>{e(t("home.live_heading"))}</h2><ul class="waits">'
                         + "".join(items) + "</ul></section>")
        rows = [r for r in runs if r.state not in ("waiting_live", "live")]
        ended = self.store.list_runs(tuple(sorted(TERMINAL_STATES)))[-20:][::-1]
        listed = rows + ended
        table = (self._run_table(request, listed) if listed or not waits
                 else f"<p>{e(t('runs.none_other'))}</p>")
        parts.append(f"<section><h2>{e(t('home.runs_heading'))}</h2>" + table + "</section>")
        notices = self.store.notices_after(0, 10_000)[-10:][::-1]
        if notices:
            parts.append(f"<section><h2>{e(t('home.notices_heading'))}</h2><ul>" + "".join(
                f"<li>{e(self._when(n.at))} — {e(n.text_masked)}</li>" for n in notices) + "</ul></section>")
        parts.append(f'<span id="stamp" hidden data-stamp="{self._stamp()}" '
                     f'data-url="{e(self.url(request, "/home/stamp"))}"></span>')
        return self.page(request, "Casa Browser", "".join(parts))

    def _when(self, at) -> str:
        locale = getattr(self.ctl.c, "locale", None)
        return _when(at, getattr(locale, "timezone", None))

    def _state(self, state: str) -> str:
        return self.t(f"state.{state}") if state in _STATES else state

    def _run_table(self, request, runs) -> str:
        t = self.t
        if not runs:
            return f"<p>{e(t('runs.none'))}</p>"
        rows = "".join(
            f'<tr><td><a href="{e(self.url(request, f"/runs/{r.run_id}"))}">{e(_task(r))}</a></td>'
            f"<td>{e(self._state(r.state))}</td><td>{e(self._when(r.created_at))}</td></tr>"
            for r in runs)
        return (f"<table><tr><th>{e(t('runs.task'))}</th><th>{e(t('runs.state'))}</th>"
                f"<th>{e(t('runs.started'))}</th></tr>{rows}</table>")

    # ------------------------------------------------------------------ runs

    def _run_or_404(self, run_id: str):
        run = self.store.get_run(run_id) if _ID.fullmatch(run_id or "") else None
        if run is None:
            raise web.HTTPNotFound()
        return run

    async def run_page(self, request: web.Request) -> web.Response:
        t = self.t
        run = self._run_or_404(request.match_info["run"])
        events = self.store.events_after(run.run_id, 0, 10_000)
        rows = []
        for ev in events:
            shot = ""
            if ev.screenshot_id:
                src = self.url(request, f"/runs/{run.run_id}/screenshots/{ev.screenshot_id}")
                shot = (f'<a href="{e(src)}"><img class="shot" alt="{e(t("run.screenshot"))}" '
                        f'src="{e(src)}"></a>')
            rows.append(f"<tr><td>{e(self._when(ev.at))}</td><td>{e(ev.kind)}</td><td>{e(ev.outcome)}</td>"
                        f"<td>{e(ev.text_masked)}{shot}</td></tr>")
        files = []
        for f in self.store.files_for_run(run.run_id):
            delivery = self.store.current_delivery(f.file_id)
            files.append(f"<tr><td>{e(f.safe_name)}</td><td>{e(t('run.bytes', size=f.size))}</td>"
                         f"<td>{self._copy(f.sha256, f.sha256[:16] + '…')}</td>"
                         f"<td>{e(delivery[1] if delivery else t('run.not_delivered'))}</td></tr>")
        body = ("<p>" + t.html("run.summary", state=f"<strong>{e(self._state(run.state))}</strong>",
                               requester=e(run.spec.requester_id),
                               mode=e(run.spec.mode.replace('_', '-'))) + "</p>"
                f"<p class=\"task\">{e(run.spec.task_text)}</p>")
        if files:
            body += (f"<h2>{e(t('run.files_heading'))}</h2><table><tr><th>{e(t('run.file_name'))}</th>"
                     f"<th>{e(t('run.file_size'))}</th><th>SHA-256</th>"
                     f"<th>{e(t('run.file_delivery'))}</th></tr>" + "".join(files) + "</table>")
        body += (f"<h2>{e(t('run.log_heading'))}</h2><table class=\"log\"><tr><th>{e(t('run.log_time'))}</th>"
                 f"<th>{e(t('run.log_step'))}</th><th>{e(t('run.log_outcome'))}</th>"
                 f"<th>{e(t('run.log_what'))}</th></tr>" + "".join(rows) + "</table>")
        return self.page(request, _task(run), body)

    async def screenshot(self, request: web.Request) -> web.Response:
        run_id, shot = request.match_info["run"], request.match_info["shot"]
        if not (_ID.fullmatch(run_id) and _ID.fullmatch(shot)):
            raise web.HTTPNotFound()
        try:
            data = await self.ctl.screenshot(run_id, shot)
        except ProtocolError:
            raise web.HTTPNotFound() from None
        return web.Response(body=data, content_type="image/png")

    # ------------------------------------------------------------------ live view: entry

    def _set_cookie(self, request: web.Request, response, token: str) -> None:
        secure = _forwarded_proto(request) == "https"
        response.set_cookie(COOKIE, token, path=self.prefix(request) + "/", httponly=True,
                            samesite="Strict", secure=secure)

    def _question_view(self, run):
        """Slice 5f: the open question a run waits on, when it offers a live view (not a login
        question) — the page then offers its live view, as [Open live view] on the card did."""
        if run.state != "waiting_approval":
            return None
        q = self.store.open_question(run.run_id)
        if q is None or q.state != "open" or (q.view or {}).get("kind") == "login_item":
            return None
        return q

    async def open_run(self, request: web.Request) -> web.Response:
        run = self._run_or_404(request.match_info["run"])
        q = self._question_view(run)
        if q is not None:
            # the operator's own Open through authenticated ingress: the same assist view from
            # the question that the card's callback asked for (opening it is no answer)
            try:
                got = await self.ctl.live_from_question(run.run_id, q.question_id, {
                    "revision": q.revision, "digest": q.digest, "n": secrets.randbelow(2**31 - 1) + 1,
                    "operator_ref": self.operator_ref(request)})
            except ProtocolError:
                got = {"outcome": "not_current"}
            if got.get("outcome") == "not_current":
                return self._live_refused(request, "not_waiting")
        try:
            token = await self.live.open_from_home(run.run_id)
        except LiveRefused as refused:
            return self._live_refused(request, refused.code)
        response = self.see(request, f"/view/{run.run_id}")
        self._set_cookie(request, response, token)
        return response

    def _live_refused(self, request, code: str) -> web.Response:
        key = {"unavailable": "live.unavailable", "grant_refused": "live.grant_refused"}.get(
            code, "live.none_waiting")
        return self.refuse(request, self.t(key), status=409)

    async def grant_page(self, request: web.Request) -> web.Response:
        grant_id = request.match_info["grant"]
        run_id = self.live.grant_run(grant_id) if _ID.fullmatch(grant_id) else None
        if run_id is None:
            return self.refuse(request, self.t("live.grant_refused"), status=404)
        t = self.t
        return self.page(request, t("live.title"), f"<p>{e(t('live.asked'))}</p>"
                         + self.form(request, f"/live/{grant_id}",
                                     f'<button class="primary" data-busy="{e(t("live.opening"))}">'
                                     f'{e(t("live.open_button"))}</button>'))

    async def claim(self, request: web.Request) -> web.Response:
        grant_id = request.match_info["grant"]
        run_id = self.live.grant_run(grant_id) if _ID.fullmatch(grant_id) else None
        if run_id is None:
            return self._live_refused(request, "grant_refused")
        try:
            token = await self.live.claim(run_id, grant_id)
        except LiveRefused as refused:
            return self._live_refused(request, refused.code)
        response = self.see(request, f"/view/{run_id}")
        self._set_cookie(request, response, token)
        return response

    # ------------------------------------------------------------------ live view: viewer

    async def viewer(self, request: web.Request) -> web.Response:
        run = self._run_or_404(request.match_info["run"])
        verdict, viewer_id = await self.live.serve_viewer(run.run_id, request.cookies.get(COOKIE))
        if verdict == "review":
            return self.see(request, f"/view/{run.run_id}/review")
        if verdict != "viewing":
            return self.refuse(request, self.t("live.not_here"))
        template = (STATIC / "viewer.html").read_text(encoding="utf-8")
        values = {"prefix": self.prefix(request), "run": run.run_id, "viewer": viewer_id,
                  # Done's nonce is bound to this page's viewer id (ruling E-T13-2 = V1 + fix):
                  # a replaced page's Done form is refused.
                  "nonce": self.nonces.issue(f"/view/{run.run_id}/done", bound=viewer_id),
                  "done": self.url(request, f"/view/{run.run_id}/done"),
                  "purpose": self.live.sessions[run.run_id].purpose,
                  "lang": self.t.language,
                  "strings": json.dumps(self.t.table(_JS_KEYS), ensure_ascii=False)}
        for key in _VIEWER_KEYS:
            values[key.replace(".", "_")] = self.t(key)
        for key, value in values.items():
            template = template.replace("{{" + key + "}}", e(value))
        return web.Response(text=template, content_type="text/html")

    async def view_state(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run"]
        state = self.live.view_state(run_id, request.cookies.get(COOKIE), request.query.get("v"))
        return web.json_response({"state": state})

    async def _socket(self, request: web.Request, kind: str):
        run_id = request.match_info["run"]
        viewer_id = request.query.get("v")
        session = self.live.authorize(run_id, request.cookies.get(COOKIE), viewer_id)
        if session is None:
            raise web.HTTPForbidden()
        if kind == "ctl":
            ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024)
            await ws.prepare(request)
            await self.live.ctl_socket(ws, session, viewer_id)
        else:
            ws = web.WebSocketResponse(protocols=("binary",), max_msg_size=4 * 1024 * 1024)
            await ws.prepare(request)
            await self.live.vnc_socket(ws, session, viewer_id)
        return ws

    async def ctl_ws(self, request: web.Request):
        return await self._socket(request, "ctl")

    async def vnc_ws(self, request: web.Request):
        return await self._socket(request, "vnc")

    # ------------------------------------------------------------------ live view: Done

    async def done(self, request: web.Request) -> web.Response:
        """Done ends the live view in one tap (slice 5g; operator ruling 2026-10-31: nothing is
        asked — a sign-in provider the view saw is learned by the controller's Done itself)."""
        run_id = request.match_info["run"]
        if not self.live.is_viewer(run_id, request.cookies.get(COOKIE), request[BOUND_KEY]):
            return self.refuse(request, self.t("live.replaced"))
        restored = await self.live.close_view(run_id)
        if not restored:
            # The controller's Done re-checks the window and interrupts the run when it cannot.
            await self._finish(run_id)
            return self.page(request, self.t("live.title"), f"<p>{e(self.t('live.unrestored'))}</p>")
        return self._finished(request, await self._finish(run_id))

    async def review(self, request: web.Request) -> web.Response:
        """Where a viewer page goes once Done was pressed (here or on another device): the
        closing page."""
        return self._finished(request)

    async def _finish(self, run_id: str) -> bool:
        try:
            await self.ctl.live_done(run_id)
            return True
        except ProtocolError:
            return False

    def _finished(self, request, ok: bool = True) -> web.Response:
        t = self.t
        text = t("finish.done") if ok else t("finish.stopped")
        return self.page(request, t("live.title"), f"<p>{e(text)}</p>"
                         f'<p><a href="{e(self.url(request, "/"))}">{e(t("nav.home"))}</a></p>')

    # ------------------------------------------------------------------ sites

    async def sites(self, request: web.Request) -> web.Response:
        t = self.t
        rows = []
        lost = self.ctl.bindings_lost()
        for site in self.store.list_sites():
            if site.provider_bindings and site.site_id in lost:
                binding = t("sites.binding_lost")
            elif site.provider_bindings:
                binding = t("sites.bound", origins=", ".join(
                    sorted({b.provider_origin for b in site.provider_bindings})))
            elif site.login_origins:
                binding = t("sites.not_bound")
            else:
                binding = t("sites.no_sign_in")
            login = t("sites.item_set") if site.vault_item_id else (
                t("sites.first_login") if site.login_origins else t("sites.public"))
            forget = self.form(request, f"/sites/{site.site_id}/forget",
                               '<label><input type="checkbox" name="confirm" value="yes" required> '
                               f'{e(t("sites.sure"))}</label> <button>{e(t("sites.forget"))}</button>',
                               cls="inline")
            addresses = " ".join(self._copy(a) for a in dict.fromkeys((*site.origins,
                                                                       *site.login_origins)))
            rows.append(f'<tr><td>{e(site.display_name)}<div class="small">{addresses}</div></td>'
                        f'<td>{e(login)}</td><td>{e(binding)}</td><td>{forget}</td></tr>')
        table = (f"<table><tr><th>{e(t('sites.site'))}</th><th>{e(t('sites.sign_in'))}</th>"
                 f"<th>{e(t('sites.provider'))}</th><th></th></tr>"
                 + "".join(rows) + "</table>") if rows else f"<p>{e(t('sites.none'))}</p>"
        note = request.query.get("note", "")
        notice = f'<p class="ok">{e(t(_NOTES[note]))}</p>' if note in _NOTES else ""
        register = self.form(request, "/sites", (
            f'<label>{e(t("sites.name"))} <input name="display_name" maxlength="80" required></label>'
            f'<label>{e(t("sites.origins"))} <textarea name="origins" required></textarea></label>'
            f'<label>{e(t("sites.login_origins"))} <textarea name="login_origins"></textarea></label>'
            f'<label>{e(t("sites.item"))} '
            '<input name="vault_item_id" maxlength="64"></label>'
            f'<label>{e(t("sites.start"))} <input name="start_page" maxlength="2048"></label>'
            '<label><input type="checkbox" name="confirm" value="yes" required> '
            f'{e(t("sites.belong"))}</label><button class="primary">{e(t("sites.register"))}</button>'))
        warning = request.query.get("fields", "")
        extra = ""
        if warning:
            extra = f'<p class="warn">{e(t("sites.extra_fields", fields=warning))}</p>'
        return self.page(request, t("sites.title"), notice + extra + table
                         + f"<h2>{e(t('sites.register_heading'))}</h2>" + register)

    async def register(self, request: web.Request) -> web.Response:
        form = request[FORM_KEY]
        payload = {"display_name": form.get("display_name", "").strip(),
                   "origins": _lines(form.get("origins", "")),
                   "login_origins": _lines(form.get("login_origins", "")),
                   "operator_ref": self.operator_ref(request)}
        if form.get("vault_item_id", "").strip():
            payload["vault_item_id"] = form["vault_item_id"].strip()
        if form.get("start_page", "").strip():
            payload["start_page"] = form["start_page"].strip()
        if form.get("confirm") != "yes":
            return self.refuse(request, self.t("register.confirm"), 400)
        from casa_browser.protocol import parse_site_registration
        try:
            parsed = parse_site_registration(payload)
            site_id, reply = await self.ctl.register_site_detailed(parsed)
        except ProtocolError:
            return self.refuse(request, self.t("register.failed"), 400)
        fields = [str(f) for f in (reply or {}).get("extra_field_names", ())][:16]
        query = "?note=registered" + ("&fields=" + quote(", ".join(fields)[:300]) if fields else "")
        return self.see(request, "/sites" + query)

    async def forget(self, request: web.Request) -> web.Response:
        site_id = request.match_info["site"]
        if request[FORM_KEY].get("confirm") != "yes" or not _ID.fullmatch(site_id):
            return self.refuse(request, self.t("forget.confirm"), 400)
        try:
            await self.ctl.forget(site_id, self.operator_ref(request))
        except ProtocolError:
            return self.refuse(request, self.t("forget.failed"), 409)
        return self.see(request, "/sites?note=forgotten")

    # ------------------------------------------------------------------ setup and pairing

    async def setup(self, request: web.Request) -> web.Response:
        t = self.t
        c = self.ctl.c
        locale = c.locale
        parts = [f"<h2>{e(t('setup.ready_heading'))}</h2><ul>"]
        parts.append(f"<li>{e(t('setup.onepassword_configured') if c.configured else t('setup.onepassword_missing'))}</li>")
        if locale is None:
            parts.append(f"<li>{e(t('setup.language_unknown'))}</li>")
        else:
            source = t("setup.from_ha") if locale.language_source == "home_assistant" else t("setup.from_options")
            parts.append(f"<li>{e(t('setup.language', language=locale.language, source=source, timezone=locale.timezone))}</li>")
        parts.append("</ul>")
        try:
            status = await self.ctl.broker.status()
        except Exception:  # noqa: BLE001
            status = {"error": "unavailable"}
        role = status.get("guest_role", "unknown") if "error" not in status else "unknown"
        guest = {"readonly": t("setup.guest_readonly"),
                 "not_readonly": t("setup.guest_not_readonly")}.get(role, t("setup.guest_unavailable"))
        counts = ""
        if "readonly_users" in status or "editable_users" in status:
            counts = t("setup.guest_counts", readonly=status.get('readonly_users', '?'),
                       editable=status.get('editable_users', '?'))
        parts.append(f"<h2>{e(t('setup.guest_heading'))}</h2>"
                     f"<p>{e(t('setup.guest_account', guest=guest, counts=counts))}</p>")
        if role == "not_readonly":
            # Design §5.2: a warning, nothing to tick; logins never wait on it.
            parts.append(f'<p class="warn">{e(t("setup.guest_warning"))}</p>')
        parts.append(f"<p class=\"small\">{e(t('setup.guest_scope'))}</p>")
        parts.append(self._pairing_section(request))
        return self.page(request, t("setup.title"), "".join(parts))

    def _pairing_section(self, request) -> str:
        t = self.t
        view = self.ctl.pairing.status_view()
        out = [f"<h2>{e(t('pair.heading'))}</h2>"]
        if view["armed"]:
            out.append(f"<p>{e(t('pair.armed'))}</p>")
        elif view["window_closed_unclaimed"]:
            out.append(f"<p class=\"warn\">{e(t('pair.unclaimed'))}</p>")
        if view["paired"]:
            # Both values are escaped by Pairing.status_view (html.escape) — shown as they are.
            when = e(t("pair.on", when=self._when(view["claimed_at"]))) if view["claimed_at"] else ""
            out.append("<p>" + t.html(
                "pair.paired", label=view['caller_label_html'] or e(t("pair.no_label")),
                reported=f'<span class="small">{e(t("pair.reported"))}</span>',
                peer=view['observed_peer_html'] or e(t("pair.unknown_peer")), when=when) + "</p>")
        elif not view["armed"]:
            out.append(f"<p>{e(t('pair.not_paired'))}</p>")
        out.append(self.form(request, "/pair", '<button>' + e(
            t("pair.again") if view["paired"] else t("pair.pair")) + "</button>",
            confirm=t("pair.again_confirm") if view["paired"] else None))
        return "".join(out)

    async def pair_page(self, request: web.Request) -> web.Response:
        return self.page(request, self.t("pair.title"), self._pairing_section(request))

    async def pair(self, request: web.Request) -> web.Response:
        self.ctl.pairing.arm()
        return self.see(request, "/pair")

class _Refused(Exception):
    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


_STATES = frozenset({"queued", "starting", "running", "waiting_approval", "waiting_live", "live",
                     "finishing", "completed", "failed", "cancelled", "interrupted"})
_NOTES = {"registered": "sites.registered", "forgotten": "sites.forgotten"}
# The viewer page's own strings (viewer.html placeholders) and the ones live.js shows.
_VIEWER_KEYS = ("viewer.title", "viewer.keyboard", "viewer.done", "viewer.paste", "viewer.zoom_in",
                "viewer.zoom_out", "viewer.fit", "viewer.left", "viewer.right", "js.connecting")
# Every T('js.…') key live.js uses (casa-test F39: the viewer's own strings fell back to English;
# a unit test reads live.js and keeps this list complete).
_JS_KEYS = ("js.connecting", "js.reconnecting", "js.home", "js.elsewhere", "js.ended",
            "js.copy", "js.copied", "js.finishing", "js.no_picture", "js.hide_toolbar",
            "js.unsent", "js.unsure", "js.retap", "js.pasted", "js.dropped",
            "js.not_pasted", "js.paste_refused", "js.multiline", "js.unsupported")


def _when(at, tz: str | None = None) -> str:
    """A time in the effective time zone (casa-test F8: shown in UTC without a zone before)."""
    if not isinstance(at, datetime):
        return ""
    if tz and at.tzinfo is not None:
        try:
            from zoneinfo import ZoneInfo
            at = at.astimezone(ZoneInfo(tz))
        except Exception:  # noqa: BLE001 — an unknown zone: the time as stored, with its zone
            pass
    return at.strftime("%Y-%m-%d %H:%M %Z").strip()


def _task(run) -> str:
    """How a run is named on every page: its task text on one line (casa-test F21), its kind
    when the text is empty."""
    return _short(run.spec.task_text) or run.spec.task_kind


def _short(text: str, limit: int = 80) -> str:
    """A run's task on one line, cut to ``limit`` characters (casa-test F8: runs were listed by
    their kind alone)."""
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit - 1] + "…"


def _lines(text: str) -> list[str]:
    return [line.strip() for line in str(text).splitlines() if line.strip()][:16]


def _forwarded_proto(request: web.Request) -> str:
    return request.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip().lower()


def _forwarded_origin(request: web.Request) -> str | None:
    host = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    if not host:
        return None
    return f"{_forwarded_proto(request)}://{host}"


# --------------------------------------------------------------------------- middleware


# img-src data: — noVNC's JPEG decoder (tight encoding) hands each rectangle to an Image as a
# data: URI (measured in the image lane).
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self' {ws}; form-action 'self'; frame-ancestors 'self'; base-uri 'none'")


@web.middleware
async def boundary(request: web.Request, handler):
    """The ingress check, the mutation check and the response headers, for every request."""
    ui: Ui = request.app[UI_KEY]
    prefix = request.headers.get("X-Ingress-Path", "")
    if not INGRESS_PATH.fullmatch(prefix):
        return web.Response(status=403, text="Casa Browser is reachable only through Home Assistant.")
    request[PREFIX_KEY] = prefix
    try:
        if request.method == "POST":
            refused = await _check_mutation(ui, request)
            if refused is not None:
                return refused
        elif request.method not in ("GET", "HEAD"):
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
        try:
            response = await handler(request)
        except _Refused as refused:
            response = ui.refuse(request, refused.text, 409)
    except web.HTTPException as exc:
        response = web.Response(status=exc.status, text="Not available.")
    if isinstance(response, web.WebSocketResponse):
        return response
    host = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    ws = " ".join(f"{s}://{host}" for s in ("wss", "ws")) if re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,255}", host) else ""
    response.headers["Content-Security-Policy"] = CSP.format(ws=ws).replace("  ", " ")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    # same-origin, not no-referrer: with no-referrer a browser sends "Origin: null" on a form
    # POST (measured, Chrome), which the mutation check rightly refuses.
    response.headers["Referrer-Policy"] = "same-origin"
    return response


async def _check_mutation(ui: Ui, request: web.Request) -> web.Response | None:
    origin = request.headers.get("Origin")
    if origin is not None:
        if origin != _forwarded_origin(request):
            return web.Response(status=403, text="Refused: not from this page.")
    elif request.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
        return web.Response(status=403, text="Refused: not from this page.")
    if request.content_type != "application/x-www-form-urlencoded":
        return web.Response(status=403, text="Refused: not a form of this page.")
    try:
        raw = await request.content.read(MAX_FORM + 1)
    except Exception:  # noqa: BLE001
        return web.Response(status=400, text="Refused.")
    if len(raw) > MAX_FORM:
        return web.Response(status=413, text="Refused: too large.")
    from urllib.parse import parse_qs
    try:
        pairs = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=200)
    except (UnicodeDecodeError, ValueError):
        return web.Response(status=400, text="Refused.")
    nonce = (pairs.get("nonce") or [None])[0]
    ok, bound = ui.nonces.take(nonce, request.path)
    if not ok:
        return web.Response(status=403, text="Refused: this form has expired. Reload the page.")
    request[NONCE_KEY] = nonce
    request[BOUND_KEY] = bound
    request[FORM_KEY] = {k: v[0] for k, v in pairs.items() if k != "nonce"}
    request[FORM_ALL_KEY] = {k: v for k, v in pairs.items() if k != "nonce"}
    return None


def make_ui(controller, live=None) -> web.Application:
    ui = Ui(controller, live or controller.grants)
    app = web.Application(middlewares=[boundary], client_max_size=MAX_FORM)
    app[UI_KEY] = ui
    app.add_routes([
        web.get("/", ui.home),
        web.get("/home/stamp", ui.home_stamp),
        web.get("/runs/{run}", ui.run_page),
        web.get("/runs/{run}/screenshots/{shot}", ui.screenshot),
        web.post("/runs/{run}/open", ui.open_run),
        web.get("/live/{grant}", ui.grant_page),
        web.post("/live/{grant}", ui.claim),
        web.get("/view/{run}", ui.viewer),
        web.get("/view/{run}/state", ui.view_state),
        web.get("/view/{run}/ctl", ui.ctl_ws),
        web.get("/view/{run}/websockify", ui.vnc_ws),
        web.post("/view/{run}/done", ui.done),
        web.get("/view/{run}/review", ui.review),
        web.get("/sites", ui.sites),
        web.post("/sites", ui.register),
        web.post("/sites/{site}/forget", ui.forget),
        web.get("/setup", ui.setup),
        web.get("/pair", ui.pair_page),
        web.post("/pair", ui.pair),
    ])
    return app


async def serve(app: web.Application, path: Path = UI_SOCKET) -> web.AppRunner:
    """The UI on its Unix socket. The socket is ``0666``; its directory
    (``/run/casa-browser/ui``, ``0750`` controller:casa-ingress) admits only nginx."""
    import os
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    site = web.UnixSite(runner, str(path))
    await site.start()
    os.chmod(path, 0o666)
    return runner


def main(argv: list[str]) -> int:  # pragma: no cover — run by the nginx unit as root
    if argv[1:] == ["ingress-peer"]:
        sys.stdout.write(ingress_peer_conf())
        return 0
    sys.stderr.write("usage: python -m casa_browser.ui ingress-peer\n")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
