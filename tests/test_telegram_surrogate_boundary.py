"""INV-TG-009 — no text leaves for the Bot API carrying a surrogate code point.

Runs in a SUBPROCESS on purpose: ``tests/conftest.py`` installs a fake
``telegram`` stub for the unit session and the suite's ``AsyncMock`` bot never
crosses the client library's request layer — which is exactly where the
defect lives. PTB 22.7 sends every request as UTF-8 FORM data, so a string
parameter still carrying a lone surrogate raises ``NetworkError`` (cause
``UnicodeEncodeError``) before any request leaves the process. A fresh
interpreter sees the real library; a real ``Bot`` over an ``httpx.MockTransport``
records every request that reached the transport, and each arm asserts the
COUNT of ``sendMessage`` / ``editMessageText`` / ``sendDocument`` requests —
never a status — plus the decoded form values: no surrogate remains, and each
raw surrogate the input carried contributes exactly one U+FFFD.

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
    import asyncio, email, sys, types
    from urllib.parse import parse_qs

    import httpx
    import telegram
    from telegram import MessageEntity
    from telegram.request import HTTPXRequest

    sys.path.insert(0, sys.argv[1])
    from channels import DeliveryOutcome
    import channels.telegram as tg
    from channels.telegram import TelegramChannel

    Request = getattr(tg, "_SurrogateSafeRequest", HTTPXRequest)

    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {
                "id": 1, "is_bot": True, "first_name": "t", "username": "t"}})
        return httpx.Response(200, json={"ok": True, "result": {
            "message_id": 7, "date": 0, "chat": {"id": 42, "type": "private"}}})

    def endpoint(r):
        return r.url.path.rsplit("/", 1)[-1]

    def counts():
        return {n: sum(1 for r in seen if endpoint(r) == n)
                for n in ("sendMessage", "editMessageText", "sendDocument")}

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

    R = "�"
    MEDIA = b"\x00casa-media\xff"
    CTX = lambda: {"chat_id": "42"}
    failures = []

    def check(name, expect, outcome, got_outcome, exc):
        # `expect`: the exact ordered (endpoint, text) list of the text requests
        # that must have reached the transport — position and every other
        # character included, so deletion, a shifted replacement or a rewritten
        # neighbour all fail here, not only a wrong count.
        got = [(endpoint(r), flat(r).get("text")) for r in seen
               if endpoint(r) in ("sendMessage", "editMessageText")]
        reason = (f"{type(exc).__name__}(cause={type(exc.__cause__).__name__})"
                  if exc is not None else "no raise")
        line = f"{name}: {counts()} outcome={got_outcome} raise={reason}"
        print(line)
        ok = exc is None and got == expect and got_outcome is outcome
        if got != expect:
            print(f"  {name}: texts differ: got {[(e, len(t or ''), (t or '')[:8]) for e, t in got]}"
                  f" expected {[(e, len(t), t[:8]) for e, t in expect]}")
        for r in seen:
            if endpoint(r) in ("sendMessage", "editMessageText") and flat(r).get("chat_id") != "42":
                ok = False
                print(f"  {name}: chat_id was not 42")
        if not ok:
            failures.append(line)
        return [flat(r) for r in seen if endpoint(r) in ("sendMessage", "editMessageText")]

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

            # ARM1 — spanless three-page reply, one surrogate per page.
            a1 = ("\ud800" + "x" * 4095 + "\n" + "\ud800" + "y" * 4095
                  + "\n" + "\ud800" + "z" * 4095)
            await run_arm("spanless-3-pages", ch.send_response(a1, CTX()),
                          [("sendMessage", R + "x" * 4095),
                           ("sendMessage", R + "y" * 4095),
                           ("sendMessage", R + "z" * 4095)],
                          outcome=DeliveryOutcome.DELIVERED)

            # ARM2 — one page whose link converts and a surrogate after it.
            a2 = "[link](https://example.test/a) \ud800"
            flats = await run_arm("single-page-link", ch.send_response(a2, CTX()),
                          [("sendMessage", "link " + R)],
                          outcome=DeliveryOutcome.DELIVERED)
            if flats and ("text_link" not in flats[0].get("entities", "")
                          or "https://example.test/a" not in flats[0].get("entities", "")):
                failures.append("single-page-link: link entity missing from the request")

            # ARM3 — plain send with a LOW surrogate (kills high-only cleaning).
            await run_arm("plain-send-low-surrogate", ch.send("\udc00 hello", CTX()),
                          [("sendMessage", R + " hello")],
                          outcome=DeliveryOutcome.DELIVERED)

            # ARM5 — two pages whose bold spans convert, surrogate after each.
            a5 = ("**bold** " + "x" * 4080 + "\ud800\n"
                  + "**bold** " + "y" * 4080 + "\ud800")
            flats = await run_arm("convertible-2-pages", ch.send_response(a5, CTX()),
                          [("sendMessage", "bold " + "x" * 4080 + R),
                           ("sendMessage", "bold " + "y" * 4080 + R)],
                          outcome=DeliveryOutcome.DELIVERED)
            if flats and '"bold"' not in flats[0].get("entities", ""):
                failures.append("convertible-2-pages: page 1 lost its bold entity")

            # ARM6 — streamed reply: page 1 is an EDIT of the streamed message;
            # pages 2-3 lose their spans to the surrogate and take the
            # conversion-failure arm, which already replaced it at the base.
            a6 = ("**bold** " + "a" * 4080 + "\ud800\n"
                  + "".join("**\ud800" + "b" * 4093 + "**\n" for _ in range(2)))
            state = {"message_id": 99}
            async def on_token(_t):
                state  # a closure CELL: the finalizer peeks the message id there
            flats = await run_arm("stream-finalize",
                          ch.finalize_response_stream(a6, CTX(), on_token),
                          [("editMessageText", "bold " + "a" * 4080 + R),
                           ("sendMessage", R + "b" * 4093),
                           ("sendMessage", R + "b" * 4093 + "\n")],
                          outcome=DeliveryOutcome.DELIVERED)
            if flats and flats[0].get("message_id") != "99":
                failures.append("stream-finalize: the edit did not target the streamed message")

            # Media — the caption is a form value too; the bytes are not.
            await run_arm("media-caption",
                          ch.send_media(MEDIA, "document", "x.bin", CTX(), caption="\ud800"),
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
    sender — block, single-page, plain, convertible-entities, streamed edit and
    media caption — so the reply is DELIVERED with the character replaced
    instead of raising before any request leaves.

    Counts are the assertion. At the base every block arm raises
    ``NetworkError`` whose cause is ``UnicodeEncodeError`` with ZERO requests,
    and the streamed arm loses its edit (0 edits, 2 sends, UNKNOWN); the
    subprocess prints each arm's counts and raise so the failure names the
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
