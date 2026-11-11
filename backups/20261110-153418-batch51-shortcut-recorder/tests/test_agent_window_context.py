import unittest

from app.brain.agent import MistyAgent


class FakeRegistry:
    def __init__(self, window=None, resolution=None):
        self.window = window
        self.resolution = resolution or {
            "success": False,
            "message": "none",
        }

    def captured_active_window(self):
        return self.window

    def resolve_window_target(self, target, allow_fuzzy=False):
        if callable(self.resolution):
            return self.resolution(target)
        return self.resolution


class WindowContextTests(unittest.TestCase):
    def agent(self, window, resolution=None):
        return MistyAgent(
            registry=FakeRegistry(window, resolution=resolution)
        )

    @staticmethod
    def plan(arguments=None):
        return {
            "steps": [
                {
                    "id": "1",
                    "tool": "move_window_to_screen",
                    "arguments": arguments or {"screen_name": "main"},
                    "depends_on": [],
                }
            ]
        }

    def test_fills_missing_target_for_this_window(self):
        plan = self.plan()
        error = self.agent(
            {"id": "win-1", "title": "Firefox"}
        )._hydrate_window_context(plan, "Move this window to the main screen.")
        self.assertIsNone(error)
        self.assertEqual(plan["steps"][0]["arguments"]["window_id"], "win-1")

    def test_misheard_cord_still_counts_as_this(self):
        plan = self.plan()
        error = self.agent(
            {"id": "win-1", "title": "Firefox"}
        )._hydrate_window_context(plan, "Move this cord to the main screen.")
        self.assertIsNone(error)
        self.assertEqual(plan["steps"][0]["arguments"]["window_id"], "win-1")

    def test_without_snapshot_asks_instead_of_validation_error(self):
        plan = self.plan()
        error = self.agent(None)._hydrate_window_context(
            plan, "Move this window to the main screen."
        )
        self.assertIn("could not identify", error)
        self.assertNotIn("window_id", plan["steps"][0]["arguments"])

    def test_named_window_is_not_replaced(self):
        plan = self.plan({"window_id": "firefox-id", "screen_name": "main"})
        error = self.agent(
            {"id": "active-id", "title": "Console"},
            resolution={
                "success": True,
                "window_id": "firefox-id",
                "window": {"id": "firefox-id", "title": "Firefox"},
            },
        )._hydrate_window_context(plan, "Move Firefox to the main screen.")
        self.assertIsNone(error)
        self.assertEqual(plan["steps"][0]["arguments"]["window_id"], "firefox-id")

    def test_named_window_overrides_model_choice(self):
        plan = self.plan({
            "window_id": "console-id",
            "screen_name": "main",
        })
        error = self.agent(
            {"id": "console-id", "title": "Konsola"},
            resolution={
                "success": True,
                "window_id": "equicord-id",
                "window": {"id": "equicord-id", "title": "Equicord"},
            },
        )._hydrate_window_context(
            plan, "Move EquiCord to the main screen."
        )
        self.assertIsNone(error)
        self.assertEqual(
            plan["steps"][0]["arguments"]["window_id"], "equicord-id"
        )

    def test_unknown_named_window_asks_instead_of_moving_wrong_one(self):
        plan = self.plan({
            "window_id": "console-id",
            "screen_name": "main",
        })
        error = self.agent(
            {"id": "console-id", "title": "Konsola"},
            resolution={"success": False, "message": "none"},
        )._hydrate_window_context(
            plan, "Move EquiCord to the main screen."
        )
        self.assertIn("couldn't find", error)
        self.assertEqual(
            plan["steps"][0]["arguments"]["window_id"], "console-id"
        )

    def test_named_window_ambiguity_asks(self):
        plan = self.plan()
        error = self.agent(
            None,
            resolution={
                "success": False,
                "ambiguous": True,
                "candidates": [
                    {"id": "1", "title": "Equicord Stable"},
                    {"id": "2", "title": "Equicord Canary"},
                ],
            },
        )._hydrate_window_context(
            plan, "Move EquiCord to the main screen."
        )
        self.assertIn("Several windows", error)

    def test_missing_target_without_deictic_asks(self):
        plan = self.plan()
        error = self.agent(
            {"id": "active-id", "title": "Console"}
        )._hydrate_window_context(plan, "Move the window to the main screen.")
        self.assertIn("Which window", error)


if __name__ == "__main__":
    unittest.main()
