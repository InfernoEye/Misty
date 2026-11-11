"""Secure, on-demand desktop screenshot capture using KDE Spectacle."""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
import uuid
from copy import deepcopy
from collections import OrderedDict
from threading import Lock
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw


MISTY_OVERLAY_IDENTIFIERS = frozenset({"mistypopup"})


class CaptureError(RuntimeError):
    """Raised when a screenshot cannot be captured or validated."""


@dataclass(frozen=True)
class CaptureResult:
    image: Image.Image
    mode: str
    captured_at: str
    width: int
    height: int
    screen_name: str | None = None
    window_id: str | None = None
    window_title: str | None = None
    window_geometry: dict[str, int] | None = None
    capture_kind: str = "fullscreen"
    masked_overlays: tuple[str, ...] = ()
    frame: dict[str, Any] = field(default_factory=dict)


class CaptureService:
    """Captures the desktop only when explicitly requested."""

    def __init__(
        self,
        context_provider: Callable[[], dict[str, Any]] | None = None,
        spectacle_path: str = "/usr/bin/spectacle",
        timeout: float = 30.0,
        max_pixels: int = 50_000_000,
        max_file_bytes: int = 100_000_000,
        target_window_provider: Callable[[], dict[str, Any]] | None = None,
        kwin_capture=None,
    ) -> None:
        self.context_provider = context_provider
        self.kwin_capture = kwin_capture
        self.target_window_provider = target_window_provider
        self.spectacle_path = spectacle_path
        self.timeout = timeout
        self.max_pixels = max_pixels
        self.max_file_bytes = max_file_bytes
        self._frames = OrderedDict()
        self._frame_lock = Lock()

    @staticmethod
    def _layout(context):
        """Return validated logical display rectangles; never infer mixed DPI."""
        if not isinstance(context, dict) or context.get("success") is not True:
            return None
        screens = context.get("screens", [])
        if not isinstance(screens, list):
            return None
        layout = []
        for screen in screens:
            try:
                geometry = screen["geometry"]
                values = tuple(geometry[key] for key in ("x", "y", "width", "height"))
                if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
                    return None
                if values[2] <= 0 or values[3] <= 0:
                    return None
                # Geometry-only APIs do not describe a piecewise pixel mapping.
                scale = screen.get("scale", screen.get("device_pixel_ratio", 1))
                if scale != 1:
                    return None
                layout.append((str(screen.get("name", "")), *values))
            except (KeyError, TypeError):
                return None
        return tuple(sorted(layout)) or None

    @classmethod
    def _desktop_mapping(cls, context, image):
        layout = cls._layout(context)
        if not layout:
            raise CaptureError("No unambiguous 1:1 display coordinate mapping is available")
        min_x = min(item[1] for item in layout)
        min_y = min(item[2] for item in layout)
        width = max(item[1] + item[3] for item in layout) - min_x
        height = max(item[2] + item[4] for item in layout) - min_y
        if image.size != (width, height):
            raise CaptureError("Screenshot pixels do not match logical display geometry; scaling is ambiguous")
        return layout, min_x, min_y

    def resolve_input_point(self, frame_id, x, y, max_age_seconds=120.0):
        """Resolve trusted recent IMAGE pixels to signed desktop input coordinates.

        Only frames issued by this capture service are accepted. The 120s cap
        allows local VLM/reasoning reload latency; it is not a guarantee that
        UI content is unchanged. Layout/window geometry are rechecked, and the
        controller must observe after every action. Callers must still enforce
        approval and input bounds. Native window images are not
        grounded because decorations/client extents are not established.
        """
        if not isinstance(frame_id, str) or not frame_id:
            raise CaptureError("A valid capture frame_id is required")
        with self._frame_lock:
            entry = self._frames.get(frame_id)
        if entry is None:
            raise CaptureError("Unknown capture frame; inspect the screen again")
        frame, layout, created = entry
        if time.monotonic() - created > max_age_seconds:
            raise CaptureError("Capture frame expired; inspect the screen again")
        transform = frame.get("image_to_input")
        if not frame.get("grounded") or not transform:
            raise CaptureError(frame.get("reason") or "Capture coordinates are not grounded")
        if self.context_provider is None:
            raise CaptureError("Cannot validate current display layout")
        try:
            context = self.context_provider()
            current = self._layout(context)
        except Exception as exc:
            raise CaptureError("Cannot validate current display layout") from exc
        if current != layout:
            raise CaptureError("Display layout changed; inspect the screen again")
        target = frame.get("window_target")
        if target:
            matches = [w for w in context.get("windows", [])
                       if isinstance(w, dict) and w.get("id") == target["id"]]
            if (len(matches) != 1 or matches[0].get("geometry") != target["geometry"]
                    or matches[0].get("minimized") is True
                    or matches[0].get("on_current_desktop") is False):
                raise CaptureError("Captured window moved or disappeared; inspect again")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (x, y)):
            raise CaptureError("Image coordinates must be integers")
        width, height = frame["image_size"]
        if not (0 <= x < width and 0 <= y < height):
            raise CaptureError("Image coordinate falls outside the capture frame")
        input_x, input_y = transform["origin_x"] + x, transform["origin_y"] + y
        if not any(left <= input_x < left + width and top <= input_y < top + height
                   for _name, left, top, width, height in layout):
            raise CaptureError("Capture coordinate is in a gap between connected displays")
        return input_x, input_y

    def _register_frame(self, image, captured_at):
        metadata = image.info.pop("_misty_frame", {})
        transform = metadata.get("image_to_input")
        frame = {
            "frame_id": uuid.uuid4().hex,
            "captured_at": captured_at,
            "coordinate_space": "image_pixels",
            "input_space": "desktop_logical",
            "image_size": [image.width, image.height],
            "image_to_input": transform,
            "window_target": metadata.get("window_target"),
            "screens": [
                {"name": name, "geometry": {"x": x, "y": y, "width": width, "height": height}}
                for name, x, y, width, height in (metadata.get("layout") or ())
            ],
            "grounded": transform is not None,
            "reason": metadata.get("reason", "Capture origin or scale is not established" if transform is None else None),
        }
        with self._frame_lock:
            self._frames[frame["frame_id"]] = (deepcopy(frame), metadata.get("layout"), time.monotonic())
            while len(self._frames) > 16:
                self._frames.popitem(last=False)
        return frame

    def capture(
        self,
        mode: str = "desktop",
        screen_name: str | None = None,
        window_id: str | None = None,
        *,
        visible_region_only: bool = False,
    ) -> CaptureResult:
        if not isinstance(visible_region_only, bool):
            raise ValueError("visible_region_only must be a boolean")
        if visible_region_only and mode != "window":
            raise ValueError("visible_region_only is only valid for mode=window")
        if mode not in {"desktop", "active_window", "screen", "window"}:
            raise ValueError(
                "mode must be desktop, active_window, screen, or window"
            )

        target: dict[str, Any] | None = None
        capture_kind = "fullscreen"
        masked_overlays: tuple[str, ...] = ()

        if mode == "screen":
            if not screen_name:
                raise ValueError("screen_name is required for screen capture")
            if window_id is not None:
                raise ValueError("window_id is not valid for screen capture")
            image = self._capture_screen(screen_name)
            capture_kind = "screen_region"
        elif mode == "window":
            if not isinstance(window_id, str) or not window_id.strip():
                raise ValueError("window_id is required for window capture")
            if screen_name is not None:
                raise ValueError("screen_name is not valid for window capture")
            image, target, masked_overlays = self._capture_identified_window(
                window_id.strip(),
                missing_message="The requested window is no longer available",
                minimized_message="The requested window is minimized",
                visible_region_only=visible_region_only,
            )
            capture_kind = "visible_region"
        else:
            if screen_name is not None:
                raise ValueError("screen_name is only valid for screen capture")
            if window_id is not None:
                raise ValueError("window_id is only valid for window capture")
            if mode == "active_window" and self.target_window_provider is not None:
                image, target, masked_overlays = self._capture_saved_window()
                capture_kind = "visible_region"
            else:
                options = (
                    ["--activewindow"]
                    if mode == "active_window"
                    else ["--fullscreen"]
                )
                image = self._capture_image(options)
                if mode == "active_window":
                    capture_kind = "active_window_native"
                else:
                    try:
                        context = self.context_provider() if self.context_provider else None
                        layout, origin_x, origin_y = self._desktop_mapping(context, image)
                        image.info["_misty_frame"] = {
                            "layout": layout,
                            "image_to_input": {"origin_x": origin_x, "origin_y": origin_y,
                                               "scale_x": 1.0, "scale_y": 1.0},
                        }
                    except Exception as exc:
                        image.info["_misty_frame"] = {"reason": str(exc)}

        geometry = None
        if target is not None and isinstance(target.get("geometry"), dict):
            raw_geometry = target["geometry"]
            parsed_geometry = {}
            for key in ("x", "y", "width", "height"):
                try:
                    parsed_geometry[key] = int(raw_geometry[key])
                except (KeyError, TypeError, ValueError):
                    parsed_geometry = {}
                    break
            if parsed_geometry:
                geometry = parsed_geometry

        captured_at = datetime.now(timezone.utc).isoformat()
        capture_kind = image.info.pop("_misty_capture_kind", capture_kind)
        frame = self._register_frame(image, captured_at)
        return CaptureResult(
            image=image,
            mode=mode,
            captured_at=captured_at,
            width=image.width,
            height=image.height,
            screen_name=screen_name,
            window_id=(
                str(target.get("id"))
                if target is not None and target.get("id") is not None
                else None
            ),
            window_title=(
                str(target.get("title"))
                if target is not None and target.get("title")
                else None
            ),
            window_geometry=geometry,
            capture_kind=capture_kind,
            masked_overlays=masked_overlays,
            frame=frame,
        )

    def _capture_saved_window(
        self,
    ) -> tuple[Image.Image, dict[str, Any], tuple[str, ...]]:
        """Capture the previously focused application, never the current popup."""
        if self.target_window_provider is None or self.context_provider is None:
            raise CaptureError("Previous active-window capture is not configured")

        try:
            saved_result = self.target_window_provider()
        except Exception as exc:
            raise CaptureError("Could not retrieve the previous active window") from exc

        if not isinstance(saved_result, dict) or not saved_result.get("success"):
            detail = (
                saved_result.get("message", "No saved target window")
                if isinstance(saved_result, dict)
                else "Invalid saved target response"
            )
            raise CaptureError(detail)

        saved = saved_result.get("active_window")
        if not isinstance(saved, dict) or not saved.get("id"):
            raise CaptureError("No valid previous active-window identity was saved")

        return self._capture_identified_window(
            str(saved["id"]),
            missing_message=(
                "The application that was active when listening began "
                "is no longer available"
            ),
            minimized_message=(
                "The previously active application is minimized"
            ),
        )

    def _capture_identified_window(
        self,
        window_id: str,
        missing_message: str,
        minimized_message: str,
        *,
        visible_region_only: bool = False,
    ) -> tuple[Image.Image, dict[str, Any], tuple[str, ...]]:
        # Foreground pilots must observe exactly the visible desktop crop, not
        # a native/off-screen client image with unproven input coordinates.
        if self.kwin_capture is not None and not visible_region_only:
            try:
                direct = self.kwin_capture.capture_window(window_id)
            except Exception:
                direct = None
            if isinstance(direct, dict) and direct.get("success") is True:
                payload = direct.get("image_bytes")
                if not isinstance(payload, (bytes, bytearray)) or not payload:
                    raise CaptureError("The window capture returned no image")
                try:
                    image = Image.open(BytesIO(bytes(payload)))
                    image.load()
                except Exception as exc:
                    raise CaptureError(
                        "The window capture returned an invalid image"
                    ) from exc
                if (
                    image.width <= 0
                    or image.height <= 0
                    or image.width * image.height > self.max_pixels
                ):
                    raise CaptureError("The window capture dimensions are invalid")
                target = {"id": direct.get("window_id") or window_id}
                if self.context_provider is not None:
                    try:
                        context = self.context_provider()
                        for window in (context or {}).get("windows", []):
                            if (
                                isinstance(window, dict)
                                and window.get("id") == window_id
                            ):
                                target = window
                                break
                    except Exception:
                        pass
                image.info["_misty_capture_kind"] = "native_window"
                image.info["_misty_frame"] = {
                    "reason": "Native window image has no verified client/decoration-to-input transform"
                }
                return image, target, ()

            if isinstance(direct, dict):
                code = direct.get("code")
                # A rejected handle may simply mean this KWin build exposes a
                # different handle shape; let the Spectacle crop path try.
                if code not in {"unsupported_api", "dbus_error", "invalid_window"}:
                    raise CaptureError(
                        str(direct.get("message") or "Window capture failed")
                    )

        if self.context_provider is None:
            raise CaptureError("Desktop context provider is not configured")

        try:
            context = self.context_provider()
        except Exception as exc:
            raise CaptureError("Could not refresh desktop window geometry") from exc

        if not isinstance(context, dict) or not context.get("success"):
            raise CaptureError("Current desktop context is unavailable")

        windows = context.get("windows")
        screens = context.get("screens")
        if not isinstance(windows, list) or not isinstance(screens, list):
            raise CaptureError("Current desktop context has invalid window or screen data")

        matches = [
            window for window in windows
            if isinstance(window, dict) and window.get("id") == window_id
        ]
        if len(matches) != 1:
            raise CaptureError(missing_message)

        target = matches[0]
        if target.get("minimized") is True:
            raise CaptureError(minimized_message)
        if target.get("on_current_desktop") is False:
            raise CaptureError(
                "The target window is on another virtual desktop and is not "
                "visible on the current desktop"
            )

        geometry = target.get("geometry")
        if not isinstance(geometry, dict):
            raise CaptureError("The target application has no valid window geometry")

        try:
            x = int(geometry["x"])
            y = int(geometry["y"])
            width = int(geometry["width"])
            height = int(geometry["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptureError("The target application geometry is invalid") from exc

        if width <= 0 or height <= 0 or width * height > self.max_pixels:
            raise CaptureError("The target application dimensions are invalid")

        geometries = [
            screen.get("geometry")
            for screen in screens
            if isinstance(screen, dict)
            and isinstance(screen.get("geometry"), dict)
        ]
        if not geometries:
            raise CaptureError("No display geometry is available")

        try:
            min_x = min(int(item["x"]) for item in geometries)
            min_y = min(int(item["y"]) for item in geometries)
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptureError("Desktop display geometry is incomplete") from exc

        desktop = self._capture_image(["--fullscreen"])
        layout, min_x, min_y = self._desktop_mapping(context, desktop)
        left = x - min_x
        top = y - min_y
        right = left + width
        bottom = top + height

        if (
            left < 0 or top < 0
            or right > desktop.width or bottom > desktop.height
        ):
            raise CaptureError(
                "The target window geometry falls outside the captured desktop"
            )

        cropped = desktop.crop((left, top, right, bottom))
        overlays = context.get("overlays", [])
        if not isinstance(overlays, list):
            overlays = []
        masked = self._mask_misty_overlays(cropped, x, y, overlays)
        cropped.info["_misty_frame"] = {
            "layout": layout,
            "window_target": {"id": target["id"], "geometry": dict(geometry)},
            "image_to_input": {"origin_x": x, "origin_y": y,
                               "scale_x": 1.0, "scale_y": 1.0},
        }
        return cropped, target, masked

    @staticmethod
    def _normalized_identity(value: Any) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value).casefold())

    def _mask_misty_overlays(
        self,
        image: Image.Image,
        origin_x: int,
        origin_y: int,
        windows: list[Any],
    ) -> tuple[str, ...]:
        """Mask Misty's own popup so it cannot contaminate an inspection."""
        masked: list[str] = []
        draw = None

        for window in windows:
            if not isinstance(window, dict):
                continue
            if not any(
                value
                and self._normalized_identity(value) in MISTY_OVERLAY_IDENTIFIERS
                for value in (
                    window.get("app_id"),
                    window.get("resource_class"),
                    window.get("resource_name"),
                )
            ):
                continue

            geometry = window.get("geometry")
            if not isinstance(geometry, dict):
                continue
            try:
                gx = int(geometry["x"])
                gy = int(geometry["y"])
                gw = int(geometry["width"])
                gh = int(geometry["height"])
            except (KeyError, TypeError, ValueError):
                continue

            left = max(0, gx - origin_x)
            top = max(0, gy - origin_y)
            right = min(image.width, gx + gw - origin_x)
            bottom = min(image.height, gy + gh - origin_y)
            if right <= left or bottom <= top:
                continue

            if draw is None:
                draw = ImageDraw.Draw(image)
            draw.rectangle((left, top, right - 1, bottom - 1), fill=(0, 0, 0))
            masked.append(str(window.get("id") or "misty-overlay"))

        return tuple(masked)

    def _capture_screen(self, screen_name: str) -> Image.Image:
        if self.context_provider is None:
            raise CaptureError("Desktop context provider is not configured")

        try:
            context = self.context_provider()
        except Exception as exc:
            raise CaptureError("Could not obtain desktop screen geometry") from exc

        if not isinstance(context, dict) or not context.get("success"):
            raise CaptureError("Desktop context is unavailable")

        screens = context.get("screens")
        if not isinstance(screens, list):
            raise CaptureError("Desktop context contains no valid screen list")

        matches = [
            screen for screen in screens
            if isinstance(screen, dict) and screen.get("name") == screen_name
        ]
        if len(matches) != 1:
            raise CaptureError(
                f"Expected exactly one display named {screen_name!r}; "
                f"found {len(matches)}"
            )

        geometry = matches[0].get("geometry")
        if not isinstance(geometry, dict):
            raise CaptureError("Selected display has no valid geometry")

        try:
            x = int(geometry["x"])
            y = int(geometry["y"])
            width = int(geometry["width"])
            height = int(geometry["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptureError("Selected display geometry is invalid") from exc

        if width <= 0 or height <= 0:
            raise CaptureError("Selected display dimensions must be positive")

        all_geometries = [
            screen.get("geometry")
            for screen in screens
            if isinstance(screen, dict)
            and isinstance(screen.get("geometry"), dict)
        ]
        try:
            min_x = min(int(item["x"]) for item in all_geometries)
            min_y = min(int(item["y"]) for item in all_geometries)
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptureError("Desktop screen geometry is incomplete") from exc

        desktop_image = self._capture_image(["--fullscreen"])
        layout, min_x, min_y = self._desktop_mapping(context, desktop_image)
        left = x - min_x
        top = y - min_y
        right = left + width
        bottom = top + height

        if (
            left < 0
            or top < 0
            or right > desktop_image.width
            or bottom > desktop_image.height
        ):
            raise CaptureError(
                "Selected display geometry falls outside the captured desktop"
            )

        cropped = desktop_image.crop((left, top, right, bottom))
        cropped.info["_misty_frame"] = {
            "layout": layout,
            "image_to_input": {"origin_x": x, "origin_y": y,
                               "scale_x": 1.0, "scale_y": 1.0},
        }
        return cropped

    def _capture_image(self, options: list[str]) -> Image.Image:
        with tempfile.TemporaryDirectory(prefix="misty-capture-") as directory:
            output = Path(directory) / "capture.png"
            command = [
                self.spectacle_path,
                "--background",
                "--nonotify",
                *options,
                "--output",
                str(output),
            ]

            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise CaptureError("Screenshot capture timed out") from exc
            except OSError as exc:
                raise CaptureError("Could not start Spectacle") from exc

            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()[:1000]
                raise CaptureError(
                    f"Spectacle failed with exit code {result.returncode}: {detail}"
                )

            if not output.is_file():
                raise CaptureError("Spectacle did not produce an image")

            if output.stat().st_size > self.max_file_bytes:
                raise CaptureError("Screenshot file exceeds the size limit")

            try:
                with Image.open(output) as source:
                    if source.format != "PNG":
                        raise CaptureError("Screenshot is not a PNG image")

                    width, height = source.size
                    if width <= 0 or height <= 0:
                        raise CaptureError("Screenshot has invalid dimensions")
                    if width * height > self.max_pixels:
                        raise CaptureError("Screenshot exceeds the pixel limit")

                    source.load()
                    return source.copy()
            except CaptureError:
                raise
            except Exception as exc:
                raise CaptureError("Screenshot image is invalid or unreadable") from exc
