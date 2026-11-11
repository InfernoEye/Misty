"""Tests for the isolated local reminder store.

Every database lives in a temporary directory.  No user data is read and no
Qt, model, network or shell component is touched.
"""

import os
import sqlite3
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.automation.reminders import (
    MAX_LIMIT,
    MAX_TEXT_LENGTH,
    ReminderError,
    ReminderStore,
    ReminderStoreError,
)

UTC = timezone.utc
LOCAL_PLUS_TWO = timezone(timedelta(hours=2))
LOCAL_PLUS_FIVE_THIRTY = timezone(timedelta(hours=5, minutes=30))

PRIVATE_TEXT = "call the pharmacy about the blue prescription"


def utc_now():
    return datetime.now(UTC)


def parse_utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def due_in(**delta):
    return (utc_now() + timedelta(**delta)).replace(microsecond=0)


class ReminderStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "reminders" / "reminders.sqlite3"
        self.store = ReminderStore(self.database)

    def add(self, text="buy milk", due_at=None):
        if due_at is None:
            due_at = due_in(hours=1)
        if isinstance(due_at, datetime):
            due_at = due_at.isoformat()
        return self.store.add(text, due_at)

    # -- add / list -----------------------------------------------------

    def test_add_returns_expected_fields_and_utc_due(self):
        due = due_in(hours=2)
        reminder = self.store.add("  buy milk  ", due.isoformat())

        self.assertEqual(
            set(reminder), {"id", "text", "due_at", "status"}
        )
        self.assertIsInstance(reminder["id"], int)
        self.assertGreater(reminder["id"], 0)
        self.assertEqual(reminder["text"], "buy milk")
        self.assertEqual(reminder["status"], "pending")
        self.assertTrue(reminder["due_at"].endswith("Z"))
        self.assertEqual(parse_utc(reminder["due_at"]), due)

    def test_list_orders_by_due_at_then_id(self):
        base = due_in(hours=1)
        third = self.store.add("third", (base + timedelta(minutes=20)).isoformat())
        first = self.store.add("first", base.isoformat())
        second = self.store.add("second", (base + timedelta(minutes=10)).isoformat())
        same_due = self.store.add("same", base.isoformat())

        rows = self.store.list()
        self.assertEqual(
            [row["text"] for row in rows],
            ["first", "same", "second", "third"],
        )
        self.assertEqual(
            [row["id"] for row in rows],
            [first["id"], same_due["id"], second["id"], third["id"]],
        )
        self.assertTrue(
            all(row["status"] == "pending" for row in rows)
        )
        self.assertTrue(all(row["fired_at"] is None for row in rows))

    def test_list_filters_by_status_and_respects_limit(self):
        base = due_in(hours=1)
        early = self.store.add("early", base.isoformat())
        middle = self.store.add(
            "middle", (base + timedelta(minutes=5)).isoformat()
        )
        late = self.store.add(
            "late", (base + timedelta(minutes=10)).isoformat()
        )
        self.store.mark_fired(middle["id"])
        self.store.cancel(late["id"])

        pending = self.store.list(status="pending")
        self.assertEqual([row["id"] for row in pending], [early["id"]])

        fired = self.store.list(status="fired")
        self.assertEqual([row["id"] for row in fired], [middle["id"]])

        cancelled = self.store.list(status="cancelled")
        self.assertEqual([row["id"] for row in cancelled], [late["id"]])

        self.assertEqual(len(self.store.list(limit=1)), 1)
        self.assertEqual(
            self.store.list(limit=1)[0]["id"], early["id"]
        )
        self.assertEqual(
            len(self.store.list(limit=MAX_LIMIT)),
            min(MAX_LIMIT, 3),
        )

    def test_empty_store(self):
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.due(), [])
        self.assertEqual(
            self.store.counts(),
            {"pending": 0, "fired": 0, "cancelled": 0},
        )

    def test_due_returns_only_pending_at_or_before_now(self):
        base = due_in(hours=1)
        first = self.store.add("first", base.isoformat())
        second = self.store.add(
            "second", (base + timedelta(minutes=10)).isoformat()
        )
        third = self.store.add(
            "third", (base + timedelta(minutes=20)).isoformat()
        )
        self.store.mark_fired(second["id"])
        self.store.cancel(third["id"])

        self.assertEqual(self.store.due(now=base - timedelta(seconds=1)), [])
        self.assertEqual(
            [row["id"] for row in self.store.due(now=base)],
            [first["id"]],
        )
        self.assertEqual(
            [
                row["id"]
                for row in self.store.due(
                    now=base + timedelta(minutes=15)
                )
            ],
            [first["id"]],
        )
        self.assertEqual(
            [
                row["id"]
                for row in self.store.due(
                    now=base + timedelta(minutes=30)
                )
            ],
            [first["id"]],
        )
        self.assertEqual(
            [
                row["text"]
                for row in self.store.due(
                    now=base + timedelta(minutes=30), limit=1
                )
            ],
            ["first"],
        )

    def test_due_boundary_is_inclusive(self):
        reminder = self.add(due_at=due_in(hours=1))
        boundary = parse_utc(reminder["due_at"])

        rows = self.store.due(now=boundary)
        self.assertEqual([row["id"] for row in rows], [reminder["id"]])

        rows = self.store.due(now=boundary - timedelta(microseconds=1))
        self.assertEqual(rows, [])

    # -- timezones ------------------------------------------------------

    def test_timezone_offsets_are_normalized_to_utc(self):
        target = due_in(days=2)
        shifted = target.astimezone(LOCAL_PLUS_TWO)

        plus_two = self.store.add("plus two", shifted.isoformat())
        self.assertTrue(plus_two["due_at"].endswith("Z"))
        self.assertEqual(parse_utc(plus_two["due_at"]), target)
        self.assertEqual(
            plus_two["due_at"],
            target.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )

        target_two = due_in(days=3)
        shifted_two = target_two.astimezone(LOCAL_PLUS_FIVE_THIRTY)
        plus_five = self.store.add("plus five", shifted_two.isoformat())
        self.assertEqual(parse_utc(plus_five["due_at"]), target_two)

        target_three = due_in(days=4)
        zulu = self.store.add(
            "zulu",
            target_three.isoformat().replace("+00:00", "Z"),
        )
        self.assertEqual(parse_utc(zulu["due_at"]), target_three)
        self.assertTrue(zulu["due_at"].endswith("Z"))
        self.assertTrue(zulu["due_at"].startswith("20"))

    def test_optional_offset_and_naive_values_are_rejected(self):
        naive = due_in(days=1).replace(tzinfo=None)
        for due_at in (
            naive.isoformat(),
            "tomorrow",
            "",
            "   ",
            "x" * 100,
            due_in(days=1).date().isoformat(),
        ):
            with self.subTest(due_at=due_at):
                with self.assertRaises(ReminderError) as caught:
                    self.store.add("naive", due_at)
                self.assertEqual(caught.exception.code, "invalid_due_at")

    # -- due_at bounds --------------------------------------------------

    def test_past_and_far_future_are_rejected(self):
        with self.assertRaises(ReminderError) as caught:
            self.store.add("past", due_in(minutes=-5).isoformat())
        self.assertEqual(caught.exception.code, "due_in_past")

        with self.assertRaises(ReminderError) as caught:
            self.store.add("far", due_in(days=366).isoformat())
        self.assertEqual(caught.exception.code, "due_too_far")

        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.counts()["pending"], 0)

    def test_due_at_accepts_near_horizon(self):
        reminder = self.store.add(
            "almost a year", due_in(days=364).isoformat()
        )
        self.assertEqual(reminder["status"], "pending")

    def test_due_at_type_is_strict(self):
        for due_at in (
            123,
            None,
            True,
            1.5,
            b"2030-01-01T00:00:00Z",
            ["2030-01-01T00:00:00Z"],
            due_in(days=1),
        ):
            with self.subTest(due_at=repr(due_at)):
                with self.assertRaises(ReminderError) as caught:
                    self.store.add("strict", due_at)
                self.assertEqual(caught.exception.code, "invalid_due_at")

    def test_now_argument_must_be_aware_datetime(self):
        self.add()
        for now in (
            "now",
            123,
            True,
            due_in(days=1).replace(tzinfo=None),
            0.0,
        ):
            with self.subTest(now=repr(now)):
                with self.assertRaises(ReminderError) as caught:
                    self.store.due(now=now)
                self.assertEqual(caught.exception.code, "invalid_now")

        self.assertEqual(
            [row["id"] for row in self.store.due(now=utc_now())], []
        )

    # -- text validation ------------------------------------------------

    def test_text_bounds_and_whitespace(self):
        exact = self.store.add("x" * MAX_TEXT_LENGTH, due_in(hours=1).isoformat())
        self.assertEqual(len(exact["text"]), MAX_TEXT_LENGTH)

        with self.assertRaises(ReminderError) as caught:
            self.store.add("x" * (MAX_TEXT_LENGTH + 1), due_in(hours=1).isoformat())
        self.assertEqual(caught.exception.code, "text_too_long")

        for text in ("", "   ", "\t", "\n"):
            with self.subTest(text=repr(text)):
                with self.assertRaises(ReminderError) as caught:
                    self.store.add(text, due_in(hours=1).isoformat())
                self.assertEqual(caught.exception.code, "invalid_text")

    def test_text_control_characters_are_rejected(self):
        for text in (
            "line\nbreak",
            "tab\tseparated",
            "nul\x00byte",
            "escape\x1b[31m",
            "delete\x7f",
            "c1\x85control",
        ):
            with self.subTest(text=repr(text)):
                with self.assertRaises(ReminderError) as caught:
                    self.store.add(text, due_in(hours=1).isoformat())
                self.assertEqual(caught.exception.code, "invalid_text")
        self.assertEqual(self.store.list(), [])

    def test_text_type_and_encoding_are_strict(self):
        for text in (
            None,
            123,
            True,
            1.5,
            b"bytes",
            ["list"],
            {"text": "secret"},
            "\ud800",
        ):
            with self.subTest(text=repr(text)):
                with self.assertRaises(ReminderError) as caught:
                    self.store.add(text, due_in(hours=1).isoformat())
                self.assertEqual(caught.exception.code, "invalid_text")

    def test_text_is_stored_verbatim_without_interpretation(self):
        text = "'; DROP TABLE reminders; -- \"quoted\" 100% done"
        reminder = self.store.add(text, due_in(hours=1).isoformat())

        self.assertEqual(reminder["text"], text)
        stored = self.store.list()[0]
        self.assertEqual(stored["text"], text)
        self.assertEqual(self.store.counts()["pending"], 1)

    # -- transitions ----------------------------------------------------

    def test_mark_fired_transition_and_idempotency(self):
        reminder = self.add(due_at=due_in(hours=1))
        fired_at = utc_now()

        fired = self.store.mark_fired(reminder["id"], now=fired_at)
        self.assertIsNotNone(fired)
        self.assertEqual(fired["status"], "fired")
        self.assertEqual(parse_utc(fired["fired_at"]), fired_at)

        repeated = self.store.mark_fired(reminder["id"])
        self.assertEqual(repeated["status"], "fired")
        self.assertEqual(repeated["fired_at"], fired["fired_at"])
        self.assertEqual(
            self.store.counts(),
            {"pending": 0, "fired": 1, "cancelled": 0},
        )

        self.assertIsNone(self.store.mark_fired(999))
        self.assertEqual(len(self.store.list()), 1)

    def test_cancel_transition_and_idempotency(self):
        reminder = self.add(due_at=due_in(hours=1))

        cancelled = self.store.cancel(reminder["id"])
        self.assertIsNotNone(cancelled)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["fired_at"])

        repeated = self.store.cancel(reminder["id"])
        self.assertEqual(repeated["status"], "cancelled")
        self.assertEqual(repeated["fired_at"], cancelled["fired_at"])

        fired = self.store.mark_fired(reminder["id"])
        self.assertEqual(fired["status"], "cancelled")
        self.assertIsNone(fired["fired_at"])

        self.assertIsNone(self.store.cancel(999))
        self.assertEqual(
            self.store.counts(),
            {"pending": 0, "fired": 0, "cancelled": 1},
        )

    def test_cancel_does_not_clear_a_fired_reminder(self):
        reminder = self.add(due_at=due_in(hours=1))
        fired = self.store.mark_fired(reminder["id"])

        after = self.store.cancel(reminder["id"])
        self.assertEqual(after["status"], "fired")
        self.assertEqual(after["fired_at"], fired["fired_at"])

    def test_reminder_id_is_strict(self):
        reminder = self.add()
        for invalid in (True, False, 1.0, "1", 0, -1, None, [1]):
            for method in (self.store.mark_fired, self.store.cancel):
                with self.subTest(method=method.__name__, id=repr(invalid)):
                    with self.assertRaises(ReminderError) as caught:
                        method(invalid)
                    self.assertEqual(caught.exception.code, "invalid_id")
        self.assertEqual(
            self.store.list(status="pending")[0]["id"], reminder["id"]
        )

    def test_counts_track_all_transitions(self):
        base = due_in(hours=1)
        pending = self.store.add("pending", base.isoformat())
        fired = self.store.add(
            "fired", (base + timedelta(minutes=1)).isoformat()
        )
        cancelled = self.store.add(
            "cancelled", (base + timedelta(minutes=2)).isoformat()
        )

        self.store.mark_fired(fired["id"])
        self.store.cancel(cancelled["id"])
        self.assertEqual(
            self.store.counts(),
            {"pending": 1, "fired": 1, "cancelled": 1},
        )

        self.store.mark_fired(pending["id"])
        self.assertEqual(
            self.store.counts(),
            {"pending": 0, "fired": 2, "cancelled": 1},
        )

    # -- argument validation -------------------------------------------

    def test_list_and_due_validate_arguments_strictly(self):
        self.add()
        for limit in (True, False, 0, -1, MAX_LIMIT + 1, "5", 5.0, None):
            for method in (self.store.list, self.store.due):
                with self.subTest(method=method.__name__, limit=repr(limit)):
                    with self.assertRaises(ReminderError) as caught:
                        method(limit=limit)
                    self.assertEqual(caught.exception.code, "invalid_limit")

        for status in (1, True, "all", "Pending!", "done", ["pending"]):
            with self.subTest(status=repr(status)):
                with self.assertRaises(ReminderError) as caught:
                    self.store.list(status=status)
                self.assertEqual(caught.exception.code, "invalid_status")

        self.assertEqual(len(self.store.list(status="PENDING")), 1)
        self.assertEqual(len(self.store.list(status=" pending ")), 1)

    # -- persistence / storage -----------------------------------------

    def test_persistence_across_reopen(self):
        reminder = self.add(
            text=PRIVATE_TEXT, due_at=due_in(hours=5)
        )
        fired = self.store.mark_fired(reminder["id"])
        self.store.close()

        reopened = ReminderStore(self.database)
        self.addCleanup(reopened.close)
        rows = reopened.list()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], reminder["id"])
        self.assertEqual(rows[0]["text"], PRIVATE_TEXT)
        self.assertEqual(rows[0]["due_at"], reminder["due_at"])
        self.assertEqual(rows[0]["status"], "fired")
        self.assertEqual(rows[0]["fired_at"], fired["fired_at"])
        self.assertEqual(
            reopened.counts(),
            {"pending": 0, "fired": 1, "cancelled": 0},
        )

    def test_existing_schema_is_reused(self):
        self.add()
        with sqlite3.connect(self.database) as connection:
            columns = [
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(reminders)"
                ).fetchall()
            ]
            mode = connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0].lower()

        self.assertEqual(
            columns,
            ["id", "text", "due_at", "created_at", "status", "fired_at"],
        )
        self.assertEqual(mode, "wal")

        reopened = ReminderStore(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(len(reopened.list()), 1)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_database_and_directory_permissions(self):
        self.assertEqual(
            stat.S_IMODE(os.stat(self.database).st_mode), 0o600
        )
        self.assertEqual(
            stat.S_IMODE(os.stat(self.database.parent).st_mode), 0o700
        )

    def test_invalid_path_is_rejected(self):
        for path in ("", "   ", 123, None, "\x00bad"):
            with self.subTest(path=repr(path)):
                with self.assertRaises(ReminderStoreError):
                    ReminderStore(path)

    # -- safety ---------------------------------------------------------

    def test_no_reminder_text_in_repr_or_errors(self):
        reminder = self.add(text=PRIVATE_TEXT)

        self.assertNotIn(PRIVATE_TEXT, repr(self.store))
        self.assertNotIn(PRIVATE_TEXT, str(self.store))
        self.assertIn("reminders.sqlite3", repr(self.store))

        with self.assertRaises(ReminderError) as caught:
            self.store.add(PRIVATE_TEXT + "\x00", due_in(hours=1).isoformat())
        self.assertNotIn(PRIVATE_TEXT, str(caught.exception))
        self.assertNotIn(PRIVATE_TEXT, repr(caught.exception))
        self.assertNotIn("Traceback", str(caught.exception))

        self.assertEqual(
            self.store.list(status="pending")[0]["id"], reminder["id"]
        )

    def test_storage_failures_do_not_leak_exception_text(self):
        with mock.patch(
            "app.automation.reminders.sqlite3.connect",
            side_effect=sqlite3.OperationalError(
                f"unable to open {PRIVATE_TEXT}"
            ),
        ):
            with self.assertRaises(ReminderStoreError) as caught:
                self.store.list()
            message = str(caught.exception)
            self.assertNotIn(PRIVATE_TEXT, message)
            self.assertNotIn("unable to open", message)
            self.assertEqual(
                message, "reminder storage is unavailable"
            )

    def test_initialization_failure_message_is_fixed(self):
        with mock.patch(
            "app.automation.reminders.sqlite3.connect",
            side_effect=sqlite3.OperationalError("secret-init-detail"),
        ):
            with self.assertRaises(ReminderStoreError) as caught:
                ReminderStore(self.root / "broken" / "db.sqlite3")
        self.assertNotIn("secret-init-detail", str(caught.exception))
        self.assertEqual(
            str(caught.exception),
            "reminder database could not be initialized",
        )

    def test_sql_injection_attempts_cannot_change_queries(self):
        reminder = self.add(due_at=due_in(hours=1))

        with self.assertRaises(ReminderError) as caught:
            self.store.list(status="pending' OR '1'='1")
        self.assertEqual(caught.exception.code, "invalid_status")

        with self.assertRaises(ReminderError) as caught:
            self.store.list(status="pending; DROP TABLE reminders; --")
        self.assertEqual(caught.exception.code, "invalid_status")

        with self.assertRaises(ReminderError):
            self.store.due(limit="1; DROP TABLE reminders")
        with self.assertRaises(ReminderError):
            self.store.mark_fired("1 OR 1=1")

        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(
            self.store.list()[0]["id"], reminder["id"]
        )

    # -- concurrency ----------------------------------------------------

    def test_concurrent_adds_remain_consistent(self):
        threads = 8
        per_thread = 10

        def worker(worker_index):
            outcomes = []
            for item in range(per_thread):
                try:
                    reminder = self.store.add(
                        f"worker {worker_index} item {item}",
                        due_in(
                            hours=1,
                            seconds=worker_index * per_thread + item,
                        ).isoformat(),
                    )
                except ReminderError:
                    outcomes.append(None)
                else:
                    outcomes.append(reminder["id"])
            return outcomes

        with ThreadPoolExecutor(max_workers=threads) as pool:
            results = list(pool.map(worker, range(threads)))

        ids = [reminder_id for batch in results for reminder_id in batch]
        self.assertEqual(len(ids), threads * per_thread)
        self.assertNotIn(None, ids)
        self.assertEqual(len(set(ids)), threads * per_thread)
        self.assertEqual(
            self.store.counts(),
            {"pending": threads * per_thread, "fired": 0, "cancelled": 0},
        )

        rows = self.store.list(limit=MAX_LIMIT)
        self.assertEqual(len(rows), threads * per_thread)
        self.assertEqual(
            len({row["text"] for row in rows}), threads * per_thread
        )
        due_values = [row["due_at"] for row in rows]
        self.assertEqual(due_values, sorted(due_values))

    def test_concurrent_add_and_fire_remains_consistent(self):
        total = 24

        def worker(index):
            reminder = self.store.add(
                f"thread reminder {index}", due_in(minutes=30).isoformat()
            )
            fired = self.store.mark_fired(reminder["id"])
            return fired["status"] if fired is not None else None

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(worker, range(total)))

        self.assertEqual(statuses, ["fired"] * total)
        self.assertEqual(
            self.store.counts(),
            {"pending": 0, "fired": total, "cancelled": 0},
        )
        self.assertEqual(len(self.store.list(limit=MAX_LIMIT)), total)


if __name__ == "__main__":
    unittest.main()
