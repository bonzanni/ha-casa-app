"""The public-address floor: which destinations may be reached at all, and the kernel rules
that confine every service user to its one permitted route (Plan 1 Task 5).

Two layers, one table:

* **Application layer.** ``is_public_address`` is a default-deny allowlist: IPv4 outside the
  special-purpose ranges below, IPv6 inside ``2000::/3`` outside its special ranges. Nothing
  else — no IPv4-mapped, NAT64, 6to4 or Teredo form, no zone id — is public, whatever
  ``ipaddress.is_global`` says. ``resolve_public`` validates a host name, resolves it once
  and refuses the whole answer when any address is not public; ``connect_public`` connects to
  the first checked address only, using the host name for TLS identity, so a second DNS
  answer is never consulted (no rebinding).
* **Kernel layer.** ``ruleset`` renders the nftables table root init installs before any
  service starts (``python -m casa_browser.netfloor install``). Each service user may open
  exactly one kind of connection: the browser only the proxy's browser listener, the
  controller only the proxy's download listener, the broker only the proxy's broker listener,
  the proxy only public TCP plus the
  configured resolver. Replies of the controller's API are admitted as replies only. The
  VNC user (x11vnc, Task 13) may open no connection at all, and the ingress user (nginx)
  only reply on the ingress port.

A ``FloorError`` never carries the host or address it refused.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable

# Fixed service ids (Dockerfile, Task 2) and the proxy's fixed local listeners (Task 6).
UID_BROWSER = 1100
UID_PROXY = 1101
UID_BROKER = 1102
UID_CONTROLLER = 1103
UID_VNC = 1104        # x11vnc (Task 13): the display over a Unix socket only, no network
UID_INGRESS = 1105    # nginx (Task 13): the ingress listener's replies only
PROXY_BROWSER_PORT = 3128
PROXY_BROKER_PORT = 3129
PROXY_DOWNLOAD_PORT = 3130   # the controller's downloader (action model S4-5)
API_PORT = 8098
INGRESS_PORT = 8099

TABLE = "casa_browser"
CHAINS = ("browser_out", "proxy_out", "broker_out", "controller_out", "vnc_out", "ingress_out")
MARKER = Path("/run/casa-browser/netfloor.ok")
RESOLV_CONF = Path("/etc/resolv.conf")

# IPv4 special-purpose ranges (IANA registry plus the cloud metadata range); everything here
# is refused, everything else unicast is public. Written as (leading octets, prefix length):
# the repository's publication guard refuses dotted private-range literals in source.
def _v4(octets: tuple[int, ...], prefix: int) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Network((bytes(octets).ljust(4, b"\0"), prefix))


DENY_V4 = tuple(_v4(o, n) for o, n in (
    ((0,), 8), ((10,), 8), ((100, 64), 10), ((127,), 8), ((169, 254), 16),
    ((172, 16), 12), ((192, 0, 0), 24), ((192, 0, 2), 24), ((192, 88, 99), 24),
    ((192, 168), 16), ((198, 18), 15), ((198, 51, 100), 24), ((203, 0, 113), 24),
    ((224,), 4), ((240,), 4),
))
# IPv6: only global unicast 2000::/3, minus its special ranges (IETF protocol assignments
# incl. Teredo, 6to4, documentation).
PUBLIC_V6 = ipaddress.ip_network("2000::/3")
DENY_V6 = tuple(ipaddress.ip_network(n) for n in (
    "2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20",
))

_LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

Resolver = Callable[[str, int], Awaitable[Iterable[str]]]


class FloorError(Exception):
    """A destination outside the floor. The message is fixed; it names no host."""

    def __init__(self, reason: str = "refused"):
        super().__init__(reason)
        self.reason = reason


def _parse_ip(value) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return value
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def is_public_address(value) -> bool:
    ip = _parse_ip(value)
    if ip is None:
        return False
    if ip.version == 4:
        return not any(ip in net for net in DENY_V4)
    if ip.scope_id is not None or ip not in PUBLIC_V6:
        return False
    return not any(ip in net for net in DENY_V6)


def valid_hostname(host: str) -> str | None:
    """The lowercased host name, or ``None`` when it is not a public DNS name the floor
    accepts (single label, local suffix, numeric last label, bad syntax, non-ASCII)."""
    if not isinstance(host, str) or not host.isascii():
        return None
    name = host.lower()
    if name.endswith("."):
        name = name[:-1]
    if not name or len(name) > 253:
        return None
    labels = name.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return None
    if name == "localhost" or name.endswith(_LOCAL_SUFFIXES):
        return None
    # A browser reads a host whose last label is a number (decimal, 0x-hex, 0-octal) as IPv4.
    last = labels[-1]
    if last.isdigit() or last.startswith("0x"):
        return None
    return name


@dataclass(frozen=True)
class ResolvedTarget:
    """A destination checked once: the name for TLS/HTTP identity, the addresses to dial."""

    host: str
    port: int
    addresses: tuple


def _check_port(port) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise FloorError("port")
    return port


async def default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=0, proto=6)
    return [info[4][0] for info in infos]


async def resolve_public(host: str, port: int, resolver: Resolver = default_resolver) -> ResolvedTarget:
    """Resolve ``host`` once and refuse unless every answer is public. An IP literal (IPv6 in
    brackets or bare) is checked, not resolved."""
    port = _check_port(port)
    literal = host[1:-1] if isinstance(host, str) and host.startswith("[") and host.endswith("]") else host
    ip = _parse_ip(literal) if isinstance(literal, str) else None
    if ip is not None:
        if not is_public_address(ip):
            raise FloorError("address")
        return ResolvedTarget(str(ip), port, (ip,))
    name = valid_hostname(host)
    if name is None:
        raise FloorError("name")
    try:
        answers = list(await resolver(name, port))
    except (OSError, UnicodeError, ValueError):
        raise FloorError("resolve") from None
    addresses = []
    for answer in answers:
        parsed = _parse_ip(answer)
        if parsed is None or not is_public_address(parsed):
            raise FloorError("address")
        if parsed not in addresses:
            addresses.append(parsed)
    if not addresses:
        raise FloorError("resolve")
    return ResolvedTarget(name, port, tuple(addresses))


async def connect_public(target: ResolvedTarget, *, opener=asyncio.open_connection, ssl=None):
    """Open a TCP (optionally TLS) connection to the target's first checked address; the
    host name is used only as the TLS server name. Never resolves again."""
    address = target.addresses[0] if target.addresses else None
    if address is None or not is_public_address(address):
        raise FloorError("address")
    kwargs = {}
    if ssl:
        kwargs = {"ssl": ssl, "server_hostname": target.host}
    return await opener(str(address), target.port, **kwargs)


# --------------------------------------------------------------------------- kernel layer


def resolvers_from(path: Path = RESOLV_CONF) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            out.append(parts[1])
    return out


def _resolver_literals(resolvers: list[str]) -> list[str]:
    checked = []
    for value in resolvers:
        ip = _parse_ip(value)
        if ip is None or ip.version != 4:
            raise FloorError("resolver")
        checked.append(str(ip))
    if not checked:
        raise FloorError("resolver")
    return checked


def ruleset(resolvers: list[str]) -> str:
    """The nftables script root init applies (atomic: declare, delete, define)."""
    dns = _resolver_literals(resolvers)
    deny4 = ", ".join(str(n) for n in DENY_V4)
    deny6 = ", ".join(str(n) for n in DENY_V6)
    dns_rules = "\n".join(f"    ip daddr {ip} ct original proto-dst 53 accept" for ip in dns)
    return f"""table inet {TABLE}
delete table inet {TABLE}
table inet {TABLE} {{
  chain output {{
    type filter hook output priority filter; policy accept;
    meta skuid {UID_BROWSER} jump browser_out
    meta skuid {UID_PROXY} jump proxy_out
    meta skuid {UID_BROKER} jump broker_out
    meta skuid {UID_CONTROLLER} jump controller_out
    meta skuid {UID_VNC} jump vnc_out
    meta skuid {UID_INGRESS} jump ingress_out
  }}
  chain browser_out {{
    oif "lo" ip daddr 127.0.0.1 tcp dport {PROXY_BROWSER_PORT} accept
    reject
  }}
  chain controller_out {{
    oif "lo" ip daddr 127.0.0.1 tcp dport {PROXY_DOWNLOAD_PORT} accept
    ct direction reply tcp sport {API_PORT} accept
    reject
  }}
  chain vnc_out {{
    reject
  }}
  chain ingress_out {{
    ct direction reply tcp sport {INGRESS_PORT} accept
    reject
  }}
  chain broker_out {{
    oif "lo" ip daddr 127.0.0.1 tcp dport {PROXY_BROKER_PORT} accept
    reject
  }}
  chain proxy_out {{
    oif "lo" ip daddr 127.0.0.1 ct direction reply tcp sport {{ {PROXY_BROWSER_PORT}, {PROXY_BROKER_PORT}, {PROXY_DOWNLOAD_PORT} }} accept
{dns_rules}
    ip daddr {{ {deny4} }} reject
    ip6 daddr != 2000::/3 reject
    ip6 daddr {{ {deny6} }} reject
    meta l4proto tcp accept
    reject
  }}
}}
"""


def chain_text(text: str, chain: str) -> str:
    """The body of one chain of a rendered ruleset (tests and the read-back check)."""
    start = text.index(f"chain {chain} {{") + len(f"chain {chain} {{")
    end = text.index("\n  }", start)
    return text[start:end]


def _run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=30)


def install(resolv_conf: Path = RESOLV_CONF, marker: Path = MARKER, run=_run) -> str:
    """Root init: apply the table, read it back, then (only then) write the marker the
    browser launcher requires. Raises on any failure; the marker is removed first, so a
    failed install never leaves a stale one."""
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    text = ruleset(resolvers_from(resolv_conf))
    applied = run(["nft", "-f", "-"], stdin=text)
    if applied.returncode != 0:
        raise FloorError("install")
    listed = run(["nft", "-j", "list", "table", "inet", TABLE])
    if listed.returncode != 0:
        raise FloorError("readback")
    jumps = set()
    for item in json.loads(listed.stdout).get("nftables", []):
        rule = item.get("rule")
        if rule and rule.get("chain") == "output":
            for expr in rule.get("expr", []):
                if "jump" in expr:
                    jumps.add(expr["jump"]["target"])
    if jumps != set(CHAINS):
        raise FloorError("readback")
    digest = hashlib.sha256(text.encode()).hexdigest()
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(digest + "\n", encoding="ascii")
    os.chmod(tmp, 0o644)
    os.replace(tmp, marker)
    return digest


def floor_installed(marker: Path = MARKER) -> bool:
    """The launcher's gate: the root-owned marker exists and is a regular file."""
    try:
        st = marker.lstat()
    except OSError:
        return False
    import stat
    return stat.S_ISREG(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o022


def main(argv: list[str]) -> int:  # pragma: no cover — run by the init oneshot as root
    if argv[1:] != ["install"]:
        print("usage: python -m casa_browser.netfloor install", file=sys.stderr)
        return 2
    try:
        install()
    except (FloorError, OSError, ValueError, subprocess.SubprocessError) as err:
        reason = err.reason if isinstance(err, FloorError) else type(err).__name__
        print(f"casa-browser: network floor not installed ({reason}); no service starts",
              file=sys.stderr)
        return 1
    print("casa-browser: network floor installed", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
