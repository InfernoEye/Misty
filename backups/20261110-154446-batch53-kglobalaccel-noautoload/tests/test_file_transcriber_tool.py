import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeTranscriber:
    def __init__(self):
        self.calls = []

    def transcribe(self, folder, relative_path, language="auto"):
        self.calls.append((folder, relative_path, language))
        return {
            "success": True,
            "text": "hello",
            "language": language,
            "message": "done",
        }


class FileTranscriberToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.transcriber = FakeTranscriber()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            file_transcriber=self.transcriber,
        )

    def test_tool_requires_transcriber(self):
        names = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertIn("transcribe_file", names)
        without = ToolRegistry(executor=Mock(), memory_store=self.memory)
        names = {
            item["function"]["name"]
            for item in without.available_tools()
        }
        self.assertNotIn("transcribe_file", names)

    def test_forwards_language(self):
        result = self.registry.execute(
            "transcribe_file",
            {
                "folder": "Documents",
                "relative_path": "clip.mp3",
                "language": "pl",
            },
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.transcriber.calls[0], ("Documents", "clip.mp3", "pl"))

    def test_invalid_arguments_do_not_transcribe(self):
        for arguments in (
            {"folder": "Documents"},
            {"folder": "Documents", "relative_path": "clip.mp3", "command": "x"},
        ):
            with self.subTest(arguments=arguments):
                self.assertFalse(
                    self.registry.execute("transcribe_file", arguments)["success"]
                )
        self.assertEqual(self.transcriber.calls, [])


if __name__ == "__main__":
    unittest.main()
