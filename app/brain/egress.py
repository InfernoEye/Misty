"""Outbound-data authorization for tools that can reach the network.

Some tools send data the user typed or selected to a remote party: a fetched
URL, a search query, an opened link, text typed into a live browser form.
Application-layer validation stops injected *instructions*, but it cannot tell
whether the payload is an ordinary public topic or a secret the user pasted.
This module is that second gate: a small, dependency-free classifier that
looks at the arguments of an egress tool and decides whether they appear to
carry private content.

Design rules
------------
* No I/O, no network, no GUI and no state outside one classifier object.  The
  module imports only the standard library.
* Content-free output.  Reasons are fixed codes and fixed sentences; no
  argument value, path, query or field text is ever copied into a reason,
  description, exception or log line.  ``EgressDecision.sanitized_arguments``
  is metadata only (see below) and never contains payload text.
* Self-contained classification.  Decisions depend only on the tool name, the
  mapping of top-level argument values and the classifier configuration.
  No global "private data" registry is consulted, because a process-wide
  value registry would itself retain secrets.
* Fail closed.  An egress tool without a classifier rule is ``uncertain``;
  values of a type the classifier does not understand are ``uncertain``.

Signals
-------
``private`` (approval required)
    * any value matches a user-supplied private marker (substring, casefold);
    * a value equals a string the caller marked private
      (``private_values``);
    * a value contains a local absolute or home path (``/home/jane/...``,
      ``~/notes``, ``/etc/...``);
    * a URL, ``site:`` search or ``key=value`` query carries a
      credential-like key: token, api_key, apikey, password, passwd, secret,
      session, auth, cookie, key, sig, signature (underscores, hyphens and
      dots are ignored, so ``API-KEY`` and ``apikey`` match);
    * a URL contains embedded userinfo (``https://user:pass@host/``);
    * a value looks like one opaque secret, or contains key material or a
      framework cookie serialisation (``long-hex-hex-hex`` RSS/CSRF tokens,
      ``-----BEGIN ... PRIVATE KEY-----``, ``S1F1R0xk...``);
    * text at or over the tool's length threshold (``search`` 180 characters,
      ``fill`` 180, ``url`` 400; ``browser_press`` 32).

``uncertain`` (approval required)
    * a value that is not a string, number or plain mapping/sequence
      (unknown objects are never inspected or coerced);
    * text that is not printable or is dominated by non-ASCII symbols
      (zero-width, private-use or control characters);

``public`` (no approval)
    * every check above passes.

Numbers are always public: a small counter such as ``pages`` or ``limit``
carries no private content.  Booleans and ``None`` are ignored.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
import re


__all__ = [
    "CONFIRMATION_PUBLIC",
    "EGRESS_TOOL_NAMES",
    "EgressDecision",
    "KIND_FILL",
    "KIND_SEARCH",
    "KIND_URL",
    "PRIVATE_CREDENTIAL_KEY",
    "PRIVATE_KEY_MATERIAL",
    "PRIVATE_MARKER_VALUES",
    "PRIVATE_OVER_LENGTH",
    "PRIVATE_PATH",
    "PRIVATE_SECRET_SHAPE",
    "PRIVATE_SIGNALS",
    "PRIVATE_USERINFO",
    "PrivateDataClassifier",
    "REASON_EGRESS_PRIVATE",
    "REASON_EGRESS_PUBLIC",
    "REASON_EGRESS_UNCERTAIN",
    "TOOL_ARGUMENT_RULES",
    "UNCERTAIN_TYPE",
    "UNCERTAIN_UNICODE",
    "checks_policy_consistency",
    "classify_egress",
    "confirmation_text",
    "SENSITIVE_EGRESS_TOOLS",
    "is_egress_tool",
    "is_sensitive_egress",
    "payload_arguments",
]


# -- Vocabulary -----------------------------------------------------------
#
# A "kind" is how a value leaves the machine, not how it is spelled:
#   search -- query text sent to a search engine / form
#   url    -- an address the user can read and that carries query data
#   fill   -- literal text typed into a page field
#
# ``address`` is an explicitly chosen URL (``browser_navigate``): it is still
# egress, but a plain navigation is ordinary browsing.  ``browser_press`` keys
# use the ``press`` kind.  Anything not listed stays local.

KIND_SEARCH = "search"
KIND_URL = "url"
KIND_FILL = "fill"
KIND_ADDRESS = "address"

_PRESS_KEY = "press"

# tool -> {argument_name: kind}
TOOL_ARGUMENT_RULES = {
    "fetch_web_page": {"url": KIND_URL},
    "web_search": {"query": KIND_SEARCH},
    "web_research": {"query": KIND_SEARCH},
    "open_url": {"url": KIND_URL},
    "search_site": {"query": KIND_SEARCH},
    "browser_navigate": {"url": KIND_ADDRESS},
    "browser_fill": {"text": KIND_FILL},
    "browser_press": {"key": _PRESS_KEY},
}

EGRESS_TOOL_NAMES = frozenset(TOOL_ARGUMENT_RULES)

# The subset whose *payload* can carry private data (everything except a plain
# navigation to an explicitly chosen address).
SENSITIVE_EGRESS_TOOLS = frozenset(
    name for name in EGRESS_TOOL_NAMES if name != "browser_navigate"
)

# Length at or above which an opaque text becomes too long to be a public
# topic.  A URL is mostly machine-readable and gets a higher bar.
_LENGTH_THRESHOLDS = {
    KIND_SEARCH: 180,
    KIND_FILL: 180,
    KIND_URL: 400,
    KIND_ADDRESS: 400,
    _PRESS_KEY: 32,
}

# -- Reason codes ---------------------------------------------------------

REASON_EGRESS_PRIVATE = "egress_private_payload"
REASON_EGRESS_UNCERTAIN = "egress_uncertain_payload"
REASON_EGRESS_PUBLIC = "egress_public"

# Content-free signal tokens recorded on a denial.  They name the rule that
# fired, never the value that fired it.
PRIVATE_MARKER_VALUES = "private_marker"
PRIVATE_CREDENTIAL_KEY = "credential_key"
PRIVATE_USERINFO = "embedded_credentials"
PRIVATE_PATH = "local_path"
PRIVATE_SECRET_SHAPE = "secret_shape"
PRIVATE_KEY_MATERIAL = "key_material"
PRIVATE_OVER_LENGTH = "over_length"
PRIVATE_SIGNALS = frozenset({
    PRIVATE_MARKER_VALUES,
    PRIVATE_CREDENTIAL_KEY,
    PRIVATE_USERINFO,
    PRIVATE_PATH,
    PRIVATE_SECRET_SHAPE,
    PRIVATE_KEY_MATERIAL,
    PRIVATE_OVER_LENGTH,
})

UNCERTAIN_TYPE = "unexpected_type"
UNCERTAIN_UNICODE = "unexpected_content"

# -- Fixed, content-free text ---------------------------------------------

CONFIRMATION_PUBLIC = "Send a public web request"

_TOOL_CONFIRMATION_TEXT = {
    "fetch_web_page": "Fetch a web address that may contain private data",
    "web_search": "Send a web search that may contain private data",
    "web_research": "Search and read web pages for a query that may contain "
                    "private data",
    "open_url": "Open a web address that may contain private data",
    "search_site": "Search a website with a query that may contain "
                   "private data",
    "browser_fill": "Type possibly private text into a webpage field",
    "browser_press": "Press a key that may send possibly private field text",
}

_DEFAULT_CONFIRMATION_TEXT = "Send data that may be private to the network"

# -- Patterns -------------------------------------------------------------

_CREDENTIAL_KEYS = frozenset({
    "token", "api_key", "apikey", "password", "passwd", "secret", "session",
    "auth", "cookie", "key", "sig", "signature",
})

# Key-value pairs in a URL query, a pasted URL or a search query.  The value
# group is bounded so a long phrase is not mistaken for a credential pair.
_KEY_VALUE = re.compile(
    r"(?<![A-Za-z0-9_.\-/])([A-Za-z][A-Za-z0-9_.\-]{0,63})=([^&\s?#]{1,512})"
)

_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\"'(\[,>])(/(?:home|root|Users|etc|var|tmp|opt|srv|mnt|media|"
    r"run|proc|sys|boot|dev|usr|bin|sbin|lib|lib64)/[^\s\"')\]<>]+)"
)
_SUMMARY_PATH = re.compile(
    r"(?:^|[\s\"'(\[,>])(?:/home/[^\s\"')\]<>]+|~(?:/|\b)[^\s\"')\]<>]*)"
)
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s\"'<>]+")
_USERINFO = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^/@\s]+@")

_OPAQUE_SECRET = re.compile(
    r"(?<![A-Za-z0-9+/_\-])"
    r"[A-Za-z0-9+/_\-]{24,}={0,2}"
    r"(?![A-Za-z0-9+/_\-])"
)
_SECRET_SHAPE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9+/]{6,}(?:[._\-][A-Za-z0-9+/]{6,}){2,}"
)
_KEY_MATERIAL = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_COOKIE_SERIALISATION = re.compile(r"S1F1R0xk[A-Za-z0-9+/=]*")


def _normalize_key(key):
    folded = str(key).strip().casefold().replace("-", "").replace(".", "")
    return folded


def _credential_key_in(text):
    """True when ``text`` contains ``key=value`` with a credential key."""
    for match in _KEY_VALUE.finditer(text):
        if _normalize_key(match.group(1)) in _CREDENTIAL_KEYS:
            return True
    return False


def _has_local_path(text):
    return bool(
        _ABSOLUTE_PATH.search(text)
        or _SUMMARY_PATH.search(text)
        or _WINDOWS_PATH.search(text)
    )


def _looks_like_secret(text):
    stripped = text.strip()
    if not stripped or len(stripped) < 16:
        return False
    if len(stripped.split()) > 1:
        words = stripped.split()
        # A single opaque blob alone; multi-word text is judged by the other
        # rules unless it is key material or a serialised cookie.
        if _KEY_MATERIAL.search(stripped) or _COOKIE_SERIALISATION.search(
            stripped
        ):
            return True
        return any(
            len(word) >= 32
            and _OPAQUE_SECRET.fullmatch(word) is not None
            and any(char.isdigit() for char in word)
            for word in words
        )
    if _KEY_MATERIAL.search(stripped) or _COOKIE_SERIALISATION.search(
        stripped
    ):
        return True
    if _SECRET_SHAPE.fullmatch(stripped) is not None:
        return True
    match = _OPAQUE_SECRET.fullmatch(stripped)
    if match is None:
        return False
    return any(char.isdigit() for char in stripped)


def _non_printable(text):
    for char in text:
        if not char.isprintable():
            return True
    return False


def _symbol_heavy(text):
    if not text:
        return False
    non_ascii = sum(1 for char in text if ord(char) > 127)
    return non_ascii > 0 and non_ascii * 2 > len(text)


def _kind_of(tool_name, argument_name):
    rules = TOOL_ARGUMENT_RULES.get(tool_name)
    if rules is None:
        return None
    return rules.get(argument_name)


def is_egress_tool(tool_name):
    """True for tools whose arguments can leave the machine."""
    return type(tool_name) is str and tool_name in EGRESS_TOOL_NAMES


def payload_arguments(tool_name):
    """Argument names that can leave the machine for ``tool_name``."""
    rules = TOOL_ARGUMENT_RULES.get(tool_name)
    if rules is None:
        return frozenset()
    return frozenset(
        name for name in rules if is_sensitive_egress(tool_name, name)
    )


def is_sensitive_egress(tool_name, argument_name=None):
    """True when this tool (and argument, if given) carries a payload.

    ``browser_navigate`` is an explicit address rather than a payload field,
    but its URL still leaves the machine (a query string is egress), so it is
    classified like every other address.  It is simply not in
    ``SENSITIVE_EGRESS_TOOLS``: a plain navigation is ordinary browsing.
    """
    if not is_egress_tool(tool_name):
        return False
    if argument_name is None:
        return tool_name in SENSITIVE_EGRESS_TOOLS
    return _kind_of(tool_name, argument_name) is not None


def _is_empty(value):
    return value is None or (type(value) is str and not value.strip())


@dataclass(frozen=True)
class EgressDecision:
    """One classification result, safe to log or show in a prompt."""

    allowed: bool
    requires_confirmation: bool
    reason_code: str
    tool: str
    signals: tuple = ()
    sanitized_arguments: Mapping = field(default_factory=dict)

    def __bool__(self):
        return self.allowed

    def to_dict(self):
        return {
            "allowed": self.allowed,
            "requires_confirmation": self.requires_confirmation,
            "reason_code": self.reason_code,
            "tool": self.tool,
            "signals": list(self.signals),
            "arguments": dict(self.sanitized_arguments),
        }


def _public(tool, sanitized):
    return EgressDecision(
        allowed=True,
        requires_confirmation=False,
        reason_code=REASON_EGRESS_PUBLIC,
        tool=tool,
        signals=(),
        sanitized_arguments=sanitized,
    )


def _deny(tool, reason_code, signals, sanitized):
    return EgressDecision(
        allowed=False,
        requires_confirmation=True,
        reason_code=reason_code,
        tool=tool,
        signals=tuple(signals),
        sanitized_arguments=sanitized,
    )


_PUBLIC_KINDS = frozenset({KIND_SEARCH, KIND_FILL, KIND_URL, KIND_ADDRESS,
                           _PRESS_KEY})


class PrivateDataClassifier:
    """Heuristic "does this argument carry private content?" classifier.

    ``private_markers`` are user-chosen substrings (for example file names,
    an email address, a project name) that must never be sent outbound.
    ``private_values`` are complete strings already known to be private.
    Both are held in memory, compared casefolded, and never echoed.
    """

    def __init__(self, private_markers=None, private_values=None):
        self._markers = tuple(
            marker.casefold().strip()
            for marker in (private_markers or ())
            if isinstance(marker, str) and marker.strip()
        )
        self._values = frozenset(
            value.casefold()
            for value in (private_values or ())
            if isinstance(value, str) and value.strip()
        )

    # -- value classification -------------------------------------------
    def marker_hits(self, text):
        """Return how many private markers occur in ``text`` (count only)."""
        folded = text.casefold()
        return sum(1 for marker in self._markers if marker in folded)

    def private_values_match(self, text):
        return bool(self._values) and text.strip().casefold() in self._values

    def classify_value(self, value, kind=KIND_SEARCH):
        """Classify one argument value.

        Returns ``(status, signal)`` where status is ``"public"``,
        ``"private"`` or ``"uncertain"`` and ``signal`` is a content-free
        token (``None`` when public).
        """
        if value is None or type(value) is bool:
            return "public", None
        if type(value) is int or type(value) is float:
            return "public", None
        if isinstance(value, Mapping):
            return self._classify_container(value.items(), kind)
        if isinstance(value, (list, tuple, set, frozenset)):
            return self._classify_container(
                ((None, item) for item in value), kind
            )
        if type(value) is not str:
            return "uncertain", UNCERTAIN_TYPE
        return self._classify_text(value, kind)

    def _classify_container(self, pairs, kind):
        uncertain = False
        for key, item in pairs:
            if key is not None:
                if type(key) is not str:
                    return "uncertain", UNCERTAIN_TYPE
                status, signal = self._classify_text(key, kind)
                if status == "private":
                    return status, signal
                if status == "uncertain":
                    uncertain = True
            status, signal = self.classify_value(item, kind)
            if status == "private":
                return status, signal
            if status == "uncertain":
                uncertain = True
        if uncertain:
            return "uncertain", UNCERTAIN_TYPE
        return "public", None

    def _is_strict_shape(self, value, depth=0):
        """Type-only check used for arguments that are not payload fields."""
        if value is None or type(value) in (bool, int, float, str):
            return True
        if depth > 3:
            return False
        if isinstance(value, Mapping):
            return all(
                type(key) is str and self._is_strict_shape(item, depth + 1)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple)):
            return all(self._is_strict_shape(item, depth + 1) for item in value)
        return False

    def _classify_text(self, text, kind):
        kind = kind if kind in _PUBLIC_KINDS else KIND_SEARCH
        if not text.strip():
            return "public", None
        if _non_printable(text) or _symbol_heavy(text):
            return "uncertain", UNCERTAIN_UNICODE
        if self.marker_hits(text) or self.private_values_match(text):
            return "private", PRIVATE_MARKER_VALUES
        if _KEY_MATERIAL.search(text) or _COOKIE_SERIALISATION.search(text):
            return "private", PRIVATE_KEY_MATERIAL
        if _credential_key_in(text):
            return "private", PRIVATE_CREDENTIAL_KEY
        if _USERINFO.search(text.strip()):
            return "private", PRIVATE_USERINFO
        if _has_local_path(text):
            return "private", PRIVATE_PATH
        if _looks_like_secret(text):
            return "private", PRIVATE_SECRET_SHAPE
        if len(text) >= _LENGTH_THRESHOLDS[kind]:
            return "private", PRIVATE_OVER_LENGTH
        return "public", None

    # -- tool classification --------------------------------------------
    def classify_tool(self, tool_name, arguments):
        """Classify one egress call.  Never raises for ordinary mappings."""
        if not is_egress_tool(tool_name):
            return _public(str(tool_name), {})
        if not isinstance(arguments, Mapping):
            return _deny(
                tool_name, REASON_EGRESS_UNCERTAIN, (UNCERTAIN_TYPE,),
                {"argument_count": 0},
            )

        rule_kinds = TOOL_ARGUMENT_RULES[tool_name]
        # Only ``payload_arguments(tool_name)`` can reach the network.  Other
        # keys (``pages``, ``limit``, ``timeout``) are strict-only: their
        # types must be plain, but their values are never treated as text.
        payload = payload_arguments(tool_name)
        sanitized = {"argument_count": len(arguments)}
        signals = []
        uncertain = False
        for name in sorted(arguments, key=repr):
            value = arguments[name]
            if _is_empty(value):
                continue
            if type(name) is not str:
                return _deny(
                    tool_name, REASON_EGRESS_UNCERTAIN, (UNCERTAIN_TYPE,),
                    sanitized,
                )
            if name not in payload:
                if not self._is_strict_shape(value):
                    return _deny(
                        tool_name, REASON_EGRESS_UNCERTAIN, (UNCERTAIN_TYPE,),
                        sanitized,
                    )
                continue
            kind = rule_kinds[name]
            status, signal = self.classify_value(value, kind)
            if status == "private":
                return _deny(
                    tool_name, REASON_EGRESS_PRIVATE, (signal,), sanitized
                )
            if status == "uncertain":
                uncertain = True
                if signal not in signals:
                    signals.append(signal)

        if uncertain:
            return _deny(
                tool_name, REASON_EGRESS_UNCERTAIN, signals, sanitized
            )
        return _public(tool_name, sanitized)


def _default_classifier():
    return _DEFAULT_CLASSIFIER


_DEFAULT_CLASSIFIER = PrivateDataClassifier()


def _install_default_policy_classifier():
    """Wire the default classifier into policy without policy importing us."""
    from app.brain import policy

    policy.install_egress_classifier(classify_egress)


def checks_policy_consistency():
    """Assert that policy's egress name set matches this module's rules.

    ``app.brain.policy`` deliberately imports nothing, so its
    ``EGRESS_TOOL_NAMES`` is a literal; this keeps the two from drifting.
    """
    from app.brain import policy

    return frozenset(policy.EGRESS_TOOL_NAMES) == EGRESS_TOOL_NAMES


def classify_egress(tool_name, arguments, classifier=None):
    """Classify one egress tool call with the default private classifier."""
    active = classifier if classifier is not None else _DEFAULT_CLASSIFIER
    return active.classify_tool(tool_name, arguments)


def confirmation_text(tool_name):
    """Fixed, content-free approval sentence for a gated egress call."""
    if type(tool_name) is not str:
        return _DEFAULT_CONFIRMATION_TEXT
    return _TOOL_CONFIRMATION_TEXT.get(tool_name, _DEFAULT_CONFIRMATION_TEXT)

_install_default_policy_classifier()