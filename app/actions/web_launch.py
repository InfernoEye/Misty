"""Validated web navigation helpers for opening pages and site searches.

The model chooses a site key and query, or supplies one public URL. This
module builds a canonical URL without shell text, never scrapes, and rejects
local/private hosts for direct opens.

Host validation is deliberately strict and *syntax-only*: it never resolves
DNS.  A hostname that looks public is accepted here even if it later resolves
to a private address; the network layer is responsible for that second
check (the fetcher pins the resolved address, the browser aborts every
request through a URL fence).  Stricter checks that *can* be made from the
text alone are enforced:

* only ``http``/``https``;
* no embedded credentials, control characters, whitespace, backslashes;
* ASCII-only hostnames (internationalized names must be already punycoded),
  so Chromium's IDNA mapping cannot turn one spelling into another;
* canonical dotted-quad IPv4 literals or bracketed IPv6 literals only.  The
  legacy/non-canonical IPv4 spellings browsers and ``inet_aton`` accept
  (``127.1``, ``2130706433``, ``0x7f000001``, ``0177.0.0.1``, ``127.0.1``)
  are rejected rather than reinterpreted;
* no ``localhost``/``.localhost``/``.local``/``.internal``/``.home.arpa``
  and no single-label (dotless) hostnames;
* IP literals must be globally routable (:func:`is_public_ip`).
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote_plus, urlsplit


SITE_SEARCHES = {
    "youtube": "https://www.youtube.com/results?search_query={query}",
    "google": "https://www.google.com/search?q={query}",
    "duckduckgo": "https://duckduckgo.com/?q={query}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={query}",
    "github": "https://github.com/search?q={query}&type=repositories",
    "archwiki": "https://wiki.archlinux.org/index.php?search={query}",
    "stackoverflow": "https://stackoverflow.com/search?q={query}",
    "allegro": "https://allegro.pl/listing?string={query}",
    "bing": "https://www.bing.com/search?q={query}",
    "reddit": "https://www.reddit.com/search/?q={query}",
    "amazon": "https://www.amazon.com/s?k={query}",
    "ebay": "https://www.ebay.com/sch/i.html?_nkw={query}",
    "twitch": "https://www.twitch.tv/search?term={query}",
    "spotify": "https://open.spotify.com/search/{query}",
}

MAX_URL_LENGTH = 2048
MAX_QUERY_LENGTH = 200

ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}

_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".home.arpa",
)
_BLOCKED_HOSTNAMES = frozenset(
    {"localhost", "local", "internal", "broadcasthost"}
)
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DOTTED_QUAD_RE = re.compile(r"^(0|[1-9][0-9]{0,2})(?:\.(0|[1-9][0-9]{0,2})){3}$")



_IPV4_COMPATIBLE_NETWORK = ipaddress.ip_network("::/96")
_NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL_NETWORK = ipaddress.ip_network("64:ff9b:1::/48")


class InvalidURLValue(ValueError):
    """A URL is malformed or points at a non-public destination."""


def is_public_ip(host_or_ip) -> bool:
    """Return True only for a globally routable IP address.

    Accepts an :mod:`ipaddress` address or its textual form (optionally
    bracketed, optionally with a ``%zone`` suffix).  Private, loopback,
    link-local, multicast, reserved, unspecified, CGNAT, site-local,
    IPv4-mapped/compatible, NAT64-to-private, Teredo and 6to4-to-private
    addresses are all rejected.  Anything that is not a canonical IP literal
    is rejected; this function never resolves names.
    """
    address = _address_from_text(host_or_ip)
    if address is None:
        return False
    return _is_global_address(address)


def _address_from_text(value):
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return value
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]
    if not candidate:
        return None
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _is_global_address(address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return _is_global_address(mapped)
        if getattr(address, "is_site_local", False):
            return False
        if address in _NAT64_LOCAL_NETWORK:
            return False
        # Deprecated IPv4-compatible ::/96 and Teredo can be interpreted as
        # an IPv4 destination by some stacks; never needed for web browsing.
        if address in _IPV4_COMPATIBLE_NETWORK:
            return False
        if getattr(address, "teredo", None) is not None:
            return False
        if address in _NAT64_NETWORK:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
            return _is_global_address(embedded)
        sixtofour = getattr(address, "sixtofour", None)
        if sixtofour is not None:
            return _is_global_address(sixtofour)
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    return bool(address.is_global)


def canonical_ip_literal(host: str):
    """Return the canonical form of a host that is an IP literal, else None.

    ``host`` is the value from :attr:`urllib.parse.SplitResult.hostname`
    (``urlsplit`` strips the brackets of an IPv6 literal).  Raises
    :class:`InvalidURLValue` for non-canonical numeric spellings such as
    ``127.1`` or ``0x7f000001`` or for zoned IPv6 literals.
    """
    if not isinstance(host, str) or not host:
        return None
    if "%" in host:
        # ``[fe80::1%25eth0]`` and friends: scoped literals are never public.
        raise InvalidURLValue("Scoped IPv6 address literals are not allowed.")

    if ":" in host:
        try:
            return str(ipaddress.IPv6Address(host))
        except ValueError:
            raise InvalidURLValue("Malformed IPv6 address literal.") from None

    if host.isdigit():
        # Bare integer (e.g. 2130706433) and legacy octal spellings.
        raise InvalidURLValue("Non-canonical numeric hosts are not allowed.")

    if all(char in "0123456789." for char in host):
        if not _DOTTED_QUAD_RE.fullmatch(host):
            raise InvalidURLValue("Non-canonical numeric hosts are not allowed.")
        octets = host.split(".")
        try:
            address = ipaddress.IPv4Address(".".join(str(int(p)) for p in octets))
        except ValueError:
            raise InvalidURLValue("Invalid IPv4 address literal.") from None
        if any(str(int(part)) != part for part in octets):
            raise InvalidURLValue("Non-canonical numeric hosts are not allowed.")
        return str(address)

    if host.startswith("0x") or host.startswith("0X"):
        raise InvalidURLValue("Non-canonical numeric hosts are not allowed.")

    return None


def _normalized_hostname(host: str) -> str:
    """Lower-case and strip a trailing dot; reject unusable spellings."""
    if not isinstance(host, str) or not host:
        raise InvalidURLValue("URL must include a hostname.")
    if not host.isascii():
        # Chromium maps Unicode hosts through IDNA itself; a non-ASCII host
        # could therefore mean something other than what we validated.
        raise InvalidURLValue("Hostnames must be ASCII (punycode) only.")
    if any(char in host for char in " \t\r\n\\/@?#[]%"):
        raise InvalidURLValue("Invalid hostname.")
    hostname = host.casefold()
    if hostname.endswith("."):
        hostname = hostname[:-1]
    if hostname.endswith(".") or not hostname or len(hostname) > 253:
        raise InvalidURLValue("Invalid hostname.")
    return hostname


def is_blocked_hostname(host_or_ip: str) -> bool:
    """Return True when a URL host must never be opened.

    Never raises for syntactically odd input: anything unparseable is
    blocked.  Names are not resolved.
    """
    try:
        return _host_is_blocked(host_or_ip)
    except (InvalidURLValue, ValueError, TypeError):
        return True


def _host_is_blocked(value) -> bool:
    if not isinstance(value, str):
        return True
    literal = canonical_ip_literal(value)
    if literal is not None:
        return not is_public_ip(literal)

    hostname = _normalized_hostname(value)
    if hostname in _BLOCKED_HOSTNAMES:
        return True
    if hostname.endswith(_BLOCKED_HOST_SUFFIXES):
        return True
    if "." not in hostname:
        # Single-label names resolve through mDNS / the local search domain
        # and are indistinguishable from intranet hosts.
        return True
    labels = hostname.split(".")
    # The last label must look like a TLD, never a number: ``example.123``
    # is parsed by some resolvers as an integer IPv4 address.
    if not _LABEL_RE.fullmatch(labels[-1]) or labels[-1][0].isdigit():
        return True
    return any(not _LABEL_RE.fullmatch(label) for label in labels[:-1])


def validate_open_url(url: str) -> str:
    """Return a normalized public http(s) URL or raise ValueError.

    The returned URL has a fixed scheme spelling, a lower-case host and no
    fragment, so later comparisons against the browser's URL are stable.
    """
    if not isinstance(url, str):
        raise InvalidURLValue("URL must be text.")
    value = url.strip()
    if not value or len(value) > MAX_URL_LENGTH:
        raise InvalidURLValue(
            "URL must be non-empty and at most 2048 characters."
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise InvalidURLValue("URL must not contain control characters.")
    if any(char.isspace() for char in value):
        raise InvalidURLValue("URL must not contain whitespace.")
    if "\\" in value:
        raise InvalidURLValue("URL must not contain backslashes.")

    try:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
        username = parts.username
        password = parts.password
    except ValueError:
        raise InvalidURLValue("Malformed URL.") from None

    if scheme not in ALLOWED_SCHEMES:
        raise InvalidURLValue("Only http and https URLs can be opened.")
    if username is not None or password is not None:
        raise InvalidURLValue("URLs with embedded credentials are not allowed.")
    if not host:
        raise InvalidURLValue("URL must include a hostname.")
    if parts.netloc.endswith(":"):
        raise InvalidURLValue("Malformed URL.")
    if port is not None and not 0 < port < 65536:
        raise InvalidURLValue("Malformed URL.")

    netloc = parts.netloc
    literal = canonical_ip_literal(host)
    if literal is not None:
        if not is_public_ip(literal):
            raise InvalidURLValue("Local and private addresses cannot be opened.")
        netloc = f"[{literal}]" if ":" in literal else literal
        if port is not None:
            netloc += f":{port}"
    else:
        hostname = _normalized_hostname(host)
        if _host_is_blocked(hostname):
            raise InvalidURLValue("Local and private addresses cannot be opened.")
        netloc = hostname
        if port is not None:
            netloc += f":{port}"

    return f"{scheme}://{netloc}{parts.path or '/'}{_query_suffix(parts.query)}"


def _query_suffix(query: str) -> str:
    return f"?{query}" if query else ""


def build_site_search(site: str, query: str) -> str:
    """Build a search URL for one allowlisted site."""
    if not isinstance(site, str):
        raise ValueError("Site must be text.")
    site_key = site.strip().casefold()
    if site_key not in SITE_SEARCHES:
        raise ValueError(
            "Site must be one of: " + ", ".join(sorted(SITE_SEARCHES))
        )
    if not isinstance(query, str):
        raise ValueError("Query must be text.")
    query = query.strip()
    if not query or len(query) > MAX_QUERY_LENGTH:
        raise ValueError("Query must be non-empty and at most 200 characters.")
    if any(ord(char) < 32 or ord(char) == 127 for char in query):
        raise ValueError("Query must not contain control characters.")

    return SITE_SEARCHES[site_key].format(
        query=quote_plus(query, safe="")
    )
