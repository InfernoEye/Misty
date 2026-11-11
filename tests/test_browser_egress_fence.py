"""Egress-fence tests for the Playwright browser backend.

Playwright is never imported or launched.  A fake ``playwright.sync_api``
module hands the backend a fake browser/context/page that records every
route registration, every ``goto``/``click``/``press`` and every request
URL the fence saw.  The real ``_PlaywrightBackend``, route handler and
``BrowserSession`` worker are exercised.
"""

import sys
import types
import unittest
from unittest import mock

import app.browser.session as session_module
from app.browser.session import BrowserCommandError, BrowserSession


# -- fake Playwright ---------------------------------------------------
class FakeRequest:
    def __init__(self, url, resource_type="document"):
        self.url = url
        self.resource_type = resource_type


class FakeFetchResponse:
    def __init__(self, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.disposed = False

    def dispose(self):
        self.disposed = True


class FakeRoute:
    """Records what the fence decided for one request."""

    def __init__(self, request, fetch_result=None):
        self.request = request
        self.fetch_result = fetch_result
        self.fetch_calls = 0
        self.actions = []

    def fetch(self):
        self.fetch_calls += 1
        if isinstance(self.fetch_result, BaseException):
            raise self.fetch_result
        return self.fetch_result

    def continue_(self):
        self.actions.append("continue")

    def abort(self):
        self.actions.append("abort")

    @property
    def decision(self):
        return self.actions[-1] if self.actions else None


class FakeWebSocketRoute:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        self.page.press(key)


class FakePage:
    def __init__(self, owner, url="https://example.com/start"):
        self.owner = owner
        self.url = url
        self.routes = []
        self.goto_calls = []
        self.goto_result = None  # exception to raise, or None
        self.goto_final_url = None
        self.clicked = []
        self.pressed = []
        self.keyboard = FakeKeyboard(self)
        self.listeners = {}

    # routing
    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    # capture
    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.owner.requests_issued.append(url)
        if self.owner.goto_via_route:
            # Emulate the fence seeing the navigation request first.
            self.owner.dispatch(self, FakeRequest(url))
        if isinstance(self.goto_result, BaseException):
            raise self.goto_result
        if self.goto_final_url is not None:
            self.url = self.goto_final_url

    def click(self, selector):
        self.clicked.append(selector)
        self.owner.requests_issued.append(f"click:{selector}")
        if self.owner.click_final_url is not None:
            self.url = self.owner.click_final_url
        if self.owner.click_via_route and self.owner.click_final_url:
            self.owner.dispatch(self, FakeRequest(self.owner.click_final_url))

    def press(self, key):
        self.pressed.append(key)
        self.owner.requests_issued.append(f"press:{key}")
        if self.owner.press_final_url is not None:
            self.url = self.owner.press_final_url

    def on(self, event, handler):
        self.listeners[event] = handler

    def set_default_timeout(self, ms):
        pass

    def set_default_navigation_timeout(self, ms):
        pass

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, state, timeout=None):
        pass

    def wait_for_selector(self, selector, timeout=None):
        pass

    def title(self):
        return "Fake Title"

    def close(self):
        pass

    def locator(self, selector):
        return FakeLocator(self)


class FakeLocator:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    def count(self):
        return 0

    def nth(self, index):
        return self

    def wait_for(self, state=None, timeout=None):
        pass

    def click(self, timeout=None):
        self.page.click("locator.click")

    def fill(self, text, timeout=None):
        self.page.fill_text = text

    def get_attribute(self, name, timeout=None):
        return None

    def inner_text(self, timeout=None):
        return "body text"


class FakeContext:
    def __init__(self, owner):
        self.owner = owner
        self.routes = []
        self.websocket_routes = []
        self.listeners = {}
        self.pages = []

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def route_web_socket(self, pattern, handler):
        self.websocket_routes.append((pattern, handler))

    def on(self, event, handler):
        self.listeners[event] = handler

    def new_page(self):
        page = FakePage(self.owner)
        self.pages.append(page)
        return page

    def close(self):
        pass


class FakeBrowser:
    def __init__(self, owner):
        self.owner = owner
        self.contexts = []

    def new_context(self, **kwargs):
        context = FakeContext(self.owner)
        self.contexts.append(context)
        return context

    def close(self):
        pass


class FakeChromium:
    def __init__(self, owner):
        self.owner = owner

    def launch(self, **kwargs):
        return FakeBrowser(self.owner)


class FakePlaywright:
    def __init__(self, owner):
        self.owner = owner
        self.chromium = FakeChromium(owner)
        self.stopped = False

    def start(self):
        return self

    def stop(self):
        self.stopped = True


class FakePlaywrightError(Exception):
    pass


class FakePlaywrightTimeoutError(Exception):
    pass


class Owner:
    """Holds the recording state shared by the fake Playwright objects."""

    def __init__(self):
        self.requests_issued = []
        self.dispatch_log = []
        self.fake = None
        self.context = None
        self.goto_via_route = True
        self.goto_final_url = None
        self.click_final_url = None
        self.press_final_url = None
        self.click_via_route = False

    def start(self):
        self.fake = FakePlaywright(self)
        return self.fake

    def dispatch(self, page, request):
        """Run the request through the page-level and context-level routes."""
        handlers = []
        for pattern, handler in page.routes:
            if pattern == "**/*":
                handlers.append(handler)
        if self.context is not None:
            context = self.context
            for pattern, handler in context.routes:
                if pattern == "**/*":
                    handlers.append(handler)
        if not handlers:
            self.dispatch_log.append("no-route")
            return None
        route = FakeRoute(request, FakeFetchResponse(200))
        self.dispatch_log.append(route)
        handlers[0](route)
        return route


def _install_fake_playwright(owner):
    module = types.ModuleType("playwright.sync_api")
    module.Error = FakePlaywrightError
    module.TimeoutError = FakePlaywrightTimeoutError
    module.sync_playwright = owner.start
    package = types.ModuleType("playwright")
    package.sync_api = module
    patcher = mock.patch.dict(
        sys.modules,
        {"playwright": package, "playwright.sync_api": module},
    )
    patcher.start()
    return patcher


# -- tests ---------------------------------------------------------------
class BackendFixture(unittest.TestCase):
    def setUp(self):
        self.owner = Owner()
        patcher = _install_fake_playwright(self.owner)
        self.addCleanup(patcher.stop)
        self.backend = session_module._PlaywrightBackend()
        self.addCleanup(self.backend.stop)

    def start(self):
        self.backend.start()
        self.owner.context = self.backend._context
        return self.backend._page

    def decide(self, url, resource_type="document", fetch_result=None):
        route = FakeRoute(FakeRequest(url, resource_type), fetch_result)
        self.backend._handle_route(route)
        return route


class FenceInstallationTests(BackendFixture):
    def test_start_installs_context_route_before_page_request(self):
        page = self.start()
        context = self.backend._context
        self.assertEqual(len(context.routes), 1)
        pattern, handler = context.routes[0]
        self.assertEqual(pattern, "**/*")
        self.assertEqual(handler, self.backend._handle_route)
        self.assertEqual(handler.__self__, self.backend)
        self.assertIn(context, self.backend._route_handlers)
        self.assertTrue(self.backend._fence_installed)

    def test_start_aborts_websockets(self):
        self.start()
        context = self.backend._context
        self.assertEqual(len(context.websocket_routes), 1)
        pattern, handler = context.websocket_routes[0]
        self.assertEqual(pattern, "**/*")
        route = FakeWebSocketRoute()
        handler(route)
        self.assertEqual(route.closed, 1)

    def test_start_fails_closed_without_route_support(self):
        class NoRouteContext(FakeContext):
            route = None

        with mock.patch.object(
            FakeBrowser, "new_context", lambda browser, **kw: NoRouteContext(
                browser.owner
            )
        ):
            with self.assertRaises(BaseException):
                self.backend.start()
        self.assertIsNone(self.backend._context)

    def test_stop_clears_fence_state(self):
        self.start()
        self.backend.stop()
        self.assertFalse(self.backend._fence_installed)


class RequestFenceTests(BackendFixture):
    def setUp(self):
        super().setUp()
        self.start()

    def assert_aborted(self, url, resource_type="document"):
        route = self.decide(url, resource_type)
        self.assertEqual(route.decision, "abort", url)
        return route

    def assert_allowed(self, url, resource_type="document"):
        route = self.decide(
            url, resource_type, FakeFetchResponse(200)
        )
        self.assertEqual(route.decision, "continue", url)
        return route

    def test_private_and_legacy_literal_urls_abort(self):
        for url in (
            "http://127.0.0.1/",
            "http://127.0.0.1:8080/",
            "http://127.1:8080/",
            "http://2130706433:8080/",
            "http://0x7f000001:8080/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/",
            "http://[::1]/",
            "http://localhost/",
            "http://printer.local/",
            "http://intranet/",
            "https://user:pass@example.com/",
        ):
            with self.subTest(url=url):
                self.assert_aborted(url)

    def test_non_http_schemes_abort(self):
        for url in (
            "file:///etc/passwd",
            "data:text/html,<script>x</script>",
            "data:image/png;base64,AAAA",
            "blob:https://example.com/1234",
            "javascript:alert(1)",
            "about:blank",
            "chrome://settings",
            "ftp://example.com/file",
            "ws://example.com/socket",
            "wss://example.com/socket",
        ):
            with self.subTest(url=url):
                self.assert_aborted(url, resource_type="other")

    def test_public_https_is_allowed(self):
        route = self.assert_allowed("https://example.com/page")
        self.assertEqual(route.fetch_calls, 1)
        self.assertTrue(route.fetch_result.disposed)

    def test_relative_redirect_is_allowed(self):
        # Relative Location inherits the validated origin.
        route = self.decide(
            "https://example.com/start",
            fetch_result=FakeFetchResponse(302, {"Location": "/next"}),
        )
        self.assertEqual(route.decision, "continue")

    def test_redirect_response_to_private_target_aborts(self):
        for location in (
            "http://127.0.0.1/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://127.1/",
            "http://10.0.0.5/",
            "file:///etc/passwd",
            "//2130706433/x",
        ):
            with self.subTest(location=location):
                route = self.decide(
                    "https://example.com/go",
                    fetch_result=FakeFetchResponse(
                        302, {"location": location}
                    ),
                )
                self.assertEqual(route.decision, "abort")
                self.assertTrue(route.fetch_result.disposed)

    def test_redirect_response_to_public_target_is_allowed(self):
        route = self.decide(
            "https://example.com/go",
            fetch_result=FakeFetchResponse(
                308, {"Location": "https://other.example/x"}
            ),
        )
        self.assertEqual(route.decision, "continue")

    def test_meta_refresh_to_private_aborts(self):
        route = self.decide(
            "https://example.com/go",
            fetch_result=FakeFetchResponse(
                200, {"Refresh": "0; url=http://127.0.0.1/"}
            ),
        )
        self.assertEqual(route.decision, "abort")

    def test_unreadable_status_or_fetch_failure_aborts(self):
        for result in (
            FakeFetchResponse("not-a-status"),
            BaseException("tls reset"),
            RuntimeError("boom"),
        ):
            with self.subTest(result=result):
                route = self.decide("https://example.com/", fetch_result=result)
                self.assertEqual(route.decision, "abort")

    def test_route_without_request_aborts(self):
        class RouteWithoutRequest:
            def __init__(self):
                self.actions = []

            def abort(self):
                self.actions.append("abort")

            def continue_(self):
                self.actions.append("continue")

        route = RouteWithoutRequest()
        self.backend._handle_route(route)
        self.assertEqual(route.actions, ["abort"])

    def test_handler_never_raises(self):
        class Broken:
            @property
            def request(self):
                raise RuntimeError("boom")

            def abort(self):
                pass

        self.backend._handle_route(Broken())  # must not raise

    def test_continue_failure_falls_back_to_abort(self):
        class FailingContinue(FakeRoute):
            def continue_(self):
                raise RuntimeError("window closed")

        route = FailingContinue(FakeRequest("https://example.com/"),
                                FakeFetchResponse(200))
        self.backend._handle_route(route)
        self.assertEqual(route.actions, ["abort"])


class NavigateTests(BackendFixture):
    def setUp(self):
        super().setUp()
        self.page = self.start()

    def test_navigate_validates_before_goto(self):
        result = self.backend.navigate("https://example.com/", 1.0)
        self.assertEqual(self.page.goto_calls, ["https://example.com/"])
        self.assertEqual(result["url"], "https://example.com/start")
        # The fence saw the dispatched request as well.
        self.assertEqual(self.owner.dispatch_log[-1].decision, "continue")

    def test_navigate_to_private_literal_issues_no_request(self):
        for url in (
            "http://127.0.0.1:8080/",
            "http://127.1/",
            "http://2130706433:8080/",
            "http://0x7f000001:8080/",
            "http://169.254.169.254/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(BrowserCommandError) as raised:
                    self.backend.navigate(url, 1.0)
                self.assertEqual(raised.exception.code, "invalid_url")
        self.assertEqual(self.page.goto_calls, [])
        self.assertEqual(self.owner.requests_issued, [])

    def test_navigate_followed_redirect_is_aborted_at_request_time(self):
        self.page.goto_result = FakePlaywrightError("net::ERR_ABORTED")
        self.page.goto_final_url = "http://127.0.0.1/"
        with self.assertRaises(BrowserCommandError) as raised:
            self.backend.navigate("https://example.com/", 1.0)
        self.assertEqual(raised.exception.code, "navigation_failed")
        # The navigation request itself is public, but Chromium then asks
        # for the redirect target, which the fence aborts.
        self.owner.dispatch(self.page, FakeRequest("http://127.0.0.1/"))
        dispatched = [
            route for route in self.owner.dispatch_log
            if isinstance(route, FakeRoute)
        ]
        self.assertEqual(dispatched[-1].decision, "abort")

    def test_navigate_with_private_final_url_fails(self):
        self.page.goto_via_route = False
        self.page.goto_final_url = "http://192.168.1.1/"
        with self.assertRaises(BrowserCommandError) as raised:
            self.backend.navigate("https://example.com/", 1.0)
        self.assertEqual(raised.exception.code, "navigation_failed")

    def test_navigate_requires_installed_fence(self):
        self.backend._fence_installed = False
        with self.assertRaises(BrowserCommandError) as raised:
            self.backend.navigate("https://example.com/", 1.0)
        self.assertEqual(raised.exception.code, "internal_error")
        self.assertEqual(self.page.goto_calls, [])


class ActionTests(BackendFixture):
    def setUp(self):
        super().setUp()
        self.page = self.start()

    def test_click_to_private_url_raises_and_session_stops(self):
        self.owner.click_final_url = "http://10.0.0.1/admin"
        with self.assertRaises(BrowserCommandError) as raised:
            self.backend.click("#go", 1.0)
        self.assertEqual(raised.exception.code, "navigation_failed")

    def test_press_to_private_url_raises(self):
        self.owner.press_final_url = "http://169.254.169.254/"
        with self.assertRaises(BrowserCommandError) as raised:
            self.backend.press("Enter")
        self.assertEqual(raised.exception.code, "navigation_failed")

    def test_click_and_press_to_public_urls_succeed(self):
        self.owner.click_final_url = "https://example.com/next"
        self.assertEqual(
            self.backend.click("#go", 1.0)["url"], "https://example.com/next"
        )
        self.assertEqual(
            self.backend.press("Enter")["url"], "https://example.com/next"
        )


class SessionFenceTests(unittest.TestCase):
    def test_navigation_failed_from_fence_stops_session(self):
        owner = Owner()
        patcher = _install_fake_playwright(owner)
        self.addCleanup(patcher.stop)
        owner.click_final_url = "http://127.0.0.1/"
        session = BrowserSession(queue_timeout=10.0)
        try:
            opened = session.navigate("https://example.com/", timeout=2.0)
            self.assertTrue(opened["success"], opened)
            clicked = session.click("#go", timeout=2.0)
            self.assertFalse(clicked["success"])
            self.assertEqual(clicked["code"], "navigation_failed")
            # The page is on an unapproved URL: the browser must be gone.
            # (The idle worker thread may still be alive for the next
            # command, but it has no backend and nothing was navigated.)
            self.assertEqual(session.read()["code"], "not_started")
        finally:
            session.close()

    def test_backend_without_fence_is_refused(self):
        class UnfencedBackend:
            _fence_installed = False

            def start(self):
                pass

            def stop(self):
                pass

            def navigate(self, url, timeout):
                return {"url": url, "title": ""}

        session = BrowserSession(
            backend_factory=UnfencedBackend, queue_timeout=5.0
        )
        try:
            result = session.navigate("https://example.com/", timeout=2.0)
            self.assertEqual(result["code"], "internal_error")
            self.assertFalse(result["success"])
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
