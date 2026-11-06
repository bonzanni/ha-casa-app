"""Warm/cold first-block latency probe (pooling spec §10).

Usage: ssh <ha-host> "sudo -n docker exec -i addon_91d4d4c8_casa \
           python3 -" < test-local/eval/voice_latency_probe.py
Prints per-turn utterance->first-block ms for 1 cold + 4 warm turns.

Protocol verified against channels/voice/channel.py's `_ws_handler` /
`_run_ws_utterance` (v0.66.0):
  - auth: X-Webhook-Signature header = HMAC-SHA256(webhook_secret, b"")
    (VoiceChannel._verify, empty body for the WS upgrade request)
  - client->server frames: {"type": "stt_start", "scope_id": ...} (registers
    the scope; ignored otherwise) and {"type": "utterance", "utterance_id":
    ..., "text": ..., "scope_id": ...}
  - server->client frames: {"type": "block", "utterance_id": ..., "text":
    ..., "final": bool}, then {"type": "done", "utterance_id": ...} (or
    {"type": "error", "utterance_id": ..., "kind": ..., "spoken": ...} on
    failure — treated as terminal by this probe too, so it can't hang).
All field names in this script matched the handler as-is; no corrections
were needed.
"""
import asyncio, hmac, hashlib, json, time, uuid

import aiohttp

SCOPE = f"latprobe-{uuid.uuid4().hex[:8]}"
URL = "http://127.0.0.1:8099/api/converse/ws"


async def main() -> None:
    secret = open("/data/webhook_secret", "rb").read().strip()
    sig = hmac.new(secret, b"", hashlib.sha256).hexdigest()
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(URL, headers={"X-Webhook-Signature": sig}) as ws:
            for i in range(5):
                uid = f"u{i}"
                await ws.send_json({"type": "stt_start", "scope_id": SCOPE})
                t0 = time.monotonic()
                await ws.send_json({"type": "utterance", "utterance_id": uid,
                                    "text": "hi", "scope_id": SCOPE})
                first = None
                async for frame in ws:
                    m = json.loads(frame.data)
                    if m.get("utterance_id") not in (None, uid):
                        continue
                    if m.get("type") == "block" and first is None:
                        first = time.monotonic() - t0
                    if m.get("type") in ("done", "error"):
                        break
                print(f"turn {i} ({'cold' if i == 0 else 'warm'}): "
                      f"first_block={first and round(first*1000)}ms")

asyncio.run(main())
