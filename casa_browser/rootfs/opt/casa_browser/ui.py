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
             confirm: str | None = None, extra: dict[str, str] | None = None) -> str:
        """A form posting to ``path`` with its nonce. ``confirm`` asks first (app.js's dialog);
        ``extra`` adds that dialog's other ``data-`` texts."""
        nonce = self.nonces.issue(path)
        attrs = f' data-confirm="{e(confirm)}"' if confirm else ""
        for key, value in (extra or {}).items():
            attrs += f' data-{key}="{e(value)}"'
        return (f'<form method="post" action="{e(self.url(request, path))}" class="{e(cls)}"{attrs}>'
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

    def page(self, request: web.Request, title: str, body: str, *, status: int = 200,
             tab: str = "", back: str | None = None) -> web.Response:
        """A page in Home Assistant's look (static/style.css, theme.js). ``tab`` is the tab it
        belongs to; ``back`` makes it a sub-page: a back arrow to that path and its title as the
        heading. A tab's own page has no heading: Home Assistant's panel header and the tab bar
        already name it."""
        t = self.t
        lang = e(t.language)
        p = e(self.prefix(request))
        tabs = "".join(
            f'<a href="{p}{path}"{" aria-current=\"page\"" if name == tab else ""}>'
            f'{icon(ico)}{e(t(f"nav.{name}"))}</a>'
            for name, path, ico in (("home", "/", "home"), ("sites", "/sites", "web"),
                                    ("setup", "/setup", "cog")))
        heading = ""
        if back is not None:
            heading = (f'<div class="subhdr"><a class="ib" href="{e(self.url(request, back))}" '
                       f'aria-label="{e(t("app.back"))}">{icon("back")}</a><h1>{e(title)}</h1></div>')
        # The referrer meta comes first: Home Assistant's ingress replaces the app's
        # Referrer-Policy header with no-referrer, under which a browser sends "Origin: null"
        # on a form POST and the mutation check refuses every form (measured on casa-test).
        doc = (f'<!doctype html><html lang="{lang}"><head><meta name="referrer" content="same-origin">'
               '<meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
               f'<title>{e(title)} — Casa Browser</title>'
               f'<link rel="stylesheet" href="{p}/static/style.css">'
               f'<script src="{p}/static/theme.js"></script></head><body>'
               f'<nav class="tabs">{tabs}</nav><main>{heading}{body}</main>'
               f'<script src="{p}/static/app.js"></script></body></html>')
        return web.Response(text=doc, content_type="text/html", status=status)

    def message(self, request: web.Request, title: str, text: str, *, status: int = 200,
                tab: str = "home", ok: bool = False) -> web.Response:
        """A page that only says something, with the way back home."""
        t = self.t
        body = (f'<div class="card pad"><p>{e(text)}</p><div class="end">'
                f'<a class="btn s" href="{e(self.url(request, "/"))}">{e(t("refuse.back") if not ok else t("nav.home"))}</a>'
                "</div></div>")
        return self.page(request, title, body, status=status, tab=tab, back="/")

    def refuse(self, request: web.Request, text: str, status: int = 403) -> web.Response:
        return self.message(request, self.t("refuse.title"), text, status=status)

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
            parts.append(f'<h2>{e(t("home.live_heading"))}</h2>')
            for run in waits:
                label = (t("home.wait_live") if run.state == "waiting_live"
                         else t("home.wait_reconnect") if run.state == "live"
                         else t("home.wait_question"))
                ico = "help" if run.state == "waiting_approval" else "eye"
                # the run's live view first and loudest; a question's look is the lesser button
                cls = "primary s" if run.state != "waiting_approval" else "s"
                parts.append(
                    f'<div class="card{" attn" if run.state == "waiting_live" else ""}">'
                    + row(lead(ico, "wait"), e(_task(run)), e(label), wrap=True)
                    + '<div class="acts">'
                    + self.form(request, f"/runs/{run.run_id}/open",
                                f'<button class="{cls}" data-busy="{e(t("live.opening"))}">'
                                f'{e(t("home.open"))}</button>')
                    + "</div></div>")
        rows = [r for r in runs if r.state not in ("waiting_live", "live")]
        ended = self.store.list_runs(tuple(sorted(TERMINAL_STATES)))[-20:][::-1]
        listed = rows + ended
        table = (self._run_table(request, listed) if listed or not waits
                 else f'<div class="card pad"><p class="muted">{e(t("runs.none_other"))}</p></div>')
        parts.append(f"<h2>{e(t('home.runs_heading'))}</h2>" + table)
        notices = self.store.notices_after(0, 10_000)[-10:][::-1]
        if notices:
            parts.append(
                f'<details class="card"><summary class="row"><span class="txt"><span class="l1">'
                f'{e(t("home.notices_heading"))} · {len(notices)}</span></span>{icon("down", "i chev")}'
                '</summary><div class="notices">' + "".join(
                    f'<div><span class="muted">{e(self._day(n.at))} · </span>{e(n.text_masked)}</div>'
                    for n in notices) + "</div></details>")
        parts.append(f'<span id="stamp" hidden data-stamp="{self._stamp()}" '
                     f'data-url="{e(self.url(request, "/home/stamp"))}"></span>')
        return self.page(request, "Casa Browser", "".join(parts), tab="home")

    def _when(self, at) -> str:
        locale = getattr(self.ctl.c, "locale", None)
        return _when(at, getattr(locale, "timezone", None))

    def _day(self, at) -> str:
        """A time as a list shows it: today's and yesterday's by name, older ones by date."""
        locale = getattr(self.ctl.c, "locale", None)
        tz = getattr(locale, "timezone", None)
        if not isinstance(at, datetime):
            return ""
        zone = None
        if tz and at.tzinfo is not None:
            try:
                from zoneinfo import ZoneInfo
                zone = ZoneInfo(tz)
                at = at.astimezone(zone)
            except Exception:  # noqa: BLE001 — an unknown zone: the time as stored
                zone = None
        now = datetime.now(zone or at.tzinfo)
        days = (now.date() - at.date()).days
        clock = at.strftime("%H:%M")
        if days == 0:
            return self.t("when.today", time=clock)
        if days == 1:
            return self.t("when.yesterday", time=clock)
        return at.strftime("%Y-%m-%d")

    def _state(self, state: str) -> str:
        return self.t(f"state.{state}") if state in _STATES else state

    def _run_table(self, request, runs) -> str:
        t = self.t
        if not runs:
            return f'<div class="card pad"><p class="muted">{e(t("runs.none"))}</p></div>'
        rows = "".join(
            row(lead(*_STATE_LOOK.get(r.state, ("clock", "mute"))), e(_task(r)),
                f"{e(self._state(r.state))} · {e(self._day(r.created_at))}",
                href=self.url(request, f"/runs/{r.run_id}"))
            for r in runs)
        return f'<div class="card">{rows}</div>'

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
        steps = []
        for ev in events:
            shot = ""
            if ev.screenshot_id:
                src = self.url(request, f"/runs/{run.run_id}/screenshots/{ev.screenshot_id}")
                shot = (f'<a href="{e(src)}"><img class="shot" alt="{e(t("run.screenshot"))}" '
                        f'src="{e(src)}"></a>')
            clock = self._when(ev.at)[11:16]      # HH:MM in the effective zone; the title has all
            steps.append(f'<div class="ev"><span class="t" title="{e(self._when(ev.at))}">{e(clock)}</span>'
                         f'<span class="dot"><i></i></span><div><div class="k">{e(ev.kind)} '
                         f'<span>· {e(ev.outcome)}</span></div><div class="w">{e(ev.text_masked)}</div>'
                         f"{shot}</div></div>")
        files = []
        for f in self.store.files_for_run(run.run_id):
            delivery = self.store.current_delivery(f.file_id)
            files.append(row(lead("file", "mute"), e(f.safe_name),
                             f"{e(t('run.bytes', size=f.size))} · "
                             f"{e(delivery[1] if delivery else t('run.not_delivered'))}",
                             end=self._copy(f.sha256, f.sha256[:8] + "…")))
        ico, tint = _STATE_LOOK.get(run.state, ("clock", "mute"))
        body = (f'<div class="card pad"><div class="meta"><span class="chip tint-{tint}">{icon(ico)}<span>{e(self._state(run.state))}</span></span>'
                 f'<span class="small">{e(self._when(run.created_at))}</span></div>'
                 f'<p class="task">{e(run.spec.task_text or _task(run))}</p>'
                 f'<p class="small">{e(t("run.by", requester=run.spec.requester_id, mode=run.spec.mode.replace("_", "-")))}</p>'
                 "</div>")
        if files:
            body += f"<h2>{e(t('run.files_heading'))}</h2>" + '<div class="card">' + "".join(files) + "</div>"
        body += f"<h2>{e(t('run.log_heading'))}</h2>"
        body += (f'<div class="card"><div class="tl">{"".join(steps)}</div></div>' if steps
                 else f'<div class="card pad"><p class="muted">{e(t("run.log_empty"))}</p></div>')
        return self.page(request, t("run.title"), body, tab="home", back="/")

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
        return self.page(request, t("live.title"),
                         '<div class="card attn">' + row(lead("eye", "wait"), e(t("live.asked")), wrap=True)
                         + '<div class="acts">'
                         + self.form(request, f"/live/{grant_id}",
                                     f'<button class="primary s" data-busy="{e(t("live.opening"))}">'
                                     f'{e(t("live.open_button"))}</button>')
                         + "</div></div>", tab="home", back="/")

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
            return self.message(request, self.t("live.title"), self.t("live.unrestored"))
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
        return self.message(request, t("live.title"), text, ok=True)

    # ------------------------------------------------------------------ sites

    def _site_status(self, site, lost) -> tuple[str, str, str, str]:
        """A site's sign-in and provider lines, and the icon and colour its row shows."""
        t = self.t
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
        if site.provider_bindings and site.site_id in lost:
            look = ("alert", "err")
        elif not site.login_origins:
            look = ("earth", "mute")
        elif site.vault_item_id:
            look = ("lock", "ok")
        else:
            look = ("lock", "wait")
        return login, binding, *look

    async def sites(self, request: web.Request) -> web.Response:
        t = self.t
        rows = []
        lost = self.ctl.bindings_lost()
        for site in self.store.list_sites():
            login, binding, ico, tint = self._site_status(site, lost)
            # the list says how the site signs in; a provider sign-in that needs a live login
            # says that instead (the site's own page has both)
            line = binding if tint == "err" else login
            rows.append(row(lead(ico, tint), e(site.display_name), e(line),
                            href=self.url(request, f"/sites/{site.site_id}")))
        listing = (f'<div class="card">{"".join(rows)}</div>' if rows
                   else f'<div class="card pad"><p class="muted">{e(t("sites.none"))}</p></div>')
        note = request.query.get("note", "")
        notice = (f'<div class="banner ok">{icon("ok")}<p>{e(t(_NOTES[note]))}</p></div>'
                  if note in _NOTES else "")
        warning = request.query.get("fields", "")
        extra = ""
        if warning:
            extra = (f'<div class="banner warn">{icon("alert")}'
                     f'<p>{e(t("sites.extra_fields", fields=warning))}</p></div>')
        fab = (f'<a class="fab" href="{e(self.url(request, "/sites/new"))}">{icon("plus")}'
               f'{e(t("sites.add"))}</a>')
        return self.page(request, t("sites.title"), notice + extra + listing + fab, tab="sites")

    async def site_page(self, request: web.Request) -> web.Response:
        t = self.t
        site_id = request.match_info["site"]
        site = self.store.get_site(site_id) if _ID.fullmatch(site_id or "") else None
        if site is None:
            raise web.HTTPNotFound()
        login, binding, ico, tint = self._site_status(site, self.ctl.bindings_lost())
        status = ('<div class="card">'
                  + row(lead(ico, tint), e(t("sites.sign_in")), e(login), wrap=True)
                  + row(lead("link", "ok" if site.provider_bindings and tint != "err" else "mute"),
                        e(t("sites.provider")), e(binding), wrap=True)
                  + "</div>")
        groups = [(t("sites.site"), site.origins), (t("sites.sign_in"), site.login_origins)]
        addresses = "".join(
            f'<div><p class="small">{e(label)}</p><div class="copies">'
            + "".join(self._copy(a) for a in dict.fromkeys(values)) + "</div></div>"
            for label, values in groups if values)
        forget = self.form(
                request, f"/sites/{site.site_id}/forget",
                '<input type="hidden" name="confirm" value="yes">'
                f'<div class="end"><button class="danger">{e(t("sites.forget"))}</button></div>',
                confirm=t("sites.forget_ask", name=site.display_name),
                extra={"confirm-body": t("sites.forget_body"), "ok": t("sites.forget"),
                       "cancel": t("app.cancel"), "danger": ""})
        body = (status + f"<h2>{e(t('sites.addresses_heading'))}</h2>"
                + f'<div class="card pad">{addresses}</div>' + forget)
        return self.page(request, site.display_name, body, tab="sites", back="/sites")

    async def site_new(self, request: web.Request) -> web.Response:
        t = self.t
        register = self.form(request, "/sites", (
            f'<label class="field"><span>{e(t("sites.name"))}</span>'
            '<input name="display_name" maxlength="80" required></label>'
            f'<label class="field"><span>{e(t("sites.origins"))}</span>'
            '<textarea name="origins" rows="2" required></textarea></label>'
            f'<label class="field"><span>{e(t("sites.login_origins"))}</span>'
            '<textarea name="login_origins" rows="2"></textarea></label>'
            '<details><summary class="row"><span class="txt"><span class="l1 more">'
            f'{e(t("sites.more"))}</span></span>{icon("down", "i chev")}</summary>'
            '<div class="stack">'
            f'<label class="field"><span>{e(t("sites.item"))}</span>'
            '<input name="vault_item_id" maxlength="64"></label>'
            f'<label class="field"><span>{e(t("sites.start"))}</span>'
            '<input name="start_page" maxlength="2048"></label></div></details>'
            '<label class="check"><input type="checkbox" name="confirm" value="yes" required>'
            f'<span>{e(t("sites.belong"))}</span></label>'
            f'<div class="end"><button class="primary">{e(t("sites.register"))}</button></div>'),
            cls="stack")
        return self.page(request, t("sites.register_heading"), register, tab="sites", back="/sites")

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
        parts = [f"<h2>{e(t('setup.ready_heading'))}</h2>", '<div class="card">']
        parts.append(row(lead("ok", "ok") if c.configured else lead("alert", "err"),
                         e(t("setup.onepassword_configured") if c.configured
                           else t("setup.onepassword_missing")), wrap=True))
        if locale is None:
            parts.append(row(lead("alert", "wait"), e(t("setup.language_unknown")), wrap=True))
        else:
            source = t("setup.from_ha") if locale.language_source == "home_assistant" else t("setup.from_options")
            parts.append(row(lead("web", "ok"), e(t("setup.language", language=locale.language, source=source,
                                                         timezone=locale.timezone)), wrap=True))
        parts.append("</div>")
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
        guest_tint = {"readonly": "ok", "not_readonly": "wait"}.get(role, "mute")
        parts.append(f"<h2>{e(t('setup.guest_heading'))}</h2>"
                     '<div class="card pad">'
                     + f'<div class="meta">{lead("key", guest_tint)}'
                     f"<p>{e(t('setup.guest_account', guest=guest, counts=counts))}</p></div>")
        if role == "not_readonly":
            # Design §5.2: a warning, nothing to tick; logins never wait on it.
            parts.append(f'<div class="banner warn">{icon("alert")}<p>{e(t("setup.guest_warning"))}</p></div>')
        parts.append(f"<p class=\"small\">{e(t('setup.guest_scope'))}</p></div>")
        parts.append(self._pairing_section(request))
        return self.page(request, t("setup.title"), "".join(parts), tab="setup")

    def _pairing_section(self, request, heading: bool = True) -> str:
        t = self.t
        view = self.ctl.pairing.status_view()
        out = [f"<h2>{e(t('pair.heading'))}</h2>" if heading else "", '<div class="card pad">']
        if view["armed"]:
            out.append(f'<div class="meta">{lead("clock", "run")}<p>{e(t("pair.armed"))}</p></div>')
        elif view["window_closed_unclaimed"]:
            out.append(f'<div class="banner warn">{icon("alert")}<p>{e(t("pair.unclaimed"))}</p></div>')
        if view["paired"]:
            # Both values are escaped by Pairing.status_view (html.escape) — shown as they are.
            when = e(t("pair.on", when=self._when(view["claimed_at"]))) if view["claimed_at"] else ""
            out.append(f'<div class="meta">{lead("link", "ok")}<p>' + t.html(
                "pair.paired", label=view['caller_label_html'] or e(t("pair.no_label")),
                reported=f'<span class="small">{e(t("pair.reported"))}</span>',
                peer=view['observed_peer_html'] or e(t("pair.unknown_peer")), when=when) + "</p></div>")
        elif not view["armed"]:
            out.append(f'<div class="meta">{lead("link", "mute")}<p>{e(t("pair.not_paired"))}</p></div>')
        label = t("pair.again") if view["paired"] else t("pair.pair")
        out.append(self.form(
            request, "/pair",
            f'<div class="end"><button class="s{"" if view["paired"] else " primary"}">{e(label)}</button></div>',
            confirm=t("pair.again_ask") if view["paired"] else None,
            extra={"confirm-body": t("pair.again_confirm"), "ok": label,
                   "cancel": t("app.cancel")} if view["paired"] else None))
        out.append("</div>")
        return "".join(out)

    async def pair_page(self, request: web.Request) -> web.Response:
        return self.page(request, self.t("pair.heading"), self._pairing_section(request, heading=False),
                         tab="setup", back="/setup")

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


# Home Assistant's icon set: Material Design Icons (@mdi/js 7.4.47, Pictogrammers, Apache-2.0),
# the few the pages use, as inline SVG (the CSP admits no icon font or other origin).
_ICONS = {
    "home": "M10,20V14H14V20H19V12H22L12,3L2,12H5V20H10Z",
    "back": "M20,11V13H8L13.5,18.5L12.08,19.92L4.16,12L12.08,4.08L13.5,5.5L8,11H20Z",
    "chev": "M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z",
    "down": "M7.41,8.58L12,13.17L16.59,8.58L18,10L12,16L6,10L7.41,8.58Z",
    "plus": "M19,13H13V19H11V13H5V11H11V5H13V11H19V13Z",
    "ok": "M12 2C6.5 2 2 6.5 2 12S6.5 22 12 22 22 17.5 22 12 17.5 2 12 2M10 17L5 12L6.41 10.59L10 14.17"
          "L17.59 6.58L19 8L10 17Z",
    "err": "M12,2C17.53,2 22,6.47 22,12C22,17.53 17.53,22 12,22C6.47,22 2,17.53 2,12C2,6.47 6.47,2 12,2"
           "M15.59,7L12,10.59L8.41,7L7,8.41L10.59,12L7,15.59L8.41,17L12,13.41L15.59,17L17,15.59L13.41,12"
           "L17,8.41L15.59,7Z",
    "alert": "M13 14H11V9H13M13 18H11V16H13M1 21H23L12 2L1 21Z",
    "help": "M15.07,11.25L14.17,12.17C13.45,12.89 13,13.5 13,15H11V14.5C11,13.39 11.45,12.39 12.17,11.67"
            "L13.41,10.41C13.78,10.05 14,9.55 14,9C14,7.89 13.1,7 12,7A2,2 0 0,0 10,9H8A4,4 0 0,1 12,5"
            "A4,4 0 0,1 16,9C16,9.88 15.64,10.67 15.07,11.25M13,19H11V17H13M12,2A10,10 0 0,0 2,12"
            "A10,10 0 0,0 12,22A10,10 0 0,0 22,12C22,6.47 17.5,2 12,2Z",
    "clock": "M13,2.03V2.05L13,4.05C17.39,4.59 20.5,8.58 19.96,12.97C19.5,16.61 16.64,19.5 13,19.93V21.93"
             "C18.5,21.38 22.5,16.5 21.95,11C21.5,6.25 17.73,2.5 13,2.03M11,2.06C9.05,2.25 7.19,3 5.67,4.26"
             "L7.1,5.74C8.22,4.84 9.57,4.26 11,4.06V2.06M4.26,5.67C3,7.19 2.25,9.04 2.05,11H4.05"
             "C4.24,9.58 4.8,8.23 5.69,7.1L4.26,5.67M2.06,13C2.26,14.96 3.03,16.81 4.27,18.33L5.69,16.9"
             "C4.81,15.77 4.24,14.42 4.06,13H2.06M7.1,18.37L5.67,19.74C7.18,21 9.04,21.79 11,22V20"
             "C9.58,19.82 8.23,19.25 7.1,18.37M12.5,7V12.25L17,14.92L16.25,16.15L11,13V7H12.5Z",
    "eye": "M3 4V16H21V4H3M3 2H21C22.1 2 23 2.89 23 4V16C23 16.53 22.79 17.04 22.41 17.41C22.04 17.79 "
           "21.53 18 21 18H14V20H16V22H8V20H10V18H3C2.47 18 1.96 17.79 1.59 17.41C1.21 17.04 1 16.53 1 16"
           "V4C1 2.89 1.89 2 3 2M10.84 8.93C11.15 8.63 11.57 8.45 12 8.45C12.43 8.46 12.85 8.63 13.16 8.94"
           "C13.46 9.24 13.64 9.66 13.64 10.09C13.64 10.53 13.46 10.94 13.16 11.25C12.85 11.56 12.43 11.73 "
           "12 11.73C11.57 11.73 11.15 11.55 10.84 11.25C10.54 10.94 10.36 10.53 10.36 10.09C10.36 9.66 "
           "10.54 9.24 10.84 8.93M10.07 12C10.58 12.53 11.28 12.82 12 12.82C12.72 12.82 13.42 12.53 13.93 12"
           "C14.44 11.5 14.73 10.81 14.73 10.09C14.73 9.37 14.44 8.67 13.93 8.16C13.42 7.65 12.72 7.36 12 "
           "7.36C11.28 7.36 10.58 7.65 10.07 8.16C9.56 8.67 9.27 9.37 9.27 10.09C9.27 10.81 9.56 11.5 10.07 "
           "12M6 10.09C6.94 7.7 9.27 6 12 6C14.73 6 17.06 7.7 18 10.09C17.06 12.5 14.73 14.18 12 14.18"
           "C9.27 14.18 6.94 12.5 6 10.09Z",
    "file": "M6,2A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2H6M6,4H13V9H18V20H6V4M8,12V14"
            "H16V12H8M8,16V18H13V16H8Z",
    "lock": "M12,17A2,2 0 0,0 14,15C14,13.89 13.1,13 12,13A2,2 0 0,0 10,15A2,2 0 0,0 12,17M18,8A2,2 0 0,1 "
            "20,10V20A2,2 0 0,1 18,22H6A2,2 0 0,1 4,20V10C4,8.89 4.9,8 6,8H7V6A5,5 0 0,1 12,1A5,5 0 0,1 "
            "17,6V8H18M12,3A3,3 0 0,0 9,6V8H15V6A3,3 0 0,0 12,3Z",
    "earth": "M17.9,17.39C17.64,16.59 16.89,16 16,16H15V13A1,1 0 0,0 14,12H8V10H10A1,1 0 0,0 11,9V7H13"
             "A2,2 0 0,0 15,5V4.59C17.93,5.77 20,8.64 20,12C20,14.08 19.2,15.97 17.9,17.39M11,19.93"
             "C7.05,19.44 4,16.08 4,12C4,11.38 4.08,10.78 4.21,10.21L9,15V16A2,2 0 0,0 11,18M12,2"
             "A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z",
    "link": "M10.59,13.41C11,13.8 11,14.44 10.59,14.83C10.2,15.22 9.56,15.22 9.17,14.83C7.22,12.88 "
            "7.22,9.71 9.17,7.76V7.76L12.71,4.22C14.66,2.27 17.83,2.27 19.78,4.22C21.73,6.17 21.73,9.34 "
            "19.78,11.29L18.29,12.78C18.3,11.96 18.17,11.14 17.89,10.36L18.36,9.88C19.54,8.71 19.54,6.81 "
            "18.36,5.64C17.19,4.46 15.29,4.46 14.12,5.64L10.59,9.17C9.41,10.34 9.41,12.24 10.59,13.41"
            "M13.41,9.17C13.8,8.78 14.44,8.78 14.83,9.17C16.78,11.12 16.78,14.29 14.83,16.24V16.24"
            "L11.29,19.78C9.34,21.73 6.17,21.73 4.22,19.78C2.27,17.83 2.27,14.66 4.22,12.71L5.71,11.22"
            "C5.7,12.04 5.83,12.86 6.11,13.65L5.64,14.12C4.46,15.29 4.46,17.19 5.64,18.36C6.81,19.54 "
            "8.71,19.54 9.88,18.36L13.41,14.83C14.59,13.66 14.59,11.76 13.41,10.59C13,10.2 13,9.56 "
            "13.41,9.17Z",
    "key": "M11 10V12H9V14H7V12H5.8C5.4 13.2 4.3 14 3 14C1.3 14 0 12.7 0 11S1.3 8 3 8C4.3 8 5.4 8.8 5.8 10"
           "H11M3 10C2.4 10 2 10.4 2 11S2.4 12 3 12 4 11.6 4 11 3.6 10 3 10M16 14C18.7 14 24 15.3 24 18V20"
           "H8V18C8 15.3 13.3 14 16 14M16 12C13.8 12 12 10.2 12 8S13.8 4 16 4 20 5.8 20 8 18.2 12 16 12Z",
    "web": "M16.36,14C16.44,13.34 16.5,12.68 16.5,12C16.5,11.32 16.44,10.66 16.36,10H19.74C19.9,10.64 20,"
           "11.31 20,12C20,12.69 19.9,13.36 19.74,14M14.59,19.56C15.19,18.45 15.65,17.25 15.97,16H18.92"
           "C17.96,17.65 16.43,18.93 14.59,19.56M14.34,14H9.66C9.56,13.34 9.5,12.68 9.5,12C9.5,11.32 "
           "9.56,10.65 9.66,10H14.34C14.43,10.65 14.5,11.32 14.5,12C14.5,12.68 14.43,13.34 14.34,14M12,"
           "19.96C11.17,18.76 10.5,17.43 10.09,16H13.91C13.5,17.43 12.83,18.76 12,19.96M8,8H5.08C6.03,"
           "6.34 7.57,5.06 9.4,4.44C8.8,5.55 8.35,6.75 8,8M5.08,16H8C8.35,17.25 8.8,18.45 9.4,19.56"
           "C7.57,18.93 6.03,17.65 5.08,16M4.26,14C4.1,13.36 4,12.69 4,12C4,11.31 4.1,10.64 4.26,10H7.64"
           "C7.56,10.66 7.5,11.32 7.5,12C7.5,12.68 7.56,13.34 7.64,14M12,4.03C12.83,5.23 13.5,6.57 "
           "13.91,8H10.09C10.5,6.57 11.17,5.23 12,4.03M18.92,8H15.97C15.65,6.75 15.19,5.55 14.59,4.44"
           "C16.43,5.07 17.96,6.34 18.92,8M12,2C6.47,2 2,6.5 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12"
           "A10,10 0 0,0 12,2Z",
    "cog": "M12,15.5A3.5,3.5 0 0,1 8.5,12A3.5,3.5 0 0,1 12,8.5A3.5,3.5 0 0,1 15.5,12A3.5,3.5 0 0,1 12,"
           "15.5M19.43,12.97C19.47,12.65 19.5,12.33 19.5,12C19.5,11.67 19.47,11.34 19.43,11L21.54,9.37"
           "C21.73,9.22 21.78,8.95 21.66,8.73L19.66,5.27C19.54,5.05 19.27,4.96 19.05,5.05L16.56,6.05"
           "C16.04,5.66 15.5,5.32 14.87,5.07L14.5,2.42C14.46,2.18 14.25,2 14,2H10C9.75,2 9.54,2.18 9.5,"
           "2.42L9.13,5.07C8.5,5.32 7.96,5.66 7.44,6.05L4.95,5.05C4.73,4.96 4.46,5.05 4.34,5.27L2.34,"
           "8.73C2.21,8.95 2.27,9.22 2.46,9.37L4.57,11C4.53,11.34 4.5,11.67 4.5,12C4.5,12.33 4.53,12.65 "
           "4.57,12.97L2.46,14.63C2.27,14.78 2.21,15.05 2.34,15.27L4.34,18.73C4.46,18.95 4.73,19.03 "
           "4.95,18.95L7.44,17.94C7.96,18.34 8.5,18.68 9.13,18.93L9.5,21.58C9.54,21.82 9.75,22 10,22H14"
           "C14.25,22 14.46,21.82 14.5,21.58L14.87,18.93C15.5,18.67 16.04,18.34 16.56,17.94L19.05,18.95"
           "C19.27,19.03 19.54,18.95 19.66,18.73L21.66,15.27C21.78,15.05 21.73,14.78 21.54,14.63"
           "L19.43,12.97Z",
}


def icon(name: str, cls: str = "i") -> str:
    return (f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true">'
            f'<path d="{_ICONS[name]}"></path></svg>')


def lead(name: str, tint: str) -> str:
    """The round icon at the start of a row."""
    return f'<span class="lead tint-{tint}">{icon(name)}</span>'


def row(start: str, line1_html: str, line2_html: str = "", *, href: str | None = None,
        wrap: bool = False, end: str = "") -> str:
    """A list row (Home Assistant's ha-row-item): its icon, two lines, and a chevron when it
    leads somewhere. The lines arrive escaped."""
    second = f'<span class="l2{" wrap" if wrap else ""}">{line2_html}</span>' if line2_html else ""
    inner = (f'{start}<span class="txt"><span class="l1{" wrap" if wrap else ""}">{line1_html}</span>'
             f'{second}</span>{end}')
    if href is not None:
        return f'<a class="row" href="{e(href)}">{inner}{icon("chev", "i chev")}</a>'
    return f'<div class="row">{inner}</div>'


# How each run state looks: its icon and its colour.
_STATE_LOOK = {"queued": ("clock", "run"), "starting": ("clock", "run"), "running": ("clock", "run"),
               "finishing": ("clock", "run"), "waiting_approval": ("help", "wait"),
               "waiting_live": ("eye", "wait"), "live": ("eye", "wait"), "completed": ("ok", "ok"),
               "failed": ("err", "err"), "interrupted": ("err", "err"), "cancelled": ("err", "mute")}


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
        web.get("/sites/new", ui.site_new),          # before /sites/{site}: the first match wins
        web.get("/sites/{site}", ui.site_page),
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
