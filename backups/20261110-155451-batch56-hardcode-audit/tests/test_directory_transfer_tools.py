import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeDirectoryTransfer:
    def __init__(self):
        self.calls = []

    def preview_copy(self, *args, **kwargs):
        return {
            "success": True,
            "preview": {"source": "tree", "destination": "copy"},
        }

    def preview_move(self, *args, **kwargs):
        return {
            "success": True,
            "preview": {"source": "tree", "destination": "moved"},
        }

    def copy(self, *args, confirmed=False, **kwargs):
        self.calls.append(("copy", args, confirmed, kwargs))
        return {"success": True, "message": "copied", "files": 2}

    def move(self, *args, confirmed=False, **kwargs):
        self.calls.append(("move", args, confirmed, kwargs))
        return {"success": True, "message": "moved", "files": 2}


class DirectoryTransferToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.engine = FakeDirectoryTransfer()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            directory_transfer_engine=self.engine,
        )

    def test_tools_require_engine(self):
        names = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertIn("copy_folder", names)
        self.assertIn("move_folder", names)
        without = ToolRegistry(executor=Mock(), memory_store=self.memory)
        names = {
            item["function"]["name"]
            for item in without.available_tools()
        }
        self.assertNotIn("copy_folder", names)

    def test_requires_confirmation_and_forwards(self):
        arguments = {
            "folder": "Documents",
            "relative_path": "tree",
            "destination_folder": "Documents",
            "destination_relative_path": "copy",
        }
        denied = self.registry.execute("copy_folder", arguments)
        self.assertFalse(denied["success"])
        self.assertEqual(self.engine.calls, [])

        result = self.registry.execute(
            "copy_folder", arguments, confirmed=True
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.engine.calls[0][0], "copy")
        self.assertTrue(self.engine.calls[0][2])

    def test_move_forwards(self):
        result = self.registry.execute(
            "move_folder",
            {
                "folder": "Documents",
                "relative_path": "tree",
                "destination_folder": "Documents",
                "destination_relative_path": "moved",
            },
            confirmed=True,
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.engine.calls[0][0], "move")

    def test_confirmation_descriptions(self):
        copy_text = self.registry.confirmation_description(
            "copy_folder",
            {
                "folder": "Documents",
                "relative_path": "tree",
                "destination_folder": "Documents",
                "destination_relative_path": "copy",
            },
        )
        self.assertIn("Copy folder tree to copy", copy_text)
        move_text = self.registry.confirmation_description(
            "move_folder",
            {
                "folder": "Documents",
                "relative_path": "tree",
                "destination_folder": "Documents",
                "destination_relative_path": "moved",
            },
        )
        self.assertIn("Move folder tree to moved", move_text)

    def test_invalid_arguments_do_not_reach_engine(self):
        result = self.registry.execute(
            "copy_folder",
            {"folder": "Documents", "relative_path": "tree"},
            confirmed=True,
        )
        self.assertFalse(result["success"])
        self.assertEqual(self.engine.calls, [])


if __name__ == "__main__":
    unittest.main()
