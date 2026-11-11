"""Validated web navigation helpers for opening pages and site searches.

The model chooses a site key and query, or supplies one public URL. This
module builds a canonical URL without shell text, never scrapes, and rejects
local/private literal hosts for direct opens.
"""

from __future__ import annotations

import ipaddress
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


def _blocked_hostname(hostname: str) -> bool:
    host = hostname.strip().strip(".").casefold()
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


def validate_open_url(url: str) -> str:
    """Return a normalized public http(s) URL or raise ValueError."""
    if not isinstance(url, str):
        raise ValueError("URL must be text.")
    url = url.strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise ValueError("URL must be non-empty and at most 2048 characters.")
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("URL must not contain control characters.")

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs can be opened.")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URLs with embedded credentials are not allowed.")
    if not parts.hostname:
        raise ValueError("URL must include a hostname.")
    if _blocked_hostname(parts.hostname):
        raise ValueError("Local and private addresses cannot be opened.")
    return url


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
