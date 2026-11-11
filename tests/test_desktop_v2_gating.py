"""Explicit v2 selection is an honest read-only gate, never legacy input."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.settings import load_desktop_backend
from app.brain.agent import MistyAgent
from app.brain.desktop_controller import CuaReadinessController
from app.brain.tools import ToolRegistry
from app.desktop.providers.base import CapabilityReport, CleanupOutcome, ToolOutcome


class BackendSettingsTests(unittest.TestCase):
    def test_missing_settings_default_to_legacy(self):
        with TemporaryDirectory() as directory:
            self.assertEqual(load_desktop_backend(path=Path(directory)/"missing", environ={}), "legacy")

    def test_explicit_file_and_environment_selections(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"desktop.json"
            path.write_text('{"backend":"cua"}')
            self.assertEqual(load_desktop_backend(path=path, environ={}), "cua")
            self.assertEqual(load_desktop_backend(path=path, environ={"MISTY_DESKTOP_BACKEND":"legacy"}), "legacy")

    def test_malformed_or_unknown_selection_never_falls_back(self):
        for value in ("", "other", "cua; command"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_desktop_backend(environ={"MISTY_DESKTOP_BACKEND":value})
        with TemporaryDirectory() as directory:
            path = Path(directory)/"desktop.json"
            for text in ("{", "[]", '{"backend":"other"}', '{"backend":"cua","unknown":true}'):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    load_desktop_backend(path=path, environ={})


class FakeProvider:
    windows = []
    stopped = True
    def __init__(self):
        self.calls = []
        self.closed = False
    def start(self, **kwargs):
        return CapabilityReport(True, "cua", "0.28.0")
    def call(self, tool, arguments, **kwargs):
        self.calls.append((tool, arguments))
        return ToolOutcome(tool, {"content": [], "structuredContent": {"windows": self.windows}}, "generation")
    def close(self):
        self.closed = True
        return CleanupOutcome(self.stopped)


class ReadinessControllerTests(unittest.TestCase):
    def check(self, windows=(), stopped=True):
        provider = FakeProvider()
        provider.windows = list(windows)
        provider.stopped = stopped
        events = []
        result = CuaReadinessController(lambda: provider, events.append).check()
        self.assertTrue(provider.closed)
        self.assertEqual(provider.calls, [("list_windows", {"on_screen_only":True})])
        self.assertFalse(result.input_ready)
        self.assertNotIn("secret-window-title", json.dumps(events))
        return result

    def test_zero_windows_cannot_report_platform_or_input_ready(self):
        result = self.check()
        self.assertEqual(result.code, "no_visible_windows")
        self.assertIn("did not fall back", result.summary())

    def test_nonempty_discovery_is_not_input_acceptance(self):
        result = self.check([{"pid":1,"window_id":2,"title":"secret-window-title"}])
        self.assertEqual(result.code, "observation_only")
        self.assertIn("read-only", result.summary())

    def test_cleanup_failure_is_explicit(self):
        result = self.check(stopped=False)
        self.assertFalse(result.cleanup_confirmed)
        self.assertEqual(result.code, "cleanup_unconfirmed")

    def test_precanceled_task_starts_nothing(self):
        factory = Mock()
        cancel = threading.Event()
        cancel.set()
        result = CuaReadinessController(factory).check(cancel=cancel)
        factory.assert_not_called()
        self.assertEqual(result.code, "canceled")


class AgentAndExecutionGatesTests(unittest.TestCase):
    def agent(self):
        agent = MistyAgent.__new__(MistyAgent)
        agent.registry = SimpleNamespace(input_backend=None, vision_service=None)
        agent._emit_activity = Mock()
        agent.cancel_event = None
        return agent

    def test_selected_cua_runs_gate_without_legacy_input_or_vision_backend(self):
        agent = self.agent()
        with patch("app.settings.load_desktop_backend", return_value="cua"), \
             patch("app.brain.task_grant.looks_like_desktop_work", return_value=True), \
             patch("app.brain.desktop_controller.run_cua_readiness", return_value="read-only") as gate:
            self.assertEqual(agent._maybe_run_desktop_task("click target"), "read-only")
            gate.assert_called_once_with(agent)

    def test_non_gui_request_stays_on_normal_path(self):
        with patch("app.settings.load_desktop_backend", return_value="cua"), \
             patch("app.brain.task_grant.looks_like_desktop_work", return_value=False):
            self.assertIsNone(self.agent()._maybe_run_desktop_task("time"))

    def test_invalid_settings_refuse_gui_work(self):
        with patch("app.settings.load_desktop_backend", side_effect=ValueError), \
             patch("app.brain.task_grant.looks_like_desktop_work", return_value=True):
            self.assertIn("invalid", self.agent()._maybe_run_desktop_task("click"))

    def test_direct_legacy_pointer_tool_is_blocked_even_when_confirmed(self):
        registry = ToolRegistry.__new__(ToolRegistry)
        for backend in ("cua", "invalid"):
            with self.subTest(backend=backend), patch("app.settings.load_desktop_backend", return_value=backend):
                result = registry.dispatch_desktop_tool("desktop_click", {"x":10,"y":20}, confirmed=True)
                self.assertFalse(result["success"])
                self.assertTrue(result["not_executed"])
                self.assertEqual(result["code"], "desktop_backend_not_ready")


if __name__ == "__main__":
    unittest.main()
