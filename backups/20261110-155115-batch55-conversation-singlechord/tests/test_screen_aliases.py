import tempfile
import unittest
from pathlib import Path

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def list_screens(self):
        return {
            "success": True,
            "screens": [
                {"name": "DP-1", "priority": 1, "geometry": {"x": 1200, "y": 175}},
                {"name": "DP-2", "priority": 2, "geometry": {"x": 0, "y": 0}},
                {"name": "DP-3", "priority": 3, "geometry": {"x": 4640, "y": 0}},
            ],
        }


class ScreenAliasTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.registry = ToolRegistry(
            executor=FakeExecutor(),
            memory_store=MemoryStore(Path(directory.name) / "memory.sqlite3"),
        )

    def test_left_right_center_primary(self):
        self.assertEqual(self.registry._resolve_screen_alias("left"), "DP-2")
        self.assertEqual(self.registry._resolve_screen_alias("right"), "DP-3")
        self.assertEqual(self.registry._resolve_screen_alias("center"), "DP-1")
        self.assertEqual(self.registry._resolve_screen_alias("primary"), "DP-1")

    def test_connector_passes_through_and_unknown_fails(self):
        self.assertEqual(self.registry._resolve_screen_alias("DP-3"), "DP-3")
        with tempfile.TemporaryDirectory() as other:
            registry = ToolRegistry(
                executor=type("Broken", (), {
                    "list_screens": lambda self: {"success": False}
                })(),
                memory_store=MemoryStore(
                    Path(other) / "m2.sqlite3"
                ),
            )
        self.assertIsNone(registry._resolve_screen_alias("left"))


if __name__ == "__main__":
    unittest.main()
