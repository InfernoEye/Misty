"""Tests for the isolated failure-recovery policy.

These tests are deliberately temp-free and side-effect free: they import only
the planner, the policy and the recovery module, and never touch the desktop,
network, configuration, databases or the real tool registry.
"""

import copy
import json
import unittest
from unittest import mock

from app.brain.planner import PlanStep, TaskPlan
from app.brain.recovery import (
    DEFAULT_RETRYABLE_TOOLS,
    RecoveryPolicy,
    summarize,
)


DECISION_KEYS = {
    "replan",
    "reason_code",
    "reason",
    "failed_tools",
    "retryable_tools",
    "blocked_tools",
    "denied",
    "remaining_steps",
    "budget",
    "requires_user",
    "verification_required",
    "message",
}

SECRET = "S3CR3T-argument-content-should-never-appear"


def make_step(
    call_id,
    tool,
    status="pending",
    *,
    result=None,
    depends_on=None,
):
    return PlanStep(
        call_id=call_id,
        name=tool,
        arguments={"private": SECRET},
        description=f"description {SECRET}",
        status=status,
        result=result,
        depends_on=list(depends_on or []),
        reason=f"reason {SECRET}",
    )


def result_entry(
    call_id,
    tool,
    status=None,
    *,
    success=None,
    blocked=False,
    skipped=False,
    denied=False,
    ambiguous=False,
    awaiting=False,
):
    payload = {"message": f"result text {SECRET}"}
    if success is not None:
        payload["success"] = success
    if blocked:
        payload["blocked"] = True
    if skipped:
        payload["skipped"] = True
    if denied:
        payload["denied"] = True
    if ambiguous:
        payload["ambiguous"] = True
    if awaiting:
        payload["awaiting_user_choice"] = True

    entry = {
        "id": call_id,
        "tool": tool,
        "arguments": {"private": SECRET},
        "reason": f"reason {SECRET}",
        "result": payload,
    }
    if status is not None:
        entry["status"] = status
    return entry


def assert_decision(testcase, decision):
    testcase.assertEqual(set(decision), DECISION_KEYS)
    testcase.assertIsInstance(decision["replan"], bool)
    testcase.assertIsInstance(decision["denied"], bool)
    testcase.assertIsInstance(decision["requires_user"], bool)
    testcase.assertIsInstance(
        decision["verification_required"], bool
    )
    testcase.assertIsInstance(decision["remaining_steps"], int)
    testcase.assertIsInstance(decision["budget"], dict)
    for key in ("failed_tools", "retryable_tools", "blocked_tools"):
        testcase.assertIsInstance(decision[key], list)
        for item in decision[key]:
            testcase.assertIsInstance(item, str)
    testcase.assertIsInstance(decision["reason_code"], str)
    testcase.assertIsInstance(decision["reason"], str)
    testcase.assertIsInstance(decision["message"], str)
    testcase.assertLessEqual(len(decision["reason"]), 240)
    testcase.assertLessEqual(len(decision["message"]), 240)
    # JSON-safe and deterministic.
    json.dumps(decision, sort_keys=True, allow_nan=False)
    json.dumps(decision, sort_keys=True, allow_nan=False)


class RecoveryPolicyTests(unittest.TestCase):

    def setUp(self):
        self.policy = RecoveryPolicy()

    # -- success and user control ---------------------------------------
    def test_success_only_does_not_replan(self):
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "get_time",
                    status="success",
                    result={"success": True},
                )
            ],
            status="completed",
        )
        results = [
            result_entry("s1", "get_time", "success", success=True)
        ]

        decision = self.policy.decide(plan, results)

        assert_decision(self, decision)
        self.assertFalse(decision["replan"])
        self.assertEqual(decision["reason_code"], "no_failure")
        self.assertEqual(decision["failed_tools"], [])
        self.assertEqual(decision["blocked_tools"], [])
        self.assertFalse(decision["requires_user"])
        self.assertFalse(decision["verification_required"])
        self.assertFalse(decision["denied"])
        self.assertEqual(decision["remaining_steps"], 0)
        self.assertEqual(decision["budget"]["executed_steps"], 1)
        self.assertEqual(decision["budget"]["consecutive_failures"], 0)

    def test_denied_never_replans_and_requires_user(self):
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "trash_path",
                    status="failed",
                    result={"success": False, "denied": True},
                )
            ],
            status="failed",
        )
        results = [
            result_entry(
                "s1", "trash_path", "failed", success=False, denied=True
            )
        ]

        decision = self.policy.decide(plan, results)

        assert_decision(self, decision)
        self.assertFalse(decision["replan"])
        self.assertEqual(decision["reason_code"], "denied")
        self.assertTrue(decision["denied"])
        self.assertTrue(decision["requires_user"])
        self.assertFalse(decision["verification_required"])
        self.assertEqual(decision["failed_tools"], ["trash_path"])

        # A top-level denial wins even when every recorded step succeeded.
        clean_plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "list_files",
                    status="success",
                    result={"success": True},
                )
            ],
            status="completed",
        )
        clean_results = [
            result_entry("s1", "list_files", "success", success=True)
        ]
        top_level = self.policy.decide(
            clean_plan, clean_results, denied=True
        )
        self.assertFalse(top_level["replan"])
        self.assertEqual(top_level["reason_code"], "denied")
        self.assertTrue(top_level["denied"])
        self.assertTrue(top_level["requires_user"])

    def test_ambiguous_window_selection_requires_user(self):
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "focus_window",
                    status="failed",
                    result={"success": False, "ambiguous": True},
                )
            ],
            status="failed",
        )
        results = [
            result_entry(
                "s1", "focus_window", "failed", success=False, ambiguous=True
            )
        ]

        decision = self.policy.decide(plan, results)

        assert_decision(self, decision)
        self.assertFalse(decision["replan"])
        self.assertEqual(
            decision["reason_code"], "awaiting_user_choice"
        )
        self.assertTrue(decision["requires_user"])
        self.assertFalse(decision["denied"])
        self.assertFalse(decision["verification_required"])

        top_level = self.policy.decide(
            plan, results, awaiting_user_choice=True
        )
        self.assertFalse(top_level["replan"])
        self.assertEqual(
            top_level["reason_code"], "awaiting_user_choice"
        )
        self.assertTrue(top_level["requires_user"])

    # -- retryable read-only failures -----------------------------------
    def test_read_only_failure_replans_once(self):
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "list_files",
                    status="failed",
                    result={"success": False},
                ),
                make_step("s2", "list_files", status="pending"),
            ],
            status="failed",
        )
        results = [
            result_entry("s1", "list_files", "failed", success=False)
        ]

        decision = self.policy.decide(plan, results)

        assert_decision(self, decision)
        self.assertTrue(decision["replan"])
        self.assertEqual(decision["reason_code"], "retryable_failure")
        self.assertEqual(decision["failed_tools"], ["list_files"])
        self.assertEqual(decision["retryable_tools"], ["list_files"])
        self.assertEqual(decision["blocked_tools"], [])
        self.assertFalse(decision["requires_user"])
        self.assertFalse(decision["verification_required"])
        self.assertEqual(decision["remaining_steps"], 1)
        self.assertEqual(decision["budget"]["executed_steps"], 1)
        self.assertEqual(decision["budget"]["remaining_steps"], 1)
        self.assertEqual(decision["budget"]["projected_total_steps"], 2)
        self.assertTrue(decision["budget"]["within_replan_budget"])
        self.assertTrue(decision["budget"]["within_step_budget"])

    def test_retryable_allowlist_is_conservative(self):
        self.assertIn("list_files", DEFAULT_RETRYABLE_TOOLS)
        self.assertIn("read_document", DEFAULT_RETRYABLE_TOOLS)
        self.assertNotIn("launch_app", DEFAULT_RETRYABLE_TOOLS)
        self.assertNotIn("trash_path", DEFAULT_RETRYABLE_TOOLS)
        self.assertNotIn("write_clipboard", DEFAULT_RETRYABLE_TOOLS)

        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "list_files",
                    status="failed",
                    result={"success": False},
                )
            ],
            status="failed",
        )
        results = [
            result_entry("s1", "list_files", "failed", success=False)
        ]

        narrowed = self.policy.decide(
            plan, results, retryable_tools=set()
        )
        self.assertFalse(narrowed["replan"])
        self.assertEqual(narrowed["reason_code"], "not_retryable")
        self.assertTrue(narrowed["verification_required"])
        self.assertTrue(narrowed["requires_user"])

        explicit = self.policy.decide(
            plan, results, retryable_tools={"list_files"}
        )
        self.assertTrue(explicit["replan"])
        self.assertEqual(explicit["reason_code"], "retryable_failure")

        # A caller cannot promote a mutating launch action into a retry.
        launch_plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "launch_app",
                    status="failed",
                    result={"success": False},
                )
            ],
            status="failed",
        )
        launch_results = [
            result_entry("s1", "launch_app", "failed", success=False)
        ]
        promoted = self.policy.decide(
            launch_plan,
            launch_results,
            retryable_tools={"launch_app"},
        )
        self.assertFalse(promoted["replan"])
        self.assertEqual(promoted["reason_code"], "not_retryable")
        self.assertTrue(promoted["verification_required"])

    # -- irreversible and sensitive effects -----------------------------
    def test_irreversible_effects_never_auto_replan(self):
        cases = (
            ("trash_path", "file_delete"),
            ("software_install", "software_install"),
            ("purchase_item", "purchase"),
            ("send_message", "send_message"),
            ("write_text_file", "confirmed local write"),
        )
        for tool, label in cases:
            with self.subTest(tool=tool, effect=label):
                plan = TaskPlan(
                    steps=[
                        make_step(
                            "s1",
                            tool,
                            status="failed",
                            result={"success": False},
                        )
                    ],
                    status="failed",
                )
                results = [
                    result_entry("s1", tool, "failed", success=False)
                ]

                decision = self.policy.decide(plan, results)

                assert_decision(self, decision)
                self.assertFalse(decision["replan"])
                self.assertEqual(
                    decision["reason_code"], "irreversible_failure"
                )
                self.assertTrue(decision["verification_required"])
                self.assertTrue(decision["requires_user"])
                self.assertEqual(decision["failed_tools"], [tool])

    def test_caller_irreversible_set_blocks_read_only_retry(self):
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "list_files",
                    status="failed",
                    result={"success": False},
                )
            ],
            status="failed",
        )
        results = [
            result_entry("s1", "list_files", "failed", success=False)
        ]

        decision = self.policy.decide(
            plan, results, irreversible_tools={"list_files"}
        )

        self.assertFalse(decision["replan"])
        self.assertEqual(
            decision["reason_code"], "irreversible_failure"
        )
        self.assertTrue(decision["verification_required"])
        self.assertTrue(decision["requires_user"])

    # -- budgets ---------------------------------------------------------
    def test_replan_budget_is_enforced(self):
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "list_files",
                    status="failed",
                    result={"success": False},
                )
            ],
            status="failed",
        )
        results = [
            result_entry("s1", "list_files", "failed", success=False)
        ]

        decision = self.policy.decide(
            plan, results, replans_used=1
        )

        assert_decision(self, decision)
        self.assertFalse(decision["replan"])
        self.assertEqual(decision["reason_code"], "replan_budget")
        self.assertEqual(decision["budget"]["replans_used"], 1)
        self.assertEqual(decision["budget"]["replans_remaining"], 0)
        self.assertFalse(decision["budget"]["within_replan_budget"])
        self.assertFalse(decision["verification_required"])
        self.assertFalse(decision["requires_user"])

    def test_step_budget_is_enforced(self):
        policy = RecoveryPolicy(max_total_steps=2)
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "get_time",
                    status="success",
                    result={"success": True},
                ),
                make_step(
                    "s2",
                    "list_files",
                    status="failed",
                    result={"success": False},
                ),
            ],
            status="failed",
        )
        results = [
            result_entry("s1", "get_time", "success", success=True),
            result_entry("s2", "list_files", "failed", success=False),
        ]

        decision = policy.decide(plan, results)

        assert_decision(self, decision)
        self.assertFalse(decision["replan"])
        self.assertEqual(decision["reason_code"], "step_budget")
        self.assertEqual(decision["budget"]["executed_steps"], 2)
        self.assertEqual(decision["budget"]["remaining_steps"], 0)
        self.assertEqual(decision["budget"]["projected_total_steps"], 2)
        self.assertFalse(decision["budget"]["within_step_budget"])

    def test_consecutive_failure_limit(self):
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "list_files",
                    status="failed",
                    result={"success": False},
                ),
                make_step(
                    "s2",
                    "get_time",
                    status="failed",
                    result={"success": False},
                ),
                make_step(
                    "s3",
                    "search_files",
                    status="failed",
                    result={"success": False},
                ),
            ],
            status="failed",
        )
        results = [
            result_entry("s1", "list_files", "failed", success=False),
            result_entry("s2", "get_time", "failed", success=False),
            result_entry("s3", "search_files", "failed", success=False),
        ]

        decision = self.policy.decide(plan, results)
        self.assertFalse(decision["replan"])
        self.assertEqual(decision["reason_code"], "failure_limit")
        self.assertEqual(decision["budget"]["consecutive_failures"], 3)

        patient = RecoveryPolicy(max_consecutive_failures=10)
        self.assertTrue(patient.decide(plan, results)["replan"])

    # -- repetition ------------------------------------------------------
    def test_repeated_failure_via_attempted_tool_counts(self):
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "list_files",
                    status="failed",
                    result={"success": False},
                )
            ],
            status="failed",
        )
        results = [
            result_entry("s1", "list_files", "failed", success=False)
        ]

        for attempted in (
            {"list_files": 2},
            ["list_files", "list_files"],
            ("list_files", "list_files"),
        ):
            with self.subTest(attempted=attempted):
                decision = self.policy.decide(
                    plan, results, attempted_tools=attempted
                )
                assert_decision(self, decision)
                self.assertFalse(decision["replan"])
                self.assertEqual(
                    decision["reason_code"], "repeated_failure"
                )
                self.assertEqual(decision["failed_tools"], ["list_files"])
                self.assertFalse(decision["verification_required"])

    def test_unrelated_attempt_counts_do_not_block_first_retry(self):
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "list_files",
                    status="failed",
                    result={"success": False},
                )
            ],
            status="failed",
        )
        results = [
            result_entry("s1", "list_files", "failed", success=False)
        ]

        decision = self.policy.decide(
            plan, results, attempted_tools={"other_tool": 7}
        )
        self.assertTrue(decision["replan"])
        self.assertEqual(decision["reason_code"], "retryable_failure")

    def test_duplicate_result_failures_are_repeated(self):
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "list_files",
                    status="failed",
                    result={"success": False},
                ),
                make_step(
                    "s2",
                    "list_files",
                    status="failed",
                    result={"success": False},
                ),
            ],
            status="failed",
        )
        results = [
            result_entry("s1", "list_files", "failed", success=False),
            result_entry("s2", "list_files", "failed", success=False),
        ]

        decision = self.policy.decide(plan, results)
        self.assertFalse(decision["replan"])
        self.assertEqual(decision["reason_code"], "repeated_failure")

    # -- blocked steps ---------------------------------------------------
    def test_blocked_only_does_not_replan(self):
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "trash_path",
                    status="blocked",
                    result={"success": False, "blocked": True},
                ),
                make_step(
                    "s2",
                    "launch_app",
                    status="skipped",
                    result={"success": False, "skipped": True},
                ),
            ],
            status="failed",
        )
        results = [
            result_entry("s1", "trash_path", "blocked", blocked=True),
            result_entry("s2", "launch_app", "skipped", skipped=True),
        ]

        decision = self.policy.decide(plan, results)

        assert_decision(self, decision)
        self.assertFalse(decision["replan"])
        self.assertEqual(decision["reason_code"], "no_failure")
        self.assertEqual(decision["failed_tools"], [])
        self.assertEqual(
            decision["blocked_tools"], ["trash_path"]
        )
        self.assertFalse(decision["verification_required"])

    def test_blocked_sensitive_step_does_not_mask_a_retryable_failure(self):
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "list_files",
                    status="failed",
                    result={"success": False},
                ),
                make_step(
                    "s2",
                    "trash_path",
                    status="blocked",
                    result={"success": False, "blocked": True},
                ),
            ],
            status="failed",
        )
        results = [
            result_entry("s1", "list_files", "failed", success=False),
            result_entry("s2", "trash_path", "blocked", blocked=True),
        ]

        decision = self.policy.decide(plan, results)

        self.assertTrue(decision["replan"])
        self.assertEqual(decision["reason_code"], "retryable_failure")
        self.assertEqual(decision["failed_tools"], ["list_files"])
        self.assertEqual(decision["retryable_tools"], ["list_files"])
        self.assertEqual(decision["blocked_tools"], ["trash_path"])
        self.assertFalse(decision["verification_required"])

    # -- plan representations and determinism ---------------------------
    def test_plain_dict_and_task_plan_produce_same_decision(self):
        task_plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "list_files",
                    status="failed",
                    result={"success": False},
                )
            ],
            status="failed",
        )
        dict_plan = {
            "status": "failed",
            "steps": [
                {
                    "id": "s1",
                    "tool": "list_files",
                    "status": "failed",
                    "depends_on": [],
                    "arguments": {"private": SECRET},
                }
            ],
        }
        results = [
            result_entry("s1", "list_files", "failed", success=False)
        ]

        from_task_plan = self.policy.decide(task_plan, results)
        from_dict = self.policy.decide(dict_plan, results)

        self.assertEqual(from_task_plan, from_dict)
        self.assertTrue(from_dict["replan"])

    def test_decide_is_deterministic_and_side_effect_free(self):
        plan = {
            "steps": [
                {
                    "id": "s1",
                    "tool": "list_files",
                    "status": "failed",
                },
                {
                    "id": "s2",
                    "tool": "list_files",
                    "status": "pending",
                },
            ]
        }
        results = [
            result_entry("s1", "list_files", "failed", success=False)
        ]
        attempted = {"list_files": 1}
        plan_before = copy.deepcopy(plan)
        results_before = copy.deepcopy(results)
        attempted_before = dict(attempted)

        first = self.policy.decide(
            plan, results, attempted_tools=attempted
        )
        with mock.patch(
            "builtins.open",
            side_effect=AssertionError("recovery must not touch files"),
        ):
            second = self.policy.decide(
                plan, results, attempted_tools=attempted
            )

        self.assertEqual(first, second)
        self.assertEqual(plan, plan_before)
        self.assertEqual(results, results_before)
        self.assertEqual(attempted, attempted_before)

    # -- malformed input -------------------------------------------------
    def test_malformed_inputs_never_raise(self):
        valid_plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "get_time",
                    status="success",
                    result={"success": True},
                )
            ],
            status="completed",
        )
        valid_results = [
            result_entry("s1", "get_time", "success", success=True)
        ]

        cases = (
            (None, [], {}),
            ("not a plan", [], {}),
            (valid_plan, None, {}),
            (valid_plan, "not a list", {}),
            ({"steps": None}, [], {}),
            ({"steps": "nope"}, [], {}),
            (
                {"steps": [{"tool": "list_files", "status": "exploded"}]},
                [],
                {},
            ),
            ({"steps": [{"id": "s1", "status": "failed"}]}, [], {}),
            (valid_plan, [42], {}),
            (
                valid_plan,
                [{"tool": "list_files", "status": "failed"}],
                {"replans_used": -1},
            ),
            (valid_plan, valid_results, {"replans_used": True}),
            (valid_plan, valid_results, {"denied": "yes"}),
            (
                valid_plan,
                valid_results,
                {"awaiting_user_choice": 1},
            ),
            (
                valid_plan,
                valid_results,
                {"attempted_tools": "list_files"},
            ),
            (
                valid_plan,
                valid_results,
                {"attempted_tools": {"list_files": "twice"}},
            ),
            (
                valid_plan,
                valid_results,
                {"retryable_tools": "list_files"},
            ),
            (
                valid_plan,
                valid_results,
                {"irreversible_tools": [42]},
            ),
        )

        for plan, results, kwargs in cases:
            with self.subTest(plan=plan, results=results, kwargs=kwargs):
                decision = self.policy.decide(
                    plan, results, **kwargs
                )
                assert_decision(self, decision)
                self.assertFalse(decision["replan"])
                self.assertEqual(
                    decision["reason_code"], "invalid_input"
                )
                self.assertFalse(decision["requires_user"])
                self.assertFalse(decision["verification_required"])
                self.assertFalse(decision["denied"])

    # -- privacy and boundedness ----------------------------------------
    def test_no_argument_or_result_content_leaks(self):
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "read_document",
                    status="failed",
                    result={
                        "success": False,
                        "message": SECRET,
                        "content": SECRET,
                    },
                )
            ],
            status="failed",
        )
        results = [
            result_entry("s1", "read_document", "failed", success=False)
        ]

        decision = self.policy.decide(plan, results)
        summary = summarize(plan, results)

        dumped_decision = json.dumps(
            decision, sort_keys=True, ensure_ascii=False
        )
        dumped_summary = json.dumps(
            summary, sort_keys=True, ensure_ascii=False
        )
        self.assertNotIn(SECRET, dumped_decision)
        self.assertNotIn(SECRET, dumped_summary)
        self.assertNotIn("arguments", decision)
        self.assertNotIn("result", decision)
        self.assertLessEqual(len(decision["reason"]), 240)
        self.assertLessEqual(len(decision["message"]), 240)
        self.assertLessEqual(len(summary["statuses"]), 32)

    def test_summarize_counts_and_invalid_input(self):
        plan = TaskPlan(
            steps=[
                make_step(
                    "s1",
                    "get_time",
                    status="success",
                    result={"success": True},
                ),
                make_step(
                    "s2",
                    "list_files",
                    status="failed",
                    result={"success": False},
                ),
                make_step(
                    "s3",
                    "trash_path",
                    status="blocked",
                    result={"success": False, "blocked": True},
                ),
                make_step(
                    "s4",
                    "launch_app",
                    status="skipped",
                    result={"success": False, "skipped": True},
                ),
            ],
            status="failed",
        )
        results = [
            result_entry("s1", "get_time", "success", success=True),
            result_entry("s2", "list_files", "failed", success=False),
            result_entry("s3", "trash_path", "blocked", blocked=True),
            result_entry("s4", "launch_app", "skipped", skipped=True),
        ]

        summary = summarize(plan, results)

        self.assertTrue(summary["valid"])
        self.assertEqual(summary["reason_code"], "ok")
        self.assertEqual(summary["plan_status"], "failed")
        self.assertEqual(summary["total_steps"], 4)
        self.assertEqual(
            summary["status_counts"],
            {
                "success": 1,
                "failed": 1,
                "blocked": 1,
                "skipped": 1,
                "pending": 0,
                "running": 0,
            },
        )
        self.assertEqual(summary["executed_steps"], 2)
        self.assertEqual(summary["remaining_steps"], 0)
        self.assertEqual(summary["failed_tools"], ["list_files"])
        self.assertEqual(summary["blocked_tools"], ["trash_path"])
        self.assertEqual(
            summary["statuses"],
            ["success", "failed", "blocked", "skipped"],
        )
        json.dumps(summary, sort_keys=True, allow_nan=False)

        invalid = summarize(None, [])
        self.assertFalse(invalid["valid"])
        self.assertEqual(invalid["reason_code"], "invalid_input")
        self.assertEqual(invalid["total_steps"], 0)
        self.assertEqual(
            set(invalid["status_counts"]),
            {"success", "failed", "blocked", "skipped", "pending", "running"},
        )
        json.dumps(invalid, sort_keys=True, allow_nan=False)

    # -- constructor limits ---------------------------------------------
    def test_constructor_rejects_invalid_limits(self):
        for kwargs in (
            {"max_replans": -1},
            {"max_replans": True},
            {"max_total_steps": 0},
            {"max_total_steps": "24"},
            {"max_consecutive_failures": 0},
            {"max_consecutive_failures": 1.5},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    RecoveryPolicy(**kwargs)


if __name__ == "__main__":
    unittest.main()
