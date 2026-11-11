"""Exercise catalog reasoning through the real shared agent binding path."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.brain.agent import MistyAgent
from app.brain.tools import ToolRegistry, TOOL_DEFINITIONS


class ReasonedBindingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.windows = [{"id":"player-id","title":"Playlist","app_id":"org.kde.elisa","pid":123}]
        # Use the actual registry resolver, without initializing unrelated
        # desktop services. Nothing here can dispatch to the live desktop.
        self.registry = ToolRegistry.__new__(ToolRegistry)
        self.registry.executor = SimpleNamespace(list_windows=self._list_windows)
        self.reason_calls = 0
        self.registry.window_selection_callback = None
        self.agent = MistyAgent(registry=self.registry)
        self.agent._chat = Mock(return_value={"content":json.dumps({"window_id":"player-id","question":""})})
        self.events = []
        self.agent.event_callback = self.events.append
        # Observation only: preflight legitimately re-verifies a bound ID.
        original = self.agent._reason_named_window
        def counted(target):
            self.reason_calls += 1
            return original(target)
        self.agent._reason_named_window = counted

    def _list_windows(self):
        return {"success":True,"windows":deepcopy(self.windows)}

    def bind(self, target="music player"):
        payload = {"steps":[{"id":"move","tool":"move_window_to_screen",
                             "arguments":{"window_id":target,"screen_name":"main"},"depends_on":[]}]}
        error = self.agent._hydrate_window_context(payload, f"Move {target} to the main screen")
        if error:
            return None, error
        arguments = payload["steps"][0]["arguments"]
        error = self.agent._preflight_window_targets(SimpleNamespace(steps=[
            SimpleNamespace(name="move_window_to_screen", arguments=arguments)]), {"move_window_to_screen"})
        return (None, error) if error else (arguments["window_id"], None)

    def test_generic_descriptor_reaches_catalog_reasoner_and_binds_only_live_id(self):
        selected, error = self.bind()
        self.assertIsNone(error)
        self.assertEqual(selected, "player-id")
        self.agent._chat.assert_called_once()
        self.assertEqual(self.agent._chat.call_args.kwargs["tools"], [])
        self.assertNotIn("Playlist", str(self.events))

    def test_hallucinated_model_id_never_becomes_execution_target(self):
        self.agent._chat.return_value = {"content":json.dumps({"window_id":"invented-id","question":""})}
        selected, error = self.bind()
        self.assertIsNone(selected)
        self.assertIsNotNone(error)

    def test_window_closed_during_reasoning_is_not_rebound(self):
        def decide(*args, **kwargs):
            self.windows = []
            return {"content":json.dumps({"window_id":"player-id","question":""})}
        self.agent._chat.side_effect = decide
        selected, error = self.bind()
        self.assertIsNone(selected)
        self.assertIn("no longer available", error)

    def test_reasoning_call_is_not_a_planner_call(self):
        before = self.windows.copy()
        self.bind()
        self.assertEqual(self.windows, before)
        self.assertEqual(self.agent._chat.call_args.args[0][0]["role"], "system")

    def test_same_id_changed_process_is_not_rebound(self):
        def decide(*args, **kwargs):
            self.windows[0]["pid"] = 999
            return {"content":json.dumps({"window_id":"player-id","question":""})}
        self.agent._chat.side_effect = decide
        selected, error = self.bind()
        self.assertIsNone(selected)
        self.assertIn("identity changed", error)

    def test_ambiguity_uses_existing_selection_flow_without_model_guess(self):
        self.windows = [{"id":"one","title":"Home","app_id":"org.kde.dolphin"},
                        {"id":"two","title":"Downloads","app_id":"org.kde.dolphin"}]
        self.registry.window_selection_callback = Mock()
        self.registry.request_window_selection = Mock()
        selected, error = self.bind("Dolphin")
        self.assertIsNone(selected)
        self.assertIn("Several windows", error)
        self.agent._chat.assert_not_called()
        call = self.registry.request_window_selection.call_args.args
        self.assertEqual(call[0], "move_window_to_screen")
        self.assertEqual({candidate["id"] for candidate in call[1]}, {"one", "two"})
        self.assertEqual(call[2], {"screen_name":"main"})
        self.assertEqual(self.events[-1]["title"], "Clarification needed")

    def test_success_does_not_emit_a_false_not_found_question(self):
        self.windows = [{"id":"notes","title":"Notes","app_id":"org.example.Nebula"}]
        selected, error = self.bind("Nebula")
        self.assertIsNone(error)
        self.assertEqual(selected, "notes")
        self.assertFalse(any(event.get("title") == "Clarification needed" for event in self.events))
        self.agent._chat.assert_not_called()

    def test_bare_clarification_answer_is_not_mistaken_for_an_opaque_id(self):
        self.windows = [{"id":"notes","title":"Daily work","app_id":"org.example.Nebula"}]
        payload = {"steps":[{"id":"move","tool":"move_window_to_screen",
            "arguments":{"window_id":"Nebulla","screen_name":"main"},"depends_on":[]}]}
        error = self.agent._hydrate_window_context(payload, "Nebulla.")
        self.assertIsNone(error)
        self.assertEqual(payload["steps"][0]["arguments"]["window_id"], "notes")
        self.agent._chat.assert_not_called()

    def test_full_fast_run_uses_real_resolver_for_any_unique_app(self):
        self.windows = [{"id":"notes","title":"Daily work","desktop_file":"org.example.Nebula.desktop"}]
        self.registry.available_tools = lambda:list(TOOL_DEFINITIONS)
        self.registry.requires_confirmation = lambda name:False
        self.registry.execute = Mock(return_value={"success":True,"verified":True,"changed":True,"actual_screen":"DP-1"})
        self.agent.start = Mock(side_effect=AssertionError("No model needed for a unique app identity"))
        self.agent.stop = Mock()
        answer = self.agent.run("Move Nebula to the main screen.")
        self.registry.execute.assert_called_once()
        self.assertEqual(self.registry.execute.call_args.args[1]["window_id"], "notes")
        self.assertIn("verified", answer)
        self.agent.start.assert_not_called()
        self.agent._chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
