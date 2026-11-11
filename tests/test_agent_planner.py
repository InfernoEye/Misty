import json
import unittest

from app.brain.agent import MistyAgent

from tests.agent_fakes import (
    TRANSIENT_FAILURE,
    FakeAudit,
    FakeJournal,
    FakeRegistry,
    TestableMistyAgent,
    failure,
    plan_response,
    plan_step,
    success,
)


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
        # get_time is read-only, so its clear transient failure earns the one
        # allowed retry; the other two steps run exactly once each.
        registry = FakeRegistry(results=[
            TRANSIENT_FAILURE,
            {"success": True, "message": "First action completed"},
            {"success": False, "message": "Simulated failure"},
            {"success": True, "message": "Independent action completed"},
        ])
        response = plan_response([
            plan_step("step-1", "get_time"),
            plan_step("step-2", "open_browser"),
            plan_step("step-3", "get_system_status"),
        ])
        final = {"content": "Some actions completed."}
        agent = TestableMistyAgent(registry, [response, final])

        answer = agent.run("Perform three independent actions")

        self.assertEqual(
            registry.executed,
            [
                "get_time",
                "get_time",
                "open_browser",
                "get_system_status",
            ],
        )
        self.assertIn("2 action(s) succeeded", answer)
        self.assertIn("1 failed", answer)
        self.assertIn("no automatic rollback", answer)
        self.assertTrue(agent.stopped)
        # One planning request plus one final summary request; the failed
        # open_browser is not a read-only action, so no recovery is offered.
        self.assertEqual(len(agent.chat_calls), 2)
        summary = json.loads(
            agent.chat_calls[1]["messages"][-1]["content"]
        )
        self.assertNotIn("recovery", summary)

    def test_failed_prerequisite_blocks_dependent_but_runs_independent(self):
        registry = FakeRegistry(results=[
            TRANSIENT_FAILURE,
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
            ["get_time", "get_time", "open_browser"],
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

    def test_screen_summary_uses_vision_without_the_model(self):
        registry = FakeRegistry(results=[{
            "success": True,
            "visual_interpretation": "The page explains local AI assistants.",
        }])
        agent = TestableMistyAgent(registry, [])

        # A screen capture needs no window target; a window capture would ask
        # which window first (covered by test_agent_window_context).
        answer = agent.run("Summarize my screen")

        self.assertEqual(registry.executed, ["inspect_screen"])
        self.assertEqual(agent.chat_calls, [])
        self.assertIn("local AI assistants", answer)

    def test_direct_url_command_skips_the_model(self):
        registry = FakeRegistry()
        agent = TestableMistyAgent(registry, [])

        answer = agent.run("Open youtube.com")

        self.assertEqual(registry.executed, ["open_url"])
        self.assertEqual(
            registry.arguments[0]["url"], "https://www.youtube.com/"
        )
        self.assertEqual(agent.chat_calls, [])
        self.assertFalse(agent.started)
        self.assertIn("Opening", answer)

    def test_youtube_search_offers_choices_before_opening(self):
        registry = FakeRegistry()
        selected = []
        agent = TestableMistyAgent(
            registry, [], content_selection_callback=selected.append
        )

        answer = agent.run("Find me on YouTube a cool Minecraft video.")

        self.assertEqual(registry.executed, [])
        self.assertEqual(
            registry.choice_queries,
            [("youtube", "minecraft video", 3)],
        )
        self.assertEqual(len(selected[0]), 3)
        self.assertIn("say 1, 2, or 3", answer)

    def test_direct_site_search_skips_the_model(self):
        registry = FakeRegistry()
        agent = TestableMistyAgent(registry, [])

        answer = agent.run("Search YouTube for lo-fi hip-hop.")

        self.assertEqual(registry.executed, ["search_site"])
        self.assertEqual(registry.arguments[0], {
            "site": "youtube",
            "query": "lo-fi hip-hop",
        })
        self.assertEqual(agent.chat_calls, [])
        self.assertIn("Searching youtube", answer)

    def test_vague_request_asks_without_starting_the_model(self):
        for transcript in ("Do that thing.", "Zrób to."):
            with self.subTest(transcript=transcript):
                registry = FakeRegistry()
                agent = TestableMistyAgent(registry, [])

                answer = agent.run(transcript)

                self.assertIn("not sure", answer.lower())
                self.assertEqual(registry.executed, [])
                self.assertEqual(agent.chat_calls, [])
                self.assertFalse(agent.started)

    def test_folder_move_is_refused_without_actions(self):
        registry = FakeRegistry()
        agent = TestableMistyAgent(registry, [])

        answer = agent.run("Move the folder Projects to Documents")

        self.assertIn("not whole folders", answer)
        self.assertEqual(registry.executed, [])
        self.assertEqual(agent.chat_calls, [])

    def test_control_request_prefers_accessibility_over_screenshot(self):
        registry = FakeRegistry()
        response = plan_response([plan_step("step-1", "get_time")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Done."}]
        )

        agent.run("What controls does Dolphin show?")

        planning = json.loads(
            agent.chat_calls[0]["messages"][1]["content"]
        )
        names = {
            item["function"]["name"]
            for item in planning["available_tools"]
        }
        self.assertIn("accessibility_inspect_application", names)
        self.assertNotIn("inspect_screen", names)

    def test_visual_appearance_keeps_screenshot(self):
        registry = FakeRegistry()
        response = plan_response([plan_step("step-1", "get_time")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Done."}]
        )

        agent.run("What does Dolphin look like?")

        planning = json.loads(
            agent.chat_calls[0]["messages"][1]["content"]
        )
        names = {
            item["function"]["name"]
            for item in planning["available_tools"]
        }
        if "inspect_screen" in {
            item["function"]["name"]
            for item in registry.available_tools()
        }:
            self.assertIn("inspect_screen", names)

    def test_file_write_request_offers_write_tool(self):
        registry = FakeRegistry()
        response = plan_response([plan_step("step-1", "get_time")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Done."}]
        )

        agent.run("Create a file called misty-node.txt")

        planning = json.loads(
            agent.chat_calls[0]["messages"][1]["content"]
        )
        names = {
            item["function"]["name"]
            for item in planning["available_tools"]
        }
        self.assertIn("write_text_file", names)
        self.assertIn("make_directory", names)
        self.assertIn("trash_path", names)

    def test_memory_and_clipboard_tools_follow_explicit_intent(self):
        ordinary = TestableMistyAgent(
            FakeRegistry(),
            [plan_response([plan_step("step-1", "get_time")]),
             {"content": "Done."}],
        )
        ordinary.run("What time is it?")
        ordinary_tools = {
            item["function"]["name"]
            for item in json.loads(
                ordinary.chat_calls[0]["messages"][1]["content"]
            )["available_tools"]
        }
        self.assertNotIn("remember", ordinary_tools)
        self.assertNotIn("read_clipboard", ordinary_tools)
        self.assertNotIn("write_clipboard", ordinary_tools)

        memory = TestableMistyAgent(
            FakeRegistry(),
            [plan_response([plan_step("step-1", "get_time")]),
             {"content": "Done."}],
        )
        memory.run("Remember that I prefer tea.")
        memory_tools = {
            item["function"]["name"]
            for item in json.loads(
                memory.chat_calls[0]["messages"][1]["content"]
            )["available_tools"]
        }
        self.assertIn("remember", memory_tools)

        clipboard = TestableMistyAgent(
            FakeRegistry(),
            [plan_response([plan_step("step-1", "get_time")]),
             {"content": "Done."}],
        )
        clipboard.run("Copy this text to my clipboard.")
        clipboard_tools = {
            item["function"]["name"]
            for item in json.loads(
                clipboard.chat_calls[0]["messages"][1]["content"]
            )["available_tools"]
        }
        self.assertIn("write_clipboard", clipboard_tools)

    def test_read_only_failure_gets_one_bounded_replan(self):
        # Two structured transient failures: the first attempt is retried
        # once, then the read-only failure earns the bounded replan.
        registry = FakeRegistry(results=[
            TRANSIENT_FAILURE,
            TRANSIENT_FAILURE,
            {"success": True, "message": "12:30"},
        ])
        first = plan_response([plan_step("step-1", "list_windows")])
        recovery = plan_response([plan_step("step-1", "get_time")])
        agent = TestableMistyAgent(
            registry, [first, recovery, {"content": "Recovered."}]
        )

        answer = agent.run("List the windows and tell me the time")

        self.assertEqual(
            registry.executed, ["list_windows", "list_windows", "get_time"]
        )
        # The recovery answer is followed by the deterministic report: the
        # original failure is never erased by a successful recovery.
        self.assertTrue(answer.startswith("Recovered."))
        self.assertIn("not fully completed", answer)
        self.assertIn("list_windows", answer)
        self.assertEqual(len(agent.chat_calls), 3)
        self.assertEqual(agent.chat_calls[1]["tools"], [])
        self.assertEqual(
            agent.chat_calls[1]["response_format"],
            {"type": "json_object"},
        )

    def test_read_only_tool_failure_is_retried_once(self):
        # Only a structured transient marker earns the retry; a free-form
        # "transient" message is treated as a final outcome.
        registry = FakeRegistry(results=[
            TRANSIENT_FAILURE,
            {"success": True, "message": "second attempt worked"},
        ])
        response = plan_response([plan_step("step-1", "list_windows")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Window listing succeeded."}]
        )

        answer = agent.run("List the open windows")

        self.assertEqual(registry.executed, ["list_windows", "list_windows"])
        self.assertEqual(answer, "Window listing succeeded.")

    def test_non_read_only_failure_is_not_retried(self):
        registry = FakeRegistry(results=[
            {"success": False, "message": "failed"},
            {"success": True, "message": "should not run"},
        ])
        response = plan_response([plan_step("step-1", "open_browser")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Browser failed."}]
        )

        agent.run("Open the browser")

        self.assertEqual(registry.executed, ["open_browser"])

    def test_low_confidence_plan_asks_without_executing(self):
        registry = FakeRegistry()
        response = plan_response(
            [plan_step("step-1", "get_time")],
            intent_confidence=0.2,
        )
        agent = TestableMistyAgent(registry, [response])

        answer = agent.run("Open the project folder")

        self.assertIn("not fully sure", answer)
        self.assertEqual(registry.executed, [])
        self.assertTrue(agent.stopped)
        self.assertEqual(len(agent.chat_calls), 1)

    def test_clarification_is_returned_without_executing(self):
        registry = FakeRegistry()
        response = plan_response(
            [plan_step("step-1", "get_time")],
            clarification="Which folder do you mean?",
            intent_confidence=0.9,
        )
        agent = TestableMistyAgent(registry, [response])

        answer = agent.run("Search that folder")

        self.assertEqual(answer, "Which folder do you mean?")
        self.assertEqual(registry.executed, [])
        self.assertTrue(agent.stopped)

    def test_step_reason_is_recorded_in_activity(self):
        registry = FakeRegistry()
        response = plan_response([
            plan_step(
                "step-1", "get_time",
                reason="The user asked for the current time.",
            ),
        ])
        agent = TestableMistyAgent(
            registry, [response, {"content": "The time was checked."}]
        )
        events = []
        agent.event_callback = events.append

        agent.run("What time is it?")

        planned = [
            event for event in events
            if event["title"] == "Planned action: get_time"
        ]
        self.assertTrue(planned)
        self.assertIn(
            "Reason: The user asked for the current time.",
            planned[0]["details"],
        )

    def test_invalid_confidence_setting_is_rejected(self):
        for value in (-0.1, 1.1, True, "high"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    MistyAgent(
                        registry=FakeRegistry(),
                        min_intent_confidence=value,
                    )

    def test_tool_outcomes_are_audited_without_content(self):
        # Two findings: a transient read-only failure that was retried once
        # gets two audit entries (failure, success), and a later read-only
        # failure is itself retried. The audit is keyed by call, not by step.
        registry = FakeRegistry(results=[
            {"success": True, "message": "First"},
            failure("Second failed"),
            failure("Second still failed"),
        ])
        response = plan_response([
            plan_step("step-1", "get_time"),
            plan_step("step-2", "get_system_status"),
        ])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Partial."}]
        )
        audit = FakeAudit()
        agent.audit_log = audit

        agent.run("Do two things")

        # A plain logical failure carries no transient marker, so it is
        # audited once and never replayed.
        self.assertEqual(len(audit.events), 2)
        self.assertEqual(audit.events[0]["action"], "get_time")
        self.assertEqual(audit.events[0]["status"], "success")
        self.assertEqual(audit.events[1]["action"], "get_system_status")
        self.assertEqual(audit.events[1]["status"], "failure")
        self.assertEqual(
            {event["request_id"] for event in audit.events},
            {audit.events[0]["request_id"]},
        )
        for event in audit.events:
            self.assertNotIn("arguments", event)
            self.assertNotIn("result", event)
            self.assertEqual(event["summary"], "")

    def test_task_journal_tracks_completed_run(self):
        registry = FakeRegistry()
        response = plan_response([plan_step("step-1", "get_time")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Done."}]
        )
        journal = FakeJournal()
        agent.task_journal = journal

        agent.run("What time is it?")

        self.assertEqual(len(journal.started), 1)
        self.assertEqual(len(journal.finished), 1)
        self.assertEqual(
            journal.started[0]["task_id"], journal.finished[0]["task_id"]
        )
        self.assertEqual(
            journal.started[0]["request_label"], "desktop request"
        )
        self.assertEqual(journal.finished[0]["status"], "completed")

    def test_task_journal_marks_failed_run(self):
        registry = FakeRegistry()
        agent = TestableMistyAgent(registry, [])
        journal = FakeJournal()
        agent.task_journal = journal

        answer = agent.run("What time is it?")

        self.assertIn("problem", answer)
        self.assertEqual(len(journal.finished), 1)
        self.assertEqual(journal.finished[0]["status"], "failed")

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
        # Tool events are redacted individually; the final answer and the
        # outcome summary are marked too because the plan touched private
        # data, so their titles no longer name the tool.
        self.assertTrue(any(
            "read_clipboard" in event["title"] for event in marked
        ))
        completed = [
            event for event in events
            if event["title"] == "Task completed"
        ]
        self.assertEqual(len(completed), 1)
        self.assertIs(completed[0]["sensitive"], True)

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
