import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeTelemetry:
    def __init__(self):
        self.calls = 0
        self.result = {
            "success": True,
            "cpu_percent": 12.5,
            "memory": {"percent": 40.0},
            "message": "ok",
        }

    def summary(self):
        self.calls += 1
        return self.result


class SystemTelemetryToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(
            Path(directory.name) / "memory.sqlite3"
        )
        self.telemetry = FakeTelemetry()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            telemetry=self.telemetry,
        )

    def test_tool_is_advertised_and_returns_summary(self):
        names = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertIn("get_system_telemetry", names)

        result = self.registry.execute(
            "get_system_telemetry", {}
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["cpu_percent"], 12.5)
        self.assertEqual(self.telemetry.calls, 1)

    def test_rejects_arguments(self):
        result = self.registry.execute(
            "get_system_telemetry", {"path": "/"}
        )

        self.assertFalse(result["success"])
        self.assertEqual(self.telemetry.calls, 0)

    def test_failure_is_returned_safely(self):
        self.telemetry.result = {
            "success": False,
            "message": "System telemetry is unavailable.",
        }

        result = self.registry.execute(
            "get_system_telemetry", {}
        )

        self.assertFalse(result["success"])
        self.assertEqual(
            result["message"], "System telemetry is unavailable."
        )


if __name__ == "__main__":
    unittest.main()
