"""Typing is allowed only into the window that the launch created.

The resolver and the gate run real code. The window service is a stand-in
for KWin's answer (KWin cannot run inside a unit test); it reports windows and
focus, and every decision is made by the real ``ToolRegistry`` path. The
editable probe is the one stub: its real AT-SPI behaviour is covered by the
live check, and here it returns a fixed answer per test.
"""

import os
import subprocess
import unittest
from unittest import mock

from app.actions.applications import launch_application
from app.brain.tools import ToolRegistry
from app.desktop.launch_window import process_session_id, window_in_session

KATE = {"id": "win-kate", "pid": 4242, "app_id": "org.kde.kate"}
TERMINAL = {"id": "win-terminal", "pid": 9000, "app_id": "terminal"}


class RecordingInput:
    def __init__(self):
        self.actions = []

    def execute(self, action):
        self.actions.append((action.kind, dict(action.fields)))
        return {"success": True, "message": "ok"}


class FakeKWinAnswer:
    def __init__(self, windows, focused_id, accept_focus=True):
        self.windows = windows
        self.focused_id = focused_id
        self.accept_focus = accept_focus
        self.focus_requests = []

    def list_windows(self):
        return {"success": True, "message": "ok", "windows": self.windows, "overlays": []}

    def control_window(self, action, window_id):
        self.focus_requests.append((action, window_id))
        if self.accept_focus:
            self.focused_id = window_id
        return {"success": True, "message": "Focused."}

    def live_active_window(self):
        active = next((w for w in self.windows if w["id"] == self.focused_id), None)
        return {"success": True, "message": "ok", "active_window": active}


class FakeExecutor:
    def __init__(self, window_service, session_id):
        self.window_service = window_service
        self.session_id = session_id

    def launch_app(self, app_id):
        return {"success": True, "message": "Launching.", "session_id": self.session_id}


class TypingFocusGateTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"MISTY_DESKTOP_BACKEND": "legacy"})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch(
            "app.desktop.ownership.native_task_active", return_value=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def registry(self, backend, window_service, session_id):
        registry = ToolRegistry(
            input_backend=backend,
            executor=FakeExecutor(window_service, session_id),
        )
        return registry

    def test_refuses_to_type_when_no_launch_target_is_known(self):
        backend = RecordingInput()
        registry = self.registry(backend, FakeKWinAnswer([], None), session_id=None)
        result = registry.execute("desktop_type_text", {"text": "hello"}, confirmed=True)
        self.assertEqual(result["code"], "no_target_window")
        self.assertEqual(backend.actions, [])

    def gated_registry(self, windows, focused_id, editable, accept_focus=True):
        backend = RecordingInput()
        service = FakeKWinAnswer(windows, focused_id, accept_focus)
        registry = self.registry(backend, service, session_id=None)
        registry._launch_target = KATE
        patcher = mock.patch(
            "app.brain.tools.focused_control_is_editable", return_value=editable
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return registry, backend, service

    def test_focuses_the_target_then_types_when_it_takes_text(self):
        registry, backend, service = self.gated_registry(
            [KATE, TERMINAL], "win-terminal", editable=True
        )
        result = registry.execute("desktop_type_text", {"text": "hello"}, confirmed=True)
        self.assertTrue(result["success"], result)
        self.assertEqual(service.focus_requests, [("focus", "win-kate")])
        self.assertEqual(backend.actions, [("type_text", {"text": "hello"})])

    def test_refuses_when_the_focus_request_does_not_move_focus(self):
        registry, backend, _ = self.gated_registry(
            [KATE, TERMINAL], "win-terminal", editable=True, accept_focus=False
        )
        result = registry.execute("desktop_type_text", {"text": "hello"}, confirmed=True)
        self.assertEqual(result["code"], "not_focused")
        self.assertEqual(backend.actions, [])

    def test_refuses_when_the_focused_control_does_not_take_text(self):
        registry, backend, _ = self.gated_registry([KATE], "win-kate", editable=False)
        result = registry.execute("desktop_type_text", {"text": "hello"}, confirmed=True)
        self.assertEqual(result["code"], "not_editable")
        self.assertEqual(backend.actions, [])

    def test_launch_records_the_window_in_its_session(self):
        windows = [
            {"id": "win-kate", "pid": os.getpid(), "app_id": "org.kde.kate"},
            {"id": "win-other", "pid": 1, "app_id": "other"},
        ]
        session = process_session_id(os.getpid())
        registry = self.registry(RecordingInput(), FakeKWinAnswer(windows, None), session)
        result = registry.execute("launch_app", {"app_id": "org.kde.kate"})
        self.assertTrue(result["success"])
        self.assertEqual(registry._launch_target["id"], "win-kate")


class WindowInSessionTests(unittest.TestCase):
    def test_picks_the_window_owned_by_the_session(self):
        session = process_session_id(os.getpid())
        windows = [{"id": "mine", "pid": os.getpid()}, {"id": "other", "pid": 1}]
        self.assertEqual(window_in_session(windows, session)["id"], "mine")

    def test_no_window_in_session_returns_none(self):
        self.assertIsNone(window_in_session([{"id": "other", "pid": 1}], 999999999))

    def test_ambiguous_session_raises(self):
        session = process_session_id(os.getpid())
        windows = [{"id": "a", "pid": os.getpid()}, {"id": "b", "pid": os.getpid()}]
        with self.assertRaises(RuntimeError):
            window_in_session(windows, session)


if __name__ == "__main__":
    unittest.main()
