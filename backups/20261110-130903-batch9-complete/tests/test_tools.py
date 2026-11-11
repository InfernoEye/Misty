import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.actions.file_scope import UserFolderResolver
from app.brain.memory import MemoryStore
from app.brain.tools import FOLDERS, ToolRegistry
from app.brain.validator import parse_tool_arguments


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, intent):
        self.calls.append(intent)
        return {"success": True, "message": "Test action accepted."}


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.executor = FakeExecutor()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.registry = ToolRegistry(
            self.executor, memory_store=self.memory
        )

    def test_open_downloads(self):
        result = self.registry.execute("open_folder", {"folder": "Downloads"})
        self.assertTrue(result["success"])
        self.assertEqual(
            self.executor.calls[-1]["arguments"]["path"],
            FOLDERS["Downloads"]
        )

    def test_open_documents(self):
        self.registry.execute("open_folder", {"folder": "Documents"})
        self.assertEqual(
            self.executor.calls[-1]["arguments"]["path"],
            "~/Documents"
        )

    def test_reject_unknown_folder(self):
        result = self.registry.execute("open_folder", {"folder": "/etc"})
        self.assertFalse(result["success"])
        self.assertEqual(len(self.executor.calls), 0)

    def test_reject_extra_arguments(self):
        result = self.registry.execute(
            "open_folder",
            {"folder": "Downloads", "command": "rm -rf"}
        )
        self.assertFalse(result["success"])
        self.assertEqual(len(self.executor.calls), 0)

    def test_reject_unknown_tool(self):
        result = self.registry.execute("run_shell", {"command": "whoami"})
        self.assertFalse(result["success"])
        self.assertEqual(len(self.executor.calls), 0)

    def test_volume_up(self):
        result = self.registry.execute("volume_up", {})
        self.assertTrue(result["success"])
        self.assertEqual(self.executor.calls[-1]["intent"], "volume_up")

    def test_reject_arguments_for_volume(self):
        result = self.registry.execute("volume_up", {"amount": 100})
        self.assertFalse(result["success"])
        self.assertEqual(len(self.executor.calls), 0)

    def test_scoped_search_forwards_resolved_root(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            downloads = home / "Pobrane"
            downloads.mkdir()
            resolver = UserFolderResolver(
                home=home, xdg_lookup=lambda _name: None
            )
            service = Mock()
            service.search.return_value = {"success": True}
            registry = ToolRegistry(
                executor=self.executor,
                memory_store=self.memory,
                file_search_service=service,
                folder_resolver=resolver,
            )

            result = registry.execute(
                "search_files",
                {"query": "report", "folder": "Downloads"},
            )

        self.assertTrue(result["success"])
        service.search.assert_called_once_with(
            "report", 30, root=downloads.resolve()
        )

    def test_scoped_search_rejects_unsafe_scope_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "Pobrane").mkdir()
            resolver = UserFolderResolver(
                home=home, xdg_lookup=lambda _name: None
            )
            service = Mock()
            registry = ToolRegistry(
                executor=self.executor,
                memory_store=self.memory,
                file_search_service=service,
                folder_resolver=resolver,
            )

            unsafe = registry.execute(
                "search_files",
                {
                    "query": "report",
                    "folder": "Downloads",
                    "relative_path": "../Documents",
                },
            )
            missing_folder = registry.execute(
                "search_files",
                {"query": "report", "relative_path": "Reports"},
            )

        self.assertFalse(unsafe["success"])
        self.assertFalse(missing_folder["success"])
        service.search.assert_not_called()

    def test_parse_json_arguments(self):
        result = parse_tool_arguments('{"folder":"Downloads"}')
        self.assertEqual(result, {"folder": "Downloads"})

    def test_reject_invalid_json(self):
        with self.assertRaises(ValueError):
            parse_tool_arguments('{"folder":')

    def test_reject_json_array(self):
        with self.assertRaises(ValueError):
            parse_tool_arguments('["Downloads"]')


if __name__ == "__main__":
    unittest.main()
