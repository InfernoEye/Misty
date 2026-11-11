import tempfile
import unittest
from pathlib import Path

from app.actions.file_scope import FileScopeError, UserFolderResolver


class UserFolderResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "Pobrane").mkdir()
        (self.home / "Documents").mkdir()
        (self.home / "Documents" / "Reports").mkdir()
        self.resolver = UserFolderResolver(
            home=self.home,
            xdg_lookup=lambda _name: None,
        )

    def test_resolves_approved_key_under_home(self):
        self.assertEqual(
            self.resolver.base_for_key("Downloads"),
            (self.home / "Pobrane").resolve(),
        )
        self.assertEqual(
            self.resolver.resolve_key("Documents", "Reports"),
            (self.home / "Documents" / "Reports").resolve(),
        )

    def test_resolves_home_key(self):
        self.assertEqual(self.resolver.base_for_key("Home"), self.home.resolve())

    def test_rejects_unknown_key(self):
        with self.assertRaisesRegex(FileScopeError, "allowlist"):
            self.resolver.base_for_key("/etc")

    def test_rejects_absolute_and_parent_relative_paths(self):
        for relative in ("/etc/passwd", "../Documents", "Reports/../../.."):
            with self.subTest(relative=relative):
                with self.assertRaisesRegex(FileScopeError, "relative paths"):
                    self.resolver.resolve_key("Documents", relative)

    def test_rejects_symlink_escape(self):
        outside = Path(self.temp.name).parent / (
            Path(self.temp.name).name + "-scope-outside"
        )
        outside.mkdir()
        try:
            (self.home / "Documents" / "escape").symlink_to(outside)
            with self.assertRaisesRegex(FileScopeError, "escapes"):
                self.resolver.resolve_key("Documents", "escape")
        finally:
            outside.rmdir()

    def test_uses_relocated_xdg_directory_inside_home(self):
        relocated = self.home / "CustomDownloads"
        relocated.mkdir()
        resolver = UserFolderResolver(
            home=self.home,
            xdg_lookup=lambda name: (
                str(relocated) if name == "DOWNLOAD" else None
            ),
        )

        self.assertEqual(
            resolver.base_for_key("Downloads"), relocated.resolve()
        )

    def test_rejects_relocated_xdg_directory_outside_home(self):
        outside = Path(self.temp.name).parent / (
            Path(self.temp.name).name + "-xdg-outside"
        )
        outside.mkdir()
        try:
            resolver = UserFolderResolver(
                home=self.home,
                xdg_lookup=lambda name: (
                    str(outside) if name == "DOWNLOAD" else None
                ),
            )
            with self.assertRaisesRegex(FileScopeError, "outside the home"):
                resolver.base_for_key("Downloads")
        finally:
            outside.rmdir()

    def test_base_for_path_maps_static_allowlist_value(self):
        self.assertEqual(
            self.resolver.base_for_path("~/Pobrane"),
            (self.home / "Pobrane").resolve(),
        )
        with self.assertRaisesRegex(FileScopeError, "allowlist"):
            self.resolver.base_for_path("/tmp")


if __name__ == "__main__":
    unittest.main()
