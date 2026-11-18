---
last_reviewed: 2026-11-18
---

# Telegram rendering

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How an answer becomes Telegram messages: the agent's Markdown sent as Telegram rich messages,
the few mechanical touches Casa makes to it, the size budget and how a long text is split,
what a refused message falls back to, how a received rich message is read back as text, and
the messages that still carry classic entities — captions, the approval card and a few
Casa-composed lines. How a message arrives and is dispatched, ordering in a chat, callbacks,
and the delivery outcome of an answer are [`architecture/telegram.md`](telegram.md); the
draft shown while a reply is written is [`telegram-reply-drafts.md`](telegram-reply-drafts.md).

## Mental model

**Telegram renders the agent's Markdown; Casa does not.** A rich message takes
GitHub-flavoured Markdown mixed with a fixed set of HTML tags, and Telegram renders it
natively: headings, lists and checklists, tables, folds (`<details><summary>`), quotes,
highlight, spoiler, footnotes, footer, URL and copy-text buttons. Every agent reply, `send`
text, job post and question, plugin message, page and card, and the edits of those, goes out
through `channels/tg_rich.py`: `send_rich` (`sendRichMessage`), `edit_rich` (`editMessageText`
with `rich_message`) and `send_rich_draft` (`sendRichMessageDraft`), raw Bot API calls made
through any bot, so the channel and a guest reply share them. The agent picks the shape of an
answer; its Telegram prompt carries one formatting guide (`tg_rich.FORMAT_GUIDE`) naming the
vocabulary and the written form of each construct, and the time-link rule below. Casa never
classifies content and never validates the agent's Markdown before sending it.

**Casa changes only what Telegram would misread or drop** (`tg_rich.prepare`):

- **Code is never touched.** Fenced blocks, indented code blocks and inline code spans pass
  byte for byte; nothing below applies inside them. Where code is, is what markdown-it-py
  finds reading the text as CommonMark with GitHub's tables: code inside a block quote, a
  list item, a table cell, a heading or a link label is code, so a task's start post, which
  quotes the request, keeps the request's code as written. A backtick with no closer is text.
  The text of an HTML block is not Markdown, with two exceptions Telegram makes: it reads a
  code span inside a `<summary>`, and it reads a `<details>` as a container whose body is
  Markdown, blank lines or not (so its tag lines are set aside before the text is parsed);
  code in either is code. Backticks anywhere else in an
  HTML block, such as an `<aside>` or an expandable quote, are text, and what they hold is
  encoded like any text. A table row is split on every unescaped pipe, inside backticks too, as GitHub
  reads it.
- **`_` becomes `&#95;`** outside code, link destinations and bare URLs. A bare underscore
  turns into italics or vanishes, and a backslash escape stays visible inside an HTML block,
  while the entity shows `_` in every context. `_italic_` is therefore never italics;
  `*italic*` is.
- **A `<` that does not open a supported tag becomes `&lt;`.** Telegram silently drops an
  unknown tag and, for some, its content. A `<` opens a tag only when its name is in the
  rich HTML tag list and the whole construct is tag syntax — every attribute quoted, or one
  of the boolean attributes rich HTML uses — so prose such as `<a lot of time>` stays text.
  A CommonMark autolink (`<https://…>`) is kept as written, and so is the text of a
  supported tag, its attributes included.
- **An escaping backslash goes with the character it escaped.** For both characters, a
  backslash run of odd length before it loses one backslash, so `living\_room` becomes
  `living&#95;room` and `\<5` becomes `&lt;5`.
- **A time link becomes a `<tg-time>` tag** (below).
- **A table whose rows have more cells than its header gets a wider header** and delimiter
  row, padded with empty cells to the widest row; GitHub-flavoured Markdown would drop the
  extra cells. The tables are the ones the same parser finds, inside a block quote or a list
  item too, whose prefix is kept; so a task's start post, which quotes the request, keeps
  every cell. A row with fewer cells is padded by Telegram itself.
- **A model-written button stays a button only when it is a URL or copy-text button**; any
  other is sent as its label. Callback data is Casa's own and only travels in the inline
  keyboard of its cards, so a `<tg-button type="callback_data">` in the text never reaches
  Telegram as a button. The `type` is read as Telegram reads it, its entities decoded, so
  `type="callback&#95;data"` is a callback button too; a button with no type, another type,
  or a second `type` of any other kind is sent as its label.
- **A lone surrogate is replaced** one code point for one before anything is measured.

Everything else, `$`, `==`, `||` and `[^x]` included, goes through as written.

**A time link is the one link target Casa reads, and it never fails the message.** An agent
writes every date or time in a message the person reads in the chat as
`[its own words](time:<ISO 8601>)`, the moment with a `T` between date and time and no spaces,
and dates plainly in files, emails, tool fields and records, a rule its Telegram prompt states;
Casa never looks for times in free text. A moment that parses (`tg_rich.time_instant`) becomes
`<tg-time unix="N">label</tg-time>` with no display format, the label HTML-escaped, so the
reader sees the agent's words and can tap them. A moment with no UTC offset, or a date alone
(its midnight), is read in Casa's timezone. A target that does not parse leaves the label as
plain text. A target with a space in it is not a Markdown link at all and stays literal.

**The budget is 32,000 bytes of prepared UTF-8, because Telegram truncates instead of
refusing.** Telegram's rich-message limit is about 32,768 characters and 500 blocks, but text
with accented or other multi-byte characters past about 35,000 UTF-8 bytes is accepted and
silently cut, while a reply of the same length in plain ASCII is refused. So the size is never left to a
refusal: `RICH_BUDGET_BYTES` (32,000) is measured on the prepared text, before sending.
`tg_rich.split` cuts a text over it into parts that each fit once prepared — at the last
paragraph break that fits, else the last line break, else after the last space, else
anywhere. No cut falls inside a link, an autolink, a tag, a time link or an inline code span,
so a link is never sent in two halves; only one of those bigger than the budget on its own is
cut through. A cut after a space or anywhere keeps every character. A cut inside a fenced
block closes the fence at the end of one part, inside the quote or list item it sits in,
and reopens it, with its language, at the start of the next, keeping every code line. Each part is one message; a reply's quote rides on the
first and a keyboard on the last. One message covers almost every answer, since 32,000 bytes
is several screens of text.

**A refused message is sent again as plain paragraph blocks.** A refusal is all or nothing:
one construct Telegram cannot take refuses the whole message. So when Telegram refuses a new
rich message for anything but its size, the same text goes once more as explicit `blocks`
(`tg_rich.plain_blocks`): one paragraph block per paragraph of the original, which Telegram
parses nothing in, so every character and every link address the agent wrote arrives as
written. Neighbouring paragraphs are joined to stay within 500 blocks, and the whole is
clipped to the budget with a closing `…` only when it still does not fit. A refusal for size
or block count (`tg_rich.SIZE_REFUSALS`) splits the part in two by paragraph count instead,
each half sent the same way, at most four levels deep; a part that cannot be cut further goes
as plain blocks. A cut inside a fence closes and reopens it as the split does. A refusal of
the plain blocks, and any failure that is not a refusal, propagates to the sender.

**A reply whose quote is refused goes without it.** When Telegram refuses a message whose
reply carries a quote, for anything but its size, the same message goes once more as a plain
reply with no quote, before any other fallback; a syntax refusal of that resend then takes
the plain blocks with the quote restored.

**An edit stays one message.** `edit_rich` cannot split: text past the budget (less the three
bytes of the ellipsis) is cut at the split's boundary and ends in `…`, and any refusal of the
text, size included, makes the same edit again as plain blocks. "Message is not modified" is
success. A refusal of the message itself — "message can't be edited" (a message sent with a
reply keyboard never is) or "message to edit not found" — propagates at once, since there is
nothing to resend into. A message first sent plain edits into a rich one. A guest reply's one
inline message has a last-resort plain-text edit of its own after the plain blocks
([`guest-summons.md`](guest-summons.md)).

**A received rich message is read back as text.** A rich message arrives with no `text`; the
library leaves its `rich_message` blocks in the message's `api_kwargs`. `tg_rich.flatten`
walks those blocks and their nested rich text by a fixed list of the schema's text-bearing
fields, whatever the block's type — list items one per line, table cells joined with ` | `, a
fold's summary and body, a mathematical expression as its LaTeX, captions and button labels —
and leaves URLs, times and other scalar fields out. A block type added later that reuses those
fields is read without a code change; a new text field is read only once it joins the list. `channels/telegram.py::quoted_text` reads a replied-to message as its
text, else its caption, else that flattening; the reply note, a guest summons' reply note and
a reply or file routed into a task all use it ([`telegram.md`](telegram.md), INV-DESK-004).

**Captions and a few Casa-composed lines keep classic entities.** A photo or file caption
cannot be rich, nor can a guest summons' placeholder, ticker and busy line. These go through
`channels/tg_richtext.py::render`, a fail-literal inline renderer returning one message's
display text and entities, or no entities when the text has no span, is past 4,096 UTF-16
units or 100 entities, or cannot be converted. It recognises fenced code, inline code,
asterisk bold and italic, ATX headings (as a bold line), labelled http(s) links and time links
(a `date_time` entity over the label); underscores are never emphasis, a line with a link of
any other scheme, an empty label or image syntax stays wholly literal, and a table stays
literal text. Its offsets are UTF-16 code units, as the platform counts them, through the
shared measurement in `text_util.py`.

**A message with no formatting shows what the rich path would show.** A caption or line with
no span has nothing to send as entities, so it goes out plain. What it shows is its display,
with the backslash escapes consumed, so `CUWVSRB8\-0007` reads `CUWVSRB8-0007`; a display that
would not fit one message is not used and the text goes out as authored. The display `render`
returns for such a text, which a caption is sent with, and `tg_richtext.plain_text`, which a
guest reply's plain lines use, are the same text. A text whose spans exist but cannot be sent
goes out as authored by `plain_text`, since its markers are the only record of its formatting
and of its link addresses. A caption whose entities Telegram refuses is sent again as its
display without entities (the authored markers could push it past the caption limit), so a
link in a caption can lose its address there.

**An approval card is never rendered.** The card Casa posts to ask for an approval carries
formatting Casa computed itself: `post_dm_keyboard` given `entities` sends the text exactly as
given with exactly those entities, and the card's status edits pass `literal=True` to
`edit_dm_message`, which sends the text as given with none. Neither reaches `prepare` or
`render`, so a field value such as `[x](https://evil.example)` reads as those characters and
never becomes a link (#1538). Lone surrogates are replaced one code point for one there too,
which keeps the entity offsets valid. When the platform refuses the entities, the same text is
sent once more without them. Which spans are bold, and that only Casa chooses them, is
[`approval-cards.md`](approval-cards.md) (INV-PLUG-059).

## Contracts & invariants

**INV-TG-025**: A text sent as new rich messages is split, before anything is sent, into parts of at most `RICH_BUDGET_BYTES` bytes of prepared UTF-8 each, cut at a paragraph break, else a line break, else after a space, else anywhere, never inside a link, tag or code span that fits the budget on its own, and losing nothing but the break each cut falls on.

Enforced in `tg_rich.split`, which measures each candidate part through `prepare`, so an
underscore counts as the five bytes it is sent as. A cut inside a fenced block adds the
closing and reopening fence lines and keeps every code line.

What it does not cover: an edit, which stays one message and is cut with `…` instead; and
Telegram's block and nesting limits, which are not measured before sending — a refusal for
them splits the part as the fallback above describes.

**INV-TG-026**: When Telegram refuses a new rich message for anything but its size, or a rich edit for anything but the message itself, the same text is sent once more as explicit plain paragraph blocks; a size refusal of a new message splits it in two by paragraphs instead, at most four levels deep, and a part that cannot be split further goes as plain blocks.

Enforced in `tg_rich._send_part` and `tg_rich.edit_rich`. The plain blocks parse nothing, so
the resend carries the text the agent wrote, link addresses included.

What it does not cover: a refusal of the plain blocks themselves, which propagates to the
sender; an edit refused because the message cannot be edited or is gone, which propagates at
once; a text so long its plain blocks pass the budget, which is clipped with `…`; and the empty
draft and the messages that keep classic entities above.

**INV-TG-010**: A single message sent without entities, whose text has no formatting span and whose display fits one message, shows that text's display — its markdown escapes consumed, as the rich path shows it — never the authored backslashes.

Enforced in `tg_richtext.render`, whose display for a span-less text is that text with its
escapes consumed, and in `tg_richtext.plain_text`, which a guest reply's plain lines use; a
photo card's caption is sent with the former.

What it does not cover: a text whose spans exist but cannot be sent (over the budgets, or
not convertible), which goes out as authored; rich messages — replies, `send` text, notices
and error lines alike — whose Markdown Telegram parses itself, so their escapes travel as
written; and the few fixed Casa lines still sent as plain text, which never parse markdown.

## Failure behavior

**Telegram refuses a message.** A new message is split on a size refusal and resent as plain
blocks on any other; an edit is resent as plain blocks on any refusal of its text; a refused
quote is dropped first. Each fallback logs a warning naming the error class only, since the
platform's error text can quote the message it refused.

**A part does not land.** `send_rich` stops at the first failure and propagates it; the
parts already sent stay sent, and the sender's delivery outcome keys on the first
([`telegram.md`](telegram.md)). Each message is handed to the sender's record the moment it
lands, a half of a part split after a size refusal included, so a failure later in the same
send leaves every landed message recorded.

**`render` cannot express a caption's entities.** It never raises: anything ambiguous stays
literal, and a conversion failure degrades to no entities.

## Extension points

**A new message from agent Markdown** goes through `send_rich` or `edit_rich`, never
`send_message` with entities, so it gets the touches, the budget and the fallbacks.

**A new character Casa must encode** joins `tg_rich._ENTITY` and follows the one rule: sent as
its entity outside code, an escaping backslash dropped with it.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/channels/tg_rich.py::prepare`
- `casa/rootfs/opt/casa/channels/tg_rich.py::split`
- `casa/rootfs/opt/casa/channels/tg_rich.py::plain_blocks`
- `casa/rootfs/opt/casa/channels/tg_rich.py::send_rich`
- `casa/rootfs/opt/casa/channels/tg_rich.py::edit_rich`
- `casa/rootfs/opt/casa/channels/tg_rich.py::flatten`
- `casa/rootfs/opt/casa/channels/tg_rich.py::time_instant`
- `casa/rootfs/opt/casa/channels/telegram.py::quoted_text`
- `casa/rootfs/opt/casa/channels/tg_richtext.py::render`
- `casa/rootfs/opt/casa/channels/tg_richtext.py::plain_text`

**Tests**
- `tests/test_tg_rich.py`
- `tests/test_telegram_rich_send.py`
- `tests/test_telegram_rich_readback.py`
- `tests/test_telegram_send_response.py`
- `tests/test_tg_richtext.py`
- `tests/test_tg_time_links.py`
- `tests/test_tg_richtext_v3.py`
- `tests/test_tg_richtext_remnants.py`
- `tests/test_telegram_link_fallback.py`
- `tests/test_telegram_link_fallback_shapes.py`
- `tests/test_telegram_plain_escapes.py`

**Related**
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/telegram-reply-drafts.md`](../architecture/telegram-reply-drafts.md)
- [`architecture/overview.md`](../architecture/overview.md)
<!-- END SOURCEMAP -->
