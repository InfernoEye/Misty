"""Isolated client for an optional self-hosted SearXNG JSON search service.

This module is deliberately independent of Misty's tools, planner, memory,
UI, configuration, and databases.  It speaks only to a SearXNG instance the
user runs on the loopback interface and returns bounded, JSON-serializable
data.

Security model
--------------
* The base URL must use ``http`` or ``https`` and must name exactly
  ``127.0.0.1``, ``localhost`` (case-insensitive), or ``::1`` (written with
  brackets, as required by URLs).  Any explicit port from 1 through 65535 is
  allowed because the user chooses where their own service listens.  Embedded
  credentials, non-root paths, queries, fragments, and control characters are
  rejected.  The stored base URL never has a trailing slash.
* Environment proxies are disabled with ``ProxyHandler({})`` so a proxy cannot
  redirect a loopback request elsewhere.  Redirects are not followed.  No
  cookies, authentication headers, or ambient credentials are sent.
* The response must be HTTP 200 and a JSON object.  It is streamed in bounded
  chunks and rejected as soon as either the declared ``Content-Length`` or the
  bytes actually read exceed one mebibyte.
* Search results are filtered for ``http``/``https`` URLs without credentials
  and bounded in title, snippet, and URL length.  Duplicate URLs are dropped.
  Answers are a bounded list of bounded strings.

All titles, snippets, URLs, and answers returned by this module are untrusted
data.  They must never be interpreted as instructions by Misty or by any model
consuming them.  This client never crawls or fetches a result page; the caller
must use the separate SSRF-safe fetcher (:mod:`app.web.fetch`) for that.

The client is read-only after construction and keeps no mutable shared state
beyond its configuration, so one instance may be used from multiple threads.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_TIMEOUT",
    "ERROR_HTTP_ERROR",
    "ERROR_INVALID_BASE_URL",
    "ERROR_INVALID_LIMIT",
    "ERROR_INVALID_QUERY",
    "ERROR_INVALID_RESPONSE",
    "ERROR_NETWORK_ERROR",
    "ERROR_RESPONSE_TOO_LARGE",
    "MAX_ANSWER_LENGTH",
    "MAX_ANSWERS",
    "MAX_LIMIT",
    "MAX_QUERY_LENGTH",
    "MAX_SNIPPET_LENGTH",
    "MAX_TITLE_LENGTH",
    "MAX_URL_LENGTH",
    "SEARCH_PATH",
    "InvalidBaseURLError",
    "SearxngClient",
    "validate_base_url",
]

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_RESULTS = 10
DEFAULT_MAX_BYTES = 1024 * 1024

SEARCH_PATH = "/search"
MAX_LIMIT = 20
MAX_QUERY_LENGTH = 300
MAX_TITLE_LENGTH = 300
MAX_SNIPPET_LENGTH = 1000
MAX_URL_LENGTH = 2048
MAX_ANSWERS = 5
MAX_ANSWER_LENGTH = 1000
_MAX_OPTIONAL_LENGTH = 64
_MAX_BASE_URL_LENGTH = 2048
_CHUNK_SIZE = 64 * 1024

ERROR_INVALID_BASE_URL = "invalid_base_url"
ERROR_INVALID_QUERY = "invalid_query"
ERROR_INVALID_LIMIT = "invalid_limit"
ERROR_NETWORK_ERROR = "network_error"
ERROR_HTTP_ERROR = "http_error"
ERROR_INVALID_RESPONSE = "invalid_response"
ERROR_RESPONSE_TOO_LARGE = "response_too_large"

_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_USER_AGENT = "Misty/0.1 (local desktop assistant; SearXNG JSON search)"
_CATEGORY_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_-]*(?:,[A-Za-z0-9][A-Za-z0-9_-]*)*"
)
_LANGUAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")


class InvalidBaseURLError(ValueError):
    """The SearXNG base URL is not an allowed loopback HTTP(S) URL."""

    error_code = ERROR_INVALID_BASE_URL


class _ValidationError(Exception):
    """Internal validation failure carrying a stable public error code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class _ResponseTooLarge(Exception):
    """Internal signal that the response exceeded the byte budget."""


class _InvalidResponse(Exception):
    """Internal signal that the response body or JSON shape was unusable."""


class _NetworkFailure(Exception):
    """Internal signal that reading the response failed at the transport."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects instead of following them to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _NoProxyHandler(urllib.request.ProxyHandler):
    """Explicit empty proxy policy that remains visible on the opener.

    On Python 3.14 an empty ``ProxyHandler`` exposes no ``*_open`` methods, so
    ``OpenerDirector.add_handler`` discards it.  Passing it still suppresses
    urllib's default environment-proxy handler, but the security control would
    be invisible to auditing.  These no-op methods keep the empty policy
    registered; they never apply a proxy.
    """

    def __init__(self):
        super().__init__({})

    def http_open(self, request):
        return None

    def https_open(self, request):
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    # The empty proxy handler disables environment proxies.  A proxy would
    # tunnel the request away from the validated loopback endpoint.
    return urllib.request.build_opener(
        _NoProxyHandler(),
        _NoRedirectHandler(),
    )


_OPENER = _build_opener()


def _build_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )


def _open_url(url: str, timeout: float):
    """Open one request through the isolated opener.

    This is the only network seam.  Tests replace ``_OPENER.open`` or this
    function; no test or production path resolves DNS for result pages.
    """
    return _OPENER.open(_build_request(url), timeout=timeout)


def _contains_control(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def validate_base_url(base_url: str) -> str:
    """Validate and normalize a loopback SearXNG base URL.

    Returns ``scheme://host[:port]`` without a trailing slash.  Raises
    :class:`InvalidBaseURLError` for anything outside the documented loopback
    policy.  No DNS lookup or network request is performed.
    """
    if not isinstance(base_url, str):
        raise InvalidBaseURLError("Base URL must be a string")
    candidate = base_url.strip()
    if not candidate:
        raise InvalidBaseURLError("Base URL is empty")
    if len(candidate) > _MAX_BASE_URL_LENGTH:
        raise InvalidBaseURLError("Base URL is too long")
    if _contains_control(candidate) or any(ch.isspace() for ch in candidate):
        raise InvalidBaseURLError("Base URL contains whitespace or controls")
    # A bare '?' or '#' has an empty query/fragment but must still be rejected.
    if "?" in candidate or "#" in candidate:
        raise InvalidBaseURLError("Base URL must not contain a query or fragment")

    try:
        parts = urllib.parse.urlsplit(candidate)
    except ValueError:
        raise InvalidBaseURLError("Base URL is malformed") from None

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise InvalidBaseURLError("Only http and https base URLs are allowed")
    if parts.path not in ("", "/"):
        raise InvalidBaseURLError("Base URL must not contain a path")
    if parts.username is not None or parts.password is not None:
        raise InvalidBaseURLError("Base URL must not contain credentials")
    if "@" in parts.netloc:
        raise InvalidBaseURLError("Base URL must not contain credentials")
    if parts.netloc.endswith(":"):
        raise InvalidBaseURLError("Base URL has an empty port")

    host = parts.hostname
    if not host or host.lower() not in _ALLOWED_HOSTS:
        raise InvalidBaseURLError("Only loopback hosts are allowed")

    try:
        port = parts.port
    except ValueError:
        raise InvalidBaseURLError("Base URL has an invalid port") from None
    if port is not None and not 1 <= port <= 65535:
        raise InvalidBaseURLError("Base URL port is outside 1-65535")

    normalized_host = host.lower()
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    if port is not None:
        normalized_host = f"{normalized_host}:{port}"
    return f"{scheme}://{normalized_host}"


def _validate_query(query: str) -> str:
    if not isinstance(query, str):
        raise _ValidationError(
            ERROR_INVALID_QUERY, "Query must be a string"
        )
    normalized = query.strip()
    if not normalized:
        raise _ValidationError(ERROR_INVALID_QUERY, "Query must not be empty")
    if len(normalized) > MAX_QUERY_LENGTH:
        raise _ValidationError(
            ERROR_INVALID_QUERY,
            f"Query must be at most {MAX_QUERY_LENGTH} characters",
        )
    if "\x00" in normalized:
        raise _ValidationError(
            ERROR_INVALID_QUERY, "Query must not contain NUL"
        )
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        raise _ValidationError(
            ERROR_INVALID_QUERY, "Query contains invalid Unicode"
        ) from None
    return normalized


def _validate_limit(limit, max_results: int) -> int:
    if limit is None:
        return min(max_results, 10)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_LIMIT
    ):
        raise _ValidationError(
            ERROR_INVALID_LIMIT,
            f"Limit must be an integer between 1 and {MAX_LIMIT}",
        )
    return limit


def _validate_optional_token(value, label: str, pattern: re.Pattern) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _ValidationError(
            ERROR_INVALID_QUERY, f"{label} must be a string"
        )
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > _MAX_OPTIONAL_LENGTH or not pattern.fullmatch(
        normalized
    ):
        raise _ValidationError(
            ERROR_INVALID_QUERY,
            f"{label} contains unsupported characters or is too long",
        )
    return normalized


def _failure(code: str, message: str) -> dict[str, object]:
    return {"success": False, "message": message, "error": code}


def _get_header(headers, name: str) -> str | None:
    if headers is None:
        return None
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
    if isinstance(value, int):
        return str(value)
    return value if isinstance(value, str) else None


def _safe_close(response) -> None:
    close = getattr(response, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        pass


def _coerce_status(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _response_status(response) -> int:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        if not callable(getcode):
            raise _InvalidResponse
        try:
            status = getcode()
        except Exception:
            raise _InvalidResponse from None
    status_int = _coerce_status(status)
    if status_int is None:
        raise _InvalidResponse
    return status_int


def _read_body(response, max_bytes: int) -> bytes:
    declared = _get_header(getattr(response, "headers", None), "Content-Length")
    if declared:
        declared_length = _coerce_status(declared.strip())
        if declared_length is not None and declared_length > max_bytes:
            raise _ResponseTooLarge

    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = response.read(_CHUNK_SIZE)
        except _ResponseTooLarge:
            raise
        except (TimeoutError, socket.timeout, OSError, http.client.HTTPException):
            raise _NetworkFailure from None
        except Exception:
            raise _InvalidResponse from None
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise _InvalidResponse
        total += len(chunk)
        if total > max_bytes:
            raise _ResponseTooLarge
        chunks.append(bytes(chunk))
    return b"".join(chunks)


def _bounded_text(value, limit: int) -> str | None:
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    return value.strip()[:limit]


def _validate_result_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        return None
    if _contains_control(candidate) or any(ch.isspace() for ch in candidate):
        return None
    try:
        parts = urllib.parse.urlsplit(candidate)
        scheme = parts.scheme.lower()
        host = parts.hostname
        username = parts.username
        password = parts.password
        port = parts.port  # Force validation of a malformed/out-of-range port.
    except ValueError:
        return None
    if scheme not in _ALLOWED_SCHEMES or not host:
        return None
    if username is not None or password is not None or "@" in parts.netloc:
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    return candidate


def _parse_result(item) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    url = _validate_result_url(item.get("url"))
    if url is None:
        return None

    if "title" not in item or not isinstance(item.get("title"), str):
        return None
    if "content" not in item and "snippet" not in item:
        return None
    content = item.get("content")
    snippet_value = item.get("snippet")
    if content is not None and not isinstance(content, str):
        return None
    if snippet_value is not None and not isinstance(snippet_value, str):
        return None
    if content is not None and content.strip():
        raw_snippet = content
    elif snippet_value is not None and snippet_value.strip():
        raw_snippet = snippet_value
    else:
        raw_snippet = ""
    title = _bounded_text(item.get("title"), MAX_TITLE_LENGTH)
    snippet = _bounded_text(raw_snippet, MAX_SNIPPET_LENGTH)
    if title is None or snippet is None:
        return None
    return {"title": title, "url": url, "snippet": snippet}


def _parse_results(raw_results: list, limit: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in raw_results:
        if len(results) >= limit:
            break
        parsed = _parse_result(item)
        if parsed is None:
            continue
        url = parsed["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        results.append(parsed)
    return results


def _parse_answers(raw_answers) -> list[str]:
    if not isinstance(raw_answers, list):
        return []
    answers: list[str] = []
    for value in raw_answers:
        if len(answers) >= MAX_ANSWERS:
            break
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text:
            continue
        answers.append(text[:MAX_ANSWER_LENGTH])
    return answers


def _validate_timeout(timeout) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be a positive finite number")
    value = float(timeout)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a positive finite number")
    return value


def _validate_max_results(max_results) -> int:
    if (
        isinstance(max_results, bool)
        or not isinstance(max_results, int)
        or not 1 <= max_results <= MAX_LIMIT
    ):
        raise ValueError(
            f"max_results must be an integer between 1 and {MAX_LIMIT}"
        )
    return max_results


class SearxngClient:
    """Read-only client for a local SearXNG JSON search endpoint.

    ``base_url`` must point at ``127.0.0.1``, ``localhost``, or ``::1`` using
    HTTP(S); any explicit port from 1 to 65535 is accepted.  ``timeout`` is a
    positive number of seconds.  ``max_results`` is the default result cap
    (further capped at 10) and must be between 1 and 20.

    After construction the instance is immutable from this module's point of
    view.  :meth:`search` uses only local state, so concurrent calls are safe
    as long as callers do not reassign the configuration attributes.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_results: int = DEFAULT_MAX_RESULTS,
    ):
        self.base_url = validate_base_url(base_url)
        self.timeout = _validate_timeout(timeout)
        self.max_results = _validate_max_results(max_results)

    def search(
        self,
        query: str,
        limit: int | None = None,
        categories: str | None = None,
        language: str | None = None,
    ) -> dict[str, object]:
        """Search the local SearXNG instance and return bounded result data.

        The returned titles, snippets, URLs, and answers are untrusted data and
        must never be treated as instructions.  This method does not fetch any
        result page; callers that need page text must use the SSRF-safe fetcher.

        On success the mapping contains ``success``, ``query``, ``results``,
        ``answers``, ``count``, ``limit``, and ``message``.  On failure it
        contains only ``success``, ``message``, and a stable ``error`` code.
        No exception text, query text, or result text is included in failures.
        """
        try:
            base_url = validate_base_url(getattr(self, "base_url", None))
            validated_query = _validate_query(query)
            validated_limit = _validate_limit(limit, self.max_results)
            validated_categories = _validate_optional_token(
                categories, "Categories", _CATEGORY_RE
            )
            validated_language = _validate_optional_token(
                language, "Language", _LANGUAGE_RE
            )
        except _ValidationError as exc:
            return _failure(exc.code, exc.message)
        except Exception:
            return _failure(ERROR_INVALID_BASE_URL, "Invalid SearXNG base URL")

        params = {
            "q": validated_query,
            "format": "json",
            "safesearch": "1",
        }
        if validated_categories is not None:
            params["categories"] = validated_categories
        if validated_language is not None:
            params["language"] = validated_language

        try:
            request_url = (
                f"{base_url}{SEARCH_PATH}?{urllib.parse.urlencode(params)}"
            )
        except Exception:
            return _failure(ERROR_INVALID_QUERY, "Could not encode the query")

        try:
            response = _open_url(request_url, self.timeout)
        except urllib.error.HTTPError as exc:
            status = _coerce_status(getattr(exc, "code", None))
            _safe_close(exc)
            message = (
                f"Search service returned HTTP {status}"
                if status is not None
                else "Search service returned an HTTP error"
            )
            return _failure(ERROR_HTTP_ERROR, message)
        except Exception:
            return _failure(
                ERROR_NETWORK_ERROR, "Search service could not be reached"
            )

        try:
            status = _response_status(response)
            if status != 200:
                return _failure(
                    ERROR_HTTP_ERROR,
                    f"Search service returned HTTP {status}",
                )
            body = _read_body(response, DEFAULT_MAX_BYTES)
        except _ResponseTooLarge:
            return _failure(
                ERROR_RESPONSE_TOO_LARGE,
                "Search response exceeded the size limit",
            )
        except _NetworkFailure:
            return _failure(
                ERROR_NETWORK_ERROR, "Search service connection failed"
            )
        except _InvalidResponse:
            return _failure(
                ERROR_INVALID_RESPONSE,
                "Search service returned an invalid response",
            )
        except Exception:
            return _failure(
                ERROR_NETWORK_ERROR, "Search service connection failed"
            )
        finally:
            _safe_close(response)

        try:
            payload = json.loads(body)
        except Exception:
            return _failure(
                ERROR_INVALID_RESPONSE, "Search service returned invalid JSON"
            )
        if not isinstance(payload, dict):
            return _failure(
                ERROR_INVALID_RESPONSE,
                "Search response was not a JSON object",
            )

        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            return _failure(
                ERROR_INVALID_RESPONSE,
                "Search response did not contain a results list",
            )

        try:
            results = _parse_results(raw_results, validated_limit)
            answers = _parse_answers(payload.get("answers"))
        except Exception:
            return _failure(
                ERROR_INVALID_RESPONSE,
                "Search response contained unusable result data",
            )

        return {
            "success": True,
            "query": validated_query,
            "results": results,
            "answers": answers,
            "count": len(results),
            "limit": validated_limit,
            "message": "ok",
        }
