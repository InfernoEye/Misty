"""Tool accuracy ratings, and what the planner does with them.

The ratings are data: they describe how reliably a tool resolves its
arguments so the planner can reason about an imprecise request. The only
enforcement is narrow — a value the user never supplied, typed into a tool
that cannot search for it, becomes a question instead of a failed call.
"""

import unittest

from app.brain.accuracy import (
    DEFAULT_ACCURACY,
    accuracy_for,
    can_discover,
    invented_value,
    needs_exact_value,
    rated_tools,
    suspicious_steps,
)
from app.brain.planner import PlanStep


class RatingTests(unittest.TestCase):
    def test_an_unlisted_tool_gets_the_conservative_default(self):
        self.assertEqual(accuracy_for("some_future_tool"), dict(DEFAULT_ACCURACY))

    def test_garbage_input_never_raises(self):
        for value in (None, 5, [], object()):
            self.assertEqual(accuracy_for(value), dict(DEFAULT_ACCURACY))

    def test_discovery_tools_can_resolve_a_description(self):
        for tool in ("search_files", "grep_files", "recall", "web_search"):
            with self.subTest(tool=tool):
                self.assertTrue(can_discover(tool))

    def test_reading_an_exact_path_cannot_discover(self):
        self.assertFalse(can_discover("read_document"))
        self.assertTrue(needs_exact_value("read_document"))

    def test_window_control_resolves_its_own_target(self):
        # The service matches titles and brings its own search; a window id
        # is not a value the planner must already know.
        self.assertFalse(needs_exact_value("move_window_to_screen"))

    def test_the_table_is_immutable(self):
        table = accuracy_for("search_files")
        table["precision"] = "exact"
        self.assertEqual(accuracy_for("search_files")["precision"], "fuzzy")

    def test_ratings_cover_the_tools_that_take_paths(self):
        for tool in (
            "read_document", "write_text_file", "copy_path", "move_path",
            "zip_create", "transcribe_file",
        ):
            with self.subTest(tool=tool):
                self.assertIn(tool, rated_tools())


class InventedValueTests(unittest.TestCase):
    REQUEST = "On desktop, there is transcript. Please read it again."

    def blocked(self, tool="read_document", argument="relative_path", value=""):
        return invented_value(tool, argument, value, self.REQUEST, []) is not None

    def test_the_hallucinated_name_from_a_real_session_is_caught(self):
        # Real failure: the user said "transcript", the plan invented
        # "transcript.NIST-D" and the call failed on a missing path.
        self.assertTrue(self.blocked(value="transcript.NIST-D"))

    def test_a_plain_extension_on_a_named_stem_is_allowed(self):
        self.assertFalse(self.blocked(value="Transcript.txt"))

    def test_a_name_the_user_never_gave_is_caught(self):
        self.assertTrue(self.blocked(value="invoice-2024.pdf"))

    def test_a_result_reference_is_never_flagged(self):
        self.assertFalse(self.blocked(value="$steps.step-1.result.path"))

    def test_a_fuzzy_tool_is_never_flagged(self):
        self.assertFalse(
            invented_value("search_files", "query", "transcript", self.REQUEST, [])
        )

    def test_a_subdirectory_of_a_named_file_is_allowed(self):
        self.assertFalse(self.blocked(value="Documents/Transcript.txt"))

    def test_a_window_id_is_not_a_verbatim_value(self):
        self.assertFalse(
            invented_value(
                "move_window_to_screen", "window_id", "firefox-id", "move Firefox", []
            )
        )

    def test_a_css_selector_is_not_a_verbatim_value(self):
        self.assertFalse(
            invented_value("browser_click", "selector", "#buy", "click buy", [])
        )

    def test_a_value_from_an_earlier_result_is_allowed(self):
        self.assertFalse(
            invented_value(
                "read_document", "relative_path", "notes.md", self.REQUEST,
                ["Found notes.md in Documents."],
            )
        )

    def test_non_path_arguments_are_ignored(self):
        self.assertFalse(
            invented_value("read_document", "folder", "Desktop", self.REQUEST, [])
        )


class ReadVersusRevealTests(unittest.TestCase):
    """Asking to read a file must not be answered by opening a file manager."""

    def test_the_reveal_tools_disclaim_returning_content(self):
        from app.brain.tools import _QOL_REVEAL_TOOL_DEFINITIONS

        by_name = {
            item["function"]["name"]: item["function"]["description"]
            for item in _QOL_REVEAL_TOOL_DEFINITIONS
        }
        for name in ("open_last_result", "reveal_in_file_manager"):
            with self.subTest(tool=name):
                description = by_name[name].casefold()
                self.assertIn("no file content", description)
                self.assertIn("read_document", description)

    def test_read_document_points_at_search_first(self):
        from app.brain.tools import TOOL_DEFINITIONS

        text = next(
            item["function"]["description"]
            for item in TOOL_DEFINITIONS
            if item["function"]["name"] == "read_document"
        ).casefold()
        self.assertIn("search_files", text)

    def test_the_prompt_separates_reading_from_opening(self):
        # The rule ships with every plan, so assert it on the assembled
        # system prompt rather than on one fragment.
        from app.brain.agent import SYSTEM_PROMPT

        text = SYSTEM_PROMPT.casefold()
        self.assertIn("read_document", text)
        self.assertIn("open_last_result", text)
        self.assertIn("contents", text)


class PlanAdviceTests(unittest.TestCase):
    def test_a_guessing_plan_is_reported(self):
        plan = [
            PlanStep(
                call_id="s1",
                name="read_document",
                arguments={"relative_path": "transcript.NIST-D"},
                description="",
                position=1,
            )
        ]
        found = suspicious_steps(plan, "read the transcript on desktop")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], "read_document")

    def test_a_search_then_read_plan_is_clean(self):
        plan = [
            PlanStep(
                call_id="s1",
                name="search_files",
                arguments={"query": "transcript"},
                description="",
                position=1,
            ),
            PlanStep(
                call_id="s2",
                name="read_document",
                arguments={"relative_path": "$steps.s1.result.path"},
                description="",
                position=2,
            ),
        ]
        self.assertEqual(
            suspicious_steps(plan, "read the transcript on desktop"), []
        )

    def test_malformed_steps_never_raise(self):
        self.assertEqual(suspicious_steps([None, 5, "x"], "request"), [])


if __name__ == "__main__":
    unittest.main()
