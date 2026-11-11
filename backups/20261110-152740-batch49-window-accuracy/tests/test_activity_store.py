import tempfile
import unittest
from pathlib import Path

from app.engine.activity_store import (
    MAX_DETAILS_LENGTH,
    REDACTED_DETAILS,
    ActivityStore,
)


class ActivityStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ActivityStore(Path(self.temp.name) / "activity.sqlite3")

    def test_sensitive_details_are_redacted(self):
        self.store.add_event({
            "kind": "tool",
            "title": "Tool completed: read_clipboard",
            "details": "private clipboard text",
            "status": "success",
            "sensitive": True,
        })

        event = self.store.recent_events(1)[0]

        self.assertEqual(event["details"], REDACTED_DETAILS)
        self.assertEqual(event["title"], "Tool completed: read_clipboard")
        self.assertEqual(event["status"], "success")

    def test_normal_details_are_preserved(self):
        self.store.add_event({
            "kind": "tool",
            "title": "Tool completed: get_time",
            "details": "It is 12:00.",
            "status": "success",
        })

        self.assertEqual(
            self.store.recent_events(1)[0]["details"], "It is 12:00."
        )

    def test_long_details_are_truncated(self):
        self.store.add_event({
            "kind": "response",
            "title": "Long answer",
            "details": "x" * (MAX_DETAILS_LENGTH + 100),
            "status": "success",
        })

        details = self.store.recent_events(1)[0]["details"]

        self.assertTrue(details.startswith("x" * 100))
        self.assertLessEqual(
            len(details), MAX_DETAILS_LENGTH + len("\n[truncated]")
        )
        self.assertTrue(details.endswith("[truncated]"))


if __name__ == "__main__":
    unittest.main()
