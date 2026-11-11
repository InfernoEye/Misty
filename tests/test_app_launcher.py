"""Universal app launching and task-scoped approval.

Two problems this covers:

* the launcher could only start eleven hardcoded applications, so everything
  else was unreachable however clearly it was asked for;
* asking before every click made a real desktop task unusable.
"""

import os
import pathlib
import tempfile
import threading
import time
import unittest

from app.brain.task_grant import (
    DEFAULT_GRANT_SECONDS,
    TaskGrant,
    looks_like_desktop_work,
    route_from_model,
)
from app.desktop.app_launcher import (
    InstalledApp,
    _read_entry,
    discover,
    resolve,
)

ENTRY = """[Desktop Entry]
Type=Application
Name={name}
GenericName={generic}
Exec={exec_line}
Keywords={keywords};
Categories={categories};
"""


def write_entry(directory, filename, **fields):
    fields.setdefault("name", filename.replace(".desktop", "").title())
    fields.setdefault("generic", "")
    fields.setdefault("exec_line", "/usr/bin/true %U")
    fields.setdefault("keywords", "")
    fields.setdefault("categories", "Utility")
    body = ENTRY.format(**fields)
    path = pathlib.Path(directory) / filename
    path.write_text(body, encoding="utf-8")
    return path


def app(desktop_id, name, **extra):
    return InstalledApp(desktop_id, name, **extra)


class ParsingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def test_a_normal_entry_parses(self):
        path = write_entry(self.directory, "gimp.desktop", name="GIMP",
                           generic="Image Editor", keywords="photo;edit")
        parsed = _read_entry(path)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.name, "GIMP")
        self.assertEqual(parsed.desktop_id, "gimp.desktop")
        self.assertIn("photo", parsed.keywords)

    def test_hidden_and_nodisplay_entries_are_skipped(self):
        path = pathlib.Path(self.directory) / "hidden.desktop"
        path.write_text(
            "[Desktop Entry]\nType=Application\nName=Hidden\n"
            "Exec=true\nNoDisplay=true\n", encoding="utf-8"
        )
        self.assertIsNone(_read_entry(path))

    def test_links_and_directories_are_skipped(self):
        for kind in ("Link", "Directory"):
            with self.subTest(kind=kind):
                path = pathlib.Path(self.directory) / f"{kind}.desktop"
                path.write_text(
                    f"[Desktop Entry]\nType={kind}\nName=X\nExec=true\n",
                    encoding="utf-8",
                )
                self.assertIsNone(_read_entry(path))

    def test_an_entry_without_exec_is_skipped(self):
        path = pathlib.Path(self.directory) / "noexec.desktop"
        path.write_text(
            "[Desktop Entry]\nType=Application\nName=X\n", encoding="utf-8"
        )
        self.assertIsNone(_read_entry(path))

    def test_a_broken_file_is_skipped_not_raised(self):
        path = pathlib.Path(self.directory) / "broken.desktop"
        path.write_bytes(b"\xff\xfe not a desktop file")
        self.assertIsNone(_read_entry(path))

    def test_the_real_system_has_launchable_apps(self):
        # A normal desktop has some; zero would mean discovery is broken.
        self.assertGreater(len(discover()), 0)


class ResolveTests(unittest.TestCase):
    APPS = [
        app("gimp.desktop", "GNU Image Manipulation Program",
            keywords=("photo", "edit"), categories=("Graphics",)),
        app("blender.desktop", "Blender", categories=("Graphics", "3DGraphics")),
        app("org.kde.gwenview.desktop", "Gwenview", categories=("Graphics",)),
        app("ghostty.desktop", "Ghostty", categories=("System", "TerminalEmulator")),
        app("kitty.desktop", "kitty", categories=("System", "TerminalEmulator")),
    ]

    def test_an_exact_name_wins(self):
        found, candidates = resolve("Blender", self.APPS)
        self.assertIsNotNone(found)
        self.assertEqual(found.desktop_id, "blender.desktop")
        self.assertEqual(candidates, [])

    def test_a_desktop_id_resolves(self):
        found, _ = resolve("gimp.desktop", self.APPS)
        self.assertEqual(found.name, "GNU Image Manipulation Program")

    def test_a_keyword_resolves(self):
        found, _ = resolve("photo", self.APPS)
        self.assertEqual(found.desktop_id, "gimp.desktop")

    def test_an_ambiguous_name_returns_candidates(self):
        found, candidates = resolve("TerminalEmulator", self.APPS)
        self.assertIsNone(found)
        self.assertEqual(
            sorted(item.name for item in candidates), ["Ghostty", "kitty"]
        )

    def test_an_unknown_name_matches_nothing(self):
        found, candidates = resolve("zzz-not-installed", self.APPS)
        self.assertIsNone(found)
        self.assertEqual(candidates, [])

    def test_empty_input_is_not_a_match(self):
        for value in ("", "   ", None, 5):
            with self.subTest(value=value):
                found, candidates = resolve(value, self.APPS)
                self.assertIsNone(found)
                self.assertEqual(candidates, [])


class GrantTests(unittest.TestCase):
    def test_a_grant_starts_active_and_counts_actions(self):
        grant = TaskGrant("open amazon and search")
        self.assertTrue(grant.active())
        self.assertTrue(grant.consume())
        self.assertTrue(grant.consume())
        self.assertEqual(grant.actions, 2)

    def test_closing_a_grant_stops_further_actions(self):
        grant = TaskGrant("task")
        grant.close()
        self.assertFalse(grant.active())
        self.assertFalse(grant.consume())

    def test_a_grant_expires(self):
        now = [1000.0]
        grant = TaskGrant("task", seconds=30, clock=lambda: now[0])
        self.assertTrue(grant.active())
        now[0] += 31
        self.assertFalse(grant.active())
        self.assertFalse(grant.consume())

    def test_a_grant_is_bound_to_its_thread(self):
        grant = TaskGrant("task")
        seen = {}

        def other_thread():
            seen["active"] = grant.active()
            seen["consume"] = grant.consume()

        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join(5)
        self.assertFalse(seen["active"])
        self.assertFalse(seen["consume"])

    def test_the_context_manager_closes_the_grant(self):
        with TaskGrant("task") as grant:
            self.assertTrue(grant.active())
        self.assertFalse(grant.active())

    def test_an_empty_task_is_refused(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    TaskGrant(value)

    def test_the_default_window_is_not_huge(self):
        self.assertLessEqual(DEFAULT_GRANT_SECONDS, 1800.0)


class RoutingTests(unittest.TestCase):
    class Model:
        def __init__(self, payload):
            self.payload = payload

        def _chat(self, messages, tools=None, response_format=None):
            return {"content": self.payload}

    def test_a_desktop_answer_is_returned(self):
        self.assertEqual(
            route_from_model(self.Model('{"route": "desktop"}'), "x"),
            "desktop",
        )

    def test_a_direct_answer_is_returned(self):
        self.assertEqual(
            route_from_model(self.Model('{"route": "direct"}'), "x"), "direct"
        )

    def test_an_unknown_route_is_not_a_decision(self):
        self.assertIsNone(route_from_model(self.Model('{"route": "maybe"}'), "x"))

    def test_broken_json_is_not_a_decision(self):
        self.assertIsNone(route_from_model(self.Model("not json"), "x"))

    def test_a_dead_model_is_not_a_decision(self):
        class Boom:
            def _chat(self, *args, **kwargs):
                raise RuntimeError("model down")

        self.assertIsNone(route_from_model(Boom(), "x"))

    def test_no_agent_is_not_a_decision(self):
        self.assertIsNone(route_from_model(None, "x"))

    def test_a_screen_request_rescues_a_direct_answer(self):
        # The model reading "click on X" as one tool call still needs a
        # screen: the planner cannot see one.
        agent = self.Model('{"route": "direct"}')
        for request in (
            "click on the light pulse on the right screen",
            "in Discord on the right screen click on the Dragon Cave",
            "find me a good vacuum on Amazon and show it",
            "in Helium go on Amazon and find me a cool vacuum",
        ):
            with self.subTest(request=request):
                self.assertTrue(looks_like_desktop_work(agent, request))

    def test_ordinary_requests_stay_direct(self):
        agent = self.Model('{"route": "direct"}')
        for request in ("what time is it", "find my invoice files",
                        "play some music", "how much disk space is left"):
            with self.subTest(request=request):
                self.assertFalse(looks_like_desktop_work(agent, request))

    def test_a_dead_model_still_routes_explicit_visual_work_to_desktop(self):
        class Boom:
            def _chat(self, *args, **kwargs):
                raise RuntimeError("down")

        # Route selection does not authorize input: desktop capability and
        # task approval checks still apply after this fallback.
        self.assertTrue(looks_like_desktop_work(Boom(), "click on the screen"))
        self.assertFalse(looks_like_desktop_work(Boom(), "what time is it"))


if __name__ == "__main__":
    unittest.main()
