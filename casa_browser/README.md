# Casa Browser

An optional companion app for [Casa](../casa): a sandboxed browser that Casa's browsing
specialist, Fido, drives on the websites you register — signing in with logins and
passkeys from a dedicated 1Password vault, downloading invoices and statements, filling
in forms — and on the open web.

- The browser can reach only public internet addresses, never your home network, Home
  Assistant or Casa.
- Casa checks Fido's steps before they run (plain reading on the open web needs no check):
  reading goes ahead, and a step that would change something (sending a form, ordering,
  booking) is put to you in Telegram first.
- No model ever sees a password or a cookie. An authenticator code is filled in from the
  vault; a code a site emails you is found in your Gmail by your assistant and passed on.
- Pairing with Casa, your sites, run logs and the live view of the browser are on the app's
  own page in Home Assistant.

See the Documentation tab for setup.
