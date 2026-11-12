"""Explicit native routing and resource hooks; no desktop/model/input runs."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.brain.agent import MistyAgent
from app.brain.tools import ToolRegistry
from app.desktop.ownership import native_task_lease
from app.engine.voice_controller import VoiceController
from app.settings import load_desktop_backend
from tests.test_voice_confirmation_binding import build_controller, SignalCollector


class BackendSelectionTests(unittest.TestCase):
    def test_kwin_is_explicit_environment_and_file_option(self):
        self.assertEqual(load_desktop_backend(environ={"MISTY_DESKTOP_BACKEND": " KWin "}), "kwin")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "desktop.json"
            self.assertEqual(load_desktop_backend(path=path, environ={}), "legacy")
            path.write_text('{"backend":"kwin"}')
            self.assertEqual(load_desktop_backend(path=path, environ={}), "kwin")
            path.write_text('{"backend":"unsupported"}')
            with self.assertRaises(ValueError):
                load_desktop_backend(path=path, environ={})

    def test_existing_cua_and_legacy_choices_remain_valid(self):
        for backend in ("cua", "legacy"):
            self.assertEqual(load_desktop_backend(environ={"MISTY_DESKTOP_BACKEND": backend}), backend)


class AgentRoutingTests(unittest.TestCase):
    def setUp(self):
        self.agent = MistyAgent.__new__(MistyAgent)
        # No legacy input or vision backend: native controller owns readiness.
        self.agent.registry = SimpleNamespace(input_backend=None, vision_service=None)
        self.module = ModuleType("app.brain.kwin_controller")
        self.module.run_kwin_task = Mock(return_value="native pilot result")

    def route(self, transcript, desktop=True):
        with patch("app.settings.load_desktop_backend", return_value="kwin"), \
             patch("app.brain.task_grant.looks_like_desktop_work", return_value=desktop) as classify, \
             patch.dict(sys.modules, {"app.brain.kwin_controller": self.module}):
            result = self.agent._maybe_run_desktop_task(transcript)
        classify.assert_called_once_with(self.agent, transcript)
        return result

    def test_native_request_dispatches_exact_goal_without_legacy_guards(self):
        self.assertEqual(self.route("Click Dragon Cave on the right screen"), "native pilot result")
        self.module.run_kwin_task.assert_called_once_with(self.agent, "Click Dragon Cave on the right screen")

    def test_non_desktop_request_does_not_launch_native_controller(self):
        self.assertIsNone(self.route("what time is it", desktop=False))
        self.module.run_kwin_task.assert_not_called()

    def test_optional_visual_callback_preserves_constructor_compatibility(self):
        registry = SimpleNamespace()
        old = MistyAgent(registry=registry)
        # Without the voice controller, a standalone run still releases Ministral.
        self.assertEqual(old.before_visual_callback, old.stop)
        callback = Mock()
        new = MistyAgent(registry=registry, before_visual_callback=callback)
        self.assertIs(new.before_visual_callback, callback)
        callback.assert_not_called()


class NativeDispatchSafetyTests(unittest.TestCase):
    def test_confirmed_direct_pointer_tool_cannot_bypass_native_controller(self):
        registry = ToolRegistry.__new__(ToolRegistry)
        with patch("app.settings.load_desktop_backend", return_value="kwin"):
            result = registry.dispatch_desktop_tool("desktop_click", {"x": 1, "y": 2}, confirmed=True)
        self.assertFalse(result["success"])
        self.assertTrue(result["not_executed"])
        self.assertEqual(result["code"], "desktop_backend_not_ready")

    def test_native_lease_blocks_concurrent_legacy_dispatch(self):
        registry = ToolRegistry.__new__(ToolRegistry)
        with native_task_lease(), patch("app.settings.load_desktop_backend", return_value="legacy"):
            result = registry.dispatch_desktop_tool("desktop_press_key", {"key": "ENTER"}, confirmed=True)
        self.assertFalse(result["success"])
        self.assertTrue(result["not_executed"])
        self.assertEqual(result["code"], "desktop_busy")


class VoiceHookWiringTests(unittest.TestCase):
    def controller(self):
        controller = VoiceController.__new__(VoiceController)
        controller._build_registry = Mock(return_value=SimpleNamespace())
        controller.audit_log = None
        controller.task_journal = None
        return controller

    def test_task_agent_factory_wires_existing_owner_release_hook(self):
        controller = self.controller()
        with patch("app.engine.voice_controller.MistyAgent") as constructor:
            controller._task_agent()
        self.assertEqual(constructor.call_args.kwargs["before_visual_callback"], controller.unload_reasoning_model)

    def test_voice_preload_factory_wires_same_owner_release_hook(self):
        controller = self.controller()
        controller._transcriber_factory = Mock(return_value=SimpleNamespace())
        controller._preload_epoch = 0
        with patch("app.engine.voice_controller.MistyAgent") as constructor, \
             patch("app.engine.voice_controller.threading.Thread"):
            controller._start_preloads()
        self.assertEqual(constructor.call_args.kwargs["before_visual_callback"], controller.unload_reasoning_model)


class NativeApprovalPrivacyTests(unittest.TestCase):
    def test_private_target_description_goes_to_dialog_not_activity(self):
        for name in ("kwin_click", "kwin_observe"):
            with self.subTest(name=name):
                broker = SimpleNamespace(
                    begin=lambda *args: {"request_id": "native-test"},
                    wait=lambda pending: False,
                )
                controller = build_controller(lambda: None, broker)
                controller.confirmation_requested = SignalCollector()
                description = "PRIVATE-WINDOW-TARGET at x=125 y=250"
                self.assertFalse(controller._request_confirmation(name, {}, description))
                emitted = controller.confirmation_requested.values[-1]
                self.assertEqual(emitted["description"], description)
                self.assertNotIn("PRIVATE-WINDOW-TARGET", json.dumps(controller.activity_event.values))
                controller._finish_recording()

    def test_other_confirmation_activity_details_are_unchanged(self):
        broker = SimpleNamespace(begin=lambda *args: {"request_id": "normal-test"}, wait=lambda pending: False)
        controller = build_controller(lambda: None, broker)
        controller.confirmation_requested = SignalCollector()
        controller._request_confirmation("desktop_task", {}, "ordinary description")
        self.assertEqual(controller.activity_event.values[0]["details"], "ordinary description")
        controller._finish_recording()


if __name__ == "__main__":
    unittest.main()
