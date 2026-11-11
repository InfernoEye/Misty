"""Secure, on-demand desktop screenshot capture using KDE Spectacle."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image


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
    ) -> None:
        self.context_provider = context_provider
        self.target_window_provider = target_window_provider
        self.spectacle_path = spectacle_path
        self.timeout = timeout
        self.max_pixels = max_pixels
        self.max_file_bytes = max_file_bytes

    def capture(
        self,
        mode: str = "desktop",
        screen_name: str | None = None,
    ) -> CaptureResult:
        if mode not in {"desktop", "active_window", "screen"}:
            raise ValueError("mode must be desktop, active_window, or screen")

        if mode == "screen":
            if not screen_name:
                raise ValueError("screen_name is required for screen capture")
            image = self._capture_screen(screen_name)
        else:
            if screen_name is not None:
                raise ValueError("screen_name is only valid for screen capture")
            if mode == "active_window" and self.target_window_provider is not None:
                image = self._capture_target_window()
            else:
                options = (
                    ["--activewindow"]
                    if mode == "active_window"
                    else ["--fullscreen"]
                )
                image = self._capture_image(options)

        return CaptureResult(
            image=image,
            mode=mode,
            captured_at=datetime.now(timezone.utc).isoformat(),
            width=image.width,
            height=image.height,
            screen_name=screen_name,
        )

    def _capture_target_window(self) -> Image.Image:
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
            if isinstance(window, dict) and window.get("id") == saved["id"]
        ]
        if len(matches) != 1:
            raise CaptureError(
                "The application that was active when listening began "
                "is no longer available"
            )

        target = matches[0]
        if target.get("minimized") is True:
            raise CaptureError("The previously active application is minimized")

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

        return desktop.crop((left, top, right, bottom))

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

        return desktop_image.crop((left, top, right, bottom))

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
