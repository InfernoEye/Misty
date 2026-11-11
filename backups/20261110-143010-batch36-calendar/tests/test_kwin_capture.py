import io
import os
import struct
import time
import unittest
import zlib

import dbus
from dbus.types import UnixFd
from PIL import Image

from app.desktop.vision.kwin_capture import (
    MAX_CAPTURE_BYTES,
    OBJECT_PATH,
    SERVICE_NAME,
    KWinWindowCapture,
)


WINDOW_ID = "123e4567-e89b-12d3-a456-426614174000"
OTHER_WINDOW_ID = "9f5c8d2a-1111-4222-8333-444455556666"


def _write_all(fd, payload):
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _close(fd):
    try:
        os.close(fd)
    except OSError:
        pass


def _png_chunk(kind, payload):
    checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", checksum)
    )


def _make_png(width=2, height=2):
    """Build a small, self-contained valid RGB PNG without external files."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanlines = b"".join(
        b"\x00" + b"\x10\x20\x30" * width for _ in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanlines))
        + _png_chunk(b"IEND", b"")
    )


def _introspection_xml(
    in_types=("s", "a{sv}", "h"),
    out_types=("a{sv}",),
):
    inputs = "".join(
        f'<arg direction="in" type="{item}"/>' for item in in_types
    )
    outputs = "".join(
        f'<arg direction="out" type="{item}"/>' for item in out_types
    )
    return (
        '<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object '
        'Introspection 1.0//EN"\n'
        '"http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">\n'
        "<node>"
        f'<interface name="org.kde.KWin.ScreenShot2">'
        f'<method name="CaptureWindow">{inputs}{outputs}</method>'
        "</interface>"
        "</node>"
    )


class FakeObject:
    def __init__(self, introspection, capture):
        self.introspection = introspection
        self.capture = capture
        self.capture_calls = 0

    def get_dbus_method(self, member, interface):
        if (
            interface == "org.freedesktop.DBus.Introspectable"
            and member == "Introspect"
        ):
            def introspect(*, timeout=None):
                if isinstance(self.introspection, Exception):
                    raise self.introspection
                return self.introspection

            return introspect

        if interface == "org.kde.KWin.ScreenShot2" and member == "CaptureWindow":
            def capture_window(*args, timeout=None):
                self.capture_calls += 1
                return self.capture(*args, timeout=timeout)

            return capture_window

        raise dbus.exceptions.DBusException(
            "unsupported fake method",
            name="org.freedesktop.DBus.Error.UnknownMethod",
        )


class FakeBus:
    def __init__(self, remote_object):
        self.remote_object = remote_object
        self.calls = []

    def get_object(self, service, path):
        self.calls.append((service, path))
        return self.remote_object


class KWinWindowCaptureTests(unittest.TestCase):
    def setUp(self):
        self.held_fds = []
        self.last_window_id = None
        self.last_options = None
        self.last_pipe = None
        self.addCleanup(self._close_held_fds)

    def _close_held_fds(self):
        for fd in self.held_fds:
            _close(fd)
        self.held_fds.clear()

    def _build(self, capture, *, in_types=None, out_types=None, xml=None):
        if xml is None:
            kwargs = {}
            if in_types is not None:
                kwargs["in_types"] = in_types
            if out_types is not None:
                kwargs["out_types"] = out_types
            xml = _introspection_xml(**kwargs)
        remote = FakeObject(xml, capture)
        return FakeBus(remote), remote

    def _pipe_handler(self, payload=b"", metadata=None, hold=False):
        def capture_window(window_id, options, pipe, timeout=None):
            self.last_window_id = window_id
            self.last_options = dict(options)
            self.last_pipe = pipe
            raw_fd = pipe.take()
            if hold:
                self.held_fds.append(os.dup(raw_fd))
            elif payload:
                _write_all(raw_fd, payload)
            result = {"windowId": window_id}
            if metadata:
                result.update(metadata)
            return result

        return capture_window

    def _returned_fd_handler(self, payload):
        def capture_window(*args, timeout=None):
            if len(args) == 2:
                window_id, options = args
                self.last_options = dict(options)
            else:
                (window_id,) = args
                self.last_options = None
            self.last_window_id = window_id

            read_fd, write_fd = os.pipe()
            try:
                _write_all(write_fd, payload)
            finally:
                _close(write_fd)
            return UnixFd(read_fd)

        return capture_window

    def test_capture_success_uses_documented_pipe_api(self):
        png = _make_png(3, 2)
        bus, remote = self._build(self._pipe_handler(png, hold=False))

        result = KWinWindowCapture(bus=bus, timeout=2.0).capture_window(
            WINDOW_ID.upper()
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["code"], "ok")
        self.assertEqual(result["message"], "Window captured.")
        self.assertEqual(result["image_bytes"], png)
        self.assertEqual((result["width"], result["height"]), (3, 2))
        self.assertEqual(result["window_id"], WINDOW_ID)
        self.assertEqual(
            set(result),
            {
                "success",
                "code",
                "message",
                "image_bytes",
                "width",
                "height",
                "window_id",
            },
        )

        self.assertEqual(bus.calls, [(SERVICE_NAME, OBJECT_PATH)])
        self.assertEqual(remote.capture_calls, 1)
        self.assertEqual(self.last_window_id, WINDOW_ID)
        self.assertIsInstance(self.last_pipe, UnixFd)
        self.assertFalse(bool(self.last_options["include-decoration"]))
        self.assertFalse(bool(self.last_options["include-shadow"]))
        self.assertFalse(bool(self.last_options["include-cursor"]))

    def test_returned_fd_variant_with_options_is_supported(self):
        png = _make_png(4, 1)
        bus, remote = self._build(
            self._returned_fd_handler(png),
            in_types=("s", "a{sv}"),
            out_types=("h",),
        )

        result = KWinWindowCapture(bus=bus, timeout=2.0).capture_window(WINDOW_ID)

        self.assertTrue(result["success"])
        self.assertEqual(result["image_bytes"], png)
        self.assertEqual((result["width"], result["height"]), (4, 1))
        self.assertEqual(remote.capture_calls, 1)
        self.assertIsNotNone(self.last_options)

    def test_returned_fd_variant_without_options_is_supported(self):
        png = _make_png(1, 1)
        bus, remote = self._build(
            self._returned_fd_handler(png),
            in_types=("s",),
            out_types=("h",),
        )

        result = KWinWindowCapture(bus=bus, timeout=2.0).capture_window(WINDOW_ID)

        self.assertTrue(result["success"])
        self.assertEqual((result["width"], result["height"]), (1, 1))
        self.assertIsNone(self.last_options)
        self.assertEqual(remote.capture_calls, 1)

    def test_raw_plasma_stream_is_converted_to_png(self):
        # Two BGRA pixels: opaque red, then opaque green (Format_ARGB32).
        raw = bytes([0, 0, 255, 255, 0, 255, 0, 255])
        metadata = {
            "type": "raw",
            "format": 6,
            "width": 2,
            "height": 1,
            "stride": 8,
            "scale": 1.0,
        }
        bus, _ = self._build(self._pipe_handler(raw, metadata=metadata))

        result = KWinWindowCapture(bus=bus, timeout=2.0).capture_window(WINDOW_ID)

        self.assertTrue(result["success"])
        self.assertEqual((result["width"], result["height"]), (2, 1))
        self.assertTrue(result["image_bytes"].startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(result["image_bytes"])) as image:
            self.assertEqual(image.size, (2, 1))
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 255))
            self.assertEqual(image.getpixel((1, 0)), (0, 255, 0, 255))

    def test_invalid_window_id_is_rejected_without_bus_access(self):
        class ExplodingBus:
            def get_object(self, service, path):
                raise AssertionError("the bus must not be used")

        capture = KWinWindowCapture(bus=ExplodingBus(), timeout=1.0)
        for invalid in (
            None,
            123,
            "",
            "   ",
            "not-a-uuid",
            WINDOW_ID + "\n",
            "\x00" + WINDOW_ID,
            WINDOW_ID.replace("-", "")[:-1],
            "{" + WINDOW_ID + "}",
            "x" * 200,
        ):
            with self.subTest(value=invalid):
                result = capture.capture_window(invalid)
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], "invalid_window")
                self.assertIsNone(result["window_id"])
                self.assertEqual(result["image_bytes"], b"")
                self.assertEqual((result["width"], result["height"]), (0, 0))

    def test_unsupported_api_signature_fails_closed(self):
        bus, remote = self._build(
            self._pipe_handler(_make_png()),
            in_types=("s", "h"),
            out_types=("a{sv}",),
        )

        result = KWinWindowCapture(bus=bus, timeout=1.0).capture_window(WINDOW_ID)

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "unsupported_api")
        self.assertEqual(remote.capture_calls, 0)

    def test_missing_screenshot_interface_fails_closed(self):
        xml = '<node><interface name="org.example.Other"/></node>'
        bus, remote = self._build(
            self._pipe_handler(_make_png()),
            xml=xml,
        )

        result = KWinWindowCapture(bus=bus, timeout=1.0).capture_window(WINDOW_ID)

        self.assertEqual(result["code"], "unsupported_api")
        self.assertEqual(remote.capture_calls, 0)

    def test_malformed_introspection_fails_closed(self):
        bus, remote = self._build(
            self._pipe_handler(_make_png()),
            xml="<node>",
        )

        result = KWinWindowCapture(bus=bus, timeout=1.0).capture_window(WINDOW_ID)

        self.assertEqual(result["code"], "unsupported_api")
        self.assertEqual(remote.capture_calls, 0)

    def test_introspection_dbus_error_is_reported(self):
        error = dbus.exceptions.DBusException(
            "secret introspection detail",
            name="org.freedesktop.DBus.Error.ServiceUnknown",
        )
        bus, _ = self._build(None, xml=error)

        result = KWinWindowCapture(bus=bus, timeout=1.0).capture_window(WINDOW_ID)

        self.assertEqual(result["code"], "dbus_error")
        self.assertNotIn("secret", result["message"])

    def test_invalid_window_dbus_error_maps_to_invalid_window(self):
        def capture_window(*args, timeout=None):
            raise dbus.exceptions.DBusException(
                "private KWin detail",
                name="org.kde.KWin.ScreenShot2.Error.InvalidWindow",
            )

        bus, remote = self._build(capture_window)

        result = KWinWindowCapture(bus=bus, timeout=1.0).capture_window(WINDOW_ID)

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "invalid_window")
        self.assertEqual(
            result["message"],
            "The window identifier is invalid or no longer available.",
        )
        self.assertNotIn("private", result["message"])
        self.assertEqual(remote.capture_calls, 1)

    def test_unexpected_exception_maps_to_dbus_error_without_leaking_text(self):
        def capture_window(*args, timeout=None):
            raise RuntimeError("private window title and path")

        bus, _ = self._build(capture_window)

        result = KWinWindowCapture(bus=bus, timeout=1.0).capture_window(WINDOW_ID)

        self.assertEqual(result["code"], "dbus_error")
        self.assertNotIn("private", result["message"])
        self.assertEqual(result["image_bytes"], b"")

    def test_metadata_window_mismatch_is_invalid_window(self):
        metadata = {"windowId": OTHER_WINDOW_ID}
        bus, _ = self._build(
            self._pipe_handler(_make_png(), metadata=metadata)
        )

        result = KWinWindowCapture(bus=bus, timeout=1.0).capture_window(WINDOW_ID)

        self.assertEqual(result["code"], "invalid_window")

    def test_pipe_timeout_is_reported(self):
        bus, _ = self._build(self._pipe_handler(hold=True))
        capture = KWinWindowCapture(bus=bus, timeout=0.1)

        started = time.monotonic()
        result = capture.capture_window(WINDOW_ID)
        elapsed = time.monotonic() - started

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "timeout")
        self.assertGreaterEqual(elapsed, 0.05)
        self.assertLess(elapsed, 2.0)

    def test_empty_pipe_is_empty_capture(self):
        bus, _ = self._build(self._pipe_handler(b""))

        result = KWinWindowCapture(bus=bus, timeout=1.0).capture_window(WINDOW_ID)

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "empty_capture")
        self.assertEqual(result["image_bytes"], b"")

    def test_truncated_png_is_invalid_image(self):
        payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 12
        bus, _ = self._build(self._pipe_handler(payload))

        result = KWinWindowCapture(bus=bus, timeout=1.0).capture_window(WINDOW_ID)

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "invalid_image")

    def test_bad_signature_is_invalid_image(self):
        bus, _ = self._build(self._pipe_handler(b"GIF89a not a png"))

        result = KWinWindowCapture(bus=bus, timeout=1.0).capture_window(WINDOW_ID)

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "invalid_image")

    def test_stream_over_configured_cap_is_too_large(self):
        bus, _ = self._build(self._pipe_handler(b"A" * 1025))
        capture = KWinWindowCapture(bus=bus, timeout=1.0, max_bytes=1024)

        result = capture.capture_window(WINDOW_ID)

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "too_large")
        self.assertEqual(result["image_bytes"], b"")

    def test_configured_cap_cannot_exceed_hard_limit(self):
        capture = KWinWindowCapture(max_bytes=10**9, max_pixels=10**12)
        self.assertEqual(capture.max_bytes, MAX_CAPTURE_BYTES)
        self.assertEqual(capture.max_pixels, 50_000_000)


if __name__ == "__main__":
    unittest.main()
