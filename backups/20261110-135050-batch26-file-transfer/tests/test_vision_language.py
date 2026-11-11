import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.desktop.vision.vlm import (
    VisionLanguageError,
    VisionLanguageService,
)


class VisionLanguageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)

        self.cli = root / "llama-mtmd-cli"
        self.model = root / "model.gguf"
        self.projector = root / "mmproj.gguf"

        for path in (self.cli, self.model, self.projector):
            path.write_bytes(b"test")

        self.service = VisionLanguageService(
            cli_path=self.cli,
            model_path=self.model,
            projector_path=self.projector,
        )

    def tearDown(self):
        self.directory.cleanup()

    def fake_success(self, command, **kwargs):
        image_path = Path(command[command.index("--image") + 1])
        self.assertTrue(image_path.is_file())
        with Image.open(image_path) as test_image:
            self.assertEqual(test_image.size, (100, 80))

        class Result:
            returncode = 0
            stdout = "A test image containing a red circle."
            stderr = ""

        return Result()

    def test_successful_inference(self):
        with patch(
            "app.desktop.vision.vlm.subprocess.run",
            side_effect=self.fake_success,
        ) as run:
            result = self.service.analyze(
                Image.new("RGB", (100, 80)),
                "Describe this image.",
            )

        self.assertIn("red circle", result.text)
        self.assertEqual(result.image_size, (100, 80))
        self.assertEqual(result.model, "model.gguf")

        command = run.call_args.args[0]
        self.assertIn("Vulkan0", command)
        self.assertIn("1024", command)
        self.assertEqual(command[command.index("-ngl") + 1], "99")
        self.assertEqual(command[command.index("-c") + 1], "16384")
        self.assertEqual(self.service.context_size, 16384)
        self.assertEqual(self.service.timeout, 180.0)

    def test_temporary_image_is_removed_after_inference(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["path"] = Path(command[command.index("--image") + 1])

            class Result:
                returncode = 0
                stdout = "Done."
                stderr = ""

            return Result()

        with patch(
            "app.desktop.vision.vlm.subprocess.run",
            side_effect=fake_run,
        ):
            self.service.analyze(Image.new("RGB", (20, 20)), "Describe.")

        self.assertFalse(captured["path"].exists())

    def test_rejects_non_image(self):
        with self.assertRaises(TypeError):
            self.service.analyze("not an image", "Describe.")

    def test_rejects_empty_prompt(self):
        with self.assertRaises(ValueError):
            self.service.analyze(Image.new("RGB", (10, 10)), "  ")

    def test_rejects_oversized_prompt(self):
        service = VisionLanguageService(
            cli_path=self.cli,
            model_path=self.model,
            projector_path=self.projector,
            max_prompt_chars=5,
        )
        with self.assertRaises(ValueError):
            service.analyze(Image.new("RGB", (10, 10)), "Too long")

    def test_rejects_invalid_token_limit(self):
        with self.assertRaises(ValueError):
            self.service.analyze(
                Image.new("RGB", (10, 10)), "Describe.", max_tokens=0
            )

    def test_rejects_oversized_image(self):
        service = VisionLanguageService(
            cli_path=self.cli,
            model_path=self.model,
            projector_path=self.projector,
            max_pixels=10,
        )
        with self.assertRaises(ValueError):
            service.analyze(Image.new("RGB", (4, 4)), "Describe.")

    def test_missing_model_fails_safely(self):
        service = VisionLanguageService(
            cli_path=self.cli,
            model_path=self.model.parent / "missing.gguf",
            projector_path=self.projector,
        )
        with self.assertRaises(VisionLanguageError):
            service.analyze(Image.new("RGB", (10, 10)), "Describe.")

    def test_timeout_is_reported(self):
        from subprocess import TimeoutExpired

        with patch(
            "app.desktop.vision.vlm.subprocess.run",
            side_effect=TimeoutExpired("llama-mtmd-cli", 1),
        ):
            with self.assertRaisesRegex(VisionLanguageError, "timed out"):
                self.service.analyze(Image.new("RGB", (10, 10)), "Describe.")

    def test_nonzero_exit_is_reported(self):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "model error"

        with patch(
            "app.desktop.vision.vlm.subprocess.run",
            return_value=Result(),
        ):
            with self.assertRaisesRegex(VisionLanguageError, "model error"):
                self.service.analyze(Image.new("RGB", (10, 10)), "Describe.")

    def test_empty_response_is_rejected(self):
        class Result:
            returncode = 0
            stdout = "   "
            stderr = ""

        with patch(
            "app.desktop.vision.vlm.subprocess.run",
            return_value=Result(),
        ):
            with self.assertRaisesRegex(VisionLanguageError, "empty response"):
                self.service.analyze(Image.new("RGB", (10, 10)), "Describe.")

    def test_oversized_response_is_rejected(self):
        service = VisionLanguageService(
            cli_path=self.cli,
            model_path=self.model,
            projector_path=self.projector,
            max_output_chars=5,
        )

        class Result:
            returncode = 0
            stdout = "This is too long."
            stderr = ""

        with patch(
            "app.desktop.vision.vlm.subprocess.run",
            return_value=Result(),
        ):
            with self.assertRaisesRegex(VisionLanguageError, "exceeds"):
                service.analyze(Image.new("RGB", (10, 10)), "Describe.")


if __name__ == "__main__":
    unittest.main()
