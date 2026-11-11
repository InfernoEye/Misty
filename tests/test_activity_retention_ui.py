"""Activity-history retention controls must actually delete rows.

The audit found that private content could persist in the local activity
history with no way to remove it. These tests exercise the MainWindow
methods against a temporary database, so the buttons are proven to work
without a real profile.
"""

import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.engine.activity_store import ActivityStore  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402


def _app():
    application = QApplication.instance()
    if application is None:
        application = QApplication([])
    return application


class ActivityRetentionControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ActivityStore(Path(self.temp.name) / "activity.sqlite3")
        self.window = MainWindow.__new__(MainWindow)
        self.window.activity_list = None
        self.window.activity_store = self.store

    def test_purge_removes_rows_older_than_the_window(self):
        self.store.add_event({"kind": "x", "title": "old", "details": "d"})
        # Advance the clock past the retention window, then add a fresh row.
        future = (
            datetime.datetime.now().astimezone()
            + datetime.timedelta(
                days=MainWindow.ACTIVITY_RETENTION_DAYS + 60
            )
        )
        self.store._clock = lambda: future
        self.store.add_event({"kind": "x", "title": "new", "details": "d"})
        self.assertEqual(self.store.count(), 2)

        removed = self.window.purge_old_activity()

        self.assertEqual(removed, 1)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.recent_events(1)[0]["title"], "new")

    def test_purge_is_explicit_about_its_window(self):
        # purge() alone would fall back to an unset retention and delete
        # nothing; the button must pass a concrete number of days.
        self.assertIsNotNone(MainWindow.ACTIVITY_RETENTION_DAYS)
        self.assertGreater(MainWindow.ACTIVITY_RETENTION_DAYS, 0)
        self.store.add_event({"kind": "x", "title": "old", "details": "d"})
        with mock.patch.object(
            self.store, "purge", return_value=1
        ) as purge:
            self.window.purge_old_activity()
        purge.assert_called_once_with(MainWindow.ACTIVITY_RETENTION_DAYS)

    def test_clear_requires_confirmation_and_deletes_everything(self):
        self.store.add_event({"kind": "x", "title": "one", "details": "d"})
        self.store.add_event({"kind": "x", "title": "two", "details": "d"})

        with mock.patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.No,
        ):
            declined = self.window.clear_activity_history()
        self.assertEqual(declined, 0)
        self.assertEqual(self.store.count(), 2)

        with mock.patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            removed = self.window.clear_activity_history()
        self.assertEqual(removed, 2)
        self.assertEqual(self.store.count(), 0)

    def test_failures_are_reported_without_raising(self):
        with mock.patch.object(
            self.store, "purge", side_effect=RuntimeError("db locked")
        ), mock.patch.object(self.window, "_refresh_activity"):
            self.assertEqual(self.window.purge_old_activity(), 0)


if __name__ == "__main__":
    unittest.main()
