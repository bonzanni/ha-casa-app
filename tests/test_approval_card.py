"""Card model, rendering and fallbacks for operator approval cards."""

import json

from approval_card import (
    Card, CardLine, card_from_reply, render_card, shift_entities,
    text_fallback, tool_fallback,
)
from text_util import utf16_len


def display_utf16_index_of(display, s):
    return utf16_len(display[:display.index(s)])


def test_layout_matches_the_approved_card():
    card = Card(title="🔐 Allow Ellen to send an email to Marco?", summary="Sends the invoice.",
                lines=(CardLine("To", "marco@example.com"), CardLine("Hidden copy", "a@b.c", True)))
    display, ents = render_card(card)
    assert display == ("🔐 Allow Ellen to send an email to Marco?\n\nSends the invoice.\n\n"
                       "To: marco@example.com\n⚠️ Hidden copy: a@b.c")
    assert [(e.type, e.offset, e.length) for e in ents] == [
        ("bold", 0, utf16_len("🔐 Allow Ellen to send an email to Marco?")),
        ("bold", display_utf16_index_of(display, "To:"), 3),
        ("bold", display_utf16_index_of(display, "Hidden copy:"), 12)]


def test_field_content_never_shapes_an_entity():
    nasty = "[Invoice](https://evil.example) **urgent** `x` 😀"
    title = f"🔐 Allow Ellen to {nasty}?"
    label = "**L** [a](https://b.c)"
    card = Card(title=title, summary=nasty, lines=(CardLine(label, nasty),))
    display, ents = render_card(card)
    assert display == f"{title}\n\n{nasty}\n\n{label}: {nasty}"          # the whole text, literal
    spans = [(e.type, e.offset, e.length) for e in ents]
    assert spans == [("bold", 0, utf16_len(title)),
                     ("bold", utf16_len(f"{title}\n\n{nasty}\n\n"), utf16_len(label) + 1)]
    decoded = [display.encode("utf-16-le")[2 * o: 2 * (o + n)].decode("utf-16-le") for _, o, n in spans]
    assert decoded == [title, label + ":"]


def test_reply_bounds_are_checked_in_utf16_and_never_clipped():
    ok = json.dumps({"action": "a" * 80, "summary": "s", "lines": []})
    assert card_from_reply(ok, who="Ellen") is not None
    assert card_from_reply(json.dumps({"action": "😀" * 41, "summary": "s", "lines": []}), who="Ellen") is None  # 82 units
    for bad in ({"action": "x", "summary": "s" * 241, "lines": []},
                {"action": "x", "summary": "s", "lines": [{"label": "l", "value": "v"}] * 6},
                {"action": "x", "summary": "a\nb", "lines": []},
                {"action": "x‮", "summary": "s", "lines": []}):
        assert card_from_reply(json.dumps(bad), who="Ellen") is None
    assert card_from_reply("not json", who="Ellen") is None


def test_reply_builds_the_title_from_who_and_action_unless_given():
    reply = json.dumps({"action": "send an email to Marco", "summary": "s", "lines": []})
    assert card_from_reply(reply, who="Ellen").title == "🔐 Allow Ellen to send an email to Marco?"
    assert card_from_reply(reply, who="Ellen", title="🔐 Erase?").title == "🔐 Erase?"


def test_reply_line_bounds_and_shape_are_checked():
    def line(**kw):
        return json.dumps({"action": "x", "summary": "s", "lines": [kw]})
    assert card_from_reply(line(label="l" * 32, value="v" * 160), who="E") is not None
    assert card_from_reply(line(label="l" * 33, value="v"), who="E") is None
    assert card_from_reply(line(label="l", value="v" * 161), who="E") is None
    assert card_from_reply(line(label="l", value="a\nb"), who="E") is None
    assert card_from_reply(line(label="l", value=3), who="E") is None
    assert card_from_reply(json.dumps(["x"]), who="E") is None
    assert card_from_reply(json.dumps({"action": "x", "summary": "s"}), who="E") is None


def test_only_the_first_two_sensitive_lines_are_flagged():
    def flags(*marks):
        reply = json.dumps({"action": "x", "summary": "s", "lines": [
            {"label": str(i), "value": "v", "sensitive": m} for i, m in enumerate(marks)]})
        return [l.sensitive for l in card_from_reply(reply, who="Ellen").lines]
    assert flags(True, True) == [True, True]
    assert flags(True, True, True) == [True, True, False]
    assert flags(False, True, False, True, True) == [False, True, False, True, False]


def test_worst_case_model_card_fits():
    # Every field at its bound in astral characters (two units each), two
    # warning marks: title 2 + 7 + 128 + 4 + 80 + 1 = 222, summary 2 + 240,
    # line separators 2 + 4, marks 2 * 3, labels 5 * (32 + 2), values 5 * 160.
    who = "😀" * 64                                 # display names are capped at 64 code points
    reply = {"action": "😀" * 40, "summary": "😀" * 120,
             "lines": [{"label": "😀" * 16, "value": "😀" * 80, "sensitive": True} for _ in range(5)]}
    display, _ = render_card(card_from_reply(json.dumps(reply), who=who))
    assert utf16_len(display) == 1446


def test_tool_fallback_respects_every_budget():
    # canonical order is recursive sorted keys: "body", the twelve "p…" leaves, then "to"
    huge = {"p" * 350: {f"k{i:02d}": "v" for i in range(12)}, "body": "x" * 5000,
            "to": ["a@b.c", "d@e.f"]}
    card = tool_fallback(who="Ellen", plugin="gmail", args=huge)
    display, _ = render_card(card)
    assert card.title == "🔐 Allow Ellen to use gmail?"
    assert utf16_len(display) <= 2600
    assert all(utf16_len(l.label) <= 40 for l in card.lines)
    assert card.lines[0].value.endswith("(5000 characters)")
    assert len(card.lines) == 13 and card.lines[-1].value == "…and 2 more"


def test_tool_fallback_of_a_call_without_arguments_shows_the_plugins_description():
    card = tool_fallback(who="Ellen", plugin="gmail", args={},
                         summary="Revokes Gmail's access at Google.\nReconnecting needs a new sign-in.")
    display, ents = render_card(card)
    assert display == ("🔐 Allow Ellen to use gmail?\n\n"
                       "Revokes Gmail's access at Google. Reconnecting needs a new sign-in.")
    assert len(ents) == 1                               # the title only: the text stays literal
    # Without a description the empty call is shown as before; with arguments
    # the description is not used.
    assert render_card(tool_fallback(who="E", plugin="p", args={}))[0].endswith("\n\n{}")
    assert tool_fallback(who="E", plugin="p", args={"a": "1"}, summary="S").summary == ""


def test_tool_fallback_description_is_literal_and_within_budget():
    card = tool_fallback(who="E", plugin="p" * 2500, args={}, summary="**x‮**" + "y" * 900)
    display, _ = render_card(card)
    assert utf16_len(display) <= 2600
    assert card.summary.startswith("**x\\u202e**") and card.summary.endswith("…")
    assert utf16_len(tool_fallback(who="E", plugin="p", args={}, summary="y" * 900).summary) == 400


def test_tool_fallback_joins_a_scalar_list():
    card = tool_fallback(who="Ellen", plugin="gmail", args={"to": ["a@b.c", "d@e.f"]})
    assert [(l.label, l.value) for l in card.lines] == [("To", "a@b.c, d@e.f")]


def test_tool_fallback_says_how_many_lines_it_left_out():
    card = tool_fallback(who="Ellen", plugin="p", args={f"k{i:02d}": "v" for i in range(20)})
    assert len(card.lines) == 13 and card.lines[-1].value == "…and 8 more"


def test_tool_fallback_labels_nested_paths_and_cuts_long_values():
    card = tool_fallback(who="E", plugin="p", args={"mail_to": {"first_name": "v" * 161}})
    (line,) = card.lines
    assert line.label == "Mail to › First name"
    assert line.value == "v" * 120 + "… (161 characters)"


def test_tool_fallback_escapes_unsafe_values_and_stops_at_the_card_budget():
    card = tool_fallback(who="E", plugin="p", args={"a": "x‮y\n"})
    assert card.lines[0].value == "x\\u202ey\\u000a"
    # a long plugin name eats the card budget before the twelve-line cap
    wide = {f"k{i:02d}" + "x" * 50: "v" * 160 for i in range(12)}
    card = tool_fallback(who="E", plugin="p" * 600, args=wide)
    display, _ = render_card(card)
    assert utf16_len(display) <= 2600
    shown = len(card.lines) - 1
    assert 0 < shown < 12
    assert card.lines[-1].label == "" and card.lines[-1].value == f"…and {12 - shown} more"


def test_lone_surrogates_never_reach_telegram():
    for card in (Card(title="🔐 Allow Ellen to x\ud800?", summary="s\udfff", lines=(CardLine("l\ud800", "v\udc00"),)),
                 tool_fallback(who="Ellen", plugin="p", args={"k": "v\ud800"}),
                 text_fallback("🔐 T\ud800\nbody\udfff")):
        display, ents = render_card(card)
        display.encode("utf-8")                     # raises on a lone surrogate
        for e in ents:
            assert e.offset + e.length <= utf16_len(display)


def test_tool_fallback_survives_deep_nesting():
    deep = cur = {}
    for _ in range(5000):
        cur["n"] = {}; cur = cur["n"]
    cur["leaf"] = "v"
    card = tool_fallback(who="Ellen", plugin="p", args={"a": deep, "b": "x"})
    display, _ = render_card(card)
    assert utf16_len(display) <= 2600 and any(l.value == "…" for l in card.lines)


def test_text_fallback_keeps_the_body_literal_with_a_bold_title():
    body = "🔐 Plugin callback consent\n\nPlugin 'gmail' wants … GET /callback/x"
    display, ents = render_card(text_fallback(body))
    assert display == body and [(e.offset, e.length) for e in ents] == [(0, utf16_len("🔐 Plugin callback consent"))]


def test_shift_entities_moves_every_offset_by_the_prefix():
    _, ents = render_card(Card(title="T"))
    assert shift_entities(ents, "(note 😀)\n\n")[0].offset == utf16_len("(note 😀)\n\n")
