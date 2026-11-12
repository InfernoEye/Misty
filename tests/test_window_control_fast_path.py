"""Window tools the agent can use directly, without vision or a model plan."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

from app.brain.agent import MistyAgent
from app.brain.tools import TOOL_DEFINITIONS
from app.brain.window_commands import window_control_plan, window_control_summary


class Registry:
    def __init__(self, result=None):
        self.result = result or {"success": True, "verified": True}
        self.executed = []
        self.input_backend = object()
        self.vision_service = object()
    def available_tools(self):
        return list(TOOL_DEFINITIONS)
    def captured_active_window(self):
        return None
    def resolve_window_target(self, target, allow_fuzzy=False):
        # Mirror the real resolver: a bound ID resolves to itself.
        window_id = target if target.endswith("-id") else f"{target.casefold()}-id"
        return {"success": True, "window_id": window_id}
    def requires_confirmation(self, *args):
        return False
    def execute(self, tool, arguments, confirmed=False):
        self.executed.append((tool, deepcopy(arguments)))
        return deepcopy(self.result)


class NoModelAgent(MistyAgent):
    def start(self):
        raise AssertionError("A single window action must not start Ministral")
    def _chat(self, *args, **kwargs):
        raise AssertionError("A single window action must not need a model")


class WindowControlFastPathTests(unittest.TestCase):
    def run_command(self, command, result=None):
        registry = Registry(result)
        agent = NoModelAgent(registry=registry)
        agent._maybe_run_desktop_task = Mock(side_effect=AssertionError("not visual"))
        answer = agent.run(command)
        agent._maybe_run_desktop_task.assert_not_called()
        return registry, answer

    def test_each_single_window_tool_is_reachable_without_a_model(self):
        cases = {
            "Focus Dolphin": ("focus_window", "Dolphin"),
            "Maximize Firefox": ("maximize_window", "Firefox"),
            "Minimize Discord": ("minimize_window", "Discord"),
            "Restore the Firefox window": ("restore_window", "Firefox"),
            "Close Discord": ("close_window", "Discord"),
        }
        for command, (tool, target) in cases.items():
            with self.subTest(command=command):
                registry, _answer = self.run_command(command)
                # The name is bound to the live catalog ID before execution.
                self.assertEqual(registry.executed,
                                 [(tool, {"window_id": f"{target.casefold()}-id"})])

    def test_verified_action_is_reported_and_unverified_is_not_overclaimed(self):
        registry, answer = self.run_command("Focus Dolphin")
        self.assertIn("Focused", answer)
        registry, answer = self.run_command(
            "Close Discord", result={"success": True, "verified": False})
        self.assertIn("could not be verified", answer)
        registry, answer = self.run_command(
            "Maximize Discord", result={"success": False, "message": "window vanished"})
        self.assertIn("failed", answer)
        self.assertNotIn("Maximized", answer)

    def test_content_and_compound_requests_stay_off_the_fast_path(self):
        for command in ("Click Dragon Cave in Discord", "Focus Discord and click settings",
                        "Focus the send button", "Move this to the main screen",
                        "List windows", "What is Discord"):
            with self.subTest(command=command):
                self.assertIsNone(window_control_plan(command))

    def test_summary_labels_match_the_tool(self):
        self.assertEqual(window_control_summary("focus_window", {"success": True}), "Focused the window.")
        self.assertIn("could not be verified",
                      window_control_summary("close_window", {"success": True, "verified": False}))


if __name__ == "__main__":
    unittest.main()
