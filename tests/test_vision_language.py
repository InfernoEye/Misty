import unittest
import urllib.request

from PIL import Image

from app.brain.agent import BASE_URL, MODEL_PATH
from app.desktop.vision.vlm import (
    VisionLanguageError,
    VisionLanguageService,
)


def _server_up():
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def _service(**overrides):
    options = {"base_url": BASE_URL, "model_name": MODEL_PATH.name}
    options.update(overrides)
    return VisionLanguageService(**options)


class VisionValidationTests(unittest.TestCase):
    def setUp(self):
        self.service = _service(timeout=5)

    def test_rejects_non_image(self):
        with self.assertRaises(TypeError):
            self.service.analyze("not an image", "Describe.")

    def test_rejects_empty_prompt(self):
        with self.assertRaises(ValueError):
            self.service.analyze(Image.new("RGB", (10, 10)), "  ")

    def test_rejects_oversized_prompt(self):
        service = _service(max_prompt_chars=5)
        with self.assertRaises(ValueError):
            service.analyze(Image.new("RGB", (10, 10)), "Too long")

    def test_rejects_invalid_token_limit(self):
        with self.assertRaises(ValueError):
            self.service.analyze(Image.new("RGB", (10, 10)), "Describe.", max_tokens=0)

    def test_rejects_oversized_image(self):
        service = _service(max_pixels=10)
        with self.assertRaises(ValueError):
            service.analyze(Image.new("RGB", (4, 4)), "Describe.")


@unittest.skipUnless(_server_up(), "shared local model server is not running on BASE_URL")
class VisionServerTests(unittest.TestCase):
    def test_describes_a_real_image_through_the_shared_server(self):
        result = _service(timeout=120).analyze(
            Image.new("RGB", (100, 80), (255, 0, 0)),
            "What colour is this image? Answer with one word.",
            max_tokens=16,
        )

        self.assertIn("red", result.text.lower())
        self.assertEqual(result.image_size, (100, 80))
        self.assertEqual(result.model, MODEL_PATH.name)
        self.assertGreater(result.elapsed_seconds, 0)

    def test_unreachable_server_is_reported_as_vision_error(self):
        service = _service(base_url="http://127.0.0.1:1", timeout=2)
        with self.assertRaises(VisionLanguageError):
            service.analyze(Image.new("RGB", (10, 10)), "Describe.")


if __name__ == "__main__":
    unittest.main()
