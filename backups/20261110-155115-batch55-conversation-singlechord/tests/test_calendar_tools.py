import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeCalendar:
    def __init__(self):
        self.calls = []

    def list_events(self, folder, relative_path, **kwargs):
        self.calls.append(("list", folder, relative_path, kwargs))
        return {
            "success": True,
            "events": [{"summary": "Standup", "start": "2026-11-10T09:00:00"}],
            "count": 1,
        }

    def add_event(self, folder, relative_path, summary, start, **kwargs):
        self.calls.append(("add", folder, relative_path, summary, start, kwargs))
        if kwargs.get("confirmed") is not True:
            return {
                "success": False,
                "code": "approval_required",
                "requires_confirmation": True,
                "event": {"summary": summary, "start": start},
            }
        return {
            "success": True,
            "added": True,
            "event": {"summary": summary, "start": start},
        }


class CalendarToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.calendar = FakeCalendar()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            calendar_engine=self.calendar,
        )

    def test_tools_require_engine(self):
        names = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertIn("list_calendar_events", names)
        self.assertIn("add_calendar_event", names)
        without = ToolRegistry(executor=Mock(), memory_store=self.memory)
        names = {
            item["function"]["name"]
            for item in without.available_tools()
        }
        self.assertNotIn("add_calendar_event", names)

    def test_list_forwards(self):
        result = self.registry.execute(
            "list_calendar_events",
            {
                "folder": "Documents",
                "relative_path": "calendar.ics",
                "limit": 10,
            },
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.calendar.calls[0][0], "list")
        self.assertEqual(self.calendar.calls[0][3]["limit"], 10)

    def test_add_requires_confirmation(self):
        arguments = {
            "folder": "Documents",
            "relative_path": "calendar.ics",
            "summary": "Standup",
            "start": "2026-11-10T09:00:00Z",
        }
        denied = self.registry.execute("add_calendar_event", arguments)
        self.assertFalse(denied["success"])
        self.assertEqual(self.calendar.calls, [])

        result = self.registry.execute(
            "add_calendar_event", arguments, confirmed=True
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.calendar.calls[0][0], "add")
        self.assertTrue(self.calendar.calls[0][5]["confirmed"])

    def test_confirmation_description(self):
        text = self.registry.confirmation_description(
            "add_calendar_event",
            {
                "folder": "Documents",
                "relative_path": "calendar.ics",
                "summary": "Standup",
                "start": "2026-11-10T09:00:00Z",
            },
        )
        self.assertIn("Add 'Standup' to calendar.ics", text)

    def test_invalid_arguments_do_not_reach_engine(self):
        for name, arguments in (
            ("list_calendar_events", {"folder": "Documents"}),
            ("add_calendar_event", {"folder": "Documents"}),
        ):
            with self.subTest(name=name):
                result = self.registry.execute(
                    name, arguments, confirmed=True
                )
                self.assertFalse(result["success"])
        self.assertEqual(self.calendar.calls, [])


if __name__ == "__main__":
    unittest.main()
