"""Native KWin pilot tests: synthetic captures/OCR and an in-memory backend only."""
from copy import deepcopy
import hashlib
from io import BytesIO
from types import SimpleNamespace
import threading
import time
import unittest

from PIL import Image

from app.desktop.providers.base import DesktopProviderError
from app.desktop.providers.kwin import KWinDesktopProvider
from app.desktop.vision.capture import CaptureService
from app.desktop.vision.ocr import OCRResult, OCRWord


def context():
    return {"success": True, "windows": [{"id": "target", "pid": 123, "title": "private-title",
        "active": True, "minimized": False, "on_current_desktop": True,
        "geometry": {"x": -90, "y": -10, "width": 50, "height": 40}}],
        "screens": [{"name": "left", "geometry": {"x": -100, "y": -20, "width": 100, "height": 100}},
                    {"name": "right", "geometry": {"x": 0, "y": -20, "width": 100, "height": 100}}],
        "overlays": []}


class FakeNative:
    def __init__(self):
        self.calls = 0
    def capture_window(self, window_id):
        self.calls += 1
        stream = BytesIO()
        Image.new("RGB", (50, 40), "red").save(stream, format="PNG")
        return {"success": True, "window_id": window_id, "image_bytes": stream.getvalue()}


class FakeCapture(CaptureService):
    def __init__(self, state):
        self.state = state
        self.calls = []
        self.fullscreen = Image.new("RGB", (200, 100), "white")
        self.after_capture = None
        self.resolutions = 0
        self.native = FakeNative()
        super().__init__(context_provider=lambda: self.state, kwin_capture=self.native)
    def _capture_image(self, options):
        self.calls.append(options)
        result = self.fullscreen.copy()
        if self.after_capture:
            self.after_capture()
        return result
    def resolve_input_point(self, *args, **kwargs):
        self.resolutions += 1
        return super().resolve_input_point(*args, **kwargs)


class FakeOCR:
    def __init__(self):
        self.calls = 0
        self.after = None
    def recognize(self, image):
        self.calls += 1
        if self.after:
            self.after()
        return OCRResult("Dragon Cave", (OCRWord("Dragon", 90, 5, 6, 12, 8),
            OCRWord("Cave", 90, 20, 6, 10, 8)), "en", image.size)


class FakeInput:
    def __init__(self):
        self.calls = []
        self.failure = None
    def execute(self, action):
        self.calls.append(action)
        if self.failure:
            raise RuntimeError("private backend failure")
        return {"success": True, "verified": True}


class NativeProviderTests(unittest.TestCase):
    def setUp(self):
        self.state = context()
        self.capture = FakeCapture(self.state)
        self.ocr = FakeOCR()
        self.backend = FakeInput()
        self.vision = SimpleNamespace(capture_service=self.capture, ocr_service=self.ocr,
                                      inspect=lambda **kw: self.fail("VLM coordinator must not be called"))
        self.registry = SimpleNamespace(vision_service=self.vision, input_backend=self.backend)
        self.provider = KWinDesktopProvider(self.registry)

    def observe(self):
        return self.provider.observe("target")

    def click(self, observation, **kwargs):
        return self.provider.click(observation.observation_id,
            observation.elements[0]["element_token"], **kwargs)

    def assert_refused(self, result, code=None):
        self.assertFalse(result["success"])
        self.assertTrue(result["not_executed"])
        self.assertFalse(result["verified"])
        if code:
            self.assertEqual(result["code"], code)
        self.assertEqual(self.backend.calls, [])

    def test_capture_default_native_and_explicit_visible_crop_are_distinct(self):
        native = self.capture.capture(mode="window", window_id="target")
        self.assertEqual(native.capture_kind, "native_window")
        self.assertFalse(native.frame["grounded"])
        cropped = self.capture.capture(mode="window", window_id="target", visible_region_only=True)
        self.assertEqual(cropped.capture_kind, "visible_region")
        self.assertTrue(cropped.frame["grounded"])
        self.assertEqual(cropped.image.size, (50, 40))
        self.assertEqual(self.capture.native.calls, 1)
        self.assertEqual(self.capture.resolve_input_point(cropped.frame["frame_id"], 10, 10), (-80, 0))
        for mode in ("desktop", "screen", "active_window"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.capture.capture(mode=mode, visible_region_only=True)
        with self.assertRaises(ValueError):
            self.capture.capture(mode="window", window_id="target", visible_region_only=1)

    def test_observation_is_png_and_contains_bounded_word_tokens_without_vlm(self):
        observed = self.observe()
        self.assertEqual(observed.target["pid"], 123)
        self.assertEqual(Image.open(BytesIO(observed.image)).format, "PNG")
        self.assertEqual(observed.digest, hashlib.sha256(observed.image).hexdigest())
        self.assertEqual([item["text"] for item in observed.elements], ["Dragon", "Cave"])
        self.assertEqual(self.capture.native.calls, 0)
        self.assertEqual(self.ocr.calls, 1)
        self.assertEqual(self.backend.calls, [])

    def test_literal_host_approval_required(self):
        observed = self.observe()
        for approved in (False, 1, "true", None):
            with self.subTest(approved=approved):
                self.assert_refused(self.click(observed, approved=approved), "approval_required")
        result = self.click(observed, approved=True)
        self.assertTrue(result["success"])
        self.assertFalse(result["verified"])

    def test_no_actual_input_backend_refuses(self):
        observed = self.observe()
        self.registry.input_backend = None
        self.assert_refused(self.click(observed, approved=True), "no_input_backend")

    def test_single_left_click_maps_trusted_negative_origin_exactly_once(self):
        observed = self.observe()
        result = self.click(observed, approved=True)
        self.assertTrue(result["success"])
        self.assertFalse(result["not_executed"])
        self.assertFalse(result["verified"])
        self.assertEqual(self.capture.resolutions, 1)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.backend.calls[0].to_dict(), {"kind": "pointer_click",
            "fields": {"x": -79, "y": 0, "button": "left", "count": 1, "screen": ""}})
        replay = self.click(observed, approved=True)
        self.assertEqual(replay["code"], "stale_token")
        self.assertEqual(len(self.backend.calls), 1)

    def test_expired_original_frame_is_replaced_by_identical_fresh_capture(self):
        observed = self.observe()
        original_frame, layout, _created = self.capture._frames[observed.frame_id]
        self.capture._frames[observed.frame_id] = (original_frame, layout, time.monotonic() - 121)
        resolved_frames = []
        original_resolver = self.capture.resolve_input_point
        def resolve(frame_id, x, y, **kwargs):
            resolved_frames.append(frame_id)
            return original_resolver(frame_id, x, y, **kwargs)
        self.capture.resolve_input_point = resolve
        result = self.click(observed, approved=True)
        self.assertTrue(result["success"])
        self.assertEqual(len(resolved_frames), 1)
        self.assertNotEqual(resolved_frames[0], observed.frame_id)
        self.assertEqual(self.capture.resolutions, 1)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.backend.calls[0].fields["x"], -79)
        self.assertEqual(self.backend.calls[0].fields["y"], 0)

    def test_changed_screen_identity_refuses_even_with_same_geometry_and_image(self):
        self.state["windows"][0]["screen"] = "left"
        observed = self.observe()
        self.state["windows"][0]["screen"] = "right"
        self.assert_refused(self.click(observed, approved=True), "stale_target")
        self.assertEqual(self.capture.resolutions, 0)

    def test_returned_metadata_cannot_rewrite_private_target_or_center(self):
        observed = self.observe()
        token = observed.elements[0]["element_token"]
        observed.target["pid"] = 999
        observed.target["geometry"]["x"] = 999
        observed.elements[0].update(x=999, y=999, element_token="forged")
        result = self.provider.click(observed.observation_id, token, approved=True)
        self.assertTrue(result["success"])
        self.assertEqual(self.backend.calls[0].fields["x"], -79)

    def test_new_external_observation_invalidates_old_even_if_new_one_fails(self):
        original = self.observe()
        current = self.observe()
        self.assert_refused(self.click(original, approved=True), "stale_token")
        with self.assertRaises(DesktopProviderError):
            self.provider.observe("missing")
        self.assert_refused(self.click(current, approved=True), "stale_token")

    def test_changed_pid_geometry_visibility_or_focus_refuses(self):
        for field, value, code in (("pid", 124, "stale_target"),
            ("geometry", {"x": -89,"y":-10,"width":50,"height":40}, "stale_target"),
            ("minimized", True, "target_not_visible"),
            ("on_current_desktop", False, "target_not_visible"),
            ("active", False, "target_not_active")):
            with self.subTest(field=field):
                self.state["windows"] = deepcopy(context()["windows"])
                observed = self.observe()
                self.state["windows"][0][field] = value
                self.assert_refused(self.click(observed, approved=True), code)

    def test_visible_foreign_overlap_and_overlay_refuse(self):
        for collection in ("windows", "overlays"):
            with self.subTest(collection=collection):
                self.state = context()
                self.capture.state = self.state
                observed = self.observe()
                other = deepcopy(self.state["windows"][0])
                other.update(id="foreign", pid=456, active=False)
                self.state[collection].append(other)
                self.assert_refused(self.click(observed, approved=True), "target_occluded")

    def test_changed_image_refuses_before_coordinate_resolution(self):
        observed = self.observe()
        self.capture.fullscreen.putpixel((20, 20), (0, 0, 0))
        self.assert_refused(self.click(observed, approved=True), "stale_image")
        self.assertEqual(self.capture.resolutions, 0)

    def test_final_focus_recheck_after_coordinate_resolution_refuses(self):
        observed = self.observe()
        resolver = self.capture.resolve_input_point
        def resolve(*args, **kwargs):
            result = resolver(*args, **kwargs)
            self.state["windows"][0]["active"] = False
            return result
        self.capture.resolve_input_point = resolve
        self.assert_refused(self.click(observed, approved=True), "target_not_active")

    def test_cancellation_and_expiry_prevent_capture_ocr_and_input(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(DesktopProviderError):
            self.provider.observe("target", cancel=cancel)
        self.assertEqual(self.capture.calls, [])
        cancel.clear()
        self.capture.after_capture = cancel.set
        with self.assertRaises(DesktopProviderError):
            self.provider.observe("target", cancel=cancel)
        self.assertEqual(self.ocr.calls, 0)
        self.capture.after_capture = None
        cancel.clear()
        observed = self.observe()
        cancel.set()
        self.assert_refused(self.click(observed, approved=True, cancel=cancel), "canceled")
        observed = self.observe()
        self.assert_refused(self.click(observed, approved=True, deadline=time.monotonic()-1), "timeout")

    def test_cancellation_after_ocr_invalidates_observation(self):
        cancel = threading.Event()
        self.ocr.after = cancel.set
        with self.assertRaises(DesktopProviderError) as error:
            self.provider.observe("target", cancel=cancel)
        self.assertEqual(error.exception.code, "canceled")
        self.assertEqual(self.backend.calls, [])

    def test_uncertain_backend_failure_consumes_whole_observation(self):
        observed = self.observe()
        self.backend.failure = True
        result = self.click(observed, approved=True)
        self.assertEqual(result, {"success": False, "not_executed": False,
                                 "code": "input_outcome_unknown", "verified": False})
        self.assertEqual(self.click(observed, approved=True)["code"], "stale_token")
        self.assertEqual(len(self.backend.calls), 1)

    def test_inactive_unoccluded_observation_is_allowed_but_input_is_not(self):
        self.state["windows"][0]["active"] = False
        observed = self.observe()
        self.assertFalse(observed.target["active"])
        self.assert_refused(self.click(observed, approved=True), "target_not_active")

    def test_cancellation_after_resolution_prevents_input(self):
        observed = self.observe()
        cancel = threading.Event()
        original = self.capture.resolve_input_point
        def resolve(*args, **kwargs):
            result = original(*args, **kwargs)
            cancel.set()
            return result
        self.capture.resolve_input_point = resolve
        self.assert_refused(self.click(observed, approved=True, cancel=cancel), "canceled")

    def test_bad_word_bounds_are_not_minted_as_tokens(self):
        self.ocr.recognize = lambda image: OCRResult("private", (
            OCRWord("negative", 90, -1, 0, 5, 5),
            OCRWord("outside", 90, 49, 0, 5, 5),
            OCRWord("boolean", 90, True, 0, 5, 5),
            OCRWord("valid", 90, 0, 0, 5, 5)), "en", image.size)
        self.assertEqual([item["text"] for item in self.observe().elements], ["valid"])

    def test_invalid_context_and_copied_discovery(self):
        discovered = self.provider.discover()
        discovered["windows"][0]["pid"] = 999
        self.assertEqual(self.state["windows"][0]["pid"], 123)
        self.state["windows"][0]["pid"] = True
        with self.assertRaises(DesktopProviderError) as error:
            self.provider.discover()
        self.assertEqual(str(error.exception), "Native KWin operation could not be admitted.")

    def test_executor_context_fallback_is_bound_to_capture_frame_source(self):
        self.capture.context_provider = None
        class WindowService:
            def list_windows(inner):
                return {"success": True, "windows": self.state["windows"], "overlays": []}
            def list_screens(inner):
                return {"success": True, "screens": self.state["screens"]}
        self.registry.executor = SimpleNamespace(window_service=WindowService())
        provider = KWinDesktopProvider(self.registry)
        observed = provider.observe("target")
        self.assertEqual(observed.target["pid"], 123)
        self.assertTrue(provider.click(observed.observation_id,
            observed.elements[0]["element_token"], approved=True)["success"])


if __name__ == "__main__":
    unittest.main()
