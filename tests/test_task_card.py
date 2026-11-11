"""TaskCard: a pure progress view with Cancel and Retry.

The card renders the request, one row per step (tool, status, one-line
detail), an elapsed timer and two buttons. It never executes anything: it
only emits ``cancel_requested`` and ``retry_requested(step_tool)``. These
tests drive it with synthetic agent activity events, offscreen and with a
single shared QApplication (creating a second one crashes Qt), and never
run an event loop.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.brain.agent import MistyAgent  # noqa: E402
from app.ui.task_card import TaskCard, format_elapsed  # noqa: E402


_APP = None


def _qt_app():
    """The process-wide QApplication, created once and never dropped.

    ``QApplication.instance()`` can return a bare ``QCoreApplication`` built
    by an earlier module; a widget cannot be created under that, and Qt
    aborts the process. Only a real QApplication will do.
    """
    global _APP
    if _APP is not None:
        return _APP
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        _APP = existing
    else:
        if existing is not None:
            raise unittest.SkipTest(
                "a non-GUI QCoreApplication is already running"
            )
        _APP = QApplication([])
    return _APP


def decision(tool, status="planned", title=None, details="", **extra):
    event = {
        "kind": "decision",
        "tool": tool,
        "title": title or f"Planned action: {tool}",
        "details": details or f"Description of {tool}",
        "status": status,
    }
    event.update(extra)
    return event


def running(tool, details="Executing the planned action."):
    return {
        "kind": "tool",
        "tool": tool,
        "title": f"Running tool: {tool}",
        "details": details,
        "status": "running",
    }


def completed(tool, details="Result: ok", success=True, **extra):
    event = {
        "kind": "tool",
        "tool": tool,
        "title": (
            f"Tool completed: {tool}" if success else f"Tool failed: {tool}"
        ),
        "details": details,
        "status": "success" if success else "failure",
    }
    event.update(extra)
    return event


def finish_event(title="Task completed"):
    return {"kind": "decision", "title": title, "details": "", "status": "planned"}


def statuses(card):
    return {step["tool"]: step["status"] for step in card.steps()}


class TaskCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _qt_app()

    def card(self, **kwargs):
        card = TaskCard(**kwargs)
        self.addCleanup(card.deleteLater)
        return card

    # -- lifecycle ------------------------------------------------------

    def test_hidden_until_a_task_starts(self):
        card = self.card()
        self.assertFalse(card.isVisible())
        self.assertEqual(card.status, "idle")

        card.set_task("find my invoices")

        self.assertEqual(card.status, "running")
        self.assertEqual(card.request, "find my invoices")
        self.assertEqual(card.title_label.text(), "find my invoices")
        self.assertEqual(card.elapsed_label.text(), "0:00")

    def test_long_request_title_is_truncated(self):
        card = self.card()
        card.set_task("x" * 500)
        self.assertLessEqual(len(card.title_label.text()), 91)
        self.assertTrue(card.title_label.text().endswith("…"))

    def test_reset_and_dismiss_clear_everything(self):
        card = self.card()
        card.set_task("task")
        card.apply_event(decision("get_time"))
        card.finish("completed")

        card.reset()

        self.assertEqual(card.status, "idle")
        self.assertEqual(card.steps(), [])
        self.assertFalse(card.isVisible())
        card.dismiss()
        self.assertFalse(card.isVisible())

    # -- status transitions --------------------------------------------

    def test_status_transitions_from_synthetic_events(self):
        card = self.card()
        card.set_task("do two things")

        card.apply_event(decision("get_time"))
        card.apply_event(decision("get_system_status"))
        self.assertEqual(
            statuses(card), {"get_time": "planned", "get_system_status": "planned"}
        )

        card.apply_event(running("get_time"))
        self.assertEqual(statuses(card)["get_time"], "running")

        card.apply_event(completed("get_time"))
        self.assertEqual(statuses(card)["get_time"], "success")
        self.assertIn("Result: ok", card.steps()[0]["detail"])

        card.apply_event(running("get_system_status"))
        card.apply_event(completed("get_system_status", success=False))
        self.assertEqual(statuses(card)["get_system_status"], "failed")

    def test_finished_steps_never_move_backwards(self):
        card = self.card()
        card.set_task("task")
        card.apply_event(running("get_time"))
        card.apply_event(completed("get_time"))

        # A transient retry re-emits "running" for the same step.
        card.apply_event(running("get_time", details="Retrying read-only tool"))
        card.apply_event(decision("get_time"))

        self.assertEqual(statuses(card)["get_time"], "success")
        self.assertNotIn("Retrying", card.steps()[0]["detail"])

    def test_unplanned_tool_gets_a_row(self):
        card = self.card()
        card.set_task("task")

        card.apply_event(running("list_windows"))

        self.assertEqual(statuses(card), {"list_windows": "running"})

    def test_finish_marks_open_steps(self):
        card = self.card()
        card.set_task("task")
        card.apply_event(decision("get_time"))
        card.finish("completed")
        self.assertEqual(statuses(card)["get_time"], "skipped")

        card = self.card()
        card.set_task("task")
        card.apply_event(decision("get_time"))
        card.finish("cancelled")
        self.assertEqual(statuses(card)["get_time"], "skipped")
        self.assertEqual(card.status, "cancelled")

        card = self.card()
        card.set_task("task")
        card.apply_event(decision("open_path"))
        card.finish("failed")
        self.assertEqual(statuses(card)["open_path"], "blocked")

    def test_agent_terminal_event_finishes_the_card(self):
        for title, expected in (
            ("Task completed", "completed"),
            ("Task not fully completed", "failed"),
            ("Task cancelled", "cancelled"),
            ("Task recovered after a failure", "recovered"),
        ):
            with self.subTest(title=title):
                card = self.card()
                card.set_task("task")
                card.apply_event(finish_event(title))
                self.assertEqual(card.status, expected)
                self.assertNotEqual(card.elapsed_label.text(), "")

    def test_no_execution_is_ever_requested_by_events(self):
        # A pure view: applying events never touches the runtime.
        card = self.card()
        card.set_task("task")
        card.apply_event(decision("open_browser"))
        card.apply_event(running("open_browser"))
        card.apply_event(completed("open_browser", success=False))
        card.finish("failed")
        self.assertFalse(hasattr(card, "execute"))
        self.assertEqual(statuses(card), {"open_browser": "failed"})

    # -- privacy ---------------------------------------------------------

    def test_sensitive_steps_hide_their_details(self):
        card = self.card()
        card.set_task("read my clipboard")
        card.apply_event(decision(
            "read_clipboard",
            details="Arguments: {}\nSECRET-ARGS",
            sensitive=True,
        ))
        card.apply_event(completed(
            "read_clipboard",
            details="Result: CANARY-CLIPBOARD",
            sensitive=True,
        ))

        step = card.steps()[0]
        self.assertTrue(step["sensitive"])
        self.assertNotIn("CANARY", step["detail"])
        self.assertNotIn("SECRET", step["detail"])
        self.assertTrue(card.is_private())

    def test_detail_is_one_short_line(self):
        card = self.card()
        card.set_task("task")
        card.apply_event(decision(
            "get_time", details=("first line\n" + "y" * 400)
        ))
        detail = card.steps()[0]["detail"]
        self.assertNotIn("\n", detail)
        self.assertLessEqual(len(detail), 140)

    # -- cancel ----------------------------------------------------------

    def test_cancel_emits_once_and_disables_itself(self):
        card = self.card()
        emitted = []
        card.cancel_requested.connect(lambda: emitted.append(True))
        card.set_task("task")

        card.cancel_button.click()
        card.cancel_button.click()

        self.assertEqual(emitted, [True])
        self.assertFalse(card.cancel_button.isEnabled())
        self.assertEqual(card.status, "cancelling")
        self.assertEqual(card.retry_candidate(), "")

    def test_cancel_is_inert_when_nothing_runs(self):
        card = self.card()
        emitted = []
        card.cancel_requested.connect(lambda: emitted.append(True))
        self.assertEqual(card.status, "idle")
        card.cancel_button.click()
        self.assertEqual(emitted, [])

    # -- retry -----------------------------------------------------------

    def failed_card(self, tool="get_time", kind="decision"):
        card = self.card()
        card.set_task("task")
        card.apply_event(running(tool))
        card.apply_event(completed(tool, success=False))
        card.finish("failed")
        return card

    def test_retry_enabled_for_one_retryable_failure(self):
        self.assertTrue(MistyAgent is not None)
        card = self.failed_card("get_time")
        self.assertEqual(card.retry_candidate(), "get_time")
        self.assertTrue(card.retry_button.isEnabled())

        emitted = []
        card.retry_requested.connect(emitted.append)
        card.retry_button.click()
        self.assertEqual(emitted, ["get_time"])

    def test_retry_disabled_when_policy_is_not_retryable(self):
        card = self.failed_card("open_browser")
        self.assertEqual(card.retry_candidate(), "")
        self.assertFalse(card.retry_button.isEnabled())

    def test_retry_disabled_with_two_failures(self):
        card = self.card()
        card.set_task("task")
        for tool in ("get_time", "get_system_status"):
            card.apply_event(running(tool))
            card.apply_event(completed(tool, success=False))
        card.finish("failed")

        self.assertEqual(card.retry_candidate(), "")
        self.assertFalse(card.retry_button.isEnabled())

    def test_retry_disabled_before_the_task_finishes(self):
        card = self.card()
        card.set_task("task")
        card.apply_event(running("get_time"))
        card.apply_event(completed("get_time", success=False))
        self.assertEqual(card.retry_candidate(), "")
        self.assertFalse(card.retry_button.isEnabled())

    def test_retry_disabled_for_a_sensitive_failure(self):
        card = self.card()
        card.set_task("task")
        card.apply_event(completed("read_clipboard", success=False,
                                   sensitive=True))
        card.finish("failed")
        self.assertEqual(card.retry_candidate(), "")
        self.assertFalse(card.retry_button.isEnabled())

    def test_controller_can_block_a_handler(self):
        card = self.failed_card("get_time")
        card.set_retryable_tools({"get_time"})
        self.assertEqual(card.retry_candidate(), "")
        self.assertFalse(card.retry_button.isEnabled())

    def test_a_later_success_supersedes_the_failure(self):
        card = self.card()
        card.set_task("task")
        card.apply_event(completed("get_time", success=False))
        card.apply_event(completed("list_windows"))
        card.finish("failed")
        # The failure row stays visible, but only the failed step counts.
        self.assertEqual(len(card.failed_steps()), 1)
        self.assertEqual(card.retry_candidate(), "get_time")

    def test_retry_keeps_the_same_card(self):
        card = self.card()
        card.set_task("task", job="job-1")
        card.apply_event(completed("get_time", success=False))
        card.finish("failed")

        card.set_task("task", job="job-1")

        self.assertEqual(card.status, "running")
        self.assertEqual(len(card.steps()), 1)
        self.assertEqual(card.retry_candidate(), "")

    def test_a_new_task_replaces_the_old_one(self):
        card = self.card()
        card.set_task("first", job="job-1")
        card.apply_event(completed("get_time"))
        card.finish("completed")

        card.set_task("second", job="job-2")

        self.assertEqual(card.request, "second")
        self.assertEqual(card.steps(), [])
        self.assertEqual(card.status, "running")

    def test_event_errors_never_escape(self):
        card = self.card()
        card.set_task("task")
        # Hostile / malformed events are ignored, never raised.
        card.apply_event(None)
        card.apply_event("not a dict")
        card.apply_event({"kind": "tool", "tool": object(), "status": 5})
        card.apply_event({"kind": "decision", "tool": "get_time",
                          "details": object(), "status": "planned"})
        self.assertEqual(card.status, "running")

    # -- elapsed ---------------------------------------------------------

    def test_elapsed_formatting(self):
        self.assertEqual(format_elapsed(0), "0:00")
        self.assertEqual(format_elapsed(65.9), "1:05")
        self.assertEqual(format_elapsed(-4), "0:00")

    def test_elapsed_uses_the_injected_clock(self):
        now = {"value": 100.0}
        card = self.card(clock=lambda: now["value"])
        card.set_task("task")
        now["value"] = 100.0 + 125
        card._tick()
        self.assertEqual(card.elapsed_label.text(), "2:05")


if __name__ == "__main__":
    unittest.main()
