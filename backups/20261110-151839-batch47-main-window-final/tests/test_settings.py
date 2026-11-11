import json
import tempfile
import unittest
from pathlib import Path

from app import settings


class ShortcutSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "shortcut.json"
        self.original = settings.SHORTCUT_CONFIG
        settings.SHORTCUT_CONFIG = self.path
        self.addCleanup(setattr, settings, "SHORTCUT_CONFIG", self.original)

    def test_default_then_save_and_load(self):
        self.assertEqual(settings.load_shortcut(), "F8")
        settings.save_shortcut("Ctrl+Alt+M")
        self.assertEqual(settings.load_shortcut(), "Ctrl+Alt+M")
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")),
            {"sequence": "Ctrl+Alt+M"},
        )

    def test_invalid_values_fall_back_or_raise(self):
        self.path.write_text('{"sequence": ""}', encoding="utf-8")
        self.assertEqual(settings.load_shortcut(), "F8")
        for value in ("", "   ", "x" * 65, "bad\nkey"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    settings.save_shortcut(value)


if __name__ == "__main__":
    unittest.main()
