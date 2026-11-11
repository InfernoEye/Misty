"""Replace the focused selection through AT-SPI ``EditableText``.

This module never synthesises keyboard events.  It resolves the *same*
accessible node that holds the current selection and, only when that node
exposes ``EditableText``, asks it to replace the selected span:

1. ``Text.get_n_selections`` / ``Text.get_selection(0)`` identify the span
   (positional index: the live bindings reject keywords);
2. ``EditableText.delete_text(start, end)`` removes exactly that span;
3. ``EditableText.insert_text(offset, text, length)`` writes the replacement
   at the now-empty selection start, byte-limited by ``length``.

Nothing is pressed blindly: an application that exposes no ``EditableText``
is reported as ``no_editable`` and the caller may choose another route
(e.g. a clipboard paste through an explicitly injected input backend).  The
concurrency guard is optimistic: when the caller supplies the text it
expected, the live selection or the node's full contents must still match it
(whitespace-normalised) or nothing is written (``selection_changed``).

Text handling is bounded and sanitised: NUL and every control character
except newline and tab are rejected, the replacement is capped, and typed or
replaced text is never logged.  No Qt is imported here.
"""

from __future__ import annotations

from app.desktop.accessibility.selection import (
    MAX_NODES,
    MAX_TEXT_CHARS,
    SELECTION_ROLES,
    SelectionReader,
    _call,
    _index_call,
    _is_focused,
    _selection_span,
    _text_value,
    _walk,
)

__all__ = [
    "AccessibilityEditableWriter",
    "EditableWriteError",
    "MAX_REPLACEMENT_CHARS",
    "MAX_TEXT_CHARS",
    "POSITIONAL_REPLACEMENTS",
    "normalise_for_compare",
    "replace_text",
    "sanitise_replacement",
]

MAX_REPLACEMENT_CHARS = 20_000

# One replacement for the whole span: two interface writes, in this order.
POSITIONAL_REPLACEMENTS = (
    "EditableText.delete_text(start, end)",
    "EditableText.insert_text(start, replacement, length)",
)


class EditableWriteError(RuntimeError):
    """Raised when the selection cannot be replaced through AT-SPI."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code, message):
    return EditableWriteError(code, message)


# ---------------------------------------------------------------------------
# Validation helpers


def sanitise_replacement(text):
    """Return ``text`` when it is safe to insert, else raise ``invalid_text``."""
    if not isinstance(text, str):
        raise _fail("invalid_text", "The replacement must be text.")
    if len(text) > MAX_REPLACEMENT_CHARS:
        raise _fail(
            "invalid_text",
            f"The replacement must be at most {MAX_REPLACEMENT_CHARS} "
            "characters.",
        )
    for character in text:
        if character in ("\n", "\t"):
            continue
        code = ord(character)
        # C0, DEL and C1 controls, plus lone surrogates (not encodable).
        if code < 0x20 or 0x7F <= code <= 0x9F or 0xD800 <= code <= 0xDFFF:
            if code == 0:
                raise _fail(
                    "invalid_text", "The replacement must not contain NUL."
                )
            raise _fail(
                "invalid_text",
                "The replacement must not contain control characters other "
                "than newline and tab.",
            )
    return text


def _normalise(text):
    return " ".join(str(text or "").split())


def normalise_for_compare(text):
    """Whitespace-normalised form used by the optimistic concurrency check."""
    return _normalise(text)


def _matches_expected(expected, *values):
    """True when any candidate value equals ``expected`` (normalised).

    An expected value with no visible characters (empty or whitespace only)
    has no normalised form, so the live selection and the full contents are
    also compared verbatim; otherwise it would match anything.
    """
    target = _normalise(expected)
    raw = str(expected)
    for value in values:
        if not isinstance(value, str):
            continue
        if not target:
            return value == raw
        if _normalise(value) == target:
            return True
    return False


# ---------------------------------------------------------------------------
# AT-SPI node operations


def _editable_interface(node):
    for name in ("get_edit", "get_editable_text", "get_text"):
        try:
            interface = getattr(node, name, None)
            if not callable(interface):
                continue
            value = interface()
        except Exception:
            continue
        if value is None:
            continue
        if callable(getattr(value, "delete_text", None)) and callable(
            getattr(value, "insert_text", None)
        ):
            return value
    return None


def _interface_call(interface, name, *arguments):
    """Call an ``EditableText`` method and report whether it worked.

    GI marshals Python ``str`` to ``char*`` and raises ``TypeError`` for the
    integer out-parameter, which the live call has already executed; a test
    double returns normally.  Both outcomes are accepted; any other error is
    reported to the caller.
    """
    try:
        getattr(interface, name)(*arguments)
        return True
    except TypeError:
        return True
    except Exception:
        return False


def replace_text(node, expected, replacement):
    """Replace the node's selected span with ``replacement``.

    Returns a content-free dict: ``{"success", "code", "selections",
    "replaced_length", "length", "length_delta"}``.  Never returns the text.
    """
    replacement = sanitise_replacement(replacement)
    interface = _editable_interface(node)
    if interface is None:
        raise _fail(
            "no_editable",
            "The focused control does not support accessibility text editing.",
        )
    span = _selection_span(node)
    if span is None:
        raise _fail(
            "no_selection", "The focused control has no selected text."
        )
    selected, start, end = span
    if not isinstance(start, int) or not isinstance(end, int):
        raise _fail("no_selection", "The selection span is not valid.")
    if end < start or start < 0:
        raise _fail("no_selection", "The selection span is not valid.")

    if expected is not None:
        if not _matches_expected(expected, selected, _text_value(node)):
            raise _fail(
                "selection_changed",
                "The selected text changed since it was previewed, so "
                "nothing was written.",
            )

    removed = end - start
    if not _interface_call(interface, "delete_text", start, end):
        raise _fail(
            "insertion_failed", "The control refused the text deletion."
        )

    try:
        payload = replacement.encode("utf-8")
    except UnicodeError as error:
        raise _fail(
            "invalid_text", "The replacement is not valid text."
        ) from error
    if not _interface_call(
        interface, "insert_text", start, replacement, len(payload)
    ):
        raise _fail(
            "insertion_failed",
            "The text was removed but the replacement could not be "
            "inserted.",
        )

    return {
        "success": True,
        "code": "replaced",
        "selections": 0,
        "replaced_length": removed,
        "length": len(replacement),
        "length_delta": len(replacement) - removed,
    }


class _Resolved:
    __slots__ = ("node", "selected", "start", "end")

    def __init__(self, node, selected, start, end):
        self.node = node
        self.selected = selected
        self.start = start
        self.end = end


class _Probe:
    """Bound the tree walk and record what can be replaced."""

    def __init__(self):
        self.result = None
        self.editable = 0

    def visit(self, node):
        role = str(_call(node, "get_role_name", default="") or "").casefold()
        relevant = role in SELECTION_ROLES or _is_focused(node)
        if not relevant:
            return False
        if _editable_interface(node) is not None:
            self.editable += 1
        span = _selection_span(node)
        if span is not None and self.result is None:
            selected, start, end = span
            if isinstance(start, int) and isinstance(end, int):
                self.result = _Resolved(node, selected, start, end)
                return True
        return False


class AccessibilityEditableWriter:
    """Resolve the focused selection and replace it through ``EditableText``.

    Shares one lazily-connected desktop handle with :class:`SelectionReader`,
    so the preview and the write look at the same accessibility tree.  Every
    failure is an :class:`EditableWriteError` with a stable code:
    ``accessibility_unavailable``, ``invalid_text``, ``no_editable``,
    ``no_selection``, ``selection_changed``, ``insertion_failed``.
    """

    def __init__(self, reader=None, desktop_provider=None):
        self._reader = reader or SelectionReader(
            desktop_provider=desktop_provider
        )

    def _desktop_object(self):
        try:
            return self._reader._desktop_object()
        except EditableWriteError:
            raise
        except Exception as error:
            # The shared reader reports its own SelectionReadError; every
            # failure of this module is an EditableWriteError.
            raise _fail(
                str(getattr(error, "code", "accessibility_unavailable")),
                str(getattr(
                    error, "message", "Accessibility is unavailable."
                )),
            ) from error

    def probe(self):
        """Return the current selection and whether it is editable.

        ``{"text", "editable", "start", "end"}``; the text is private and
        the caller must never log it.
        """
        desktop = self._desktop_object()
        state = _Probe()
        _walk(desktop, state.visit, max_nodes=MAX_NODES)
        if state.result is None:
            raise _fail(
                "no_selection",
                "No selected text was found in the focused control.",
            )
        return {
            "node": state.result.node,
            "text": state.result.selected,
            "start": state.result.start,
            "end": state.result.end,
            "editable": _editable_interface(state.result.node) is not None,
            "editable_candidates": state.editable,
        }

    def replace_selection(self, replacement, expected_text=None):
        """Re-read the selection, then replace it on the same node.

        Returns a content-free result dict or raises
        :class:`EditableWriteError`.
        """
        replacement = sanitise_replacement(replacement)
        if expected_text is not None and not isinstance(expected_text, str):
            raise _fail("invalid_expected_text", "expected_text must be text.")
        state = self.probe()
        node = state["node"]
        if expected_text is not None and not _matches_expected(
            expected_text, state["text"], _text_value(node)
        ):
            raise _fail(
                "selection_changed",
                "The selected text changed since it was previewed, so "
                "nothing was written.",
            )
        return replace_text(node, None, replacement)
