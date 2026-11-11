import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeSearchClient:
    def __init__(self):
        self.calls = []
        self.result = {
            "success": True,
            "query": "misty",
            "results": [
                {
                    "title": "Misty",
                    "url": "https://example.org/misty",
                    "snippet": "A local assistant.",
                },
            ],
            "answers": [],
            "count": 1,
            "limit": 5,
            "message": "ok",
        }

    def search(self, query, limit=None, categories=None, language=None):
        self.calls.append({
            "query": query,
            "limit": limit,
            "categories": categories,
            "language": language,
        })
        return self.result


class WebSearchToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(
            Path(directory.name) / "memory.sqlite3"
        )
        self.client = FakeSearchClient()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            web_search_client=self.client,
        )

    def test_tool_is_advertised_only_when_configured(self):
        without = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
        )
        names_without = {
            item["function"]["name"]
            for item in without.available_tools()
        }
        names_with = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertNotIn("web_search", names_without)
        self.assertIn("web_search", names_with)

    def test_search_forwards_bounded_arguments(self):
        result = self.registry.execute("web_search", {
            "query": "  misty  ",
            "limit": 3,
            "language": "en",
            "categories": "general",
        })

        self.assertTrue(result["success"])
        self.assertEqual(self.client.calls, [{
            "query": "misty",
            "limit": 3,
            "language": "en",
            "categories": "general",
        }])

    def test_rejects_invalid_arguments_without_calling_service(self):
        for arguments in (
            {},
            {"query": ""},
            {"query": "x" * 301},
            {"query": "misty", "limit": 0},
            {"query": "misty", "limit": 11},
            {"query": "misty", "command": "run"},
            {"query": "misty", "language": 5},
        ):
            with self.subTest(arguments=arguments):
                result = self.registry.execute("web_search", arguments)
                self.assertFalse(result["success"])
        self.assertEqual(self.client.calls, [])

    def test_service_failure_is_returned_without_content(self):
        self.client.result = {
            "success": False,
            "message": "The local search service is unavailable.",
            "error": "network_error",
        }

        result = self.registry.execute("web_search", {"query": "misty"})

        self.assertFalse(result["success"])
        self.assertNotIn("misty", result["message"])
        self.assertEqual(result["error"], "network_error")


if __name__ == "__main__":
    unittest.main()
