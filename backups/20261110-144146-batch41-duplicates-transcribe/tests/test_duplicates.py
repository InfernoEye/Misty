import os
import tempfile
import unittest
from pathlib import Path

from app.actions.duplicates import DuplicateFinder
from app.actions.file_scope import UserFolderResolver


class DuplicateFinderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.docs = self.home / "Documents"
        self.docs.mkdir()
        self.resolver = UserFolderResolver(
            home=self.home, xdg_lookup=lambda key: None
        )
        self.finder = DuplicateFinder(resolver=self.resolver)

    def write(self, relative, data):
        path = self.docs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_finds_pairs_and_triples(self):
        self.write("a.txt", b"same")
        self.write("sub/b.txt", b"same")
        self.write("c.txt", b"same")
        self.write("unique.txt", b"different")

        result = self.finder.find("Documents")

        self.assertTrue(result["success"], result)
        self.assertEqual(result["duplicate_groups"], 1)
        self.assertEqual(len(result["groups"][0]["paths"]), 3)
        self.assertEqual(result["wasted_bytes"], 8)

    def test_min_bytes_and_excluded_dirs(self):
        self.write("empty1", b"")
        self.write("empty2", b"")
        self.write("node_modules/x.bin", b"dupe")
        self.write("node_modules/y.bin", b"dupe")
        self.write("keep/a.bin", b"dupe")
        self.write("keep/b.bin", b"dupe")

        result = self.finder.find("Documents", min_bytes=1)

        self.assertEqual(result["duplicate_groups"], 1)
        self.assertEqual(result["groups"][0]["paths"], ["keep/a.bin", "keep/b.bin"])

    def test_scope_escape_and_symlink_are_ignored(self):
        outside = self.home / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"secret")
        link = self.docs / "link.txt"
        try:
            link.symlink_to(outside / "secret.txt")
        except OSError:
            self.skipTest("symlinks unavailable")
        self.write("a.txt", b"content")
        self.write("b.txt", b"content")

        result = self.finder.find("Documents")

        self.assertTrue(result["success"], result)
        self.assertEqual(result["duplicate_groups"], 1)
        paths = result["groups"][0]["paths"]
        self.assertNotIn("link.txt", paths)
        self.assertFalse(self.finder.find("../outside")["success"])

    def test_group_limit_truncates(self):
        for index in range(3):
            self.write(f"{index}/a.bin", f"data{index}".encode())
            self.write(f"{index}/b.bin", f"data{index}".encode())

        result = self.finder.find("Documents", max_groups=1)

        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["groups"]), 1)

    def test_no_duplicates_is_success(self):
        self.write("a.txt", b"one")
        self.write("b.txt", b"two")

        result = self.finder.find("Documents")

        self.assertTrue(result["success"])
        self.assertEqual(result["duplicate_groups"], 0)

    def test_invalid_limits(self):
        for kwargs in (
            {"min_bytes": 0},
            {"max_files": 0},
            {"max_groups": 0},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertFalse(self.finder.find("Documents", **kwargs)["success"])


if __name__ == "__main__":
    unittest.main()
