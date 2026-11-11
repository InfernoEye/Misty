import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.brain.tools import INSPECT_SCREEN_DEFINITION, ToolRegistry


def make_result(
    ocr_text="Recognized screen text",
    visual_text="A window with a button.",
    screen_name=None,
    mode="desktop",
):
    capture = SimpleNamespace(
        mode=mode,
        screen_name=screen_name,
        captured_at="2026-11-10T10:00:00+00:00",
        width=1920,
        height=1080,
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


class InspectScreenToolTests(unittest.TestCase):
    def setUp(self):
        self.vision = Mock()
        self.vision.inspect.return_value = make_result()
        self.registry = ToolRegistry(
            executor=Mock(),
            vision_service=self.vision,
        )

    def test_tool_is_advertised_only_when_vision_is_injected(self):
        registry_without_vision = ToolRegistry(executor=Mock())
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
        )

    def test_screen_capture_requires_and_forwards_screen_name(self):
        result = self.registry.execute(
            "inspect_screen",
            {"mode": "screen", "screen_name": "DP-1"},
        )
        self.assertTrue(result["success"])
        self.vision.inspect.assert_called_once_with(
            prompt=None,
            mode="screen",
            screen_name="DP-1",
            include_visual=True,
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
        self.vision.inspect.assert_not_called()

    def test_rejects_non_boolean_visual_toggle(self):
        result = self.registry.execute(
            "inspect_screen",
            {"include_visual": "false"},
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
