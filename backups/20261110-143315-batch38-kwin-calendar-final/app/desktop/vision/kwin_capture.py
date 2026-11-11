"""Isolated KWin ScreenShot2 window capture for KDE Plasma 6 on Wayland.

The adapter talks only to the session D-Bus API exposed by KWin.  It does not
raise or otherwise focus the target window, so a capture also works while the
window is occluded.  No screenshots are written to disk here; callers receive
the encoded PNG bytes and decide how to store or consume them.
"""

from __future__ import annotations

import io
import math
import os
import re
import select
import struct
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Any

import dbus
from dbus.types import UnixFd


SERVICE_NAME = "org.kde.KWin"
OBJECT_PATH = "/org/kde/KWin/ScreenShot2"
INTERFACE = "org.kde.KWin.ScreenShot2"
INTROSPECTABLE_INTERFACE = "org.freedesktop.DBus.Introspectable"
CAPTURE_METHOD = "CaptureWindow"

# KWin sends a raw QImage through the pipe (see the Plasma 6 source), so the
# byte cap applies to that raw stream as well as to PNG payloads.
MAX_CAPTURE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 50_000_000
MAX_DIMENSION = 32_768
MAX_INTROSPECTION_BYTES = 256 * 1024
READ_CHUNK_BYTES = 64 * 1024
DEFAULT_TIMEOUT = 5.0
MIN_TIMEOUT = 0.1
MAX_TIMEOUT = 30.0

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

CAPTURE_CODES = (
    "ok",
    "invalid_window",
    "unsupported_api",
    "dbus_error",
    "timeout",
    "empty_capture",
    "too_large",
    "invalid_image",
)

_MESSAGES = {
    "ok": "Window captured.",
    "invalid_window": "The window identifier is invalid or no longer available.",
    "unsupported_api": (
        "KWin does not expose a supported ScreenShot2 window capture API."
    ),
    "dbus_error": "KWin could not capture the window over D-Bus.",
    "timeout": "The window capture timed out.",
    "empty_capture": "KWin returned no window image data.",
    "too_large": "The window capture exceeded the size limit.",
    "invalid_image": "KWin returned invalid window image data.",
}

_DASHED_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX_UUID = re.compile(r"^[0-9a-fA-F]{32}$")

# QImage::Format -> (Pillow mode, raw byte layout, bytes per pixel).  The
# formats below are the uncompressed layouts KWin can hand to ScreenShot2.
_RAW_LAYOUTS = {
    4: ("RGBA", "BGRA", 4),   # Format_RGB32
    5: ("RGBA", "BGRA", 4),   # Format_ARGB32
    6: ("RGBA", "BGRA", 4),   # Format_ARGB32_Premultiplied (opaque windows)
    13: ("RGB", "RGB", 3),    # Format_RGB888
    16: ("RGBA", "RGBA", 4),  # Format_RGBX8888
    17: ("RGBA", "RGBA", 4),  # Format_RGBA8888
    18: ("RGBA", "RGBA", 4),  # Format_RGBA8888_Premultiplied
    24: ("L", "L", 1),        # Format_Grayscale8
    29: ("RGB", "BGR", 3),    # Format_BGR888
}


def _safe_close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _normalize_window_id(value: Any) -> str | None:
    """Return a canonical UUID for a KWin window handle, or ``None``."""
    if not isinstance(value, str) or not value or len(value) > 128:
        return None

    # Reject control characters before trimming so a caller cannot smuggle
    # D-Bus-adjacent text into the handle.
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return None

    candidate = value.strip()
    if not candidate:
        return None
    if not (_DASHED_UUID.fullmatch(candidate) or _HEX_UUID.fullmatch(candidate)):
        return None

    try:
        parsed = uuid.UUID(candidate)
    except (ValueError, TypeError):
        return None
    return str(parsed)


def _coerce_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be a positive number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive number")
    return min(max(timeout, MIN_TIMEOUT), MAX_TIMEOUT)


def _coerce_limit(value: Any, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("capture limits must be positive integers")
    return min(value, maximum)


def _local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _to_uint(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _metadata_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _dbus_error_code(error: dbus.exceptions.DBusException) -> str:
    try:
        name = error.get_dbus_name() or ""
    except Exception:
        name = ""
    lowered = str(name).casefold()
    if lowered.endswith(".invalidwindow") or lowered.endswith(".noactivewindow"):
        return "invalid_window"
    if lowered.endswith(".cancelled"):
        return "empty_capture"
    if "timeout" in lowered or "timedout" in lowered or lowered.endswith(".noreply"):
        return "timeout"
    return "dbus_error"


class KWinWindowCapture:
    """Capture one KWin window through the ScreenShot2 D-Bus interface.

    ``bus`` exists for dependency injection in tests.  When omitted, the
    regular session bus is used.  ``timeout`` bounds both the D-Bus calls and
    the pipe read; it cannot be changed per capture.
    """

    def __init__(
        self,
        bus: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        max_bytes: int = MAX_CAPTURE_BYTES,
        max_pixels: int = MAX_PIXELS,
    ) -> None:
        self.bus = bus
        self.timeout = _coerce_timeout(timeout)
        self.max_bytes = _coerce_limit(max_bytes, MAX_CAPTURE_BYTES)
        self.max_pixels = _coerce_limit(max_pixels, MAX_PIXELS)

    def capture_window(self, window_id: Any) -> dict[str, Any]:
        """Capture ``window_id`` and return bounded PNG bytes plus metadata."""
        canonical_id = _normalize_window_id(window_id)
        if canonical_id is None:
            return self._failure("invalid_window", None)

        try:
            bus = self.bus if self.bus is not None else dbus.SessionBus()
            remote_object = bus.get_object(SERVICE_NAME, OBJECT_PATH)
        except dbus.exceptions.DBusException as error:
            return self._failure(_dbus_error_code(error), canonical_id)
        except TimeoutError:
            return self._failure("timeout", canonical_id)
        except Exception:
            return self._failure("dbus_error", canonical_id)

        try:
            variant = self._detect_api(remote_object)
        except dbus.exceptions.DBusException as error:
            return self._failure(_dbus_error_code(error), canonical_id)
        except TimeoutError:
            return self._failure("timeout", canonical_id)
        except Exception:
            return self._failure("dbus_error", canonical_id)

        if variant is None:
            return self._failure("unsupported_api", canonical_id)

        mode, takes_options = variant
        if mode == "pipe":
            return self._capture_via_pipe(remote_object, canonical_id)
        return self._capture_via_returned_fd(
            remote_object,
            canonical_id,
            takes_options=takes_options,
        )

    # -- discovery -------------------------------------------------------

    def _detect_api(self, remote_object: Any) -> tuple[str, bool] | None:
        """Introspect CaptureWindow and classify its exact signature.

        Two documented shapes are accepted:

        * ``CaptureWindow(s handle, a{sv} options, h pipe) -> a{sv}``
          (Plasma 6; KWin writes the image into the caller's pipe)
        * ``CaptureWindow(s handle[, a{sv} options]) -> h``
          (variant that returns a readable file descriptor)
        """
        introspectable = dbus.Interface(
            remote_object,
            INTROSPECTABLE_INTERFACE,
        )
        xml = introspectable.Introspect(timeout=self.timeout)
        if isinstance(xml, (bytes, bytearray)):
            try:
                xml = bytes(xml).decode("utf-8")
            except UnicodeDecodeError:
                return None
        if not isinstance(xml, str) or len(xml) > MAX_INTROSPECTION_BYTES:
            return None
        # Do not let a reply declare entities; the standard introspection DTD
        # is accepted, but entity expansion is refused.
        if "<!ENTITY" in xml.upper():
            return None

        try:
            root = ET.fromstring(xml)
        except (ET.ParseError, ValueError, UnicodeError):
            return None

        screenshot_interface = None
        for element in root.iter():
            if (
                _local_name(element.tag) == "interface"
                and element.get("name") == INTERFACE
            ):
                screenshot_interface = element
                break
        if screenshot_interface is None:
            return None

        methods = [
            child
            for child in screenshot_interface
            if _local_name(child.tag) == "method"
            and child.get("name") == CAPTURE_METHOD
        ]
        if len(methods) != 1:
            return None

        arguments = [
            child for child in methods[0] if _local_name(child.tag) == "arg"
        ]
        in_types = tuple(
            argument.get("type")
            for argument in arguments
            if argument.get("direction") == "in"
        )
        out_types = tuple(
            argument.get("type")
            for argument in arguments
            if argument.get("direction") == "out"
        )
        if any(item is None for item in in_types + out_types):
            return None

        if in_types == ("s", "a{sv}", "h") and out_types == ("a{sv}",):
            return ("pipe", True)
        if out_types == ("h",) and in_types == ("s", "a{sv}"):
            return ("returned_fd", True)
        if out_types == ("h",) and in_types == ("s",):
            return ("returned_fd", False)
        return None

    # -- capture variants ------------------------------------------------

    def _capture_via_pipe(
        self,
        remote_object: Any,
        window_id: str,
    ) -> dict[str, Any]:
        try:
            read_fd, write_fd = os.pipe()
        except OSError:
            return self._failure("dbus_error", window_id)

        try:
            try:
                screenshot = dbus.Interface(remote_object, INTERFACE)
                metadata = screenshot.CaptureWindow(
                    window_id,
                    self._window_options(),
                    UnixFd(write_fd),
                    timeout=self.timeout,
                )
            except dbus.exceptions.DBusException as error:
                return self._failure(_dbus_error_code(error), window_id)
            except TimeoutError:
                return self._failure("timeout", window_id)
            except Exception:
                return self._failure("dbus_error", window_id)
            finally:
                # KWin duplicates the descriptor before returning, so closing
                # our copy here lets the read side observe a clean EOF.
                _safe_close(write_fd)

            if not isinstance(metadata, dict):
                return self._failure("dbus_error", window_id)

            return self._read_capture(read_fd, window_id, metadata)
        finally:
            _safe_close(read_fd)

    def _capture_via_returned_fd(
        self,
        remote_object: Any,
        window_id: str,
        *,
        takes_options: bool,
    ) -> dict[str, Any]:
        try:
            screenshot = dbus.Interface(remote_object, INTERFACE)
            if takes_options:
                returned = screenshot.CaptureWindow(
                    window_id,
                    self._window_options(),
                    timeout=self.timeout,
                )
            else:
                returned = screenshot.CaptureWindow(
                    window_id,
                    timeout=self.timeout,
                )
        except dbus.exceptions.DBusException as error:
            return self._failure(_dbus_error_code(error), window_id)
        except TimeoutError:
            return self._failure("timeout", window_id)
        except Exception:
            return self._failure("dbus_error", window_id)

        read_fd = self._take_returned_fd(returned)
        if read_fd is None:
            return self._failure("empty_capture", window_id)

        try:
            return self._read_capture(read_fd, window_id, {})
        finally:
            _safe_close(read_fd)

    @staticmethod
    def _take_returned_fd(value: Any) -> int | None:
        try:
            if isinstance(value, UnixFd):
                value = value.take()
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            if value < 0:
                return None
            return value
        except Exception:
            return None

    @staticmethod
    def _window_options() -> dbus.Dictionary:
        return dbus.Dictionary(
            {
                "include-decoration": dbus.Boolean(False),
                "include-shadow": dbus.Boolean(False),
                "include-cursor": dbus.Boolean(False),
            },
            signature="sv",
        )

    # -- stream handling -------------------------------------------------

    def _read_capture(
        self,
        read_fd: int,
        window_id: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if not self._metadata_window_matches(metadata, window_id):
            return self._failure("invalid_window", window_id)

        deadline = time.monotonic() + self.timeout
        payload = bytearray()

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._failure("timeout", window_id)

            try:
                ready, _, _ = select.select([read_fd], [], [], remaining)
            except InterruptedError:
                continue
            except (OSError, ValueError):
                return self._failure("empty_capture", window_id)
            if not ready:
                return self._failure("timeout", window_id)

            try:
                chunk = os.read(
                    read_fd,
                    min(READ_CHUNK_BYTES, self.max_bytes + 1 - len(payload)),
                )
            except InterruptedError:
                continue
            except OSError:
                return self._failure("empty_capture", window_id)

            if not chunk:
                break

            payload.extend(chunk)
            if len(payload) > self.max_bytes:
                return self._failure("too_large", window_id)

        if not payload:
            return self._failure("empty_capture", window_id)

        return self._decode_capture(bytes(payload), window_id, metadata)

    @staticmethod
    def _metadata_window_matches(
        metadata: dict[str, Any],
        window_id: str,
    ) -> bool:
        if "windowId" in metadata:
            returned = metadata["windowId"]
        elif "window-id" in metadata:
            returned = metadata["window-id"]
        else:
            return True
        return _normalize_window_id(returned) == window_id

    def _decode_capture(
        self,
        payload: bytes,
        window_id: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if payload.startswith(PNG_SIGNATURE):
            dimensions = self._png_dimensions(payload)
            if dimensions is None:
                return self._failure("invalid_image", window_id)
            width, height = dimensions
            return self._success(payload, width, height, window_id)

        converted = self._convert_raw_image(payload, metadata)
        if converted is None:
            return self._failure("invalid_image", window_id)
        png_bytes, width, height = converted
        return self._success(png_bytes, width, height, window_id)

    def _png_dimensions(self, payload: bytes) -> tuple[int, int] | None:
        # Signature (8) + IHDR length/type (8) + 13 data bytes + CRC (4).
        if len(payload) < 33 or not payload.startswith(PNG_SIGNATURE):
            return None
        if struct.unpack(">I", payload[8:12])[0] != 13:
            return None
        if payload[12:16] != b"IHDR":
            return None

        width, height = struct.unpack(">II", payload[16:24])
        if not self._valid_dimensions(width, height):
            return None
        return width, height

    def _valid_dimensions(self, width: int, height: int) -> bool:
        return (
            0 < width <= MAX_DIMENSION
            and 0 < height <= MAX_DIMENSION
            and width * height <= self.max_pixels
        )

    def _convert_raw_image(
        self,
        payload: bytes,
        metadata: dict[str, Any],
    ) -> tuple[bytes, int, int] | None:
        """Convert KWin's documented raw QImage stream into PNG bytes."""
        if _metadata_text(metadata.get("type")) != "raw":
            return None

        width = _to_uint(metadata.get("width"))
        height = _to_uint(metadata.get("height"))
        stride = _to_uint(metadata.get("stride"))
        image_format = _to_uint(metadata.get("format"))
        if (
            width is None
            or height is None
            or stride is None
            or image_format is None
        ):
            return None
        if not self._valid_dimensions(width, height):
            return None

        layout = _RAW_LAYOUTS.get(image_format)
        if layout is None:
            return None
        image_mode, raw_mode, bytes_per_pixel = layout

        minimum_stride = width * bytes_per_pixel
        if stride < minimum_stride or stride > self.max_bytes:
            return None
        expected_size = stride * height
        if expected_size <= 0 or expected_size > self.max_bytes:
            return None
        if expected_size != len(payload):
            return None

        try:
            from PIL import Image
        except ImportError:
            return None

        try:
            image = Image.frombytes(
                image_mode,
                (width, height),
                payload,
                "raw",
                raw_mode,
                stride,
                1,
            )
            output = io.BytesIO()
            image.save(output, format="PNG")
            png_bytes = output.getvalue()
        except Exception:
            return None

        if not png_bytes.startswith(PNG_SIGNATURE):
            return None
        return png_bytes, width, height

    # -- results ---------------------------------------------------------

    @staticmethod
    def _success(
        image_bytes: bytes,
        width: int,
        height: int,
        window_id: str,
    ) -> dict[str, Any]:
        return {
            "success": True,
            "code": "ok",
            "message": _MESSAGES["ok"],
            "image_bytes": image_bytes,
            "width": int(width),
            "height": int(height),
            "window_id": window_id,
        }

    @staticmethod
    def _failure(code: str, window_id: str | None) -> dict[str, Any]:
        return {
            "success": False,
            "code": code,
            "message": _MESSAGES.get(code, _MESSAGES["dbus_error"]),
            "image_bytes": b"",
            "width": 0,
            "height": 0,
            "window_id": window_id,
        }
