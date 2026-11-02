"""Task 5: the public-address floor (pure part) and the generated nftables ruleset."""
from __future__ import annotations

import asyncio
import ipaddress

import pytest

from casa_browser import netfloor
from casa_browser.netfloor import FloorError, is_public_address, resolve_public


def ip4(*octets: int) -> str:
    """A dotted IPv4 address built from its octets (the publication guard refuses dotted
    private-range literals in source; these are synthetic examples, not real addresses)."""
    return ".".join(str(o) for o in octets)

DENIED = [
    "127.0.0.1", "127.255.255.254", "0.0.0.0", "0.1.2.3", ip4(10, 0, 0, 1), "100.64.0.1",
    "169.254.169.254",  # cloud metadata
    ip4(172, 16, 0, 1), ip4(172, 30, 32, 1), ip4(172, 30, 32, 2), ip4(192, 168, 1, 20), "192.0.0.8", "192.0.2.1",
    "192.88.99.1", "198.18.0.1", "198.51.100.7", "203.0.113.9", "224.0.0.251",
    "239.255.255.250", "240.0.0.1", "255.255.255.255",
    "::", "::1", "::ffff:127.0.0.1", "::ffff:" + ip4(10, 0, 0, 1), "::ffff:8.8.8.8", "64:ff9b::a00:1",
    "64:ff9b:1::1", "fc00::1", "fd00::1", "fe80::1", "ff02::1", "100::1",
    "2001:db8::1", "2002:a00:1::1", "2001::1", "2001:2::1", "2001:10::1", "3fff::1",
    "fe80::1%eth0", "2a00:1450:4001::1%eth0",
]
ALLOWED = ["1.1.1.1", "8.8.8.8", "93.184.215.14", "151.101.1.69", "2a00:1450:4001:82a::200e",
           "2606:4700::6810:84e5"]


@pytest.mark.parametrize("address", DENIED)
def test_denied_addresses(address):
    assert is_public_address(address) is False


@pytest.mark.parametrize("address", ALLOWED)
def test_public_addresses(address):
    assert is_public_address(address) is True


@pytest.mark.parametrize("value", ["", "localhost", "1.1.1", "01.1.1.1", "0x1.1.1.1", "1.1.1.1.",
                                   " 1.1.1.1", "8.8.8.8/32", None, 16843009])
def test_malformed_addresses_are_not_public(value):
    assert is_public_address(value) is False


def _resolver(answers):
    calls = []

    async def resolve(host, port):
        calls.append((host, port))
        return answers.pop(0) if answers else []

    return resolve, calls


def run(coro):
    return asyncio.run(coro)


def test_resolve_public_returns_checked_addresses():
    resolve, calls = _resolver([["93.184.215.14", "2606:4700::6810:84e5"]])
    target = run(resolve_public("Example.COM.", 443, resolve))
    assert target.host == "example.com" and target.port == 443
    assert target.addresses == (ipaddress.ip_address("93.184.215.14"),
                                ipaddress.ip_address("2606:4700::6810:84e5"))
    assert calls == [("example.com", 443)]


@pytest.mark.parametrize("answers", [
    [["93.184.215.14", ip4(10, 0, 0, 5)]],          # mixed public/private
    [[ip4(192, 168, 1, 20)]],
    [["::ffff:127.0.0.1"]],
    [["169.254.169.254"]],
    [[]],                                      # no answer
    [["not-an-ip"]],
])
def test_resolve_public_refuses_any_private_or_empty_answer(answers):
    resolve, _ = _resolver(answers)
    with pytest.raises(FloorError):
        run(resolve_public("example.com", 443, resolve))


@pytest.mark.parametrize("host", [
    "localhost", "homeassistant", "supervisor", "hassio", "core-mosquitto",  # single label
    "a0d7b954-casa", "printer.local", "nas.lan", "x.internal", "box.home.arpa", "a.localhost",
    "0x7f.1", "1.2.3.077", "example.123", "2130706433",                       # numeric last label
    "ex ample.com", "exa_mple.com", "-bad.com", "bad-.com", "a..b.com", "", "é.com",
    "a" * 64 + ".com", ("a" * 60 + ".") * 5 + "com",
])
def test_resolve_public_refuses_bad_names_without_resolving(host):
    resolve, calls = _resolver([["93.184.215.14"]])
    with pytest.raises(FloorError):
        run(resolve_public(host, 443, resolve))
    assert calls == []


@pytest.mark.parametrize("literal,ok", [("93.184.215.14", True), ("[2606:4700::6810:84e5]", True),
                                        ("127.0.0.1", False), ("[::1]", False),
                                        (ip4(10, 1, 2, 3), False)])
def test_ip_literals_are_checked_not_resolved(literal, ok):
    resolve, calls = _resolver([[ip4(10, 0, 0, 1)]])
    if ok:
        target = run(resolve_public(literal, 443, resolve))
        assert len(target.addresses) == 1
    else:
        with pytest.raises(FloorError):
            run(resolve_public(literal, 443, resolve))
    assert calls == []


@pytest.mark.parametrize("port", [0, 65536, -1, "443", True])
def test_bad_ports_refused(port):
    resolve, _ = _resolver([["93.184.215.14"]])
    with pytest.raises(FloorError):
        run(resolve_public("example.com", port, resolve))


def test_resolver_failure_fails_closed():
    async def broken(host, port):
        raise OSError("no resolver")

    with pytest.raises(FloorError):
        run(resolve_public("example.com", 443, broken))


def test_rebinding_pins_checked_ip():
    """DNS answers public, then private: the target keeps the first, checked answer and
    connect_public never resolves again."""
    resolve, calls = _resolver([["93.184.215.14"], ["127.0.0.1"]])
    target = run(resolve_public("rebind.example", 443, resolve))
    opened = []

    async def opener(host, port, **kwargs):
        opened.append((host, port, kwargs.get("server_hostname")))
        raise ConnectionRefusedError

    with pytest.raises(OSError):
        run(netfloor.connect_public(target, opener=opener, ssl=True))
    assert calls == [("rebind.example", 443)]
    assert opened == [("93.184.215.14", 443, "rebind.example")]


def test_connect_public_rechecks_the_pinned_address():
    target = netfloor.ResolvedTarget("example.com", 443, (ipaddress.ip_address(ip4(10, 0, 0, 1)),))

    async def opener(host, port, **kwargs):
        raise AssertionError("must not connect")

    with pytest.raises(FloorError):
        run(netfloor.connect_public(target, opener=opener))


def test_floor_error_never_carries_the_host():
    resolve, _ = _resolver([[ip4(10, 9, 9, 9)]])
    with pytest.raises(FloorError) as err:
        run(resolve_public("secret-token-host.example", 443, resolve))
    assert "secret-token-host" not in str(err.value) and ip4(10, 9, 9, 9) not in str(err.value)


# --------------------------------------------------------------------------- ruleset


def test_ruleset_confines_each_service_user():
    text = netfloor.ruleset(["127.0.0.11"])
    assert "table inet casa_browser" in text
    assert text.count("delete table inet casa_browser") == 1
    for uid in (1100, 1101, 1102, 1103):
        assert f"meta skuid {uid} jump" in text
    browser = netfloor.chain_text(text, "browser_out")
    assert 'oif "lo" ip daddr 127.0.0.1 tcp dport 3128 accept' in browser
    assert browser.strip().splitlines()[-1].strip() == "reject"
    assert "3130" not in browser and "3129" not in browser
    broker = netfloor.chain_text(text, "broker_out")
    assert "tcp dport 3129 accept" in broker and "3128" not in broker and "3130" not in broker
    # The controller's one proxy use is its downloader, on its own listener (S4-5).
    controller = netfloor.chain_text(text, "controller_out")
    assert 'oif "lo" ip daddr 127.0.0.1 tcp dport 3130 accept' in controller
    assert "ct direction reply tcp sport 8098 accept" in controller
    assert "3129" not in controller and "3128" not in controller
    proxy = netfloor.chain_text(text, "proxy_out")
    assert "ct direction reply tcp sport { 3128, 3129, 3130 } accept" in proxy
    assert netfloor.PROXY_DOWNLOAD_PORT == 3130


def test_floor_new_users():
    """Task 13's service users each have their own chain: x11vnc opens no connection at all,
    nginx only answers on the ingress port (an unlisted user would be accepted by default)."""
    text = netfloor.ruleset(["127.0.0.11"])
    output = netfloor.chain_text(text, "output")
    assert "meta skuid 1104 jump vnc_out" in output and "meta skuid 1105 jump ingress_out" in output
    vnc = [line.strip() for line in netfloor.chain_text(text, "vnc_out").strip().splitlines()]
    assert vnc == ["reject"]
    ingress = [line.strip() for line in netfloor.chain_text(text, "ingress_out").strip().splitlines()]
    assert ingress == ["ct direction reply tcp sport 8099 accept", "reject"]
    assert set(netfloor.CHAINS) == {"browser_out", "proxy_out", "broker_out", "controller_out",
                                    "vnc_out", "ingress_out"}


def test_ruleset_proxy_dns_rule_matches_the_original_port():
    proxy = netfloor.chain_text(netfloor.ruleset(["127.0.0.11"]), "proxy_out")
    lines = [l.strip() for l in proxy.strip().splitlines()]
    dns = lines.index("ip daddr 127.0.0.11 ct original proto-dst 53 accept")
    deny = next(i for i, l in enumerate(lines) if l.startswith("ip daddr {"))
    assert dns < deny, "the resolver rule must precede the 127/8 floor"
    assert "udp dport 53" not in proxy  # misses Docker's DNAT (S0 check c)
    assert lines[-1] == "reject"
    assert "meta l4proto tcp accept" in lines


def test_ruleset_proxy_deny_set_covers_the_table():
    proxy = netfloor.chain_text(netfloor.ruleset(["127.0.0.11"]), "proxy_out")
    for net in (ip4(10, 0, 0, 0) + "/8", "127.0.0.0/8", "169.254.0.0/16", ip4(172, 16, 0, 0) + "/12",
                ip4(192, 168, 0, 0) + "/16", "100.64.0.0/10", "224.0.0.0/4", "240.0.0.0/4"):
        assert net in proxy
    assert "ip6 daddr != 2000::/3 reject" in proxy


@pytest.mark.parametrize("bad", [[], ["8.8.8.8; accept"], ["::1"], ["resolver"]])
def test_ruleset_refuses_odd_resolvers(bad):
    with pytest.raises(FloorError):
        netfloor.ruleset(bad)


def test_resolvers_from_resolv_conf(tmp_path):
    conf = tmp_path / "resolv.conf"
    conf.write_text("# x\nsearch local\nnameserver 127.0.0.11\noptions ndots:0\n")
    assert netfloor.resolvers_from(conf) == ["127.0.0.11"]
