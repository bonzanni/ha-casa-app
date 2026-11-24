"""The text of an operator question in Telegram: its button labels and its body.

Pure text logic with no Telegram I/O: the whole-set button-label resolver
(the options' own words, the agent's shorts, or the numbered ``Option n``
floor), and the one rendered ask body the DM ask posters share.
"""

from __future__ import annotations

# v0.81.0 (W-R3, Sol r1-5): button labels are DERIVED channel-side from the
# full option so they stay readable — Telegram truncates long labels to a few
# words, which made most choices unpickable. The full option text lives VERBATIM
# in the message body (``render_ask_body`` below); the button carries
# only a short summary. The button's IDENTITY stays the option INDEX in
# ``callback_data`` — the label is display-only, so nothing is lost by shortening.
#
# Round 4 (spec D2, Sol r1-3/r2-3/r2-7/r3-5): the v0.83.0 per-option elision
# ladder is GONE — it garbled labels ("1 · A —…MCP…MCPB"). Button labels are now MODEL-generated: the agent
# supplies an optional ``short`` per option, resolved with WHOLE-SET semantics by
# ``resolve_button_labels`` below. #1386: an option without a short offers its
# own label, so a set of short options ("Delete" / "Keep") renders its own
# words; the WHOLE set floors to ``Option 1``, ``Option 2``, … only when the
# words do not fit, collide or are blank — never mutated, never a rejection.
_ASK_BUTTON_CAPTION_CAP = 64
# #1386: an option's OWN label becomes its button when it is at most this many
# characters — about what a full-width button shows on a phone without cutting.
_ASK_BUTTON_WORDS_FIT = 32


def _button_short(item: "str | dict") -> str | None:
    """The agent-supplied ``short`` of an options-list ITEM, iff it is a
    ``str`` (any string — blank or not; blankness is checked separately).
    ``None`` when absent, non-string, or the item is a bare ``str`` (a bare
    label never carries a short)."""
    if isinstance(item, dict):
        short = item.get("short")
        if isinstance(short, str):
            return short
    return None


def _button_label_text(item: "str | dict") -> str:
    """The full label text of an options-list ITEM (a bare ``str``, or a
    dict's ``"label"``)."""
    if isinstance(item, dict):
        return str(item.get("label", ""))
    return str(item)


def _floor_captions(n: int) -> list[str]:
    """The whole-set floor: numbered, content-free placeholders."""
    return [f"Option {i + 1}" for i in range(n)]


def _fit_captions(
    texts: list, own: list, numbered: bool,
) -> "tuple[str | None, list[str]]":
    """One whole-set attempt: ``(None, captions)`` when every text is non-blank,
    the set is pairwise-distinct (casefold) and every caption fits, else
    ``(reason, floor)``. ``own[i]`` marks a text that is the option's own label
    (it must fit ``_ASK_BUTTON_WORDS_FIT``); ``numbered`` prefixes ``n · ``."""
    n = len(texts)
    # wb1-4: ``strip()`` is used ONLY as the BLANK predicate (verbatim-or-
    # floor, never mutated): distinctness, captions and lengths read the RAW
    # text, so ``"  Setup  "`` renders ``"1 ·   Setup  "``.
    if any(t.strip() == "" for t in texts):
        return "blank", _floor_captions(n)
    if len({t.casefold() for t in texts}) != n:
        return "dup", _floor_captions(n)
    captions = [f"{i + 1} · {t}" if numbered else t for i, t in enumerate(texts)]
    for i, cap in enumerate(captions):
        if (own[i] and len(texts[i]) > _ASK_BUTTON_WORDS_FIT) or (
                len(cap) > _ASK_BUTTON_CAPTION_CAP):
            return "too_long", _floor_captions(n)
    return None, captions


def _classify_button_labels(options: list) -> list[str]:
    """Whole-set classification behind ``resolve_button_labels``
    (Sol r2-3/r2-7).

    #1386: each option offers its agent-supplied ``short`` when it has one (a
    ``str``), otherwise its OWN label. When no option has a short, the buttons
    are the labels verbatim (``Delete`` / ``Keep``); when any has one, every
    caption is ``n · <text>`` so a summary still points at its numbered full
    option in the body. The set floors to ``Option n`` only when a text is
    blank, two collide (casefold), an own label is wider than
    ``_ASK_BUTTON_WORDS_FIT``, or a caption exceeds the 64-char contract
    (Sol r3-5). If the shorts fail, the plain labels get one try of their own
    before the floor. Purely mechanical — length and distinctness, never
    content.

    Returns the captions, or the numbered floor. Pure; never mutates
    ``options``; never raises.
    """
    shorts = [_button_short(o) for o in options]
    labels = [_button_label_text(o) for o in options]
    has_short = [s is not None for s in shorts]
    texts = [s if s is not None else lab for s, lab in zip(shorts, labels)]
    own = [not h for h in has_short]
    reason, captions = _fit_captions(texts, own, any(has_short))
    if reason is not None and any(has_short):
        alt_reason, alt = _fit_captions(labels, [True] * len(labels), False)
        if alt_reason is None:
            return alt
    return captions


def resolve_button_labels(options: list) -> list[str]:
    """THE pure whole-set button-label resolver (spec D2 item 1).

    ``options`` items are either a bare ``str`` (full label, no short) or
    ``{"label": str, "short": ...}``. Returns the final captions — the
    options' own words, or ``n · <short or label>`` when shorts are in play
    (see ``_classify_button_labels``) — or the whole-set floor
    ``["Option 1", "Option 2", …]``. Never mixed (some real, some floored),
    never mutated, never raises. Run BEFORE ``BROKER.register`` so the
    resolved captions can be placed in static broker meta (wiring: Task A5).
    """
    return _classify_button_labels(options)


def short_option_labels(
    labels: list, shorts: "list | None" = None,
) -> list[str]:
    """Derive button labels for a WHOLE option set (name/signature PINNED —
    resident ``ask_user`` calls this via
    ``post_dm_keyboard(short_labels=True)``, wired from parallel-owned
    ``tools.py``).

    Same whole-set rule as ``resolve_button_labels``: agent shorts VERBATIM,
    an option without one shows its own label, and the WHOLE set floors to the
    numbered placeholder only when the words do not fit. No elision anywhere
    (spec D2's ``short_option_labels()`` note) — this is a thin convenience
    wrapper that zips ``labels``/``shorts`` into ``resolve_button_labels``'s
    input shape for a single-select ask.
    """
    options = [
        {
            "label": str(lab),
            "short": (
                shorts[i] if shorts is not None and i < len(shorts) else None
            ),
        }
        for i, lab in enumerate(labels)
    ]
    return resolve_button_labels(options)


def _canonical_question(question: str, number: int) -> str:
    """v0.79.0 §4 — the DISPLAYED question prefix is ALWAYS the allocated
    durable number: ``Q<number>: `` is PREPENDED to the question. v0.85.0
    (round 4, D4): the old leading ``Q<digits>:`` strip is REMOVED — the
    agent's question text is preserved VERBATIM (an agent-authored
    ``"Q7: which flavor?"`` renders ``"Q<n>: Q7: which flavor?"``); doctrine,
    not a regex, tells the agent not to self-number."""
    return f"Q{number}: {question}"


def buttons_show_options(options: list, captions: "list | None") -> bool:
    """#1392: True when the buttons are the options' own words verbatim
    (``captions`` is what the keyboard shows; ``None`` means no keyboard is
    known). Then the numbered list under the question only repeats them."""
    return bool(options) and captions is not None and (
        [str(c) for c in captions] == [str(o) for o in options])


def render_ask_body(
    number: "int | None", question: str, options: list,
    captions: "list | None" = None,
) -> str:
    """v0.81.0 (W-R3, Sol r1-5) — the SINGLE canonical rendered ask body.

    Used IDENTICALLY by the DM ask posters (``ask_user``, ``wipe_memory``)
    for the posted body and the finish-hook settlement base, so the two can
    never disagree.

    Format::

        Q<n>: <question>

        1. <opt0>
        2. <opt1>
        …

    EVERY option is rendered VERBATIM (no truncation, no ellipsis), 1-based
    numbered. A free-text anchor (``options == []``) renders the numbered
    question ALONE — no option list. ``number`` may be ``None`` (no durable
    number allocated / degraded boot), in which case the bare question is used
    without the ``Q<n>:`` prefix.

    v0.84.0 (round 4, D1): the LENGTH caps this docstring used to lean on for a
    "no overflow path" claim are gone (``_validate_ask_args`` no longer bounds
    question/label length; only option COUNT stays capped at
    ``_ASK_MAX_OPTIONS``). The real Telegram 4096-char body limit is now
    enforced by a dedicated per-ask render-and-measure lifecycle validator
    (Task A3, spec §D1 bullet 2) rather than by an invented length heuristic
    here — this function never truncates.

    #1392: when ``captions`` (the button captions the keyboard shows) are the
    options' own words verbatim, the list is left out — the buttons already
    carry every option in full. The list stays whenever a button shows a
    ``short`` or the ``Option n`` floor, and when no captions are passed.
    """
    base = _canonical_question(question, number) if number else question
    if not options or buttons_show_options(options, captions):
        return base
    numbered = "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options))
    return f"{base}\n\n{numbered}"
