"""Exercise the real shared name->ID binding, never a live window service.

These fixtures deliberately avoid branding in titles and simple resource
names. A service-level alias test alone cannot expose hydration preserving a
spoken application name as though it were an authoritative window ID.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

from app.brain.agent import MistyAgent
from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class CatalogExecutor:
    def __init__(self, windows, failure=None):
        self.windows = windows
        self.failure = failure
        self.mutations = []

    def list_windows(self):
        if self.failure is not None:
            return deepcopy(self.failure)
        return {"success": True, "windows": deepcopy(self.windows)}

    def list_screens(self):
        return {"success": True, "screens": [{"name": "DP-1", "priority": 1,
                 "geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080}}]}


class SharedWindowBindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.registry_index = 0

    def agent(self, windows, failure=None):
        self.registry_index += 1
        executor = CatalogExecutor(windows, failure=failure)
        registry = ToolRegistry(executor=executor, memory_store=MemoryStore(
            Path(self.directory.name) / f"memory-{self.registry_index}.sqlite3"))
        agent = MistyAgent(registry=registry)
        agent._chat = Mock(side_effect=AssertionError("Obvious live identity needs no model"))
        return agent, executor

    def bind(self, agent, name, *, tool="move_window_to_screen", raw_target=None,
             omit_target=False):
        verbs = {"move_window_to_screen": "Move", "focus_window": "Focus",
                 "maximize_window": "Maximize", "minimize_window": "Minimize",
                 "restore_window": "Restore", "close_window": "Close"}
        arguments = {} if omit_target else {"window_id": name if raw_target is None else raw_target}
        if tool == "move_window_to_screen":
            arguments["screen_name"] = "main"
        payload = {"steps": [{"id": "target", "tool": tool,
                              "arguments": arguments, "depends_on": []}]}
        transcript = verbs[tool] + " " + name
        if tool == "move_window_to_screen":
            transcript += " to the main screen"
        error = agent._hydrate_window_context(payload, transcript)
        if error:
            return None, error
        bound = payload["steps"][0]["arguments"]
        plan = SimpleNamespace(steps=[SimpleNamespace(name=tool, arguments=bound)])
        error = agent._preflight_window_targets(plan, {tool})
        return (None, error) if error else (bound["window_id"], None)

    def assert_bound(self, windows, name, expected_id, **kwargs):
        agent, executor = self.agent(windows)
        window_id, error = self.bind(agent, name, **kwargs)
        self.assertIsNone(error, error)
        self.assertEqual(window_id, expected_id)
        agent._chat.assert_not_called()
        self.assertEqual(executor.mutations, [])

    def test_dolphin_reverse_dns_identity_not_title_branding(self):
        self.assert_bound([{"id": "dolphin-id", "title": "Home",
                            "app_id": "org.kde.dolphin", "resource_class": "org.kde.dolphin"}],
                          "Dolphin", "dolphin-id")

    def test_firefox_with_nonbrand_tab_title(self):
        self.assert_bound([{"id": "browser-id", "title": "Account dashboard",
                            "app_id": "org.mozilla.firefox", "resource_class": "org.mozilla.firefox"}],
                          "Firefox", "browser-id")

    def test_generic_unlisted_app_uses_desktop_identity(self):
        self.assert_bound([{"id": "nebula-id", "title": "Daily Notes",
                            "desktop_file": "io.aurora.Nebula.desktop"}],
                          "Nebula", "nebula-id")

    def test_spoken_titlecase_typo_uses_identity_not_app_specific_alias(self):
        self.assert_bound([{"id": "nebula-id", "title": "Daily Notes",
                            "app_id": "nebula"}], "Nebulla", "nebula-id")

    def test_planner_omitted_target_resolves_same_user_spelling(self):
        cases = [
            ("Discord", {"id": "chat-id", "title": "Dragon Cave", "app_id": "equicord"}),
            ("Nebulla", {"id": "notes-id", "title": "Daily Notes", "app_id": "nebula"}),
        ]
        for name, window in cases:
            with self.subTest(name=name):
                self.assert_bound([window], name, window["id"], omit_target=True)

    def test_existing_client_aliases_survive_shared_hydration(self):
        windows = [{"id": "chat-id", "title": "Dragon Cave",
                    "app_id": "equicord", "resource_class": "equicord"}]
        for name in ("Discord", "Equicord", "Aquicord"):
            with self.subTest(name=name):
                self.assert_bound(windows, name, "chat-id")

    def test_real_identity_outranks_terminal_title_that_mentions_app(self):
        windows = [
            {"id": "terminal-id", "title": "Dolphin", "app_id": "foot", "resource_class": "foot"},
            {"id": "dolphin-id", "title": "Home", "app_id": "org.kde.dolphin"},
        ]
        self.assert_bound(windows, "Dolphin", "dolphin-id")

    def test_shared_binding_covers_all_window_control_tools(self):
        windows = [{"id": "dolphin-id", "title": "Home", "app_id": "org.kde.dolphin"}]
        for tool in ("focus_window", "maximize_window", "minimize_window", "restore_window", "close_window"):
            with self.subTest(tool=tool):
                self.assert_bound(windows, "Dolphin", "dolphin-id", tool=tool)

    def test_ambiguous_identity_does_not_select_first_window(self):
        agent, executor = self.agent([
            {"id": "one", "title": "Home", "app_id": "org.kde.dolphin"},
            {"id": "two", "title": "Downloads", "app_id": "org.kde.dolphin"},
        ])
        window_id, error = self.bind(agent, "Dolphin")
        self.assertIsNone(window_id)
        self.assertIn("Several windows", error)
        self.assertIn("Home", error)
        self.assertIn("Downloads", error)
        self.assertEqual(executor.mutations, [])

    def test_authoritative_uuid_is_preserved_not_fuzzy_guessed(self):
        exact_id = "{23958a0b-2112-491a-bacb-000000000001}"
        self.assert_bound([{"id": exact_id, "title": "Home", "app_id": "org.kde.dolphin"}],
                          "this window", exact_id, raw_target=exact_id)
        agent, executor = self.agent([{"id": exact_id, "title": "Home", "app_id": "org.kde.dolphin"}])
        window_id, error = self.bind(agent, "this window", raw_target="{23958a0b-2112-491a-bacb-000000000002}")
        self.assertIsNone(window_id)
        self.assertIsNotNone(error)
        self.assertEqual(executor.mutations, [])

    def test_named_lookup_preserves_catalog_service_failure(self):
        failure = {"success": False, "code": "window_service_unavailable",
                   "message": "Could not list open windows: catalog service unavailable"}
        agent, executor = self.agent([], failure=failure)
        window_id, error = self.bind(agent, "Dolphin", omit_target=True)
        self.assertIsNone(window_id)
        self.assertIn("catalog service unavailable", error)
        self.assertNotIn("couldn't find", error)
        self.assertEqual(executor.mutations, [])


if __name__ == "__main__":
    unittest.main()
