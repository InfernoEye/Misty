"""Tests for the isolated browser-session service.

The unit tests never launch Playwright.  They either exercise validation (the
worker must not start) or route commands to a fake backend that records which
thread touched it.  One live smoke test uses https://example.com and is skipped
when the network is unavailable.
"""

import json
import socket
import threading
import time
import unittest

from app.browser.session import BrowserSession, BrowserCommandError


def _network_available():
    try:
        with socket.create_connection(("example.com", 443), timeout=3):
            return True
    except OSError:
        return False


NETWORK_AVAILABLE = _network_available()


class FakeBackend:
    """A thread-recording backend; all methods must run on the worker."""

    def __init__(self):
        self.calls = []
        self.thread_names = set()
        self.start_count = 0
        self.stop_count = 0
        self.read_result = {
            "url": "https://example.com/",
            "title": "Example Domain",
            "text": "Example Domain text",
            "links": [],
            "truncated": False,
            "links_truncated": False,
        }

    def _record(self, name, *args):
        self.calls.append((name,) + args)
        self.thread_names.add(threading.current_thread().name)

    def start(self):
        self._record("start")
        self.start_count += 1

    def stop(self):
        self._record("stop")
        self.stop_count += 1

    def navigate(self, url, timeout):
        self._record("navigate", url, timeout)
        return {"url": url, "title": "Example Domain"}

    def read(self, max_chars, max_links):
        self._record("read", max_chars, max_links)
        return dict(self.read_result)

    def click(self, selector, timeout):
        self._record("click", selector, timeout)
        return {
            "url": "https://example.com/next",
            "title": "Next",
            "text": "A" * 900,
        }

    def fill(self, selector, text, timeout):
        self._record("fill", selector, text, timeout)
        return {"url": "https://example.com/", "title": "Example Domain"}

    def press(self, key):
        self._record("press", key)
        return {
            "url": "https://example.com/",
            "title": "Example Domain",
            "text": "pressed",
        }


def _factory(created, backend_class=FakeBackend):
    def build():
        backend = backend_class()
        created.append(backend)
        return backend

    return build


def _must_not_start():
    raise AssertionError("Backend must not be created during validation")


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.session = BrowserSession(backend_factory=_must_not_start)

    def tearDown(self):
        self.session.close()

    def assert_result_shape(self, result, action, code):
        self.assertEqual(
            set(result),
            {"success", "action", "code", "message", "data"},
        )
        self.assertEqual(result["action"], action)
        self.assertEqual(result["code"], code)
        self.assertIsInstance(result["success"], bool)
        self.assertIsInstance(result["message"], str)
        self.assertIsInstance(result["data"], dict)
        json.dumps(result)

    def test_invalid_urls_are_rejected_without_worker(self):
        urls = [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "ftp://example.org/file",
            "https://user:pass@example.org/",
            "http://localhost/",
            "http://127.0.0.1/",
            "http://10.0.0.1/",
            "http://192.168.1.1/",
            "http://[::1]/",
            "http://router.local/",
            "https://example.org/\x00",
            "https://example.org/\n",
            "x" * 2049,
            "",
            None,
            123,
        ]
        for url in urls:
            with self.subTest(url=url):
                result = self.session.navigate(url)
                self.assertFalse(result["success"])
                self.assert_result_shape(result, "navigate", "invalid_url")
                self.assertEqual(result["data"], {})
        self.assertFalse(self.session.is_running)

    def test_invalid_selectors_are_rejected_without_worker(self):
        for selector in (
            "",
            "   ",
            "div\n",
            "div\x00",
            "x" * 301,
            None,
            42,
        ):
            with self.subTest(selector=selector):
                click_result = self.session.click(selector)
                self.assert_result_shape(
                    click_result, "click", "invalid_selector"
                )
                fill_result = self.session.fill(selector, "ok")
                self.assert_result_shape(
                    fill_result, "fill", "invalid_selector"
                )
        self.assertFalse(self.session.is_running)

    def test_invalid_fill_text_is_rejected_without_worker(self):
        for text in (
            "x" * 2001,
            "bad\x00text",
            "bad\x01text",
            "bad\x7ftext",
            "bad\rtext",
            None,
            123,
        ):
            with self.subTest(text=text):
                result = self.session.fill("#field", text)
                self.assert_result_shape(result, "fill", "invalid_text")
        for text in ("", "line one\nline two\tvalue", "x" * 2000):
            with self.subTest(valid_text=text):
                result = self.session.fill("#field", text)
                # Valid input passes validation but no page is open.
                self.assertEqual(result["code"], "not_started")
        # A not_started command may create the idle worker, but it must never
        # create a browser backend.
        self.assertNotEqual(result["code"], "internal_error")

    def test_invalid_keys_are_rejected_without_worker(self):
        for key in (
            "Control+A",
            "Shift+Tab",
            "enter",
            "Enter\n",
            "F1",
            "",
            None,
            5,
            ["Enter"],
        ):
            with self.subTest(key=key):
                result = self.session.press(key)
                self.assert_result_shape(result, "press", "invalid_key")
        self.assertFalse(self.session.is_running)

    def test_read_before_navigation_is_not_started(self):
        result = self.session.read()
        self.assert_result_shape(result, "read", "not_started")
        self.assertFalse(result["success"])
        self.assertNotEqual(result["code"], "internal_error")

    def test_close_is_idempotent_and_commands_fail_closed(self):
        first = self.session.close()
        self.assertTrue(first["success"])
        self.assert_result_shape(first, "close", "ok")
        second = self.session.close()
        self.assertTrue(second["success"])
        self.assert_result_shape(second, "close", "ok")
        self.assertTrue(self.session.is_closed)
        for result, action in (
            (self.session.read(), "read"),
            (self.session.navigate("https://example.com/"), "navigate"),
            (self.session.click("#x"), "click"),
            (self.session.fill("#x", "v"), "fill"),
            (self.session.press("Enter"), "press"),
        ):
            with self.subTest(action=action):
                self.assert_result_shape(result, action, "closed")
                self.assertFalse(result["success"])

    def test_backend_factory_must_be_callable(self):
        with self.assertRaises(TypeError):
            BrowserSession(backend_factory="not callable")


class CommandRoutingTests(unittest.TestCase):
    def setUp(self):
        self.backends = []
        self.session = BrowserSession(
            backend_factory=_factory(self.backends),
            queue_timeout=10.0,
        )

    def tearDown(self):
        self.session.close()

    def _navigate(self):
        result = self.session.navigate("https://example.com/", timeout=4.0)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["code"], "ok")
        self.assertEqual(result["data"]["url"], "https://example.com/")
        self.assertEqual(result["data"]["title"], "Example Domain")
        self.assertEqual(len(self.backends), 1)
        return self.backends[0]

    def test_navigate_starts_one_backend_on_worker_thread(self):
        backend = self._navigate()
        self.assertEqual(backend.start_count, 1)
        self.assertEqual(backend.calls[0], ("start",))
        self.assertEqual(
            backend.calls[1],
            ("navigate", "https://example.com/", 4.0),
        )
        self.assertEqual(len(backend.thread_names), 1)
        self.assertNotIn(threading.current_thread().name, backend.thread_names)
        self.assertTrue(self.session.is_running)

    def test_read_is_bounded_even_if_backend_overproduces(self):
        backend = self._navigate()
        backend.read_result = {
            "url": "https://example.com/" + "u" * 5000,
            "title": "T" * 5000,
            "text": "X" * 50000,
            "links": [
                {
                    "url": f"https://example.com/{index}",
                    "text": "L" * 5000,
                }
                for index in range(8)
            ],
            "truncated": True,
            "links_truncated": True,
        }
        result = self.session.read(max_chars=12, max_links=2)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["code"], "ok")
        self.assertEqual(backend.calls[-1], ("read", 12, 2))
        self.assertEqual(result["data"]["text"], "X" * 12)
        self.assertEqual(len(result["data"]["title"]), 300)
        self.assertLessEqual(len(result["data"]["url"]), 2048)
        self.assertEqual(len(result["data"]["links"]), 2)
        for link in result["data"]["links"]:
            self.assertEqual(set(link), {"url", "text"})
            self.assertLessEqual(len(link["text"]), 200)
        json.dumps(result)

    def test_read_defaults_and_clamping(self):
        backend = self._navigate()
        self.session.read()
        self.assertEqual(backend.calls[-1], ("read", 20000, 100))
        self.session.read(max_chars=10**9, max_links=10**9)
        self.assertEqual(backend.calls[-1], ("read", 100000, 500))
        self.session.read(max_chars=-5, max_links=-5)
        self.assertEqual(backend.calls[-1], ("read", 0, 0))

    def test_click_fill_press_route_to_shared_page(self):
        backend = self._navigate()
        click_result = self.session.click("#go", timeout=3.0)
        self.assertTrue(click_result["success"], click_result)
        self.assertEqual(backend.calls[-1], ("click", "#go", 3.0))
        self.assertEqual(click_result["data"]["url"], "https://example.com/next")
        self.assertLessEqual(len(click_result["data"]["text"]), 500)

        fill_result = self.session.fill("#q", "hello\nworld\t!", timeout=4.0)
        self.assertTrue(fill_result["success"], fill_result)
        self.assertEqual(
            backend.calls[-1], ("fill", "#q", "hello\nworld\t!", 4.0)
        )

        press_result = self.session.press("Enter")
        self.assertTrue(press_result["success"], press_result)
        self.assertEqual(backend.calls[-1], ("press", "Enter"))

        # One lazy browser/backend and one worker thread for the whole flow.
        self.assertEqual(len(self.backends), 1)
        self.assertEqual(backend.start_count, 1)
        self.assertEqual(len(backend.thread_names), 1)

    def test_navigation_reuses_page_and_backend(self):
        backend = self._navigate()
        second = self.session.navigate("https://example.org/", timeout=6.0)
        self.assertTrue(second["success"], second)
        self.assertEqual(len(self.backends), 1)
        self.assertEqual(backend.start_count, 1)
        self.assertEqual(
            backend.calls[-1],
            ("navigate", "https://example.org/", 6.0),
        )

    def test_timeout_is_bounded_and_never_starts_playwright(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        class BlockingBackend(FakeBackend):
            def start(self):
                self._record("start")
                self.start_count += 1
                entered.set()
                release.wait(5.0)
                finished.set()

        created = []
        session = BrowserSession(
            backend_factory=_factory(created, BlockingBackend),
            queue_timeout=0.2,
        )
        try:
            started = time.monotonic()
            result = session.navigate("https://example.com/", timeout=0.1)
            elapsed = time.monotonic() - started
            self.assertEqual(result["code"], "timeout")
            self.assertLess(elapsed, 2.0)
            self.assertTrue(entered.wait(2.0))
            release.set()
            self.assertTrue(finished.wait(2.0))
            self.assertTrue(session.close()["success"])
        finally:
            release.set()
            session.close()
        self.assertFalse(session.is_running)

    def test_unexpected_backend_error_recovers_and_does_not_leak_text(self):
        state = {"fail": True}

        class FlakyBackend(FakeBackend):
            def navigate(self, url, timeout):
                self._record("navigate", url, timeout)
                if state["fail"]:
                    state["fail"] = False
                    raise RuntimeError("SECRET PAGE TEXT")
                return {"url": url, "title": "Recovered"}

        created = []
        session = BrowserSession(
            backend_factory=_factory(created, FlakyBackend),
            queue_timeout=5.0,
        )
        try:
            failed = session.navigate("https://example.com/", timeout=2.0)
            self.assertFalse(failed["success"])
            self.assertIn(failed["code"], {"internal_error", "closed"})
            self.assertNotIn("SECRET", failed["message"])
            self.assertEqual(failed["data"], {})
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].stop_count, 1)

            recovered = session.navigate("https://example.com/", timeout=2.0)
            self.assertTrue(recovered["success"], recovered)
            self.assertEqual(recovered["data"]["title"], "Recovered")
            self.assertEqual(len(created), 2)
            self.assertEqual(created[1].start_count, 1)

            read_result = session.read()
            self.assertTrue(read_result["success"], read_result)
            self.assertEqual(len(created), 2)
        finally:
            session.close()

    def test_expected_backend_errors_are_mapped(self):
        class CommandErrorBackend(FakeBackend):
            def navigate(self, url, timeout):
                self._record("navigate", url, timeout)
                raise BrowserCommandError("navigation_failed", "raw detail")

            def click(self, selector, timeout):
                self._record("click", selector, timeout)
                raise BrowserCommandError("action_failed", "raw detail")

        created = []
        session = BrowserSession(
            backend_factory=_factory(created, CommandErrorBackend),
            queue_timeout=5.0,
        )
        try:
            result = session.navigate("https://example.com/")
            self.assertEqual(result["code"], "navigation_failed")
            self.assertNotIn("raw detail", result["message"])
            self.assertEqual(len(created), 1)
            # Navigation failed, so interaction commands are not started.
            click_result = session.click("#go")
            self.assertEqual(click_result["code"], "not_started")
            session.close()
        finally:
            session.close()

    def test_close_stops_backend_and_thread(self):
        backend = self._navigate()
        worker = self.session._worker
        self.assertIsNotNone(worker)
        self.assertTrue(worker.daemon)
        self.assertTrue(worker.is_alive())
        result = self.session.close()
        self.assertTrue(result["success"], result)
        self.assertEqual(result["code"], "ok")
        self.assertEqual(backend.stop_count, 1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.session.is_running)
        self.assertEqual(self.session.read()["code"], "closed")


@unittest.skipUnless(NETWORK_AVAILABLE, "example.com is not reachable")
class LiveBrowserSessionTests(unittest.TestCase):
    def test_example_com_navigate_read_close(self):
        session = BrowserSession()
        try:
            navigation = session.navigate("https://example.com", timeout=20.0)
            if navigation["code"] in {"navigation_failed", "timeout"}:
                self.skipTest(
                    "live navigation unavailable: " + navigation["code"]
                )
            self.assertTrue(navigation["success"], navigation["message"])
            self.assertEqual(navigation["code"], "ok")
            self.assertTrue(
                navigation["data"]["url"].startswith("https://example.com")
            )
            self.assertIn("Example", navigation["data"]["title"])

            page = session.read(max_chars=5000, max_links=20)
            self.assertTrue(page["success"], page["message"])
            self.assertEqual(page["code"], "ok")
            self.assertIn("Example Domain", page["data"]["title"])
            self.assertTrue(page["data"]["text"].strip())
            self.assertLessEqual(len(page["data"]["text"]), 5000)
            self.assertLessEqual(len(page["data"]["links"]), 20)
            seen = set()
            for link in page["data"]["links"]:
                self.assertTrue(link["url"].startswith(("http://", "https://")))
                self.assertNotIn(link["url"], seen)
                seen.add(link["url"])
        finally:
            closed = session.close()
            self.assertTrue(closed["success"], closed)
        self.assertFalse(session.is_running)
        self.assertEqual(session.read()["code"], "closed")


if __name__ == "__main__":
    unittest.main()
