"""Contract guard: the real python-telegram-bot must render our entities as the
fake stub (and the parser's offsets) assume.

Runs in a SUBPROCESS on purpose — tests/conftest.py installs a fake ``telegram``
stub for the unit session, which would shadow the real ``MessageEntity`` and its
UTF-16 helper. A fresh interpreter sees the real PTB (installed via requirements),
so this pins ``render()``'s astral-offset behavior to reality (the fake's
``adjust_message_entities_to_utf_16`` could otherwise drift from PTB's).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
CASA = REPO / "casa" / "rootfs" / "opt" / "casa"

_SCRIPT = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {casa!r})
    from telegram import MessageEntity  # REAL ptb
    from channels.tg_richtext import render

    # Astral emoji: codepoint offset 2 must become UTF-16 offset 3.
    display, ents = render("🧾 **hi**")
    assert display == "🧾 hi", display
    assert len(ents) == 1, ents
    assert ents[0].type == MessageEntity.BOLD, ents[0].type
    assert ents[0].offset == 3, ents[0].offset
    assert ents[0].length == 2, ents[0].length

    # Fenced table + inline code render as PRE/CODE with real entity types.
    display, ents = render("```\\nA  1\\nB  2\\n```")
    assert display == "A  1\\nB  2", repr(display)
    assert ents[0].type == MessageEntity.PRE, ents[0].type

    print("OK")
    """
).format(casa=str(CASA))


_ENCODING_SCRIPT = textwrap.dedent(
    """
    import asyncio, sys
    from urllib.parse import parse_qs

    import httpx
    import telegram
    from telegram.request import HTTPXRequest

    sys.path.insert(0, {casa!r})
    from channels.tg_richtext import render_paged  # REAL ptb

    LABEL = "DESTINATION"
    URL = "https://example.test/target"
    AUTHORED = "\\ud800[" + LABEL + "](" + URL + ")\\n\\n" + "x" * 4096

    pages = render_paged(AUTHORED)
    assert pages == [
        ("\\ufffd" + LABEL + " (" + URL + ")", None),
        ("x" * 4096, None),
    ], repr(pages[0])

    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={{"ok": True, "result": {{
                "id": 1, "is_bot": True, "first_name": "t", "username": "t"}}}})
        return httpx.Response(200, json={{"ok": True, "result": {{
            "message_id": 1, "date": 0,
            "chat": {{"id": 42, "type": "private"}}}}}})

    async def main():
        bot = telegram.Bot("1:AA", request=HTTPXRequest(
            httpx_kwargs={{"transport": httpx.MockTransport(handler)}}))
        async with bot:
            await bot.send_message(chat_id=42, text=pages[0][0])

    asyncio.run(main())

    assert len(seen) == 2, [r.url.path for r in seen]
    assert [r.url.path.rsplit("/", 1)[-1] for r in seen] == [
        "getMe", "sendMessage"], [r.url.path for r in seen]
    assert parse_qs(seen[1].content.decode("ascii")) == {{
        "chat_id": ["42"],
        "text": ["\\ufffd" + LABEL + " (" + URL + ")"],
    }}, seen[1].content

    print("OK")
    """
).format(casa=str(CASA))


def test_a_repaired_page_survives_ptbs_request_encoding():
    """#834: the repaired page must be able to LEAVE the process.

    PTB 22.7 sends `data=request_data.json_parameters` — form data, which httpx
    encodes as UTF-8 — not `json_payload`, whose `ensure_ascii` would have
    escaped a lone surrogate harmlessly. So a page still carrying one raises
    `NetworkError(UnicodeEncodeError)` before any request is made, and a
    restored destination reaches nobody. The unit suite's `AsyncMock` bot
    cannot see this: it never crosses the request layer. This case does, in the
    same subprocess idiom, against a real `Bot` over a mock TRANSPORT — the
    encoder runs before the transport is reached, so a green result here means
    the bytes were actually encodable.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _ENCODING_SCRIPT],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip().endswith("OK"), proc.stdout


def test_render_matches_real_ptb():
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip().endswith("OK"), proc.stdout
