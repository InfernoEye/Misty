import json
import unittest

from app.brain.agent import MistyAgent, TOOL_DEFINITIONS


class FakeRegistry:
    """Named-window registry stub that also records lookups and executions."""

    def __init__(self, window=None, resolution=None, resolutions=None):
        self.window = window
        self.resolution = resolution or {"success": False, "message": "none"}
        self.resolutions = dict(resolutions or {})
        self.lookups = []
        self.executed = []
        self.messages = []

    def captured_active_window(self):
        return self.window

    def resolve_window_target(self, target, allow_fuzzy=False):
        self.lookups.append((target, allow_fuzzy))
        if target in self.resolutions:
            return self.resolutions[target]
        if callable(self.resolution):
            return self.resolution(target)
        return self.resolution

    def available_tools(self):
        return []

    def requires_confirmation(self, name):
        return False

    def execute(self, name, arguments, confirmed=False):
        self.executed.append((name, dict(arguments)))
        result = {"success": True, "message": "simulated success"}
        self.messages.append(str(result["message"]))
        return result


class WindowlessToolsRegistry(FakeRegistry):
    """Offers the real tool schema so plan validation still works."""

    def available_tools(self):
        return list(TOOL_DEFINITIONS)


def window(window_id, title=None, app_id=None, **extra):
    entry = {"id": window_id, "title": title or window_id}
    if app_id:
        entry["app_id"] = app_id
    entry.update(extra)
    return entry


BOTH = [
    window("firefox-id", "Firefox", "firefox"),
    window("discord-id", "Discord", "discord"),
]


class WindowContextTests(unittest.TestCase):
    def agent(self, snapshot=None, resolution=None, resolutions=None):
        return MistyAgent(
            registry=FakeRegistry(
                snapshot, resolution=resolution, resolutions=resolutions
            )
        )

    @staticmethod
    def plan(arguments=None, tool="move_window_to_screen"):
        return {
            "steps": [
                {
                    "id": "1",
                    "tool": tool,
                    "arguments": arguments or {"screen_name": "main"},
                    "depends_on": [],
                }
            ]
        }

    # -- deictic saved window -------------------------------------------

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
        registry = FakeRegistry(None)
        error = MistyAgent(registry=registry)._hydrate_window_context(
            plan, "Move this window to the main screen."
        )
        self.assertIn("could not identify", error)
        self.assertNotIn("window_id", plan["steps"][0]["arguments"])
        self.assertEqual(registry.lookups, [])

    def test_omitted_target_with_deictic_request_uses_snapshot(self):
        plan = self.plan({"screen_name": "main"})
        error = self.agent(
            {"id": "win-1", "title": "Firefox"}
        )._hydrate_window_context(plan, "Move it to the main screen")
        self.assertIsNone(error)
        self.assertEqual(plan["steps"][0]["arguments"]["window_id"], "win-1")

    def test_missing_target_without_deictic_asks(self):
        plan = self.plan()
        error = self.agent(
            {"id": "active-id", "title": "Console"}
        )._hydrate_window_context(plan, "Move the window to the main screen.")
        self.assertIn("Which window", error)

    # -- named targets ---------------------------------------------------

    def test_named_window_is_not_replaced(self):
        plan = self.plan({"window_id": "firefox-id", "screen_name": "main"})
        registry = FakeRegistry(
            {"id": "active-id", "title": "Console"},
            resolution={
                "success": True,
                "window_id": "firefox-id",
                "window": {"id": "firefox-id", "title": "Firefox"},
            },
        )
        agent = MistyAgent(registry=registry)
        error = agent._hydrate_window_context(
            plan, "Move Firefox to the main screen."
        )
        self.assertIsNone(error)
        self.assertEqual(plan["steps"][0]["arguments"]["window_id"], "firefox-id")
        # A real, non-alias id present in the plan is kept.
        self.assertEqual(registry.lookups, [])

    def test_named_window_overrides_model_choice(self):
        plan = self.plan({
            "window_id": "console-id",
            "screen_name": "main",
        })
        registry = FakeRegistry(
            {"id": "console-id", "title": "Konsola"},
            resolution={
                "success": True,
                "window_id": "equicord-id",
                "window": {"id": "equicord-id", "title": "Equicord"},
            },
        )
        error = MistyAgent(registry=registry)._hydrate_window_context(
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

    def test_single_title_case_name_is_not_fuzzy_matched(self):
        # A transcribed single Title-case word must not fuzzy-match some
        # other application; only exact identity/title matches apply.
        plan = self.plan()
        registry = FakeRegistry(None)
        error = MistyAgent(registry=registry)._hydrate_window_context(
            plan, "Move Equicord to the main screen."
        )
        self.assertIn("couldn't find", error)
        self.assertEqual(registry.lookups, [("Equicord", False)])

    def test_brand_casing_still_allows_fuzzy_alias(self):
        # "EquiCord" is the brand spelling, not a plain Title-case echo.
        plan = self.plan()
        registry = FakeRegistry(
            None, resolution={"success": True, "window_id": "discord-id"}
        )
        error = MistyAgent(registry=registry)._hydrate_window_context(
            plan, "Move EquiCord to the main screen."
        )
        self.assertIsNone(error)
        self.assertEqual(registry.lookups, [("EquiCord", True)])
        self.assertEqual(
            plan["steps"][0]["arguments"]["window_id"], "discord-id"
        )

    def test_lowercase_or_multiword_names_allow_fuzzy(self):
        seen = []

        def resolution(target):
            seen.append(target)
            return {"success": True, "window_id": "discord-id"}

        plan = self.plan()
        error = self.agent(None, resolution=resolution)._hydrate_window_context(
            plan, "move discord to the left screen"
        )
        self.assertIsNone(error)
        self.assertEqual(seen, ["discord"])
        self.assertEqual(plan["steps"][0]["arguments"]["window_id"], "discord-id")

        plan = self.plan()
        self.agent(None, resolution=resolution)._hydrate_window_context(
            plan, "Move EquiCord window to the left screen"
        )
        self.assertEqual(seen[-1].casefold(), "equicord")

    # -- per-step routing ------------------------------------------------

    def test_two_named_windows_get_their_own_targets(self):
        plan = {
            "steps": [
                {
                    "id": "1",
                    "tool": "move_window_to_screen",
                    "arguments": {"window_id": "konsola-id", "screen_name": "left"},
                    "depends_on": [],
                },
                {
                    "id": "2",
                    "tool": "move_window_to_screen",
                    "arguments": {"window_id": "konsola-id", "screen_name": "right"},
                    "depends_on": [],
                },
            ]
        }
        registry = FakeRegistry(resolutions={
            "Firefox": {"success": True, "window_id": "firefox-id"},
            "Discord": {"success": True, "window_id": "discord-id"},
        })
        error = MistyAgent(registry=registry)._hydrate_window_context(
            plan, "Move Firefox to the left screen and Discord to the right screen"
        )
        self.assertIsNone(error)
        self.assertEqual(
            plan["steps"][0]["arguments"]["window_id"], "firefox-id"
        )
        self.assertEqual(
            plan["steps"][1]["arguments"]["window_id"], "discord-id"
        )
        self.assertEqual(sorted(registry.lookups), [
            ("Discord", False), ("Firefox", False),
        ])

    def test_two_named_windows_without_punctuation(self):
        plan = {
            "steps": [
                {
                    "id": "1",
                    "tool": "move_window_to_screen",
                    "arguments": {"screen_name": "left"},
                    "depends_on": [],
                },
                {
                    "id": "2",
                    "tool": "move_window_to_screen",
                    "arguments": {"screen_name": "right"},
                    "depends_on": [],
                },
            ]
        }
        registry = FakeRegistry(resolutions={
            "Firefox": {"success": True, "window_id": "firefox-id"},
            "Discord": {"success": True, "window_id": "discord-id"},
        })
        error = MistyAgent(registry=registry)._hydrate_window_context(
            plan, "Move Firefox to the left screen and Discord to the right screen"
        )
        self.assertIsNone(error)
        self.assertEqual(
            [step["arguments"]["window_id"] for step in plan["steps"]],
            ["firefox-id", "discord-id"],
        )

    def test_single_named_window_is_not_duplicated_across_steps(self):
        plan = {
            "steps": [
                {
                    "id": "1",
                    "tool": "move_window_to_screen",
                    "arguments": {"screen_name": "left"},
                    "depends_on": [],
                },
                {
                    "id": "2",
                    "tool": "move_window_to_screen",
                    "arguments": {"screen_name": "right"},
                    "depends_on": [],
                },
            ]
        }
        registry = FakeRegistry(resolutions={
            "Firefox": {"success": True, "window_id": "firefox-id"},
        })
        error = MistyAgent(registry=registry)._hydrate_window_context(
            plan, "Move Firefox to the left screen and to the right screen"
        )
        self.assertIsNotNone(error)
        self.assertIn("only one window", error)
        self.assertIn("2 window action", error)
        self.assertEqual(
            plan["steps"][1]["arguments"].get("window_id"), None
        )

    def test_two_deictic_steps_use_snapshot(self):
        plan = {
            "steps": [
                {
                    "id": "1",
                    "tool": "move_window_to_screen",
                    "arguments": {"screen_name": "left"},
                    "depends_on": [],
                },
                {
                    "id": "2",
                    "tool": "maximize_window",
                    "arguments": {},
                    "depends_on": [],
                },
            ]
        }
        error = self.agent(
            {"id": "win-1", "title": "Firefox"}
        )._hydrate_window_context(
            plan,
            "Move this window to the left screen and maximize it",
        )
        self.assertIsNone(error)
        self.assertEqual(
            [step["arguments"]["window_id"] for step in plan["steps"]],
            ["win-1", "win-1"],
        )

    # -- non-window plans ------------------------------------------------

    def test_file_move_plan_has_no_window_lookup(self):
        plan = {
            "steps": [
                {
                    "id": "1",
                    "tool": "move_path",
                    "arguments": {
                        "folder": "Downloads",
                        "relative_path": "report.txt",
                        "destination_folder": "Documents",
                    },
                    "depends_on": [],
                }
            ]
        }
        registry = FakeRegistry(None)
        error = MistyAgent(registry=registry)._hydrate_window_context(
            plan, "Move report.txt to Documents."
        )
        self.assertIsNone(error)
        self.assertEqual(registry.lookups, [])
        self.assertEqual(
            plan["steps"][0]["arguments"]["destination_folder"], "Documents"
        )
        self.assertNotIn("window_id", plan["steps"][0]["arguments"])

    def test_mixed_plan_only_touches_window_steps(self):
        plan = {
            "steps": [
                {
                    "id": "1",
                    "tool": "move_path",
                    "arguments": {
                        "folder": "Downloads",
                        "relative_path": "report.txt",
                        "destination_folder": "Documents",
                    },
                    "depends_on": [],
                },
                {
                    "id": "2",
                    "tool": "move_window_to_screen",
                    "arguments": {"screen_name": "left"},
                    "depends_on": [],
                },
            ]
        }
        error = self.agent(
            {"id": "win-1", "title": "Firefox"}
        )._hydrate_window_context(
            plan, "Move report.txt to Documents and move this window to the left"
        )
        self.assertIsNone(error)
        self.assertEqual(
            plan["steps"][0]["arguments"]["destination_folder"], "Documents"
        )
        self.assertEqual(plan["steps"][1]["arguments"]["window_id"], "win-1")


class ClauseSplittingTests(unittest.TestCase):
    def test_splits_on_boundaries(self):
        self.assertEqual(
            MistyAgent.split_request_clauses(
                "Move Firefox to the left screen and Discord to the right screen"
            ),
            [
                "Move Firefox to the left screen",
                "Discord to the right screen",
            ],
        )
        self.assertEqual(
            MistyAgent.split_request_clauses(
                "Move Firefox to the left screen, and move Discord right"
            ),
            ["Move Firefox to the left screen", "move Discord right"],
        )
        self.assertEqual(
            MistyAgent.split_request_clauses(
                "Move report.txt to Documents. Then maximize Firefox"
            ),
            ["Move report.txt to Documents", "Then maximize Firefox"],
        )
        self.assertEqual(
            MistyAgent.split_request_clauses("Close Firefox; minimize Discord"),
            ["Close Firefox", "minimize Discord"],
        )
        self.assertEqual(MistyAgent.split_request_clauses("  "), [])


def plan_response(steps, response=""):
    payload = {"steps": steps, "response": response}
    return {"content": json.dumps(payload, ensure_ascii=False)}


class TestableAgent(MistyAgent):
    """Agent whose model server and lifecycle are replaced by fakes."""

    def __init__(self, registry, responses):
        super().__init__(registry=registry)
        self.responses = list(responses)

    def start(self):
        pass

    def stop(self):
        pass

    def _chat(self, messages, tools=None, response_format=None):
        if not self.responses:
            raise AssertionError("Unexpected extra model request")
        return self.responses.pop(0)


class RunIntegrationTests(unittest.TestCase):
    """End-to-end through ``run()`` with a fake model and registry."""

    def test_file_request_makes_no_window_lookup(self):
        registry = WindowlessToolsRegistry(None)
        agent = TestableAgent(registry, [
            plan_response([{
                "id": "1",
                "tool": "write_text_file",
                "arguments": {
                    "folder": "Downloads",
                    "relative_path": "report.txt",
                    "content": "hello",
                },
                "depends_on": [],
            }]),
            {"content": "Saved."},
        ])

        agent.run("Move report.txt to Documents.")

        self.assertEqual(registry.lookups, [])
        self.assertNotIn("couldn't find", "".join(registry.messages))

    def test_two_named_windows_reach_execution_with_own_targets(self):
        registry = WindowlessToolsRegistry(
            None,
            resolutions={
                "Firefox": {"success": True, "window_id": "firefox-id"},
                "Discord": {"success": True, "window_id": "discord-id"},
            },
        )
        steps = [
            {
                "id": "1",
                "tool": "move_window_to_screen",
                "arguments": {"window_id": "this", "screen_name": "left"},
                "depends_on": [],
            },
            {
                "id": "2",
                "tool": "move_window_to_screen",
                "arguments": {"window_id": "this", "screen_name": "right"},
                "depends_on": [],
            },
        ]
        agent = TestableAgent(
            registry,
            [plan_response(steps), {"content": "Moved both."}],
        )

        agent.run(
            "Move Firefox to the left screen and Discord to the right screen"
        )

        executed = [
            arguments.get("window_id")
            for name, arguments in registry.executed
            if name == "move_window_to_screen"
        ]
        self.assertEqual(executed, ["firefox-id", "discord-id"])

    def test_single_named_window_for_two_steps_is_not_executed(self):
        registry = WindowlessToolsRegistry(
            None, resolution={"success": True, "window_id": "firefox-id"}
        )
        steps = [
            {
                "id": "1",
                "tool": "move_window_to_screen",
                "arguments": {"window_id": "this", "screen_name": "left"},
                "depends_on": [],
            },
            {
                "id": "2",
                "tool": "move_window_to_screen",
                "arguments": {"window_id": "this", "screen_name": "right"},
                "depends_on": [],
            },
        ]
        agent = TestableAgent(
            registry, [plan_response(steps)]
        )

        answer = agent.run(
            "Move Firefox to the left screen and to the right screen"
        )

        self.assertIn("only one window", answer)
        self.assertEqual(
            [name for name, _a in registry.executed if name.startswith("move")],
            [],
        )


if __name__ == "__main__":
    unittest.main()
