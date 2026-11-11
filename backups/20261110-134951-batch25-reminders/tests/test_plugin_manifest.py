"""Tests for the isolated, declarative plugin manifest validator.

Everything here runs on temporary directories and inert data. No plugin code is
imported or executed, and no network, Qt, model or database module is touched.
"""

import copy
import json
import math
import os
import tempfile
import unittest
from pathlib import Path

from app.plugins.manifest import (
    ALLOWED_EFFECTS,
    ALLOWED_PERMISSIONS,
    MAX_ERRORS,
    MAX_MANIFEST_BYTES,
    PluginManifest,
    discover_manifests,
    load_manifest,
    validate_manifest,
)


VALID_TOML = """\
id = "example.plugin"
name = "Example Plugin"
version = "1.2.3"
description = "A test plugin."
license = "MIT"
entrypoint = "plugin.py"
permissions = ["read_files", "clipboard"]
min_misty_version = "1.0.0"

[requires]
"core.base" = ">=1.0.0 <2.0.0"

[[tools]]
name = "example_run"
description = "Run the example."
effect = "read_only"
parameters = { type = "object", properties = { query = { type = "string" } }, required = ["query"] }
"""


def valid_data():
    """Return a fresh, fully valid manifest dictionary."""
    return {
        "id": "example.plugin",
        "name": "Example Plugin",
        "version": "1.2.3",
        "description": "A test plugin.",
        "license": "MIT",
        "entrypoint": "plugin.py",
        "permissions": ["read_files", "clipboard"],
        "tools": [
            {
                "name": "example_run",
                "description": "Run the example.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "maxLength": 100}
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "effect": "read_only",
            }
        ],
        "requires": {"core.base": ">=1.0.0 <2.0.0"},
        "min_misty_version": "1.0.0",
    }


def write_json(directory, data, filename="plugin.json"):
    path = Path(directory) / filename
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_text(directory, text, filename="plugin.json"):
    path = Path(directory) / filename
    path.write_text(text, encoding="utf-8")
    return path


def assert_error_field(testcase, result, field):
    fields = [item["field"] for item in result["errors"]]
    testcase.assertIn(field, fields, result)


def assert_json_safe(testcase, value):
    encoded = json.dumps(value, allow_nan=False)
    testcase.assertIsInstance(encoded, str)
    testcase.assertEqual(json.loads(encoded), value)


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tmp = Path(self._temp.name)

    def make_plugin_dir(self, name, data=None, text=None, fmt="json"):
        directory = self.tmp / "plugins" / name
        directory.mkdir(parents=True)
        if text is not None:
            return write_text(directory, text, f"plugin.{fmt}")
        return write_json(directory, data or valid_data(), f"plugin.{fmt}")


class ValidManifestTests(TempDirTestCase):
    def test_valid_json_manifest_loads(self):
        path = write_json(self.tmp, valid_data())
        result = load_manifest(path)

        self.assertTrue(result["success"], result)
        manifest = result["manifest"]
        self.assertEqual(manifest["id"], "example.plugin")
        self.assertEqual(manifest["name"], "Example Plugin")
        self.assertEqual(manifest["version"], "1.2.3")
        self.assertEqual(manifest["permissions"], ["read_files", "clipboard"])
        self.assertEqual(manifest["tools"][0]["effect"], "read_only")
        self.assertEqual(
            manifest["tools"][0]["parameters"]["properties"]["query"],
            {"type": "string", "maxLength": 100},
        )
        self.assertEqual(
            manifest["requires"],
            {"core.base": ">=1.0.0 <2.0.0"},
        )
        self.assertEqual(manifest["min_misty_version"], "1.0.0")
        self.assertEqual(result["errors"], [])
        assert_json_safe(self, result)

    def test_valid_toml_manifest_loads(self):
        path = write_text(self.tmp, VALID_TOML, "plugin.toml")
        result = load_manifest(path)

        self.assertTrue(result["success"], result)
        manifest = result["manifest"]
        self.assertEqual(manifest["id"], "example.plugin")
        self.assertEqual(manifest["permissions"], ["read_files", "clipboard"])
        self.assertEqual(manifest["tools"][0]["name"], "example_run")
        self.assertEqual(
            manifest["tools"][0]["parameters"]["required"],
            ["query"],
        )
        self.assertEqual(
            manifest["requires"],
            {"core.base": ">=1.0.0 <2.0.0"},
        )
        assert_json_safe(self, result)

    def test_optional_fields_may_be_absent(self):
        data = valid_data()
        for key in (
            "description",
            "license",
            "entrypoint",
            "permissions",
            "tools",
            "requires",
            "min_misty_version",
        ):
            data.pop(key)
        result = validate_manifest(data)

        self.assertTrue(result["success"], result)
        manifest = result["manifest"]
        self.assertEqual(manifest["description"], "")
        self.assertEqual(manifest["license"], "")
        self.assertEqual(manifest["entrypoint"], "")
        self.assertEqual(manifest["permissions"], [])
        self.assertEqual(manifest["tools"], [])
        self.assertEqual(manifest["requires"], {})
        self.assertEqual(manifest["min_misty_version"], "")

    def test_validate_manifest_does_not_mutate_input(self):
        data = valid_data()
        snapshot = copy.deepcopy(data)
        validate_manifest(data)
        self.assertEqual(data, snapshot)

    def test_manifest_dataclass_serializes(self):
        result = validate_manifest(valid_data())
        self.assertTrue(result["success"], result)
        manifest = PluginManifest(
            id="example.plugin",
            name="Example",
            version="1.0.0",
            permissions=("read_files",),
        )
        payload = manifest.to_dict()
        self.assertEqual(payload["permissions"], ["read_files"])
        assert_json_safe(self, payload)


class FieldValidationTests(unittest.TestCase):
    def test_invalid_ids(self):
        cases = [
            "",
            "Upper",
            ".hidden",
            "-leading",
            "_leading",
            "bad id",
            "bad/id",
            "a" * 65,
        ]
        for value in cases:
            with self.subTest(value=value):
                data = valid_data()
                data["id"] = value
                result = validate_manifest(data)
                self.assertFalse(result["success"])
                assert_error_field(self, result, "id")

    def test_invalid_versions(self):
        cases = [
            "1.0",
            "v1.0.0",
            "01.0.0",
            "1.00.0",
            "1.0.0-alpha",
            "1.0.0.0",
            "1.0.0 ",
            "0" * 65,
        ]
        for value in cases:
            with self.subTest(value=value):
                data = valid_data()
                data["version"] = value
                result = validate_manifest(data)
                self.assertFalse(result["success"])
                assert_error_field(self, result, "version")

    def test_invalid_min_misty_version(self):
        for value in ("1.0", "latest", 2):
            with self.subTest(value=value):
                data = valid_data()
                data["min_misty_version"] = value
                result = validate_manifest(data)
                self.assertFalse(result["success"])
                assert_error_field(self, result, "min_misty_version")

    def test_invalid_name_description_and_license(self):
        cases = [
            ("name", ""),
            ("name", "x" * 121),
            ("name", "bad\nname"),
            ("description", "x" * 501),
            ("description", 42),
            ("license", "x" * 65),
            ("license", ["MIT"]),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                data = valid_data()
                data[key] = value
                result = validate_manifest(data)
                self.assertFalse(result["success"])
                assert_error_field(self, result, key)

    def test_invalid_permissions(self):
        cases = [
            (["root"], "permissions[0]"),
            (["read_files", "read_files"], "permissions[1]"),
            ("read_files", "permissions"),
            ([123], "permissions[0]"),
            (["read_files"] * 21, "permissions"),
            (["READ_FILES"], "permissions[0]"),
        ]
        for value, field in cases:
            with self.subTest(value=value):
                data = valid_data()
                data["permissions"] = value
                result = validate_manifest(data)
                self.assertFalse(result["success"])
                assert_error_field(self, result, field)

    def test_permissions_allowlist_matches_requirement(self):
        self.assertEqual(
            ALLOWED_PERMISSIONS,
            {
                "read_files",
                "write_files",
                "clipboard",
                "network",
                "desktop",
                "accessibility",
                "memory",
            },
        )

    def test_effects_allowlist_matches_requirement(self):
        self.assertEqual(
            ALLOWED_EFFECTS,
            {
                "read_only",
                "local_write",
                "launch",
                "window_control",
                "accessibility_action",
                "clipboard_read",
                "clipboard_write",
                "memory_write",
                "memory_delete",
                "file_delete",
                "software_install",
                "send_message",
                "upload_private_data",
                "purchase",
                "privileged_setting",
                "unknown_mutation",
            },
        )

    def test_invalid_effect(self):
        for value in ("admin", "", 1):
            with self.subTest(value=value):
                data = valid_data()
                data["tools"][0]["effect"] = value
                result = validate_manifest(data)
                self.assertFalse(result["success"])
                assert_error_field(self, result, "tools[0].effect")

    def test_missing_tool_fields(self):
        for key in ("name", "description", "parameters", "effect"):
            with self.subTest(key=key):
                data = valid_data()
                del data["tools"][0][key]
                result = validate_manifest(data)
                self.assertFalse(result["success"])
                assert_error_field(self, result, f"tools[0].{key}")

    def test_duplicate_tool_names_are_rejected(self):
        data = valid_data()
        second = copy.deepcopy(data["tools"][0])
        data["tools"].append(second)
        result = validate_manifest(data)

        self.assertFalse(result["success"])
        assert_error_field(self, result, "tools[1].name")
        self.assertTrue(
            any("unique" in item["message"] for item in result["errors"])
        )

    def test_reserved_core_tool_names_are_rejected(self):
        result = validate_manifest(
            valid_data(),
            core_tool_names={"example_run", "list_files"},
        )
        self.assertFalse(result["success"])
        assert_error_field(self, result, "tools[0].name")
        self.assertTrue(
            any("core" in item["message"] for item in result["errors"])
        )

    def test_core_tool_names_accepts_any_bounded_iterable(self):
        result = validate_manifest(
            valid_data(),
            core_tool_names=("example_run",),
        )
        self.assertFalse(result["success"])
        # A string is not treated as an iterable of names.
        result = validate_manifest(valid_data(), core_tool_names="example_run")
        self.assertTrue(result["success"], result)

    def test_unknown_tool_field_is_rejected(self):
        data = valid_data()
        data["tools"][0]["shell"] = "rm -rf /"
        result = validate_manifest(data)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "tools[0].shell")

    def test_too_many_tools_are_rejected(self):
        data = valid_data()
        data["tools"] = []
        for index in range(51):
            tool = copy.deepcopy(valid_data()["tools"][0])
            tool["name"] = f"tool_{index}"
            data["tools"].append(tool)
        result = validate_manifest(data)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "tools")
        self.assertTrue(any("at most" in item["message"] for item in result["errors"]))

    def test_invalid_parameters_root(self):
        for value in ([], "object", 1, None):
            with self.subTest(value=value):
                data = valid_data()
                data["tools"][0]["parameters"] = value
                result = validate_manifest(data)
                self.assertFalse(result["success"])
                assert_error_field(self, result, "tools[0].parameters")

    def test_unknown_manifest_field_is_rejected(self):
        data = valid_data()
        data["shell"] = "echo"
        result = validate_manifest(data)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "shell")
        self.assertTrue(
            any("unknown" in item["message"] for item in result["errors"])
        )

    def test_invalid_requires(self):
        cases = [
            (["core.base"], "requires"),
            ({"Bad": ">=1.0.0"}, "requires.Bad"),
            ({"core.base": ">=1.0.0; rm -rf /"}, "requires.core.base"),
            ({"core.base": ""}, "requires.core.base"),
            ({"core.base": "x" * 65}, "requires.core.base"),
            ({"core.base": 3}, "requires.core.base"),
        ]
        for value, field in cases:
            with self.subTest(value=value):
                data = valid_data()
                data["requires"] = value
                result = validate_manifest(data)
                self.assertFalse(result["success"])
                assert_error_field(self, result, field)

    def test_too_many_requires_are_rejected(self):
        data = valid_data()
        data["requires"] = {
            f"dep{index}": ">=1.0.0" for index in range(21)
        }
        result = validate_manifest(data)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "requires")

    def test_errors_are_sorted_by_field(self):
        data = valid_data()
        data["id"] = "BAD"
        data["version"] = "1.0"
        data["name"] = ""
        result = validate_manifest(data)

        fields = [item["field"] for item in result["errors"]]
        self.assertEqual(fields, sorted(fields))
        self.assertLessEqual(len(fields), MAX_ERRORS)

    def test_errors_are_bounded(self):
        data = valid_data()
        data["id"] = "BAD"
        data["tools"] = []
        for index in range(50):
            tool = copy.deepcopy(valid_data()["tools"][0])
            tool["name"] = f"tool_{index}"
            tool["effect"] = "bad_effect"
            data["tools"].append(tool)
        result = validate_manifest(data)

        self.assertFalse(result["success"])
        self.assertEqual(len(result["errors"]), MAX_ERRORS)
        self.assertIn("Showing the first", result["message"])
        assert_json_safe(self, result)


class EntrypointValidationTests(unittest.TestCase):
    def test_entrypoint_traversal_and_absolute_paths_are_rejected(self):
        cases = [
            "/etc/passwd",
            "../outside.py",
            "sub/../../outside.py",
            "sub/..",
            "a\\b.py",
            "bad\x00name.py",
            "./plugin.py",
            "a//b.py",
            "a/./b.py",
            "~/plugin.py",
            "sub/",
            "C:/plugin.py",
        ]
        for value in cases:
            with self.subTest(value=value):
                data = valid_data()
                data["entrypoint"] = value
                result = validate_manifest(data)
                self.assertFalse(result["success"])
                assert_error_field(self, result, "entrypoint")

    def test_relative_entrypoint_is_accepted(self):
        data = valid_data()
        data["entrypoint"] = "sub/run.py"
        result = validate_manifest(data)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["manifest"]["entrypoint"], "sub/run.py")

    def test_empty_entrypoint_is_rejected_when_present(self):
        data = valid_data()
        data["entrypoint"] = ""
        result = validate_manifest(data)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "entrypoint")


class FileLoadingTests(TempDirTestCase):
    def test_missing_file_fails_without_leaking_path(self):
        result = load_manifest(self.tmp / "missing.json")
        self.assertFalse(result["success"])
        assert_error_field(self, result, "path")
        serialized = json.dumps(result)
        self.assertNotIn(str(self.tmp), serialized)
        self.assertNotIn("Traceback", serialized)

    def test_directory_path_is_rejected(self):
        directory = self.tmp / "folder.json"
        directory.mkdir()
        result = load_manifest(directory)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "path")

    def test_unsupported_extension_is_rejected(self):
        path = write_json(self.tmp, valid_data(), "plugin.yaml")
        result = load_manifest(path)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "path")

    def test_malformed_json_is_rejected(self):
        path = write_text(self.tmp, '{"id": "broken",')
        result = load_manifest(path)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "$")
        self.assertIn("malformed", result["message"])

    def test_malformed_toml_is_rejected(self):
        path = write_text(self.tmp, "id = ", "plugin.toml")
        result = load_manifest(path)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "$")
        self.assertIn("malformed", result["message"])

    def test_wrong_top_level_json_type_is_rejected(self):
        path = write_text(self.tmp, "[]")
        result = load_manifest(path)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "$")
        self.assertIn("JSON object", result["errors"][0]["message"])

    def test_duplicate_json_keys_are_rejected(self):
        text = (
            '{"id":"one","id":"two","name":"N","version":"1.0.0"}'
        )
        path = write_text(self.tmp, text)
        result = load_manifest(path)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "$")
        self.assertIn("duplicate", result["message"])

    def test_json_nan_literal_is_rejected(self):
        text = (
            '{"id":"one","name":"N","version":"1.0.0","tools":['
            '{"name":"t","description":"d","effect":"read_only",'
            '"parameters":{"value":NaN}}]}'
        )
        path = write_text(self.tmp, text)
        result = load_manifest(path)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "$")
        self.assertIn("NaN", result["message"])

    def test_toml_nan_is_rejected(self):
        text = VALID_TOML.replace(
            "{ type = \"string\" }",
            "{ type = \"number\", minimum = nan }",
        )
        path = write_text(self.tmp, text, "plugin.toml")
        result = load_manifest(path)
        self.assertFalse(result["success"])
        self.assertTrue(
            any("NaN" in item["message"] for item in result["errors"]),
            result,
        )

    def test_invalid_utf8_is_rejected(self):
        path = self.tmp / "plugin.json"
        path.write_bytes(b'{"id":"\xff","name":"N","version":"1.0.0"}')
        result = load_manifest(path)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "$")
        self.assertIn("UTF-8", result["message"])

    def test_file_size_limit_is_enforced(self):
        data = valid_data()
        data["padding"] = "x" * 2_000
        path = write_json(self.tmp, data)
        result = load_manifest(path, max_bytes=100)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "path")
        self.assertIn("size limit", result["message"])

    def test_default_file_size_limit_is_enforced(self):
        path = self.tmp / "plugin.json"
        path.write_bytes(b" " * (MAX_MANIFEST_BYTES + 1))
        result = load_manifest(path)
        self.assertFalse(result["success"])
        self.assertIn("size limit", result["message"])

    def test_invalid_max_bytes_is_rejected(self):
        path = write_json(self.tmp, valid_data())
        for value in (0, -1, "100", True, None):
            with self.subTest(value=value):
                result = load_manifest(path, max_bytes=value)
                self.assertFalse(result["success"])
                assert_error_field(self, result, "max_bytes")

    def test_symlink_manifest_is_rejected(self):
        target = write_json(self.tmp, valid_data(), "outside.json")
        link = self.tmp / "plugin.json"
        try:
            os.symlink(target, link)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        result = load_manifest(link)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "path")
        self.assertIn("symbolic link", result["message"])

    def test_regular_entrypoint_inside_plugin_directory_is_accepted(self):
        plugin_dir = self.tmp / "plugin"
        plugin_dir.mkdir()
        write_json(plugin_dir, valid_data())
        (plugin_dir / "plugin.py").write_text("# inert\n", encoding="utf-8")
        result = load_manifest(plugin_dir / "plugin.json")
        self.assertTrue(result["success"], result)

    def test_missing_entrypoint_is_allowed_at_validation_time(self):
        plugin_dir = self.tmp / "plugin"
        plugin_dir.mkdir()
        write_json(plugin_dir, valid_data())
        result = load_manifest(plugin_dir / "plugin.json")
        self.assertTrue(result["success"], result)

    def test_entrypoint_directory_is_rejected(self):
        plugin_dir = self.tmp / "plugin"
        plugin_dir.mkdir()
        write_json(plugin_dir, valid_data())
        (plugin_dir / "plugin.py").mkdir()
        result = load_manifest(plugin_dir / "plugin.json")
        self.assertFalse(result["success"])
        assert_error_field(self, result, "entrypoint")
        self.assertIn("regular file", result["errors"][0]["message"])

    def test_entrypoint_symlink_escape_is_rejected(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        secret = outside / "secret.py"
        secret.write_text("# outside\n", encoding="utf-8")
        plugin_dir = self.tmp / "plugin"
        plugin_dir.mkdir()
        write_json(plugin_dir, valid_data())
        try:
            os.symlink(secret, plugin_dir / "plugin.py")
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")

        result = load_manifest(plugin_dir / "plugin.json")
        self.assertFalse(result["success"])
        assert_error_field(self, result, "entrypoint")
        self.assertIn("symbolic link", result["errors"][0]["message"])

    def test_entrypoint_symlinked_parent_directory_is_rejected(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "run.py").write_text("# outside\n", encoding="utf-8")
        plugin_dir = self.tmp / "plugin"
        plugin_dir.mkdir()
        data = valid_data()
        data["entrypoint"] = "bin/run.py"
        write_json(plugin_dir, data)
        try:
            os.symlink(outside, plugin_dir / "bin")
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")

        result = load_manifest(plugin_dir / "plugin.json")
        self.assertFalse(result["success"])
        assert_error_field(self, result, "entrypoint")

    def test_entrypoint_symlink_inside_plugin_directory_is_still_rejected(self):
        plugin_dir = self.tmp / "plugin"
        plugin_dir.mkdir()
        write_json(plugin_dir, valid_data())
        real = plugin_dir / "real.py"
        real.write_text("# inert\n", encoding="utf-8")
        try:
            os.symlink(real.name, plugin_dir / "plugin.py")
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")

        result = load_manifest(plugin_dir / "plugin.json")
        self.assertFalse(result["success"])
        assert_error_field(self, result, "entrypoint")


class InMemorySafetyTests(unittest.TestCase):
    def _parameters(self, value):
        data = valid_data()
        data["tools"][0]["parameters"] = value
        return data

    def test_wrong_top_level_types_are_rejected(self):
        for value in ([], "manifest", 1, None, True, (("id", "x"),)):
            with self.subTest(value=value):
                result = validate_manifest(value)
                self.assertFalse(result["success"])
                assert_error_field(self, result, "$")
                assert_json_safe(self, result)

    def test_cycle_is_rejected_without_recursing_forever(self):
        parameters = {"type": "object"}
        parameters["self"] = parameters
        result = validate_manifest(self._parameters(parameters))

        self.assertFalse(result["success"])
        assert_error_field(self, result, "$")
        self.assertIn("cycle", result["errors"][0]["message"])
        assert_json_safe(self, result)

    def test_deep_nesting_is_rejected(self):
        parameters = {"type": "object"}
        cursor = parameters
        for _ in range(20):
            child = {}
            cursor["properties"] = {"child": child}
            cursor = child
        result = validate_manifest(self._parameters(parameters))

        self.assertFalse(result["success"])
        self.assertTrue(
            any("nested" in item["message"] for item in result["errors"]),
            result,
        )

    def test_nan_and_infinity_are_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                result = validate_manifest(
                    self._parameters({"value": value})
                )
                self.assertFalse(result["success"])
                assert_error_field(
                    self,
                    result,
                    "tools[0].parameters.value",
                )
                self.assertIn("NaN", result["errors"][0]["message"])
                assert_json_safe(self, result)

    def test_oversized_schema_string_is_rejected(self):
        result = validate_manifest(
            self._parameters({"description": "x" * 2_001})
        )
        self.assertFalse(result["success"])
        self.assertTrue(
            any("at most" in item["message"] for item in result["errors"]),
            result,
        )

    def test_non_string_schema_keys_are_rejected(self):
        result = validate_manifest(self._parameters({1: "value"}))
        self.assertFalse(result["success"])
        self.assertTrue(
            any("keys must be strings" in item["message"]
                for item in result["errors"]),
            result,
        )

    def test_huge_integers_are_rejected(self):
        result = validate_manifest(self._parameters({"value": 2**53}))
        self.assertFalse(result["success"])
        assert_error_field(self, result, "tools[0].parameters.value")

    def test_non_json_values_are_rejected(self):
        result = validate_manifest(self._parameters({"value": object()}))
        self.assertFalse(result["success"])
        self.assertIn("JSON-safe", result["errors"][0]["message"])
        assert_json_safe(self, result)

    def test_hostile_mapping_key_is_not_queried(self):
        class HostileKey:
            def __hash__(self):
                return hash("id")

            def __eq__(self, other):
                raise AssertionError(
                    "validator invoked equality on a hostile mapping key"
                )

        data = valid_data()
        del data["id"]
        data[HostileKey()] = "evil"
        result = validate_manifest(data)

        self.assertFalse(result["success"])
        self.assertTrue(
            any("field names must be strings" in item["message"]
                for item in result["errors"]),
            result,
        )
        assert_error_field(self, result, "id")

    def test_dict_subclass_is_not_traversed(self):
        class SneakyDict(dict):
            def items(self):  # pragma: no cover - must never be called
                raise AssertionError("validator called hostile method")

        data = SneakyDict(valid_data())
        result = validate_manifest(data)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "$")

    def test_parameter_node_budget_fails_closed(self):
        # Wide but structurally shallow: the shared schema-node budget is
        # exhausted, and the tool must not be silently dropped from a
        # "successful" manifest.
        wide = {
            f"k{outer}": {f"s{outer}_{inner}": inner for inner in range(64)}
            for outer in range(64)
        }
        result = validate_manifest(self._parameters(wide))

        self.assertFalse(result["success"])
        self.assertIsNone(result["manifest"])
        self.assertTrue(
            any("size limit" in item["message"] or "safely" in item["message"]
                for item in result["errors"]),
            result,
        )

    def test_too_many_top_level_fields_are_rejected(self):
        data = valid_data()
        for index in range(10):
            data[f"extra_{index}"] = index
        result = validate_manifest(data)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "$")

    def test_deprecated_or_unknown_keys_cannot_hide_tools(self):
        data = valid_data()
        data["toolz"] = data["tools"]
        result = validate_manifest(data)
        self.assertFalse(result["success"])
        assert_error_field(self, result, "toolz")


class DiscoveryTests(TempDirTestCase):
    def _plugins_root(self):
        root = self.tmp / "plugins"
        root.mkdir()
        return root

    def test_discovers_valid_manifests_and_reports_invalid_plugins(self):
        root = self._plugins_root()
        alpha = copy.deepcopy(valid_data())
        alpha["id"] = "alpha.plugin"
        alpha["tools"][0]["name"] = "alpha_run"
        alpha_dir = root / "alpha"
        alpha_dir.mkdir()
        write_json(alpha_dir, alpha)

        beta = copy.deepcopy(valid_data())
        beta["id"] = "beta.plugin"
        beta["tools"][0]["name"] = "beta_run"
        beta_dir = root / "beta"
        beta_dir.mkdir()
        write_text(beta_dir, VALID_TOML.replace(
            "example.plugin", "beta.plugin"
        ).replace("example_run", "beta_run"), "plugin.toml")

        broken_dir = root / "broken"
        broken_dir.mkdir()
        broken = valid_data()
        broken["id"] = "BAD"
        write_json(broken_dir, broken)

        ambiguous_dir = root / "ambiguous"
        ambiguous_dir.mkdir()
        write_json(ambiguous_dir, valid_data())
        write_text(ambiguous_dir, VALID_TOML, "plugin.toml")

        empty_dir = root / "not_a_plugin"
        empty_dir.mkdir()
        (empty_dir / "readme.txt").write_text("nothing", encoding="utf-8")
        (root / "loose_file.txt").write_text("nothing", encoding="utf-8")

        result = discover_manifests(root)

        self.assertTrue(result["success"], result)
        self.assertEqual(
            [item["id"] for item in result["manifests"]],
            ["alpha.plugin", "beta.plugin"],
        )
        self.assertEqual(
            [item["plugin"] for item in result["errors"]],
            ["ambiguous", "broken"],
        )
        self.assertEqual(result["scanned"], 4)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["skipped_symlinks"], 0)
        for entry in result["errors"]:
            self.assertIn("errors", entry)
            self.assertIn("message", entry)
            self.assertIn("plugin", entry)
        assert_json_safe(self, result)

    def test_invalid_manifest_errors_are_grouped_per_plugin(self):
        root = self._plugins_root()
        directory = root / "broken"
        directory.mkdir()
        data = valid_data()
        data["id"] = "BAD"
        data["version"] = "1"
        write_json(directory, data)

        result = discover_manifests(root)
        self.assertTrue(result["success"])
        self.assertEqual(result["manifests"], [])
        self.assertEqual(len(result["errors"]), 1)
        entry = result["errors"][0]
        self.assertEqual(entry["plugin"], "broken")
        self.assertEqual(
            [item["field"] for item in entry["errors"]],
            ["id", "version"],
        )

    def test_discovery_skips_symlinked_directories(self):
        root = self._plugins_root()
        outside = self.tmp / "outside_plugin"
        outside.mkdir()
        data = valid_data()
        data["id"] = "linked.plugin"
        data["tools"][0]["name"] = "linked_run"
        write_json(outside, data)
        try:
            os.symlink(outside, root / "linked")
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")

        result = discover_manifests(root)
        self.assertTrue(result["success"])
        self.assertEqual(result["manifests"], [])
        self.assertEqual(result["errors"], [])
        self.assertGreaterEqual(result["skipped_symlinks"], 1)

    def test_discovery_skips_symlinked_manifest_files(self):
        root = self._plugins_root()
        outside = self.tmp / "outside.json"
        data = valid_data()
        data["id"] = "outside.plugin"
        write_json(self.tmp, data, "outside.json")
        directory = root / "linked_manifest"
        directory.mkdir()
        try:
            os.symlink(outside, directory / "plugin.json")
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")

        result = discover_manifests(root)
        self.assertTrue(result["success"])
        self.assertEqual(result["manifests"], [])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["scanned"], 0)
        self.assertGreaterEqual(result["skipped_symlinks"], 1)

    def test_discovery_respects_max_plugins(self):
        root = self._plugins_root()
        for index in range(5):
            data = valid_data()
            data["id"] = f"plugin{index}.plugin"
            data["tools"][0]["name"] = f"tool_{index}"
            directory = root / f"p{index}"
            directory.mkdir()
            write_json(directory, data)

        result = discover_manifests(root, max_plugins=2)
        self.assertTrue(result["success"])
        self.assertEqual(
            [item["id"] for item in result["manifests"]],
            ["plugin0.plugin", "plugin1.plugin"],
        )
        self.assertEqual(result["scanned"], 2)
        self.assertTrue(result["truncated"])
        assert_json_safe(self, result)

        full = discover_manifests(root, max_plugins=10)
        self.assertTrue(full["success"])
        self.assertEqual(len(full["manifests"]), 5)
        self.assertFalse(full["truncated"])

    def test_invalid_max_plugins_is_rejected(self):
        root = self._plugins_root()
        for value in (0, -1, "2", True, None):
            with self.subTest(value=value):
                result = discover_manifests(root, max_plugins=value)
                self.assertFalse(result["success"])
                self.assertEqual(result["manifests"], [])
                self.assertIn("max_plugins", result["message"])

    def test_missing_or_invalid_directory_is_rejected(self):
        result = discover_manifests(self.tmp / "missing")
        self.assertFalse(result["success"])
        self.assertEqual(result["manifests"], [])

        file_path = self.tmp / "file.txt"
        file_path.write_text("x", encoding="utf-8")
        result = discover_manifests(file_path)
        self.assertFalse(result["success"])
        self.assertEqual(result["manifests"], [])

    def test_symlinked_root_directory_is_rejected(self):
        real = self._plugins_root()
        link = self.tmp / "link"
        try:
            os.symlink(real, link)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        result = discover_manifests(link)
        self.assertFalse(result["success"])
        self.assertIn("symbolic link", result["message"])

    def test_discovery_accepts_core_tool_names(self):
        root = self._plugins_root()
        data = valid_data()
        data["tools"][0]["name"] = "list_files"
        directory = root / "reserved"
        directory.mkdir()
        write_json(directory, data)

        result = discover_manifests(root, core_tool_names={"list_files"})
        self.assertTrue(result["success"])
        self.assertEqual(result["manifests"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["plugin"], "reserved")

    def test_discovery_does_not_import_plugin_files(self):
        # An entrypoint containing code that would fail loudly if imported.
        root = self._plugins_root()
        directory = root / "no_import"
        directory.mkdir()
        write_json(directory, valid_data())
        (directory / "plugin.py").write_text(
            "raise RuntimeError('must never be imported')\n",
            encoding="utf-8",
        )
        result = discover_manifests(root)
        self.assertTrue(result["success"])
        self.assertEqual(len(result["manifests"]), 1)


class SerializationTests(TempDirTestCase):
    def test_every_result_shape_is_json_safe(self):
        success = load_manifest(write_json(self.tmp, valid_data()))
        invalid = validate_manifest({"id": "BAD"})
        load_failure = load_manifest(self.tmp / "missing.json")

        root = self.tmp / "plugins"
        root.mkdir()
        plugin = root / "valid"
        plugin.mkdir()
        write_json(plugin, valid_data())
        discovery = discover_manifests(root)

        for result in (success, invalid, load_failure, discovery):
            with self.subTest(result=result):
                assert_json_safe(self, result)

        for result in (success, invalid, load_failure):
            self.assertEqual(
                set(result),
                {"success", "manifest", "errors", "message"},
            )
        self.assertEqual(
            set(discovery),
            {
                "success",
                "manifests",
                "errors",
                "scanned",
                "skipped_symlinks",
                "truncated",
                "message",
            },
        )

    def test_manifest_payload_round_trips_through_json(self):
        result = validate_manifest(valid_data())
        self.assertTrue(result["success"], result)
        encoded = json.dumps(result, allow_nan=False)
        decoded = json.loads(encoded)
        self.assertEqual(decoded, result)


if __name__ == "__main__":
    unittest.main()
