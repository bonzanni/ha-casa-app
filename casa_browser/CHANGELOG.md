# Changelog

## [0.1.5] - 2026-11-03

### Changed

- Nothing changes in the app itself. Maintenance: a proxy test of the sign-in rule (a request that only the sign-in allowed is cut when the sign-in ends) failed about one run in four, because its held request sometimes went through while the sign-in was still on, which the rule allows. The test now holds the request until the sign-in has ended.

## [0.1.4] - 2026-11-02

### Changed

- Fewer 1Password tabs and unlocks: the browser's 1Password extension is no longer signed in or unlocked at the start of every run. It happens only when a site's sign-in page offers a passkey, or just before you get a sign-in live view (so 1Password's own fill is ready there), and at most once per browser start. A sign-in with a password or an emailed code, or a site you are already signed in to, opens no 1Password tab at all, and a site profile that never needs a passkey no longer causes 1Password's "new sign-in" email. When a passkey is needed, the sign-in page loads once more after the unlock.

## [0.1.3] - 2026-11-02

### Security

- The browser's proxy runs on mitmproxy 12.3.1, and with it on fixed releases of the libraries it uses: Brotli 1.2.1 (a decoder flaw that a crafted compressed response could exploit), cryptography 50.0.2, h2 4.4.1, tornado 6.5.10 and pyasn1 0.6.5. msgpack is no longer installed (mitmproxy stopped using it). Three tornado advisories fixed only in tornado 6.6 remain open: mitmproxy does not yet accept 6.6, and only mitmproxy's web interface uses tornado, which the app never starts (the proxy does not load tornado at all).

## [0.1.2] - 2026-11-02

### Fixed

- A tap in the live view right after it opens or reconnects (after you switch apps and come back) now presses what you aimed at. For about half a second the picture showed Chrome's toolbar and then jumped up by its height, so a quick tap could press something a toolbar's height away from what you aimed at, such as another button. The picture now appears already in place.

## [0.1.1] - 2026-11-02

### Fixed

- Typing in the live view no longer loses or changes accented letters and emoji when the machine is busy: text with such a character goes through Chrome's own input, so `perché`, `wörd` or a password with an accent arrives exactly as typed. Plain letters, digits and the keys (Backspace, the arrows) are typed as before. The address bar still types the old way, so an accent there can still be lost on a busy machine.

## [0.1.0] - 2026-11-02

### Added

- First version of Casa Browser, an optional app beside Casa: a sandboxed browser that Casa's browsing specialist, Fido, drives on the sites you register and on the open web. Experimental.
- Casa checks Fido's steps before they run (plain reading on the open web needs no check). Reading goes ahead; a step that would change something (sending a form, ordering, booking) is put to you first in Telegram as one message: the question, a picture of the page with the button outlined whenever the check asks for one (always for a step that spends money), and normally Yes, Not now and Open live view. After your Yes the page is read and checked again, and if something you would weigh changed (a price, an item, a recipient, where the button leads) nothing is pressed. With no check, nothing runs.
- Public sites are open for reading: opening pages, following links and searching need no check. Their buttons and forms are checked like any step that could change something.
- Logins come from your 1Password vault, with one question: for a site with no linked login, Casa finds the items that fit, and Fido asks whether to use the one it found, or which one. Your answer links the item; the item itself is never changed.
- Signing in to a site you have not set up takes one tap: Fido sends a card with the page's address (and its picture when one could be taken), and your Yes saves the site and signs in, so next time it signs in there without asking; Forget sign-in on the Sites page undoes that. A puzzle in front of the sign-in form leads to a live view for you to solve it.
- Passkeys: the browser's 1Password extension signs in as the vault's guest and unlocks by itself when a run starts, and when a site asks for a passkey and 1Password offers that site's item, it is picked for you. A passkey prompt offering anything else is left alone: the item's login is used when the page shows a form, and otherwise you get a live view.
- A login you type in the live view is saved to the vault by itself once the sign-in worked, never a failed attempt; a sign-in that took only your email saves just the email; a sign-in through another provider (such as Google) is remembered for the site.
- Sign-in codes: an authenticator code stored in the 1Password item is filled in; a code the site emails you is found in your Gmail by your assistant and typed into the page that asked; a code sent by text message you type in the live view.
- Fido fills in date and time fields and pickers, types text with several lines into a page's multi-line text box, and reads what ordinary fields hold, never a password, code or card field. Pictures of a page hide those fields too.
- The live view, made for phones: one message in Telegram hands it over; Chrome's window fits the screen, your phone is its keyboard (a field fills as you type, hidden for a password; a strip offers Paste and ← → in the field), one finger scrolls and drags, − · Fit · + zoom the picture, Chrome's address bar takes an address, and the view reconnects by itself after you switch apps, on an iPhone too. Done is one tap.
- The app's page in Home Assistant: whatever waits for you in the browser first, then runs with their step logs, screenshots and files, notices, your sites (forget a site's sign-in, copy an address, add one by hand), setup and pairing.
- Setup warns when no user of the browsing vault is read-only; it cannot tell which user is the browser's 1Password guest. The guest needs only read access, and nothing has to be confirmed.
- The app's pages and live view in English, Italian, Dutch and German, following Home Assistant's language or the `language` option.
- After restoring a backup, the Sites page says which sites' provider sign-in needs a live login again.
- Chrome's developer tools are switched on in the browser (the app reads the 1Password extension's window through them), so the live view can open them too.
- An AppArmor profile of the app's own, validated on a running system.
- The `log_level` option applies to the app's own log.
