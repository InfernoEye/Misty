import unittest

from app.desktop.accessibility.service import AccessibilityService


class FakeNumericState:
    def __init__(self, name, number):
        self.name = name
        self.number = number

    def __str__(self):
        return str(self.number)

    def __repr__(self):
        return f"<StateType.{self.name}: {self.number}>"


class FakeStateSet:
    def get_states(self):
        return [
            FakeNumericState("VISIBLE", 30),
            FakeNumericState("SENSITIVE", 24),
        ]


class FakeNode:
    def __init__(self, name="", role="panel", pid=123, children=None):
        self.name = name
        self.role = role
        self.pid = pid
        self.children = list(children or [])

    def get_name(self):
        return self.name

    def get_description(self):
        return "test description"

    def get_role_name(self):
        return self.role

    def get_process_id(self):
        return self.pid

    def get_accessible_id(self):
        return "test-id"

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_state_set(self):
        return FakeStateSet()

    def get_attributes(self):
        return {"toolkit": "test"}

    def get_interfaces(self):
        return ["Accessible", "Component"]


class AccessibilityServiceTests(unittest.TestCase):
    def setUp(self):
        button = FakeNode("Confirm", "button")
        panel = FakeNode("Dialog", "frame", children=[button])
        app = FakeNode("Test App", "application", children=[panel])
        self.desktop = FakeNode(
            "main", "desktop", children=[app]
        )
        self.service = AccessibilityService(lambda: self.desktop)

    def test_connect(self):
        result = self.service.connect()
        self.assertTrue(result["success"])
        self.assertTrue(self.service.connected)

    def test_list_applications(self):
        result = self.service.list_applications()
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(
            result["applications"][0]["name"], "Test App"
        )
        self.assertEqual(
            result["applications"][0]["process_id"], 123
        )

    def test_inspect_nested_controls(self):
        result = self.service.inspect_application(process_id=123)
        self.assertTrue(result["success"])

        button = result["tree"]["children"][0]["children"][0]
        self.assertEqual(button["name"], "Confirm")
        self.assertEqual(button["role"], "button")
        self.assertIn("visible", button["states"])
        self.assertEqual(result["nodes_inspected"], 3)

    def test_state_name_handles_numeric_gi_enums(self):
        self.assertEqual(
            self.service._state_name(FakeNumericState("ENABLED", 8)),
            "enabled",
        )
        self.assertEqual(
            self.service._state_name("Atspi.StateType.VISIBLE"),
            "visible",
        )

    def test_requires_exactly_one_selector(self):
        self.assertFalse(
            self.service.inspect_application()["success"]
        )
        self.assertFalse(
            self.service.inspect_application(
                process_id=123, name="Test App"
            )["success"]
        )

    def test_duplicate_name_is_ambiguous(self):
        self.desktop.children.append(
            FakeNode("Test App", "application", pid=456)
        )
        result = self.service.inspect_application(name="Test App")
        self.assertFalse(result["success"])
        self.assertIn("process_id", result["message"])

    def test_depth_and_node_limits(self):
        depth = self.service.inspect_application(
            process_id=123, max_depth=0
        )
        self.assertTrue(depth["success"])
        self.assertTrue(depth["tree"]["truncated"])

        nodes = self.service.inspect_application(
            process_id=123, max_nodes=1
        )
        self.assertEqual(nodes["nodes_inspected"], 1)
        self.assertTrue(nodes["truncated"])

    def test_missing_application(self):
        result = self.service.inspect_application(process_id=999)
        self.assertFalse(result["success"])
        self.assertIsNone(result["tree"])

    def test_disconnect(self):
        self.service.connect()
        self.service.disconnect()
        self.assertFalse(self.service.connected)


if __name__ == "__main__":
    unittest.main()
