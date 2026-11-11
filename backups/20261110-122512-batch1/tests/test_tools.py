import unittest

from app.brain.tools import FOLDERS, ToolRegistry
from app.brain.validator import parse_tool_arguments


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, intent):
        self.calls.append(intent)
        return {"success": True, "message": "Test action accepted."}


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.executor = FakeExecutor()
        self.registry = ToolRegistry(self.executor)

    def test_open_downloads(self):
        result = self.registry.execute("open_folder", {"folder": "Downloads"})
        self.assertTrue(result["success"])
        self.assertEqual(
            self.executor.calls[-1]["arguments"]["path"],
            FOLDERS["Downloads"]
        )

    def test_open_documents(self):
        self.registry.execute("open_folder", {"folder": "Documents"})
        self.assertEqual(
            self.executor.calls[-1]["arguments"]["path"],
            "~/Documents"
        )

    def test_reject_unknown_folder(self):
        result = self.registry.execute("open_folder", {"folder": "/etc"})
        self.assertFalse(result["success"])
        self.assertEqual(len(self.executor.calls), 0)

    def test_reject_extra_arguments(self):
        result = self.registry.execute(
            "open_folder",
            {"folder": "Downloads", "command": "rm -rf"}
        )
        self.assertFalse(result["success"])
        self.assertEqual(len(self.executor.calls), 0)

    def test_reject_unknown_tool(self):
        result = self.registry.execute("run_shell", {"command": "whoami"})
        self.assertFalse(result["success"])
        self.assertEqual(len(self.executor.calls), 0)

    def test_volume_up(self):
        result = self.registry.execute("volume_up", {})
        self.assertTrue(result["success"])
        self.assertEqual(self.executor.calls[-1]["intent"], "volume_up")

    def test_reject_arguments_for_volume(self):
        result = self.registry.execute("volume_up", {"amount": 100})
        self.assertFalse(result["success"])
        self.assertEqual(len(self.executor.calls), 0)

    def test_parse_json_arguments(self):
        result = parse_tool_arguments('{"folder":"Downloads"}')
        self.assertEqual(result, {"folder": "Downloads"})

    def test_reject_invalid_json(self):
        with self.assertRaises(ValueError):
            parse_tool_arguments('{"folder":')

    def test_reject_json_array(self):
        with self.assertRaises(ValueError):
            parse_tool_arguments('["Downloads"]')


if __name__ == "__main__":
    unittest.main()
