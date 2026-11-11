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
    "bing": "https://www.bing.com/",
    "reddit": "https://www.reddit.com/",
    "amazon": "https://www.amazon.com/",
    "ebay": "https://www.ebay.com/",
    "twitch": "https://www.twitch.tv/",
    "spotify": "https://open.spotify.com/",
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
    "bing": "bing",
    "reddit": "reddit",
    "amazon": "amazon",
    "ebay": "ebay",
    "twitch": "twitch",
    "spotify": "spotify",
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
_IMPLICIT_SEARCH_RE = re.compile(
    r"\b(?:show me|give me|recommend|suggest|what are|which are|"
    r"any|i want|i'd like|looking for|pole[cć]|poka[zż])\b",
    re.IGNORECASE,
)
_SCREEN_SUMMARY_RE = re.compile(
    r"\b(?:summari[sz]e|explain|what(?:'s| is) (?:on|in)|"
    r"what am i seeing on)\b.{0,80}?"
    r"\b(left|right|center|centre|middle|primary|main)\s+"
    r"(?:screen|monitor|display)\b",
    re.IGNORECASE,
)
_PAGE_SUMMARY_RE = re.compile(
    r"\b(?:summari[sz]e|explain|what(?:'s| is) (?:on|in))\b.{0,50}?"
    r"\b(?:this|the)?\s*(?:page|website|site|tab)\b"
    r"|\bpage (?:i'?m|i am) (?:on|viewing|looking at)\b"
    r"|\bwhat am i looking at\b",
    re.IGNORECASE,
)
_VIDEO_INTENT_RE = re.compile(
    r"\b(?:find|show|recommend|suggest|get)\b.{0,50}?"
    r"\b(?:video|videos|clip|clips)\b"
    r"|\b(?:video|videos)\s+about\b",
    re.IGNORECASE,
)
_WATCH_INTENT_RE = re.compile(
    r"\b(?:something|anything)\s+(?:cool|fun|interesting|good)\s+to\s+watch"
    r"|\bwhat\s+should\s+i\s+watch\b"
    r"|\brecommend\s+(?:me\s+)?(?:a\s+)?(?:video|something)\b"
    r"|\bwhat(?:'s|\s+(?:is|are))\s+(?:popular|trending)\b"
    r"|\b(?:popular|trending)\s+videos?\b",
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
        r"^(?:me|mi|for me|dla mnie)(?:\s+|$)",
        "",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"^(?:a|an|some|any)\s+", "", query, flags=re.IGNORECASE
    )
    query = re.sub(r"\s+", " ", query)
    if not query or len(query) > 200:
        return None
    return query


def _search_query_for_site(text, site, aliases):
    alias_pattern = "|".join(re.escape(alias) for alias in aliases)
    about_match = re.search(
        rf"(?:{alias_pattern})\b\s+about\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if about_match:
        topic = _clean_query(about_match.group(1))
        pre_match = re.search(
            rf"(?:search|find|look up|look for|szukaj|wyszukaj|znajd[zź])"
            rf"(?:\s+me)?\s+(.+?)\s+(?:on|in|na|w)\s+"
            rf"(?:{alias_pattern})\b",
            text,
            flags=re.IGNORECASE,
        )
        base = _clean_query(pre_match.group(1)) if pre_match else ""
        if base and topic:
            return f"{base} about {topic}"
        if topic:
            return topic

    patterns = (
        rf"(?:search|find|look up|look for|szukaj|wyszukaj|znajd[zź])"
        rf"(?:\s+me)?(?:\s+(?:on|in|na|w))?\s+(?:{alias_pattern})\s+"
        rf"(?:for\s+|na\s+|o\s+)?(.+)",
        rf"(?:search|find|look up|look for|szukaj|wyszukaj|znajd[zź])"
        rf"\s+(.+?)\s+(?:on|in|na|w)\s+(?:{alias_pattern})\b",
        rf"^(?:{alias_pattern})\s+"
        rf"(?:search|szukaj|wyszukaj)\s+(.+)",
        rf"(?:show me|give me|recommend|suggest|what are|which are|"
        rf"any|i want|i'd like|looking for|pole[cć]|poka[zż])"
        rf"\s+(.+?)\s+(?:on|in|na|w)\s+(?:{alias_pattern})\b",
        rf"(?:show me|give me|recommend|suggest|i want|i'd like|"
        rf"looking for)\s+(?:{alias_pattern})\s+(.+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            query = _clean_query(match.group(1))
            if query:
                first = query.casefold().split(" ", 1)[0]
                if first in {"is", "are", "was", "were", "means"}:
                    continue
                return query

    # Fallback: keep everything after the named site, stripping separators
    # and filler such as "for" or a comma.
    for match in re.finditer(
        rf"(?:{alias_pattern})\b", text, flags=re.IGNORECASE
    ):
        tail = text[match.end():].lstrip(" ,:-")
        tail = re.sub(
            r"^(?:for|na|o)\s+", "", tail, flags=re.IGNORECASE
        )
        query = _clean_query(tail)
        if query:
            first = query.casefold().split(" ", 1)[0]
            if first in {"is", "are", "was", "were", "means"}:
                continue
            return query
    return None


def _generic_domain_search(text):
    patterns = (
        r"\b(?:search|find|look for|szukaj|wyszukaj)\b"
        r"(?:\s+(?:on|in|na|w))?\s+"
        r"((?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,})\s+"
        r"(?:for\s+|na\s+)?(.+)",
        r"\b(?:search|find|look for)\b\s+(.+?)\s+"
        r"(?:on|in|na|w)\s+"
        r"((?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,})\b",
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        if index == 0:
            domain, query = match.group(1), match.group(2)
        else:
            query, domain = match.group(1), match.group(2)
        query = _clean_query(query)
        if not query:
            continue
        try:
            validated = validate_open_url(
                domain if domain.lower().startswith(("http://", "https://"))
                else "https://" + domain
            )
        except ValueError:
            continue
        host = validated.split("://", 1)[-1].split("/", 1)[0]
        host = host.casefold().removeprefix("www.")
        if not host:
            continue
        return host, query
    return None


def _open_query_for_site(text, aliases):
    alias_pattern = "|".join(re.escape(alias) for alias in aliases)
    patterns = (
        rf"\b(?:open|launch|start|otw[oó]rz|uruchom)\s+(?:the\s+)?"
        rf"(?:{alias_pattern})\s+"
        rf"(?:with|for|and\s+search(?:\s+for)?|to\s+search(?:\s+for)?)\s+(.+)",
        rf"\b(?:open|launch|start|otw[oó]rz|uruchom)\s+(?:the\s+)?"
        rf"(?:{alias_pattern})\s+(?:and\s+)?"
        rf"(?:search|find|look\s+for)\s+(?:for\s+)?(.+)",
        rf"^(?:{alias_pattern})\s+(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        query = _clean_query(match.group(1))
        if not query:
            continue
        if query.casefold().split(" ", 1)[0] in {
            "is", "are", "was", "were", "has", "have", "can", "could",
            "will", "would",
        }:
            continue
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

    screen_summary = _SCREEN_SUMMARY_RE.search(text)
    if screen_summary:
        return "inspect_screen", {
            "mode": "screen",
            "screen_name": screen_summary.group(1).casefold(),
            "prompt": (
                "Summarize what is visible on this screen. Mention the main "
                "application, page, or dialog and the key readable content."
            ),
        }

    if _PAGE_SUMMARY_RE.search(text):
        return "inspect_screen", {
            "mode": "active_window",
            "prompt": (
                "Summarize the main readable content of this page in a few "
                "short paragraphs. Mention the page title and key points."
            ),
        }

    if re.search(
        r"\b(?:summari[sz]e|explain)\s+what\s+i'?m\s+seeing\b"
        r"|\bsummari[sz]e\s+(?:my|this)\s+screen\b",
        text,
        re.IGNORECASE,
    ):
        return "inspect_screen", {
            "mode": "screen",
            "screen_name": "primary",
            "prompt": (
                "Summarize what is visible on this screen. Mention the main "
                "application, page, or dialog and the key readable content."
            ),
        }

    site = _site_in(text)
    watch_intent = _WATCH_INTENT_RE.search(text)
    if watch_intent and site in (None, "youtube"):
        lowered = text.casefold()
        if "trend" in lowered:
            query = "trending videos"
        elif "popular" in lowered:
            query = "popular videos"
        else:
            query = "cool videos to watch"
        return "search_site", {"site": "youtube", "query": query}

    if site is None and _VIDEO_INTENT_RE.search(text):
        query = re.sub(
            r"^(?:please\s+|can you\s+|could you\s+)?"
            r"(?:find|show|recommend|suggest|get)\s+(?:me\s+)?",
            "",
            text.strip(),
            flags=re.IGNORECASE,
        )
        query = _clean_query(query)
        if query:
            return "search_site", {"site": "youtube", "query": query}

    aliases = [
        alias for alias, mapped in SITE_ALIASES.items() if mapped == site
    ] if site else []

    if site and _OPEN_RE.search(text):
        open_query = _open_query_for_site(text, aliases)
        if open_query:
            return "search_site", {"site": site, "query": open_query}

    if site and (
        _SEARCH_RE.search(text) or _IMPLICIT_SEARCH_RE.search(text)
    ):
        query = _search_query_for_site(text, site, aliases)
        if query:
            return "search_site", {"site": site, "query": query}

    if site:
        open_query = _open_query_for_site(text, aliases)
        if open_query:
            return "search_site", {"site": site, "query": open_query}

    if site and (_OPEN_RE.search(text) or _DOMAIN_RE.search(text)):
        return "open_url", {"url": SITE_HOME_URLS[site]}

    # A bare site name still means "open it" when it is the whole request.
    if site and text.casefold().strip(" .!?") in SITE_ALIASES:
        return "open_url", {"url": SITE_HOME_URLS[site]}

    if _SEARCH_RE.search(text):
        generic = _generic_domain_search(text)
        if generic is not None:
            host, query = generic
            return "search_site", {
                "site": "google",
                "query": f"site:{host} {query}",
            }
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
