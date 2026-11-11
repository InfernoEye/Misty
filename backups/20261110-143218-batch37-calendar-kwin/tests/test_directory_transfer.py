"""Tests for the isolated recursive directory transfer engine.

Every test uses a temporary home with approved Documents/Downloads folders.
No test touches the real user home, the network, a model, the registry, or
any code outside this package. The suite is pure ``unittest`` and the engine
is exercised through its public API only, except where a private helper is
patched to simulate an I/O failure or a cross-device rename.
"""

import errno
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.actions.directory_transfer import (
    DIRECTORY_MODE,
    FILE_MODE,
    MAX_DEPTH,
    MAX_FILES,
    MAX_PATH_LENGTH,
    MAX_TOTAL_BYTES,
    DirectoryTransferEngine,
)
from app.actions.file_scope import UserFolderResolver

RESULT_KEYS = {
    "success",
    "action",
    "code",
    "message",
    "source",
    "destination",
    "files",
    "bytes",
    "overwritten",
    "partial",
    "verified",
    "preview",
    "requires_confirmation",
}


class DirectoryTransferEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / "Documents").mkdir()
        (self.home / "Pobrane").mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()

        self.resolver = UserFolderResolver(
            home=self.home,
            xdg_lookup=lambda _name: None,
        )
        self.engine = DirectoryTransferEngine(resolver=self.resolver)
        self.docs = self.home / "Documents"
        self.downloads = self.home / "Pobrane"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _populate(self, name="tree"):
        """Two files (6 bytes) and one empty directory."""
        root = self.docs / name
        (root / "nested" / "deeper").mkdir(parents=True)
        (root / "top.txt").write_text("top", encoding="utf-8")
        (root / "nested" / "deeper" / "deep.bin").write_bytes(
            b"\x00\x01\x02"
        )
        (root / "empty").mkdir()
        return root

    def _entries(self, root):
        return sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
        )

    def _snapshot(self, root):
        snapshot = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            entry_st = path.lstat()
            if stat.S_ISDIR(entry_st.st_mode):
                snapshot[relative] = ("dir", stat.S_IMODE(entry_st.st_mode))
            elif stat.S_ISREG(entry_st.st_mode):
                snapshot[relative] = (
                    "file",
                    stat.S_IMODE(entry_st.st_mode),
                    path.read_bytes(),
                )
            else:
                snapshot[relative] = ("other", None)
        return snapshot

    def _assert_no_temp_files(self, *directories):
        for directory in directories:
            for path in directory.rglob("*"):
                self.assertFalse(
                    path.name.startswith(".misty-dir-"),
                    f"leftover temporary path: {path}",
                )

    def _assert_modes(self, root):
        self.assertEqual(
            stat.S_IMODE(root.lstat().st_mode), DIRECTORY_MODE
        )
        for path in root.rglob("*"):
            expected = (
                DIRECTORY_MODE if path.is_dir() else FILE_MODE
            )
            self.assertEqual(
                stat.S_IMODE(path.lstat().st_mode),
                expected,
                f"unexpected mode for {path}",
            )

    def _assert_tree_equal(self, left, right):
        self.assertEqual(self._entries(left), self._entries(right))
        for relative in self._entries(left):
            left_path = left / relative
            right_path = right / relative
            self.assertEqual(left_path.is_dir(), right_path.is_dir())
            if left_path.is_file():
                self.assertEqual(
                    left_path.read_bytes(), right_path.read_bytes()
                )

    # ------------------------------------------------------------------
    # Approval gating
    # ------------------------------------------------------------------

    def test_copy_and_move_require_confirmation_and_write_nothing(self):
        source = self._populate()
        before = self._snapshot(source)

        for method in (self.engine.copy, self.engine.move):
            for confirmed in (False, None, 0, 1, "yes"):
                with self.subTest(
                    action=method.__name__, confirmed=confirmed
                ):
                    result = method(
                        "Documents",
                        "tree",
                        "Downloads",
                        "gated",
                        confirmed=confirmed,
                    )
                    self.assertFalse(result["success"])
                    self.assertTrue(result["requires_confirmation"])
                    self.assertEqual(
                        result["code"], "confirmation_required"
                    )
                    self.assertIsNone(result["files"])
                    self.assertIsNone(result["bytes"])
                    self.assertFalse(result["verified"])
                    self.assertIsNotNone(result["preview"])
                    self.assertEqual(
                        result["preview"]["files"], 2
                    )
                    self.assertEqual(
                        result["preview"]["bytes"], 6
                    )
                    self.assertFalse(
                        (self.downloads / "gated").exists()
                    )
                    self.assertEqual(self._snapshot(source), before)
                    self._assert_no_temp_files(self.home)

    # ------------------------------------------------------------------
    # Previews
    # ------------------------------------------------------------------

    def test_previews_are_read_only_and_accurate(self):
        source = self._populate()
        before = self._snapshot(source)

        preview = self.engine.preview_copy(
            "Documents", "tree", "Downloads", "backup"
        )
        self.assertTrue(preview["success"])
        self.assertIsNone(preview["code"])
        self.assertFalse(preview["requires_confirmation"])
        self.assertFalse(preview["verified"])
        self.assertEqual(preview["files"], 2)
        self.assertEqual(preview["bytes"], 6)
        details = preview["preview"]
        self.assertEqual(details["operation"], "copy")
        self.assertEqual(details["kind"], "directory")
        self.assertEqual(details["files"], 2)
        self.assertEqual(details["bytes"], 6)
        self.assertEqual(details["max_files"], MAX_FILES)
        self.assertEqual(details["max_bytes"], MAX_TOTAL_BYTES)
        self.assertEqual(details["max_depth"], MAX_DEPTH)
        self.assertFalse(details["destination_exists"])
        self.assertFalse(details["overwrite"])
        self.assertEqual(details["method"], "copy")
        self.assertTrue(details["atomic"])

        move_preview = self.engine.preview_move(
            "Documents", "tree", "Downloads", "backup"
        )
        self.assertTrue(move_preview["success"])
        self.assertEqual(move_preview["preview"]["method"], "rename")
        self.assertTrue(move_preview["preview"]["same_filesystem"])
        self.assertTrue(move_preview["preview"]["atomic"])

        with patch.object(
            self.engine, "_same_filesystem", return_value=False
        ):
            fallback = self.engine.preview_move(
                "Documents", "tree", "Downloads", "backup"
            )
        self.assertTrue(fallback["success"])
        self.assertEqual(
            fallback["preview"]["method"], "copy+remove"
        )
        self.assertFalse(fallback["preview"]["atomic"])
        self.assertEqual(fallback["preview"]["files"], 2)
        self.assertEqual(fallback["preview"]["bytes"], 6)

        self.assertEqual(self._snapshot(source), before)
        self.assertFalse((self.downloads / "backup").exists())
        self._assert_no_temp_files(self.home)

        actual = self.engine.copy(
            "Documents", "tree", "Downloads", "backup", confirmed=True
        )
        self.assertTrue(actual["success"])
        self.assertEqual(actual["files"], preview["files"])
        self.assertEqual(actual["bytes"], preview["bytes"])

    # ------------------------------------------------------------------
    # Copy
    # ------------------------------------------------------------------

    def test_nested_copy_round_trip_and_metadata(self):
        source = self._populate()
        source_snapshot = self._snapshot(source)

        result = self.engine.copy(
            "Documents", "tree", "Downloads", "backup", confirmed=True
        )

        destination = self.downloads / "backup"
        self.assertTrue(result["success"])
        self.assertIsNone(result["code"])
        self.assertEqual(result["action"], "copy")
        self.assertFalse(result["requires_confirmation"])
        self.assertEqual(result["source"], "tree")
        self.assertEqual(result["destination"], "backup")
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["bytes"], 6)
        self.assertFalse(result["overwritten"])
        self.assertFalse(result["partial"])
        self.assertTrue(result["verified"])
        self.assertTrue(destination.is_dir())
        self._assert_tree_equal(source, destination)
        self._assert_modes(destination)
        self.assertTrue((destination / "empty").is_dir())
        self.assertEqual(self._snapshot(source), source_snapshot)
        self._assert_no_temp_files(self.home, self.docs, self.downloads)
        json.dumps(result)

    def test_copy_preserves_empty_directory_only_tree(self):
        source = self.docs / "empty_tree"
        (source / "a" / "b").mkdir(parents=True)

        preview = self.engine.preview_copy(
            "Documents", "empty_tree", "Downloads", "empty_tree"
        )
        self.assertTrue(preview["success"])
        self.assertEqual(preview["files"], 0)
        self.assertEqual(preview["bytes"], 0)

        result = self.engine.copy(
            "Documents",
            "empty_tree",
            "Downloads",
            "empty_tree",
            confirmed=True,
        )
        destination = self.downloads / "empty_tree"
        self.assertTrue(result["success"])
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["bytes"], 0)
        self.assertTrue(result["verified"])
        self.assertTrue((destination / "a" / "b").is_dir())
        self._assert_modes(destination)

        moved = self.engine.move(
            "Documents",
            "empty_tree",
            "Downloads",
            "empty_tree_moved",
            confirmed=True,
        )
        self.assertTrue(moved["success"])
        self.assertEqual(moved["files"], 0)
        self.assertEqual(moved["bytes"], 0)
        self.assertTrue((self.downloads / "empty_tree_moved" / "a").is_dir())

    # ------------------------------------------------------------------
    # Overwrite policy
    # ------------------------------------------------------------------

    def test_copy_and_move_refuse_existing_destination_without_overwrite(self):
        source = self._populate()
        destination = self.downloads / "target"
        destination.mkdir()
        (destination / "old.txt").write_text("old", encoding="utf-8")
        before = self._snapshot(destination)

        for method in (self.engine.copy, self.engine.move):
            with self.subTest(action=method.__name__):
                result = method(
                    "Documents",
                    "tree",
                    "Downloads",
                    "target",
                    confirmed=True,
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], "destination_exists")
                self.assertEqual(self._snapshot(destination), before)
                self.assertTrue(source.exists())
                self._assert_no_temp_files(self.home)

    def test_copy_overwrite_replaces_the_whole_tree(self):
        self._populate("tree")
        destination = self.downloads / "target"
        destination.mkdir()
        (destination / "old.txt").write_text("old", encoding="utf-8")

        pending = self.engine.copy(
            "Documents",
            "tree",
            "Downloads",
            "target",
            overwrite=True,
        )
        self.assertTrue(pending["requires_confirmation"])
        self.assertEqual(
            (destination / "old.txt").read_text(encoding="utf-8"), "old"
        )

        result = self.engine.copy(
            "Documents",
            "tree",
            "Downloads",
            "target",
            overwrite=True,
            confirmed=True,
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["overwritten"])
        self.assertTrue(result["verified"])
        self.assertFalse((destination / "old.txt").exists())
        self.assertEqual(
            (destination / "top.txt").read_text(encoding="utf-8"), "top"
        )
        self._assert_modes(destination)
        self._assert_no_temp_files(self.home)

    def test_move_overwrite_replaces_the_whole_tree(self):
        self._populate("tree")
        destination = self.downloads / "target"
        destination.mkdir()
        (destination / "old.txt").write_text("old", encoding="utf-8")

        result = self.engine.move(
            "Documents",
            "tree",
            "Downloads",
            "target",
            overwrite=True,
            confirmed=True,
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["overwritten"])
        self.assertTrue(result["verified"])
        self.assertFalse((self.docs / "tree").exists())
        self.assertEqual(
            (destination / "nested" / "deeper" / "deep.bin").read_bytes(),
            b"\x00\x01\x02",
        )
        self.assertFalse((destination / "old.txt").exists())
        self._assert_no_temp_files(self.home)

    def test_destination_file_is_rejected(self):
        self._populate("tree")
        (self.downloads / "plain").write_text("file", encoding="utf-8")

        result = self.engine.copy(
            "Documents",
            "tree",
            "Downloads",
            "plain",
            overwrite=True,
            confirmed=True,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "destination_not_directory")
        self.assertEqual(
            (self.downloads / "plain").read_text(encoding="utf-8"),
            "file",
        )

    # ------------------------------------------------------------------
    # Symlink and special-file rejection
    # ------------------------------------------------------------------

    def test_symlink_entries_are_rejected(self):
        source = self._populate("tree")
        outside_file = self.outside / "secret.txt"
        outside_file.write_text("outside", encoding="utf-8")
        (source / "link.txt").symlink_to(outside_file)

        result = self.engine.copy(
            "Documents", "tree", "Downloads", "copy", confirmed=True
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "symlink_entry")
        self.assertFalse((self.downloads / "copy").exists())
        self.assertEqual(
            outside_file.read_text(encoding="utf-8"), "outside"
        )
        self._assert_no_temp_files(self.home)

        (source / "link.txt").unlink()
        (source / "linkdir").symlink_to(self.outside)
        preview = self.engine.preview_copy(
            "Documents", "tree", "Downloads", "copy"
        )
        self.assertFalse(preview["success"])
        self.assertEqual(preview["code"], "symlink_entry")

    def test_top_level_symlink_and_symlink_parent_are_rejected(self):
        self._populate("tree")
        (self.docs / "linktree").symlink_to(self.docs / "tree")
        result = self.engine.copy(
            "Documents", "linktree", "Downloads", "copy", confirmed=True
        )
        self.assertEqual(result["code"], "source_symlink")
        self.assertFalse((self.downloads / "copy").exists())

        (self.docs / "linkdir").symlink_to(self.outside)
        result = self.engine.copy(
            "Documents",
            "linkdir/tree",
            "Downloads",
            "copy",
            confirmed=True,
        )
        self.assertEqual(result["code"], "symlink_parent")

        (self.downloads / "destlink").symlink_to(self.outside / "target")
        result = self.engine.copy(
            "Documents",
            "tree",
            "Downloads",
            "destlink",
            overwrite=True,
            confirmed=True,
        )
        self.assertEqual(result["code"], "destination_symlink")
        self.assertFalse((self.outside / "target").exists())

        (self.downloads / "parentlink").symlink_to(self.outside)
        result = self.engine.copy(
            "Documents",
            "tree",
            "Downloads",
            "parentlink/copy",
            confirmed=True,
        )
        self.assertEqual(result["code"], "symlink_parent")
        self.assertFalse((self.outside / "copy").exists())

    def test_special_entries_are_rejected(self):
        source = self._populate("tree")
        os.mkfifo(source / "pipe")

        preview = self.engine.preview_copy(
            "Documents", "tree", "Downloads", "copy"
        )
        self.assertFalse(preview["success"])
        self.assertEqual(preview["code"], "special_entry")

        result = self.engine.copy(
            "Documents", "tree", "Downloads", "copy", confirmed=True
        )
        self.assertEqual(result["code"], "special_entry")
        self.assertFalse((self.downloads / "copy").exists())
        self._assert_no_temp_files(self.home)

    # ------------------------------------------------------------------
    # Structural nesting
    # ------------------------------------------------------------------

    def test_destination_inside_source_is_rejected(self):
        self._populate("tree")

        result = self.engine.copy(
            "Documents",
            "tree",
            "Documents",
            "tree/nested_copy",
            overwrite=True,
            confirmed=True,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "destination_inside_source")
        self.assertFalse(
            (self.docs / "tree" / "nested_copy").exists()
        )
        self._assert_no_temp_files(self.home)

    def test_source_inside_destination_is_rejected(self):
        (self.docs / "outer" / "inner").mkdir(parents=True)
        (self.docs / "outer" / "inner" / "a.txt").write_text(
            "a", encoding="utf-8"
        )

        result = self.engine.move(
            "Documents",
            "outer/inner",
            "Documents",
            "outer",
            overwrite=True,
            confirmed=True,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "source_inside_destination")
        self.assertTrue((self.docs / "outer" / "inner" / "a.txt").is_file())

    def test_same_source_and_destination_is_rejected(self):
        self._populate("tree")

        for folder, relative, destination_relative in (
            ("Documents", "tree", "tree"),
            ("Documents", "tree", "./tree"),
        ):
            with self.subTest(destination=destination_relative):
                result = self.engine.copy(
                    folder,
                    relative,
                    folder,
                    destination_relative,
                    overwrite=True,
                    confirmed=True,
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], "same_directory")
                self.assertTrue((self.docs / "tree" / "top.txt").is_file())

    # ------------------------------------------------------------------
    # Bounded limits
    # ------------------------------------------------------------------

    def test_depth_limit_is_enforced(self):
        root = self.docs / "deep"
        (root / "a" / "b" / "c").mkdir(parents=True)
        (root / "a" / "b" / "c" / "leaf.txt").write_text(
            "leaf", encoding="utf-8"
        )

        with patch(
            "app.actions.directory_transfer.MAX_DEPTH", 2
        ):
            preview = self.engine.preview_copy(
                "Documents", "deep", "Downloads", "deep"
            )
            self.assertFalse(preview["success"])
            self.assertEqual(preview["code"], "path_too_deep")

            result = self.engine.copy(
                "Documents", "deep", "Downloads", "deep", confirmed=True
            )
            self.assertEqual(result["code"], "path_too_deep")

        self.assertFalse((self.downloads / "deep").exists())
        self._assert_no_temp_files(self.home)

    def test_file_count_limit_is_enforced(self):
        root = self.docs / "many"
        root.mkdir()
        for index in range(3):
            (root / f"file{index}.txt").write_text(
                "x", encoding="utf-8"
            )

        with patch("app.actions.directory_transfer.MAX_FILES", 2):
            preview = self.engine.preview_copy(
                "Documents", "many", "Downloads", "many"
            )
            self.assertFalse(preview["success"])
            self.assertEqual(preview["code"], "too_many_files")
            self.assertEqual(preview["preview"]["files"], 3)

            result = self.engine.copy(
                "Documents", "many", "Downloads", "many", confirmed=True
            )
            self.assertEqual(result["code"], "too_many_files")

        self.assertFalse((self.downloads / "many").exists())

    def test_total_size_limit_is_enforced(self):
        root = self.docs / "heavy"
        root.mkdir()
        (root / "big.bin").write_bytes(b"123456")

        with patch(
            "app.actions.directory_transfer.MAX_TOTAL_BYTES", 5
        ):
            preview = self.engine.preview_copy(
                "Documents", "heavy", "Downloads", "heavy"
            )
            self.assertFalse(preview["success"])
            self.assertEqual(preview["code"], "total_too_large")
            self.assertEqual(preview["preview"]["bytes"], 6)
            self.assertEqual(preview["preview"]["max_bytes"], 5)

            result = self.engine.copy(
                "Documents", "heavy", "Downloads", "heavy", confirmed=True
            )
            self.assertEqual(result["code"], "total_too_large")

        self.assertFalse((self.downloads / "heavy").exists())

    def test_path_byte_limit_is_enforced(self):
        self._populate("tree")

        with patch(
            "app.actions.directory_transfer.MAX_PATH_BYTES", 24
        ):
            result = self.engine.copy(
                "Documents", "tree", "Downloads", "copy", confirmed=True
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "path_too_long")
        self.assertFalse((self.downloads / "copy").exists())
        self._assert_no_temp_files(self.home)

    # ------------------------------------------------------------------
    # Failure hygiene
    # ------------------------------------------------------------------

    def test_copy_read_failure_removes_temp_and_keeps_source(self):
        source = self._populate("tree")
        before = self._snapshot(source)

        with patch(
            "app.actions.directory_transfer.os.read",
            side_effect=OSError("boom"),
        ):
            result = self.engine.copy(
                "Documents", "tree", "Downloads", "copy", confirmed=True
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "copy_failed")
        self.assertNotIn("boom", result["message"])
        self.assertFalse((self.downloads / "copy").exists())
        self.assertEqual(self._snapshot(source), before)
        self._assert_no_temp_files(self.home, self.docs, self.downloads)

    def test_publish_failure_removes_temp_and_keeps_source(self):
        source = self._populate("tree")
        before = self._snapshot(source)
        real_rename = os.rename

        def fake_rename(source_path, destination_path):
            if Path(destination_path) == self.downloads / "copy":
                raise OSError("boom")
            return real_rename(source_path, destination_path)

        with patch(
            "app.actions.directory_transfer.os.rename",
            side_effect=fake_rename,
        ):
            result = self.engine.copy(
                "Documents", "tree", "Downloads", "copy", confirmed=True
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "publish_failed")
        self.assertNotIn("boom", result["message"])
        self.assertFalse((self.downloads / "copy").exists())
        self.assertEqual(self._snapshot(source), before)
        self._assert_no_temp_files(self.home)

    def test_failed_overwrite_restores_existing_destination(self):
        self._populate("tree")
        destination = self.downloads / "target"
        destination.mkdir()
        (destination / "old.txt").write_text("old", encoding="utf-8")
        before = self._snapshot(destination)
        real_rename = os.rename

        def fake_rename(source_path, destination_path):
            source_name = Path(source_path).name
            if (
                Path(destination_path) == destination
                and source_name.startswith(".misty-dir-")
                and not source_name.startswith(".misty-dir-backup-")
            ):
                raise OSError("boom")
            return real_rename(source_path, destination_path)

        with patch(
            "app.actions.directory_transfer.os.rename",
            side_effect=fake_rename,
        ):
            result = self.engine.copy(
                "Documents",
                "tree",
                "Downloads",
                "target",
                overwrite=True,
                confirmed=True,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "publish_failed")
        self.assertEqual(self._snapshot(destination), before)
        self.assertTrue((self.docs / "tree" / "top.txt").is_file())
        self._assert_no_temp_files(self.home)

    # ------------------------------------------------------------------
    # Move
    # ------------------------------------------------------------------

    def test_move_same_filesystem_is_atomic_rename(self):
        source = self._populate("tree")
        source_entries = self._entries(source)
        source_snapshot = self._snapshot(source)

        result = self.engine.move(
            "Documents", "tree", "Downloads", "moved", confirmed=True
        )

        destination = self.downloads / "moved"
        self.assertTrue(result["success"])
        self.assertIsNone(result["code"])
        self.assertEqual(result["action"], "move")
        self.assertEqual(result["source"], "tree")
        self.assertEqual(result["destination"], "moved")
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["bytes"], 6)
        self.assertFalse(result["overwritten"])
        self.assertFalse(result["partial"])
        self.assertTrue(result["verified"])
        self.assertFalse(source.exists())
        self.assertTrue(destination.is_dir())
        self.assertEqual(self._entries(destination), source_entries)
        self.assertEqual(
            (destination / "nested" / "deeper" / "deep.bin").read_bytes(),
            b"\x00\x01\x02",
        )
        # A rename preserves the source metadata exactly; only the copy
        # path republishes 0700/0600 modes.
        self.assertEqual(self._snapshot(destination), source_snapshot)
        self._assert_no_temp_files(self.home)

    def test_move_cross_device_verifies_before_removing_source(self):
        source = self._populate("tree")
        destination = self.downloads / "cross"
        real_rename = os.rename
        calls = []

        def fake_rename(source_path, destination_path):
            if (
                Path(source_path) == source
                and Path(destination_path) == destination
            ):
                raise OSError(errno.EXDEV, "cross-device")
            return real_rename(source_path, destination_path)

        original = self.engine._move_across_devices

        def spy(*args, **kwargs):
            calls.append(True)
            return original(*args, **kwargs)

        with patch(
            "app.actions.directory_transfer.os.rename",
            side_effect=fake_rename,
        ), patch.object(
            self.engine, "_move_across_devices", side_effect=spy
        ):
            result = self.engine.move(
                "Documents", "tree", "Downloads", "cross",
                confirmed=True,
            )

        self.assertEqual(calls, [True])
        self.assertTrue(result["success"])
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["bytes"], 6)
        self.assertFalse(result["partial"])
        self.assertTrue(result["verified"])
        self.assertFalse(source.exists())
        self.assertEqual(
            (destination / "top.txt").read_text(encoding="utf-8"), "top"
        )
        self._assert_modes(destination)
        self._assert_no_temp_files(self.home)

    def test_move_across_devices_reports_partial_when_removal_fails(self):
        source = self._populate("tree")
        destination = self.downloads / "cross"
        real_rename = os.rename
        real_unlink = os.unlink

        def fake_rename(source_path, destination_path):
            if (
                Path(source_path) == source
                and Path(destination_path) == destination
            ):
                raise OSError(errno.EXDEV, "cross-device")
            return real_rename(source_path, destination_path)

        def fake_unlink(path, *args, **kwargs):
            try:
                blocked = Path(path).is_relative_to(source)
            except (TypeError, ValueError):
                blocked = False
            if blocked:
                raise OSError("cannot unlink")
            return real_unlink(path, *args, **kwargs)

        with patch(
            "app.actions.directory_transfer.os.rename",
            side_effect=fake_rename,
        ), patch(
            "app.actions.directory_transfer.os.unlink",
            side_effect=fake_unlink,
        ):
            result = self.engine.move(
                "Documents", "tree", "Downloads", "cross",
                confirmed=True,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "source_remove_failed")
        self.assertIn(
            "original could not be fully removed", result["message"]
        )
        self.assertNotIn("cannot unlink", result["message"])
        self.assertTrue(result["partial"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["bytes"], 6)
        self.assertTrue(source.exists())
        self.assertTrue((source / "top.txt").is_file())
        self.assertEqual(
            (destination / "top.txt").read_text(encoding="utf-8"), "top"
        )
        self._assert_modes(destination)
        self._assert_no_temp_files(self.home)

    def test_move_cross_device_copy_failure_keeps_source(self):
        source = self._populate("tree")
        destination = self.downloads / "cross"
        real_rename = os.rename

        def fake_rename(source_path, destination_path):
            if (
                Path(source_path) == source
                and Path(destination_path) == destination
            ):
                raise OSError(errno.EXDEV, "cross-device")
            return real_rename(source_path, destination_path)

        with patch(
            "app.actions.directory_transfer.os.rename",
            side_effect=fake_rename,
        ), patch(
            "app.actions.directory_transfer.os.read",
            side_effect=OSError("write failed"),
        ):
            result = self.engine.move(
                "Documents", "tree", "Downloads", "cross",
                confirmed=True,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "copy_failed")
        self.assertNotIn("write failed", result["message"])
        self.assertFalse(result["partial"])
        self.assertFalse(result["verified"])
        self.assertTrue((source / "top.txt").is_file())
        self.assertFalse(destination.exists())
        self._assert_no_temp_files(self.home)

    # ------------------------------------------------------------------
    # Scope, paths and validation
    # ------------------------------------------------------------------

    def test_scope_escapes_are_rejected(self):
        self._populate("tree")

        cases = [
            ("Documents", "../escape", "Downloads", "x", "traversal"),
            ("Documents", "/etc", "Downloads", "x", "absolute_path"),
            ("Documents", "tree", "Downloads", "../x", "traversal"),
            ("Documents", "tree", "Downloads", "/tmp/x", "absolute_path"),
            ("Documents", "", "Downloads", "x", "empty_relative_path"),
            ("Documents", "tree", "Downloads", "", "empty_relative_path"),
            ("Documents", "tree/", "Downloads", "x", "invalid_relative_path"),
            ("Documents", "tree", "Downloads", "x/", "invalid_relative_path"),
            ("/etc", "tree", "Downloads", "x", "invalid_folder"),
            (None, "tree", "Downloads", "x", "invalid_folder"),
            ("Documents", "tree", "Missing", "x", "invalid_folder"),
        ]
        for folder, relative, dest_folder, dest_relative, expected in cases:
            with self.subTest(
                relative=relative, destination=dest_relative
            ):
                result = self.engine.copy(
                    folder,
                    relative,
                    dest_folder,
                    dest_relative,
                    confirmed=True,
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], expected)

        self.assertFalse((self.home / "escape").exists())
        self.assertFalse((self.outside / "x").exists())
        self.assertFalse((self.downloads / "x").exists())
        self._assert_no_temp_files(self.home)

    def test_invalid_and_too_long_paths_are_rejected(self):
        self._populate("tree")

        for relative in (
            "a" * (MAX_PATH_LENGTH + 1),
            "a" * 256,
            "/".join(["a"] * (MAX_DEPTH + 1)),
        ):
            with self.subTest(relative_length=len(relative)):
                result = self.engine.copy(
                    "Documents", relative, "Downloads", "x",
                    confirmed=True,
                )
                self.assertFalse(result["success"])
                self.assertIn(
                    result["code"],
                    {"path_too_long", "path_too_deep"},
                )

        result = self.engine.copy(
            "Documents",
            "tree",
            "Downloads",
            "d" * (MAX_PATH_LENGTH + 1),
            confirmed=True,
        )
        self.assertEqual(result["code"], "path_too_long")
        self.assertFalse((self.downloads / "x").exists())

    def test_missing_and_invalid_parents_are_rejected(self):
        self._populate("tree")

        result = self.engine.copy(
            "Documents", "tree", "Downloads", "incoming/tree",
            confirmed=True,
        )
        self.assertEqual(result["code"], "missing_parent")
        self.assertFalse((self.downloads / "incoming").exists())

        (self.docs / "plain.txt").write_text("data", encoding="utf-8")
        result = self.engine.copy(
            "Documents", "plain.txt/tree", "Downloads", "x",
            confirmed=True,
        )
        self.assertEqual(result["code"], "parent_not_directory")

    def test_missing_source_is_reported(self):
        result = self.engine.preview_move(
            "Documents", "missing", "Downloads", "x"
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "source_not_found")

        result = self.engine.copy(
            "Documents", "plain.txt", "Downloads", "x", confirmed=True
        )
        # "plain.txt" does not exist yet in this test.
        self.assertEqual(result["code"], "source_not_found")

    def test_resolver_returning_outside_folder_is_rejected(self):
        self._populate("tree")

        class OutsideResolver:
            home = self.home

            def base_for_key(self, key):
                return self.outside

        engine = DirectoryTransferEngine(resolver=OutsideResolver())
        result = engine.copy(
            "Documents", "tree", "Downloads", "x", confirmed=True
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "invalid_folder")
        self.assertFalse((self.outside / "x").exists())

    # ------------------------------------------------------------------
    # Result shape and information leak
    # ------------------------------------------------------------------

    def test_results_never_leak_content_or_exceptions(self):
        secret = "TOPSECRET-CONTENT"
        root = self.docs / "tree"
        root.mkdir()
        (root / "secret.txt").write_text(secret, encoding="utf-8")

        preview = self.engine.preview_copy(
            "Documents", "tree", "Downloads", "copy"
        )
        result = self.engine.copy(
            "Documents", "tree", "Downloads", "copy", confirmed=True
        )
        for value in (preview, result):
            dumped = json.dumps(value)
            self.assertNotIn(secret, dumped)
            self.assertNotIn(str(self.home), dumped)
            self.assertNotIn(str(self.root), dumped)

        def fake_rename(source_path, destination_path):
            raise OSError(secret)

        with patch(
            "app.actions.directory_transfer.os.rename",
            side_effect=fake_rename,
        ):
            failed = self.engine.copy(
                "Documents", "tree", "Downloads", "broken",
                confirmed=True,
            )
        failed_dump = json.dumps(failed)
        self.assertNotIn(secret, failed_dump)
        self.assertNotIn(secret, failed["message"])
        self.assertEqual(failed["code"], "publish_failed")

        class BoomResolver:
            def base_for_key(self, key):
                raise RuntimeError(secret)

        boom = DirectoryTransferEngine(resolver=BoomResolver()).copy(
            "Documents", "tree", "Downloads", "copy", confirmed=True
        )
        self.assertNotIn(secret, json.dumps(boom))
        self.assertNotIn(secret, boom["message"])
        self.assertEqual(boom["code"], "invalid_folder")

    def test_result_shape_is_stable_and_json_safe(self):
        self._populate("tree")
        (self.downloads / "existing").mkdir()

        results = [
            self.engine.preview_copy(
                "Documents", "tree", "Downloads", "new"
            ),
            self.engine.preview_move(
                "Documents", "tree", "Downloads", "new"
            ),
            self.engine.copy(
                "Documents", "tree", "Downloads", "new"
            ),
            self.engine.copy(
                "Documents", "tree", "Downloads", "existing",
                confirmed=True,
            ),
            self.engine.move(
                "Documents", "missing", "Downloads", "new",
                confirmed=True,
            ),
            self.engine.copy(
                "Documents", "tree", "Downloads", "done", confirmed=True
            ),
        ]

        for value in results:
            json.dumps(value)
            self.assertEqual(set(value), RESULT_KEYS)
            self.assertIsInstance(value["action"], str)
            self.assertIsInstance(value["message"], str)
            self.assertIsInstance(value["success"], bool)
            self.assertIsInstance(value["overwritten"], bool)
            self.assertIsInstance(value["partial"], bool)
            self.assertIsInstance(value["verified"], bool)
            self.assertIsInstance(value["requires_confirmation"], bool)
            self.assertTrue(
                value["files"] is None
                or isinstance(value["files"], int)
            )
            self.assertTrue(
                value["bytes"] is None
                or isinstance(value["bytes"], int)
            )
            self.assertIsInstance(
                value["preview"], (dict, type(None))
            )
            self.assertIsInstance(value["code"], (str, type(None)))


if __name__ == "__main__":
    unittest.main()
