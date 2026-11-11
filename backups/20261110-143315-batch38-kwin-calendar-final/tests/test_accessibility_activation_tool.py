import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeActivator:
    def __init__(self):
        self.calls = []
        self.result = {
            "success": True,
            "code": "ok",
            "message": "Control pressed.",
            "control": {"name": "Save", "role": "push button", "path": "0/0"},
            "verified": None,
        }

    def activate(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)


class AccessibilityActivationToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.activator = FakeActivator()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            control_activator=self.activator,
        )

    def test_tool_requires_activator(self):
        names = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertIn("accessibility_activate", names)
        without = ToolRegistry(executor=Mock(), memory_store=self.memory)
        names = {
            item["function"]["name"]
            for item in without.available_tools()
        }
        self.assertNotIn("accessibility_activate", names)

    def test_requires_approval_before_activation(self):
        result = self.registry.execute(
            "accessibility_activate",
            {"app_name": "Dolphin", "control_name": "Save"},
        )
        self.assertFalse(result["success"])
        self.assertEqual(self.activator.calls, [])

        result = self.registry.execute(
            "accessibility_activate",
            {"app_name": "Dolphin", "control_name": "Save"},
            confirmed=True,
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(self.activator.calls[0]["app_name"], "Dolphin")
        self.assertEqual(self.activator.calls[0]["name"], "Save")
        self.assertTrue(self.activator.calls[0]["confirmed"])

    def test_exactly_one_target_is_required(self):
        for arguments in (
            {"control_name": "Save"},
            {
                "app_name": "Dolphin",
                "process_id": 123,
                "control_name": "Save",
            },
        ):
            with self.subTest(arguments=arguments):
                result = self.registry.execute(
                    "accessibility_activate", arguments, confirmed=True
                )
                self.assertFalse(result["success"])
        self.assertEqual(self.activator.calls, [])

    def test_confirmation_description_names_target(self):
        text = self.registry.confirmation_description(
            "accessibility_activate",
            {"app_name": "Dolphin", "control_name": "Save"},
        )
        self.assertIn("Press 'Save' in Dolphin", text)


if __name__ == "__main__":
    unittest.main()
