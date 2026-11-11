"""Read the selected text of the focused control, locally and boundedly.

The preferred source is the accessibility layer: a focused text widget can
report its selected span (``Text``) and its current contents (``EditableText``)
without touching the clipboard. When the focused application does not expose
those interfaces, the caller may fall back to a clipboard copy through an
injected reader; this module never invokes a keyboard shortcut itself.

Everything is bounded: the number of inspected nodes, the selection length,
and the returned text. Nothing is logged; the text is returned to the caller
and marked private by the tools that expose it.
"""

from __future__ import annotations

MAX_TEXT_CHARS = 100_000
MAX_NODES = 400
MAX_DEPTH = 12

SELECTION_ROLES = frozenset({
    "entry", "text", "paragraph", "label", "document text", "document frame",
    "terminal", "list item", "table cell", "table row", "heading",
    "static", "section", "html content", "frame",
})


class SelectionReadError(RuntimeError):
    """Raised when the selected text cannot be read locally."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code, message):
    return SelectionReadError(code, message)


def _call(node, name, *arguments, default=None):
    """Call ``node.name(*arguments)`` and return its value, or ``default``.

    Live Atspi methods are positional (``Atspi.Accessible.get_child_at_index(self,
    index:int)``, verified against Atspi 2.0), and some reject keywords
    outright. Index arguments therefore go positionally first; a keyword-only
    test double is supported as a fallback.
    """
    try:
        method = getattr(node, name, None)
        if not callable(method):
            return default
        if arguments:
            try:
                return method(*arguments)
            except TypeError:
                return method(index=arguments[0])
        return method()
    except Exception:
        return default


def _index_call(node, name, index, default=None):
    """Call an AT-SPI index method the way the real bindings accept it.

    Live Atspi methods are ``Atspi.Text.get_selection(self, index:int)`` and reject the keyword
    form (verified against Atspi 2.0 on this machine), so the positional call
    is authoritative. A test double that only accepts ``index=`` is supported
    as a fallback.
    """
    try:
        method = getattr(node, name, None)
        if not callable(method):
            return default
        try:
            return method(index)
        except TypeError:
            return method(index=index)
    except Exception:
        return default


def _text_value(node):
    """Return the editable text held by ``node``, or None."""
    for name in ("get_text", "get_contents"):
        try:
            interface = getattr(node, name, None)
            if not callable(interface):
                continue
            # GI exposes these as methods returning interfaces.
            value = interface()
            if value is None:
                continue
            text = _call(value, "get_text", default=None)
            if isinstance(text, str):
                return text
            if value is not interface:
                contents = _call(value, "get_contents", default=None)
                if isinstance(contents, str):
                    return contents
        except Exception:
            continue
    return None


def _selection_span(node):
    """Return ``(text, start, end)`` for the node's selected text, or None."""
    for name in ("get_text", "get_text_iface"):
        try:
            interface = getattr(node, name, None)
            if not callable(interface):
                continue
            # A node that lacks the interface raises or returns None; fall
            # through to the next name instead of aborting the whole probe.
            text_iface = interface()
            if text_iface is None:
                continue
            count = _call(text_iface, "get_n_selections", default=None)
            if not isinstance(count, int) or count <= 0:
                continue
            # The first selection is the one a user reads and copies.
            selection = _index_call(
                text_iface, "get_selection", 0, default=None
            )
            if not isinstance(selection, tuple) or len(selection) != 3:
                continue
            start, end, selected = selection
            if not isinstance(selected, str):
                continue
            return selected, start, end
        except Exception:
            continue
    return None


def _walk(desktop, visit, max_nodes=MAX_NODES, max_depth=MAX_DEPTH):
    """Visit nodes breadth-first, honouring both bounds. Returns a visited count."""
    try:
        root_count = int(_call(desktop, "get_child_count", default=0) or 0)
    except Exception:
        root_count = 0
    queue = []
    for index in range(min(root_count, max_nodes)):
        child = _index_call(desktop, "get_child_at_index", index)
        if child is not None:
            queue.append((child, 1))
    visited = 0
    while queue and visited < max_nodes:
        node, depth = queue.pop(0)
        visited += 1
        try:
            if visit(node) is True:
                return visited
        except Exception:
            pass
        if depth >= max_depth:
            continue
        try:
            children = int(_call(node, "get_child_count", default=0) or 0)
        except Exception:
            children = 0
        for index in range(min(children, max_nodes - visited)):
            child = _index_call(node, "get_child_at_index", index)
            if child is not None:
                queue.append((child, depth + 1))
    return visited


def _is_focused(node):
    try:
        state_set = _call(node, "get_state_set", default=None)
        if state_set is None:
            return False
        for name in ("contains",):
            method = getattr(state_set, name, None)
            if not callable(method):
                continue
            # Atspi.StateType.FOCUSED == 12 on the wire; accept enum or int.
            try:
                import gi
                gi.require_version("Atspi", "2.0")
                from gi.repository import Atspi
                focused = Atspi.StateType.FOCUSED
            except Exception:
                focused = 12
            try:
                return bool(method(focused))
            except Exception:
                continue
    except Exception:
        return False
    return False


class SelectionReader:
    """Read the focused selection through accessibility, with a clipboard fallback."""

    def __init__(self, desktop_provider=None, clipboard_reader=None):
        self._desktop_provider = desktop_provider
        self._clipboard_reader = clipboard_reader
        self._desktop = None

    # -- connection ----------------------------------------------------
    def _desktop_object(self):
        if self._desktop is not None:
            return self._desktop
        if self._desktop_provider is not None:
            try:
                self._desktop = self._desktop_provider()
            except Exception as error:
                raise _fail(
                    "accessibility_unavailable",
                    f"Accessibility is unavailable: {error}",
                ) from error
        else:
            try:
                import gi
                gi.require_version("Atspi", "2.0")
                from gi.repository import Atspi
                self._desktop = Atspi.get_desktop(0)
            except Exception as error:
                raise _fail(
                    "accessibility_unavailable",
                    f"Accessibility is unavailable: {error}",
                ) from error
        if self._desktop is None:
            raise _fail("accessibility_unavailable", "No accessibility desktop.")
        return self._desktop

    # -- reading -------------------------------------------------------
    def read_selection(self, max_chars=MAX_TEXT_CHARS, allow_clipboard=False):
        """Return ``{"text", "source", "truncated", "length"}`` for the selection.

        ``source`` is ``"accessibility"`` or ``"clipboard"``. The clipboard is
        consulted only when accessibility exposes nothing and the caller
        explicitly allows it.
        """
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or not 1 <= max_chars <= MAX_TEXT_CHARS
        ):
            raise _fail("invalid_limit", "max_chars must be 1..%d." % MAX_TEXT_CHARS)

        text = self._from_accessibility()
        source = "accessibility"
        if text is None:
            if not allow_clipboard:
                raise _fail(
                    "no_selection",
                    "No selected text was found in the focused control.",
                )
            text = self._from_clipboard()
            source = "clipboard"

        if not isinstance(text, str):
            raise _fail("no_selection", "No selected text was found.")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            raise _fail("empty_selection", "The selected text is empty.")
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        return {
            "text": text,
            "source": source,
            "truncated": truncated,
            "length": len(text),
        }

    def _from_accessibility(self):
        desktop = self._desktop_object()
        found = {}

        def visit(node):
            role = str(_call(node, "get_role_name", default="") or "").casefold()
            if role not in SELECTION_ROLES and not _is_focused(node):
                return False
            span = _selection_span(node)
            if span is not None:
                found["selection"] = span[0]
                return True
            if _is_focused(node):
                # A focused entry with a caret marks its selection differently
                # on some toolkits; the interface query above is authoritative,
                # so remember the first focused multiline text as a last resort.
                value = _text_value(node)
                if isinstance(value, str) and value.strip():
                    found.setdefault("focused_text", value)
            return False

        _walk(desktop, visit)
        if "selection" in found:
            return found["selection"]
        return None

    def _from_clipboard(self):
        if self._clipboard_reader is None:
            raise _fail(
                "no_selection",
                "No selected text was found and the clipboard is unavailable.",
            )
        try:
            result = self._clipboard_reader()
        except Exception as error:
            raise _fail("clipboard_error", "Clipboard read failed.") from error
        if not isinstance(result, dict) or result.get("success") is not True:
            raise _fail("clipboard_error", "Clipboard read failed.")
        text = result.get("text")
        if not isinstance(text, str):
            raise _fail("clipboard_error", "Clipboard read failed.")
        return text
