"""Tests for the isolated, read-only document extraction module.

Every fixture lives under a :class:`tempfile.TemporaryDirectory`; no test
reads the user's real home directory. Files are created with explicit
encodings so the tests exercise the documented decode fallback.
"""

import tempfile
import unittest
from pathlib import Path

from app.documents import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_CHARS,
    SUPPORTED_EXTENSIONS,
    DocumentReader,
    DocumentReaderError,
)


class DocumentReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        base = Path(self.temp.name)
        self.root = base / "root"
        self.root.mkdir()
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text(
            "OUTSIDE SECRET", encoding="utf-8"
        )

        (self.root / "notes.txt").write_text(
            "Hello Misty.\nSecond line.\n", encoding="utf-8"
        )
        (self.root / "readme.md").write_text(
            "# Title\n\nSome **bold** text.\n", encoding="utf-8"
        )
        (self.root / "page.html").write_text(
            """<!DOCTYPE html>
<html>
  <head>
    <title>Ignored title</title>
    <style>.secret { color: red; }</style>
    <script>window.alert("bad");</script>
  </head>
  <body>
    <h1>Visible heading</h1>
    <p>Body text &amp; more</p>
    <noscript>noscript fallback</noscript>
    <template><p>template content</p></template>
    <p>Final paragraph</p>
  </body>
</html>
""",
            encoding="utf-8",
        )
        (self.root / "page.htm").write_text(
            "<html><body><p>Small page</p></body></html>",
            encoding="utf-8",
        )
        (self.root / "upper.TXT").write_text(
            "Uppercase extension", encoding="utf-8"
        )
        (self.root / "empty.txt").write_text("", encoding="utf-8")
        (self.root / "bom.txt").write_bytes(
            b"\xef\xbb\xbfBOM text"
        )
        (self.root / "fallback.txt").write_bytes(
            "Café déjà vu — £5".encode("cp1252")
        )
        (self.root / "latin1.txt").write_bytes(b"Latin-1 \x81 fallback")
        (self.root / "binary.txt").write_bytes(b"text\x00more")
        (self.root / "control.txt").write_bytes(b"\x01\x02\x03\x04" * 100)
        (self.root / "big.txt").write_bytes(b"x" * 17)
        (self.root / "truncate.txt").write_text(
            "abcdefghijklmnopqrstuvwxyz", encoding="utf-8"
        )
        (self.root / "manual.pdf").write_text(
            "%PDF-1.4 not really a PDF", encoding="utf-8"
        )
        (self.root / "data.bin").write_text(
            "not a supported extension", encoding="utf-8"
        )
        (self.root / "folder").mkdir()

        self.reader = DocumentReader(self.root)

    def _make_symlink(self, link, target):
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks are not supported in this environment.")

    def test_reads_txt(self):
        result = self.reader.read("notes.txt")

        self.assertTrue(result.ok)
        self.assertEqual(result.format, "text")
        self.assertEqual(result.text, "Hello Misty.\nSecond line.\n")
        self.assertEqual(result.encoding, "utf-8")
        self.assertEqual(
            result.source_bytes,
            len("Hello Misty.\nSecond line.\n".encode("utf-8")),
        )
        self.assertFalse(result.truncated)
        self.assertFalse(result.text_truncated)
        self.assertIsNone(result.error)
        self.assertEqual(result.as_dict()["text"], result.text)

    def test_reads_markdown_without_altering_markup(self):
        result = self.reader.read("readme.md")

        self.assertTrue(result.ok)
        self.assertEqual(result.format, "markdown")
        self.assertEqual(result.text, "# Title\n\nSome **bold** text.\n")

    def test_reads_html_and_strips_scripts_and_styles(self):
        result = self.reader.read("page.html")

        self.assertTrue(result.ok)
        self.assertEqual(result.format, "html")
        self.assertIn("Visible heading", result.text)
        self.assertIn("Body text & more", result.text)
        self.assertIn("Final paragraph", result.text)
        for forbidden in (
            "window.alert",
            "color: red",
            "noscript fallback",
            "template content",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, result.text)
        self.assertNotIn("\x00", result.text)

    def test_reads_htm_extension(self):
        result = self.reader.read("page.htm")

        self.assertTrue(result.ok)
        self.assertEqual(result.format, "html")
        self.assertEqual(result.text, "Small page")

    def test_extension_is_case_insensitive(self):
        result = self.reader.read("upper.TXT")

        self.assertTrue(result.ok)
        self.assertEqual(result.format, "text")
        self.assertEqual(result.text, "Uppercase extension")

    def test_empty_file_is_readable(self):
        result = self.reader.read("empty.txt")

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "")
        self.assertEqual(result.source_bytes, 0)
        self.assertFalse(result.truncated)

    def test_utf8_bom_is_stripped(self):
        result = self.reader.read("bom.txt")

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "BOM text")
        self.assertEqual(result.encoding, "utf-8-sig")

    def test_falls_back_to_cp1252_for_legacy_text(self):
        result = self.reader.read("fallback.txt")

        self.assertTrue(result.ok)
        self.assertEqual(result.encoding, "cp1252")
        self.assertEqual(result.text, "Café déjà vu — £5")

    def test_falls_back_to_latin1_for_undecodable_legacy_text(self):
        result = self.reader.read("latin1.txt")

        self.assertTrue(result.ok)
        self.assertEqual(result.encoding, "latin-1")
        self.assertEqual(result.text, "Latin-1 \x81 fallback")

    def test_accepts_absolute_path_inside_root(self):
        absolute = self.root / "notes.txt"

        result = self.reader.read(str(absolute))

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Hello Misty.\nSecond line.\n")

    def test_accepts_pathlib_path_inside_root(self):
        result = self.reader.read(self.root / "notes.txt")

        self.assertTrue(result.ok)
        self.assertEqual(result.format, "text")

    def test_accepts_symlink_to_file_inside_root(self):
        link = self.root / "alias.txt"
        self._make_symlink(link, self.root / "notes.txt")

        result = self.reader.read("alias.txt")

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Hello Misty.\nSecond line.\n")

    def test_rejects_parent_traversal(self):
        for path in (
            "../outside/secret.txt",
            "folder/../../outside/secret.txt",
            str(self.root / "folder" / ".." / ".." / "outside" / "secret.txt"),
        ):
            with self.subTest(path=path):
                result = self.reader.read(path)
                self.assertFalse(result.ok)
                self.assertEqual(result.error, "path_not_allowed")
                self.assertEqual(result.text, "")
                self.assertIsNone(result.format)

    def test_rejects_absolute_path_outside_root(self):
        result = self.reader.read(str(self.outside / "secret.txt"))

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "path_not_allowed")
        self.assertEqual(result.text, "")
        self.assertNotIn(self.temp.name, result.message)

    def test_rejects_symlink_file_escape(self):
        link = self.root / "escape.txt"
        self._make_symlink(link, self.outside / "secret.txt")

        result = self.reader.read("escape.txt")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "path_not_allowed")
        self.assertEqual(result.text, "")

    def test_rejects_symlink_directory_escape(self):
        link = self.root / "escape-dir"
        self._make_symlink(link, self.outside)

        result = self.reader.read("escape-dir/secret.txt")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "path_not_allowed")
        self.assertEqual(result.text, "")

    def test_rejects_missing_file(self):
        result = self.reader.read("missing.txt")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "path_not_found")
        self.assertEqual(result.text, "")

    def test_rejects_directory(self):
        result = self.reader.read("folder")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_a_file")
        self.assertEqual(result.text, "")

    def test_rejects_oversized_file_without_reading_it(self):
        reader = DocumentReader(self.root, max_bytes=16)

        result = reader.read("big.txt")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "file_too_large")
        self.assertEqual(result.text, "")
        self.assertEqual(result.source_bytes, 17)
        self.assertEqual(result.format, "text")

    def test_rejects_nul_bytes_as_binary(self):
        result = self.reader.read("binary.txt")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "binary_content")
        self.assertEqual(result.text, "")

    def test_rejects_control_heavy_content_as_binary(self):
        result = self.reader.read("control.txt")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "binary_content")
        self.assertEqual(result.text, "")

    def test_truncates_output_and_sets_flags(self):
        reader = DocumentReader(self.root, max_chars=5)

        result = reader.read("truncate.txt")

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "abcde")
        self.assertEqual(len(result.text), 5)
        self.assertTrue(result.text_truncated)
        self.assertTrue(result.truncated)
        self.assertEqual(result.source_bytes, 26)

    def test_exact_output_limit_is_not_truncated(self):
        reader = DocumentReader(self.root, max_chars=26)

        result = reader.read("truncate.txt")

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "abcdefghijklmnopqrstuvwxyz")
        self.assertFalse(result.text_truncated)
        self.assertFalse(result.truncated)

    def test_invalid_pdf_returns_clear_conversion_failure(self):
        result = self.reader.read("manual.pdf")

        self.assertFalse(result.ok)
        self.assertIn(
            result.error,
            {"conversion_failed", "unsupported_format"},
        )
        self.assertEqual(result.text, "")
        self.assertEqual(result.format, "pdf")
        self.assertNotIn(str(self.root), result.message)

    def test_unsupported_extension_returns_clear_result(self):
        result = self.reader.read("data.bin")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "unsupported_format")
        self.assertIsNone(result.format)
        self.assertTrue(result.message)

    def test_rejects_invalid_path_values(self):
        for path in (None, b"notes.txt", 42, "", "notes\x00.txt"):
            with self.subTest(path=path):
                result = self.reader.read(path)
                self.assertFalse(result.ok)
                self.assertEqual(result.error, "path_not_allowed")
                self.assertEqual(result.text, "")

    def test_rejects_invalid_root(self):
        for root in (
            self.root / "missing",
            self.root / "notes.txt",
            None,
            "",
        ):
            with self.subTest(root=root):
                with self.assertRaises(DocumentReaderError):
                    DocumentReader(root)

    def test_rejects_invalid_limits(self):
        for value in (0, -1, True, 1.5, "10", None):
            with self.subTest(limit=value):
                with self.assertRaises(DocumentReaderError):
                    DocumentReader(self.root, max_bytes=value)
                with self.assertRaises(DocumentReaderError):
                    DocumentReader(self.root, max_chars=value)

    def test_defaults_and_supported_formats(self):
        reader = DocumentReader(self.root)

        self.assertEqual(reader.max_bytes, DEFAULT_MAX_BYTES)
        self.assertEqual(reader.max_chars, DEFAULT_MAX_CHARS)
        self.assertEqual(reader.allowed_root, self.root.resolve())
        self.assertEqual(
            SUPPORTED_EXTENSIONS,
            frozenset({".txt", ".md", ".html", ".htm"}),
        )


if __name__ == "__main__":
    unittest.main()
