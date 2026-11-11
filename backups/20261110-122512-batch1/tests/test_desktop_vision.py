import unittest
from dataclasses import dataclass
from unittest.mock import Mock

from PIL import Image

from app.desktop.vision.capture import CaptureResult
from app.desktop.vision.coordinator import (
    DEFAULT_PROMPT,
    DesktopVision,
)
from app.desktop.vision.ocr import OCRResult
from app.desktop.vision.vlm import VisionLanguageResult


@dataclass
class FakeCapture:
    image: Image.Image
    mode: str = "desktop"
    captured_at: str = "2026-01-01T00:00:00+00:00"
    width: int = 100
    height: int = 80
    screen_name: str | None = None


class DesktopVisionTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new("RGB", (100, 80), "white")
        self.capture_result = CaptureResult(
            image=self.image,
            mode="desktop",
            captured_at="2026-01-01T00:00:00+00:00",
            width=100,
            height=80,
        )
        self.ocr_result = OCRResult(
            text="Save Cancel",
            words=(),
            language="eng+pol",
            image_size=(100, 80),
        )
        self.visual_result = VisionLanguageResult(
            text="A dialog with Save and Cancel buttons.",
            elapsed_seconds=1.2,
            image_size=(100, 80),
            model="test-model.gguf",
        )

        self.capture = Mock()
        self.capture.capture.return_value = self.capture_result
        self.ocr = Mock()
        self.ocr.recognize.return_value = self.ocr_result
        self.vlm = Mock()
        self.vlm.analyze.return_value = self.visual_result

        self.service = DesktopVision(
            self.capture,
            self.ocr,
            self.vlm,
        )

    def test_inspect_combines_all_three_services(self):
        result = self.service.inspect("Describe the dialog.")

        self.assertIs(result.capture, self.capture_result)
        self.assertIs(result.ocr, self.ocr_result)
        self.assertIs(result.visual, self.visual_result)
        self.capture.capture.assert_called_once_with(
            mode="desktop",
            screen_name=None,
        )
        self.ocr.recognize.assert_called_once_with(self.image)
        self.vlm.analyze.assert_called_once_with(
            self.image,
            "Describe the dialog.",
        )

    def test_default_prompt_is_used(self):
        self.service.inspect()
        self.vlm.analyze.assert_called_once_with(self.image, DEFAULT_PROMPT)

    def test_visual_analysis_can_be_disabled(self):
        result = self.service.inspect(include_visual=False)

        self.assertIsNone(result.visual)
        self.ocr.recognize.assert_called_once_with(self.image)
        self.vlm.analyze.assert_not_called()

    def test_screen_selection_is_forwarded(self):
        self.service.inspect(mode="screen", screen_name="DP-1")

        self.capture.capture.assert_called_once_with(
            mode="screen",
            screen_name="DP-1",
        )

    def test_result_exposes_capture_metadata(self):
        result = self.service.inspect(include_visual=False)

        self.assertEqual(result.image_size, (100, 80))
        self.assertEqual(result.captured_at, "2026-01-01T00:00:00+00:00")

    def test_capture_failure_stops_pipeline(self):
        self.capture.capture.side_effect = RuntimeError("capture failed")

        with self.assertRaisesRegex(RuntimeError, "capture failed"):
            self.service.inspect()

        self.ocr.recognize.assert_not_called()
        self.vlm.analyze.assert_not_called()

    def test_ocr_failure_stops_visual_inference(self):
        self.ocr.recognize.side_effect = RuntimeError("OCR failed")

        with self.assertRaisesRegex(RuntimeError, "OCR failed"):
            self.service.inspect()

        self.vlm.analyze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
