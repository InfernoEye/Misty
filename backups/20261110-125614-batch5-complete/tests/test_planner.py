import unittest

from app.brain.planner import (
    MAX_PLAN_STEPS,
    PlanValidationError,
    TaskPlanner,
)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "open_folder",
            "description": "Open a known folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "enum": ["Downloads", "Documents"],
                    }
                },
                "required": ["folder"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_numeric_limit",
            "description": "Test numeric boundaries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                    }
                },
                "required": ["limit"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "volume_up",
            "description": "Increase volume.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]


def call(call_id, name, arguments, depends_on=None):
    result = {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }
    if depends_on is not None:
        result["depends_on"] = depends_on
    return result


class TaskPlannerTests(unittest.TestCase):
    def setUp(self):
        self.planner = TaskPlanner()

    def test_validates_complete_plan(self):
        plan = self.planner.prepare([
            call("1", "open_folder", '{"folder":"Downloads"}'),
            call("2", "volume_up", "{}"),
        ], TOOLS)

        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.steps[0].arguments["folder"], "Downloads")
        self.assertEqual(plan.status, "ready")

    def test_rejects_unknown_tool(self):
        with self.assertRaises(PlanValidationError):
            self.planner.prepare(
                [call("1", "run_shell", "{}")], TOOLS
            )

    def test_rejects_unknown_argument(self):
        with self.assertRaises(PlanValidationError):
            self.planner.prepare(
                [call("1", "open_folder",
                      '{"folder":"Downloads","path":"/tmp"}')],
                TOOLS,
            )

    def test_rejects_missing_argument(self):
        with self.assertRaises(PlanValidationError):
            self.planner.prepare(
                [call("1", "open_folder", "{}")], TOOLS
            )

    def test_rejects_invalid_enum(self):
        with self.assertRaises(PlanValidationError):
            self.planner.prepare(
                [call("1", "open_folder", '{"folder":"Secret"}')],
                TOOLS,
            )

    def test_rejects_invalid_json(self):
        with self.assertRaises(PlanValidationError):
            self.planner.prepare(
                [call("1", "open_folder", "{bad json}")], TOOLS
            )

    def test_rejects_duplicate_ids(self):
        with self.assertRaises(PlanValidationError):
            self.planner.prepare([
                call("same", "volume_up", "{}"),
                call("same", "volume_up", "{}"),
            ], TOOLS)

    def test_rejects_empty_plan(self):
        with self.assertRaises(PlanValidationError):
            self.planner.prepare([], TOOLS)

    def test_rejects_oversized_plan(self):
        calls = [
            call(str(i), "volume_up", "{}")
            for i in range(MAX_PLAN_STEPS + 1)
        ]
        with self.assertRaises(PlanValidationError):
            self.planner.prepare(calls, TOOLS)

    def test_tracks_successful_execution(self):
        plan = self.planner.prepare(
            [call("1", "volume_up", "{}")], TOOLS
        )

        self.planner.start_step(plan, 0)
        self.planner.finish_step(plan, 0, {"success": True})

        self.assertEqual(plan.steps[0].status, "success")
        self.assertEqual(plan.status, "completed")
        self.assertEqual(plan.completed, 1)

    def test_failure_preserves_independent_remaining_steps(self):
        plan = self.planner.prepare([
            call("1", "volume_up", "{}"),
            call("2", "open_folder", '{"folder":"Downloads"}'),
            call("3", "volume_up", "{}"),
        ], TOOLS)

        self.planner.start_step(plan, 0)
        self.planner.finish_step(
            plan, 0, {"success": False, "message": "Failed"}
        )

        self.assertEqual(plan.status, "running")
        self.assertEqual(plan.failed, 1)
        self.assertEqual(plan.skipped, 0)

        self.planner.start_step(plan, 1)
        self.planner.finish_step(plan, 1, {"success": True})
        self.planner.start_step(plan, 2)
        self.planner.finish_step(plan, 2, {"success": True})

        self.assertEqual(plan.status, "failed")
        self.assertEqual(plan.completed, 2)

    def test_cannot_finish_unstarted_step(self):
        plan = self.planner.prepare(
            [call("1", "volume_up", "{}")], TOOLS
        )

        with self.assertRaises(RuntimeError):
            self.planner.finish_step(plan, 0, {"success": True})



    def test_accepts_explicit_dependencies(self):
        plan = self.planner.prepare([
            call("open", "open_folder", '{"folder":"Downloads"}'),
            call("focus", "volume_up", "{}", ["open"]),
        ], TOOLS)
        self.assertEqual(plan.steps[1].depends_on, ["open"])

    def test_rejects_unknown_dependency(self):
        with self.assertRaises(PlanValidationError):
            self.planner.prepare([
                call("focus", "volume_up", "{}", ["missing"]),
            ], TOOLS)

    def test_rejects_self_dependency(self):
        with self.assertRaises(PlanValidationError):
            self.planner.prepare([
                call("one", "volume_up", "{}", ["one"]),
            ], TOOLS)

    def test_rejects_circular_dependencies(self):
        with self.assertRaises(PlanValidationError):
            self.planner.prepare([
                call("one", "volume_up", "{}", ["two"]),
                call("two", "volume_up", "{}", ["one"]),
            ], TOOLS)

    def test_rejects_non_list_dependencies(self):
        with self.assertRaises(PlanValidationError):
            self.planner.prepare([
                call("one", "volume_up", "{}", "two"),
            ], TOOLS)

    def test_rejects_duplicate_dependencies(self):
        with self.assertRaises(PlanValidationError):
            self.planner.prepare([
                call("one", "volume_up", "{}"),
                call("two", "volume_up", "{}", ["one", "one"]),
            ], TOOLS)

    def test_dependency_must_succeed_before_step_starts(self):
        plan = self.planner.prepare([
            call("open", "open_folder", '{"folder":"Downloads"}'),
            call("focus", "volume_up", "{}", ["open"]),
        ], TOOLS)

        with self.assertRaises(RuntimeError):
            self.planner.start_step(plan, 1)

        self.planner.start_step(plan, 0)
        self.planner.finish_step(plan, 0, {"success": True})
        self.planner.start_step(plan, 1)
        self.planner.finish_step(plan, 1, {"success": True})
        self.assertEqual(plan.status, "completed")

    def test_failure_blocks_transitive_dependents_but_not_independent_steps(self):
        plan = self.planner.prepare([
            call("first", "volume_up", "{}"),
            call("second", "volume_up", "{}", ["first"]),
            call("third", "volume_up", "{}", ["second"]),
            call("independent", "volume_up", "{}"),
        ], TOOLS)

        self.planner.start_step(plan, 0)
        self.planner.finish_step(
            plan, 0, {"success": False, "message": "Simulated failure"}
        )

        self.assertEqual(plan.steps[1].status, "blocked")
        self.assertEqual(plan.steps[2].status, "blocked")
        self.assertEqual(plan.steps[3].status, "pending")
        self.assertEqual(plan.blocked, 2)
        self.assertEqual(plan.status, "running")

        self.planner.start_step(plan, 3)
        self.planner.finish_step(plan, 3, {"success": True})
        self.assertEqual(plan.steps[3].status, "success")
        self.assertEqual(plan.status, "failed")

    def test_independent_steps_can_run_before_dependencies(self):
        plan = self.planner.prepare([
            call("dependent", "volume_up", "{}", ["later"]),
            call("independent", "volume_up", "{}"),
            call("later", "volume_up", "{}"),
        ], TOOLS)

        self.planner.start_step(plan, 1)
        self.planner.finish_step(plan, 1, {"success": True})
        self.planner.start_step(plan, 2)
        self.planner.finish_step(plan, 2, {"success": True})
        self.planner.start_step(plan, 0)
        self.planner.finish_step(plan, 0, {"success": True})
        self.assertEqual(plan.status, "completed")

    def test_rejects_value_below_minimum(self):
        with self.assertRaises(PlanValidationError):
            self.planner.prepare(
                [call("1", "test_numeric_limit", '{"limit": 0}')],
                TOOLS,
            )

    def test_rejects_value_above_maximum(self):
        with self.assertRaises(PlanValidationError):
            self.planner.prepare(
                [call("1", "test_numeric_limit", '{"limit": 101}')],
                TOOLS,
            )

    def test_accepts_confidence_clarification_and_reason(self):
        plan = self.planner.prepare_structured_plan(
            {
                "steps": [{
                    "id": "1",
                    "tool": "volume_up",
                    "arguments": {},
                    "depends_on": [],
                    "reason": "The user asked for louder audio.",
                }],
                "response": "",
                "intent_confidence": 0.87,
                "clarification": "",
            },
            TOOLS,
        )

        self.assertEqual(plan.intent_confidence, 0.87)
        self.assertEqual(plan.clarification, "")
        self.assertEqual(
            plan.steps[0].reason,
            "The user asked for louder audio.",
        )

    def test_rejects_invalid_confidence(self):
        for value in (-0.1, 1.1, True, "high", None):
            with self.subTest(value=value):
                with self.assertRaises(PlanValidationError):
                    self.planner.prepare_structured_plan(
                        {
                            "steps": [],
                            "response": "",
                            "intent_confidence": value,
                        },
                        TOOLS,
                    )

    def test_rejects_invalid_clarification_and_reason(self):
        for clarification in (123, "x" * 501):
            with self.subTest(clarification=clarification):
                with self.assertRaises(PlanValidationError):
                    self.planner.prepare_structured_plan(
                        {
                            "steps": [],
                            "response": "",
                            "clarification": clarification,
                        },
                        TOOLS,
                    )

        for reason in (123, "x" * 301):
            with self.subTest(reason=reason):
                with self.assertRaises(PlanValidationError):
                    self.planner.prepare_structured_plan(
                        {
                            "steps": [{
                                "id": "1",
                                "tool": "volume_up",
                                "arguments": {},
                                "depends_on": [],
                                "reason": reason,
                            }],
                            "response": "",
                        },
                        TOOLS,
                    )

    def test_accepts_value_at_numeric_boundaries(self):
        plan = self.planner.prepare(
            [call("1", "test_numeric_limit", '{"limit": 100}')],
            TOOLS,
        )
        self.assertEqual(plan.steps[0].arguments["limit"], 100)


if __name__ == "__main__":
    unittest.main()
