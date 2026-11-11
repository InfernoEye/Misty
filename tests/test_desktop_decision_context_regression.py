"""Decision context and grounded-frame binding without live desktop input."""
import json
import unittest
from types import SimpleNamespace

from app.brain.desktop_task import make_decider
from app.brain.agent_loop import run_loop


class DecisionContextTests(unittest.TestCase):
    def agent(self, decision):
        schema = {"type": "function", "function": {
            "name": "desktop_click", "parameters": {"type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}}}}
        calls = []
        def chat(messages, **kwargs):
            calls.append(json.loads(messages[-1]["content"]))
            return {"content": json.dumps(decision)}
        return SimpleNamespace(registry=SimpleNamespace(available_tools=lambda: [schema]),
                               _chat=chat), calls, schema

    def test_schema_and_actual_frame_are_passed_and_bound(self):
        agent, calls, schema = self.agent({"kind": "act", "tool": "desktop_click",
            "arguments": {"x": 20, "y": 30, "frame_id": "invented"}})
        observation = {"available": True, "frame": {"grounded": True, "frame_id": "actual"}}
        decision = make_decider(agent, "click target")(observation, [])
        self.assertEqual(calls[0]["tool_schemas"], [schema])
        self.assertEqual(decision["arguments"]["frame_id"], "actual")

    def test_ungrounded_observation_never_generates_coordinate_action(self):
        agent, _, _ = self.agent({"kind": "act", "tool": "desktop_click",
                                 "arguments": {"x": 20, "y": 30}})
        result = make_decider(agent, "click target")({"available": True}, [])
        self.assertEqual(result["kind"], "ask")

    def test_unavailable_tool_is_rejected(self):
        agent, _, _ = self.agent({"kind": "act", "tool": "imaginary", "arguments": {}})
        with self.assertRaisesRegex(ValueError, "unavailable"):
            make_decider(agent, "click target")({"available": True}, [])

    def test_discovery_results_reach_next_decision_but_not_public_report(self):
        histories = []
        def decide(observation, history):
            histories.append(history)
            return ({"kind": "act", "tool": "list_windows", "arguments": {}}
                    if not history else {"kind": "done", "note": "observed"})
        report = run_loop(lambda: "screen", decide,
                          lambda *_: {"success": True, "windows": [{"id": "discord-123"}]})
        self.assertEqual(histories[1][0]["result"]["windows"][0]["id"], "discord-123")
        self.assertNotIn("discord-123", json.dumps(report.to_dict()))

    def test_only_explicitly_unexecuted_failures_can_be_repaired(self):
        results = iter([
            {"success": False, "not_executed": True, "code": "invalid_arguments"},
            {"success": True},
        ])
        decisions = iter([
            {"kind": "act", "tool": "get_time", "arguments": {}},
            {"kind": "act", "tool": "get_time", "arguments": {}},
            {"kind": "done", "note": "observed"},
        ])
        counter = iter(range(10))
        report = run_loop(lambda: next(counter), lambda *_: next(decisions),
                          lambda *_: next(results))
        self.assertEqual(report.status, "done")
        self.assertEqual(len(report.actions), 2)

    def test_changing_frame_ids_are_not_visual_progress(self):
        count = [0]
        def observe():
            count[0] += 1
            return {"available": True, "description": "unchanged",
                    "frame": {"frame_id": str(count[0])}, "captured_at": str(count[0])}
        report = run_loop(observe,
            lambda *_: {"kind": "act", "tool": "get_time", "arguments": {}},
            lambda *_: {"success": True}, max_rounds=10)
        self.assertEqual(report.status, "failed")
        self.assertLess(len(report.actions), 5)


if __name__ == "__main__": 
    unittest.main()
