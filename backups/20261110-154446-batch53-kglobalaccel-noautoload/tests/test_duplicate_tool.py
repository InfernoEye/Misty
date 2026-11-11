import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeDuplicates:
    def __init__(self):
        self.calls = []

    def find(self, folder, **kwargs):
        self.calls.append((folder, kwargs))
        return {
            "success": True,
            "groups": [],
            "duplicate_groups": 0,
            "message": "none",
        }


class DuplicateToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.finder = FakeDuplicates()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            duplicate_finder=self.finder,
        )

    def test_tool_requires_finder(self):
        names = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertIn("find_duplicates", names)
        without = ToolRegistry(executor=Mock(), memory_store=self.memory)
        names = {
            item["function"]["name"]
            for item in without.available_tools()
        }
        self.assertNotIn("find_duplicates", names)

    def test_forwards_options(self):
        result = self.registry.execute(
            "find_duplicates",
            {
                "folder": "Downloads",
                "relative_path": "media",
                "min_bytes": 100,
                "max_groups": 5,
            },
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.finder.calls[0][0], "Downloads")
        self.assertEqual(self.finder.calls[0][1]["relative_path"], "media")
        self.assertEqual(self.finder.calls[0][1]["min_bytes"], 100)

    def test_invalid_arguments_do_not_scan(self):
        for arguments in (
            {"relative_path": "x"},
            {"folder": "Downloads", "command": "run"},
        ):
            with self.subTest(arguments=arguments):
                self.assertFalse(
                    self.registry.execute("find_duplicates", arguments)["success"]
                )
        self.assertEqual(self.finder.calls, [])


if __name__ == "__main__":
    unittest.main()
