import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.actions.file_scope import UserFolderResolver
from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class DocumentToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "Documents").mkdir()
        (self.home / "Pobrane").mkdir()
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
        )

    def test_reads_markdown_inside_approved_folder(self):
        target = self.home / "Documents" / "note.md"
        target.write_text("# Title\n\nVisible body.", encoding="utf-8")

        result = self.registry.execute("read_document", {
            "folder": "Documents",
            "relative_path": "note.md",
        })

        self.assertTrue(result["success"], result)
        self.assertEqual(result["format"], "markdown")
        self.assertIn("Visible body.", result["text"])
        self.assertFalse(result["truncated"])

    def test_reads_html_without_scripts(self):
        target = self.home / "Documents" / "page.html"
        target.write_text(
            "<html><body><script>secret()</script><p>Hello</p></body></html>",
            encoding="utf-8",
        )

        result = self.registry.execute("read_document", {
            "folder": "Documents",
            "relative_path": "page.html",
        })

        self.assertTrue(result["success"], result)
        self.assertIn("Hello", result["text"])
        self.assertNotIn("secret", result["text"])

    def test_rejects_traversal_and_absolute_paths(self):
        for relative in ("../secret.txt", "/etc/passwd"):
            with self.subTest(relative=relative):
                result = self.registry.execute("read_document", {
                    "folder": "Documents",
                    "relative_path": relative,
                })
                self.assertFalse(result["success"])

    def test_invalid_pdf_reports_conversion_failure(self):
        target = self.home / "Documents" / "scan.pdf"
        target.write_bytes(b"%PDF-1.4")

        result = self.registry.execute("read_document", {
            "folder": "Documents",
            "relative_path": "scan.pdf",
        })

        self.assertFalse(result["success"])
        self.assertIn(
            result["error"],
            {"conversion_failed", "unsupported_format"},
        )

    def test_unsupported_format_is_reported(self):
        target = self.home / "Documents" / "archive.bin"
        target.write_bytes(b"not a document")

        result = self.registry.execute("read_document", {
            "folder": "Documents",
            "relative_path": "archive.bin",
        })

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "unsupported_format")

    def test_output_is_truncated_at_tool_limit(self):
        target = self.home / "Documents" / "long.txt"
        target.write_text("x" * 25_000, encoding="utf-8")

        result = self.registry.execute("read_document", {
            "folder": "Documents",
            "relative_path": "long.txt",
        })

        self.assertTrue(result["success"], result)
        self.assertEqual(len(result["text"]), 20_000)
        self.assertTrue(result["truncated"])

    def test_rejects_invalid_arguments(self):
        for arguments in (
            {},
            {"folder": "Documents"},
            {"folder": "Secret", "relative_path": "x.txt"},
            {"folder": "Documents", "relative_path": ""},
            {"folder": "Documents", "relative_path": "x.txt", "command": "cat"},
        ):
            with self.subTest(arguments=arguments):
                result = self.registry.execute("read_document", arguments)
                self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
