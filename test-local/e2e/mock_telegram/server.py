"""Minimal Telegram Bot API mock for engagement e2e.

Implements just enough of the Bot API for TelegramChannel and the agent log:
- getMe → returns a stub bot user
- getChatMember → an administrator with can_manage_topics=True (default)
- createForumTopic → assigns incrementing message_thread_id, stores state
- sendMessage → stores message under thread_id (or the main feed)
- editMessageText → records the edit
- deleteMessages / deleteMessage → records the deleted ids

State is in-memory; server restart clears it. POST /_inspect returns a JSON
dump of state so the e2e script can assert on it.
"""

from __future__ import annotations

import asyncio
import json
import os
from aiohttp import web

STATE: dict = {
    "supergroup_id": int(os.environ.get("MOCK_TG_SUPERGROUP_ID", "-1001")),
    "bot_id": 4242,
    "next_thread_id": 1001,
    "topics": {},
    "messages_by_thread": {},
    "main_feed_messages": [],
    "edits": [],
    "deleted": [],
    "can_manage_topics": True,
    "pending_updates": [],
    "next_update_id": 1,
}


# ---------------------------------------------------------------------------
# Pure Bot-API result builders. Extracted so tests/test_mock_telegram_ptb_contract.py
# can feed each payload through the REAL python-telegram-bot de_json parsers —
# a mock that drifts from PTB's strict parsing (as getChatMember did before
# v0.57.1) then fails in the 90s unit gate, not after six red tier2 releases.
# Keep these BEHAVIOUR-IDENTICAL to what the handlers return.
# ---------------------------------------------------------------------------


def build_getme_result(bot_id: int) -> dict:
    return {"id": bot_id, "is_bot": True, "first_name": "CasaBot"}


def build_getchatmember_result(bot_id: int, can_manage_topics: bool) -> dict:
    # PTB parses this strictly: ChatMemberAdministrator requires the full
    # admin-rights field set, and User requires first_name + is_bot. A thinner
    # payload raises inside de_json → the channel logs "bot-permissions check
    # failed" and disables engagements.
    return {
        "status": "administrator",
        "user": {"id": bot_id, "is_bot": True,
                 "first_name": "CasaBot", "username": "casabot"},
        "can_be_edited": False,
        "is_anonymous": False,
        "can_manage_chat": True,
        "can_delete_messages": True,
        "can_manage_video_chats": True,
        "can_restrict_members": True,
        "can_promote_members": False,
        "can_change_info": True,
        "can_invite_users": True,
        "can_post_stories": False,
        "can_edit_stories": False,
        "can_delete_stories": False,
        "can_pin_messages": True,
        "can_manage_topics": can_manage_topics,
    }


def build_createforumtopic_result(thread_id: int, name: str) -> dict:
    return {"message_thread_id": thread_id, "name": name, "icon_color": 0}


def build_sendmessage_result(message_id: int, chat_id: int, text: str,
                             thread_id: int | None = None) -> dict:
    # PTB's Message.de_json requires message_id + date + chat. Return a
    # minimally-well-formed Message so callers can deserialize the reply.
    result = {
        "message_id": message_id,
        "date": 0,
        "chat": {"id": chat_id, "type": "supergroup" if thread_id else "private"},
        "text": text,
    }
    if thread_id:
        result["message_thread_id"] = int(thread_id)
    return result


async def handle_method(request: web.Request) -> web.Response:
    method = request.match_info["method"]
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        try:
            form = await request.post()
            payload = dict(form)
        except Exception:
            payload = {}

    if method == "getMe":
        return web.json_response({"ok": True, "result": build_getme_result(STATE["bot_id"])})

    if method == "getChatMember":
        return web.json_response({"ok": True, "result": build_getchatmember_result(
            STATE["bot_id"], STATE["can_manage_topics"])})

    if method == "createForumTopic":
        tid = STATE["next_thread_id"]
        STATE["next_thread_id"] += 1
        STATE["topics"][tid] = {"name": payload.get("name", "")}
        return web.json_response({"ok": True, "result": build_createforumtopic_result(
            tid, payload.get("name", ""))})

    if method == "sendMessage":
        tid = payload.get("message_thread_id")
        text = payload.get("text", "")
        chat_id = int(payload.get("chat_id", 0))
        if tid:
            STATE["messages_by_thread"].setdefault(int(tid), []).append(text)
        else:
            STATE["main_feed_messages"].append(text)
        STATE["next_message_id"] = STATE.get("next_message_id", 1) + 1
        result = build_sendmessage_result(
            STATE["next_message_id"], chat_id, text,
            thread_id=int(tid) if tid else None,
        )
        return web.json_response({"ok": True, "result": result})

    if method == "editMessageText":
        STATE["edits"].append({"chat_id": int(payload.get("chat_id", 0)),
                               "message_id": int(payload.get("message_id", 0)),
                               "text": payload.get("text", "")})
        return web.json_response({"ok": True, "result": True})

    if method == "deleteMessages":
        ids = payload.get("message_ids") or []
        if isinstance(ids, str):
            ids = json.loads(ids)
        STATE["deleted"].extend(int(i) for i in ids)
        return web.json_response({"ok": True, "result": True})

    if method == "deleteMessage":
        STATE["deleted"].append(int(payload.get("message_id", 0)))
        return web.json_response({"ok": True, "result": True})

    if method == "getUpdates":
        updates = list(STATE["pending_updates"])
        STATE["pending_updates"].clear()
        return web.json_response({"ok": True, "result": updates})

    # Swallow all other methods.
    return web.json_response({"ok": True, "result": True})


async def handle_inspect(request: web.Request) -> web.Response:
    return web.json_response(STATE)


async def handle_reset(request: web.Request) -> web.Response:
    STATE["topics"].clear()
    STATE["messages_by_thread"].clear()
    STATE["main_feed_messages"].clear()
    STATE["edits"].clear()
    STATE["deleted"].clear()
    STATE["pending_updates"].clear()
    STATE["next_thread_id"] = 1001
    STATE["next_update_id"] = 1
    STATE["can_manage_topics"] = True
    return web.json_response({"ok": True})


async def handle_inject_update(request: web.Request) -> web.Response:
    """Test-only endpoint: inject an incoming Telegram Update into the
    getUpdates pipeline. Used by e2e scripts to simulate incoming
    messages.

    Body: JSON - an Update payload.
    """
    payload = await request.json()
    STATE["pending_updates"].append(payload)
    STATE["next_update_id"] += 1
    return web.json_response({"ok": True})


def app_factory() -> web.Application:
    app = web.Application()
    # Match both /bot<token>/<method> and /<method>
    app.router.add_post(r"/bot{token}/{method}", handle_method)
    app.router.add_post(r"/{method}", handle_method)
    app.router.add_get("/_inspect", handle_inspect)
    app.router.add_post("/_reset", handle_reset)
    app.router.add_post("/_inject", handle_inject_update)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_TG_PORT", "8081"))
    web.run_app(app_factory(), host="0.0.0.0", port=port)
