import unittest
from unittest.mock import Mock, patch

from app.desktop.accessibility.service import AccessibilityService


class AccessibilityActionTests(unittest.TestCase):
    def setUp(self):
        self.service = AccessibilityService(desktop_provider=lambda: None)
        self.node = object()
        self.interface = Mock()
        self.interface.do_action.return_value = True

        self.control = {
            "name": "Test button",
            "role": "button",
            "states": ["enabled", "sensitive", "visible", "showing"],
        }

        self.resolve = patch.object(
            self.service,
            "_resolve_action_target",
            return_value=(self.node, {"name": "Test app"}, None),
        )
        self.resolve.start()
        self.addCleanup(self.resolve.stop)

        self.describe = patch.object(
            self.service, "_describe", return_value=self.control
        )
        self.describe.start()
        self.addCleanup(self.describe.stop)

        self.actions = patch.object(
            self.service,
            "_available_actions",
            return_value=[
                {"index": 0, "name": "Press"},
                {"index": 1, "name": "Set Focus"},
            ],
        )
        self.actions.start()
        self.addCleanup(self.actions.stop)

        self.action_interface = patch.object(
            self.service,
            "_action_interface",
            return_value=self.interface,
        )
        self.action_interface.start()
        self.addCleanup(self.action_interface.stop)

    def perform(self, action="Press", **kwargs):
        return self.service.perform_action(
            process_id=1234,
            path="0/0/0",
            action_name=action,
            **kwargs,
        )

    def test_press_requires_confirmation(self):
        result = self.perform()
        self.assertFalse(result["success"])
        self.assertTrue(result["requires_confirmation"])
        self.interface.do_action.assert_not_called()

    def test_confirmed_press_invokes_action(self):
        result = self.perform(confirmed=True)
        self.assertTrue(result["success"])
        self.interface.do_action.assert_called_once_with(0)

    def test_focus_does_not_require_confirmation(self):
        result = self.perform("Set Focus")
        self.assertTrue(result["success"])
        self.interface.do_action.assert_called_once_with(1)

    def test_focus_without_observable_state_is_unverified(self):
        result = self.perform("Set Focus")
        self.assertTrue(result["success"])
        self.assertTrue(result["invoked"])
        self.assertEqual(result["verification"]["status"], "unverified")
        self.assertIsNone(result["verification"]["focused"])

    def test_focus_with_observed_state_is_verified(self):
        with patch.object(self.service, "_focused_state", return_value=True):
            result = self.perform("Set Focus")
        self.assertTrue(result["success"])
        self.assertEqual(result["verification"]["status"], "verified")
        self.assertTrue(result["verification"]["focused"])

    def test_press_is_unverified_until_outcome_checked(self):
        result = self.perform(confirmed=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["verification"]["status"], "unverified")

    def test_unknown_action_rejected(self):
        result = self.perform("Delete")
        self.assertFalse(result["success"])
        self.interface.do_action.assert_not_called()

    def test_disabled_control_rejected(self):
        self.control["states"] = ["sensitive", "visible"]
        result = self.perform(confirmed=True)
        self.assertFalse(result["success"])
        self.interface.do_action.assert_not_called()

    def test_insensitive_control_rejected(self):
        self.control["states"] = ["enabled", "visible"]
        result = self.perform(confirmed=True)
        self.assertFalse(result["success"])
        self.interface.do_action.assert_not_called()

    def test_invisible_control_rejected(self):
        self.control["states"] = ["enabled", "sensitive"]
        result = self.perform(confirmed=True)
        self.assertFalse(result["success"])
        self.interface.do_action.assert_not_called()

    def test_missing_action_rejected(self):
        with patch.object(
            self.service,
            "_available_actions",
            return_value=[{"index": 0, "name": "Set Focus"}],
        ):
            result = self.perform(confirmed=True)
        self.assertFalse(result["success"])
        self.interface.do_action.assert_not_called()

    def test_failed_atspi_action_reported(self):
        self.interface.do_action.return_value = False
        result = self.perform(confirmed=True)
        self.assertFalse(result["success"])

    def test_stale_target_rejected(self):
        with patch.object(
            self.service,
            "_resolve_action_target",
            return_value=(None, None, "Control disappeared."),
        ):
            result = self.perform("Set Focus")
        self.assertFalse(result["success"])
        self.interface.do_action.assert_not_called()

    def test_action_discovery_is_read_only(self):
        result = self.service.list_actions(
            process_id=1234,
            path="0/0/0",
            expected_name="Test button",
        )
        self.assertTrue(result["success"])
        self.assertEqual(len(result["actions"]), 2)
        self.interface.do_action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
