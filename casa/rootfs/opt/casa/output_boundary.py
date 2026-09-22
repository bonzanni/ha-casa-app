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
  shows the model inbound paths) or by a ``Read`` attempt on an inbox path;
  discharged when any listed file was read successfully (#1036 ruling, operator
  2026-09-22: any-one-read). Remedy: a Casa line at the head of every emission,
  or — for a payload STORED for a later turn to send — a resolved note carried
  beside the payload.
* :class:`InheritedNote` — the resolved note of a payload authored by an
  earlier turn, re-registered on the turn that sends it, so that turn's model
  cannot paraphrase it away. Never discharged.

Nothing is held or withheld: the model's words are never suppressed here
(operator ruling, #1036). Casa-composed text enters through :func:`casa_text`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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

    def __new__(cls, text: str, *, scope_id: str, kind: IntentKind | None,
                annotations: tuple[str, ...] = (), source: str = "model",
                note: str = "") -> "Admitted":
        obj = str.__new__(cls, text)
        obj.scope_id = scope_id
        obj.kind = kind
        obj.annotations = tuple(annotations)
        obj.source = source
        obj.note = note
        return obj

    @property
    def text(self) -> str:
        return str(self)

    def with_text(self, text: str) -> "Admitted":
        """The same admission over a re-rendered body — used by the one Casa
        prepend that happens after admission (the plugin-health notice)."""
        return Admitted(text, scope_id=self.scope_id, kind=self.kind,
                        annotations=self.annotations, source=self.source,
                        note=self.note)

    def __repr__(self) -> str:  # pragma: no cover — logging aid
        return (f"Admitted({str.__repr__(self)}, scope_id={self.scope_id!r}, "
                f"annotations={self.annotations!r}, source={self.source!r})")


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
# Obligations
# ---------------------------------------------------------------------------

class Obligation:
    """Base of the closed set. Registered only by Casa code."""


@dataclass
class ReadBeforeDescribe(Obligation):
    """The turn was shown these inbound files (``(path, display_name)``) and
    must open one before what it says about them is delivered bare."""

    files: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class InheritedNote(Obligation):
    """A resolved line owed by a payload this turn did not author."""

    text: str


@dataclass
class Evidence:
    read_ok: set[str] = field(default_factory=set)
    read_failed: set[str] = field(default_factory=set)


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
        return scope

    @classmethod
    def for_child(cls, parent: "TurnScope", note: str) -> "TurnScope":
        """The view a delegated child runs under: the launcher's identity and
        markers, plus the launch-time note — never the parent's LIVE
        obligations, which a parent ``Read`` after launch would discharge
        although the child's brief was written unread."""
        child = cls(
            id=parent.id, cid=parent.cid, role=parent.role,
            display_name=parent.display_name, channel=parent.channel,
            message_type=parent.message_type, markers=dict(parent.markers),
        )
        if note.strip():
            child.arm(InheritedNote(note))
        return child

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
                seen = {p for p, _ in current.files}
                current.files = current.files + tuple(
                    f for f in obligation.files if f[0] not in seen)
                return
        self.obligations.append(obligation)

    def note_read_ok(self, path: str) -> None:
        self.evidence.read_ok.add(_norm(path))

    def note_read_failed(self, path: str) -> None:
        self.evidence.read_failed.add(_norm(path))

    def note_read_attempt(self, path: str, *, display_name: str) -> None:
        """A ``Read`` aimed at an inbox file the turn never listed (a path
        copied from an earlier listing) makes this a file turn too."""
        self.arm(ReadBeforeDescribe(files=((_norm(path), display_name),)))

    def _read_before_describe(self) -> ReadBeforeDescribe | None:
        for ob in self.obligations:
            if isinstance(ob, ReadBeforeDescribe):
                return ob
        return None

    def _undischarged_files(self) -> tuple[tuple[str, str], ...]:
        rbd = self._read_before_describe()
        if rbd is None or not rbd.files:
            return ()
        if any(_norm(p) in self.evidence.read_ok for p, _ in rbd.files):
            return ()
        return rbd.files

    # -- admission ----------------------------------------------------------

    def admit(self, kind: IntentKind, text: str) -> Admitted:
        """Apply every obligation to *text* at this moment. Empty text is never
        annotated. Order: inherited notes first, then today's line."""
        if not (text or "").strip():
            return Admitted(text=text, scope_id=self.id, kind=kind)
        head: list[str] = [ob.text for ob in self.obligations
                           if isinstance(ob, InheritedNote)]
        stored_note: list[str] = list(head)
        unread = self._undischarged_files()
        if unread:
            head.append(self._answered_line(unread))
            stored_note.append(self._wrote_line(unread))
        if kind is IntentKind.STORED:
            note = "\n\n".join(stored_note)
            return Admitted(text=text, scope_id=self.id, kind=kind,
                            annotations=tuple(stored_note), note=note)
        if not head:
            return Admitted(text=text, scope_id=self.id, kind=kind)
        self.annotations_applied.extend(head)
        return Admitted(text="\n\n".join(head) + "\n\n" + text, scope_id=self.id,
                        kind=kind, annotations=tuple(head))

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

    def _answered_line(self, unread: tuple[tuple[str, str], ...]) -> str:
        return (f"Casa: {self._persona()} answered without opening "
                f"{_which(unread)} in this turn.")

    def _wrote_line(self, unread: tuple[tuple[str, str], ...]) -> str:
        return (f"Casa: {self._persona()} wrote this without opening "
                f"{_which(unread)}.")


# The persona name a disclosure line may carry — `authz_grants._DISPLAY_NAME_MAX`,
# the bound the challenge headline applies. A file's display name is bounded
# where the inbox stores it (`agent_inbox._bounded_display`).
_NAME_MAX = 64


def _which(unread: tuple[tuple[str, str], ...]) -> str:
    if len(unread) == 1:
        return f"“{unread[0][1]}”"
    return f"any of the {len(unread)} files you sent"


def _display_name(config: Any) -> str:
    character = getattr(config, "character", None)
    name = getattr(character, "name", None)
    return str(name) if name else str(getattr(config, "role", "") or "Casa")
