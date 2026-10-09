"""v0.84.0 (round 4, spec D2 items 1 & 4) — ``resolve_button_labels``, the ONE
pure whole-set button-label resolver, plus its content-free floor telemetry.

Round 3's per-option ELISION LADDER (``_elide_one`` / ``_resolve_label_collisions``
/ ``_short_option_label``) is gone entirely (D2: "Drop the ``_short_option_label``
elision ladder entirely (garbled, inconsistent — the '1 · A —…MCP…MCPB' failure)").
Labels are now MODEL-generated (the agent supplies an optional ``short`` per
option) and resolved with WHOLE-SET semantics: either EVERY option has a usable
short (non-blank after strip, pairwise-distinct, and whose final DECORATED
caption fits the 64-char product contract), in which case the whole set renders
``n · <short>`` verbatim — or the WHOLE set floors to ``Option 1``, ``Option 2``,
… Never mixed (some real shorts + some derived/floored), never mutated, never a
rejection.

``options`` items are either a bare ``str`` (full label, no short) or
``{"label": str, "short": ...}``. The DECORATED caption the resolver validates
is ``f"{i+1} · {short}"`` for a single-select ask, or ``f"☑ {i+1} · {short}"``
for a multi ask (the toggle-many keyboard prepends the checkbox glyph to the
stored caption) — so the SAME short can pass for single-select and fail for
multi at the exact same length (Sol r3-5's decoration-budget-is-an-input point).

``floored_ask_telemetry`` is the CONTENT-FREE log line (Sol r2-7): option count,
floor reason (``blank|dup|too_long``), a short-presence bitmap, and a
bounded hash of the option set — NEVER the option/question text itself.
"""

from __future__ import annotations

from channels.telegram import (
    _ASK_BUTTON_CAPTION_CAP,
    floored_ask_telemetry,
    resolve_button_labels,
)


# ---------------------------------------------------------------------------
# (a) all-shorts-usable → verbatim ``n · <short>`` captions
# ---------------------------------------------------------------------------


def test_all_shorts_usable_returns_verbatim_captions() -> None:
    options = [
        {"label": "Python MCP server, packaged as MCPB", "short": "Py-MCPB"},
        {"label": "Python MCP server via a venv install", "short": "Py-venv"},
        {"label": "A curl-based install skill", "short": "curl-skill"},
    ]
    assert resolve_button_labels(options, multi=False) == [
        "1 · Py-MCPB", "2 · Py-venv", "3 · curl-skill",
    ]


def test_all_shorts_usable_multi_also_verbatim_undecorated() -> None:
    # The RETURNED caption is always the undecorated stored form (``n ·
    # <short>``) even for a multi ask — the ☐/☑ glyph is a separate, mutable
    # render-time concern (D2 item 3), never part of the persisted caption.
    options = [
        {"label": "Python MCP server, packaged as MCPB", "short": "Py-MCPB"},
        {"label": "A curl-based install skill", "short": "curl-skill"},
    ]
    assert resolve_button_labels(options, multi=True) == [
        "1 · Py-MCPB", "2 · curl-skill",
    ]


# ---------------------------------------------------------------------------
# (a2) wb1-1..4 · wb1-4 — a non-blank short is used VERBATIM, never stripped.
# D2 says verbatim-or-floor, never mutated. ``strip()`` is a BLANK predicate
# only; distinctness, caption construction, and the ≤64 decorated-length check
# all read the RAW short.
# ---------------------------------------------------------------------------


def test_non_blank_short_with_surrounding_whitespace_renders_verbatim() -> None:
    # "  Setup  " is non-blank after strip, so it is USABLE — and it renders
    # VERBATIM ("1 ·   Setup  "), not mutated to the stripped "1 · Setup".
    options = [
        {"label": "Configure the server", "short": "  Setup  "},
        {"label": "Tear the server down", "short": "Teardown"},
    ]
    assert resolve_button_labels(options, multi=False) == [
        "1 ·   Setup  ", "2 · Teardown",
    ]


def test_distinctness_uses_raw_short_not_stripped() -> None:
    # "Setup" and " Setup " are DISTINCT raw values → the whole set is usable
    # and renders verbatim. Stripping first would have collapsed them to a
    # duplicate and floored the set (a mutation-driven decision D2 forbids).
    options = [
        {"label": "First", "short": "Setup"},
        {"label": "Second", "short": " Setup "},
    ]
    assert resolve_button_labels(options, multi=False) == [
        "1 · Setup", "2 ·  Setup ",
    ]


def test_length_check_uses_raw_short_floors_when_padding_overflows() -> None:
    # 60 non-space chars + 4 trailing spaces == 64 raw; the decorated caption
    # "1 · " + 64 == 68 > 64 → the RAW length overflows and the set floors. A
    # stripped length check (60 → decorated 64) would have WRONGLY passed.
    short = "s" * 60 + "    "
    options = [{"label": "full label text, far too wide for one button", "short": short}]
    assert resolve_button_labels(options, multi=False) == ["Option 1"]


# ---------------------------------------------------------------------------
# (b) one blank short floors the WHOLE set
# ---------------------------------------------------------------------------


def test_one_blank_short_floors_whole_set() -> None:
    options = [
        {"label": "Python MCP server, packaged as MCPB", "short": "Py-MCPB"},
        {"label": "Python MCP server via a venv install", "short": "   "},
        {"label": "A curl-based install skill for the server", "short": "curl"},
    ]
    assert resolve_button_labels(options, multi=False) == [
        "Option 1", "Option 2", "Option 3",
    ]


# ---------------------------------------------------------------------------
# (c) duplicate shorts float the WHOLE set
# ---------------------------------------------------------------------------


def test_duplicate_shorts_floor_whole_set() -> None:
    options = [
        {"label": "Python MCP server, packaged as MCPB", "short": "Setup"},
        {"label": "Python MCP server via a venv install", "short": "Setup"},
        {"label": "A curl-based install skill for the server", "short": "curl"},
    ]
    assert resolve_button_labels(options, multi=False) == [
        "Option 1", "Option 2", "Option 3",
    ]


def test_duplicate_shorts_case_insensitive_floor() -> None:
    # Pairwise-distinct is casefold-insensitive — "Setup"/"setup" collide.
    options = [
        {"label": "Python MCP server, packaged as MCPB", "short": "Setup"},
        {"label": "Python MCP server via a venv install", "short": "setup"},
    ]
    assert resolve_button_labels(options, multi=False) == ["Option 1", "Option 2"]


# ---------------------------------------------------------------------------
# (d) non-string short treated as absent → the option offers its own label
#     (#1386); it floors only when that label is too wide
# ---------------------------------------------------------------------------


def test_non_string_short_treated_as_absent_uses_own_label() -> None:
    options = [
        {"label": "Python MCP server", "short": "Py-MCPB"},
        {"label": "Python venv install", "short": "Py-venv"},
        {"label": "curl install skill", "short": 7},  # non-string — absent
    ]
    assert resolve_button_labels(options, multi=False) == [
        "1 · Py-MCPB", "2 · Py-venv", "3 · curl install skill",
    ]
    wide = [dict(o) for o in options]
    wide[2]["label"] = "A curl-based install skill for the server"
    assert resolve_button_labels(wide, multi=False) == [
        "Option 1", "Option 2", "Option 3",
    ]
    assert "reason=too_long" in floored_ask_telemetry(wide)


def test_bare_str_option_has_no_short_uses_own_label() -> None:
    # A bare ``str`` item never carries a short — beside a dict item with a
    # short it shows its own words, numbered like the rest (#1386).
    options = [
        "Python MCP server",
        {"label": "Python venv install", "short": "Py-venv"},
    ]
    assert resolve_button_labels(options, multi=False) == [
        "1 · Python MCP server", "2 · Py-venv"]
    assert "reason=none" in floored_ask_telemetry(options)


# ---------------------------------------------------------------------------
# (e) 64-char decorated-caption boundary; the SAME short diverges
#     single-select (passes) vs. multi (floors) at the identical length.
# ---------------------------------------------------------------------------


def test_single_select_caption_exactly_64_chars_passes() -> None:
    short = "s" * 60  # "1 · " (4) + 60 == 64
    options = [{"label": "full label text, far too wide for one button", "short": short}]
    caption = resolve_button_labels(options, multi=False)[0]
    assert len(caption) == _ASK_BUTTON_CAPTION_CAP == 64
    assert caption == f"1 · {short}"


def test_single_select_caption_65_chars_floors() -> None:
    short = "s" * 61  # "1 · " (4) + 61 == 65 > 64
    options = [{"label": "full label text, far too wide for one button", "short": short}]
    assert resolve_button_labels(options, multi=False) == ["Option 1"]


def test_multi_decorated_caption_exactly_64_passes() -> None:
    short = "s" * 58  # "☑ 1 · " (6) + 58 == 64
    options = [{"label": "full label text, far too wide for one button", "short": short}]
    caption = resolve_button_labels(options, multi=True)[0]
    assert caption == f"1 · {short}"  # returned caption stays undecorated
    assert len(f"☑ {caption}") == 64


def test_same_short_passes_single_but_floors_multi() -> None:
    # Length chosen so the SINGLE-select decorated caption sits exactly at the
    # 64-char boundary (passes) while the MULTI decorated caption (2 extra
    # chars for "☑ ") pushes past it (floors) — the divergence the decoration
    # budget must catch (Sol r3-5).
    short = "s" * 60
    options = [{"label": "full label text, far too wide for one button", "short": short}]

    single = resolve_button_labels(options, multi=False)
    multi = resolve_button_labels(options, multi=True)

    assert single == [f"1 · {short}"]
    assert len(single[0]) == 64
    assert multi == ["Option 1"]
    assert len(f"☑ {single[0]}") == 66


# ---------------------------------------------------------------------------
# (f) telemetry: content-free — count/reason/bitmap/hash, NEVER option text
# ---------------------------------------------------------------------------


def test_telemetry_line_has_dimensions_but_no_option_text() -> None:
    options = [
        {"label": "Secret project codename Falcon, phase one", "short": "Falcon"},
        {"label": "Secret project codename Osprey, phase two", "short": None},
    ]
    telemetry = floored_ask_telemetry(options)

    assert "count=2" in telemetry
    assert "reason=too_long" in telemetry
    assert "shorts=" in telemetry
    assert "hash=" in telemetry

    # NEVER the option/question text — not the label, not the short.
    assert "Falcon" not in telemetry
    assert "Osprey" not in telemetry
    assert "codename" not in telemetry
    assert "Secret" not in telemetry


def test_telemetry_bitmap_reflects_short_presence_per_option() -> None:
    options = [
        {"label": "A", "short": "a"},
        {"label": "B", "short": None},
        "C",  # bare str — no short
    ]
    telemetry = floored_ask_telemetry(options)
    assert "shorts=100" in telemetry


def test_telemetry_hash_is_bounded_and_deterministic() -> None:
    options = [{"label": "A", "short": "a"}, {"label": "B", "short": "b"}]
    t1 = floored_ask_telemetry(options)
    t2 = floored_ask_telemetry(options)
    assert t1 == t2  # pure, deterministic
    h1 = t1.split("hash=", 1)[1].split()[0]
    assert len(h1) == 12
    assert all(c in "0123456789abcdef" for c in h1)


def test_telemetry_multi_uses_multi_decoration_budget() -> None:
    # A short that floors under multi decoration but would PASS single-select
    # (verbatim, no floor) must report reason ``too_long`` when telemetry is
    # asked about the multi ask specifically, and no floor reason at all for
    # the single-select ask (it never floored).
    short = "s" * 60
    options = [{"label": "full label text, far too wide for one button", "short": short}]
    assert "reason=too_long" in floored_ask_telemetry(options, multi=True)
    assert "reason=none" in floored_ask_telemetry(options, multi=False)


# ---------------------------------------------------------------------------
# (g) Sol's live case — no shorts anywhere → floor, never a garbled elision
# ---------------------------------------------------------------------------


def test_sol_live_case_no_shorts_floors_never_elides() -> None:
    options = [
        "Option A — Python MCP server, MCPB packaged",
        "Option B — Python MCP server via venv",
        "Option C — curl-based install skill",
    ]
    labels = resolve_button_labels(options, multi=False)
    assert labels == ["Option 1", "Option 2", "Option 3"]
    for lab in labels:
        assert "MCP" not in lab
        assert "…" not in lab
        assert " · " not in lab


# ---------------------------------------------------------------------------
# Purity / never-raises
# ---------------------------------------------------------------------------


def test_never_raises_on_empty_options() -> None:
    assert resolve_button_labels([], multi=False) == []
    assert "count=0" in floored_ask_telemetry([])


def test_pure_no_mutation_of_input() -> None:
    options = [{"label": "A", "short": "a"}, {"label": "B", "short": "b"}]
    snapshot = [dict(o) for o in options]
    resolve_button_labels(options, multi=False)
    floored_ask_telemetry(options)
    assert options == snapshot


# ---------------------------------------------------------------------------
# #1386 — an option without a short shows its OWN words when they fit
# ---------------------------------------------------------------------------


def test_1386_short_options_without_shorts_render_their_own_words() -> None:
    # The live case: the assistant's purge confirmation, bare options, no
    # shorts — the buttons read the options, not "Option 1" / "Option 2".
    assert resolve_button_labels(["Delete", "Keep"], multi=False) == [
        "Delete", "Keep"]
    from channels.telegram import short_option_labels
    assert short_option_labels(["Delete", "Keep"]) == ["Delete", "Keep"]


def test_1386_own_words_fit_is_a_width_boundary() -> None:
    from channels.telegram import _ASK_BUTTON_WORDS_FIT
    fits = "w" * _ASK_BUTTON_WORDS_FIT
    wide = "w" * (_ASK_BUTTON_WORDS_FIT + 1)
    assert resolve_button_labels([fits, "Keep"], multi=False) == [fits, "Keep"]
    assert resolve_button_labels([wide, "Keep"], multi=False) == [
        "Option 1", "Option 2"]
    assert "reason=too_long" in floored_ask_telemetry([wide, "Keep"])


def test_1386_a_missing_short_borrows_the_options_own_label() -> None:
    # Mixed: one option has a short, the other is already a couple of words —
    # numbered captions, the short-less option showing its own label.
    options = [
        "Personal Gmail",
        {"label": "Configure the enterprise SSO integration", "short": "SSO"},
    ]
    assert resolve_button_labels(options, multi=False) == [
        "1 · Personal Gmail", "2 · SSO"]


def test_1386_failed_shorts_fall_back_to_fitting_labels() -> None:
    options = [
        {"label": "Morning", "short": "Setup"},
        {"label": "Evening", "short": "setup"},
    ]
    assert resolve_button_labels(options, multi=False) == ["Morning", "Evening"]
    assert "reason=none" in floored_ask_telemetry(options)


def test_1386_multi_own_words_stay_undecorated_in_storage() -> None:
    assert resolve_button_labels(["Lights", "Heating"], multi=True) == [
        "Lights", "Heating"]


def test_ask_user_description_names_the_button_fit() -> None:
    """#1390: the description is where the model learns which options fit a
    button as written and that a longer one takes a ``short``. The number it
    quotes must be the renderer's own fit, or the steer points at the wrong
    length; the ``short`` it names must be a key the schema accepts."""
    import tools
    from channels.telegram import _ASK_BUTTON_WORDS_FIT

    desc = tools.ask_user.description
    assert f"{_ASK_BUTTON_WORDS_FIT} characters or fewer" in desc
    assert '"short"' in desc
    item = tools.ask_user.input_schema["properties"]["options"]["items"]
    obj = next(a for a in item["anyOf"] if a["type"] == "object")
    assert set(obj["properties"]) == {"label", "short"}
