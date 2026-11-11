"""QOL reveal tools: reveal_in_file_manager and open_last_result.

Every test runs against a temporary home with an injected launcher, so no
file manager (or any other process) is ever started.  The launcher is
injected through ``app.actions.executor`` so the real argv construction is
exercised too.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.actions.executor import ActionExecutor
from app.actions.file_scope import UserFolderResolver
from app.brain import policy
from app.brain.agent import PLANNING_INSTRUCTIONS
from app.brain.memory import MemoryStore
from app.brain.tools import (
    MAX_REMEMBERED_RESULTS,
    TOOL_DEFINITIONS,
    ToolRegistry,
)


DOLPHIN = "/usr/bin/dolphin"
XDG_OPEN = "/usr/bin/xdg-open"



def _temp_memory_store(folder):
    """A MemoryStore inside the test's temporary folder.

    Tests must never open the real ``data/memory.sqlite3``: the suite runs
    against a read-only project tree, where that store reports
    "attempt to write a readonly database".
    """
    return MemoryStore(Path(folder) / "memory.sqlite3")

class FakeSearchService:
    """Returns absolute paths like ``FileSearchService.search``."""

    def __init__(self, paths):
        self.paths = paths if isinstance(paths, Exception) else list(paths)
        self.calls = []

    def search(self, query, limit=30, root=None):
        self.calls.append((query, limit, root))
        if isinstance(self.paths, Exception):
            raise self.paths
        return {
            "success": True,
            "results": [
                {"name": Path(path).name, "path": str(path),
                 "type": "file", "match_score": 1.0}
                for path in self.paths[:limit]
            ],
            "message": "Found results.",
        }


class RevealTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.downloads = self.home / "Pobrane"
        self.downloads.mkdir(parents=True)
        (self.home / "Documents").mkdir()
        (self.home / "Documents" / "private.txt").write_text("secret")
        self.report = self.downloads / "report.txt"
        self.report.write_text("report")
        self.notes = self.downloads / "notes.md"
        self.notes.write_text("notes")

        self.resolver = UserFolderResolver(
            home=self.home, xdg_lookup=lambda _name: None
        )
        self.executor = ActionExecutor(folder_resolver=self.resolver)
        self.argv = []
        patcher = patch.object(
            ActionExecutor,
            "_argv_launcher",
            staticmethod(lambda launcher: self.argv.append),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.search = FakeSearchService([self.report, self.notes])
        self.registry = ToolRegistry(
            executor=self.executor,
            folder_resolver=self.resolver,
            file_search_service=self.search,
            # Never let a test open the real data/memory.sqlite3: the suite
            # runs against a read-only project tree.
            memory_store=MemoryStore(self.home / "memory.sqlite3"),
        )

    # -- helpers ---------------------------------------------------------

    def with_dolphin(self):
        return patch(
            "app.actions.executor.shutil.which",
            side_effect=lambda name: DOLPHIN if name == "dolphin" else None,
        )

    def with_xdg_open_only(self):
        return patch(
            "app.actions.executor.shutil.which",
            side_effect=lambda name: XDG_OPEN if name == "xdg-open" else None,
        )

    def with_no_launcher(self):
        return patch("app.actions.executor.shutil.which", return_value=None)

    def assert_no_process_started(self):
        """The injected launcher replaced ``subprocess.Popen`` entirely."""
        self.assertEqual(self.argv, [])

    def assert_message_is_private(self, result):
        message = result["message"]
        self.assertNotIn(str(self.home), message)
        self.assertNotIn(self.temp.name, message)
        self.assertNotIn(str(Path.home()), message)

    def search_downloads(self):
        return self.registry.execute(
            "search_files", {"query": "report", "folder": "Downloads"}
        )


class SchemaTests(RevealTestBase):
    def advertised(self):
        return {
            item["function"]["name"]: item["function"]
            for item in self.registry.available_tools()
        }

    def test_both_tools_are_advertised_once(self):
        names = [
            item["function"]["name"]
            for item in self.registry.available_tools()
        ]
        self.assertEqual(names.count("reveal_in_file_manager"), 1)
        self.assertEqual(names.count("open_last_result"), 1)

    def test_reveal_schema(self):
        parameters = self.advertised()["reveal_in_file_manager"]["parameters"]
        self.assertEqual(parameters["required"], ["folder", "relative_path"])
        self.assertIs(parameters["additionalProperties"], False)
        properties = parameters["properties"]
        from app.brain.tools import FOLDERS

        self.assertEqual(
            set(properties["folder"]["enum"]), set(FOLDERS)
        )
        path = properties["relative_path"]
        self.assertEqual(path["minLength"], 1)
        self.assertEqual(path["maxLength"], 1024)
        self.assertEqual(properties["mode"]["enum"], ["select", "open_folder"])
        self.assertEqual(properties["mode"]["default"], "select")

    def test_open_last_result_schema(self):
        function = self.advertised()["open_last_result"]
        parameters = function["parameters"]
        index = parameters["properties"]["index"]
        self.assertEqual(index["type"], "integer")
        self.assertEqual(index["minimum"], 1)
        self.assertEqual(index["maximum"], 20)
        self.assertEqual(index["default"], 1)
        self.assertIs(parameters["additionalProperties"], False)
        # The description documents the intended search-then-open ordering.
        self.assertIn("open_last_result", function["description"])
        self.assertIn("depends_on", function["description"])
        self.assertIn("index", function["description"])

    def test_schemas_are_json_serialisable(self):
        for definition in TOOL_DEFINITIONS:
            json.dumps(definition)


class PolicyTests(RevealTestBase):
    def test_effect_is_launch_not_read_only(self):
        # A file manager is a GUI launch: never ``read_only`` (which would
        # make the tool retryable) and never ``local_write`` (nothing in the
        # user's data changes).
        for name in ("reveal_in_file_manager", "open_last_result"):
            with self.subTest(tool=name):
                spec = policy.policy_for(name)
                self.assertIsNotNone(spec)
                self.assertEqual(spec.effect, "launch")
                self.assertIn(spec.effect, policy.EFFECT_NAMES)
                self.assertEqual(spec.risk, "low")
                self.assertTrue(spec.private)
                self.assertFalse(spec.requires_confirmation)

    def test_matches_open_folder_pattern(self):
        open_folder = policy.policy_for("open_folder")
        reveal = policy.policy_for("reveal_in_file_manager")
        self.assertEqual(reveal.effect, open_folder.effect)
        self.assertEqual(reveal.risk, open_folder.risk)
        self.assertEqual(
            reveal.requires_confirmation, open_folder.requires_confirmation
        )

    def test_never_egress_and_never_retried(self):
        for name in ("reveal_in_file_manager", "open_last_result"):
            with self.subTest(tool=name):
                self.assertFalse(policy.is_egress_tool(name))
                self.assertFalse(policy.is_retryable(name))
                self.assertFalse(
                    policy.is_retryable(name, {"idempotent": True})
                )

    def test_no_confirmation_is_required(self):
        for name in ("reveal_in_file_manager", "open_last_result"):
            with self.subTest(tool=name):
                needed, _reason = policy.requires_confirmation_for(
                    name, {"index": 2}
                )
                self.assertFalse(needed)
                decision = policy.evaluate(name, {"index": 2})
                self.assertTrue(decision["allowed"])
                self.assertFalse(decision["requires_confirmation"])


class RevealInFileManagerTests(RevealTestBase):
    def test_select_argv_is_dolphin_select(self):
        with self.with_dolphin():
            result = self.registry.execute("reveal_in_file_manager", {
                "folder": "Downloads",
                "relative_path": "notes.md",
            })

        self.assertTrue(result["success"], result)
        self.assertEqual(
            self.argv, [[DOLPHIN, "--select", str(self.notes.resolve())]]
        )
        self.assertEqual(result["path"], "notes.md")
        self.assert_message_is_private(result)

    def test_open_folder_opens_parent(self):
        with self.with_dolphin():
            result = self.registry.execute("reveal_in_file_manager", {
                "folder": "Downloads",
                "relative_path": "notes.md",
                "mode": "open_folder",
            })

        self.assertTrue(result["success"], result)
        self.assertEqual(self.argv, [[DOLPHIN, str(self.downloads.resolve())]])

    def test_xdg_open_fallback_when_dolphin_missing(self):
        with self.with_xdg_open_only():
            result = self.registry.execute("reveal_in_file_manager", {
                "folder": "Downloads",
                "relative_path": "notes.md",
            })

        self.assertTrue(result["success"], result)
        self.assertEqual(self.argv, [[XDG_OPEN, str(self.downloads.resolve())]])

    def test_no_launcher_fails_closed_without_starting_anything(self):
        with self.with_no_launcher():
            result = self.registry.execute("reveal_in_file_manager", {
                "folder": "Downloads",
                "relative_path": "notes.md",
            })

        self.assertFalse(result["success"])
        self.assert_no_process_started()
        self.assert_message_is_private(result)

    def test_launch_error_is_reported(self):
        def explode(argv):
            raise OSError("boom")

        with self.with_dolphin(), patch.object(
            ActionExecutor,
            "_argv_launcher",
            staticmethod(lambda launcher: explode),
        ):
            result = self.registry.execute("reveal_in_file_manager", {
                "folder": "Downloads",
                "relative_path": "notes.md",
            })

        self.assertFalse(result["success"])
        self.assert_message_is_private(result)

    def test_traversal_is_refused(self):
        with self.with_dolphin():
            result = self.registry.execute("reveal_in_file_manager", {
                "folder": "Downloads",
                "relative_path": "../Documents/private.txt",
            })

        self.assertFalse(result["success"])
        self.assert_no_process_started()
        self.assertNotIn("private.txt", json.dumps(self.argv))

    def test_absolute_path_is_refused(self):
        with self.with_dolphin():
            result = self.registry.execute("reveal_in_file_manager", {
                "folder": "Downloads",
                "relative_path": str(self.report),
            })

        self.assertFalse(result["success"])
        self.assert_no_process_started()

    def test_symlink_escape_is_refused(self):
        (self.downloads / "link").symlink_to(
            self.home / "Documents" / "private.txt"
        )
        with self.with_dolphin():
            result = self.registry.execute("reveal_in_file_manager", {
                "folder": "Downloads",
                "relative_path": "link",
            })

        self.assertFalse(result["success"])
        self.assert_no_process_started()

    def test_folder_argument_must_be_an_approved_key(self):
        for folder in ("/tmp", "Downloads/..", "Unknown", None):
            with self.subTest(folder=folder), self.with_dolphin():
                result = self.registry.execute("reveal_in_file_manager", {
                    "folder": folder,
                    "relative_path": "notes.md",
                })
                self.assertFalse(result["success"])
        self.assert_no_process_started()

    def test_argument_contract(self):
        bad_arguments = [
            {},
            {"folder": "Downloads"},
            {"relative_path": "notes.md"},
            {"folder": "Downloads", "relative_path": ""},
            {"folder": "Downloads", "relative_path": "x" * 1025},
            {"folder": "Downloads", "relative_path": 5},
            {"folder": "Downloads", "relative_path": "notes.md",
             "mode": "rename"},
            {"folder": "Downloads", "relative_path": "notes.md",
             "absolute": "/etc/passwd"},
        ]
        with self.with_dolphin():
            for arguments in bad_arguments:
                with self.subTest(arguments=arguments):
                    result = self.registry.execute(
                        "reveal_in_file_manager", arguments
                    )
                    self.assertFalse(result["success"])
        self.assert_no_process_started()

    def test_missing_file_is_refused(self):
        with self.with_dolphin():
            result = self.registry.execute("reveal_in_file_manager", {
                "folder": "Downloads",
                "relative_path": "missing.txt",
            })

        self.assertFalse(result["success"])
        self.assert_no_process_started()

    def test_plain_mock_executor_fails_closed(self):
        from unittest.mock import Mock

        registry = ToolRegistry(
            executor=Mock(),
            folder_resolver=self.resolver,
            memory_store=_temp_memory_store(self.home),
        )
        result = registry.execute("reveal_in_file_manager", {
            "folder": "Downloads",
            "relative_path": "notes.md",
        })
        self.assertFalse(result["success"])


class OpenLastResultTests(RevealTestBase):
    def test_no_remembered_result(self):
        result = self.registry.execute("open_last_result", {})
        self.assertFalse(result["success"])
        self.assertIn("no file result", result["message"].lower())
        self.assert_no_process_started()

    def test_index_selects_nth_search_result(self):
        self.assertTrue(self.search_downloads()["success"])
        self.assertEqual(
            self.registry._last_paths(),
            [("Downloads", "report.txt"), ("Downloads", "notes.md")],
        )

        with self.with_dolphin():
            second = self.registry.execute("open_last_result", {"index": 2})

        self.assertTrue(second["success"], second)
        self.assertEqual(self.argv[-1], [DOLPHIN, "--select", str(self.notes.resolve())])
        self.assertEqual(second["path"], "notes.md")
        self.assert_message_is_private(second)

        with self.with_dolphin():
            first = self.registry.execute("open_last_result", {})
        self.assertEqual(first["path"], "report.txt")
        self.assertEqual(
            self.argv[-1], [DOLPHIN, "--select", str(self.report.resolve())]
        )

    def test_default_index_is_one(self):
        self.search_downloads()
        with self.with_dolphin():
            result = self.registry.execute("open_last_result", {})
        self.assertTrue(result["success"], result)
        self.assertEqual(result["path"], "report.txt")

    def test_index_out_of_range(self):
        self.search_downloads()
        with self.with_dolphin():
            for index in (3, 20, 21, 0, -1, True, "2", 1.5):
                with self.subTest(index=index):
                    result = self.registry.execute(
                        "open_last_result", {"index": index}
                    )
                    self.assertFalse(result["success"])
        self.assert_no_process_started()

    def test_unknown_arguments_are_refused(self):
        self.search_downloads()
        with self.with_dolphin():
            result = self.registry.execute("open_last_result", {
                "index": 1, "relative_path": "notes.md",
            })
        self.assertFalse(result["success"])
        self.assert_no_process_started()

    def test_never_accepts_a_path_from_the_model(self):
        with self.with_dolphin():
            result = self.registry.execute("open_last_result", {
                "index": 1, "path": str(self.home / "Documents" / "private.txt"),
            })
        self.assertFalse(result["success"])
        self.assert_no_process_started()

    def test_paths_outside_approved_folders_are_dropped(self):
        outside = Path(self.temp.name) / "elsewhere" / "secret.txt"
        outside.parent.mkdir()
        outside.write_text("x")
        # A scoped search that reports paths outside its own folder still
        # maps them to their own approved key (Documents), never Home, and
        # drops anything outside every approved folder (/etc, elsewhere).
        self.search.paths = [
            self.report,
            self.home / "Documents" / "private.txt",
            Path("/etc/passwd"),
            outside,
        ]
        self.search_downloads()
        self.assertEqual(
            self.registry._last_paths(),
            [("Downloads", "report.txt"), ("Documents", "private.txt")],
        )
        for _folder, relative in self.registry._last_paths():
            self.assertFalse(relative.startswith("Pobrane/"))
            self.assertNotIn("..", Path(relative).parts)

    def test_list_files_results_are_remembered(self):
        result = self.registry.execute(
            "list_files", {"folder": "Downloads"}
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(
            self.registry._last_paths(),
            [("Downloads", "notes.md"), ("Downloads", "report.txt")],
        )
        with self.with_dolphin():
            opened = self.registry.execute("open_last_result", {"index": 1})
        self.assertTrue(opened["success"], opened)
        self.assertEqual(opened["path"], "notes.md")

    def test_list_files_subfolder_keeps_relative_base(self):
        (self.downloads / "2026").mkdir()
        (self.downloads / "2026" / "plan.txt").write_text("p")
        self.registry.execute(
            "list_files", {"folder": "Downloads", "relative_path": "2026"}
        )
        self.assertEqual(self.registry._last_paths(), [("Downloads", "2026/plan.txt")])
        with self.with_dolphin():
            opened = self.registry.execute("open_last_result", {"index": 1})
        self.assertTrue(opened["success"], opened)
        self.assertEqual(
            self.argv[-1],
            [DOLPHIN, "--select", str((self.downloads / "2026" / "plan.txt").resolve())],
        )

    def test_find_duplicates_results_are_remembered(self):
        class FakeFinder:
            def find(self, folder, relative_path="", min_bytes=1,
                     max_groups=100):
                return {
                    "success": True,
                    "groups": [{"size": 3, "sha256": "x",
                                "paths": ["report.txt", "notes.md"]}],
                    "message": "Found 1 duplicate group(s).",
                }

        registry = ToolRegistry(
            executor=self.executor,
            folder_resolver=self.resolver,
            duplicate_finder=FakeFinder(),
            memory_store=_temp_memory_store(self.home),
        )
        self.assertTrue(
            registry.execute("find_duplicates", {"folder": "Downloads"})["success"]
        )
        self.assertEqual(
            registry._last_paths(),
            [("Downloads", "report.txt"), ("Downloads", "notes.md")],
        )
        with self.with_dolphin():
            result = registry.execute("open_last_result", {"index": 2})
        self.assertTrue(result["success"], result)
        self.assertEqual(self.argv[-1], [DOLPHIN, "--select", str(self.notes.resolve())])

    def test_failed_search_does_not_replace_memory(self):
        self.search_downloads()
        failed = FakeSearchService(RuntimeError("the search service died"))
        self.registry.file_search_service = failed
        # The native search error propagates; nothing was remembered anew.
        with self.assertRaises(RuntimeError):
            self.registry.execute("search_files", {"query": "x"})
        self.assertEqual(len(self.registry._last_paths()), 2)

    def test_other_tools_are_not_remembered(self):
        self.search_downloads()
        self.registry.execute(
            "get_file_info",
            {"folder": "Documents", "relative_path": "private.txt"},
        )
        self.assertEqual(
            self.registry._last_paths(),
            [("Downloads", "report.txt"), ("Downloads", "notes.md")],
        )

    def test_memory_is_bounded(self):
        many = []
        for number in range(MAX_REMEMBERED_RESULTS + 5):
            path = self.downloads / f"file{number:03d}.txt"
            path.write_text("x")
            many.append(path)
        self.search.paths = many
        self.registry.execute("search_files", {"query": "file"})
        self.assertEqual(len(self.search.calls), 1)

        remembered = self.registry._last_paths()
        self.assertEqual(len(remembered), MAX_REMEMBERED_RESULTS)
        self.assertEqual(remembered[0], ("Downloads", "file000.txt"))
        self.assertEqual(
            remembered[-1], ("Downloads", f"file{MAX_REMEMBERED_RESULTS - 1:03d}.txt")
        )
        self.assertEqual(len(remembered), len(set(remembered)))

    def test_memory_holds_relative_paths_only(self):
        self.search_downloads()
        for folder, relative in self.registry._last_paths():
            self.assertIn(folder, ("Home", "Downloads", "Documents", "Desktop",
                                   "Pictures", "Music", "Videos"))
            self.assertFalse(Path(relative).is_absolute())
            self.assertNotIn("..", Path(relative).parts)
            self.assertNotIn(str(self.home), relative)

    def test_memory_resets(self):
        self.search_downloads()
        self.assertTrue(self.registry._last_paths())
        self.registry.reset_result_memory()
        self.assertEqual(self.registry._last_paths(), [])
        with self.with_dolphin():
            result = self.registry.execute("open_last_result", {"index": 1})
        self.assertFalse(result["success"])
        self.assert_no_process_started()

    def test_a_new_request_does_not_see_the_previous_request(self):
        self.search_downloads()
        # A fresh agent request builds a fresh registry over the same
        # services; nothing carries over.
        fresh = ToolRegistry(
            executor=self.executor,
            folder_resolver=self.resolver,
            file_search_service=self.search,
            memory_store=_temp_memory_store(self.home),
        )
        with self.with_dolphin():
            result = fresh.execute("open_last_result", {"index": 1})
        self.assertFalse(result["success"])
        self.assert_no_process_started()

    def test_a_later_search_in_the_same_request_does_not_override(self):
        # One step = one execute(); the first successful file result of that
        # step is the one "open the second one" refers to.
        self.search_downloads()
        self.search.paths = [self.notes]
        result = self.registry.execute("search_files", {"query": "notes"})
        self.assertEqual(self.search.calls[-1][0], "notes")
        self.assertEqual(
            self.registry._last_paths(),
            [("Downloads", "report.txt"), ("Downloads", "notes.md")],
        )
        self.assertTrue(isinstance(result, dict))


class PromptTests(unittest.TestCase):
    def test_planner_is_told_about_open_the_second_one(self):
        self.assertIn("open_last_result", PLANNING_INSTRUCTIONS)
        self.assertIn("open", PLANNING_INSTRUCTIONS.casefold())
        self.assertIn("index 2", PLANNING_INSTRUCTIONS)
        self.assertIn("depends_on", PLANNING_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
