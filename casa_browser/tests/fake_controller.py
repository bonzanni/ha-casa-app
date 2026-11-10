"""``FakeController``: an in-process controller that serves every ``/v1`` route of protocol 1.

Plan 1 Task 3 fixes its home and constructor; Plan 2's contract test imports it and runs
Fido's real client against ``casa_browser.api.make_app(FakeController(...))``. It stays in
the tree beside the real controller, and keeps serving every route (``test_fake_serves_every_route``).

It is a fake of the *controller*, not of the API: authentication, protocol checks, wire
parsing and error codes are the real ``casa_browser.api`` code. State lives in a real
``Store`` (runs, events, records, files, notices, pairing); browser work is canned.
Everything here is synthetic.
"""
from __future__ import annotations

import base64
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from casa_browser.pairing import Pairing
from casa_browser.protocol import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    ActionResult,
    ActiveRun,
    ElementView,
    FileRecord,
    LiveGrant,
    Observation,
    ProtocolError,
    Run,
    RunRecord,
    SafeEvent,
    Site,
    SiteSummary,
    Source,
)
from casa_browser.store import Store

# A 1x1 transparent PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
PDF = b"%PDF-1.7\n% synthetic test file\n%%EOF\n"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


class FakeController:
    def __init__(self, store_path: Path | str, *, clock: Callable[[], datetime] = _utcnow,
                 configured: bool = True, needs_locale: bool = False,
                 language_source: str | None = "home_assistant"):
        self.store = Store(store_path)
        self.clock = clock
        self.observe_flags: list[bool] = []    # each observe's sign_in flag, in order
        self.observe_finds: list = []          # each observe's words, in order
        self.configured = configured
        self.needs_locale = needs_locale
        self.language_source = None if needs_locale else language_source
        self.pairing = Pairing(self.store, clock=clock, on_revoke=self.revoke_authority)
        self.pairing.close_window_at_start()
        self.generation: dict[str, int] = {}
        self.live: dict[str, LiveGrant] = {}     # run_id -> grant
        self.live_purpose: dict[str, str] = {}
        self.sources_by_run: dict[str, list[Source]] = {}
        self.screenshots: dict[tuple[str, str], bytes] = {}
        self.blobs: dict[str, bytes] = {}
        self.deliveries: dict[tuple[str, str], str] = {}
        self.revocations = 0
        # The action gate, canned: an account run's actions and downloads are judged (a packet
        # per pending judgement); a verdict is applied once and its response stored.
        self.pending: dict[str, dict] = {}        # run_id -> {id, digest, kind, ...}
        self.responses: dict[tuple[str, str], dict] = {}
        self.questions: dict[str, dict] = {}      # question_id -> {run_id, revision, digest, ...}
        # Slice 6: a run whose sign-in waits for a code the site sent (run_id -> site_id), set by
        # a test; the canned reply of the code's fill; the codes received (test-private).
        self.code_sites: dict[str, str] = {}
        self.code_reply: dict = {"outcome": "done"}
        self.codes: list[str] = []
        self.sign_ins: list = []                # slice 5n: (run_id, site_name) per sign-in card

    # -- helpers --------------------------------------------------------------------------

    def _event(self, run_id: str, kind: str, outcome: str, text: str = "",
               screenshot_id: str | None = None) -> int:
        return self.store.append_event(run_id, SafeEvent(0, run_id, kind, None, None, text, outcome,
                                                         screenshot_id, self.clock()))

    def _run(self, run_id: str) -> Run:
        run = self.store.get_run(run_id)
        if run is None:
            raise ProtocolError("not_found")
        return run

    def _require_running(self, run_id: str) -> Run:
        run = self._run(run_id)
        if run.state == "interrupted":
            raise ProtocolError("interrupted")
        if run.state != "running":
            raise ProtocolError("not_ready")
        return run

    # -- hello, sites --------------------------------------------------------------------------

    async def hello(self) -> dict:
        return {"configured": self.configured, "needs_locale": self.needs_locale,
                "language_source": self.language_source}

    async def list_sites(self) -> list[SiteSummary]:
        return [SiteSummary(s.site_id, s.display_name, "ready" if s.vault_item_id else "needs_item")
                for s in self.store.list_sites()]

    async def register_site(self, payload: dict) -> str:
        site_id = _new_id("site")
        self.store.put_site(Site(site_id, payload["display_name"], payload["origins"],
                                 payload["login_origins"], payload["vault_item_id"], (),
                                 payload["start_page"], 1))
        return site_id

    # -- runs ------------------------------------------------------------------------------

    async def start_run(self, spec) -> tuple[Run, bool]:
        if self.needs_locale or not self.configured:
            raise ProtocolError("not_ready")
        for grant in spec.sites:
            if self.store.get_site(grant.site_id) is None:
                raise ProtocolError("not_found")
        run, created = self.store.create_run(spec, _new_id("run"), now=self.clock())
        if created:
            self._event(run.run_id, "run_queued", "queued")
            leased = self.store.lease_next_run()
            if leased is not None:
                self.store.transition(leased.run_id, "starting", "running")
                self._event(leased.run_id, "run_started", "running")
            run = self._run(run.run_id)
        return run, created

    async def list_active(self) -> list[ActiveRun]:
        def open_question(run_id):
            return next((qid for qid, q in self.questions.items()
                         if q["run_id"] == run_id and q["state"] in ("open", "confirming")), None)
        return [ActiveRun(r.run_id, r.spec.request_id, r.state, r.created_at,
                          open_question(r.run_id), self.code_sites.get(r.run_id))
                for r in self.store.list_runs(ACTIVE_STATES)]

    async def ask_sign_in(self, run_id: str, site_name: str) -> ActionResult:
        self._require_running(run_id)
        self.sign_ins.append((run_id, site_name))
        return ActionResult("not_configured", 0)

    async def login_code(self, run_id: str, login) -> ActionResult:
        self._require_running(run_id)
        if self.code_sites.get(run_id) != login.site_id:
            raise ProtocolError("not_ready")
        del self.code_sites[run_id]
        self.codes.append(login.value)
        reply = dict(self.code_reply)
        outcome = reply.pop("outcome")
        if outcome == "login_needed" and reply.get("reason") == "needs_code":
            self.code_sites[run_id] = login.site_id
        self._event(run_id, "login_code", "logged_in" if outcome == "done" else "not_signed_in",
                    "A sign-in code was given.")
        return ActionResult(outcome, self.generation.setdefault(run_id, 1), reply)

    async def get_run(self, run_id: str) -> Run | None:
        return self.store.get_run(run_id)

    async def events(self, run_id: str, after: int) -> list[SafeEvent]:
        return self.store.events_after(run_id, after)

    async def observe(self, run_id: str, sign_in: bool = False,
                      find: str | None = None) -> Observation:
        self.observe_flags.append(sign_in)
        self.observe_finds.append(find)
        run = self._require_running(run_id)
        gen = self.generation.setdefault(run_id, 1)
        source_id = None
        if run.spec.mode == "open_web":
            src = self.sources_by_run.setdefault(run_id, [])
            if not src:
                src.append(Source("src_1", "https://example.org/article", "Example article"))
            source_id = src[0].source_id
        return Observation(page_handle="pg_1", generation=gen,
                           text="Example page. Invoices are listed below.",
                           elements=(ElementView("el_1", "link", "Invoices"),
                                     ElementView("el_2", "button", "Download invoice")),
                           source_id=source_id)

    def _judge(self, run: Run, kind: str, target: str, **facts) -> ActionResult:
        """A canned pending judgement: the packet is the app's canonical JSON text."""
        import hashlib
        import json
        packet = {"task": run.spec.task_text, "action": {"kind": kind, **facts},
                  "target": {"name": target}, "page": {"blocks": [], "controls": []}}
        text = json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        jid = _new_id("j")
        self.pending[run.run_id] = {"id": jid, "kind": "action", "op": kind,
                                    "digest": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        return ActionResult("judgement_required", self.generation.setdefault(run.run_id, 1),
                            {"judgement": {"id": jid, "kind": "action", "packet": text}})

    async def act(self, run_id: str, action) -> ActionResult:
        run = self._require_running(run_id)
        gen = self.generation.setdefault(run_id, 1)
        if action.generation != gen:
            raise ProtocolError("stale_handle")
        if run.spec.mode == "account" and action.kind != "scroll":
            return self._judge(run, action.kind, action.handle)
        self.generation[run_id] = gen + 1
        self._event(run_id, "action", "done")
        return ActionResult("done", gen + 1)

    async def search(self, run_id: str, query: str) -> Observation:
        return await self.observe(run_id)

    async def fetch(self, run_id: str, url: str, fields: tuple[str, ...]):
        self._require_running(run_id)
        return ActionResult("not_run", 0, {"reason": "http_403"})

    async def download(self, run_id: str, handle: str):
        run = self._require_running(run_id)
        if run.spec.mode == "account":
            result = self._judge(run, "download", handle)
            self.pending[run_id]["handle"] = handle
            return result
        return self._file(run, handle)

    async def apply_judgement(self, run_id: str, judgement_id: str, body: dict) -> dict:
        import hashlib
        import json
        stored = self.responses.get((run_id, judgement_id))
        if stored is not None:
            return {**stored, "repeat": True}
        run = self._run(run_id)
        p = self.pending.get(run_id)
        if p is None or p["id"] != judgement_id:
            raise ProtocolError("judgement_stale")
        if "no_verdict" not in body and body["packet_sha256"] != p["digest"]:
            raise ProtocolError("digest_mismatch")
        del self.pending[run_id]
        verdict = body.get("verdict") or {}
        gen = self.generation.setdefault(run_id, 1)
        v = verdict.get("verdict")
        if p["kind"] == "rejudge":
            q = self.questions[p["question_id"]]
            if v == "same" and not verdict.get("text_incomplete"):
                q["state"] = "executed"
                self.store.transition(run_id, "waiting_approval", "running")
                response = {"outcome": "done", "generation": gen + 1}
                self.generation[run_id] = gen + 1
            else:
                q["state"] = "lapsed"
                self.store.transition(run_id, "waiting_approval", "running")
                response = {"outcome": "lapsed", "generation": gen, "reason": "changed"}
        elif v is None:
            response = {"outcome": "check_unavailable", "generation": gen}
        elif v == "look_first":
            response = {"outcome": "look_first", "generation": gen,
                        "note": str(verdict.get("look_first") or "")[:600]}
        elif v == "ask" or verdict.get("category") not in ("reading", "local"):
            qid = _new_id("q")
            question = verdict.get("question") if v == "ask" else None
            view = {"text": (question or {}).get("text", "Ellen was about to press a control."),
                    "confirm_label": (question or {}).get("confirm_label", ""),
                    "extra_options": list((question or {}).get("extra_options") or []),
                    "highlight": list((question or {}).get("highlight") or []),
                    "screenshot_id": None, "confirm_offered": question is not None
                    and not question.get("text_incomplete"),
                    "live_only_copy": None, "category": verdict.get("category")}
            self.questions[qid] = {"run_id": run_id, "revision": 1, "digest": p["digest"],
                                   "state": "open", "view": view, "response": None}
            self.store.transition(run_id, "running", "waiting_approval")
            response = {"outcome": "asked", "generation": gen, "question_id": qid}
        else:
            self.generation[run_id] = gen + 1
            response = {"outcome": "done", "generation": gen + 1}
            if p["op"] == "download":
                response["file"] = self._file(run, p["handle"]).to_wire()
            self._event(run_id, "action", "done")
        self.responses[(run_id, judgement_id)] = response
        return response

    async def pending_judgements(self, run_id: str) -> dict:
        """Slice 5: the app-started judgements (``login_item``, ``signin``) waiting for the
        plugin's relay; canned by the tests (``app_pending[run_id]``)."""
        self._run(run_id)
        return {"judgements": list(getattr(self, "app_pending", {}).get(run_id, ()))}

    async def get_question(self, run_id: str, question_id: str) -> dict:
        q = self.questions.get(question_id)
        if q is None or q["run_id"] != run_id:
            raise ProtocolError("not_found")
        return {"question_id": question_id, "run_id": run_id, "revision": q["revision"],
                "digest": q["digest"], "state": q["state"], "view": q["view"]}

    async def live_from_question(self, run_id: str, question_id: str, body: dict) -> dict:
        q = self.questions.get(question_id)
        if q is None or q["run_id"] != run_id:
            raise ProtocolError("not_found")
        key = ("live", question_id, body["revision"], body["n"])
        stored = self.responses.get(key)
        if stored is not None:
            return {**stored, "repeat": True}
        gen = self.generation.setdefault(run_id, 1)
        if q["state"] != "open" or q["revision"] != body["revision"] or q["digest"] != body["digest"]:
            response = {"outcome": "not_current", "generation": gen}
        elif self._run(run_id).state in ("waiting_live", "live"):
            response = {"outcome": "live_open", "generation": gen}
        else:
            self.store.transition(run_id, "waiting_approval", "waiting_live")
            response = {"outcome": "live", "generation": gen,
                        "grant": {"grant_id": _new_id("g"), "expires_at": "2030-01-01T00:00:00Z",
                                  "operator_delivery_path": "live/fake"}}
        self.responses[key] = response
        return response

    async def answer_question(self, run_id: str, question_id: str, body: dict) -> dict:
        import hashlib
        import json
        q = self.questions.get(question_id)
        if q is None or q["run_id"] != run_id:
            raise ProtocolError("not_found")
        if q["response"] is not None and q["revision"] == body["revision"]:
            return {**q["response"], "repeat": True}
        gen = self.generation.setdefault(run_id, 1)
        if q["state"] != "open" or q["revision"] != body["revision"] or q["digest"] != body["digest"]:
            return {"outcome": "not_current", "generation": gen}
        choice = body["choice"]
        if choice == "not_now":
            q["state"], response = "declined", {"outcome": "declined", "generation": gen}
            self.store.transition(run_id, "waiting_approval", "running")
        elif isinstance(choice, dict):
            options = q["view"]["extra_options"]
            if choice["extra"] >= len(options):
                raise ProtocolError("unsupported")
            q["state"] = "answered"
            response = {"outcome": "answered", "generation": gen, "label": options[choice["extra"]]}
            self.store.transition(run_id, "waiting_approval", "running")
        else:
            if not q["view"]["confirm_offered"]:
                raise ProtocolError("unsupported")
            text = json.dumps({"approved_question": q["view"]["text"]}, sort_keys=True,
                              ensure_ascii=False, separators=(",", ":"))
            jid = _new_id("j")
            self.pending[run_id] = {"id": jid, "kind": "rejudge", "question_id": question_id,
                                    "digest": hashlib.sha256(text.encode("utf-8")).hexdigest()}
            q["state"] = "confirming"
            response = {"outcome": "rejudgement_required", "generation": gen,
                        "question_id": question_id,
                        "judgement": {"id": jid, "kind": "rejudge", "packet": text}}
        q["response"] = response
        return response

    def _file(self, run: Run, handle: str) -> FileRecord:
        run_id = run.run_id
        file_id = _new_id("file")
        self.blobs[file_id] = PDF
        import hashlib
        record = FileRecord(file_id, run_id, run.spec.requester_id, "invoice.pdf", "application/pdf",
                            len(PDF), hashlib.sha256(PDF).hexdigest(), f"/fake/{file_id}", handle)
        self.store.put_file(record, now=self.clock())
        return record

    async def archive(self, run_id: str, file_ids: tuple) -> FileRecord:
        run = self._run(run_id)
        for fid in file_ids:
            rec = self.store.get_file(fid)
            if rec is None or rec.run_id != run_id or rec.requester_id != run.spec.requester_id:
                raise ProtocolError("not_found")
        file_id = _new_id("file")
        data = b"PK\x05\x06" + b"\x00" * 18
        self.blobs[file_id] = data
        import hashlib
        record = FileRecord(file_id, run_id, run.spec.requester_id, "files.zip", "application/zip",
                            len(data), hashlib.sha256(data).hexdigest(), f"/fake/{file_id}", None)
        self.store.put_file(record, now=self.clock())
        return record

    # -- live view ----------------------------------------------------------------------------

    async def request_live(self, run_id: str, purpose: str) -> LiveGrant:
        self._require_running(run_id)
        grant_id = _new_id("grant")
        grant = LiveGrant(grant_id, self.clock() + timedelta(seconds=120), f"live/{grant_id}")
        self.live[run_id] = grant
        self.live_purpose[run_id] = purpose
        self.store.transition(run_id, "running", "waiting_live")
        return grant

    async def live_complete(self, run_id: str, operator_ref: str) -> Run:
        run = self._run(run_id)
        if run.state not in ("waiting_live", "live"):
            raise ProtocolError("conflict")
        self.live.pop(run_id, None)
        self.live_purpose.pop(run_id, None)
        self.store.transition(run_id, run.state, "running")
        return self._run(run_id)

    # -- ending ------------------------------------------------------------------------------

    async def finish(self, run_id: str, manifest) -> Run:
        run = self._run(run_id)
        issued = {s.source_id for s in self.sources_by_run.get(run_id, [])}
        if any(s not in issued for s in manifest.source_ids):
            raise ProtocolError("unsupported")
        for fid in manifest.file_ids:
            rec = self.store.get_file(fid)
            if rec is None or rec.run_id != run_id:
                raise ProtocolError("unsupported")
        if run.state in TERMINAL_STATES:
            raise ProtocolError("conflict")
        if run.state != "running":
            raise ProtocolError("not_ready")
        self.store.transition(run_id, "running", "finishing")
        self.store.put_record(RunRecord(run_id, run.spec.requester_id,
                                        tuple(g.site_id for g in run.spec.sites),
                                        manifest.summary_masked, (), manifest.file_ids, (),
                                        self.clock()))
        self.store.transition(run_id, "finishing", "completed", now=self.clock())
        self._event(run_id, "run_finished", "completed")
        return self._run(run_id)

    async def cancel(self, run_id: str) -> Run:
        run = self._run(run_id)
        if run.state not in TERMINAL_STATES:
            self.store.transition(run_id, run.state, "cancelled", now=self.clock())
            self.live.pop(run_id, None)
            self._event(run_id, "run_cancelled", "cancelled")
        return self._run(run_id)

    async def request_stopped(self, request_id: str) -> bool:
        return self.store.request_cancelled(request_id)

    async def cancel_requests(self, request_ids: tuple) -> dict:
        out = []
        for request_id in request_ids:
            run = self.store.run_by_request(request_id)
            if run is None or run.state not in TERMINAL_STATES:
                self.store.cancel_request_id(request_id, now=self.clock())
            if run is not None:
                run = await self.cancel(run.run_id)
            out.append({"request_id": request_id, "run_id": None if run is None else run.run_id,
                        "stopped": self.store.request_cancelled(request_id),
                        "state": "cancelled" if run is None else run.state})
        return {"requests": out}

    async def forget(self, site_id: str, operator_ref: str) -> dict:
        if self.store.get_site(site_id) is None:
            raise ProtocolError("not_found")
        return {"site_id": site_id, "status": "forgotten"}

    # -- records, files, notices, screenshots, sources --------------------------------------

    async def records(self, requester_id: str, since: datetime) -> list[RunRecord]:
        return self.store.records_for(requester_id, since)

    async def file_bytes(self, file_id: str) -> tuple[FileRecord, bytes]:
        rec = self.store.get_file(file_id)
        if rec is None or file_id not in self.blobs:
            raise ProtocolError("not_found")
        return rec, self.blobs[file_id]

    async def ack_delivery(self, file_id: str, body: dict) -> dict:
        rec = self.store.get_file(file_id)
        if rec is None or rec.requester_id != body["requester_id"]:
            raise ProtocolError("not_found")
        key = (file_id, body["delivery_id"])
        current = self.deliveries.get(key)
        if current == body["outcome"]:
            return {"file_id": file_id, "delivery_id": body["delivery_id"],
                    "outcome": current, "recorded": False}
        if current is not None and current != "deposited":
            raise ProtocolError("conflict")
        self.deliveries[key] = body["outcome"]
        return {"file_id": file_id, "delivery_id": body["delivery_id"],
                "outcome": body["outcome"], "recorded": True}

    async def notices(self, after: int):
        return self.store.notices_after(after)

    async def screenshot(self, run_id: str, screenshot_id: str) -> bytes:
        data = self.screenshots.get((run_id, screenshot_id))
        if data is None:
            raise ProtocolError("not_found")
        return data

    async def judgement_picture(self, run_id: str, judgement_id: str) -> bytes:
        p = self.pending.get(run_id)
        if p is None or p["id"] != judgement_id:
            raise ProtocolError("not_found")
        return b"\x89PNG\r\n\x1a\n" + b"\0" * 32

    async def look(self, run_id: str, handle: str | None) -> str | None:
        self._require_running(run_id)
        return "iVBORw0KGgo="

    async def read(self, run_id: str, handle: str | None, file_id: str | None, offset: int,
                   rows: bool = False) -> dict:
        self._require_running(run_id)
        return {"text": "page text"[offset:], "next_offset": None, "complete": True, "masked": False}

    async def take_screenshot(self, run_id: str) -> str:
        run = self._run(run_id)
        if run.state != "running":
            raise ProtocolError("not_ready" if run.state not in TERMINAL_STATES else "interrupted")
        shot = "shot_" + secrets.token_hex(8)
        self.screenshots[(run_id, shot)] = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
        return shot

    async def sources(self, run_id: str) -> list[Source]:
        self._run(run_id)
        return list(self.sources_by_run.get(run_id, []))

    # -- authority -----------------------------------------------------------------------------

    def revoke_authority(self) -> None:
        """Pairing rotation: every run and live grant issued under the old secret stops."""
        self.revocations += 1
        for run in self.store.list_runs(ACTIVE_STATES):
            self.store.transition(run.run_id, run.state, "interrupted", now=self.clock())
        self.live.clear()
        self.live_purpose.clear()
