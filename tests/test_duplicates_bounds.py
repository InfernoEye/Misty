"""Resource bounds for the duplicate finder: bytes, entries, time, races."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.actions import duplicates as duplicates_module
from app.actions.duplicates import DuplicateFinder
from app.actions.file_scope import UserFolderResolver


class DuplicateBoundsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.docs = self.home / "Documents"
        self.docs.mkdir()
        self.finder = DuplicateFinder(
            resolver=UserFolderResolver(
                home=self.home, xdg_lookup=lambda key: None
            )
        )

    def write(self, relative, data):
        path = self.docs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_hash_bytes_stay_within_patched_budget(self):
        size = 4096
        self.write("a.bin", b"x" * size)
        self.write("b.bin", b"x" * size)
        reads = []
        real_read = os.read

        def counting_read(fd, count):
            chunk = real_read(fd, count)
            reads.append(len(chunk))
            return chunk

        budget = 1000
        with mock.patch.object(
            duplicates_module, "MAX_TOTAL_HASH_BYTES", budget
        ), mock.patch.object(
            duplicates_module, "CHUNK_SIZE", 256
        ), mock.patch.object(
            duplicates_module.os, "read", side_effect=counting_read
        ):
            result = self.finder.find("Documents")

        self.assertTrue(result["success"], result)
        self.assertEqual(result["duplicate_groups"], 0)
        self.assertLessEqual(sum(reads), budget)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "hashing budget")

    def test_file_growing_between_walk_and_read_is_skipped(self):
        first = self.write("a.bin", b"same")
        second = self.write("b.bin", b"same")
        third = self.write("c.bin", b"same")

        real_hash = DuplicateFinder._hash_stable

        def racing_hash(path, expected_size, expected_mtime, budget, deadline):
            # The file grows after the walk stat'ed it but before the read.
            if os.path.abspath(path) == str(first):
                with open(first, "ab") as handle:
                    handle.write(b"-grown")
            return real_hash(
                path, expected_size, expected_mtime, budget, deadline
            )

        with mock.patch.object(
            DuplicateFinder, "_hash_stable", staticmethod(racing_hash)
        ):
            result = self.finder.find("Documents")

        self.assertTrue(result["success"], result)
        paths = [p for group in result["groups"] for p in group["paths"]]
        self.assertNotIn("a.bin", paths)
        self.assertEqual(sorted(paths), ["b.bin", "c.bin"])
        self.assertEqual(result["groups"][0]["paths"][0] in (
            "b.bin", "c.bin"
        ), True)
        # Sanity: the grown file really is different now.
        self.assertNotEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(second.read_bytes(), third.read_bytes())

    def test_empty_fanout_stops_at_entry_budget(self):
        # A flat directory of empty files has no hashable group: every entry
        # is charged, and the walk stops at the budget with a reason.
        for index in range(50):
            self.write(f"pad{index:03d}.txt", b"")

        with mock.patch.object(duplicates_module, "MAX_VISITED_ENTRIES", 10):
            result = self.finder.find("Documents", max_files=1000)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["duplicate_groups"], 0)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "entry limit")

    def test_unsupported_fanout_without_groups_stops_at_budget(self):
        # Only sized, non-duplicate files plus excluded dirs: hashing finds
        # nothing, and the entry budget still caps the walk.
        for index in range(30):
            self.write(f"uniq{index:03d}.bin", f"value-{index}".encode())
        for index in range(30):
            self.write(f"node_modules/n{index:03d}.bin", b"ignored")

        with mock.patch.object(duplicates_module, "MAX_VISITED_ENTRIES", 12):
            result = self.finder.find("Documents", max_files=500)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["duplicate_groups"], 0)
        self.assertLessEqual(result["scanned_files"], 12)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "entry limit")

    def test_deadline_is_honoured(self):
        for index in range(20):
            self.write(f"{index}/a.bin", b"dupe" * 4)
            self.write(f"{index}/b.bin", b"dupe" * 4)

        real_monotonic = time.monotonic
        started = real_monotonic()
        calls = {"n": 0}

        def jumping_monotonic():
            calls["n"] += 1
            # Deadline check at the top of the walk is the 1st call after
            # the deadline computation; later calls are past the deadline.
            if calls["n"] >= 3:
                return started + 10_000.0
            return real_monotonic()

        with mock.patch.object(
            duplicates_module.time, "monotonic", side_effect=jumping_monotonic
        ), mock.patch.object(
            duplicates_module, "MAX_VISITED_ENTRIES", 100_000
        ):
            result = self.finder.find("Documents", max_files=1000, timeout=30)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["duplicate_groups"], 0)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "time limit")

    def test_per_group_path_cap_sets_truncated(self):
        for index in range(6):
            self.write(f"d{index}/same.bin", b"payload")

        with mock.patch.object(duplicates_module, "MAX_RESULT_PATHS", 3):
            result = self.finder.find("Documents")

        self.assertTrue(result["success"], result)
        self.assertEqual(result["duplicate_groups"], 1)
        self.assertEqual(len(result["groups"][0]["paths"]), 3)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "result path limit")


if __name__ == "__main__":
    unittest.main()
