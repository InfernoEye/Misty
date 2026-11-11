import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry
from app.web.fetch import BlockedAddressError


class FakeSearch:
    def __init__(self):
        self.queries = []
        self.result = {
            "success": True,
            "results": [
                {"title": "One", "url": "https://example.org/one", "snippet": "one"},
                {"title": "Two", "url": "https://example.org/two", "snippet": "two"},
                {"title": "Three", "url": "https://example.org/three", "snippet": "three"},
                {"title": "Four", "url": "https://example.org/four", "snippet": "four"},
            ],
        }

    def search(self, query, limit=None, **kwargs):
        self.queries.append({"query": query, "limit": limit})
        return self.result


class FakePage:
    def __init__(self, url):
        self.title = "Page " + url.rsplit("/", 1)[-1]
        self.final_url = url
        self.text = "Readable text for " + url
        self.truncated = False


class FakeFetcher:
    def __init__(self):
        self.calls = []
        self.fail_urls = set()

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if url in self.fail_urls:
            raise BlockedAddressError("blocked")
        return FakePage(url)


class WebResearchToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.search = FakeSearch()
        self.fetcher = FakeFetcher()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            web_search_client=self.search,
            web_fetcher=self.fetcher,
        )

    def test_tool_requires_a_search_client(self):
        without_search = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
        )
        names = {
            item["function"]["name"]
            for item in without_search.available_tools()
        }
        self.assertNotIn("web_research", names)
        names = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertIn("web_research", names)

    def test_reads_top_pages_and_bounds_output(self):
        result = self.registry.execute(
            "web_research", {"query": "arch linux", "pages": 2}
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            [item["url"] for item in result["results"]],
            ["https://example.org/one", "https://example.org/two"],
        )
        self.assertEqual(self.search.queries[0]["limit"], 4)
        self.assertEqual(len(self.fetcher.calls), 2)
        self.assertIn("untrusted", result["message"])

    def test_fetch_failure_is_skipped_and_reported(self):
        self.fetcher.fail_urls.add("https://example.org/one")

        result = self.registry.execute(
            "web_research", {"query": "arch linux", "pages": 2}
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["fetch_errors"], 1)
        self.assertEqual(
            [item["url"] for item in result["results"]],
            ["https://example.org/two", "https://example.org/three"],
        )

    def test_invalid_arguments_do_not_search(self):
        for arguments in (
            {},
            {"query": ""},
            {"query": "x" * 301},
            {"query": "x", "pages": 0},
            {"query": "x", "pages": 6},
            {"query": "x", "command": "run"},
            {"query": "cool videos"},
        ):
            with self.subTest(arguments=arguments):
                result = self.registry.execute("web_research", arguments)
                self.assertFalse(result["success"])
        self.assertEqual(self.search.queries, [])


if __name__ == "__main__":
    unittest.main()
