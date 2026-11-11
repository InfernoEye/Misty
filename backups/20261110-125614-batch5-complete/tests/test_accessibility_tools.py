import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ACCESSIBILITY_TOOL_DEFINITIONS, ToolRegistry


class FakeInspector:
    def __init__(self, applications=None):
        self.applications = list(applications or [
            {"name": "Firefox", "process_id": 123},
        ])
        self.list_calls = 0
        self.inspect_calls = []

    def list_applications(self):
        self.list_calls += 1
        return {
            "success": True,
            "applications": list(self.applications),
            "count": len(self.applications),
            "truncated": False,
            "truncation": {"truncated": False, "reasons": []},
            "message": "ok",
        }

    def inspect_application(
        self, selector, max_depth=8, max_nodes=500, include_actions=True
    ):
        self.inspect_calls.append({
            "selector": selector,
            "max_depth": max_depth,
            "max_nodes": max_nodes,
            "include_actions": include_actions,
        })
        return {
            "success": True,
            "application": {"name": "Firefox"},
            "nodes": [],
            "count": 0,
            "truncated": False,
            "message": "inspected",
        }


class AccessibilityToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(
            Path(directory.name) / "memory.sqlite3"
        )
        self.inspector = FakeInspector()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            accessibility_inspector=self.inspector,
        )

    def test_tools_are_advertised(self):
        names = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertIn("accessibility_list_applications", names)
        self.assertIn("accessibility_inspect_application", names)
        self.assertEqual(len(ACCESSIBILITY_TOOL_DEFINITIONS), 2)

    def test_list_forwards_and_trims_large_results(self):
        self.inspector.applications = [
            {"name": f"App {index}", "process_id": 1000 + index}
            for index in range(150)
        ]

        result = self.registry.execute(
            "accessibility_list_applications", {}
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 100)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["applications"]), 100)

    def test_inspect_by_name_uses_bounded_defaults(self):
        result = self.registry.execute(
            "accessibility_inspect_application",
            {"app_name": "Firefox"},
        )

        self.assertTrue(result["success"])
        self.assertEqual(self.inspector.inspect_calls, [{
            "selector": "Firefox",
            "max_depth": 6,
            "max_nodes": 80,
            "include_actions": False,
        }])

    def test_inspect_by_process_id_forwards_selector(self):
        self.registry.execute(
            "accessibility_inspect_application",
            {"process_id": 123, "max_depth": 3, "max_nodes": 20},
        )

        self.assertEqual(
            self.inspector.inspect_calls[0]["selector"], 123
        )
        self.assertEqual(self.inspector.inspect_calls[0]["max_depth"], 3)
        self.assertEqual(self.inspector.inspect_calls[0]["max_nodes"], 20)

    def test_rejects_both_or_neither_selector(self):
        for arguments in (
            {},
            {"app_name": "Firefox", "process_id": 123},
        ):
            with self.subTest(arguments=arguments):
                result = self.registry.execute(
                    "accessibility_inspect_application", arguments
                )
                self.assertFalse(result["success"])
        self.assertEqual(self.inspector.inspect_calls, [])

    def test_rejects_invalid_bounds_and_arguments(self):
        for arguments in (
            {"app_name": "Firefox", "max_depth": 0},
            {"app_name": "Firefox", "max_depth": 21},
            {"app_name": "Firefox", "max_nodes": 0},
            {"app_name": "Firefox", "max_nodes": 301},
            {"app_name": "", "process_id": None},
            {"process_id": True},
            {"command": "press"},
        ):
            with self.subTest(arguments=arguments):
                result = self.registry.execute(
                    "accessibility_inspect_application", arguments
                )
                self.assertFalse(result["success"])
        self.assertEqual(self.inspector.inspect_calls, [])

    def test_list_rejects_arguments(self):
        result = self.registry.execute(
            "accessibility_list_applications", {"app_name": "Firefox"}
        )
        self.assertFalse(result["success"])
        self.assertEqual(self.inspector.list_calls, 0)


if __name__ == "__main__":
    unittest.main()
