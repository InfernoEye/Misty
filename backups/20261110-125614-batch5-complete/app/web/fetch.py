"""SSRF-resistant HTTP fetching and readable-text extraction for Misty.

This module is deliberately isolated from Misty's tools, planner, memory, and
UI.  It accepts one URL, validates it, fetches it with the standard library,
and returns plain text plus bounded links.  Raw HTML is not returned unless the
caller explicitly asks for it.

Security model
--------------
* Only ``http`` and ``https`` are accepted; credentials, fragments, and ports
  other than 80/443 are rejected.
* The hostname is resolved with :func:`socket.getaddrinfo` and *every* address
  must be global unicast.  Private, loopback, link-local, multicast, reserved,
  unspecified, CGNAT, and site-local addresses are refused.
* Redirects are never followed by urllib.  Each ``Location`` is validated and
  re-resolved before the next request, with a hard redirect limit.
* Downloads are streamed in bounded chunks and aborted once the byte budget is
  exceeded.  Content types are restricted to a small allowlist.
* No cookies, authentication headers, or page-content logging are used.

All page text is untrusted data.  It must never be interpreted as
instructions by Misty or by an LLM consuming this module's output.
"""

from __future__ import annotations

import codecs
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from typing import Collection

from bs4 import BeautifulSoup

__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINKS",
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_MAX_TEXT_CHARS",
    "DEFAULT_TIMEOUT",
    "USER_AGENT",
    "BlockedAddressError",
    "FetchResult",
    "FetchTimeoutError",
    "HTTPStatusError",
    "InvalidURLError",
    "NetworkError",
    "ResponseTooLargeError",
    "TooManyRedirectsError",
    "UnsupportedContentTypeError",
    "WebFetchError",
    "fetch",
    "validate_url",
]

DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_TEXT_CHARS = 100_000
DEFAULT_MAX_LINKS = 100
DEFAULT_MAX_REDIRECTS = 5

USER_AGENT = "Misty/0.1 (local desktop assistant; research fetch)"

ALLOWED_CONTENT_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain"}
)

_MAX_URL_LENGTH = 8192
_CHUNK_SIZE = 64 * 1024
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_PORTS = frozenset({80, 443})
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "pre",
        "blockquote",
        "table",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "figure",
        "figcaption",
        "main",
        "header",
        "footer",
        "nav",
        "aside",
        "form",
    }
)
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9._:-]+)""",
    re.IGNORECASE,
)


class WebFetchError(Exception):
    """Base class for every expected fetch failure."""


class InvalidURLError(WebFetchError):
    """The URL is malformed or uses a disallowed scheme/port/credentials."""


class BlockedAddressError(WebFetchError):
    """The URL or one of its DNS answers points at a non-public address."""


class UnsupportedContentTypeError(WebFetchError):
    """The response content type is outside the allowlist."""

    def __init__(self, content_type: str, url: str):
        self.content_type = content_type
        self.url = url
        super().__init__(
            f"Unsupported content type {content_type or 'missing'!r} for {url}"
        )


class ResponseTooLargeError(WebFetchError):
    """The response exceeded the byte budget and was aborted."""

    def __init__(self, limit: int, url: str):
        self.limit = limit
        self.url = url
        super().__init__(f"Response exceeded {limit} bytes for {url}")


class FetchTimeoutError(WebFetchError):
    """Connecting, reading, or resolving timed out."""


class NetworkError(WebFetchError):
    """A transport, TLS, or DNS error occurred."""


class HTTPStatusError(WebFetchError):
    """The server returned a non-success status."""

    def __init__(self, status: int, url: str):
        self.status = status
        self.url = url
        super().__init__(f"HTTP {status} for {url}")


class TooManyRedirectsError(WebFetchError):
    """The redirect chain exceeded the configured limit."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Plain-text result of a successful fetch.

    ``links`` contains dictionaries with ``url`` and ``text`` keys.  ``html``
    is ``None`` unless the caller opted in with ``include_html=True``.
    """

    final_url: str
    status: int
    content_type: str
    title: str
    text: str
    links: list[dict[str, str]]
    truncated: bool
    text_truncated: bool
    links_truncated: bool
    bytes_read: int
    charset: str
    html: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation of the result."""
        data: dict[str, object] = {
            "final_url": self.final_url,
            "status": self.status,
            "content_type": self.content_type,
            "title": self.title,
            "text": self.text,
            "links": [dict(link) for link in self.links],
            "truncated": self.truncated,
            "text_truncated": self.text_truncated,
            "links_truncated": self.links_truncated,
            "bytes_read": self.bytes_read,
            "charset": self.charset,
        }
        if self.html is not None:
            data["html"] = self.html
        return data


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Make urllib raise the 3xx response instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    # ProxyHandler({}) disables environment proxies: a proxy would tunnel the
    # request and bypass the resolved-address checks.  _NoRedirectHandler
    # replaces urllib's default redirect handler.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )


_OPENER = _build_opener()


def _build_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1"
            ),
        },
        method="GET",
    )


def _open_url(url: str, timeout: float):
    """Open one URL without following redirects.

    This is the single network seam used by :func:`fetch`; tests replace it.
    """
    return _OPENER.open(_build_request(url), timeout=timeout)


def validate_url(url: str) -> str:
    """Validate and normalize a URL without performing DNS resolution.

    Returns the URL with any fragment removed.  Raises :class:`InvalidURLError`
    for malformed URLs, non-http(s) schemes, credentials, or unusual ports, and
    :class:`BlockedAddressError` for non-public IP literals.
    """
    if not isinstance(url, str):
        raise InvalidURLError("URL must be a string")
    candidate = url.strip()
    if not candidate:
        raise InvalidURLError("URL is empty")
    if len(candidate) > _MAX_URL_LENGTH:
        raise InvalidURLError("URL is too long")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        raise InvalidURLError("URL contains control characters")
    if any(ch.isspace() for ch in candidate):
        raise InvalidURLError("URL contains whitespace")

    try:
        parts = urllib.parse.urlsplit(candidate)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
        username = parts.username
        password = parts.password
    except ValueError:
        raise InvalidURLError("Malformed URL") from None

    if scheme not in _ALLOWED_SCHEMES:
        raise InvalidURLError("Only http and https URLs are allowed")
    if username is not None or password is not None:
        raise InvalidURLError("URLs with embedded credentials are not allowed")
    if not host:
        raise InvalidURLError("URL must include a host")
    if port is not None and port not in _ALLOWED_PORTS:
        raise InvalidURLError("Only ports 80 and 443 are allowed")

    _validate_host_syntax(host)

    literal = _parse_ip_literal(host)
    if literal is not None and not _is_public_ip(literal):
        raise BlockedAddressError(
            f"Refusing non-public address {literal} in URL"
        )

    return urllib.parse.urlunsplit(
        (scheme, parts.netloc, parts.path, parts.query, "")
    )


def _validate_host_syntax(host: str) -> None:
    if not host or len(host) > 253:
        raise InvalidURLError("Invalid hostname")
    if any(ch in host for ch in " /\\?#@[]"):
        raise InvalidURLError("Invalid hostname")
    if _parse_ip_literal(host) is not None:
        return
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise InvalidURLError("Invalid hostname") from None
    if not ascii_host or len(ascii_host) > 253:
        raise InvalidURLError("Invalid hostname")


def _parse_ip_literal(host: str):
    candidate = host
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if "%" in candidate:
        return None
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _is_public_ip(address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return _is_public_ip(mapped)
        if getattr(address, "is_site_local", False):
            return False
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    # ``is_global`` catches additional special-purpose ranges (for example
    # CGNAT, benchmarking, and documentation networks) that the explicit
    # flags above may not cover on every Python version.
    return bool(address.is_global)


def _idna_host(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        raise InvalidURLError("Invalid hostname") from None


def _ensure_resolved_public(url: str) -> None:
    """Resolve the URL's host and reject if any answer is non-public."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise InvalidURLError("Malformed URL") from None
    if not host:
        raise InvalidURLError("URL must include a host")
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80

    literal = _parse_ip_literal(host)
    if literal is not None:
        _require_public_ip(literal)
        return

    ascii_host = _idna_host(host)
    try:
        infos = socket.getaddrinfo(
            ascii_host, port, type=socket.SOCK_STREAM
        )
    except (socket.gaierror, UnicodeError, OSError):
        raise NetworkError(f"Could not resolve host {host!r}") from None
    if not infos:
        raise NetworkError(f"Could not resolve host {host!r}")

    for info in infos:
        try:
            raw_address = info[4][0]
        except (IndexError, TypeError):
            raise BlockedAddressError(
                f"Refusing unparseable DNS answer for {host!r}"
            ) from None
        if not isinstance(raw_address, str):
            raise BlockedAddressError(
                f"Refusing unparseable DNS answer for {host!r}"
            )
        try:
            address = ipaddress.ip_address(raw_address.split("%", 1)[0])
        except ValueError:
            raise BlockedAddressError(
                f"Refusing unparseable DNS answer for {host!r}"
            ) from None
        _require_public_ip(address)


def _require_public_ip(address) -> None:
    if not _is_public_ip(address):
        raise BlockedAddressError(f"Refusing non-public address {address}")


def _get_header(headers, name: str) -> str | None:
    if headers is None:
        return None
    value = None
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
    except AttributeError:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("latin-1")
        except UnicodeError:
            return None
    return value if isinstance(value, str) else None


def _response_status(response) -> int:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        if callable(getcode):
            status = getcode()
    try:
        return int(status)
    except (TypeError, ValueError):
        raise NetworkError("Invalid HTTP response status") from None


def _parse_content_type(value: str | None) -> tuple[str, dict[str, str]]:
    if not value:
        return "", {}
    pieces = value.split(";")
    mime = pieces[0].strip().lower()
    params: dict[str, str] = {}
    for piece in pieces[1:]:
        if "=" not in piece:
            continue
        key, _, raw = piece.partition("=")
        key = key.strip().lower()
        if key:
            params[key] = raw.strip().strip("\"'")
    return mime, params


def _read_response(
    response, max_bytes: int, url: str
) -> tuple[bytes, int]:
    headers = getattr(response, "headers", None)
    declared = _get_header(headers, "Content-Length")
    if declared:
        try:
            if int(declared.strip()) > max_bytes:
                raise ResponseTooLargeError(max_bytes, url)
        except ValueError:
            pass

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(_CHUNK_SIZE)
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise NetworkError("Response body was not bytes")
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLargeError(max_bytes, url)
        chunks.append(bytes(chunk))
    return b"".join(chunks), total


def _sniff_meta_charset(body: bytes) -> str:
    match = _META_CHARSET_RE.search(body[:4096])
    if not match:
        return ""
    return match.group(1).decode("ascii", "ignore").strip().lower()


def _decode_body(
    body: bytes, declared_charset: str | None, content_type: str
) -> tuple[str, str]:
    if body.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return body.decode("utf-32", "replace"), "utf-32"
    if body.startswith(codecs.BOM_UTF8):
        return body.decode("utf-8-sig", "replace"), "utf-8-sig"
    if body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return body.decode("utf-16", "replace"), "utf-16"

    charset = (declared_charset or "").strip().strip("\"'").lower()
    if not charset and content_type in ("text/html", "application/xhtml+xml"):
        charset = _sniff_meta_charset(body)
    if not charset:
        charset = "utf-8"

    try:
        codecs.lookup(charset)
    except LookupError:
        charset = "utf-8"

    try:
        text = body.decode(charset, "replace")
    except (LookupError, UnicodeError):
        charset = "utf-8"
        text = body.decode("utf-8", "replace")

    if text.startswith("\ufeff"):
        text = text[1:]
    return text, charset


def _collapse(value: str, limit: int | None = None) -> str:
    collapsed = " ".join(value.split())
    if limit is not None and len(collapsed) > limit:
        collapsed = collapsed[:limit].rstrip()
    return collapsed


def _absolute_link(href: str, base_url: str) -> str | None:
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    try:
        joined = urllib.parse.urljoin(base_url, href)
        parts = urllib.parse.urlsplit(joined)
        scheme = parts.scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            return None
        if not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        # Force port parsing so malformed ports are rejected.
        _ = parts.port
    except ValueError:
        return None
    return urllib.parse.urlunsplit(
        (scheme, parts.netloc, parts.path, parts.query, "")
    )


def _extract_links(
    soup: BeautifulSoup, base_url: str, max_links: int
) -> tuple[list[dict[str, str]], bool]:
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    truncated = False
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        absolute = _absolute_link(href, base_url)
        if absolute is None or absolute in seen:
            continue
        if len(links) >= max_links:
            truncated = True
            break
        text = _collapse(anchor.get_text(" ", strip=True), 300)
        if not text:
            title = anchor.get("title")
            text = _collapse(title, 300) if isinstance(title, str) else ""
        seen.add(absolute)
        links.append({"url": absolute, "text": text})
    return links, truncated


def _normalize_readable(raw: str) -> str:
    lines: list[str] = []
    for line in raw.splitlines():
        collapsed = " ".join(line.split())
        if collapsed:
            lines.append(collapsed)
    return "\n".join(lines)


def _normalize_plain(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    previous_blank = False
    for line in text.split("\n"):
        stripped = line.rstrip()
        if stripped:
            lines.append(stripped)
            previous_blank = False
        elif not previous_blank:
            lines.append("")
            previous_blank = True
    return "\n".join(lines).strip()


def _extract_html(
    html_text: str,
    base_url: str,
    max_chars: int,
    max_links: int,
) -> tuple[str, str, list[dict[str, str]], bool, bool]:
    soup = BeautifulSoup(html_text, "html.parser")

    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()

    title = ""
    title_tag = soup.find("title")
    if title_tag is not None:
        title = _collapse(title_tag.get_text(" ", strip=True), 500)

    links, links_truncated = _extract_links(soup, base_url, max_links)

    root = soup.find("main")
    if root is None:
        root = soup.find("article")
    if root is None:
        root = soup.body
    if root is None:
        root = soup

    for tag in root.find_all(
        ["nav", "footer", "aside", "form", "svg", "iframe", "button"]
    ):
        tag.decompose()
    if root.name != "article":
        for tag in root.find_all("header"):
            tag.decompose()

    for tag in root.find_all(_BLOCK_TAGS):
        tag.insert_after("\n")

    text = _normalize_readable(root.get_text(""))
    text_truncated = len(text) > max_chars
    if text_truncated:
        text = text[:max_chars].rstrip()
    return title, text, links, text_truncated, links_truncated


def _validate_limits(
    timeout: float,
    max_bytes: int,
    max_chars: int,
    max_links: int,
    max_redirects: int,
) -> None:
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be a positive number")
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if not isinstance(max_links, int) or max_links < 0:
        raise ValueError("max_links must be a non-negative integer")
    if not isinstance(max_redirects, int) or max_redirects < 0:
        raise ValueError("max_redirects must be a non-negative integer")


def fetch(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_chars: int = DEFAULT_MAX_TEXT_CHARS,
    max_links: int = DEFAULT_MAX_LINKS,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    allowed_content_types: Collection[str] = ALLOWED_CONTENT_TYPES,
    include_html: bool = False,
) -> FetchResult:
    """Fetch one public HTTP(S) URL and return readable text.

    Parameters are intentionally small and explicit.  ``timeout`` applies to
    the socket (connection and each read).  ``max_bytes`` bounds the entire
    body; overflow aborts the request.  ``max_chars`` and ``max_links`` bound
    the returned data and set the corresponding truncation flags.

    Raises a :class:`WebFetchError` subclass for any expected failure.  The
    function never logs or returns response headers beyond the content type.
    """
    _validate_limits(timeout, max_bytes, max_chars, max_links, max_redirects)

    if isinstance(allowed_content_types, str):
        allowed = {allowed_content_types.strip().lower()}
    else:
        allowed = {
            str(item).split(";", 1)[0].strip().lower()
            for item in allowed_content_types
        }
    if not allowed:
        raise ValueError("allowed_content_types must not be empty")

    current_url = validate_url(url)
    redirects_followed = 0

    while True:
        _ensure_resolved_public(current_url)

        try:
            response = _open_url(current_url, timeout)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            location = _get_header(exc.headers, "Location")
            _safe_close(exc)
            if status in _REDIRECT_CODES and location:
                if redirects_followed >= max_redirects:
                    raise TooManyRedirectsError(
                        f"Redirect limit of {max_redirects} exceeded"
                    ) from None
                redirects_followed += 1
                current_url = validate_url(
                    urllib.parse.urljoin(current_url, location)
                )
                continue
            raise HTTPStatusError(status, current_url) from None
        except (TimeoutError, socket.timeout):
            raise FetchTimeoutError(
                f"Timed out fetching {current_url}"
            ) from None
        except urllib.error.URLError as exc:
            if isinstance(
                getattr(exc, "reason", None), (TimeoutError, socket.timeout)
            ):
                raise FetchTimeoutError(
                    f"Timed out fetching {current_url}"
                ) from None
            raise NetworkError(
                f"Could not fetch {current_url}"
            ) from None
        except OSError:
            raise NetworkError(f"Could not fetch {current_url}") from None

        try:
            with closing(response):
                status = _response_status(response)
                headers = getattr(response, "headers", None)

                if status in _REDIRECT_CODES:
                    location = _get_header(headers, "Location")
                    if not location:
                        raise HTTPStatusError(status, current_url)
                    if redirects_followed >= max_redirects:
                        raise TooManyRedirectsError(
                            f"Redirect limit of {max_redirects} exceeded"
                        )
                    redirects_followed += 1
                    current_url = validate_url(
                        urllib.parse.urljoin(current_url, location)
                    )
                    continue

                if not 200 <= status < 300:
                    raise HTTPStatusError(status, current_url)

                content_type, params = _parse_content_type(
                    _get_header(headers, "Content-Type")
                )
                if content_type not in allowed:
                    raise UnsupportedContentTypeError(
                        content_type, current_url
                    )

                body, bytes_read = _read_response(
                    response, max_bytes, current_url
                )
        except (TimeoutError, socket.timeout):
            raise FetchTimeoutError(
                f"Timed out fetching {current_url}"
            ) from None
        except urllib.error.URLError as exc:
            if isinstance(
                getattr(exc, "reason", None), (TimeoutError, socket.timeout)
            ):
                raise FetchTimeoutError(
                    f"Timed out fetching {current_url}"
                ) from None
            raise NetworkError(
                f"Could not fetch {current_url}"
            ) from None
        except OSError:
            raise NetworkError(f"Could not fetch {current_url}") from None

        decoded, charset = _decode_body(
            body, params.get("charset"), content_type
        )

        if content_type == "text/plain":
            title = ""
            text = _normalize_plain(decoded)
            text_truncated = len(text) > max_chars
            if text_truncated:
                text = text[:max_chars].rstrip()
            links: list[dict[str, str]] = []
            links_truncated = False
        else:
            (
                title,
                text,
                links,
                text_truncated,
                links_truncated,
            ) = _extract_html(decoded, current_url, max_chars, max_links)

        return FetchResult(
            final_url=current_url,
            status=status,
            content_type=content_type,
            title=title,
            text=text,
            links=links,
            truncated=text_truncated or links_truncated,
            text_truncated=text_truncated,
            links_truncated=links_truncated,
            bytes_read=bytes_read,
            charset=charset,
            html=decoded if include_html else None,
        )


def _safe_close(response) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
