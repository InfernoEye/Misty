import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeBrowser:
    def __init__(self):
        self.calls = []

    def _result(self, action, data):
        return {
            "success": True,
            "action": action,
            "code": "ok",
            "message": f"{action} ok",
            "data": data,
        }

    def navigate(self, url, **kwargs):
        self.calls.append(("navigate", url, kwargs))
        return self._result("navigate", {
            "url": url, "title": "Page title",
        })

    def read(self, **kwargs):
        self.calls.append(("read", kwargs))
        return self._result("read", {
            "url": "https://www.youtube.com/results?search_query=x",
            "title": "YouTube",
            "text": "",
            "links": [
                {"url": "https://www.youtube.com/watch?v=1", "text": "First video"},
                {"url": "https://www.youtube.com/watch?v=2", "text": "Second video"},
                {"url": "https://www.youtube.com/watch?v=3", "text": "Third video"},
                {"url": "https://www.youtube.com/watch?v=1", "text": "First video"},
            ],
            "truncated": False,
            "links_truncated": False,
        })

    def click(self, selector, **kwargs):
        self.calls.append(("click", selector, kwargs))
        return self._result("click", {
            "url": "https://example.org/", "title": "Page title",
            "text": "Preview",
        })

    def fill(self, selector, text, **kwargs):
        self.calls.append(("fill", selector, text, kwargs))
        return self._result("fill", {
            "url": "https://example.org/", "title": "Page title",
        })

    def press(self, key):
        self.calls.append(("press", key))
        return self._result("press", {
            "url": "https://example.org/", "title": "Page title",
            "text": "Preview",
        })


class BrowserToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.browser = FakeBrowser()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            browser_session=self.browser,
        )

    def test_browser_tools_are_advertised_only_with_session(self):
        names = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertIn("browser_navigate", names)
        self.assertIn("browser_read", names)
        without = ToolRegistry(
            executor=Mock(), memory_store=self.memory
        )
        names = {
            item["function"]["name"]
            for item in without.available_tools()
        }
        self.assertNotIn("browser_navigate", names)

    def test_navigate_and_read_are_forwarded(self):
        result = self.registry.execute(
            "browser_navigate", {"url": "https://example.org/"}
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["title"], "Page title")

        result = self.registry.execute("browser_read", {})
        self.assertTrue(result["success"])
        self.assertEqual(result["links"][0]["url"], "https://www.youtube.com/watch?v=1")
        self.assertEqual(self.browser.calls[0][0], "navigate")
        self.assertEqual(self.browser.calls[1][0], "read")

    def test_site_choices_returns_bounded_unique_videos(self):
        choices = self.registry.site_choices("youtube", "minecraft", 2)
        self.assertEqual(len(choices), 2)
        self.assertEqual(choices[0]["title"], "First video")
        self.assertEqual(choices[0]["url"], "https://www.youtube.com/watch?v=1")
        self.assertEqual(choices[1]["title"], "Second video")

    def test_interaction_is_forwarded(self):
        self.registry.execute(
            "browser_click", {"selector": "#go"}, confirmed=True
        )
        self.registry.execute(
            "browser_fill", {"selector": "#q", "text": "misty"}
        )
        self.registry.execute(
            "browser_press", {"key": "Enter"}, confirmed=True
        )
        self.assertEqual(
            [call[0] for call in self.browser.calls],
            ["click", "fill", "press"],
        )
        self.assertEqual(self.browser.calls[1][2], "misty")

    def test_invalid_arguments_do_not_touch_browser(self):
        for name, arguments in (
            ("browser_navigate", {}),
            ("browser_click", {}),
            ("browser_fill", {"selector": "#q"}),
            ("browser_press", {"key": "F13"}),
            ("browser_read", {"command": "run"}),
        ):
            with self.subTest(name=name):
                result = self.registry.execute(name, arguments)
                self.assertFalse(result["success"])
        self.assertEqual(self.browser.calls, [])

    def test_click_and_press_require_confirmation(self):
        self.assertTrue(self.registry.requires_confirmation("browser_click"))
        self.assertTrue(self.registry.requires_confirmation("browser_press"))
        self.assertFalse(self.registry.requires_confirmation("browser_read"))
        self.assertFalse(self.registry.requires_confirmation("browser_fill"))


if __name__ == "__main__":
    unittest.main()
