import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeTextSearch:
    def __init__(self):
        self.calls = []

    def search(self, folder, query, **kwargs):
        self.calls.append((folder, query, kwargs))
        return {
            "success": True,
            "message": "Found 1 matching line.",
            "results": [
                {"path": "notes.txt", "line": 3, "text": "needle"}
            ],
            "count": 1,
            "truncated": False,
        }


class GrepToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.text = FakeTextSearch()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            text_search_service=self.text,
        )

    def test_tool_requires_service(self):
        names = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertIn("grep_files", names)
        without = ToolRegistry(executor=Mock(), memory_store=self.memory)
        names = {
            item["function"]["name"]
            for item in without.available_tools()
        }
        self.assertNotIn("grep_files", names)

    def test_forwards_query_and_flags(self):
        result = self.registry.execute(
            "grep_files",
            {
                "folder": "Documents",
                "query": "needle",
                "limit": 10,
                "case_sensitive": True,
                "fixed_strings": True,
            },
        )
        self.assertTrue(result["success"])
        folder, query, kwargs = self.text.calls[0]
        self.assertEqual(folder, "Documents")
        self.assertEqual(query, "needle")
        self.assertEqual(kwargs["limit"], 10)
        self.assertTrue(kwargs["case_sensitive"])
        self.assertTrue(kwargs["fixed_strings"])

    def test_invalid_arguments_do_not_search(self):
        for arguments in (
            {"folder": "Documents"},
            {"folder": "Documents", "query": "x", "command": "run"},
            {"folder": "Documents", "query": "x" * 201},
        ):
            with self.subTest(arguments=arguments):
                result = self.registry.execute("grep_files", arguments)
                self.assertFalse(result["success"])
        self.assertEqual(self.text.calls, [])


if __name__ == "__main__":
    unittest.main()
