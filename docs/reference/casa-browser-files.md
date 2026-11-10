---
last_reviewed: 2026-11-07
---

# Casa Browser — downloads, files and delivery records

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a file enters a browser run of the optional `casa_browser/` app and how its delivery is
recorded: `casa_browser/rootfs/opt/casa_browser/downloads.py`, the file and delivery tables of
`store.py`, and the browser's downloads directory made by root bootstrap. The request policy
the downloads obey is [`casa-browser.md`](casa-browser.md); the proxy that enforces it is
[`casa-browser-runtime.md`](casa-browser-runtime.md); handles and the page-view gate are
[`casa-browser-observations.md`](casa-browser-observations.md). The run controller calls these methods
from the `/v1` download, archive, file and delivery routes
([`casa-browser-runs.md`](casa-browser-runs.md)).

## Mental model

**The controller fetches, the policy decides every hop.** For a download a model never names an
address: it names a link handle from the current observation (a plain read, below, is the one
place it names one, and no cookie goes with it). The controller resolves the handle (same run,
same page generation, and the page still belonging to the session) and fetches the link's
address itself — through the enforcing proxy's own download listener, with the proxy's CA,
never through Patchright's request client. Each hop is a download hop: the floor, the
infrastructure rules and Chrome's background refusals decide it, never the account session's
document rule, so an off-site CDN hop is fetched. Cookies, not the policy, keep the account to
its site (below).

**A file is complete or absent.** Bytes stream into a private partial file under its generated
id, bounded, hashed, and are renamed into place only when complete; then the file is recorded.
A cancelled, refused or failed transfer leaves neither a file nor a row.

## Link downloads

`Downloader.fetch(run, handle, session, policy)`:

- the handle must resolve for the run at the session's current page generation, on a page that
  belongs to the session (`observe.page_view_allowed`), else `stale_handle`; a target that is
  not a link is `unsupported`;
- an `http(s)` address is fetched hop by hop, at most 10 redirects, through
  `downloads.PROXY_URL` (the proxy's download listener, `netfloor.PROXY_DOWNLOAD_PORT`). Before a
  hop is sent the run's policy is asked about it (`policy.decide(…, hop=True)`, as a `GET`
  document) — about the address exactly as it goes on the wire, built from the authority and
  request target the HTTP client writes (no fragment, an empty query dropped, an empty path sent
  as `/`), and that value is what is sent: a refusal (the floor) is `blocked` and the hop is
  never followed. The proxy then decides the same hop again on that listener, on the policy it
  holds, as a hop; a proxy refusal, an upstream answer other
  than a redirect or `200` (a `206` part is not a file), a compressed body, or a transport
  failure is `blocked`. The downloader asks for the identity encoding only, so a body is complete
  when its declared length or its chunked framing says so (a shorter one is a transport failure);
  a body with neither ends where the server ends it, as in a browser. Each request is sent
  once: the HTTP client's own resend of an idempotent request whose connection broke is turned
  off, so a lost response is a refusal, never a second send, and a judged download's caller is
  told immediately before its first request can leave (``on_send``, the action gate's journal);
- a `blob:` or `data:` address is read inside the frame that holds it, by fixed isolated-world
  code, only while that frame's page belongs to the session, and refused past the size bound
  before it is read; every other scheme is `unsupported`.

**Cookies.** A hop carries the browser context's cookies for its address only when the hop's
origin is the origin of the page the link was on or a registered origin of the active account
site; any other hop — a capability host a redirect leads to, or a bearer link to it — carries
none. No hop carries an `Authorization` header. The user agent is the browser's own.

**INV-BRW-045**: A download hop the run's policy refuses as a download hop is never sent; a hop it admits is decided again by the enforcing proxy on its download listener, which alone admits it to the upstream, and an account cookie is attached only to a hop whose origin is the link's page's or the active account site's.

Where the two read an address differently (a non-ASCII path the HTTP client sends as raw
bytes, say), the proxy refuses the hop: it reaches the proxy, never the upstream, and the
download is `blocked` (accepted residual, operator ruling E-C3-3, 2026-10-22).

**A public download is checked after the fetch** (design §3). In the public session a link
download is free: it is fetched at once (`fetch(…, quarantine=True)`). A file whose detected type
is in the document set (`downloads.DOCUMENT_TYPES`: PDF, PNG, JPEG, GIF, HTML, plain text, CSV,
JSON, XML, calendar, the zip-based office and e-book types) is kept as above. Any other type —
an executable, an installer, a zip, an unknown binary — stays a private partial file
(`downloads.Quarantined`, never recorded) and becomes the run's pending judgement of kind
`download`, whose packet carries `downloaded_file` (kept name, detected type, size). A `go`
keeps it (`Downloader.keep`: renamed into place and recorded); an `ask` holds it while the
question stands and keeps it only when the confirmed operation executes; any other end — `look
first`, no verdict, a superseding command, a declined, lapsed without a fresh judgement or
expired question, the run's end — deletes it (`controller._sweep_quarantine`), and an app start
removes one a crash left (`cleanup_partials`).

**INV-BRW-046**: A public link download whose detected type is outside the document set is recorded only when a go verdict or an executed confirmation for its own pending judgement keeps it, and at any other end of that judgement its bytes are deleted.

## Plain reading

`Downloader.fetch_plain(run, url, policy, agent)` reads a public address with no browser: the
hops of a link download (`_fetch_http`), the run's policy asked about each, the proxy's download
listener, no `Authorization`, with an address the model holds. The controller checks the address
with `start_url_ok` first (`not_run` `bad_address`) and hands in the User-Agent it kept.

**Never a cookie.** It takes no session, and passes `_fetch_http` an empty cookie scope, because
`_cookie_scope` would add the account site's origins during an account session even with no page
origin. Reading the signed-in site's own address sends nothing of the sign-in.

**INV-BRW-064**: A plain fetch carries no cookie, whatever the session or destination, sends no hop the policy or the floor refuses, and neither reads nor changes Chrome, the session or the installed policy; only its own answer keeps addresses whole, and a file downloaded or captured from a page stays fully masked.

**What the answer becomes.** The one request's answer decides, so nothing asks twice
(`_fetch_http`'s `last_hop` holds the status and the address of the hop that ended it). A 200 with a
whole plain body is a stored file, like any other. Anything else keeps nothing and is `not_run`:

| Reason | Answer |
|---|---|
| `http_<status>` | the site's own status when the hop that ended the fetch was not a whole plain 200: 403, 404, 503, a 206. A 200 whose body came compressed against `identity`, or broke off, is `http_200`. The proxy's floor refusal of a name that resolves privately is a 403 too |
| `blocked` | no answer: the policy refused a hop before it was sent, the address did not connect, or there were too many hops |
| `not_document` | a 200 of a type outside the document set: not kept, because a public link download of it waits for the check, and a plain read must not be the way around that |

A body past the file bound is the protocol's `file_too_large`, as for a download.

**The reply.** The first `MAX_READ` window of the file as `read` serves it, with its `file`,
`next_offset` and `source_id`, and `url`, the address that answered, so relative links can be
resolved after a redirect; `read` with the `file_id` goes on. A type that is not
readable text comes back with its file and `text` null.

`text/html` is read as plain text by `downloads.html_text` (stdlib `html.parser`; script and style
and template dropped; undecodable bytes replaced), downloaded pages too. A page's links stand inline:
on a plain read they are resolved from the address that answered and the page's first `<base>`, as a
browser would, so each can be read directly; on a downloaded page they stand as the page writes them.

**Addresses stay whole on a plain read.** There are no handles to follow here, so
`mask_text(..., keep_addresses=True)` leaves addresses out of the address pass: the reply, `read` of a file in `_Active.plain_files`, the
`fields` reply and `url`. The known formats stay masked, and a secret-shaped token is masked in
place (`_TOKEN_IN_ADDRESS`, judged between an address's separators, so a path holding a word and a
number stays an address). A file downloaded or captured from a page is fully masked, as ever: its
address can carry what only the operator can see. A documented residual: no cookie goes with a plain
read, but a site that recognises the requester some other way (its source address, a token in the
address) can still answer with a private link, and the reader sees it whole.

`fields` (dotted paths, JSON only) are `downloads.pick_fields`: the reply holds the same shape with
only those members, a step onto a list maps over it, and a path that is not there is absent. Past
one window it says `complete: false` and has no `next_offset`, because the rest is the file's, not
the fields'. A body that is not JSON sets `not_json` and the reply is the ordinary window.

## Browser downloads

A file the browser downloads itself — a button that builds a `blob:` and clicks it, or a form
`POST` the proxy admitted — lands in `/data/downloads` (Chrome's `downloads_path`; root
bootstrap makes it `casa-browser:casa-controller` `2770`, empties it at every start, and
`backup_exclude` leaves it out of backups). `Downloader.capture_browser_download(run, download,
session, policy)` keeps it only when it came from the session's current page and that page
belongs to the session — still the same document when the download completes — copies it with
the same bounds, and always deletes the browser's copy.
The bound holds while Chrome writes: a download still growing is cancelled
(`file_too_large`) once the downloads directory holds more than the limit; a capture that is
refused, fails or is itself cancelled before Chrome finished cancels the browser's download. A download from
another tab of the session is not kept.

## Files

- **Bounds:** 100 MiB a file, 500 MiB an archive (its content and the zip itself); past either, `file_too_large`,
  never a truncated file. `cleanup_partials` removes partial files an interrupted transfer left.
- **Storage:** `/data/artifacts/files/<file_id>`, `0600`, controller-only; the path never leaves
  the store (`FileRecord.private_path` is excluded from `repr` and the wire).
- **Type:** `detect_mime` believes the content: a signature (`%PDF-`, zip, PNG, JPEG, GIF) or an
  HTML start wins over the declared type; a declared zip-based office or e-book type from a
  fixed list is kept on a zip, and a declared text type from a fixed list on content without a
  signature; everything else is `application/octet-stream` — so the type is always one of a
  fixed set of names, never a declared string echoed. So the measured `application/octet-stream` PDF is a PDF, and a
  login page served as `invoice.pdf` is `text/html`.
- **Name:** `safe_name` keeps the base name of the `Content-Disposition` filename (RFC 6266,
  `filename*` decoded) or of the address's last segment, without directory parts, control or
  path characters or a leading dot, at most 80 characters, with the extension of the detected
  type. Masking comes first (operator ruling E-C3-1, 2026-10-21): observation masking runs on the
  name exactly as received, before any base name is taken, character dropped or cut; if it
  changes the name anywhere, the name is `download` with the type's extension.
- **Archives:** `make_archive(run, file_ids)` zips files of that run and that requester only
  (any other id is `not_found`), entries named by their safe names, made unique (`x (2).pdf`).
  Remote archives are stored as files, never extracted.

**INV-BRW-016**: A stored file is complete and within its bound; a refused, failed or cancelled transfer leaves no file and no record.

## Delivery records

`Downloader.ack_delivery(file_id, requester_id, delivery_id, outcome)` (the
`POST /v1/control/files/{file}/delivery` callback) records one delivery attempt, `outcome` one
of `deposited`, `delivered`, `withheld`, `handed_off`. Each `delivery_id` is one attempt and a
new one becomes the file's current attempt; `deposited` may move to `delivered` or `withheld`;
the same outcome again changes nothing (`recorded: false`); any other change is `conflict`; a
file that is unknown or another requester's is `not_found`. A run record's `delivery_outcomes`
lists every attempt of the run's files, oldest first — a withheld one included.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/downloads.py`

**Tests**
- `casa_browser/tests/test_downloads.py`
- `casa_browser/tests/test_plain_fetch.py`
- `casa_browser/tests/integration/test_file_downloads.py`

**Related**
- [`reference/casa-browser.md`](../reference/casa-browser.md)
- [`reference/casa-browser-runtime.md`](../reference/casa-browser-runtime.md)
- [`reference/casa-browser-browser.md`](../reference/casa-browser-browser.md)
<!-- END SOURCEMAP -->
