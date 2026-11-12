import gi

from app.desktop.accessibility.editable import _editable_interface
from app.desktop.accessibility.selection import _is_focused, _walk

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

FOCUS_SEARCH_NODES = 5000


def focused_control_is_editable(pid):
    """True when the focused control of the application with ``pid`` takes text.

    The application is matched by its AT-SPI process id, never by name. An
    application that is not exposed (no node with that pid) is not editable.
    """
    desktop = Atspi.get_desktop(0)
    found = []

    def visit(node):
        if _is_focused(node) and _editable_interface(node) is not None:
            found.append(True)

    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is None or app.get_process_id() != pid:
            continue
        _walk(app, visit, max_nodes=FOCUS_SEARCH_NODES)
    return bool(found)


def focused_control_text(pid):
    """Return the text of the focused editable control of ``pid``, or None.

    Read through the AT-SPI text interface as a range, so it returns the
    actual contents the user would see.
    """
    desktop = Atspi.get_desktop(0)
    found = []

    def visit(node):
        if found or not _is_focused(node) or _editable_interface(node) is None:
            return
        count = Atspi.Text.get_character_count(node)
        found.append(Atspi.Text.get_text(node, 0, count))

    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is None or app.get_process_id() != pid:
            continue
        _walk(app, visit, max_nodes=FOCUS_SEARCH_NODES)
    return found[0] if found else None


class SnapshotBackend:
    """Adds the ``snapshot_state`` hook the input verifier reads around an action."""

    def __init__(self, backend, snapshot):
        self._backend = backend
        self._snapshot = snapshot

    def execute(self, action):
        return self._backend.execute(action)

    def snapshot_state(self):
        return self._snapshot()


def typed_text_landed(action, before, after):
    """Verifier: True when the focused text grew by exactly the typed text.

    ``before`` and ``after`` are the focused-text snapshots taken around the
    action. Returns None when a read is unavailable or the action is not a
    text entry, so nothing is reported as verified without evidence.
    """
    if action.kind != "type_text" or before is None or after is None:
        return None
    return after == before + action.fields["text"]


def typing_refusal(target_id, focused_id, editable):
    """Return the refusal code for typing into ``target_id``, or None to allow.

    ``focused_id`` is the live keyboard-focused window after the focus request.
    ``editable`` says whether that window's focused control accepts text.
    """
    if target_id is None:
        return "no_target_window"
    if focused_id != target_id:
        return "not_focused"
    if not editable:
        return "not_editable"
    return None
