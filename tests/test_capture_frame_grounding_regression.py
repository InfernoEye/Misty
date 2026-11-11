"""Capture-to-input contract tests: all images and input backends are fake."""
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.desktop.vision.capture import CaptureService, CaptureError
from app.desktop.vision.coordinator import DesktopVisionResult
from app.brain.tools import ToolRegistry, DESKTOP_CLICK_DEFINITION, DESKTOP_MOVE_POINTER_DEFINITION


def context():
    return {"success": True, "screens": [
        {"name": "left", "geometry": {"x": -100, "y": -20, "width": 100, "height": 100}},
        {"name": "right", "geometry": {"x": 0, "y": -20, "width": 100, "height": 100}},
    ], "windows": [{"id": "win", "geometry": {"x": -80, "y": 0, "width": 40, "height": 30}}]}


class FakeCapture(CaptureService):
    def __init__(self, state=None, size=(200, 100), **kwargs):
        self.state = state if state is not None else context()
        self.size = size
        super().__init__(context_provider=lambda: deepcopy(self.state), **kwargs)

    def _capture_image(self, options):
        return Image.new("RGB", self.size)


class CaptureFrameGroundingTests(unittest.TestCase):
    def test_desktop_negative_origin(self):
        service = FakeCapture()
        result = service.capture()
        self.assertEqual(service.resolve_input_point(result.frame["frame_id"], 10, 5), (-90, -15))
        self.assertEqual(result.frame["image_to_input"], {
            "origin_x": -100, "origin_y": -20, "scale_x": 1.0, "scale_y": 1.0,
        })
        json.dumps(result.frame)
        self.assertEqual(result.frame["screens"][0]["geometry"]["x"], -100)
        self.assertEqual(DesktopVisionResult(result, None, None).frame, result.frame)

    def test_screen_crop_coordinates_are_not_global(self):
        service = FakeCapture()
        left = service.capture(mode="screen", screen_name="left")
        right = service.capture(mode="screen", screen_name="right")
        self.assertEqual(left.image.size, (100, 100))
        self.assertEqual(service.resolve_input_point(left.frame["frame_id"], 10, 10), (-90, -10))
        self.assertEqual(service.resolve_input_point(right.frame["frame_id"], 10, 10), (10, -10))

    def test_window_crop_origin_and_window_movement(self):
        service = FakeCapture()
        result = service.capture(mode="window", window_id="win")
        self.assertEqual(result.image.size, (40, 30))
        self.assertEqual(service.resolve_input_point(result.frame["frame_id"], 5, 6), (-75, 6))
        service.state["windows"][0]["geometry"]["x"] += 1
        with self.assertRaisesRegex(CaptureError, "window moved"):
            service.resolve_input_point(result.frame["frame_id"], 5, 6)

    def test_ambiguous_scale_does_not_guess(self):
        service = FakeCapture(size=(400, 200))
        result = service.capture()
        self.assertFalse(result.frame["grounded"])
        with self.assertRaises(CaptureError):
            service.resolve_input_point(result.frame["frame_id"], 1, 1)
        with self.assertRaisesRegex(CaptureError, "scaling is ambiguous"):
            service.capture(mode="screen", screen_name="left")
        service = FakeCapture()
        service.state["screens"][0]["scale"] = 1.25
        self.assertFalse(service.capture().frame["grounded"])

    def test_bounds_expiry_unknown_and_layout_changes(self):
        service = FakeCapture()
        result = service.capture()
        frame_id = result.frame["frame_id"]
        for x, y in [(-1, 0), (200, 0), (0, 100), (True, 0)]:
            with self.assertRaises(CaptureError):
                service.resolve_input_point(frame_id, x, y)
        with self.assertRaises(CaptureError):
            service.resolve_input_point("unknown", 0, 0)
        with self.assertRaisesRegex(CaptureError, "expired"):
            service.resolve_input_point(frame_id, 0, 0, max_age_seconds=-1)
        service.state["screens"][0]["geometry"]["x"] -= 1
        with self.assertRaisesRegex(CaptureError, "layout changed"):
            service.resolve_input_point(frame_id, 0, 0)

    def test_desktop_gap_rejected_without_window_service(self):
        state = context()
        state["screens"][1]["geometry"]["x"] = 100
        service = FakeCapture(state=state, size=(300, 100))
        frame = service.capture().frame
        with self.assertRaisesRegex(CaptureError, "gap"):
            service.resolve_input_point(frame["frame_id"], 150, 30)

    def test_returned_frame_cannot_mutate_trusted_transform(self):
        service = FakeCapture()
        frame = service.capture().frame
        frame["image_to_input"]["origin_x"] = 999
        self.assertEqual(service.resolve_input_point(frame["frame_id"], 0, 0), (-100, -20))

    def test_native_window_capture_is_not_grounded_from_outer_geometry(self):
        from io import BytesIO
        buffer = BytesIO()
        Image.new("RGB", (40, 30)).save(buffer, format="PNG")
        native = SimpleNamespace(capture_window=lambda window_id: {
            "success": True, "image_bytes": buffer.getvalue(), "window_id": window_id,
        })
        service = FakeCapture(kwin_capture=native)
        result = service.capture(mode="window", window_id="win")
        self.assertEqual(result.capture_kind, "native_window")
        self.assertFalse(result.frame["grounded"])
        with self.assertRaisesRegex(CaptureError, "decoration"):
            service.resolve_input_point(result.frame["frame_id"], 1, 1)


class FakeBackend:
    def __init__(self):
        self.calls = []

    def execute(self, action):
        self.calls.append(action)
        return {"success": True}


class FramePointerDispatchTests(unittest.TestCase):
    def setUp(self):
        self.capture = FakeCapture()
        self.backend = FakeBackend()
        self.registry = ToolRegistry(input_backend=self.backend,
                                     vision_service=SimpleNamespace(capture_service=self.capture))

    def test_frame_transform_happens_once_before_input(self):
        frame = self.capture.capture(mode="screen", screen_name="left").frame
        with patch.object(self.registry, "_coordinate_problem", return_value=None):
            result = self.registry.dispatch_desktop_tool("desktop_click", {
                "x": 10, "y": 20, "frame_id": frame["frame_id"],
            }, confirmed=True)
        self.assertTrue(result["success"], result)
        self.assertEqual(self.backend.calls[0].to_dict()["fields"]["x"], -90)
        self.assertEqual(self.backend.calls[0].to_dict()["fields"]["y"], 0)

    def test_approval_not_bypassed(self):
        frame = self.capture.capture().frame
        result = self.registry.dispatch_desktop_tool("desktop_click", {
            "x": 1, "y": 1, "frame_id": frame["frame_id"],
        })
        self.assertEqual(result["code"], "confirmation_required")
        self.assertEqual(self.backend.calls, [])

    def test_unknown_frame_and_screen_parameter_rejected_without_input(self):
        for arguments in ({"x": 1, "y": 1, "frame_id": "fake"},
                          {"x": 1, "y": 1, "screen": "left"}):
            result = self.registry.dispatch_desktop_tool("desktop_click", arguments, confirmed=True)
            self.assertFalse(result["success"])
            self.assertTrue(result["not_executed"])
        self.assertEqual(self.backend.calls, [])

    def test_global_signed_coordinates_preserved_and_schema_matches(self):
        with patch.object(self.registry, "_coordinate_problem", return_value=None):
            result = self.registry.dispatch_desktop_tool("desktop_move_pointer", {"x": -5, "y": -10}, confirmed=True)
        self.assertTrue(result["success"])
        for definition in (DESKTOP_CLICK_DEFINITION, DESKTOP_MOVE_POINTER_DEFINITION):
            properties = definition["function"]["parameters"]["properties"]
            self.assertNotIn("minimum", properties["x"])
            self.assertNotIn("minimum", properties["y"])
            self.assertIn("frame_id", properties)


if __name__ == "__main__":
    unittest.main()
