"""Tests for the isolated, safety-first file transfer engine.

Every test uses a temporary home with approved Documents/Downloads folders.
No test touches the real user home, the network, a model, or the registry.
"""

import errno
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.actions.file_scope import UserFolderResolver
from app.actions.file_transfer import (
    CHUNK_SIZE,
    MAX_FILE_BYTES,
    MAX_PATH_LENGTH,
    FileTransferEngine,
)


class FileTransferEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / "Documents").mkdir()
        (self.home / "Pobrane").mkdir()
        (self.home / "Documents" / "sub").mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.quarantine = self.root / "quarantine"

        self.resolver = UserFolderResolver(
            home=self.home,
            xdg_lookup=lambda _name: None,
        )
        self.engine = FileTransferEngine(resolver=self.resolver)
        self.docs = self.home / "Documents"
        self.downloads = self.home / "Pobrane"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _names(self, directory):
        return {path.name for path in directory.iterdir()}

    def _assert_no_temp_files(self, directory):
        self.assertFalse(
            [
                name
                for name in self._names(directory)
                if name.startswith(".misty-") or name.endswith(".tmp")
            ]
        )

    # ------------------------------------------------------------------
    # Approval gating
    # ------------------------------------------------------------------

    def test_copy_requires_confirmation_and_writes_nothing(self):
        source = self.docs / "report.txt"
        source.write_text("alpha", encoding="utf-8")

        result = self.engine.copy(
            "Documents", "report.txt", "Downloads", "report.txt"
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["code"], "confirmation_required")
        self.assertEqual(result["error_code"], "confirmation_required")
        self.assertEqual(result["source"], "report.txt")
        self.assertEqual(result["target"], "report.txt")
        self.assertIsNone(result["bytes"])
        self.assertIsNotNone(result["preview"])
        self.assertEqual(result["preview"]["operation"], "copy")
        self.assertFalse((self.downloads / "report.txt").exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "alpha")

    def test_move_requires_confirmation_and_writes_nothing(self):
        source = self.docs / "report.txt"
        source.write_text("alpha", encoding="utf-8")

        result = self.engine.move(
            "Documents", "report.txt", "Downloads", "report.txt"
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["code"], "confirmation_required")
        self.assertEqual(result["preview"]["method"], "rename")
        self.assertTrue(source.exists())
        self.assertFalse((self.downloads / "report.txt").exists())

    def test_rename_requires_confirmation_and_writes_nothing(self):
        source = self.docs / "report.txt"
        source.write_text("alpha", encoding="utf-8")

        result = self.engine.rename(
            "Documents", "report.txt", "final.txt"
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["code"], "confirmation_required")
        self.assertEqual(result["source"], "report.txt")
        self.assertEqual(result["destination"], "final.txt")
        self.assertTrue(source.exists())
        self.assertFalse((self.docs / "final.txt").exists())

    def test_truthy_non_boolean_confirmation_is_refused(self):
        source = self.docs / "report.txt"
        source.write_text("alpha", encoding="utf-8")

        result = self.engine.copy(
            "Documents",
            "report.txt",
            "Downloads",
            "report.txt",
            confirmed=1,
        )

        self.assertTrue(result["requires_confirmation"])
        self.assertFalse((self.downloads / "report.txt").exists())

    # ------------------------------------------------------------------
    # Previews are read-only
    # ------------------------------------------------------------------

    def test_previews_are_read_only_and_bounded(self):
        source = self.docs / "report.txt"
        source.write_text("alpha", encoding="utf-8")
        (self.downloads / "report.txt").write_text("old", encoding="utf-8")

        preview = self.engine.preview_copy(
            "Documents",
            "report.txt",
            "Downloads",
            "report.txt",
            overwrite=True,
        )

        self.assertTrue(preview["success"])
        self.assertFalse(preview["requires_confirmation"])
        self.assertIsNone(preview["code"])
        self.assertIsNone(preview["bytes"])
        details = preview["preview"]
        self.assertEqual(details["operation"], "copy")
        self.assertEqual(details["size"], 5)
        self.assertEqual(details["bytes"], 5)
        self.assertTrue(details["overwrite"])
        self.assertTrue(details["destination_exists"])
        self.assertEqual(details["chunk_bytes"], CHUNK_SIZE)
        self.assertEqual(details["max_bytes"], MAX_FILE_BYTES)
        self.assertEqual(details["mode"], "0o600")
        self.assertEqual(details["method"], "stream")
        self.assertTrue(details["atomic"])
        self.assertEqual(
            (self.downloads / "report.txt").read_text(encoding="utf-8"),
            "old",
        )
        self.assertEqual(source.read_text(encoding="utf-8"), "alpha")
        self._assert_no_temp_files(self.downloads)

        denied = self.engine.preview_copy(
            "Documents", "report.txt", "Downloads", "report.txt"
        )
        self.assertFalse(denied["success"])
        self.assertEqual(denied["code"], "destination_exists")
        self.assertTrue(denied["preview"]["destination_exists"])
        self.assertEqual(
            (self.downloads / "report.txt").read_text(encoding="utf-8"),
            "old",
        )

    def test_move_preview_reports_rename_and_fallback(self):
        (self.docs / "report.txt").write_text("alpha", encoding="utf-8")

        preview = self.engine.preview_move(
            "Documents", "report.txt", "Downloads", "report.txt"
        )
        self.assertTrue(preview["success"])
        self.assertEqual(preview["preview"]["method"], "rename")
        self.assertTrue(preview["preview"]["atomic"])
        self.assertTrue(preview["preview"]["same_filesystem"])

        with patch.object(
            self.engine, "_same_filesystem", return_value=False
        ):
            fallback = self.engine.preview_move(
                "Documents", "report.txt", "Downloads", "report.txt"
            )
        self.assertTrue(fallback["success"])
        self.assertEqual(
            fallback["preview"]["method"], "copy+unlink"
        )
        self.assertFalse(fallback["preview"]["atomic"])
        self.assertTrue((self.docs / "report.txt").exists())
        self.assertFalse((self.downloads / "report.txt").exists())

    # ------------------------------------------------------------------
    # Copy
    # ------------------------------------------------------------------

    def test_copy_success_streams_and_preserves_mtime(self):
        source = self.docs / "report.txt"
        source.write_bytes(b"alpha")
        mtime_ns = 1_600_000_000_000_000_000
        os.utime(source, ns=(mtime_ns, mtime_ns))

        result = self.engine.copy(
            "Documents", "report.txt", "Downloads", "report.txt",
            confirmed=True,
        )

        destination = self.downloads / "report.txt"
        self.assertTrue(result["success"])
        self.assertFalse(result["requires_confirmation"])
        self.assertIsNone(result["code"])
        self.assertEqual(result["source"], "report.txt")
        self.assertEqual(result["destination"], "report.txt")
        self.assertEqual(result["target"], "report.txt")
        self.assertEqual(result["bytes"], 5)
        self.assertFalse(result["overwritten"])
        self.assertTrue(result["durable"])
        self.assertFalse(result["partial"])
        self.assertTrue(result["atomic"])
        self.assertEqual(result["method"], "stream")
        self.assertTrue(result["verified"])
        self.assertTrue(result["mtime_preserved"])
        self.assertEqual(destination.read_bytes(), b"alpha")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(destination.stat().st_mtime_ns, mtime_ns)
        self.assertTrue(source.exists())
        self.assertEqual(self._names(self.docs), {"report.txt", "sub"})
        self.assertEqual(self._names(self.downloads), {"report.txt"})
        self._assert_no_temp_files(self.downloads)
        json.dumps(result)

    def test_copy_streams_in_bounded_chunks(self):
        source = self.docs / "chunks.bin"
        source.write_bytes(b"0123456789")

        with patch("app.actions.file_transfer.CHUNK_SIZE", 3):
            result = self.engine.copy(
                "Documents", "chunks.bin", "Downloads", "chunks.bin",
                confirmed=True,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["bytes"], 10)
        self.assertEqual(
            (self.downloads / "chunks.bin").read_bytes(), b"0123456789"
        )

    def test_copy_overwrite_denied_then_allowed(self):
        source = self.docs / "report.txt"
        source.write_text("new", encoding="utf-8")
        destination = self.downloads / "report.txt"
        destination.write_text("old", encoding="utf-8")

        denied = self.engine.copy(
            "Documents", "report.txt", "Downloads", "report.txt",
            confirmed=True,
        )
        self.assertFalse(denied["success"])
        self.assertEqual(denied["code"], "destination_exists")
        self.assertEqual(destination.read_text(encoding="utf-8"), "old")

        pending = self.engine.copy(
            "Documents", "report.txt", "Downloads", "report.txt",
            overwrite=True,
        )
        self.assertTrue(pending["requires_confirmation"])
        self.assertEqual(destination.read_text(encoding="utf-8"), "old")

        allowed = self.engine.copy(
            "Documents", "report.txt", "Downloads", "report.txt",
            overwrite=True, confirmed=True,
        )
        self.assertTrue(allowed["success"])
        self.assertTrue(allowed["overwritten"])
        self.assertEqual(allowed["bytes"], 3)
        self.assertEqual(destination.read_text(encoding="utf-8"), "new")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self._assert_no_temp_files(self.downloads)

    def test_copy_across_approved_folders_and_subdirectory(self):
        (self.docs / "sub" / "deep.txt").write_text(
            "deep", encoding="utf-8"
        )

        result = self.engine.copy(
            "Documents", "sub/deep.txt", "Downloads", "incoming/deep.txt",
            confirmed=True,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "missing_parent")
        self.assertFalse(
            (self.downloads / "incoming" / "deep.txt").exists()
        )

        (self.downloads / "incoming").mkdir()
        result = self.engine.copy(
            "Documents", "sub/deep.txt", "Downloads", "incoming/deep.txt",
            confirmed=True,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["source"], "sub/deep.txt")
        self.assertEqual(result["destination"], "incoming/deep.txt")
        self.assertEqual(
            (self.downloads / "incoming" / "deep.txt").read_text(
                encoding="utf-8"
            ),
            "deep",
        )

    # ------------------------------------------------------------------
    # Scope and target rejection
    # ------------------------------------------------------------------

    def test_copy_rejects_path_scope_violations(self):
        (self.docs / "a.txt").write_text("data", encoding="utf-8")

        cases = [
            ("Documents", "../escape.txt", "Downloads", "x.txt",
             "traversal"),
            ("Documents", "a/../../escape.txt", "Downloads", "x.txt",
             "traversal"),
            ("Documents", "/etc/passwd", "Downloads", "x.txt",
             "absolute_path"),
            ("Documents", "", "Downloads", "x.txt",
             "empty_relative_path"),
            ("Documents", "a.txt/", "Downloads", "x.txt",
             "invalid_relative_path"),
            ("Documents", ".", "Downloads", "x.txt",
             "invalid_relative_path"),
            ("Documents", "a.txt", "/etc", "x.txt", "invalid_folder"),
            ("Documents", "a.txt", "Downloads", "/tmp/x.txt",
             "absolute_path"),
            ("Documents", "a.txt", "Downloads", "",
             "empty_relative_path"),
            ("Documents", "a.txt", "Downloads", "x.txt/",
             "invalid_relative_path"),
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

        self.assertFalse((self.home / "escape.txt").exists())
        self.assertFalse((self.outside / "x.txt").exists())

    def test_copy_rejects_long_and_deep_paths(self):
        (self.docs / "a.txt").write_text("data", encoding="utf-8")

        for relative in (
            "a" * (MAX_PATH_LENGTH + 1),
            "a" * 256,
            "/".join(["a"] * 33),
        ):
            with self.subTest(relative_length=len(relative)):
                result = self.engine.copy(
                    "Documents", relative, "Downloads", "x.txt",
                    confirmed=True,
                )
                self.assertFalse(result["success"])
                self.assertIn(
                    result["code"], {"path_too_long", "path_too_deep"}
                )

        result = self.engine.copy(
            "Documents",
            "a.txt",
            "Downloads",
            "d" * (MAX_PATH_LENGTH + 1),
            confirmed=True,
        )
        self.assertEqual(result["code"], "path_too_long")
        self.assertEqual(self._names(self.downloads), set())

    def test_copy_rejects_symlinks_directories_and_special_files(self):
        (self.docs / "a.txt").write_text("data", encoding="utf-8")
        outside_file = self.outside / "secret.txt"
        outside_file.write_text("safe", encoding="utf-8")

        (self.docs / "link.txt").symlink_to(outside_file)
        result = self.engine.copy(
            "Documents", "link.txt", "Downloads", "x.txt", confirmed=True
        )
        self.assertEqual(result["code"], "symlink_target")
        self.assertEqual(outside_file.read_text(encoding="utf-8"), "safe")
        self.assertFalse((self.downloads / "x.txt").exists())

        (self.docs / "linkdir").symlink_to(self.outside)
        result = self.engine.copy(
            "Documents", "linkdir/secret.txt", "Downloads", "x.txt",
            confirmed=True,
        )
        self.assertEqual(result["code"], "symlink_parent")
        self.assertFalse((self.downloads / "x.txt").exists())

        (self.downloads / "destlink.txt").symlink_to(
            self.outside / "target.txt"
        )
        result = self.engine.copy(
            "Documents",
            "a.txt",
            "Downloads",
            "destlink.txt",
            overwrite=True,
            confirmed=True,
        )
        self.assertEqual(result["code"], "destination_symlink")
        self.assertFalse((self.outside / "target.txt").exists())

        (self.downloads / "dirlink").symlink_to(self.outside)
        result = self.engine.copy(
            "Documents", "a.txt", "Downloads", "dirlink/x.txt",
            confirmed=True,
        )
        self.assertEqual(result["code"], "symlink_parent")
        self.assertFalse((self.outside / "x.txt").exists())

        result = self.engine.copy(
            "Documents", "sub", "Downloads", "sub", confirmed=True
        )
        self.assertEqual(result["code"], "source_is_directory")

        os.mkfifo(self.docs / "pipe")
        result = self.engine.copy(
            "Documents", "pipe", "Downloads", "pipe", confirmed=True
        )
        self.assertEqual(result["code"], "source_not_regular")

        (self.downloads / "existing_dir").mkdir()
        result = self.engine.copy(
            "Documents",
            "a.txt",
            "Downloads",
            "existing_dir",
            confirmed=True,
        )
        self.assertEqual(result["code"], "destination_is_directory")
        self.assertTrue((self.downloads / "existing_dir").is_dir())

        os.mkfifo(self.downloads / "fifo")
        result = self.engine.copy(
            "Documents",
            "a.txt",
            "Downloads",
            "fifo",
            overwrite=True,
            confirmed=True,
        )
        self.assertEqual(result["code"], "destination_not_regular")
        self.assertTrue((self.downloads / "fifo").exists())

    def test_copy_rejects_same_source_and_destination(self):
        source = self.docs / "a.txt"
        source.write_text("data", encoding="utf-8")

        for destination_relative in ("a.txt", "./a.txt"):
            with self.subTest(destination=destination_relative):
                result = self.engine.copy(
                    "Documents",
                    "a.txt",
                    "Documents",
                    destination_relative,
                    overwrite=True,
                    confirmed=True,
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], "same_file")
                self.assertEqual(source.read_text(encoding="utf-8"), "data")

    def test_copy_rejects_resolver_escape(self):
        (self.docs / "a.txt").write_text("data", encoding="utf-8")

        class OutsideResolver:
            home = self.home

            def base_for_key(self, key):
                return self.outside

        engine = FileTransferEngine(resolver=OutsideResolver())
        result = engine.copy(
            "Documents", "a.txt", "Downloads", "x.txt", confirmed=True
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "invalid_folder")
        self.assertFalse((self.outside / "x.txt").exists())

    def test_copy_enforces_file_size_limit(self):
        source = self.docs / "big.bin"
        source.write_bytes(b"123456")

        with patch("app.actions.file_transfer.MAX_FILE_BYTES", 4):
            preview = self.engine.preview_copy(
                "Documents", "big.bin", "Downloads", "big.bin"
            )
            self.assertFalse(preview["success"])
            self.assertEqual(preview["code"], "file_too_large")
            self.assertEqual(preview["preview"]["size"], 6)
            self.assertEqual(preview["preview"]["max_bytes"], 4)

            result = self.engine.copy(
                "Documents", "big.bin", "Downloads", "big.bin",
                confirmed=True,
            )
            self.assertEqual(result["code"], "file_too_large")

        self.assertFalse((self.downloads / "big.bin").exists())

    # ------------------------------------------------------------------
    # Copy failure hygiene
    # ------------------------------------------------------------------

    def test_copy_failure_removes_temp_and_leaves_source(self):
        source = self.docs / "a.txt"
        source.write_text("data", encoding="utf-8")
        before = self._names(self.docs)

        with patch(
            "app.actions.file_transfer.os.replace",
            side_effect=OSError("boom"),
        ):
            result = self.engine.copy(
                "Documents", "a.txt", "Downloads", "a.txt",
                confirmed=True,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "copy_failed")
        self.assertNotIn("boom", result["message"])
        self.assertEqual(source.read_text(encoding="utf-8"), "data")
        self.assertEqual(self._names(self.docs), before)
        self.assertEqual(self._names(self.downloads), set())
        self._assert_no_temp_files(self.docs)
        self._assert_no_temp_files(self.downloads)

    def test_copy_succeeds_when_mtime_cannot_be_preserved(self):
        source = self.docs / "a.txt"
        source.write_text("data", encoding="utf-8")

        with patch(
            "app.actions.file_transfer.os.utime",
            side_effect=OSError("not permitted"),
        ):
            result = self.engine.copy(
                "Documents", "a.txt", "Downloads", "a.txt",
                confirmed=True,
            )

        self.assertTrue(result["success"])
        self.assertFalse(result["mtime_preserved"])
        self.assertEqual(
            (self.downloads / "a.txt").read_text(encoding="utf-8"), "data"
        )

    def test_no_content_leak_in_results(self):
        secret = "TOPSECRET-CONTENT"
        source = self.docs / "a.txt"
        source.write_text(secret, encoding="utf-8")

        preview = self.engine.preview_copy(
            "Documents", "a.txt", "Downloads", "a.txt"
        )
        result = self.engine.copy(
            "Documents", "a.txt", "Downloads", "a.txt", confirmed=True
        )

        for value in (preview, result):
            dumped = json.dumps(value)
            self.assertNotIn(secret, dumped)
            self.assertNotIn("TOPSECRET", result["message"])

    # ------------------------------------------------------------------
    # Move
    # ------------------------------------------------------------------

    def test_move_success_on_same_filesystem(self):
        source = self.docs / "a.txt"
        source.write_text("payload", encoding="utf-8")

        result = self.engine.move(
            "Documents", "a.txt", "Downloads", "b.txt", confirmed=True
        )

        destination = self.downloads / "b.txt"
        self.assertTrue(result["success"])
        self.assertIsNone(result["code"])
        self.assertEqual(result["source"], "a.txt")
        self.assertEqual(result["destination"], "b.txt")
        self.assertEqual(result["target"], "b.txt")
        self.assertEqual(result["bytes"], 7)
        self.assertFalse(result["overwritten"])
        self.assertTrue(result["durable"])
        self.assertFalse(result["partial"])
        self.assertTrue(result["atomic"])
        self.assertEqual(result["method"], "rename")
        self.assertTrue(result["verified"])
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_text(encoding="utf-8"), "payload")
        self.assertEqual(self._names(self.downloads), {"b.txt"})
        self._assert_no_temp_files(self.downloads)
        json.dumps(result)

    def test_move_overwrite_denied_then_allowed(self):
        source = self.docs / "a.txt"
        source.write_text("new", encoding="utf-8")
        destination = self.downloads / "a.txt"
        destination.write_text("old", encoding="utf-8")

        denied = self.engine.move(
            "Documents", "a.txt", "Downloads", "a.txt", confirmed=True
        )
        self.assertFalse(denied["success"])
        self.assertEqual(denied["code"], "destination_exists")
        self.assertTrue(source.exists())
        self.assertEqual(destination.read_text(encoding="utf-8"), "old")

        pending = self.engine.move(
            "Documents",
            "a.txt",
            "Downloads",
            "a.txt",
            overwrite=True,
        )
        self.assertTrue(pending["requires_confirmation"])
        self.assertEqual(destination.read_text(encoding="utf-8"), "old")

        allowed = self.engine.move(
            "Documents",
            "a.txt",
            "Downloads",
            "a.txt",
            overwrite=True,
            confirmed=True,
        )
        self.assertTrue(allowed["success"])
        self.assertTrue(allowed["overwritten"])
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_text(encoding="utf-8"), "new")

    def test_move_rejects_same_file_and_symlink_source(self):
        source = self.docs / "a.txt"
        source.write_text("data", encoding="utf-8")

        result = self.engine.move(
            "Documents",
            "a.txt",
            "Documents",
            "a.txt",
            overwrite=True,
            confirmed=True,
        )
        self.assertEqual(result["code"], "same_file")
        self.assertTrue(source.exists())

        (self.docs / "link.txt").symlink_to(source)
        result = self.engine.move(
            "Documents", "link.txt", "Downloads", "x.txt", confirmed=True
        )
        self.assertEqual(result["code"], "symlink_target")
        self.assertTrue(source.exists())
        self.assertFalse((self.downloads / "x.txt").exists())

    def test_move_cross_device_fallback_copy_then_unlink(self):
        source = self.docs / "a.txt"
        source.write_text("payload", encoding="utf-8")
        destination = self.downloads / "b.txt"
        real_replace = os.replace

        def fake_replace(src, dst):
            if Path(src) == source and Path(dst) == destination:
                raise OSError(errno.EXDEV, "cross-device")
            return real_replace(src, dst)

        with patch(
            "app.actions.file_transfer.os.replace",
            side_effect=fake_replace,
        ):
            result = self.engine.move(
                "Documents", "a.txt", "Downloads", "b.txt",
                confirmed=True,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "copy+unlink")
        self.assertFalse(result["atomic"])
        self.assertTrue(result["durable"])
        self.assertFalse(result["partial"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["bytes"], 7)
        self.assertFalse(result["overwritten"])
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_text(encoding="utf-8"), "payload")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self._assert_no_temp_files(self.downloads)

    def test_move_cross_device_partial_when_unlink_fails(self):
        source = self.docs / "a.txt"
        source.write_text("payload", encoding="utf-8")
        destination = self.downloads / "b.txt"
        real_replace = os.replace
        real_unlink = os.unlink

        def fake_replace(src, dst):
            if Path(src) == source and Path(dst) == destination:
                raise OSError(errno.EXDEV, "cross-device")
            return real_replace(src, dst)

        def fake_unlink(path, *args, **kwargs):
            if Path(path) == source:
                raise OSError("cannot unlink")
            return real_unlink(path, *args, **kwargs)

        with patch(
            "app.actions.file_transfer.os.replace",
            side_effect=fake_replace,
        ), patch(
            "app.actions.file_transfer.os.unlink",
            side_effect=fake_unlink,
        ):
            result = self.engine.move(
                "Documents", "a.txt", "Downloads", "b.txt",
                confirmed=True,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "unlink_failed")
        self.assertIn("original could not be removed", result["message"])
        self.assertNotIn("cannot unlink", result["message"])
        self.assertTrue(result["partial"])
        self.assertTrue(result["durable"])
        self.assertEqual(result["bytes"], 7)
        self.assertEqual(result["method"], "copy+unlink")
        self.assertTrue(source.exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "payload")
        self.assertEqual(destination.read_text(encoding="utf-8"), "payload")
        self._assert_no_temp_files(self.downloads)

    def test_move_cross_device_copy_failure_keeps_source(self):
        source = self.docs / "a.txt"
        source.write_text("payload", encoding="utf-8")
        destination = self.downloads / "b.txt"

        def fake_replace(src, dst):
            if Path(src) == source and Path(dst) == destination:
                raise OSError(errno.EXDEV, "cross-device")
            raise OSError(errno.EIO, "write failed")

        with patch(
            "app.actions.file_transfer.os.replace",
            side_effect=fake_replace,
        ):
            result = self.engine.move(
                "Documents", "a.txt", "Downloads", "b.txt",
                confirmed=True,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "copy_failed")
        self.assertNotIn("write failed", result["message"])
        self.assertFalse(result["partial"])
        self.assertTrue(source.exists())
        self.assertFalse(destination.exists())
        self._assert_no_temp_files(self.downloads)

    # ------------------------------------------------------------------
    # Rename
    # ------------------------------------------------------------------

    def test_rename_success_and_subdirectory(self):
        source = self.docs / "a.txt"
        source.write_text("data", encoding="utf-8")

        result = self.engine.rename(
            "Documents", "a.txt", "b.txt", confirmed=True
        )

        self.assertTrue(result["success"])
        self.assertIsNone(result["code"])
        self.assertEqual(result["source"], "a.txt")
        self.assertEqual(result["destination"], "b.txt")
        self.assertEqual(result["target"], "b.txt")
        self.assertEqual(result["bytes"], 4)
        self.assertFalse(result["overwritten"])
        self.assertTrue(result["durable"])
        self.assertFalse(result["partial"])
        self.assertTrue(result["atomic"])
        self.assertEqual(result["method"], "rename-in-place")
        self.assertTrue(result["verified"])
        self.assertFalse(source.exists())
        self.assertEqual(
            (self.docs / "b.txt").read_text(encoding="utf-8"), "data"
        )
        self._assert_no_temp_files(self.docs)

        (self.docs / "sub" / "nested.txt").write_text(
            "nested", encoding="utf-8"
        )
        nested = self.engine.rename(
            "Documents", "sub/nested.txt", "renamed.txt", confirmed=True
        )
        self.assertTrue(nested["success"])
        self.assertEqual(nested["destination"], "sub/renamed.txt")
        self.assertTrue((self.docs / "sub" / "renamed.txt").is_file())

        maximum = self.engine.rename(
            "Documents", "b.txt", "x" * 255, confirmed=True
        )
        self.assertTrue(maximum["success"])
        self.assertTrue((self.docs / ("x" * 255)).is_file())

    def test_rename_never_overwrites(self):
        (self.docs / "a.txt").write_text("one", encoding="utf-8")
        (self.docs / "b.txt").write_text("two", encoding="utf-8")

        preview = self.engine.preview_rename(
            "Documents", "a.txt", "b.txt"
        )
        self.assertFalse(preview["success"])
        self.assertEqual(preview["code"], "destination_exists")
        self.assertTrue(preview["preview"]["destination_exists"])

        result = self.engine.rename(
            "Documents", "a.txt", "b.txt", confirmed=True
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "destination_exists")
        self.assertEqual(
            (self.docs / "a.txt").read_text(encoding="utf-8"), "one"
        )
        self.assertEqual(
            (self.docs / "b.txt").read_text(encoding="utf-8"), "two"
        )

    def test_rename_rejects_invalid_names(self):
        source = self.docs / "a.txt"
        source.write_text("data", encoding="utf-8")

        for name in (
            "",
            ".",
            "..",
            "../escape.txt",
            "a/b.txt",
            "a\\b.txt",
            "a..b",
            "bad\x00name",
        ):
            with self.subTest(name=name):
                result = self.engine.rename(
                    "Documents", "a.txt", name, confirmed=True
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], "invalid_name")

        for name in ("x" * 256, "ł" * 200):
            with self.subTest(name_length=len(name)):
                result = self.engine.rename(
                    "Documents", "a.txt", name, confirmed=True
                )
                self.assertEqual(result["code"], "name_too_long")

        self.assertTrue(source.exists())
        self.assertEqual(self._names(self.docs), {"a.txt", "sub"})

    def test_rename_rejects_symlinks_directories_missing_and_same_name(self):
        source = self.docs / "a.txt"
        source.write_text("data", encoding="utf-8")

        (self.docs / "link.txt").symlink_to(source)
        result = self.engine.rename(
            "Documents", "link.txt", "renamed.txt", confirmed=True
        )
        self.assertEqual(result["code"], "symlink_target")
        self.assertTrue(source.exists())

        result = self.engine.rename(
            "Documents", "sub", "other", confirmed=True
        )
        self.assertEqual(result["code"], "source_is_directory")

        result = self.engine.rename(
            "Documents", "missing.txt", "other.txt", confirmed=True
        )
        self.assertEqual(result["code"], "source_not_found")

        (self.docs / "destlink").symlink_to(self.outside)
        result = self.engine.rename(
            "Documents", "a.txt", "destlink", confirmed=True
        )
        self.assertEqual(result["code"], "destination_symlink")

        (self.docs / "existing").mkdir()
        result = self.engine.rename(
            "Documents", "a.txt", "existing", confirmed=True
        )
        self.assertEqual(result["code"], "destination_is_directory")

        result = self.engine.rename(
            "Documents", "a.txt", "a.txt", confirmed=True
        )
        self.assertEqual(result["code"], "same_file")
        self.assertTrue(source.exists())

        result = self.engine.rename(
            "Documents", "../escape/a.txt", "x.txt", confirmed=True
        )
        self.assertEqual(result["code"], "traversal")

    # ------------------------------------------------------------------
    # Errors, resolver failures and result shape
    # ------------------------------------------------------------------

    def test_invalid_folders_and_resolver_failure(self):
        (self.docs / "a.txt").write_text("data", encoding="utf-8")

        for folder in ("/etc", "", None, 123):
            with self.subTest(folder=folder):
                result = self.engine.copy(
                    folder, "a.txt", "Downloads", "x.txt", confirmed=True
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], "invalid_folder")

        class BoomResolver:
            def base_for_key(self, key):
                raise RuntimeError("secret boom")

        engine = FileTransferEngine(resolver=BoomResolver())
        result = engine.copy(
            "Documents", "a.txt", "Downloads", "x.txt", confirmed=True
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "invalid_folder")
        self.assertNotIn("secret", result["message"])
        self.assertNotIn("boom", result["message"])

    def test_quarantine_dir_is_accepted_and_never_created(self):
        engine = FileTransferEngine(
            resolver=self.resolver,
            quarantine_dir=self.quarantine,
        )
        (self.docs / "a.txt").write_text("data", encoding="utf-8")

        result = engine.copy(
            "Documents", "a.txt", "Downloads", "a.txt", confirmed=True
        )

        self.assertTrue(result["success"])
        self.assertFalse(self.quarantine.exists())

    def test_result_shape_is_json_safe_for_every_action(self):
        (self.docs / "a.txt").write_text("data", encoding="utf-8")
        (self.downloads / "a.txt").write_text("old", encoding="utf-8")

        results = [
            self.engine.preview_copy(
                "Documents", "a.txt", "Downloads", "b.txt"
            ),
            self.engine.preview_move(
                "Documents", "a.txt", "Downloads", "b.txt"
            ),
            self.engine.preview_rename(
                "Documents", "a.txt", "c.txt"
            ),
            self.engine.copy(
                "Documents", "a.txt", "Downloads", "b.txt"
            ),
            self.engine.preview_copy(
                "Documents", "a.txt", "Downloads", "a.txt"
            ),
        ]
        keys = {
            "success",
            "action",
            "requires_confirmation",
            "message",
            "code",
            "error_code",
            "source",
            "destination",
            "target",
            "preview",
            "bytes",
            "overwritten",
            "durable",
            "partial",
            "atomic",
            "method",
            "verified",
            "mtime_preserved",
        }
        for value in results:
            json.dumps(value)
            self.assertTrue(keys.issubset(value.keys()))
            self.assertIsInstance(value["message"], str)
            self.assertIsInstance(value["preview"], (dict, type(None)))


if __name__ == "__main__":
    unittest.main()
