"""The 1Password vault tools: list_vault_items, get_item_fields and
vault_drop_off, the field projection they share, and the secret candidates
a plugin mutation reports for a just-installed plugin."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess

from claude_agent_sdk import tool

import plugin_registry
import plugin_store
from tool_result import _result

logger = logging.getLogger("tools")


_SECRET_CANDIDATE_MAX_QUERIES = 3
_SECRET_CANDIDATE_MAX_ITEMS = 5


def _secret_candidate_queries(name: str, unresolved: list[str]) -> list[str]:
    """The plugin name, then each distinct vendor stem of the unresolved vars
    (``GMAIL_CLIENT_ID`` → ``gmail``), deduplicated, at most three."""
    queries: list[str] = []
    for q in [name.lower()] + [v.split("_", 1)[0].lower() for v in unresolved]:
        if q and q not in queries:
            queries.append(q)
    return queries[:_SECRET_CANDIDATE_MAX_QUERIES]


def _declared_setup_provides(manifest) -> frozenset[str]:
    """The ``casa.setupProvides`` names of ``manifest`` — the manifest the
    mutation just published (#241), never the resolved snapshot, which can
    still be the old artifact during a plugin_update. #1024: those names are
    made by the plugin's own setup tool and wired from its report, so the
    exploration neither searches for them nor lists them as unresolved.

    Absent, not a mapping, or a declaration that does not parse → none: the
    exploration then runs exactly as it would with no declaration, and never
    fails the mutation (``artifact_verdict`` already refuses such an artifact;
    this is the belt). Only the exception's class is logged."""
    if not isinstance(manifest, dict):
        return frozenset()
    try:
        return frozenset(plugin_store.manifest_setup_provides(manifest))
    except Exception as exc:  # noqa: BLE001 — the exploration never fails the mutation
        logger.warning("casa.setupProvides unreadable for secret exploration "
                       "(%s) — treating it as absent", type(exc).__name__)
        return frozenset()


def _secret_candidates(name: str, required_env_vars: list[str], *,
                       manifest: dict | None = None) -> dict:
    """Explore the configured default vault for a just-installed plugin's
    unresolved secrets and return what is there — per item the query term it
    matched and its id, per field its id, its role and its type; never a
    title, a label, a section or a value (the projection is
    ``_project_item_fields``, the same one ``get_item_fields`` uses). Wires
    nothing: ``set_plugin_env_reference`` stays the deciding step, so "ask
    when not sure" is the configurator's call.

    Absent (``{}``) when there is nothing to explore for: no required var, no
    default vault, or no 1Password token. A failed ``op`` is reported as the
    classified ``op_failed`` and never fails the mutation that called this.
    Runs AFTER the registry write and reload, so it cannot delay activation.

    #1024: a name ``manifest`` declares in ``casa.setupProvides`` is neither
    searched for nor listed as unresolved (:func:`_declared_setup_provides`).
    """
    declared = _declared_setup_provides(manifest)
    unresolved = [v for v in required_env_vars
                  if v not in declared and not os.environ.get(v)]
    vault = _default_vault()
    if not unresolved or not vault or not os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
        return {}
    queries = _secret_candidate_queries(name, unresolved)
    try:
        return {"secret_candidates": _explore_vault(vault, queries, unresolved)}
    except Exception as exc:  # noqa: BLE001 — the exploration never fails the mutation
        # Terra diff r1 D2: anything the helpers did not classify (a foreign
        # exception, a non-string id) is still reported as a fixed
        # classification. Round 2 E2: only the exception's CLASS is logged;
        # its message can carry what op printed, and so can a traceback.
        logger.warning("secret exploration for %s failed (%s)", name,
                       type(exc).__name__)
        return {"secret_candidates": _op_failed(-1)}


def _explore_vault(vault: str, queries: list[str], unresolved: list[str]) -> dict:
    items: list[dict] = []
    seen: set[str] = set()
    for q in queries:
        listed = _op_item_list(q, vault)
        if "error" in listed:
            return listed
        for it in listed.get("rows", []):
            if it.get("id") in seen or len(items) >= _SECRET_CANDIDATE_MAX_ITEMS:
                continue
            seen.add(it.get("id"))
            got = _op_item_get(str(it.get("id")), vault)
            if "error" in got:
                return got
            doc = got["doc"]
            iid = it.get("id")
            items.append({"matched_query": _matched_term(it.get("title"), [q]),
                          "id": iid if isinstance(iid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", iid) else None,
                          "fields": _project_item_fields(doc)})
    return {"vault": vault, "queries": queries, "items": items,
            "unresolved": unresolved}


def _default_vault() -> str:
    """#535: the operator's configured default vault (``svc-casa/run`` exports
    the ``onepassword_default_vault`` option, "" when unset). The recipe
    doctrine promises the vault-facing tools fall back to it — without the
    fallback the configurator guessed vault names against the account."""
    return os.environ.get("ONEPASSWORD_DEFAULT_VAULT", "")


def _op_item_list(query: str, vault: str) -> dict:
    """The parsed ``op item list`` rows matching ``query`` (case-folded
    substring of the title), or a classified error dict. Rows carry titles;
    they never leave a caller unprojected: the two below drop the title, and
    :mod:`logins` keeps it for the few candidates of one login (see there)."""
    cmd = ["op", "item", "list", "--format", "json"]
    if vault:
        cmd += ["--vault", vault]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return _op_timeout()
    if r.returncode != 0:
        return _op_failed(r.returncode)
    try:
        items = json.loads(r.stdout)
        if not isinstance(items, list):
            raise ValueError("op item list did not return a list")
    except ValueError:
        return _op_unreadable()
    if query:
        items = [i for i in items if isinstance(i, dict)
                 and query.lower() in str(i.get("title", "")).lower()]
    return {"rows": items}


def _tool_list_vault_items(*, query: str = "", vault: str = "") -> dict:
    vault = vault or _default_vault()
    got = _op_item_list(query, vault)
    if "error" in got:
        return got
    rows = []
    for i in got["rows"]:
        iid = i.get("id")
        cat = i.get("category")
        # #1019: the key is `matched_query`, never `name` — the value is the
        # search term the title contained, and a reader of `name` takes it for
        # the item's name (two different items read as two items sharing one).
        rows.append({"matched_query": _matched_term(i.get("title"), [query]),
                     "id": iid if isinstance(iid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", iid) else None,
                     "category": cat if isinstance(cat, str) and re.fullmatch(r"[A-Z_]{1,40}", cat) else None})
    return {"items": rows}


def _op_item_get(item: str, vault: str) -> dict:
    """The parsed ``op item get`` document, or a classified error dict. The
    document holds VALUES; it never leaves a caller unprojected (the two
    below, and :mod:`logins`, which looks only at labels and emptiness)."""
    cmd = ["op", "item", "get", item, "--format", "json"]
    if vault:
        cmd += ["--vault", vault]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return _op_timeout()
    if r.returncode != 0:
        return _op_failed(r.returncode)
    try:
        doc = json.loads(r.stdout)
        if not isinstance(doc, dict):
            raise ValueError("op item get did not return an object")
    except ValueError:
        return _op_unreadable()
    return {"doc": doc}


def _tool_get_item_fields(*, item: str, vault: str = "") -> dict:
    vault = vault or _default_vault()
    got = _op_item_get(item, vault)
    if "error" in got:
        return got
    return {"fields": _project_item_fields(got["doc"])}


def _op_failed(exit_code: int) -> dict:
    """A classified ``op`` failure. NEVER the subprocess's stderr or stdout:
    whatever ``op`` prints on failure lands in the configurator's transcript
    and from there in Telegram, and truncation is not redaction."""
    return {"error": "op_failed", "exit_code": int(exit_code)}


def _op_timeout() -> dict:
    """``op`` did not return within its budget (fixed string, no output)."""
    return {"error": "op_timeout"}


def _op_unreadable() -> dict:
    """``op`` exited 0 but its stdout was not the JSON document expected — a
    fixed string; the unparseable bytes are never forwarded."""
    return {"error": "op_unreadable"}


# Casa never repeats an operator-typed vault string. Three review rounds
# tried to decide which metadata strings were safe to echo by comparing them
# against where secrets live (field values; then every string in the
# document; then also by shape) and each round found a string that got
# through: a value duplicated into a label, a value split across labels, a
# plain-shaped secret typed as the label itself, and — the other way — an
# ordinary label withheld because the item's notes mentioned it. The mechanism
# is cut. What is returned about an item is: its op-generated id, and for each
# field its op-generated id, its type, and a ROLE from the closed set below,
# derived in-process from the label (which is never returned). In place of a
# title, the query term the item matched. A wiring reference is
# ``op://<vault>/<item id>/<field id>``, which the resolver accepts.
# The tools of this module never return a title. One other tool does, and
# only that: ``connect_login`` shows the resident assistant, in the
# operator's own chat, the titles of the few items that match a login's
# search terms (:func:`logins.candidates`), still without a label or a value.
_FIELD_ROLES = (
    # (regex over the case-folded, whitespace-normalised label, role)
    (re.compile(r"^(oauth[ -]?)?client[ _-]?id$"), "client_id"),
    (re.compile(r"^(oauth[ -]?)?client[ _-]?secret$"), "client_secret"),
    (re.compile(r"^refresh[ _-]?token$"), "refresh_token"),
    (re.compile(r"^access[ _-]?token$"), "access_token"),
    (re.compile(r"^(api[ _-]?)?(token|bearer( token)?)$"), "token"),
    (re.compile(r"^(api[ _-]?)?(key|api key|apikey|secret[ _-]?key)$"), "api_key"),
    (re.compile(r"^(client|app|application|consumer)[ _-]?(key|secret)$"), "api_key"),
    (re.compile(r"^(secret|credential|credentials)$"), "credential"),
    (re.compile(r"^(user ?name|user|login|account[ _-]?name)$"), "username"),
    (re.compile(r"^(pass ?word|pass|pin|passphrase)$"), "password"),
    (re.compile(r"^(e[ -]?mail|e[ -]?mail address|mail|user[ _-]?email)$"), "email"),
    (re.compile(r"^(host ?name|host|server|domain)$"), "hostname"),
    (re.compile(r"^(url|uri|endpoint|base[ _-]?url|website|address)$"), "url"),
    (re.compile(r"^(account|account[ _-]?id|tenant|tenant[ _-]?id|org|organi[sz]ation|project|project[ _-]?id)$"), "account"),
    (re.compile(r"^(region|zone|environment|env)$"), "region"),
    # 1Password's own templates (API Credential, Login, Database, Server) and
    # the plugins Casa ships today (finance's BANKFEED_OP_VAULT → "Vault").
    (re.compile(r"^(vault|vault[ _-]?name|database|db|schema|port|type|connection[ _-]?options|"
                r"phone|phone number|number|chat[ _-]?id|bot[ _-]?token|webhook[ _-]?secret|"
                r"voice[ _-]?id|model|one[ _-]?time[ _-]?password|otp|notes?)$"), "other_known"),
)
_FIELD_TYPES = frozenset({"STRING", "CONCEALED", "EMAIL", "URL", "OTP", "DATE",
                          "MONTH_YEAR", "MENU", "PHONE", "ADDRESS", "REFERENCE",
                          "FILE", "SSHKEY", "CREDIT_CARD_NUMBER", "CREDIT_CARD_TYPE",
                          "GENDER", "UNKNOWN"})


def _field_role(label: object) -> str | None:
    """The fixed role a field label denotes, or None. The LABEL never leaves
    this function; a role is one of a closed set of Casa-chosen tokens, so
    nothing operator-typed reaches the result and nothing is compared against
    the document. An unrecognised label is a field the configurator asks the
    operator about, by the field's id."""
    t = label if isinstance(label, str) else ""
    t = re.sub(r"\s+", " ", t.strip().lower())
    if not t:
        return None
    for rx, role in _FIELD_ROLES:
        if rx.match(t):
            return role
    return None


def _matched_term(title: object, queries: list[str]) -> str:
    """The query term an item's title matched, never the title. Case-folded
    substring match, the same rule the listing filters by; the first matching
    query wins; an item that matched none reads ``(unnamed)``."""
    t = title.lower() if isinstance(title, str) else ""
    for q in queries:
        if q and q.lower() in t:
            return q.lower()
    return "(unnamed)"


def _project_item_fields(item: dict) -> list[dict]:
    """The ONE projection of an ``op item get`` document: per field its
    op-generated ``id``, its ``role`` (a closed set derived from the label,
    which is never returned) and its ``type`` (validated against op's own
    enum, else None). Never a label, a section, a value, a reference or a
    note. Shared by ``get_item_fields`` and ``plugin_add``'s
    ``secret_candidates`` so there is a single parser to audit for disclosure."""
    out: list[dict] = []
    for f in (item or {}).get("fields", []) or []:
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        ftype = f.get("type")
        out.append({
            "id": fid if isinstance(fid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fid) else None,
            "role": _field_role(f.get("label")),
            "type": ftype if isinstance(ftype, str) and ftype in _FIELD_TYPES else None,
        })
    return out


@tool(
    "list_vault_items",
    "List 1Password vault items whose title contains the query: each as "
    "matched_query (the search term its title contained — not the item's "
    "name; the title is never returned), its op item id and its category. "
    "An omitted vault falls back to the configured "
    "onepassword_default_vault.",
    # An explicit JSON Schema: the shorthand {key: type} form marks EVERY key
    # required, so the omitted-vault call the description promises was rejected
    # by the MCP input validator before the handler's #535 fallback could run
    # (measured on the production box, 2026-09-15). `query` stays required — the recipe's
    # "never enumerate the whole vault" is enforced here, not in prose.
    {"type": "object",
     "properties": {"query": {"type": "string"},
                    "vault": {"type": "string"}},
     "required": ["query"]},
)
async def list_vault_items(args: dict) -> dict:
    return _result(await asyncio.to_thread(
        _tool_list_vault_items,
        query=args.get("query", ""),
        vault=args.get("vault", ""),
    ))


@tool(
    "get_item_fields",
    "Get a 1Password item's fields as op field ids, roles (client_id, "
    "client_secret, api_key, token, email, username, password, hostname, url, "
    "account, ...) and types. Never returns labels, sections or values; wire "
    "with op://<vault>/<item id>/<field id>. An omitted vault falls back to "
    "the configured onepassword_default_vault.",
    # Explicit schema for the same reason as list_vault_items: `vault` optional.
    {"type": "object",
     "properties": {"item": {"type": "string"},
                    "vault": {"type": "string"}},
     "required": ["item"]},
)
async def get_item_fields(args: dict) -> dict:
    return _result(await asyncio.to_thread(
        _tool_get_item_fields,
        item=args["item"],
        vault=args.get("vault", ""),
    ))


# #1047: the vault drop-off — the one vault WRITE an agent holds. It writes
# only the Casa-titled item of a drop-off the named plugin declares
# (vault_drop_off.item_title), and returns, logs and raises nothing that
# carries the value.
_DROP_OFF_STORED_NOTE = (
    "Stored. Tell the agent that runs this plugin only that it is waiting, "
    "and ask it to run its step now — never the value, in no brief, reply "
    "or summary.")


def _drop_off_target(plugin: str) -> "tuple[str | None, list[str], dict]":
    """``(runtime name, declared drop-offs, error)`` for *plugin*, matched
    against the current resolution by registry name or runtime name — the
    name the plugin itself knows is its manifest name, which is what its
    setup text tells an agent."""
    from plugin_store import StoreError, manifest_drop_offs
    resolution = plugin_registry.resolve_all()
    hits = [rp for rp in getattr(resolution, "plugins", None) or []
            if plugin in (rp.name, plugin_registry.runtime_name(rp))]
    if len(hits) != 1:
        return None, [], {"error": "unknown_plugin" if not hits
                          else "ambiguous_plugin"}
    rp = hits[0]
    try:
        declared = manifest_drop_offs(rp.manifest)
    except StoreError:
        declared = []
    return plugin_registry.runtime_name(rp), declared, {}


def _tool_vault_drop_off(*, plugin: str, drop_off: str, value: object) -> dict:
    import vault_drop_off
    runtime, declared, err = _drop_off_target(plugin)
    if err:
        return err
    if drop_off not in declared:
        return {"error": "unknown_drop_off", "declared": declared}
    if not vault_drop_off.valid_value(value):
        return {"error": "invalid_value"}
    out = vault_drop_off.store(runtime, drop_off, value)  # type: ignore[arg-type]
    if out.get("status") == "ok":
        logger.info("vault drop-off stored: plugin=%s drop_off=%s",
                    runtime, drop_off)
        return {"status": "ok", "plugin": runtime, "drop_off": drop_off,
                "note": _DROP_OFF_STORED_NOTE}
    logger.warning("vault drop-off failed: plugin=%s drop_off=%s kind=%s",
                   runtime, drop_off, out.get("error"))
    return out


@tool(
    "vault_drop_off",
    "Store a sign-in link or one-time code in the vault drop-off a plugin "
    "declares, so the plugin redeems it itself and the value never travels "
    "in a delegation brief. Use it for a link you read from a mailbox at the "
    "operator's request or one the operator pasted to you. Writes only that "
    "drop-off; returns no value. Afterwards tell the plugin's agent only that "
    "it is waiting.",
    {"type": "object",
     "properties": {"plugin": {"type": "string"},
                    "drop_off": {"type": "string"},
                    "value": {"type": "string"}},
     "required": ["plugin", "drop_off", "value"]},
)
async def vault_drop_off(args: dict) -> dict:
    return _result(await asyncio.to_thread(
        _tool_vault_drop_off,
        plugin=str(args.get("plugin") or ""),
        drop_off=str(args.get("drop_off") or ""),
        value=args.get("value"),
    ))
