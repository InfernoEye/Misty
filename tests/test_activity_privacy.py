"""Privacy and retention tests for local activity history.

These tests cover the confirmed leak: a tool marked ``sensitive`` was
redacted, but the later "Task completed" decision event embedded the final
model answer (which can quote clipboard or screen text) without any flag.
"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.brain.agent import (
    PRIVATE_RESULT_TOOLS,
    MistyAgent,
    _answer_tainted_by_private_actions,
    _topic_touches_private_data,
)
from app.brain.tools import (
    ACCESSIBILITY_TOOL_DEFINITIONS,
    KNOWLEDGE_TOOL_DEFINITIONS,
    TOOL_DEFINITIONS,
)
from app.engine.activity_store import (
    DEFAULT_RETENTION_DAYS,
    MAX_RETENTION_DAYS,
    REDACTED_DETAILS,
    ActivityStore,
)

CANARY = "CANARY-CLIPBOARD-7f3a9c"


def plan_response(steps, response=""):
    import json

    return {
        "content": json.dumps(
            {"steps": steps, "response": response}, ensure_ascii=False
        )
    }


class ClipboardRegistry:
    """Fake registry whose read_clipboard returns the canary."""

    def __init__(self):
        self.executed = []
        self.tool_results = {}

    def available_tools(self):
        return (
            list(TOOL_DEFINITIONS)
            + list(ACCESSIBILITY_TOOL_DEFINITIONS)
            + list(KNOWLEDGE_TOOL_DEFINITIONS)
        )

    def execute(self, name, arguments, confirmed=False):
        self.executed.append(name)
        return self.tool_results.get(
            name, {"success": True, "message": "ok"}
        )

    def requires_confirmation(self, name):
        return False

    def resolve_window_target(self, window_id):
        return None


class TestableAgent(MistyAgent):
    def __init__(self, registry, responses):
        super().__init__(registry=registry)
        self.responses = list(responses)
        self.chat_calls = []

    def start(self):
        pass

    def stop(self):
        pass

    def _chat(self, messages, tools=None, response_format=None):
        self.chat_calls.append({"messages": messages})
        if not self.responses:
            raise AssertionError("Unexpected additional model request")
        return self.responses.pop(0)


class ActivityStoreRedactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "activity.sqlite3"
        self.store = ActivityStore(self.path)

    def _details(self, **event):
        self.store.add_event({"kind": "decision", "title": "t", **event})
        return self.store.recent_events(1)[0]["details"]

    def test_truthy_sensitive_variants_are_redacted(self):
        for flag in (True, 1, "yes"):
            with self.subTest(flag=flag):
                self.assertEqual(
                    self._details(details="secret text", sensitive=flag),
                    REDACTED_DETAILS,
                )
                self.store.clear()

    def test_answer_and_details_flags_are_redacted(self):
        for event in (
            {"details": "secret text", "answer_sensitive": True},
            {"details": "secret text", "details_sensitive": True},
            {"details": {"sensitive": True, "value": "secret text"}},
        ):
            with self.subTest(event=event):
                self.store.clear()
                self.assertEqual(self._details(**event), REDACTED_DETAILS)

    def test_falsy_sensitive_keeps_details(self):
        self.assertEqual(
            self._details(details="public", sensitive=False),
            "public",
        )

    def test_redaction_keeps_title_and_status(self):
        self.store.add_event({
            "kind": "decision",
            "title": "Task completed",
            "details": CANARY,
            "status": "success",
            "sensitive": True,
        })
        row = self.store.recent_events(1)[0]
        self.assertEqual(row["title"], "Task completed")
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["kind"], "decision")
        self.assertEqual(row["details"], REDACTED_DETAILS)

    def test_connections_are_closed(self):
        import gc
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            gc.collect()
            for index in range(5):
                self.store.add_event({"title": f"e{index}", "details": "x"})
                self.store.recent_events(3)
            self.store.purge()
            self.store.clear()
            gc.collect()
        leaks = [
            item for item in caught
            if issubclass(item.category, ResourceWarning)
            and "sqlite" in str(item.message).lower()
        ]
        self.assertEqual(leaks, [])


class ActivityRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "activity.sqlite3"
        self.now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

    def test_retention_is_applied_on_add_event(self):
        clock = {"now": self.now - timedelta(days=40)}
        store = ActivityStore(
            self.path,
            retention_days=30,
            clock=lambda: clock["now"],
        )
        store.add_event({"title": "old", "details": "old"})

        clock["now"] = self.now
        store.add_event({"title": "new", "details": "new"})

        titles = [row["title"] for row in store.recent_events(10)]
        self.assertEqual(titles, ["new"])

    def test_retention_on_construction_purges_stale_rows(self):
        clock = {"now": self.now - timedelta(days=90)}
        store = ActivityStore(
            self.path, retention_days=30, clock=lambda: clock["now"]
        )
        for index in range(3):
            store.add_event({"title": f"old{index}"})
        self.assertEqual(store.count(), 3)

        reopened = ActivityStore(
            self.path, retention_days=30, clock=lambda: self.now
        )
        self.assertEqual(reopened.count(), 0)

    def test_purge_uses_argument_then_default(self):
        store = ActivityStore(self.path, retention_days=30)
        # Timestamps are stored to the second; 11 days is unambiguously
        # outside a 10-day window and inside the 30-day default.
        store._clock = lambda: self.now - timedelta(days=11)
        store.add_event({"title": "eleven-days-old"})
        store._clock = lambda: self.now

        with self.assertRaises(ValueError):
            store.purge(older_than_days=False)
        self.assertEqual(store.purge(older_than_days=10), 1)
        self.assertEqual(store.count(), 0)

        store._clock = lambda: self.now - timedelta(days=11)
        store.add_event({"title": "eleven-days-old"})
        store._clock = lambda: self.now
        # Default retention (30 days) keeps this row.
        self.assertEqual(store.purge(), 0)
        self.assertEqual(store.count(), 1)
        self.assertEqual(store.purge(older_than_days=5), 1)
        self.assertEqual(store.count(), 0)

    def test_purge_without_retention_deletes_nothing(self):
        store = ActivityStore(self.path)
        store.add_event({"title": "kept"})
        self.assertEqual(store.purge(), 0)
        self.assertEqual(store.count(), 1)

    def test_purge_deletes_in_bounded_chunks(self):
        from app.engine.activity_store import PURGE_CHUNK

        clock = {"now": self.now - timedelta(days=60)}
        store = ActivityStore(self.path, clock=lambda: clock["now"])
        total = PURGE_CHUNK * 2 + 3
        for index in range(total):
            store.add_event({"title": f"row{index}"})
        self.assertEqual(store.count(), total)

        clock["now"] = self.now
        self.assertEqual(store.purge(older_than_days=30), total)
        self.assertEqual(store.count(), 0)

    def test_clear_removes_every_row_and_returns_count(self):
        store = ActivityStore(self.path)
        for index in range(7):
            store.add_event({"title": f"row{index}"})
        self.assertEqual(store.clear(), 7)
        self.assertEqual(store.count(), 0)
        self.assertEqual(store.recent_events(5), [])
        self.assertEqual(store.clear(), 0)

    def test_invalid_retention_is_rejected(self):
        for value in (-1, True, "30", MAX_RETENTION_DAYS + 1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ActivityStore(self.path, retention_days=value)
        store = ActivityStore(self.path)
        with self.assertRaises(ValueError):
            store.purge(older_than_days=-5)

    def test_default_retention_constant(self):
        self.assertEqual(DEFAULT_RETENTION_DAYS, 30)


class PrivateResultClassificationTests(unittest.TestCase):
    def test_knowledge_search_counts_as_private(self):
        self.assertIn("knowledge_search", PRIVATE_RESULT_TOOLS)
        self.assertIn("read_clipboard", PRIVATE_RESULT_TOOLS)
        self.assertIn("inspect_screen", PRIVATE_RESULT_TOOLS)

    def test_taint_detection_uses_step_names(self):
        class Step:
            def __init__(self, name):
                self.name = name

        class Plan:
            def __init__(self, names):
                self.steps = [Step(name) for name in names]

        self.assertTrue(
            _answer_tainted_by_private_actions(Plan(["get_time",
                                                     "knowledge_search"]))
        )
        self.assertFalse(_answer_tainted_by_private_actions(Plan(["get_time"])))
        self.assertTrue(_answer_tainted_by_private_actions(
            Plan([]), [{"tool": "read_clipboard"}]
        ))

    def test_private_topic_words(self):
        self.assertTrue(_topic_touches_private_data(
            "Which clipboard entry should I read?"
        ))
        self.assertTrue(_topic_touches_private_data("Który ekran?"))
        self.assertFalse(_topic_touches_private_data(
            "Which folder do you mean?"
        ))
        self.assertFalse(_topic_touches_private_data(None))


class FinalAnswerPrivacyReproTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ActivityStore(Path(self.temp.name) / "activity.sqlite3")

    def _run(self, steps, final_answer):
        registry = ClipboardRegistry()
        registry.tool_results["read_clipboard"] = {
            "success": True,
            "message": "Clipboard read.",
            "text": CANARY,
        }
        agent = TestableAgent(
            registry, [plan_response(steps), {"content": final_answer}]
        )
        events = []
        agent.event_callback = lambda event: (
            events.append(event), self.store.add_event(event)
        )[0]
        answer = agent.run("Read my clipboard")
        return agent, events, answer

    def _all_stored_text(self):
        connection = sqlite3.connect(self.store.database)
        try:
            rows = connection.execute(
                "SELECT timestamp, kind, title, details, status FROM activity"
            ).fetchall()
        finally:
            connection.close()
        return rows

    def test_clipboard_canary_never_reaches_activity_history(self):
        final_answer = f"Your clipboard says: {CANARY}"
        agent, events, answer = self._run(
            [
                {
                    "id": "step-1",
                    "tool": "read_clipboard",
                    "arguments": {},
                    "depends_on": [],
                }
            ],
            final_answer,
        )

        rows = self._all_stored_text()
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn(CANARY, str(row), row)

        completed = [
            event for event in events if event["title"] == "Task completed"
        ]
        self.assertEqual(len(completed), 1)
        self.assertIs(completed[0]["sensitive"], True)
        self.assertIn(CANARY, completed[0]["details"])

        stored = [
            row for row in rows if row[2] == "Task completed"
        ]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0][3], REDACTED_DETAILS)

        # The model still receives the real tool result and the real answer.
        final_prompt = agent.chat_calls[1]["messages"][1]["content"]
        self.assertIn(CANARY, final_prompt)
        self.assertIn(CANARY, answer)

    def test_knowledge_search_plan_marks_final_answer_sensitive(self):
        final_answer = "The budget note says the quarter closes in March."
        _, events, _ = self._run(
            [
                {
                    "id": "step-1",
                    "tool": "knowledge_search",
                    "arguments": {"query": "budget"},
                    "depends_on": [],
                }
            ],
            final_answer,
        )

        completed = [
            event for event in events if event["title"] == "Task completed"
        ]
        self.assertEqual(len(completed), 1)
        self.assertIs(completed[0]["sensitive"], True)
        self.assertEqual(
            self.store.recent_events(1)[0]["details"], REDACTED_DETAILS
        )

    def test_public_only_plan_keeps_final_answer_readable(self):
        final_answer = "It is 12:00."
        _, events, _ = self._run(
            [
                {
                    "id": "step-1",
                    "tool": "get_time",
                    "arguments": {},
                    "depends_on": [],
                }
            ],
            final_answer,
        )

        completed = [
            event for event in events if event["title"] == "Task completed"
        ]
        self.assertEqual(completed[0]["details"], final_answer)
        self.assertNotIn("sensitive", completed[0])
        self.assertEqual(
            self.store.recent_events(1)[0]["details"], final_answer
        )

    def test_failed_private_plan_marks_summary_sensitive(self):
        registry = ClipboardRegistry()
        registry.tool_results["read_clipboard"] = {
            "success": False,
            "message": f"Could not read: {CANARY}",
        }
        agent = TestableAgent(
            registry,
            [
                plan_response([{
                    "id": "step-1",
                    "tool": "read_clipboard",
                    "arguments": {},
                    "depends_on": [],
                }]),
            ],
        )
        events = []
        agent.event_callback = lambda event: (
            events.append(event), self.store.add_event(event)
        )[0]

        agent.run("Read my clipboard")

        summary = [
            event for event in events
            if event["title"] == "Task not fully completed"
        ]
        self.assertEqual(len(summary), 1)
        self.assertIs(summary[0]["sensitive"], True)
        for row in self._all_stored_text():
            self.assertNotIn(CANARY, str(row), row)


if __name__ == "__main__":
    unittest.main()
