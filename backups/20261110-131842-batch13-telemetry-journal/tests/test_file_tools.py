import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.actions.file_ops import FileOpsEngine
from app.actions.file_scope import UserFolderResolver
from app.brain.memory import MemoryStore
from app.brain.tools import TOOL_DEFINITIONS, ToolRegistry
from app.engine.voice_controller import VoiceController


class FileToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        (self.home / "Documents").mkdir()
        self.quarantine = Path(self.temp.name) / "quarantine"
        self.resolver = UserFolderResolver(
            home=self.home, xdg_lookup=lambda _name: None
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(
            Path(directory.name) / "memory.sqlite3"
        )
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            folder_resolver=self.resolver,
            file_ops_engine=FileOpsEngine(
                resolver=self.resolver,
                quarantine_root=self.quarantine,
            ),
        )

    def test_tools_are_advertised(self):
        names = {
            item["function"]["name"]
            for item in TOOL_DEFINITIONS
        }
        self.assertIn("write_text_file", names)
        self.assertIn("make_directory", names)
        self.assertIn("trash_path", names)

    def test_write_requires_confirmation(self):
        target = self.home / "Documents" / "note.txt"

        result = self.registry.execute("write_text_file", {
            "folder": "Documents",
            "relative_path": "note.txt",
            "content": "hello",
        })

        self.assertFalse(result["success"])
        self.assertIn("approval", result["message"].lower())
        self.assertFalse(target.exists())

    def test_confirmed_write_creates_file(self):
        target = self.home / "Documents" / "note.txt"

        result = self.registry.execute(
            "write_text_file",
            {
                "folder": "Documents",
                "relative_path": "note.txt",
                "content": "hello",
            },
            confirmed=True,
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(target.read_text(encoding="utf-8"), "hello")

    def test_overwrite_requires_flag(self):
        target = self.home / "Documents" / "note.txt"
        target.write_text("old", encoding="utf-8")

        refused = self.registry.execute(
            "write_text_file",
            {
                "folder": "Documents",
                "relative_path": "note.txt",
                "content": "new",
            },
            confirmed=True,
        )
        allowed = self.registry.execute(
            "write_text_file",
            {
                "folder": "Documents",
                "relative_path": "note.txt",
                "content": "new",
                "overwrite": True,
            },
            confirmed=True,
        )

        self.assertFalse(refused["success"])
        self.assertTrue(allowed["success"], allowed)
        self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_traversal_is_refused(self):
        result = self.registry.execute(
            "write_text_file",
            {
                "folder": "Documents",
                "relative_path": "../outside.txt",
                "content": "no",
            },
            confirmed=True,
        )

        self.assertFalse(result["success"])
        self.assertFalse((self.home / "outside.txt").exists())

    def test_mkdir_and_trash(self):
        made = self.registry.execute(
            "make_directory",
            {"folder": "Documents", "relative_path": "Projects"},
            confirmed=True,
        )
        folder = self.home / "Documents" / "Projects"
        self.assertTrue(made["success"], made)
        self.assertTrue(folder.is_dir())
        (folder / "note.txt").write_text("data", encoding="utf-8")

        trashed = self.registry.execute(
            "trash_path",
            {
                "folder": "Documents",
                "relative_path": "Projects",
                "recursive": True,
            },
            confirmed=True,
        )

        self.assertTrue(trashed["success"], trashed)
        self.assertFalse(folder.exists())
        self.assertTrue(self.quarantine.is_dir())

    def test_confirmation_arguments_never_include_content(self):
        write_safe = VoiceController._confirmation_arguments(
            "write_text_file",
            {
                "folder": "Documents",
                "relative_path": "note.txt",
                "content": "very private text",
                "overwrite": True,
            },
        )
        clipboard_safe = VoiceController._confirmation_arguments(
            "write_clipboard", {"text": "private clipboard"}
        )

        self.assertEqual(write_safe["content_chars"], 17)
        self.assertNotIn("content", write_safe)
        self.assertEqual(clipboard_safe, {"text_chars": 17})


if __name__ == "__main__":
    unittest.main()
