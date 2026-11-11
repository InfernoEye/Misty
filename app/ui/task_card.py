"""Task card: the progress of one request, with Cancel and Retry.

The card is a pure view. It renders a title, one row per step (tool name,
status, a one-line detail), an elapsed timer and two buttons, and it emits
``cancel_requested`` / ``retry_requested(step_tool)``. It never executes a
tool, never starts a thread of its own and never asks the model anything:
the controller decides what those requests mean, and only the controller
knows whether a failed step may be retried (``app.brain.policy.is_retryable``).

Events consumed by :meth:`TaskCard.apply_event` are the agent activity
events (``kind`` in ``{"decision", "tool"}`` with a ``tool`` name). Detail
text is reduced to its first line and truncated, and steps marked
``sensitive`` only show a fixed placeholder, so clipboard, screen or
document text never reaches the card.
"""

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

#: Pure policy lookup; the card decides eligibility, the controller re-checks.
from app.brain.policy import is_retryable


#: Statuses a step row can show, in the order they are counted.
STEP_STATUSES = (
    "planned",
    "running",
    "success",
    "failed",
    "blocked",
    "skipped",
)
#: One-character glyph per status (no icon files, no emoji fonts needed).
STATUS_GLYPHS = {
    "planned": ("○", "#8d9ab2"),
    "running": ("◐", "#8fb3ff"),
    "success": ("●", "#7fd4a2"),
    "failed": ("✕", "#f0857c"),
    "blocked": ("!", "#e9bf6a"),
    "skipped": ("–", "#8d9ab2"),
}
#: Statuses from which a step never moves back to planned/running.
FINAL_STATUSES = ("success", "failed", "blocked", "skipped")
SENSITIVE_DETAIL = "Private details hidden."
#: Event titles the agent uses for outcomes that are not per-step tools.
_FINISH_TITLES = {
    "Task completed": "completed",
    "Task recovered after a failure": "recovered",
    "Task not fully completed": "failed",
    "Task cancelled": "cancelled",
    "Task plan rejected": "failed",
    "Window target needed": "failed",
    "Clarification needed": "needs_input",
    "No executable actions": "completed",
    "Misty replied": "completed",
}
_TITLE_MAX = 90
_DETAIL_MAX = 140
_MAX_ROWS = 24


def _one_line(value, limit):
    if not isinstance(value, str):
        return ""
    line = value.strip().splitlines()[0].strip() if value.strip() else ""
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _event_detail(event):
    """First line of an event's details, or a placeholder when private."""
    if event.get("sensitive") is True:
        return SENSITIVE_DETAIL
    return _one_line(event.get("details"), _DETAIL_MAX)


def _event_status(event):
    status = event.get("status")
    if status == "failure":
        return "failed"
    if status == "success":
        return "success"
    if status in ("running", "planned"):
        return status
    return ""


def format_elapsed(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class _StepRow(QWidget):
    """One compact row: glyph, tool name, status word, one-line detail."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.name_label = QLabel("")
        self.name_label.setStyleSheet("font-weight: 600;")
        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("muted")
        self.detail_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.detail_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        glyph = QLabel("○")
        glyph.setFixedWidth(14)
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(glyph)
        layout.addWidget(self.name_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.detail_label, 1)
        self._glyph = glyph
        self.set_status("planned", "")

    def set_status(self, status, detail):
        glyph, color = STATUS_GLYPHS.get(status, STATUS_GLYPHS["planned"])
        self._glyph.setText(glyph)
        self._glyph.setStyleSheet(f"color: {color}; font-weight: 700;")
        self.status_label.setText(status)
        self.status_label.setToolTip(f"Step status: {status}")
        self.detail_label.setText(detail or "")
        self.detail_label.setToolTip(detail or "")

    def set_tool(self, tool):
        self.name_label.setText(tool or "")
        self.setAccessibleName(tool or "")


class TaskCard(QFrame):
    """Compact task progress view; see the module docstring."""

    cancel_requested = Signal()
    retry_requested = Signal(str)

    def __init__(
        self,
        parent=None,
        *,
        hide_after_ms=4000,
        elapsed_interval_ms=1000,
        clock=time.monotonic,
    ):
        super().__init__(parent)
        self.setObjectName("taskCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("""
            QFrame#taskCard {
                background: #192130;
                border: 1px solid #34415a;
                border-radius: 12px;
            }
            QFrame#taskCard QLabel { color: #e8edf7; }
            QFrame#taskCard QLabel#muted { color: #8d9ab2; }
        """)

        self._clock = clock
        self._started_monotonic = None
        self._status = "idle"
        self._request = ""
        self._steps = []
        self._rows = []
        self._job = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(10)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-weight: 650;")
        self.elapsed_label = QLabel("0:00")
        self.elapsed_label.setObjectName("muted")
        header.addWidget(self.title_label, 1)
        header.addWidget(self.elapsed_label)
        outer.addLayout(header)

        self.steps_container = QWidget()
        self.steps_layout = QVBoxLayout(self.steps_container)
        self.steps_layout.setContentsMargins(0, 0, 0, 0)
        self.steps_layout.setSpacing(2)
        outer.addWidget(self.steps_container)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        footer.addWidget(self.status_label, 1)
        self.retry_button = QPushButton("Retry failed step")
        self.retry_button.setObjectName("secondary")
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self._emit_retry)
        footer.addWidget(self.retry_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("secondary")
        self.cancel_button.clicked.connect(self._emit_cancel)
        footer.addWidget(self.cancel_button)
        outer.addLayout(footer)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(int(hide_after_ms))
        self._hide_timer.timeout.connect(self.hide)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(int(elapsed_interval_ms))
        self._elapsed_timer.timeout.connect(self._tick)

        self.setVisible(False)

    # -- public API -----------------------------------------------------

    def set_task(self, request, job=""):
        """Start showing a new task; any previous task is replaced.

        ``job`` optionally names the controller request id so a retry can be
        attached to the same card; an empty value starts a fresh card.
        """
        if job and job == self._job and self._steps:
            # A retry is the same task: keep the card and its rows.
            self._hide_timer.stop()
            self._status = "running"
            self.status_label.setText("Working…")
            self.cancel_button.setEnabled(True)
            self.cancel_button.setText("Cancel")
            self._started_monotonic = self._clock()
            self._elapsed_timer.start()
            self._refresh_retry()
            self.setVisible(True)
            return
        self.reset()
        self._job = job or ""
        self._request = _one_line(str(request or ""), _TITLE_MAX) or "Task"
        self.title_label.setText(self._request)
        self.title_label.setToolTip(str(request or "")[:2000])
        self.status_label.setText("Working…")
        self._status = "running"
        self._started_monotonic = self._clock()
        self._tick()
        self._elapsed_timer.start()
        self.setVisible(True)

    def apply_event(self, event):
        """Fold one agent activity event into the card; never raises."""
        if not isinstance(event, dict):
            return
        try:
            self._apply_event(event)
        except Exception as error:  # the card must never break a request
            print(f"[Misty] Task card event error: {error}", flush=True)

    def finish(self, status):
        """Final status ("completed", "failed", "cancelled", ...)."""
        status = _one_line(str(status or ""), 40) or "finished"
        self._status = status
        self._elapsed_timer.stop()
        self._tick()
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancel")
        if status in ("completed", "recovered"):
            self._mark_open_steps("skipped", "Finished.")
        elif status in ("cancelled", "failed"):
            self._mark_open_steps(
                "skipped" if status == "cancelled" else "blocked",
                "Not started." if status == "cancelled" else "Not run.",
            )
        failed = self.failed_steps()
        self.status_label.setText(
            f"{status.capitalize()}" + (
                f" · {len(failed)} step(s) failed" if failed else ""
            )
        )
        self._refresh_retry()
        # Keep the final status visible; hide only after a grace period.
        self._hide_timer.start()

    def reset(self):
        """Clear everything and hide (the next set_task starts fresh)."""
        self._hide_timer.stop()
        self._elapsed_timer.stop()
        self._started_monotonic = None
        self._status = "idle"
        self._request = ""
        self._job = ""
        self._steps = []
        self.title_label.setText("")
        self.elapsed_label.setText("0:00")
        self.status_label.setText("")
        self._clear_rows()
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancel")
        self.retry_button.setEnabled(False)
        self.retry_button.setToolTip("")
        self.setVisible(False)

    def dismiss(self):
        """The user closed the finished card; stop showing it."""
        self.reset()

    # -- queries --------------------------------------------------------

    @property
    def status(self):
        return self._status

    @property
    def request(self):
        return self._request

    def steps(self):
        """Defensive copy of the current step list."""
        return [dict(step) for step in self._steps]

    def failed_steps(self):
        return [dict(step) for step in self._steps if step["status"] == "failed"]

    def retry_candidate(self):
        """Tool name of the single retryable failed step, else ``""``.

        Exactly one step may have failed, the card must not be mid-task,
        and ``app.brain.policy.is_retryable`` (checked against the registry
        handler when the controller supplied one) must allow repeating it.
        """
        if self._status in ("running", "idle", "cancelling"):
            return ""
        failed = [step for step in self._steps if step["status"] == "failed"]
        if len(failed) != 1:
            return ""
        step = failed[0]
        if not step["retryable"]:
            return ""
        return step["tool"]

    def set_retryable_tools(self, tools):
        """Controller hint: tools whose handler makes a retry unsafe."""
        allowed = set(tools or ())
        for row, step in zip(self._rows, self._steps):
            step["retryable"] = (
                step["retryable"] and step["tool"] not in allowed
            )
            self._render_row(row, step)
        self._refresh_retry()

    def is_private(self):
        return any(step["sensitive"] for step in self._steps)

    # -- internals ------------------------------------------------------

    def _clear_rows(self):
        for row in self._rows:
            self.steps_layout.removeWidget(row)
            row.deleteLater()
        self._rows = []

    def _row_for(self, tool, create=True):
        for row, step in zip(self._rows, self._steps):
            if step["tool"] == tool:
                return row, step
        if not create or len(self._steps) >= _MAX_ROWS:
            return None, None
        step = {
            "tool": tool or "step",
            "status": "planned",
            "detail": "",
            "sensitive": False,
            "retryable": is_retryable(tool),
            "arguments": None,
        }
        if tool:
            self._steps.append(step)
            row = _StepRow()
            self._rows.append(row)
            self.steps_layout.addWidget(row)
            return row, step
        return None, None

    def _render_row(self, row, step):
        detail = step["detail"]
        if step["sensitive"]:
            detail = SENSITIVE_DETAIL
        row.set_tool(step["tool"])
        row.set_status(step["status"], detail)

    def _set_step(self, tool, status, detail=None):
        """Set a step's status; a non-status event never regresses it."""
        row, step = self._row_for(tool)
        if step is None:
            return
        if status and step["status"] in FINAL_STATUSES and status in (
            "planned", "running"
        ):
            # A finished step never moves backwards (the agent emits
            # "Retrying ..." as running for a transient retry).
            status = ""
        if status:
            step["status"] = status
        if detail is not None:
            step["detail"] = detail
        if step["status"] == "failed":
            # A failure is retryable only through a fresh policy answer.
            step["retryable"] = is_retryable(step["tool"])
        self._render_row(row, step)
        self._refresh_retry()

    def _mark_open_steps(self, status, detail):
        for row, step in zip(self._rows, self._steps):
            if step["status"] in ("planned", "running"):
                step["status"] = status
                step["detail"] = step["detail"] or detail
                self._render_row(row, step)

    def _apply_event(self, event):
        kind = event.get("kind")
        title = event.get("title")
        if not isinstance(title, str):
            title = ""
        tool = event.get("tool")
        tool = tool if isinstance(tool, str) and tool else ""

        if kind in ("decision", "tool") and tool:
            status = _event_status(event)
            row, step = self._row_for(tool)
            if step is None:
                return
            if kind == "decision":
                # A per-step decision only creates/labels a planned row; it
                # never regresses a step that already ran.
                if status == "planned" and step["status"] == "planned":
                    self._set_step(tool, "", _event_detail(event))
                return
            if status == "running":
                if step["status"] in FINAL_STATUSES:
                    return
                self._set_step(tool, "running", _event_detail(event))
                return
            if status in ("success", "failed"):
                if event.get("sensitive") is True:
                    step["sensitive"] = True
                self._set_step(tool, status, _event_detail(event))
                return
            return

        mapped = _FINISH_TITLES.get(title)
        if mapped is not None:
            self.finish(mapped)

    def _refresh_retry(self):
        candidate = self.retry_candidate()
        self.retry_button.setEnabled(bool(candidate))
        if self._status == "running":
            self.retry_button.setToolTip(
                "Available once the task stops."
            )
        elif candidate:
            self.retry_button.setToolTip(
                f"Run {candidate} again with the same arguments."
            )
        else:
            self.retry_button.setToolTip(
                "Retry is available only when exactly one step failed and "
                "that step is read-only and safe to repeat."
            )

    def _tick(self):
        if self._started_monotonic is None:
            seconds = 0.0
        else:
            seconds = self._clock() - self._started_monotonic
        self.elapsed_label.setText(format_elapsed(seconds))

    def mark_retry_requested(self, tool):
        """Show that ``tool`` is about to run again (no execution here)."""
        self._hide_timer.stop()
        self._status = "running"
        self._started_monotonic = self._clock()
        self._elapsed_timer.start()
        self.cancel_button.setEnabled(True)
        self.status_label.setText(f"Retrying {tool}…")
        self._set_step(tool, "running", "Retrying with the same arguments.")
        self.setVisible(True)

    def _emit_cancel(self):
        if self._status != "running":
            return
        self._status = "cancelling"
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelling…")
        self.retry_button.setEnabled(False)
        self.status_label.setText("Cancelling after the current step…")
        self.cancel_requested.emit()

    def _emit_retry(self):
        tool = self.retry_candidate()
        if not tool:
            return
        self.retry_requested.emit(tool)


def summarize_task_event(event):
    """Human one-liner for an activity event (kept tiny and pure)."""
    if not isinstance(event, dict):
        return ""
    title = _one_line(event.get("title"), _TITLE_MAX)
    detail = _event_detail(event)
    return f"{title}: {detail}" if detail else title


__all__ = [
    "STEP_STATUSES",
    "STATUS_GLYPHS",
    "TaskCard",
    "format_elapsed",
    "summarize_task_event",
]
