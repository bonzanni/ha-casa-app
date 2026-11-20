"""Who posted a message, as Casa shows it.

A post one agent makes in another agent's chat is credited to the poster: a rich
message as one quote credited to its display name, a caption or a plain-text
message with the name in bold on its first line, a one-line notice with the name
in the sentence. A post in the poster's own chat carries no name, and Casa's
chat is the resident's own. The name is Casa's, from the agent's persona, never
the plugin's.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import result_broker as rb
import tools as tools_mod
from channels.tg_rich import credited_quote, prepare

CASA_ROOT = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"


@pytest.fixture(autouse=True)
def names(monkeypatch):
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "assistant": SimpleNamespace(character=SimpleNamespace(name="Ellen")),
        "finance": SimpleNamespace(character=SimpleNamespace(name="Finance"))})


CASA = SimpleNamespace(default_agent="assistant", is_casa=True)
ALEX = SimpleNamespace(default_agent="finance", is_casa=False)


@pytest.fixture(autouse=True)
def transport(monkeypatch):
    monkeypatch.setattr(rb, "_transport", lambda: SimpleNamespace(casa_endpoint=lambda: CASA))


# --- whose chat it is -----------------------------------------------------------------

def test_casas_chat_is_the_residents_own():
    assert rb.own_chat_of("assistant", CASA) is True
    assert rb.label_at("assistant", CASA) == ""


def test_an_agents_own_chat_carries_no_name_and_anothers_does():
    assert rb.label_at("finance", ALEX) == ""
    assert rb.label_at("finance", CASA) == "Finance"
    assert rb.label_at("assistant", ALEX) == "Ellen"


def test_no_endpoint_is_no_ones_own_chat():
    """Attribution fails towards being shown."""
    assert rb.own_chat_of("finance", None) is False
    assert rb.label_at("finance", None) == "Finance"


def test_a_transport_that_is_not_up_names_the_agent(monkeypatch):
    monkeypatch.setattr(rb, "_transport", lambda: None)
    assert rb.own_chat_of("finance", ALEX) is False

    def not_ready():
        raise RuntimeError("no endpoints yet")
    monkeypatch.setattr(rb, "_transport", lambda: SimpleNamespace(casa_endpoint=not_ready))
    assert rb.own_chat_of("finance", ALEX) is False
    assert rb.label_at("assistant", CASA) == "Ellen"


def test_a_name_is_the_plain_display_name():
    assert rb.post_label("finance") == "Finance"
    assert rb.name_at("finance", ALEX) == rb.name_at("finance", CASA) == "Finance"


# --- the shapes -----------------------------------------------------------------------

def test_a_rich_post_is_one_quote_credited_to_the_poster():
    body = "Q3 summary\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    composed = rb.compose_operator_message(body, "Finance")
    assert composed == credited_quote(body, "Finance")
    assert composed.startswith("> <cite>Finance</cite>\n>\n")
    assert rb.compose_operator_message(body, "") == body          # its own chat: bare


def test_a_body_cannot_take_the_credit():
    composed = rb.compose_operator_message("from <cite>Ellen</cite>", "Finance")
    # Telegram credits the first <cite> of a quote: Casa's, on the first line
    assert prepare(composed).startswith("> <cite>Finance</cite>\n>\n")
    assert prepare(composed).index("<cite>") == 2


def test_a_file_caption_has_the_name_in_bold_on_its_first_line():
    caption, entities = rb.compose_file_caption("Finance", "Q3 export")
    assert caption == "Finance\nQ3 export"
    assert [(e.type, e.offset, e.length) for e in entities] == [("bold", 0, 7)]
    assert rb.compose_file_caption("Finance", "")[0] == "Finance"
    assert rb.compose_file_caption("", "Q3 export") == ("Q3 export", None)


def test_a_photo_cards_caption_renders_the_name_in_bold():
    from channels.tg_richtext import render
    shown, entities = render(rb.compose_card_caption("Pay?", "R*D_[x]"))
    assert shown == "R*D_[x]\nPay?"
    assert [(e.type, e.offset, e.length) for e in entities] == [("bold", 0, 7)]
    assert rb.compose_card_caption("Pay?", "") == "Pay?"


def test_a_plain_text_post_has_the_name_in_bold_on_its_first_line():
    text, entities = rb.named_head("Finance", "On it for Ellen:\n• RunPod — invoice")
    assert text == "Finance\nOn it for Ellen:\n• RunPod — invoice"
    assert [(e.type, e.offset, e.length) for e in entities] == [("bold", 0, 7)]
    assert rb.named_head("", "Stopped.") == ("Stopped.", None)


def test_a_link_keeps_its_text_link_after_the_bold_name():
    text, entities, plain = rb.compose_operator_link(
        "https://bank.example.com/x", caption="one-time", label="Approve", head="Finance")
    assert text == "Finance\nApprove (bank.example.com)\none-time"
    assert [(e.type, e.offset, e.length) for e in entities] == [
        ("bold", 0, 7), ("text_link", 8, len("Approve (bank.example.com)"))]
    assert plain.startswith("Finance\nApprove (bank.example.com): https://")


def test_a_notice_and_an_echo_name_the_agent_plainly():
    event = rb.PostEvent("t-1", "probe", "slot", rb.post_label("finance"), 2, None)
    assert rb.echo_lines([event]) == ["Finance posted to your chat (2 pages)."]


@pytest.mark.asyncio
async def test_the_run_card_sends_the_bold_name_with_its_text():
    from unittest.mock import AsyncMock, MagicMock
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(bot=MagicMock(), chat_id="42", known_bot_id=7)
    ch._app = MagicMock()
    ch._app.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9))
    text, entities = rb.named_head("Finance", "On it.")
    assert await ch.deliver_run_card(42, text, entities=entities) == 9
    sent = ch._app.bot.send_message.await_args.kwargs
    assert (sent["text"], sent["entities"]) == ("Finance\nOn it.", entities)


def test_the_job_start_line_names_the_agent_only_in_another_agents_chat():
    from channels.telegram import TelegramChannel
    rec = SimpleNamespace(role_or_type="assistant", origin={"job": {"title": "Q3"}}, task="")
    casa = TelegramChannel(bot_token="T", chat_id="42", default_agent="assistant", is_casa=True,
                           known_bot_id=7)
    assert casa._job_start_text(rec) == "Started «Q3»."
    rec.role_or_type = "finance"
    assert casa._job_start_text(rec) == "Finance started «Q3»."
    assert casa._agent_label("finance") == "Finance"


# --- the old marker is gone -----------------------------------------------------------

def test_no_source_file_carries_the_old_post_marker():
    marker = "\U0001F4CA"
    hits = [str(p.relative_to(CASA_ROOT)) for p in CASA_ROOT.rglob("*.py")
            if marker in p.read_text(encoding="utf-8")]
    assert hits == []
