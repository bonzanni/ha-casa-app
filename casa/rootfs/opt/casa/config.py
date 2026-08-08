"""Configuration dataclasses and model mapping for Casa agents.

The actual loader lives in ``agent_loader.load_agent_from_dir`` — this
module only defines the dataclasses, ``MODEL_MAP`` / ``resolve_model``,
and the ``${ENV}`` substitution helper used by the loader.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from persona_pack import PersonaPack
    from role_artifact import RoleArtifactSource
    from role_slot import RoleSlot

logger = logging.getLogger(__name__)

MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}


def resolve_model(shortname: str) -> str:
    """Resolve a shortname to a full Anthropic model ID.

    If *shortname* is already a full model ID (contains a hyphen and digits),
    it is returned unchanged. Otherwise it must be a key in MODEL_MAP.

    An unresolved ``${VAR}`` env placeholder is returned unchanged (a DEFERRED
    value, resolved at boot when ``_substitute_env`` runs with the var set). This
    makes ``validate_config_repo`` env-INDEPENDENT: any caller (config_sync, the
    live invariant auditor, the configurator pre-commit gate, future tools) that
    validates ``runtime.yaml`` without the model env exported no longer
    false-positives on the placeholder. Boot is unaffected — the value is
    already substituted before it reaches here, so boot never sees a placeholder
    and keeps rejecting genuine typos. (Generalises the point-local D1 fix.)

    Raises ValueError for unknown shortnames.
    """
    if shortname in MODEL_MAP:
        return MODEL_MAP[shortname]
    if _ENV_RE.fullmatch(shortname.strip()):
        return shortname  # deferred env placeholder — resolved at boot
    if "-" in shortname:
        return shortname
    raise ValueError(
        f"Unknown model shortname '{shortname}'. "
        f"Valid shortnames: {', '.join(MODEL_MAP)}"
    )


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)}")


def _substitute_env(text: str) -> str:
    """Replace ``${VAR_NAME}`` placeholders with environment variable values.

    A placeholder whose variable is unset is left in place — that is what makes
    ``resolve_model`` above able to treat it as a DEFERRED value.
    """

    def _replacer(match: re.Match) -> str:
        var = match.group(1)
        return os.environ.get(var, match.group(0))

    return _ENV_RE.sub(_replacer, text)


class _EnvSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` that resolves ``${VAR}`` as each scalar is CONSTRUCTED.

    Substituting into a YAML file's *text* and parsing afterwards re-lexes the
    substituted VALUE as part of the document, so whatever the variable happens
    to contain decides what the document means: a ``#`` truncates the scalar at
    a comment, a quote character ends it early and usually breaks the parse
    outright, a newline splits it. For a resident agent a file that fails to
    parse stops boot, so an environment variable's punctuation could stop Casa
    starting (#409).

    Resolving at construction time removes that, because the document's shape is
    already decided: the parser has finished lexing, every field and entry
    boundary is fixed, and a variable's contents can no longer move a field,
    truncate its neighbour or fail the parse.

    It is done HERE, in the constructor, rather than by walking the finished
    document, because this is the last point at which the scalar's own STYLE is
    still known — and in YAML the style is what says whether the author meant
    text. So the rule is YAML's own, applied to one scalar instead of to the
    file:

    * **Quoted, or tagged ``!!str``** (``prompt: "${DETAIL}"``) — a string.
      Quoting is how YAML says "this is text", and it is what an author reaches
      for when a value might contain punctuation. This is the form that gets
      #409's guarantee: the value arrives exactly as it is, whatever it holds.
      A placeholder under any OTHER tag never reaches this function — PyYAML
      dispatches on the tag and builds it itself, so ``k: !!int ${V}`` fails on
      the unresolved text. That is a load error rather than a silent value, and
      nothing shipped writes one.
    * **Embedded in other text** (``prompt: Send ${DETAIL}``) — a string too. It
      always was one; a placeholder with text around it was never the whole
      scalar.
    * **Plain and alone** (``minutes: ${MINUTES}``) — the resolved text means
      what that text means as YAML read ON ITS OWN, which is substantially what
      it has always meant here. "On its own" is the one qualifier: the read-back
      is a separate parse, so a value that is itself a YAML alias cannot reach an
      anchor defined elsewhere in the file, and reads as text instead.

    That last case is deliberately NOT filtered by what the read-back turns out
    to be. Three review rounds each rejected a different filter — "always a
    string" stringified a list and made ``path_scope`` iterate CHARACTERS, so a
    lone ``/`` prefix matched every absolute path and the guard failed OPEN;
    "anything but a string" lost the file's quoting and retyped ``"true"`` to
    ``True``; an allowlist of "safe" types excluded ``!!set`` and reopened the
    very fail-open the first round had closed. Each filter was a judgment about
    what an operator "meant", and each grew a new hole. There is no judgment
    left: an unquoted scalar means what YAML says it means, and an author who
    wants text says so by quoting.

    The one thing the read-back does that plain YAML would not is fall back to
    the literal string when the value is not a valid YAML document on its own.
    That is not a type judgment — it is the same reading YAML gives any text it
    cannot parse as anything else — and it is what stops PyYAML's constructors,
    which raise more than ``YAMLError`` (an implausible date raises
    ``ValueError`` from the timestamp constructor), from taking the process down.
    """

    def compose_scalar_node(self, anchor):
        # ``event.tag`` is None (or "!") exactly when the document did NOT name
        # a tag. Recorded before the event is consumed, because the finished
        # node cannot tell an authored ``!!str`` from a resolved one — and an
        # explicit tag is a stronger statement of intent than quoting is.
        explicit = self.peek_event().tag not in (None, "!")
        node = super().compose_scalar_node(anchor)
        node.env_explicit_tag = explicit
        return node


def _construct_env_scalar(loader: _EnvSafeLoader, node):
    value = loader.construct_scalar(node)
    resolved = _substitute_env(value)
    if resolved == value:
        # No placeholder, or the variable is unset — the latter deliberately
        # leaves ``${VAR}`` in place so ``resolve_model`` can treat it as a
        # DEFERRED value and ``validate_config_repo`` stays env-independent.
        return value
    if (node.style is not None
            or getattr(node, "env_explicit_tag", False)
            or not _ENV_RE.fullmatch(value.strip())):
        return resolved                      # quoted, tagged, or embedded → text
    try:
        return yaml.safe_load(resolved)      # plain: it means what it means
    except (Exception, RecursionError):      # noqa: BLE001 — see class docstring
        return resolved                      # not YAML on its own → it is text


_EnvSafeLoader.add_constructor(
    "tag:yaml.org,2002:str", _construct_env_scalar)


# YAML's string type, and the handle prefixes a document gets without asking.
# A tag is RESOLVED rather than pattern-matched below, because the same type has
# arbitrarily many spellings — `!!str`, the verbatim `!<tag:yaml.org,2002:str>`,
# and any handle a `%TAG` directive cares to define — and a spelling missing
# from a list is a MISS, which is the expensive direction.
_STR_TAG = "tag:yaml.org,2002:str"
_DEFAULT_TAG_HANDLES = {"!": "!", "!!": "tag:yaml.org,2002:"}


def text_has_lone_placeholder(text: str) -> bool:
    """Whether *text* holds a scalar a rewrite could change the MEANING of.

    The question every component that REWRITES one of these files has to ask
    before it does. A rewrite re-emits through a plain YAML dump, which does not
    preserve quote style or tags — and those are what make a placeholder text
    (see :class:`_EnvSafeLoader`). Refusing to rewrite is the answer; knowing
    exactly when to refuse is this.

    Exactly one shape qualifies: a scalar that is **nothing but** a placeholder
    **and** is declared text by its source — quoted in any style, or carrying
    YAML's string tag however that tag is spelled. Every other shape survives a
    rewrite unchanged, and each was a false refusal first — which is not the
    cheap direction, because refusing costs a file its entry-level
    reconciliation (image-wins, destroying every locally-added entry) or costs
    the user a reminder they asked for:

    * a placeholder in a COMMENT reaches no loader at all, and comments are not
      tokens;
    * one EMBEDDED in a larger scalar (``Send ${DETAIL}``) is a string under
      every style, because it could never have been read back as a value;
    * one that is not the loader's pattern (``${NOT-A-VAR}``) is never resolved;
    * a PLAIN lone placeholder (``minutes: ${MINUTES}``) is read back as a value
      both before and after the rewrite — a dump re-emits it plain, so nothing
      about it changes.

    Only the declared-text lone form means one thing before a rewrite and
    another after, and it is the loader's own ``fullmatch``-plus-style branch
    read backwards.

    It reads TOKENS, not the constructed document, and is therefore exact about
    what the text says and conservative about what survives building it: a
    declared-text placeholder that construction then discards — the losing side
    of a merge key, an overridden duplicate key — is still reported, and costs
    that file its entry-level reconciliation. Chasing that would mean tracking
    which scalars survive construction, which is a second model of YAML's
    semantics living next to PyYAML's; the conservatism is the cheaper error,
    and it is bounded to configuration that is already dead.

    Fails CLOSED on unscannable text (True) for the same reason: a file this
    cannot tokenise is one no caller should be rewriting either.
    """
    try:
        handles = dict(_DEFAULT_TAG_HANDLES)
        pending_tag = None
        for token in yaml.scan(text):
            if isinstance(token, yaml.tokens.DirectiveToken):
                if token.name == "TAG":
                    handle, prefix = token.value
                    handles[handle] = prefix
                continue
            if isinstance(token, yaml.tokens.TagToken):
                pending_tag = token.value
                continue
            if isinstance(token, yaml.tokens.AnchorToken):
                # A tag and an anchor may be written in EITHER order before the
                # scalar they belong to, so an anchor must not discard the tag
                # that preceded it.
                continue
            if isinstance(token, yaml.tokens.ScalarToken):
                declared_text = not token.plain or (
                    pending_tag is not None
                    and _resolve_tag(pending_tag, handles) == _STR_TAG)
                # No ``.strip()`` here, unlike the loader's own fullmatch: a
                # quoted value with padding is one YAML must KEEP quoted, so a
                # rewrite cannot drop its quotes and cannot change its meaning.
                if declared_text and _ENV_RE.fullmatch(token.value):
                    return True
            pending_tag = None
        return False
    except (Exception, RecursionError):  # noqa: BLE001 — never raise at boot
        return True


def _resolve_tag(tag, handles: dict) -> str:
    """A scanned ``(handle, suffix)`` tag as its full URI, as the parser sees it.

    A verbatim tag (``!<...>``) arrives with no handle and is already whole; a
    handled one is its prefix plus its suffix, where the prefix is a default or
    whatever a ``%TAG`` directive bound it to.
    """
    handle, suffix = tag
    if handle is None:
        return suffix
    return handles.get(handle, handle) + suffix


def load_yaml_with_env(text: str):
    """Parse *text*, resolving ``${VAR}`` per scalar. See :class:`_EnvSafeLoader`.

    Raises whatever parsing *text* raises — ``yaml.YAMLError`` for malformed
    input, but also the ``ValueError``/``KeyError`` PyYAML's own constructors
    raise for an explicitly tagged scalar they cannot build (``!!int nope``).
    Callers fold ALL of it into their own error type; see
    ``agent_loader.parse_yaml_text``, which is the only caller that should
    exist.
    """
    return yaml.load(text, Loader=_EnvSafeLoader)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ToolsConfig:
    allowed: list[str] = field(default_factory=list)
    disallowed: list[str] = field(default_factory=list)
    permission_mode: str = ""
    max_turns: int = 10
    skills: str = "all"
    voice_guard: str = "none"


_VALID_READ_STRATEGIES = ("per_turn", "cached")


@dataclass
class MemoryConfig:
    token_budget: int = 4000
    read_strategy: str = "per_turn"
    cross_peer_token_budget: int = 2000   # M6 § 6.3


@dataclass
class SessionConfig:
    strategy: str = "ephemeral"
    idle_timeout: int = 300


_VALID_TAG_DIALECTS = ("square_brackets", "parens", "none")


@dataclass
class TTSConfig:
    tag_dialect: str = "square_brackets"

    def __post_init__(self) -> None:
        if self.tag_dialect not in _VALID_TAG_DIALECTS:
            raise ValueError(
                f"Invalid tts.tag_dialect {self.tag_dialect!r}; "
                f"must be one of {_VALID_TAG_DIALECTS}"
            )


@dataclass
class CharacterConfig:
    name: str = ""
    archetype: str = ""
    card: str = ""
    prompt: str = ""


@dataclass
class VoiceConfig:
    tone: list[str] = field(default_factory=list)
    cadence: str = "natural"
    forbidden_patterns: list[str] = field(default_factory=list)
    signature_phrases: dict[str, str] = field(default_factory=dict)


@dataclass
class ResponseShapeConfig:
    max_sentences_confirmation: int = 2
    max_sentences_status: int = 3
    register: str = "written"
    format: str = "plain"
    rules: list[str] = field(default_factory=list)


@dataclass
class DisclosureConfig:
    policy: str = ""
    overrides: dict[str, Any] = field(default_factory=dict)


@dataclass
class DelegateEntry:
    agent: str
    purpose: str
    when: str


@dataclass
class ExecutorEntry:
    executor_type: str
    purpose: str
    when: str


@dataclass
class TriggerSpec:
    name: str
    type: str                            # interval | cron | date | webhook
    minutes: int = 0
    schedule: str = ""
    path: str = ""
    channel: str = ""
    prompt: str = ""
    # v2 (Release A): per-trigger webhook auth policy + memory read clearance.
    # ``auth`` is None for non-webhook triggers; webhook triggers are normalized
    # to a full policy dict by agent_loader (defaulting absent auth to hmac_body).
    # ``clearance`` bounds what memory tiers a webhook-origin turn may recall
    # (never "private"); ignored for non-webhook triggers.
    auth: dict[str, Any] | None = None
    clearance: str = "public"
    # #396: point-in-time reminders. ``at`` is an ISO-8601 instant WITH a UTC
    # offset and is meaningful only for type="date" — cron has no year field,
    # so a dated one-shot written as cron is an ANNUAL trigger in disguise.
    # ``one_shot`` makes the registry drop the scheduler job after a single
    # fire, and — only for an entry the agent owns (see ``managed_by``) — remove
    # the triggers.yaml entry too. An operator's own dated one-shot keeps its
    # entry, which then lingers inert (INV-TRIG-009).
    at: str = ""
    one_shot: bool = False
    # #398 release 2: who owns this entry. ``"agent"`` for a reminder the
    # resident's own tools created; empty for operator configuration.
    #
    # Read straight off the entry's ``managed_by`` field and never inferred.
    # Under #396 this was ``from_reminder_store``, derived from WHICH FILE the
    # spec was loaded from — reminders lived in their own reminders.yaml
    # because config_sync would otherwise erase a locally-added triggers.yaml
    # entry on an update that changed the shipped default. Release 1 made that
    # file reconcile per entry, so the separate file lost its purpose and the
    # provenance signal had to move onto the entry itself.
    #
    # It must stay a field. The schema permits an operator to author
    # ``name: reminder-bins / type: date / one_shot: true`` — so neither the
    # reserved name prefix, nor the type, nor the flag distinguishes an agent's
    # reminder from an operator's own dated trigger. Three review rounds of
    # inferring provenance each found a new way to delete a live operator
    # trigger. This bounds the reminder writer, the overdue sweep, reverse job
    # reconciliation and the post-fire cleanup.
    managed_by: str = ""


@dataclass
class HooksConfig:
    pre_tool_use: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RequiresConfig:
    """A delegated agent's declared launch dependencies (spec A5).

    ``plugins`` names must be a subset of the plugins actually resolved
    for the agent's tier:role target; ``tools`` are full MCP tool names
    (``mcp__plugin_<plugin>_<server>__<tool>``) that must be BOTH
    manifest-declared (``casa.provides_tools``) AND have their SERVER
    grant actually attached (``grants_for_resolution``). Empty on both
    fields (the default) skips the requires gate entirely — a delegated
    agent with no ``requires:`` block launches from model memory exactly
    as before."""
    plugins: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)


@dataclass
class AgentConfig:
    role: str = ""
    model: str = ""
    enabled: bool = True
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    mcp_server_names: list[str] = field(default_factory=list)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    voice_errors: dict[str, str] = field(default_factory=dict)
    channels: list[str] = field(default_factory=list)
    cwd: str = ""

    character: CharacterConfig = field(default_factory=CharacterConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    response_shape: ResponseShapeConfig = field(default_factory=ResponseShapeConfig)
    disclosure: DisclosureConfig | None = None
    delegates: list[DelegateEntry] = field(default_factory=list)
    executors: list[ExecutorEntry] = field(default_factory=list)
    triggers: list[TriggerSpec] = field(default_factory=list)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    system_prompt: str = ""
    requires: RequiresConfig = field(default_factory=RequiresConfig)
    # Personality Phase A, Task 5/6: the image-owned canonical role artifact
    # (defaults/roles/<kind>/<slot>/{role.yaml,doctrine.md}) this agent was
    # loaded against. REQUIRED as of Task 6 (kw_only, no default) — every
    # AgentConfig is now backed by a real role artifact; a caller building one
    # outside agent_loader.load_agent_from_dir (unit-test fixtures probing
    # unrelated subsystems) must pass a stand-in explicitly
    # (tests/role_artifact_stub.py). Task 6 consumes this exact source for
    # model resolution and the role checksum.
    role_artifact: RoleArtifactSource = dataclasses.field(kw_only=True)
    # --- Personality Phase A, Task 6 additive fields -----------------------
    # Populated by agent_loader._build_runtime_fields from the materialized
    # RoleSlot; role_slot/persona_pack/binding/compiled_prompt_bundle/
    # speaker_provenance stay None until Task 7/8 wire persona binding.
    role_id: str = dataclasses.field(default="", kw_only=True)
    kind: str = dataclasses.field(default="", kw_only=True)
    resolved_model: str = dataclasses.field(default="", kw_only=True)
    role_checksum: str = dataclasses.field(default="", kw_only=True)
    role_slot: "RoleSlot | None" = dataclasses.field(default=None, kw_only=True)
    persona_pack: "PersonaPack | None" = dataclasses.field(default=None, kw_only=True)
    binding: "BindingRecord | None" = dataclasses.field(default=None, kw_only=True)
    compiled_prompt_bundle: "CompiledPromptBundle | None" = dataclasses.field(default=None, kw_only=True)
    binding_digest: str = dataclasses.field(default="", kw_only=True)
    speaker_provenance: "SpeakerProvenance | None" = dataclasses.field(default=None, kw_only=True)


@dataclass
class ExecutorMemoryConfig:
    """Per-executor memory wiring (M4).

    Defaults to ``enabled=False`` so existing executor definitions without
    a ``memory:`` block continue to work unchanged. When enabled, the
    engager pulls a per-channel-per-chat archive of prior engagement
    summaries and interpolates the digest into ``{executor_memory}`` in
    the prompt template.
    """
    enabled: bool = False
    token_budget: int = 2000


@dataclass
class ExecutorDefinition:
    """Tier 3 Executor type definition.

    Materially different shape from AgentConfig: no session, no
    channels. Mirrors spec section 5.2 YAML.
    """
    type: str
    description: str
    model: str
    driver: str                                  # "in_casa" | "claude_code"
    enabled: bool = True
    tools_allowed: list[str] = field(default_factory=list)
    tools_disallowed: list[str] = field(default_factory=list)
    permission_mode: str = "acceptEdits"
    mcp_server_names: list[str] = field(default_factory=list)
    idle_reminder_days: int = 7
    prompt_template_path: str = ""
    hooks_path: str | None = None
    # Task 3 (#360): the load-time-validated, constructible parsed hooks
    # document — snapshotted verbatim for EVERY executor regardless of
    # driver (agent_loader.load_all_executors / _resolve_executor_hooks).
    # `{}` ONLY when no hooks file exists at all; an executor WITH a hooks
    # file (including in_casa's configurator) gets its REAL declared
    # entries here, never a synthesized default (REVISION 3b). Consumed
    # verbatim downstream (Tasks 4-6) — never re-read from disk.
    hooks_document: dict = field(default_factory=dict)
    observer_policy_path: str | None = None
    doctrine_dir: str = ""
    # --- Plan 4a additions (claude_code driver) ---
    extra_dirs: list[str] = field(default_factory=list)
    mirror_chat_to_topic: bool = True
    plugins_dir: str = ""   # absolute path to per-executor plugins/ dir; "" = none
    # --- M4 addition (engagement memory) ---
    memory: ExecutorMemoryConfig = field(default_factory=ExecutorMemoryConfig)
    # --- Personality Phase A, Task 5/6 addition ---
    # The image-owned canonical role artifact at defaults/roles/executor/<type>/.
    # Executors have no persona and no binding (see role.yaml persona: forbidden).
    # REQUIRED as of Task 6 (kw_only, no default) — see AgentConfig's identical
    # note above; unit-test fixtures pass tests/role_artifact_stub.py's stand-in.
    role_artifact: RoleArtifactSource = dataclasses.field(kw_only=True)
    # Personality Phase A, Task 6: the role-only identity triple (spec §2.3),
    # populated by agent_loader.load_all_executors via materialize_role +
    # compute_executor_identity.
    role_id: str = dataclasses.field(default="", kw_only=True)
    role_checksum: str = dataclasses.field(default="", kw_only=True)
    resolved_model: str = dataclasses.field(default="", kw_only=True)
    effective_config_digest: str = dataclasses.field(default="", kw_only=True)
