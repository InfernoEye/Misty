import json
import unittest
from types import SimpleNamespace

from app.brain.replan import build_replan_context, should_replan


class Step:
    def __init__(self, call_id, name, status):
        self.call_id = call_id
        self.name = name
        self.status = status


def plan(*steps):
    return SimpleNamespace(steps=list(steps))


class ReplanContextTests(unittest.TestCase):
    def test_should_replan_respects_user_requirement(self):
        self.assertTrue(should_replan({"replan": True, "requires_user": False}))
        self.assertFalse(should_replan({"replan": True, "requires_user": True}))
        self.assertFalse(should_replan({"replan": False}))
        self.assertFalse(should_replan(None))

    def test_context_excludes_arguments_messages_and_content(self):
        failed = plan(
            Step("s1", "list_windows", "failed"),
            Step("s2", "get_time", "success"),
        )
        results = [
            {
                "id": "s1",
                "tool": "list_windows",
                "status": "failed",
                "arguments": {"window_id": "private-window"},
                "result": {
                    "success": False,
                    "message": "private failure text",
                    "error_code": "timeout",
                },
            },
            {
                "id": "s2",
                "tool": "get_time",
                "status": "success",
                "arguments": {},
                "result": {"success": True, "message": "private success"},
            },
        ]
        context = build_replan_context(
            "List windows",
            failed,
            results,
            {"replan": True, "reason_code": "retryable_failure"},
            max_steps=6,
        )

        serialized = json.dumps(context)
        self.assertTrue(context["can_replan"])
        self.assertEqual(context["failed_steps"][0]["tool"], "list_windows")
        self.assertEqual(context["failed_steps"][0]["error_code"], "timeout")
        self.assertNotIn("private-window", serialized)
        self.assertNotIn("private failure text", serialized)
        self.assertNotIn("private success", serialized)

    def test_context_is_bounded_and_json_safe(self):
        steps = [
            Step(f"s{i}", "get_time", "failed") for i in range(30)
        ]
        results = [
            {
                "id": f"s{i}",
                "tool": "get_time",
                "status": "failed",
                "result": {"success": False, "error_code": "x" * 100},
            }
            for i in range(30)
        ]
        context = build_replan_context(
            "x" * 2000,
            plan(*steps),
            results,
            {"replan": True, "reason_code": "retryable_failure"},
            max_steps=6,
            max_chars=1200,
        )
        self.assertLessEqual(len(json.dumps(context)), 1200)
        self.assertLessEqual(len(context["original_request"]), 500)
        json.dumps(context)

    def test_malformed_input_fails_closed(self):
        for bad in (None, {}, {"steps": "nope"}):
            with self.subTest(bad=bad):
                context = build_replan_context(
                    "request", bad, "nope", {"replan": True}
                )
                self.assertFalse(context["can_replan"])
                json.dumps(context)


if __name__ == "__main__":
    unittest.main()
