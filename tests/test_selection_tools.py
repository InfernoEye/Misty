"""Tests for the selection tools: read, preview and replace.

No live desktop, no clipboard writes and no ydotool: AT-SPI is replaced by
fake nodes, the clipboard by a recording fake and the input backend by an
object that records the paste chord.  The registry path is exercised end to
end (schema -> policy -> dispatch) so the inventory contract holds.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain import agent as agent_module
from app.brain import policy as policy_module
from app.brain import tools as tools_module
from app.brain.memory import MemoryStore
from app.brain.tools import (
    SELECTION_TOOL_DEFINITIONS,
    SelectionReplacementService,
    ToolRegistry,
    selection_diff_summary,
)
from app.desktop.accessibility.editable import AccessibilityEditableWriter
from app.desktop.accessibility.selection import SelectionReader
from app.desktop.input import InputAction
from tests.test_editable_writer import _Desktop, _Node

SECRET = "my private draft about Zxqv"
REPLACEMENT = "Zxqv rewritten"


# ---------------------------------------------------------------------------
# Fakes


class FakeClipboard:
    """Records every read/write; can fail specific writes."""

    def __init__(self, initial="clipboard-original", fail_writes=()):
        self.text = initial
        self.fail_writes = set(fail_writes)
        self.calls = []

    def read_text(self):
        self.calls.append(("read", None))
        return {"success": True, "text": self.text, "length": len(self.text)}

    def write_text(self, text):
        self.calls.append(("write", text))
        if "all" in self.fail_writes:
            return {"success": False, "message": "failed"}
        if text == REPLACEMENT and "replacement" in self.fail_writes:
            return {"success": False, "message": "failed"}
        self.text = text
        return {"success": True, "length": len(text)}

    def writes(self):
        return [value for kind, value in self.calls if kind == "write"]


class FakeInputBackend:
    """Records canonical actions; produces a fixed result."""

    def __init__(self, success=True):
        self.actions = []
        self.success = success

    def execute(self, action):
        self.actions.append(action)
        return {"success": self.success, "verified": None}


def writer_for(desktop):
    return AccessibilityEditableWriter(
        reader=SelectionReader(desktop_provider=lambda: desktop)
    )


def service(desktop=None, clipboard=None, input_backend=None, editable=True):
    writer = None
    if desktop is not False:
        writer = writer_for(desktop or _Desktop([_Node(
            contents="hello world",
            selection=(0, 5),
            editable=True if editable else None,
        )]))
    return SelectionReplacementService(
        editable_writer=writer,
        clipboard_service=clipboard,
        input_backend=input_backend,
    )


def registry(**overrides):
    directory = tempfile.TemporaryDirectory()
    options = dict(
        memory_store=MemoryStore(Path(directory.name) / "memory.sqlite3"),
    )
    options.update(overrides)
    instance = ToolRegistry(**options)
    _DIRECTORIES.append(directory)
    return instance


# Temp dirs live until the interpreter exits so every registry built here
# keeps a valid (read-only, unused) memory path for its whole lifetime.
_DIRECTORIES = []


def tearDownModule():
    for directory in _DIRECTORIES:
        directory.cleanup()
    _DIRECTORIES.clear()


# ---------------------------------------------------------------------------
# Schemas and policy


class SchemaTests(unittest.TestCase):
    def definitions(self):
        return {
            item["function"]["name"]: item["function"]
            for item in SELECTION_TOOL_DEFINITIONS
        }

    def test_three_tools_are_defined(self):
        self.assertEqual(
            set(self.definitions()),
            {"read_selection", "preview_selection_replacement",
             "replace_selection"},
        )

    def test_read_selection_schema(self):
        function = self.definitions()["read_selection"]
        parameters = function["parameters"]
        self.assertNotIn("required", parameters)
        self.assertIs(parameters["additionalProperties"], False)
        max_chars = parameters["properties"]["max_chars"]
        self.assertEqual(max_chars["type"], "integer")
        self.assertEqual(max_chars["minimum"], 1)
        self.assertEqual(max_chars["maximum"], 100000)
        self.assertEqual(
            parameters["properties"]["allow_clipboard"]["type"], "boolean"
        )

    def test_replacement_schemas(self):
        preview = self.definitions()["preview_selection_replacement"]
        self.assertEqual(preview["parameters"]["required"], ["replacement"])
        self.assertEqual(
            preview["parameters"]["properties"]["replacement"]["maxLength"],
            20000,
        )
        replace = self.definitions()["replace_selection"]
        self.assertEqual(replace["parameters"]["required"], ["replacement"])
        self.assertEqual(
            replace["parameters"]["properties"]["expected_text"]["maxLength"],
            100000,
        )
        self.assertIs(
            replace["parameters"]["additionalProperties"], False
        )

    def test_preview_is_recommended_first(self):
        replace = self.definitions()["replace_selection"]
        self.assertIn(
            "preview_selection_replacement", replace["description"]
        )
        preview = self.definitions()["preview_selection_replacement"]
        self.assertIn("Recommended first", preview["description"])

    def test_registry_advertises_the_selection_tools(self):
        names = {
            item["function"]["name"] for item in registry().available_tools()
        }
        self.assertTrue(
            {"read_selection", "preview_selection_replacement",
             "replace_selection"} <= names
        )


class PolicyTests(unittest.TestCase):
    def test_read_selection_policy(self):
        spec = policy_module.policy_for("read_selection")
        self.assertEqual(spec.effect, "read_only")
        self.assertEqual(spec.risk, "medium")
        self.assertTrue(spec.private)
        self.assertFalse(spec.requires_confirmation)
        self.assertFalse(spec.arguments_affect_risk)

    def test_preview_policy(self):
        spec = policy_module.policy_for("preview_selection_replacement")
        self.assertEqual(spec.effect, "read_only")
        self.assertTrue(spec.private)
        self.assertFalse(spec.requires_confirmation)

    def test_replace_policy(self):
        spec = policy_module.policy_for("replace_selection")
        self.assertEqual(spec.effect, "local_write")
        self.assertEqual(spec.risk, "high")
        self.assertTrue(spec.requires_confirmation)
        self.assertFalse(spec.arguments_affect_risk)
        self.assertEqual(spec.risk, "high")  # confirmation is mandatory

    def test_confirmation_text_is_content_free(self):
        text = policy_module.describe_for_confirmation("replace_selection")
        self.assertNotIn(SECRET, text)
        self.assertNotIn(REPLACEMENT, text)
        self.assertEqual(text, text.strip())
        self.assertNotIn("{", text)

    def test_private_result_tools(self):
        for name in (
            "read_selection", "preview_selection_replacement",
            "replace_selection",
        ):
            with self.subTest(tool=name):
                self.assertIn(name, agent_module.PRIVATE_RESULT_TOOLS)

    def test_prompt_mentions_the_selection_tools(self):
        prompt = agent_module.SYSTEM_PROMPT
        self.assertIn("read_selection", prompt)
        self.assertIn("preview_selection_replacement", prompt)


# ---------------------------------------------------------------------------
# read_selection dispatch


class ReadSelectionDispatchTests(unittest.TestCase):
    def test_reads_accessibility_first_and_returns_the_text(self):
        node = _Node(contents="alpha beta gamma", selection=(6, 10))
        instance = registry(
            selection_reader=SelectionReader(
                desktop_provider=lambda: _Desktop([node])
            )
        )
        result = instance.execute("read_selection", {})
        self.assertTrue(result["success"], result)
        self.assertEqual(result["text"], "beta")
        self.assertEqual(result["source"], "accessibility")
        self.assertEqual(result["length"], 4)
        self.assertFalse(result["truncated"])

    def test_clipboard_is_not_consulted_by_default(self):
        clipboard = FakeClipboard("clip-text")
        instance = registry(
            clipboard_service=clipboard,
            selection_reader=SelectionReader(
                desktop_provider=lambda: _Desktop([_Node(selection=None)])
            ),
        )
        result = instance.execute("read_selection", {})
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "no_selection")
        self.assertNotIn("clip-text", json.dumps(result))
        self.assertEqual(clipboard.calls, [])

    def test_clipboard_is_used_only_when_allowed(self):
        # Built by the registry itself: its reader is wired to the injected
        # clipboard service, and only an explicit allow_clipboard reaches it.
        clipboard = FakeClipboard("clip-text")
        instance = registry(clipboard_service=clipboard)
        instance.selection_reader = SelectionReader(
            desktop_provider=lambda: _Desktop([_Node(selection=None)]),
            clipboard_reader=instance.selection_replacement_service
            .clipboard_reader,
        )
        result = instance.execute(
            "read_selection", {"allow_clipboard": True}
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["source"], "clipboard")
        self.assertEqual(result["text"], "clip-text")

    def test_truncation_is_reported(self):
        node = _Node(contents="x" * 100, selection=(0, 100))
        instance = registry(
            selection_reader=SelectionReader(
                desktop_provider=lambda: _Desktop([node])
            )
        )
        result = instance.execute("read_selection", {"max_chars": 10})
        self.assertEqual(len(result["text"]), 10)
        self.assertTrue(result["truncated"])

    def test_invalid_arguments(self):
        instance = registry()
        for arguments in (
            {"max_chars": 0},
            {"max_chars": True},
            {"max_chars": 100001},
            {"allow_clipboard": "yes"},
            {"max_items": 3},
        ):
            with self.subTest(arguments=arguments):
                result = instance.execute("read_selection", arguments)
                self.assertFalse(result["success"])

    def test_accessibility_unavailable_degrades_cleanly(self):
        def broken():
            raise RuntimeError("no bus")

        instance = registry(
            selection_reader=SelectionReader(desktop_provider=broken)
        )
        result = instance.execute("read_selection", {})
        self.assertEqual(result["code"], "accessibility_unavailable")

    def test_reader_failure_codes_survive_dispatch(self):
        from app.desktop.accessibility.selection import (
            SelectionReadError,
        )

        class Exploding:
            def read_selection(self, **_kwargs):
                raise SelectionReadError("empty_selection", "empty")

        instance = registry(selection_reader=Exploding())
        result = instance.execute("read_selection", {})
        self.assertEqual(result["code"], "empty_selection")

    def test_unexpected_reader_error_is_contained(self):
        class Exploding:
            def read_selection(self, **_kwargs):
                raise ValueError("boom")

        result = registry(selection_reader=Exploding()).execute(
            "read_selection", {}
        )
        self.assertEqual(result["code"], "accessibility_unavailable")
        self.assertNotIn("boom", json.dumps(result))


# ---------------------------------------------------------------------------
# preview


class PreviewTests(unittest.TestCase):
    def test_preview_lists_changed_lines_and_counts(self):
        selected = "first\nsecond\nthird"
        replacement = "first\nSECOND\nthird"
        desktop = _Desktop([_Node(
            contents=selected, selection=(0, len(selected))
        )])
        instance = registry(selection_replacement_service=service(desktop))
        result = instance.execute(
            "preview_selection_replacement",
            {"replacement": replacement},
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["selection"]["text"], selected)
        diff = result["diff"]
        self.assertEqual(diff["total_changed"], 2)
        self.assertFalse(diff["truncated"])
        self.assertEqual(
            diff["lines"],
            [
                {"op": "-", "line": "second", "clipped": False},
                {"op": "+", "line": "SECOND", "clipped": False},
            ],
        )
        self.assertEqual(diff["chars_delta"], 0)
        self.assertEqual(diff["replacement_chars"], len(replacement))
        self.assertEqual(diff["selected_chars"], len(selected))

    def test_preview_is_bounded_to_twenty_lines(self):
        summary = selection_diff_summary(
            "a" * 0 or "old", "new"
        )
        self.assertEqual(summary["total_changed"], 2)
        long_summary = selection_diff_summary(
            "x", "\n".join(str(n) for n in range(100))
        )
        self.assertTrue(long_summary["truncated"])
        self.assertEqual(len(long_summary["lines"]), 20)
        self.assertGreater(long_summary["total_changed"], 20)

    def test_preview_clips_long_lines(self):
        summary = selection_diff_summary("short", "y" * 500)
        added = [line for line in summary["lines"] if line["op"] == "+"][0]
        self.assertEqual(len(added["line"]), tools_module.MAX_DIFF_LINE_CHARS)
        self.assertTrue(added["clipped"])

    def test_preview_never_writes(self):
        node = _Node(contents="hello world", selection=(0, 5))
        clipboard = FakeClipboard()
        backend = FakeInputBackend()
        instance = registry(
            clipboard_service=clipboard,
            selection_replacement_service=service(
                _Desktop([node]), clipboard=clipboard, input_backend=backend
            ),
        )
        result = instance.execute(
            "preview_selection_replacement", {"replacement": "bye"}
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["code"], "previewed")
        self.assertEqual(node.contents, "hello world")
        self.assertEqual(node.text.calls, [])
        self.assertEqual(clipboard.calls, [])
        self.assertEqual(backend.actions, [])

    def test_preview_reports_an_uneditable_selection(self):
        desktop = _Desktop([_Node(contents="read only", selection=(0, 4),
                                  editable=None)])
        result = registry(
            selection_replacement_service=service(desktop)
        ).execute("preview_selection_replacement", {"replacement": "x"})
        self.assertTrue(result["success"], result)
        self.assertFalse(result["editable"])

    def test_preview_validation(self):
        instance = registry(selection_replacement_service=service())
        for arguments in (
            {},
            {"replacement": ""},
            {"replacement": "x" * 20001},
            {"replacement": "ok", "extra": 1},
            {"replacement": "bad\x00"},
            {"replacement": "bad\x1b"},
        ):
            with self.subTest(arguments=str(arguments)[:40]):
                result = instance.execute(
                    "preview_selection_replacement", arguments
                )
                self.assertFalse(result["success"])
                self.assertNotIn("bad", json.dumps(result.get("lines", [])))

    def test_preview_failure_without_accessibility(self):
        result = registry(
            selection_replacement_service=service(desktop=False)
        ).execute("preview_selection_replacement", {"replacement": "x"})
        self.assertFalse(result["success"])


# ---------------------------------------------------------------------------
# replace_selection: confirmation and routing


class ReplaceSelectionTests(unittest.TestCase):
    def test_requires_confirmation(self):
        node = _Node()
        instance = registry(
            selection_replacement_service=service(_Desktop([node]))
        )
        result = instance.execute(
            "replace_selection", {"replacement": "bye"}
        )
        self.assertFalse(result["success"])
        self.assertIn("approval", result["message"])
        self.assertEqual(node.contents, "hello world")
        self.assertEqual(node.text.calls, [])

    def test_accessibility_route_replaces_without_touching_the_clipboard(self):
        node = _Node(contents="hello world", selection=(0, 5))
        clipboard = FakeClipboard()
        backend = FakeInputBackend()
        instance = registry(
            clipboard_service=clipboard,
            selection_replacement_service=service(
                _Desktop([node]), clipboard=clipboard, input_backend=backend
            ),
        )
        result = instance.execute(
            "replace_selection", {"replacement": "bye"}, confirmed=True
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["route"], "accessibility")
        self.assertEqual(node.contents, "bye world")
        self.assertEqual(result["length"], 3)
        self.assertEqual(result["replaced_length"], 5)
        self.assertEqual(clipboard.calls, [])
        self.assertEqual(backend.actions, [])
        # Only lengths may be returned, never either text.
        self.assertNotIn("bye", json.dumps(result))
        self.assertNotIn("hello", json.dumps(result))

    def test_stale_selection_is_refused(self):
        node = _Node(contents="hello world", selection=(0, 5))
        instance = registry(
            selection_replacement_service=service(_Desktop([node]))
        )
        result = instance.execute(
            "replace_selection",
            {"replacement": "bye", "expected_text": "different"},
            confirmed=True,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "selection_changed")
        self.assertEqual(node.contents, "hello world")
        self.assertEqual(node.text.calls, [])

    def test_matching_expected_text_writes(self):
        node = _Node(contents="hello  world", selection=(0, 14))
        instance = registry(
            selection_replacement_service=service(_Desktop([node]))
        )
        result = instance.execute(
            "replace_selection",
            {"replacement": "x", "expected_text": "hello world"},
            confirmed=True,
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(node.contents, "x")

    def test_invalid_arguments_and_text(self):
        node = _Node()
        instance = registry(
            selection_replacement_service=service(_Desktop([node]))
        )
        for arguments in (
            {},
            {"replacement": ""},
            {"replacement": "x" * 20001},
            {"replacement": "ok", "unexpected": True},
            {"replacement": "ok", "expected_text": 5},
            {"replacement": "ok", "expected_text": "x" * 100001},
            {"replacement": "a\u0000b"},
            {"replacement": "a\rb"},
        ):
            with self.subTest(arguments=str(arguments)[:40]):
                result = instance.execute(
                    "replace_selection", arguments, confirmed=True
                )
                self.assertFalse(result["success"])
        self.assertEqual(node.text.calls, [])

    def test_confirmation_description_counts_only(self):
        instance = registry()
        text = instance.confirmation_description(
            "replace_selection",
            {"replacement": REPLACEMENT, "expected_text": SECRET},
        )
        self.assertIn(str(len(REPLACEMENT)), text)
        self.assertIn(str(len(SECRET)), text)
        for value in (REPLACEMENT, SECRET):
            self.assertNotIn(value, text)
        for fragment in ("Zxqv", "draft", str(len(SECRET))):
            if fragment != str(len(SECRET)):
                self.assertNotIn(fragment, text)


# ---------------------------------------------------------------------------
# Clipboard route ordering with fake collaborators


class ClipboardRouteTests(unittest.TestCase):
    def make(self, node=None, clipboard=None, backend=None):
        node = node or _Node(contents="hello world", selection=(0, 5),
                             editable=None)
        clipboard = clipboard or FakeClipboard()
        backend = backend or FakeInputBackend()
        return (
            service(_Desktop([node]), clipboard=clipboard,
                    input_backend=backend),
            clipboard,
            backend,
        )

    def test_clipboard_route_order_and_restore(self):
        replacer, clipboard, backend = self.make()
        result = replacer.replace("bye", None, confirmed=True)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["route"], "clipboard")
        self.assertTrue(result["clipboard_restored"])
        self.assertEqual(clipboard.writes(), ["bye", "clipboard-original"])
        self.assertEqual(clipboard.text, "clipboard-original")
        self.assertEqual(len(backend.actions), 1)
        action = backend.actions[0]
        self.assertIsInstance(action, InputAction)
        self.assertEqual(action.kind, "press_key")
        self.assertEqual(dict(action.fields), {"combo": "ctrl+v"})
        # The replacement never travels through the paste chord or argv.
        self.assertNotIn("bye", json.dumps(action.to_dict()))

    def test_text_is_never_in_the_paste_action(self):
        backend = FakeInputBackend()
        replacer, clipboard, _ = self.make(backend=backend)
        replacer.replace(REPLACEMENT, None, confirmed=True)
        rendered = repr(backend.actions[0])
        self.assertNotIn(REPLACEMENT, rendered)
        self.assertNotIn("fields={'", rendered)

    def test_restore_happens_even_when_the_paste_fails(self):
        replacer, clipboard, backend = self.make(
            backend=FakeInputBackend(success=False)
        )
        result = replacer.replace("bye", None, confirmed=True)

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "insertion_failed")
        self.assertEqual(result["route"], "clipboard")
        self.assertTrue(result["clipboard_restored"])
        self.assertEqual(clipboard.text, "clipboard-original")
        self.assertEqual(clipboard.writes(), ["bye", "clipboard-original"])

    def test_restore_happens_when_the_backend_raises(self):
        class Exploding(FakeInputBackend):
            def execute(self, action):
                raise RuntimeError("boom")

        replacer, clipboard, _ = self.make(backend=Exploding())
        result = replacer.replace("bye", None, confirmed=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["clipboard_restored"], True)
        self.assertEqual(clipboard.text, "clipboard-original")

    def test_restore_happens_when_the_selection_is_stale(self):
        replacer, clipboard, backend = self.make()
        result = replacer.replace("bye", "other text", confirmed=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "selection_changed")
        self.assertEqual(backend.actions, [])
        # The clipboard was never touched: no save, no replacement, no
        # restore write.
        self.assertEqual(clipboard.calls, [])
        self.assertEqual(clipboard.text, "clipboard-original")

    def test_restore_failure_is_reported_honestly(self):
        clipboard = FakeClipboard()

        original_write = clipboard.write_text

        def flaky(text):
            if text == "clipboard-original":
                return {"success": False}
            return original_write(text)

        clipboard.write_text = flaky
        replacer, _, _ = self.make(clipboard=clipboard)
        result = replacer.replace("bye", None, confirmed=True)
        self.assertFalse(result["clipboard_restored"])

    def test_failed_clipboard_write_still_restores(self):
        clipboard = FakeClipboard(fail_writes={"replacement"})
        replacer, _, backend = self.make(clipboard=clipboard)
        result = replacer.replace(REPLACEMENT, None, confirmed=True)
        self.assertEqual(result["code"], "clipboard_error")
        self.assertEqual(backend.actions, [])
        self.assertTrue(result["clipboard_restored"])
        self.assertEqual(clipboard.text, "clipboard-original")

    def test_no_backend_fails_closed(self):
        node = _Node(editable=None)
        result = SelectionReplacementService(
            editable_writer=writer_for(_Desktop([node])),
            clipboard_service=FakeClipboard(),
        ).replace("bye", None, confirmed=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "no_insertion_backend")

    def test_nothing_configured_fails_closed(self):
        result = SelectionReplacementService().replace(
            "bye", None, confirmed=True
        )
        self.assertEqual(result["code"], "no_insertion_backend")

    def test_accessibility_refusal_does_not_fall_back(self):
        # A node with EditableText that refuses the write must not be
        # bypassed by a blind paste.
        node = _Node(contents="hello world", selection=(0, 5),
                     fail={"insert_text"})
        clipboard = FakeClipboard()
        backend = FakeInputBackend()
        result = service(
            _Desktop([node]), clipboard=clipboard, input_backend=backend
        ).replace("bye", None, confirmed=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "insertion_failed")
        self.assertEqual(backend.actions, [])
        self.assertEqual(clipboard.calls, [])

    def test_registry_passes_an_injected_backend_only(self):
        node = _Node(editable=None)
        clipboard = FakeClipboard()
        backend = FakeInputBackend()
        instance = registry(
            clipboard_service=clipboard,
            selection_replacement_service=service(
                _Desktop([node]), clipboard=clipboard, input_backend=backend
            ),
        )
        result = instance.execute(
            "replace_selection", {"replacement": "bye"}, confirmed=True
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["route"], "clipboard")

    def test_registry_builds_a_clipboard_route_only_with_a_backend(self):
        # Constructing the registry never creates an input backend; without
        # one, the clipboard route is not offered at all.
        clipboard = FakeClipboard()
        instance = registry(clipboard_service=clipboard)
        result = instance.execute(
            "replace_selection", {"replacement": "bye"}, confirmed=True
        )
        self.assertEqual(result["code"], "no_insertion_backend")
        self.assertEqual(clipboard.calls, [])

    def test_registry_accepts_an_explicit_input_backend(self):
        node = _Node(editable=None)
        clipboard = FakeClipboard()
        backend = FakeInputBackend()
        instance = registry(
            clipboard_service=clipboard,
            input_backend=backend,
            selection_replacement_service=service(
                _Desktop([node]), clipboard=clipboard, input_backend=backend
            ),
        )
        result = instance.execute(
            "replace_selection", {"replacement": "bye"}, confirmed=True
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(len(backend.actions), 1)


if __name__ == "__main__":
    unittest.main()
