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

    def _registry_for(self, screens):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return ToolRegistry(
            executor=type("Screens", (), {
                "list_screens": lambda self: {"success": True, "screens": screens}
            })(),
            memory_store=MemoryStore(Path(directory.name) / "s.sqlite3"),
        )

    def test_primary_without_priority_one_does_not_pick_second_screen(self):
        registry = self._registry_for([
            {"name": "DP-1", "priority": 2, "geometry": {"x": 1920}},
            {"name": "DP-2", "priority": 3, "geometry": {"x": 0}},
        ])
        self.assertIsNone(registry._resolve_screen_alias("primary"))
        self.assertIsNone(registry._resolve_screen_alias("main"))

    def test_middle_with_even_screens_is_not_upper_middle(self):
        registry = self._registry_for([
            {"name": "DP-1", "priority": 1, "geometry": {"x": 0}},
            {"name": "DP-2", "priority": 2, "geometry": {"x": 1920}},
        ])
        for phrase in ("middle", "center", "centre"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(registry._resolve_screen_alias(phrase))

    def test_alias_helper_never_contradicts_resolve_screen_target(self):
        layouts = [
            [{"name": "A", "priority": 1, "geometry": {"x": 0}}],
            [
                {"name": "A", "priority": 2, "geometry": {"x": 0}},
                {"name": "B", "priority": 3, "geometry": {"x": 1920}},
            ],
            [
                {"name": "A", "priority": 3, "geometry": {"x": 0}},
                {"name": "B", "priority": 1, "geometry": {"x": 1200}},
                {"name": "C", "priority": 2, "geometry": {"x": 4640}},
            ],
        ]
        for screens in layouts:
            registry = self._registry_for(screens)
            for phrase in ("left", "right", "center", "primary"):
                with self.subTest(names=[s["name"] for s in screens], phrase=phrase):
                    target = registry.resolve_screen_target(phrase)
                    alias = registry._resolve_screen_alias(phrase)
                    if target.get("success"):
                        self.assertEqual(alias, target["screen_name"])
                    else:
                        self.assertIsNone(alias, target)


if __name__ == "__main__":
    unittest.main()
