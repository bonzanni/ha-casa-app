"""Agent-parameterized live-probe driver for WS-A (block S — delegation & fleet authz).

Runs INSIDE the casa container (like audit/live_invariant_audit.py):

    docker cp wsa_probe.py addon_<slug>_casa:/tmp/wsa_probe.py
    docker exec addon_<slug>_casa python3 /tmp/wsa_probe.py <cmd> ...

Every command takes the agent role / target / scope as PARAMETERS so the same
probe re-runs unchanged against any future agent (a new voice resident, an MTG
specialist, an installed agent-repo agent): onboard the agent, point the probe
at its role, assert the same typed invariants.

Commands
  converse --role R --scope S --prompt TEXT [--timeout N]
      Signed SSE POST /api/converse. Prints "HTTP <code>" then one line per
      frame: "block|final=<bool>|<text>", "error|<kind>|<spoken>", "done".
      Non-200 prints "HTTP <code> <body>".
  ws --role R --scope S --text TEXT [--timeout N]
      Signed WS /api/converse/ws utterance. Prints frames like converse.
  invoke --agent A --prompt TEXT [--chat-id C] [--timeout N]
      Signed POST /invoke/{A}. Prints "HTTP <code> <body>".
  post --path P --body JSON
      Arbitrary signed POST; prints "HTTP <code> <body>" (exact body bytes —
      used for 404 no-existence-oracle byte-parity assertions).
  telegram --text T [--chat-id C] [--bot-id B]
      Synthetic Telegram DM update on bot B's route (default: Casa's own bot,
      as stored in /data/telegram-casa-bot.json); drives the telegram-resident
      path.
  sessions
      Print sessions.json rows as "key|agent|scope_class|last_active".
  key --channel C --role R --scope S
      Print the expected v2 session key (build_scoped_session_key is the
      source of truth — imported from /opt/casa).

Read-only except for the turns it drives. Never prints the webhook secret.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8099"
# Operator's Telegram user/chat id for synthetic-DM drives. NOT baked in —
# set CASA_PROBE_TG_USER in the environment (or pass --chat-id per call). The
# default 0 is an intentionally-invalid placeholder so a misconfigured run
# fails loudly rather than posting to someone else's chat.
DEFAULT_TG_USER = int(os.environ.get("CASA_PROBE_TG_USER", "0"))


def _secret() -> str:
    s = os.environ.get("WEBHOOK_SECRET", "").strip()
    if s:
        return s
    try:
        with open("/data/webhook_secret") as f:
            return f.read().strip()
    except OSError:
        return ""


def _sign(body: bytes) -> dict:
    sec = _secret()
    if not sec:
        return {}
    return {"X-Webhook-Signature": hmac.new(sec.encode(), body, hashlib.sha256).hexdigest()}


def _post(path: str, payload: dict, timeout: float, stream: bool = False):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": "application/json", **_sign(body)})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp
    except urllib.error.HTTPError as e:
        return e.code, e


def cmd_converse(a) -> int:
    payload = {"prompt": a.prompt, "agent_role": a.role, "scope_id": a.scope}
    status, resp = _post("/api/converse", payload, a.timeout, stream=True)
    if status != 200:
        print(f"HTTP {status} {resp.read().decode(errors='replace')}")
        return 1
    print("HTTP 200")
    event = None
    deadline = time.time() + a.timeout
    try:
        for raw in resp:
            if time.time() > deadline:
                print("TIMEOUT mid-stream")
                return 2
            line = raw.decode(errors="replace").rstrip("\n")
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: ") and event:
                data = json.loads(line[6:] or "{}")
                if event == "block":
                    print(f"block|final={data.get('final')}|{data.get('text', '')}")
                elif event == "error":
                    print(f"error|{data.get('kind')}|{data.get('spoken', '')}")
                elif event == "done":
                    print("done")
                    return 0
                event = None
    except Exception as e:  # noqa: BLE001 — probe surface, report and exit
        print(f"STREAM-ERROR {e!r}")
        return 2
    print("EOF-without-done")
    return 2


def cmd_ws(a) -> int:
    import asyncio

    import aiohttp

    async def run() -> int:
        headers = _sign(b"")
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(BASE + "/api/converse/ws",
                                       headers=headers, timeout=15) as ws:
                await ws.send_json({
                    "type": "utterance", "text": a.text, "agent_role": a.role,
                    "scope_id": a.scope, "utterance_id": a.utterance_id})
                deadline = time.time() + a.timeout
                while time.time() < deadline:
                    try:
                        msg = await ws.receive(timeout=max(1.0, deadline - time.time()))
                    except asyncio.TimeoutError:
                        print("TIMEOUT waiting for frame")
                        return 2
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        print(f"WS-{msg.type.name}")
                        return 2
                    d = json.loads(msg.data)
                    t = d.get("type")
                    if t == "block":
                        print(f"block|final={d.get('final')}|{d.get('text', '')}")
                    elif t == "error":
                        print(f"error|{d.get('kind')}|{d.get('spoken', '')}")
                        return 0
                    elif t == "done":
                        print("done")
                        return 0
                print("TIMEOUT")
                return 2

    return asyncio.run(run())


def cmd_invoke(a) -> int:
    payload: dict = {"prompt": a.prompt}
    if a.chat_id:
        payload["context"] = {"chat_id": a.chat_id}
    status, resp = _post(f"/invoke/{a.agent}", payload, a.timeout)
    print(f"HTTP {status} {resp.read().decode(errors='replace')}")
    return 0 if status == 200 else 1


def cmd_post(a) -> int:
    status, resp = _post(a.path, json.loads(a.body), a.timeout)
    print(f"HTTP {status} {resp.read().decode(errors='replace')}")
    return 0


def _casa_bot_id() -> int:
    """The bot id Casa's own endpoint last verified (0 when none is stored)."""
    try:
        with open("/data/telegram-casa-bot.json") as f:
            return int(json.load(f).get("bot_id") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def cmd_telegram(a) -> int:
    now = int(time.time())
    payload = {
        "update_id": int(time.time() * 1000) % 2**31,
        "message": {"message_id": now % 2**31,
                    "from": {"id": a.chat_id, "is_bot": False, "first_name": "Nicola"},
                    "chat": {"id": a.chat_id, "type": "private"},
                    "date": now, "text": a.text}}
    body = json.dumps(payload).encode()
    bot_id = a.bot_id or _casa_bot_id()
    if not bot_id:
        print("no bot id: pass --bot-id (Casa's bot has not connected yet)")
        return 2
    # Each bot has its own update route, signed with a secret derived from
    # the configured one for that bot.
    sys.path.insert(0, "/opt/casa")
    from channels.telegram_transport import webhook_secret_for
    req = urllib.request.Request(
        f"{BASE}/telegram/update/{bot_id}", data=body,
        headers={"Content-Type": "application/json",
                 "X-Telegram-Bot-Api-Secret-Token":
                     webhook_secret_for(_secret(), bot_id)})
    with urllib.request.urlopen(req, timeout=10) as r:
        print(f"HTTP {r.status} {r.read().decode(errors='replace')}")
    return 0


def cmd_sessions(_a) -> int:
    with open("/data/sessions.json") as f:
        data = json.load(f)
    for k, e in sorted(data.items()):
        print(f"{k}|{e.get('agent')}|{e.get('scope_class')}|{e.get('last_active')}")
    return 0


def cmd_key(a) -> int:
    sys.path.insert(0, "/opt/casa")
    from session_registry import build_scoped_session_key  # noqa: PLC0415
    print(build_scoped_session_key(a.channel, a.role, a.scope))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="wsa_probe")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("converse")
    c.add_argument("--role", required=True)
    c.add_argument("--scope", required=True)
    c.add_argument("--prompt", required=True)
    c.add_argument("--timeout", type=float, default=90)
    c.set_defaults(fn=cmd_converse)

    w = sub.add_parser("ws")
    w.add_argument("--role", required=True)
    w.add_argument("--scope", required=True)
    w.add_argument("--text", required=True)
    w.add_argument("--utterance-id", default="probe-u1")
    w.add_argument("--timeout", type=float, default=90)
    w.set_defaults(fn=cmd_ws)

    i = sub.add_parser("invoke")
    i.add_argument("--agent", required=True)
    i.add_argument("--prompt", required=True)
    i.add_argument("--chat-id", default="")
    i.add_argument("--timeout", type=float, default=120)
    i.set_defaults(fn=cmd_invoke)

    o = sub.add_parser("post")
    o.add_argument("--path", required=True)
    o.add_argument("--body", required=True)
    o.add_argument("--timeout", type=float, default=30)
    o.set_defaults(fn=cmd_post)

    t = sub.add_parser("telegram")
    t.add_argument("--text", required=True)
    t.add_argument("--chat-id", type=int, default=DEFAULT_TG_USER)
    t.add_argument("--bot-id", type=int, default=0,
                   help="the receiving bot (default: Casa's own bot)")
    t.set_defaults(fn=cmd_telegram)

    s = sub.add_parser("sessions")
    s.set_defaults(fn=cmd_sessions)

    k = sub.add_parser("key")
    k.add_argument("--channel", required=True)
    k.add_argument("--role", required=True)
    k.add_argument("--scope", required=True)
    k.set_defaults(fn=cmd_key)

    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
