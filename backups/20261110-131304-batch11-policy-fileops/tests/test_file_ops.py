"""Tests for the isolated, safety-first file operations engine.

Every test uses a temporary home and a temporary quarantine root. No test
touches the real user home, the network, a model, or the registry.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.actions.file_ops import (
    MAX_CONTENT_BYTES,
    MAX_DEPTH,
    MAX_PATH_LENGTH,
    FileOpsEngine,
)
from app.actions.file_scope import UserFolderResolver


class FileOpsEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "Documents").mkdir()
        (self.home / "Pobrane").mkdir()
        self.quarantine = self.home / "quarantine"
        self.quarantine.mkdir()
        self.resolver = UserFolderResolver(
            home=self.home,
            xdg_lookup=lambda _name: None,
        )
        self.engine = FileOpsEngine(
            resolver=self.resolver,
            quarantine_root=self.quarantine,
        )
        self.docs = self.home / "Documents"

    # ------------------------------------------------------------------
    # Confirmation gating
    # ------------------------------------------------------------------

    def test_write_requires_confirmation_and_preview(self):
        result = self.engine.write_text("Documents", "note.txt", "hello")

        self.assertFalse(result["success"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["error_code"], "confirmation_required")
        self.assertEqual(result["target"], "note.txt")
        self.assertIsNotNone(result["preview"])
        self.assertEqual(result["preview"]["content"], "hello")
        self.assertFalse((self.docs / "note.txt").exists())

        preview = self.engine.preview_write("Documents", "note.txt", "hello")
        self.assertTrue(preview["success"])
        self.assertFalse(preview["requires_confirmation"])
        self.assertFalse((self.docs / "note.txt").exists())

    def test_mkdir_requires_confirmation_and_preview(self):
        result = self.engine.mkdir("Documents", "newdir")

        self.assertFalse(result["success"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["error_code"], "confirmation_required")
        self.assertEqual(result["target"], "newdir")
        self.assertIsNotNone(result["preview"])
        self.assertFalse((self.docs / "newdir").exists())

        preview = self.engine.preview_mkdir("Documents", "newdir")
        self.assertTrue(preview["success"])
        self.assertFalse(preview["requires_confirmation"])
        self.assertFalse((self.docs / "newdir").exists())

    def test_trash_requires_confirmation_and_preview(self):
        target = self.docs / "note.txt"
        target.write_text("hello")

        result = self.engine.trash("Documents", "note.txt")

        self.assertFalse(result["success"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["error_code"], "confirmation_required")
        self.assertEqual(result["target"], "note.txt")
        self.assertIsNotNone(result["preview"])
        self.assertEqual(result["preview"]["type"], "file")
        self.assertTrue(target.exists())

        preview = self.engine.preview_trash("Documents", "note.txt")
        self.assertTrue(preview["success"])
        self.assertFalse(preview["requires_confirmation"])
        self.assertTrue(target.exists())

    def test_trash_recursive_nonempty_requires_confirmation(self):
        full = self.docs / "full"
        full.mkdir()
        (full / "child.txt").write_text("child")

        result = self.engine.trash(
            "Documents", "full", recursive=True
        )
        self.assertTrue(result["requires_confirmation"])
        self.assertTrue(full.exists())

        refused = self.engine.trash(
            "Documents", "full", recursive=False, confirmed=True
        )
        self.assertEqual(refused["error_code"], "non_empty_directory")
        self.assertTrue(full.exists())

    # ------------------------------------------------------------------
    # write_text
    # ------------------------------------------------------------------

    def test_successful_write_and_read_back(self):
        result = self.engine.write_text(
            "Documents", "note.txt", "hello", confirmed=True
        )

        self.assertTrue(result["success"])
        self.assertFalse(result["requires_confirmation"])
        self.assertTrue(result["verified"])
        self.assertIsNone(result["error_code"])
        self.assertEqual(result["target"], "note.txt")
        self.assertEqual((self.docs / "note.txt").read_text(), "hello")
        self.assertEqual(
            {path.name for path in self.docs.iterdir()},
            {"note.txt"},
        )

    def test_overwrite_refusal_then_allowed(self):
        self.engine.write_text("Documents", "note.txt", "one", confirmed=True)

        refused = self.engine.write_text(
            "Documents", "note.txt", "two", confirmed=True
        )
        self.assertFalse(refused["success"])
        self.assertEqual(refused["error_code"], "target_exists")
        self.assertEqual((self.docs / "note.txt").read_text(), "one")

        allowed = self.engine.write_text(
            "Documents",
            "note.txt",
            "two",
            overwrite=True,
            confirmed=True,
        )
        self.assertTrue(allowed["success"])
        self.assertTrue(allowed["verified"])
        self.assertEqual((self.docs / "note.txt").read_text(), "two")

    def test_atomicity_no_temp_leftovers_on_failure(self):
        target = self.docs / "note.txt"
        target.write_text("one")
        before = {path.name for path in self.docs.iterdir()}

        with patch(
            "app.actions.file_ops.os.replace",
            side_effect=OSError("replace failed"),
        ):
            result = self.engine.write_text(
                "Documents",
                "note.txt",
                "two",
                overwrite=True,
                confirmed=True,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "write_failed")
        self.assertEqual(target.read_text(), "one")
        after = {path.name for path in self.docs.iterdir()}
        self.assertEqual(after, before)
        self.assertFalse(
            any(name.endswith(".tmp") for name in after)
        )

    def test_atomicity_no_temp_leftovers_on_success(self):
        self.engine.write_text("Documents", "note.txt", "one", confirmed=True)
        result = self.engine.write_text(
            "Documents",
            "note.txt",
            "two",
            overwrite=True,
            confirmed=True,
        )
        self.assertTrue(result["success"])
        names = {path.name for path in self.docs.iterdir()}
        self.assertEqual(names, {"note.txt"})
        self.assertFalse(any(name.endswith(".tmp") for name in names))

    def test_read_back_verification_failure_is_reported(self):
        with patch.object(
            self.engine,
            "_read_bytes",
            side_effect=[b"hello", b"wrong"],
        ):
            result = self.engine.write_text(
                "Documents", "note.txt", "hello", confirmed=True
            )

        self.assertFalse(result["success"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["error_code"], "verification_failed")
        self.assertEqual((self.docs / "note.txt").read_text(), "hello")

    def test_write_rejects_traversal_and_absolute_paths(self):
        for relative in (
            "../escape.txt",
            "a/../../escape.txt",
            "/etc/passwd",
            "//etc/passwd",
        ):
            with self.subTest(relative=relative):
                result = self.engine.write_text(
                    "Documents", relative, "x", confirmed=True
                )
                self.assertFalse(result["success"])
                self.assertIn(
                    result["error_code"],
                    {"traversal", "absolute_path"},
                )
        self.assertFalse((self.home / "escape.txt").exists())

    def test_write_rejects_symlink_target(self):
        outside = self.home / "outside.txt"
        outside.write_text("safe")
        (self.docs / "link.txt").symlink_to(outside)

        result = self.engine.write_text(
            "Documents",
            "link.txt",
            "overwritten",
            overwrite=True,
            confirmed=True,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "symlink_target")
        self.assertEqual(outside.read_text(), "safe")

    def test_write_rejects_symlink_parent(self):
        outside = self.home / "outside_dir"
        outside.mkdir()
        (self.docs / "linkdir").symlink_to(outside)

        result = self.engine.write_text(
            "Documents", "linkdir/note.txt", "x", confirmed=True
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "symlink_parent")
        self.assertFalse((outside / "note.txt").exists())

    def test_write_rejects_directory_and_non_regular_targets(self):
        (self.docs / "folder").mkdir()
        result = self.engine.write_text(
            "Documents", "folder", "x", confirmed=True
        )
        self.assertEqual(result["error_code"], "target_is_directory")

        fifo = self.docs / "fifo"
        os.mkfifo(fifo)
        result = self.engine.write_text(
            "Documents",
            "fifo",
            "x",
            overwrite=True,
            confirmed=True,
        )
        self.assertEqual(result["error_code"], "target_not_regular")
        self.assertTrue(fifo.exists())

    def test_write_rejects_oversized_and_invalid_content(self):
        oversized = "x" * (MAX_CONTENT_BYTES + 1)
        result = self.engine.write_text(
            "Documents", "big.txt", oversized, confirmed=True
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "content_too_large")
        self.assertFalse((self.docs / "big.txt").exists())

        result = self.engine.write_text(
            "Documents", "bytes.txt", b"bytes", confirmed=True
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "invalid_content")
        self.assertFalse((self.docs / "bytes.txt").exists())

    def test_write_rejects_missing_parent_and_parent_file(self):
        result = self.engine.write_text(
            "Documents", "missing/note.txt", "x", confirmed=True
        )
        self.assertEqual(result["error_code"], "missing_parent")

        (self.docs / "file.txt").write_text("file")
        result = self.engine.write_text(
            "Documents", "file.txt/note.txt", "x", confirmed=True
        )
        self.assertEqual(result["error_code"], "parent_not_directory")

    def test_write_rejects_path_too_long_and_too_deep(self):
        result = self.engine.write_text(
            "Documents",
            "a" * (MAX_PATH_LENGTH + 1),
            "x",
            confirmed=True,
        )
        self.assertEqual(result["error_code"], "path_too_long")

        result = self.engine.write_text(
            "Documents",
            "a" * 256,
            "x",
            confirmed=True,
        )
        self.assertEqual(result["error_code"], "path_too_long")

        deep = "/".join(["a"] * (MAX_DEPTH + 1))
        result = self.engine.write_text(
            "Documents", deep, "x", confirmed=True
        )
        self.assertEqual(result["error_code"], "path_too_deep")

    def test_write_rejects_trailing_slash_and_empty_path(self):
        result = self.engine.write_text(
            "Documents", "note.txt/", "x", confirmed=True
        )
        self.assertEqual(result["error_code"], "invalid_relative_path")
        self.assertFalse((self.docs / "note.txt").exists())

        result = self.engine.write_text(
            "Documents", "", "x", confirmed=True
        )
        self.assertEqual(result["error_code"], "empty_relative_path")

    # ------------------------------------------------------------------
    # mkdir
    # ------------------------------------------------------------------

    def test_mkdir_success_and_conflicts(self):
        result = self.engine.mkdir("Documents", "newdir", confirmed=True)
        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertTrue((self.docs / "newdir").is_dir())

        again = self.engine.mkdir("Documents", "newdir", confirmed=True)
        self.assertEqual(again["error_code"], "target_exists")

        (self.docs / "file.txt").write_text("file")
        conflict = self.engine.mkdir(
            "Documents", "file.txt", confirmed=True
        )
        self.assertEqual(conflict["error_code"], "target_is_file")

        outside = self.home / "mkdir_outside"
        outside.mkdir()
        (self.docs / "link").symlink_to(outside)
        link = self.engine.mkdir("Documents", "link", confirmed=True)
        self.assertEqual(link["error_code"], "symlink_target")

    def test_mkdir_parents_option(self):
        missing = self.engine.mkdir(
            "Documents", "p/q", parents=False, confirmed=True
        )
        self.assertEqual(missing["error_code"], "missing_parent")
        self.assertFalse((self.docs / "p").exists())

        created = self.engine.mkdir(
            "Documents", "p/q", parents=True, confirmed=True
        )
        self.assertTrue(created["success"])
        self.assertTrue((self.docs / "p" / "q").is_dir())

        again = self.engine.mkdir(
            "Documents", "p/q", parents=True, confirmed=True
        )
        self.assertEqual(again["error_code"], "target_exists")

        nested = self.engine.mkdir(
            "Documents", "p/q/r", parents=True, confirmed=True
        )
        self.assertTrue(nested["success"])
        self.assertTrue((self.docs / "p" / "q" / "r").is_dir())

    def test_mkdir_rejects_symlink_parent(self):
        outside = self.home / "mkdir_outside_parent"
        outside.mkdir()
        (outside / "new").mkdir()
        (self.docs / "linkdir").symlink_to(outside)

        result = self.engine.mkdir(
            "Documents", "linkdir/new", parents=True, confirmed=True
        )
        self.assertEqual(result["error_code"], "symlink_parent")
        self.assertTrue((outside / "new").is_dir())

    def test_mkdir_rejects_parent_file(self):
        (self.docs / "file.txt").write_text("file")
        result = self.engine.mkdir(
            "Documents", "file.txt/new", parents=True, confirmed=True
        )
        self.assertEqual(result["error_code"], "parent_not_directory")

    # ------------------------------------------------------------------
    # trash
    # ------------------------------------------------------------------

    def test_trash_file_and_manifest(self):
        target = self.docs / "note.txt"
        target.write_text("hello")

        result = self.engine.trash(
            "Documents", "note.txt", confirmed=True
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertIsNone(result["error_code"])
        self.assertFalse(target.exists())
        self.assertTrue(Path(result["quarantine_path"]).is_dir())
        self.assertTrue(Path(result["manifest_path"]).is_file())
        self.assertTrue(Path(result["trashed_path"]).is_file())
        self.assertEqual(
            Path(result["trashed_path"]).read_text(),
            "hello",
        )

        manifest = json.loads(
            Path(result["manifest_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["original_relative_path"], "note.txt")
        self.assertEqual(manifest["type"], "file")
        self.assertEqual(manifest["size"], 5)
        self.assertEqual(manifest["trashed_path"], result["trashed_path"])
        self.assertEqual(manifest["folder"], "Documents")
        self.assertIsInstance(manifest["time"], str)
        self.assertTrue(manifest["time"])
        self.assertEqual(manifest["original_name"], "note.txt")
        self.assertFalse(manifest["recursive"])
        self.assertIsNone(manifest["entry_count"])
        self.assertIn("quarantine_relative_path", manifest)
        self.assertEqual(
            Path(result["manifest_path"]).name, "manifest.json"
        )
        self.assertEqual(
            {path.name for path in Path(result["quarantine_path"]).iterdir()},
            {"item", "manifest.json"},
        )
        self.assertFalse(
            any(
                name.endswith(".tmp")
                for name in os.listdir(result["quarantine_path"])
            )
        )

    def test_trash_empty_directory(self):
        target = self.docs / "empty"
        target.mkdir()

        result = self.engine.trash(
            "Documents", "empty", confirmed=True
        )

        self.assertTrue(result["success"])
        self.assertFalse(target.exists())
        self.assertTrue(Path(result["trashed_path"]).is_dir())
        manifest = json.loads(
            Path(result["manifest_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["type"], "directory")
        self.assertEqual(manifest["entry_count"], 0)
        self.assertFalse(manifest["recursive"])
        self.assertFalse(manifest["non_empty"])

    def test_trash_non_empty_directory_refused_then_recursive(self):
        full = self.docs / "full"
        full.mkdir()
        (full / "child.txt").write_text("child")

        refused = self.engine.trash(
            "Documents", "full", confirmed=True
        )
        self.assertFalse(refused["success"])
        self.assertEqual(refused["error_code"], "non_empty_directory")
        self.assertTrue(full.exists())

        pending = self.engine.trash(
            "Documents", "full", recursive=True
        )
        self.assertTrue(pending["requires_confirmation"])
        self.assertTrue(full.exists())

        result = self.engine.trash(
            "Documents", "full", recursive=True, confirmed=True
        )
        self.assertTrue(result["success"])
        self.assertFalse(full.exists())
        trashed = Path(result["trashed_path"])
        self.assertTrue(trashed.is_dir())
        self.assertTrue((trashed / "child.txt").is_file())
        self.assertEqual(
            (trashed / "child.txt").read_text(), "child"
        )

        manifest = json.loads(
            Path(result["manifest_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["type"], "directory")
        self.assertEqual(manifest["entry_count"], 1)
        self.assertTrue(manifest["recursive"])

    def test_trash_does_not_overwrite_prior_quarantine_item(self):
        first = self.docs / "same.txt"
        first.write_text("one")
        first_result = self.engine.trash(
            "Documents", "same.txt", confirmed=True
        )

        second = self.docs / "same.txt"
        second.write_text("two")
        second_result = self.engine.trash(
            "Documents", "same.txt", confirmed=True
        )

        self.assertNotEqual(
            first_result["quarantine_path"],
            second_result["quarantine_path"],
        )
        self.assertEqual(
            Path(first_result["trashed_path"]).read_text(), "one"
        )
        self.assertEqual(
            Path(second_result["trashed_path"]).read_text(), "two"
        )
        self.assertTrue(Path(first_result["manifest_path"]).is_file())
        self.assertTrue(Path(second_result["manifest_path"]).is_file())

    def test_trash_rejects_symlink_and_missing_target(self):
        outside = self.home / "trash_outside.txt"
        outside.write_text("safe")
        (self.docs / "link.txt").symlink_to(outside)

        result = self.engine.trash(
            "Documents", "link.txt", confirmed=True
        )
        self.assertEqual(result["error_code"], "symlink_target")
        self.assertEqual(outside.read_text(), "safe")
        self.assertTrue((self.docs / "link.txt").is_symlink())

        missing = self.engine.trash(
            "Documents", "missing.txt", confirmed=True
        )
        self.assertEqual(missing["error_code"], "target_not_found")

    def test_trash_rejects_non_regular_target(self):
        fifo = self.docs / "fifo"
        os.mkfifo(fifo)

        result = self.engine.trash(
            "Documents", "fifo", confirmed=True
        )
        self.assertEqual(result["error_code"], "target_not_regular")
        self.assertTrue(fifo.exists())

    def test_trash_rejects_trailing_slash_and_empty_path(self):
        (self.docs / "dir").mkdir()
        result = self.engine.trash(
            "Documents", "dir/", confirmed=True
        )
        self.assertEqual(result["error_code"], "invalid_relative_path")
        self.assertTrue((self.docs / "dir").exists())

        result = self.engine.trash("Documents", "", confirmed=True)
        self.assertEqual(result["error_code"], "empty_relative_path")

    def test_trash_requires_quarantine_root(self):
        engine = FileOpsEngine(
            resolver=self.resolver,
            quarantine_root=None,
        )
        target = self.docs / "note.txt"
        target.write_text("hello")

        result = engine.trash("Documents", "note.txt", confirmed=True)
        self.assertEqual(result["error_code"], "quarantine_unavailable")
        self.assertTrue(target.exists())

    def test_trash_rejects_quarantine_root_symlink(self):
        link = self.home / "quarantine_link"
        link.symlink_to(self.quarantine)
        engine = FileOpsEngine(
            resolver=self.resolver,
            quarantine_root=link,
        )
        target = self.docs / "note.txt"
        target.write_text("hello")

        result = engine.trash("Documents", "note.txt", confirmed=True)
        self.assertEqual(result["error_code"], "invalid_quarantine_root")
        self.assertTrue(target.exists())

    # ------------------------------------------------------------------
    # Previews on existing targets
    # ------------------------------------------------------------------

    def test_previews_reflect_existing_targets_without_changing_them(self):
        (self.docs / "note.txt").write_text("one")
        (self.docs / "folder").mkdir()
        (self.docs / "full").mkdir()
        (self.docs / "full" / "child.txt").write_text("child")

        write_preview = self.engine.preview_write(
            "Documents", "note.txt", "two"
        )
        self.assertFalse(write_preview["success"])
        self.assertEqual(write_preview["error_code"], "target_exists")
        self.assertTrue(write_preview["preview"]["target_exists"])
        self.assertEqual((self.docs / "note.txt").read_text(), "one")

        write_overwrite = self.engine.preview_write(
            "Documents", "note.txt", "two", overwrite=True
        )
        self.assertTrue(write_overwrite["success"])
        self.assertEqual((self.docs / "note.txt").read_text(), "one")

        mkdir_preview = self.engine.preview_mkdir(
            "Documents", "folder"
        )
        self.assertFalse(mkdir_preview["success"])
        self.assertEqual(mkdir_preview["error_code"], "target_exists")
        self.assertTrue((self.docs / "folder").is_dir())

        trash_preview = self.engine.preview_trash(
            "Documents", "full"
        )
        self.assertFalse(trash_preview["success"])
        self.assertEqual(
            trash_preview["error_code"], "non_empty_directory"
        )
        self.assertTrue((self.docs / "full" / "child.txt").exists())

        trash_recursive_preview = self.engine.preview_trash(
            "Documents", "full", recursive=True
        )
        self.assertTrue(trash_recursive_preview["success"])
        self.assertTrue(trash_recursive_preview["preview"]["non_empty"])
        self.assertTrue((self.docs / "full" / "child.txt").exists())
        self.assertEqual(list(self.quarantine.iterdir()), [])

    # ------------------------------------------------------------------
    # Invalid folder and result shape
    # ------------------------------------------------------------------

    def test_invalid_folder_key(self):
        for operation in (
            lambda: self.engine.write_text(
                "/etc", "passwd", "x", confirmed=True
            ),
            lambda: self.engine.mkdir(
                "/etc", "newdir", confirmed=True
            ),
            lambda: self.engine.trash(
                "/etc", "passwd", confirmed=True
            ),
        ):
            result = operation()
            self.assertFalse(result["success"])
            self.assertEqual(result["error_code"], "invalid_folder")

    def test_result_shape_for_preview_and_mutation(self):
        preview = self.engine.preview_write("Documents", "note.txt", "x")
        result = self.engine.write_text(
            "Documents", "note.txt", "x", confirmed=True
        )
        for value in (preview, result):
            for key in (
                "success",
                "action",
                "requires_confirmation",
                "message",
                "target",
                "preview",
                "verified",
                "error_code",
            ):
                self.assertIn(key, value)


if __name__ == "__main__":
    unittest.main()
