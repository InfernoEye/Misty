"""Tests for reading the focused selection without a live AT-SPI bus."""

import unittest

from app.desktop.accessibility.selection import (
    MAX_TEXT_CHARS,
    SelectionReadError,
    SelectionReader,
)


class _StateSet:
    def __init__(self, focused):
        self._focused = focused

    def contains(self, _state):
        return self._focused


class _TextInterface:
    def __init__(self, selected):
        self._selected = selected

    def get_n_selections(self):
        return 1 if self._selected is not None else 0

    def get_selection(self, _index):
        return (0, len(self._selected or ""), self._selected)


class _Editable:
    def __init__(self, text):
        self._text = text

    def get_text(self):
        return self._text

    def get_contents(self):
        return self._text


class _Node:
    def __init__(
        self,
        role="entry",
        name="",
        children=(),
        focused=False,
        selected=None,
        editable_text=None,
    ):
        self._role = role
        self._name = name
        self._children = list(children)
        self._focused = focused
        self._selected = selected
        self._editable = editable_text

    def get_role_name(self):
        return self._role

    def get_name(self):
        return self._name

    def get_child_count(self):
        return len(self._children)

    def get_child_at_index(self, index):
        return self._children[index]

    def get_state_set(self):
        return _StateSet(self._focused)

    def get_text(self):
        # GI exposes the text interface as get_text(); a node without one
        # (a frame, for example) simply lacks the attribute.
        if self._selected is None:
            raise AttributeError("get_text")
        return _TextInterface(self._selected)

    def get_edit(self):
        raise AttributeError("get_edit")


class _Desktop:
    def __init__(self, children):
        self._children = list(children)

    def get_child_count(self):
        return len(self._children)

    def get_child_at_index(self, index):
        return self._children[index]


def reader_for(desktop, clipboard=None):
    return SelectionReader(
        desktop_provider=lambda: desktop, clipboard_reader=clipboard
    )


class SelectionReaderTests(unittest.TestCase):
    def test_reads_selected_text_from_a_focused_entry(self):
        node = _Node(focused=True, selected="selected words")
        result = reader_for(_Desktop([node])).read_selection()

        self.assertEqual(result["text"], "selected words")
        self.assertEqual(result["source"], "accessibility")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["length"], len("selected words"))

    def test_finds_a_nested_selection_by_role(self):
        leaf = _Node(role="text", selected="deep selection")
        middle = _Node(role="frame", children=[leaf])
        result = reader_for(_Desktop([middle])).read_selection()

        self.assertEqual(result["text"], "deep selection")

    def test_truncates_to_the_requested_limit(self):
        node = _Node(selected="x" * 50)
        result = reader_for(_Desktop([node])).read_selection(max_chars=10)

        self.assertEqual(len(result["text"]), 10)
        self.assertTrue(result["truncated"])

    def test_normalises_windows_line_endings(self):
        node = _Node(selected="one\r\ntwo\rthree")
        result = reader_for(_Desktop([node])).read_selection()

        self.assertEqual(result["text"], "one\ntwo\nthree")

    def test_no_selection_without_clipboard_raises(self):
        desktop = _Desktop([_Node(role="frame")])
        with self.assertRaises(SelectionReadError) as caught:
            reader_for(desktop).read_selection()

        self.assertEqual(caught.exception.code, "no_selection")

    def test_clipboard_fallback_when_allowed(self):
        desktop = _Desktop([_Node(role="frame")])
        clipboard = lambda: {"success": True, "text": "copied text"}
        result = reader_for(desktop, clipboard).read_selection(
            allow_clipboard=True
        )

        self.assertEqual(result["text"], "copied text")
        self.assertEqual(result["source"], "clipboard")

    def test_clipboard_fallback_is_not_used_by_default(self):
        desktop = _Desktop([_Node(role="frame")])
        clipboard = lambda: {"success": True, "text": "copied text"}
        with self.assertRaises(SelectionReadError):
            reader_for(desktop, clipboard).read_selection()

    def test_clipboard_failure_is_reported(self):
        desktop = _Desktop([_Node(role="frame")])
        clipboard = lambda: {"success": False, "message": "no"}
        with self.assertRaises(SelectionReadError) as caught:
            reader_for(desktop, clipboard).read_selection(allow_clipboard=True)

        self.assertEqual(caught.exception.code, "clipboard_error")

    def test_empty_selection_is_reported(self):
        node = _Node(selected="   \n  ")
        with self.assertRaises(SelectionReadError) as caught:
            reader_for(_Desktop([node])).read_selection()

        self.assertEqual(caught.exception.code, "empty_selection")

    def test_invalid_limit_is_rejected(self):
        node = _Node(selected="text")
        reader = reader_for(_Desktop([node]))
        for value in (0, -1, True, "10", MAX_TEXT_CHARS + 1):
            with self.subTest(value=value):
                with self.assertRaises(SelectionReadError) as caught:
                    reader.read_selection(max_chars=value)
                self.assertEqual(caught.exception.code, "invalid_limit")

    def test_accessibility_unavailable_is_reported(self):
        def broken():
            raise RuntimeError("no bus")

        reader = SelectionReader(desktop_provider=broken)
        with self.assertRaises(SelectionReadError) as caught:
            reader.read_selection()

        self.assertEqual(
            caught.exception.code, "accessibility_unavailable"
        )

    def test_walk_is_bounded_on_a_deep_tree(self):
        node = _Node(role="frame")
        current = node
        for _ in range(200):
            child = _Node(role="frame")
            current._children = [child]
            current = child
        # No selection exists, so this must terminate quickly instead of
        # walking the whole tree.
        with self.assertRaises(SelectionReadError):
            reader_for(_Desktop([node])).read_selection()


if __name__ == "__main__":
    unittest.main()
