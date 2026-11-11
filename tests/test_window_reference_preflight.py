"""Window preflight must not resolve deferred discovery placeholders."""
import unittest
from types import SimpleNamespace

from app.brain.agent import MistyAgent


class Registry:
    def __init__(self):
        self.lookups = []

    def resolve_window_target(self, target, allow_fuzzy=False):
        self.lookups.append(target)
        return {"success": False, "message": "No open window matches target"}


class WindowReferencePreflightTests(unittest.TestCase):
    def test_deferred_reference_is_not_looked_up_as_window_title(self):
        registry = Registry()
        agent = MistyAgent(registry=registry)
        for reference in (
            "$steps.step-1.result.window_id",
            "$steps.step-1.result.windows.0.id",
        ):
            with self.subTest(reference=reference):
                arguments = {"window_id": reference}
                plan = SimpleNamespace(steps=[SimpleNamespace(
                    name="focus_window", arguments=arguments,
                )])
                self.assertIsNone(agent._preflight_window_targets(
                    plan, {"focus_window"},
                ))
                self.assertEqual(arguments["window_id"], reference)
        self.assertEqual(registry.lookups, [])

    def test_real_unknown_target_still_fails_preflight(self):
        registry = Registry()
        agent = MistyAgent(registry=registry)
        plan = SimpleNamespace(steps=[SimpleNamespace(
            name="focus_window", arguments={"window_id": "Discord"},
        )])
        error = agent._preflight_window_targets(plan, {"focus_window"})
        self.assertIn("couldn't find a window matching 'Discord'", error)
        self.assertEqual(registry.lookups, ["Discord"])


if __name__ == "__main__":
    unittest.main()
