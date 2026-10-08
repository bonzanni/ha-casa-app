"""One place for what the operator sees from a turn (#1038).

Every turn Casa runs gets one :class:`TurnScope`, minted in
``Agent.handle_message`` and carried on the origin snapshot — through it, into
the pooled SDK client's mutable holder — beside the turn's other live facts.
Model-authored text reaches a transport only as an :class:`Admitted` value, and
the only minter of an ``Admitted`` with ``source="model"`` is
:meth:`TurnScope.admit`: it applies the scope's obligations to the text at the
moment the text is COMMITTED, against the evidence accumulated so far, and
returns what the operator will see plus a record of what changed.

Obligations are a closed, Casa-owned set; the model can never add one.

* :class:`ReadBeforeDescribe` — armed by ``list_inbound_files`` (the tool that
  shows the model inbound paths) or by a ``Read`` attempt on an inbox path; on
  a turn that RECEIVED a file (an S6 file desk turn) armed at the start over
  that file alone, and nothing else widens it (#1218). Discharged when any
  armed file was read successfully or handed to a plugin by
  ``share_inbound_file`` (#1036 ruling, operator 2026-09-22: any-one-read;
  #1218: a hand-off acts on the file). Remedy: a Casa line at the head of every
  emission, or — for a payload STORED for a later turn to send — a resolved
  note carried beside the payload; its count says how the files were armed.
* :class:`InheritedNote` — the resolved note of a payload authored by an
  earlier turn, re-registered on the turn that sends it, so that turn's model
  cannot paraphrase it away. Never discharged.
* :class:`NoStream` — a scheduled turn or an event wake thinks privately and
  delivers only its final text (#534); registered at mint from the message's
  own facts, read by ``handle_message`` as :attr:`TurnScope.streaming_allowed`.
* :class:`DestinationOperatorOnly` — an untrusted webhook turn may notify only
  the operator's Telegram surface (Release A egress binding); registered at
  mint, applied by :meth:`TurnScope.resolve_channel`.
* Closing silence is intrinsic to a final reply: ``admit(FINAL_REPLY, …)`` on
  text that strips to nothing but ``<silent/>`` sentinels returns an empty,
  ``suppressed`` admission — tested on the UNANNOTATED text, so a silent turn is
  never turned into a visible line; prose after a sentinel is delivered whole
  (the G-3 recant contract, INV-TURN-009). A suppressed admission says whether
  the model CHOSE the silence (``chosen_silence``: at least one sentinel) or
  merely produced nothing (#1079). On a buffered (``NoStream``) turn, a final
  reply whose LAST message is a closing ``<silent/>`` after earlier text is the
  operator's ruling on #1075: silent after a confirmed send, else the earlier
  text without the tag (:meth:`TurnScope.admit`).

Every admission of model text the operator is meant to see outside the final
reply (``DISCRETE``, ``CAPTION``, ``KEYBOARD``) also opens an
:class:`OperatorSend` record on the admitting scope, promoted only when the
sender confirms delivery (#1079) — so the turn knows whether everything it
committed to the operator arrived. Every CALL of a send tool also leaves a
:class:`SendAttempt` (#1075), so a send refused before it was ever admitted is
on the record too.

Nothing is held or withheld: the model's words are never suppressed here
(operator ruling, #1036) — beyond closing silence, whose one case that drops
earlier words is the #1075 ruling above, and one more ruled case (#1252): on an
``Agent.handle_message`` turn or a specialist desk turn whose protected call is
waiting on the operator's approval, the words the model wrote after that call
are withheld from the operator, while those written before it are kept
(:class:`ApprovalCut`). The same cut applies to a resident's words after a
synchronous delegate whose specialist's call is waiting (its child's outcome
reaches the resident's scope by delegation id, and is read at that delegate's
result even when the CLI rewrote it, #1274), and to an engagement topic and
a plugin background job, whose driver applies it (#1207). Casa-composed text
enters through :func:`casa_text`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class IntentKind(Enum):
    """The closed set of operator-visible effects model text can take."""

    STREAM_UPDATE = "stream_update"   # a streamed cumulative (agent._emit)
    FINAL_REPLY = "final_reply"       # the turn's final text (handle_message)
    DISCRETE = "discrete"             # a message a tool asked Casa to send
    KEYBOARD = "keyboard"             # a question body with options
    CAPTION = "caption"               # a media caption
    STORED = "stored"                 # text stored for a LATER turn to send


class Admitted(str):
    """Text that passed admission — the only thing the channel's model-text
    methods accept. A ``str`` subclass, so everything downstream that renders,
    splits or logs text keeps working, while ``isinstance(x, Admitted)`` is
    what the channel checks; any string operation yields a plain ``str``,
    which is right: derived text is not admitted text.

    ``annotations`` names every line Casa added, verbatim, so a tool result
    can tell the model what the operator actually saw. For
    :attr:`IntentKind.STORED` the text is unchanged and ``note`` carries the
    resolved line the emitting turn will prepend."""

    scope_id: str
    kind: IntentKind | None
    annotations: tuple[str, ...]
    source: str
    note: str
    suppressed: bool
    chosen_silence: bool
    send: "OperatorSend | None"

    def __new__(cls, text: str, *, scope_id: str, kind: IntentKind | None,
                annotations: tuple[str, ...] = (), source: str = "model",
                note: str = "", suppressed: bool = False,
                chosen_silence: bool = False,
                send: "OperatorSend | None" = None) -> "Admitted":
        obj = str.__new__(cls, text)
        obj.scope_id = scope_id
        obj.kind = kind
        obj.annotations = tuple(annotations)
        obj.source = source
        obj.note = note
        obj.suppressed = suppressed
        obj.chosen_silence = chosen_silence
        obj.send = send
        return obj

    @property
    def text(self) -> str:
        return str(self)

    def with_text(self, text: str) -> "Admitted":
        """The same admission over a re-rendered body — used by the one Casa
        prepend that happens after admission (the plugin-health notice)."""
        return Admitted(text, scope_id=self.scope_id, kind=self.kind,
                        annotations=self.annotations, source=self.source,
                        note=self.note, suppressed=self.suppressed,
                        chosen_silence=self.chosen_silence, send=self.send)

    def mark_delivered(self) -> None:
        """#1079: the sender's confirmation that this text reached the
        operator — called only on the sender's existing positive evidence.
        A no-op for text that opened no record (Casa text, a final reply)."""
        if self.send is not None:
            self.send.delivered = True

    def __repr__(self) -> str:  # pragma: no cover — logging aid
        return (f"Admitted({str.__repr__(self)}, scope_id={self.scope_id!r}, "
                f"annotations={self.annotations!r}, source={self.source!r})")


# ---------------------------------------------------------------------------
# Closing silence — the predicates, shared with agent.py (the #650 resume-
# health classification and the #666 stream hold import them from here)
# ---------------------------------------------------------------------------

SILENCE_SENTINEL = "<silent/>"


def strips_to_silence(text: str | None) -> bool:
    """True when a turn's entire output is silence: empty/whitespace, or
    nothing but one-or-more literal ``<silent/>`` sentinels and whitespace.
    Strict, exact-match-after-strip: residual PROSE after a sentinel is NOT
    silence (the G-3 recant contract)."""
    if not text:
        return True
    stripped = text.strip()
    return not stripped or not stripped.replace(SILENCE_SENTINEL, "").strip()


def closing_silence_prefix(text: str | None, messages: Any) -> str | None:
    """#1075: when the LAST of *messages* — the text-bearing messages the final
    reply *text* was joined from — strips to ``<silent/>`` (silence with at
    least one sentinel in it), return the messages before the trailing run of
    messages that each strip to silence, joined exactly as the reply joins
    them. ``None`` when the rule does not apply: no per-message fact, a fact
    that is not THIS text's, a last message with prose in it (the G-3 recant
    contract), a last message with no sentinel — a whitespace-only one
    included, whatever sentinel precedes it — or no earlier text. Earlier
    messages are never inspected, so a mid-turn recant stays verbatim."""
    if (not isinstance(messages, (list, tuple)) or len(messages) < 2
            or not all(isinstance(m, str) for m in messages)
            or "\n\n".join(messages) != text):
        return None
    last = messages[-1]
    if SILENCE_SENTINEL not in last or not strips_to_silence(last):
        return None
    k = len(messages)
    while k > 0 and strips_to_silence(messages[k - 1]):
        k -= 1
    if k == 0:
        return None
    return "\n\n".join(messages[:k])


def may_still_be_silence(text: str | None) -> bool:
    """True when *text* is silence today, or could still BECOME silence as more
    deltas arrive: what remains after consuming leading complete sentinels and
    whitespace is a prefix of one more sentinel (#666). Strictly weaker than
    :func:`strips_to_silence`; used on the partial-delta path only. The
    incomplete prefix must be the SUFFIX: ``"<sil<silent/>"`` releases."""
    if strips_to_silence(text):
        return True
    rest = (text or "").lstrip()
    while rest.startswith(SILENCE_SENTINEL):
        rest = rest[len(SILENCE_SENTINEL):].lstrip()
    return SILENCE_SENTINEL.startswith(rest)


def without_sentinels(text: str) -> str:
    """#1342: *text* without a run of ``<silent/>`` markers at its start or at
    its end — how a desk reply that holds words as well as the marker is
    shown: the tag never reaches the operator, the words still do. Only the
    markers and the whitespace between them and the words go; nothing else is
    touched (no other whitespace, no line endings, no indentation), and a
    marker anywhere else — between words, in a code span, a link, a quoting
    sentence, or escaped as ``\\<silent/>`` — is the specialist's content and
    stays. Text with no sentinel is returned unchanged."""
    if SILENCE_SENTINEL not in text:
        return text
    tag = re.escape(SILENCE_SENTINEL)
    text = re.sub(rf"(?:\s*(?<!\\){tag})+\s*\Z", "", text)
    return re.sub(rf"\A(?:\s*{tag})+(?:(?:[^\S\r\n]*\r?\n)+|[^\S\r\n]*)", "", text)


class ApprovalCut:
    """#1252: where one attempt's operator-visible words stop because a
    protected call is waiting on the operator's approval.

    The authorization hook records, per call, whether it left an approval
    pending (a POSTED or PENDING deny) or consumed a grant
    (:meth:`TurnScope.note_approval`). A record is read only when its call's
    ``ToolResultBlock`` reaches the consumer, in stream order — never when the
    hook ran, which the SDK does in a task of its own, possibly before the
    consumer has folded the text written before the call. *folded* is the
    number of text-bearing messages folded when that result arrived: the words
    kept are the messages before it. A consume of the same grant, read at its
    own call's result, clears that grant's cut; with another grant still
    pending, the cut moves to that grant's deny."""

    def __init__(self) -> None:
        self.pending: dict[Any, int] = {}

    def observe(self, tool_use_id: Any, approvals: dict, folded: int) -> None:
        record = approvals.get(tool_use_id) if tool_use_id else None
        if record is None:
            return
        kind, key = record
        if kind == "pending":
            self.pending.setdefault(key, folded)
        elif kind == "consumed":
            self.pending.pop(key, None)

    @property
    def cut(self) -> int | None:
        return min(self.pending.values()) if self.pending else None

    @property
    def pending_keys(self) -> tuple:
        """The grants whose cut still stands, in the order first observed —
        what a delegated run publishes for its launcher (#1207)."""
        return tuple(self.pending)


class UnadmittedText(RuntimeError):
    """A channel method whose failure contract is to raise (``send_media``)
    was handed a bare string where an ``Admitted`` was required. Distinct
    from the channel-unavailable ``RuntimeError`` so the tool reports
    ``refused``, never "the channel was down"."""


def current_turn_scope() -> "TurnScope | None":
    """The scope of the running turn, off the origin snapshot — for code that
    runs INSIDE a turn without a tool handler's own snapshot (hook callbacks,
    the authz challenge post). ``None`` when no turn is bound."""
    import agent as agent_mod
    origin = agent_mod.origin_var.get(None) or {}
    scope = origin.get("turn_scope")
    return scope if isinstance(scope, TurnScope) else None


def resolve_scope(origin: "dict | None" = None) -> "TurnScope | None":
    """The scope an emission commits under (design §3.4), for every caller —
    the tool handlers and the authorization challenge alike: a bound
    engagement's scope first, minted from its persisted record (a resumed
    engagement, or a specialist engagement whose brief carried a note, has no
    ambient turn); else the turn scope on *origin* — a handler's ENTRY
    snapshot when it has one, the current holder otherwise; else ``None``.
    ``tools`` is imported lazily because it imports this module."""
    try:
        import tools as tools_mod
        eng = tools_mod.engagement_var.get(None)
    except Exception:  # noqa: BLE001 — no tools module (a bare unit test): no engagement
        eng, tools_mod = None, None
    if eng is not None:
        eng_origin = dict(getattr(eng, "origin", None) or {})
        role = str(eng_origin.get("role") or "")
        try:
            name = tools_mod._display_name_for_role(role)
        except Exception:  # noqa: BLE001 — an unknown role keeps its id as its name
            name = role or "Casa"
        return TurnScope.for_engagement(eng, display_name=name)
    if origin is None:
        import agent as agent_mod
        origin = agent_mod.origin_var.get(None) or {}
    scope = origin.get("turn_scope")
    return scope if isinstance(scope, TurnScope) else None


def casa_text(text: str) -> Admitted:
    """Casa-composed text: a template with controlled inputs. Recorded call
    sites only (see tests/test_output_boundary.py) — a template that
    interpolates model-supplied text is model text and goes through
    :meth:`TurnScope.admit` instead."""
    return Admitted(text=text, scope_id="", kind=None, source="casa")


# ---------------------------------------------------------------------------
# What the turn committed to the operator (#1079)
# ---------------------------------------------------------------------------

@dataclass
class OperatorSend:
    """One piece of model-authored content committed to the operator outside
    the final reply. Opened undelivered; only the sender's confirmation
    promotes it, so an exit the sender never confirmed — a refusal, an unknown
    outcome, a raise, a cancel — stays undelivered."""

    intent: str
    delivered: bool = False


_SENT_INTENTS = frozenset({IntentKind.DISCRETE, IntentKind.CAPTION,
                           IntentKind.KEYBOARD})


@dataclass
class SendAttempt:
    """One call of a send tool (#1075), opened when the call begins and
    resolved when it returns: ``ok`` for a normal result, ``failed`` for an
    error result or a raise. It records the calls an :class:`OperatorSend`
    cannot — a send refused BEFORE admission opens no commitment at all — and
    one still ``open`` is a send whose outcome is not known yet."""

    tool: str
    state: str = "open"


# ---------------------------------------------------------------------------
# Obligations
# ---------------------------------------------------------------------------

class Obligation:
    """Base of the closed set. Registered only by Casa code."""


@dataclass
class ReadBeforeDescribe(Obligation):
    """The turn was shown these inbound files (``(path, display_name)``) and
    must open one before what it says about them is delivered bare.

    ``source`` is how the given files were armed — ``"own"`` (the turn received
    them), ``"listed"`` (``list_inbound_files`` showed them) or ``"tried"`` (a
    ``Read`` attempt on an inbox path the turn never listed) — and ``sources``
    keeps it per file across merges, so the line's count says what it counts
    (#1218)."""

    files: tuple[tuple[str, str], ...]
    source: str = "listed"
    sources: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for path, _ in self.files:
            self.sources.setdefault(path, self.source)


@dataclass(frozen=True)
class InheritedNote(Obligation):
    """A resolved line owed by a payload this turn did not author."""

    text: str


@dataclass(frozen=True)
class NoStream(Obligation):
    """The turn thinks privately: no token callback, only the final text is
    delivered (#534 event wakes; scheduled turns). ``reason`` is the fact it
    was registered from — ``"scheduled"`` or ``"event_wake"``."""

    reason: str


@dataclass(frozen=True)
class DestinationOperatorOnly(Obligation):
    """An untrusted webhook turn may notify only the operator's Telegram
    surface: the caller-selected channel of a discrete send is replaced, never
    honoured (Release A / Layer 1 egress binding, confused-deputy containment).
    Trusted ``/invoke`` turns and every non-webhook origin are unaffected."""


@dataclass
class Evidence:
    read_ok: set[str] = field(default_factory=set)
    read_failed: set[str] = field(default_factory=set)
    # #1218: inbox files the turn handed to a plugin (``share_inbound_file``
    # published the copy) — acting on a file, as a read is
    handed_off: set[str] = field(default_factory=set)


def _norm(path: str) -> str:
    # The same normalisation the PreToolUse ``path_scope`` check applies —
    # lexical, no OS calls. Imported lazily: ``hooks`` is a large module and
    # ``agent`` imports this one.
    from hooks import _normalize_path
    return _normalize_path(path)


# ---------------------------------------------------------------------------
# The scope
# ---------------------------------------------------------------------------

@dataclass
class TurnScope:
    id: str
    cid: str
    role: str
    display_name: str
    channel: str
    message_type: str
    markers: dict[str, Any] = field(default_factory=dict)
    obligations: list[Obligation] = field(default_factory=list)
    evidence: Evidence = field(default_factory=Evidence)
    annotations_applied: list[str] = field(default_factory=list)
    operator_sends: list[OperatorSend] = field(default_factory=list)
    send_attempts: list[SendAttempt] = field(default_factory=list)
    # #1252: the authorization hook's per-call record (tool_use_id →
    # ("pending" | "consumed", GrantKey)), read at the call's result by an
    # ApprovalCut. Never shared with a child scope.
    approvals: dict[str, tuple[str, Any]] = field(default_factory=dict)
    # #1252: the words a desk run kept, published by the delegated runner
    # when its run ended with an approval pending; None otherwise.
    approval_kept: str | None = None
    # #1207: the grants a delegated run left waiting on the operator (its
    # ApprovalCut's standing keys), published by the runner whatever the run's
    # outcome.
    approval_waiting: tuple = ()
    # #1207: a synchronous delegate's approval outcome, by delegation id —
    # ("consumed" | "pending", GrantKey) records, consumes first — copied here
    # by the handler before it returns, and read only by this turn's fold at
    # that delegate call's result.
    delegated_approvals: dict[str, tuple] = field(default_factory=dict)

    # -- minting ------------------------------------------------------------

    @classmethod
    def mint(cls, msg: Any, config: Any) -> "TurnScope":
        """One scope per dispatched turn, from the bus message and the agent
        config. The reserved provenance markers present on the context ride
        along; an ``_inherited_note`` marker registers the note it carries."""
        from provenance import RESERVED_CONTEXT_KEYS
        ctx = dict(getattr(msg, "context", None) or {})
        markers = {k: ctx[k] for k in RESERVED_CONTEXT_KEYS if k in ctx}
        scope = cls(
            id=str(getattr(msg, "id", "") or ""),
            cid=str(ctx.get("cid") or "-"),
            role=str(getattr(config, "role", "") or ""),
            display_name=_display_name(config),
            channel=str(getattr(msg, "channel", "") or ""),
            message_type=str(getattr(getattr(msg, "type", None), "value", "") or ""),
            markers=markers,
        )
        note = ctx.get("_inherited_note")
        if isinstance(note, str) and note.strip():
            scope.arm(InheritedNote(note))
        # The per-turn output decisions that used to be inline checks (R2):
        # registered here, from the same facts those checks read.
        if scope.message_type == "scheduled":
            scope.arm(NoStream("scheduled"))
        elif markers.get("synthetic") == "event_wake":
            scope.arm(NoStream("event_wake"))
        if scope.channel == "webhook" and markers.get("_origin_route") != "invoke":
            scope.arm(DestinationOperatorOnly())
        return scope

    @classmethod
    def for_child(cls, parent: "TurnScope", note: str, *,
                  synchronous: bool = True) -> "TurnScope":
        """The view a delegated child runs under: the launcher's identity and
        markers, plus the launch-time note — never the parent's LIVE
        obligations, which a parent ``Read`` after launch would discharge
        although the child's brief was written unread.

        #1079: a SYNCHRONOUS child also shares the launcher's record of what
        was committed to the operator, since the launcher waits for it inside
        its own turn. An async child does not: it runs past the launcher's
        turn, and its result comes back as its own announcement."""
        child = cls(
            id=parent.id, cid=parent.cid, role=parent.role,
            display_name=parent.display_name, channel=parent.channel,
            message_type=parent.message_type, markers=dict(parent.markers),
            # #1079: the SAME list, not a copy — what a synchronous delegate
            # commits to the operator is part of what the launching turn did.
            operator_sends=parent.operator_sends if synchronous else [],
            # #1075: and so is every send it TRIED, refused ones included.
            send_attempts=parent.send_attempts if synchronous else [],
        )
        if note.strip():
            child.arm(InheritedNote(note))
        return child

    @classmethod
    def for_desk(cls, origin: dict, *, display_name: str) -> "TurnScope":
        """S4 §5.2: the scope a specialist desk turn runs under — a fresh
        scope with the DM's reserved markers and no inherited obligations
        (there is no launcher turn whose brief could be unread); the
        specialist's display name, the resident's role (the desk acts on the
        resident's behalf in the resident's chat)."""
        from provenance import RESERVED_CONTEXT_KEYS
        ctx = dict(origin or {})
        return cls(
            id=str(ctx.get("_delegation_id") or ""),
            cid=str(ctx.get("cid") or "-"),
            role=str(ctx.get("role") or ""),
            display_name=display_name,
            channel=str(ctx.get("channel") or ""),
            message_type=str(ctx.get("message_type") or ""),
            markers={k: ctx[k] for k in RESERVED_CONTEXT_KEYS if k in ctx},
        )

    @classmethod
    def for_engagement(cls, eng: Any, *, display_name: str) -> "TurnScope":
        """An engagement's scope, minted on demand from its record (the origin
        persisted with live objects stripped): the persisted note, if any, and
        nothing else — launch, resume and boot recovery all see the same."""
        origin = dict(getattr(eng, "origin", None) or {})
        scope = cls(
            id=str(getattr(eng, "id", "") or ""),
            cid=str(origin.get("cid") or "-"),
            role=str(origin.get("role") or ""),
            display_name=display_name,
            channel=str(origin.get("channel") or ""),
            message_type="engagement",
            markers={},
        )
        note = origin.get("_inherited_note")
        if isinstance(note, str) and note.strip():
            scope.arm(InheritedNote(note))
        return scope

    # -- obligations and evidence -------------------------------------------

    def arm(self, obligation: Obligation) -> None:
        if isinstance(obligation, ReadBeforeDescribe):
            current = self._read_before_describe()
            if current is not None:
                own = "own" in current.sources.values()
                if obligation.source == "own" and not own:
                    # #1218: the turn's own files ARE its obligation; what a
                    # listing armed before them is not
                    current.files, current.sources = (), {}
                elif own and obligation.source != "own":
                    return              # nothing widens a turn's own files
                seen = {_norm(p) for p, _ in current.files}
                for f in obligation.files:
                    if _norm(f[0]) not in seen:
                        seen.add(_norm(f[0]))
                        current.files = current.files + (f,)
                        current.sources[f[0]] = obligation.source
                return
        self.obligations.append(obligation)

    def receive_files(self, files: tuple[tuple[str, str], ...]) -> None:
        """#1218: the files this turn RECEIVED (an S6 file desk turn), armed
        before the turn runs: the obligation covers them alone."""
        self.arm(ReadBeforeDescribe(
            files=tuple((_norm(p), n) for p, n in files), source="own"))

    def note_read_ok(self, path: str) -> None:
        self.evidence.read_ok.add(_norm(path))

    def note_handed_off(self, path: str) -> None:
        """#1218: ``share_inbound_file`` published a copy of this inbox file
        for a plugin — the turn acted on it."""
        self.evidence.handed_off.add(_norm(path))

    def note_read_failed(self, path: str) -> None:
        self.evidence.read_failed.add(_norm(path))

    def note_read_attempt(self, path: str, *, display_name: str) -> None:
        """A ``Read`` aimed at an inbox file the turn never listed (a path
        copied from an earlier listing) makes this a file turn too."""
        self.arm(ReadBeforeDescribe(files=((_norm(path), display_name),),
                                    source="tried"))

    @property
    def streaming_allowed(self) -> bool:
        """False when a :class:`NoStream` obligation is registered."""
        return not any(isinstance(ob, NoStream) for ob in self.obligations)

    def open_send(self, intent: str) -> OperatorSend:
        """#1079: record a commitment to the operator that carries no admitted
        text (a caption-less ``send_media``). Admissions of the sent intents
        record their own."""
        record = OperatorSend(intent)
        self.operator_sends.append(record)
        return record

    @property
    def operator_sends_delivered(self) -> bool:
        """True when every commitment recorded on this scope was confirmed
        delivered — vacuously True when there was none."""
        return all(r.delivered for r in self.operator_sends)

    def note_approval(self, tool_use_id: Any, kind: str, key: Any) -> None:
        """#1252: what the authorization hook decided for one call —
        ``"pending"`` (it left an approval keyboard pending) or
        ``"consumed"`` (it spent the operator's grant)."""
        if tool_use_id:
            self.approvals[str(tool_use_id)] = (kind, key)

    def open_attempt(self, tool: str) -> SendAttempt:
        """#1075: record one send-tool call as it begins (see
        ``tools._account_send_attempts``)."""
        attempt = SendAttempt(tool)
        self.send_attempts.append(attempt)
        return attempt

    @property
    def closing_silence_earned(self) -> bool:
        """#1075 rule 1, the record half: at least one commitment, every one
        confirmed delivered, and every send-tool call resolved ``ok`` — none
        refused, failed or still in flight. Never vacuous, unlike
        :attr:`operator_sends_delivered`: a turn with no send has earned
        nothing."""
        return (bool(self.operator_sends)
                and self.operator_sends_delivered
                and all(a.state == "ok" for a in self.send_attempts))

    @property
    def delivers_to_operator(self) -> bool:
        """#1142: this turn is a plugin webhook-trigger fire whose route
        declared ``deliver: operator`` — its final reply goes to the operator's
        Telegram, and ``send_message`` is neither offered nor honoured. The ONE
        predicate every reader uses (final delivery, the restricted-options
        builder, the ``send_message`` handler), read from markers registered at
        mint from server-stamped, reserved context keys."""
        return (self.channel == "webhook"
                and self.markers.get("_origin_route") == "webhook_trigger"
                and self.markers.get("_webhook_deliver") in (
                    "operator", "operator_always"))

    @property
    def silence_forbidden(self) -> bool:
        """#1158: a ``deliver: operator_always`` fire — every accepted fire
        ends in exactly one operator message, so a reply that is silent anyway
        is replaced by Casa's fallback line, not suppressed into nothing."""
        return (self.delivers_to_operator
                and self.markers.get("_webhook_deliver") == "operator_always")

    def resolve_channel(self, requested: str) -> str:
        """The channel a discrete send actually goes to: the requested one,
        unless :class:`DestinationOperatorOnly` binds it to Telegram."""
        if any(isinstance(ob, DestinationOperatorOnly) for ob in self.obligations):
            return "telegram"
        return requested

    def _read_before_describe(self) -> ReadBeforeDescribe | None:
        for ob in self.obligations:
            if isinstance(ob, ReadBeforeDescribe):
                return ob
        return None

    def _undischarged_files(self) -> tuple[tuple[str, str], ...]:
        rbd = self._read_before_describe()
        if rbd is None or not rbd.files:
            return ()
        acted = self.evidence.read_ok | self.evidence.handed_off
        if any(_norm(p) in acted for p, _ in rbd.files):
            return ()
        return rbd.files

    # -- admission ----------------------------------------------------------

    def admit(self, kind: IntentKind, text: str, *,
              report: dict[str, Any] | None = None) -> Admitted:
        """Apply every obligation to *text* at this moment. Empty text is never
        annotated. Order: silence (final replies only, on the UNANNOTATED
        text), then inherited notes, then today's line.

        *report* is the turn report ``_process`` filled (final replies only):
        on a buffered turn it carries the facts the #1075 closing-silence rule
        reads — the winning attempt's text-bearing messages, the attempt count
        and the consumed retries. Without it, today's whole-text rule.
        On any turn, the report's ``approval_cut`` (#1252) — the winning
        attempt's count of text-bearing messages folded before a call whose
        approval is still pending — keeps only those messages."""
        if kind is IntentKind.FINAL_REPLY and strips_to_silence(text):
            return Admitted(text="", scope_id=self.id, kind=kind, suppressed=True,
                            chosen_silence=SILENCE_SENTINEL in (text or ""))
        cut = report.get("approval_cut") if report is not None else None
        if kind is IntentKind.FINAL_REPLY and isinstance(cut, int):
            # #1252 (operator ruling): the operator sees the approval keyboard
            # and nothing the model wrote after the protected call. The stream
            # already stopped at the same point (agent._make_on_message).
            messages = report.get("reply_messages")
            if (isinstance(messages, (list, tuple))
                    and all(isinstance(m, str) for m in messages)
                    and "\n\n".join(messages) == text
                    and 0 <= cut <= len(messages)):
                text = "\n\n".join(messages[:cut])
            else:
                logger.warning(
                    "approval cut without this reply's messages: role=%s "
                    "cid=%s — reply withheld", self.role, self.cid)
                text = ""
            if strips_to_silence(text):
                return Admitted(text="", scope_id=self.id, kind=kind,
                                suppressed=True)
        if (kind is IntentKind.FINAL_REPLY and report is not None
                and not self.streaming_allowed):
            kept = closing_silence_prefix(text, report.get("reply_messages"))
            if kept is not None:
                # #1075 (operator ruling): the LAST message is a closing
                # sentinel after earlier text. Rule 1 — the same conditions as
                # the #1079 acknowledgement (no error: only an error-free reply
                # is admitted here; no retries, and exactly one attempt, so
                # every record below is the winning attempt's), plus at least
                # one send — makes the turn silent. Not a CHOSEN silence: that
                # stays a final text of nothing but sentinels (INV-JOB-010).
                if (report.get("retries") == []
                        and report.get("attempts") == 1
                        and self.closing_silence_earned):
                    logger.info(
                        "closing silence after confirmed sends: role=%s "
                        "cid=%s — earlier text dropped", self.role, self.cid)
                    return Admitted(text="", scope_id=self.id, kind=kind,
                                    suppressed=True)
                # Rule 2: keep the earlier text, drop the closing tag.
                logger.info(
                    "closing silence without confirmed sends: role=%s cid=%s "
                    "— earlier text kept, tag dropped", self.role, self.cid)
                text = kept
        send = None
        if kind in _SENT_INTENTS:
            send = self.open_send(kind.value)
        if not (text or "").strip():
            return Admitted(text=text, scope_id=self.id, kind=kind, send=send)
        head: list[str] = [ob.text for ob in self.obligations
                           if isinstance(ob, InheritedNote)]
        stored_note: list[str] = list(head)
        unread = self._undischarged_files()
        if unread:
            sources = self._read_before_describe().sources
            head.append(self._answered_line(unread, sources))
            stored_note.append(self._wrote_line(unread, sources))
        if kind is IntentKind.STORED:
            note = "\n\n".join(stored_note)
            return Admitted(text=text, scope_id=self.id, kind=kind,
                            annotations=tuple(stored_note), note=note)
        if not head:
            return Admitted(text=text, scope_id=self.id, kind=kind, send=send)
        self.annotations_applied.extend(head)
        return Admitted(text="\n\n".join(head) + "\n\n" + text, scope_id=self.id,
                        kind=kind, annotations=tuple(head), send=send)

    # -- wording ------------------------------------------------------------

    def _persona(self) -> str:
        """The name the line calls the agent by: the persona name when it is
        within ``_NAME_MAX``, else the role — the authorization challenge
        headline's own rule — so the line is bounded whatever an operator
        configured; a head past the platform limit is what stalled the
        overflow splitter in plan round 2."""
        name = self.display_name
        if name and len(name) <= _NAME_MAX:
            return name
        return (self.role or "Casa")[:_NAME_MAX]

    def _answered_line(self, unread: tuple[tuple[str, str], ...],
                       sources: dict[str, str]) -> str:
        persona = self._persona()
        which, own = _which(unread, sources, persona)
        # "in this turn" only where it is true of the files: one named file
        # (it qualifies the answering) or files the turn received (#1218)
        when = " in this turn" if len(unread) == 1 or own else ""
        return f"Casa: {persona} answered without opening {which}{when}."

    def _wrote_line(self, unread: tuple[tuple[str, str], ...],
                    sources: dict[str, str]) -> str:
        persona = self._persona()
        which, _own = _which(unread, sources, persona)
        return f"Casa: {persona} wrote this without opening {which}."


# The persona name a disclosure line may carry — `authz_grants._DISPLAY_NAME_MAX`,
# the bound the challenge headline applies. A file's display name is bounded
# where the inbox stores it (`agent_inbox._bounded_display`).
_NAME_MAX = 64


# #1218: how a count of several files is worded, by how they were armed —
# after the persona's name, never a pronoun: Casa does not know one (#1247)
_HOW = {frozenset({"listed"}): "listed",
        frozenset({"tried"}): "tried to open",
        frozenset({"listed", "tried"}): "listed or tried to open"}


def _which(unread: tuple[tuple[str, str], ...],
           sources: dict[str, str], persona: str) -> tuple[str, bool]:
    """The files the line names, and whether they are the turn's own. Only a
    turn's own files are ones "you sent"; one file is named whatever its
    source; several that are not the turn's own are the files *persona*
    listed or tried to open."""
    kinds = frozenset(sources.get(p, "listed") for p, _ in unread)
    own = kinds == {"own"}
    if len(unread) == 1:
        return f"“{unread[0][1]}”", own
    if own:
        return f"any of the {len(unread)} files you sent", True
    return f"any of the {len(unread)} files {persona} {_HOW[kinds - {'own'}]}", False


def _display_name(config: Any) -> str:
    character = getattr(config, "character", None)
    name = getattr(character, "name", None)
    return str(name) if name else str(getattr(config, "role", "") or "Casa")
