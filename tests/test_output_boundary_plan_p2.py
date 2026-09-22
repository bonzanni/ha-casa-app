"""Plan round 2 findings on R1 (#1038), each pinned before it was fixed:

* a disclosure head at or over the platform limit drove the failed-head overflow
  path into an endless loop (Terra S1, Astra S1): the line is bounded at its source —
  a persona name over 64 characters names the role instead, the rule the
  authorization challenge headline already applies;
* the caption cap cut the line while the tool result reported it delivered
  (Astra S1): the cap falls on the model's body, never on the line;
* the prefixed overflow re-split a page's plain form at the budget and could cut a
  destination across units (Astra S1) — the prefixed-unit mechanism was then cut in
  plan round 3 (`test_output_boundary_plan_p3.py`); the splitter and destination pins
  of this round went with it.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import output_boundary as ob
import tools
from output_boundary import IntentKind as K
from output_boundary_testing import scope as _scope

pytestmark = [pytest.mark.unit]

INVOICE = ("/data/agent-inbox/assistant/ready/1758500000000-a1b2.pdf", "invoice.pdf")


# ---------------------------------------------------------------------------
# The head is bounded at its source (the splitter and overflow pins of this
# round pinned the prefixed-unit mechanism, cut in plan round 3 — see _plan_p3)
# ---------------------------------------------------------------------------

def test_a_persona_name_over_the_bound_names_the_role_instead():
    over = _scope(role="assistant", name="E" * 65)
    over.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    assert over.admit(K.FINAL_REPLY, "hi").annotations == (
        "Casa: assistant answered without opening “invoice.pdf” in this turn.",)
    at = _scope(role="assistant", name="E" * 64)
    at.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    assert at.admit(K.FINAL_REPLY, "hi").annotations[0].startswith("Casa: " + "E" * 64 + " answered")
    # a scope minted from an agent config, and the stored form, follow the same rule
    cfg = SimpleNamespace(role="assistant", character=SimpleNamespace(name="E" * 4100))
    msg = SimpleNamespace(id="t1", channel="telegram",
                          type=SimpleNamespace(value="channel_in"), context={"cid": "c"})
    minted = ob.TurnScope.mint(msg, cfg)
    minted.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    assert minted.admit(K.STORED, "brief").note == (
        "Casa: assistant wrote this without opening “invoice.pdf”.")


# ---------------------------------------------------------------------------
# The caption cap falls on the body, never on the line
# ---------------------------------------------------------------------------

async def test_the_caption_cap_falls_on_the_body_never_on_the_line(tmp_path):
    import agent as agent_mod
    import plugin_outbox
    outbox = plugin_outbox.init_outbox(str(tmp_path / "plugin-outbox"))
    ch = MagicMock()
    ch.send_media = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = ch
    tools.init_tools(channel_manager=cm, bus=MagicMock(), specialist_registry=MagicMock(),
                     mcp_registry=MagicMock(), trigger_registry=MagicMock(),
                     engagement_registry=MagicMock())
    s = _scope()
    # three notes inherited down a delegation chain plus today's line: a head
    # longer than the caption cap, which must fall on the body and never here
    for i in range(3):
        s.arm(ob.InheritedNote(f"Casa: Test wrote this without opening “{'x' * 300}{i}.pdf”."))
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    path = os.path.join(outbox._root_realpath, "invoice.pdf")
    with open(path, "wb") as fh:
        fh.write(b"%PDF-1.4\n" + b"x" * 64)
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram",
                                      "chat_id": 1197017861, "turn_scope": s})
    try:
        res = await tools.send_media.handler(
            {"path": path, "kind": "document", "caption": "Here it is. " * 100})
    finally:
        agent_mod.origin_var.reset(token)
        outbox.close()
        plugin_outbox._OUTBOX = None
    payload = json.loads(res["content"][0]["text"])
    assert payload["status"] == "ok", payload
    sent = ch.send_media.await_args.kwargs["caption"]
    assert len(payload["casa_prefixed"]) == 4
    assert sent.startswith("\n\n".join(payload["casa_prefixed"])), sent[-120:]
