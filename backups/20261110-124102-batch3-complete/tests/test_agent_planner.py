import json
import unittest

from app.brain.agent import MistyAgent, TOOL_DEFINITIONS


def plan_step(step_id, tool, arguments=None, depends_on=None):
    return {
        "id": step_id,
        "tool": tool,
        "arguments": arguments or {},
        "depends_on": depends_on or [],
    }


def plan_response(steps, response=""):
    return {
        "content": json.dumps(
            {"steps": steps, "response": response},
            ensure_ascii=False,
        )
    }


class FakeRegistry:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.executed = []

    def available_tools(self):
        return list(TOOL_DEFINITIONS)

    def execute(self, name, arguments, confirmed=False):
        self.executed.append(name)
        if self.results:
            return self.results.pop(0)
        return {"success": True, "message": "Simulated success"}

    def requires_confirmation(self, name):
        return False

    def resolve_window_target(self, window_id):
        return None


class TestableMistyAgent(MistyAgent):
    def __init__(self, registry, responses):
        super().__init__(registry=registry)
        self.responses = list(responses)
        self.started = False
        self.stopped = False
        self.chat_calls = []

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


class AgentPlannerIntegrationTests(unittest.TestCase):

    def test_invalid_batch_executes_nothing(self):
        registry = FakeRegistry()
        response = plan_response([
            plan_step("step-1", "get_time"),
            plan_step("step-2", "not_a_real_tool"),
        ])
        agent = TestableMistyAgent(registry, [response])

        answer = agent.run("Do two actions")

        self.assertIn("Task plan rejected", answer)
        self.assertEqual(registry.executed, [])
        self.assertTrue(agent.started)
        self.assertTrue(agent.stopped)
        self.assertEqual(len(agent.chat_calls), 1)
        self.assertEqual(agent.chat_calls[0]["tools"], [])
        self.assertEqual(
            agent.chat_calls[0]["response_format"],
            {"type": "json_object"},
        )

    def test_failure_allows_independent_actions_to_continue(self):
        registry = FakeRegistry(results=[
            {"success": True, "message": "First action completed"},
            {"success": False, "message": "Simulated failure"},
            {"success": True, "message": "Independent action completed"},
        ])
        response = plan_response([
            plan_step("step-1", "get_time"),
            plan_step("step-2", "get_system_status"),
            plan_step("step-3", "open_browser"),
        ])
        final = {"content": "Some actions completed."}
        agent = TestableMistyAgent(registry, [response, final])

        answer = agent.run("Perform three independent actions")

        self.assertEqual(
            registry.executed,
            ["get_time", "get_system_status", "open_browser"],
        )
        self.assertIn("2 action(s) succeeded", answer)
        self.assertIn("1 failed", answer)
        self.assertIn("no automatic rollback", answer)
        self.assertTrue(agent.stopped)
        self.assertEqual(len(agent.chat_calls), 1)

    def test_failed_prerequisite_blocks_dependent_but_runs_independent(self):
        registry = FakeRegistry(results=[
            {"success": False, "message": "Prerequisite failed"},
            {"success": True, "message": "Independent action completed"},
        ])
        response = plan_response([
            plan_step("step-1", "get_time"),
            plan_step("step-2", "get_system_status", depends_on=["step-1"]),
            plan_step("step-3", "open_browser"),
        ])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Partial completion."}]
        )

        answer = agent.run("Run dependent and independent actions")

        self.assertEqual(
            registry.executed,
            ["get_time", "open_browser"],
        )
        self.assertIn("1 failed", answer)
        self.assertIn("1 blocked", answer)
        self.assertIn("1 action(s) succeeded", answer)

    def test_dependency_on_later_step_is_scheduled_first(self):
        registry = FakeRegistry()
        response = plan_response([
            plan_step("step-1", "get_system_status", depends_on=["step-2"]),
            plan_step("step-2", "get_time"),
        ])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Both actions completed."}]
        )

        answer = agent.run("Check the time, then report system status")

        self.assertEqual(
            registry.executed,
            ["get_time", "get_system_status"],
        )
        self.assertEqual(answer, "Both actions completed.")
        self.assertTrue(agent.stopped)

    def test_successful_batch_executes_in_order(self):
        registry = FakeRegistry()
        response = plan_response([
            plan_step("step-1", "get_time"),
            plan_step("step-2", "get_system_status"),
            plan_step("step-3", "open_browser"),
        ])
        agent = TestableMistyAgent(
            registry, [response, {"content": "All three actions completed."}]
        )

        answer = agent.run("Perform three actions")

        self.assertEqual(
            registry.executed,
            ["get_time", "get_system_status", "open_browser"],
        )
        self.assertEqual(answer, "All three actions completed.")
        self.assertTrue(agent.stopped)

    def test_empty_plan_returns_explanation_without_final_model_call(self):
        registry = FakeRegistry()
        response = plan_response([], "That capability is not available yet.")
        agent = TestableMistyAgent(registry, [response])

        answer = agent.run("Do something unsupported")

        self.assertEqual(answer, "That capability is not available yet.")
        self.assertEqual(registry.executed, [])
        self.assertEqual(len(agent.chat_calls), 1)
        self.assertTrue(agent.stopped)

    def test_planning_and_summary_requests_include_shared_policy(self):
        registry = FakeRegistry()
        response = plan_response([plan_step("step-1", "get_time")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "The time was checked."}]
        )

        agent.run("What time is it?")

        planning_system = agent.chat_calls[0]["messages"][0]["content"]
        final_system = agent.chat_calls[1]["messages"][0]["content"]

        for system in (planning_system, final_system):
            self.assertIn("English or Polish", system)
            self.assertIn("untrusted data", system)
            self.assertIn("Never invent a tool", system)
            self.assertIn("Respond in the same language", system)

        self.assertIn("local task planner", planning_system)
        self.assertIn("execution report", final_system)
        self.assertIn('mode="window"', planning_system)
        self.assertIn("search_files.folder", planning_system)

    def test_private_tool_payloads_are_marked_sensitive(self):
        registry = FakeRegistry()
        response = plan_response([plan_step("step-1", "read_clipboard")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Clipboard read."}]
        )
        events = []
        agent.event_callback = events.append

        agent.run("Read my clipboard")

        marked = [event for event in events if event.get("sensitive") is True]
        self.assertTrue(marked)
        self.assertTrue(all(
            "read_clipboard" in event["title"] for event in marked
        ))

    def test_visual_request_without_launch_verb_hides_launchers(self):
        registry = FakeRegistry()
        response = plan_response([plan_step("step-1", "list_windows")])
        agent = TestableMistyAgent(registry, [response, {"content": "Done."}])

        agent.run("Look at Helium and tell me what's visible.")

        planning = json.loads(
            agent.chat_calls[0]["messages"][1]["content"]
        )
        names = {
            item["function"]["name"]
            for item in planning["available_tools"]
        }
        self.assertNotIn("open_browser", names)
        self.assertIn("list_windows", names)

    def test_explicit_launch_keeps_launcher_tools(self):
        registry = FakeRegistry()
        response = plan_response([plan_step("step-1", "list_windows")])
        agent = TestableMistyAgent(registry, [response, {"content": "Done."}])

        agent.run("Open Helium and tell me what's visible.")

        planning = json.loads(
            agent.chat_calls[0]["messages"][1]["content"]
        )
        names = {
            item["function"]["name"]
            for item in planning["available_tools"]
        }
        self.assertIn("open_browser", names)

    def test_final_summary_request_has_no_tools(self):
        registry = FakeRegistry()
        response = plan_response([plan_step("step-1", "get_time")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "The time was checked."}]
        )

        agent.run("What time is it?")

        self.assertEqual(len(agent.chat_calls), 2)
        self.assertEqual(agent.chat_calls[0]["tools"], [])
        self.assertEqual(agent.chat_calls[0]["response_format"], {
            "type": "json_object"
        })
        self.assertEqual(agent.chat_calls[1]["tools"], [])
        self.assertIsNone(agent.chat_calls[1]["response_format"])


if __name__ == "__main__":
    unittest.main()
