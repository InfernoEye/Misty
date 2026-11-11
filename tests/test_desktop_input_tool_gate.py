import os
import unittest
from unittest import mock

from app.brain.task_grant import TaskGrant
from app.brain.tools import ToolRegistry


class RecordingInput:
    def __init__(self):
        self.actions = []

    def execute(self, action):
        self.actions.append((action.kind, dict(action.fields)))
        return {"success": True, "message": "ok"}


class DesktopInputToolGateTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"MISTY_DESKTOP_BACKEND": "legacy"})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch(
            "app.desktop.ownership.native_task_active", return_value=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def registry(self, backend=None, grant=None):
        registry = ToolRegistry(input_backend=backend or RecordingInput())
        registry.task_grant = grant
        return registry

    def test_click_with_confirmation_reaches_the_backend(self):
        backend = RecordingInput()
        result = self.registry(backend).execute(
            "desktop_click", {"x": 40, "y": 50}, confirmed=True
        )
        self.assertTrue(result["success"])
        self.assertEqual(backend.actions[0][0], "pointer_click")
        self.assertEqual(backend.actions[0][1]["x"], 40)

    def test_click_without_confirmation_sends_nothing(self):
        backend = RecordingInput()
        result = self.registry(backend).execute(
            "desktop_click", {"x": 40, "y": 50}, confirmed=False
        )
        self.assertEqual(result["code"], "confirmation_required")
        self.assertEqual(backend.actions, [])

    def test_drag_with_confirmation_reaches_the_backend(self):
        backend = RecordingInput()
        result = self.registry(backend).execute(
            "desktop_drag",
            {"from_x": 10, "from_y": 20, "to_x": 300, "to_y": 400},
            confirmed=True,
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(backend.actions[0][0], "pointer_drag")
        self.assertEqual(
            backend.actions[0][1],
            {"start_x": 10, "start_y": 20, "end_x": 300, "end_y": 400, "screen": ""},
        )

    def test_drag_without_confirmation_sends_nothing(self):
        backend = RecordingInput()
        result = self.registry(backend).execute(
            "desktop_drag",
            {"from_x": 10, "from_y": 20, "to_x": 300, "to_y": 400},
            confirmed=False,
        )
        self.assertEqual(result["code"], "confirmation_required")
        self.assertEqual(backend.actions, [])

    def test_drag_with_a_non_integer_endpoint_sends_nothing(self):
        backend = RecordingInput()
        result = self.registry(backend).execute(
            "desktop_drag",
            {"from_x": "10", "from_y": 20, "to_x": 300, "to_y": 400},
            confirmed=True,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "invalid_arguments")
        self.assertEqual(backend.actions, [])

    def test_press_key_without_confirmation_sends_nothing(self):
        backend = RecordingInput()
        result = self.registry(backend).execute(
            "desktop_press_key", {"combo": "ctrl+v"}, confirmed=False
        )
        self.assertEqual(result["code"], "confirmation_required")
        self.assertEqual(backend.actions, [])

    def test_an_active_task_grant_stands_in_for_confirmation(self):
        backend = RecordingInput()
        grant = TaskGrant("type a greeting")
        result = self.registry(backend, grant).execute(
            "desktop_type_text", {"text": "hello"}, confirmed=False
        )
        self.assertTrue(result["success"])
        self.assertEqual(backend.actions[0][0], "type_text")

    def test_an_expired_task_grant_asks_again(self):
        backend = RecordingInput()
        now = [0.0]
        grant = TaskGrant("type a greeting", seconds=5, clock=lambda: now[0])
        now[0] = 10.0
        result = self.registry(backend, grant).execute(
            "desktop_type_text", {"text": "hello"}, confirmed=False
        )
        self.assertEqual(result["code"], "confirmation_required")
        self.assertEqual(backend.actions, [])

    def test_a_bad_click_button_is_refused_before_the_backend(self):
        backend = RecordingInput()
        result = self.registry(backend).execute(
            "desktop_click", {"x": 1, "y": 1, "button": "thumb"}, confirmed=True
        )
        self.assertFalse(result["success"])
        self.assertEqual(backend.actions, [])

    def test_a_non_legacy_backend_is_refused_before_the_backend(self):
        backend = RecordingInput()
        with mock.patch.dict(os.environ, {"MISTY_DESKTOP_BACKEND": "kwin"}):
            result = self.registry(backend).execute(
                "desktop_click", {"x": 1, "y": 1}, confirmed=True
            )
        self.assertEqual(result["code"], "desktop_backend_not_ready")
        self.assertEqual(backend.actions, [])

    def test_a_native_task_that_owns_input_blocks_the_tool(self):
        backend = RecordingInput()
        with mock.patch(
            "app.desktop.ownership.native_task_active", return_value=True
        ):
            result = self.registry(backend).execute(
                "desktop_click", {"x": 1, "y": 1}, confirmed=True
            )
        self.assertEqual(result["code"], "desktop_busy")
        self.assertEqual(backend.actions, [])

    def test_missing_backend_reports_not_configured(self):
        result = ToolRegistry(input_backend=None).execute(
            "desktop_click", {"x": 1, "y": 1}, confirmed=True
        )
        self.assertEqual(result["code"], "no_input_backend")


if __name__ == "__main__":
    unittest.main()
