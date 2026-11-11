import unittest
from unittest.mock import patch

from app.actions.executor import ActionExecutor
from app.brain.intent_parser import parse_intent


class TestActionExecutor(unittest.TestCase):
    def setUp(self):
        self.executor = ActionExecutor()

    def test_unknown_intent_is_rejected(self):
        result = self.executor.execute(parse_intent("do something unusual"))
        self.assertFalse(result["success"])

    def test_close_window_requires_confirmation(self):
        intent = parse_intent("close window")

        self.assertTrue(intent["requires_confirmation"])

        result = self.executor.execute(intent)
        self.assertFalse(result["success"])

    def test_unknown_application_is_rejected(self):
        result = self.executor.execute({
            "intent": "open_application",
            "arguments": {"application": "some-random-command"},
            "requires_confirmation": False,
        })

        self.assertFalse(result["success"])

    def test_time_returns_success(self):
        result = self.executor.execute(parse_intent("what time is it?"))
        self.assertTrue(result["success"])
        self.assertIn("time", result)

    def test_system_status_returns_memory(self):
        result = self.executor.execute(
            parse_intent("system status")
        )

        self.assertTrue(result["success"])
        self.assertIn("memory_used_mb", result)
        self.assertIn("memory_total_mb", result)

    @patch("app.actions.executor.shutil.which", return_value=None)
    def test_browser_fails_safely_without_xdg_open(self, _):
        result = self.executor.execute(
            parse_intent("open the browser")
        )

        self.assertFalse(result["success"])

    def test_confirmation_flag_is_not_an_argument(self):
        intent = parse_intent("close window")

        self.assertEqual(intent["arguments"], {})
        self.assertTrue(intent["requires_confirmation"])


if __name__ == "__main__":
    unittest.main()
