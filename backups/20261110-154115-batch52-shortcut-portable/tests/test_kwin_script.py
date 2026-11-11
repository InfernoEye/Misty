import threading
import unittest
from pathlib import Path

from app.engine.kwin_windows import KWinWindowService


class KWinScriptTests(unittest.TestCase):
    def setUp(self):
        self.service = KWinWindowService.__new__(KWinWindowService)
        self.service._startup_error = None
        self.service._thread = type(
            "Alive", (), {"is_alive": lambda _self: True}
        )()
        self.service._pending = {}
        self.service._pending_lock = threading.Lock()
        self.captured = []

    def test_generated_script_reports_overlays_and_visibility(self):
        def fake_qdbus(*arguments, timeout=5):
            if arguments[2].endswith("loadScript"):
                self.captured.append(
                    Path(arguments[3]).read_text(encoding="utf-8")
                )
                return "1"
            raise RuntimeError("script captured; stop before waiting")

        self.service._qdbus = fake_qdbus

        with self.assertRaises(RuntimeError):
            self.service._query()

        self.assertEqual(len(self.captured), 1)
        script = self.captured[0]

        for required in (
            "on_current_desktop",
            "overlays: overlays",
            "isMistyPopup",
            'appId === "misty-popup"',
            "compared > 0 ? false : null",
        ):
            self.assertIn(required, script)

        # The popup must stay out of the model-visible window array.
        self.assertNotIn("result.push(overlay", script)
        self.assertIn("overlays.push(", script)


if __name__ == "__main__":
    unittest.main()
