"""Tests for the isolated, safety-first ZIP archive engine.

Every test uses a temporary home with approved Documents/Downloads folders and
an unrelated ``outside`` directory. No test touches the real user home, the
network, a model, the registry, or existing application wiring.
"""

import json
import os
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.actions.archive_zip import (
    MAX_COMPRESSION_RATIO,
    MAX_CREATE_ENTRIES,
    MAX_ENTRY_BYTES,
    MAX_EXTRACT_ENTRIES,
    MAX_TOTAL_BYTES,
    RATIO_MIN_OUTPUT_BYTES,
    ZipEngine,
)
from app.actions.file_scope import UserFolderResolver


class ZipEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.docs = self.home / "Documents"
        self.docs.mkdir()
        self.downloads = self.home / "Pobrane"
        self.downloads.mkdir()
        self.desktop = self.home / "Desktop"
        self.desktop.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()

        self.resolver = UserFolderResolver(
            home=self.home,
            xdg_lookup=lambda _name: None,
        )
        self.engine = ZipEngine(resolver=self.resolver)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _names(self, directory):
        return {path.name for path in directory.iterdir()}

    def _assert_no_temp_files(self, directory):
        leftovers = [
            path.name
            for path in directory.iterdir()
            if path.name.startswith(".misty-zip-")
            or path.name.startswith(".misty-extract-")
            or path.name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])

    def _make_zip(self, path, entries, compression=zipfile.ZIP_DEFLATED):
        """Write ``(name, data[, mode])`` entries exactly as given.

        Entry names are not sanitised here on purpose: tests need to produce
        hostile archives (absolute paths, ``..``, backslashes, duplicates).
        """
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for entry in entries:
                name, data = entry[0], entry[1]
                mode = (
                    entry[2]
                    if len(entry) > 2
                    else stat.S_IFREG | 0o600
                )
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = compression
                info.create_system = 3
                info.external_attr = mode << 16
                archive.writestr(info, data)

    def _make_encrypted_zip(self, path):
        """Create a ZIP whose local and central headers claim encryption."""
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("secret.txt", (1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            zf.writestr(info, b"classified")

        data = bytearray(path.read_bytes())
        for signature, offset in (
            (b"PK\x03\x04", 6),
            (b"PK\x01\x02", 8),
        ):
            index = data.find(signature)
            self.assertGreaterEqual(index, 0)
            flags = int.from_bytes(
                data[index + offset : index + offset + 2], "little"
            )
            data[index + offset : index + offset + 2] = (
                flags | 0x1
            ).to_bytes(2, "little")
        path.write_bytes(bytes(data))

    def _create_fixture_zip(self, name="bundle.zip", folder=None):
        folder = folder or self.docs
        (folder / "alpha.txt").write_text("alpha", encoding="utf-8")
        (folder / "sub").mkdir(exist_ok=True)
        (folder / "sub" / "beta.bin").write_bytes(b"beta-data")
        result = self.engine.create(
            "Documents",
            ["alpha.txt", "sub/beta.bin"],
            name,
            confirmed=True,
        )
        self.assertTrue(result["success"], result)
        return folder / name

    # ------------------------------------------------------------------
    # Approval gating: zero writes
    # ------------------------------------------------------------------

    def test_create_requires_exact_confirmation_and_writes_nothing(self):
        (self.docs / "alpha.txt").write_text("alpha", encoding="utf-8")

        for confirmed in (False, None, 1, "yes"):
            with self.subTest(confirmed=confirmed):
                result = self.engine.create(
                    "Documents",
                    ["alpha.txt"],
                    "bundle.zip",
                    confirmed=confirmed,
                )
                self.assertFalse(result["success"])
                self.assertTrue(result["requires_confirmation"])
                self.assertEqual(result["code"], "confirmation_required")
                self.assertEqual(result["error_code"], "confirmation_required")
                self.assertEqual(result["archive"], "bundle.zip")
                self.assertIsNone(result["bytes"])
                self.assertIsNotNone(result["preview"])
                self.assertEqual(result["preview"]["entries"], 1)
                self.assertEqual(result["entries"], 1)
                self.assertEqual(
                    result["preview"]["items"],
                    [
                        {
                            "name": "alpha.txt",
                            "size": 5,
                            "compressed_size": None,
                            "kind": "file",
                        }
                    ],
                )
                self.assertFalse((self.docs / "bundle.zip").exists())
                self._assert_no_temp_files(self.docs)

    def test_extract_requires_exact_confirmation_and_writes_nothing(self):
        archive = self._create_fixture_zip()

        for confirmed in (False, None, 1, "yes"):
            with self.subTest(confirmed=confirmed):
                result = self.engine.extract(
                    "Documents",
                    archive.name,
                    "Downloads",
                    "unpacked",
                    confirmed=confirmed,
                )
                self.assertFalse(result["success"])
                self.assertTrue(result["requires_confirmation"])
                self.assertEqual(result["code"], "confirmation_required")
                self.assertEqual(result["archive"], archive.name)
                self.assertEqual(result["destination"], "unpacked")
                self.assertIsNotNone(result["preview"])
                self.assertFalse((self.downloads / "unpacked").exists())
                self._assert_no_temp_files(self.downloads)

    # ------------------------------------------------------------------
    # Round trip and preview accuracy
    # ------------------------------------------------------------------

    def test_create_preview_is_read_only_and_accurate(self):
        (self.docs / "alpha.txt").write_text("alpha", encoding="utf-8")
        (self.docs / "sub").mkdir()
        (self.docs / "sub" / "beta.bin").write_bytes(b"beta-data")

        preview = self.engine.preview_create(
            "Documents",
            ["alpha.txt", "sub/beta.bin"],
            "bundle.zip",
        )

        self.assertTrue(preview["success"])
        self.assertFalse(preview["requires_confirmation"])
        self.assertIsNone(preview["code"])
        self.assertEqual(preview["archive"], "bundle.zip")
        self.assertEqual(preview["destination"], "bundle.zip")
        self.assertEqual(preview["bytes"], 14)
        self.assertEqual(preview["entry_count"], 2)
        self.assertEqual(preview["entries"], 2)
        self.assertEqual(preview["preview"]["entries"], 2)
        self.assertEqual(
            [entry["name"] for entry in preview["items"]],
            ["alpha.txt", "sub/beta.bin"],
        )
        self.assertEqual(
            [entry["size"] for entry in preview["items"]], [5, 9]
        )
        details = preview["preview"]
        self.assertEqual(details["operation"], "create")
        self.assertFalse(details["archive_exists"])
        self.assertTrue(details["atomic"])
        self.assertEqual(details["mode"], "0o600")
        self.assertFalse((self.docs / "bundle.zip").exists())
        self._assert_no_temp_files(self.docs)

        created = self.engine.create(
            "Documents",
            ["alpha.txt", "sub/beta.bin"],
            "bundle.zip",
            confirmed=True,
        )
        self.assertTrue(created["success"])
        with zipfile.ZipFile(self.docs / "bundle.zip") as archive:
            self.assertEqual(
                archive.namelist(), ["alpha.txt", "sub/beta.bin"]
            )
            self.assertEqual(
                [info.file_size for info in archive.infolist()], [5, 9]
            )
            self.assertEqual(archive.read("sub/beta.bin"), b"beta-data")

    def test_create_extract_round_trip_with_modes_and_nested_dirs(self):
        (self.docs / "alpha.txt").write_text("alpha", encoding="utf-8")
        (self.docs / "nested").mkdir()
        (self.docs / "nested" / "deep.bin").write_bytes(b"\x00\x01\x02")

        created = self.engine.create(
            "Documents",
            ["alpha.txt", "nested/deep.bin"],
            "bundle.zip",
            confirmed=True,
        )

        archive = self.docs / "bundle.zip"
        self.assertTrue(created["success"])
        self.assertIsNone(created["code"])
        self.assertEqual(created["action"], "create")
        self.assertEqual(created["archive"], "bundle.zip")
        self.assertEqual(created["destination"], "bundle.zip")
        self.assertEqual(created["bytes"], 8)
        self.assertEqual(created["entry_count"], 2)
        self.assertTrue(created["verified"])
        self.assertTrue(archive.is_file())
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self._assert_no_temp_files(self.docs)

        preview = self.engine.preview_extract(
            "Documents", "bundle.zip", "Downloads"
        )
        self.assertTrue(preview["success"])
        self.assertEqual(preview["archive"], "bundle.zip")
        self.assertEqual(preview["destination"], "bundle")
        self.assertEqual(preview["bytes"], 8)
        self.assertEqual(preview["entries"], 2)
        self.assertEqual(
            [entry["name"] for entry in preview["items"]],
            ["alpha.txt", "nested/deep.bin"],
        )
        self.assertEqual(
            [entry["kind"] for entry in preview["items"]],
            ["file", "file"],
        )
        self.assertFalse((self.downloads / "bundle").exists())
        self._assert_no_temp_files(self.downloads)

        extracted = self.engine.extract(
            "Documents", "bundle.zip", "Downloads", "bundle", confirmed=True
        )
        destination = self.downloads / "bundle"
        self.assertTrue(extracted["success"])
        self.assertIsNone(extracted["code"])
        self.assertEqual(extracted["action"], "extract")
        self.assertEqual(extracted["archive"], "bundle.zip")
        self.assertEqual(extracted["destination"], "bundle")
        self.assertEqual(extracted["bytes"], 8)
        self.assertEqual(extracted["entry_count"], 2)
        self.assertTrue(extracted["verified"])
        self.assertEqual(
            (destination / "alpha.txt").read_text(encoding="utf-8"), "alpha"
        )
        self.assertEqual(
            (destination / "nested" / "deep.bin").read_bytes(),
            b"\x00\x01\x02",
        )
        self.assertEqual(
            stat.S_IMODE(destination.stat().st_mode), 0o700
        )
        self.assertEqual(
            stat.S_IMODE((destination / "nested").stat().st_mode), 0o700
        )
        self.assertEqual(
            stat.S_IMODE((destination / "alpha.txt").stat().st_mode), 0o600
        )
        self.assertTrue(archive.is_file())
        self._assert_no_temp_files(self.downloads)

    def test_tool_layer_preview_contract_uses_entry_counts(self):
        (self.docs / "a.txt").write_text("alpha", encoding="utf-8")
        (self.docs / "b.txt").write_text("beta", encoding="utf-8")

        preview = self.engine.preview_create(
            "Documents", ["a.txt", "b.txt"], "bundle.zip"
        )
        details = preview["preview"]
        self.assertIsInstance(details["entries"], int)
        self.assertEqual(details["entries"], 2)
        self.assertEqual(
            f"Create {details['archive']} from "
            f"{details['entries']} file(s).",
            "Create bundle.zip from 2 file(s).",
        )

        created = self.engine.create(
            "Documents", ["a.txt", "b.txt"], "bundle.zip", confirmed=True
        )
        self.assertEqual(created["entries"], 2)

        extract_preview = self.engine.preview_extract(
            "Documents", "bundle.zip", "Downloads"
        )
        extract_details = extract_preview["preview"]
        self.assertIsInstance(extract_details["entries"], int)
        self.assertEqual(extract_details["entries"], 2)
        self.assertEqual(extract_details["destination"], "bundle")
        self.assertEqual(
            f"Extract {extract_details['entries']} file(s) into "
            f"{extract_details['destination']}.",
            "Extract 2 file(s) into bundle.",
        )

    def test_extract_archive_from_an_approved_subdirectory(self):
        (self.docs / "archives").mkdir()
        self._make_zip(
            self.docs / "archives" / "bundle.zip",
            [("a.txt", b"data")],
        )

        result = self.engine.extract(
            "Documents",
            "archives/bundle.zip",
            "Downloads",
            "out",
            confirmed=True,
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(
            (self.downloads / "out" / "a.txt").read_text(encoding="utf-8"),
            "data",
        )

    def test_extract_missing_destination_parent_is_rejected(self):
        archive = self._create_fixture_zip()

        result = self.engine.extract(
            "Documents",
            archive.name,
            "Downloads",
            "missing/out",
            confirmed=True,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "missing_parent")
        self.assertFalse((self.downloads / "missing").exists())
        self._assert_no_temp_files(self.downloads)

    def test_extract_supports_explicit_directory_entries(self):
        archive = self.docs / "folders.zip"
        self._make_zip(
            archive,
            [
                ("folder/", b"", stat.S_IFDIR | 0o755),
                ("folder/data.txt", b"data", stat.S_IFREG | 0o755),
            ],
        )

        result = self.engine.extract(
            "Documents", "folders.zip", "Downloads", "out", confirmed=True
        )

        self.assertTrue(result["success"], result)
        target = self.downloads / "out" / "folder"
        self.assertTrue(target.is_dir())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertEqual(
            (target / "data.txt").read_text(encoding="utf-8"), "data"
        )
        self.assertEqual(
            stat.S_IMODE((target / "data.txt").stat().st_mode), 0o600
        )
        self.assertEqual(result["entries"], 2)
        self.assertEqual(
            [(entry["name"], entry["kind"]) for entry in result["items"]],
            [("folder", "directory"), ("folder/data.txt", "file")],
        )

    # ------------------------------------------------------------------
    # Overwrite policy
    # ------------------------------------------------------------------

    def test_create_overwrite_denied_then_allowed(self):
        (self.docs / "one.txt").write_text("one", encoding="utf-8")
        (self.docs / "two.txt").write_text("two", encoding="utf-8")

        first = self.engine.create(
            "Documents", ["one.txt"], "bundle.zip", confirmed=True
        )
        self.assertTrue(first["success"])
        archive = self.docs / "bundle.zip"
        self.assertEqual(archive.read_bytes()[:2], b"PK")

        denied = self.engine.create(
            "Documents", ["two.txt"], "bundle.zip", confirmed=True
        )
        self.assertFalse(denied["success"])
        self.assertEqual(denied["code"], "archive_exists")
        with zipfile.ZipFile(archive) as opened:
            self.assertEqual(opened.namelist(), ["one.txt"])

        pending = self.engine.create(
            "Documents",
            ["two.txt"],
            "bundle.zip",
            overwrite=True,
        )
        self.assertTrue(pending["requires_confirmation"])
        self.assertTrue(pending["preview"]["archive_exists"])
        with zipfile.ZipFile(archive) as opened:
            self.assertEqual(opened.namelist(), ["one.txt"])

        allowed = self.engine.create(
            "Documents",
            ["two.txt"],
            "bundle.zip",
            overwrite=True,
            confirmed=True,
        )
        self.assertTrue(allowed["success"])
        self.assertTrue(allowed["preview"]["archive_exists"])
        with zipfile.ZipFile(archive) as opened:
            self.assertEqual(opened.namelist(), ["two.txt"])
            self.assertEqual(opened.read("two.txt"), b"two")
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self._assert_no_temp_files(self.docs)

    def test_extract_refuses_existing_destination(self):
        archive = self._create_fixture_zip()
        existing = self.downloads / "unpacked"
        existing.mkdir()
        (existing / "sentinel.txt").write_text("keep", encoding="utf-8")

        preview = self.engine.preview_extract(
            "Documents", archive.name, "Downloads", "unpacked"
        )
        self.assertFalse(preview["success"])
        self.assertEqual(preview["code"], "destination_exists")

        result = self.engine.extract(
            "Documents", archive.name, "Downloads", "unpacked", confirmed=True
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "destination_exists")
        self.assertEqual(
            (existing / "sentinel.txt").read_text(encoding="utf-8"), "keep"
        )
        self.assertEqual(self._names(existing), {"sentinel.txt"})

        file_destination = self.downloads / "plainfile"
        file_destination.write_text("keep", encoding="utf-8")
        result = self.engine.extract(
            "Documents", archive.name, "Downloads", "plainfile",
            confirmed=True,
        )
        self.assertEqual(result["code"], "destination_exists")
        self.assertEqual(
            file_destination.read_text(encoding="utf-8"), "keep"
        )

    # ------------------------------------------------------------------
    # Traversal, absolute, symlink and special-file rejection
    # ------------------------------------------------------------------

    def test_create_rejects_scope_violations_and_special_sources(self):
        (self.docs / "alpha.txt").write_text("alpha", encoding="utf-8")
        (self.outside / "outside.txt").write_text("outside", encoding="utf-8")
        (self.docs / "sub").mkdir()
        (self.docs / "link.txt").symlink_to(self.outside / "outside.txt")
        (self.docs / "dirlink").symlink_to(self.outside)
        os.mkfifo(self.docs / "pipe")

        cases = [
            ("../outside.txt", "traversal"),
            ("a/../../outside.txt", "traversal"),
            ("/etc/passwd", "absolute_path"),
            ("", "empty_relative_path"),
            (".", "invalid_relative_path"),
            ("alpha.txt/", "invalid_relative_path"),
            ("link.txt", "symlink_target"),
            ("dirlink/outside.txt", "symlink_parent"),
            ("pipe", "source_not_regular"),
            ("sub", "source_is_directory"),
            (123, "invalid_relative_path"),
        ]
        for relative, expected in cases:
            with self.subTest(relative=relative):
                result = self.engine.create(
                    "Documents", [relative], "bundle.zip", confirmed=True
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], expected)

        self.assertFalse((self.docs / "bundle.zip").exists())
        self.assertEqual(
            (self.outside / "outside.txt").read_text(encoding="utf-8"),
            "outside",
        )
        self._assert_no_temp_files(self.docs)

    def test_extract_rejects_hostile_entry_paths(self):
        cases = [
            ("../evil.txt", "traversal"),
            ("a/../../evil.txt", "traversal"),
            ("..\\..\\evil.txt", "traversal"),
            ("/etc/passwd", "absolute_path"),
            ("\\evil.txt", "absolute_path"),
            ("C:\\evil.txt", "absolute_path"),
            ("C:/evil.txt", "absolute_path"),
            ("", "invalid_entry_name"),
            (".", "invalid_entry_name"),
        ]
        for index, (entry_name, expected) in enumerate(cases):
            with self.subTest(entry_name=entry_name):
                archive = self.docs / ("bad%d.zip" % index)
                self._make_zip(archive, [(entry_name, b"payload")])
                destination = "out%d" % index

                preview = self.engine.preview_extract(
                    "Documents", archive.name, "Downloads", destination
                )
                self.assertFalse(preview["success"])
                self.assertEqual(preview["code"], expected)

                result = self.engine.extract(
                    "Documents",
                    archive.name,
                    "Downloads",
                    destination,
                    confirmed=True,
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], expected)
                self.assertFalse((self.downloads / destination).exists())
                self.assertFalse((self.outside / "evil.txt").exists())
                self._assert_no_temp_files(self.downloads)

    def test_extract_rejects_symlink_entries(self):
        archive = self.docs / "link.zip"
        self._make_zip(
            archive,
            [
                (
                    "link",
                    b"../../outside/secret.txt",
                    stat.S_IFLNK | 0o777,
                )
            ],
        )

        preview = self.engine.preview_extract(
            "Documents", "link.zip", "Downloads", "out"
        )
        self.assertFalse(preview["success"])
        self.assertEqual(preview["code"], "symlink_entry")

        result = self.engine.extract(
            "Documents", "link.zip", "Downloads", "out", confirmed=True
        )
        self.assertEqual(result["code"], "symlink_entry")
        self.assertFalse((self.downloads / "out").exists())
        self._assert_no_temp_files(self.downloads)

    def test_extract_rejects_symlinked_or_special_archive_paths(self):
        real = self.outside / "real.zip"
        self._make_zip(real, [("a.txt", b"data")])
        (self.docs / "link.zip").symlink_to(real)
        (self.docs / "folder.zip").mkdir()
        (self.docs / "bad.zip").write_bytes(b"not a zip")
        os.mkfifo(self.docs / "pipe.zip")

        for archive_name, expected in (
            ("link.zip", "archive_symlink"),
            ("folder.zip", "archive_is_directory"),
            ("pipe.zip", "archive_not_regular"),
            ("missing.zip", "archive_not_found"),
            ("bad.zip", "invalid_archive"),
        ):
            with self.subTest(archive=archive_name):
                result = self.engine.preview_extract(
                    "Documents", archive_name, "Downloads", "out"
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], expected)
                self.assertFalse((self.downloads / "out").exists())
        self._assert_no_temp_files(self.downloads)

    def test_extract_rejects_scope_violations(self):
        archive = self._create_fixture_zip()

        cases = [
            ("Documents", "../bundle.zip", "Downloads", "out",
             "traversal"),
            ("Documents", "/tmp/bundle.zip", "Downloads", "out",
             "absolute_path"),
            ("Documents", archive.name + "/", "Downloads", "out",
             "invalid_relative_path"),
            ("Documents", archive.name, "Downloads", "../out",
             "traversal"),
            ("Documents", archive.name, "Downloads", "/tmp/out",
             "absolute_path"),
            ("Documents", archive.name, "Downloads", "out/",
             "invalid_relative_path"),
            ("Documents", archive.name, "Downloads", "",
             "empty_relative_path"),
        ]
        for folder, archive_path, dest_folder, dest_path, expected in cases:
            with self.subTest(
                archive=archive_path, destination=dest_path
            ):
                result = self.engine.extract(
                    folder,
                    archive_path,
                    dest_folder,
                    dest_path,
                    confirmed=True,
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], expected)

        self.assertFalse((self.root / "out").exists())
        self.assertFalse((self.outside / "out").exists())

    def test_extract_rejects_archive_under_symlinked_parent(self):
        real = self.outside / "bundle.zip"
        self._make_zip(real, [("a.txt", b"data")])
        (self.docs / "linkdir").symlink_to(self.outside)

        preview = self.engine.preview_extract(
            "Documents", "linkdir/bundle.zip", "Downloads", "out"
        )
        self.assertFalse(preview["success"])
        self.assertEqual(preview["code"], "symlink_parent")

        result = self.engine.extract(
            "Documents",
            "linkdir/bundle.zip",
            "Downloads",
            "out",
            confirmed=True,
        )
        self.assertEqual(result["code"], "symlink_parent")
        self.assertFalse((self.downloads / "out").exists())

    def test_extract_rejects_symlinked_destination_or_parent(self):
        archive = self._create_fixture_zip()
        (self.downloads / "link").symlink_to(self.outside)

        result = self.engine.extract(
            "Documents", archive.name, "Downloads", "link", confirmed=True
        )
        self.assertEqual(result["code"], "destination_symlink")

        result = self.engine.extract(
            "Documents", archive.name, "Downloads", "link/new",
            confirmed=True,
        )
        self.assertEqual(result["code"], "symlink_parent")
        self.assertFalse((self.outside / "new").exists())

    # ------------------------------------------------------------------
    # Entry, size and compression-ratio limits
    # ------------------------------------------------------------------

    def test_default_limits_match_contract(self):
        self.assertEqual(MAX_CREATE_ENTRIES, 100)
        self.assertEqual(MAX_EXTRACT_ENTRIES, 1000)
        self.assertEqual(MAX_TOTAL_BYTES, 8 * 1024 ** 3)
        self.assertEqual(MAX_ENTRY_BYTES, 2 * 1024 ** 3)
        self.assertEqual(MAX_COMPRESSION_RATIO, 200)
        self.assertEqual(RATIO_MIN_OUTPUT_BYTES, 100 * 1024 ** 2)

    def test_create_accepts_one_hundred_entries_and_rejects_more(self):
        for index in range(MAX_CREATE_ENTRIES):
            (self.docs / ("f%03d.txt" % index)).write_text(
                "x", encoding="utf-8"
            )
        names = ["f%03d.txt" % index for index in range(MAX_CREATE_ENTRIES)]

        allowed = self.engine.create(
            "Documents", names, "many.zip", confirmed=True
        )
        self.assertTrue(allowed["success"])
        self.assertEqual(allowed["entry_count"], MAX_CREATE_ENTRIES)
        self.assertEqual(allowed["bytes"], MAX_CREATE_ENTRIES)

        (self.docs / "extra.txt").write_text("x", encoding="utf-8")
        denied = self.engine.create(
            "Documents",
            names + ["extra.txt"],
            "too-many.zip",
            confirmed=True,
        )
        self.assertFalse(denied["success"])
        self.assertEqual(denied["code"], "too_many_entries")
        self.assertFalse((self.docs / "too-many.zip").exists())

    def test_create_rejects_empty_and_invalid_entry_lists(self):
        (self.docs / "a.txt").write_text("data", encoding="utf-8")

        for value, expected in (
            ([], "no_entries"),
            ("a.txt", "invalid_entries"),
            (None, "invalid_entries"),
            ([123], "invalid_relative_path"),
            ([""], "empty_relative_path"),
            (["a.txt", "a.txt"], "duplicate_entry"),
            (["a.txt", "./a.txt"], "duplicate_entry"),
            (["a\\b.txt"], "invalid_relative_path"),
        ):
            with self.subTest(value=value):
                result = self.engine.preview_create(
                    "Documents", value, "bundle.zip"
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], expected)
        self.assertFalse((self.docs / "bundle.zip").exists())

    def test_create_rejects_invalid_archive_names(self):
        (self.docs / "a.txt").write_text("data", encoding="utf-8")

        for name, expected in (
            ("bundle.tar", "archive_extension"),
            ("bundle", "archive_extension"),
            (".zip", "invalid_archive_name"),
            ("sub/bundle.zip", "invalid_archive_name"),
            ("..\\bundle.zip", "invalid_archive_name"),
            ("", "invalid_archive_name"),
            (None, "invalid_archive_name"),
            (123, "invalid_archive_name"),
            ("a" * 252 + ".zip", "invalid_archive_name"),
        ):
            with self.subTest(name=name):
                result = self.engine.create(
                    "Documents", ["a.txt"], name, confirmed=True
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], expected)
        self.assertEqual(self._names(self.docs), {"a.txt"})

    def test_extract_rejects_non_zip_extension(self):
        (self.docs / "a.txt").write_text("data", encoding="utf-8")
        source = self.docs / "archive.zip"
        self._make_zip(source, [("a.txt", b"data")])
        (self.docs / "archive.bin").write_bytes(source.read_bytes())

        result = self.engine.preview_extract(
            "Documents", "archive.bin", "Downloads", "out"
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "archive_extension")

    def test_create_total_size_limit(self):
        (self.docs / "a.bin").write_bytes(b"123456")

        with patch(
            "app.actions.archive_zip.MAX_TOTAL_BYTES", 4
        ):
            preview = self.engine.preview_create(
                "Documents", ["a.bin"], "bundle.zip"
            )
            self.assertFalse(preview["success"])
            self.assertEqual(preview["code"], "total_too_large")
            self.assertEqual(preview["preview"]["bytes"], 6)

            result = self.engine.create(
                "Documents", ["a.bin"], "bundle.zip", confirmed=True
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["code"], "total_too_large")

        self.assertFalse((self.docs / "bundle.zip").exists())
        self._assert_no_temp_files(self.docs)

    def test_extract_entry_count_limit(self):
        archive = self.docs / "many.zip"
        self._make_zip(
            archive,
            [("f%d.txt" % index, b"x") for index in range(3)],
        )

        with patch(
            "app.actions.archive_zip.MAX_EXTRACT_ENTRIES", 2
        ):
            result = self.engine.preview_extract(
                "Documents", "many.zip", "Downloads", "out"
            )
            self.assertEqual(result["code"], "too_many_entries")

            result = self.engine.extract(
                "Documents", "many.zip", "Downloads", "out", confirmed=True
            )
            self.assertEqual(result["code"], "too_many_entries")
        self.assertFalse((self.downloads / "out").exists())

    def test_extract_default_entry_limit_is_one_thousand(self):
        archive = self.docs / "thousand.zip"
        self._make_zip(
            archive,
            [("f%d.txt" % index, b"x") for index in range(1001)],
        )

        result = self.engine.preview_extract(
            "Documents", "thousand.zip", "Downloads", "out"
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "too_many_entries")
        self.assertEqual(
            result["preview"]["entry_limit"], MAX_EXTRACT_ENTRIES
        )

    def test_extract_single_entry_size_limit(self):
        archive = self.docs / "big.zip"
        self._make_zip(archive, [("big.bin", b"0123456789")])

        with patch("app.actions.archive_zip.MAX_ENTRY_BYTES", 4):
            result = self.engine.extract(
                "Documents", "big.zip", "Downloads", "out", confirmed=True
            )
            self.assertEqual(result["code"], "entry_too_large")
        self.assertFalse((self.downloads / "out").exists())
        self._assert_no_temp_files(self.downloads)

    def test_extract_total_size_limit(self):
        archive = self.docs / "total.zip"
        self._make_zip(
            archive,
            [("one.bin", b"1234"), ("two.bin", b"5678")],
        )

        with patch("app.actions.archive_zip.MAX_TOTAL_BYTES", 5):
            result = self.engine.extract(
                "Documents", "total.zip", "Downloads", "out", confirmed=True
            )
            self.assertEqual(result["code"], "archive_too_large")
        self.assertFalse((self.downloads / "out").exists())

    def test_extract_compression_ratio_limit(self):
        archive = self.docs / "bomb.zip"
        self._make_zip(
            archive,
            [("bomb.bin", b"\x00" * 65536)],
            compression=zipfile.ZIP_DEFLATED,
        )
        with zipfile.ZipFile(archive) as opened:
            info = opened.infolist()[0]
            self.assertGreater(
                info.file_size / max(info.compress_size, 1),
                MAX_COMPRESSION_RATIO,
            )

        with patch(
            "app.actions.archive_zip.RATIO_MIN_OUTPUT_BYTES", 0
        ), patch(
            "app.actions.archive_zip.MAX_COMPRESSION_RATIO", 10
        ):
            preview = self.engine.preview_extract(
                "Documents", "bomb.zip", "Downloads", "out"
            )
            self.assertFalse(preview["success"])
            self.assertEqual(preview["code"], "compression_ratio")

            result = self.engine.extract(
                "Documents", "bomb.zip", "Downloads", "out", confirmed=True
            )
            self.assertEqual(result["code"], "compression_ratio")

        self.assertFalse((self.downloads / "out").exists())
        self._assert_no_temp_files(self.downloads)

    def test_extract_allows_high_ratio_below_output_floor(self):
        archive = self.docs / "small-ratio.zip"
        self._make_zip(
            archive,
            [("small.bin", b"\x00" * 65536)],
            compression=zipfile.ZIP_DEFLATED,
        )

        result = self.engine.extract(
            "Documents", "small-ratio.zip", "Downloads", "out",
            confirmed=True,
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(
            (self.downloads / "out" / "small.bin").stat().st_size, 65536
        )

    # ------------------------------------------------------------------
    # Duplicates and encrypted entries
    # ------------------------------------------------------------------

    def test_create_rejects_duplicate_archive_names(self):
        (self.docs / "a.txt").write_text("data", encoding="utf-8")
        (self.docs / "sub").mkdir()
        (self.docs / "sub" / "a.txt").write_text("other", encoding="utf-8")

        result = self.engine.preview_create(
            "Documents", ["a.txt", "a.txt"], "bundle.zip"
        )
        self.assertEqual(result["code"], "duplicate_entry")
        self.assertEqual(result["preview"]["entries"], 0)
        self.assertEqual(result["preview"]["items"], [])

        result = self.engine.create(
            "Documents",
            ["a.txt", "sub/../a.txt"],
            "bundle.zip",
            confirmed=True,
        )
        self.assertEqual(result["code"], "traversal")
        self.assertFalse((self.docs / "bundle.zip").exists())

    def test_extract_rejects_duplicate_entry_names(self):
        archive = self.docs / "dupes.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._make_zip(
                archive,
                [("same.txt", b"one"), ("same.txt", b"two")],
            )

        preview = self.engine.preview_extract(
            "Documents", "dupes.zip", "Downloads", "out"
        )
        self.assertEqual(preview["code"], "duplicate_entry")

        result = self.engine.extract(
            "Documents", "dupes.zip", "Downloads", "out", confirmed=True
        )
        self.assertEqual(result["code"], "duplicate_entry")
        self.assertFalse((self.downloads / "out").exists())

    def test_extract_rejects_encrypted_entries(self):
        archive = self.docs / "encrypted.zip"
        self._make_encrypted_zip(archive)

        preview = self.engine.preview_extract(
            "Documents", "encrypted.zip", "Downloads", "out"
        )
        self.assertFalse(preview["success"])
        self.assertEqual(preview["code"], "encrypted_entry")

        result = self.engine.extract(
            "Documents", "encrypted.zip", "Downloads", "out", confirmed=True
        )
        self.assertEqual(result["code"], "encrypted_entry")
        self.assertFalse((self.downloads / "out").exists())
        self._assert_no_temp_files(self.downloads)

    # ------------------------------------------------------------------
    # Failure hygiene and no partial output
    # ------------------------------------------------------------------

    def test_create_failure_removes_temp_and_keeps_sources(self):
        source = self.docs / "a.txt"
        source.write_text("data", encoding="utf-8")

        with patch(
            "app.actions.archive_zip.os.replace",
            side_effect=OSError("RAW-CREATE-FAILURE"),
        ):
            result = self.engine.create(
                "Documents", ["a.txt"], "bundle.zip", confirmed=True
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "create_failed")
        self.assertNotIn("RAW-CREATE-FAILURE", result["message"])
        self.assertNotIn("RAW-CREATE-FAILURE", json.dumps(result))
        self.assertEqual(source.read_text(encoding="utf-8"), "data")
        self.assertFalse((self.docs / "bundle.zip").exists())
        self._assert_no_temp_files(self.docs)

    def test_extract_failure_removes_partial_directory(self):
        archive = self._create_fixture_zip()

        with patch(
            "app.actions.archive_zip.os.replace",
            side_effect=OSError("RAW-EXTRACT-FAILURE"),
        ):
            result = self.engine.extract(
                "Documents", archive.name, "Downloads", "out",
                confirmed=True,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "extract_failed")
        self.assertNotIn("RAW-EXTRACT-FAILURE", result["message"])
        self.assertNotIn("RAW-EXTRACT-FAILURE", json.dumps(result))
        self.assertFalse((self.downloads / "out").exists())
        self._assert_no_temp_files(self.downloads)
        self.assertTrue(archive.is_file())

    def test_extract_corrupt_payload_removes_partial_directory(self):
        archive = self.docs / "corrupt.zip"
        with zipfile.ZipFile(archive, "w") as opened:
            info = zipfile.ZipInfo("corrupt.txt", (1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            opened.writestr(info, b"payload")

        data = bytearray(archive.read_bytes())
        payload = data.find(b"payload")
        self.assertGreaterEqual(payload, 0)
        data[payload : payload + 7] = b"BROKEN!"
        archive.write_bytes(bytes(data))

        result = self.engine.extract(
            "Documents", "corrupt.zip", "Downloads", "out", confirmed=True
        )
        self.assertFalse(result["success"])
        self.assertIn(
            result["code"], {"invalid_archive", "extract_failed"}
        )
        self.assertFalse((self.downloads / "out").exists())
        self._assert_no_temp_files(self.downloads)

    # ------------------------------------------------------------------
    # Scope escape through the resolver
    # ------------------------------------------------------------------

    def test_resolver_escape_is_rejected_for_both_operations(self):
        (self.docs / "a.txt").write_text("data", encoding="utf-8")
        archive = self._create_fixture_zip()

        class OutsideResolver:
            home = self.home

            def base_for_key(self, key):
                return self.outside

        engine = ZipEngine(resolver=OutsideResolver())

        created = engine.create(
            "Documents", ["a.txt"], "bundle.zip", confirmed=True
        )
        self.assertEqual(created["code"], "invalid_folder")
        self.assertFalse((self.outside / "bundle.zip").exists())

        extracted = engine.extract(
            "Documents", archive.name, "Downloads", "out", confirmed=True
        )
        self.assertEqual(extracted["code"], "invalid_folder")
        self.assertFalse((self.outside / "out").exists())

        for folder in ("/etc", "", None, 123):
            with self.subTest(folder=folder):
                result = self.engine.preview_create(
                    folder, ["a.txt"], "bundle.zip"
                )
                self.assertEqual(result["code"], "invalid_folder")

    def test_resolver_exception_does_not_leak_text(self):
        class BoomResolver:
            def base_for_key(self, key):
                raise RuntimeError("SECRET-RESOLVER-TEXT")

        engine = ZipEngine(resolver=BoomResolver())
        result = engine.create(
            "Documents", ["a.txt"], "bundle.zip", confirmed=True
        )

        self.assertEqual(result["code"], "invalid_folder")
        self.assertNotIn(
            "SECRET-RESOLVER-TEXT", json.dumps(result)
        )

    # ------------------------------------------------------------------
    # Result shape and content hygiene
    # ------------------------------------------------------------------

    def test_no_content_or_exception_leak_in_results(self):
        secret = "TOPSECRET-ARCHIVE-CONTENT"
        (self.docs / "note.txt").write_text(secret, encoding="utf-8")

        preview_create = self.engine.preview_create(
            "Documents", ["note.txt"], "bundle.zip"
        )
        created = self.engine.create(
            "Documents", ["note.txt"], "bundle.zip", confirmed=True
        )
        preview_extract = self.engine.preview_extract(
            "Documents", "bundle.zip", "Downloads", "out"
        )
        extracted = self.engine.extract(
            "Documents", "bundle.zip", "Downloads", "out", confirmed=True
        )

        for result in (
            preview_create,
            created,
            preview_extract,
            extracted,
        ):
            dumped = json.dumps(result)
            self.assertNotIn("TOPSECRET", dumped)
            self.assertNotIn("note.txt: TOPSECRET", dumped)
        self.assertEqual(
            (self.downloads / "out" / "note.txt").read_text(
                encoding="utf-8"
            ),
            secret,
        )

    def test_result_shape_is_json_safe_for_every_action(self):
        (self.docs / "a.txt").write_text("data", encoding="utf-8")

        results = [
            self.engine.preview_create(
                "Documents", ["a.txt"], "bundle.zip"
            ),
            self.engine.create(
                "Documents", ["a.txt"], "bundle.zip"
            ),
            self.engine.create(
                "Documents", ["a.txt"], "bundle.zip", confirmed=True
            ),
            self.engine.preview_extract(
                "Documents", "bundle.zip", "Downloads", "out"
            ),
            self.engine.extract(
                "Documents", "bundle.zip", "Downloads", "out"
            ),
            self.engine.extract(
                "Documents", "bundle.zip", "Downloads", "out",
                confirmed=True,
            ),
            self.engine.preview_create(
                "Documents", ["a.txt"], "bundle.tar"
            ),
        ]

        required = {
            "success",
            "action",
            "code",
            "message",
            "archive",
            "entries",
            "bytes",
            "destination",
            "preview",
            "verified",
        }
        for result in results:
            json.dumps(result)
            self.assertTrue(required.issubset(result.keys()))
            self.assertIsInstance(result["success"], bool)
            self.assertIsInstance(result["action"], str)
            self.assertIsInstance(result["message"], str)
            self.assertIsInstance(result["verified"], bool)
            self.assertIsInstance(result["entries"], int)
            self.assertIsInstance(result["items"], list)
            for entry in result["items"]:
                self.assertIsInstance(entry, dict)
                self.assertIsInstance(entry["name"], str)
                self.assertIsInstance(entry["size"], int)
                self.assertTrue(
                    entry["compressed_size"] is None
                    or isinstance(entry["compressed_size"], int)
                )
                self.assertIn(entry["kind"], {"file", "directory"})


if __name__ == "__main__":
    unittest.main()
