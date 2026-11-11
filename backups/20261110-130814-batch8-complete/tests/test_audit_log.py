"""Tests for the isolated audit log and task journal.

All storage lives in temporary directories.  No user data is read and no
network, model or Qt component is touched.
"""

import json
import os
import re
import stat
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.engine.audit_log import (
    MAX_CHECKPOINT_BYTES,
    MAX_REQUEST_LABEL_LENGTH,
    MAX_SUMMARY_LENGTH,
    SENSITIVE_KEYS,
    AuditLog,
    TaskJournal,
    is_sensitive_key,
    redact_fields,
    redact_summary,
)

ARCHIVE_NAME_RE = re.compile(
    r"^audit-\d{8}T\d{6}\.\d{6}Z-\d{8}-[0-9a-f]{8}\.jsonl$"
)

AUDIT_FIELDS = {
    "timestamp",
    "event_type",
    "action",
    "status",
    "request_id",
    "duration_ms",
    "summary",
    "error_code",
}


def parse_utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make_log(self, name="audit", **kwargs):
        log = AuditLog(self.root / name, **kwargs)
        self.addCleanup(log.close)
        return log

    def all_records(self, log):
        records = []
        for path in log.archives() + [log.path]:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
        return records

    # -- schema / append / read ----------------------------------------

    def test_append_and_read_explicit_fields(self):
        log = self.make_log()
        result = log.log_event(
            "tool_call",
            "read_clipboard",
            "started",
            request_id="req-1",
            duration_ms=12.6,
            summary="clipboard read requested",
            error_code="tool_busy",
        )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.record["duration_ms"], 13)
        self.assertEqual(result.record["summary"], "clipboard read requested")

        records = log.read_records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(set(record), AUDIT_FIELDS)
        self.assertEqual(record["event_type"], "tool_call")
        self.assertEqual(record["action"], "read_clipboard")
        self.assertEqual(record["status"], "started")
        self.assertEqual(record["request_id"], "req-1")
        self.assertEqual(record["duration_ms"], 13)
        self.assertEqual(record["error_code"], "tool_busy")
        self.assertTrue(record["timestamp"].endswith("Z"))

        raw_lines = log.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(raw_lines), 1)
        self.assertEqual(json.loads(raw_lines[0]), record)

    def test_optional_fields_default_to_null(self):
        log = self.make_log()
        result = log.log_event("app", "startup", "success")

        self.assertTrue(result.ok, result.error)
        record = log.read_records()[0]
        self.assertIsNone(record["request_id"])
        self.assertIsNone(record["duration_ms"])
        self.assertIsNone(record["error_code"])

    def test_timestamp_is_utc_and_recent(self):
        log = self.make_log()
        before = datetime.now(timezone.utc)
        log.log_event("tool_call", "get_time", "success")
        after = datetime.now(timezone.utc)

        timestamp = parse_utc(log.read_records()[0]["timestamp"])
        self.assertIsNotNone(timestamp.tzinfo)
        self.assertEqual(timestamp.utcoffset(), timedelta(0))
        self.assertGreaterEqual(timestamp, before - timedelta(seconds=1))
        self.assertLessEqual(timestamp, after + timedelta(seconds=1))

    def test_read_limit_returns_most_recent(self):
        log = self.make_log()
        for index in range(3):
            log.log_event(
                "tool_call",
                "step",
                "success",
                request_id=f"req-{index}",
            )

        records = log.read_records(limit=2)
        self.assertEqual(
            [record["request_id"] for record in records],
            ["req-1", "req-2"],
        )
        self.assertEqual(log.read_records(limit=0), [])
        self.assertEqual(log.read_records(limit=-1), [])

    # -- permissions ----------------------------------------------------

    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_private_directory_and_file_permissions(self):
        log = self.make_log("private", max_bytes=64)
        log.log_event("tool_call", "first", "success")

        self.assertEqual(
            stat.S_IMODE(os.stat(log.directory).st_mode), 0o700
        )
        self.assertEqual(
            stat.S_IMODE(os.stat(log.path).st_mode), 0o600
        )

        log.log_event("tool_call", "second", "success")
        archives = log.archives()
        self.assertTrue(archives)
        for archive in archives:
            self.assertEqual(
                stat.S_IMODE(os.stat(archive).st_mode), 0o600
            )

    # -- redaction ------------------------------------------------------

    def test_sensitive_assignments_are_removed_from_summary(self):
        log = self.make_log()
        summary = (
            "password=hunter2 token: abc123 "
            'api_key="sk-live-secret" result={"ok":true} '
            "clipboard=private words"
        )
        result = log.log_event("tool_call", "login", "failure", summary=summary)

        self.assertTrue(result.ok, result.error)
        stored = log.read_records()[0]["summary"]
        for secret in ("hunter2", "abc123", "sk-live-secret", "private words"):
            self.assertNotIn(secret, stored)
        self.assertGreaterEqual(stored.count("[redacted]"), 4)

    def test_json_style_nested_sensitive_values_are_removed(self):
        log = self.make_log()
        summary = (
            '{"clipboard": "json-secret", '
            '"result": {"nested": "deep-secret"}, '
            '"secretKey": "camel-secret"}'
        )
        result = log.log_event(
            "tool_call", "read_clipboard", "success", summary=summary
        )

        self.assertTrue(result.ok, result.error)
        stored = log.read_records()[0]["summary"]
        for secret in ("json-secret", "deep-secret", "camel-secret"):
            self.assertNotIn(secret, stored)
        self.assertGreaterEqual(stored.count("[redacted]"), 3)

    def test_action_is_scrubbed_as_well(self):
        log = self.make_log()
        result = log.log_event(
            "tool_call", "password=hunter2", "success"
        )

        self.assertTrue(result.ok, result.error)
        record = log.read_records()[0]
        self.assertNotIn("hunter2", record["action"])
        self.assertEqual(record["action"], "password=[redacted]")

    def test_authorization_header_is_removed(self):
        log = self.make_log()
        log.log_event(
            "tool_call",
            "fetch",
            "failure",
            summary="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret",
        )

        stored = log.read_records()[0]["summary"]
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", stored)
        self.assertNotIn("secret", stored)

    def test_long_summary_is_capped(self):
        log = self.make_log()
        result = log.log_event(
            "response", "summarize", "success", summary="x" * 5000
        )

        self.assertTrue(result.ok, result.error)
        stored = log.read_records()[0]["summary"]
        self.assertLessEqual(len(stored), MAX_SUMMARY_LENGTH)
        self.assertTrue(stored.endswith("[truncated]"))

    def test_summary_rejects_structured_payload(self):
        log = self.make_log("structured")
        result = log.log_event(
            "tool_call",
            "read_clipboard",
            "success",
            summary={"clipboard": "private clipboard text"},
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_summary")
        self.assertNotIn("private clipboard text", result.error or "")
        self.assertFalse(log.path.exists())

    def test_no_arbitrary_payload_keyword_is_accepted(self):
        log = self.make_log("payload")
        with self.assertRaises(TypeError):
            log.log_event(
                "tool_call",
                "read_clipboard",
                "success",
                metadata={"clipboard": "private"},
            )

    def test_redact_summary_helper(self):
        self.assertEqual(
            redact_summary("password=abc token: def"),
            "password=[redacted] token=[redacted]",
        )
        self.assertNotIn(
            "abc.def",
            redact_summary("Authorization: Bearer abc.def"),
        )
        self.assertEqual(redact_summary("one\ntwo"), "one two")
        self.assertNotIn(
            "single-secret",
            redact_summary("result={'nested': 'single-secret'}"),
        )
        self.assertEqual(redact_summary(None), "")
        with self.assertRaises(TypeError):
            redact_summary({"text": "secret"})
        long_value = redact_summary("y" * 1000, max_length=50)
        self.assertLessEqual(len(long_value), 50)
        self.assertTrue(long_value.endswith("[truncated]"))

    def test_redact_fields_drops_sensitive_and_nested_values(self):
        safe = redact_fields(
            {
                "tool": "read_clipboard",
                "password": "hunter2",
                "ocr_text": "private",
                "user_token": "abc",
                "nested": {"text": "private"},
                "long": "z" * 1000,
                "count": 3,
                "ok": True,
                "none": None,
                "not_finite": float("nan"),
                1: "non-string key",
            }
        )

        self.assertEqual(
            set(safe), {"tool", "long", "count", "ok", "none"}
        )
        self.assertLessEqual(len(safe["long"]), MAX_SUMMARY_LENGTH)
        with self.assertRaises(TypeError):
            redact_fields(["not", "a", "mapping"])

    def test_sensitive_key_detection(self):
        for key in SENSITIVE_KEYS:
            self.assertTrue(is_sensitive_key(key), key)
        for key in (
            "user_token",
            "clipboardText",
            "ocr-text",
            "AccessToken",
            "document_text",
            "visualInterpretation",
            "OCRText",
            "APIToken",
        ):
            self.assertTrue(is_sensitive_key(key), key)
        for key in ("step", "action", "status", "count", "url", "labels"):
            self.assertFalse(is_sensitive_key(key), key)

    # -- validation / failures -----------------------------------------

    def test_invalid_explicit_fields_are_rejected_safely(self):
        log = self.make_log("invalid")

        cases = [
            (("", "action", "success"), {}, "invalid_event_type"),
            (("tool_call", "", "success"), {}, "invalid_action"),
            (("tool_call", "action", "bad status"), {}, "invalid_status"),
            (
                ("tool_call", "action", "success"),
                {"request_id": "bad id"},
                "invalid_request_id",
            ),
            (
                ("tool_call", "action", "success"),
                {"duration_ms": -1},
                "invalid_duration_ms",
            ),
            (
                ("tool_call", "action", "success"),
                {"duration_ms": float("nan")},
                "invalid_duration_ms",
            ),
            (
                ("tool_call", "action", "success"),
                {"error_code": "bad code"},
                "invalid_error_code",
            ),
        ]

        for args, kwargs, expected_code in cases:
            with self.subTest(code=expected_code):
                result = log.log_event(*args, **kwargs)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, expected_code)
                self.assertIsInstance(result.error, str)
                self.assertNotIn("Traceback", result.error)

        self.assertFalse(log.path.exists())

    def test_closed_log_refuses_writes(self):
        log = self.make_log("closed")
        log.close()
        result = log.log_event("tool_call", "late", "success")

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "log_closed")
        log.close()  # idempotent

    def test_open_failure_returns_safe_message(self):
        log = self.make_log("open-failure")
        with mock.patch.object(
            AuditLog, "_open_locked", side_effect=OSError("private detail")
        ):
            result = log.log_event("tool_call", "fail", "failure")

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "write_failed")
        self.assertNotIn("private detail", result.error or "")
        self.assertFalse(log.path.exists())

    # -- rotation -------------------------------------------------------

    def test_rotation_by_size_unique_names_and_preserved_records(self):
        log = self.make_log("rotate", max_bytes=64, max_files=10)
        unrelated = {
            "keep.txt": "keep me",
            "audit-manual.jsonl": "manual",
            "other.jsonl": "other",
        }
        for name, content in unrelated.items():
            (log.directory / name).write_text(content, encoding="utf-8")
        odd_directory = (
            log.directory
            / "audit-20260101T000000.000000Z-00000000-deadbeef.jsonl"
        )
        odd_directory.mkdir()

        for index in range(5):
            result = log.log_event(
                "tool_call",
                "step",
                "success",
                request_id=f"req-{index}",
                summary="rotation payload",
            )
            self.assertTrue(result.ok, result.error)

        archives = log.archives()
        self.assertEqual(len(archives), 4)
        names = [archive.name for archive in archives]
        self.assertEqual(len(set(names)), len(names))
        for name in names:
            self.assertRegex(name, ARCHIVE_NAME_RE)

        for name, content in unrelated.items():
            self.assertEqual(
                (log.directory / name).read_text(encoding="utf-8"), content
            )
        self.assertTrue(odd_directory.is_dir())

        records = self.all_records(log)
        self.assertEqual(
            {record["request_id"] for record in records},
            {f"req-{index}" for index in range(5)},
        )

    def test_pruning_keeps_newest_archives_and_unrelated_files(self):
        log = self.make_log("prune", max_bytes=64, max_files=2)
        (log.directory / "keep.txt").write_text("keep", encoding="utf-8")

        for index in range(6):
            result = log.log_event(
                "tool_call",
                "step",
                "success",
                request_id=f"req-{index}",
            )
            self.assertTrue(result.ok, result.error)

        archives = log.archives()
        self.assertEqual(len(archives), 2)
        self.assertTrue((log.directory / "keep.txt").is_file())

        records = self.all_records(log)
        self.assertEqual(
            {record["request_id"] for record in records},
            {"req-3", "req-4", "req-5"},
        )

    def test_rotation_failure_continues_logging_safely(self):
        log = self.make_log("rotation-failure", max_bytes=64, max_files=2)
        self.assertTrue(
            log.log_event("tool_call", "first", "success").ok
        )

        with mock.patch(
            "app.engine.audit_log.os.replace",
            side_effect=OSError("disk full"),
        ):
            result = log.log_event("tool_call", "second", "success")

        self.assertTrue(result.ok, result.error)
        self.assertIsNone(result.archived_to)
        self.assertIn("rotation_failed", result.warnings)
        self.assertEqual(log.archives(), [])
        self.assertEqual(len(log.read_records()), 2)

    # -- concurrency ----------------------------------------------------

    def test_concurrent_logging_does_not_corrupt_lines(self):
        log = self.make_log(
            "concurrent", max_bytes=10_000_000, max_files=5
        )
        threads = 8
        per_thread = 25

        def worker(worker_index):
            outcomes = []
            for item in range(per_thread):
                result = log.log_event(
                    "tool_call",
                    "concurrent",
                    "success",
                    request_id=f"t{worker_index}-{item}",
                    summary="threaded event",
                )
                outcomes.append(result.ok)
            return all(outcomes)

        with ThreadPoolExecutor(max_workers=threads) as pool:
            results = list(pool.map(worker, range(threads)))

        self.assertTrue(all(results))
        lines = log.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), threads * per_thread)

        records = []
        for line in lines:
            records.append(json.loads(line))
        self.assertEqual(len(records), threads * per_thread)
        self.assertEqual(
            len({record["request_id"] for record in records}),
            threads * per_thread,
        )
        self.assertTrue(
            all(record["summary"] == "threaded event" for record in records)
        )

    def test_concurrent_logging_with_rotation(self):
        log = self.make_log(
            "concurrent-rotate", max_bytes=64, max_files=100
        )
        threads = 4
        per_thread = 10

        def worker(worker_index):
            outcomes = []
            for item in range(per_thread):
                result = log.log_event(
                    "tool_call",
                    "rotate",
                    "success",
                    request_id=f"r{worker_index}-{item}",
                )
                outcomes.append(result.ok)
            return all(outcomes)

        with ThreadPoolExecutor(max_workers=threads) as pool:
            results = list(pool.map(worker, range(threads)))

        self.assertTrue(all(results))
        self.assertGreaterEqual(len(log.archives()), 1)
        records = self.all_records(log)
        self.assertEqual(len(records), threads * per_thread)
        self.assertEqual(
            len({record["request_id"] for record in records}),
            threads * per_thread,
        )


class TaskJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "journal" / "tasks.sqlite3"
        self.journal = TaskJournal(self.database)

    def start(self, task_id="task-1", **kwargs):
        result = self.journal.start_task(task_id, **kwargs)
        self.assertTrue(result.ok, result.error)
        return result.task

    # -- lifecycle ------------------------------------------------------

    def test_start_checkpoint_finish_get_and_recent(self):
        task = self.start(
            "task-1",
            request_label="Open the weather page",
            checkpoint={"step": 1, "labels": ["a"], "ok": True},
        )
        self.assertEqual(task["task_id"], "task-1")
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["request_label"], "Open the weather page")
        self.assertEqual(
            task["checkpoint"], {"step": 1, "labels": ["a"], "ok": True}
        )
        self.assertTrue(task["created_at"].endswith("Z"))
        self.assertEqual(task["created_at"], task["updated_at"])
        self.assertEqual(
            parse_utc(task["created_at"]).utcoffset(), timedelta(0)
        )

        time.sleep(0.002)
        result = self.journal.checkpoint(
            "task-1", {"step": 2, "labels": ["a", "b"]}
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.task["status"], "running")
        self.assertEqual(result.task["checkpoint"]["step"], 2)
        self.assertGreater(
            result.task["updated_at"], result.task["created_at"]
        )

        result = self.journal.checkpoint(
            "task-1", {"step": 3}, status="paused"
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.task["status"], "paused")

        time.sleep(0.002)
        result = self.journal.finish_task(
            "task-1",
            status="completed",
            checkpoint={"step": 4, "done": True},
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.task["status"], "completed")
        self.assertEqual(result.task["checkpoint"]["done"], True)

        stored = self.journal.get_task("task-1")
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["checkpoint"], {"step": 4, "done": True})
        self.assertIsNone(self.journal.get_task("missing-task"))

        self.start("task-2", request_label="Second")
        self.start("task-3", request_label="Third")
        time.sleep(0.002)
        self.assertTrue(
            self.journal.finish_task("task-2", status="failed").ok
        )

        recent = self.journal.recent_tasks(limit=10)
        self.assertEqual(len(recent), 3)
        self.assertEqual(recent[0]["task_id"], "task-2")

        completed = self.journal.recent_tasks(
            limit=10, status="completed"
        )
        self.assertEqual([task["task_id"] for task in completed], ["task-1"])

    def test_finish_without_checkpoint_preserves_it(self):
        self.start("task-1", checkpoint={"step": 1})
        result = self.journal.finish_task("task-1")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.task["checkpoint"], {"step": 1})
        self.assertEqual(result.task["status"], "completed")

    def test_cancelled_alias_is_normalized(self):
        self.start("task-1")
        result = self.journal.finish_task("task-1", status="canceled")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.task["status"], "cancelled")

    # -- request redaction ---------------------------------------------

    def test_request_label_is_redacted_and_capped(self):
        label = "password=hunter2 " + "x" * 400
        task = self.start("task-redact", request_label=label)
        stored = task["request_label"]

        self.assertNotIn("hunter2", stored)
        self.assertIn("[redacted]", stored)
        self.assertLessEqual(len(stored), MAX_REQUEST_LABEL_LENGTH)

        raw_database = self.database.read_bytes()
        self.assertNotIn(b"hunter2", raw_database)

    # -- checkpoints ----------------------------------------------------

    def test_checkpoint_accepts_json_safe_non_sensitive_data(self):
        self.start("task-1")
        checkpoint = {
            "step": 2,
            "labels": ["alpha", "beta"],
            "ok": True,
            "score": 1.5,
            "note": None,
            "name": "Zażółć gęślą jaźń",
            "nested": {"values": [1, 2, 3]},
        }
        result = self.journal.checkpoint("task-1", checkpoint)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.task["checkpoint"], checkpoint)
        json.dumps(result.task["checkpoint"], allow_nan=False)
        self.assertEqual(
            self.journal.get_task("task-1")["checkpoint"], checkpoint
        )

    def test_sensitive_checkpoint_keys_are_rejected(self):
        self.start("task-1", checkpoint={"step": 1})
        before = self.journal.get_task("task-1")["checkpoint"]

        checkpoints = [
            {"step": 2, "password": "hunter2"},
            {"step": 2, "ocr_text": "private"},
            {"step": 2, "arguments": ["private"]},
            {"step": 2, "result": {"private": True}},
            {"outer": {"visualInterpretation": "private"}},
            {"clipboardContent": "private"},
        ]
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint):
                result = self.journal.checkpoint("task-1", checkpoint)
                self.assertFalse(result.ok)
                self.assertEqual(
                    result.error_code, "sensitive_checkpoint_key"
                )
                self.assertEqual(
                    self.journal.get_task("task-1")["checkpoint"], before
                )

        result = self.journal.start_task(
            "task-sensitive", checkpoint={"token": "abc"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "sensitive_checkpoint_key")
        self.assertIsNone(self.journal.get_task("task-sensitive"))

    def test_invalid_checkpoints_are_rejected(self):
        self.start("task-1")
        bad_checkpoints = [
            ["not", "an", "object"],
            "not an object",
            {"bad": object()},
            {"bad": float("nan")},
            {"bad": float("inf")},
            {"bad": b"bytes"},
            {"bad": {1, 2}},
        ]
        for checkpoint in bad_checkpoints:
            with self.subTest(checkpoint=repr(checkpoint)):
                result = self.journal.checkpoint("task-1", checkpoint)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, "invalid_checkpoint")

        deep = {"level": 1}
        current = deep
        for level in range(2, 8):
            current["next"] = {"level": level}
            current = current["next"]
        result = self.journal.checkpoint("task-1", deep)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_checkpoint")

        oversized = {"blob": "x" * (MAX_CHECKPOINT_BYTES + 100)}
        result = self.journal.checkpoint("task-1", oversized)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "checkpoint_too_large")

    # -- IDs and statuses ----------------------------------------------

    def test_task_id_validation(self):
        invalid_ids = [
            "",
            "   ",
            "bad id",
            "id/with/slash",
            "../escape",
            "x" * 129,
            123,
            None,
        ]
        for task_id in invalid_ids:
            with self.subTest(task_id=repr(task_id)):
                result = self.journal.start_task(task_id)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, "invalid_task_id")
                self.assertIsNone(self.journal.get_task(task_id))

        boundary_id = "a" * 128
        result = self.journal.start_task(boundary_id)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(
            self.journal.get_task(boundary_id)["task_id"], boundary_id
        )

        result = self.journal.checkpoint("bad id", {})
        self.assertEqual(result.error_code, "invalid_task_id")
        result = self.journal.finish_task("bad id")
        self.assertEqual(result.error_code, "invalid_task_id")

    def test_status_validation(self):
        result = self.journal.start_task("task-1", status="done")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_status")

        result = self.journal.start_task("task-1", status="completed")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_status")

        self.start("task-1")
        result = self.journal.checkpoint("task-1", {"step": 2}, status="nope")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_status")
        self.assertEqual(
            self.journal.get_task("task-1")["status"], "running"
        )

        result = self.journal.finish_task("task-1", status="running")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_status")
        self.assertEqual(
            self.journal.get_task("task-1")["status"], "running"
        )

        result = self.journal.finish_task("task-1", status="failed")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.task["status"], "failed")

    def test_duplicate_start_and_missing_tasks(self):
        self.start("task-1", request_label="original")

        duplicate = self.journal.start_task(
            "task-1", request_label="replacement"
        )
        self.assertFalse(duplicate.ok)
        self.assertEqual(duplicate.error_code, "task_exists")
        self.assertEqual(
            self.journal.get_task("task-1")["request_label"], "original"
        )

        result = self.journal.checkpoint("missing", {})
        self.assertEqual(result.error_code, "not_found")
        result = self.journal.finish_task("missing")
        self.assertEqual(result.error_code, "not_found")

    def test_recent_tasks_validates_arguments(self):
        self.start("task-1")
        for invalid_limit in (0, -1, 101, True, "5"):
            with self.subTest(limit=invalid_limit):
                with self.assertRaises(ValueError):
                    self.journal.recent_tasks(limit=invalid_limit)
        with self.assertRaises(ValueError):
            self.journal.recent_tasks(status="not-a-status")

        self.assertEqual(
            len(self.journal.recent_tasks(limit=1)), 1
        )

    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_database_permissions(self):
        self.assertEqual(
            stat.S_IMODE(os.stat(self.database).st_mode), 0o600
        )
        self.assertEqual(
            stat.S_IMODE(os.stat(self.database.parent).st_mode), 0o700
        )

    # -- concurrency ----------------------------------------------------

    def test_concurrent_task_lifecycle(self):
        total = 16

        def worker(index):
            task_id = f"task-{index}"
            start = self.journal.start_task(task_id)
            if not start.ok:
                return ("start", start.error_code)
            checkpoint = self.journal.checkpoint(
                task_id, {"step": 2, "index": index}
            )
            if not checkpoint.ok:
                return ("checkpoint", checkpoint.error_code)
            finish = self.journal.finish_task(task_id)
            if not finish.ok:
                return ("finish", finish.error_code)
            return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, range(total)))

        self.assertEqual(results, [None] * total)
        self.assertEqual(
            len(self.journal.recent_tasks(limit=100)), total
        )
        for index in range(total):
            task = self.journal.get_task(f"task-{index}")
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["checkpoint"]["index"], index)


if __name__ == "__main__":
    unittest.main()
