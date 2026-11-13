---
last_reviewed: 2026-11-13
---

# Telegram rendering

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How an answer becomes Telegram messages: the markdown grammar the rich renderer recognises
and how a table is judged, pagination to the platform's message-length and entity budgets,
what a page whose formatting is refused or cannot be expressed still carries, the unit
both rendering paths measure in, and the approval card, which bypasses rendering. How a message arrives and is dispatched, ordering in a
chat, callbacks, and the delivery outcome of an answer are
[`architecture/telegram.md`](telegram.md).

## Mental model

**Rendering recognizes a small, deliberate grammar, and fails literal.** Agent output is
markdown-ish; the rich renderer recognizes fenced code, inline code, asterisk bold/italic,
ATX headings, labelled `[text](url)` links (http/https only — any other scheme leaves its
whole line untouched), time links, and confident markdown tables — nothing else. Block structure
(fences, table runs) is segmented by casa's own line-based scanner, so an unclosed fence
keeps its remainder byte-for-byte; inline semantics run through a CommonMark engine one
line at a time, with the deliberate exception that underscores are never emphasis (tool
and identifier names carry them). A confident table is re-emitted from its parsed cells
in one of three forms — a padded monospace box when narrow and link-free, per-record
`Header: value` stanzas when wide or link-bearing under a real header, or plain rows with
their formatting intact otherwise — chosen so cell content and link destinations are never
silently dropped. Anything ambiguous stays literal rather than rendering wrongly.

**A time link is the one other link target, and it never fails the line.** An agent writes
every date or time in a message the person reads in the chat as
`[its own words](time:<ISO 8601>)`, and dates plainly in files, emails, tool fields and records,
a rule its Telegram prompt states; Casa never looks for times in free text. A moment that parses becomes a `date_time` entity
over the label, sent without a display format, so the reader sees the agent's words and can
tap them. A moment with no UTC offset, or a date alone (its midnight), is read in Casa's
timezone. A target that does not parse, a phrase with spaces included, leaves the label as
plain text and the rest of the line formatted as usual. Like code and a link, the entity is an
atom: bold splits around it, a table that holds one is never the monospace box (which carries
no entity), and a page cut through it gives an entity on each page. The plain fallbacks show
the label alone, since its words already say the time.

**What counts as ambiguous is judged against the delimiter row.** A `|---|` row declares how
many columns the table has; a bare run of pipe-bordered lines only implies one. So under a
delimiter a data row with fewer cells than the header is padded with empty cells and the run
renders as the table it plainly is, which is what GFM specifies — while a row with *more*
cells than the header still leaves the whole run literal, because GFM resolves that by
truncating and truncation drops content, which this renderer does not do for any reason.
Without a delimiter nothing is padded and every row must match exactly: a guess about the
column count is not a declaration, and a run that only looks like a table is better read as
the text it was. A link in the header is no longer disqualifying either — the stanza's field
label carries it as a real link, with the label's bold split around it so the two never
overlap. What does disqualify the stanza is a column that is blank in *every* record: the
stanza prints a field line only where there is a value, so such a column would contribute
no line at all and its header cell — and any address that cell carried — would appear
nowhere. A table like that stays in the plain-rows form, where every header cell survives.

**A message with no formatting shows what the rich path would show.** Some text carries no
span at all, such as a plugin card whose fields are escaped for markdown but contain no bold
word. Such a text has nothing to send as entities, so it goes out plain. What it shows is its
display, with the backslash escapes consumed, so `CUWVSRB8\-0007` reads `CUWVSRB8-0007`
whether or not the same message happens to carry a bold word. Besides the escapes, a
span-less parse re-emits a table from its cells (its row spacing and its delimiter row) and
replaces a NUL character, exactly as the rich path shows them, and a text with no span has no
link address to lose. A table's padding can make that display longer than the text, so a
display that would not fit one message is not used and the text goes out as authored. One helper decides this for every sender
of a single message. A text whose spans exist but cannot be sent is different: it is over the
length or entity budget, or it cannot be converted. Its markers are the only record of its
formatting and of its link addresses, so it goes out as authored. The once-only retry after
the platform refuses a message's entities also sends the authored text, as described next —
except a photo card's caption, whose retry sends its rendered display without entities (the
authored markers could push it past the caption limit), so a link in a caption can lose its
address there.

**A page whose formatting is refused, or cannot be expressed, still carries its link
destinations.** Formatting fails in two ways. The platform can REFUSE a message's
entities; the sender then re-sends that message as plain text, exactly once. A single-page
reply re-sends the text the agent authored, which still contains the address of every
`[label](url)` link. A paginated reply has only its rendered pages, which are
deliberately marker-free, so its plain form is reconstructed from the page and its entities
instead: a link destination is the one datum a display cannot carry, and it is re-attached
after its label. When that reconstruction would not fit one message the page's own text is
sent unchanged and the destinations follow as a further message, one per line — the plain
splitter cuts at newlines, so an inline reconstruction could otherwise be cut through an
address. A destination that would not fit a whole message on its own cannot be delivered as
text in any shape, so it is omitted with a log line rather than sent as fragments of an
address. One rejection is still retried once and only once; what may take more than one
message is the retried payload, never a second attempt at the rejected one.

The second way is that a page's entities cannot be EXPRESSED at all. Telegram's offsets are
UTF-16, and a lone surrogate — which the shared measurement tolerates by design, and the
client library does not — makes the conversion raise. That degrades to no entities, which is
what a page with no formatting looks like, so nothing downstream can tell the two apart and
no rejection is raised for the paragraph above to catch. The paginating renderer decides it
itself, while it still holds the spans: the page is emitted with each destination re-attached
in the same `label (url)` form, inline while it fits the page's budget and otherwise as the
page's own text followed by destination-only pages. The spans are page-relative Python
offsets, so this needs no UTF-16 round trip — the round trip is what the surrogate breaks.
Those pages also have their lone surrogates replaced one code point for one, which keeps the
inline-fit measurement exact. That replacement is not what makes a reply deliverable, though:
a SINGLE-page reply is outside the arm — emitted untouched, entities and bytes both, its
senders falling back to the text the agent authored, address and all — and a page with no
spans never enters it. Whatever a page still carries, the surrogate is replaced where the
text leaves for the Bot API, at the channel's request boundary, which
[`architecture/telegram.md`](telegram.md) owns (INV-TG-009).

**An approval card is never rendered.** The card Casa posts to ask for an approval carries
formatting Casa computed itself: `post_dm_keyboard` given `entities` sends the text exactly as
given with exactly those entities, and the card's status edits pass `literal=True` to
`edit_dm_message`, which sends the text as given with none. Neither reaches the grammar above,
so a field value such as `[x](https://evil.example)` reads as those characters and never
becomes a link (#1538). Lone surrogates are replaced one code point for one there too, which
keeps the entity offsets valid. When the platform refuses the entities, the same text is sent
once more without them. Which spans are bold, and that only Casa chooses them, is
[`approval-cards.md`](approval-cards.md) (INV-PLUG-059).

**Both rendering paths measure length in the unit Telegram counts.** The platform's limits
are counted in UTF-16 code units — an astral character (most emoji) counts as two. The rich
paginator, the plain splitter, a reply's draft, and the authorization-challenge size gate
all resolve to one shared measurement helper, so the paths cannot drift apart again. The
plain splitter prefers newline boundaries, consumes exactly the one newline it splits at
(blank-line separation survives into the next chunk), and drops a whitespace-only chunk
rather than sending a message the platform would reject.

## Contracts & invariants

**INV-TG-005**: A rich response is paginated to Telegram's message-length and entity budgets.

Enforced in the rich renderer's pagination, which measures in UTF-16 units as the platform
does.

What it does not cover: the plain splitting path. That path now measures the
same UTF-16 units through the shared helper, but it splits plain text only — it carries no
entities and makes no entity-budget promise.

**INV-TG-008**: In a paginated rich response, a page whose spans cannot be converted to Telegram entities still carries every individually deliverable destination of its link spans — inline after the label, or on destination-only page(s) that follow it.

Enforced in the paginating renderer, which reconstructs from the page's own spans rather than
from entities it no longer has. "Individually deliverable" is the one exclusion: a destination
longer than a whole message cannot be a message on its own in any shape, and is dropped with a
log line rather than cut into fragments of an address.

What it does not cover: a single-page reply, whose senders fall back to the text the agent
authored, address and all; a page whose entities Telegram merely rejects, which is the
sender-side path above; and a page with no spans at all, which does not enter this path.
None of those exclusions is a lost reply: a lone surrogate on any page, this arm's or not, is
replaced at the channel's request boundary before the text is encoded (INV-TG-009 in
[`architecture/telegram.md`](telegram.md)).

**INV-TG-010**: A single message sent without entities, whose text has no formatting span and whose display fits one message, shows that text's display — its markdown escapes consumed, as the rich path shows it — never the authored backslashes.

Enforced in the rich renderer's plain-text helper, which every single-message sender's
no-entities branch uses.

What it does not cover: a text whose spans exist but cannot be sent (over the budgets, or
not convertible), which goes out as authored; the retry after the platform refuses a
message's entities, which re-sends the authored text (a photo card's caption re-sends its
rendered display instead); and the plain transports used for
tool output, notices and error text, which never parse markdown.

## Failure behavior

**Formatting is refused, or cannot be expressed.** Both cases are described under the
mental model: a message whose entities the platform refuses is re-sent plain exactly once,
and a page whose spans cannot be converted is emitted with its link destinations
re-attached. The paginating renderer itself never raises — a page whose destinations could
not be reconstructed is emitted as plain text with a log line.

## Extension points

**A new rich response** belongs on the paginating path. The plain splitter measures the same
units but sends unformatted text only.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/channels/tg_richtext.py::render_paged`
- `casa/rootfs/opt/casa/channels/tg_richtext.py::plain_text`
- `casa/rootfs/opt/casa/channels/tg_richtext.py::time_instant`
- `casa/rootfs/opt/casa/channels/telegram.py::_split_message`

**Tests**
- `tests/test_tg_richtext.py`
- `tests/test_tg_time_links.py`
- `tests/test_tg_richtext_v3.py`
- `tests/test_tg_richtext_remnants.py`
- `tests/test_telegram_split.py`
- `tests/test_telegram_link_fallback.py`
- `tests/test_telegram_link_fallback_shapes.py`
- `tests/test_telegram_plain_escapes.py`

**Related**
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/overview.md`](../architecture/overview.md)
<!-- END SOURCEMAP -->
