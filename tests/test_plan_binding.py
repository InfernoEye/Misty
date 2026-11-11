"""Observation-driven continuation: ``$steps.<id>.result.<field>`` references.

Covered here:
* TaskPlanner rejects self, forward, unknown and not-in-depends_on refs,
  multiple refs per argument, and refs with disallowed characters.
* Execution substitutes a scalar taken from the fake prior result and the
  registry receives the resolved value (never the literal template).
* A missing/non-scalar/oversized field fails the step closed: the tool is
  never called and the failure is reported.
* Injection-shaped strings in a path field do not crash; they simply fail.
* At most one continuation round reacts to an observation.
"""

import json
import unittest

from app.brain.agent import FINAL_RESPONSE_INSTRUCTIONS, PLANNING_INSTRUCTIONS
from app.brain.planner import (
    PlanValidationError,
    ResultReferenceError,
    TaskPlanner,
    find_result_references,
    parse_reference,
)

from tests.agent_fakes import (
    EXTENDED_TOOL_DEFINITIONS,
    FakeRegistry,
    TestableMistyAgent,
    failure,
    plan_response,
    plan_step,
    success,
)


DEFINITIONS = EXTENDED_TOOL_DEFINITIONS

REF = "$steps.step-1.result.folder"


def planner():
    return TaskPlanner()


def structured(steps):
    return planner().prepare_structured_plan({"steps": steps}, DEFINITIONS)


class ReferenceSyntaxTests(unittest.TestCase):

    def test_valid_reference_parses(self):
        reference = parse_reference(REF)
        self.assertIsNotNone(reference)
        self.assertEqual(reference.step_id, "step-1")
        self.assertEqual(reference.path, ("folder",))
        nested = parse_reference("$steps.step_2.result.outer.inner")
        self.assertEqual(nested.path, ("outer", "inner"))

    def test_whole_value_only(self):
        for value in (
            "prefix " + REF,
            REF + " suffix",
            REF + " and $steps.step-1.result.other",
            "$steps.step-1.result",
            "$steps..result.folder",
            "$steps.step-1.result.",
            "$steps.step-1.result.__class__",
            "$steps.step-1.result.a.b.c.d.e",
            "$steps.step 1.result.folder",
            "",
            None,
            5,
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_reference(value))

    def test_a_list_item_is_addressable(self):
        # search_files returns "paths" as a list; "the first hit" is the
        # natural way to feed one into a tool that takes a single path.
        reference = parse_reference("$steps.step-1.result.paths.0")
        self.assertIsNotNone(reference)
        self.assertEqual(reference.path, ("paths", "0"))

    def test_a_nested_list_item_is_addressable(self):
        reference = parse_reference("$steps.step-1.result.data.paths.12")
        self.assertEqual(reference.path, ("data", "paths", "12"))

    def test_bad_indices_are_not_references(self):
        for value in (
            "$steps.step-1.result.0",          # index with no field
            "$steps.step-1.result.paths.0.1",  # nested indexing
            "$steps.step-1.result.paths.-1",   # negative
            "$steps.step-1.result.paths.0x",   # not a plain index
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_reference(value))

    def test_find_references_is_bounded(self):
        found = find_result_references(
            "$steps.a-1.result.x/$steps.b-2.result.y"
        )
        self.assertEqual([item.step_id for item in found], ["a-1", "b-2"])


class PlannerReferenceValidationTests(unittest.TestCase):

    def _plan(self, source_id="step-1", depends=("step-1",), tool="open_folder"):
        return [
            plan_step(
                "step-1",
                "get_file_info",
                {"folder": "Home", "relative_path": "notes.txt"},
            ),
            plan_step(
                "step-2", tool, {"folder": REF}, depends_on=list(depends)
            ),
        ]

    def test_valid_reference_in_dependency_is_accepted(self):
        plan = structured(self._plan())
        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.steps[1].arguments["folder"], REF)

    def test_reference_to_later_step_is_rejected(self):
        with self.assertRaises(PlanValidationError) as caught:
            structured([
                plan_step("step-1", "open_folder",
                          {"folder": "$steps.step-2.result.folder"},
                          depends_on=["step-2"]),
                plan_step("step-2", "get_file_info",
                          {"folder": "Home", "relative_path": "a.txt"}),
            ])
        self.assertIn("not declared earlier", str(caught.exception))

    def test_self_reference_is_rejected(self):
        with self.assertRaises(PlanValidationError) as caught:
            structured([
                plan_step("step-1", "open_folder",
                          {"folder": "$steps.step-1.result.folder"},
                          depends_on=["step-1"]),
            ])
        self.assertIn("itself", str(caught.exception))

    def test_unknown_step_reference_is_rejected(self):
        # A step that depends on step-1 but names step-9 in its template is
        # rejected because step-9 is not part of the plan.
        plan = self._plan()
        plan[1]["arguments"]["folder"] = "$steps.step-9.result.folder"
        with self.assertRaises(PlanValidationError) as caught:
            structured(plan)
        self.assertIn("step-9", str(caught.exception))

    def test_dependency_without_reference_is_fine(self):
        plan = structured([
            plan_step("step-1", "get_time"),
            plan_step("step-2", "open_folder",
                      {"folder": "Home"}, depends_on=["step-1"]),
        ])
        self.assertEqual(plan.steps[1].arguments["folder"], "Home")

    def test_reference_without_depends_on_is_rejected(self):
        with self.assertRaises(PlanValidationError) as caught:
            structured([
                plan_step("step-1", "get_file_info",
                          {"folder": "Home", "relative_path": "a.txt"}),
                plan_step("step-2", "open_folder",
                          {"folder": REF}, depends_on=[]),
            ])
        self.assertIn("depends_on", str(caught.exception))

    def test_reference_through_a_non_dependency_is_rejected(self):
        # step-3 depends on step-2 only; step-1 is earlier but not a
        # dependency of step-3, so its result may not be used.
        with self.assertRaises(PlanValidationError) as caught:
            structured([
                plan_step("step-1", "get_file_info",
                          {"folder": "Home", "relative_path": "a.txt"}),
                plan_step("step-2", "get_time"),
                plan_step("step-3", "open_folder",
                          {"folder": REF}, depends_on=["step-2"]),
            ])
        self.assertIn("depends_on", str(caught.exception))

    def test_multiple_references_in_one_argument_are_rejected(self):
        # A free-form string argument can hold the double reference, so the
        # "at most one" rule is what rejects it (not enum validation).
        with self.assertRaises(PlanValidationError) as caught:
            structured([
                plan_step("step-1", "get_file_info",
                          {"folder": "Home", "relative_path": "a.txt"}),
                plan_step("step-2", "search_files",
                          {"query": REF + "/" + REF.replace("folder", "name")},
                          depends_on=["step-1"]),
            ])
        self.assertIn("at most one", str(caught.exception))

    def test_reference_with_surrounding_text_is_rejected(self):
        with self.assertRaises(PlanValidationError):
            structured([
                plan_step("step-1", "get_file_info",
                          {"folder": "Home", "relative_path": "a.txt"}),
                plan_step("step-2", "open_folder",
                          {"folder": "go to " + REF},
                          depends_on=["step-1"]),
            ])

    def test_reference_to_non_string_argument_is_rejected(self):
        # search_files.limit is an integer with a minimum of 1, so it is a
        # scalar reference target; the refusal must come from the dependency
        # and reference rules, never from silently substituting an object.
        with self.assertRaises(PlanValidationError) as caught:
            structured([
                plan_step("step-1", "list_files",
                          {"folder": "Home"}),
                plan_step("step-2", "search_files",
                          {"query": "x",
                           "limit": "$steps.step-1.result.count"},
                          depends_on=[]),
            ])
        self.assertIn("depends_on", str(caught.exception))

    def test_malformed_reference_in_enum_argument_is_rejected(self):
        # open_folder.folder is an enum: a "$steps." template that is not a
        # well-formed reference cannot fill it and is rejected as invalid.
        for value in (
            "$steps.step-1.result",
            "$steps..result.folder",
            "$steps.step-1.result.__class__",
        ):
            with self.subTest(value=value):
                with self.assertRaises(PlanValidationError):
                    structured([
                        plan_step("step-1", "get_file_info",
                                  {"folder": "Home", "relative_path": "a.txt"}),
                        plan_step("step-2", "open_folder",
                                  {"folder": value},
                                  depends_on=["step-1"]),
                    ])

    def test_valid_reference_in_enum_argument_is_accepted(self):
        # A well-formed reference may fill an enum argument: the provider
        # chooses the value, and the executor substitutes it at run time.
        plan = structured([
            plan_step("step-1", "get_file_info",
                      {"folder": "Home", "relative_path": "a.txt"}),
            plan_step("step-2", "open_folder",
                      {"folder": "$steps.step-1.result.folder"},
                      depends_on=["step-1"]),
        ])
        self.assertEqual(
            plan.steps[1].arguments["folder"],
            "$steps.step-1.result.folder",
        )


class SubstitutionTests(unittest.TestCase):

    def _plan_with_result(self, result):
        plan = structured([
            plan_step("step-1", "get_file_info",
                      {"folder": "Home", "relative_path": "a.txt"}),
            plan_step("step-2", "open_folder",
                      {"folder": REF}, depends_on=["step-1"]),
        ])
        plan.steps[0].status = "success"
        plan.steps[0].result = result
        return plan

    def test_scalar_is_substituted_from_prior_result(self):
        plan = self._plan_with_result({"success": True, "folder": "Documents"})
        arguments = planner().substitute_references(plan, 1)
        self.assertEqual(arguments, {"folder": "Documents"})
        # The plan itself is untouched (immutable evidence).
        self.assertEqual(plan.steps[1].arguments["folder"], REF)

    def test_nested_field_and_number_are_substituted(self):
        plan = structured([
            plan_step("step-1", "get_file_info",
                      {"folder": "Home", "relative_path": "a.txt"}),
            plan_step("step-2", "search_files",
                      {"query": "$steps.step-1.result.meta.name",
                       "limit": "$steps.step-1.result.count"},
                      depends_on=["step-1"]),
        ])
        plan.steps[0].status = "success"
        plan.steps[0].result = {
            "success": True, "meta": {"name": "report"}, "count": 7,
        }
        arguments = planner().substitute_references(plan, 1)
        self.assertEqual(arguments, {"query": "report", "limit": "7"})

    def test_missing_field_fails_closed(self):
        plan = self._plan_with_result({"success": True, "directory": "Docs"})
        with self.assertRaises(ResultReferenceError) as caught:
            planner().substitute_references(plan, 1)
        self.assertIn("missing field", str(caught.exception))

    def test_non_scalar_value_fails_closed(self):
        for value in (["Documents"], {"key": "v"}, None):
            with self.subTest(value=value):
                plan = self._plan_with_result(
                    {"success": True, "folder": value}
                )
                with self.assertRaises(ResultReferenceError):
                    planner().substitute_references(plan, 1)

    def test_failed_dependency_fails_closed(self):
        plan = self._plan_with_result({"success": False, "folder": "Docs"})
        plan.steps[0].status = "failed"
        with self.assertRaises(ResultReferenceError):
            planner().substitute_references(plan, 1)

    def test_oversized_value_fails_closed(self):
        plan = self._plan_with_result(
            {"success": True, "folder": "x" * 1000}
        )
        with self.assertRaises(ResultReferenceError):
            planner().substitute_references(plan, 1)

    def test_control_characters_fail_closed(self):
        plan = self._plan_with_result(
            {"success": True, "folder": "Docs\nrm -rf"}
        )
        with self.assertRaises(ResultReferenceError):
            planner().substitute_references(plan, 1)

    def test_injection_shaped_path_does_not_crash(self):
        # Model-supplied text in a *path* position that is not a reference
        # is ordinary text: no traversal, no attribute access, no crash.
        for folder in (
            "$" + "{__import__('os')}",
            "$steps",
            "$steps.",
            "$steps.step-1",
            "$steps.step-1.result",
            "__import__('os').system('id')",
            "../../etc/passwd",
        ):
            with self.subTest(folder=folder):
                try:
                    plan = structured([
                        plan_step("step-1", "get_file_info",
                                  {"folder": "Home",
                                   "relative_path": "a.txt"}),
                        plan_step("step-2", "open_folder",
                                  {"folder": folder},
                                  depends_on=["step-1"]),
                    ])
                except PlanValidationError:
                    continue  # rejected by enum validation: also fine
                plan.steps[0].status = "success"
                plan.steps[0].result = {"success": True, "folder": "Docs"}
                if folder == "$steps.step-1.result":
                    with self.assertRaises(ResultReferenceError):
                        planner().substitute_references(plan, 1)
                    continue
                arguments = planner().substitute_references(plan, 1)
                self.assertEqual(arguments["folder"], folder)

    def test_dict_attribute_is_never_traversed(self):
        plan = self._plan_with_result({"success": True, "folder": "Docs"})
        plan.steps[1].arguments["folder"] = (
            "$steps.step-1.result.__class__.__mro__"
        )
        with self.assertRaises(ResultReferenceError):
            planner().substitute_references(plan, 1)


class AgentBindingTests(unittest.TestCase):
    """End-to-end through MistyAgent.run with the fake registry."""

    def test_substituted_value_reaches_the_registry(self):
        registry = FakeRegistry(extended_tools=True, results=[
            success("found", folder="Documents"),
            success("opened"),
        ])
        agent = TestableMistyAgent(registry, [
            plan_response([
                plan_step("step-1", "get_file_info",
                          {"folder": "Home", "relative_path": "a.txt"}),
                plan_step("step-2", "open_folder",
                          {"folder": REF}, depends_on=["step-1"]),
            ]),
            {"content": "Opened it."},
        ])

        answer = agent.run("Find notes.txt and open its folder")

        self.assertEqual(registry.executed, ["get_file_info", "open_folder"])
        self.assertEqual(registry.arguments[1], {"folder": "Documents"})
        self.assertNotIn(REF, json.dumps(registry.arguments[1]))
        self.assertEqual(answer, "Opened it.")
        self.assertEqual(agent.final_payload()["outcome"], "completed")

    def test_plan_validation_rejects_forward_reference_without_executing(self):
        registry = FakeRegistry(extended_tools=True)
        agent = TestableMistyAgent(registry, [
            plan_response([
                plan_step("step-1", "open_folder",
                          {"folder": REF}, depends_on=["step-2"]),
                plan_step("step-2", "get_file_info",
                          {"folder": "Home", "relative_path": "a.txt"}),
            ]),
        ])

        answer = agent.run("Open that folder")

        self.assertIn("Task plan rejected", answer)
        self.assertEqual(registry.executed, [])

    def test_missing_result_field_is_not_executed(self):
        registry = FakeRegistry(extended_tools=True, results=[
            success("found"),  # no "folder" field
            success("opened"),
        ])
        agent = TestableMistyAgent(registry, [
            plan_response([
                plan_step("step-1", "get_file_info",
                          {"folder": "Home", "relative_path": "a.txt"}),
                plan_step("step-2", "open_folder",
                          {"folder": REF}, depends_on=["step-1"]),
            ]),
            {"content": "Could not open."},
        ])

        agent.run("Find notes.txt and open its folder")

        # The dependent tool never ran; only the prerequisite did.
        self.assertEqual(registry.executed, ["get_file_info"])
        payload = agent.final_payload()
        self.assertEqual(payload["outcome"], "failed")
        dependent = [
            entry for entry in payload["execution_results"]
            if entry["tool"] == "open_folder"
        ][0]
        self.assertEqual(dependent["status"], "failed")
        self.assertIs(dependent["result"].get("not_executed"), True)

    def test_injection_ref_in_path_field_is_reported_not_crashing(self):
        registry = FakeRegistry(extended_tools=True, results=[success("x")])
        agent = TestableMistyAgent(registry, [
            plan_response([
                plan_step("step-1", "get_file_info",
                          {"folder": "Home", "relative_path": "a.txt"}),
                plan_step("step-2", "open_folder",
                          {"folder": "$steps.step-1.result"},
                          depends_on=["step-1"]),
            ]),
        ], )

        aanswer = agent.run("Open something")
        self.assertIsInstance(aanswer, str)
        self.assertTrue(aanswer)
        self.assertEqual(registry.executed, [])

    def test_one_continuation_round_only(self):
        # step-2 reacts to step-1 (allowed). step-3 quotes step-1's result
        # text, i.e. the model is proposing a *second* continuation round
        # from a fresh observation; that step must not execute.
        registry = FakeRegistry(extended_tools=True, results=[
            success("the folder is Documents", folder="Documents"),
            success("opened"),
            success("third"),
        ])
        agent = TestableMistyAgent(registry, [
            plan_response([
                plan_step("step-1", "get_file_info",
                          {"folder": "Home", "relative_path": "a.txt"}),
                plan_step("step-2", "open_folder",
                          {"folder": REF}, depends_on=["step-1"]),
                plan_step("step-3", "search_files",
                          {"query": "the folder is Documents"},
                          depends_on=["step-1"]),
            ]),
            {"content": "Did what was allowed."},
        ])

        agent.run("Find it and go")

        self.assertEqual(registry.executed, ["get_file_info", "open_folder"])
        payload = agent.final_payload()
        self.assertEqual(payload["outcome"], "failed")
        third = [
            entry for entry in payload["execution_results"]
            if entry["tool"] == "search_files"
        ][0]
        self.assertIs(third["result"].get("not_executed"), True)


class PromptTests(unittest.TestCase):

    def test_planning_instructions_document_references_and_retries(self):
        self.assertIn("$steps.<step_id>.result.<field>", PLANNING_INSTRUCTIONS)
        self.assertIn("depends_on", PLANNING_INSTRUCTIONS)
        self.assertIn("never retried automatically", PLANNING_INSTRUCTIONS)
        self.assertIn("one follow-up step", PLANNING_INSTRUCTIONS)

    def test_final_instructions_use_outcome(self):
        self.assertIn("'outcome'", FINAL_RESPONSE_INSTRUCTIONS)
        self.assertIn("'recovered'", FINAL_RESPONSE_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
