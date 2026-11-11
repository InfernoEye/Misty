import unittest

from app.brain.tools import ToolRegistry, SENSITIVE_TOOLS, TOOL_DEFINITIONS


class FakeClipboard:
    def __init__(self, text="initial"):
        self.text = text
        self.writes = []

    def read_text(self):
        return {
            "success": True,
            "text": self.text,
            "length": len(self.text),
            "message": "Read clipboard text.",
        }

    def write_text(self, text):
        self.text = text
        self.writes.append(text)
        return {
            "success": True,
            "length": len(text),
            "message": "Clipboard text was replaced.",
        }


class ClipboardToolTests(unittest.TestCase):
    def setUp(self):
        self.clipboard = FakeClipboard()
        self.registry = ToolRegistry(clipboard_service=self.clipboard)

    def test_clipboard_tools_are_registered(self):
        names = {
            item["function"]["name"]
            for item in TOOL_DEFINITIONS
        }
        self.assertIn("read_clipboard", names)
        self.assertIn("write_clipboard", names)

    def test_read_clipboard(self):
        result = self.registry.execute("read_clipboard", {})
        self.assertTrue(result["success"])
        self.assertEqual(result["text"], "initial")

    def test_read_clipboard_rejects_arguments(self):
        result = self.registry.execute(
            "read_clipboard", {"path": "/tmp/example"}
        )
        self.assertFalse(result["success"])

    def test_read_clipboard_without_service(self):
        registry = ToolRegistry()
        result = registry.execute("read_clipboard", {})
        self.assertFalse(result["success"])

    def test_write_clipboard_requires_confirmation(self):
        self.assertIn("write_clipboard", SENSITIVE_TOOLS)
        result = self.registry.execute(
            "write_clipboard", {"text": "replacement"}
        )
        self.assertFalse(result["success"])
        self.assertEqual(self.clipboard.text, "initial")
        self.assertEqual(self.clipboard.writes, [])

    def test_confirmed_write_replaces_text(self):
        result = self.registry.execute(
            "write_clipboard",
            {"text": "replacement"},
            confirmed=True,
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.clipboard.text, "replacement")
        self.assertEqual(self.clipboard.writes, ["replacement"])

    def test_write_rejects_non_text(self):
        result = self.registry.execute(
            "write_clipboard", {"text": 123}, confirmed=True
        )
        self.assertFalse(result["success"])
        self.assertEqual(self.clipboard.text, "initial")

    def test_write_rejects_oversized_text(self):
        result = self.registry.execute(
            "write_clipboard",
            {"text": "x" * 20001},
            confirmed=True,
        )
        self.assertFalse(result["success"])
        self.assertEqual(self.clipboard.text, "initial")


if __name__ == "__main__":
    unittest.main()
