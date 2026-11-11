import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeExecutor:
    def __init__(self, windows):
        self.windows = windows

    def list_windows(self):
        return {"success": True, "windows": self.windows}


class WindowAliasTests(unittest.TestCase):
    def registry(self, windows):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return ToolRegistry(
            executor=FakeExecutor(windows),
            memory_store=MemoryStore(Path(directory.name) / "m.sqlite3"),
        )

    def test_equicord_alias_resolves_discord_window(self):
        registry = self.registry([
            {
                "id": "discord-1",
                "title": "Discord",
                "app_id": "discord",
                "on_current_desktop": True,
            }
        ])
        result = registry.resolve_window_target("EquiCord", allow_fuzzy=True)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["window_id"], "discord-1")

    def test_two_discord_windows_are_ambiguous(self):
        registry = self.registry([
            {"id": "d1", "title": "Discord", "app_id": "discord"},
            {"id": "d2", "title": "Discord Canary", "app_id": "discord"},
        ])
        result = registry.resolve_window_target("EquiCord", allow_fuzzy=True)
        self.assertFalse(result["success"])
        self.assertTrue(result.get("ambiguous"))
        self.assertEqual(len(result["candidates"]), 2)


if __name__ == "__main__":
    unittest.main()
