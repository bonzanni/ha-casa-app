"""Shared helpers for tests that drive the install and upgrade library
directly: the values an operator's approval freezes, and the consent ledger
entry the card records for them.

An approval binds the exact settings the library will stage. For a specialist
those are ``{"config": <the merged non-secret settings>, "secrets": <the sorted
secret names provided>}``; a persona has none, so its values are ``{}``. The
library refuses unless the ledger holds the identity built from those values and
its call's ``approved_values`` equal them."""
from __future__ import annotations

import contextlib
from collections.abc import Iterable, Mapping
from typing import Any

from approval_records import values_digest
from persona_install import persona_install_consent_identity
from specialist_install_consent import install_consent_identity

PERSONA_VALUES: dict = {}
PERSONA_DIGEST = values_digest(PERSONA_VALUES)


def settings(config: "Mapping[str, Any] | None" = None,
             secrets: Iterable[str] = ()) -> dict:
    """The values of a specialist approval for these staged settings."""
    return {"config": dict(config or {}), "secrets": sorted(secrets)}


NO_SETTINGS = settings()
NO_SETTINGS_DIGEST = values_digest(NO_SETTINGS)


def specialist_identity(inspection: Any = None, *, values: "dict | None" = None,
                        **fields: str) -> str:
    """The consent identity of an inspection (or of explicit *fields*:
    component_id, version, root_digest, slug, receipt_digest) approved with
    *values* (no settings when omitted)."""
    vals = NO_SETTINGS if values is None else values
    if inspection is not None:
        fields = dict(component_id=inspection.component_id, version=inspection.version,
                      root_digest=inspection.root_digest, slug=inspection.slug,
                      receipt_digest=getattr(inspection, "receipt_digest", "") or "",
                      **fields)
    return install_consent_identity(values_digest=values_digest(vals), **fields)


def record_specialist_ack(acks: Any, inspection: Any, values: "dict | None" = None) -> dict:
    """Record the operator's approval of *inspection* with *values* (no
    settings when omitted) as the consent card does; returns the values, to be
    passed on as the library call's ``approved_values``."""
    vals = NO_SETTINGS if values is None else values
    acks.record(identity=specialist_identity(inspection, values=vals),
                component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug,
                receipt_digest=getattr(inspection, "receipt_digest", "") or "",
                values_digest=values_digest(vals))
    return vals


def persona_identity(*, persona_id: str, version: str, checksum: str) -> str:
    """The consent identity of a persona install (a persona has no settings)."""
    return persona_install_consent_identity(persona_id=persona_id, version=version,
                                            checksum=checksum, values_digest=PERSONA_DIGEST)


def use_tmp_approval_store(monkeypatch: Any, tmp_path: Any) -> None:
    """Approval records under *tmp_path*, never /data, with a fresh plugin
    tools lock (a request and an attempt run under it)."""
    import asyncio

    import approval_records
    import tools as tools_mod
    monkeypatch.setattr(approval_records, "PATH", str(tmp_path / "approval-records.json"))
    approval_records.reset()
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())


@contextlib.asynccontextmanager
async def approved_run(tool: Any, args: dict, *, acks: Any = None,
                       values: "dict | None" = None, specialists_dir: Any = None,
                       origin: "dict | None" = None):
    """Inside this block, calls of the governed specialist *tool*
    (``specialist_install_commit`` or ``specialist_upgrade``) run as the run
    Casa starts after the operator approved *args*: an approved, current
    approval record for the inspection those args name, named on the run's
    origin. One approval covers one attempt, so a second governed call inside
    the block is refused as used.

    The values approved are *values* when given; else the settings the call
    will stage, computed by ``preview_effective_config`` when *specialists_dir*
    is given, else the call's own config and secret names. With *acks* the
    consent ledger entry Approve records is written too. Args the tool would
    refuse get no approval, so the tool's own refusal is what a call returns."""
    from pathlib import Path

    import agent as agent_mod
    import approval_records
    import specialist_install
    import tools as tools_mod

    kind = "specialist_upgrade" if tool is tools_mod.specialist_upgrade else "specialist_install"
    staged_dir = Path(str(args.get("staged_dir") or ""))
    checked = specialist_install.validate_resume_inputs(
        staged_dir=staged_dir, receipt_id=args.get("receipt_id"),
        root_digest=args.get("root_digest"))
    run_origin = dict(origin or {"role": "assistant", "user_text": "go ahead"})
    if checked.ok:
        inspection = tools_mod._inspection_from_checked(checked, staged_dir)
        secrets = sorted(args.get("secret_names_provided") or ())
        if values is None:
            config = dict(args.get("config") or {})
            if specialists_dir is not None:
                config = specialist_install.preview_effective_config(
                    kind, inspection, slug=args["slug"], config=config,
                    secret_names_provided=secrets, specialists_dir=specialists_dir)
            values = settings(config, secrets)
        if acks is not None:
            record_specialist_ack(acks, inspection, values)
        async with tools_mod._plugin_tools_guard():
            record, _, _ = approval_records.request(
                kind, f"{inspection.component_id}:{inspection.slug}",
                specialist_identity(inspection, values=values), values)
        record = approval_records.approve(record.rid)
        run_origin[approval_records.APPROVAL_KEY] = {"rid": record.rid,
                                                     "generation": record.generation}
    token = agent_mod.origin_var.set(run_origin)
    try:
        yield
    finally:
        agent_mod.origin_var.reset(token)


async def call_as_approved_run(tool: Any, args: dict, **kw: Any) -> dict:
    """One call of *tool* with *args* inside its own :func:`approved_run`."""
    async with approved_run(tool, args, **kw):
        return await tool.handler(args)
