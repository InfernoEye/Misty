import threading
import time
import unittest

from app.engine.clipboard_bridge import ClipboardBridge


class ClipboardExpiryTests(unittest.TestCase):
    def test_expired_request_is_not_executed(self):
        bridge = ClipboardBridge()
        writes = []
        bridge._write_wayland_text = lambda text: writes.append(text)
        bridge._write_qt_text = lambda text: writes.append(text)

        request = {
            "operation": "write",
            "value": "private text",
            "event": threading.Event(),
            "result": None,
            "expires_at": time.monotonic() - 1,
        }
        bridge._handle_request(request)

        self.assertEqual(writes, [])
        self.assertFalse(request["result"]["success"])
        self.assertIn("timed out", request["result"]["message"])
        self.assertTrue(request["event"].is_set())

    def test_unexpired_request_still_reaches_normal_checks(self):
        bridge = ClipboardBridge()
        request = {
            "operation": "write",
            "value": "text",
            "event": threading.Event(),
            "result": None,
            "expires_at": time.monotonic() + 60,
        }
        bridge._handle_request(request)

        self.assertIsInstance(request["result"], dict)
        self.assertTrue(request["event"].is_set())


if __name__ == "__main__":
    unittest.main()
