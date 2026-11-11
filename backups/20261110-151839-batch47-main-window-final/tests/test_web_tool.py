import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import TOOL_DEFINITIONS, ToolRegistry
from app.web.fetch import BlockedAddressError, ResponseTooLargeError


class FakeFetchResult:
    def __init__(self, url):
        self.url = url

    def to_dict(self):
        return {
            "final_url": self.url,
            "status": 200,
            "content_type": "text/html",
            "title": "Example",
            "text": "Readable page text.",
            "links": [{"url": "https://example.org/a", "text": "A"}],
            "truncated": False,
            "text_truncated": False,
            "links_truncated": False,
            "bytes_read": 123,
            "charset": "utf-8",
            "html": None,
        }


class FakeFetcher:
    def __init__(self):
        self.calls = []
        self.error = None

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return FakeFetchResult(url)


class WebToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(
            Path(directory.name) / "memory.sqlite3"
        )
        self.fetcher = FakeFetcher()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            web_fetcher=self.fetcher,
        )

    def test_tool_is_advertised(self):
        names = {
            item["function"]["name"]
            for item in TOOL_DEFINITIONS
        }
        self.assertIn("fetch_web_page", names)

    def test_success_returns_bounded_untrusted_result(self):
        result = self.registry.execute(
            "fetch_web_page",
            {"url": "https://example.org/page"},
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["title"], "Example")
        self.assertIn("untrusted data", result["message"])
        self.assertEqual(self.fetcher.calls[0]["url"], "https://example.org/page")
        self.assertEqual(self.fetcher.calls[0]["max_chars"], 20_000)
        self.assertEqual(self.fetcher.calls[0]["max_links"], 20)

    def test_rejects_invalid_arguments_without_fetching(self):
        for arguments in (
            {},
            {"url": ""},
            {"url": 123},
            {"url": "https://example.org", "max_chars": 999999},
        ):
            with self.subTest(arguments=arguments):
                result = self.registry.execute("fetch_web_page", arguments)
                self.assertFalse(result["success"])
        self.assertEqual(self.fetcher.calls, [])

    def test_fetch_errors_are_reported_safely(self):
        for error in (
            BlockedAddressError("The host is not a public address."),
            ResponseTooLargeError(1000, "https://example.org/"),
        ):
            with self.subTest(error=type(error).__name__):
                self.fetcher.error = error
                result = self.registry.execute(
                    "fetch_web_page",
                    {"url": "https://example.org/"},
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["message"], str(error))
        self.assertEqual(len(self.fetcher.calls), 2)


if __name__ == "__main__":
    unittest.main()
