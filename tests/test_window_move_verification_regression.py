"""Window moves need actual KWin output evidence; all desktop calls are fake."""
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.actions.executor import ActionExecutor
from app.brain.tools import ToolRegistry
from app.engine.kwin_windows import KWinWindowService


def window(screen="DP-1", window_id="discord-id"):
    return {"id": window_id, "title": "Discord", "screen": screen}


class WindowMoveVerificationTests(unittest.TestCase):
    def setUp(self):
        self.service = KWinWindowService.__new__(KWinWindowService)
        self.service._startup_error = None
        self.service._thread = SimpleNamespace(is_alive=lambda: True)
        self.service._pending = {}
        self.service._pending_lock = threading.Lock()
        self.payload = {"success": True, "title": "Discord", "requested_screen": "DP-1",
                        "previous_screen": "DP-3", "move_requested": True}
        self.service._query = Mock(return_value={"windows": [window()]})
        self.scripts = []
        self.dbus_calls = []
        def fake_qdbus(*arguments, **kwargs):
            self.dbus_calls.append(arguments)
            method = arguments[2]
            if method.endswith("loadScript"):
                self.scripts.append(Path(arguments[3]).read_text(encoding="utf-8"))
                return "1"
            if method.endswith("Script.run"):
                future = next(iter(self.service._pending.values()))
                future.set_result(json.dumps(self.payload))
                return ""
            if method.endswith("unloadScript"):
                return ""
            raise AssertionError("Unexpected fake D-Bus operation")
        self.service._qdbus = fake_qdbus

    def move(self):
        return self.service.move_window_to_screen("discord-id", "DP-1")

    def test_success_requires_actual_requested_output(self):
        result = self.move()
        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertTrue(result["changed"])
        self.assertFalse(result["already_on_screen"])
        self.assertEqual(result["screen"], "DP-1")
        self.assertEqual(result["requested_screen"], "DP-1")
        self.service._query.assert_called_once_with()
        self.assertEqual(self.service._pending, {})

    def test_request_echo_cannot_claim_move_if_window_stayed_put(self):
        self.service._query.return_value = {"windows": [window("DP-3")]}
        result = self.move()
        self.assertFalse(result["success"])
        self.assertFalse(result["verified"])
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["not_executed"])
        self.assertEqual(result["screen"], "DP-3")
        self.assertEqual(result["code"], "window_move_not_observed")
        self.assertNotIn("Moved Discord", result["message"])

    def test_fresh_query_failure_is_uncertain_not_success_and_no_move_retry(self):
        self.service._query.side_effect = RuntimeError("query unavailable")
        result = self.move()
        self.assertFalse(result["success"])
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["code"], "window_move_unverified")
        self.service._query.assert_called_once_with()
        self.assertEqual(sum(call[2].endswith("Script.run") for call in self.dbus_calls), 1)

    def test_missing_ambiguous_or_unidentified_window_never_succeeds(self):
        for windows in ([], [window(window_id="another-id")], [window(), window()],
                        [{"id": "discord-id"}], [window(screen=None)]):
            with self.subTest(windows=windows):
                self.service._query.return_value = {"windows": windows}
                result = self.move()
                self.assertFalse(result["success"])
                self.assertFalse(result["verified"])

    def test_noop_is_verified_but_never_described_as_a_move(self):
        self.payload.update(move_requested=False, previous_screen="DP-1")
        result = self.move()
        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertFalse(result["changed"])
        self.assertTrue(result["already_on_screen"])
        self.assertFalse(result["move_requested"])
        self.assertIn("already on DP-1", result["message"])
        self.assertNotIn("Moved", result["message"])

    def test_unknown_previous_output_does_not_claim_observed_change(self):
        self.payload["previous_screen"] = ""
        result = self.move()
        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertIsNone(result["changed"])
        self.assertNotIn("Moved Discord", result["message"])

    def test_inconsistent_noop_evidence_is_refused(self):
        self.payload["move_requested"] = False
        self.assertFalse(self.move()["success"])

    def test_legacy_success_payload_without_destination_evidence_is_refused(self):
        self.payload = {"success": True, "screen": "DP-1", "message": "Moved"}
        result = self.move()
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "window_move_unverified")

    def test_native_refusal_is_not_reinterpreted_as_success(self):
        self.payload = {"success": False, "message": "More than one display matches."}
        result = self.move()
        self.assertFalse(result["success"])
        self.assertTrue(result["not_executed"])
        self.service._query.assert_not_called()

    def test_generated_script_requests_and_leaves_success_claim_to_fresh_state(self):
        self.move()
        script = self.scripts[0]
        self.assertIn("workspace.sendClientToScreen(target, destination)", script)
        self.assertIn("move_requested: true", script)
        self.assertIn("move_requested: false", script)
        self.assertNotIn('message: "Moved "', script)
        self.assertLess(script.index("previousScreen ==="), script.index("!target.moveableAcrossScreens"))

    def test_executor_preserves_verified_failure_and_noop_distinction(self):
        executor = ActionExecutor(window_service=self.service, folder_resolver=object())
        self.service._query.return_value = {"windows": [window("DP-3")]}
        result = executor.move_window_to_screen("discord-id", "DP-1")
        self.assertFalse(result["success"])
        self.payload.update(move_requested=False, previous_screen="DP-1")
        self.service._query.return_value = {"windows": [window()]}
        self.assertTrue(executor.move_window_to_screen("discord-id", "DP-1")["already_on_screen"])


class MainScreenAliasRegressionTests(unittest.TestCase):
    def registry(self, screens):
        registry = ToolRegistry.__new__(ToolRegistry)
        registry.executor = SimpleNamespace(list_screens=lambda: {"success": True, "screens": screens})
        return registry

    def test_main_uses_priority_not_horizontal_position(self):
        registry = self.registry([
            {"name":"DP-1", "priority":2, "geometry":{"x":0}},
            {"name":"DP-3", "priority":1, "geometry":{"x":4640}},
        ])
        self.assertEqual(registry.resolve_screen_target("main")["screen_name"], "DP-3")

    def test_ambiguous_or_missing_main_fails_without_guessing(self):
        for priorities in ((1, 1), (2, 3)):
            with self.subTest(priorities=priorities):
                registry = self.registry([
                    {"name":"DP-1", "priority":priorities[0]},
                    {"name":"DP-3", "priority":priorities[1]},
                ])
                self.assertFalse(registry.resolve_screen_target("main")["success"])


if __name__ == "__main__":
    unittest.main()
