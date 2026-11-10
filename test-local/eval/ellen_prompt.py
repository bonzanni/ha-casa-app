"""The resident's real compiled system prompt and one model turn, shared by
the live-model evals in this folder."""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", "casa", "rootfs", "opt", "casa"))


def _paths(local: bool) -> tuple[str, str]:
    if local:
        d = os.path.join(LOCAL_ROOT, "defaults")
        return (os.path.join(d, "agents", "assistant"),
                os.path.join(d, "policies", "disclosure.yaml"))
    return ("/config/agents/assistant", "/config/policies/disclosure.yaml")


def _system_prompt(local: bool, voice: bool = False) -> tuple[str, str]:
    from agent_loader import load_agent_from_dir
    from policies import load_policies
    from prompt_compiler import projection_for
    agent_dir, policy = _paths(local)
    cfg = load_agent_from_dir(agent_dir, policies=load_policies(policy),
                              binding_commit=False)
    bundle = cfg.compiled_prompt_bundle
    if bundle is None:
        raise SystemExit("no compiled bundle")
    if voice:
        base = projection_for(bundle, channel="voice", origin_route="voice").system_prompt
        channel = ("\n<channel_context>\nchannel: voice\ntrust: authenticated"
                   "\n</channel_context>")
    else:
        base = projection_for(bundle, channel="telegram", origin_route="telegram").system_prompt
        channel = ("\n<channel_context>\nchannel: telegram\ntrust: authenticated "
                   "(operator)\n</channel_context>")
    from config import resolve_model
    return base + channel, resolve_model(cfg.model)


async def _turn(client, text: str) -> str:
    from claude_agent_sdk import AssistantMessage, TextBlock
    from datetime import datetime
    from timekeeping import compose_time_envelope, resolve_tz
    await client.query(compose_time_envelope(datetime.now(resolve_tz())) + text)
    out: list[str] = []
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    out.append(b.text)
    return "\n".join(out)
