"""Casa Browser's protocol v1: the shared persistence types and the wire contract.

Every type the store keeps and every body the API accepts or returns is defined here,
once. Later modules add behaviour behind these shapes; they never replace a wire shape.

Three rules hold for every type in this module:

* Private members (raw addresses, vault item ids, file paths)
  are excluded from ``repr`` and from every ``to_wire`` projection. Only the trusted
  ``sources`` route returns an address (``Source.url``), and only for public-session pages.
* A mutation body is parsed by a ``parse_*`` function that refuses unknown members, a
  missing required member and every value outside its bound with ``ProtocolError``
  (``unsupported``) before anything happens.
* Ids the app issues match ``ID_PATTERN``; identifiers a caller supplies are printable
  ASCII without whitespace (``CALLER_ID_PATTERN``).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

PROTOCOL_VERSION = 1
PROTOCOL_HEADER = "X-Casa-Browser-Protocol"

ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
CALLER_ID_PATTERN = re.compile(r"[\x21-\x7e]{1,128}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")

# Text bounds (Plan 1 Task 3 "Bounds", at least Plan 2's limits and no more).
MAX_TASK_KIND = 60
MAX_TASK_TEXT = 2000
MAX_RESULT_EXPECTATION = 500
MAX_QUERY = 300
MAX_SUMMARY = 2000
MAX_CALLER_LABEL = 64
MAX_DELIVERY_PATH = 256
MAX_ACTION_TEXT = 2000
MAX_LIST = 200

# ---------------------------------------------------------------------------
# Errors: one stable code per failure, one HTTP status per code. The reply body of
# every refusal is exactly {"error": "<code>"}.
# ---------------------------------------------------------------------------

ERROR_STATUS: dict[str, int] = {
    "unauthorized": 401,
    "protocol_mismatch": 400,
    "unsupported": 400,
    "blocked": 403,
    "not_found": 404,
    "conflict": 409,
    "stale_handle": 409,
    "interrupted": 409,
    "judgement_stale": 409,     # a verdict for a judgement that is not the run's pending one
    "digest_mismatch": 409,     # a verdict bound to other packet bytes than the frozen ones
    "file_too_large": 413,
    "internal": 500,
    "not_ready": 503,
}
ERROR_CODES = frozenset(ERROR_STATUS)


class ProtocolError(Exception):
    """A typed refusal. ``code`` is one of ``ERROR_CODES``; the message never carries
    the offending value (it could be a secret or a raw address)."""

    def __init__(self, code: str = "unsupported"):
        if code not in ERROR_STATUS:
            raise ValueError("unknown error code")
        super().__init__(code)
        self.code = code

    @property
    def status(self) -> int:
        return ERROR_STATUS[self.code]


def _refuse() -> ProtocolError:
    return ProtocolError("unsupported")


# ---------------------------------------------------------------------------
# Small validators
# ---------------------------------------------------------------------------


def is_app_id(value: Any) -> bool:
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def is_caller_id(value: Any) -> bool:
    return isinstance(value, str) and CALLER_ID_PATTERN.fullmatch(value) is not None


def _obj(body: Any, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(body, dict):
        raise _refuse()
    keys = set(body)
    if not required <= keys or keys - required - optional:
        raise _refuse()
    return body


def _str(value: Any, limit: int, *, allow_empty: bool = True) -> str:
    """``allow_empty=False`` also refuses a whitespace-only value."""
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value.strip()):
        raise _refuse()
    if any(ch in value for ch in ("\x00",)):
        raise _refuse()
    return value


def _app_id(value: Any) -> str:
    if not is_app_id(value):
        raise _refuse()
    return value


def _caller_id(value: Any) -> str:
    if not is_caller_id(value):
        raise _refuse()
    return value


def _int(value: Any, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise _refuse()
    return value


def _choice(value: Any, allowed: frozenset[str] | set[str] | tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _refuse()
    return value


def _list(value: Any, lo: int, hi: int) -> list:
    if not isinstance(value, list) or not lo <= len(value) <= hi:
        raise _refuse()
    return value


def rfc3339(at: datetime) -> str:
    """UTC, second precision, ``Z`` suffix."""
    if at.tzinfo is None:
        raise ValueError("naive datetime")
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Admission digest (X4): SHA-256 over the RFC 8785 canonical JSON of the RunSpec
# members except ``admission``.
# ---------------------------------------------------------------------------

_MAX_SAFE_INT = 2**53 - 1


def _check_canonical(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("too deep")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INT:
            raise ValueError("integer outside the I-JSON range")
        return
    if isinstance(value, str):
        value.encode("utf-8")  # a lone surrogate raises UnicodeEncodeError (a ValueError)
        return
    if isinstance(value, list):
        for item in value:
            _check_canonical(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key.isascii():
                raise ValueError("object keys must be ASCII strings")
            _check_canonical(item, depth + 1)
        return
    raise ValueError("unsupported JSON value")


def canonical_json(members: Mapping[str, Any]) -> bytes:
    """RFC 8785 canonical bytes for the value domain the protocol allows.

    ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`` equals JCS
    only because floats, non-ASCII keys (JCS sorts by UTF-16 code units, Python by code
    points; they agree on ASCII), integers beyond 2**53-1 and every non-JSON type are
    refused first with ``ValueError``.
    """
    if not isinstance(members, Mapping):
        raise ValueError("members must be an object")
    _check_canonical(dict(members))
    text = json.dumps(dict(members), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def spec_digest(members: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(members)).hexdigest()


# ---------------------------------------------------------------------------
# Locale
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectiveLocale:
    language: str
    timezone: str
    language_source: str  # 'option' | 'home_assistant'


# ---------------------------------------------------------------------------
# Sites, grants and runs
# ---------------------------------------------------------------------------

SITE_ACCESS = ("account", "public")
RUN_MODES = ("account", "open_web")
ORIGIN_KINDS = ("operator_telegram",)


@dataclass(frozen=True)
class SiteGrant:
    site_id: str
    access: str  # 'account' | 'public'

    def to_wire(self) -> dict:
        return {"site_id": self.site_id, "access": self.access}


@dataclass(frozen=True)
class ProviderBinding:
    """A recorded sign-in provider binding. Private: never serialised to a caller."""

    provider_origin: str = field(repr=False)
    path_tag: str = field(repr=False)
    client_id_tag: str = field(repr=False)
    redirect_uri_tag: str = field(repr=False)

    def __post_init__(self):
        if any(not isinstance(tag, str) or re.fullmatch(r"[0-9a-f]{64}", tag) is None
               for tag in (self.path_tag, self.client_id_tag, self.redirect_uri_tag)):
            raise ValueError("unsupported")



@dataclass(frozen=True)
class Site:
    site_id: str
    display_name: str
    origins: tuple[str, ...] = field(repr=False)
    login_origins: tuple[str, ...] = field(repr=False)
    vault_item_id: str | None = field(repr=False)
    provider_bindings: tuple[ProviderBinding, ...] = field(repr=False)
    start_page_private: str | None = field(repr=False)
    revision: int = 1


@dataclass(frozen=True)
class Admission:
    origin_kind: str
    origin_ref: str
    admitted_spec_digest: str

    def to_wire(self) -> dict:
        return {"origin_kind": self.origin_kind, "origin_ref": self.origin_ref,
                "admitted_spec_digest": self.admitted_spec_digest}


@dataclass(frozen=True)
class RunSpec:
    request_id: str
    requester_id: str
    mode: str
    task_text: str
    task_kind: str
    sites: tuple[SiteGrant, ...]
    result_expectation: str
    admission: Admission
    #: Slice 5b (F-a): the public address an open-web run opens first, when the task gives one.
    start_url: str | None = None

    def members(self) -> dict:
        """The digest members (every RunSpec member except ``admission``; ``start_url`` only
        when the spec carries one, so a spec without it digests as before)."""
        out = {"request_id": self.request_id, "requester_id": self.requester_id,
               "mode": self.mode, "task_text": self.task_text, "task_kind": self.task_kind,
               "sites": [s.to_wire() for s in self.sites],
               "result_expectation": self.result_expectation}
        if self.start_url is not None:
            out["start_url"] = self.start_url
        return out

    def to_wire(self) -> dict:
        return {**self.members(), "admission": self.admission.to_wire()}


RUN_STATES = ("queued", "starting", "running", "waiting_approval", "waiting_live", "live",
              "finishing", "completed", "failed", "cancelled", "interrupted")
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})
ACTIVE_STATES = tuple(s for s in RUN_STATES if s not in TERMINAL_STATES)


@dataclass(frozen=True)
class Run:
    run_id: str
    spec: RunSpec
    state: str
    created_at: datetime
    policy_revision: int = 0

    def to_wire(self) -> dict:
        return {"run_id": self.run_id, "request_id": self.spec.request_id, "state": self.state,
                "created_at": rfc3339(self.created_at), "mode": self.spec.mode}


@dataclass(frozen=True)
class SafeEvent:
    seq: int
    run_id: str
    kind: str
    origin: str | None
    handle: str | None
    text_masked: str
    outcome: str
    screenshot_id: str | None
    at: datetime

    def to_wire(self) -> dict:
        return {"seq": self.seq, "run_id": self.run_id, "kind": self.kind,
                "origin": self.origin, "handle": self.handle, "text_masked": self.text_masked,
                "outcome": self.outcome, "screenshot_id": self.screenshot_id,
                "at": rfc3339(self.at)}


@dataclass(frozen=True)
class SafeNotice:
    """A durable notice for trusted delivery (credential change, interruption)."""

    notice_id: int
    kind: str
    run_id: str | None
    site_id: str | None
    text_masked: str
    at: datetime
    dedupe_key: str | None = field(default=None, repr=False)

    def to_wire(self) -> dict:
        # ``seq`` is the notice cursor (``GET /v1/notices?after=<seq>``).
        return {"seq": self.notice_id, "kind": self.kind, "run_id": self.run_id,
                "site_id": self.site_id, "text_masked": self.text_masked,
                "at": rfc3339(self.at)}


# ---------------------------------------------------------------------------
# Observations and actions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ElementView:
    handle: str
    role: str
    label_masked: str
    kind: str | None = None    # slice 5g: a field whose value is not free text (gate.FIELD_KINDS)
    # Slice 5j: what an ordinary field holds, masked and bounded (a select's chosen option), and
    # a checkbox's or radio's state; never for a password, one-time-code or payment-card field.
    value_masked: str | None = None
    checked: bool | None = None

    def to_wire(self) -> dict:
        out = {"handle": self.handle, "role": self.role, "label_masked": self.label_masked}
        if self.kind is not None:
            out["kind"] = self.kind
        if self.value_masked is not None:
            out["value_masked"] = self.value_masked
        if self.checked is not None:
            out["checked"] = self.checked
        return out


@dataclass(frozen=True)
class Observation:
    page_handle: str
    generation: int
    text: str
    elements: tuple[ElementView, ...]
    source_id: str | None = None
    # Signing in off the start page: the sign-in step the look recorded (Casa Browser signed
    # in by itself, or the login it needs), or None.
    notice: str | None = None

    def to_wire(self) -> dict:
        return {"page_handle": self.page_handle, "generation": self.generation,
                "text": self.text, "elements": [e.to_wire() for e in self.elements],
                "source_id": self.source_id, "notice": self.notice}


@dataclass(frozen=True)
class Source:
    source_id: str
    url: str = field(repr=False)
    title_masked: str = ""

    def to_wire(self) -> dict:
        # Only the trusted control sources route calls this.
        return {"source_id": self.source_id, "url": self.url, "title_masked": self.title_masked}


ACTION_KINDS = ("navigate", "click", "type", "scroll", "select", "back")
SCROLL_DIRECTIONS = ("up", "down")
SCROLL_AMOUNTS = ("step", "page")


@dataclass(frozen=True)
class Action:
    kind: str
    handle: str
    generation: int
    text: str | None = field(default=None, repr=False)
    choice_handle: str | None = None
    direction: str | None = None
    amount: str | None = None
    enter: bool = False          # a ``type`` that ends in Enter (the guarded Enter, S2-4)


# The outcomes of a step (an action, a download, a search, a verdict, an answer). ``done`` ran
# it; ``judgement_required``/``rejudgement_required`` carry the packet the plugin's code relays
# to Casa's check; ``asked`` froze a question; ``look_first`` carries the check's note;
# ``not_run`` ran nothing (``reason``); ``check_unavailable`` had no verdict (F1);
# ``outcome_unknown`` may have run and is never retried (F8). Tap answers add ``declined``,
# ``answered``, ``lapsed`` and ``not_current``.
STEP_OUTCOMES = ("done", "judgement_required", "rejudgement_required", "asked", "look_first",
                 "not_run", "check_unavailable", "outcome_unknown", "declined", "answered",
                 "lapsed", "not_current",
                 # slice 5: an app-started judgement's or a login question's outcome
                 "login_needed", "linked", "not_linked", "credential_saved",
                 "credential_not_saved", "not_saved",
                 # slice 5n: the sign-in card for a site that is not set up (``POST
                 # /v1/runs/{run}/sign-in``) and its answer
                 "judging", "signing_in", "not_opened", "already_saved", "not_configured", "question_open",
                 "account_site", "registered_site", "not_https", "page_changed")


@dataclass(frozen=True)
class ActionResult:
    outcome: str
    generation: int
    extra: dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        return {"outcome": self.outcome, "generation": self.generation, **self.extra}


# ---------------------------------------------------------------------------
# Files and records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileRecord:
    file_id: str
    run_id: str
    requester_id: str
    safe_name: str
    mime: str
    size: int
    sha256: str
    private_path: str = field(repr=False)
    source_handle: str | None = None
    created_at: datetime | None = None

    def to_wire(self) -> dict:
        return {"file_id": self.file_id, "run_id": self.run_id,
                "requester_id": self.requester_id, "safe_name": self.safe_name,
                "mime": self.mime, "size": self.size, "sha256": self.sha256,
                "source_handle": self.source_handle}


@dataclass(frozen=True)
class Change:
    kind: str
    target_handle: str | None
    before_masked: str
    after_masked: str

    def to_wire(self) -> dict:
        return {"kind": self.kind, "target_handle": self.target_handle,
                "before_masked": self.before_masked, "after_masked": self.after_masked}


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    requester_id: str
    sites: tuple[str, ...]
    summary_masked: str
    changes: tuple[Change, ...]
    file_ids: tuple[str, ...]
    delivery_outcomes: tuple[tuple[str, str, str], ...]  # (file_id, delivery_id, outcome)
    ended_at: datetime

    def to_wire(self) -> dict:
        return {"run_id": self.run_id, "requester_id": self.requester_id,
                "sites": list(self.sites), "summary_masked": self.summary_masked,
                "changes": [c.to_wire() for c in self.changes],
                "file_ids": list(self.file_ids),
                "delivery_outcomes": [{"file_id": f, "delivery_id": d, "outcome": o}
                                      for f, d, o in self.delivery_outcomes],
                "ended_at": rfc3339(self.ended_at)}


DELIVERY_OUTCOMES = ("deposited", "delivered", "withheld", "handed_off")
EXPECTATION_STATUS = ("matched", "mismatch", "unknown")


@dataclass(frozen=True)
class ResultManifest:
    summary_masked: str
    file_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    expectation_status: str


# ---------------------------------------------------------------------------
# Live view, listing (X5, X8)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LiveGrant:
    grant_id: str
    expires_at: datetime
    operator_delivery_path: str

    def to_wire(self) -> dict:
        return {"grant_id": self.grant_id, "expires_at": rfc3339(self.expires_at),
                "operator_delivery_path": self.operator_delivery_path}


@dataclass(frozen=True)
class ActiveRun:
    run_id: str
    request_id: str
    state: str
    created_at: datetime
    pending_question: str | None = None
    code_site: str | None = None         # slice 6: the site waiting for a sign-in code it sent

    def to_wire(self) -> dict:
        return {"run_id": self.run_id, "request_id": self.request_id, "state": self.state,
                "created_at": rfc3339(self.created_at),
                "pending_question": self.pending_question, "code_site": self.code_site}


@dataclass(frozen=True)
class SiteSummary:
    """``GET /v1/sites`` row: masked display name, id, setup status. No item id or URL."""

    site_id: str
    display_name: str
    status: str  # setup status: 'ready' | 'needs_login' | 'needs_item'

    def to_wire(self) -> dict:
        return {"site_id": self.site_id, "display_name": self.display_name,
                "status": self.status}


# ---------------------------------------------------------------------------
# Wire parsing — mutation bodies
# ---------------------------------------------------------------------------

RUN_SPEC_MEMBERS = {"request_id", "requester_id", "mode", "task_text", "task_kind", "sites",
                    "result_expectation", "admission"}


MAX_START_URL_BYTES = 2048
_START_URL = re.compile(r"https://[^\s/?#]+[^\s]*")


def start_url_ok(value: Any) -> bool:
    """Slice 5b (F-a): an absolute ``https`` address with a host, at most 2,048 bytes,
    printable, no whitespace."""
    if (not isinstance(value, str) or len(value.encode("utf-8")) > MAX_START_URL_BYTES
            or not value.isprintable() or _START_URL.fullmatch(value) is None):
        return False
    try:
        return bool(urlsplit(value).hostname)
    except ValueError:
        return False


def parse_run_spec(body: Any) -> tuple[RunSpec, dict]:
    """Parse ``POST /v1/runs``. Returns the RunSpec and the digest members exactly as
    received (``admission`` removed), so the digest is computed over the received body,
    not over a re-serialisation. ``start_url`` (slice 5b) only in an open-web spec."""
    body = _obj(body, RUN_SPEC_MEMBERS, {"start_url"})
    start_url = body.get("start_url")
    if "start_url" in body and (body.get("mode") != "open_web" or not start_url_ok(start_url)):
        raise _refuse()
    adm = _obj(body["admission"], {"origin_kind", "origin_ref", "admitted_spec_digest"})
    # An open-web run starts with no site; an account run names at least one.
    sites_raw = _list(body["sites"], 0 if body.get("mode") == "open_web" else 1, 16)
    sites = []
    for raw in sites_raw:
        raw = _obj(raw, {"site_id", "access"})
        sites.append(SiteGrant(_app_id(raw["site_id"]), _choice(raw["access"], SITE_ACCESS)))
    if len({s.site_id for s in sites}) != len(sites):
        raise _refuse()
    digest = adm["admitted_spec_digest"]
    if not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest):
        raise _refuse()
    spec = RunSpec(
        request_id=_caller_id(body["request_id"]),
        requester_id=_caller_id(body["requester_id"]),
        mode=_choice(body["mode"], RUN_MODES),
        task_text=_str(body["task_text"], MAX_TASK_TEXT, allow_empty=False),
        task_kind=_str(body["task_kind"], MAX_TASK_KIND, allow_empty=False),
        sites=tuple(sites),
        result_expectation=_str(body["result_expectation"], MAX_RESULT_EXPECTATION),
        admission=Admission(
            origin_kind=_str(adm["origin_kind"], 64, allow_empty=False),
            origin_ref=_caller_id(adm["origin_ref"]),
            admitted_spec_digest=digest,
        ),
        start_url=start_url,
    )
    members = {k: v for k, v in body.items() if k != "admission"}
    return spec, members


# The keyboard path is a closed set (S2-4 rule 1): typed text holds printable characters only.
# No C0 or C1 control character (Tab, Escape, Backspace and the rest) and no line or paragraph
# separator, so no character the pacer types can submit or navigate; only the guarded Enter
# (``enter``) can. Slice 6b: the one exception is a line break (CR and CRLF arrive as LF, what a
# textarea holds), which is never a key: the pacer inserts it as text, and only into a real
# <textarea> on a judged step (the controller decides that).
_NOT_PRINTABLE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def printable_text(value: str) -> bool:
    return _NOT_PRINTABLE.search(value) is None


def _typed_text(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if not printable_text(text.replace("\n", "")):
        raise _refuse()
    return text


def parse_action(body: Any) -> Action:
    body = _obj(body, {"kind", "handle", "generation"},
                {"text", "choice_handle", "direction", "amount", "enter"})
    kind = _choice(body["kind"], ACTION_KINDS)
    enter = body.get("enter", False)
    if not isinstance(enter, bool):
        raise _refuse()
    action = Action(
        kind=kind,
        handle=_app_id(body["handle"]),
        generation=_int(body["generation"], 0, 2**31),
        text=(None if body.get("text") is None
              else _typed_text(_str(body["text"], MAX_ACTION_TEXT))),
        choice_handle=None if body.get("choice_handle") is None else _app_id(body["choice_handle"]),
        direction=None if body.get("direction") is None else _choice(body["direction"], SCROLL_DIRECTIONS),
        amount=None if body.get("amount") is None else _choice(body["amount"], SCROLL_AMOUNTS),
        enter=enter,
    )
    if enter and kind != "type":
        raise _refuse()
    if kind == "scroll":
        if action.direction is None or action.amount is None:
            raise _refuse()
    elif action.direction is not None or action.amount is not None:
        raise _refuse()
    if kind == "type" and action.text is None:
        raise _refuse()
    if kind != "type" and action.text is not None:
        raise _refuse()
    if kind == "select" and action.choice_handle is None:
        raise _refuse()
    if kind != "select" and action.choice_handle is not None:
        raise _refuse()
    return action


DIGEST_HEX = re.compile(r"[0-9a-f]{64}")
MAX_VERDICT_BYTES = 16 * 1024


def parse_judgement_verdict(body: Any) -> dict:
    """``POST /v1/control/runs/{run}/judgements/{judgement}``: the verdict variant
    ``{"packet_sha256", "verdict"}`` (optional ``latency_ms``, the check's own), or the
    no-verdict variant ``{"no_verdict": "check_unavailable"}``. Only the shape is parsed here: a
    verdict outside the check's reply bounds is no verdict (F1), decided by the controller,
    never refused."""
    if isinstance(body, dict) and "no_verdict" in body:
        body = _obj(body, {"no_verdict"})
        _choice(body["no_verdict"], ("check_unavailable",))
        return {"no_verdict": "check_unavailable"}
    body = _obj(body, {"packet_sha256", "verdict"}, {"latency_ms"})
    sha = body["packet_sha256"]
    if not isinstance(sha, str) or not DIGEST_HEX.fullmatch(sha):
        raise _refuse()
    verdict = body["verdict"]
    if not isinstance(verdict, dict):
        raise _refuse()
    if len(json.dumps(verdict, ensure_ascii=False)) > MAX_VERDICT_BYTES:
        raise _refuse()
    latency = body.get("latency_ms")
    if latency is not None:
        latency = _int(latency, 0, 10**7)
    return {"packet_sha256": sha, "verdict": verdict, "latency_ms": latency}


ANSWER_CHOICES = ("confirm", "not_now")


def parse_answer(body: Any) -> dict:
    """``POST /v1/control/runs/{run}/questions/{question}/answer``: ``{"revision", "digest",
    "choice": "confirm" | "not_now" | {"extra": i} | {"item": i}, "operator_ref"}`` — ``item``
    answers a login question (slice 5: one of at most five 1Password items)."""
    body = _obj(body, {"revision", "digest", "choice", "operator_ref"})
    digest = body["digest"]
    if not isinstance(digest, str) or not DIGEST_HEX.fullmatch(digest):
        raise _refuse()
    choice = body["choice"]
    if isinstance(choice, dict) and "item" in choice:
        choice = {"item": _int(_obj(choice, {"item"})["item"], 0, 4)}
    elif isinstance(choice, dict):
        choice = {"extra": _int(_obj(choice, {"extra"})["extra"], 0, 2)}
    else:
        choice = _choice(choice, ANSWER_CHOICES)
    return {"revision": _int(body["revision"], 1, 2**31), "digest": digest, "choice": choice,
            "operator_ref": _caller_id(body["operator_ref"])}


def parse_question_live(body: Any) -> dict:
    """``POST /v1/control/runs/{run}/questions/{question}/live``: ``{"revision", "digest",
    "n", "operator_ref"}`` — ``n`` numbers the operator's live views from this question, so
    each is its own request and a repeat of one is answered from its stored response."""
    body = _obj(body, {"revision", "digest", "n", "operator_ref"})
    digest = body["digest"]
    if not isinstance(digest, str) or not DIGEST_HEX.fullmatch(digest):
        raise _refuse()
    return {"revision": _int(body["revision"], 1, 2**31), "digest": digest,
            "n": _int(body["n"], 1, 2**31), "operator_ref": _caller_id(body["operator_ref"])}


#: a site's one-time sign-in code (slice 6, S6-3): what sites send by email or SMS
LOGIN_CODE = re.compile(r"[A-Za-z0-9-]{4,10}")


@dataclass(frozen=True)
class LoginCode:
    """A sign-in code the model supplied, for the site that asked for it; its ``repr`` never
    shows the code (S6-3)."""

    value: str = field(repr=False)
    site_id: str = ""

    def __str__(self) -> str:
        return f"LoginCode(site_id={self.site_id!r})"


def parse_login_code(body: Any) -> LoginCode:
    body = _obj(body, {"code", "site_id"})
    code = body["code"]
    if not isinstance(code, str) or LOGIN_CODE.fullmatch(code) is None:
        raise _refuse()
    return LoginCode(code, _app_id(body["site_id"]))


MAX_SIGN_IN_NAME = 60
_EMAIL_IDENTIFIER = re.compile(r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+")


def is_email_identifier(value) -> bool:
    """Slice 5n (O2, r2): a sign-in identifier sent with no password is saved only when it is an
    email address (a mechanical shape: local part, ``@``, a dotted domain) — a one-time code
    never is one, whatever field the page put it in."""
    return isinstance(value, str) and _EMAIL_IDENTIFIER.fullmatch(value) is not None


def parse_sign_in(body: Any) -> str:
    """``POST /v1/runs/{run}/sign-in`` (slice 5n): ``{"site_name"}`` — the site's everyday name,
    a label only (never an address: the host is read from the browser)."""
    body = _obj(body, {"site_name"})
    return _str(body["site_name"], MAX_SIGN_IN_NAME, allow_empty=False)


def parse_claim(body: Any) -> str:
    body = _obj(body, {"caller_label"})
    return _str(body["caller_label"], MAX_CALLER_LABEL)


def parse_search(body: Any) -> str:
    body = _obj(body, {"query"})
    return _str(body["query"], MAX_QUERY, allow_empty=False)


def parse_download(body: Any) -> str:
    body = _obj(body, {"handle"})
    return _app_id(body["handle"])


def parse_archive(body: Any) -> tuple[str, ...]:
    body = _obj(body, {"file_ids"})
    ids = _list(body["file_ids"], 1, MAX_LIST)
    return tuple(_app_id(i) for i in ids)


LIVE_PURPOSES = ("login", "assist")


def parse_live(body: Any) -> str:
    body = _obj(body, {"purpose"})
    return _choice(body["purpose"], LIVE_PURPOSES)


def parse_operator_ref(body: Any) -> str:
    body = _obj(body, {"operator_ref"})
    return _caller_id(body["operator_ref"])


def parse_finish(body: Any) -> ResultManifest:
    body = _obj(body, {"summary_masked", "file_ids", "source_ids", "expectation_status"})
    return ResultManifest(
        summary_masked=_str(body["summary_masked"], MAX_SUMMARY),
        file_ids=tuple(_app_id(i) for i in _list(body["file_ids"], 0, MAX_LIST)),
        source_ids=tuple(_app_id(i) for i in _list(body["source_ids"], 0, MAX_LIST)),
        expectation_status=_choice(body["expectation_status"], EXPECTATION_STATUS),
    )


def parse_cancel(body: Any) -> None:
    _obj(body, set())


#: a request's runs: Casa admits at most eight runs per request (``run_admission.MAX_RUNS``)
MAX_REQUEST_IDS = 8


def parse_request_cancel(body: Any) -> tuple[str, ...]:
    """``{"request_ids": [...]}``: every request id of one stopped request, 1–8, distinct."""
    body = _obj(body, {"request_ids"})
    ids = tuple(_caller_id(i) for i in _list(body["request_ids"], 1, MAX_REQUEST_IDS))
    if len(set(ids)) != len(ids):
        raise _refuse()
    return ids


def parse_delivery(body: Any) -> dict:
    body = _obj(body, {"requester_id", "delivery_id", "outcome"})
    return {"requester_id": _caller_id(body["requester_id"]),
            "delivery_id": _caller_id(body["delivery_id"]),
            "outcome": _choice(body["outcome"], DELIVERY_OUTCOMES)}


_ORIGIN = re.compile(r"https://[a-z0-9.-]{1,253}(?::[0-9]{1,5})?")


def _https_origin(value: Any) -> str:
    """A registered origin in the one spelling request identities use: lowercase host,
    the default port dropped (``https://x:443`` is stored as ``https://x``)."""
    if not isinstance(value, str) or not _ORIGIN.fullmatch(value):
        raise _refuse()
    _, _, port = value[len("https://"):].partition(":")
    if port and not 1 <= int(port) <= 65535:
        raise _refuse()
    return value[:-4] if value.endswith(":443") else value


def parse_site_registration(body: Any) -> dict:
    """``POST /v1/control/sites``: the operator's registration payload."""
    body = _obj(body, {"display_name", "origins", "login_origins", "operator_ref"},
                {"vault_item_id", "start_page"})
    origins = tuple(_https_origin(o) for o in _list(body["origins"], 1, 16))
    login = tuple(_https_origin(o) for o in _list(body["login_origins"], 0, 16))
    item = body.get("vault_item_id")
    if item is not None and not is_app_id(item):
        raise _refuse()
    start = body.get("start_page")
    if start is not None:
        start = _str(start, 2048, allow_empty=False)
        if not start.startswith("https://"):
            raise _refuse()
    return {"display_name": _str(body["display_name"], 80, allow_empty=False),
            "origins": origins, "login_origins": login, "vault_item_id": item,
            "start_page": start, "operator_ref": _caller_id(body["operator_ref"])}
