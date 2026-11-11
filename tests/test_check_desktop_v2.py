"""Content-free Cua diagnostic tests; no native tools, input, or models run."""
import base64
import contextlib
import copy
from io import BytesIO, StringIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from app.desktop.providers.base import DesktopProviderError, ToolOutcome
from scripts.check_desktop_v2 import main, probe


PRIVATE = "PRIVATE-WINDOW-TITLE-DO-NOT-PRINT"


def png_block(size=(4, 3)):
    stream = BytesIO()
    Image.new("RGB", size, (20, 40, 60)).save(stream, format="PNG")
    return {"type": "image", "mimeType": "image/png",
            "data": base64.b64encode(stream.getvalue()).decode("ascii")}


def outcome(tool, structured, content=None, is_error=False):
    return ToolOutcome(tool, {"content": content if content is not None else [],
                             "structuredContent": structured}, "test-generation", is_error)


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.close_stopped = True
        self.close_error = None
        self.start_error = None
        self.call_error = None
        self.window_result = outcome("list_windows", {"windows": [
            {"pid": 123, "window_id": 456, "is_on_screen": True, "title": PRIVATE},
        ]})
        self.snapshot_result = outcome("get_window_state", {
            "pid": 123, "window_id": 456,
            "screenshot_width": 4, "screenshot_height": 3,
            "screenshot_mime_type": "image/png", "window_title": PRIVATE,
            "snapshot_id": "opaque", "screenshot_frame_valid": True,
        }, [{"type": "text", "text": PRIVATE}, png_block()])

    def start(self, **kwargs):
        if self.start_error:
            raise self.start_error
        return SimpleNamespace(available=True, version="0.28.0")

    def catalog(self):
        return [{"name": name} for name in ("list_apps", "list_windows", "get_window_state")]

    def call(self, name, arguments, **kwargs):
        self.calls.append((name, copy.deepcopy(arguments), kwargs))
        if self.call_error:
            raise self.call_error
        if name == "list_windows":
            return self.window_result
        if name == "get_window_state":
            return self.snapshot_result
        raise AssertionError("Diagnostic attempted an unreviewed tool")

    def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error
        return SimpleNamespace(stopped=self.close_stopped)


class DiagnosticTests(unittest.TestCase):
    def run_probe(self, provider=None, **kwargs):
        provider = provider or FakeProvider()
        result = probe(provider_factory=lambda: provider, **kwargs)
        self.assertTrue(provider.closed)
        self.assertFalse(result[0]["input_ready"])
        self.assertFalse(result[0]["platform_verified"])
        self.assertNotIn(PRIVATE, json.dumps(result))
        return provider, result[0], result[1]

    def test_default_only_catalog_and_visible_window_discovery(self):
        provider, summary, code = self.run_probe()
        self.assertEqual(code, 0)
        self.assertEqual(summary["phase_gate"], "discovery_only_snapshot_not_checked")
        self.assertFalse(summary["observation_ready"])
        self.assertEqual([(name, args) for name, args, _ in provider.calls], [
            ("list_windows", {"on_screen_only": True})])
        self.assertEqual(summary["visible_window_count"], 1)
        self.assertTrue(summary["cleanup_confirmed"])

    def test_no_windows_is_unverified_not_host_unsupported(self):
        provider = FakeProvider()
        provider.window_result = outcome("list_windows", {"windows": []})
        _, summary, code = self.run_probe(provider)
        self.assertEqual(code, 2)
        self.assertEqual(summary["phase_gate"], "no_visible_windows")

    def test_exact_snapshot_is_bounded_decoded_and_never_input_ready(self):
        provider, summary, code = self.run_probe(pid=123, window_id=456)
        self.assertEqual(code, 0)
        self.assertEqual(summary["phase_gate"], "observation_only_input_disabled")
        self.assertTrue(summary["observation_ready"])
        self.assertTrue(summary["snapshot_target_matches"])
        self.assertTrue(summary["image_decoded"])
        self.assertEqual(provider.calls[1][1], {
            "pid": 123, "window_id": 456, "include_accessibility_tree": True,
            "include_screenshot": True, "max_elements": 100, "max_depth": 8})
        self.assertTrue(all("deadline" in kw and "cancel" in kw for _, _, kw in provider.calls))
        self.assertNotIn("screenshot_out_file", provider.calls[1][1])

    def test_absent_or_duplicate_discovery_target_does_not_capture(self):
        for windows in ([{"pid": 123, "window_id": 999}],
                        [{"pid": 123, "window_id": 456, "is_on_screen": False}],
                        [{"pid": True, "window_id": 456}],
                        [{"pid": 123, "window_id": 456}] * 2):
            with self.subTest(windows=windows):
                provider = FakeProvider()
                provider.window_result = outcome("list_windows", {"windows": windows})
                provider, summary, code = self.run_probe(provider, pid=123, window_id=456)
                self.assertEqual(code, 2)
                self.assertEqual(summary["phase_gate"], "exact_visible_target_not_found")
                self.assertEqual(len(provider.calls), 1)

    def test_snapshot_requires_exact_integer_target_metadata(self):
        for structured in ({}, {"pid": 123, "window_id": "456"},
                           {"pid": 123, "window_id": 999},
                           {"pid": True, "window_id": 456}):
            with self.subTest(structured=structured):
                provider = FakeProvider()
                provider.snapshot_result = outcome("get_window_state", structured, [png_block()])
                _, summary, code = self.run_probe(provider, pid=123, window_id=456)
                self.assertEqual(code, 2)
                self.assertFalse(summary["observation_ready"])
                self.assertEqual(summary["phase_gate"], "snapshot_target_mismatch")

    def test_wayland_surface_refusal_is_preserved_without_private_prose(self):
        provider = FakeProvider()
        provider.snapshot_result.raw["structuredContent"].update({
            "screenshot_frame_valid": False,
            "screenshot_error": {"code": "surface_identity_unproven", "message": PRIVATE},
        })
        _, summary, code = self.run_probe(provider, pid=123, window_id=456)
        self.assertEqual(code, 2)
        self.assertEqual(summary["screenshot_error"], "surface_identity_unproven")
        self.assertEqual(summary["phase_gate"], "snapshot_not_grounded")

    def test_arbitrary_screenshot_errors_are_not_echoed(self):
        for error in ({"code": PRIVATE}, PRIVATE):
            with self.subTest(error_type=type(error).__name__):
                provider = FakeProvider()
                provider.snapshot_result.raw["structuredContent"]["screenshot_error"] = error
                _, summary, code = self.run_probe(provider, pid=123, window_id=456)
                self.assertEqual(code, 2)
                self.assertEqual(summary["screenshot_error"], "reported_screenshot_error")

    def test_missing_image_is_not_visual_readiness(self):
        provider = FakeProvider()
        provider.snapshot_result.raw["content"] = [{"type": "text", "text": PRIVATE}]
        _, summary, code = self.run_probe(provider, pid=123, window_id=456)
        self.assertEqual(code, 2)
        self.assertFalse(summary["image_block_present"])
        self.assertEqual(summary["phase_gate"], "snapshot_image_missing")

    def test_invalid_base64_bytes_mime_or_dimensions_cannot_pass(self):
        bad_blocks = [
            {"type": "image", "mimeType": "image/png", "data": "not base64!"},
            {"type": "image", "mimeType": "image/png", "data": base64.b64encode(b"not an image").decode()},
            {**png_block(), "mimeType": "image/jpeg"},
            {**png_block(), "mimeType": "image/svg+xml"},
        ]
        for block in bad_blocks:
            with self.subTest(mime=block["mimeType"]):
                provider = FakeProvider()
                provider.snapshot_result.raw["content"] = [block]
                _, summary, code = self.run_probe(provider, pid=123, window_id=456)
                self.assertEqual(code, 2)
                self.assertFalse(summary["image_decoded"])
                self.assertEqual(summary["phase_gate"], "snapshot_image_invalid")
        for width in (5, True, "4"):
            provider = FakeProvider()
            provider.snapshot_result.raw["structuredContent"]["screenshot_width"] = width
            _, summary, code = self.run_probe(provider, pid=123, window_id=456)
            self.assertEqual(code, 2)
            self.assertEqual(summary["phase_gate"], "snapshot_image_invalid")

    def test_snapshot_reported_mime_or_validity_must_be_consistent(self):
        for fields in ({"screenshot_mime_type": "image/jpeg"},
                       {"screenshot_frame_valid": "true"},
                       {"screenshot_frame_valid": 0}):
            with self.subTest(fields=fields):
                provider = FakeProvider()
                provider.snapshot_result.raw["structuredContent"].update(fields)
                _, summary, code = self.run_probe(provider, pid=123, window_id=456)
                self.assertEqual(code, 2)
                self.assertFalse(summary["observation_ready"])

    def test_pixel_byte_and_image_count_limits_are_enforced(self):
        for name, value in (("MAX_IMAGE_PIXELS", 4), ("MAX_IMAGE_BYTES", 4), ("MAX_IMAGES", 0)):
            with self.subTest(limit=name), patch("scripts.check_desktop_v2." + name, value):
                _, summary, code = self.run_probe(pid=123, window_id=456)
                self.assertEqual(code, 2)
                self.assertFalse(summary["image_decoded"])

    def test_tool_refusal_and_malformed_window_result_are_safe(self):
        for result, expected in ((outcome("list_windows", {}, is_error=True), "window_discovery_failed"),
                                 (outcome("list_windows", {"windows": ["wrong"]}), "window_result_invalid")):
            provider = FakeProvider()
            provider.window_result = result
            _, summary, code = self.run_probe(provider)
            self.assertEqual(code, 2)
            self.assertEqual(summary["phase_gate"], expected)

    def test_failures_always_cleanup_and_do_not_echo_exception_content(self):
        for field, error, expected in (
            ("start_error", DesktopProviderError("native", PRIVATE), "provider_error"),
            ("call_error", RuntimeError(PRIVATE), "probe_error"),
            ("call_error", KeyboardInterrupt(), "canceled"),
        ):
            with self.subTest(field=field, expected=expected):
                provider = FakeProvider()
                setattr(provider, field, error)
                _, summary, code = self.run_probe(provider)
                self.assertEqual(summary["phase_gate"], expected)
                self.assertEqual(code, 130 if expected == "canceled" else 2)

    def test_cleanup_failure_revokes_observation_readiness(self):
        for error in (None, RuntimeError(PRIVATE)):
            provider = FakeProvider()
            provider.close_stopped = False
            provider.close_error = error
            _, summary, code = self.run_probe(provider, pid=123, window_id=456)
            self.assertEqual(code, 2)
            self.assertEqual(summary["phase_gate"], "cleanup_unconfirmed")
            self.assertFalse(summary["cleanup_confirmed"])
            self.assertFalse(summary["observation_ready"])

    def test_invalid_inputs_never_construct_provider(self):
        for kwargs in ({"pid": 123}, {"window_id": 456}, {"timeout": float("nan")},
                       {"timeout": True}, {"timeout": 121},
                       {"pid": True, "window_id": 456}):
            factory = unittest.mock.Mock()
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                probe(provider_factory=factory, **kwargs)
            factory.assert_not_called()

    def test_main_emits_only_summary_json(self):
        output = StringIO()
        summary = {"input_ready": False, "phase_gate": "no_visible_windows"}
        with patch("scripts.check_desktop_v2.probe", return_value=(summary, 2)) as fake:
            with contextlib.redirect_stdout(output):
                self.assertEqual(main([]), 2)
        fake.assert_called_once_with(pid=None, window_id=None, timeout=30.0, health=False)
        self.assertEqual(json.loads(output.getvalue()), summary)

    def test_cli_requires_exact_target_pair(self):
        with contextlib.redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            main(["--pid", "123"])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
