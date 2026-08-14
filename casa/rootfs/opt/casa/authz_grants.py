"""Canonical argument JSON + the single-use, TTL-bound authorization
GrantStore (A:§3.3, §3.6).

This module is Part 1 of 3 for the ``ask_user`` + authorization-grants
project (v0.76.0). Later tasks EXTEND it — Task 5 adds a
``ChallengeCoordinator`` (async settlement, two latches) and Task 7 adds
``make_resident_authz_hook``/``AuthzDeps`` and ``protected_map``
consumption. Keep this file leaf-level: no imports of ``agent``/``tools``
at module scope, new sections appended below rather than interleaved, and
the ``GRANTS`` singleton kept at the very bottom of its section so later
tasks can import it without reaching into internals.

Sections, top to bottom:

1. Canonical JSON + hash (§3.6) — pure functions used both to compute the
   ``GrantKey.args_hash`` and to render the challenge message body.
2. ``GrantKey`` + ``GrantStore`` (§3.3) — a thread-safe, single-use,
   TTL-bound store of "operator approved this exact tool call" grants.
   ``GRANTS`` is the process-wide singleton.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from text_util import escape_unsafe_text, is_unsafe_text, utf16_len

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical JSON (A:§3.6)
# ---------------------------------------------------------------------------


def canonical_args_json(tool_input: dict) -> str:
    """Render *tool_input* as canonical JSON.

    ``sort_keys=True`` gives a stable key order (recursively, for every
    nested dict), ``separators=(",", ":")`` drops the default whitespace,
    ``ensure_ascii=False`` leaves unicode unescaped, and ``allow_nan=False``
    rejects ``nan``/``inf``/``-inf`` — these cannot be rendered faithfully
    to an operator confirming a tool call, so they must never silently
    round-trip through a hash.

    Raises ``ValueError`` if *tool_input* contains a non-finite float
    (``allow_nan=False`` makes ``json.dumps`` raise ``ValueError`` for
    those) or any value ``json.dumps`` cannot serialize at all (a
    ``TypeError`` is re-raised as ``ValueError`` with a clearer message,
    so callers only need to catch one exception type).
    """
    try:
        return json.dumps(
            tool_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except TypeError as exc:
        raise ValueError(f"tool_input is not JSON-serializable: {exc}") from exc


def canonical_args_hash(tool_input: dict) -> str:
    """sha256 hexdigest of ``canonical_args_json(tool_input)`` (UTF-8)."""
    return hashlib.sha256(
        canonical_args_json(tool_input).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# GrantKey + GrantStore (A:§3.3)
# ---------------------------------------------------------------------------

# Grant TTL default, per the plan's Global Constraints: "grant TTL 300 s
# default; single-use".
DEFAULT_GRANT_TTL_S = 300.0


def normalize_role(target: str) -> str:
    """Strip a plugin target's tier qualifier so it matches
    ``GrantKey.enforcement_role`` (r2-B5): plugin targets are tier-qualified
    (``"specialist:finance"``) while the enforcement role is always plain
    (``"finance"``). Already-plain input (no ``":"``) is returned unchanged.
    Used at EVERY purge/cancel call site — tools.py's plugin-lifecycle
    mutations (plugin_update/plugin_remove/plugin_unassign) and reload.py's
    per-role reload seams — so a tier-qualified target reliably invalidates
    the plain-role grants/challenges it actually governs."""
    if not isinstance(target, str):
        return target
    _, sep, role = target.partition(":")
    return role if sep else target


@dataclass(frozen=True)
class GrantKey:
    """Identifies exactly one operator-approved tool call.

    ``artifact_id`` binds the grant to the resolved plugin artifact at
    challenge time — a plugin update mid-TTL changes the artifact and
    invalidates the grant. ``enforcement_role`` is the plain role (never
    tier-qualified) that is actually allowed to consume it.
    """

    operator_id: int
    chat_id: int
    enforcement_role: str
    artifact_id: str
    tool_name: str
    args_hash: str
    # #400: the engagement this grant was minted inside, or "" for the
    # direct/delegated DM path (which has no engagement). Binding it into the
    # key means a grant approved inside engagement E1 can NEVER be consumed by
    # a DIFFERENT engagement E2 whose (operator, chat, role, artifact, tool,
    # args) happen to match — the approval is call-AND-engagement-bound. The
    # "" default preserves the DM/direct path's key identity byte-for-byte.
    engagement_id: str = ""


@dataclass
class _Grant:
    """Internal record: when it expires, and whether it has been used."""

    expires_at: float
    used: bool = False


class GrantStore:
    """Thread-safe, single-use, TTL-bound store of operator-approved
    tool-call grants.

    A ``threading.Lock`` guards every mutation — not ``asyncio.Lock`` —
    because the SDK PreToolUse hook's thread-context relative to the
    event loop is uncertain (pinned in the plan as a load-bearing
    assumption); a real-thread concurrency test proves ``consume`` is
    genuinely serialized, not just coroutine-safe.

    ``_now`` is an injectable zero-arg clock (default ``time.monotonic``)
    so tests can drive TTL expiry deterministically without sleeping.
    """

    def __init__(self, *, _now: Callable[[], float] = time.monotonic) -> None:
        self._now = _now
        self._lock = threading.Lock()
        self._grants: dict[GrantKey, _Grant] = {}

    def mint(self, key: GrantKey, *, ttl_s: float = DEFAULT_GRANT_TTL_S) -> None:
        """Create a fresh, unused grant for *key*, replacing any live one.

        Minting always wins over whatever was there before — a stale
        unused grant, an already-consumed one, or an expired one are all
        simply overwritten with a new record.
        """
        grant = _Grant(expires_at=self._now() + ttl_s)
        with self._lock:
            self._grants[key] = grant

    def consume(self, key: GrantKey) -> bool:
        """Atomically mark *key* used and return ``True`` — exactly once.

        Returns ``False`` when no grant exists for *key*, it already
        expired, or it was already consumed. The whole check-and-mark
        happens under the lock, so N callers racing the SAME key see
        exactly one ``True``.
        """
        with self._lock:
            grant = self._grants.get(key)
            if grant is None:
                return False
            if grant.used or grant.expires_at <= self._now():
                return False
            grant.used = True
            return True

    def purge_chat(self, chat_id: int) -> int:
        """Drop every grant for *chat_id*. Returns the count removed."""
        return self._purge(lambda k: k.chat_id == chat_id)

    def purge_role(self, role: str) -> int:
        """Drop every grant whose ``enforcement_role`` is *role*."""
        return self._purge(lambda k: k.enforcement_role == role)

    def purge_artifact(self, artifact_id: str) -> int:
        """Drop every grant whose ``artifact_id`` is *artifact_id*."""
        return self._purge(lambda k: k.artifact_id == artifact_id)

    def sweep(self) -> int:
        """Drop every grant whose TTL has passed. Returns the count
        removed. Used grants that have not yet expired are left alone —
        this is a pure TTL sweep, not a used-grant reaper."""
        now = self._now()
        return self._purge(lambda k: self._grants[k].expires_at <= now)

    def _purge(self, predicate: Callable[[GrantKey], bool]) -> int:
        with self._lock:
            matched = [k for k in self._grants if predicate(k)]
            for k in matched:
                del self._grants[k]
            return len(matched)


# Process-wide singleton. Later tasks (the ask_user tool's /new purge, the
# PreToolUse authz hook's consume, plugin/reload lifecycle purges, and the
# casa_core.py hourly sweep job) all import THIS instance.
GRANTS = GrantStore()


# ---------------------------------------------------------------------------
# ChallengeCoordinator (A:§3.4) — Part 2 (Task 5)
# ---------------------------------------------------------------------------
#
# The coordinator is the single owner of the authorization-challenge
# lifecycle: it renders + size-validates the challenge, atomically registers
# ONE broker record per (full) GrantKey, drives the keyboard post on an owned
# background task, installs the authz finish hook, and reaps its own entry
# once BOTH the request future and the setup task have settled. It is
# LOOP-CONFINED — every entry point is called from a hook coroutine on the
# agent's event loop (documented invariant, NOT thread-safe) — so the
# atomic "check the dict, then register + spawn" happens with no await in
# between, and production concurrency is modelled as multiple hook coroutines
# on ONE loop.
#
# This section keeps ``authz_grants.py`` leaf-level: the broker singleton is
# resolved lazily by name (``verdict_broker.BROKER``) so tests can substitute
# it, and the Telegram channel is passed in as a duck-typed ``channel`` object
# (``post_dm_keyboard`` / ``edit_dm_message`` / ``_dispatch_button_continuation``)
# — there is NO import of ``channels`` / ``telegram`` here.

# Challenge message size ceiling (plan Global Constraints): a rendered
# challenge body longer than this is REFUSED rather than posted (Telegram's
# hard message cap is 4096 UTF-16 units; 3900 leaves headroom for the keyboard
# + envelope). #328: measured in UTF-16 units via ``text_util.utf16_len``.
_CHALLENGE_MAX_CHARS = 3900

# Challenge TTL. Was 120 s (plan Global Constraints); raised to 600 s (#498):
# two minutes assumed the operator was watching the chat when the ask landed —
# a missed keyboard forced a fresh chat turn to re-run the delegation and mint
# a new ask. The challenge is DETACHED (no turn blocks on it; approval
# dispatches a continuation / resumes the engagement), so a longer window
# holds nothing open. 600 s gives ask-class parity with trigger/callback
# consent (trigger_consent.TRIGGER_CONSENT_TTL_S) — both are operator
# decisions on a phone, and the asymmetry read as broken. The GRANT minted on
# approval keeps its own 300 s single-use TTL (DEFAULT_GRANT_TTL_S).
_CHALLENGE_TTL_S = 600.0


def short_tool_name(tool_name: str) -> str:
    """The operator-facing short form of *tool_name*: the segment after the
    LAST ``__`` when the name is MCP-namespaced
    (``mcp__plugin_<p>_<s>__<tool>`` -> ``<tool>``), else *tool_name*
    unchanged. Purely syntactic — works for ANY tool name, MCP or not, and
    for names with exactly one ``__`` too. B8 note: this is display-only —
    the FULL name is still carried verbatim elsewhere in the challenge/
    continuation text (see ``render_challenge_message`` / the finish hook).
    """
    if "__" in tool_name:
        return tool_name.rsplit("__", 1)[-1]
    return tool_name


# -- W2 challenge-renderer helpers ------------------------------------------
#
# Display-name render-time guard bound: a ``cfg.character.name`` longer than
# this (or empty / UNSAFE-TEXT) falls back to the role string — a render-time
# fallback, NEVER a validation error (character.yaml is operator-owned config
# and must not brick a reload). 64 is ACCEPTED; 65 falls back (boundary).
_DISPLAY_NAME_MAX = 64

# Interpolated summary-value handling: a STRING value longer than this is
# ellipsized to 77 + '…'; exactly 80 passes untouched (boundary).
_SUMMARY_VALUE_MAX = 80
_SUMMARY_ELLIPSIS_BODY = 77
_SUMMARY_ELLIPSIS = "…"

# A summary placeholder is EXACTLY ``{identifier}`` where identifier matches
# this (Python-identifier-ish); ANY other brace syntax ⇒ fail-safe fallback.
_SUMMARY_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _resolve_display_name(name: "str | None", role: str) -> str:
    """Render-time guard (W2): return *name* when it is a safe, non-empty,
    ≤ ``_DISPLAY_NAME_MAX``-char display name; otherwise fall back to *role*.
    Never raises — the character schema permits arbitrary non-empty strings, so
    the guard runs at render time and a bad name simply degrades to the role."""
    if not name or len(name) > _DISPLAY_NAME_MAX or is_unsafe_text(name):
        return role
    return name


def _render_summary_value(val: Any) -> "str | None":
    """Render ONE canonical-arg value for summary interpolation, or ``None`` to
    signal fallback. SCALARS ONLY (str/int/float/bool). ``bool`` renders
    ``true``/``false`` to match the canonical JSON (never ``True``/``0``);
    numbers render via ``json.dumps`` so they match the binding block exactly.
    STRING values that are UNSAFE-TEXT or brace-bearing ⇒ ``None`` (the
    no-leftover-braces guarantee applies to the FINAL output); > 80 chars ⇒ a
    successful ellipsis (77 + '…'); exactly 80 passes untouched."""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return json.dumps(val)
    if isinstance(val, str):
        if is_unsafe_text(val) or "{" in val or "}" in val:
            return None
        if len(val) > _SUMMARY_VALUE_MAX:
            return val[:_SUMMARY_ELLIPSIS_BODY] + _SUMMARY_ELLIPSIS
        return val
    return None


def _interpolate_summary(template: str, args: dict) -> "str | None":
    """Fail-safe literal substitutor (deliberately NOT ``str.format`` — Sol
    r1-3). Scan for ``{identifier}`` tokens and replace each with its scalar
    canonical-arg value. Returns the interpolated string, or ``None`` when the
    caller must fall back to the v0.77 headline.

    ANY of the following ⇒ ``None``: a lone/unmatched ``{`` or ``}``; an
    escaped ``{{`` / ``}}``; a conversion (``{x!r}``), format spec
    (``{x:>10}``), indexing (``{x[0]}``), or attribute access (``{x.y}``); a
    placeholder that does not resolve to a key; a non-scalar / unsafe /
    brace-bearing value; or ANY leftover brace in the final output."""
    out: list[str] = []
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if ch == "}":
            return None  # lone/unmatched (or escaped '}}') closing brace
        if ch == "{":
            j = template.find("}", i + 1)
            if j == -1:
                return None  # unmatched '{'
            inner = template[i + 1:j]
            if _SUMMARY_IDENT_RE.fullmatch(inner) is None:
                # '{{', '{}', '{x!r}', '{x:>10}', '{x[0]}', '{x.y}', '{a{b}' …
                return None
            if inner not in args:
                return None  # placeholder does not resolve
            rendered = _render_summary_value(args[inner])
            if rendered is None:
                return None  # non-scalar / unsafe / brace-bearing value
            out.append(rendered)
            i = j + 1
            continue
        out.append(ch)
        i += 1
    result = "".join(out)
    if "{" in result or "}" in result:
        return None  # defensive: no leftover braces in the FINAL output
    if is_unsafe_text(result):
        # Belt-and-suspenders: the template is UNSAFE-TEXT-validated at install
        # time (W1), but a control/bidi codepoint from the template text itself
        # (never from a value — those are pre-checked) must still never render.
        return None
    return result


def _interpolated_summary_or_none(
    summary: "str | None", canonical_json: str,
) -> "str | None":
    """Parse ``canonical_json`` back into the canonical args and interpolate
    *summary* against them (``None`` when there is no summary, the JSON is not
    a top-level object, or interpolation fails). The args come from the SAME
    canonical string shown in the binding block, so the sentence can never
    disagree with the exact-action block."""
    if not summary:
        return None
    try:
        args = json.loads(canonical_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(args, dict):
        return None
    return _interpolate_summary(summary, args)


def render_challenge_message(
    *, tool_name: str, enforcement_role: str, canonical_json: str,
    summary: "str | None" = None, display_name: "str | None" = None,
) -> str:
    """Render the operator-facing challenge body (W2).

    When a plugin-declared *summary* template exists AND interpolates cleanly
    against the canonical args, the headline leads with the agent's display
    name and a plain-language sentence, then DEMOTES the exact action (the FULL
    canonical args in a fenced block) below, then the FULL tool id. Otherwise
    it falls back to the v0.77-form headline (still name-led). BOTH forms keep
    the canonical args and the complete tool id verbatim (B8); size is measured
    on THIS string (``get_or_create`` refuses over ``_CHALLENGE_MAX_CHARS``).

    *display_name* is the CURRENT ``cfg.character.name`` threaded from the
    AuthzDeps factory; it passes through ``_resolve_display_name`` (empty / >64
    / UNSAFE-TEXT ⇒ the role string). Sent WITHOUT ``parse_mode`` — there is no
    Markdown/entity escaping here (pinned by a test).

    #328: the DISPLAYED args block passes through ``escape_unsafe_text`` —
    a bidi control inside a tool-input value would otherwise reorder what the
    operator reads, approving a display that differs from the bound value.
    The escape is a JSON string escape, so the shown block still parses to
    exactly the arguments the grant binds; the BINDING (``canonical_json`` /
    ``args_hash``) is untouched. For safe input the block is byte-identical
    (B8 verbatim contract preserved)."""
    short = short_tool_name(tool_name)
    display = _resolve_display_name(display_name, enforcement_role)
    shown_json = escape_unsafe_text(canonical_json)
    interpolated = _interpolated_summary_or_none(summary, canonical_json)
    if interpolated is not None:
        return (
            "\U0001F510 Approval needed\n\n"
            f"{display} ({enforcement_role}) wants to: {interpolated}\n\n"
            "Exact action (binding):\n"
            f"```\n{shown_json}\n```\n"
            f"Tool id: {tool_name}"
        )
    return (
        "\U0001F510 Approval needed\n\n"
        f"{display} ({enforcement_role}) wants to run {short} with EXACTLY "
        "these arguments:\n\n"
        f"```\n{shown_json}\n```\n"
        f"Tool id: {tool_name}"
    )


def _challenge_expired_text(tool_name: str) -> str:
    return f"⌛ Expired — {short_tool_name(tool_name)} was not approved in time"


def _broker() -> Any:
    """Resolve the process broker singleton by name so a test's monkeypatch of
    ``verdict_broker.BROKER`` is honored at call time."""
    import verdict_broker

    return verdict_broker.BROKER


@dataclass
class _Challenge:
    """Coordinator-owned record for one live authorization challenge.

    The two cleanup latches (``request_settled`` — the broker future has
    resolved; ``setup_settled`` — the owned setup driver's post/settlement is
    done) gate entry removal: whichever lands SECOND removes the entry, and
    removal is identity-guarded so a stale late latch can never evict a newer
    challenge registered under the same key by a retry.
    """

    key: Any            # GrantKey | trigger_consent.TriggerConsentKey
    scope: str
    rid: str
    req: Any                       # verdict_broker.PendingRequest
    broker: Any                    # the VerdictBroker instance that owns req
    driver: "asyncio.Task | None" = None
    setup_settled: bool = False
    request_settled: bool = False


@dataclass
class ChallengeHandle:
    """Returned synchronously by ``get_or_create``.

    ``created`` is ``False`` when an identical challenge was already pending.
    ``refused`` is ``"args_too_large"`` when the rendered challenge exceeded
    the size ceiling (no entry / no keyboard / no registration). ``settled_post``
    awaits the coordinator-owned setup driver (shielded, so a cancelled HOOK
    caller never cancels the driver — exactly ONE keyboard is ever posted) and
    then classifies the outcome.
    """

    created: bool
    refused: str | None = None
    _challenge: _Challenge | None = field(default=None, repr=False)

    async def settled_post(self) -> str:
        """``"posted"`` | ``"delivery_failed"`` | ``"inactive"`` (r2-B2/r3-B3).

        ``inactive`` = the request settled TERMINALLY (TTL timeout or /new)
        before or while the post was in flight — even a post that then
        succeeds shows an immediately-expired button, so the caller must NOT
        treat it as a live confirmation.
        """
        ch = self._challenge
        if ch is None or ch.driver is None:
            # Refused (or otherwise no live challenge) — nothing was posted.
            return "inactive"
        # Shield: a cancelled hook caller must not cancel the owned driver.
        await asyncio.shield(ch.driver)
        fut = ch.req._future
        if not fut.done():
            return "posted"
        outcome = fut.result()
        if isinstance(outcome, dict) and outcome.get("outcome") == "delivery_failed":
            return "delivery_failed"
        return "inactive"


class ChallengeCoordinator:
    """Atomic challenge registration + async-settled setup driver + two-latch
    cleanup (A:§3.4).

    Release B refactor: the registration/driver/latch machinery is GENERIC
    (:meth:`register_challenge`) — what Approve *does* is supplied by the
    caller (a sync commit step + a finish-hook factory), not hardwired.
    :meth:`get_or_create` is the authz-grant flavor (GrantKey mint + agent
    continuation dispatch, byte-for-byte the pre-refactor behavior);
    ``trigger_consent.prompt_trigger_consent`` is the plugin-trigger flavor
    (consent-ack record + reconcile, no continuation).
    """

    def __init__(self) -> None:
        # Keys are hashable challenge identities: ``GrantKey`` for authz
        # grants, ``trigger_consent.TriggerConsentKey`` for trigger consents.
        self._entries: dict[Any, _Challenge] = {}
        # Strong refs to the owned setup drivers so they are never GC'd
        # mid-flight; ``drain`` awaits exactly this set at shutdown.
        self._drivers: set[asyncio.Task] = set()

    # -- generic registration (SYNCHRONOUS, loop-confined, single owner) -----

    def register_challenge(
        self, key: Any, *, chat_id: int, operator_id: int, channel: Any,
        challenge_text: str, options: "list[str] | None" = None,
        on_commit_sync: "Callable[[int, dict], None] | None" = None,
        finish_factory: "Callable[[int, Any], Callable[[dict], Any]] | None" = None,
        kind: str = "authz", meta_extra: "dict | None" = None,
        timeout_s: "float | None" = None,
    ) -> ChallengeHandle:
        """Register ONE operator challenge keyboard for *key* (dedup by key).

        *on_commit_sync(option_index, meta)* runs in the Telegram callback
        IMMEDIATELY after a successful commit (no await between) — the
        caller's atomic record step (GrantKey mint / consent-ack write); it
        mutates the broker-owned *meta* to signal the finish hook.
        *finish_factory(message_id, req)* returns the settlement hook — the
        single serialized owner of ALL post-commit async work.

        Size validation runs BEFORE any insert: oversized ⇒ refused handle,
        NO entry, NO keyboard, NO registration. The broker scope stays
        ``authz:{chat_id}`` for every kind — the Telegram DM callback resolves
        taps under that scope and fail-closes on the meta's chat/operator.
        """
        existing = self._entries.get(key)
        if existing is not None:
            return ChallengeHandle(created=False, _challenge=existing)

        # #328: measured in UTF-16 units — Telegram's unit. A code-point
        # ``len()`` under-counts astral characters, letting a challenge pass
        # here and then fail ``post_dm_keyboard``, which unregisters it as a
        # delivery failure — permanently denying that exact-argument action.
        if utf16_len(challenge_text) > _CHALLENGE_MAX_CHARS:
            return ChallengeHandle(created=True, refused="args_too_large")

        options = list(options or ["Approve", "Deny"])
        broker = _broker()
        rid = uuid.uuid4().hex
        scope = f"authz:{chat_id}"
        # register() shallow-copies this dict; the complete meta (minus the
        # self-referential on_commit_sync) is supplied up front.
        meta = {
            "kind": kind,
            "chat_id": chat_id,
            "operator_id": operator_id,
            "options": options,
            "_scope": scope,
        }
        meta.update(meta_extra or {})
        req, _created = broker.register(
            namespace="resident_ask", scope=scope, request_id=rid,
            timeout_s=(_CHALLENGE_TTL_S if timeout_s is None else timeout_s),
            detached=True, supersede=False, meta=meta,
        )

        # The sync step MUST mutate the BROKER-OWNED req.meta (register()
        # shallow-copied our dict), so it is attached AFTER register with the
        # broker's own dict captured — mutating the caller's original would be
        # invisible to the finish hook (which reads e.g. req.meta["minted"]).
        if on_commit_sync is not None:
            def _step(idx: int, _meta: dict = req.meta,
                      _cb: Callable[[int, dict], None] = on_commit_sync) -> None:
                _cb(idx, _meta)

            req.meta["on_commit_sync"] = _step

        ch = _Challenge(key=key, scope=scope, rid=rid, req=req, broker=broker)
        # Request latch: fires on EVERY terminal resolution of the future
        # (answered / no_answer / cancelled / delivery_failed).
        req._future.add_done_callback(
            lambda _f, _c=ch: self._settle_request(_c)
        )
        self._entries[key] = ch

        async def _post() -> Any:
            return await channel.post_dm_keyboard(
                chat_id=chat_id, request_id=rid, text=challenge_text,
                options=options,
            )

        def _finish_factory(message_id: int, _req: Any = req) -> Callable[[dict], Any]:
            if finish_factory is None:
                async def _noop(_outcome: dict) -> None:
                    return None
                return _noop
            return finish_factory(message_id, _req)

        # Spawn the owned SETUP DRIVER (strong-ref'd): it does ONLY the
        # posting / setup settlement (single registration owner — register
        # already happened synchronously above).
        driver = asyncio.get_running_loop().create_task(
            self._drive(ch, _post, _finish_factory)
        )
        ch.driver = driver
        self._drivers.add(driver)
        driver.add_done_callback(self._drivers.discard)

        return ChallengeHandle(created=True, _challenge=ch)

    # -- the authz-grant flavor ----------------------------------------------

    def get_or_create(
        self, key: GrantKey, *, chat_id: int, operator_id: int,
        target_role: str, tool_name: str, canonical_json: str,
        enforcement_role: str, channel: Any, grants: "GrantStore",
        summary: "str | None" = None, display_name: "str | None" = None,
        engagement_id: str = "",
    ) -> ChallengeHandle:
        existing = self._entries.get(key)
        if existing is not None:
            return ChallengeHandle(created=False, _challenge=existing)

        # Rendering + size validation runs BEFORE any insert [A:§3.4]:
        # oversized -> refused handle, NO entry, NO keyboard, NO registration.
        # The plugin-declared *summary* + the current *display_name* are
        # captured here (W2) into the challenge render AND, below, the
        # settlement finish hook.
        challenge_text = render_challenge_message(
            tool_name=tool_name, enforcement_role=enforcement_role,
            canonical_json=canonical_json, summary=summary,
            display_name=display_name,
        )

        def _on_commit_sync(idx: int, meta: dict, _key: GrantKey = key) -> None:
            # Runs in the Telegram callback IMMEDIATELY after a successful
            # commit (no await between): idx 0 -> mint + record; idx 1 -> no-op.
            # #311: mint into the CALLER'S store (deps.grants in production —
            # the same store the enforcement hook consumes from), never the
            # module-global GRANTS, or an injected store never sees the
            # approval and the continuation re-challenges.
            if idx == 0:
                grants.mint(_key)
                meta["minted"] = True

        def _finish_factory(message_id: int, req: Any) -> Callable[[dict], Any]:
            # rid comes from the req itself (never an entry lookup — a retry
            # re-registered under the same key must not lend its rid here).
            return self._make_finish_hook(
                channel=channel, chat_id=chat_id, operator_id=operator_id,
                target_role=target_role, enforcement_role=enforcement_role,
                tool_name=tool_name, canonical_json=canonical_json,
                rid=req.request_id, message_id=message_id, req=req,
                display_name=display_name, engagement_id=engagement_id,
            )

        return self.register_challenge(
            key, chat_id=chat_id, operator_id=operator_id, channel=channel,
            challenge_text=challenge_text,
            on_commit_sync=_on_commit_sync, finish_factory=_finish_factory,
            kind="authz",
            meta_extra={
                "target_role": target_role,
                "grant_key": key,
                "canonical_args_json": canonical_json,
                "enforcement_role": enforcement_role,
            },
        )

    # -- owned setup driver -------------------------------------------------

    async def _drive(
        self, ch: _Challenge,
        post_factory: Callable[[], Any],
        finish_factory: Callable[[int], Callable[[dict], Any]],
    ) -> None:
        req = ch.req
        await ch.broker.ensure_posted(req, post_factory, finish_factory)
        if req._setup_task is None:
            # The request was ALREADY terminal before any post — ensure_posted
            # no-ops on a done future, so no setup task exists. Settle the
            # setup latch DIRECTLY (r4-B2).
            self._settle_setup(ch)
        else:
            # The setup task exists only now (r1-B1); attach the setup latch to
            # it (already done — ensure_posted awaited it — so the callback
            # fires on the next loop turn).
            req._setup_task.add_done_callback(
                lambda _t, _c=ch: self._settle_setup(_c)
            )

    # -- the single-owner authz finish hook ---------------------------------

    def _make_finish_hook(
        self, *, channel: Any, chat_id: int, operator_id: int,
        target_role: str, enforcement_role: str, tool_name: str,
        canonical_json: str, rid: str, message_id: int, req: Any,
        display_name: "str | None" = None, engagement_id: str = "",
    ) -> Callable[[dict], Any]:
        short = short_tool_name(tool_name)
        # Same render-time guard as the challenge headline (W2): the approved
        # settlement names the agent; deny/expired are name-free by decision.
        display = _resolve_display_name(display_name, enforcement_role)

        # #400: an engagement-origin challenge resumes the SPECIALIST ENGAGEMENT
        # (the finish hook re-delivers a system turn into rec.id/topic via the
        # channel's engagement-resume seam) instead of dispatching a synthetic
        # button turn to a resident bus role. The engagement id is bound into the
        # GrantKey too, so the retried call consumes only THIS engagement's grant.
        async def _dispatch_continuation(text: str) -> bool:
            if engagement_id:
                return await channel._dispatch_engagement_continuation(
                    engagement_id=engagement_id, text=text,
                )
            return await channel._dispatch_button_continuation(
                chat_id=chat_id, user_id=operator_id,
                target_role=target_role, request_id=rid, text=text,
            )

        _target_desc = (
            f"engagement {engagement_id[:8]}" if engagement_id else target_role
        )

        async def _finish(outcome: dict) -> None:
            o = outcome.get("outcome") if isinstance(outcome, dict) else None
            if o != "answered":
                await channel.edit_dm_message(
                    chat_id, message_id, _challenge_expired_text(tool_name),
                )
                return
            idx = outcome.get("option_index")
            if idx == 0:
                if not req.meta.get("minted"):
                    # Commit landed but the sync step never recorded the mint
                    # (raised + swallowed) — surface the internal error and
                    # NEVER dispatch an authorization the store can't back.
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        "internal error recording the approval — "
                        "call the tool again",
                    )
                    return
                # Edit the SUCCESS state FIRST, then dispatch, then overwrite
                # ONLY on dispatch failure (ordered inside this one hook task).
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"✅ Approved — {display} ({enforcement_role}) may run "
                    f"{short} once with exactly these arguments",
                )
                ok = await _dispatch_continuation(
                    "[authorization approved]: have "
                    f"{enforcement_role} call {tool_name} with EXACTLY "
                    f"these arguments:\n{canonical_json}"
                )
                if not ok:
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        f"⚠️ Approved, but delivery to {_target_desc} "
                        "failed — say 'retry' in chat",
                    )
            else:
                await channel.edit_dm_message(
                    chat_id, message_id, f"❌ Denied — {short} will not run",
                )
                ok = await _dispatch_continuation(
                    f"[authorization denied]: do not retry {tool_name}"
                )
                if not ok:
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        f"⚠️ Denied, but delivery to {_target_desc} "
                        "failed — say 'retry' in chat",
                    )

        return _finish

    # -- two-latch cleanup (coordinator-driven, never inside the edit) ------

    def _settle_request(self, ch: _Challenge) -> None:
        ch.request_settled = True
        self._maybe_remove(ch)

    def _settle_setup(self, ch: _Challenge) -> None:
        ch.setup_settled = True
        self._maybe_remove(ch)

    def _maybe_remove(self, ch: _Challenge) -> None:
        if not (ch.setup_settled and ch.request_settled):
            return
        # Identity-guarded: only evict THIS challenge, never a newer one that a
        # retry may have registered under the same key.
        if self._entries.get(ch.key) is ch:
            del self._entries[ch.key]

    # -- cancellation / drain ----------------------------------------------

    def cancel_matching(
        self, *, role: str | None = None, artifact: str | None = None,
        chat: int | None = None, plugin: str | None = None,
        persona: str | None = None,
    ) -> int:
        """Cancel the broker records for every live challenge matching ANY of
        the provided filters (keyboard -> expired via the finish hook). Returns
        the number of records actually cancelled."""

        def _matches(k: Any) -> bool:
            # getattr-based so BOTH key types match on the fields they carry:
            # GrantKey has all three; TriggerConsentKey carries artifact_id
            # only (a lifecycle artifact invalidation must kill its pending
            # consent keyboard; role/chat filters never match it).
            if role is not None and getattr(k, "enforcement_role", None) == role:
                return True
            if artifact is not None and getattr(k, "artifact_id", None) == artifact:
                return True
            if chat is not None and getattr(k, "chat_id", None) == chat:
                return True
            # plugin filter (Terra shipB-r1 P1-1): trigger_ack_revoke kills a
            # plugin's PENDING consent keyboards so a stale Approve tap can
            # never undo the revoke. GrantKey has no `plugin` attr ⇒ never
            # matched by this filter.
            if plugin is not None and getattr(k, "plugin", None) == plugin:
                return True
            # persona filter (#543): persona_remove / persona_ack_revoke kill a
            # persona's PENDING install keyboards, matching
            # PersonaInstallConsentKey.persona_id. This is best-effort only —
            # an ALREADY-ANSWERED challenge cannot be cancelled, which is why
            # the authoritative guard is the ack ledger's revocation
            # generation, not this sweep.
            if persona is not None and getattr(k, "persona_id", None) == persona:
                return True
            return False

        matched = [ch for k, ch in self._entries.items() if _matches(k)]
        n = 0
        for ch in matched:
            if ch.broker.cancel(
                namespace="resident_ask", scope=ch.scope,
                request_id=ch.rid, reason="challenge_cancelled",
            ):
                n += 1
        return n

    async def drain(self) -> None:
        """Await all outstanding setup-driver tasks (r4-B2/r5-B2). Called from
        casa_core's shutdown ladder AFTER ``BROKER.cancel_all()`` so a draining
        driver can only find a cancelled request (no fresh keyboard is posted)."""
        drivers = list(self._drivers)
        if drivers:
            await asyncio.gather(*drivers, return_exceptions=True)


# Process-wide singleton (mirrors GRANTS): casa_core's shutdown ladder and the
# resident authz hook (Task 7) import THIS instance.
CHALLENGES = ChallengeCoordinator()


# ---------------------------------------------------------------------------
# make_resident_authz_hook + AuthzDeps (A:§3.2) — Part 3 (Task 7)
# ---------------------------------------------------------------------------
#
# The enforcement core: a fail-closed PreToolUse hook the SDK invokes for EVERY
# tool call. It passes through unprotected tools untouched, and for a PROTECTED
# plugin tool it gates on turn provenance, consumes a single-use grant if one
# exists, and otherwise posts (or reuses) an operator confirmation challenge and
# DENIES the call — telling the model to retry the identical call after the
# operator taps Approve.
#
# The approval token is server-side only (``GrantStore``); it never enters model
# context. Every degraded path (wrong provenance, restart, expiry, oversized /
# unserializable args, unconfirmed teardown, ANY internal exception) fails
# CLOSED to an explicit SDK ``deny`` — an ESCAPED callback exception would
# become an SDK error control response, not a deny, which would violate the
# §3.1 fail-closed contract, so the entire protected path runs inside a
# try/except that re-raises only ``CancelledError``.


@dataclass
class AuthzDeps:
    """Runtime deps the hook resolves LAZILY at call time (r1-B7).

    ``channel`` is the Telegram channel object (``post_dm_keyboard`` /
    ``edit_dm_message`` / ``_dispatch_button_continuation``) — the DM the
    confirmation button is posted to. ``grants`` / ``challenges`` are the
    process-wide ``GRANTS`` / ``CHALLENGES`` singletons in production, injected
    here so tests can substitute fakes.
    """

    channel: Any
    grants: GrantStore
    challenges: ChallengeCoordinator
    # CURRENT ``cfg.character.name`` (W2) — resolved LAZILY at call time by the
    # factory so a reload's new name surfaces on the next challenge; ``None`` /
    # empty / unsafe falls back to the role string at render time.
    display_name: "str | None" = None


# Deny reasons — one source of truth (tests assert on these exact strings).
# #400: the blanket "not available inside engagements" deny is GONE — an
# operator-gated plugin tool on an interactive specialist engagement now routes
# through the DM authorization challenge (engagement-bound grant). This deny
# remains for engagement turns that CANNOT be authorized: a non-specialist /
# non-active / topic-less / operator-less engagement record. Same
# instruction-not-description rule as _DENY_POSTED — control flow only.
_DENY_ENGAGEMENT_UNAVAILABLE = (
    "not executed: this engagement cannot authorize a protected action "
    "(no reachable operator). Do NOT retry the call."
)
_DENY_UNSUPPORTED_ORIGIN = (
    "protected action requires operator confirmation over the Telegram DM "
    "(unsupported origin)"
)
_DENY_ROLE_MISMATCH = "execution-role mismatch"
_DENY_UNRENDERABLE = (
    "arguments cannot be rendered for confirmation (unserializable / too "
    "large) — narrow the call"
)
_DENY_PENDING = (
    # #221: same instruction-not-description rule as _DENY_POSTED below. The
    # old wording ("waiting for the user's tap") is exactly the phrasing the
    # §Protected tools rule forbids the resident from repeating.
    "not executed: operator authorization is already outstanding for this "
    "exact call. Produce NO user-facing narration and end your turn now."
)
_DENY_DELIVERY_FAILED = "could not post the confirmation button — try again"
_DENY_INACTIVE = (
    "the confirmation expired before it could be delivered — call again"
)
_DENY_POSTED = (
    # #221: this string is the PROXIMAL cause of a doctrine violation, so it
    # is written as an instruction, not as a description. The old wording
    # ("a confirmation button was posted to the user…") described the UI, and
    # the resident paraphrased it straight back to the operator ("Sent you an
    # approval prompt — tap Approve when you're ready"), which the §Protected
    # tools rule in every resident prompt explicitly forbids: the operator
    # already sees the button, and any "waiting for you" phrasing is false the
    # moment they tap. Describing the UI to a model invites the model to
    # relay it. So: NO user-facing fact here at all — not even "the UI was
    # delivered", which is just as paraphrasable ("I sent the confirmation
    # separately"). Only the control flow the model needs.
    "not executed: awaiting operator authorization. Produce NO user-facing "
    "narration and end your turn now. When authorization arrives, retry the "
    "SAME call with EXACTLY the same arguments."
)
_DENY_INTERNAL = "internal authorization error — the call was not executed"
_DENY_NOT_OPERATOR = (
    # #368: an authorization challenge is answerable only by the configured
    # operator, so a turn from any other sender is refused OUTRIGHT — posting
    # a challenge would hand the requester their own approval button. Same
    # instruction-not-description rule as _DENY_POSTED: control flow only,
    # nothing about who is (or is not) at the keyboard for the model to relay.
    "not executed: this protected action requires authorization by the "
    "configured operator, and this conversation cannot provide it. Do NOT "
    "retry the call."
)


def make_resident_authz_hook(
    role: str,
    protected: dict[str, dict],
    deps_factory: "Callable[[], AuthzDeps | None]",
) -> "Callable":
    """Build the fail-closed PreToolUse authz hook for one resident/specialist.

    ``role`` is the agent's own (plain, tier-stripped) role — asserted equal to
    ``origin.execution_role`` as defense in depth and used as
    ``GrantKey.enforcement_role``. ``protected`` maps each full tool name to
    ``{"artifact_id": ..., "summary": ...}`` (from ``plugin_grants.protected_map``
    over the SAME ``ResolutionResult`` used to build the agent's options — a
    mid-TTL plugin update changes the artifact and invalidates the grant).
    This hook consumes ONLY ``artifact_id`` — exactly as before v0.78.0, no
    grant/GrantKey/enforcement change; ``summary`` is advisory copy threaded
    to the challenge render by the coordinator (W2), not read here.
    ``deps_factory`` resolves the Telegram channel + stores LAZILY at call time;
    ``None`` (no DM reachable) is the unsupported-origin deny.

    The returned callable matches the SDK ``HookCallback`` signature
    ``(input_data, tool_use_id, context) -> HookJSONOutput``: ``{}`` passthrough,
    or the shipped ``PreToolUseHookSpecificOutput`` deny shape (mirrors
    ``hooks._deny`` — the engagement relay's precedent).
    """
    from hooks import _deny  # mirror the shipped PreToolUse deny shape

    async def _hook(
        input_data: "dict[str, Any]",
        tool_use_id: "str | None",
        context: "dict[str, Any]",
    ) -> "dict[str, Any]":
        tool_name = (input_data or {}).get("tool_name", "")
        if tool_name not in protected:
            # Passthrough: unprotected tool — never touch the store/coordinator
            # or even resolve deps.
            return {}
        # ---- fail-closed wrapper (r4-B2): everything below returns a VALID
        # deny on any non-cancellation exception; CancelledError re-raises. ----
        try:
            import agent as agent_mod
            from provenance import strict_positive_id, turn_provenance

            prov = turn_provenance()

            tool_input = (input_data or {}).get("tool_input") or {}

            # Shared authorization tail (#368/#336/#221 discipline unchanged):
            # operator check ⇒ canonicalize ⇒ single-use engagement-aware grant
            # consume ⇒ else post/reuse ONE challenge and deny. ``engagement_id``
            # is "" for the direct/delegated DM path and the engagement id for
            # the #400 specialist-engagement path — it is folded into the
            # GrantKey (so an approval binds to THIS engagement) AND threaded to
            # the coordinator (so the approval finish hook resumes THIS
            # engagement instead of dispatching a resident continuation).
            async def _authorize(
                *, deps: "AuthzDeps", operator_id: int, chat_id: int,
                engagement_id: str, target_role: "str | None",
            ) -> "dict[str, Any]":
                # #368: only the CONFIGURED operator may satisfy a challenge —
                # deny OUTRIGHT for any other sender, BEFORE the grant lookup and
                # with NO challenge (the requester must never receive their own
                # approval button). Accept-all mode (empty telegram_chat_id) ⇒ NO
                # sender is the operator (#336). Fail-closed when the channel
                # cannot answer the question.
                is_op = getattr(deps.channel, "_user_id_is_operator", None)
                if not callable(is_op) or not is_op(operator_id):
                    return _deny(_DENY_NOT_OPERATOR)

                # Canonicalize — the args_hash for the grant key AND the
                # challenge body. Unserializable ⇒ deny with NO challenge (an
                # unrenderable call can never match a grant minted from a valid
                # hash).
                try:
                    canonical_json = canonical_args_json(tool_input)
                except ValueError:
                    return _deny(_DENY_UNRENDERABLE)
                args_hash = hashlib.sha256(
                    canonical_json.encode("utf-8")).hexdigest()

                key = GrantKey(
                    operator_id=operator_id, chat_id=chat_id,
                    enforcement_role=role,
                    artifact_id=protected[tool_name]["artifact_id"],
                    tool_name=tool_name, args_hash=args_hash,
                    engagement_id=engagement_id,
                )

                # Single-use consume (provenance already gated above).
                if deps.grants.consume(key):
                    return {}  # allow — operator freshly approved THIS call.

                # No grant — post (or reuse) a confirmation challenge and deny.
                # get_or_create renders + size-validates BEFORE any coordinator
                # insert (too-large ⇒ refused).
                handle = deps.challenges.get_or_create(
                    key, chat_id=chat_id, operator_id=operator_id,
                    target_role=target_role, tool_name=tool_name,
                    canonical_json=canonical_json, enforcement_role=role,
                    channel=deps.channel, grants=deps.grants,
                    summary=protected[tool_name].get("summary"),
                    display_name=deps.display_name,
                    engagement_id=engagement_id,
                )
                if handle.refused == "args_too_large":
                    return _deny(_DENY_UNRENDERABLE)
                if handle.created is False:
                    return _deny(_DENY_PENDING)  # identical challenge already up.

                # settled_post awaits the coordinator-owned setup driver
                # (shielded); deny latency ≈ one Telegram post RTT.
                outcome = await handle.settled_post()
                if outcome == "delivery_failed":
                    return _deny(_DENY_DELIVERY_FAILED)
                if outcome == "inactive":
                    return _deny(_DENY_INACTIVE)
                return _deny(_DENY_POSTED)

            # 1. ENGAGEMENT path (#400) FIRST — an interactive specialist
            # engagement's protected call routes through the DM authorization
            # challenge, bound to THIS engagement. NO closure-role assertion:
            # an in_casa specialist inherits the outer execution_role, so the
            # rec.kind gate + engagement-bound key contain it (never assert).
            # Fail-closed unless the record is an ACTIVE SPECIALIST with a topic
            # and a reachable operator DM (read from the record's own origin —
            # the operator who started the engagement, not the caller-supplied
            # turn origin, which for an engagement is not a DM).
            if prov.execution == "engagement":
                import tools as tools_mod
                rec = tools_mod.engagement_var.get(None)
                # A non-empty id is REQUIRED: it becomes the GrantKey's
                # engagement_id AND the finish hook's routing discriminator
                # (empty ⇒ the DM/bus-role path), so an id-less record must never
                # reach the engagement-bound path — fail closed.
                if (rec is None
                        or not getattr(rec, "id", "")
                        or getattr(rec, "kind", None) != "specialist"
                        or getattr(rec, "status", None) != "active"
                        or strict_positive_id(
                            getattr(rec, "topic_id", None)) is None):
                    return _deny(_DENY_ENGAGEMENT_UNAVAILABLE)
                eng_origin = getattr(rec, "origin", None) or {}
                operator_id = strict_positive_id(eng_origin.get("user_id"))
                chat_id = strict_positive_id(eng_origin.get("chat_id"))
                if operator_id is None or chat_id is None:
                    return _deny(_DENY_ENGAGEMENT_UNAVAILABLE)
                deps = deps_factory()
                if deps is None:
                    return _deny(_DENY_ENGAGEMENT_UNAVAILABLE)
                return await _authorize(
                    deps=deps, operator_id=operator_id, chat_id=chat_id,
                    engagement_id=rec.id,
                    target_role=getattr(rec, "role_or_type", None),
                )

            # 2. Transport/execution gate — no challenge. Provenance is consulted
            # BEFORE any grant lookup (a copied chat_id/user_id on a webhook turn
            # can never consume a grant).
            if (prov.transport not in ("dm", "button")
                    or prov.execution not in ("direct", "delegated")):
                return _deny(_DENY_UNSUPPORTED_ORIGIN)

            origin = agent_mod.origin_var.get(None) or {}

            # 3. Explicit role-mismatch deny (defense in depth) — an explicit
            # deny, never an assert.
            if role != origin.get("execution_role"):
                return _deny(_DENY_ROLE_MISMATCH)

            # 4. Resolve the DM channel + stores lazily. None ⇒ no DM reachable.
            deps = deps_factory()
            if deps is None:
                return _deny(_DENY_UNSUPPORTED_ORIGIN)

            operator_id = strict_positive_id(origin.get("user_id"))
            chat_id = strict_positive_id(origin.get("chat_id"))
            if operator_id is None or chat_id is None:
                # The transport gate already guarantees these; stay fail-closed.
                return _deny(_DENY_UNSUPPORTED_ORIGIN)

            # target_role is the ORIGINATING resident (for a delegated specialist
            # this is origin.role, i.e. Ellen — the continuation routes back to
            # her; B1 ruling).
            return await _authorize(
                deps=deps, operator_id=operator_id, chat_id=chat_id,
                engagement_id="", target_role=origin.get("role"),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — fail closed, never let it escape
            logger.exception(
                "authz hook internal error (tool=%s role=%s) — denying",
                tool_name, role)
            return _deny(_DENY_INTERNAL)

    # Markers so the options-build wiring tests can identify the appended
    # matcher (without depending on the closure's name) and exercise the
    # display-name factory threaded through both AuthzDeps factory sites.
    _hook._casa_authz_role = role  # type: ignore[attr-defined]
    _hook._casa_authz_deps_factory = deps_factory  # type: ignore[attr-defined]
    return _hook
