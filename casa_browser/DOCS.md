# Casa Browser

Casa Browser is an optional app beside Casa. Casa's browsing specialist, **Fido**, uses it to
work on websites for you: on the sites you register (signed in, with logins from a dedicated
1Password vault) and on the open web. You ask your assistant as usual (Ellen, unless you
renamed her); she hands the browsing to Fido, Fido drives the browser, and Casa checks each
step before it runs. It is **experimental**.

## Before you start

- A **dedicated 1Password vault** for the logins the browser may use (default name
  `Casa Browser`), a **service account** that can see only that vault and edit its items
  (Casa Browser saves the logins you type in the live view through it), and a **guest** of
  the same vault (the browser's own 1Password extension signs in as the guest). The
  extension only reads the vault's items, so give the guest read-only access. The Setup page
  warns when no user of the vault is read-only; it cannot tell which user is the guest.
- Casa, with the browsing specialist (Fido) installed.
- Optional: for sign-in codes that a site emails you, your assistant needs to be able to read
  your Gmail.

## Configuration

| Option | What it is |
|---|---|
| `onepassword_service_account_token` | Token of the service account that sees only the browsing vault. |
| `onepassword_vault` | Name of the browsing vault (default `Casa Browser`). |
| `guest_sign_in_address`, `guest_email`, `guest_password`, `guest_secret_key` | The 1Password guest the browser's extension signs in as (read-only is enough); it is what lets a site sign in with a passkey. The sign-in address is your account's own (`yourteam.1password.eu`), not the generic `my.1password.eu`. |
| `language` | Optional. Overrides the browser language, which otherwise follows Home Assistant's. |
| `timezone` | Optional. Overrides the browser time zone, which otherwise follows Home Assistant's. |
| `retention_days` | How long step logs, screenshots, downloaded files and run records are kept after a run ends (default 30). |
| `log_level` | `debug`, `info` (default), `warning` or `error`. |

The browser's language follows Home Assistant's configured language, read when the app
starts; a change takes effect at the next app restart. If the app cannot read it and no
`language` is set, it holds new runs until it can. The app's own pages use the same language
when they have it — English, Italian, Dutch or German — and English otherwise.

## Pairing with Casa

Casa and the app share one secret, so the app answers only Casa. On the app's Setup page,
press **Pair with Casa**, then ask Fido to pair within two minutes. The page then shows which
caller paired. Pairing again replaces the secret and stops everything that was running under
the old one.

## What is checked, and what you are asked

Fido never decides alone whether a step may change something. Before a step runs, Casa
checks it against your request and the page:

- **Reading goes ahead.** On the open web, opening pages, following links, searching and
  scrolling are not checked. On a signed-in site, an address Fido opens itself on the same host as
  the page it is on is not checked either, so one that acts as soon as it is opened (an unsubscribe
  address, say) does so without a question. A subdomain goes through the check, and pressing a link
  to a signed-in site is still checked, by the name the link shows.
  Any other step the check sees as only reading (opening a statement, downloading an invoice) goes
  ahead without a question.
- **Filling in goes ahead.** Everywhere, typing without pressing Enter, ticking a box, choosing
  from a list and opening a menu run without a check, since they send nothing yet. The check reads
  the whole form, every tick and value, when Fido presses what sends it. A page whose own script
  saves as soon as you tick or type (a setting that saves itself) does so without a question;
  Fido touches such a box or field only when the task asks for that change.
- **A change is put to you first.** When the check sees that a step would change something —
  sending a form, ordering, booking, paying, deleting, changing a setting — you are asked in
  Telegram before it runs. On a public site, its buttons, sending its forms and downloading
  anything but a document are checked the same way, so a booking or a sign-up there is asked too.
- **One Yes for one job.** When a task changes several things on one site, Fido first shows you
  the whole plan, with its list, and asks once. After your Yes, each step that plainly carries
  out the plan runs without another question, on that site, in that task. Every step is still
  checked. Anything else is asked as usual, and so is anything with money, one step per
  question, and any step on a page with a card field. Fido finishes by reading back what the site
  holds and telling you every difference: no code compares a long list line by line.
- **No check, no step.** If the check cannot be made, the step does not run, and Fido tells
  you the task cannot go on right now.

The check and Fido can both see a picture of the page. Password, code and card fields and frames
are blacked out; a key printed on a page is readable, as it is in any screenshot. Pictures for them
are never stored by the app.

**A question** is one message from Fido: the question (normally what the step would do, and what it
would send) and its buttons, with a picture of the page, the button or field outlined,
whenever the check asks for one (always for a step that spends money). The buttons:

- **Yes, …** (what the step does): the page is read once more and checked again before
  anything is pressed. If something you would weigh changed meanwhile (a price, an item, a
  recipient, where the button leads), nothing is pressed (usually you are asked again). A
  change that does not matter to the step, such as a clock, need not stop it, but a page that
  keeps changing while it is checked can still need your answer again; when the step went through, the card ends with "Done: …".
- the check's other options, when it offers any;
- **Not now**: nothing is pressed; Fido looks for another way or tells you;
- **Open live view**: opens the app's page, where the question's live view lets you look at
  the page or do the step yourself. Opening it is not an answer.

A question stays open for an hour at most. Some questions come without **Yes**: when the
picture of a step that spends money could not be taken, when a question's picture did not
reach you, when the check could not put the question in full, or when Casa Browser could not read
the whole page and so could not check it again before pressing. Their other buttons stay,
and you can do the step yourself in the live view.

**Every picture** hides password, one-time-code and payment-card fields and shows frames
embedded in the page as black; ordinary fields (a name, a date, a choice) show what they
hold. Fido sees the same values when it reads a page, never those of a password, code or
card field, so it can check a date or a choice it filled in. It fills date and time fields
and pickers, and types text with several lines only into a page's own multi-line text box.

**Signing in.**

- A site with a 1Password item signs in by itself, an authenticator code stored in the item
  included; when that does not work (a rejected login, a challenge), you get a live view.
  That holds on any page of the site that shows its sign-in form, not only its start page:
  Casa first checks that the page really is the site's sign-in (a "confirm your password"
  step, a password reset, a newsletter box or a passkey that approves a payment is not) and
  which of its fields take your username and password, so a site that does not mark its
  username field still signs in and a search box beside the form is never filled. It signs
  in only on the page it checked, sends your password at most once there, and tells Fido
  it sent the form; Fido reads the page to see whether it worked. A sign-in that did not work is
  not retried by itself in the same task.
- A site you registered without an item: Casa checks which items in the browsing vault fit
  it, and Fido asks you once — "To sign in to *address* I'd use your 1Password item
  *title*. OK?" (**Yes** / **No**), or, when several fit, which one (their titles and
  **None of these**). Your choice links the item to the site; the item itself is never
  changed. No, or no item that fits, means a live view to sign in yourself.
- A site you have not set up: Fido sends one card, "Sign in to *address* (*name*)?" (or "…
  with your 1Password item "*title*"?" when exactly one item fits), with a picture of the page
  when one could be taken, and the line "Yes, and next time I'll sign in here without asking.
  You can undo that in Casa Browser's Sites page." **Yes** saves that address as one of your
  sites and signs in; **Forget sign-in** on the Sites page undoes it (the next sign-in asks
  about the 1Password item again). **No** saves nothing. A puzzle in front of the
  sign-in form ("verify you are human") still gets the card. Fido never solves a puzzle:
  after your Yes, a page that does not show you signed in brings you a live view.
- A site that signs in with a **passkey**: keep the passkey in the site's 1Password item in
  the browsing vault. When a sign-in page first offers a passkey, or before a sign-in live
  view, Casa Browser signs the browser's 1Password extension in as the guest, or unlocks it,
  by itself (once per browser start; the sign-in page then loads once more). When the site's
  page asks for a passkey and 1Password offers exactly the site's item, it is picked for you,
  once per run; the item needs a username and a title no other login in the vault has. A passkey prompt offering anything
  else is left alone: if the page also shows a login form, the item's login is filled in as
  usual; if not, you get a live view, where you can pick the passkey yourself.
- A code the site emails you: Fido asks whoever handed it the task (usually your assistant)
  to find the code in your Gmail — only an email from that site, from the last few minutes —
  and pass it on; Casa Browser types it into the page that asked. Without a code within ten
  minutes, you get a live view. A code sent by text message you type in the live view. A
  site that asks you to approve the sign-in in its own app waits until you have.

**Long tasks.** About every 20 minutes Fido asks "Still on *task* (*n* min). Fido keeps
going; tap Stop to end it." with **Keep going** and **Stop**; no answer means it keeps going.

## The app's page

The app's panel in Home Assistant is where you look after it:

- **Home** lists, first, whatever waits for you in the browser — a live view to open or to
  reconnect to, or a question whose page you can look at first — each with **Open**; then the
  runs (each with its step log, screenshots and files) and the latest notices. It shows a new
  live view by itself.
- **Sites** lists your sites, how each signs in, the sign-in provider it learned (such as
  Google), and **Forget sign-in** (the 1Password item stays). Tap an address to copy it. You
  do not have to add a site here: the sign-in card above does it. You can still add one by
  hand (its addresses, and either a 1Password item or nothing: then your first login in the
  live view saves it).
- **Setup** shows whether the app is ready, what the vault's user list says about the
  guest (read-only when at least one user of the vault is read-only; a warning when none is;
  nothing to confirm), and pairs with Casa.

## The live view

When Fido needs you in the browser — to sign in, to type a code sent by text message, to solve
a puzzle, or for a step it cannot do — you get one message in Telegram headed "📊 Fido": the
reason, and **Open live view**, which opens the app's page with the waiting live view first.
Press **Open**. You see Chrome's window fitted to your phone, and your phone is
its keyboard. Tap a field in the picture: the keyboard comes up and what you type fills the field
as you type (hidden for a password). Each key acts as on a keyboard plugged into that
computer, and the phone's own suggestions and corrections are off. Return closes the keyboard — tap the page's own button
(Login, Submit) to send the form. If your tap did not reach the field (a pop-up was in the way),
nothing is typed and a line says so: tap the field again. While you type, a strip above the keyboard has **Paste** (pastes what
you copied; the phone may ask you to allow it the first time) and **← →** to move the cursor
in the field. Tap the site name to show Chrome's own toolbar;
tap its address bar to type an address there — return opens it (typed without `https://`, a
registered site's address still opens over https). Use one finger: swipe to scroll the page,
hold then drag to drag. The **+** button in the top bar
zooms into the picture (then a swipe moves the picture, and scrolls the page at its edge);
**Fit** shows it whole again. If you
switch to another app (to approve a sign-in, say), the view reconnects by itself when you come
back. Date and time fields take the phone's keys too. Copy and paste with
the phone's clipboard need Home Assistant over https.

Press **Done** when you are finished: that one tap ends the view and Fido carries on (you can
go back to Telegram). A login you completed in the view is saved to the browsing vault by
itself once Casa's check sees that the sign-in worked — never a failed attempt. A sign-in that
took only your email (the site emails you a code) saves just the email, and next time Fido
fills it and asks for the code as above. If the site already has a 1Password item that works
and you used another password, the item is left as it is and Fido tells you once; if the
vault could not be written, a notice says the login may not be in the vault. A sign-in through
another provider (such as Google) is remembered for the site too. The 1Password extension's
own "Save login?" prompt in the picture can be ignored.

Chrome's developer tools are switched on in this browser (Casa Browser reads the 1Password
extension's own window through them), so they can also be opened from the live view.

## What the app keeps

- Saved logins (browser profiles) stay on this machine and are **excluded from Home
  Assistant backups**; after a restore, each site needs a login again, and a site that signs
  in through another provider (such as Google) needs it in the live view — the Sites page
  says "provider sign-in needs a live login". They are not encrypted at rest.
- Registered sites, run records and downloaded files are kept in the app's data and are part
  of normal backups. Run records, step logs, screenshots and files are deleted
  `retention_days` after a run ends.
