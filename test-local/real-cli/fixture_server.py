#!/usr/bin/env python3
"""Fixture MCP server for the real-CLI gate (S5 §5.3). Stdlib-only stdio
JSON-RPC, the same shape as a Casa plugin's server.

One tool, ``probe``: a required integer ``n`` and an optional string ``mode``
with a schema default. Every argument object the server RECEIVES on a
``tools/call`` is appended, verbatim, to the evidence file named by
``GATE_EVIDENCE`` (one JSON line per call) — that file is the gate's wire
witness. The tool itself does nothing else and returns ``ok``.
"""
from __future__ import annotations

import json
import os
import sys

PROTOCOL_VERSION = "2024-11-05"
TOOLS = {
    "probe": {
        "description": "Gate fixture: records the arguments it receives and returns ok.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "a number"},
                "mode": {"type": "string", "default": "plain",
                         "description": "optional, schema default 'plain'"},
            },
            "required": ["n"],
        },
    },
}


def _record(arguments) -> None:
    path = os.environ.get("GATE_EVIDENCE")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"received": arguments}, sort_keys=True) + "\n")


def _result(id_, payload):
    return {"jsonrpc": "2.0", "id": id_, "result": payload}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(req: dict):
    method, id_ = req.get("method"), req.get("id")
    if method == "initialize":
        return _result(id_, {"protocolVersion": PROTOCOL_VERSION,
                             "capabilities": {"tools": {}},
                             "serverInfo": {"name": "gate-fixture", "version": "0"}})
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _result(id_, {"tools": [
            {"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
            for n, t in TOOLS.items()]})
    if method == "tools/call":
        params = req.get("params") or {}
        if params.get("name") not in TOOLS:
            return _error(id_, -32601, f"unknown tool {params.get('name')!r}")
        _record(params.get("arguments"))
        return _result(id_, {"content": [{"type": "text", "text": "ok"}]})
    if id_ is None:
        return None
    return _error(id_, -32601, f"unknown method {method!r}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
