"""Trusted source receipt for a specialist install/upgrade (spec §3.2.1,
Task 8). Captures where the component AND every sourced plugin dependency's
bytes came from (repo/ref/resolved-revision/subdir) plus a content
attestation per plugin — so commit (Task 9) can publish exactly these
attested bytes, and consent (Task 7) can bind the whole closure's provenance
into its identity via ``receipt_digest``.

Trust boundary (spec §3.2.1, Sol r4): the receipt is persisted server-side as
opaque state keyed by an opaque ``receipt_id`` (``uuid4().hex``) under
``receipts_dir``. A commit tool accepts ONLY the receipt id — never
caller-supplied coordinates — so ``load`` re-derives and verifies the digest
on every read; a tampered or hand-edited sidecar file fails closed (returns
``None``), never silently trusted.

``component_staged_path`` (and each row's ``staged_path``) is NON-attested
runtime state — it points at a staging directory that may not survive a
crash/restart — so it is EXCLUDED from ``receipt_digest``. Terra plan-r2: if
it vanished, the commit path re-fetches the component (``resolve_and_fetch``
with the receipt's own ``component_repo``/``component_ref``/
``component_subdir`` and ``expected_revision=component_revision``), rebuilds
``.dep-plugins`` from the attested per-row coordinates, and re-verifies BOTH
the root digest and every attested content digest before proceeding — any
mismatch is the caller's ``receipt_drift`` to raise (Task 9 concern, not
this module's)."""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from canonical_bytes import checksum_json

logger = logging.getLogger(__name__)

DEFAULT_RECEIPTS_DIR = Path("/config/specialists/.receipts")

_RECEIPT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class PluginReceiptRow:
    identifier: str
    scoped_name: str
    manifest_name: str
    version: str
    source_type: str
    repo: str
    ref: str
    revision: str
    subdir: str
    content_digest: str
    staged_path: str  # NON-attested runtime state — excluded from receipt_digest
    # Task 8 fix-round-1 (consent-review CRITICAL, spec §3.2): the surfaces the
    # consent DM must enumerate per plugin — "scoped name, manifest name,
    # version, MCP servers/commands, protected tools, secrets surface". ALL
    # THREE are attested (included in ``compute_receipt_digest`` via
    # ``_attested_row``'s plain ``asdict`` — no exclusion added for them)
    # because they describe exactly what the operator's consent tap approves;
    # a tampered mcp_servers/protected_tools/env_names list must invalidate
    # the receipt digest the same way a tampered digest/repo/ref would.
    mcp_servers: tuple[str, ...] = ()      # one-line "name: command arg1 arg2…" per server
    protected_tools: tuple[str, ...] = ()  # names, from plugin_store.manifest_protected_tools
    env_names: tuple[str, ...] = ()        # required env var names — the secrets surface


@dataclass(frozen=True, slots=True)
class SourceReceipt:
    receipt_id: str
    slug: str
    component_repo: str
    component_ref: str
    component_revision: str
    component_subdir: str
    component_staged_path: str  # NON-attested runtime state
    plugins: tuple[PluginReceiptRow, ...]
    receipt_digest: str


def _attested_row(row: PluginReceiptRow) -> dict:
    d = asdict(row)
    del d["staged_path"]
    return d


def compute_receipt_digest(
    *, slug: str, component_repo: str, component_ref: str, component_revision: str,
    component_subdir: str, plugins: "tuple[PluginReceiptRow, ...]",
) -> str:
    """Digest over canonical JSON of everything EXCEPT ``receipt_id`` and any
    ``staged_path`` (component-level or per-row) — those are non-attested
    runtime state (Terra plan-r2). Rows are sorted by ``identifier`` so
    build order never perturbs the digest."""
    payload = {
        "slug": slug,
        "component_repo": component_repo,
        "component_ref": component_ref,
        "component_revision": component_revision,
        "component_subdir": component_subdir,
        "plugins": [_attested_row(r) for r in sorted(plugins, key=lambda r: r.identifier)],
    }
    return checksum_json(payload)


def build_receipt(
    *, slug: str, component_repo: str, component_ref: str, component_revision: str,
    component_subdir: str, component_staged_path: str,
    plugins: "tuple[PluginReceiptRow, ...]",
) -> SourceReceipt:
    """Mint a fresh receipt (new opaque ``receipt_id``) with its digest
    computed over the attested fields. Every inspect issues one of these —
    also for plugin-less components (spec §3.2.1) — so ``plugins`` may be
    empty; ``receipt_digest`` is still always non-empty."""
    digest = compute_receipt_digest(
        slug=slug, component_repo=component_repo, component_ref=component_ref,
        component_revision=component_revision, component_subdir=component_subdir,
        plugins=plugins,
    )
    return SourceReceipt(
        receipt_id=uuid.uuid4().hex, slug=slug, component_repo=component_repo,
        component_ref=component_ref, component_revision=component_revision,
        component_subdir=component_subdir, component_staged_path=component_staged_path,
        plugins=tuple(plugins), receipt_digest=digest,
    )


def _is_opaque_receipt_id(receipt_id: object) -> bool:
    return isinstance(receipt_id, str) and _RECEIPT_ID_RE.fullmatch(receipt_id) is not None


def _to_json(receipt: SourceReceipt) -> dict:
    payload = asdict(receipt)
    payload["plugins"] = [asdict(r) for r in receipt.plugins]
    return payload


def _from_json(raw: dict) -> "SourceReceipt | None":
    try:
        rows = tuple(
            PluginReceiptRow(
                identifier=r["identifier"], scoped_name=r["scoped_name"],
                manifest_name=r["manifest_name"], version=r["version"],
                source_type=r["source_type"], repo=r["repo"], ref=r["ref"],
                revision=r["revision"], subdir=r["subdir"],
                content_digest=r["content_digest"], staged_path=r["staged_path"],
                # Task 8 fix-round-1: tolerate a sidecar written before these
                # fields existed (`.get(..., ())`) rather than fail closed on
                # every already-staged receipt — the digest recompute below
                # still fails closed on any genuine tamper.
                mcp_servers=tuple(r.get("mcp_servers") or ()),
                protected_tools=tuple(r.get("protected_tools") or ()),
                env_names=tuple(r.get("env_names") or ()),
            )
            for r in raw["plugins"]
        )
        receipt = SourceReceipt(
            receipt_id=raw["receipt_id"], slug=raw["slug"],
            component_repo=raw["component_repo"], component_ref=raw["component_ref"],
            component_revision=raw["component_revision"],
            component_subdir=raw["component_subdir"],
            component_staged_path=raw["component_staged_path"],
            plugins=rows, receipt_digest=raw["receipt_digest"],
        )
    except (KeyError, TypeError):
        return None
    if not _is_opaque_receipt_id(receipt.receipt_id):
        return None
    # Fail-closed: recompute the digest from the loaded (attested) fields and
    # compare — a tampered or hand-edited sidecar must never load as valid.
    expected = compute_receipt_digest(
        slug=receipt.slug, component_repo=receipt.component_repo,
        component_ref=receipt.component_ref, component_revision=receipt.component_revision,
        component_subdir=receipt.component_subdir, plugins=receipt.plugins,
    )
    if expected != receipt.receipt_digest:
        return None
    return receipt


def persist(receipt: SourceReceipt, receipts_dir: Path = DEFAULT_RECEIPTS_DIR) -> None:
    from atomic_io import atomic_write_text

    if not _is_opaque_receipt_id(receipt.receipt_id):
        raise ValueError(f"refusing to persist a non-opaque receipt_id {receipt.receipt_id!r}")
    receipts_dir = Path(receipts_dir)
    receipts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = receipts_dir / f"{receipt.receipt_id}.json"
    atomic_write_text(path, json.dumps(_to_json(receipt), indent=2, sort_keys=True) + "\n", mode=0o600)


def delete(receipt_id: str, receipts_dir: Path = DEFAULT_RECEIPTS_DIR) -> bool:
    """Whole-branch N: prune a CONSUMED receipt sidecar (the commit/upgrade tool
    calls this after its journal completes). Fails closed on a non-opaque id
    (never joins an unvalidated id into a path). Returns True if a file was
    removed."""
    if not _is_opaque_receipt_id(receipt_id):
        return False
    path = Path(receipts_dir) / f"{receipt_id}.json"
    try:
        path.unlink()
        return True
    except OSError:
        return False


def sweep_aged(*, receipts_dir: Path = DEFAULT_RECEIPTS_DIR,
               max_age_s: float = 7 * 24 * 3600, now: "float | None" = None,
               keep_slugs: "frozenset[str] | set[str]" = frozenset(),
               keep_receipt_ids: "frozenset[str] | set[str]" = frozenset()) -> int:
    """Whole-branch N: boot-time age sweep — delete receipt sidecars older than
    `max_age_s` (default 7 days). An inspect that never reached commit (operator
    denied, or the flow was abandoned) leaves an orphan receipt behind; without
    this they accumulate unbounded. Never raises; returns the count removed.

    ``keep_slugs`` (#331, Sol r5-2 / Terra r6-1): slugs with a LIVE
    pending-configuration candidate — a receipt is required by the supported
    configure re-commit, so age alone must never delete the last one (a
    pending install older than the cutoff plus one restart became
    permanently unfinishable). Exactly the NEWEST receipt per kept slug is
    exempt — not every receipt naming the slug, or repeated pre-commit
    inspections would pin unbounded staging forever; any receipt for the
    same root resumes the flow, so one suffices. Unparseable receipts still
    sweep.

    #929: the documented resume is a second commit with the RETAINED
    receipt — the one this exemption keeps — never a re-inspect, which
    refuses the occupied slug. Which is why sweeping the last usable receipt
    for a live pending candidate ends the install rather than delaying it.

    ``keep_receipt_ids`` (Sol r6-2): the receipts pending candidates were
    actually committed with (recorded durably at pending time) — exempted by
    exact id, since a same-slug receipt for a DIFFERENT root cannot resume a
    pending tuple and newest-by-mtime alone could keep the wrong one.
    ``keep_slugs`` is the fallback for pending slugs with no readable
    marker."""
    import time as _time

    receipts_dir = Path(receipts_dir)
    if not receipts_dir.is_dir():
        return 0
    cutoff = (now if now is not None else _time.time()) - max_age_s
    rows: list[tuple[Path, float]] = []
    for path in receipts_dir.iterdir():
        if not path.is_file() or path.suffix != ".json":
            continue
        try:
            rows.append((path, path.stat().st_mtime))
        except OSError:
            continue
    keep_newest: set[Path] = set()
    if keep_slugs:
        newest_by_slug: dict[str, tuple[float, Path]] = {}
        for path, mtime in rows:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            slug = raw.get("slug") if isinstance(raw, dict) else None
            if isinstance(slug, str) and slug in keep_slugs:
                best = newest_by_slug.get(slug)
                if best is None or mtime > best[0]:
                    newest_by_slug[slug] = (mtime, path)
        keep_newest = {p for _, p in newest_by_slug.values()}
    removed = 0
    for path, mtime in rows:
        if mtime >= cutoff or path in keep_newest:
            continue
        if keep_receipt_ids and path.stem in keep_receipt_ids:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def load(receipt_id: str, receipts_dir: Path = DEFAULT_RECEIPTS_DIR) -> "SourceReceipt | None":
    """Opaque-id lookup — never accepts caller-supplied coordinates. Fails
    closed (returns ``None``) on a missing/malformed/tampered sidecar or a
    non-opaque ``receipt_id`` shape (never joins an unvalidated id into a
    filesystem path)."""
    if not _is_opaque_receipt_id(receipt_id):
        return None
    path = Path(receipts_dir) / f"{receipt_id}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return _from_json(raw)
