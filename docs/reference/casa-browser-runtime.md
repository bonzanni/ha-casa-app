---
last_reviewed: 2026-10-22
---

# Casa Browser — network floor and enforcing proxy

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The runtime layers of the optional `casa_browser/` app that decide what may leave the
container: the kernel egress floor root init installs before any service starts, and the
enforcing TLS proxy every browser request passes through. The policy the proxy applies — the
address check, the precedence of its rules, the fixed infrastructure exceptions — is the
pure `policy.decide` described in [`casa-browser.md`](casa-browser.md); this document covers
how it is enforced on real sockets.

## Mental model

**One route per service user.** The app runs six service users with fixed ids
(`casa-browser` 1100, `casa-proxy` 1101, `casa-broker` 1102, `casa-controller` 1103, and for
the live view `casa-vnc` 1104 and `casa-ingress` 1105). The kernel lets each open at most one
kind of connection, and everything else is refused before a packet leaves: the browser reaches
only the proxy's browser listener (`127.0.0.1:3128`), the controller only the proxy's download
listener (`127.0.0.1:3130`, its downloader's one route), the broker only the proxy's broker
listener (`127.0.0.1:3129`), the proxy only public TCP and the configured DNS
resolver, x11vnc (`casa-vnc`) nothing at all, nginx (`casa-ingress`) only its replies on the
ingress port 8099 ([`casa-browser-ingress.md`](casa-browser-ingress.md)). A user without a
chain would be accepted by the output chain's default, so every service user has one. Root is not confined: root bootstrap reads
Home Assistant's language before any service starts. Application checks sit on top of the
kernel rules, never instead of them.

**The proxy decides before it connects.** mitmproxy runs with lazy upstream connections, so a
request reaches its destination only after the addon has decided it, with its whole body, on
the policy the controller installed last. The address the proxy then dials is the one the
decision checked: names are resolved once per decision and nowhere else.

## The kernel floor

`casa_browser/rootfs/opt/casa_browser/netfloor.py::ruleset` renders one nftables table,
`inet casa_browser`, with an `output` chain that jumps per `meta skuid` to one chain per
service user. The script declares, deletes and redefines the table in one transaction, so a
re-run replaces it atomically. The proxy's DNS rule matches `ct original proto-dst 53` to the
resolvers in `/etc/resolv.conf`: Docker's embedded resolver at `127.0.0.11` is reached through
a NAT rule that rewrites the port before the filter hook, so a rule on `udp dport 53` would
never match. The proxy's remaining rules refuse the special-purpose IPv4 ranges and every
IPv6 address outside `2000::/3` (and its special ranges) before admitting TCP. The controller
may also answer connections to its API port (`ct direction reply`, source port 8098), and
nginx connections to port 8099 the same way.

The oneshot `init-browser-netfloor` (`casa_browser/rootfs/etc/s6-overlay/scripts/browser-netfloor.sh`)
runs `netfloor.py::install` as root after `init-browser-config`: it removes the marker
`/run/casa-browser/netfloor.ok`, applies the table, reads it back (`nft -j list`) and writes the
marker only when the six jumps are present. A failure fails the oneshot, and every service
depends on it.

**INV-BRW-047**: Each service user can open only its one permitted route — the browser user the proxy's browser listener, the controller user the proxy's download listener, the broker user the proxy's broker listener, the proxy user public TCP and the configured resolver, the VNC user none, the ingress user only replies on its port — and no service starts unless root init installed that floor.

## Public destinations

`netfloor.py::is_public_address` is a default-deny allowlist: IPv4 outside the special-purpose
ranges (`netfloor.py::DENY_V4`, including the link-local metadata range), IPv6 only inside
`2000::/3` and outside `netfloor.py::DENY_V6` — so no IPv4-mapped, NAT64, 6to4 or Teredo form
and no address with a zone id is public. `netfloor.py::resolve_public` refuses a name that is
single-label, ends in a local suffix or a number, or is not plain ASCII DNS syntax, resolves
it once, and refuses the whole answer if any address is not public. An IP literal is checked,
never resolved. A `netfloor.py::FloorError` names no host or address.

## The enforcing proxy

`casa_browser/rootfs/opt/casa_browser/proxy_main.py::Proxy` runs mitmproxy in-process with a
curated addon set and `proxy_addon.py::EnforcingAddon` last: three loopback listeners, lazy
upstream connections, raw TCP off, no upstream certificate fetch, upstream certificates
verified normally. The service `svc-browser-proxy` starts it as `casa-proxy` with an
allowlisted environment; it generates a per-boot CA in `/run/casa-browser/proxy-private`
(`0700`, made by root bootstrap) and exports only the public certificate to
`/run/casa-browser/ca/ca.pem`, then signals readiness. Every library log record is reduced to
its level and logger name.

**Per request on the browser listener** (`proxy_addon.py::EnforcingAddon.request`): the
request must name one host — tunnel, TLS server name and `Host` agree — and carry at most one
of each kept semantic header; its resource class comes from what the browser itself sets
(`Sec-Fetch-Dest`, `Sec-Fetch-Mode`, `Upgrade: websocket`, `Access-Control-Request-Method`),
and a request without them is `other`. `policy.decide` runs first without addresses; if it
allows, the host is resolved through the floor and the request is decided again on the policy
current at that moment. A refusal is answered locally with an empty `403`. A request whose
body cannot be bounded before it is read — a chunked HTTP/1.1 body, a declared length over
1 MiB, or an HTTP/2 request with no declared length unless a browser cannot attach a body to it
(GET, HEAD, a CORS preflight) — is refused at its headers and its stream killed, so the body is never read into memory (mitmproxy's own `body_size_limit` is not used:
it aborts large responses before a hook can stream them). Two buffers inside mitmproxy have no
bound a hook can reach — the accepted residual and the proxy's memory cap are described in
[`casa-browser.md`](casa-browser.md), "Known limits". An absolute-form request target must name the same host as the tunnel. A
`CONNECT` to a private literal or a malformed name is refused at the tunnel; opaque traffic
inside a tunnel is closed.

**The download listener** (`netfloor.py::PROXY_DOWNLOAD_PORT`, 3130) carries only the
controller's downloader (`downloads.PROXY_URL`). It is decided exactly as the browser listener
is — the same checks, pins and body bounds — except that `policy.decide` runs with `hop=True`:
the floor, the infrastructure rules and Chrome's background refusals, never the account
session's document rule, so a link download's off-site CDN hop is fetched while the browser's
own top-level documents stay on the site. Its decisions are recorded with the listener
`download`.

**Every hook fails closed** (`proxy_addon.py::fail_closed`). mitmproxy logs an exception
raised in an addon hook and then carries on, so a hook that raised would let its request
through undecided; here a request-phase hook refuses the flow, a later one closes the client
connection and drops a pending WebSocket frame, and `server_connect` refuses the dial.

**Pins.** An allowing resolution leaves a pin, host and port to the checked addresses, for 60
seconds. mitmproxy dials through the event loop's resolver, and the proxy's loop
(`proxy_main.py::PinnedLoop`) answers only from fresh pins: any other name, and any non-public
literal, fails to resolve. `server_connect` refuses a host without a fresh pin as well. The
address dialled is therefore one the floor checked for that host — the answer of the latest
resolution that admitted a request to it, which a concurrent admission may have refreshed —
and mitmproxy's upstream connection reuse is kept.

**Withdrawing authority.** A policy or lease change replaces the policy in one assignment on
the proxy's event loop and advances an epoch. `install`, `revoke` and the end of a manual
lease then close every browser- and download-listener connection through mitmproxy's live
connection handlers — the browser's connection, its upstream connections and any upstream
connection still opening — before the control call replies; a flow's own kill flag does not
stop a flow in transit, so closing the sockets is the mechanism. `end_login` closes them only
when a login lease admitted a request the policy without it refuses (a provider's sign-in
page) and no close of every connection happened since — whichever lease the call names: a
replaced lease, or one already closed at its landing document, keeps the mark, and only such a
close ends it. Otherwise every request admitted under a lease is one the policy after it admits too, and
closing would only cut a page still loading: measured, a sign-in page's own scripts and calls
cut right after its first load left it on a spinner until a reload. A WebSocket frame arriving under a newer
epoch is decided again, and a refused frame is dropped and its connection closed. Frames on an
allowed WebSocket are not inspected.

**Login-lease closure.** After the broker's submit the controller arms the lease
(`arm_login_close`); the first document response whose request started after the arming
clears the login lease in `responseheaders`, before any byte of that response reaches the
browser. Every request the landing page makes is decided without the lease. Ending the lease
preserves the credential window described below.

**Credential windows.** The proxy starts with a window open. `begin_login`,
`begin_manual(purpose='login'|'assist')` and `open_window` reopen it and return a fresh
token consisting of a per-start random nonce and an increasing counter. A manual lease is
only for a `login` or an `assist` view (`begin_manual` refuses any other purpose before
anything changes); a reinstall that keeps that lease keeps its purpose, which the binding
projection of a login view reads. Only `close_window(token)` for the current token schedules its
close, three seconds after the later of now and the last connection-closing authority
change. Repeated or stale closes have no effect. While a close is scheduled, authority
withdrawals and the post-submit lease closure push the deadline forward by three seconds;
another opener cancels it.

Every listener withholds what it can when the window was open at request headers or is open
at recording. Events carry fixed policy outcomes and resource classes; unknown methods become
`OTHER`. Browser origins are disclosed only from the registered run origins and fixed
extension, broker and infrastructure hosts; broker origins are disclosed only
when registered by the run. Other origins are null. The window does not bound a site's
script that deliberately retains a credential and sends it after the window closes: normal
private event recording resumes then.

Only a manual lease of purpose `login` projects provider authorization candidates. The
proxy tags the raw path, client id and redirect URI with label-separated HMAC-SHA256 and
numbers candidates within the lease. A later document matching the decoded callback's
origin and path carries only a candidate reference. Ending or replacing the lease clears
its candidate state. The proxy creates its 32-byte key at `/data/proxy/binding.key`, in a
`0700` directory with a `0600` file. Restarts reuse it; a missing or unreadable key is
replaced. HA backups exclude `proxy/*`, so restored provider tags require a live login. The
proxy names its key without revealing it — `key_id`, the first 16 hex digits of an HMAC of a
fixed label under the key — and reports it as `binding_key_id` in its `status`; the controller
reads it at start and tells a binding learned under another key by it.

**INV-BRW-042**: On the browser and download listeners no byte reaches an upstream for a request the current policy refuses — an account session's off-site top-level document included, while its pages' other requests reach any floor-checked origin — the proxy dials only an address the floor checked for that host (never a name resolved outside a decision), and once a policy change is acknowledged no request or WebSocket frame admitted under the earlier policy is sent, except that `end_login` leaves in flight what the policy after it admits when no login lease has admitted anything the policy without it refuses since the last close of every connection.

**The broker listener** carries only the broker's 1Password SDK traffic: `CONNECT` to a host
on `infra.py::BROKER_SDK_HOSTS` or the configured account host, port 443, intercepted, with
its TLS server name and every `Host` equal to that host. It applies no run policy, and the
browser listener uses its separate, scoped 1Password rule described below.

**INV-BRW-012**: The broker listener admits a request only to a listed 1Password host on port 443 whose `CONNECT` host, TLS server name and `Host` header are the same host.

**The account host.** Root bootstrap validates `guest_sign_in_address` as one exact account
host (`<team>.1password.<eu|com|ca>`; the generic `my.` address is refused) and writes it only
to `/run/casa-browser/proxy-private/hosts.json`. The proxy admits it for the broker listener
and, with its region's exact service hosts (`infra.py::onepassword_extension_hosts`: the
sign-in, account-cookie, static-app, user-content and push hosts), for sandbox browser
traffic in an account session (`policy.decide`'s `extra_hosts`). Admission uses exact host
names, HTTPS or WSS and port 443; no wildcard or request header widens that list. It is
separate from each run's site lock and does not attribute requests to the extension.
The guest driver opens its credential window before navigation to withhold request
origins while guest credentials are in use; closing that window does not withdraw the
fixed host list.

**Chrome's own updates.** The managed policy force-installs the 1Password extension, and Chrome
fetches it through three fixed infrastructure rules in `infra.py`, admitted only in an account
session: the extension update check (`GET clients2.google.com/service/update2/crx`), the CRX
download (`GET clients2.googleusercontent.com/crx/blobs/…`) and the signed update check
(`POST update.googleapis.com/service/update2/json` carrying its `cup2key`/`cup2hreq`
signature), each with its own query grammar. Everything else Chrome sends on its own — account
sign-in, device check-in, field-trial seeds, the per-cache download redirector — is refused,
and the extension still installs. In an account session a request the browser or the 1Password
extension makes on its own, not a navigation, is refused as `browser_background`: never a
question to the operator. "On its own" is the browser's signal (`RequestMeta.browser_own`, set by
`proxy_addon.request_meta`): `Sec-Fetch-Site: none` outside a navigation, to any host outside the
run's site set (`infra.browser_initiated`, checked before the account session's own rules;
measured: Chrome's own requests carry `none`, `no-cors`, `empty`, the extension's calls to
`my.1password.com` and `watchtower.1password.com` carry `none`, `cors`, `empty`; a page cannot
send `none`, and a page's `fetch` carries a real `Sec-Fetch-Site`); with no `Sec-Fetch-*` header
at all (plain http carries none) only to one of `infra.py::CHROME_BACKGROUND_HOSTS` or a
per-cache `…gvt1.com` host (`infra.chrome_background`, right after the infrastructure rules).
The same host asked by a page is an ordinary request.

**Plain http to a site.** Chrome sends an address typed without a scheme as plain http (its
HTTPS upgrades are off under the driver). A bodiless http `GET`/`HEAD` on port 80 to a host a
granted site registered over https (`policy.https_upgrade`) is answered by the proxy itself with
a `307` to that https address, same path and query — never sent upstream, recorded as the quiet
`https_upgrade` refusal; the https request is then decided like any other.

**The flag warning.** Patchright's `--disable-blink-features=AutomationControlled` (it keeps
`navigator.webdriver` false) is on Chrome's list of unsupported flags; the managed policy's
`CommandLineFlagSecurityWarningsEnabled: false` hides the bar Chrome shows for it.

## The control plane

`casa_browser/rootfs/opt/casa_browser/proxy_client.py::ProxyClient` is the controller's side
of the control socket `/run/casa-browser/proxy/control.sock`: newline-delimited JSON, one
request and one reply per connection, and the proxy answers only a peer whose `SO_PEERCRED`
uid is `casa-controller`. `install` returns only after the proxy holds the new policy and has
withdrawn the old one's connections. An install may name the proxy start it expects
(`start`, the first member of the window token `status()` reports, drawn at each proxy start):
a proxy that restarted since refuses it (`restarted`) and keeps its policy, so the controller
never reinstalls a run across a proxy restart. `proxy_client.py::decode_policy` refuses any member it
does not recognise, so a malformed install leaves the current policy in place. A proxy that
(re)starts holds no policy and refuses every request until one is installed.

`proxy_client.py::PrivateRequestEvent` carries each decision — run, revision, listener,
outcome and reason, method, resource class and, outside credential windows (or for a known
host), the origin — to controller-private memory only, never a path, query, header or body, from
a ring bounded at 10 000 events and 64 MiB in total; the oldest go first, and the ring counts what it
drops. The control encoding preserves the optional binding tags or callback reference;
private fields are excluded from repr. It has no wire projection.

## Testing

`casa_browser/tests/test_proxy_adapter.py` runs the real proxy on loopback against a TLS
fixture behind a relay that counts every upstream connection and byte; refusals assert zero of
both. The only test seam is at the harness boundary (`casa_browser/tests/proxyharness.py`): a
resolver answering a public-looking address, which the production floor checks, and a loop
that maps that pinned address to the fixture's port. The image lane
(`casa_browser/test/run-integration.sh`, `make test-browser-integration`) starts the app image
with its own s6 `/init` and `NET_ADMIN` only, beside a reachable private target, and probes
every service user with root positive controls on the same destinations.

The addon accepts an injected binding tagger and passes it to every policy decision.
Policy encoding and decoding carry only the provider origin and three validated tags;
legacy raw binding members are refused. The private control status also returns the
active run and site ids with the site's account origins so the driver can distinguish a
landing page from a login page and refuse metadata for another session. Status also returns
the window's open state and current token. The client returns opener acknowledgements'
window tokens to the session owner and forwards explicit window open and close calls.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/etc/s6-overlay/scripts/browser-netfloor.sh`
- `casa_browser/rootfs/etc/s6-overlay/s6-rc.d/svc-browser-proxy/run`
- `casa_browser/rootfs/opt/casa_browser/netfloor.py`
- `casa_browser/rootfs/opt/casa_browser/proxy_addon.py`
- `casa_browser/rootfs/opt/casa_browser/proxy_client.py`
- `casa_browser/rootfs/opt/casa_browser/proxy_main.py`
- `casa_browser/test/run-integration.sh`

**Tests**
- `casa_browser/tests/test_netfloor.py`
- `casa_browser/tests/test_proxy_adapter.py`
- `casa_browser/tests/integration/test_network_floor.py`
- `casa_browser/tests/integration/test_proxy_limits.py`
- `casa_browser/tests/integration/test_secret_boundaries.py`

**Related**
- [`reference/casa-browser.md`](../reference/casa-browser.md)
<!-- END SOURCEMAP -->
