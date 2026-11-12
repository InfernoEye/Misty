import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.brain.memory import MemoryStore
from app.brain.tools import INSPECT_SCREEN_DEFINITION, ToolRegistry


def make_result(
    ocr_text="Recognized screen text",
    visual_text="A window with a button.",
    screen_name=None,
    mode="desktop",
    window_id=None,
    window_title=None,
    capture_kind="fullscreen",
):
    capture = SimpleNamespace(
        mode=mode,
        screen_name=screen_name,
        captured_at="2026-11-10T10:00:00+00:00",
        width=1920,
        height=1080,
        window_id=window_id,
        window_title=window_title,
        window_geometry=(
            {"x": 10, "y": 20, "width": 1920, "height": 1080}
            if window_id else None
        ),
        capture_kind=capture_kind,
        masked_overlays=(),
    )
    ocr = SimpleNamespace(text=ocr_text)
    visual = (
        None if visual_text is None
        else SimpleNamespace(text=visual_text, elapsed_seconds=2.5)
    )
    return SimpleNamespace(
        capture=capture,
        ocr=ocr,
        visual=visual,
        captured_at=capture.captured_at,
    )


class FakeWindowExecutor:
    def __init__(self, windows, screens=None):
        self.windows = windows
        self.screens = screens or []

    def list_windows(self):
        return {"success": True, "windows": self.windows}

    def list_screens(self):
        return {"success": True, "screens": self.screens}


class InspectScreenToolTests(unittest.TestCase):
    def setUp(self):
        self.vision = Mock()
        self.vision.inspect.return_value = make_result()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            vision_service=self.vision,
        )

    def test_tool_is_advertised_only_when_vision_is_injected(self):
        registry_without_vision = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
        )
        names_without = {
            item["function"]["name"]
            for item in registry_without_vision.available_tools()
        }
        names_with = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertNotIn("inspect_screen", names_without)
        self.assertIn("inspect_screen", names_with)
        self.assertEqual(
            INSPECT_SCREEN_DEFINITION["function"]["name"],
            "inspect_screen",
        )

    def test_inspection_returns_bounded_serializable_data(self):
        self.vision.inspect.return_value = make_result(
            ocr_text="O" * 13000,
            visual_text="V" * 7000,
        )
        result = self.registry.execute("inspect_screen", {})

        self.assertTrue(result["success"])
        self.assertEqual(len(result["ocr_text"]), 12000)
        self.assertTrue(result["ocr_truncated"])
        self.assertEqual(len(result["visual_interpretation"]), 6000)
        self.assertTrue(result["visual_interpretation_is_unverified"])
        self.assertNotIn("image", result)
        self.assertNotIn("path", result)
        self.assertNotIn("screenshot", result)
        self.vision.inspect.assert_called_once_with(
            prompt=None,
            mode="desktop",
            screen_name=None,
            include_visual=True,
            window_id=None,
        )

    def test_screen_capture_requires_and_forwards_screen_name(self):
        # Screen resolution is strict now: the executor must expose the
        # connector that the request names, both for the lookup and for the
        # forwarded arguments.
        executor = FakeWindowExecutor(
            [],
            screens=[{"name": "DP-1", "geometry": {"x": 0, "y": 0}}],
        )
        registry = ToolRegistry(
            executor=executor,
            memory_store=self.memory,
            vision_service=self.vision,
        )

        result = registry.execute(
            "inspect_screen",
            {"mode": "screen", "screen_name": "DP-1"},
        )
        self.assertTrue(result["success"], result)
        self.vision.inspect.assert_called_once_with(
            prompt=None,
            mode="screen",
            screen_name="DP-1",
            include_visual=True,
            window_id=None,
        )

    def test_active_window_rejects_screen_name(self):
        result = self.registry.execute(
            "inspect_screen",
            {"mode": "active_window", "screen_name": "DP-1"},
        )
        self.assertFalse(result["success"])
        self.vision.inspect.assert_not_called()

    def test_screen_mode_requires_screen_name(self):
        result = self.registry.execute(
            "inspect_screen",
            {"mode": "screen"},
        )
        self.assertFalse(result["success"])
        self.vision.inspect.assert_not_called()

    def test_invalid_mode_types_are_rejected_safely(self):
        for mode in ([], {}, 42, None):
            with self.subTest(mode=mode):
                result = self.registry.execute(
                    "inspect_screen",
                    {"mode": mode},
                )
                self.assertFalse(result["success"])
        self.vision.inspect.assert_not_called()

    def test_prompt_and_visual_toggle_are_forwarded(self):
        self.vision.inspect.return_value = make_result(visual_text=None)
        result = self.registry.execute(
            "inspect_screen",
            {
                "mode": "active_window",
                "prompt": "What dialog is visible?",
                "include_visual": False,
            },
        )
        self.assertTrue(result["success"])
        self.assertIsNone(result["visual_interpretation"])
        self.assertFalse(result["visual_interpretation_is_unverified"])
        self.vision.inspect.assert_called_once_with(
            prompt="What dialog is visible?",
            mode="active_window",
            screen_name=None,
            include_visual=False,
            window_id=None,
        )

    def test_rejects_oversized_or_invalid_prompt(self):
        for prompt in ("", " " * 5, "x" * 501, 123):
            with self.subTest(prompt=repr(prompt)):
                result = self.registry.execute(
                    "inspect_screen",
                    {"prompt": prompt},
                )
                self.assertFalse(result["success"])
        self.vision.inspect.assert_not_called()

    def test_rejects_unknown_arguments(self):
        result = self.registry.execute(
            "inspect_screen",
            {"command": "do something"},
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "invalid_arguments")
        self.assertIs(result["not_executed"], True)
        self.vision.inspect.assert_not_called()

    def test_unsupported_argument_diagnostics_do_not_echo_private_names_or_values(self):
        result = self.registry.execute(
            "inspect_screen", {"private customer note": "confidential payload"}
        )
        self.assertNotIn("private customer note", str(result))
        self.assertNotIn("confidential payload", str(result))
        properties = INSPECT_SCREEN_DEFINITION["function"]["parameters"]["properties"]
        for name in properties:
            self.assertIn(name, result["message"])
        self.vision.inspect.assert_not_called()

    def test_advertised_optional_fields_pass_strict_dispatch(self):
        # Exercise each property rather than comparing duplicate allowlists.
        self.registry.executor = FakeWindowExecutor([], screens=[{
            "name": "DP-1", "width": 1920, "height": 1080,
        }])
        for arguments in (
            {"mode": "desktop"},
            {"mode": "screen", "screen_name": "DP-1"},
            {"prompt": "What is visible?"},
            {"include_visual": False},
        ):
            with self.subTest(arguments=arguments):
                result = self.registry.execute("inspect_screen", arguments)
                self.assertTrue(result["success"], result)

    def test_safe_invalid_argument_repair_runs_only_corrected_capture(self):
        from app.brain.agent_loop import run_loop
        decisions = iter([
            {"kind": "act", "tool": "inspect_screen",
             "arguments": {"mode": "active_window", "question": "Read dialog"}},
            {"kind": "act", "tool": "inspect_screen",
             "arguments": {"mode": "active_window", "prompt": "Read dialog"}},
            {"kind": "done", "note": "Observed"},
        ])
        observations = iter(["before", "repair", "after"])
        history = []
        def decide(observation, previous):
            history.append(previous)
            return next(decisions)
        report = run_loop(lambda: next(observations), decide, self.registry.execute)
        self.assertEqual(report.status, "done")
        self.assertEqual(history[1][0]["result"]["code"], "invalid_arguments")
        self.vision.inspect.assert_called_once_with(
            mode="active_window", screen_name=None, window_id=None,
            prompt="Read dialog", include_visual=True,
        )

    def test_invalid_argument_repairs_are_bounded(self):
        from app.brain.agent_loop import run_loop
        observe = Mock(side_effect=["first", "second", "third", "fourth"])
        decide = Mock(return_value={
            "kind": "act", "tool": "inspect_screen", "arguments": {"question": "Read dialog"},
        })
        report = run_loop(observe, decide, self.registry.execute, max_rounds=8)
        self.assertEqual(report.status, "failed")
        self.assertEqual(decide.call_count, 3)
        self.vision.inspect.assert_not_called()

    def test_uncertain_inspection_failure_is_not_repaired(self):
        from app.brain.agent_loop import run_loop
        self.vision.inspect.side_effect = RuntimeError("capture failed")
        decide = Mock(return_value={"kind": "act", "tool": "inspect_screen", "arguments": {}})
        report = run_loop(lambda: "screen", decide, self.registry.execute)
        self.assertEqual(report.status, "failed")
        decide.assert_called_once()
        self.vision.inspect.assert_called_once()

    def test_rejects_non_boolean_visual_toggle(self):
        result = self.registry.execute(
            "inspect_screen",
            {"include_visual": "false"},
        )
        self.assertFalse(result["success"])
        self.vision.inspect.assert_not_called()

    def test_schema_advertises_window_mode_and_window_id(self):
        parameters = INSPECT_SCREEN_DEFINITION["function"]["parameters"]
        self.assertIn("window", parameters["properties"]["mode"]["enum"])
        self.assertIn("window_id", parameters["properties"])

    def test_named_window_is_resolved_and_forwarded(self):
        executor = FakeWindowExecutor([
            {
                "id": "win-11",
                "title": "Helium",
                "app_id": "helium",
                "resource_name": "helium",
                "resource_class": "Helium",
            },
        ])
        vision = Mock()
        vision.inspect.return_value = make_result(
            mode="window",
            window_id="win-11",
            window_title="Helium",
            capture_kind="visible_region",
        )
        registry = ToolRegistry(
            executor=executor,
            memory_store=self.memory,
            vision_service=vision,
        )

        result = registry.execute(
            "inspect_screen",
            {
                "mode": "window",
                "window_id": "helium",
                "prompt": "What is visible?",
            },
        )

        self.assertTrue(result["success"], result)
        vision.inspect.assert_called_once_with(
            prompt="What is visible?",
            mode="window",
            screen_name=None,
            include_visual=True,
            window_id="win-11",
        )
        self.assertEqual(result["window_id"], "win-11")
        self.assertEqual(result["window_title"], "Helium")
        self.assertEqual(result["capture_kind"], "visible_region")
        self.assertTrue(result["occlusion_possible"])
        self.assertIn("occlude", result["message"])

    def test_ambiguous_named_window_auto_selects_active_and_reports(self):
        self.vision.inspect.return_value = make_result(
            mode="window",
            window_id="win-2",
            window_title="Helium - Tab 2",
            capture_kind="visible_region",
        )
        executor = FakeWindowExecutor([
            {
                "id": "win-1", "title": "Helium - Tab 1",
                "app_id": "helium", "active": False,
            },
            {
                "id": "win-2", "title": "Helium - Tab 2",
                "app_id": "helium", "active": True,
            },
        ])
        registry = ToolRegistry(
            executor=executor,
            memory_store=self.memory,
            vision_service=self.vision,
        )

        result = registry.execute(
            "inspect_screen",
            {"mode": "window", "window_id": "helium"},
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["window_id"], "win-2")
        self.assertTrue(result["target_auto_selected"])
        self.assertEqual(result["other_matching_windows"], 1)
        self.assertIn("chosen automatically", result["message"])
        self.vision.inspect.assert_called_once_with(
            prompt=None,
            mode="window",
            screen_name=None,
            include_visual=True,
            window_id="win-2",
        )

    def test_window_lookup_retries_after_launch(self):
        class FlakyWindowExecutor:
            def __init__(self):
                self.calls = 0

            def list_windows(self):
                self.calls += 1
                windows = [] if self.calls < 3 else [{
                    "id": "win-dolphin",
                    "title": "Pobrane — Dolphin",
                    "app_id": "org.kde.dolphin",
                    "on_current_desktop": True,
                }]
                return {"success": True, "windows": windows}

        executor = FlakyWindowExecutor()
        vision = Mock()
        vision.inspect.return_value = make_result(
            mode="window",
            window_id="win-dolphin",
            window_title="Pobrane — Dolphin",
            capture_kind="visible_region",
        )
        registry = ToolRegistry(
            executor=executor,
            memory_store=self.memory,
            vision_service=vision,
        )

        with patch("app.brain.tools.time.sleep", return_value=None):
            result = registry.execute(
                "inspect_screen",
                {"mode": "window", "window_id": "dolphin"},
            )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["window_id"], "win-dolphin")
        self.assertGreaterEqual(executor.calls, 3)

    def test_misspelled_window_name_is_fuzzy_for_inspection_only(self):
        executor = FakeWindowExecutor([
            {
                "id": "win-helium",
                "title": "Nowa karta – Helium",
                "app_id": "helium",
                "on_current_desktop": True,
            },
        ])
        vision = Mock()
        vision.inspect.return_value = make_result(
            mode="window",
            window_id="win-helium",
            window_title="Nowa karta – Helium",
            capture_kind="visible_region",
        )
        registry = ToolRegistry(
            executor=executor,
            memory_store=self.memory,
            vision_service=vision,
        )

        self.assertFalse(
            registry.resolve_window_target("Heleium")["success"]
        )
        result = registry.execute(
            "inspect_screen",
            {"mode": "window", "window_id": "Heleium"},
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["window_id"], "win-helium")
        vision.inspect.assert_called_once_with(
            prompt=None,
            mode="window",
            screen_name=None,
            include_visual=True,
            window_id="win-helium",
        )

    def test_a_window_mode_without_a_window_looks_at_the_desktop(self):
        # Real failure this fixed: the model asked to look at a window it had
        # no id for, and the whole request died on that. Inspection is
        # read-only and "look at everything" answers the same question.
        for arguments in (
            {"mode": "window"},
            {"mode": "window", "window_id": ""},
            {"mode": "window", "window_id": "   "},
        ):
            with self.subTest(arguments=arguments):
                self.vision.inspect.reset_mock()
                result = self.registry.execute("inspect_screen", arguments)
                self.assertTrue(result["success"], result)
                self.vision.inspect.assert_called_once()

    def test_a_window_mode_with_a_screen_name_is_treated_as_a_screen_capture(self):
        # It must reach screen resolution rather than being rejected as an
        # invalid flag combination; this fake has no display service, so the
        # honest outcome is a resolution failure, not a shape error.
        self.vision.inspect.reset_mock()
        result = self.registry.execute(
            "inspect_screen", {"mode": "window", "screen_name": "left"}
        )
        self.assertFalse(result["success"])
        self.assertIn("display", str(result.get("message", "")).lower())
        self.vision.inspect.assert_not_called()

    def test_a_malformed_window_id_still_fails_closed(self):
        for arguments in ({"mode": "window", "window_id": 42},):
            with self.subTest(arguments=arguments):
                result = self.registry.execute("inspect_screen", arguments)
                self.assertFalse(result["success"])
        self.vision.inspect.assert_not_called()

    def test_window_id_is_rejected_outside_window_mode(self):
        for mode in ("desktop", "active_window", "screen"):
            arguments = {"mode": mode, "window_id": "win-1"}
            if mode == "screen":
                arguments["screen_name"] = "DP-1"
            with self.subTest(mode=mode):
                result = self.registry.execute("inspect_screen", arguments)
                self.assertFalse(result["success"])
        self.vision.inspect.assert_not_called()

    def test_screen_name_is_rejected_in_window_mode(self):
        result = self.registry.execute(
            "inspect_screen",
            {
                "mode": "window",
                "window_id": "win-1",
                "screen_name": "DP-1",
            },
        )
        self.assertFalse(result["success"])
        self.vision.inspect.assert_not_called()

    def test_service_failure_returns_safe_failure_result(self):
        self.vision.inspect.side_effect = RuntimeError("capture backend unavailable")
        result = self.registry.execute("inspect_screen", {})
        self.assertFalse(result["success"])
        self.assertIn("capture backend unavailable", result["message"])


if __name__ == "__main__":
    unittest.main()
