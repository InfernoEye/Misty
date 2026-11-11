import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image

from app.desktop.vision.capture import CaptureError, CaptureService
from app.desktop.vision.ocr import OCRService


class CaptureTests(unittest.TestCase):
    def test_active_window_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "fake.png"
            Image.new("RGB", (1200, 800), "white").save(image_path)

            def fake_run(command, **kwargs):
                output = Path(command[command.index("--output") + 1])
                Image.new("RGB", (1200, 800), "white").save(output)

                class Result:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return Result()

            with patch(
                "app.desktop.vision.capture.subprocess.run",
                side_effect=fake_run,
            ):
                result = CaptureService().capture("active_window")

            self.assertEqual(result.image.size, (1200, 800))
            self.assertEqual(result.mode, "active_window")

    def test_active_window_uses_saved_identity_and_fresh_geometry(self):
        saved = {
            "success": True,
            "active_window": {"id": "original-window", "title": "Original"},
        }
        context = {
            "success": True,
            "windows": [
                {
                    "id": "misty-popup",
                    "minimized": False,
                    "geometry": {"x": 0, "y": 0, "width": 40, "height": 30},
                },
                {
                    "id": "original-window",
                    "minimized": False,
                    "geometry": {"x": 20, "y": 10, "width": 50, "height": 40},
                },
            ],
            "screens": [
                {"geometry": {"x": 0, "y": 0, "width": 100, "height": 80}},
            ],
        }
        service = CaptureService(
            context_provider=lambda: context,
            target_window_provider=lambda: saved,
        )
        desktop = Image.new("RGB", (100, 80), "white")

        with patch.object(service, "_capture_image", return_value=desktop):
            result = service.capture("active_window")

        self.assertEqual(result.size if hasattr(result, "size") else
                         (result.width, result.height), (50, 40))

    def test_active_window_fails_when_saved_target_is_missing(self):
        service = CaptureService(
            context_provider=lambda: {
                "success": True,
                "windows": [],
                "screens": [
                    {"geometry": {"x": 0, "y": 0, "width": 100, "height": 80}},
                ],
            },
            target_window_provider=lambda: {
                "success": True,
                "active_window": {"id": "gone"},
            },
        )
        with self.assertRaisesRegex(CaptureError, "no longer available"):
            service.capture("active_window")

    def test_active_window_fails_when_no_snapshot_exists(self):
        service = CaptureService(
            context_provider=lambda: {"success": True, "windows": [], "screens": []},
            target_window_provider=lambda: {
                "success": False,
                "message": "No active-window snapshot exists",
            },
        )
        with self.assertRaisesRegex(CaptureError, "No active-window snapshot"):
            service.capture("active_window")

    def test_active_window_fails_when_target_is_minimized(self):
        service = CaptureService(
            context_provider=lambda: {
                "success": True,
                "windows": [
                    {
                        "id": "original",
                        "minimized": True,
                        "geometry": {"x": 0, "y": 0, "width": 50, "height": 40},
                    },
                ],
                "screens": [
                    {"geometry": {"x": 0, "y": 0, "width": 100, "height": 80}},
                ],
            },
            target_window_provider=lambda: {
                "success": True,
                "active_window": {"id": "original"},
            },
        )
        with self.assertRaisesRegex(CaptureError, "minimized"):
            service.capture("active_window")

    def test_explicit_window_capture_returns_provenance(self):
        context = {
            "success": True,
            "windows": [
                {
                    "id": "win-1",
                    "title": "Helium",
                    "minimized": False,
                    "geometry": {"x": 10, "y": 5, "width": 40, "height": 30},
                },
            ],
            "screens": [
                {"geometry": {"x": 0, "y": 0, "width": 100, "height": 80}},
            ],
        }
        service = CaptureService(context_provider=lambda: context)
        desktop = Image.new("RGB", (100, 80), "white")
        desktop.putpixel((10, 5), (255, 0, 0))

        with patch.object(service, "_capture_image", return_value=desktop):
            result = service.capture("window", window_id="win-1")

        self.assertEqual(result.image.size, (40, 30))
        self.assertEqual(result.image.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(result.mode, "window")
        self.assertEqual(result.window_id, "win-1")
        self.assertEqual(result.window_title, "Helium")
        self.assertEqual(
            result.window_geometry,
            {"x": 10, "y": 5, "width": 40, "height": 30},
        )
        self.assertEqual(result.capture_kind, "visible_region")
        self.assertIsNone(result.screen_name)

    def test_explicit_window_capture_fails_when_target_is_missing(self):
        service = CaptureService(
            context_provider=lambda: {
                "success": True,
                "windows": [],
                "screens": [
                    {"geometry": {"x": 0, "y": 0, "width": 100, "height": 80}},
                ],
            },
        )

        with self.assertRaisesRegex(CaptureError, "no longer available"):
            service.capture("window", window_id="gone")

    def test_capture_modes_reject_contradictory_targets(self):
        service = CaptureService()

        with self.assertRaisesRegex(ValueError, "window_id is required"):
            service.capture("window")
        with self.assertRaisesRegex(ValueError, "screen_name is not valid"):
            service.capture("window", window_id="win-1", screen_name="DP-1")
        with self.assertRaisesRegex(ValueError, "only valid for window"):
            service.capture("desktop", window_id="win-1")
        with self.assertRaisesRegex(ValueError, "not valid for screen"):
            service.capture("screen", "DP-1", window_id="win-1")

    def test_screen_crop_uses_desktop_origin(self):
        context = {
            "success": True,
            "screens": [
                {
                    "name": "LEFT",
                    "geometry": {"x": -100, "y": 0, "width": 100, "height": 80},
                },
                {
                    "name": "RIGHT",
                    "geometry": {"x": 0, "y": 0, "width": 120, "height": 80},
                },
            ],
        }

        service = CaptureService(context_provider=lambda: context)

        with patch.object(
            service,
            "_capture_image",
            return_value=Image.new("RGB", (220, 80), "white"),
        ):
            result = service.capture("screen", "RIGHT")

        self.assertEqual(result.image.size, (120, 80))

    def test_screen_capture_rejects_missing_display(self):
        service = CaptureService(
            context_provider=lambda: {"success": True, "screens": []}
        )
        with self.assertRaises(CaptureError):
            service.capture("screen", "DP-1")

    def test_invalid_capture_mode_rejected(self):
        with self.assertRaises(ValueError):
            CaptureService().capture("continuous")


class OCRTests(unittest.TestCase):
    def test_parses_tesseract_tsv(self):
        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
            "left\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t96.5\tHello\n"
            "5\t1\t1\t1\t1\t2\t45\t20\t25\t12\t91.0\tworld\n"
            "5\t1\t1\t1\t2\t1\t10\t50\t20\t12\t88.0\tAgain\n"
        )

        class Result:
            returncode = 0
            stdout = tsv.encode()
            stderr = b""

        with patch(
            "app.desktop.vision.ocr.subprocess.run",
            return_value=Result(),
        ) as run:
            result = OCRService().recognize(Image.new("RGB", (100, 100)))

        self.assertEqual(result.text, "Hello world\nAgain")
        self.assertEqual(len(result.words), 3)
        self.assertEqual(result.words[0].left, 10)
        self.assertEqual(result.image_size, (100, 100))
        self.assertIn(b"PNG", run.call_args.kwargs["input"][:32])

    def test_rejects_oversized_image(self):
        service = OCRService(max_pixels=10)
        with self.assertRaises(ValueError):
            service.recognize(Image.new("RGB", (4, 4)))

    def test_rejects_non_image_input(self):
        with self.assertRaises(TypeError):
            OCRService().recognize("not an image")


if __name__ == "__main__":
    unittest.main()
