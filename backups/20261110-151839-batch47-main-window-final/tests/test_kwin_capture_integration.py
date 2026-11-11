import io
import unittest

from PIL import Image

from app.desktop.vision.capture import CaptureError, CaptureService


class FakeKWin:
    def __init__(self, result):
        self.result = result

    def capture_window(self, window_id):
        return dict(self.result)


def png_bytes(width=4, height=3):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format="PNG")
    return buffer.getvalue()


class KWinCaptureIntegrationTests(unittest.TestCase):
    def test_direct_capture_bypasses_spectacle(self):
        service = CaptureService(
            kwin_capture=FakeKWin({
                "success": True,
                "code": "ok",
                "image_bytes": png_bytes(),
                "width": 4,
                "height": 3,
                "window_id": "abc",
            })
        )

        result = service.capture(mode="window", window_id="abc")

        self.assertEqual((result.width, result.height), (4, 3))
        self.assertEqual(result.window_id, "abc")
        self.assertEqual(result.masked_overlays, ())

    def test_invalid_window_is_not_cropped_as_fallback(self):
        service = CaptureService(
            kwin_capture=FakeKWin({
                "success": False,
                "code": "invalid_window",
                "message": "gone",
            })
        )

        with self.assertRaises(CaptureError):
            service.capture(mode="window", window_id="abc")

    def test_invalid_image_is_rejected(self):
        service = CaptureService(
            kwin_capture=FakeKWin({
                "success": True,
                "image_bytes": b"not a png",
                "window_id": "abc",
            })
        )

        with self.assertRaises(CaptureError):
            service.capture(mode="window", window_id="abc")


if __name__ == "__main__":
    unittest.main()
