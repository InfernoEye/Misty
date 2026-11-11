import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.actions.file_scope import UserFolderResolver
from app.actions.text_search import (
    EXCLUDED_NAMES,
    MAX_LINE_CHARS,
    TextSearchService,
    _RunResult,
)


RG = shutil.which("rg")


def match_line(path, line_number, text):
    return json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": str(path)},
                "lines": {"text": text},
                "line_number": line_number,
                "absolute_offset": 0,
                "submatches": [],
            },
        }
    ).encode()


class FakeTextSearchService(TextSearchService):
    """Inject a deterministic event stream instead of a real process."""

    def __init__(self, lines=(), outcome=None, **kwargs):
        kwargs.setdefault("rg_path", sys.executable)
        super().__init__(**kwargs)
        self.lines = list(lines)
        self.outcome = outcome or _RunResult(returncode=0)
        self.argv = None
        self.run_count = 0

    def _run(self, argv, consume):
        self.argv = list(argv)
        self.run_count += 1
        for line in self.lines:
            if consume(line) is False:
                return _RunResult(stopped=True)
        return self.outcome


class TextSearchTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.docs = self.home / "Documents"
        self.downloads = self.home / "Pobrane"
        self.docs.mkdir()
        self.downloads.mkdir()
        self.resolver = UserFolderResolver(
            home=self.home, xdg_lookup=lambda _name: None
        )

    def service(self, **kwargs):
        return TextSearchService(resolver=self.resolver, **kwargs)

    def fake(self, lines=(), outcome=None, **kwargs):
        return FakeTextSearchService(
            lines=lines,
            outcome=outcome,
            resolver=self.resolver,
            **kwargs,
        )

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path


class ValidationTests(TextSearchTestBase):
    def test_rejects_invalid_queries(self):
        for query in (
            "",
            "   ",
            "x" * 201,
            "needle\x00tail",
            "needle\nsecond",
            "needle\tcluded",
            "needle\x1f",
            "needle\x7f",
            "\ud800",
            None,
            123,
        ):
            with self.subTest(query=query):
                service = self.fake()
                result = service.search("Documents", query)
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "invalid_query")
                self.assertEqual(
                    result["message"],
                    TextSearchService.ERROR_MESSAGES["invalid_query"],
                )
                self.assertEqual(service.run_count, 0)

    def test_accepts_bounded_query(self):
        service = self.fake()
        result = service.search("Documents", "q" * 200)
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 0)

    def test_strips_surrounding_whitespace(self):
        service = self.fake()
        result = service.search("Documents", "  needle  ")
        self.assertTrue(result["success"])
        self.assertEqual(result["query"], "needle")

    def test_rejects_invalid_limits(self):
        service = self.fake()
        for kwargs in (
            {"limit": 0},
            {"limit": 201},
            {"limit": True},
            {"limit": 1.5},
            {"limit": "5"},
            {"max_matches_per_file": 0},
            {"max_matches_per_file": 51},
            {"max_matches_per_file": True},
        ):
            with self.subTest(kwargs=kwargs):
                result = service.search("Documents", "needle", **kwargs)
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "invalid_limit")
                self.assertEqual(
                    result["message"],
                    TextSearchService.ERROR_MESSAGES["invalid_limit"],
                )
        self.assertEqual(service.run_count, 0)

    def test_rejects_unapproved_or_unsafe_folders(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_path = Path(outside.name)
        link = self.home / "linked"
        link.symlink_to(outside_path, target_is_directory=True)
        inside_link = self.docs / "linked-inside"
        inside_link.symlink_to(outside_path, target_is_directory=True)
        a_file = self.write(self.docs / "plain.txt", "needle\n")

        service = self.fake()
        for folder in (
            "Unknown",
            "Music",  # valid key, but the configured path is missing
            "../Documents",
            "Documents/../Escaped",
            "Documents/missing",
            "/etc",
            str(outside_path),
            str(link),
            str(inside_link),
            str(a_file),
            "Documents/linked-inside",
            123,
            None,
        ):
            with self.subTest(folder=folder):
                result = service.search(folder, "needle")
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "invalid_folder")
                self.assertEqual(
                    result["message"],
                    TextSearchService.ERROR_MESSAGES["invalid_folder"],
                )

    def test_missing_ripgrep_is_reported_without_paths(self):
        service = self.service(rg_path=str(self.home / "missing-rg"))
        result = service.search("Documents", "needle")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "rg_missing")
        self.assertEqual(
            result["message"],
            TextSearchService.ERROR_MESSAGES["rg_missing"],
        )
        self.assertNotIn(str(self.home), json.dumps(result))

    def test_rg_lookup_failure_is_rg_missing(self):
        with patch(
            "app.actions.text_search.shutil.which", return_value=None
        ):
            service = TextSearchService(resolver=self.resolver)
            result = service.search("Documents", "needle")
        self.assertEqual(result["error"], "rg_missing")

    def test_rejects_symlink_component_inside_absolute_root(self):
        real = self.docs / "real"
        real.mkdir()
        self.write(real / "sub" / "x.txt", "needle\n")
        alias = self.docs / "alias"
        alias.symlink_to(real, target_is_directory=True)

        result = self.fake().search(str(alias / "sub"), "needle")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "invalid_folder")

    def test_accepts_key_relative_and_absolute_folder_forms(self):
        sub = self.docs / "Reports"
        sub.mkdir()
        for folder in ("Documents", "Documents/Reports", str(self.docs), sub):
            with self.subTest(folder=folder):
                service = self.fake()
                result = service.search(folder, "needle")
                self.assertTrue(result["success"], result)


class CommandConstructionTests(TextSearchTestBase):
    def test_argv_is_a_bounded_list_with_exclusions(self):
        service = self.fake()
        service.search(
            "Documents",
            "-needle",
            limit=7,
            max_matches_per_file=3,
            case_sensitive=True,
            fixed_strings=True,
        )
        argv = service.argv
        self.assertIsInstance(argv, list)
        self.assertTrue(all(isinstance(item, str) for item in argv))
        self.assertEqual(argv[0], sys.executable)
        self.assertIn("--json", argv)
        self.assertIn("--no-messages", argv)
        self.assertIn("--no-config", argv)
        self.assertIn("--no-follow", argv)
        self.assertIn("--hidden", argv)
        self.assertIn("--no-ignore", argv)
        self.assertIn("--fixed-strings", argv)
        self.assertIn("--case-sensitive", argv)
        self.assertNotIn("--ignore-case", argv)
        self.assertEqual(
            argv[argv.index("--max-count") + 1],
            "3",
        )
        self.assertEqual(argv[argv.index("--max-filesize") + 1], "2M")

        globs = [
            argv[index + 1]
            for index, item in enumerate(argv)
            if item == "--glob"
        ]
        for name in (".git", "node_modules", ".cache", "__pycache__",
                     "build", "dist", "target"):
            self.assertIn(f"!{name}", globs)
        self.assertIn("!.local/share/Trash", globs)
        self.assertTrue(EXCLUDED_NAMES.issubset(
            {glob[1:] for glob in globs if not glob.endswith("/**")}
        ))

        separator = argv.index("--")
        self.assertEqual(argv[separator + 1], "-needle")
        self.assertEqual(argv[separator + 2], str(self.docs.resolve()))

    def test_case_insensitive_by_default(self):
        service = self.fake()
        service.search("Documents", "needle")
        self.assertIn("--ignore-case", service.argv)
        self.assertNotIn("--case-sensitive", service.argv)
        self.assertNotIn("--fixed-strings", service.argv)

    def test_popen_receives_no_shell_and_devnull_stdin(self):
        service = self.service(rg_path=sys.executable)
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b'{"type":"summary","data":{}}\n')
        finally:
            os.close(write_fd)

        process = Mock()
        process.stdout = os.fdopen(read_fd, "rb", buffering=0)
        process.wait.return_value = 0
        process.poll.return_value = 0

        with patch(
            "app.actions.text_search.subprocess.Popen",
            return_value=process,
        ) as popen:
            outcome = service._run([sys.executable, "-c", "pass"], lambda _: True)

        self.assertEqual(outcome.returncode, 0)
        self.assertFalse(outcome.timed_out)
        argv = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertIsInstance(argv, list)
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)

    def test_timeout_kills_the_process(self):
        service = self.service(rg_path=sys.executable)
        service.TIMEOUT_SECONDS = 0.05
        read_fd, write_fd = os.pipe()
        process = Mock()
        process.stdout = os.fdopen(read_fd, "rb", buffering=0)
        process.wait.return_value = -9
        process.poll.return_value = -9
        try:
            with patch(
                "app.actions.text_search.subprocess.Popen",
                return_value=process,
            ):
                outcome = service._run(
                    [sys.executable, "-c", "pass"], lambda _: True
                )
        finally:
            os.close(write_fd)

        self.assertTrue(outcome.timed_out)
        process.kill.assert_called_once_with()

    def test_output_cap_kills_the_process(self):
        service = self.service(rg_path=sys.executable)
        service.MAX_OUTPUT_BYTES = 32
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"x" * 200)
        finally:
            os.close(write_fd)
        process = Mock()
        process.stdout = os.fdopen(read_fd, "rb", buffering=0)
        process.wait.return_value = -9
        process.poll.return_value = -9

        with patch(
            "app.actions.text_search.subprocess.Popen",
            return_value=process,
        ):
            outcome = service._run([sys.executable, "-c", "pass"], lambda _: True)

        self.assertTrue(outcome.too_large)
        process.kill.assert_called_once_with()

    def test_timeout_and_output_cap_failures_are_fixed(self):
        for outcome, code in (
            (_RunResult(timed_out=True), "timeout"),
            (_RunResult(too_large=True), "output_too_large"),
            (_RunResult(returncode=2), "rg_error"),
            (_RunResult(launch_error=True), "rg_missing"),
            (_RunResult(failed=True), "rg_error"),
        ):
            with self.subTest(code=code):
                service = self.fake(outcome=outcome)
                result = service.search("Documents", "needle")
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], code)
                self.assertEqual(
                    result["message"],
                    TextSearchService.ERROR_MESSAGES[code],
                )
                self.assertEqual(result["results"], [])
                self.assertFalse(result["truncated"])

    def test_runner_exception_is_hidden_behind_rg_error(self):
        service = self.fake()
        service._run = Mock(side_effect=OSError("raw detail /secret/path"))
        result = service.search("Documents", "needle")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "rg_error")
        serialized = json.dumps(result)
        self.assertNotIn("raw detail", serialized)
        self.assertNotIn("/secret/path", serialized)

    def test_no_matches_exit_status_is_success(self):
        service = self.fake(outcome=_RunResult(returncode=1))
        result = service.search("Documents", "needle")
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 0)
        self.assertFalse(result["truncated"])


class ParsingTests(TextSearchTestBase):
    def test_parses_match_events_and_ignores_begin_end_summary(self):
        root = self.docs
        lines = [
            json.dumps(
                {"type": "begin", "data": {"path": {"text": str(root / "a.txt")}}}
            ).encode(),
            match_line(root / "a.txt", 3, "first needle\n"),
            match_line(root / "sub" / "b.txt", 9, "second needle\r\n"),
            json.dumps(
                {
                    "type": "end",
                    "data": {
                        "path": {"text": str(root / "a.txt")},
                        "binary_offset": None,
                    },
                }
            ).encode(),
            json.dumps({"type": "summary", "data": {}}).encode(),
        ]
        result = self.fake(lines).search("Documents", "needle")

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["file_count"], 2)
        self.assertEqual(
            result["results"],
            [
                {"path": "a.txt", "line_number": 3, "text": "first needle"},
                {
                    "path": "sub/b.txt",
                    "line_number": 9,
                    "text": "second needle",
                },
            ],
        )
        self.assertFalse(result["truncated"])

    def test_malformed_json_is_ignored(self):
        root = self.docs
        lines = [
            b"not json at all",
            b'{"type":"match","data":',
            b'"just a string"',
            b'{"type":"match","data":{}}',
            match_line(root / "ok.txt", 1, "needle\n"),
        ]
        result = self.fake(lines).search("Documents", "needle")

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["path"], "ok.txt")

    def test_all_malformed_output_is_empty_success(self):
        result = self.fake([b"{", b"oops"]).search("Documents", "needle")
        self.assertTrue(result["success"])
        self.assertEqual(result["results"], [])
        self.assertEqual(result["count"], 0)

    def test_binary_line_is_never_returned(self):
        root = self.docs
        lines = [
            match_line(root / "bin.dat", 1, "needle before\x00needle after\n"),
            match_line(root / "text.txt", 2, "needle text\n"),
        ]
        result = self.fake(lines).search("Documents", "needle")

        self.assertEqual(
            [item["path"] for item in result["results"]], ["text.txt"]
        )
        self.assertNotIn("\x00", json.dumps(result))

    def test_end_binary_offset_removes_prior_matches(self):
        root = self.docs
        lines = [
            match_line(root / "bin.dat", 1, "needle\n"),
            json.dumps(
                {
                    "type": "end",
                    "data": {
                        "path": {"text": str(root / "bin.dat")},
                        "binary_offset": 0,
                    },
                }
            ).encode(),
        ]
        result = self.fake(lines).search("Documents", "needle")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["count"], 0)

    def test_per_file_cap_applies_even_to_untrusted_events(self):
        root = self.docs
        lines = [
            match_line(root / "a.txt", number, f"needle {number}\n")
            for number in range(1, 6)
        ]
        result = self.fake(lines).search(
            "Documents", "needle", max_matches_per_file=2
        )
        self.assertEqual(result["count"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "per-file limit")

    def test_global_limit_stops_parsing(self):
        root = self.docs
        lines = [
            match_line(root / f"{index}.txt", 1, "needle\n")
            for index in range(5)
        ]
        result = self.fake(lines).search("Documents", "needle", limit=2)
        self.assertEqual(result["count"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "result limit")

    def test_line_text_is_bounded(self):
        root = self.docs
        long_text = "needle" + "x" * (MAX_LINE_CHARS + 200) + "\n"
        result = self.fake(
            [match_line(root / "long.txt", 1, long_text)]
        ).search("Documents", "needle")

        self.assertEqual(len(result["results"][0]["text"]), MAX_LINE_CHARS)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "line length")

    def test_missing_or_bad_fields_are_skipped(self):
        root = self.docs
        lines = [
            b'{"type":"match","data":{"path":{"text":"x"},"lines":{"text":"x"}}}',
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": str(root / "x.txt")},
                        "lines": {"text": "needle\n"},
                        "line_number": True,
                    },
                }
            ).encode(),
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": str(root / "y.txt")},
                        "lines": {"text": "needle\n"},
                        "line_number": 0,
                    },
                }
            ).encode(),
        ]
        result = self.fake(lines).search("Documents", "needle")
        self.assertEqual(result["results"], [])

    def test_byte_encoded_paths_and_lines_are_safe(self):
        import base64

        root = self.docs
        payload = {
            "type": "match",
            "data": {
                "path": {
                    "bytes": base64.b64encode(
                        str(root / "utf8.txt").encode()
                    ).decode()
                },
                "lines": {"text": "needle\n"},
                "line_number": 1,
            },
        }
        result = self.fake(
            [json.dumps(payload).encode()]
        ).search("Documents", "needle")
        self.assertEqual(result["results"][0]["path"], "utf8.txt")


class ScopeEscapeTests(TextSearchTestBase):
    def test_outside_absolute_and_parent_paths_are_dropped(self):
        root = self.docs
        lines = [
            match_line("/etc/passwd", 1, "root:x:0:0:needle\n"),
            match_line(root / ".." / ".." / "secret.txt", 1, "needle secret\n"),
            match_line(root / "sub" / ".." / ".." / "secret.txt", 1, "needle\n"),
        ]
        result = self.fake(lines).search("Documents", "needle")

        self.assertEqual(result["results"], [])
        serialized = json.dumps(result)
        self.assertNotIn("root:x", serialized)
        self.assertNotIn("secret", serialized)

    def test_absolute_outside_event_does_not_leak_content(self):
        outside = self.write(self.home / "outside-secret.txt", "TOP_SECRET needle\n")
        result = self.fake(
            [match_line(outside, 1, "TOP_SECRET needle\n")]
        ).search("Documents", "needle")
        self.assertEqual(result["results"], [])
        self.assertNotIn("TOP_SECRET", json.dumps(result))

    def test_symlinked_match_event_is_dropped(self):
        outside = self.write(
            self.home / "outside-secret.txt", "TOP_SECRET needle\n"
        )
        link = self.docs / "leak.txt"
        link.symlink_to(outside)
        result = self.fake(
            [match_line(link, 1, "TOP_SECRET needle\n")]
        ).search("Documents", "needle")
        self.assertEqual(result["results"], [])
        self.assertNotIn("TOP_SECRET", json.dumps(result))

    def test_results_never_contain_absolute_paths(self):
        root = self.docs
        result = self.fake(
            [match_line(root / "nested" / "a.txt", 4, "needle\n")]
        ).search("Documents", "needle")
        self.assertEqual(result["results"][0]["path"], "nested/a.txt")
        serialized = json.dumps(result)
        self.assertNotIn(str(self.home), serialized)
        self.assertFalse(result["results"][0]["path"].startswith("/"))


@unittest.skipUnless(RG, "ripgrep is not installed")
class RealRipgrepTests(TextSearchTestBase):
    def test_finds_matches_with_relative_path_line_and_text(self):
        target = self.write(
            self.docs / "sub" / "notes.txt",
            "first line\nAlpha needle here\nlast\n",
        )
        result = self.service().search("Documents", "needle")

        self.assertTrue(result["success"], result)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(
            result["results"],
            [
                {
                    "path": "sub/notes.txt",
                    "line_number": 2,
                    "text": "Alpha needle here",
                }
            ],
        )
        self.assertFalse(result["truncated"])
        self.assertNotIn(str(self.home), json.dumps(result))
        self.assertNotIn(str(target), json.dumps(result))

    def test_case_sensitivity(self):
        self.write(self.docs / "case.txt", "Alpha\n")
        insensitive = self.service().search("Documents", "alpha")
        sensitive = self.service().search(
            "Documents", "alpha", case_sensitive=True
        )
        self.assertEqual(insensitive["count"], 1)
        self.assertEqual(sensitive["count"], 0)

    def test_fixed_strings(self):
        self.write(self.docs / "regex.txt", "axb\na.b\n")
        regex = self.service().search("Documents", "a.b")
        literal = self.service().search(
            "Documents", "a.b", fixed_strings=True
        )
        self.assertEqual(regex["count"], 2)
        self.assertEqual(literal["count"], 1)
        self.assertEqual(literal["results"][0]["text"], "a.b")

    def test_per_file_limit(self):
        self.write(
            self.docs / "many.txt",
            "needle 1\nneedle 2\nneedle 3\nneedle 4\n",
        )
        result = self.service().search(
            "Documents", "needle", max_matches_per_file=2
        )
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            [item["line_number"] for item in result["results"]], [1, 2]
        )

    def test_global_limit_and_truncation(self):
        self.write(self.docs / "a.txt", "needle\nneedle\nneedle\n")
        self.write(self.docs / "b.txt", "needle\nneedle\nneedle\n")
        result = self.service().search("Documents", "needle", limit=2)
        self.assertEqual(result["count"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "result limit")

    def test_excluded_directories(self):
        for relative in (
            ".git/config.txt",
            "node_modules/pkg.txt",
            ".cache/cached.txt",
            "__pycache__/cache.txt",
            "build/out.txt",
            "dist/out.txt",
            "target/out.txt",
            ".local/share/Trash/deleted.txt",
            ".venv/lib.txt",
        ):
            self.write(self.docs / relative, "needle hidden\n")
        self.write(self.docs / "visible.txt", "needle visible\n")

        result = self.service().search("Documents", "needle")
        self.assertEqual(
            [item["path"] for item in result["results"]], ["visible.txt"]
        )

    def test_binary_files_are_skipped(self):
        self.write(self.docs / "binary.dat", b"needle\x00binary needle\n")
        self.write(self.docs / "text.txt", "needle text\n")

        result = self.service().search("Documents", "needle")
        self.assertEqual(
            [item["path"] for item in result["results"]], ["text.txt"]
        )
        self.assertNotIn("\x00", json.dumps(result))

    def test_symlinks_are_not_followed(self):
        outside = self.write(
            self.home / "outside-secret.txt", "TOP_SECRET needle\n"
        )
        outside_dir = self.home / "outside-dir"
        self.write(outside_dir / "secret.txt", "TOP_SECRET needle\n")
        (self.docs / "link.txt").symlink_to(outside)
        (self.docs / "linkdir").symlink_to(outside_dir, target_is_directory=True)

        result = self.service().search("Documents", "needle")
        self.assertEqual(result["results"], [])
        self.assertNotIn("TOP_SECRET", json.dumps(result))

    def test_line_text_is_bounded(self):
        self.write(
            self.docs / "long.txt",
            "needle" + "x" * (MAX_LINE_CHARS + 300) + "\n",
        )
        result = self.service().search("Documents", "needle")
        self.assertEqual(len(result["results"][0]["text"]), MAX_LINE_CHARS)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "line length")

    def test_invalid_regex_is_rg_error(self):
        self.write(self.docs / "x.txt", "needle\n")
        result = self.service().search("Documents", "[")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "rg_error")
        self.assertEqual(
            result["message"],
            TextSearchService.ERROR_MESSAGES["rg_error"],
        )

    def test_invalid_regex_as_fixed_string_succeeds(self):
        self.write(self.docs / "x.txt", "a [ bracket\n")
        result = self.service().search(
            "Documents", "[", fixed_strings=True
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["count"], 1)

    def test_key_relative_and_absolute_scope_agree(self):
        self.write(self.docs / "Reports" / "r.txt", "needle\n")
        self.write(self.docs / "other.txt", "needle\n")
        for folder in ("Documents/Reports", str(self.docs / "Reports")):
            with self.subTest(folder=folder):
                result = self.service().search(folder, "needle")
                self.assertEqual(
                    [item["path"] for item in result["results"]],
                    ["r.txt"],
                )

    def test_no_match_is_success(self):
        self.write(self.docs / "x.txt", "unrelated\n")
        result = self.service().search("Documents", "needle")
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["message"], "No matching text was found.")


if __name__ == "__main__":
    unittest.main()
