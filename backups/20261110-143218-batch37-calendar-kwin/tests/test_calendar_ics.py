"""Tests for the isolated, safety-first ICS calendar engine.

Every test uses a temporary home. No test touches the real user home, the
network, a model, or the registry.
"""

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.actions import calendar_ics
from app.actions.calendar_ics import (
    MAX_EVENTS,
    MAX_EVENT_DURATION_DAYS,
    MAX_FILE_BYTES,
    MAX_RESULT_LIMIT,
    MAX_SUMMARY_LENGTH,
    MAX_TEXT_FIELD_LENGTH,
    IcsCalendar,
)
from app.actions.file_scope import UserFolderResolver


def _calendar(*events, prologue=""):
    parts = ["BEGIN:VCALENDAR", "VERSION:2.0"]
    if prologue:
        parts.append(prologue)
    for event in events:
        parts.extend(event)
    parts.extend(["END:VCALENDAR", ""])
    return "\r\n".join(parts).encode("utf-8")


def _event(uid="event@example.test", summary="Event", start=None, end=None):
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{summary}"]
    if start:
        lines.append(start)
    if end:
        lines.append(end)
    lines.append("END:VEVENT")
    return lines


def _many_events(count):
    parts = ["BEGIN:VCALENDAR", "VERSION:2.0"]
    for index in range(count):
        parts.extend(
            [
                "BEGIN:VEVENT",
                f"UID:e{index}@example.test",
                f"SUMMARY:Event {index}",
                "DTSTART:20260301T090000Z",
                "DTEND:20260301T100000Z",
                "END:VEVENT",
            ]
        )
    parts.extend(["END:VCALENDAR", ""])
    return "\r\n".join(parts).encode("utf-8")


class IcsCalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "Documents").mkdir()
        (self.home / "Pobrane").mkdir()
        self.resolver = UserFolderResolver(
            home=self.home,
            xdg_lookup=lambda _name: None,
        )
        self.engine = IcsCalendar(resolver=self.resolver)
        self.docs = self.home / "Documents"
        self.calendar = self.docs / "calendar.ics"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _write(self, content, name="calendar.ics"):
        target = self.docs / name
        target.write_bytes(content)
        return target

    def _read(self, name="calendar.ics"):
        return (self.docs / name).read_bytes()

    def _assert_code(self, result, code):
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], code)
        self.assertIsInstance(result["message"], str)
        self.assertNotEqual(result["message"], "")
        return result

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def test_parse_simple_events(self):
        content = _calendar(
            _event(
                uid="one@example.test",
                summary="First",
                start="DTSTART:20260301T090000Z",
                end="DTEND:20260301T100000Z",
            ),
            _event(
                uid="two@example.test",
                summary="Second",
                start="DTSTART:20260302T090000Z",
                end="DTEND:20260302T093000Z",
            ),
        )
        self._write(content)

        result = self.engine.list_events("Documents", "calendar.ics")

        self.assertTrue(result["success"])
        self.assertIsNone(result["code"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["total"], 2)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["file"]["event_count"], 2)
        self.assertEqual(result["file"]["size"], len(content))
        first = result["events"][0]
        self.assertEqual(first["uid"], "one@example.test")
        self.assertEqual(first["summary"], "First")
        self.assertEqual(first["start"], "2026-03-01T09:00:00+00:00")
        self.assertEqual(first["start_utc"], "2026-03-01T09:00:00Z")
        self.assertEqual(first["duration_seconds"], 3600)
        self.assertFalse(first["all_day"])
        self.assertFalse(first["recurring"])

    def test_parse_folded_lines(self):
        content = _calendar(
            [
                "BEGIN:VEVENT",
                "UID:fold@example.test",
                "SUMMARY:Team lunch at the new place",
                "  downtown near the river",
                "DESCRIPTION:First line of the note",
                "  continues here",
                "DTSTART:20260301T120000Z",
                "DTEND:20260301T130000Z",
                "END:VEVENT",
            ]
        )
        self._write(content)

        result = self.engine.list_events("Documents", "calendar.ics")

        self.assertTrue(result["success"])
        event = result["events"][0]
        self.assertEqual(
            event["summary"], "Team lunch at the new place downtown near the river"
        )
        self.assertEqual(
            event["description"], "First line of the note continues here"
        )

    def test_parse_escaped_text(self):
        content = _calendar(
            [
                "BEGIN:VEVENT",
                "UID:escape@example.test",
                r"SUMMARY:Lunch\, team \; more",
                r"DESCRIPTION:Line one\nLine two\\backslash",
                r"LOCATION:Room 4\, Building A",
                "DTSTART:20260301T120000Z",
                "DTEND:20260301T130000Z",
                "END:VEVENT",
            ]
        )
        self._write(content)

        event = self.engine.list_events("Documents", "calendar.ics")["events"][0]

        self.assertEqual(event["summary"], "Lunch, team ; more")
        self.assertEqual(event["description"], "Line one\nLine two\\backslash")
        self.assertEqual(event["location"], "Room 4, Building A")

    def test_parse_all_day_events(self):
        content = _calendar(
            _event(
                uid="day@example.test",
                summary="Conference",
                start="DTSTART;VALUE=DATE:20260303",
                end="DTEND;VALUE=DATE:20260305",
            ),
            _event(
                uid="implicit@example.test",
                summary="Implicit end",
                start="DTSTART;VALUE=DATE:20260310",
            ),
        )
        self._write(content)

        events = self.engine.list_events("Documents", "calendar.ics")["events"]

        self.assertTrue(events[0]["all_day"])
        self.assertEqual(events[0]["start"], "2026-03-03")
        self.assertEqual(events[0]["end"], "2026-03-05")
        self.assertEqual(events[0]["start_utc"], "2026-03-03T00:00:00Z")
        self.assertEqual(events[0]["end_utc"], "2026-03-05T00:00:00Z")
        self.assertEqual(events[0]["duration_seconds"], 2 * 86400)
        self.assertIsNone(events[0]["timezone"])

        # A missing DTEND means a single all-day day.
        self.assertEqual(events[1]["duration_seconds"], 86400)

    def test_parse_timed_timezones(self):
        content = _calendar(
            _event(
                uid="warsaw@example.test",
                summary="Warsaw",
                start="DTSTART;TZID=Europe/Warsaw:20260302T090000",
                end="DTEND;TZID=Europe/Warsaw:20260302T103000",
            ),
            _event(
                uid="utc@example.test",
                summary="UTC",
                start="DTSTART:20260702T090000Z",
                end="DTEND:20260702T100000Z",
            ),
        )
        self._write(content)

        events = self.engine.list_events("Documents", "calendar.ics")["events"]

        self.assertEqual(events[0]["timezone"], "Europe/Warsaw")
        self.assertEqual(events[0]["start"], "2026-03-02T09:00:00+01:00")
        self.assertEqual(events[0]["start_utc"], "2026-03-02T08:00:00Z")
        self.assertEqual(events[0]["duration_seconds"], 5400)
        self.assertEqual(events[1]["timezone"], "UTC")
        self.assertEqual(events[1]["start_utc"], "2026-07-02T09:00:00Z")

    def test_timed_event_without_end_has_zero_duration(self):
        content = _calendar(
            _event(
                uid="point@example.test",
                summary="Point",
                start="DTSTART:20260301T090000Z",
            )
        )
        self._write(content)

        event = self.engine.list_events("Documents", "calendar.ics")["events"][0]

        self.assertEqual(event["end_utc"], event["start_utc"])
        self.assertEqual(event["duration_seconds"], 0)

    def test_filter_windows(self):
        content = _calendar(
            _event(
                uid="monday@example.test",
                summary="Monday",
                start="DTSTART:20260301T090000Z",
                end="DTEND:20260301T100000Z",
            ),
            _event(
                uid="tuesday@example.test",
                summary="Tuesday",
                start="DTSTART:20260302T090000Z",
                end="DTEND:20260302T100000Z",
            ),
            _event(
                uid="allday@example.test",
                summary="All day",
                start="DTSTART;VALUE=DATE:20260303",
                end="DTEND;VALUE=DATE:20260304",
            ),
        )
        self._write(content)

        def summaries(result):
            return [event["uid"] for event in result["events"]]

        window = self.engine.list_events(
            "Documents",
            "calendar.ics",
            start="2026-03-01T09:30:00Z",
            end="2026-03-02T09:30:00Z",
        )
        self.assertEqual(
            summaries(window),
            ["monday@example.test", "tuesday@example.test"],
        )

        # DTEND is exclusive, so the window starting at 10:00 excludes Monday.
        boundary = self.engine.list_events(
            "Documents",
            "calendar.ics",
            start="2026-03-02T10:00:00Z",
            end="2026-03-03T00:00:00Z",
        )
        self.assertEqual(boundary["events"], [])

        all_day = self.engine.list_events(
            "Documents", "calendar.ics", start="2026-03-03", end="2026-03-04"
        )
        self.assertEqual(summaries(all_day), ["allday@example.test"])

        # A start-only filter keeps events that end after it.
        tail = self.engine.list_events(
            "Documents", "calendar.ics", start="2026-03-02T09:30:00Z"
        )
        self.assertEqual(
            summaries(tail),
            ["tuesday@example.test", "allday@example.test"],
        )

        # An end-only filter keeps events that start before it.
        head = self.engine.list_events(
            "Documents", "calendar.ics", end="2026-03-02T00:00:00Z"
        )
        self.assertEqual(summaries(head), ["monday@example.test"])

    def test_filter_validation(self):
        self._write(_many_events(1))
        result = self.engine.list_events(
            "Documents", "calendar.ics", start="2026-03-02", end="2026-03-01"
        )
        self._assert_code(result, "invalid_event")

        for bad in ("tomorrow", "", 42, "2026-13-01"):
            with self.subTest(bad=bad):
                result = self.engine.list_events(
                    "Documents", "calendar.ics", start=bad
                )
                self._assert_code(result, "invalid_event")

    def test_limit_is_bounded(self):
        self._write(_many_events(3))

        limited = self.engine.list_events("Documents", "calendar.ics", limit=2)
        self.assertEqual(limited["count"], 2)
        self.assertEqual(limited["total"], 3)
        self.assertTrue(limited["truncated"])

        zero = self.engine.list_events("Documents", "calendar.ics", limit=0)
        self.assertEqual(zero["count"], 1)
        self.assertEqual(zero["limit"], 1)

        huge = self.engine.list_events("Documents", "calendar.ics", limit=10**9)
        self.assertEqual(huge["limit"], MAX_RESULT_LIMIT)
        self.assertEqual(huge["count"], 3)

        text = self.engine.list_events("Documents", "calendar.ics", limit="2")
        self.assertEqual(text["count"], 2)

        fallback = self.engine.list_events("Documents", "calendar.ics", limit=None)
        self.assertEqual(fallback["count"], 3)

    def test_read_is_read_only(self):
        content = _many_events(2)
        target = self._write(content)
        before_mtime = target.stat().st_mtime_ns
        before_entries = sorted(path.name for path in self.docs.iterdir())

        self.engine.list_events("Documents", "calendar.ics")
        self.engine.list_events("Documents", "calendar.ics", limit=1)

        self.assertEqual(target.read_bytes(), content)
        self.assertEqual(target.stat().st_mtime_ns, before_mtime)
        self.assertEqual(
            sorted(path.name for path in self.docs.iterdir()),
            before_entries,
        )

    # ------------------------------------------------------------------
    # Adding events
    # ------------------------------------------------------------------

    def test_add_to_existing_preserves_content(self):
        original = _calendar(
            _event(
                uid="old@example.test",
                summary="Existing",
                start="DTSTART:20260301T090000Z",
                end="DTEND:20260301T100000Z",
            ),
            prologue="X-WR-CALNAME:Work",
        )
        self._write(original)

        result = self.engine.add_event(
            "Documents",
            "calendar.ics",
            "Planning, review; Q1",
            "2026-03-05T10:00:00+01:00",
            end="2026-03-05T11:30:00+01:00",
            description="Line 1\nLine 2",
            location="Room 1, Building 2",
            confirmed=True,
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["added"])
        self.assertEqual(result["file"]["event_count"], 2)
        self.assertEqual(result["event"]["summary"], "Planning, review; Q1")
        self.assertEqual(result["event"]["start_utc"], "2026-03-05T09:00:00Z")

        data = self._read()
        text = data.decode("utf-8")
        self.assertIn("X-WR-CALNAME:Work", text)
        self.assertIn("UID:old@example.test", text)
        self.assertIn("SUMMARY:Planning\\, review\\; Q1", text)
        self.assertIn("DESCRIPTION:Line 1\\nLine 2", text)
        self.assertIn("LOCATION:Room 1\\, Building 2", text)
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        # CRLF only, never a bare LF.
        self.assertEqual(text.count("\n"), text.count("\r\n"))

        listed = self.engine.list_events("Documents", "calendar.ics")
        self.assertEqual(listed["total"], 2)
        new_event = listed["events"][1]
        self.assertEqual(new_event["summary"], "Planning, review; Q1")
        self.assertEqual(new_event["description"], "Line 1\nLine 2")
        self.assertEqual(new_event["location"], "Room 1, Building 2")
        self.assertNotEqual(
            listed["events"][0]["uid"], listed["events"][1]["uid"]
        )

    def test_add_normalizes_existing_line_endings(self):
        content = _calendar(
            _event(
                uid="lf@example.test",
                summary="LF input",
                start="DTSTART:20260301T090000Z",
                end="DTEND:20260301T100000Z",
            )
        ).replace(b"\r\n", b"\n")
        target = self._write(content)

        result = self.engine.add_event(
            "Documents",
            "calendar.ics",
            "Added",
            "2026-03-02T09:00:00Z",
            confirmed=True,
        )

        self.assertTrue(result["success"], result)
        data = target.read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        self.assertNotIn(b"\r", data.replace(b"\r\n", b""))
        listed = self.engine.list_events("Documents", "calendar.ics")
        self.assertEqual(listed["total"], 2)
        self.assertEqual(listed["events"][0]["summary"], "LF input")

    def test_add_accepts_lowercase_components(self):
        content = (
            _calendar(
                _event(
                    uid="lower@example.test",
                    summary="Lowercase calendar",
                    start="DTSTART:20260301T090000Z",
                    end="DTEND:20260301T100000Z",
                )
            )
            .replace(b"BEGIN:VCALENDAR", b"begin:vcalendar")
            .replace(b"END:VCALENDAR", b"end:vcalendar")
        )
        self._write(content)

        result = self.engine.add_event(
            "Documents",
            "calendar.ics",
            "Added",
            "2026-03-02T09:00:00Z",
            confirmed=True,
        )

        self.assertTrue(result["success"], result)
        listed = self.engine.list_events("Documents", "calendar.ics")
        self.assertEqual(listed["total"], 2)

    def test_add_creates_new_file(self):
        (self.docs / "Calendars").mkdir()

        result = self.engine.add_event(
            "Documents",
            "Calendars/work.ics",
            "Kickoff",
            date(2026, 4, 1),
            location="Online",
            confirmed=True,
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["added"])
        self.assertEqual(result["file"]["event_count"], 1)
        self.assertTrue(result["event"]["all_day"])
        self.assertEqual(result["event"]["start"], "2026-04-01")
        self.assertEqual(result["event"]["end"], "2026-04-02")

        target = self.docs / "Calendars" / "work.ics"
        text = target.read_bytes().decode("utf-8")
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("VERSION:2.0", text)
        self.assertIn("PRODID:", text)
        self.assertIn("DTSTART;VALUE=DATE:20260401", text)
        self.assertIn("DTEND;VALUE=DATE:20260402", text)
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        self.assertEqual(text.count("\n"), text.count("\r\n"))

        listed = self.engine.list_events("Documents", "Calendars/work.ics")
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["events"][0]["summary"], "Kickoff")

    def test_add_defaults_end(self):
        self._write(_many_events(1))
        timed = self.engine.add_event(
            "Documents",
            "calendar.ics",
            "Default end",
            "2026-05-01T10:00:00Z",
            confirmed=True,
        )
        self.assertEqual(timed["event"]["duration_seconds"], 3600)

    def test_uids_are_unique_across_adds(self):
        self._write(_many_events(1))
        for summary in ("First added", "Second added"):
            result = self.engine.add_event(
                "Documents",
                "calendar.ics",
                summary,
                "2026-06-01T10:00:00Z",
                confirmed=True,
            )
            self.assertTrue(result["success"], result)

        events = self.engine.list_events("Documents", "calendar.ics")["events"]
        uids = [event["uid"] for event in events]
        self.assertEqual(len(uids), 3)
        self.assertEqual(len(set(uids)), 3)
        self.assertEqual(sum(uid.endswith("@misty.local") for uid in uids), 2)

    def test_approval_gating_writes_nothing(self):
        original = _many_events(1)
        target = self._write(original)
        before_entries = sorted(path.name for path in self.docs.iterdir())

        result = self.engine.add_event(
            "Documents",
            "calendar.ics",
            "Needs approval",
            "2026-03-05T10:00:00Z",
        )

        self._assert_code(result, "approval_required")
        self.assertTrue(result["requires_confirmation"])
        self.assertFalse(result["added"])
        self.assertEqual(result["event"]["summary"], "Needs approval")
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(
            sorted(path.name for path in self.docs.iterdir()),
            before_entries,
        )

        created = self.engine.add_event(
            "Documents",
            "never-created.ics",
            "Needs approval",
            "2026-03-05T10:00:00Z",
        )
        self._assert_code(created, "approval_required")
        self.assertFalse((self.docs / "never-created.ics").exists())

    def test_atomic_write_failure_cleans_up(self):
        original = _many_events(1)
        target = self._write(original)
        before_entries = sorted(path.name for path in self.docs.iterdir())

        with patch(
            "app.actions.calendar_ics.os.replace",
            side_effect=OSError("boom /secret/path"),
        ):
            result = self.engine.add_event(
                "Documents",
                "calendar.ics",
                "Will fail",
                "2026-03-05T10:00:00Z",
                confirmed=True,
            )

        self._assert_code(result, "write_failed")
        self.assertFalse(result["added"])
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(
            sorted(path.name for path in self.docs.iterdir()),
            before_entries,
        )
        payload = json.dumps(result)
        self.assertNotIn("boom", payload)
        self.assertNotIn("/secret", payload)

    def test_existing_file_is_never_clobbered_on_race(self):
        self._write(_many_events(1))
        target = self.docs / "calendar.ics"
        real_replace = os.replace

        def replace_and_check(source, destination):
            self.assertEqual(Path(source).parent, target.parent)
            return real_replace(source, destination)

        with patch(
            "app.actions.calendar_ics.os.replace", side_effect=replace_and_check
        ):
            result = self.engine.add_event(
                "Documents",
                "calendar.ics",
                "Racy",
                "2026-03-05T10:00:00Z",
                confirmed=True,
            )
        self.assertTrue(result["success"], result)
        self.assertTrue((self.docs / "calendar.ics").exists())

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def test_invalid_dates_and_event_fields(self):
        self._write(_many_events(1))
        before = self._read()
        invalid_inputs = [
            ("", "2026-03-01T09:00:00Z"),
            ("   ", "2026-03-01T09:00:00Z"),
            ("x" * (MAX_SUMMARY_LENGTH + 1), "2026-03-01T09:00:00Z"),
            ("Summary", "not a date"),
            ("Summary", ""),
            ("Summary", "2026-02-30"),
            ("Summary", "2026-03-01T09:00:00"),  # naive datetime
            ("Summary", "2026-03-01T09:00:00+25:00"),
            ("Summary", "20260301"),
            ("Summary", 42),
            ("Summary", datetime(2026, 3, 1, 9, 0)),
            ("Summary", "2026-03-01T09:00:00Z", "2026-02-28T09:00:00Z"),
            ("Summary", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z"),
            ("Summary", "2026-03-01", "2026-03-02T00:00:00Z"),
            (
                "Summary",
                "2026-01-01T00:00:00Z",
                "2027-01-03T00:00:00Z",  # 367 days
            ),
        ]
        for summary, start, *rest in invalid_inputs:
            with self.subTest(summary=summary[:12], start=start):
                result = self.engine.add_event(
                    "Documents",
                    "calendar.ics",
                    summary,
                    start,
                    end=rest[0] if rest else None,
                    confirmed=True,
                )
                self._assert_code(result, "invalid_event")
                self.assertNotIn("Traceback", json.dumps(result))
        self.assertEqual(self._read(), before)

        self._assert_code(
            self.engine.add_event(
                "Documents",
                "calendar.ics",
                "Summary",
                "2026-03-01T09:00:00Z",
                description="d" * (MAX_TEXT_FIELD_LENGTH + 1),
                confirmed=True,
            ),
            "invalid_event",
        )
        self._assert_code(
            self.engine.add_event(
                "Documents",
                "calendar.ics",
                "Summary",
                "2026-03-01T09:00:00Z",
                location="l" * (MAX_TEXT_FIELD_LENGTH + 1),
                confirmed=True,
            ),
            "invalid_event",
        )
        self._assert_code(
            self.engine.add_event(
                "Documents",
                "calendar.ics",
                "Summary",
                "2026-03-01T09:00:00Z",
                end="2026-03-02T09:00:00Z",
                description=42,
                confirmed=True,
            ),
            "invalid_event",
        )
        self.assertEqual(self._read(), before)

    def test_max_duration_boundary_is_allowed(self):
        self._write(_many_events(1))
        start = date(2026, 1, 1)
        end = start + timedelta(days=MAX_EVENT_DURATION_DAYS)
        result = self.engine.add_event(
            "Documents",
            "calendar.ics",
            "Long planning",
            start,
            end=end,
            confirmed=True,
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(
            result["event"]["duration_seconds"],
            MAX_EVENT_DURATION_DAYS * 86400,
        )

    def test_duplicate_uid_is_rejected(self):
        content = _calendar(
            _event(
                uid="dup@example.test",
                summary="One",
                start="DTSTART:20260301T090000Z",
                end="DTEND:20260301T100000Z",
            ),
            _event(
                uid="dup@example.test",
                summary="Two",
                start="DTSTART:20260302T090000Z",
                end="DTEND:20260302T100000Z",
            ),
        )
        self._write(content)

        result = self.engine.add_event(
            "Documents",
            "calendar.ics",
            "Three",
            "2026-03-03T09:00:00Z",
            confirmed=True,
        )

        self._assert_code(result, "duplicate_uid")
        self.assertEqual(self._read(), content)

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------

    def test_scope_rejects_unknown_folders(self):
        for folder in ("Nope", "", None, "/etc", "../Documents"):
            with self.subTest(folder=folder):
                self._assert_code(
                    self.engine.list_events(folder, "calendar.ics"),
                    "invalid_folder",
                )
                self._assert_code(
                    self.engine.add_event(
                        folder,
                        "calendar.ics",
                        "Summary",
                        "2026-03-01T09:00:00Z",
                        confirmed=True,
                    ),
                    "invalid_folder",
                )

    def test_scope_rejects_absolute_traversal_and_non_ics(self):
        outside = self.home / "outside.ics"
        outside.write_bytes(b"do not touch")
        before = outside.read_bytes()

        bad_paths = (
            "/etc/passwd",
            str(outside),
            "../outside.ics",
            "nested/../../outside.ics",
            "notes.txt",
            "calendar.ICS.bak",
            "",
            "./",
            "calendar.ics/",
            "a" * 300 + ".ics",
        )
        for relative in bad_paths:
            with self.subTest(relative=relative):
                self._assert_code(
                    self.engine.list_events("Documents", relative),
                    "invalid_path",
                )
                self._assert_code(
                    self.engine.add_event(
                        "Documents",
                        relative,
                        "Summary",
                        "2026-03-01T09:00:00Z",
                        confirmed=True,
                    ),
                    "invalid_path",
                )
        self.assertEqual(outside.read_bytes(), before)

    def test_scope_rejects_missing_parents_and_files(self):
        self._assert_code(
            self.engine.list_events("Documents", "missing.ics"),
            "file_not_found",
        )
        self._assert_code(
            self.engine.add_event(
                "Documents",
                "missing/calendar.ics",
                "Summary",
                "2026-03-01T09:00:00Z",
                confirmed=True,
            ),
            "invalid_path",
        )
        self.assertFalse((self.docs / "missing").exists())

    def test_scope_rejects_symlinks(self):
        outside_temp = tempfile.TemporaryDirectory()
        self.addCleanup(outside_temp.cleanup)
        outside = Path(outside_temp.name)
        victim = outside / "victim.ics"
        original = _many_events(1)
        victim.write_bytes(original)

        (self.docs / "link.ics").symlink_to(victim)
        (self.docs / "linkdir").symlink_to(outside)

        self._assert_code(
            self.engine.list_events("Documents", "link.ics"), "invalid_path"
        )
        self._assert_code(
            self.engine.add_event(
                "Documents",
                "link.ics",
                "Attack",
                "2026-03-05T10:00:00Z",
                confirmed=True,
            ),
            "invalid_path",
        )
        self._assert_code(
            self.engine.list_events("Documents", "linkdir/victim.ics"),
            "invalid_path",
        )
        self._assert_code(
            self.engine.add_event(
                "Documents",
                "linkdir/new.ics",
                "Attack",
                "2026-03-05T10:00:00Z",
                confirmed=True,
            ),
            "invalid_path",
        )

        self.assertEqual(victim.read_bytes(), original)
        self.assertFalse((outside / "new.ics").exists())

    # ------------------------------------------------------------------
    # Bounds
    # ------------------------------------------------------------------

    def test_file_size_limit(self):
        oversized = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-FILL:"
            + b"a" * MAX_FILE_BYTES
            + b"\r\nEND:VCALENDAR\r\n"
        )
        self.assertGreater(len(oversized), MAX_FILE_BYTES)
        target = self._write(oversized)

        result = self.engine.list_events("Documents", "calendar.ics")
        self._assert_code(result, "file_too_large")

        result = self.engine.add_event(
            "Documents",
            "calendar.ics",
            "Summary",
            "2026-03-01T09:00:00Z",
            confirmed=True,
        )
        self._assert_code(result, "file_too_large")
        self.assertEqual(target.read_bytes(), oversized)

    def test_event_count_limit(self):
        self._write(_many_events(MAX_EVENTS))
        result = self.engine.list_events("Documents", "calendar.ics")
        self.assertTrue(result["success"])
        self.assertEqual(result["file"]["event_count"], MAX_EVENTS)
        self.assertEqual(result["total"], MAX_EVENTS)
        self.assertEqual(result["count"], 100)

        self._write(_many_events(MAX_EVENTS + 1))
        self._assert_code(
            self.engine.list_events("Documents", "calendar.ics"),
            "too_many_events",
        )

        full_content = _many_events(MAX_EVENTS)
        self._write(full_content)
        result = self.engine.add_event(
            "Documents",
            "calendar.ics",
            "One too many",
            "2026-03-01T09:00:00Z",
            confirmed=True,
        )
        self._assert_code(result, "too_many_events")
        self.assertEqual(self._read(), full_content)

    def test_parse_truncates_long_text_fields(self):
        content = _calendar(
            [
                "BEGIN:VEVENT",
                "UID:long@example.test",
                "SUMMARY:" + "s" * (MAX_SUMMARY_LENGTH + 50),
                "DESCRIPTION:" + "d" * (MAX_TEXT_FIELD_LENGTH + 50),
                "LOCATION:" + "l" * (MAX_TEXT_FIELD_LENGTH + 50),
                "DTSTART:20260301T090000Z",
                "DTEND:20260301T100000Z",
                "END:VEVENT",
            ]
        )
        self._write(content)

        event = self.engine.list_events("Documents", "calendar.ics")["events"][0]

        self.assertEqual(len(event["summary"]), MAX_SUMMARY_LENGTH)
        self.assertEqual(len(event["description"]), MAX_TEXT_FIELD_LENGTH)
        self.assertEqual(len(event["location"]), MAX_TEXT_FIELD_LENGTH)
        self.assertTrue(event["truncated"])

    # ------------------------------------------------------------------
    # Invalid calendars
    # ------------------------------------------------------------------

    def test_invalid_calendar_documents(self):
        invalid_documents = (
            b"not a calendar at all",
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n",  # no END
            b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\n",  # unclosed
            b"BEGIN:VEVENT\r\nUID:x\r\nDTSTART:20260301T090000Z\r\nEND:VEVENT\r\n",
            _calendar(
                [
                    "BEGIN:VEVENT",
                    "UID:nodate@example.test",
                    "SUMMARY:No date",
                    "END:VEVENT",
                ]
            ),
            _calendar(
                _event(
                    uid="bad@example.test",
                    summary="Bad date",
                    start="DTSTART:20260230T090000Z",
                    end="DTEND:20260230T100000Z",
                )
            ),
            _calendar(
                _event(
                    uid="tz@example.test",
                    summary="Bad zone",
                    start="DTSTART;TZID=Mars/Base:20260301T090000",
                    end="DTEND;TZID=Mars/Base:20260301T100000",
                )
            ),
            _calendar(
                _event(
                    uid="order@example.test",
                    summary="Backwards",
                    start="DTSTART:20260302T090000Z",
                    end="DTEND:20260301T090000Z",
                )
            ),
            _calendar(
                [
                    "BEGIN:VEVENT",
                    "UID:long@example.test",
                    "SUMMARY:Too long",
                    "DTSTART:20260101T000000Z",
                    "DURATION:P367D",
                    "END:VEVENT",
                ]
            ),
            b"BEGIN:VCALENDAR\r\nSUMMARY:\xff\xfe\r\nEND:VCALENDAR\r\n",
            b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\n"
            b"DTSTART;VALUE=DATE:20260301T090000Z\r\nEND:VEVENT\r\n"
            b"END:VCALENDAR\r\n",
        )
        for index, content in enumerate(invalid_documents):
            with self.subTest(index=index):
                self._write(content)
                self._assert_code(
                    self.engine.list_events("Documents", "calendar.ics"),
                    "invalid_ics",
                )
                self._assert_code(
                    self.engine.add_event(
                        "Documents",
                        "calendar.ics",
                        "Summary",
                        "2026-03-01T09:00:00Z",
                        confirmed=True,
                    ),
                    "invalid_ics",
                )
                self.assertEqual(self._read(), content)

    def test_errors_do_not_leak_content(self):
        secret = "BEGIN:VCALENDAR\r\nX-SECRET:TOPSECRET-PAYLOAD\r\n"
        self._write(secret.encode("utf-8"))

        result = self.engine.list_events("Documents", "calendar.ics")
        self._assert_code(result, "invalid_ics")
        payload = json.dumps(result)
        self.assertNotIn("TOPSECRET", payload)
        self.assertNotIn("X-SECRET", payload)
        self.assertNotIn(str(self.home), payload)
        self.assertNotIn("BEGIN:VCALENDAR", payload)

        result = self.engine.add_event(
            "Documents",
            "calendar.ics",
            "Summary",
            "2026-03-01T09:00:00Z",
            confirmed=True,
        )
        self._assert_code(result, "invalid_ics")
        self.assertNotIn("TOPSECRET", json.dumps(result))

    def test_error_payloads_are_json_safe(self):
        self._write(_many_events(1))
        results = [
            self.engine.list_events("Documents", "calendar.ics"),
            self.engine.list_events("Nope", "calendar.ics"),
            self.engine.list_events("Documents", "../escape.ics"),
            self.engine.add_event(
                "Documents",
                "calendar.ics",
                "Summary",
                "2026-03-05T10:00:00Z",
            ),
            self.engine.add_event(
                "Documents",
                "calendar.ics",
                "",
                "2026-03-05T10:00:00Z",
                confirmed=True,
            ),
        ]
        for result in results:
            with self.subTest(code=result["code"]):
                encoded = json.dumps(result, allow_nan=False)
                self.assertIsInstance(encoded, str)
                for key in (
                    "success",
                    "code",
                    "message",
                    "events",
                    "event",
                    "file",
                    "added",
                ):
                    self.assertIn(key, result)


if __name__ == "__main__":
    unittest.main()
