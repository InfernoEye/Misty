import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeTransfer:
    def __init__(self):
        self.calls = []

    def _result(self, action, success=True):
        return {
            "success": success,
            "action": action,
            "message": f"{action} result",
            "code": None,
            "source": "a.txt",
            "destination": "b.txt",
            "preview": {
                "source": "a.txt",
                "destination": "b.txt",
                "bytes": 5,
            },
        }

    def preview_copy(self, *args, **kwargs):
        return self._result("preview_copy")

    def preview_move(self, *args, **kwargs):
        return self._result("preview_move")

    def preview_rename(self, *args, **kwargs):
        return self._result("preview_rename")

    def copy(self, *args, confirmed=False, **kwargs):
        self.calls.append(("copy", args, confirmed, kwargs))
        return self._result("copy")

    def move(self, *args, confirmed=False, **kwargs):
        self.calls.append(("move", args, confirmed, kwargs))
        return self._result("move")

    def rename(self, *args, confirmed=False, **kwargs):
        self.calls.append(("rename", args, confirmed, kwargs))
        return self._result("rename")


class FileTransferToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.transfer = FakeTransfer()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            file_transfer_engine=self.transfer,
        )

    def test_tools_are_advertised_with_engine(self):
        names = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertTrue({"copy_path", "move_path", "rename_path"} <= names)
        without = ToolRegistry(
            executor=Mock(), memory_store=self.memory
        )
        names = {
            item["function"]["name"]
            for item in without.available_tools()
        }
        self.assertNotIn("copy_path", names)

    def test_copy_requires_confirmation_and_forwards(self):
        denied = self.registry.execute(
            "copy_path",
            {
                "folder": "Downloads",
                "relative_path": "a.txt",
                "destination_folder": "Documents",
            },
        )
        self.assertFalse(denied["success"])
        self.assertEqual(self.transfer.calls, [])

        result = self.registry.execute(
            "copy_path",
            {
                "folder": "Downloads",
                "relative_path": "a.txt",
                "destination_folder": "Documents",
                "destination_relative_path": "b.txt",
            },
            confirmed=True,
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.transfer.calls[0][0], "copy")
        self.assertFalse(self.transfer.calls[0][3].get("overwrite", False))
        self.assertEqual(self.transfer.calls[0][2], True)

    def test_rename_forwards_exactly(self):
        result = self.registry.execute(
            "rename_path",
            {
                "folder": "Documents",
                "relative_path": "a.txt",
                "new_name": "b.txt",
            },
            confirmed=True,
        )
        self.assertTrue(result["success"])
        self.assertEqual(
            self.transfer.calls[0][0], "rename"
        )

    def test_confirmation_description_is_content_free(self):
        text = self.registry.confirmation_description(
            "copy_path",
            {
                "folder": "Downloads",
                "relative_path": "a.txt",
                "destination_folder": "Documents",
                "destination_relative_path": "b.txt",
            },
        )
        self.assertIn("Copy a.txt to b.txt", text)
        self.assertIn("5 bytes", text)

    def test_invalid_arguments_do_not_reach_engine(self):
        result = self.registry.execute(
            "copy_path",
            {"folder": "Downloads", "relative_path": "a.txt"},
            confirmed=True,
        )
        self.assertFalse(result["success"])
        self.assertEqual(self.transfer.calls, [])


if __name__ == "__main__":
    unittest.main()
