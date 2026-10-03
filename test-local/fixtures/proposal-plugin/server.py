#!/usr/bin/env python3
"""PLAY fixture plugin for stored-call buttons (S5). Stdlib-only stdio
JSON-RPC MCP server with five tools:

- ``offer``  — deposits a proposal through Casa's broker (the
  ``operator_proposal`` slot): a question with the buttons Yes / No / More.
- ``apply``  — the stored call the Yes and No buttons name: records the
  arguments it received into the plugin data directory and answers with a
  plain receipt; an ``expected_revision`` other than the current one is a
  refusal IN the receipt (the plugin's own revision guard), never an error.
- ``more``   — the ``More`` exception: page 2 deposits another proposal
  (its own buttons, its More naming page 3); page 3 and beyond answer the
  contract's no-post shape ``{"proposal": null, "note": "no more entries"}``.
- ``offer_hang`` — deposits a proposal with one button, ``Hang``, naming
  ``hang``: the live check of the hung-call path.
- ``hang``   — the stored call that never returns: it starts one child
  process (a ``sleep``, so the termination path has a descendant to account
  for), records both pids into the plugin data directory, then blocks.

The broker is reached the way every Casa plugin reaches it: the Unix socket
and client id Casa puts in the server's environment (``CASA_BROKER_SOCKET``,
``CASA_BROKER_CLIENT``), ``POST /internal/broker/deposit``.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time

PROTOCOL_VERSION = "2024-11-05"
RENDER_ID = "r-1"
REVISION = 1

TOOLS = {
    "offer": {
        "description": "Offer the operator a decision as buttons (a proposal).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "apply": {
        "description": "Apply the operator's choice to the rendered view.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "choice": {"type": "string"},
                "render_id": {"type": "string"},
                "expected_revision": {"type": "integer"},
            },
            "required": ["choice", "render_id", "expected_revision"],
        },
    },
    "more": {
        "description": "Show the next page of the proposal.",
        "inputSchema": {
            "type": "object",
            "properties": {"page": {"type": "integer"}, "render_id": {"type": "string"}},
            "required": ["page", "render_id"],
        },
    },
    "offer_hang": {
        "description": "Offer the operator a button whose call never returns (hung-call test).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "ingest_document": {
        "description": "Ingest a document the operator sent: pass the path share_inbound_file returned.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    "hang": {
        "description": "Start a child process and block forever (hung-call test).",
        "inputSchema": {
            "type": "object",
            "properties": {"render_id": {"type": "string"}},
            "required": ["render_id"],
        },
    },
}


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost")
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


def deposit(proposal: dict) -> str:
    """Deposit *proposal* on the ``proposal`` slot; the broker's reference."""
    conn = _UnixHTTPConnection(os.environ["CASA_BROKER_SOCKET"])
    body = json.dumps({"client": os.environ["CASA_BROKER_CLIENT"], "slot": "proposal",
                       "value": json.dumps(proposal)})
    conn.request("POST", "/internal/broker/deposit", body=body,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8"))
    conn.close()
    if "reference" not in data:
        raise RuntimeError(f"deposit refused: {data.get('error')}")
    return data["reference"]


def _page(page: int) -> dict:
    binding = {"render_id": RENDER_ID, "expected_revision": REVISION}
    return {
        "text": (f"Pair invoice 17 with the Adobe payment? (page {page}, render {RENDER_ID}, "
                 f"revision {REVISION})"),
        "buttons": [
            {"label": "Yes", "call": {"tool": "apply", "arguments": {"choice": "yes", **binding}}},
            {"label": "No", "call": {"tool": "apply", "arguments": {"choice": "no", **binding}}},
            {"label": "More", "call": {"tool": "more",
                                       "arguments": {"page": page + 1, "render_id": RENDER_ID}}},
            {"label": "📎 Add a document", "arm_file": True},      # S6: arms the next file for this specialist
        ],
        "revision": f"{RENDER_ID}:{REVISION}",
    }


def _data_dir() -> str:
    path = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(os.getcwd(), ".proposal-fixture")
    os.makedirs(path, exist_ok=True)
    return path


def tool_offer(_args: dict) -> dict:
    return {"proposal": deposit(_page(1)), "note": "proposal posted"}


def tool_apply(args: dict) -> dict:
    with open(os.path.join(_data_dir(), "applied.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(args, sort_keys=True) + "\n")
    expected = args.get("expected_revision")
    if expected != REVISION:
        return {"applied": False,
                "receipt": (f"refused: render {args.get('render_id')} is at revision "
                            f"{REVISION}, shown {expected}")}
    return {"applied": True,
            "receipt": f"applied {args.get('choice')} to {args.get('render_id')} at revision {REVISION}"}


def tool_more(args: dict) -> dict:
    page = args.get("page")
    if not isinstance(page, int) or page > 2:
        return {"proposal": None, "note": "no more entries"}
    return {"proposal": deposit(_page(page)), "note": f"page {page} posted"}


def tool_ingest_document(args: dict) -> dict:
    """S6: a minimal observable ingestion — the specialist hands over the path
    share_inbound_file published into Casa's handoff folder; the fixture copies
    it into its data dir and reports name and size."""
    import shutil
    src = str(args.get("path") or "")
    if not src or not os.path.isfile(src):
        return {"ingested": False, "receipt": f"no such file: {src!r}"}
    dest_dir = os.path.join(_data_dir(), "ingested")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))
    shutil.copyfile(src, dest)
    size = os.path.getsize(dest)
    with open(os.path.join(_data_dir(), "ingested.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"name": os.path.basename(src), "size": size, "at": time.time()}) + "\n")
    return {"ingested": True, "receipt": f"ingested {os.path.basename(src)} ({size} bytes)"}


def tool_offer_hang(_args: dict) -> dict:
    proposal = {
        "text": f"Run the hung-call test? (render {RENDER_ID})",
        "buttons": [{"label": "Hang", "call": {"tool": "hang", "arguments": {"render_id": RENDER_ID}}}],
    }
    return {"proposal": deposit(proposal), "note": "proposal posted"}


def tool_hang(args: dict) -> dict:
    child = subprocess.Popen(["sleep", "3600"])
    with open(os.path.join(_data_dir(), "hang.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"server_pid": os.getpid(), "child_pid": child.pid,
                             "render_id": args.get("render_id"), "at": time.time()}) + "\n")
    while True:
        time.sleep(3600)


HANDLERS = {"offer": tool_offer, "apply": tool_apply, "more": tool_more,
            "offer_hang": tool_offer_hang, "hang": tool_hang,
            "ingest_document": tool_ingest_document}


def _result(id_, payload):
    return {"jsonrpc": "2.0", "id": id_, "result": payload}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(req: dict):
    method, id_ = req.get("method"), req.get("id")
    if method == "initialize":
        return _result(id_, {"protocolVersion": PROTOCOL_VERSION,
                             "capabilities": {"tools": {}},
                             "serverInfo": {"name": "proposal-fixture", "version": "0.2.0"}})
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _result(id_, {"tools": [
            {"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
            for n, t in TOOLS.items()]})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        if name not in HANDLERS:
            return _error(id_, -32601, f"unknown tool {name!r}")
        try:
            out = HANDLERS[name](params.get("arguments") or {})
        except Exception as exc:  # noqa: BLE001 — a tool error is the MCP error shape
            return _result(id_, {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                                 "isError": True})
        return _result(id_, {"content": [{"type": "text", "text": json.dumps(out)}]})
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
        res = handle(req)
        if res is not None:
            sys.stdout.write(json.dumps(res) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
