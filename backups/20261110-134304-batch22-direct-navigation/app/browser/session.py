"""Isolated, persistent Playwright browser session for Misty.

The service owns exactly one daemon worker thread.  That thread is the only
thread that creates, touches, or destroys Playwright objects; callers submit
validated command descriptions and receive JSON-safe result dictionaries.  No
Playwright object, page text, or raw exception ever crosses the thread
boundary.

Design rules
------------
* Lazy launch.  Chromium starts on the first ``navigate`` and is reused for
  later commands.  The context is fresh: no persistent profile, no storage
  state, no cookies from the user's browser, no extensions.
* One page.  ``navigate``, ``read``, ``click``, ``fill`` and ``press`` work on
  the same page so a small workflow can be built without losing state.
* Read-only extraction and direct interaction only.  There is no JavaScript
  evaluation API, no download API, no upload API, and no shell access.
* Bounded output.  URLs, titles, selectors, text, link counts and queue waits
  all have hard caps.
* Fail closed.  Invalid input is rejected before the worker starts.  An
  unexpected worker failure shuts the browser down and returns ``closed`` or
  ``internal_error``; a later command may lazily start a new browser.
* All page text is untrusted data.  It is returned to the caller but never
  interpreted, logged, or echoed in an error message.

Public API
----------
``BrowserSession(queue_timeout=30.0, backend_factory=None)``

``navigate(url, timeout=15.0)``
``read(max_chars=20000, max_links=100)``
``click(selector, timeout=10.0)``
``fill(selector, text, timeout=10.0)``
``press(key)``
``close()`` / ``shutdown()``

Every result has the shape::

    {
        "success": bool,
        "action": str,
        "code": str,
        "message": str,
        "data": dict,
    }

Success uses ``code == "ok"``.  Failure codes are ``invalid_url``,
``invalid_selector``, ``invalid_text``, ``invalid_key``, ``not_started``,
``timeout``, ``navigation_failed``, ``action_failed``, ``closed`` and
``internal_error``.
"""

from __future__ import annotations

import math
import os
import queue
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from app.actions.web_launch import MAX_URL_LENGTH, validate_open_url

__all__ = ["BrowserSession"]


# Set the browser bundle path before Playwright is imported.  Playwright is
# imported lazily inside the worker so validation-only callers never load it.
_DEFAULT_BROWSERS_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "playwright-browsers"
)
if (
    not os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    and _DEFAULT_BROWSERS_PATH.is_dir()
):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_DEFAULT_BROWSERS_PATH)


DEFAULT_QUEUE_TIMEOUT = 30.0
DEFAULT_NAVIGATE_TIMEOUT = 15.0
DEFAULT_ACTION_TIMEOUT = 10.0
DEFAULT_MAX_CHARS = 20_000
DEFAULT_MAX_LINKS = 100

MAX_SELECTOR_CHARS = 300
MAX_FILL_CHARS = 2_000
MAX_READ_CHARS = 100_000
MAX_READ_LINKS = 500
MAX_TITLE_CHARS = 300
MAX_PREVIEW_CHARS = 500
MAX_LINK_TEXT_CHARS = 200
MAX_LINK_SCAN = 500
MAX_DATA_ITEMS = 1_000
MAX_DATA_DEPTH = 5
MAX_MESSAGE_CHARS = 300

VIEWPORT_WIDTH = 1_440
VIEWPORT_HEIGHT = 900
LOCALE = "en-US"
SETTLE_MS = 250
READ_TIMEOUT_MS = 5_000
LINK_ITEM_TIMEOUT_MS = 1_000
DEFAULT_PAGE_TIMEOUT_MS = 10_000
DEFAULT_NAVIGATION_TIMEOUT_MS = 15_000
WORKER_JOIN_SECONDS = 2.0

SUCCESS_CODE = "ok"
ERROR_CODES = frozenset(
    {
        "invalid_url",
        "invalid_selector",
        "invalid_text",
        "invalid_key",
        "not_started",
        "timeout",
        "navigation_failed",
        "action_failed",
        "closed",
        "internal_error",
    }
)
ALLOWED_KEYS = frozenset(
    {
        "Enter",
        "Tab",
        "Escape",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Backspace",
        "Delete",
        "Home",
        "End",
        "PageUp",
        "PageDown",
    }
)

_MESSAGES = {
    "invalid_url": "URL must be a public HTTP(S) address without credentials.",
    "invalid_selector": (
        "Selector must be 1 to 300 characters with no control characters."
    ),
    "invalid_text": (
        "Text must be at most 2000 characters with no NUL or control "
        "characters except newline and tab."
    ),
    "invalid_key": "Key is not in the allowed key list.",
    "not_started": "No page is open. Navigate to a page first.",
    "timeout": "The browser command timed out.",
    "navigation_failed": "The page could not be navigated.",
    "action_failed": "The browser action could not be completed.",
    "closed": "The browser session is closed.",
    "internal_error": "The browser session encountered an internal error.",
}


class BrowserCommandError(Exception):
    """Backend-level failure with a bounded public code.

    It is used by the built-in backend and by injectable test backends.  The
    worker never forwards its text directly; ``_MESSAGES`` is authoritative so
    an exception message can never leak page content.
    """

    def __init__(self, code: str, message: str | None = None):
        safe_code = code if code in ERROR_CODES else "internal_error"
        super().__init__(message or _MESSAGES[safe_code])
        self.code = safe_code
        self.message = message or _MESSAGES[safe_code]


class _Command:
    __slots__ = ("request_id", "action", "payload", "reply")

    def __init__(self, request_id, action, payload, reply):
        self.request_id = request_id
        self.action = action
        self.payload = payload
        self.reply = reply


def _clip(value, limit, default=""):
    """Return a bounded string, or a default for non-strings."""
    if not isinstance(value, str):
        return default
    return value[:limit]


def _json_safe(value, depth=0):
    """Copy a value into a bounded JSON-safe structure."""
    if depth > MAX_DATA_DEPTH:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:MAX_READ_CHARS]
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 100:
                break
            safe_key = key if isinstance(key, str) else str(key)
            result[safe_key[:100]] = _json_safe(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _json_safe(item, depth + 1)
            for item in list(value)[:MAX_DATA_ITEMS]
        ]
    return None


def _make_result(action, success, code, message, data=None):
    """Build the only public result shape."""
    return {
        "success": bool(success),
        "action": action,
        "code": code,
        "message": _clip(message, MAX_MESSAGE_CHARS, _MESSAGES["internal_error"]),
        "data": _json_safe(data if isinstance(data, dict) else {}),
    }


def _failure(action, code, message=None):
    return _make_result(
        action,
        False,
        code,
        message or _MESSAGES.get(code, _MESSAGES["internal_error"]),
        {},
    )


def _normalize_timeout(value, default, ceiling):
    """Return a finite, bounded seconds value or the default."""
    if isinstance(value, bool):
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(seconds):
        return default
    return max(0.1, min(seconds, ceiling))


def _normalize_int(value, default, minimum, maximum):
    """Return a bounded integer or the default for non-integers."""
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(number, maximum))


def _is_control(char):
    """True for ASCII and C1 control characters."""
    codepoint = ord(char)
    return codepoint < 32 or codepoint == 127 or 0x80 <= codepoint <= 0x9F


def _valid_selector(selector):
    if not isinstance(selector, str):
        return False
    if not selector or len(selector) > MAX_SELECTOR_CHARS:
        return False
    if not selector.strip():
        return False
    return not any(_is_control(char) for char in selector)


def _valid_fill_text(text):
    if not isinstance(text, str) or len(text) > MAX_FILL_CHARS:
        return False
    for char in text:
        if char in "\n\t":
            continue
        if _is_control(char):
            return False
    return True


def _exception_code(exc):
    """Classify an unexpected exception without exposing its text."""
    name = type(exc).__name__.casefold()
    detail = str(exc).casefold()
    if "closed" in name or "closed" in detail or "target page" in detail:
        return "closed"
    return "internal_error"


class _PlaywrightBackend:
    """The sole owner of Playwright objects; used only on the worker thread."""

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._error_type = None
        self._timeout_error_type = None

    # -- lifecycle ---------------------------------------------------
    def start(self):
        if self._page is not None:
            return
        from playwright.sync_api import (
            Error as PlaywrightError,
            TimeoutError as PlaywrightTimeoutError,
            sync_playwright,
        )

        self._error_type = PlaywrightError
        self._timeout_error_type = PlaywrightTimeoutError
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                viewport={
                    "width": VIEWPORT_WIDTH,
                    "height": VIEWPORT_HEIGHT,
                },
                locale=LOCALE,
                accept_downloads=False,
                storage_state=None,
            )
            self._page = self._context.new_page()
            self._page.set_default_timeout(DEFAULT_PAGE_TIMEOUT_MS)
            self._page.set_default_navigation_timeout(
                DEFAULT_NAVIGATION_TIMEOUT_MS
            )
            self._page.on("download", self._cancel_download)
            self._page.on("filechooser", self._cancel_file_chooser)
            self._context.on("page", self._close_extra_page)
        except BaseException:
            self.stop()
            raise

    def stop(self):
        page = self._page
        context = self._context
        browser = self._browser
        playwright = self._playwright
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        for closer in (
            getattr(page, "close", None),
            getattr(context, "close", None),
            getattr(browser, "close", None),
            getattr(playwright, "stop", None),
        ):
            if closer is None:
                continue
            try:
                closer()
            except BaseException:
                pass

    @staticmethod
    def _cancel_download(download):
        try:
            download.cancel()
        except BaseException:
            pass

    @staticmethod
    def _cancel_file_chooser(chooser):
        try:
            chooser.cancel()
        except BaseException:
            pass

    def _close_extra_page(self, page):
        if page is self._page:
            return
        try:
            page.close()
        except BaseException:
            pass

    # -- helpers -----------------------------------------------------
    def _require_page(self):
        if self._page is None:
            raise BrowserCommandError("internal_error")
        return self._page

    def _title(self):
        try:
            return _clip(self._page.title(), MAX_TITLE_CHARS)
        except BaseException:
            return ""

    def _identity(self):
        page = self._require_page()
        return _clip(page.url, MAX_URL_LENGTH), self._title()

    def _preview(self):
        try:
            text = self._page.locator("body").inner_text(
                timeout=READ_TIMEOUT_MS
            )
        except BaseException:
            return ""
        return _clip(text.strip(), MAX_PREVIEW_CHARS)

    def _read_links(self, max_links):
        page = self._require_page()
        links = []
        seen = set()
        try:
            anchors = page.locator("a[href]")
            count = anchors.count()
        except BaseException:
            return links, False

        scan_limit = min(count, MAX_LINK_SCAN)
        if max_links <= 0:
            return [], count > 0
        scanned = 0
        for index in range(scan_limit):
            scanned = index + 1
            if len(links) >= max_links:
                break
            try:
                anchor = anchors.nth(index)
                href = anchor.get_attribute(
                    "href", timeout=LINK_ITEM_TIMEOUT_MS
                )
                text = anchor.inner_text(
                    timeout=LINK_ITEM_TIMEOUT_MS
                ).strip()
            except BaseException:
                continue
            if not href:
                continue
            absolute = self._public_link(page.url, href)
            if absolute is None or absolute in seen:
                continue
            seen.add(absolute)
            links.append(
                {
                    "url": absolute,
                    "text": _clip(text, MAX_LINK_TEXT_CHARS),
                }
            )
            if len(links) >= max_links:
                break

        truncated = count > scanned
        if len(links) >= max_links and count > scanned:
            truncated = True
        return links, truncated

    @staticmethod
    def _public_link(base_url, href):
        try:
            absolute = urljoin(base_url, href)
            parts = urlsplit(absolute)
            if parts.scheme not in {"http", "https"} or not parts.netloc:
                return None
            if len(absolute) > MAX_URL_LENGTH:
                return None
            absolute = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, parts.query, "")
            )
            return validate_open_url(absolute)
        except (TypeError, ValueError):
            return None

    # -- commands ----------------------------------------------------
    def navigate(self, url, timeout):
        page = self._require_page()
        timeout_ms = int(timeout * 1000)
        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
        except self._timeout_error_type:
            raise BrowserCommandError("timeout") from None
        except self._error_type:
            raise BrowserCommandError("navigation_failed") from None

        page.wait_for_timeout(SETTLE_MS)
        final_url = _clip(page.url, MAX_URL_LENGTH)
        if not final_url.casefold().startswith(("http://", "https://")):
            raise BrowserCommandError("navigation_failed")
        try:
            validate_open_url(final_url)
        except (TypeError, ValueError):
            raise BrowserCommandError("navigation_failed") from None
        return {"url": final_url, "title": self._title()}

    def read(self, max_chars, max_links):
        page = self._require_page()
        try:
            text = page.locator("body").inner_text(timeout=READ_TIMEOUT_MS)
        except self._timeout_error_type:
            raise BrowserCommandError("timeout") from None
        except self._error_type:
            raise BrowserCommandError("action_failed") from None

        text = text.strip()
        truncated = len(text) > max_chars
        links, links_truncated = self._read_links(max_links)
        return {
            "url": _clip(page.url, MAX_URL_LENGTH),
            "title": self._title(),
            "text": text[:max_chars],
            "links": links,
            "truncated": truncated,
            "links_truncated": links_truncated,
        }

    def click(self, selector, timeout):
        page = self._require_page()
        timeout_ms = int(timeout * 1000)
        try:
            target = page.locator(selector).first
            target.wait_for(state="visible", timeout=timeout_ms)
            target.click(timeout=timeout_ms)
        except self._timeout_error_type:
            raise BrowserCommandError("timeout") from None
        except self._error_type:
            raise BrowserCommandError("action_failed") from None

        try:
            page.wait_for_load_state(
                "domcontentloaded", timeout=min(timeout_ms, 5_000)
            )
        except BaseException:
            pass
        page.wait_for_timeout(SETTLE_MS)
        url, title = self._identity()
        return {"url": url, "title": title, "text": self._preview()}

    def fill(self, selector, text, timeout):
        page = self._require_page()
        timeout_ms = int(timeout * 1000)
        try:
            target = page.locator(selector).first
            target.wait_for(state="visible", timeout=timeout_ms)
            target.fill(text, timeout=timeout_ms)
        except self._timeout_error_type:
            raise BrowserCommandError("timeout") from None
        except self._error_type:
            raise BrowserCommandError("action_failed") from None
        url, title = self._identity()
        return {"url": url, "title": title}

    def press(self, key):
        page = self._require_page()
        try:
            page.keyboard.press(key)
        except self._error_type:
            raise BrowserCommandError("action_failed") from None
        try:
            page.wait_for_load_state("domcontentloaded", timeout=2_000)
        except BaseException:
            pass
        page.wait_for_timeout(SETTLE_MS)
        url, title = self._identity()
        return {"url": url, "title": title, "text": self._preview()}


class BrowserSession:
    """Thread-safe, lazily started, isolated browser command queue."""

    def __init__(
        self,
        *,
        backend_factory=None,
        queue_timeout=DEFAULT_QUEUE_TIMEOUT,
    ):
        if backend_factory is not None and not callable(backend_factory):
            raise TypeError("backend_factory must be callable")
        self._backend_factory = backend_factory or _PlaywrightBackend
        if isinstance(queue_timeout, bool):
            queue_timeout = DEFAULT_QUEUE_TIMEOUT
        try:
            queue_timeout = float(queue_timeout)
        except (TypeError, ValueError):
            queue_timeout = DEFAULT_QUEUE_TIMEOUT
        if not math.isfinite(queue_timeout):
            queue_timeout = DEFAULT_QUEUE_TIMEOUT
        self._queue_timeout = max(0.05, min(queue_timeout, 300.0))

        self._lock = threading.RLock()
        self._commands = None
        self._worker = None
        self._next_request_id = 0
        self._closed = False

    # -- properties --------------------------------------------------
    @property
    def is_running(self):
        """True while a worker thread is alive (the browser may be lazy)."""
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    @property
    def is_closed(self):
        with self._lock:
            return self._closed

    # -- public commands --------------------------------------------
    def navigate(self, url, timeout=DEFAULT_NAVIGATE_TIMEOUT):
        """Open one public HTTP(S) URL in the shared page."""
        # The shared validator strips surrounding whitespace before checking
        # controls, so reject raw control characters here as well.
        if not isinstance(url, str) or any(
            ord(char) < 32 or ord(char) == 127 for char in url
        ):
            return _failure("navigate", "invalid_url")
        try:
            clean_url = validate_open_url(url)
        except (TypeError, ValueError):
            return _failure("navigate", "invalid_url")
        seconds = _normalize_timeout(
            timeout, DEFAULT_NAVIGATE_TIMEOUT, self._action_ceiling()
        )
        return self._submit("navigate", {"url": clean_url, "timeout": seconds})

    def read(self, max_chars=DEFAULT_MAX_CHARS, max_links=DEFAULT_MAX_LINKS):
        """Read bounded visible text and public links from the shared page."""
        text_limit = _normalize_int(
            max_chars, DEFAULT_MAX_CHARS, 0, MAX_READ_CHARS
        )
        link_limit = _normalize_int(
            max_links, DEFAULT_MAX_LINKS, 0, MAX_READ_LINKS
        )
        return self._submit(
            "read",
            {"max_chars": text_limit, "max_links": link_limit},
        )

    def click(self, selector, timeout=DEFAULT_ACTION_TIMEOUT):
        """Wait for a CSS selector and click the first visible match."""
        if not _valid_selector(selector):
            return _failure("click", "invalid_selector")
        seconds = _normalize_timeout(
            timeout, DEFAULT_ACTION_TIMEOUT, self._action_ceiling()
        )
        return self._submit(
            "click", {"selector": selector, "timeout": seconds}
        )

    def fill(self, selector, text, timeout=DEFAULT_ACTION_TIMEOUT):
        """Fill one field without pressing Enter."""
        if not _valid_selector(selector):
            return _failure("fill", "invalid_selector")
        if not _valid_fill_text(text):
            return _failure("fill", "invalid_text")
        seconds = _normalize_timeout(
            timeout, DEFAULT_ACTION_TIMEOUT, self._action_ceiling()
        )
        return self._submit(
            "fill",
            {"selector": selector, "text": text, "timeout": seconds},
        )

    def press(self, key):
        """Press exactly one allowlisted keyboard key."""
        if not isinstance(key, str) or key not in ALLOWED_KEYS:
            return _failure("press", "invalid_key")
        return self._submit("press", {"key": key})

    def close(self):
        """Close page, context, browser and stop the worker thread."""
        with self._lock:
            if self._closed:
                return _make_result(
                    "close", True, SUCCESS_CODE, "Browser session already closed."
                )
            self._closed = True
            worker = self._worker
            commands = self._commands
            if (
                worker is None
                or commands is None
                or not worker.is_alive()
            ):
                self._worker = None
                self._commands = None
                return _make_result(
                    "close", True, SUCCESS_CODE, "Browser session closed."
                )

            self._next_request_id += 1
            request_id = self._next_request_id
            reply = queue.Queue(maxsize=1)
            commands.put_nowait(
                _Command(request_id, "close", {}, reply)
            )

        status, response = self._wait_for_reply(reply, worker)
        with self._lock:
            if self._worker is worker:
                self._worker = None
                self._commands = None
        if worker.is_alive():
            worker.join(timeout=WORKER_JOIN_SECONDS)

        if status == "ok" and isinstance(response, tuple):
            if response[0] == request_id and isinstance(response[1], dict):
                return response[1]
        if status == "timeout":
            return _failure("close", "timeout")
        return _failure("close", "internal_error")

    def shutdown(self):
        """Alias for :meth:`close` provided for service lifecycles."""
        return self.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    # -- queue and lifecycle ----------------------------------------
    def _action_ceiling(self):
        # Leave headroom so a browser-side timeout can report before the
        # caller's queue wait expires.
        return max(0.1, self._queue_timeout - 1.0)

    def _ensure_worker_locked(self):
        if (
            self._worker is not None
            and self._commands is not None
            and self._worker.is_alive()
        ):
            return
        commands = queue.Queue()
        worker = threading.Thread(
            target=self._worker_main,
            args=(commands,),
            name="misty-browser-session",
            daemon=True,
        )
        self._commands = commands
        self._worker = worker
        worker.start()

    def _submit(self, action, payload):
        with self._lock:
            if self._closed:
                return _failure(action, "closed")
            self._ensure_worker_locked()
            commands = self._commands
            worker = self._worker
            self._next_request_id += 1
            request_id = self._next_request_id
            reply = queue.Queue(maxsize=1)
            command = _Command(request_id, action, dict(payload), reply)
            commands.put_nowait(command)

        status, response = self._wait_for_reply(reply, worker)
        if status == "timeout":
            return _failure(action, "timeout")
        if status == "dead":
            return _failure(action, "internal_error")
        if (
            not isinstance(response, tuple)
            or len(response) != 2
            or response[0] != request_id
            or not isinstance(response[1], dict)
        ):
            return _failure(action, "internal_error")
        return response[1]

    def _wait_for_reply(self, reply, worker):
        """Wait at most queue_timeout, returning ('ok'|'timeout'|'dead', value)."""
        deadline = time.monotonic() + self._queue_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", None
            try:
                return "ok", reply.get(timeout=min(0.1, remaining))
            except queue.Empty:
                if not worker.is_alive():
                    try:
                        return "ok", reply.get_nowait()
                    except queue.Empty:
                        return "dead", None

    def _worker_main(self, commands):
        """Thread entry point.  No exception text or page text leaves here."""
        backend = None
        navigated = False
        try:
            while True:
                command = commands.get()
                action = command.action
                if action == "close":
                    self._stop_backend(backend)
                    backend = None
                    command.reply.put(
                        (
                            command.request_id,
                            _make_result(
                                "close",
                                True,
                                SUCCESS_CODE,
                                "Browser session closed.",
                            ),
                        )
                    )
                    return

                try:
                    if action == "navigate":
                        if backend is None:
                            backend = self._create_backend()
                        data = self._call(
                            backend, "navigate",
                            command.payload["url"],
                            command.payload["timeout"],
                        )
                        navigated = True
                        result = _make_result(
                            "navigate",
                            True,
                            SUCCESS_CODE,
                            "Navigation completed.",
                            self._shape_data(action, command.payload, data),
                        )
                    elif action in {"read", "click", "fill", "press"}:
                        if backend is None or not navigated:
                            result = _failure(action, "not_started")
                        else:
                            if action == "read":
                                data = self._call(
                                    backend, "read",
                                    command.payload["max_chars"],
                                    command.payload["max_links"],
                                )
                            elif action == "click":
                                data = self._call(
                                    backend, "click",
                                    command.payload["selector"],
                                    command.payload["timeout"],
                                )
                            elif action == "fill":
                                data = self._call(
                                    backend, "fill",
                                    command.payload["selector"],
                                    command.payload["text"],
                                    command.payload["timeout"],
                                )
                            else:
                                data = self._call(
                                    backend, "press",
                                    command.payload["key"],
                                )
                            result = _make_result(
                                action,
                                True,
                                SUCCESS_CODE,
                                "Browser command completed.",
                                self._shape_data(
                                    action, command.payload, data
                                ),
                            )
                    else:
                        result = _failure(action, "internal_error")
                except BrowserCommandError as exc:
                    if exc.code in {"internal_error", "closed"}:
                        self._stop_backend(backend)
                        backend = None
                        navigated = False
                    result = _failure(action, exc.code)
                except BaseException as exc:
                    self._stop_backend(backend)
                    backend = None
                    navigated = False
                    result = _failure(action, _exception_code(exc))

                command.reply.put((command.request_id, result))
        finally:
            self._stop_backend(backend)

    def _create_backend(self):
        try:
            backend = self._backend_factory()
        except BaseException:
            raise BrowserCommandError("internal_error") from None
        if backend is None:
            raise BrowserCommandError("internal_error")
        try:
            backend.start()
        except BrowserCommandError:
            self._stop_backend(backend)
            raise
        except BaseException:
            self._stop_backend(backend)
            raise BrowserCommandError("internal_error") from None
        return backend

    @staticmethod
    def _call(backend, method_name, *args):
        method = getattr(backend, method_name, None)
        if not callable(method):
            raise BrowserCommandError("internal_error")
        data = method(*args)
        if not isinstance(data, dict):
            raise BrowserCommandError("internal_error")
        return data

    @staticmethod
    def _stop_backend(backend):
        if backend is None:
            return
        stop = getattr(backend, "stop", None)
        if not callable(stop):
            return
        try:
            stop()
        except BaseException:
            pass

    @staticmethod
    def _shape_data(action, payload, data):
        """Bound and whitelist backend data before it leaves the worker."""
        if action == "navigate":
            return {
                "url": _clip(data.get("url"), MAX_URL_LENGTH),
                "title": _clip(data.get("title"), MAX_TITLE_CHARS),
            }
        if action == "read":
            max_chars = payload["max_chars"]
            max_links = payload["max_links"]
            raw_links = data.get("links")
            links = []
            if isinstance(raw_links, (list, tuple)):
                for item in list(raw_links)[:max_links]:
                    if not isinstance(item, dict):
                        continue
                    links.append(
                        {
                            "url": _clip(
                                item.get("url"), MAX_URL_LENGTH
                            ),
                            "text": _clip(
                                item.get("text"), MAX_LINK_TEXT_CHARS
                            ),
                        }
                    )
            text = _clip(data.get("text"), max_chars)
            return {
                "url": _clip(data.get("url"), MAX_URL_LENGTH),
                "title": _clip(data.get("title"), MAX_TITLE_CHARS),
                "text": text,
                "links": links,
                "truncated": bool(data.get("truncated")),
                "links_truncated": bool(data.get("links_truncated")),
            }
        if action in {"click", "press"}:
            return {
                "url": _clip(data.get("url"), MAX_URL_LENGTH),
                "title": _clip(data.get("title"), MAX_TITLE_CHARS),
                "text": _clip(data.get("text"), MAX_PREVIEW_CHARS),
            }
        if action == "fill":
            return {
                "url": _clip(data.get("url"), MAX_URL_LENGTH),
                "title": _clip(data.get("title"), MAX_TITLE_CHARS),
            }
        return {}
