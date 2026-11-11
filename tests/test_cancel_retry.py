"""Cancel and retry of the request in flight (task card actions).

Cancel sets the per-request cancellation event the real agent already
honours between steps; retry replays exactly one failed, policy-retryable
step with its original arguments through the same agent runtime and
registry path, without a new planning round. Both are driven through a
small local fake registry/agent: no model, desktop or network is touched.
"""

import os
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.brain.agent import MistyAgent  # noqa: E402

from tests.agent_fakes import (  # noqa: E402
    TestableMistyAgent,
    TRANSIENT_FAILURE,
    plan_response,
    plan_step,
    success,
)
from tests.test_typed_request_state import build_controller  # noqa: E402


class ArgRegistry:
    """Registry double: results keyed by tool, arguments recorded per call."""

    def __init__(self, results=None):
        self.results = {name: list(items) for name, items in (results or {}).items()}
        self.executed = []
        self.arguments = []

    def available_tools(self):
        from tests.agent_fakes import EXTENDED_TOOL_DEFINITIONS

        return list(EXTENDED_TOOL_DEFINITIONS)

    @staticmethod
    def requires_confirmation(name):
        return False

    def get_handler(self, name):
        return {}

    def execute(self, name, arguments, confirmed=False):
        self.executed.append(name)
        self.arguments.append(dict(arguments))
        queue = self.results.get(name) or []
        if queue:
            return queue.pop(0)
        return {"success": True, "message": "Simulated success"}

    def resolve_window_target(self, window_id, allow_fuzzy=False):
        return None


#: The hand-built controller must outlive every test: Qt deletes a QObject
#: whose last Python reference is dropped while a signal is still bound.
_KEEP_ALIVE = []



def finish_task(controller, event_id="task-1"):
    """Mark the simulated task as finished.

    Retry is only offered once its task is over (the same rule the card
    enforces), so every retry fixture has to end the task explicitly.
    """
    controller._agent_activity_event({
        "kind": "decision",
        "title": "Task not fully completed",
        "details": "one step failed",
        "status": "failure",
    })
    return event_id
def make_controller(registry):
    controller = build_controller()
    controller._build_registry = lambda: registry
    # Qt deletes a QObject whose last Python reference disappears while a
    # signal is still bound ("Signal source has been deleted"), so every
    # controller built here is kept alive for the rest of the process.
    _KEEP_ALIVE.append(controller)
    return controller


def retrying_controller(registry):
    """A controller whose retry would succeed (built with __new__)."""
    from app.engine.voice_controller import VoiceController

    # Build ONE live controller instead of copying a throw-away one's state:
    # a copied __dict__ shares Qt signal objects owned by a different object,
    # which is exactly what "Signal source has been deleted" reports.
    controller = make_controller(registry)
    controller._build_registry = lambda: registry
    return controller



#: Cross-thread signal delivery needs a live Qt event loop, so this module
#: owns one application for the whole process (a second one crashes Qt).
from tests.agent_fakes import qt_application  # noqa: E402

# A widget-capable application: a bare QCoreApplication makes any later
# widget test abort the process.
_APP = qt_application()


def pump(ms=100):
    """Let queued (cross-thread) signal deliveries arrive.

    The controller emits task signals from its worker thread, so receivers
    connected on the test thread only see them once the event loop runs.
    Bounded and re-entrancy safe: the loop stops on the timer even if a
    worker keeps posting events.
    """
    from PySide6.QtCore import QEventLoop, QTimer

    app = QCoreApplication.instance()
    if app is None:
        return
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(ms)
    loop.exec()
    timer.stop()


def swallow(*_args, **_kwargs):
    return None


def task_state(card):
    return {step["tool"]: step["status"] for step in card.steps()}


class CancelTests(unittest.TestCase):
    def test_cancel_sets_the_agents_event(self):
        controller = make_controller(ArgRegistry())
        agent = TestableMistyAgent(ArgRegistry(), [])
        controller._agent = agent
        event = threading.Event()
        agent.cancel_event = event
        controller._task_cancel_event = event
        controller._task_event = "task-1"
        controller._task_request = "list my windows"
        controller._task_failed_id = "task-1"

        self.assertTrue(controller.cancel_active_request())

        self.assertTrue(event.is_set())
        self.assertTrue(agent._cancelled())

    def test_cancel_without_an_event_reports_false(self):
        controller = make_controller(ArgRegistry())
        self.assertFalse(controller.cancel_active_request())

    def test_cancel_before_a_step_stops_the_agent(self):
        registry = ArgRegistry()
        plan = plan_response([
            plan_step("s1", "get_time"),
            plan_step("s2", "get_system_status"),
        ])
        agent = TestableMistyAgent(registry, [plan, {"content": "x"}])
        # The agent reads ``cancel_event``; it is None unless the owner
        # assigns one, so the test must provide it before running.
        agent.cancel_event = threading.Event()
        events = []
        agent.event_callback = events.append
        registry.execute = self._recording_cancel(agent, registry.execute)

        agent.run("do two things")

        self.assertEqual(registry.executed, ["get_time"])
        titles = [event.get("title") for event in events]
        self.assertIn("Task cancelled", titles)
        self.assertNotIn("Running tool: get_system_status", titles)

    def test_cancel_during_a_step_stops_before_the_next(self):
        registry = ArgRegistry()
        agent = None

        def execute(name, arguments, confirmed=False):
            registry.executed.append(name)
            registry.arguments.append(dict(arguments))
            # The user presses Cancel while the first step runs.
            agent.cancel_event.set()
            return {"success": True, "message": "done"}

        plan = plan_response([
            plan_step("s1", "get_time"),
            plan_step("s2", "get_system_status"),
        ])
        agent = TestableMistyAgent(registry, [plan, {"content": "x"}])
        agent.cancel_event = threading.Event()
        registry.execute = execute

        agent.run("do two things")

        # The running step finished; the pending one was never started.
        self.assertEqual(registry.executed, ["get_time"])

    def test_stopped_request_is_not_started(self):
        registry = ArgRegistry()
        agent = TestableMistyAgent(registry, [])
        agent.cancel_event = threading.Event()
        agent.cancel_event.set()

        answer = agent.run("get the time")

        self.assertIn("cancelled", answer)
        self.assertEqual(registry.executed, [])

    @staticmethod
    def _recording_cancel(agent, execute):
        def wrapped(name, arguments, confirmed=False):
            result = execute(name, arguments, confirmed=confirmed)
            # Cancel arrives right after the first step reports.
            agent.cancel_event.set()
            return result

        return wrapped

    def test_controller_cancel_reaches_the_agents_event(self):
        controller = make_controller(ArgRegistry())
        agent = TestableMistyAgent(ArgRegistry(), [])
        agent.cancel_event = threading.Event()
        controller._agent = agent
        controller._task_cancel_event = agent.cancel_event
        controller._task_event = "task-1"
        controller._task_request = "list my windows"
        controller._task_failed_id = "task-1"

        self.assertTrue(controller.cancel_active_request())

        self.assertTrue(agent.cancel_event.is_set())
        # The agent itself now refuses to start planning.
        answer = agent.run("what time is it")
        self.assertIn("cancelled", answer)
        self.assertEqual(agent.chat_calls, [])


# ---------------------------------------------------------------- retry


class RetryTests(unittest.TestCase):
    def run_plan(self, registry, plan, controller=None):
        """Run one plan through the controller path, like a typed request."""
        controller = controller or make_controller(registry)
        controller._cleanup_models = swallow
        from app.engine import voice_controller as module

        agent = TestableMistyAgent(
            registry, [plan, {"content": "summary"}]
        )
        original = module.MistyAgent

        def build(**kwargs):
            # The real factory wires the controller's callbacks into the
            # agent; a bare replacement would hide every tool event from the
            # task card and from session context.
            agent.event_callback = kwargs.get("event_callback")
            agent.confirmation_callback = kwargs.get("confirmation_callback")
            return agent

        module.MistyAgent = build
        try:
            controller.submit_text("find the file")
            worker = controller._worker
            if worker is not None:
                worker.join(5)
        finally:
            module.MistyAgent = original
        return controller, agent

    def test_retry_without_failure_is_explained(self):
        controller = make_controller(ArgRegistry())
        answer = controller.retry_last_failed_step()
        self.assertIn("No failed step", answer)

    def test_retry_not_supported_for_a_non_retryable_tool(self):
        controller = make_controller(ArgRegistry())
        plan = plan_response([plan_step("s1", "open_browser")])
        controller, agent = self.run_plan(controller._build_registry(), plan,
                                          controller)
        # The plan itself ran open_browser once; what matters here is that
        # the RETRY never replays it.
        executed_before = list(agent.registry.executed)
        answer = controller.retry_last_failed_step("open_browser")
        self.assertIn("No failed step", answer)
        self.assertEqual(agent.registry.executed, executed_before)

    def test_retry_not_supported_for_a_private_tool(self):
        controller = make_controller(ArgRegistry())
        registry = controller._build_registry()
        plan = plan_response([plan_step("s1", "read_clipboard")])
        controller, agent = self.run_plan(registry, plan, controller)
        self.assertIn("No failed step", controller.retry_last_failed_step())

    def test_retry_requires_a_matching_tool_name(self):
        controller = make_controller(ArgRegistry())
        self.assertIn("No failed step", controller.retry_last_failed_step(
            "get_system_status"
        ))


class RetryMarkerTests(unittest.TestCase):
    """The failed-step marker the controller keeps for a retry."""

    def setUp(self):
        self.registry = ArgRegistry(results={
            "list_windows": [{"success": False, "message": "boom"}],
        })
        self.controller = make_controller(self.registry)
        # A finished task: the event is cleared and the failure id kept.
        self.controller._task_event = "task-1"
        self.controller._task_request = "list my windows"
        self.controller._task_failed_id = "task-1"
        self.controller._task_request = "list windows"

    def observe(self, event):
        self.controller._agent_activity_event(event)

    def test_failed_step_keeps_arguments_only_when_retryable(self):
        arguments = {"allow_fuzzy": True}
        self.observe({
            "kind": "tool", "tool": "list_windows", "status": "failure",
            "title": "Tool failed: list_windows", "arguments": arguments,
        })
        failed = self.controller._task_failed_step
        self.assertTrue(failed["retryable"])
        self.assertEqual(failed["arguments"], arguments)

    def test_private_failure_keeps_no_arguments(self):
        self.observe({
            "kind": "tool", "tool": "read_clipboard", "status": "failure",
            "title": "Tool failed: read_clipboard", "sensitive": True,
            "arguments": {"text": "CANARY"},
        })
        failed = self.controller._task_failed_step
        self.assertFalse(failed["retryable"])
        self.assertIsNone(failed["arguments"])
        self.assertIn("private", failed["reason"])

    def test_mutating_failure_is_not_retryable(self):
        self.observe({
            "kind": "tool", "tool": "open_browser", "status": "failure",
            "title": "Tool failed: open_browser", "arguments": {},
        })
        self.assertFalse(self.controller._task_failed_step["retryable"])

    def test_a_later_success_clears_the_failure(self):
        self.observe({
            "kind": "tool", "tool": "list_windows", "status": "failure",
            "title": "Tool failed: list_windows", "arguments": {},
        })
        self.observe({
            "kind": "tool", "tool": "get_time", "status": "success",
            "title": "Tool completed: get_time",
        })
        self.assertIsNone(self.controller._task_failed_step)

    def test_task_events_never_carry_payloads(self):
        forwarded = []
        self.controller.task_event.connect(forwarded.append)
        _KEEP_ALIVE.append(self.controller)
        self.observe({
            "kind": "tool", "tool": "list_windows", "status": "failure",
            "title": "Tool failed: list_windows",
            "details": "Result: CANARY",
            "result": {"message": "CANARY"},
            "arguments": {"path": "CANARY"},
        })
        self.assertEqual(len(forwarded), 1)
        serialized = repr(forwarded[0])
        self.assertNotIn("CANARY", serialized)
        self.assertEqual(forwarded[0]["status"], "failed")

    def test_retry_not_supported_while_another_request_runs(self):
        self.observe({
            "kind": "tool", "tool": "list_windows", "status": "failure",
            "title": "Tool failed: list_windows", "arguments": {},
        })
        release = threading.Event()
        worker = threading.Thread(target=lambda: release.wait(5), daemon=True)
        self.controller._worker = worker
        worker.start()
        try:
            answer = self.controller.retry_last_failed_step("list_windows")
        finally:
            release.set()
            worker.join(2)
        self.assertIn("still", answer)
        self.assertEqual(self.registry.executed, [])


class RetryExecutionTests(unittest.TestCase):
    def test_retry_replays_the_failed_step_with_the_same_arguments(self):
        registry = ArgRegistry()
        controller = retrying_controller(registry)

        # Simulate a failed task that has already FINISHED: retry is offered
        # only once the task is over, never while a step is still running.
        controller._session_begin_turn("list my windows")
        controller._task_card_started("list my windows", "task-1")
        controller._agent_activity_event({
            "kind": "tool", "tool": "list_windows", "status": "failure",
            "title": "Tool failed: list_windows",
            "arguments": {"allow_fuzzy": True},
        })
        controller._agent_activity_event({
            "kind": "decision",
            "title": "Task not fully completed",
            "details": "one step failed",
            "status": "failure",
        })

        started = controller.retry_last_failed_step("list_windows")
        self.assertEqual(started, "started")
        worker = controller._worker
        if worker is not None:
            worker.join(5)

        # Executed through the registry with the original arguments, once.
        self.assertEqual(registry.executed, ["list_windows"])
        self.assertEqual(registry.arguments, [{"allow_fuzzy": True}])

    def test_retry_goes_through_the_agent_tool_path(self):
        registry = ArgRegistry(results={"list_windows": [TRANSIENT_FAILURE]})
        controller = retrying_controller(registry)
        agent = TestableMistyAgent(registry, [])

        from app.engine import voice_controller as module

        created = []

        def factory(**_kwargs):
            created.append(1)
            return agent

        original = module.MistyAgent
        module.MistyAgent = factory
        try:
            controller._task_event = "task-1"
            controller._task_request = "list my windows"
            controller._task_failed_id = "task-1"
            controller._task_failed_step = {
                "tool": "list_windows",
                "arguments": {"allow_fuzzy": True},
                "request_id": "task-1",
                "retryable": True,
                "reason": "",
            }
            started = controller.retry_last_failed_step()
            worker = controller._worker
            if worker is not None:
                worker.join(5)
        finally:
            module.MistyAgent = original

        self.assertEqual(started, "started")
        self.assertEqual(len(created), 1)
        # The same agent retry path as a normal step: a transient read-only
        # failure is replayed once by ``_execute_with_single_retry``.
        self.assertEqual(registry.executed, ["list_windows", "list_windows"])
        self.assertEqual(registry.arguments[0], {"allow_fuzzy": True})
        # No planning round: the model was never asked anything.
        self.assertEqual(agent.chat_calls, [])

    def test_retry_refuses_a_non_retryable_step(self):
        registry = ArgRegistry()
        controller = retrying_controller(registry)
        controller._task_event = "task-1"
        controller._task_request = "list my windows"
        controller._task_failed_id = "task-1"
        controller._task_failed_step = {
            "tool": "open_browser",
            "arguments": {"url": "https://example.invalid"},
            "request_id": "task-1",
            "retryable": False,
            "reason": "open_browser can change something.",
        }
        answer = controller.retry_last_failed_step("open_browser")
        self.assertIn("can change something", answer)
        self.assertEqual(registry.executed, [])
        self.assertEqual(controller._task_event, "task-1")

    def test_retry_refuses_when_arguments_were_not_kept(self):
        registry = ArgRegistry()
        controller = retrying_controller(registry)
        controller._task_event = "task-1"
        controller._task_request = "list my windows"
        controller._task_failed_id = "task-1"
        controller._task_failed_step = {
            "tool": "list_windows",
            "arguments": None,
            "request_id": "task-1",
            "retryable": True,
            "reason": "",
        }
        answer = controller.retry_last_failed_step()
        self.assertIn("retry", answer.lower())
        self.assertEqual(registry.executed, [])

    def test_retry_emits_task_signals(self):
        registry = ArgRegistry()
        controller = retrying_controller(registry)
        started = []
        finished = []
        controller.task_started.connect(started.append)
        controller.task_finished.connect(finished.append)
        controller._task_event = "task-1"
        controller._task_request = "list my windows"
        controller._task_failed_id = "task-1"
        controller._task_failed_step = {
            "tool": "list_windows",
            "arguments": {},
            "request_id": "task-1",
            "retryable": True,
            "reason": "",
        }

        self.assertEqual(controller.retry_last_failed_step(), "started")
        worker = controller._worker
        if worker is not None:
            worker.join(5)
        pump()

        self.assertEqual(len(started), 1)
        self.assertTrue(started[0]["retry"])
        self.assertEqual(started[0]["tool"], "list_windows")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["status"], "completed")
        self.assertEqual(registry.executed, ["list_windows"])

    def test_failed_retry_can_be_retried_again(self):
        registry = ArgRegistry(results={
            "list_windows": [{"success": False, "message": "nope"}],
        })
        controller = retrying_controller(registry)
        controller._task_event = "task-1"
        controller._task_request = "list my windows"
        controller._task_failed_id = "task-1"
        controller._task_failed_step = {
            "tool": "list_windows",
            "arguments": {"allow_fuzzy": False},
            "request_id": "task-1",
            "retryable": True,
            "reason": "",
        }

        self.assertEqual(controller.retry_last_failed_step(), "started")
        worker = controller._worker
        if worker is not None:
            worker.join(5)
        pump()

        # The replay failed again and is still the one failed step.
        self.assertIsNotNone(controller._task_failed_step)
        self.assertEqual(
            controller._task_failed_step["tool"], "list_windows"
        )
        self.assertTrue(controller._task_failed_step["retryable"])


class TaskSignalTests(unittest.TestCase):
    def test_request_emits_started_and_finished(self):
        registry = ArgRegistry()
        controller = retrying_controller(registry)
        started = []
        finished = []
        events = []
        controller.task_started.connect(started.append)
        controller.task_finished.connect(finished.append)
        controller.task_event.connect(events.append)
        plan = plan_response([plan_step("s1", "get_time")])
        RetryTests().run_plan(registry, plan, controller)
        pump()

        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["status"], "running")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["status"], "completed")
        tools = [event["tool"] for event in events if event.get("tool")]
        self.assertIn("get_time", tools)


if __name__ == "__main__":
    unittest.main()
