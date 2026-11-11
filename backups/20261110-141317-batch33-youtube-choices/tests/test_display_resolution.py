import tempfile
import unittest
from pathlib import Path

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeScreenExecutor:
    def __init__(self):
        self.screens = [
            {"name": "DP-3", "priority": 2, "geometry": {"x": 4640}},
            {"name": "DP-2", "priority": 3, "geometry": {"x": 0}},
            {"name": "DP-1", "priority": 1, "geometry": {"x": 1200}},
        ]

    def list_screens(self):
        return {"success": True, "screens": self.screens}


class DisplayResolutionTests(unittest.TestCase):
    def setUp(self):
        self.executor = FakeScreenExecutor()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.registry = ToolRegistry(
            self.executor, memory_store=self.memory
        )

    def test_priority_ordinals(self):
        expected = {
            "first screen": "DP-1",
            "primary monitor": "DP-1",
            "main display": "DP-1",
            "second screen": "DP-3",
            "2nd monitor": "DP-3",
            "third screen": "DP-2",
            "3rd display": "DP-2",
            "drugi ekran": "DP-3",
            "trzeci monitor": "DP-2",
            "główny ekran": "DP-1",
        }
        for phrase, connector in expected.items():
            with self.subTest(phrase=phrase):
                result = self.registry.resolve_screen_target(phrase)
                self.assertTrue(result["success"], result)
                self.assertEqual(result["screen_name"], connector)

    def test_spatial_aliases_use_geometry_not_priority(self):
        expected = {
            "left screen": "DP-2",
            "right monitor": "DP-3",
            "center screen": "DP-1",
            "lewy ekran": "DP-2",
            "prawy monitor": "DP-3",
            "środkowy ekran": "DP-1",
        }
        for phrase, connector in expected.items():
            with self.subTest(phrase=phrase):
                result = self.registry.resolve_screen_target(phrase)
                self.assertTrue(result["success"], result)
                self.assertEqual(result["screen_name"], connector)

    def test_missing_priority_fails_safely(self):
        for screen in self.executor.screens:
            screen.pop("priority", None)

        result = self.registry.resolve_screen_target("first screen")
        self.assertFalse(result["success"])
        self.assertIn("priority", result["message"])


if __name__ == "__main__":
    unittest.main()
