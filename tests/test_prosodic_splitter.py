"""Spec §5.1 + §11 — ProsodicSplitter behaviour.

The splitter is delta-fed: it receives token *suffixes* and emits whole
blocks (prosodic units) at sentence/paragraph boundaries, treating any
bracket span ([…], (…), {…}) as opaque; ``<`` is plain text (#1646).

Since #257 each emission also carries ``sep`` — the exact whitespace that
separated it from the previous block — so a consumer that concatenates
blocks (the HA companion integration streams them as text deltas)
reconstructs the source stream instead of welding sentences together.
"""

import itertools

from channels.voice.prosodic import Block, ProsodicSplitter


def texts(blocks: list[Block]) -> list[str]:
    return [b.text for b in blocks]


def feed_all(deltas: list[str]) -> list[Block]:
    """Feed every delta, then drain — the full emission sequence."""
    s = ProsodicSplitter()
    out: list[Block] = []
    for d in deltas:
        out.extend(s.feed(d))
    tail = s.flush_tail()
    if tail is not None:
        out.append(tail)
    return out


def rejoin(blocks: list[Block]) -> str:
    return "".join(b.sep + b.text for b in blocks)


class TestSentenceBoundaries:
    def test_dot_flushes(self):
        s = ProsodicSplitter()
        assert texts(s.feed("Done.")) == ["Done."]

    def test_bang_flushes(self):
        assert texts(ProsodicSplitter().feed("Great!")) == ["Great!"]

    def test_question_flushes(self):
        assert texts(ProsodicSplitter().feed("Yes?")) == ["Yes?"]

    def test_ellipsis_flushes(self):
        assert texts(ProsodicSplitter().feed("Hmm…")) == ["Hmm…"]

    def test_comma_does_not_flush(self):
        s = ProsodicSplitter()
        assert s.feed("Well, ") == []
        assert texts(s.feed("okay.")) == ["Well, okay."]

    def test_paragraph_break_flushes(self):
        s = ProsodicSplitter()
        assert texts(s.feed("First line\n\nsecond")) == ["First line"]

    def test_multiple_sentences_in_one_feed(self):
        s = ProsodicSplitter()
        assert texts(s.feed("Hi. Done!")) == ["Hi.", "Done!"]


class TestTagOpacity:
    def test_period_inside_square_bracket_does_not_flush(self):
        s = ProsodicSplitter()
        assert texts(s.feed("[confident.point] Done.")) == [
            "[confident.point] Done.",
        ]

    def test_period_inside_parens_does_not_flush(self):
        s = ProsodicSplitter()
        assert texts(s.feed("(soft.sigh) Goodnight.")) == [
            "(soft.sigh) Goodnight.",
        ]

    def test_period_inside_braces_does_not_flush(self):
        s = ProsodicSplitter()
        assert texts(s.feed("{emotion.warm} Hello.")) == [
            "{emotion.warm} Hello.",
        ]

    def test_angle_bracket_is_plain_text(self):
        """#1646: a bare "<" in speech is not an opener."""
        s = ProsodicSplitter()
        assert texts(s.feed("1 < 2. Yes.")) == ["1 < 2.", "Yes."]

    def test_tag_binds_forward(self):
        """[warm] Good morning. is one block, not two."""
        s = ProsodicSplitter()
        assert texts(s.feed("[warm] Good morning.")) == [
            "[warm] Good morning.",
        ]

    def test_flush_pushed_past_closing_bracket(self):
        """If a boundary lands inside a tag, it moves to after the close."""
        s = ProsodicSplitter()
        # The '.' in '[warm.rising]' must not trigger a flush.
        assert texts(s.feed("[warm.rising] Hi.")) == ["[warm.rising] Hi."]

    def test_unclosed_bracket_does_not_flush(self):
        """Partial tag at end of feed stays in the buffer."""
        s = ProsodicSplitter()
        assert s.feed("[warm") == []
        assert texts(s.feed("] Hi.")) == ["[warm] Hi."]


class TestSafetyCap:
    def test_char_cap_fallback_on_clause_mark(self):
        """At 200 chars we break on the rightmost clause mark."""
        s = ProsodicSplitter()
        long = "word, " * 50 + "end"
        out = texts(s.feed(long))
        # At least one block emitted at or before char 200, on a comma.
        assert out, "expected a safety-cap flush"
        assert out[0].endswith(",") or out[0].endswith(";")

    def test_char_cap_hard_cut_if_no_clause_mark(self):
        """No clause mark in 200 chars — hard cut."""
        s = ProsodicSplitter()
        blob = "a" * 250
        out = texts(s.feed(blob))
        assert out
        assert len(out[0]) == 200

    def test_time_cap_honours_boundary(self, monkeypatch):
        """1.5 s wall-clock since last flush forces a break."""
        clock = [0.0]
        monkeypatch.setattr("channels.voice.prosodic.time.monotonic", lambda: clock[0])

        s = ProsodicSplitter()
        assert s.feed("no punct here ") == []
        clock[0] = 2.0  # past the 1.5 s cap
        out = s.feed("more, text")
        # Expect a safety flush on the rightmost clause mark.
        assert out

    def test_time_cap_not_tripped_by_construction_delay(self, monkeypatch):
        """Creating a splitter then waiting for first token must not
        pre-arm the time cap. The 1.5 s window starts when the buffer
        first fills, not at construction.
        """
        clock = [0.0]
        monkeypatch.setattr(
            "channels.voice.prosodic.time.monotonic", lambda: clock[0],
        )
        s = ProsodicSplitter()
        clock[0] = 2.0  # SDK-first-token delay past the cap
        # First real delta arrives. Must NOT immediately emit a cap block.
        assert s.feed("Hello ") == []
        # Now advance another 2 s — cap should fire on the next delta
        # because the buffer has been filling for 2 s.
        clock[0] = 4.0
        out = s.feed("there, more")
        assert out  # the clause-mark-based safety flush fires now

    def test_char_cap_breaks_on_em_dash_when_no_comma(self):
        """Spec §5.1: safety-cap fallback clause marks are `,` `;` and em-dash."""
        s = ProsodicSplitter()
        # 190 chars of 'a', then em-dash+space, then more — char cap at 200.
        blob = ("a" * 190) + " — more continuation padding padding"
        out = texts(s.feed(blob))
        assert out
        # Should break at the em-dash region, not hard-cut
        assert out[0].rstrip().endswith("—") or out[0].rstrip().endswith("— ") \
            or out[0].rstrip().endswith("—")


def _timed(seq: list[tuple[float, str]], monkeypatch) -> list[str]:
    clock = [seq[0][0]]
    monkeypatch.setattr(
        "channels.voice.prosodic.time.monotonic", lambda: clock[0],
    )
    s = ProsodicSplitter()
    out: list[Block] = []
    for at, delta in seq:
        clock[0] = at
        out.extend(s.feed(delta))
    tail = s.flush_tail()
    return texts(out + ([tail] if tail else []))


def _by_word(text: str, step: float) -> list[tuple[float, str]]:
    words = text.split(" ")
    deltas = [words[0]] + [" " + w for w in words[1:]]
    return [(i * step, d) for i, d in enumerate(deltas)]


class TestBareLessThan:
    """#1646: a bare "<" in a spoken answer must not hold back every later
    sentence until the cap and then split a word."""

    REPLY = (
        "So < 19 is what you set. The heating is on. It will rain tomorrow "
        "afternoon. Take an umbrella. The bins go out tonight. Your parcel "
        "arrives on Friday. The front door is locked. The garage light is "
        "still on in the back. Anything else?"
    )

    def test_splits_like_the_same_text_without_it(self, monkeypatch):
        plain = self.REPLY.replace("So < 19", "So 19")
        for step in (0.0, 0.1):
            got = _timed(_by_word(self.REPLY, step), monkeypatch)
            want = _timed(_by_word(plain, step), monkeypatch)
            assert [t.replace("So < 19", "So 19") for t in got] == want
            assert len(got) == 9 and got[0] == "So < 19 is what you set."


class TestSquareAndRoundBracketsUnchanged:
    """#1646 changes only "<": these sequences keep their earlier blocks,
    including the known remainder that an unclosed "(" still waits for the
    cap."""

    def test_late_closing_tag_after_clause(self, monkeypatch):
        assert _timed([
            (0, "Okay, "), (0.1, "[wa"), (0.2, "rm,"), (2, " reass"),
            (2.1, "uring"), (2.2, "] Hello."),
        ], monkeypatch) == ["Okay,", "[warm, reassuring] Hello."]

    def test_tag_open_past_the_time_cap(self, monkeypatch):
        assert _timed([
            (0, "[warm"), (2, ".rising The status is ready."),
            (2.1, "] Follow up."),
        ], monkeypatch) == ["[warm.rising The status is ready.", "] Follow up."]

    def test_tag_after_prose_closes_late(self, monkeypatch):
        assert _timed([
            (0, "Right, the lights"), (2, " are off [so"),
            (4, "ft] and the door is shut."),
        ], monkeypatch) == [
            "Right,", "the lights are off [soft] and the door is shut.",
        ]

    def test_tag_mid_sentence_closes_late(self, monkeypatch):
        assert _timed([
            (0, "The lights are"), (0.5, " off [so"), (2, "ft.wa"),
            (2.2, "rm] now. Done."),
        ], monkeypatch) == ["The lights are off [soft.wa", "rm] now.", "Done."]

    def test_tag_at_start_closes_late(self, monkeypatch):
        assert _timed([
            (0, "[so"), (2, "ft] Good night."), (2.1, " Sleep well."),
        ], monkeypatch) == ["[soft] Good night.", "Sleep well."]

    def test_parenthetical_closes_late(self, monkeypatch):
        assert _timed([
            (0, "The boiler"), (0.5, " (the one in"), (2, " the attic"),
            (2.5, ", I mean) is fine."),
        ], monkeypatch) == [
            "The boiler (the one in the attic", ", I mean) is fine.",
        ]

    def test_unclosed_paren_still_waits_for_the_cap(self, monkeypatch):
        reply = TestBareLessThan.REPLY.replace("So < 19", "So (about 19")
        assert _timed(_by_word(reply, 0.1), monkeypatch) == [
            "So (about 19 is what you set. The heating is on. It will rain "
            "tomorrow afternoon.",
            "Take an umbrella.", "The bins go out tonight.",
            "Your parcel arrives on Friday.", "The front door is locked.",
            "The garage light is still on in the back.", "Anything else?",
        ]


class TestFinalFlush:
    def test_flush_tail_emits_remainder(self):
        s = ProsodicSplitter()
        s.feed("Unterminated thought")
        tail = s.flush_tail()
        assert tail is not None
        assert tail.text == "Unterminated thought"
        assert tail.sep == ""

    def test_flush_tail_empty_when_drained(self):
        s = ProsodicSplitter()
        s.feed("Done.")
        assert s.flush_tail() is None

    def test_flush_tail_none_for_trailing_whitespace_only(self):
        """Whitespace trailing the whole stream separates nothing — it is
        not worth a wire frame."""
        s = ProsodicSplitter()
        s.feed("Done. ")
        assert s.flush_tail() is None

    def test_flush_tail_carries_pending_separator(self):
        s = ProsodicSplitter()
        assert texts(s.feed("Done. ")) == ["Done."]
        tail = s.flush_tail()
        assert tail is None
        # …but with real text after the separator the tail keeps it:
        s2 = ProsodicSplitter()
        s2.feed("Done. ")
        s2.feed("and more")
        tail2 = s2.flush_tail()
        assert tail2 is not None
        assert (tail2.sep, tail2.text) == (" ", "and more")

    def test_flush_tail_whitespace_only_stream(self):
        s = ProsodicSplitter()
        s.feed("   ")
        assert s.flush_tail() is None

    def test_flush_tail_empty_stream(self):
        assert ProsodicSplitter().flush_tail() is None


class TestNonAscii:
    def test_emoji_passthrough(self):
        s = ProsodicSplitter()
        assert texts(s.feed("Hi 👋. Bye.")) == ["Hi 👋.", "Bye."]

    def test_non_ascii_passthrough(self):
        s = ProsodicSplitter()
        assert texts(s.feed("Ciao, amico. ¿Sí?")) == ["Ciao, amico.", "¿Sí?"]


class TestSeparatorPreservation:
    """#257 — the whitespace between blocks is data, not noise."""

    def test_space_between_sentences_rides_the_next_block(self):
        s = ProsodicSplitter()
        blocks = s.feed("Yep, I'm here. What's on your mind?")
        assert [(b.sep, b.text) for b in blocks] == [
            ("", "Yep, I'm here."),
            (" ", "What's on your mind?"),
        ]

    def test_separator_arriving_in_a_later_delta(self):
        """The live failure: the sentence closes on one delta and its
        trailing space only shows up on the next."""
        blocks = feed_all(["Yep, I'm here.", " What's", " on your mind?"])
        assert rejoin(blocks) == "Yep, I'm here. What's on your mind?"

    def test_separator_split_across_deltas_one_char_at_a_time(self):
        blocks = feed_all(["One.", " ", " ", "Two."])
        assert rejoin(blocks) == "One.  Two."
        assert texts(blocks) == ["One.", "Two."]

    def test_no_separator_when_source_had_none(self):
        blocks = feed_all(["One.Two."])
        assert [(b.sep, b.text) for b in blocks] == [
            ("", "One."), ("", "Two."),
        ]

    def test_tabs_and_newlines_preserved_verbatim(self):
        blocks = feed_all(["One.\tTwo.\nThree."])
        assert rejoin(blocks) == "One.\tTwo.\nThree."

    def test_paragraph_break_preserved(self):
        blocks = feed_all(["First line\n\nsecond line."])
        assert rejoin(blocks) == "First line\n\nsecond line."

    def test_paragraph_break_with_trailing_spaces_preserved(self):
        blocks = feed_all(["First line  \n\n  second line."])
        assert rejoin(blocks) == "First line  \n\n  second line."
        assert texts(blocks) == ["First line", "second line."]

    def test_leading_whitespace_of_the_stream_is_kept(self):
        blocks = feed_all(["  Hi there."])
        assert rejoin(blocks) == "  Hi there."

    def test_hard_cut_mid_word_gets_no_separator(self):
        """A 200-char hard cut can land inside a word — a synthetic space
        there would split the word in two."""
        blob = "a" * 250
        blocks = feed_all([blob])
        assert rejoin(blocks) == blob
        assert blocks[1].sep == ""

    def test_hard_cut_immediately_before_real_whitespace(self):
        blob = "a" * 200 + " tail."
        blocks = feed_all([blob])
        assert rejoin(blocks) == blob

    def test_clause_cap_keeps_whitespace_on_both_sides_of_em_dash(self):
        source = ("a" * 190) + " — no speaker to announce it."
        blocks = feed_all([source])
        assert rejoin(blocks) == source

    def test_time_cap_block_keeps_its_separator(self, monkeypatch):
        clock = [0.0]
        monkeypatch.setattr(
            "channels.voice.prosodic.time.monotonic", lambda: clock[0],
        )
        s = ProsodicSplitter()
        out: list[Block] = list(s.feed("no punct here, "))
        clock[0] = 2.0
        out.extend(s.feed("more text"))
        tail = s.flush_tail()
        if tail is not None:
            out.append(tail)
        assert rejoin(out) == "no punct here, more text"

    def test_bare_paragraph_break_emits_no_empty_block(self):
        blocks = feed_all(["One.", "\n\n", "Two."])
        assert texts(blocks) == ["One.", "Two."]
        assert rejoin(blocks) == "One.\n\nTwo."

    def test_consecutive_paragraph_breaks(self):
        blocks = feed_all(["One.\n\n\n\nTwo."])
        assert rejoin(blocks) == "One.\n\n\n\nTwo."
        assert texts(blocks) == ["One.", "Two."]

    def test_no_block_is_empty_or_padded(self):
        blocks = feed_all(["  One.  \n\n  Two, three… ", "\tFour!  "])
        for b in blocks:
            assert b.text
            assert b.text == b.text.strip()
            assert not b.sep.strip()


class TestLosslessProperty:
    """The invariant the fix rests on: for one uninterrupted splitter
    lifetime, concatenating ``sep + text`` over every emission reproduces
    the fed stream (minus whitespace trailing the stream as a whole).
    """

    SOURCES = [
        "Yep, I'm here. What's on your mind?",
        "I manage your home through Home Assistant. I can turn lights on "
        "or off, adjust temperature, play media, and handle your to-do "
        "lists. Anything else, I'll point you elsewhere.",
        "I can't reach the judge on this device — no speaker to announce "
        "the answer when it comes back. You'll need to ask from somewhere "
        "that can take a follow-up.",
        "[warm] Good morning. [confident] Everything is fine.",
        "First\n\nsecond\n\n\nthird.",
        "One.Two.Three.",
        "Ciao, amico. ¿Sí? Hi 👋. Bye…",
        "a" * 250 + " then, a clause mark; and an em — dash.",
        "  padded start and unterminated end",
        "Tabs\tand\nnewlines. Kept\t\tverbatim.",
    ]

    def _chunkings(self, source: str):
        """A handful of delta splittings, including one char at a time."""
        yield [source]
        yield list(source)
        for size in (3, 7, 32):
            yield [source[i:i + size] for i in range(0, len(source), size)]
        # Splits placed deliberately around every whitespace run.
        cuts = [i for i, c in enumerate(source) if c.isspace()]
        if cuts:
            parts, prev = [], 0
            for c in cuts:
                parts.append(source[prev:c])
                prev = c
            parts.append(source[prev:])
            yield [p for p in parts if p]

    def test_stream_is_reconstructible(self):
        for source in self.SOURCES:
            for deltas in self._chunkings(source):
                assert "".join(deltas) == source, "chunking bug in the test"
                blocks = feed_all(deltas)
                assert rejoin(blocks) == source.rstrip(), (
                    f"lossy for {source!r} chunked as {deltas[:6]}…"
                )

    def test_no_block_ever_empty_across_the_corpus(self):
        for source in self.SOURCES:
            for deltas in self._chunkings(source):
                for b in feed_all(deltas):
                    assert b.text.strip() == b.text and b.text

    def test_whitespace_only_streams_emit_nothing(self):
        for source in (" ", "   ", "\n\n", "\t\n \n\n "):
            for deltas in (list(source), [source]):
                assert feed_all(deltas) == []

    def test_reconstruction_survives_pathological_bracket_spans(self):
        source = "[a.b] One. (c.d) Two. {e.f} Three. <g.h> Four."
        for deltas in itertools.chain(
            [[source]], [list(source)],
        ):
            assert rejoin(feed_all(deltas)) == source


class TestPendingSepAccessor:
    """`pending_sep` is what a caller abandoning the splitter (the AR-B
    non-prefix reset) must rescue: the gap in front of the next block, which
    is not part of the buffered text the reset means to discard."""

    def test_empty_on_a_fresh_splitter(self):
        assert ProsodicSplitter().pending_sep == ""

    def test_reports_whitespace_left_in_the_buffer(self):
        s = ProsodicSplitter()
        assert texts(s.feed("Old. ")) == ["Old."]
        assert s.pending_sep == " "

    def test_reports_a_consumed_paragraph_break(self):
        s = ProsodicSplitter()
        assert texts(s.feed("Old.\n\n")) == ["Old."]
        assert s.pending_sep == "\n\n"

    def test_spans_consumed_separator_and_buffer_head(self):
        s = ProsodicSplitter()
        s.feed("Old. \n\nmore")
        assert s.pending_sep == " \n\n"

    def test_empty_once_the_next_block_starts(self):
        s = ProsodicSplitter()
        s.feed("Old. more text")
        assert s.pending_sep == " "  # the run before "more" is still the gap
        s2 = ProsodicSplitter()
        s2.feed("Old.more")
        assert s2.pending_sep == ""

    def test_does_not_consume_anything(self):
        s = ProsodicSplitter()
        s.feed("Old. ")
        assert s.pending_sep == s.pending_sep  # idempotent
        assert texts(s.feed("Next.")) == ["Next."]
