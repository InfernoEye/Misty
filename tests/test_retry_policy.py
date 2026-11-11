"""Retry policy and recovery accounting (no model, network or desktop).

Covered here:
* ``app.brain.policy.is_retryable`` is the single retry decision and rejects
  every mutating or confirmation-gated tool.
* The agent's inline retry calls a read-only tool at most once, and only for
  a clear transient failure with no side effect.
* An uncertain or partial failure is never retried, and the retried activity
  text never claims that nothing happened unless policy says read-only.
* Recovery preserves the original failure in the final model payload and in
  the deterministic summary, and the task journal reflects the final state.
"""

import unittest

from app.brain.policy import (
    EffectSpec,
    is_read_only_effect,
    is_retryable,
    is_transient_failure,
    policy_for,
)

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


MUTATING_TOOLS = (
    "browser_click",
    "browser_fill",
    "browser_press",
    "accessibility_activate",
    "copy_path",
    "move_path",
    "rename_path",
    "zip_create",
    "zip_extract",
    "copy_folder",
    "move_folder",
    "add_calendar_event",
    "set_reminder",
    "cancel_reminder",
    # Mutating but not confirmation-gated: a retry would repeat the change.
    "launch_app",
    "open_browser",
    "focus_window",
    "close_window",
    "write_clipboard",
    "remember",
    "trash_path",
    "write_text_file",
    "volume_up",
)

READ_ONLY_TOOLS = (
    "get_time",
    "get_system_status",
    "list_windows",
    "list_files",
    "search_files",
    "read_document",
)


class IsRetryablePolicyTests(unittest.TestCase):

    def test_mutating_tools_are_never_retryable(self):
        for name in MUTATING_TOOLS:
            with self.subTest(tool=name):
                self.assertFalse(is_retryable(name))

    def test_unconfirmed_mutation_is_rejected_even_without_approval(self):
        # focus_window does not require confirmation but changes state.
        spec = policy_for("focus_window")
        self.assertFalse(spec.requires_confirmation)
        self.assertTrue(spec.mutating)
        self.assertFalse(is_retryable("focus_window"))

    def test_read_only_tools_are_retryable(self):
        for name in READ_ONLY_TOOLS:
            with self.subTest(tool=name):
                self.assertTrue(is_retryable(name))

    def test_clipboard_is_not_an_interchangeable_read(self):
        # policy treats clipboard_read as its own (non-mutating) effect; the
        # retry decision is deliberately narrower than "not mutating".
        self.assertFalse(is_retryable("read_clipboard"))
        self.assertTrue(is_read_only_effect("clipboard_read"))

    def test_unknown_and_malformed_tools_fail_closed(self):
        for value in (
            "not_a_real_tool",
            "",
            None,
            42,
            b"get_time",
            ["get_time"],
            {"tool": "get_time"},
        ):
            with self.subTest(value=value):
                self.assertFalse(is_retryable(value))

    def test_non_idempotent_handler_blocks_retry(self):
        self.assertTrue(is_retryable("list_windows"))
        self.assertFalse(
            is_retryable("list_windows", {"idempotent": False})
        )
        self.assertTrue(
            is_retryable("list_windows", {"idempotent": True})
        )
        # A mapping cannot promote a mutation.
        self.assertFalse(
            is_retryable("browser_click", {"idempotent": True})
        )

    def test_helper_tracks_policy_changes_without_a_second_list(self):
        # There is exactly one policy source: the registry entry.
        for name in MUTATING_TOOLS + READ_ONLY_TOOLS:
            spec = policy_for(name)
            with self.subTest(tool=name):
                self.assertEqual(
                    is_retryable(name),
                    spec.effect == "read_only"
                    and not spec.requires_confirmation
                    and spec.risk not in {"high", "critical"},
                )

    def test_confirmation_spec_is_never_retryable(self):
        spec = EffectSpec(
            effect="read_only",
            risk="high",
            requires_confirmation=True,
            description="Read-only but gated",
        )
        self.assertTrue(spec.requires_confirmation)
        self.assertTrue(is_read_only_effect(spec.effect))


class TransientFailureTests(unittest.TestCase):

    def test_explicit_transient_failure_is_transient(self):
        self.assertTrue(is_transient_failure(TRANSIENT_FAILURE))
        self.assertTrue(is_transient_failure(
            {"success": False, "message": "x", "transient": True}
        ))
        self.assertTrue(is_transient_failure(
            {"success": False, "message": "x", "retryable": True}
        ))
        # Only a structured code names an infrastructure condition. Prose is
        # never inspected: descriptive messages routinely contain words like
        # "busy" or "connection" while describing a final outcome.
        for code in (
            "service_unavailable",
            "timeout",
            "timed_out",
            "connection_reset",
            "network_error",
            "resource_pending",
        ):
            with self.subTest(code=code):
                self.assertTrue(is_transient_failure(
                    {"success": False, "message": "x", "code": code}
                ))
                self.assertTrue(is_transient_failure(
                    {"success": False, "message": "x", "code": code.upper()}
                ))
        for prose in (
            "window service temporarily unavailable",
            "microphone busy",
            "no connection profile found",
            "timed out waiting for the user to answer",
        ):
            with self.subTest(prose=prose):
                self.assertFalse(is_transient_failure(
                    {"success": False, "message": prose}
                ))

    def test_plain_logical_failure_is_not_transient(self):
        # Audit regression: a generic failure must never be executed twice.
        for result in (
            failure(),
            {"success": False, "message": "not found"},
            {"success": False, "error": "invalid_argument"},
        ):
            with self.subTest(result=result):
                self.assertFalse(is_transient_failure(result))

    def test_explicit_veto_wins_over_marker(self):
        self.assertFalse(is_transient_failure({
            "success": False, "transient": False,
            "code": "service_unavailable",
        }))
        self.assertFalse(is_transient_failure({
            "success": False, "retryable": False,
            "code": "service_unavailable",
        }))

    def test_uncertain_and_partial_are_not_transient(self):
        cases = (
            {"success": False, "uncertain": True, "message": "x"},
            {"success": False, "partial": True, "message": "x"},
            {
                "success": False,
                "result": {"partial": True},
                "message": "x",
            },
            {
                "success": False,
                "result": {"uncertain": True},
                "message": "x",
            },
            {"success": False, "denied": True, "message": "x"},
            {"success": False, "ambiguous": True, "message": "x"},
            {"success": False, "timed_out": True, "message": "x"},
            {"success": False, "skipped": True, "message": "x"},
            {"success": False, "blocked": True, "message": "x"},
        )
        for result in cases:
            with self.subTest(result=result):
                self.assertFalse(is_transient_failure(result))

    def test_success_absent_or_malformed_is_not_retried(self):
        for result in (
            None,
            [],
            "failure",
            {},
            {"success": None},
            {"success": "false"},
            {"success": True},
        ):
            with self.subTest(result=result):
                self.assertFalse(is_transient_failure(result))


class AgentRetryTests(unittest.TestCase):

    def test_mutating_tool_in_any_list_is_not_retried(self):
        # Every one of these is mutating and NOT approval-gated, so the plan
        # executes it and the retry logic is what the test exercises. A
        # transient-looking failure does not change that: repeating a change
        # can duplicate a side effect.
        for name in (
            "focus_window",
            "launch_app",
            "open_browser",
            "volume_up",
        ):
            with self.subTest(tool=name):
                registry = FakeRegistry(results=[
                    TRANSIENT_FAILURE,
                    success("second attempt"),
                ])
                arguments = {
                    # A live-bound ID: preflight must admit the mutating call
                    # this test wants the retry policy to exercise.
                    "focus_window": {"window_id": "w1-id"},
                    "launch_app": {"app_id": "firefox"},
                }.get(name, {})
                response = plan_response([
                    plan_step("step-1", name, arguments)
                ])
                agent = TestableMistyAgent(
                    registry, [response, {"content": "Done."}]
                )

                agent.run("Do the action")

                self.assertEqual(registry.executed, [name])
                self.assertEqual(agent.activity("Retrying"), [])

    def test_approval_gated_mutating_tools_are_not_retried(self):
        # Approval-gated mutations (browser_click, copy_path, ...) are never
        # retryable either; is_retryable rejects them before any execution.
        for name in ("browser_click", "accessibility_activate", "copy_path"):
            with self.subTest(tool=name):
                self.assertFalse(is_retryable(name))

    def test_read_only_tool_transient_failure_is_retried_once(self):
        registry = FakeRegistry(results=[
            TRANSIENT_FAILURE,
            success("second attempt worked"),
        ])
        response = plan_response([plan_step("step-1", "list_windows")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Window listing succeeded."}]
        )

        answer = agent.run("List the open windows")

        self.assertEqual(registry.executed, ["list_windows", "list_windows"])
        self.assertEqual(answer, "Window listing succeeded.")
        self.assertEqual(agent.final_payload()["outcome"], "completed")

    def test_at_most_one_retry(self):
        registry = FakeRegistry(results=[
            TRANSIENT_FAILURE,
            failure("temporarily unavailable again"),
            success("third"),
        ])
        response = plan_response([plan_step("step-1", "list_windows")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Failed."}]
        )

        agent.run("List the open windows")

        self.assertEqual(registry.executed, ["list_windows", "list_windows"])

    def test_plain_failure_is_never_retried(self):
        # The audit regression: get_time / get_system_status must execute
        # exactly once when they return a plain logical failure.
        for name in ("get_time", "get_system_status"):
            with self.subTest(tool=name):
                registry = FakeRegistry(results=[
                    failure("Simulated failure"),
                    success("second attempt"),
                ])
                agent = TestableMistyAgent(
                    registry,
                    [plan_response([plan_step("step-1", name)]),
                     {"content": "Failed."}],
                )

                agent.run("Check it")

                self.assertEqual(registry.executed, [name])

    def test_uncertain_failure_is_not_retried(self):
        cases = (
            {"success": False, "uncertain": True, "message": "maybe"},
            {"success": False, "partial": True, "message": "half done"},
            {
                "success": False,
                "result": {"uncertain": True},
                "message": "maybe",
            },
        )
        for result in cases:
            with self.subTest(result=result):
                registry = FakeRegistry(results=[result, success("again")])
                response = plan_response([plan_step("step-1", "list_windows")])
                agent = TestableMistyAgent(
                    registry, [response, {"content": "Not sure."}]
                )

                agent.run("List the open windows")

                self.assertEqual(registry.executed, ["list_windows"])

    def test_registry_exception_is_not_retried(self):
        class ExplodingRegistry(FakeRegistry):
            def execute(self, name, arguments, confirmed=False):
                self.executed.append(name)
                raise RuntimeError("infrastructure exploded")

        registry = ExplodingRegistry()
        response = plan_response([plan_step("step-1", "list_windows")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Broke."}]
        )

        agent.run("List the open windows")

        self.assertEqual(registry.executed, ["list_windows"])

    def test_denied_action_is_not_retried(self):
        class ConfirmingRegistry(FakeRegistry):
            @staticmethod
            def requires_confirmation(name):
                return True

        registry = ConfirmingRegistry(results=[
            failure("should not run"),
        ])
        response = plan_response([plan_step("step-1", "close_window")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Denied."}]
        )
        agent.confirmation_callback = lambda *args: False

        agent.run("Close the window")

        self.assertEqual(registry.executed, [])
        self.assertEqual(agent.activity("Retrying"), [])

    def test_retry_text_does_not_claim_no_effects_for_mutations(self):
        registry = FakeRegistry(results=[TRANSIENT_FAILURE, success()])
        response = plan_response([plan_step("step-1", "get_time")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Done."}]
        )

        agent.run("What time is it?")

        retries = agent.activity("Retrying")
        self.assertEqual(len(retries), 1)
        details = retries[0]["details"]
        self.assertIn("policy classifies get_time as read-only", details)
        self.assertNotIn("Nothing was written", details)
        self.assertIn("Actions that change anything are never retried", details)

    def test_retry_audit_records_both_attempts(self):
        # Both attempts of one retried call are audited: the transient failure
        # and the retry's success.
        registry = FakeRegistry(results=[TRANSIENT_FAILURE, success()])
        response = plan_response([plan_step("step-1", "get_time")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Done."}]
        )
        audit = FakeAudit()
        agent.audit_log = audit

        agent.run("What time is it?")

        self.assertEqual(
            [(event["action"], event["status"]) for event in audit.events],
            [("get_time", "failure"), ("get_time", "success")],
        )


class RecoveryAccountingTests(unittest.TestCase):
    """Recovery must never erase the original failure."""

    def _recovered_agent(self, journal=None):
        registry = FakeRegistry(results=[
            # get_system_status: transient failure, one legitimate retry,
            # still failing, then recovery fetches the time successfully.
            TRANSIENT_FAILURE,
            failure("system status unavailable"),
            success("12:30"),
        ])
        first = plan_response([plan_step("step-1", "get_system_status")])
        recovery = plan_response([plan_step("step-1", "get_time")])
        agent = TestableMistyAgent(
            registry, [first, recovery, {"content": "Recovered the time."}]
        )
        if journal is not None:
            agent.task_journal = journal
        answer = agent.run("Tell me the system status and the time")
        return agent, answer

    def test_final_payload_keeps_the_original_failure(self):
        agent, _answer = self._recovered_agent()

        self.assertEqual(len(agent.chat_calls), 3)
        payload = agent.final_payload()
        self.assertEqual(payload["outcome"], "recovered")

        entries = payload["execution_results"]
        phases = [entry["phase"] for entry in entries]
        self.assertEqual(phases, ["original", "recovery"])

        original = entries[0]
        self.assertEqual(original["tool"], "get_system_status")
        self.assertEqual(original["status"], "failed")
        self.assertIs(original["result"]["success"], False)

        recovered = entries[1]
        self.assertEqual(recovered["tool"], "get_time")
        self.assertEqual(recovered["status"], "success")

        summary = payload["execution_summary"]
        self.assertIn("not fully completed", summary)
        self.assertIn("get_system_status", summary)
        self.assertIn("read-only recovery", summary)
        self.assertNotIn("All planned actions succeeded", summary)

    def test_summary_never_claims_all_actions_succeeded(self):
        _agent, answer = self._recovered_agent()

        self.assertIn("get_time: 12:30", answer)
        self.assertNotIn("Recovered the time.", answer)
        deterministic = answer.split("\n\n", 1)[1]
        self.assertIn("not fully completed", deterministic)
        self.assertIn("get_system_status", deterministic)
        self.assertIn("still reported as failed", deterministic)
        self.assertNotIn("all planned actions succeeded", answer.lower())

    def test_journal_is_failed_although_recovery_succeeded(self):
        journal = FakeJournal()
        self._recovered_agent(journal)

        self.assertEqual(len(journal.finished), 1)
        self.assertEqual(journal.finished[0]["status"], "failed")

    def test_journal_is_completed_when_original_intent_completed(self):
        registry = FakeRegistry()
        response = plan_response([plan_step("step-1", "get_time")])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Done."}]
        )
        journal = FakeJournal()
        agent.task_journal = journal

        agent.run("What time is it?")

        self.assertEqual(journal.finished[0]["status"], "completed")

    def test_failed_recovery_keeps_both_failures(self):
        registry = FakeRegistry(results=[
            # get_system_status: transient failure, one retry, fails again
            # with another transient failure (no further retry).
            TRANSIENT_FAILURE,
            failure("status still temporarily unavailable"),
            # Recovery get_time: same shape, fails again.
            TRANSIENT_FAILURE,
            failure("clock still temporarily unavailable"),
        ])
        first = plan_response([plan_step("step-1", "get_system_status")])
        recovery = plan_response([plan_step("step-1", "get_time")])
        agent = TestableMistyAgent(
            registry, [first, recovery, {"content": "Tried."}]
        )
        journal = FakeJournal()
        agent.task_journal = journal

        agent.run("Tell me the system status and the time")

        payload = agent.final_payload()
        self.assertEqual(payload["outcome"], "failed")
        self.assertEqual(
            [(entry["phase"], entry["tool"], entry["status"])
             for entry in payload["execution_results"]],
            [
                ("original", "get_system_status", "failed"),
                ("recovery", "get_time", "failed"),
            ],
        )
        self.assertEqual(
            registry.executed,
            ["get_system_status", "get_system_status", "get_time", "get_time"],
        )
        self.assertIn("original failures stand", payload["execution_summary"])
        self.assertEqual(journal.finished[0]["status"], "failed")

    def test_no_recovery_means_completed_evidence_unchanged(self):
        registry = FakeRegistry(results=[success(), success()])
        response = plan_response([
            plan_step("step-1", "get_time"),
            plan_step("step-2", "get_system_status"),
        ])
        agent = TestableMistyAgent(
            registry, [response, {"content": "Both done."}]
        )

        agent.run("Time and status")

        payload = agent.final_payload()
        self.assertEqual(payload["outcome"], "completed")
        self.assertEqual(
            [entry["phase"] for entry in payload["execution_results"]],
            ["original", "original"],
        )
        self.assertEqual(payload["execution_summary"], "All planned actions succeeded.")


if __name__ == "__main__":
    unittest.main()
