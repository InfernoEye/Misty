import unittest

from app.desktop.context import DesktopContext


class FakeDesktopProvider:
    def __init__(self):
        self.windows = [
            {
                "id": "window-1",
                "title": "Firefox",
                "app_id": "firefox",
                "active": False,
                "geometry": {"x": 0, "y": 0, "width": 1200, "height": 900},
            },
            {
                "id": "window-2",
                "title": "Dolphin",
                "app_id": "org.kde.dolphin",
                "active": True,
                "geometry": {"x": 1200, "y": 100, "width": 1400, "height": 900},
            },
        ]
        self.screens = [
            {
                "name": "DP-1",
                "priority": 1,
                "geometry": {"x": 1200, "y": 0, "width": 3440, "height": 1440},
            },
            {
                "name": "DP-2",
                "priority": 3,
                "geometry": {"x": 0, "y": 0, "width": 1200, "height": 1920},
            },
        ]

    def list_windows(self):
        return {"success": True, "windows": self.windows}

    def list_screens(self):
        return {"success": True, "screens": self.screens}


class DesktopContextTests(unittest.TestCase):
    def setUp(self):
        self.provider = FakeDesktopProvider()
        self.context = DesktopContext(self.provider)

    def test_combines_windows_screens_and_active_window(self):
        result = self.context.snapshot()

        self.assertTrue(result["success"])
        self.assertEqual(result["window_count"], 2)
        self.assertEqual(result["screen_count"], 2)
        self.assertEqual(result["active_window"]["id"], "window-2")
        self.assertEqual(result["screens"][0]["priority"], 1)
        self.assertTrue(result["captured_at"].endswith("+00:00"))

    def test_no_active_window_is_valid(self):
        for window in self.provider.windows:
            window["active"] = False

        result = self.context.snapshot()

        self.assertTrue(result["success"])
        self.assertIsNone(result["active_window"])

    def test_window_discovery_failure_fails_closed(self):
        self.provider.list_windows = lambda: {
            "success": False,
            "message": "KWin unavailable",
        }

        result = self.context.snapshot()

        self.assertFalse(result["success"])
        self.assertEqual(result["windows"], [])
        self.assertEqual(result["screens"], [])
        self.assertIn("KWin unavailable", result["message"])

    def test_screen_discovery_failure_fails_closed(self):
        self.provider.list_screens = lambda: {
            "success": False,
            "message": "Display discovery failed",
        }

        result = self.context.snapshot()

        self.assertFalse(result["success"])
        self.assertEqual(result["window_count"], 0)
        self.assertIn("Display discovery failed", result["message"])

    def test_snapshot_does_not_mutate_provider_data(self):
        result = self.context.snapshot()
        result["windows"][0]["title"] = "Changed"
        result["screens"][0]["geometry"]["x"] = -999

        self.assertEqual(self.provider.windows[0]["title"], "Firefox")
        self.assertEqual(self.provider.screens[0]["geometry"]["x"], 1200)

    def test_multiple_active_windows_are_rejected(self):
        self.provider.windows[0]["active"] = True

        result = self.context.snapshot()

        self.assertFalse(result["success"])
        self.assertIn("multiple active", result["message"])


if __name__ == "__main__":
    unittest.main()
