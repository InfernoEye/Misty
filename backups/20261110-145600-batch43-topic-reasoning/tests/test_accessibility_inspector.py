"""Tests for the isolated read-only accessibility inspector adapter."""

import json
import unittest

from app.desktop.accessibility.inspector import (
    MAX_DEPTH,
    MAX_NODES,
    AccessibilityInspector,
)


def make_node(
    name,
    role,
    *,
    description="",
    children=None,
    states=None,
    truncated=False,
    child_count=None,
):
    children = list(children or [])
    return {
        "name": name,
        "role": role,
        "description": description,
        "states": list(states or []),
        "child_count": (
            len(children) if child_count is None else child_count
        ),
        "children": children,
        "truncated": truncated,
    }


class FakeService:
    """Duck-typed service double with explicit mutating tripwires."""

    def __init__(
        self,
        applications=None,
        tree=None,
        actions=None,
        list_result=None,
        inspection=None,
        list_raises=False,
        inspect_raises=False,
        actions_raise=False,
    ):
        self.applications = list(applications or [])
        self.tree = tree
        self.actions = dict(actions or {})
        self.list_result = list_result
        self.inspection = inspection
        self.list_raises = list_raises
        self.inspect_raises = inspect_raises
        self.actions_raise = actions_raise

        self.list_calls = 0
        self.inspect_calls = []
        self.action_calls = []
        self.mutating_calls = []

    def list_applications(self):
        self.list_calls += 1
        if self.list_raises:
            raise RuntimeError("raw service failure: secret accessible text")
        if self.list_result is not None:
            return self.list_result
        return {
            "success": True,
            "applications": self.applications,
            "count": len(self.applications),
            "message": "ok",
        }

    def inspect_application(
        self,
        *,
        process_id=None,
        name=None,
        max_depth=8,
        max_nodes=500,
    ):
        self.inspect_calls.append({
            "process_id": process_id,
            "name": name,
            "max_depth": max_depth,
            "max_nodes": max_nodes,
        })
        if self.inspect_raises:
            raise RuntimeError("raw service failure: secret accessible text")
        if self.inspection is not None:
            return self.inspection
        return {
            "success": True,
            "application": (
                self.applications[0] if self.applications else None
            ),
            "tree": self.tree,
            "truncated": False,
            "errors": [],
            "message": "ok",
        }

    def list_actions(self, *, process_id, path):
        self.action_calls.append({"process_id": process_id, "path": path})
        if self.actions_raise:
            raise RuntimeError("raw action failure: secret accessible text")
        actions = self.actions.get(path, [])
        return {"success": True, "actions": actions, "message": "ok"}

    # These must never be reached by an observation-only adapter.
    def perform_action(self, *args, **kwargs):
        self.mutating_calls.append("perform_action")
        raise AssertionError("perform_action must not be called")

    def do_action(self, *args, **kwargs):
        self.mutating_calls.append("do_action")
        raise AssertionError("do_action must not be called")

    def press(self, *args, **kwargs):
        self.mutating_calls.append("press")
        raise AssertionError("press must not be called")

    def set_value(self, *args, **kwargs):
        self.mutating_calls.append("set_value")
        raise AssertionError("set_value must not be called")


class AccessibilityInspectorTests(unittest.TestCase):
    def setUp(self):
        self.button = make_node(
            "Confirm",
            "button",
            description="Confirm the dialog",
            states=["enabled", "sensitive", "visible"],
        )
        self.panel = make_node(
            "Dialog",
            "frame",
            description="A dialog",
            children=[self.button],
            states=["showing", "visible"],
        )
        self.root = make_node(
            "Test App",
            "application",
            description="Test application",
            children=[self.panel],
            states=["active"],
        )
        self.service = self.make_service()

    def make_service(self, **overrides):
        applications = overrides.pop(
            "applications",
            [{
                "name": "Test App",
                "role": "application",
                "process_id": 123,
                "desktop_index": 0,
                "accessible_id": "app-1",
                "child_count": 1,
            }],
        )
        tree = overrides.pop("tree", self.root)
        actions = overrides.pop(
            "actions",
            {
                "0/0": [{"index": 0, "name": "Set Focus"}],
                "0/0/0": [
                    {"index": 0, "name": "Press"},
                    {"index": 1, "name": "Set Focus"},
                ],
            },
        )
        return FakeService(
            applications=applications,
            tree=tree,
            actions=actions,
            **overrides,
        )

    def assert_no_mutations(self, service=None):
        service = service or self.service
        self.assertEqual(service.mutating_calls, [])

    def assert_json(self, result):
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertIsInstance(encoded, str)
        self.assertEqual(json.loads(encoded), result)

    # ------------------------------------------------------------------
    # Success paths

    def test_list_applications_returns_plain_bounded_records(self):
        inspector = AccessibilityInspector(self.service)

        result = inspector.list_applications()

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["applications"][0]["name"], "Test App")
        self.assertEqual(result["applications"][0]["process_id"], 123)
        self.assertEqual(result["applications"][0]["role"], "application")
        self.assertEqual(result["applications"][0]["desktop_index"], 0)
        self.assert_json(result)
        self.assertEqual(self.service.list_calls, 1)
        self.assert_no_mutations()

    def test_flattens_nested_tree_with_stable_paths_and_actions(self):
        inspector = AccessibilityInspector(self.service)

        result = inspector.inspect_application(123, 8, 100)

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 3)
        self.assertEqual(
            [node["path"] for node in result["nodes"]],
            ["0", "0/0", "0/0/0"],
        )
        self.assertEqual(
            [node["depth"] for node in result["nodes"]], [0, 1, 2]
        )
        self.assertEqual(
            [node["parent_path"] for node in result["nodes"]],
            [None, "0", "0/0"],
        )
        self.assertEqual(
            [node["child_index"] for node in result["nodes"]],
            [None, 0, 0],
        )
        self.assertEqual(
            [node["role"] for node in result["nodes"]],
            ["application", "frame", "button"],
        )
        self.assertEqual(result["nodes"][2]["name"], "Confirm")
        self.assertEqual(
            result["nodes"][2]["description"], "Confirm the dialog"
        )
        self.assertEqual(
            result["nodes"][2]["states"],
            ["enabled", "sensitive", "visible"],
        )
        self.assertEqual(
            result["nodes"][2]["actions"], ["Press", "Set Focus"]
        )
        self.assertEqual(result["nodes"][1]["actions"], ["Set Focus"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["truncation"]["reasons"], [])
        self.assert_json(result)
        self.assert_no_mutations()

    def test_selector_can_be_a_name_or_single_key_dict(self):
        inspector = AccessibilityInspector(self.service)

        by_name = inspector.inspect_application("Test App", 4, 20)
        by_dict = inspector.inspect_application(
            {"name": "Test App"}, 4, 20
        )

        self.assertTrue(by_name["success"])
        self.assertTrue(by_dict["success"])
        self.assertEqual(
            [call["process_id"] for call in self.service.inspect_calls],
            [123, 123],
        )
        self.assert_json(by_name)
        self.assert_json(by_dict)
        self.assert_no_mutations()

    def test_accepts_adapter_depth_above_service_depth(self):
        inspector = AccessibilityInspector(self.service)

        result = inspector.inspect_application(123, MAX_DEPTH, 100)

        self.assertTrue(result["success"])
        self.assertEqual(self.service.inspect_calls[0]["max_depth"], 20)
        self.assertEqual(
            result["truncation"]["service_depth_limit"], 20
        )
        self.assert_json(result)

    # ------------------------------------------------------------------
    # Truncation

    def test_depth_truncation_is_flagged(self):
        inspector = AccessibilityInspector(self.service)

        result = inspector.inspect_application(123, 1, 100)

        self.assertTrue(result["success"])
        self.assertEqual(
            [node["path"] for node in result["nodes"]], ["0", "0/0"]
        )
        panel = result["nodes"][1]
        self.assertTrue(panel["truncated"])
        self.assertEqual(panel["truncation_reasons"], ["depth"])
        self.assertEqual(panel["children_included"], 0)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation"]["reasons"], ["depth"])
        self.assertEqual(result["truncation"]["depth_limit"], 1)
        self.assert_json(result)

    def test_node_count_truncation_is_flagged(self):
        inspector = AccessibilityInspector(self.service)

        result = inspector.inspect_application(123, 8, 2)

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            [node["path"] for node in result["nodes"]], ["0", "0/0"]
        )
        panel = result["nodes"][1]
        self.assertTrue(panel["truncated"])
        self.assertEqual(panel["truncation_reasons"], ["nodes"])
        self.assertEqual(result["truncation"]["reasons"], ["nodes"])
        self.assertEqual(result["truncation"]["node_limit"], 2)
        self.assert_json(result)

    def test_service_reported_truncation_is_flagged(self):
        truncated_root = make_node(
            "Test App",
            "application",
            child_count=4,
            truncated=True,
        )
        service = self.make_service(tree=truncated_root)
        inspector = AccessibilityInspector(service)

        result = inspector.inspect_application(123, 8, 100)

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["nodes"][0]["truncated"])
        self.assertEqual(
            result["nodes"][0]["truncation_reasons"], ["service"]
        )
        self.assertEqual(result["truncation"]["reasons"], ["service"])
        self.assert_json(result)

    def test_top_level_service_truncation_is_flagged(self):
        flat_root = make_node("Test App", "application")
        inspection = {
            "success": True,
            "tree": flat_root,
            "truncated": True,
            "errors": [],
            "message": "ok",
        }
        service = self.make_service(
            tree=flat_root, inspection=inspection
        )
        inspector = AccessibilityInspector(service)

        result = inspector.inspect_application(123, 8, 100)

        self.assertTrue(result["success"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation"]["reasons"], ["service"])
        self.assert_json(result)

    # ------------------------------------------------------------------
    # Selector and service failures

    def test_duplicate_application_name_is_ambiguous(self):
        duplicate_apps = [
            {
                "name": "Test App",
                "role": "application",
                "process_id": 123,
                "desktop_index": 0,
            },
            {
                "name": "Test App",
                "role": "application",
                "process_id": 456,
                "desktop_index": 1,
            },
        ]
        service = self.make_service(applications=duplicate_apps)
        inspector = AccessibilityInspector(service)

        result = inspector.inspect_application("Test App", 8, 100)

        self.assertFalse(result["success"])
        self.assertIn("process_id", result["message"])
        self.assertEqual(service.inspect_calls, [])
        self.assertEqual(result["nodes"], [])
        self.assert_json(result)
        self.assert_no_mutations(service)

    def test_missing_application_is_reported(self):
        inspector = AccessibilityInspector(self.service)

        result = inspector.inspect_application(999, 8, 100)

        self.assertFalse(result["success"])
        self.assertIn("No matching", result["message"])
        self.assertEqual(self.service.inspect_calls, [])
        self.assert_json(result)
        self.assert_no_mutations()

    def test_connection_failure_is_bounded_and_sanitized(self):
        failure = {
            "success": False,
            "applications": [],
            "count": 0,
            "message": "Could not connect: secret accessible text",
        }
        service = self.make_service(list_result=failure)
        inspector = AccessibilityInspector(service)

        listing = inspector.list_applications()
        inspection = inspector.inspect_application(123, 8, 100)

        self.assertFalse(listing["success"])
        self.assertEqual(listing["applications"], [])
        self.assertNotIn("secret", listing["message"].lower())
        self.assertFalse(inspection["success"])
        self.assertNotIn("secret", inspection["message"].lower())
        self.assertEqual(service.inspect_calls, [])
        self.assert_json(listing)
        self.assert_json(inspection)
        self.assert_no_mutations(service)

    def test_service_exceptions_are_bounded_and_sanitized(self):
        service = self.make_service(list_raises=True)
        inspector = AccessibilityInspector(service)

        listing = inspector.list_applications()
        inspection = inspector.inspect_application(123, 8, 100)

        self.assertFalse(listing["success"])
        self.assertFalse(inspection["success"])
        self.assertNotIn("secret", listing["message"].lower())
        self.assertNotIn("secret", inspection["message"].lower())
        self.assert_json(listing)
        self.assert_json(inspection)

    def test_inspection_failure_and_invalid_tree_are_safe(self):
        failed = self.make_service(
            inspection={
                "success": False,
                "message": "secret accessible text",
            }
        )
        invalid = self.make_service(tree=["not", "a", "node"])
        raising = self.make_service(inspect_raises=True)

        for service in (failed, invalid, raising):
            with self.subTest(service=service):
                result = AccessibilityInspector(service).inspect_application(
                    123, 8, 100
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["nodes"], [])
                self.assertNotIn("secret", result["message"].lower())
                self.assert_json(result)
                self.assert_no_mutations(service)

    def test_malformed_children_are_skipped_without_losing_indices(self):
        good = make_node("Good", "button")
        root = make_node(
            "Test App",
            "application",
            children=["not-a-node", good],
            child_count=2,
        )
        service = self.make_service(tree=root)
        inspector = AccessibilityInspector(service)

        result = inspector.inspect_application(123, 8, 100)

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            [node["path"] for node in result["nodes"]], ["0", "0/1"]
        )
        self.assertEqual(
            result["nodes"][1]["path"], "0/1"
        )
        self.assertTrue(result["nodes"][0]["truncated"])
        self.assertEqual(
            result["nodes"][0]["truncation_reasons"], ["malformed"]
        )
        self.assert_json(result)

    def test_failing_action_listing_does_not_fail_inspection(self):
        service = self.make_service(actions_raise=True)
        inspector = AccessibilityInspector(service)

        result = inspector.inspect_application(123, 8, 100)

        self.assertTrue(result["success"])
        self.assertEqual(result["nodes"][2]["actions"], [])
        self.assertNotIn("secret", json.dumps(result).lower())
        self.assert_json(result)
        self.assert_no_mutations(service)

    # ------------------------------------------------------------------
    # Input validation happens before any service call

    def test_invalid_selectors_are_rejected_before_service_calls(self):
        selectors = [
            "",
            "   ",
            "x" * 257,
            "bad\x00name",
            0,
            -1,
            True,
            1.5,
            None,
            [],
            {},
            {"name": ""},
            {"process_id": 0},
            {"process_id": True},
            {"process_id": 123, "name": "Test App"},
            {"unknown": 1},
        ]

        for selector in selectors:
            with self.subTest(selector=selector):
                service = self.make_service()
                inspector = AccessibilityInspector(service)
                result = inspector.inspect_application(selector, 5, 100)
                self.assertFalse(result["success"])
                self.assertEqual(result["nodes"], [])
                self.assertEqual(service.list_calls, 0)
                self.assertEqual(service.inspect_calls, [])
                self.assert_json(result)
                self.assert_no_mutations(service)

    def test_invalid_depth_is_rejected_before_service_calls(self):
        for depth in (0, MAX_DEPTH + 1, -1, True, "8", None, 8.0):
            with self.subTest(depth=depth):
                service = self.make_service()
                inspector = AccessibilityInspector(service)
                result = inspector.inspect_application(123, depth, 100)
                self.assertFalse(result["success"])
                self.assertIn("max_depth", result["message"])
                self.assertEqual(service.list_calls, 0)
                self.assertEqual(service.inspect_calls, [])
                self.assert_json(result)
                self.assert_no_mutations(service)

    def test_invalid_node_budget_is_rejected_before_service_calls(self):
        for nodes in (0, MAX_NODES + 1, -1, True, "500", None, 500.0):
            with self.subTest(nodes=nodes):
                service = self.make_service()
                inspector = AccessibilityInspector(service)
                result = inspector.inspect_application(123, 8, nodes)
                self.assertFalse(result["success"])
                self.assertIn("max_nodes", result["message"])
                self.assertEqual(service.list_calls, 0)
                self.assertEqual(service.inspect_calls, [])
                self.assert_json(result)
                self.assert_no_mutations(service)

    def test_observation_only_adapter_never_mutates(self):
        inspector = AccessibilityInspector(self.service)

        inspector.list_applications()
        result = inspector.inspect_application(
            {"process_id": 123}, 8, 100
        )

        self.assertTrue(result["success"])
        self.assertGreater(len(self.service.action_calls), 0)
        self.assert_no_mutations()
        self.assertFalse(hasattr(inspector, "perform_action"))
        self.assertFalse(hasattr(inspector, "do_action"))


if __name__ == "__main__":
    unittest.main()
