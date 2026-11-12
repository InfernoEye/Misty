"""Regression tests for defects proven during the 2026-11-10 read-only audit.

Each test reproduces one previously confirmed failure mode and asserts the
fixed behaviour end-to-end through ``MistyAgent.run`` with deterministic
fakes (no model, network, desktop, browser or real filesystem writes).
"""

import json
import unittest

from app.brain.agent import CORE_POLICY, MistyAgent, PRIVATE_RESULT_TOOLS
from app.brain.direct_actions import resolve_direct_action
from app.brain.policy import is_retryable, is_transient_failure
from tests.agent_fakes import (
    FakeRegistry,
    failure,
    plan_response,
    plan_step,
    success,
)


class _Agent(MistyAgent):
    """Agent with the model and the server replaced by a response queue."""

    def __init__(self, registry, responses, **kwargs):
        super().__init__(registry=registry, **kwargs)
        self.responses = list(responses)
        self.chat_calls = []
        self.final_payload = None

    def start(self):
        pass

    def stop(self):
        pass

    def _chat(self, messages, tools=None, response_format=None):
        self.chat_calls.append({
            "messages": messages,
            "tools": tools,
            "response_format": response_format,
        })
        if len(self.responses) == 1:
            # The last request in every run is the final summary request.
            try:
                self.final_payload = json.loads(
                    messages[-1]["content"]
                )
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                self.final_payload = None
        if not self.responses:
            raise AssertionError("Unexpected additional model request")
        return self.responses.pop(0)


def _failure_response():
    return {
        "content": json.dumps({
            "steps": [{
                "id": "1",
                "tool": "get_system_status",
                "arguments": {},
                "depends_on": [],
            }],
            "response": "",
            "intent_confidence": 1.0,
            "clarification": "",
        })
    }


class MutationsAreNeverRetriedTests(unittest.TestCase):
    """Audit defect 2: approved browser_click executed twice."""

    def test_approved_browser_click_runs_once_after_transient_failure(self):
        registry = FakeRegistry(
            results=[
                {"success": False, "transient": True, "message": "timeout"},
                success("clicked"),
            ],
            extended_tools=True,
        )
        agent = _Agent(
            registry,
            [
                plan_response([plan_step("1", "browser_click", {
                    "selector": "#buy",
                })]),
                {"content": "Clicked."},
            ],
        )

        agent.run("Click the buy button")

        self.assertEqual(registry.executed, ["browser_click"])
        self.assertFalse(is_retryable("browser_click"))

    def test_read_only_tool_is_retried_once_on_transient_failure(self):
        registry = FakeRegistry(
            results=[
                {"success": False, "transient": True, "message": "busy"},
                success("time"),
            ]
        )
        agent = _Agent(
            registry,
            [plan_response([plan_step("1", "get_time")]), {"content": "Ok."}],
        )

        agent.run("What time is it?")

        self.assertEqual(registry.executed, ["get_time", "get_time"])
        self.assertTrue(is_retryable("get_time"))

    def test_plain_logical_failure_is_not_retried(self):
        # The audit repro: a plain failure was treated as transient and the
        # observation ran twice.
        self.assertFalse(is_transient_failure(failure("Simulated failure")))
        registry = FakeRegistry(results=[failure("Simulated failure")])
        agent = _Agent(
            registry,
            [plan_response([plan_step("1", "get_time")]), {"content": "No."}],
        )

        agent.run("What time is it?")

        self.assertEqual(registry.executed, ["get_time"])

    def test_uncertain_mutation_outcome_is_not_retried(self):
        registry = FakeRegistry(
            results=[
                {"success": False, "uncertain": True, "message": "lost"},
                success("second"),
            ],
            extended_tools=True,
        )
        agent = _Agent(
            registry,
            [
                plan_response([plan_step("1", "move_path", {
                    "folder": "Downloads",
                    "relative_path": "a.txt",
                    "destination_folder": "Documents",
                })]),
                {"content": "Uncertain."},
            ],
        )

        agent.run("Move a.txt to Documents")

        self.assertEqual(registry.executed, ["move_path"])


class RecoveryAccountingTests(unittest.TestCase):
    """Audit defect 10: a recovery success erased the original failure."""

    def test_original_failure_stays_in_final_report(self):
        registry = FakeRegistry(
            results=[failure("service rejected the request"), success("time")]
        )
        agent = _Agent(
            registry,
            [
                _failure_response(),
                plan_response([plan_step("1", "get_time")]),
                {"content": "Recovered."},
            ],
        )

        answer = agent.run("Check system status")

        summary = agent.final_payload["execution_summary"]
        self.assertIn("not fully completed", summary)
        self.assertNotIn("All planned actions succeeded", summary)
        phases = {
            row["tool"]: row["phase"]
            for row in agent.final_payload["execution_results"]
        }
        self.assertEqual(phases["get_system_status"], "original")
        self.assertEqual(phases["get_time"], "recovery")
        self.assertIn("not fully completed", answer)


class WindowTargetingTests(unittest.TestCase):
    """Audit defect 9: every window step collapsed onto one target."""

    def test_file_move_is_not_blocked_by_window_lookup(self):
        registry = FakeRegistry()
        lookups = []
        registry.resolve_window_target = lambda target: lookups.append(
            target
        ) or {"success": False}
        agent = _Agent(
            registry,
            [
                plan_response([plan_step("1", "move_path", {
                    "folder": "Home",
                    "relative_path": "report.txt",
                    "destination_folder": "Documents",
                })]),
                {"content": "Moved."},
            ],
        )

        agent.run("Move report.txt to Documents.")

        self.assertEqual(lookups, [])

    def test_two_named_windows_are_not_collapsed(self):
        lookups = []

        def resolve(target, allow_fuzzy=False):
            lookups.append(target)
            window_id = target if target.endswith("-id") else f"{target.casefold()}-id"
            return {
                "success": True, "window_id": window_id,
                "window": {"id": window_id, "title": target},
                "match": "id_exact" if target == window_id else "identity_exact",
            }

        registry = FakeRegistry()
        registry.resolve_window_target = resolve
        agent = _Agent(
            registry,
            [
                plan_response([
                    plan_step("1", "move_window_to_screen", {
                        "window_id": "firefox-id",
                        "screen_name": "left",
                    }),
                    plan_step("2", "move_window_to_screen", {
                        "window_id": "discord-id",
                        "screen_name": "right",
                    }),
                ]),
                {"content": "Moved."},
            ],
        )

        agent.run(
            "Move Firefox to the left screen and Discord to the right screen"
        )

        self.assertEqual(registry.executed, [
            "move_window_to_screen",
            "move_window_to_screen",
        ])
        self.assertEqual(
            [arguments.get("window_id") for arguments in registry.arguments],
            ["firefox-id", "discord-id"],
        )
        self.assertEqual(
            [arguments.get("screen_name") for arguments in registry.arguments],
            ["left", "right"],
        )
        self.assertEqual(lookups, ["firefox-id", "discord-id", "firefox-id", "discord-id"])


class DirectRoutingTests(unittest.TestCase):
    """Audit defect 8: routing changed or contradicted the request."""

    def test_negation_is_not_executed_as_the_positive_command(self):
        self.assertIsNone(resolve_direct_action("Don't open Google."))

    def test_compound_request_is_left_to_the_planner(self):
        self.assertIsNone(
            resolve_direct_action(
                "Open Google and list all files in Downloads."
            )
        )

    def test_topic_words_survive(self):
        action = resolve_direct_action("Find popular Minecraft videos.")
        self.assertIsNotNone(action)
        self.assertIn("minecraft", action[1]["query"].casefold())

    def test_unicode_topic_survives(self):
        action = resolve_direct_action("Find a video about 日本語.")
        self.assertIsNotNone(action)
        self.assertIn("日本語", action[1]["query"])

    def test_url_summary_is_not_reduced_to_a_bare_open(self):
        self.assertIsNone(
            resolve_direct_action("Summarize https://example.org/article.")
        )


def _planning_call(agent):
    return next(
        call for call in agent.chat_calls
        if call["messages"][0]["content"].startswith(CORE_POLICY)
    )


class ClarificationContextTests(unittest.TestCase):
    """Audit defect: clarification answers could not continue a request."""

    def test_context_reaches_planning_payload(self):
        agent = _Agent(
            FakeRegistry(),
            [{"content": json.dumps({"request": "Open Downloads"})}, plan_response([plan_step("1", "get_time")]), {"content": "Ok."}],
        )

        agent.run(
            "Downloads",
            context={
                "transcript": "Find my October invoice",
                "question": "Which folder should I search?",
            },
        )

        planning = json.loads(_planning_call(agent)["messages"][1]["content"])
        self.assertEqual(
            planning["pending_clarification"]["original_request"],
            "Find my October invoice",
        )
        self.assertEqual(
            planning["pending_clarification"]["clarification_question"],
            "Which folder should I search?",
        )

    def test_answer_fragment_is_not_hijacked_by_shortcuts(self):
        agent = _Agent(
            FakeRegistry(),
            [plan_response([plan_step("1", "get_time")]), {"content": "Ok."}],
        )

        agent.run(
            "open google",
            context={
                "transcript": "Which site should I open?",
                "question": "Which one?",
            },
        )

        # The direct-navigation shortcut must not fire on a clarification
        # answer, so the planner is still asked.
        self.assertEqual(len(agent.chat_calls), 2)

    def test_invalid_context_is_ignored(self):
        agent = _Agent(
            FakeRegistry(),
            [plan_response([plan_step("1", "get_time")]), {"content": "Ok."}],
        )

        agent.run("What time is it?", context={"transcript": "", "question": ""})

        planning = json.loads(_planning_call(agent)["messages"][1]["content"])
        self.assertNotIn("pending_clarification", planning)


class ClarificationPlumbingTests(unittest.TestCase):
    """The controller passes {"pending": {...}}; the agent must read it.

    A mismatch here silently drops the user's clarification and re-plans the
    fragment on its own, which is worse than failing loudly.
    """

    def test_pending_key_reaches_the_planner(self):
        agent = _Agent(
            FakeRegistry(),
            [{"content": json.dumps({"request": "Open Downloads"})}, plan_response([plan_step("1", "get_time")]), {"content": "Ok."}],
        )

        agent.run(
            "Downloads",
            context={
                "pending": {
                    "transcript": "Find my October invoice",
                    "question": "Which folder should I search?",
                },
                "session": {"recent_turns": []},
            },
        )

        planning = json.loads(_planning_call(agent)["messages"][1]["content"])
        self.assertEqual(
            planning["pending_clarification"]["original_request"],
            "Find my October invoice",
        )
        self.assertEqual(
            planning["pending_clarification"]["clarification_question"],
            "Which folder should I search?",
        )


class PrivacyMarkingTests(unittest.TestCase):
    """Audit defect 7: a private tool result leaked through the final answer."""

    def test_private_tool_final_answer_is_marked_sensitive(self):
        registry = FakeRegistry(
            results=[success("data", text="PRIVATE_CANARY")]
        )
        events = []
        agent = _Agent(
            registry,
            [
                plan_response([plan_step("1", "read_clipboard")]),
                {"content": "The clipboard says PRIVATE_CANARY."},
            ],
            event_callback=events.append,
        )

        agent.run("Read my clipboard")

        final_events = [
            event for event in events
            if event.get("title") == "Task completed"
        ]
        self.assertTrue(final_events)
        self.assertTrue(
            all(event.get("sensitive") is True for event in final_events)
        )
        self.assertIn("read_clipboard", PRIVATE_RESULT_TOOLS)


if __name__ == "__main__":
    unittest.main()
