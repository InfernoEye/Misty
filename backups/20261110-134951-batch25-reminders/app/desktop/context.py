"""Unified, read-only snapshot of the current KDE desktop."""

from copy import deepcopy
from datetime import datetime, timezone


class DesktopContext:
    """Combines existing KWin window and display discovery into one snapshot.

    The provider must expose:
        list_windows() -> dict
        list_screens() -> dict

    This service never captures screenshots or performs desktop actions.
    """

    def __init__(self, window_service):
        self._window_service = window_service

    def snapshot(self):
        """Return a consistent, explicitly validated desktop snapshot."""
        captured_at = datetime.now(timezone.utc).isoformat()

        try:
            window_result = self._window_service.list_windows()
            if not isinstance(window_result, dict):
                raise RuntimeError("Window service returned an invalid response.")

            if not window_result.get("success"):
                raise RuntimeError(
                    window_result.get("message", "Window discovery failed.")
                )

            screen_result = self._window_service.list_screens()
            if not isinstance(screen_result, dict):
                raise RuntimeError("Display service returned an invalid response.")

            if not screen_result.get("success"):
                raise RuntimeError(
                    screen_result.get("message", "Display discovery failed.")
                )

            windows = window_result.get("windows")
            screens = screen_result.get("screens")
            overlays = window_result.get("overlays", [])

            if not isinstance(windows, list):
                raise RuntimeError("Window service returned an invalid window list.")

            if not isinstance(screens, list):
                raise RuntimeError("Display service returned an invalid screen list.")

            if not isinstance(overlays, list):
                raise RuntimeError("Window service returned an invalid overlay list.")

            active_windows = [
                window for window in windows
                if isinstance(window, dict) and window.get("active") is True
            ]

            if len(active_windows) > 1:
                raise RuntimeError(
                    "KWin reported multiple active application windows."
                )

            active_window = (
                deepcopy(active_windows[0]) if active_windows else None
            )

            return {
                "success": True,
                "captured_at": captured_at,
                "active_window": active_window,
                "windows": deepcopy(windows),
                "overlays": deepcopy(overlays),
                "screens": deepcopy(screens),
                "window_count": len(windows),
                "screen_count": len(screens),
                "message": "Desktop context captured successfully.",
            }

        except Exception as error:
            return {
                "success": False,
                "captured_at": captured_at,
                "active_window": None,
                "windows": [],
                "screens": [],
                "window_count": 0,
                "screen_count": 0,
                "message": f"Could not capture desktop context: {error}",
            }
