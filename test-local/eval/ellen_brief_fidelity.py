#!/usr/bin/env python3
"""Ellen brief-fidelity eval (W3/Sol B11) — pins the invoice_reset mistranslation.

Live-model gate: drives Ellen's REAL composed system prompt (loaded the same
way ``casa_core.py`` boots her — ``agent_loader.load_agent_from_dir`` +
``policies.load_policies``) through ONE turn of the exact CASE user message
that motivated this release, and asserts the ``engage_executor`` call she
emits carries the user's process instructions VERBATIM in
``brief.process_requirements`` (never paraphrased into a feature
requirement) with ``brief.interaction_required = True``.

The ``engage_executor`` tool exposed to the model here is a side-effect-free
FAKE — it captures ``args`` instead of starting a real engagement — but its
``@tool`` description + JSON Schema are the PRODUCTION contract imported
from ``tools.py`` (``tools.engage_executor.description`` /
``.input_schema``, the same ``SdkMcpTool`` the real ``CASA_TOOLS`` registry
exposes), so the model sees exactly what it sees in prod.

Requires a live model (real SDK subprocess, real Anthropic call) — this is
NOT part of the pytest unit gate. It is a REQUIRED pre-merge command
(Task 11), run inside the deployed container:

    cat test-local/eval/ellen_brief_fidelity.py \\
        | ssh n150-ha -- sudo -n docker exec -i addon_91d4d4c8_casa python3 -

Exit 0 = PASS (doctrine held), exit 1 = FAIL (diff printed).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import traceback

sys.path.insert(0, "/opt/casa")

from agent_loader import load_agent_from_dir  # noqa: E402
from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
    tool,
)
from claude_runtime import CLAUDE_CLI_PATH  # noqa: E402
from config import effort_for  # noqa: E402
from policies import load_policies  # noqa: E402
import tools as casa_tools  # noqa: E402 — production engage_executor contract

# The exact-substring contract [Sol B11] — no weakening; a paraphrase into a
# feature requirement must FAIL. This is the live reproduction of the
# invoice_reset mistranslation that motivated the brief-envelope doctrine.
CASE = {
    "user_message": (
        "Build invoice_reset for the finance plugin, but engage with "
        "superpowers and I expect a discussion with the implementer "
        "before any code."
    ),
    "must_substrings": ["superpowers", "discussion with the implementer"],
    "must_interaction_required": True,
}


def _build_fake_engage_executor(captured: list[dict]):
    """A capturing stand-in for the real ``engage_executor`` MCP tool.

    Reuses the PRODUCTION ``@tool`` description + input schema registered on
    ``tools.engage_executor`` (an ``SdkMcpTool`` — ``tool()`` decorates the
    handler and returns that dataclass) so the model is shown the identical
    contract it sees in a real engagement. No real engagement is ever
    started — the handler only appends ``args`` to ``captured`` and returns
    a canned stub result.
    """
    production = casa_tools.engage_executor

    @tool(production.name, production.description, production.input_schema)
    async def fake_engage_executor(args: dict) -> dict:
        captured.append(args)
        return {
            "content": [
                {"type": "text", "text": "[engagement started — test stub]"}
            ]
        }

    return fake_engage_executor


def _diff_report(captured: list[dict]) -> str:
    lines = [f"captured {len(captured)} engage_executor call(s):"]
    for i, call in enumerate(captured):
        lines.append(f"  [{i}] {call!r}")
    if not captured:
        lines.append("  (none — Ellen never called engage_executor)")
    return "\n".join(lines)


def _seed_container_env(*names: str) -> None:
    """Seed env vars from s6-overlay's container_environment directory.

    ``docker exec`` shells do not inherit the ``with-contenv`` environment the
    supervised casa-main process runs under; the values live as one-file-per-var
    under /run/s6/container_environment/ (e.g. CLAUDE_CODE_OAUTH_TOKEN — without
    it the SDK subprocess reports "Not logged in").
    """
    for name in names:
        if os.environ.get(name):
            continue
        try:
            with open(f"/run/s6/container_environment/{name}",
                      encoding="utf-8") as fh:
                os.environ[name] = fh.read().strip("\n\x00")
        except OSError:
            pass


async def _run() -> bool:
    _seed_container_env("CLAUDE_CODE_OAUTH_TOKEN")
    policies = load_policies("/config/policies/disclosure.yaml")
    cfg = load_agent_from_dir("/config/agents/assistant", policies=policies)

    # runtime.yaml carries a DEFERRED "${PRIMARY_AGENT_MODEL}" placeholder that
    # boot resolves from the svc-casa run script's env (bashio option export,
    # s6-rc.d/svc-casa/run:44). A `docker exec` shell does not inherit that
    # env, so resolve it here the same way boot does: seed the var from
    # /data/options.json, then apply the loader's own _substitute_env +
    # resolve_model.
    model = cfg.model
    if "${" in model:
        from config import _substitute_env, resolve_model
        try:
            with open("/data/options.json", encoding="utf-8") as fh:
                _opts = json.load(fh)
            os.environ.setdefault(
                "PRIMARY_AGENT_MODEL", str(_opts.get("primary_agent_model", "")))
        except OSError:
            pass
        model = resolve_model(_substitute_env(model))
    print(f"loaded assistant config: model={model!r}")

    captured: list[dict] = []
    fake_tool = _build_fake_engage_executor(captured)
    server = create_sdk_mcp_server(name="casa-framework", tools=[fake_tool])

    # Side-effect isolation (Sol r4-B7): `allowed_tools` only AUTO-APPROVES —
    # it does not restrict the tool surface. `tools=[]` disables every
    # built-in tool, `strict_mcp_config=True` ignores any ambient
    # .mcp.json / project MCP config, `setting_sources=[]` loads no
    # filesystem settings, and `cwd` is an isolated tempdir — so the ONLY
    # tool the model can reach is the fake engage_executor above.
    opts = ClaudeAgentOptions(
        model=model,
        effort=effort_for(model),
        cli_path=CLAUDE_CLI_PATH,
        system_prompt=cfg.system_prompt,
        mcp_servers={"casa-framework": server},
        tools=[],
        allowed_tools=["mcp__casa-framework__engage_executor"],
        strict_mcp_config=True,
        setting_sources=[],
        skills=[],
        cwd=tempfile.mkdtemp(),
        max_turns=2,
    )

    async with ClaudeSDKClient(opts) as client:
        await client.query(CASE["user_message"])
        async for _msg in client.receive_response():
            pass

    ok = True
    if len(captured) != 1:
        print(
            f"FAIL: expected exactly 1 engage_executor call, got "
            f"{len(captured)}"
        )
        print(_diff_report(captured))
        return False

    call = captured[0]
    brief = call.get("brief") or {}

    got_interaction = brief.get("interaction_required")
    if got_interaction is not CASE["must_interaction_required"]:
        print(
            f"FAIL: brief.interaction_required must be "
            f"{CASE['must_interaction_required']!r}, got {got_interaction!r}"
        )
        ok = False

    process_requirements = brief.get("process_requirements") or []
    for must in CASE["must_substrings"]:
        if not any(must in p for p in process_requirements):
            print(
                f"FAIL: {must!r} not found VERBATIM in any "
                f"brief.process_requirements entry: {process_requirements!r}"
            )
            ok = False

    if not ok:
        print()
        print("=== full captured call (diff aid) ===")
        print(_diff_report(captured))

    return ok


def main() -> int:
    print("=== Ellen brief-fidelity eval (W3/Sol B11) ===")
    try:
        ok = asyncio.run(_run())
    except Exception:  # noqa: BLE001 — surface the failure, don't hang the gate
        traceback.print_exc()
        ok = False
    print("VERDICT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
