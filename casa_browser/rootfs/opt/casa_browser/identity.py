"""The floor's URL check: ``address`` splits a web address into its origin and parts, or
refuses it (``IdentityError``). ``SEMANTIC_HEADERS`` names the request headers that change
what a server does with a request (the infrastructure rules and the proxy read them)."""

import re
from urllib.parse import urlsplit

SEMANTIC_HEADERS = frozenset(
    (
        "content-type",
        "content-encoding",
        "next-action",
        "x-http-method-override",
        "x-http-method",
        "x-method-override",
        "soapaction",
    )
)


class IdentityError(ValueError):
    def __init__(self):
        super().__init__("unidentifiable")


def address(url):
    try:
        if (
            not isinstance(url, str)
            or any(ord(c) <= 32 or ord(c) == 127 for c in url)
            or "\\" in url
        ):
            raise IdentityError()
        u = urlsplit(url)
        host = u.hostname
        if (
            u.scheme not in ("http", "https", "ws", "wss")
            or not host
            or not host.isascii()
            or u.username is not None
            or u.password is not None
            or "%" in host
        ):
            raise IdentityError()
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
            raise IdentityError()
        port = u.port
        if port == 0 or u.netloc.endswith(":"):
            raise IdentityError()
        scheme = {"ws": "http", "wss": "https"}.get(u.scheme, u.scheme)
        host = host.lower()
        host = "[" + host + "]" if ":" in host else host
        origin = scheme + "://" + host
        if port is not None and port != {"http": 80, "https": 443}[scheme]:
            origin += ":" + str(port)
        return origin, u.path, u.query, "?" in url.split("#", 1)[0], u.fragment, "#" in url
    except (ValueError, TypeError, UnicodeError):
        raise IdentityError() from None
