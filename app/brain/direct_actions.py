"""Deterministic routing for clear URL and site-search commands.

The local model remains the planner for open-ended tasks. These helpers only
recognize unambiguous navigation commands such as "open youtube.com" or
"search YouTube for X", so a small model cannot turn them into an application
launch or a generic web search.

The direct path runs before the planner and before intent confidence, so it
is fail-closed. :func:`resolve_direct_action` returns ``None`` (letting the
planner answer, decline, or ask) whenever:

* the action verb is negated ("don't open Google", "nie otwieraj Google");
* the request carries a second independent instruction ("open Google and
  list the files in Downloads", "open example.com; then summarize it");
* the request asks for a summary of a URL ("summarize https://..."), which
  needs the page fetched and read, not merely opened.

Topical words are preserved verbatim, including Unicode ("日本語") and
articles that are part of a name ("the band The The"); :func:`_clean_query`
only strips leading request boilerplate and trailing generic words.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    r"\b(?:find|show|recommend|suggest|get|what are|which are)\b.{0,50}?"
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
_MAX_TEXT = 300

# A path may contain a quoted apostrophe, a comma, or sentence punctuation
# ("/it's", "/a,b", "/article?"), so only whitespace and angle brackets end
# the URL. Trailing punctuation is trimmed separately when a URL is opened.
_DOMAIN_RE = re.compile(
    r"(?<![\w@])((?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}"
    r"(?::\d{1,5})?(?:/[^\s<>\"']*)?)",
    re.IGNORECASE,
)
_DOMAIN_AT_RE = re.compile(
    r"(?<![\w@])(?:(?:https?://)(?:[a-z0-9-]+\.)+[a-z]{2,}"
    r"(?::\d{1,5})?(?:/[^\s<>\"']*)?"
    r"|(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d{1,5})?(?:/[^\s<>\"']*)?)",
    re.IGNORECASE,
)
# Domain-only form used to rebuild a search parameter; it must not also match
# a dotted sentence end ("I like it. Tell me more").
_DOMAIN_AT_STRICT_RE = re.compile(
    r"(?<![\w@])(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d{1,5})?",
    re.IGNORECASE,
)

# Typographic apostrophes and backticks are folded to "'" so "don’t",
# "dont" and "don't" all match the negation and filler patterns.
_APOSTROPHE_TABLE = str.maketrans({"\u2019": "'", "\u02bc": "'", "`": "'"})
_LEADING_FILLER_RE = re.compile(
    r"^(?:(?:please|could you|can you|would you|will you|kindly)\s+)+",
    re.IGNORECASE,
)


# --- URL masking -----------------------------------------------------------


@dataclass(frozen=True)
class _MaskedText:
    """A case-folded request with every URL/domain replaced by spaces.

    ``folded`` keeps the URLs so a site name glued to "open"
    ("OpenYouTube.com") can still be recognised. Words are never read from
    ``lowered``: a URL may contain "and" or "open", so the mask only decides
    *where* a match may occur; matched text is taken from the original
    request at the same offsets.
    """

    folded: str = ""
    folded_spans: tuple = ()
    lowered: str = ""
    spans: tuple = ()

    @classmethod
    def from_request(cls, text):
        folded = text.casefold().translate(_APOSTROPHE_TABLE)
        spans = tuple(
            match.span(1) for match in _DOMAIN_RE.finditer(folded)
        )
        characters = list(folded)
        for start, end in spans:
            characters[start:end] = " " * (end - start)
        return cls(folded, spans, "".join(characters), spans)

    def url_clause_remainder(self):
        """Return the sentence before the first URL, URL blanked out.

        "open https://example.com and tell me ..." keeps "open" before the
        blank, so any *other* word left there is a second instruction. A
        sentence end before the URL ("Can you open it. Summarize
        https://...") starts a fresh sentence.
        """
        if not self.spans:
            return self.lowered
        start = self.spans[0][0]
        boundary = re.search(r"[.!?][\s,;:]*(\S*)$", self.lowered[:start])
        if boundary is not None:
            return boundary.group(1)
        return self.lowered[:start]


# --- Negation guard --------------------------------------------------------

# Verbs a direct action would perform on the user's behalf. A negation scoped
# to one of these means "do not do it", never "do the opposite".
_ACTION_VERBS = (
    r"open|opening|launch|launching|start|starting|go\s+to|"
    r"navigate(?:\s+to)?|visit|search|searching|find|finding|"
    r"look\s+up|look\s+for|show|google|googling|youtube|"
    r"otw[oó]rz|otwieraj|otwierac|otwierać|uruchamiaj|uruchom|"
    r"uruchamiac|uruchamiać|szukaj|szukac|szukać|wyszukaj|wyszukuj|"
    r"znajd[zź]|id[zź]|idz|wchod[zź]|wejd[zź]|wejdz|w[łl][aą]cz"
)

_NEGATION_PATTERNS = (
    # English negated imperative: "don't open", "do not launch",
    # "never search", "avoid opening", "stop trying to", "without opening".
    re.compile(
        r"\b(?:do\s+not|don'?t|never|avoid|refrain\s+from|"
        r"stop(?:\s+trying\s+to)?|without)\s+(?:ever\s+|you\s+)?"
        rf"(?:{_ACTION_VERBS})\b"
    ),
    # "Can you not open Google?", "could you please not launch it"
    re.compile(
        r"\b(?:can|could|would|will)\s+you(?:\s+please)?\s+not\s+"
        rf"(?:{_ACTION_VERBS})\b"
    ),
    # "I don't want you to open Google", "don't forget to search"
    re.compile(
        r"\bdon'?t\s+(?:want|need)\s+(?:you|me|it)\s+to\s+"
        rf"(?:{_ACTION_VERBS})\b"
    ),
    # "please, no opening Google"
    re.compile(r"\bno\s+(?:opening|launching|searching|googling|finding)\b"),
    # Polish negated imperative: "proszę nie otwieraj", "nie idź do",
    # "nie wchodź na", "nie uruchamiaj", "nie szukaj".
    re.compile(
        r"\bnie\s+(?:\w+\s+){0,2}?"
        r"(?:otwieraj|otworz|otwierac|otwierać|uruchamiaj|uruchom|"
        r"uruchamiac|uruchamiać|szukaj|szukac|szukać|wyszukaj|wyszukuj|"
        r"znajd[zź]|id[zź]|idz|wchod[zź]|wejd[zź]|wejdz|w[łl][aą]cz)\b"
    ),
)


def _has_action_negation(text):
    lowered = text.casefold().translate(_APOSTROPHE_TABLE)
    return any(pattern.search(lowered) for pattern in _NEGATION_PATTERNS)


# --- Multi-intent guard ----------------------------------------------------

# Applied to the URL-masked request: a conjunction that introduces a
# different instruction verb means the sentence asks for more than opening.
_SECOND_CLAUSE_RE = re.compile(
    # Explicit clause separators.
    r";"
    r"|\bthen\b"
    r"|(?:^|\s)potem\s"
    r"|(?:^|\s)nast[eę]pnie\s"
    # Polish "i" / "oraz" + an instruction verb ("Otwórz Google i wypisz ...").
    r"|(?:^|\s)(?:i|oraz)\s+(?:wypisz|poka[zż]|otw[oó]rz|uruchom|"
    r"przenie[sś]|skopiuj|usu[nń]|zapisz|znajd[zź]|szukaj|wyszukaj|"
    r"utw[oó]rz|dodaj|wy[lł]ij|wyslij|przypomnij|przeczytaj|sprawd[zź]|"
    r"podsumuj|streszcz\w*|opowiedz|napisz|ustaw|w[ył][lł][aą]cz)\b"
    # A conjunction that introduces a different instruction verb.
    r"|\band\s+(?:(?:please|also|then)\s+)?"
    r"(?:list|show|search|find|move|copy|rename|send|email|e-mail|delete|"
    r"remove|download|upload|create|make|write|save|tell|summari[sz]e|"
    r"sum\s+up|explain|read|open|launch|visit|check|translate|print|"
    r"book|schedule|remind|zip|unzip|clear|set|turn|record|index|grep|"
    r"scan|play|pause|stop|close|quit|go\s+to|navigate|google|youtube|"
    r"pokaz|pokaż|wypisz|znajd[zź]|wyslij|wyślij|otworz|otwórz|uruchom|"
    r"stworz|stwórz|zapisz|usun|usuń|przenies|przenieś|skopiuj|dodaj|"
    r"przypomnij|sprawdz|sprawdź|przeczytaj)\b",
    re.IGNORECASE,
)

# Words allowed to remain in a URL clause; they carry no extra instruction.
_OPENING_VERBS = frozenset({
    "please", "could", "can", "would", "will", "you", "kindly", "just",
    "hey", "misty", "and", "the", "me", "url", "link", "this", "that",
    "open", "opens", "launch", "start", "visit", "go", "navigate", "to",
    "at", "on", "in", "otworz", "otwórz", "uruchom", "weidz", "wejdź",
    "przejdz", "przejdź", "w", "na", "prosze", "proszę", "of", "page",
    "browser", "przegladarce", "przeglądarke", "przeglądce", "naszej",
    "my", "default", "web", "swiat", "świata",
})

# A URL that follows one of these was mentioned as an object, not as the
# whole command ("open example.com", "is example.com up?").
_OBJECT_URL_CUES = re.compile(
    r"\b(?:summari[sz]e|summarise|sum\s+up|podsumuj|streszcz|stre[sś][cć]|"
    r"fetch|read|explain|describe|compare|translate|tell|check|is|are|"
    r"was|were|does|do|did|what|how|why|when|where|who|about|of|from|"
    r"with|using|w|z|ze|czy|co|jak|na|o)\b",
    re.IGNORECASE,
)


def _has_second_instruction(masked):
    return bool(_SECOND_CLAUSE_RE.search(masked.lowered))


def _url_clause_has_other_instruction(masked):
    """True when the clause holding a URL asks for more than opening it."""
    remainder = masked.url_clause_remainder()
    if _SECOND_CLAUSE_RE.search(remainder):
        return True
    if _OBJECT_URL_CUES.search(remainder):
        return True
    return any(
        word not in _OPENING_VERBS
        for word in re.findall(r"\w+", remainder.strip(" :,-"))
    )


# --- Query cleaning --------------------------------------------------------

# Removed from the end of a video request. Articles are intentionally absent:
# "the band The The" and "The National" carry meaning.
_TRAILING_VIDEO_FILLER = frozenset({
    "video", "videos", "clip", "clips", "about", "on", "of", "for", "in",
    "to", "watch", "watching", "please", "youtube", "yt",
})
# Removed from the start of a video request: request boilerplate and generic
# qualifiers. "a"/"an"/"the" are removed only before another word, so a name
# such as "The The" keeps its first article.
_LEADING_VIDEO_FILLER = frozenset({
    # request verbs and their polite wrappers
    "find", "show", "recommend", "suggest", "give", "get", "search", "look",
    "watch", "want", "looking", "for", "like", "could", "would", "can",
    "please", "me", "us", "you", "i", "i'd", "im", "there", "to", "on",
    "in", "should",
    # question words
    "what", "what's", "whats", "which", "is", "are", "was", "were", "how",
    "why", "when", "where", "who",
    # generic qualifiers that are not a topic
    "some", "any", "something", "anything", "cool", "fun", "funny",
    "interesting", "good", "nice", "great", "best", "popular", "trending",
    "latest", "new", "recent", "random", "videos", "video", "clips", "clip",
    "a", "an",
})
_LEADING_ARTICLES = frozenset({"a", "an", "the"})
# Words that describe the category of a watch request, not its topic.
_GENERIC_WATCH_WORDS = frozenset({
    "something", "anything", "cool", "fun", "funny", "interesting", "good",
    "nice", "great", "best", "popular", "trending", "latest", "new",
    "recent", "random", "video", "videos", "clip", "clips", "watch", "to",
    "a", "an", "the", "me", "please",
})
_LEADING_PARTICLES = frozenset({"about", "on", "of", "regarding", "o"})
_GENERIC_CATEGORY_ARTICLES = frozenset({"a", "an", "the", "to", "of", "on"})


def _tokens(value):
    """Unicode word tokens; keeps CJK and Cyrillic topics intact."""
    return re.findall(r"\w[\w'+-]*", value.casefold(), flags=re.UNICODE)


def _clean_query(value):
    query = value.strip().strip(" \t\"'.,;:!?")
    query = _LEADING_FILLER_RE.sub("", query)
    query = re.sub(
        r"^(?:me|mi|for me|dla mnie)(?:\s+|$)",
        "",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"^(?:a|an|some|any)\s+", "", query, flags=re.IGNORECASE
    )
    query = re.sub(r"\s+", " ", query).strip()
    if not query or len(query) > 200:
        return None
    return query


def _video_search_query(text):
    """Build a YouTube query without deleting interior topic words.

    Only request boilerplate ("find me", "show me", "what are", "some",
    "cool") and generic qualifiers are removed. Every interior word -
    including the articles in "the band The The" - is kept verbatim, and
    non-ASCII topics such as "日本語" survive tokenization because the split
    uses ``\\w`` with ``re.UNICODE``.
    """
    # "... on YouTube" at the end names the site, not the topic; a site named
    # mid-sentence ("a video on YouTube about Minecraft") is left alone.
    tokens = _tokens(_strip_trailing_site_phrase(text))

    # One left-to-right pass for the request boilerplate. Two things stop it:
    # the last token (a lone topic), and a word repeated back to back, which
    # is a name ("The The"), not boilerplate.
    start = 0
    while start < len(tokens) - 1 and (
        tokens[start] in _LEADING_VIDEO_FILLER
        or tokens[start] in _LEADING_ARTICLES
        or tokens[start] in _LEADING_PARTICLES
    ):
        if tokens[start] == tokens[start + 1]:
            break
        start += 1
    tokens = tokens[start:]
    # Trailing generic words ("... videos", "... to watch") carry no topic.
    while tokens and tokens[-1] in _TRAILING_VIDEO_FILLER:
        tokens.pop()
    if not tokens:
        # No topic: keep the generic category the user named, never a canned
        # query that hides their words.
        return (
            "trending videos"
            if re.search(r"\btrend", text, re.IGNORECASE)
            else "popular videos"
        )
    # "something cool to watch" is a category, not a topic: with nothing but
    # generic words left, the category word is the query.
    if not [
        token
        for token in tokens
        if token not in _GENERIC_WATCH_WORDS
        and token not in _GENERIC_CATEGORY_ARTICLES
    ] and len(tokens) < 2:
        return (
            "trending videos"
            if re.search(r"\btrend", text, re.IGNORECASE)
            else "popular videos"
        )
    # "video about <topic>" already carries the head noun.
    if "video" in tokens or "videos" in tokens:
        return " ".join(tokens[:8])
    # Otherwise keep it so YouTube ranks video results ("minecraft video").
    return " ".join(tokens[:8]) + " video"


# --- Summary requests ------------------------------------------------------

# "Summarize <url>" is not a navigation request. No dedicated direct action
# exists for a URL summary: inspect_screen reads the screen (not a URL), and
# fetch_web_page / web_research / browser_read are planner tools. Such
# requests therefore return None so the planner fetches and reads the page;
# returning open_url would silently drop the summary. "Summarize this page"
# without a URL keeps the existing inspect_screen window-summary behaviour.
_URL_SUMMARY_RE = re.compile(
    r"^\W*(?:(?:please|could you|can you|would you|hey misty|misty)[\s,]+)*"
    r"(?:summari[sz]e|summari[sz]ing|summarise|sum\s+up|"
    r"what(?:'s|\s+is)\s+(?:on|in)|explain|describe|read|tell\s+me\s+about)"
    r"\b",
    re.IGNORECASE,
)
_URL_SUMMARY_PL_RE = re.compile(
    r"^\W*(?:(?:prosz[eę]|hej|misty)[\s,]+)*"
    r"(?:podsumuj|streszcz\w*|stre[sś][cć]\w*|co\s+jest\s+na|opisz|"
    r"przeczytaj)\b",
    re.IGNORECASE,
)


def _is_url_summary_request(text):
    if not _DOMAIN_AT_STRICT_RE.search(text):
        return False
    return bool(_URL_SUMMARY_RE.match(text) or _URL_SUMMARY_PL_RE.match(text))


_WHOLE_SCREEN_SUMMARY_RE = re.compile(
    r"\b(?:summari[sz]e|explain)\s+what\s+i'?m\s+seeing\b"
    r"|\bsummari[sz]e\s+(?:my|this)\s+screen\b",
    re.IGNORECASE,
)


def _screen_summary(mode, screen_name):
    """inspect_screen action for a desktop or active-window summary."""
    if mode == "screen":
        return "inspect_screen", {
            "mode": "screen",
            "screen_name": screen_name,
            "prompt": (
                "Summarize what is visible on this screen. Mention the main "
                "application, page, or dialog and the key readable content."
            ),
        }
    return "inspect_screen", {
        "mode": "active_window",
        "prompt": (
            "Summarize the main readable content of this page in a few "
            "short paragraphs. Mention the page title and key points."
        ),
    }


# --- Conversational replies ------------------------------------------------

_CONVERSATIONAL_REPLIES = (
    (
        re.compile(
            r"^(?:hi|hello|hey|yo|hej|czesc|cześć|dzien dobry|"
            r"dzień dobry|dobry wieczor|dobry wieczór)"
            r"[\s!.,?]*$",
            re.IGNORECASE,
        ),
        "Hello! How can I help?",
    ),
    (
        re.compile(
            r"^(?:thanks|thank you|thx|dzieki|dzięki|dziekuje|dziękuję)"
            r"[\s!.,?]*$",
            re.IGNORECASE,
        ),
        "You're welcome!",
    ),
    (
        re.compile(
            r"^(?:how are you|how's it going|jak sie masz|jak się masz)"
            r"[\s!?,]*$",
            re.IGNORECASE,
        ),
        "I'm running locally and ready. What can I do?",
    ),
)


def resolve_conversational_reply(text):
    """Answer simple greetings/thanks without starting the model or tools."""
    value = str(text or "").strip()
    for pattern, reply in _CONVERSATIONAL_REPLIES:
        if pattern.fullmatch(value):
            return reply
    return None


# --- Site helpers ----------------------------------------------------------


_ALIASES_LONGEST_FIRST = sorted(
    SITE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True
)
def _site_in(masked):
    """Return the site alias named by the request, or None.

    Matching uses the original casing so a transcription that glued the verb
    to the site name ("OpenYouTube.com") still counts, exactly as before.
    """
    folded = masked.folded
    for alias, site in _ALIASES_LONGEST_FIRST:
        alias_pattern = r"\s*".join(
            re.escape(part) for part in alias.split()
        )
        if re.search(rf"\b{alias_pattern}\b", folded):
            return site
    return None


_SITE_HOSTS = {
    "youtube": ("youtube.com", "youtu.be", "www.youtube.com"),
    "google": ("google.com", "www.google.com"),
    "wikipedia": ("wikipedia.org", "en.wikipedia.org", "pl.wikipedia.org"),
    "github": ("github.com", "www.github.com"),
    "archwiki": ("wiki.archlinux.org",),
    "stackoverflow": ("stackoverflow.com", "www.stackoverflow.com"),
    "allegro": ("allegro.pl", "www.allegro.pl"),
    "duckduckgo": ("duckduckgo.com",),
    "bing": ("bing.com", "www.bing.com"),
    "reddit": ("reddit.com", "www.reddit.com"),
    "amazon": ("amazon.com", "www.amazon.com"),
    "ebay": ("ebay.com", "www.ebay.com"),
    "twitch": ("twitch.tv", "www.twitch.tv"),
    "spotify": ("open.spotify.com", "spotify.com"),
}


def _strip_trailing_site_phrase(text):
    """Drop a trailing "on YouTube" / "from YouTube" phrase and what follows.

    "Find Minecraft videos on YouTube" -> "Find Minecraft videos"; a site
    named mid-sentence ("a video on YouTube about Minecraft") is untouched
    because the phrase is not at the end.
    """
    stripped = re.sub(r"[\s.!,?]+$", "", text)
    match = re.search(
        r"\s+(?:on|from|using|at|in|na|w)\s+(?:youtube|yt)$",
        stripped,
        flags=re.IGNORECASE,
    )
    return stripped[:match.start()] if match else text


def _search_query_for_site(text, masked, site, aliases):
    alias_pattern = "|".join(re.escape(alias) for alias in aliases)
    about_match = re.search(
        rf"(?:{alias_pattern})\b\s+about\s+(.+)$",
        masked.lowered,
        flags=re.IGNORECASE,
    )
    if about_match:
        topic = _clean_query(_parameter(text, masked, about_match.span(1)))
        pre_match = re.search(
            rf"(?:search|find|look up|look for|szukaj|wyszukaj|znajd[zź])"
            rf"(?:\s+me)?\s+(.+?)\s+(?:on|in|na|w)\s+"
            rf"(?:{alias_pattern})\b",
            masked.lowered,
            flags=re.IGNORECASE,
        )
        base = (
            _clean_query(_parameter(text, masked, pre_match.span(1)))
            if pre_match else None
        )
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
        match = re.search(pattern, masked.lowered, flags=re.IGNORECASE)
        if match:
            query = _clean_query(_parameter(text, masked, match.span(1)))
            if query:
                first = query.casefold().split(" ", 1)[0]
                if first in {"is", "are", "was", "were", "means"}:
                    continue
                return query

    # Fallback: keep everything after the named site, stripping separators
    # and filler such as "for" or a comma.
    for match in re.finditer(
        rf"(?:{alias_pattern})\b", masked.lowered, flags=re.IGNORECASE
    ):
        tail = masked.lowered[match.end():].lstrip(" ,:-")
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


def _parameter(text, masked, span):
    """Return the original wording of a matched action parameter.

    ``masked.lowered`` is space-for-character identical to the folded
    original (each URL replaced by blanks), so a span found in it addresses
    the same characters in ``text``. That keeps the user's casing and
    Unicode while making it impossible for a pattern to match inside a URL.
    """
    return text[span[0]:span[1]]


def _generic_domain_search(text, masked):
    """Return ``(host, query)`` for "search <topic> on <domain>", else None."""
    patterns = (
        r"\b(?:search|find|look for|szukaj|wyszukaj)\b"
        r"(?:\s+(?:on|in|na|w))?\s+"
        r"((?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,})\s+"
        r"(?:for\s+|na\s+)?(.+)",
        r"\b(?:search|find|look for)\b\s+(.+?)\s+"
        r"(?:on|in|na|w)\s+"
        r"((?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,})\b",
    )
    # The domain must be readable, so this one matches the request itself
    # (the caller only offers it when the request names no other URL).
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        if index == 0:
            domain, query = match.group(1), match.group(2)
        else:
            query, domain = match.group(1), match.group(2)
        query = _clean_query(query)
        if not query or _DOMAIN_AT_RE.search(query):
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


def _open_query_for_site(text, masked, aliases):
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
        match = re.search(pattern, masked.lowered, flags=re.IGNORECASE)
        if not match:
            continue
        query = _clean_query(_parameter(text, masked, match.span(1)))
        if not query:
            continue
        if query.casefold().split(" ", 1)[0] in {
            "is", "are", "was", "were", "has", "have", "can", "could",
            "will", "would",
        }:
            continue
        return query
    return None


# --- Entry point -----------------------------------------------------------


def resolve_direct_action(text):
    """Return ``(tool, arguments)`` for a clear navigation command, else None.

    Only a single, unambiguous command is resolved here. A negated action, a
    sentence with a second instruction, and a request for a summary of a URL
    all return ``None`` so the planner receives the whole request and nothing
    the user asked for is silently dropped.
    """
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > _MAX_TEXT:
        return None
    if "\n" in text:
        return None

    # 1. Negation scoped to the action verb blocks every direct action,
    #    including summaries ("don't summarize ...").
    if _has_action_negation(text):
        return None

    masked = _MaskedText.from_request(text)

    # 2. A second independent instruction would be silently dropped.
    if _has_second_instruction(masked):
        return None

    # 3. Read-only summaries of the visible desktop stay direct actions. A
    #    page/website/site/tab summary keeps the existing window behaviour
    #    only when no URL is named ("summarize this page" vs "<url>").
    screen_summary = _SCREEN_SUMMARY_RE.search(text)
    if screen_summary:
        return _screen_summary("screen", screen_summary.group(1).casefold())
    if _WHOLE_SCREEN_SUMMARY_RE.search(text):
        return _screen_summary("screen", "primary")
    if _PAGE_SUMMARY_RE.search(text) and not masked.spans:
        return _screen_summary("active_window", None)

    # 4. "Summarize https://..." needs the page fetched and read; only the
    #    planner can do that, and a bare open_url would drop the summary.
    if _is_url_summary_request(text):
        return None

    site = _site_in(masked)
    aliases = [
        alias for alias, mapped in SITE_ALIASES.items() if mapped == site
    ] if site else []

    # 5. Video and watch requests become YouTube searches.
    if _WATCH_INTENT_RE.search(text) and site in (None, "youtube"):
        return "search_site", {
            "site": "youtube",
            "query": _video_search_query(text),
        }
    if site is None and _VIDEO_INTENT_RE.search(text):
        return "search_site", {
            "site": "youtube",
            "query": _video_search_query(text),
        }
    # "Show me something to watch": a watch request with no topic at all.
    if site is None and re.search(
        r"\b(?:something|anything)\s+to\s+(?:watch|see)\b"
        r"|\bwhat\s+should\s+i\s+watch\b",
        text,
        re.IGNORECASE,
    ):
        return "search_site", {
            "site": "youtube",
            "query": _video_search_query(text),
        }

    # 6. A search on a named site wins over opening it ("search X on Google").
    if site and (
        _SEARCH_RE.search(masked.lowered)
        or _IMPLICIT_SEARCH_RE.search(masked.lowered)
    ):
        query = _search_query_for_site(text, masked, site, aliases)
        if query:
            if site == "youtube" and _VIDEO_INTENT_RE.search(text):
                query = _video_search_query(query)
            return "search_site", {"site": site, "query": query}

    # 7. "Search <topic> on example.com" scopes a search to that domain: it
    #    names a search target, not a page to open, so it is decided before
    #    the URL rules below.
    scoped_search = (
        _generic_domain_search(text, masked)
        if _SEARCH_RE.search(text)
        else None
    )
    if scoped_search is not None:
        host, query = scoped_search
        return "search_site", {
            "site": "google",
            "query": f"site:{host} {query}",
        }

    # 8. A URL that names a site home opens it; any other URL is a page the
    #    user typed and is opened as written. This is decided before the
    #    site-name rules below, which would otherwise read "wikipedia" inside
    #    "en.wikipedia.org/wiki/X" as a bare site mention.
    home = _known_site_home(masked)
    if home is not None:
        return "open_url", {"url": SITE_HOME_URLS[home]}
    if masked.spans:
        return _resolve_url_open(text, masked)

    # 9. "open <site> with/for <topic>" and "Google <topic>." are searches.
    if site:
        open_query = _open_query_for_site(text, masked, aliases)
        if open_query:
            return "search_site", {"site": site, "query": open_query}

    # 10. A bare site name means "open it" only when it is the whole request.
    if site and text.casefold().strip(" .!?") in SITE_ALIASES:
        return "open_url", {"url": SITE_HOME_URLS[site]}

    if site and _OPEN_RE.search(masked.lowered):
        return "open_url", {"url": SITE_HOME_URLS[site]}


    return None


def _known_site_home(masked):
    """Site key whose *home* URL the request names, or None.

    Only a URL without a path ("youtube.com", "openYouTube.com") maps to the
    site table; "youtube.com/results?..." or "en.wikipedia.org/wiki/X" are
    specific pages and are opened exactly as written. A glued transcription
    ("openyoutube.com") is the same site, not a URL the user typed.
    """
    for start, end in masked.folded_spans:
        raw = re.sub(r"^https?://", "", masked.folded[start:end])
        host, _, path = raw.partition("/")
        host = host.removeprefix("www.")
        glued = host.startswith("open") and "." in host
        bare_host = host.removeprefix("open") if glued else host

        if path:
            continue
        for site, hosts in _SITE_HOSTS.items():
            if bare_host in {item.removeprefix("www.") for item in hosts}:
                return site
        if glued:
            for alias, site in _ALIASES_LONGEST_FIRST:
                if len(alias) >= 5 and bare_host.split(".", 1)[0] == alias:
                    return site
    return None


def _resolve_url_open(text, masked):
    """Open the request's public URL, or None to leave it to the planner."""
    if _url_clause_has_other_instruction(masked):
        return None

    candidate = text[masked.spans[0][0]:masked.spans[0][1]].rstrip(".,;:!?")
    # A scheme, "www.", an opening verb or a summary verb makes the URL an
    # explicit target; a bare host inside prose ("what is example.com") is
    # not a command and goes to the planner.
    explicit = (
        candidate.lower().startswith(("http://", "https://"))
        or "www." in text.casefold()
        or bool(_OPEN_RE.search(masked.lowered))
        or bool(_URL_SUMMARY_RE.match(text) or _URL_SUMMARY_PL_RE.match(text))
    )
    if not explicit:
        return None
    if not candidate.lower().startswith(("http://", "https://")):
        candidate = "https://" + candidate
    try:
        url = validate_open_url(candidate)
    except ValueError:
        return None
    # A bare host opens as "https://example.com", not ".../": the site's own
    # redirect decides the trailing slash.
    host_part = candidate.split("://", 1)[1]
    if not re.search(r"[/?#]", host_part):
        url = url.rstrip("/")
    return "open_url", {"url": url}
