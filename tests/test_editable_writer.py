"""Tests for AT-SPI EditableText replacement with fake accessibility nodes.

No live bus is touched: every node is a plain Python object, and the writer
is given a ``desktop_provider`` that returns a fake desktop.
"""

import unittest

from app.desktop.accessibility.editable import (
    MAX_REPLACEMENT_CHARS,
    AccessibilityEditableWriter,
    EditableWriteError,
    normalise_for_compare,
    replace_text,
    sanitise_replacement,
)
from app.desktop.accessibility.selection import SelectionReader


class _StateSet:
    def __init__(self, focused):
        self._focused = focused

    def contains(self, _state):
        return self._focused


class _EditableText:
    """Records delete/insert calls; mimics a live TextBuffer.

    A live GI call with an integer out-parameter raises the recorded error
    after the operation ran; ``raise_after_insert`` models that.
    """

    def __init__(self, owner, raise_after_insert=False, fail=None):
        self.owner = owner
        self.calls = []
        self.raise_after_insert = raise_after_insert
        self.fail = fail or set()

    def get_text(self, start=None, end=None):
        # Live Atspi.Text.get_text(start, end); with no arguments GI exposes
        # the whole buffer, which is what the contents comparison reads.
        if start is None or end is None or (start == 0 and end == -1):
            return self.owner.contents
        return self.owner.contents[start:end]

    def get_n_selections(self):
        return 1 if self.owner.selection is not None else 0

    def get_selection(self, index):
        # Positional-only, like the live binding.
        if index != 0:
            raise IndexError
        start, end = self.owner.selection or (0, 0)
        return (start, end, self.owner.contents[start:end])

    def delete_text(self, start, end):
        self.calls.append(("delete_text", start, end))
        if "delete_text" in self.fail:
            raise RuntimeError("refused")
        text = self.owner.contents
        self.owner.contents = text[:start] + text[end:]
        self.owner.selection = (start, start)

    def insert_text(self, offset, text, length):
        self.calls.append(("insert_text", offset, text, length))
        if "insert_text" in self.fail:
            raise RuntimeError("refused")
        current = self.owner.contents
        self.owner.contents = current[:offset] + text + current[offset:]
        self.owner.selection = None
        if self.raise_after_insert:
            raise TypeError("GDestroyNotify argument not supported")


class _TextOnly:
    """``Atspi.Text`` without ``EditableText`` (a static label, a terminal)."""

    def __init__(self, owner):
        self.owner = owner

    def get_text(self, start=None, end=None):
        if start is None or end is None or (start == 0 and end == -1):
            return self.owner.contents
        return self.owner.contents[start:end]

    def get_n_selections(self):
        return 1 if self.owner.selection is not None else 0

    def get_selection(self, index):
        if index != 0:
            raise IndexError
        start, end = self.owner.selection or (0, 0)
        return (start, end, self.owner.contents[start:end])


class _Node:
    def __init__(
        self,
        contents="hello world",
        selection=(0, 5),
        role="entry",
        focused=True,
        editable=True,
        **text_options,
    ):
        self.role = role
        self.focused = focused
        self.contents = contents
        self.selection = selection
        self.children = []
        if editable is None:
            # No EditableText.  A static text node still exposes Text (used
            # to find the selection) but cannot be edited.
            self.text = _TextOnly(self)
        else:
            self.text = _EditableText(self, **text_options)

    def get_role_name(self):
        return self.role

    def get_name(self):
        return "field"

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_state_set(self):
        return _StateSet(self.focused)

    def get_text(self):
        return self.text

    def get_edit(self):
        # Some toolkits name the EditableText getter get_edit; a node with
        # no EditableText simply lacks it.
        return self.text


class _Desktop:
    def __init__(self, children):
        self.children = list(children)

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]


def writer_for(desktop):
    return AccessibilityEditableWriter(
        reader=SelectionReader(desktop_provider=lambda: desktop)
    )


class ReplaceTextTests(unittest.TestCase):
    def test_replaces_the_selected_span_through_editable_text(self):
        node = _Node()
        result = replace_text(node, None, "hi")

        self.assertTrue(result["success"])
        self.assertEqual(node.contents, "hi world")
        self.assertEqual(
            node.text.calls,
            [
                ("delete_text", 0, 5),
                ("insert_text", 0, "hi", 2),
            ],
        )
        self.assertEqual(result["replaced_length"], 5)
        self.assertEqual(result["length"], 2)
        self.assertEqual(result["length_delta"], -3)

    def test_insert_length_is_utf8_bytes(self):
        node = _Node(contents="abc", selection=(0, 3))
        replace_text(node, None, "\u017c\u00f3\u0142\u0107")

        self.assertEqual(
            node.text.calls[1], ("insert_text", 0, "\u017c\u00f3\u0142\u0107", 8)
        )

    def test_result_never_contains_text(self):
        node = _Node(contents="SECRET-9f3a", selection=(0, 6))
        result = replace_text(node, None, "SECRET-other")
        self.assertNotIn("SECRET", repr(result))

    def test_gi_type_error_after_insert_counts_as_success(self):
        node = _Node(raise_after_insert=True)
        result = replace_text(node, None, "x")
        self.assertTrue(result["success"])
        self.assertEqual(node.contents, "x world")

    def test_failure_keeps_the_span_and_reports_the_code(self):
        node = _Node(fail={"insert_text"})
        with self.assertRaises(EditableWriteError) as caught:
            replace_text(node, None, "x")
        self.assertEqual(caught.exception.code, "insertion_failed")

    def test_selection_changed_when_expected_text_differs(self):
        node = _Node(contents="hello world", selection=(0, 5))
        with self.assertRaises(EditableWriteError) as caught:
            replace_text(node, "HELLO", "x")
        # Case differs, but the comparison is whitespace-normalised only,
        # so this is a mismatch.
        self.assertEqual(caught.exception.code, "selection_changed")
        self.assertEqual(node.text.calls, [])

    def test_expected_text_matches_after_whitespace_normalisation(self):
        node = _Node(contents="hello   world", selection=(0, 13))
        replace_text(node, "  hello\t\nworld  ", "x")
        self.assertEqual(node.contents, "x")

    def test_expected_text_can_match_the_full_contents(self):
        # Some toolkits return the whole text from get_selection for a
        # select-all; the contents comparison still accepts it.
        node = _Node(contents="alpha beta", selection=(0, 5))
        replace_text(node, "alpha beta", "x")
        self.assertEqual(node.contents, "x beta")

    def test_whitespace_only_expected_text_is_compared_verbatim(self):
        # A whitespace-only expectation has no normalised form; it must not
        # match everything by accident.
        node = _Node(contents="x   y", selection=(1, 4))
        with self.assertRaises(EditableWriteError) as caught:
            replace_text(node, "   extra", "z")
        self.assertEqual(caught.exception.code, "selection_changed")
        self.assertEqual(node.text.calls, [])
        # The exact whitespace still matches the live selection.
        replace_text(node, "   ", "z")
        self.assertEqual(node.contents, "xzy")

    def test_no_editable_interface(self):
        node = _Node(editable=None)
        with self.assertRaises(EditableWriteError) as caught:
            replace_text(node, None, "x")
        self.assertEqual(caught.exception.code, "no_editable")

    def test_no_selection(self):
        node = _Node(selection=None)
        with self.assertRaises(EditableWriteError) as caught:
            replace_text(node, None, "x")
        self.assertEqual(caught.exception.code, "no_selection")

    def test_control_characters_are_rejected_before_any_call(self):
        for value in ("a\x00b", "a\x01b", "a\x7fb", "a\x9bb", "a\rb"):
            with self.subTest(value=repr(value)):
                node = _Node()
                with self.assertRaises(EditableWriteError) as caught:
                    replace_text(node, None, value)
                self.assertEqual(caught.exception.code, "invalid_text")
                self.assertEqual(node.text.calls, [])
                self.assertNotIn(value, caught.exception.message)

    def test_newline_and_tab_are_allowed(self):
        self.assertEqual(sanitise_replacement("a\nb\tc"), "a\nb\tc")

    def test_length_cap(self):
        with self.assertRaises(EditableWriteError) as caught:
            sanitise_replacement("x" * (MAX_REPLACEMENT_CHARS + 1))
        self.assertEqual(caught.exception.code, "invalid_text")

    def test_non_text_replacement(self):
        with self.assertRaises(EditableWriteError):
            sanitise_replacement(None)

    def test_normalise_helper(self):
        self.assertEqual(normalise_for_compare(" a \n b "), "a b")


class AccessibilityEditableWriterTests(unittest.TestCase):
    def test_probe_reports_selection_and_editability(self):
        node = _Node(contents="some selected words", selection=(5, 13))
        state = writer_for(_Desktop([_Node(role="frame", selection=None),
                                     node])).probe()
        self.assertEqual(state["text"], "selected")
        self.assertTrue(state["editable"])
        self.assertEqual(state["start"], 5)
        self.assertEqual(state["end"], 13)

    def test_probe_marks_a_read_only_selection(self):
        node = _Node(role="text", editable=None)
        self.assertFalse(writer_for(_Desktop([node])).probe()["editable"])

    def test_replace_selection_writes_on_the_same_node(self):
        node = _Node(contents="hello world", selection=(0, 5))
        result = writer_for(_Desktop([node])).replace_selection("bye")
        self.assertTrue(result["success"])
        self.assertEqual(node.contents, "bye world")

    def test_replace_refuses_when_the_selection_changed(self):
        node = _Node(contents="hello world", selection=(0, 5))
        with self.assertRaises(EditableWriteError) as caught:
            writer_for(_Desktop([node])).replace_selection(
                "bye", expected_text="something else"
            )
        self.assertEqual(caught.exception.code, "selection_changed")
        self.assertEqual(node.text.calls, [])

    def test_positive_expected_text_writes(self):
        node = _Node(contents="hello world", selection=(0, 5))
        writer_for(_Desktop([node])).replace_selection(
            "bye", expected_text="hello"
        )
        self.assertEqual(node.contents, "bye world")

    def test_no_selection_raises(self):
        node = _Node(selection=None)
        with self.assertRaises(EditableWriteError) as caught:
            writer_for(_Desktop([node])).probe()
        self.assertEqual(caught.exception.code, "no_selection")

    def test_accessibility_unavailable_is_reported(self):
        def broken():
            raise RuntimeError("no bus")

        writer = AccessibilityEditableWriter(
            reader=SelectionReader(desktop_provider=broken)
        )
        with self.assertRaises(EditableWriteError) as caught:
            writer.probe()
        self.assertEqual(caught.exception.code, "accessibility_unavailable")

    def test_walk_is_bounded(self):
        root = _Node(role="frame", selection=None, editable=None)
        current = root
        for _ in range(300):
            child = _Node(role="frame", selection=None, editable=None)
            current.children = [child]
            current = child
        with self.assertRaises(EditableWriteError):
            writer_for(_Desktop([root])).probe()

    def test_live_bus_is_never_contacted_when_the_provider_fails(self):
        # The writer shares the reader's lazily-connected handle; a failing
        # provider must surface as a code, not probe the real Atspi module.
        writer = AccessibilityEditableWriter(
            reader=SelectionReader(desktop_provider=lambda: None)
        )
        with self.assertRaises(EditableWriteError) as caught:
            writer.probe()
        self.assertEqual(caught.exception.code, "accessibility_unavailable")


if __name__ == "__main__":
    unittest.main()
