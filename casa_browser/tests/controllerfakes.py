"""Doubles for the run controller's tests (Task 12): the browser manager, the proxy client,
the broker client, the login driver and the downloader, each recording its calls in one
shared log so a test can assert their order. The store, the handle registry and the observer
are the real ones (the observer reads a scripted extraction).
Everything here is synthetic.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from casa_browser.broker import BrokerError
from casa_browser.browser import BrowserSession
from casa_browser.downloads import DownloadRefused, PlainAnswer
from casa_browser.observe import Extraction, Observer
from casa_browser.protocol import (
    Admission,
    EffectiveLocale,
    FileRecord,
    RunSpec,
    Site,
    SiteGrant,
    spec_digest,
)

LOCALE = EffectiveLocale("en-GB", "Europe/Amsterdam", "option")
PNG = b"\x89PNG\r\n\x1a\nfake"


class Log(list):
    def kinds(self):
        return [entry[0] for entry in self]

    def index_of(self, *entry, after=-1):
        for n, e in enumerate(self):
            if n > after and e[:len(entry)] == entry:
                return n
        raise AssertionError(f"{entry!r} not in log: {list(self)!r}")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += timedelta(seconds=seconds)


# --------------------------------------------------------------------------- browser


class FakeElement:
    """A page element: an extraction item plus the element-handle methods the controller,
    the pacer and the observer's screenshot path use. ``facts`` are the gate reading's extra
    control facts (state, form, destinations); ``covered`` fails the hit test."""

    def __init__(self, page, role="button", name="Go", href=None, field=False, input_type="",
                 value="", facts=None, options=None, covered=False):
        self.page, self.role, self.name, self.href = page, role, name, href
        self.field, self.input_type = field, input_type
        self.value = value
        self.facts = dict(facts or {})
        self.options = list(options or [])      # a select's options: [(label, value), …]
        self.selected = None
        self.covered = covered
        self.hidden = False
        self.mark = None

    async def evaluate(self, js, *args):
        if "isConnected" in js:
            return self.mark == args[0]
        if "setAttribute" in js:
            self.mark = args[0]
            return None
        if "removeAttribute" in js:
            self.mark = None
            return None
        if "e.type" in js:
            return self.input_type
        return None

    def item(self):
        return {"role": self.role, "name": self.name, "href": self.href, "field": self.field}

    async def is_visible(self):
        return not self.hidden

    async def scroll_into_view_if_needed(self, timeout=None):
        return None


class FakeFrame:
    def __init__(self, page):
        self.page = page

    @property
    def url(self):
        return self.page.url

    def is_detached(self):
        return False


class FakePage:
    def __init__(self, session_ref, log, contents=None):
        self._session = session_ref
        self.log = log
        self.contents = contents if contents is not None else {}
        self.url = "about:blank"
        self.title = ""
        self.text = ""
        self.elements: list[FakeElement] = []
        self.main_frame = FakeFrame(self)
        self.closed = False
        self.goto_hook = None       # async callable(url) run inside goto (a test can stall it)
        self.download_for = {}      # element -> download object a click starts
        self.handlers = {}
        self.history = []
        # The action gate's reading (FakeOps): completeness, a payment-card field, a closed
        # shadow root, the focused element, and hooks a test uses to change the page.
        self.complete = True
        self.has_cc = False
        self.closed_shadow = False      # a closed shadow root holds a field
        self.focus = None
        self.read_hook = None       # callable(page, n) run before the n-th gate reading
        self.reads = 0

    @property
    def frames(self):
        return [self.main_frame]

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    def is_closed(self):
        return self.closed

    def _navigated(self, url):
        self.history.append(self.url)
        self.url = url
        self.elements = []
        self._session().view_generation = next(self._session().views)

    async def goto(self, url, **kw):
        self.log.append(("goto", url))
        if self.goto_hook is not None:
            await self.goto_hook(url)
        content = self.contents.get(url)
        if content is not None:
            self.show(content.get("final", url), content.get("text", ""),
                      content.get("elements", ()), content.get("title", "T"))
        else:
            self._navigated(url)

    async def wait_for_load_state(self, *a, **kw):
        return None

    async def go_back(self, **kw):
        self.log.append(("back",))
        if self.history:
            url = self.history.pop()
            self.url = url
            self._session().view_generation = next(self._session().views)

    async def evaluate(self, js, *args):
        return None

    async def screenshot(self, **kw):
        return PNG

    def locator(self, selector):
        return ("locator", selector)

    def show(self, url, text="", elements=(), title="T"):
        """The test puts a page in the browser (as if it loaded)."""
        self._navigated(url)
        self.text, self.title = text, title
        self.elements = [FakeElement(self, **e) for e in elements]
        return self.elements


class FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.handlers = {}
        self.jar = []                # the cookies Chrome holds
        self.cookies_error = None    # raised by ``cookies``
        self.add_error = None        # raised by ``add_cookies``

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    async def cookies(self, urls=None):
        if self.cookies_error is not None:
            raise self.cookies_error
        return [dict(c) for c in self.jar]

    async def add_cookies(self, cookies):
        if self.add_error is not None:
            raise self.add_error
        self.pages[0].log.append(("add_cookies", sorted(c["name"] for c in cookies)))
        self.jar.extend(cookies)

    async def close(self):
        for p in self.pages:
            p.closed = True


class FakeBrowserManager:
    def __init__(self, log):
        self.log = log
        self.session: BrowserSession | None = None
        self._generation = 0
        self.views = itertools.count(100)
        self._lock = asyncio.Lock()
        self.open_hook = None
        self.forgotten = []
        self.contents = {}           # url -> {text, elements, title, final}: what a goto loads
        self.force_fail = False      # the launcher cannot confirm Chrome stopped

    @property
    def next_generation(self):
        return self._generation + 1

    async def open(self, site_id, mode, locale):
        kind = "public" if mode == "open_web" or site_id is None else "account"
        async with self._lock:
            await self._close_locked()
            self._generation += 1
            holder = {}
            page = FakePage(lambda: holder["s"], self.log, self.contents)
            session = BrowserSession(kind, None if kind == "public" else site_id,
                                     FakeContext(page), self._generation, next(self.views))
            session.views = self.views
            holder["s"] = session
            self.log.append(("open", kind, session.site_id, self._generation))
            if self.open_hook is not None:
                await self.open_hook(session)
            self.session = session
            return session

    async def _close_locked(self):
        session, self.session = self.session, None
        if session is not None:
            await session.context.close()
            self.log.append(("close", session.kind, session.site_id))

    async def close(self, *, session=None, force=False):
        async with self._lock:
            if session is not None and self.session is not session:
                raise RuntimeError("session")
            if force:
                self.log.append(("exit_proof", not self.force_fail))
                if self.force_fail:
                    raise RuntimeError("close unconfirmed")
            await self._close_locked()

    async def forget(self, site_id):
        async with self._lock:
            await self._close_locked()
            self.forgotten.append(site_id)
            self.log.append(("forget_profile", site_id))

    async def shutdown(self):
        await self.close()

    @property
    def page(self):
        return self.session.page if self.session else None


class FakePacer:
    """The pacer's contract without a browser: a guarded press awaits ``guard(point)`` after
    the (logged) move, then ``on_dispatch()``, then the press; ``type`` checks ``focus()``
    before every character; Enter only through ``press_enter``. ``gate`` holds the press
    after mouse-down (a cut lands there); ``fail_after_down`` raises a timeout there;
    ``move_hook(el)`` runs during the pointer movement (a page changing under it)."""

    def __init__(self, log, page):
        self.log, self.page = log, page
        self.gate: asyncio.Event | None = None
        self.fail_after_down = None
        self.move_hook = None
        self.key_hook = None        # callable(n) before the n-th character
        self.enter_gate = None      # holds the Enter after its keydown (a cut lands there)
        self.refuse = None          # the target is refused with this code, before any move
        self.scroll_to_position = None   # what ``scroll_to`` answers: None, it was in view

    async def _maybe_wait(self):
        if self.gate is not None:
            await self.gate.wait()

    async def shown(self, el):
        from casa_browser.pacing import ActionError
        if el.hidden:
            raise ActionError("not_visible")

    async def click(self, el, *, guard=None, on_dispatch=None):
        from casa_browser.pacing import ActionError, GuardRefused
        if self.refuse is not None:
            raise ActionError(self.refuse)
        self.log.append(("move", el.name))
        if self.move_hook is not None:
            self.move_hook(el)
        if guard is not None:
            verdict = await guard((10.0, 10.0))
            self.log.append(("guard", el.name, verdict))
            if verdict != "ok":
                raise GuardRefused(verdict)
        if on_dispatch is not None:
            on_dispatch()
        self.log.append(("click", el.name))
        self.page.focus = el if el.field else self.page.focus
        await self._maybe_wait()
        if self.fail_after_down is not None:
            raise ActionError(self.fail_after_down)
        download = self.page.download_for.get(el)
        if download is not None:
            for fn in self.page.handlers.get("download", []):
                fn(download)

    async def type(self, el, text, *, guard=None, on_dispatch=None, focus=None,
                   line_breaks=False):
        from casa_browser.pacing import ActionError, GuardRefused
        if "\n" in text and not line_breaks:
            raise ActionError("unsupported")
        await self.click(el, guard=guard, on_dispatch=on_dispatch)
        self.page.focus = el
        if not el.facts.get("contenteditable"):      # the select: the text replaces the value
            el.value = ""
        for n, ch in enumerate(text):
            if self.key_hook is not None:
                self.key_hook(n)
            if focus is not None and not await focus():
                self.log.append(("focus_lost", el.name, n))
                raise GuardRefused("changed")
            el.value += ch
        self.log.append(("type", el.name, len(text), line_breaks))

    async def insert(self, el, text, *, guard=None, on_dispatch=None, focus=None):
        from casa_browser.pacing import GuardRefused
        await self.click(el, guard=guard, on_dispatch=on_dispatch)
        self.page.focus = el
        if focus is not None and not await focus():
            raise GuardRefused("changed")
        el.value = text
        self.log.append(("insert", el.name, len(text)))

    async def press_enter(self, guard, on_dispatch=None):
        from casa_browser.pacing import GuardRefused
        verdict = await guard()
        self.log.append(("enter_guard", verdict))
        if verdict != "ok":
            raise GuardRefused(verdict)
        if on_dispatch is not None:
            on_dispatch()
        self.log.append(("enter",))
        if self.enter_gate is not None:
            await self.enter_gate.wait()

    async def scroll_to(self, el):
        await self.shown(el)
        await el.scroll_into_view_if_needed()
        self.log.append(("scroll_to", el.name))
        return self.scroll_to_position

    async def scroll(self, direction, amount):
        self.log.append(("scroll", direction, amount))
        return {"top": 0, "end": 0, "moved": False}


TEXT_TYPES = ("", "text", "search", "email", "tel", "url", "number")
VALUE_TYPES = ("time", "date", "datetime-local", "month", "week", "range", "color")


class FakeOps:
    """``gate.PageOps`` over FakePage: the gate reading is built from the page's text lines
    (one block each) and its elements (one control each, ``facts`` merged in)."""

    def __init__(self, log):
        self.log = log

    @staticmethod
    def _control(el, idx):
        tag = "a" if el.href else ("input" if el.field else ("select" if el.options else "button"))
        state = {}
        if el.field:
            state["value"] = el.value
        if el.options:
            state["selected"] = el.selected
            state["options"] = [label for label, _v in el.options]
        c = {"idx": idx, "role": el.role, "name": el.name, "tag": tag,
             "input_type": el.input_type or None, "href": el.href, "is_submitter": False,
             "submit_action": None, "submit_method": None, "submit_target": None, "form": None,
             "container": None, "record": None, "block": -1, "heading": None, "state": state,
             "secret": el.input_type == "password", "download_attr": False,
             "editable_text": el.field and el.input_type in TEXT_TYPES,
             "search_field": el.role == "searchbox" or el.input_type == "search",
             "password": el.input_type == "password", "form_self": -1}
        facts = dict(el.facts)
        if "state" in facts:
            c["state"] = {**state, **facts.pop("state")}
        c.update(facts)
        return c

    async def read(self, frame, target=None, *, with_handles=False):
        from casa_browser.gate import Reading
        page = frame.page
        page.reads += 1
        if page.read_hook is not None:
            page.read_hook(page, page.reads)
        blocks = [{"text": line, "containers": [], "record": None}
                  for line in page.text.splitlines() if line.strip()]
        controls = [self._control(e, i) for i, e in enumerate(page.elements)]
        tfacts, tidx = None, -1
        if target is not None and target in page.elements:
            tidx = page.elements.index(target)
            tfacts = dict(controls[tidx], connected=True, visible=True, enabled=True)
        data = {"href": page.url, "title": page.title, "complete": page.complete,
                "blocks": blocks, "controls": controls, "target": tfacts, "target_idx": tidx,
                "has_cc": page.has_cc}
        self.log.append(("read", page.reads))
        return Reading(data, list(page.elements) if with_handles else None)

    async def read_page_and_bound(self, page, frames, *, policy=None):
        """One reading of the page (one frame here) as both the whole and the bound reading."""
        import copy
        self.log.append(("read_page_and_bound",))
        whole = await self.read_page(page, None, policy=policy)
        return whole, type(whole)(copy.deepcopy(whole.data), None)

    async def read_page(self, page, target=None, *, policy=None, with_handles=False):
        reading = await self.read(page.main_frame, target, with_handles=with_handles)
        for c in reading.data["controls"]:
            c["frame_href"] = page.url
        if page.closed_shadow:
            reading.data["complete"] = False
        return reading

    async def link_href(self, element):
        return element.href

    async def closed_shadow(self, page):
        return page.closed_shadow

    async def frame_of(self, page, element):
        return page.main_frame

    async def hit(self, element, point):
        return not element.covered

    async def focused(self, element):
        return element.page.focus is element

    async def text_facts(self, element, text=None):
        ac = str(element.facts.get("autocomplete", ""))
        kind = element.input_type if element.field and element.input_type in VALUE_TYPES else None
        return {"editable_text": element.field and element.input_type in TEXT_TYPES,
                "value_kind": kind,
                # the browser's value format: a fake field takes what ``formats`` allows
                "accepts": (text is not None and element.facts.get("formats", lambda t: True)(text))
                if kind else None,
                "readonly": bool(element.facts.get("readonly")),
                "textarea": element.facts.get("tag") == "textarea",
                "password": element.input_type == "password",
                "secret": element.input_type == "password" or "one-time-code" in ac
                or ac.startswith("cc-"),
                "search_field": element.role == "searchbox" or element.input_type == "search"}

    async def value(self, element):
        return element.value

    async def set_value(self, element, input_type, text):
        if (not element.field or element.input_type != input_type
                or not element.facts.get("formats", lambda t: True)(text)):
            self.log.append(("set_refused", element.name))
            return "field" if element.input_type != input_type else "not_taken"
        element.value = element.facts.get("normalise", lambda t: t)(text)
        self.log.append(("set", element.name, element.value))
        return "ok" if element.value == text else "changed_by_page"

    async def option_facts(self, select, option):
        # an option given an ``owner`` belongs to that dropdown only (another row's list)
        pair = (option.name, option.facts.get("value", option.name))
        mine = option.facts.get("owner") in (None, select.name)
        return {"belongs": mine and pair in select.options, "label": pair[0], "value": pair[1]}

    async def own_option(self, select, option):
        same = [p for p in select.options if p[0] == option.name]
        if option.role != "option" or len(same) != 1:
            return None
        self.log.append(("own_option", select.name, same[0][0]))
        return FakeElement(select.page, role="option", name=same[0][0],
                           facts={"value": same[0][1], "owner": select.name})

    async def select_one(self, select, option, label, value):
        if (label, value) not in select.options or option.facts.get("disabled"):
            self.log.append(("select_refused", label))
            return "option_changed"
        select.selected = label
        self.log.append(("select", select.name, label))
        return "ok"


def make_observer(registry, artifacts, store):
    async def extractor(frame, find=None):
        page = frame.page
        els = page.elements
        return Extraction(href=page.url, title=page.title, text=page.text,
                          elements=[dict(e.item(), found=bool(find) and find.casefold() in e.name.casefold())
                                    for e in els], element_handles=list(els),
                          field_handles=[e for e in els if e.field], frame_handles=[],
                          complete=not getattr(page, "unfaithful", False))

    async def no_shadow(page):
        return False

    return Observer(registry, artifacts, store, extractor=extractor, closed_shadow=no_shadow,
                    closed_any=no_shadow)


# --------------------------------------------------------------------------- proxy


class FakeProxy:
    def __init__(self, log):
        self.log = log
        self.policy = None
        self.nonce = "n1"
        self.counter = 1
        self.window_open = True      # a started proxy opens the credential window
        self.queue = []              # events to deliver
        self.dropped = 0
        self.fail = set()            # ops that raise ControlError
        self.install_hook = None
        self.binding_key_id = "k1"   # names the proxy's binding key (an HA restore makes another)

    def _check(self, op):
        if op in self.fail:
            from casa_browser.proxy_client import ControlError
            raise ControlError("proxy_unavailable")

    @property
    def token(self):
        return (self.nonce, self.counter)

    def _open(self):
        self.counter += 1
        self.window_open = True
        return self.token

    async def install(self, policy, *, start=None):
        self._check("install")
        if start is not None and start != self.nonce:
            from casa_browser.proxy_client import ControlError
            raise ControlError("restarted")
        if self.install_hook is not None:
            await self.install_hook(policy)
        self.policy = policy
        self.log.append(("install", policy.revision, policy.active_session.kind,
                         policy.active_session.site_id,
                         None if policy.manual_lease is None else policy.manual_lease.lease_id))
        return policy.revision

    async def revoke(self, run_id):
        self._check("revoke")
        if self.policy is None or self.policy.run_id == run_id:
            self.policy = None
        self.log.append(("revoke", run_id))

    async def begin_manual(self, lease, *, purpose):
        self._check("begin_manual")
        self.log.append(("begin_manual", lease.lease_id, purpose))
        from dataclasses import replace
        self.policy = replace(self.policy, manual_lease=lease)
        return self._open() if purpose in ("login", "assist") else None

    async def end_manual(self, lease_id):
        self.log.append(("end_manual", lease_id))
        if self.policy is not None and self.policy.manual_lease is not None:
            from dataclasses import replace
            self.policy = replace(self.policy, manual_lease=None)

    async def open_window(self):
        self.log.append(("open_window",))
        return self._open()

    async def close_window(self, token):
        self.log.append(("close_window", tuple(token)))
        if tuple(token) == self.token:
            self.window_open = False

    async def status(self):
        self._check("status")
        p = self.policy
        return {"ok": True, "run_id": p.run_id if p else None,
                "revision": p.revision if p else None, "window_open": self.window_open,
                "token": list(self.token), "site_id": None, "site_origins": [],
                "binding_key_id": self.binding_key_id}

    async def events(self, after_seq):
        self._check("events")
        return [e for e in self.queue if e.seq > after_seq], self.dropped

    def restart(self):
        """The proxy process restarted: a new nonce, no policy, the window open."""
        self.nonce = self.nonce + "'"
        self.counter = 1
        self.policy = None
        self.window_open = True
        self.queue = []


# --------------------------------------------------------------------------- broker / driver


class FakeBroker:
    def __init__(self, log, store=None):
        self.log = log
        self.store = store
        self.login_reply = {"status": "logged_in", "reason": "completed"}
        self.unlock_reply = {"state": "unlocked", "reason": "completed"}
        self.capture_reply = {"capture_id": "cap1"}
        # slice 5
        self.items_reply = {"items": [], "truncated": False}
        self.attempt_reply = {"attempt": None, "incomplete": False}
        self.save_attempt_reply = {"saved": True, "item_id": "item-auto"}
        self.registered = []          # (site_id, item_id, operator_ref) of every register_item
        self.captured = []
        self.register_origins = []    # slice 5n: the login_origins of every register_item            # slice 5n: (site_id, login_origins, ref, title) per register_capture
        self.login_hook = None
        # The lazy guest unlock: the page shows a passkey hint, so a login asked with
        # ``guest_ask`` replies needs_guest (as the real broker does, and only then).
        self.passkey_hint = False
        self.guest_asks = []          # the guest_ask of every login, in order
        self.unlock_hook = None
        self.end_run_fail = 0
        # slice 6: the sign-in code's fill
        self.fill_reply = {"status": "logged_in", "reason": "completed"}
        self.fill_hook = None
        self.codes = []               # test-private: the codes the broker received
        self.page_generation = None   # callable: the page a needs_code reply names
        self.filled_generations = []
        # Signing in off the start page: the look's read (None: the page has no sign-in fields)
        self.sign_in_read = None
        self.sign_in_hook = None      # async callable(args) run inside read_sign_in
        self.logins = []              # the arguments of every login, in order

    async def call(self, op, **args):
        self.log.append(("broker", op, args.get("run_id"), args.get("site_id")))
        if op == "end_run" and self.end_run_fail:
            self.end_run_fail -= 1
            return {"error": "unavailable"}
        if op == "read_sign_in":
            if self.sign_in_hook is not None:
                await self.sign_in_hook(args)
            read = self.sign_in_read(args) if callable(self.sign_in_read) else self.sign_in_read
            return json.loads(json.dumps(read)) if read is not None else {"found": False}
        if op == "login":
            self.logins.append(dict(args))
            self.guest_asks.append(args.get("guest_ask"))
            if self.login_hook is not None:
                await self.login_hook(args)
            if self.passkey_hint and args.get("guest_ask") is True:
                return {"status": "needs_guest", "reason": "passkey"}
            return self._named(dict(self.login_reply))
        if op == "fill_code":
            self.codes.append(args["code"])
            self.filled_generations.append(args["page_generation"])
            if self.fill_hook is not None:
                await self.fill_hook(args)
            return self._named(dict(self.fill_reply))
        if op == "unlock_guest":
            if self.unlock_hook is not None:
                await self.unlock_hook(args)
            return dict(self.unlock_reply)
        if op == "begin_capture":
            return dict(self.capture_reply)
        if op == "register_item":
            self.registered.append((args["site_id"], args["item_id"], args["operator_ref"]))
            self.register_origins.append(tuple(args["login_origins"]))
            return {"site_id": args["site_id"], "roles_available": ["username", "password"],
                    "extra_field_names": [], "warning": ""}
        if op in ("list_login_items", "list_vault_logins"):
            return json.loads(json.dumps(self.items_reply))
        if op == "register_capture":
            self.captured.append((args["site_id"], tuple(args["login_origins"]),
                                  args["operator_ref"], args["title"]))
            return {"site_id": args["site_id"]}
        if op == "take_attempt":
            return json.loads(json.dumps(self.attempt_reply))
        if op == "save_attempt":
            self.log.append(("save_attempt", args.get("title")))
            return dict(self.save_attempt_reply)
        if op == "status":
            return {"configured": True, "sdk": "ok", "guest_role": "readonly"}
        if op == "pending_notices":
            return {"notices": []}
        return {}

    def _named(self, reply):
        if reply.get("status") == "needs_code" and "page_generation" not in reply \
                and self.page_generation is not None:
            reply["page_generation"] = self.page_generation()
        return reply

    async def import_notices(self, store=None):
        self.log.append(("broker", "import_notices", None, None))

    async def start(self):
        await self.import_notices()

    def __getattr__(self, op):
        if op.startswith("_"):
            raise AttributeError(op)

        async def call(*args, **kw):
            names = {"begin_run": ("run_id", "account_site_ids"), "end_run": ("run_id",),
                     "login": ("run_id", "site_id", "driver_session"),
                     "unlock_guest": ("run_id", "site_id", "driver_session"),
                     "begin_capture": ("run_id", "site_id", "driver_session", "purpose"),
                     "end_capture": ("run_id", "site_id", "capture_id"),
                     "forget_site": ("site_id", "operator_ref"),
                     "register_item": ("site_id", "item_id", "login_origins", "operator_ref"),
                     "register_capture": ("site_id", "login_origins", "operator_ref", "title"),
                     "status": (), "pending_notices": ("after",),
                     "list_login_items": ("run_id", "site_id"),
                     "list_vault_logins": ("run_id",),
                     "take_attempt": ("run_id", "site_id", "capture_id"),
                     "save_attempt": ("run_id", "site_id", "capture_id", "title"),
                     "drop_attempt": ("run_id", "site_id", "capture_id"),
                     "fill_code": ("run_id", "site_id", "driver_session", "page_generation",
                                   "code"),
                     "read_sign_in": ("run_id", "site_id", "driver_session")}[op]
            return await self.call(op, **dict(zip(names, args)), **kw)
        return call


class FakeDriver:
    """``login.DriverServer``'s controller-facing calls. ``close_mode``: 'drained' (the normal
    path), 'forced' (K1: the driver closed the browser), 'unconfirmed' (the browser could not
    be confirmed closed: the session stays and ``close_session`` raises)."""

    def __init__(self, log, manager):
        self.log, self.manager = log, manager
        self.sessions = {}
        self.close_mode = "drained"
        self.close_modes = {}        # kind -> mode, overriding close_mode
        self.ids = itertools.count(1)
        self.reader = None
        self.readings = {}           # capture id -> {"after": [...], "at_done": reading}

    def take_readings(self, capture_id):
        return self.readings.pop(capture_id, {})

    def sign_in_frames(self, run_id, site_id):
        """The look's bound frames: the page's main frame here."""
        session = self.manager.session
        return (session.page.main_frame,) if session is not None else ()

    async def open_session(self, kind, run_id, site_id, browser_session, proxy_client,
                           login_origins, reader=None):
        if self.sessions or self.manager.session is not browser_session:
            raise BrokerError("refused")
        sid = f"d{next(self.ids)}"
        if reader is not None:
            self.reader = reader         # the controller's page reader (slice 5)
        self.sessions[sid] = (kind, browser_session)
        if kind in ("login", "guest"):
            browser_session.login_active = True
        self.log.append(("open_session", kind, sid))
        return sid

    async def close_session(self, sid):
        if sid not in self.sessions:
            return
        kind, session = self.sessions[sid]
        mode = self.close_modes.get(kind, self.close_mode)
        self.log.append(("close_session", kind, sid, mode))
        if mode == "unconfirmed":
            raise BrokerError("unavailable")
        if mode == "forced":
            await self.manager.close(session=session, force=True)
        session.login_active = False
        del self.sessions[sid]

    def open_sessions(self):
        return tuple(self.sessions)

    async def start(self):
        pass

    async def close(self):
        for sid in list(self.sessions):
            await self.close_session(sid)


# --------------------------------------------------------------------------- downloads


class FakeDownloader:
    def __init__(self, log, store, root: Path):
        self.log, self.store, self.root = log, store, root
        self.refuse = None          # DownloadRefused to raise
        self.gate: asyncio.Event | None = None
        self.ids = itertools.count(1)
        self.cleaned = 0
        self.plain = None           # PlainAnswer or (body, mime) a plain fetch comes to
        self.plain_url = None       # the address that served it, when not the one asked

    def _record(self, run, handle, data=b"%PDF-1.7 synthetic"):
        file_id = f"f{next(self.ids)}"
        path = self.root / file_id
        path.write_bytes(data)
        rec = FileRecord(file_id, run.run_id, run.spec.requester_id, "invoice.pdf",
                         "application/pdf", len(data), hashlib.sha256(data).hexdigest(),
                         str(path), handle)
        self.store.put_file(rec)
        return rec

    async def fetch(self, run, handle, session, policy, *, on_send=None, quarantine=False):
        # ``refuse`` stands for the policy's refusal of a hop, which comes before any send;
        # ``gate`` holds the transfer after the request went out.
        if self.refuse is not None:
            self.log.append(("fetch_refused", handle))
            raise self.refuse
        if on_send is not None:
            on_send()
        self.log.append(("fetch", handle))
        if self.gate is not None:
            await self.gate.wait()
        return self._record(run, handle)

    async def user_agent(self, session):
        return "Mozilla/5.0 synthetic"

    async def fetch_plain(self, run, url, policy, agent):
        self.log.append(("fetch_plain", url, agent))
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.plain, tuple):
            data, mime = self.plain
            file_id = f"f{next(self.ids)}"
            path = self.root / file_id
            path.write_bytes(data)
            record = FileRecord(file_id, run.run_id, run.spec.requester_id, "page", mime, len(data),
                                hashlib.sha256(data).hexdigest(), str(path), None)
            self.store.put_file(record)
            return PlainAnswer(record=record, url=self.plain_url or url)
        return self.plain

    async def capture_browser_download(self, run, download, session, policy):
        self.log.append(("capture_download",))
        if self.refuse is not None:
            raise self.refuse
        return self._record(run, None)

    def make_archive(self, run, file_ids):
        self.log.append(("archive", tuple(file_ids)))
        return self._record(run, None, b"PK\x05\x06" + b"\0" * 18)

    def ack_delivery(self, file_id, requester_id, delivery_id, outcome):
        rec = self.store.get_file(file_id)
        if rec is None or rec.requester_id != requester_id:
            from casa_browser.protocol import ProtocolError
            raise ProtocolError("not_found")
        outcome, recorded = self.store.record_delivery(file_id, requester_id, delivery_id,
                                                       outcome)
        return {"file_id": file_id, "delivery_id": delivery_id, "outcome": outcome,
                "recorded": recorded}

    def cleanup_partials(self):
        self.cleaned += 1
        self.log.append(("cleanup_partials",))
        return 0


# --------------------------------------------------------------------------- specs


ACCOUNT = Site("sa", "Site A", ("https://a.example",), ("https://login.a.example",), "item-a",
               (), "https://a.example/start", 1)
PUBLIC = Site("sp", "Public B", ("https://b.example",), (), None, (), "https://b.example/", 1)
OTHER = Site("sc", "Site C", ("https://c.example",), (), None, (), None, 1)
SEARCH = Site("sg", "Search", ("https://www.google.com",), (), None, (), None, 1)


def spec(request_id="rq1", requester="finance", mode="account", sites=(("sa", "account"),),
         text="get the invoice", kind="invoice", start_url=None):
    grants = tuple(SiteGrant(s, a) for s, a in sites)
    members = {"request_id": request_id, "requester_id": requester, "mode": mode,
               "task_text": text, "task_kind": kind,
               "sites": [g.to_wire() for g in grants], "result_expectation": ""}
    if start_url is not None:
        members["start_url"] = start_url
    return RunSpec(request_id, requester, mode, text, kind, grants, "",
                   Admission("operator_telegram", "m1", spec_digest(members)), start_url=start_url)


# --------------------------------------------------------------------------- the live view


class FakeScreen:
    """``liveview.ChromeScreen`` without Chrome or X: every call lands in the log; the probe
    returns ``fields``; ``inject_gate`` (an Event) holds an injection until set."""

    def __init__(self, log):
        self.log = log
        self.fields = [{"id": 1, "x": 40, "y": 200, "w": 300, "h": 30, "type": "email", "im": "",
                        "ac": "username", "ml": 0, "focused": True, "len": 0, "lb": "Email",
                        "val": "", "fi": 0, "doc": 7}]
        self.password = False
        self.restore_ok = True
        self.bounds = (0, 0, 1920, 1080)
        self.inject_gate = None
        self.selections = []
        self.focus_ok = True     # what the guard answers: Chrome's focus is the named field
        self.inject_error = None  # raised by inject (a typing that failed part-way)

    def page(self, bs):
        return bs.page

    async def fit(self, bs, w, h):
        self.bounds = (0, 0, w, h)
        self.log.append(("fit", w, h))

    async def restore(self, bs):
        self.log.append(("restore", self.restore_ok))
        if self.restore_ok:
            self.bounds = (0, 0, 1920, 1080)
        return self.restore_ok

    async def probe(self, bs):
        page = bs.page
        if page is None:
            return None
        self.log.append(("probe",))
        return {"win": [0, 0, self.bounds[2], self.bounds[3]], "origin": [0, 80],
                "view": [self.bounds[2], self.bounds[3] - 80], "host": "a.example",
                "title": getattr(page, "title", ""), "url": page.url,
                "fields": [dict(f) for f in self.fields], "password": self.password}

    async def focused_is(self, bs, to):
        self.log.append(("focused_is", to["id"], self.focus_ok))
        return self.focus_ok

    async def inject(self, bs, op):
        self.log.append(("inject", op["t"]))
        if self.inject_error is not None:
            raise self.inject_error
        if self.inject_gate is not None:
            await self.inject_gate.wait()
        self.log.append(("injected", op["t"], op.get("s")))

    async def selection(self):
        return self.selections.pop(0) if self.selections else ""

    async def clear_selection(self):
        self.log.append(("clear_selection",))


# --------------------------------------------------------------------------- the rig


class Rig:
    """A ``RunController`` over the doubles above and the real store, registry and observer;
    the pump is driven by hand (``pump()``)."""

    def __init__(self, tmp_path, *, sites=(ACCOUNT, PUBLIC, OTHER, SEARCH), configured=True,
                 locale=LOCALE):
        from casa_browser import controller as ctl_mod
        from casa_browser.controller import Components, RunController
        from casa_browser.handles import HandleRegistry
        from casa_browser.store import Store

        self.tmp = tmp_path
        self.log = Log()
        self.clock = Clock()
        self.store = Store(tmp_path / "state.db")
        for site in sites:
            self.store.put_site(site)
        self.registry = HandleRegistry()
        self.browser = FakeBrowserManager(self.log)
        self.proxy = FakeProxy(self.log)
        self.broker = FakeBroker(self.log, self.store)
        self.driver = FakeDriver(self.log, self.browser)
        self.broker.page_generation = lambda: (self.browser.session.view_generation
                                               if self.browser.session is not None else 0)
        (tmp_path / "files").mkdir(exist_ok=True)
        self.downloader = FakeDownloader(self.log, self.store, tmp_path / "files")
        self.observer = make_observer(self.registry, tmp_path / "artifacts", self.store)
        self.pacers = []
        self.gate = None
        self.pacer_hooks = {}       # attributes every new FakePacer gets (move_hook, …)
        from casa_browser.liveview import LiveView
        self.screen = FakeScreen(self.log)
        self.live = LiveView(store=self.store, browser=self.browser, screen=self.screen,
                             clock=self.clock, vnc_socket=tmp_path / "vnc.sock")
        self.ctl = RunController(Components(
            store=self.store, browser=self.browser, proxy=self.proxy, broker=self.broker,
            driver=self.driver, observer=self.observer, registry=self.registry,
            downloader=self.downloader, pacer=self._pacer,
            locale=locale, configured=configured, clock=self.clock, grants=self.live,
            ops=FakeOps(self.log)))
        ctl_mod.PUMP_S = 3600   # tests drive the pump by hand
        self.mod = ctl_mod

    def _pacer(self, page):
        p = FakePacer(self.log, page)
        p.gate = self.gate
        for name, value in self.pacer_hooks.items():
            setattr(p, name, value)
        self.pacers.append(p)
        return p

    async def start(self):
        await self.ctl.start()
        await self.settle()
        return self

    async def settle(self):
        """Let spawned controller tasks (advance, interrupts) run to their next wait."""
        for _ in range(50):
            await asyncio.sleep(0)
        for _ in range(20):   # a test that holds a gate leaves its task waiting: bounded
            pending = [t for t in self.ctl._tasks if not t.done()]
            busy = [t for t in pending if not _parked(t)]
            if not busy:
                return
            await asyncio.wait(busy, timeout=0.02)

    async def run(self, s=None, **kw):
        run, created = await self.ctl.start_run(s or spec(**kw))
        await self.settle()
        return self.store.get_run(run.run_id)

    async def pump(self):
        """One poll of the controller's own event pump."""
        await self.ctl._pump_once(self.ctl.active)
        await self.settle()

    def events(self, run_id, kind=None):
        return [e for e in self.store.events_after(run_id, 0, 10_000)
                if kind is None or e.kind == kind]

    def state(self, run_id):
        return self.store.get_run(run_id).state

    def page(self):
        return self.browser.session.page

    def close(self):
        self.store.close()


def _parked(task) -> bool:
    """A task waiting on a long sleep (retention, teardown retry) counts as idle."""
    coro = task.get_coro()
    frame = getattr(coro, "cr_frame", None)
    while frame is not None:
        if frame.f_code.co_name in ("_retry_teardown", "_retention_loop", "_pump"):
            return True
        awaited = getattr(coro, "cr_await", None)
        coro = awaited
        frame = getattr(coro, "cr_frame", None) if coro is not None else None
    return False
