"""Native target scoping: which window the pilot observes, with fakes only."""
import unittest
from unittest.mock import Mock

from app.brain.kwin_controller import KWinTaskController


class Provider:
    def __init__(self):
        self.chat = {"id": "chat", "title": "Dragon Cave", "app_id": "equibop",
                     "minimized": False, "on_current_desktop": True, "screen": "DP-3"}
        self.notes = {"id": "notes", "title": "Home", "app_id": "org.kde.dolphin",
                      "minimized": False, "on_current_desktop": True, "screen": "DP-1"}
        self.other = {"id": "other", "title": "Terminal", "app_id": "foot",
                      "minimized": False, "on_current_desktop": True, "screen": "DP-1"}

    def discover(self):
        return {"windows": [self.chat, self.notes, self.other],
                "screens": [], "overlays": [], "active_window": self.other}


class TargetScopeTests(unittest.TestCase):
    def controller(self, resolver=None):
        agent = Mock()
        agent.registry.resolve_window_target = resolver or (lambda target, allow_fuzzy=False: {
            "success": True, "window_id": "chat", "match": "fuzzy_alias"})
        agent.registry.resolve_screen_target = Mock(return_value={"success": True, "screen_name": "DP-3"})
        return KWinTaskController(agent, provider_factory=lambda registry: Provider(),
                                  environment={"MISTY_KWIN_INPUT": "0"}), agent

    def test_named_app_is_resolved_even_without_a_destination(self):
        controller, agent = self.controller(Mock(return_value={"success": True, "window_id": "chat"}))
        target = controller._target("Click Dragon Cave in Discord")
        self.assertEqual(target["id"], "chat")
        agent.registry.resolve_window_target.assert_called_once_with("Discord", allow_fuzzy=True)

    def test_app_is_extracted_from_two_clause_and_connector_forms(self):
        for goal in ("Open Discord and click Dragon Cave", "Click the Dragon Cave channel in Discord.",
                     "In Discord open the Dragon Cave channel", "Search in Firefox for tickets"):
            with self.subTest(goal=goal):
                controller, agent = self.controller(Mock(return_value={"success": True, "window_id": "chat"}))
                controller._target(goal)
                self.assertIn(agent.registry.resolve_window_target.call_args.args[0],
                              {"Discord", "Firefox"})

    def test_screen_scope_combines_with_named_app(self):
        controller, agent = self.controller(Mock(return_value={"success": True, "window_id": "chat"}))
        self.assertEqual(controller._target("Open the Dragon Cave channel in Discord on the right screen")["id"], "chat")
        agent.registry.resolve_screen_target.assert_called_once_with("right")

    def test_unknown_app_or_ambiguity_never_picks_another_window(self):
        for resolution, status in (({"success": False, "message": "No open window matches 'Gimp'."}, "target_not_found"),
                                   ({"success": False, "ambiguous": True, "candidates": []}, "target_ambiguous"),
                                   (None, "target_unavailable")):
            with self.subTest(status=status):
                controller, _ = self.controller(Mock(return_value=resolution))
                with self.assertRaises(Exception) as raised:
                    controller._target("Click Save in Gimp")
                self.assertEqual(getattr(raised.exception, "status", None), status)

    def test_without_a_name_or_destination_uses_the_window_that_was_in_front(self):
        controller, _ = self.controller()
        self.assertEqual(controller._target("Click the send button")["id"], "other")

    def test_bare_single_named_window_still_needs_the_pilot_capability(self):
        controller, agent = self.controller(Mock(return_value={"success": True, "window_id": "notes"}))
        # "Move Discord to main screen" is a window tool, not a content task;
        # the controller here only sees requests the router already sent it.
        self.assertIsNone(KWinTaskController._named_app("Move Discord to the main screen"))
        self.assertEqual(controller._target("Focus the address bar")["id"], "other")
        agent.registry.resolve_window_target.assert_not_called()


if __name__ == "__main__":
    unittest.main()
