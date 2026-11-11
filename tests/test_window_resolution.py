import tempfile
import unittest
from pathlib import Path

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeExecutor:
    def __init__(self, windows=None, screens=None):
        self.windows = windows or []
        self.screens = screens or []

    def list_windows(self):
        return {"success": True, "windows": self.windows}

    def list_screens(self):
        return {"success": True, "screens": self.screens}


class BrokenExecutor:
    def list_windows(self):
        raise RuntimeError("no window service")

    def list_screens(self):
        raise RuntimeError("no display service")


def make_registry(executor):
    directory = tempfile.TemporaryDirectory()
    registry = ToolRegistry(
        executor=executor,
        memory_store=MemoryStore(Path(directory.name) / "m.sqlite3"),
    )
    registry._tmpdir = directory
    return registry


def title_mentions_equicord_terminal():
    return {
        "id": "terminator-1",
        "title": "~/projects — Equicord troubleshooting — terminal",
        "app_id": "terminator",
        "resource_name": "terminator",
        "resource_class": "Terminator",
    }


def discord_window():
    return {
        "id": "discord-1",
        "title": "Discord",
        "app_id": "discord",
        "resource_name": "discord",
        "resource_class": "discord",
    }


class WindowIdentityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.cleanups = []

    def tearDown(self):
        for cleanup in self.cleanups:
            cleanup()

    def registry(self, executor):
        registry = make_registry(executor)
        self.cleanups.append(registry._tmpdir.cleanup)
        return registry

    # -- title text vs application identity -----------------------------

    def test_terminal_title_mentioning_app_loses_to_real_app(self):
        registry = self.registry(
            FakeExecutor([title_mentions_equicord_terminal(), discord_window()])
        )
        result = registry.resolve_window_target("EquiCord", allow_fuzzy=True)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["window_id"], "discord-1")

    def test_terminal_title_mentioning_app_is_not_chosen_without_app(self):
        # The only window merely mentions the name in its title. Without
        # application identity and without fuzzy alias permission, that is
        # incidental content, not the named window.
        registry = self.registry(
            FakeExecutor([title_mentions_equicord_terminal()])
        )
        result = registry.resolve_window_target("EquiCord", allow_fuzzy=False)
        self.assertFalse(result["success"], result)
        self.assertFalse(result.get("ambiguous"))

    def test_exact_title_beats_word_token_match(self):
        registry = self.registry(FakeExecutor([
            {"id": "t1", "title": "Firefox", "app_id": "foot"},
            {"id": "t2", "title": "Reading about Firefox tips", "app_id": "x"},
        ]))
        result = registry.resolve_window_target("Firefox")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["window_id"], "t1")

    def test_token_boundary_not_substring(self):
        # "fire" is a substring of "Firefox" but not a whole word there.
        registry = self.registry(FakeExecutor([
            {"id": "ff", "title": "Firefox", "app_id": "firefox"},
        ]))
        result = registry.resolve_window_target("fire")
        self.assertFalse(result["success"], result)

    def test_name_buried_in_unrelated_title_body_is_not_a_match(self):
        registry = self.registry(FakeExecutor([
            {
                "id": "doc",
                "title": "How to configure discord bots",
                "app_id": "libreoffice-writer",
            },
        ]))
        result = registry.resolve_window_target("discord", allow_fuzzy=True)
        self.assertFalse(result["success"], result)
        self.assertFalse(result.get("ambiguous"))

    def test_title_leading_with_the_name_matches(self):
        registry = self.registry(FakeExecutor([
            {"id": "ff", "title": "Firefox — Tips", "app_id": "foot"},
        ]))
        result = registry.resolve_window_target("Firefox")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["match"], "title_leading")

    def test_two_discord_windows_stay_ambiguous_by_identity(self):
        registry = self.registry(FakeExecutor([
            {"id": "d1", "title": "Discord", "app_id": "discord"},
            {"id": "d2", "title": "Discord Canary", "app_id": "discord"},
        ]))
        result = registry.resolve_window_target("Discord")
        self.assertFalse(result["success"])
        self.assertTrue(result.get("ambiguous"))
        self.assertEqual(
            {candidate["id"] for candidate in result["candidates"]},
            {"d1", "d2"},
        )

    def test_two_discord_windows_with_title_filter_resolve_identity_ambiguity(self):
        # Identity is shared, so a unique exact title among equal identity
        # matches is stronger than the rest.
        registry = self.registry(FakeExecutor([
            {"id": "d1", "title": "Discord Canary", "app_id": "discord"},
            {"id": "d2", "title": "Discord", "app_id": "discord"},
        ]))
        result = registry.resolve_window_target("Discord")
        self.assertFalse(result["success"])
        self.assertTrue(result.get("ambiguous"))

    def test_equicord_alias_beats_terminal_title_text(self):
        # EquiCord is a declared alias of the Discord identity, so the
        # terminal's title mentioning it must lose even though its title
        # text is the only literal occurrence.
        registry = self.registry(FakeExecutor([
            {"id": "t", "title": "Equicord troubleshooting", "app_id": "terminator"},
            {"id": "d", "title": "Discord", "app_id": "discord"},
        ]))
        result = registry.resolve_window_target("Equicord", allow_fuzzy=True)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["window_id"], "d")

    def test_unknown_query_returns_candidates_only_when_equally_strong(self):
        registry = self.registry(FakeExecutor([discord_window()]))
        result = registry.resolve_window_target("Writable")
        self.assertFalse(result["success"])
        self.assertFalse(result.get("ambiguous"))

    def test_exact_id_is_authoritative(self):
        registry = self.registry(FakeExecutor([discord_window()]))
        result = registry.resolve_window_target("discord-1")
        self.assertTrue(result["success"])
        self.assertEqual(result["match"], "id_exact")

    def test_empty_and_oversized_queries_are_rejected(self):
        registry = self.registry(FakeExecutor([discord_window()]))
        for value in ("", "   ", None, 5, "x" * 129):
            with self.subTest(value=value):
                result = registry.resolve_window_target(value)
                self.assertFalse(result["success"])

    def test_listing_failure_is_reported_not_raised(self):
        registry = self.registry(BrokenExecutor())
        result = registry.resolve_window_target("Discord")
        self.assertFalse(result["success"])
        self.assertIn("Could not list", result["message"])

    def test_garbage_listing_is_reported_not_raised(self):
        registry = self.registry(FakeExecutor([None, "x", {"no": "id"}]))
        result = registry.resolve_window_target("Discord")
        self.assertFalse(result["success"])


class ScreenAliasParityTests(unittest.TestCase):
    """Both screen code paths must make identical decisions."""

    LAYOUTS = {
        1: [{"name": "DP-1", "priority": 1, "geometry": {"x": 0}}],
        2: [
            {"name": "DP-1", "priority": 1, "geometry": {"x": 0}},
            {"name": "DP-2", "priority": 2, "geometry": {"x": 1920}},
        ],
        3: [
            {"name": "DP-1", "priority": 2, "geometry": {"x": 1200}},
            {"name": "DP-2", "priority": 3, "geometry": {"x": 0}},
            {"name": "DP-3", "priority": 1, "geometry": {"x": 4640}},
        ],
    }
    PHRASES = ("left", "right", "center", "middle", "primary", "main")

    def registry(self, screens):
        return make_registry(FakeExecutor(screens=screens))

    def test_alias_and_target_agree_for_every_layout(self):
        for count, screens in self.LAYOUTS.items():
            registry = self.registry(screens)
            for phrase in self.PHRASES:
                with self.subTest(screens=count, phrase=phrase):
                    target = registry.resolve_screen_target(phrase)
                    alias = registry._resolve_screen_alias(phrase)
                    if target.get("success"):
                        self.assertEqual(alias, target["screen_name"])
                    else:
                        self.assertIsNone(alias)

    def test_left_right_are_outermost_by_x(self):
        registry = self.registry(self.LAYOUTS[3])
        self.assertEqual(registry._resolve_screen_alias("left"), "DP-2")
        self.assertEqual(registry._resolve_screen_alias("right"), "DP-3")
        self.assertEqual(registry._resolve_screen_alias("center"), "DP-1")
        self.assertEqual(registry._resolve_screen_alias("primary"), "DP-3")

    def test_primary_without_priority_one_fails_instead_of_second_screen(self):
        screens = [
            {"name": "DP-1", "priority": 2, "geometry": {"x": 1200}},
            {"name": "DP-2", "priority": 2, "geometry": {"x": 0}},
            {"name": "DP-3", "priority": 3, "geometry": {"x": 4640}},
        ]
        registry = self.registry(screens)
        self.assertIsNone(registry._resolve_screen_alias("primary"))
        self.assertIsNone(registry._resolve_screen_alias("main"))
        result = registry.resolve_screen_target("primary")
        self.assertFalse(result["success"])
        self.assertIn("priority", result["message"])
        self.assertIn("connector", result["message"])

    def test_primary_without_priority_one_never_picks_sort_position(self):
        # Previously "primary" fell back to the second screen sorted by x;
        # ordering must not influence the failure.
        screens = [
            {"name": "A", "geometry": {"x": 5000}},
            {"name": "B", "geometry": {"x": 0}},
        ]
        registry = self.registry(screens)
        self.assertIsNone(registry._resolve_screen_alias("primary"))

    def test_even_layout_has_no_middle(self):
        registry = self.registry(self.LAYOUTS[2])
        for phrase in ("middle", "center", "centre"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(registry._resolve_screen_alias(phrase))
                result = registry.resolve_screen_target(phrase)
                self.assertFalse(result["success"])
                self.assertIn("connector", result["message"])

    def test_no_screens_fails_cleanly(self):
        registry = self.registry([])
        self.assertIsNone(registry._resolve_screen_alias("left"))
        self.assertFalse(registry.resolve_screen_target("left")["success"])

    def test_screen_service_failure_is_reported(self):
        registry = make_registry(BrokenExecutor())
        self.assertIsNone(registry._resolve_screen_alias("left"))
        result = registry.resolve_screen_target("left")
        self.assertFalse(result["success"])
        self.assertIn("Could not list", result["message"])

    def test_connector_names_pass_through(self):
        registry = self.registry(self.LAYOUTS[3])
        self.assertEqual(registry._resolve_screen_alias("DP-2"), "DP-2")
        self.assertIsNone(registry._resolve_screen_alias("DP-9"))


if __name__ == "__main__":
    unittest.main()
