"""Race-safety of the confined filesystem primitives in ``fs_safe``."""

import errno
import os
import tempfile
import unittest
from pathlib import Path

from app.actions import fs_safe
from app.actions.fs_safe import (
    OVERWRITE_RACE,
    PUBLISH_OK,
    PUBLISH_REPLACED,
    open_dir_nofollow,
    publish_noreplace,
    publish_replace,
    remove_tree_confined,
    resolve_dir,
)


class RemoveTreeConfinedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "secret.txt"
        self.secret.write_bytes(b"keep me")
        (self.outside / "nested").mkdir()
        (self.outside / "nested" / "deep.txt").write_bytes(b"keep too")

    def test_raced_root_symlink_is_not_followed(self):
        victim = self.root / "victim"
        victim.mkdir()
        (victim / "payload.txt").write_bytes(b"payload")
        # The "private tree" root was replaced by a link to the victim.
        (victim / "link-to-outside").symlink_to(
            self.outside, target_is_directory=True
        )

        ok, leftover = remove_tree_confined(victim / "link-to-outside")

        # Exact refusal semantics are covered by test_directory_transfer;
        # here the invariant is that nothing outside the tree is touched.
        self.assertTrue(self.secret.read_bytes() == b"keep me")
        self.assertTrue((self.outside / "nested" / "deep.txt").exists())
        self.assertTrue((victim / "payload.txt").exists())
        if not ok:
            self.assertEqual(leftover, 0)

    def test_raced_symlinked_subdir_inside_tree_is_unlinked_not_followed(self):
        tree = self.root / "private"
        tree.mkdir()
        (tree / "file.txt").write_bytes(b"data")
        (tree / "escape").symlink_to(
            self.outside, target_is_directory=True
        )

        ok, leftover = remove_tree_confined(tree)

        self.assertTrue(ok)
        self.assertEqual(leftover, 0)
        self.assertFalse(os.path.lexists(tree))
        self.assertEqual(self.secret.read_bytes(), b"keep me")
        self.assertTrue((self.outside / "nested" / "deep.txt").exists())

    def test_symlink_swapped_between_stat_and_descent(self):
        tree = self.root / "private"
        tree.mkdir()
        (tree / "sub").mkdir()
        (tree / "sub" / "inner.txt").write_bytes(b"inner")
        real_open = fs_safe._open_child_dir
        swapped = {"done": False}

        def racing_open(dir_fd, name):
            # Race: after the stat said "directory", replace it with a link.
            if name == "sub" and not swapped["done"]:
                swapped["done"] = True
                os.rename(
                    "sub",
                    "sub-moved",
                    src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd,
                )
                os.symlink(
                    str(self.outside), "sub", dir_fd=dir_fd
                )
            return real_open(dir_fd, name)

        original = fs_safe._open_child_dir
        fs_safe._open_child_dir = racing_open
        try:
            ok, leftover = remove_tree_confined(tree)
        finally:
            fs_safe._open_child_dir = original

        self.assertTrue(swapped["done"], "race hook never fired")
        if not tree.exists():
            self.assertTrue(ok, leftover)
            self.assertEqual(leftover, 0)
        self.assertFalse(tree.is_symlink())
        self.assertEqual(self.secret.read_bytes(), b"keep me")
        self.assertTrue((self.outside / "nested" / "deep.txt").exists())

    def test_missing_root_is_clean(self):
        self.assertEqual(remove_tree_confined(self.root / "nope"), (True, 0))


class PublishNoReplaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dest_dir = self.root / "dest"
        self.dest_dir.mkdir()

    def test_existing_destination_is_never_clobbered(self):
        dest = self.dest_dir / "report.txt"
        dest.write_bytes(b"original")
        temp = self.root / "stage.txt"
        temp.write_bytes(b"new")

        outcome = publish_noreplace(temp, dest)

        self.assertNotEqual(outcome, PUBLISH_OK)
        self.assertIn(outcome, (OVERWRITE_RACE, "publish_blocked"))
        self.assertEqual(dest.read_bytes(), b"original")
        self.assertTrue(temp.exists())
        self.assertEqual(
            sorted(p.name for p in self.dest_dir.iterdir()), ["report.txt"]
        )

    def test_fresh_destination_is_published(self):
        dest = self.dest_dir / "fresh.txt"
        temp = self.root / "stage.txt"
        temp.write_bytes(b"payload")

        self.assertEqual(publish_noreplace(temp, dest), PUBLISH_OK)
        self.assertEqual(dest.read_bytes(), b"payload")
        self.assertFalse(temp.exists())

    def test_existing_directory_destination_is_not_clobbered(self):
        dest = self.dest_dir / "folder"
        dest.mkdir()
        (dest / "keep.txt").write_bytes(b"keep")
        temp = self.root / "stage-tree"
        temp.mkdir()
        (temp / "new.txt").write_bytes(b"new")

        outcome = publish_noreplace(temp, dest)

        self.assertNotEqual(outcome, PUBLISH_OK)
        self.assertTrue(dest.is_dir())
        self.assertFalse(dest.is_symlink())
        self.assertEqual((dest / "keep.txt").read_bytes(), b"keep")
        self.assertFalse((dest / "new.txt").exists())

    def test_missing_renameat2_fallback_still_refuses(self):
        dest = self.dest_dir / "report.txt"
        dest.write_bytes(b"original")
        temp = self.root / "stage.txt"
        temp.write_bytes(b"new")

        original = fs_safe._renameat2
        fs_safe._renameat2 = False
        try:
            outcome = publish_noreplace(temp, dest)
        finally:
            fs_safe._renameat2 = original

        self.assertNotEqual(outcome, PUBLISH_OK)
        self.assertEqual(dest.read_bytes(), b"original")
        self.assertTrue(temp.exists())


class OpenDirNoFollowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        (self.outside / "x").mkdir()

    def test_rejects_symlinked_final_component(self):
        base = self.root / "base"
        base.mkdir()
        (base / "link").symlink_to(self.outside, target_is_directory=True)

        base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, base_fd)
        # Invariant: refusal, never a fd onto the link target. O_NOFOLLOW |
        # O_DIRECTORY yields ENOTDIR on Linux (ELOOP is also acceptable).
        with self.assertRaises(OSError) as caught:
            fd = open_dir_nofollow("link", base_fd=base_fd)
            os.close(fd)
        self.assertIn(caught.exception.errno, (errno.ELOOP, errno.ENOTDIR))
        self.assertTrue((self.outside / "x").is_dir())

    def test_rejects_symlinked_intermediate_component(self):
        (self.root / "hop").symlink_to(self.outside, target_is_directory=True)

        old = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old)
        with self.assertRaises(OSError) as caught:
            fd = open_dir_nofollow("hop/x")
            os.close(fd)
        self.assertIn(
            caught.exception.errno, (errno.ELOOP, errno.ENOTDIR, errno.ENOENT)
        )
        self.assertFalse((self.outside / "x" / "x").exists())

    def test_accepts_real_directory_components(self):
        base = self.root / "real"
        base.mkdir()
        (base / "child").mkdir()
        real_fd = os.open(base / "child", os.O_RDONLY | os.O_DIRECTORY)
        base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fd = open_dir_nofollow("child", base_fd=base_fd)
            try:
                self.assertEqual(
                    os.fstat(fd).st_ino, os.fstat(real_fd).st_ino
                )
            finally:
                os.close(fd)
        finally:
            os.close(base_fd)
            os.close(real_fd)

    def test_resolve_dir_refuses_symlinked_parent(self):
        (self.root / "hoped").symlink_to(self.outside, target_is_directory=True)

        with self.assertRaises(OSError):
            fd, name = resolve_dir(self.root / "hoped" / "file.txt")
            os.close(fd)
        # Nothing was created through the link.
        self.assertFalse((self.outside / "file.txt").exists())
        self.assertTrue((self.outside / "x").is_dir())

    def test_publish_replace_through_symlinked_parent_fails(self):
        (self.root / "hoped").symlink_to(self.outside, target_is_directory=True)
        temp = self.root / "stage.txt"
        temp.write_bytes(b"new")

        outcome = publish_replace(temp, self.root / "hoped" / "x.txt")

        self.assertEqual(outcome, "publish_failed")
        self.assertFalse((self.outside / "x.txt").exists())


if __name__ == "__main__":
    unittest.main()
