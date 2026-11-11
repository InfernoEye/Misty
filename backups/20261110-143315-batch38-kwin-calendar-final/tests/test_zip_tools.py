import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeZip:
    def __init__(self):
        self.calls = []

    def preview_create(self, *args, **kwargs):
        return {
            "success": True,
            "preview": {
                "archive": "bundle.zip",
                "entries": 2,
                "bytes": 10,
            },
        }

    def preview_extract(self, *args, **kwargs):
        return {
            "success": True,
            "preview": {
                "entries": 2,
                "destination": "unpacked",
            },
        }

    def create(self, *args, confirmed=False, **kwargs):
        self.calls.append(("create", args, confirmed, kwargs))
        return {
            "success": True,
            "message": "created",
            "archive": "bundle.zip",
            "entries": 2,
        }

    def extract(self, *args, confirmed=False, **kwargs):
        self.calls.append(("extract", args, confirmed, kwargs))
        return {
            "success": True,
            "message": "extracted",
            "destination": "unpacked",
            "entries": 2,
        }


class ZipToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.zip = FakeZip()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            zip_engine=self.zip,
        )

    def test_tools_require_engine(self):
        names = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertIn("zip_create", names)
        self.assertIn("zip_extract", names)
        without = ToolRegistry(executor=Mock(), memory_store=self.memory)
        names = {
            item["function"]["name"]
            for item in without.available_tools()
        }
        self.assertNotIn("zip_create", names)

    def test_create_requires_confirmation(self):
        arguments = {
            "folder": "Documents",
            "relative_paths": ["a.txt", "b.txt"],
            "archive_name": "bundle.zip",
        }
        denied = self.registry.execute("zip_create", arguments)
        self.assertFalse(denied["success"])
        self.assertEqual(self.zip.calls, [])

        result = self.registry.execute(
            "zip_create", arguments, confirmed=True
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.zip.calls[0][0], "create")
        self.assertTrue(self.zip.calls[0][2])

    def test_extract_forwards_destination(self):
        result = self.registry.execute(
            "zip_extract",
            {
                "folder": "Downloads",
                "archive_relative_path": "bundle.zip",
                "destination_folder": "Documents",
                "destination_relative_path": "unpacked",
            },
            confirmed=True,
        )
        self.assertTrue(result["success"])
        self.assertEqual(
            self.zip.calls[0][3]["destination_relative_path"], "unpacked"
        )

    def test_confirmation_descriptions(self):
        create_text = self.registry.confirmation_description(
            "zip_create",
            {
                "folder": "Documents",
                "relative_paths": ["a.txt"],
                "archive_name": "bundle.zip",
            },
        )
        self.assertIn("Create bundle.zip from 2 file(s)", create_text)

        extract_text = self.registry.confirmation_description(
            "zip_extract",
            {
                "folder": "Downloads",
                "archive_relative_path": "bundle.zip",
                "destination_folder": "Documents",
            },
        )
        self.assertIn("Extract 2 file(s) into unpacked", extract_text)

    def test_invalid_arguments_do_not_reach_engine(self):
        for name, arguments in (
            ("zip_create", {"folder": "Documents"}),
            ("zip_extract", {"folder": "Downloads"}),
        ):
            with self.subTest(name=name):
                result = self.registry.execute(
                    name, arguments, confirmed=True
                )
                self.assertFalse(result["success"])
        self.assertEqual(self.zip.calls, [])


if __name__ == "__main__":
    unittest.main()
