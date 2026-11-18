"""INV-TG-009 — no text leaves for the Bot API carrying a surrogate code point.

Runs in a SUBPROCESS on purpose: ``tests/conftest.py`` installs a fake
``telegram`` stub for the unit session and the suite's ``AsyncMock`` bot never
crosses the client library's request layer — which is exactly where the
defect lives. PTB 22.7 sends every request as UTF-8 FORM data, so a string
parameter still carrying a lone surrogate raises ``NetworkError`` (cause
``UnicodeEncodeError``) before any request leaves the process. A fresh
interpreter sees the real library; a real ``Bot`` over an ``httpx.MockTransport``
records every request that reached the transport, and each arm asserts the
COUNT of ``sendMessage`` / ``editMessageText`` / ``sendMessageDraft`` /
``sendRichMessage`` / ``sendDocument`` requests —
never a status — plus the decoded form values: no surrogate remains, and each
raw surrogate the input carried contributes exactly one U+FFFD. The rich
endpoints (``sendRichMessage``, ``editMessageText`` with ``rich_message``,
``sendRichMessageDraft``) carry the text nested in a JSON value; there the
decoded Markdown is checked the same way.

The request class is resolved as ``channels.telegram._SurrogateSafeRequest``
where it exists and the library's own ``HTTPXRequest`` otherwise — what the
base's ``Application.builder().token(...)`` installs — so the pre-fix tree
stays importable and goes red for the ENCODING failure, not for a missing
class. Specified by terra, accepted by astra (drive run 2026-09-06, cluster C).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CASA = REPO / "casa" / "rootfs" / "opt" / "casa"

_SCRIPT = textwrap.dedent(
    r"""
    import asyncio, email, json, sys, types
    from urllib.parse import parse_qs

    import httpx
    import telegram
    from telegram import MessageEntity
    from telegram.request import HTTPXRequest

    sys.path.insert(0, sys.argv[1])
    from channels import DeliveryOutcome
    import channels.telegram as tg
    from channels.telegram import TelegramChannel
    from output_boundary import IntentKind, TurnScope

    def admitted(text):
        # #1038: the channel accepts only admitted text; a scope owing nothing.
        return TurnScope(id="t", cid="c", role="assistant", display_name="Test",
                         channel="telegram", message_type="channel_in",
                         ).admit(IntentKind.FINAL_REPLY, text)

    Request = getattr(tg, "_SurrogateSafeRequest", HTTPXRequest)

    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {
                "id": 1, "is_bot": True, "first_name": "t", "username": "t"}})
        if request.url.path.endswith(("sendMessageDraft", "sendRichMessageDraft")):
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(200, json={"ok": True, "result": {
            "message_id": 7, "date": 0, "chat": {"id": 42, "type": "private"}}})

    def endpoint(r):
        return r.url.path.rsplit("/", 1)[-1]

    def counts():
        return {n: sum(1 for r in seen if endpoint(r) == n)
                for n in ("sendMessage", "editMessageText", "sendMessageDraft",
                          "sendDocument")}

    def flat(r):
        return {k: v[0] for k, v in parse_qs(
            r.content.decode("utf-8"), keep_blank_values=True).items()}

    def multipart(r):
        msg = email.message_from_bytes(
            b"Content-Type: " + r.headers["content-type"].encode("ascii")
            + b"\r\n\r\n" + r.content)
        return [(part.get_param("name", header="content-disposition"),
                 part.get_param("filename", header="content-disposition"),
                 part.get_payload(decode=True)) for part in msg.get_payload()]

    TEXT_ENDPOINTS = ("sendMessage", "editMessageText", "sendMessageDraft",
                      "sendRichMessage", "sendRichMessageDraft")

    def text_of(r):
        # A rich message carries its text as the Markdown inside rich_message.
        f = flat(r)
        if "rich_message" in f:
            return json.loads(f["rich_message"]).get("markdown")
        return f.get("text")
    R = "�"
    MEDIA = b"\x00casa-media\xff"
    CTX = lambda: {"chat_id": "42"}
    failures = []

    def check(name, expect, outcome, got_outcome, exc):
        # `expect`: the exact ordered (endpoint, text) list of the text requests
        # that must have reached the transport — position and every other
        # character included, so deletion, a shifted replacement or a rewritten
        # neighbour all fail here, not only a wrong count.
        got = [(endpoint(r), text_of(r)) for r in seen
               if endpoint(r) in TEXT_ENDPOINTS]
        reason = (f"{type(exc).__name__}(cause={type(exc.__cause__).__name__})"
                  if exc is not None else "no raise")
        line = f"{name}: {counts()} outcome={got_outcome} raise={reason}"
        print(line)
        ok = exc is None and got == expect and got_outcome is outcome
        if got != expect:
            print(f"  {name}: texts differ: got {[(e, len(t or ''), (t or '')[:8]) for e, t in got]}"
                  f" expected {[(e, len(t), t[:8]) for e, t in expect]}")
        for r in seen:
            if endpoint(r) in TEXT_ENDPOINTS and flat(r).get("chat_id") != "42":
                ok = False
                print(f"  {name}: chat_id was not 42")
        if not ok:
            failures.append(line)
        return [flat(r) for r in seen if endpoint(r) in TEXT_ENDPOINTS]

    async def run_arm(name, coro, expect, outcome=None):
        seen.clear()
        exc = None
        got_outcome = None
        try:
            got_outcome = await coro
        except telegram.error.TelegramError as e:
            exc = e
        return check(name, expect, outcome, got_outcome, exc)

    async def main():
        bot = telegram.Bot("1:AA", request=Request(
            httpx_kwargs={"transport": httpx.MockTransport(handler)}))
        ch = TelegramChannel(bot_token="1:AA", chat_id="42",
                             default_agent="assistant")
        async with bot:
            ch._app = types.SimpleNamespace(bot=bot)
            seen.clear()

            # ARM1 — a long reply, one surrogate on each of its three lines:
            # one rich message, each surrogate replaced in place.
            a1 = ("\ud800" + "x" * 4095 + "\n" + "\ud800" + "y" * 4095
                  + "\n" + "\ud800" + "z" * 4095)
            await run_arm("long-reply", ch.send_response(admitted(a1), CTX()),
                          [("sendRichMessage", R + "x" * 4095 + "\\\n" + R + "y" * 4095
                            + "\\\n" + R + "z" * 4095)],
                          outcome=DeliveryOutcome.DELIVERED)

            # ARM2 — a reply with a link and a surrogate after it.
            a2 = "[link](https://example.test/a) \ud800"
            await run_arm("reply-with-link", ch.send_response(admitted(a2), CTX()),
                          [("sendRichMessage", "[link](https://example.test/a) " + R)],
                          outcome=DeliveryOutcome.DELIVERED)

            # ARM3 — send with a LOW surrogate (kills high-only cleaning).
            await run_arm("send-low-surrogate", ch.send(admitted("\udc00 hello"), CTX()),
                          [("sendRichMessage", R + " hello")],
                          outcome=DeliveryOutcome.DELIVERED)

            # ARM5 — two bold lines, a surrogate after each.
            a5 = ("**bold** " + "x" * 4080 + "\ud800\n"
                  + "**bold** " + "y" * 4080 + "\ud800")
            await run_arm("bold-reply", ch.send_response(admitted(a5), CTX()),
                          [("sendRichMessage", "**bold** " + "x" * 4080 + R + "\\\n"
                            + "**bold** " + "y" * 4080 + R)],
                          outcome=DeliveryOutcome.DELIVERED)

            # Draft — the text a reply is written into while the model writes,
            # a rich draft.
            await run_arm("draft", ch._send_draft("42", 7, admitted("x\ud800")),
                          [("sendRichMessageDraft", "x" + R)])

            # Rich — the Markdown nested in rich_message, for a send, an edit
            # and a draft. The ordered list is the count assertion.
            rich_endpoints = ("sendRichMessage", "editMessageText", "sendRichMessageDraft")
            seen.clear()
            try:
                await ch._send_rich(42, "x\ud800")
                await ch._edit_rich(42, 7, "\udc00 y")
                await ch._send_rich_draft(42, 7, "z\ud800", can_stop=True,
                                          keep_on_stop=True)
            except telegram.error.TelegramError as e:
                failures.append(f"rich: {type(e).__name__}(cause={type(e.__cause__).__name__})")
            got = [(endpoint(r), json.loads(flat(r)["rich_message"]).get("markdown"),
                    flat(r).get("chat_id"))
                   for r in seen if endpoint(r) in rich_endpoints]
            print(f"rich: {[(e, m) for e, m, _c in got]!r}")
            if got != [("sendRichMessage", "x" + R, "42"),
                       ("editMessageText", R + " y", "42"),
                       ("sendRichMessageDraft", "z" + R, "42")]:
                failures.append(f"rich: requests differ: {got!r}")
            if any(0xD800 <= ord(c) <= 0xDFFF for _e, m, _c in got for c in m or ""):
                failures.append("rich: a surrogate reached the transport")

            # Media — the caption is a form value too; the bytes are not.
            await run_arm("media-caption",
                          ch.send_media(MEDIA, "document", "x.bin", CTX(), caption=admitted("\ud800")),
                          [])
            docs = [r for r in seen if endpoint(r) == "sendDocument"]
            parts = multipart(docs[0]) if docs else []
            got = {(n, f, payload) for n, f, payload in parts}
            print(f"media-caption: parts={[(n, f, len(p)) for n, f, p in parts]}")
            if len(docs) != 1 or got != {
                    ("chat_id", None, b"42"),
                    ("caption", None, R.encode("utf-8")),
                    ("document", "x.bin", MEDIA)}:
                failures.append(f"media-caption: {len(docs)} sendDocument, parts {got!r}")

            # Nested control — a JSON-encoded value keeps its literal escape.
            seen.clear()
            await bot.send_message(chat_id=42, text="x", entities=[
                MessageEntity(type="text_link", offset=0, length=1, url="\ud800")])
            f = flat(seen[-1])
            print(f"nested-escape-control: {counts()} entities={f.get('entities')!r}")
            if (counts()["sendMessage"] != 1 or f.get("text") != "x"
                    or "\\ud800" not in f.get("entities", "")):
                failures.append("nested-escape-control: literal escape was rewritten")

    asyncio.run(main())
    assert not failures, "\n".join(failures)
    print("OK")
    """
)



def test_pin_inv_tg_009_no_request_leaves_with_a_surrogate_code_point():
    """INV-TG-009: every string parameter of every request Casa's Telegram bot
    makes has each surrogate code point replaced by one U+FFFD below every
    sender — a long reply, a reply with a link or bold, ``send``, the draft
    send, the rich send, edit and draft, and media caption — so the reply is
    DELIVERED with the character replaced instead of raising before any
    request leaves.

    Counts are the assertion. At the base every block arm raises
    ``NetworkError`` whose cause is ``UnicodeEncodeError`` with ZERO requests;
    the subprocess prints each arm's counts and raise so the failure names the
    reason. The nested control (an entity url carrying a surrogate) passes on
    both trees: the library JSON-escapes it and the boundary must not rewrite
    the literal escape.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(CASA)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip().endswith("OK"), proc.stdout
    # The raw rich edit does not trip the library's "use the method" warning.
    assert "Please use" not in proc.stderr, proc.stderr
