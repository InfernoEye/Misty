import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.automation.reminders import ReminderStore
from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class ReminderToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.reminders = ReminderStore(self.root / "reminders.sqlite3")
        self.addCleanup(self.reminders.close)
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=MemoryStore(self.root / "memory.sqlite3"),
            reminder_store=self.reminders,
        )

    def test_tools_require_a_store(self):
        names = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertIn("set_reminder", names)
        without = ToolRegistry(
            executor=Mock(),
            memory_store=MemoryStore(self.root / "memory2.sqlite3"),
        )
        names = {
            item["function"]["name"]
            for item in without.available_tools()
        }
        self.assertNotIn("set_reminder", names)

    def test_create_list_and_cancel_reminder(self):
        created = self.registry.execute(
            "set_reminder",
            {"text": "stretch", "minutes_from_now": 5},
        )
        self.assertTrue(created["success"], created)
        reminder_id = created["reminder"]["id"]

        listed = self.registry.execute("list_reminders", {})
        self.assertTrue(listed["success"], listed)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["reminders"][0]["text"], "stretch")

        cancelled = self.registry.execute(
            "cancel_reminder", {"id": reminder_id}
        )
        self.assertTrue(cancelled["success"], cancelled)
        listed = self.registry.execute(
            "list_reminders", {"status": "pending"}
        )
        self.assertEqual(listed["count"], 0)

    def test_invalid_arguments_do_not_create_reminders(self):
        for arguments in (
            {"text": "", "minutes_from_now": 5},
            {"text": "x", "minutes_from_now": 0},
            {"text": "x", "minutes_from_now": 525601},
            {"text": "x"},
        ):
            with self.subTest(arguments=arguments):
                result = self.registry.execute("set_reminder", arguments)
                self.assertFalse(result["success"])
        self.assertEqual(self.reminders.counts()["pending"], 0)

    def test_cancel_unknown_id_fails(self):
        result = self.registry.execute("cancel_reminder", {"id": 999})
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
