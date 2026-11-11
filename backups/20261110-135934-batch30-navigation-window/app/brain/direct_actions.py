"""Deterministic routing for clear URL and site-search commands.

The local model remains the planner for open-ended tasks. These helpers only
recognize unambiguous navigation commands such as "open youtube.com" or
"search YouTube for X", so a small model cannot turn them into an application
launch or a generic web search.
"""

from __future__ import annotations

import re

from app.actions.web_launch import SITE_SEARCHES, validate_open_url


SITE_HOME_URLS = {
    "youtube": "https://www.youtube.com/",
    "google": "https://www.google.com/",
    "wikipedia": "https://en.wikipedia.org/",
    "github": "https://github.com/",
    "archwiki": "https://wiki.archlinux.org/",
    "stackoverflow": "https://stackoverflow.com/",
    "allegro": "https://allegro.pl/",
    "duckduckgo": "https://duckduckgo.com/",
}

SITE_ALIASES = {
    "youtube": "youtube",
    "you tube": "youtube",
    "yt": "youtube",
    "google": "google",
    "wikipedia": "wikipedia",
    "wiki": "wikipedia",
    "github": "github",
    "arch wiki": "archwiki",
    "archwiki": "archwiki",
    "stack overflow": "stackoverflow",
    "stackoverflow": "stackoverflow",
    "allegro": "allegro",
    "duckduckgo": "duckduckgo",
    "duck duck go": "duckduckgo",
}

_OPEN_RE = re.compile(
    r"\b(?:open|launch|start|go to|navigate to|visit|"
    r"otw[oó]rz|uruchom|wejd[zź]|przejd[zź])\b",
    re.IGNORECASE,
)
_SEARCH_RE = re.compile(
    r"\b(?:search|find|look up|look for|"
    r"szukaj|wyszukaj|znajd[zź])\b",
    re.IGNORECASE,
)
_DOMAIN_RE = re.compile(
    r"(?<![\w@])((?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}"
    r"(?::\d{1,5})?(?:/[^\s<>\"']*)?)",
    re.IGNORECASE,
)
_MAX_TEXT = 300


def _site_in(text):
    lowered = text.casefold()
    ordered = sorted(
        SITE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True
    )
    for alias, site in ordered:
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return site
    # Transcription may glue "open" to the site name ("OpenYouTube.com").
    for alias, site in ordered:
        if len(alias) >= 5 and alias in lowered:
            return site
    return None


def _clean_query(value):
    query = value.strip().strip(" \t\"'.,;:!?")
    query = re.sub(
        r"^(?:me|mi|for me|dla mnie)\s+", "", query, flags=re.IGNORECASE
    )
    query = re.sub(r"\s+", " ", query)
    if not query or len(query) > 200:
        return None
    return query


def _search_query_for_site(text, site, aliases):
    alias_pattern = "|".join(re.escape(alias) for alias in aliases)
    patterns = (
        rf"(?:search|find|look up|look for|szukaj|wyszukaj|znajd[zź])"
        rf"(?:\s+(?:on|in|na|w))?\s+(?:{alias_pattern})\s+"
        rf"(?:for\s+|na\s+|o\s+)?(.+)",
        rf"(?:search|find|look up|look for|szukaj|wyszukaj|znajd[zź])"
        rf"\s+(.+?)\s+(?:on|in|na|w)\s+(?:{alias_pattern})\b",
        rf"^(?:{alias_pattern})\s+"
        rf"(?:search|szukaj|wyszukaj)\s+(.+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            query = _clean_query(match.group(1))
            if query:
                return query
    return None


def resolve_direct_action(text):
    """Return ``(tool, arguments)`` for a clear navigation command, else None."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > _MAX_TEXT:
        return None
    if "\n" in text:
        return None

    site = _site_in(text)
    aliases = [
        alias for alias, mapped in SITE_ALIASES.items() if mapped == site
    ] if site else []

    if site and _SEARCH_RE.search(text):
        query = _search_query_for_site(text, site, aliases)
        if query:
            return "search_site", {"site": site, "query": query}

    if site and (_OPEN_RE.search(text) or _DOMAIN_RE.search(text)):
        return "open_url", {"url": SITE_HOME_URLS[site]}

    # A bare site name still means "open it" when it is the whole request.
    if site and text.casefold().strip(" .!?") in SITE_ALIASES:
        return "open_url", {"url": SITE_HOME_URLS[site]}

    if _SEARCH_RE.search(text):
        return None

    match = _DOMAIN_RE.search(text)
    if match:
        candidate = match.group(1)
        explicit = (
            candidate.lower().startswith(("http://", "https://"))
            or "www." in text.casefold()
            or bool(_OPEN_RE.search(text))
        )
        if not explicit:
            return None
        if not candidate.lower().startswith(("http://", "https://")):
            candidate = "https://" + candidate
        try:
            return "open_url", {"url": validate_open_url(candidate)}
        except ValueError:
            return None

    return None
