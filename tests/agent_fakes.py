"""Shared fakes for agent-level tests (no model, network, desktop or browser).

Kept in one module so ``test_agent_planner``, ``test_retry_policy`` and
``test_plan_binding`` all exercise the same ``MistyAgent.run`` path with the
same deterministic doubles.
"""

import json

from app.brain.agent import MistyAgent, TOOL_DEFINITIONS
from app.brain.tools import (
    ACCESSIBILITY_TOOL_DEFINITIONS,
    BROWSER_TOOL_DEFINITIONS,
    CALENDAR_TOOL_DEFINITIONS,
    DIRECTORY_TRANSFER_TOOL_DEFINITIONS,
    FILE_TRANSFER_TOOL_DEFINITIONS,
    REMINDER_TOOL_DEFINITIONS,
    ZIP_TOOL_DEFINITIONS,
)


#: Every definition group the registry can offer, including the groups that
#: are not part of ``ToolRegistry.available_tools()`` by default.  Policy
#: coverage of a tool is decided by ``app.brain.policy``, not by whether the
#: planner was offered it, so retry tests need the full set of names.
EXTENDED_TOOL_DEFINITIONS = (
    list(TOOL_DEFINITIONS)
    + list(ACCESSIBILITY_TOOL_DEFINITIONS)
    + list(BROWSER_TOOL_DEFINITIONS)
    + list(CALENDAR_TOOL_DEFINITIONS)
    + list(DIRECTORY_TRANSFER_TOOL_DEFINITIONS)
    + list(FILE_TRANSFER_TOOL_DEFINITIONS)
    + list(REMINDER_TOOL_DEFINITIONS)
    + list(ZIP_TOOL_DEFINITIONS)
)


def plan_step(step_id, tool, arguments=None, depends_on=None, reason=""):
    step = {
        "id": step_id,
        "tool": tool,
        "arguments": arguments or {},
        "depends_on": depends_on or [],
    }
    if reason:
        step["reason"] = reason
    return step


def plan_response(steps, response="", **extra):
    payload = {"steps": steps, "response": response}
    payload.update(extra)
    return {"content": json.dumps(payload, ensure_ascii=False)}


class FakeRegistry:
    """Registry double whose result queue survives the real retry logic.

    Earlier queues in this suite matched one result per *test plan step*.  A
    legitimate single retry of a read-only observation consumes one more
    result, so the queue is now advanced per *executed call* and tests state
    the transient retry explicitly (``TRANSIENT_FAILURE``).
    """

    def __init__(self, results=None, extended_tools=False):
        self.results = list(results or [])
        self.executed = []
        self.arguments = []
        self.extended_tools = bool(extended_tools)

    def site_choices(self, site, query, limit=3):
        if not hasattr(self, "choice_queries"):
            self.choice_queries = []
        self.choice_queries.append((site, query, limit))
        return [
            {"title": "First video", "url": "https://www.youtube.com/watch?v=1"},
            {"title": "Second video", "url": "https://www.youtube.com/watch?v=2"},
            {"title": "Third video", "url": "https://www.youtube.com/watch?v=3"},
        ][:limit]

    def available_tools(self):
        if self.extended_tools:
            return list(EXTENDED_TOOL_DEFINITIONS)
        return list(TOOL_DEFINITIONS) + list(
            ACCESSIBILITY_TOOL_DEFINITIONS
        )

    @staticmethod
    def requires_confirmation(name):
        return False

    def execute(self, name, arguments, confirmed=False):
        self.executed.append(name)
        self.arguments.append(dict(arguments))
        if self.results:
            return self.results.pop(0)
        return {"success": True, "message": "Simulated success"}

    def resolve_window_target(self, window_id, allow_fuzzy=False):
        return None


#: First result for ``list_windows`` when a test wants the one allowed
#: transient read-only retry to happen.
TRANSIENT_FAILURE = {
    "success": False,
    "code": "service_unavailable",
    "message": "window service temporarily unavailable",
}


def failure(message="Simulated failure"):
    return {"success": False, "message": message}


def success(message="Simulated success", **extra):
    payload = {"success": True, "message": message}
    payload.update(extra)
    return payload


class FakeAudit:
    def __init__(self):
        self.events = []

    def log_event(self, event_type, action, status, **kwargs):
        self.events.append({
            "event_type": event_type,
            "action": action,
            "status": status,
            **kwargs,
        })
        return {"ok": True}


class FakeJournal:
    def __init__(self):
        self.started = []
        self.finished = []

    def start_task(self, task_id, **kwargs):
        self.started.append({"task_id": task_id, **kwargs})
        return {"ok": True}

    def finish_task(self, task_id, **kwargs):
        self.finished.append({"task_id": task_id, **kwargs})
        return {"ok": True}


class TestableMistyAgent(MistyAgent):
    """``MistyAgent`` with the local model replaced by scripted responses."""

    def __init__(self, registry, responses, content_selection_callback=None):
        super().__init__(
            registry=registry,
            content_selection_callback=content_selection_callback,
        )
        self.responses = list(responses)
        self.started = False
        self.stopped = False
        self.chat_calls = []
        self.activity_events = []
        # Collect activity events for assertions AND still deliver them to
        # whatever callback the owner installed (the controller wires its
        # task card through it). Replacing the callback outright would
        # silently hide every tool event from the card.
        owner_callback = self.event_callback
        if owner_callback is None:
            self.event_callback = self.activity_events.append
        else:
            def fan_out(event):
                self.activity_events.append(event)
                return owner_callback(event)

            self.event_callback = fan_out

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def _chat(self, messages, tools=None, response_format=None):
        self.chat_calls.append({
            "messages": messages,
            "tools": tools,
            "response_format": response_format,
        })
        if not self.responses:
            raise AssertionError("Unexpected additional model request")
        return self.responses.pop(0)

    # -- helpers used by the assertions --------------------------------
    def final_payload(self):
        """Decoded JSON payload of the final (report summarization) request."""
        return json.loads(self.chat_calls[-1]["messages"][1]["content"])

    def activity(self, title_prefix=""):
        events = getattr(self, "activity_events", [])
        return [
            event for event in events
            if event.get("title", "").startswith(title_prefix)
        ]


# ---------------------------------------------------------------------------
# Shared Qt application
# ---------------------------------------------------------------------------
# Only one application object may exist per process, and a widget cannot be
# created under a bare QCoreApplication (Qt aborts). Tests therefore all ask
# for the application through this one helper, which always returns a
# widget-capable QApplication.

_QT_APP = None


def qt_application():
    """The process-wide QApplication, created once and never dropped."""
    global _QT_APP
    if _QT_APP is not None:
        return _QT_APP
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    try:
        from PySide6.QtGui import QGuiApplication
    except Exception:  # pragma: no cover - QtGui is always present
        QGuiApplication = None

    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        _QT_APP = existing
    elif existing is not None and QGuiApplication is not None:
        # A QGuiApplication (no widgets) is enough for the non-widget
        # modules; a widget test will skip rather than crash the process.
        _QT_APP = existing
    else:
        _QT_APP = QApplication([])
    return _QT_APP
