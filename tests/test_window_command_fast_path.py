"""The reported move command must use verified tools, never a visual guess."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

from app.brain.agent import MistyAgent
from app.brain.tools import TOOL_DEFINITIONS
from app.brain.window_commands import window_move_plan, window_move_summary
from app.brain.agent_loop import LoopReport, LoopStep
from app.brain.desktop_task import guard_desktop_completion


class Registry:
    def __init__(self, result):
        self.result = result
        self.executed = []
        self.input_backend = object()
        self.vision_service = object()
    def available_tools(self):
        return list(TOOL_DEFINITIONS)
    def captured_active_window(self):
        return None
    def resolve_window_target(self, target, allow_fuzzy=False):
        return {"success":True,"window_id":"discord-id"}
    def requires_confirmation(self, *args):
        return False
    def execute(self, tool, arguments, confirmed=False):
        self.executed.append((tool, deepcopy(arguments), confirmed))
        return deepcopy(self.result)


class NoModelAgent(MistyAgent):
    def start(self):
        raise AssertionError("A simple move must not start Ministral")
    def _chat(self, *args, **kwargs):
        raise AssertionError("A simple move must not need classification, planning or final prose")


class FastWindowCommandTests(unittest.TestCase):
    def test_exact_reported_move_reaches_tool_and_verified_summary_without_model(self):
        registry = Registry({"success":True,"verified":True,"changed":True,
                             "actual_screen":"DP-1","message":"observed"})
        agent = NoModelAgent(registry=registry)
        agent._maybe_run_desktop_task = Mock(side_effect=AssertionError("visual path not allowed"))
        answer = agent.run("Move Discord to the main screen.")
        self.assertEqual(len(registry.executed), 1)
        self.assertEqual(registry.executed[0][0], "move_window_to_screen")
        self.assertEqual(registry.executed[0][1], {"window_id":"discord-id","screen_name":"main"})
        self.assertIn("DP-1", answer)
        self.assertIn("verified", answer)
        agent._maybe_run_desktop_task.assert_not_called()

    def test_noop_and_failure_do_not_claim_a_move(self):
        for result, expected in (
            ({"success":True,"verified":True,"changed":False,"already_on_screen":True,"actual_screen":"DP-3"}, "no move was needed"),
            ({"success":False,"verified":False,"message":"Requested screen not observed"}, "Window move failed"),
            ({"success":True,"message":"request accepted"}, "could not be verified"),
        ):
            with self.subTest(result=result):
                registry = Registry(result)
                answer = NoModelAgent(registry=registry).run("Move Discord to the main screen.")
                self.assertEqual(len(registry.executed), 1)
                self.assertIn(expected, answer)
                self.assertNotIn("successfully completed", answer.casefold())

    def test_parser_preserves_target_and_rejects_ambiguous_or_negated_commands(self):
        for request in ("Don't move Discord to the main screen", "Move Discord to the main screen and click General",
                        "Move report.txt to the main screen", "Move the folder to the right screen", "Move Discord to Documents",
                        "Move Discord to main screen; delete files"):
            with self.subTest(request=request):
                self.assertIsNone(window_move_plan(request))
        self.assertEqual(window_move_plan("Please move Discord to the right monitor!")["steps"][0]["arguments"],
                         {"window_id":"Discord","screen_name":"right"})

    def test_unknown_previous_output_does_not_claim_transition(self):
        text = window_move_summary({"success":True,"verified":True,"changed":None,"actual_screen":"DP-1"})
        self.assertNotIn("Moved", text)
        self.assertIn("DP-1", text)


class FalseDesktopSuccessTests(unittest.TestCase):
    def test_text_only_loop_cannot_claim_to_have_seen_the_screen(self):
        # The legacy loop feeds OCR/caption text, never pixels. A model that
        # says "the requested interface is visible" without acting must not
        # produce a completed answer.
        import app.brain.desktop_task as desktop_task
        from app.brain.agent_loop import LoopReport
        report = desktop_task.guard_desktop_completion(
            LoopReport("done", [], "The Discords images panel is visible."),
            "Naciśnij obrazy w dolfin", images_used=False)
        self.assertEqual(report.status, "unverified")
        self.assertIn("No state-changing action", report.summary())

    def test_failed_structured_actions_cannot_be_overridden_by_model_prose(self):
        from tests.agent_fakes import FakeRegistry, TestableMistyAgent, plan_response, plan_step
        registry = FakeRegistry(results=[{"success": True, "message": "12:30"},
                                         {"success": False, "message": "browser launch rejected"}])
        agent = TestableMistyAgent(registry, [
            plan_response([plan_step("time", "get_time"), plan_step("browser", "open_browser")]),
            {"content": "Every action succeeded; the browser is open."},
        ])
        answer = agent.run("Tell me the time and open the browser")
        self.assertIn("get_time: 12:30", answer)
        self.assertIn("not fully completed", answer)
        self.assertNotIn("Every action succeeded", answer)
        self.assertNotIn("browser is open", answer)

    def test_model_done_without_any_action_cannot_claim_completion(self):
        report = guard_desktop_completion(LoopReport("done", [], "Moved Discord successfully!"),
                                          "Move Discord to the main screen")
        self.assertEqual(report.status, "unverified")
        self.assertIn("No state-changing action", report.summary())
        self.assertNotIn("successfully", report.summary())

    def test_read_only_discovery_does_not_count_as_moving(self):
        step = LoopStep(1, "act", "list_windows", {}, {"success":True})
        report = guard_desktop_completion(LoopReport("done", [step], "Moved it!"), "Move Discord to main screen")
        self.assertEqual(report.status, "unverified")
        self.assertIn("No state-changing", report.summary())

    def test_delivered_click_is_not_goal_verification(self):
        step = LoopStep(1, "act", "desktop_click", {}, {"success":True})
        report = guard_desktop_completion(LoopReport("done", [step], "Purchased successfully"), "Click a channel")
        self.assertEqual(report.status, "unverified")
        self.assertIn("not independently verified", report.summary())
        self.assertNotIn("Purchased", report.summary())


if __name__ == "__main__":
    unittest.main()
