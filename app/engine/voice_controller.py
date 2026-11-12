"""Connect Misty's hold-to-talk pipeline to the native Qt desktop app."""

import threading
import time
import uuid
from pathlib import Path

from PySide6.QtCore import (
    QEventLoop,
    QObject,
    QProcess,
    QProcessEnvironment,
    QTimer,
    Signal,
    Slot,
)

from app.actions.executor import ActionExecutor
from app.brain.agent import MistyAgent
from app.brain.memory import MemoryStore
from app.brain.policy import is_retryable
from app.brain.session_context import SessionContext
from app.brain.tools import ToolRegistry
from app.engine.confirmations import ConfirmationBroker
from app.speech.recorder import Recorder
from app.speech.transcriber import Transcriber
from app.settings import load_shortcut


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "kde-shortcut" / "build" / "misty-shortcut"
WINDOW_SELECTION_TIMEOUT_SECONDS = 60.0
#: How long a pending clarification stays usable for the follow-up answer.
CLARIFICATION_TIMEOUT_SECONDS = 300.0
#: How long set_shortcut waits for the helper's READY line before failing.
SHORTCUT_READY_TIMEOUT_MS = 5000
MAX_TYPED_TEXT_CHARS = 2000
#: A normal (non-confirmation) recording is aborted after this many seconds.
MAX_RECORDING_SECONDS = 120.0
#: Shutdown wait budgets. The Qt thread must never block for long.
SHUTDOWN_TOTAL_BUDGET_SECONDS = 2.8
SHUTDOWN_HELPER_BUDGET_MS = 1200
#: Before whisper runs on RELEASE, the Ministral preload thread is joined for at
#: most this long (the preload keeps running if it is slower).
PRELOAD_JOIN_SECONDS = 3.0


#: Sentinel: no request has started collecting events for the session.
_NO_SESSION_TURN = object()

#: Distinguishes "not computed yet" from a cached None.
_UNSET = object()

#: Event keys a task card may see. Everything else (``details``,
#: ``result``, ``arguments``, ...) is private payload and never forwarded.
_TASK_CARD_EVENT_KEYS = (
    "kind", "tool", "title", "status", "sensitive", "fact_key",
)


def _task_card_event(event):
    """Return a card-facing copy of ``event`` without private payloads."""
    if not isinstance(event, dict):
        return {}
    card_event = {}
    for key in _TASK_CARD_EVENT_KEYS:
        if key in event:
            card_event[key] = event[key]
    if isinstance(card_event.get("status"), str):
        card_event["status"] = _task_card_status(card_event["status"])
    return card_event


def _task_card_status(status):
    """Map the agent's status words onto the card's vocabulary."""
    return {
        "failure": "failed",
        "blocked": "blocked",
        "denied": "failed",
        "skipped": "skipped",
        "cancelled": "cancelled",
    }.get(str(status), str(status))


class TypedAdmission:
    """Admission decision for one typed request (see VoiceController.submit_text)."""

    __slots__ = ("accepted", "reason", "request_id")

    def __init__(self, accepted, reason="", request_id=None):
        self.accepted = bool(accepted)
        self.reason = str(reason or "")
        self.request_id = request_id

    def __bool__(self):
        return self.accepted

    def __iter__(self):
        # Tuple-compatible: ``accepted, reason, request_id = result``.
        return iter((self.accepted, self.reason, self.request_id))

    def __repr__(self):
        return (
            f"TypedAdmission(accepted={self.accepted!r}, "
            f"reason={self.reason!r}, request_id={self.request_id!r})"
        )


def _current_thread():
    return threading.current_thread()


def _is_worker_thread(controller, thread):
    if thread is None or not thread.is_alive():
        return False
    return thread is controller._worker and thread is not controller._start_error_thread


def _stop_if_running(agent):
    """Stop an agent model server only when there is one to stop."""
    if agent is None or getattr(agent, "process", None) is None:
        return
    try:
        agent.stop()
    except Exception as error:
        print(f"[Misty] Ministral cleanup error: {error}", flush=True)


class VoiceController(QObject):
    state_changed = Signal(str)
    transcript_changed = Signal(str)
    response_changed = Signal(str)
    activity_event = Signal(dict)
    confirmation_requested = Signal(dict)
    confirmation_resolved = Signal(bool)
    window_selection_requested = Signal(list)
    window_selection_message_requested = Signal(str)
    window_selection_cleared = Signal()
    ready_reset_requested = Signal()
    escape_sync_requested = Signal()
    #: (success: bool, reason: str) once a helper (re)start has really bound.
    shortcut_ready = Signal(bool, str)
    typed_rejected = Signal(str, str, str)
    typed_failed = Signal(str, str, str)
    typed_completed = Signal(str, str, str)
    #: Task card feed: started/event/finished payloads (see TaskCard).
    task_started = Signal(dict)
    task_event = Signal(dict)
    task_finished = Signal(dict)

    def __init__(self, popup_ipc, parent=None, window_service=None, clipboard_bridge=None, vision_service=None, confirmation_broker=None, audit_log=None, web_search_client=None, knowledge_index=None, task_journal=None, browser_session=None, reminder_store=None, file_transfer_engine=None, control_activator=None, media_controller=None, zip_engine=None, text_search_service=None, directory_transfer_engine=None, calendar_engine=None, duplicate_finder=None, file_transcriber=None, active_window_provider=None, max_recording_seconds=MAX_RECORDING_SECONDS):
        super().__init__(parent)
        if (
            not isinstance(max_recording_seconds, (int, float))
            or isinstance(max_recording_seconds, bool)
            or max_recording_seconds <= 0
        ):
            raise ValueError("max_recording_seconds must be positive")
        self.max_recording_seconds = float(max_recording_seconds)
        self.popup_ipc = popup_ipc
        self.window_service = window_service
        self.clipboard_bridge = clipboard_bridge
        self.vision_service = vision_service
        self.audit_log = audit_log
        self.web_search_client = web_search_client
        self.knowledge_index = knowledge_index
        self.task_journal = task_journal
        self.browser_session = browser_session
        self.reminder_store = reminder_store
        self.file_transfer_engine = file_transfer_engine
        self.control_activator = control_activator
        self.media_controller = media_controller
        self.zip_engine = zip_engine
        self.text_search_service = text_search_service
        self.directory_transfer_engine = directory_transfer_engine
        self.calendar_engine = calendar_engine
        self.duplicate_finder = duplicate_finder
        self.file_transcriber = file_transcriber
        self.active_window_provider = active_window_provider
        self.popup_hovered = False
        self._popup_state = "ready"
        self._popup_transcript = "Your commands will appear here."
        self._popup_response = "I'm ready whenever you are."
        self.memory_store = MemoryStore()
        self.recorder = Recorder()

        self.process = QProcess(self)
        self.process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        self.process.readyReadStandardOutput.connect(
            self._read_helper_output
        )
        self.process.errorOccurred.connect(self._helper_error)
        self.process.finished.connect(self._helper_finished)

        self._buffer = ""
        self._recording = False
        self._confirmation_recording = False
        self._confirmation_request_id = None
        self._confirmation_worker = None
        self._busy = False
        self._shutting_down = False
        self._restarting_helper = False
        self.shortcut = load_shortcut()
        # Two-state shortcut model: the value from disk versus the chord the
        # helper has actually bound (None while unknown/suspended).
        self._shortcut_saved = self.shortcut
        self._shortcut_active = None
        self._shortcut_suspended = False
        self._helper_ready = False
        self._helper_ready_event = threading.Event()
        self._helper_error_reason = ""
        self._shortcut_apply_lock = threading.RLock()

        # Typed requests carry their own identity so a voice request can
        # never inherit their origin (or vice versa).
        self._active_typed_request_id = None
        self._typed_request_text = ""
        self._typed_outcomes = []
        self._pending_clarification = None
        self._clarification_flag = threading.Event()
        # Bounded, private memory of the last few turns (Feature: follow-ups).
        # Only events of the request being recorded are accepted, so a late
        # activity event from a previous turn cannot leak into this one.
        self.session_context = SessionContext(accepts=self._session_accepts)
        self._session_turn = _NO_SESSION_TURN
        # Task card state: the request in flight, its cancellation event and
        # the single failed step that may be replayed through the agent.
        self._task_request = ""
        self._task_event = None
        self._task_failed_step = None
        self._session_events = []
        # Locks are created lazily by the _*_state_lock accessors below, so
        # a controller built without __init__ (tests, fakes) still works.
        self._session_events_lock = None
        self._clarification_lock = None
        self._task_lock = None
        #: Cancellation event of the current request; each request gets one.
        self._task_cancel_event = None
        self._latest_request_id = None
        self._request_origins = {}
        self._request_texts = {}
        # Task card: the task in flight, its request, its cancellation
        # event and the single failed step that may be replayed.
        self._task_request = ""
        self._task_event = None
        self._task_failed_step = None
        self._task_cancel_event = None

        self._transcriber = None
        self._whisper_thread = None
        self._transcriber_factory = Transcriber
        self._agent = None
        self._agent_thread = None
        self._agent_errors = []
        self._agent_busy = False
        self._preload_epoch = 0
        self._worker = None
        self._start_error_thread = None
        self._cleanup_lock = threading.RLock()
        # Recording state machine: a monotonic generation (stale watchdog
        # and stale RELEASE events are ignored), the watchdog timer, and the
        # lock serialising abort against RELEASE/press.
        self._recording_generation = 0
        self._recording_lock = threading.RLock()
        self._watchdog_timer = None

        self._confirmation_broker = (
            confirmation_broker or ConfirmationBroker()
        )
        self._window_selection_lock = threading.Lock()
        self._pending_window_selection = None
        self._pending_window_selection_timer = None
        self._popup_window_selection = None

        self._completion_timer = QTimer(self)
        self._completion_timer.setSingleShot(True)
        self._completion_timer.timeout.connect(self._return_to_ready)
        self._completion_waiting_for_hover = False

        self.popup_ipc.message_received.connect(self._on_popup_message)
        self.state_changed.connect(self._send_state)
        self.escape_sync_requested.connect(self.sync_task_escape)
        self.transcript_changed.connect(self._send_transcript)
        self.response_changed.connect(self._send_response)
        self.window_selection_requested.connect(
            self._send_window_selection
        )
        self.window_selection_message_requested.connect(
            self._send_window_selection_message
        )
        self.window_selection_cleared.connect(
            self._clear_window_selection_ui
        )
        self.ready_reset_requested.connect(self._schedule_ready_reset)

    def _send_state(self, state):
        self._popup_state = state
        self.popup_ipc.send({"type": "set_state", "state": state})

    def _send_transcript(self, text):
        self._popup_transcript = text
        self.popup_ipc.send({"type": "set_transcript", "text": text})

    def _send_response(self, text):
        self._popup_response = text
        self.popup_ipc.send({"type": "set_response", "text": text})

    def _send_window_selection(self, candidates):
        self._popup_window_selection = [
            dict(item) for item in candidates
        ]
        self.popup_ipc.send({
            "type": "window_selection",
            "candidates": self._popup_window_selection,
        })

    def _send_window_selection_message(self, message):
        self.popup_ipc.send({
            "type": "window_selection_message",
            "text": str(message),
        })

    def _clear_window_selection_ui(self):
        self._popup_window_selection = None
        self.popup_ipc.send({"type": "window_selection_clear"})

    def _has_pending_window_selection(self):
        with self._window_selection_lock:
            return self._pending_window_selection is not None

    def _on_popup_message(self, message):
        message_type = message.get("type")

        if message_type == "popup_ready":
            self.popup_hovered = bool(message.get("hovered", False))
            self.popup_ipc.send({
                "type": "shortcut",
                "text": self.shortcut,
            })
            self._send_state(self._popup_state)
            self._send_transcript(self._popup_transcript)
            self._send_response(self._popup_response)
            if self._popup_window_selection is not None:
                self.popup_ipc.send({
                    "type": "window_selection",
                    "candidates": self._popup_window_selection,
                })

        elif message_type == "hover_changed":
            self._handle_popup_hover_changed(
                bool(message.get("hovered", False))
            )

    def start(self):
        if not HELPER.is_file():
            raise FileNotFoundError(
                f"KDE shortcut helper not found: {HELPER}"
            )

        self.recorder.prepare()
        self._shortcut_launch_helper(self.shortcut)

        if not self.process.waitForStarted(3000):
            raise RuntimeError(
                f"Could not start shortcut helper: "
                f"{self.process.errorString()}"
            )

        if not self._shortcut_wait_until_ready(SHORTCUT_READY_TIMEOUT_MS):
            reason = self._helper_error_reason or (
                "the shortcut helper did not report READY in time"
            )
            raise RuntimeError(f"Could not bind shortcut: {reason}")

        print(
            f"[Misty] Voice controller ready. Hold {self.shortcut} to talk.",
            flush=True,
        )

    def set_shortcut(self, sequence):
        """Bind ``sequence`` for real, restarting the helper if needed.

        Returns True only when the helper confirmed the chord with a READY
        line. Launching the process, or the helper being NotRunning, is not
        success. On success ``shortcut``/``_shortcut_active`` both move to the
        new chord; on failure the previous chord is restored.
        """
        accepted, reason = self._shortcut_validate(sequence)
        if not accepted:
            self._shortcut_report(False, reason or "invalid shortcut")
            return False
        sequence = self._shortcut_normalize(sequence)
        # Any rebind request means the chord may change under a held key:
        # stop and discard the recording before the helper is touched.
        self._abort_recording(
            f"Recording stopped: the shortcut changed to {sequence}."
        )
        with self._shortcut_apply_lock:
            previous = self.shortcut
            if (
                not self._shutting_down
                and self._helper_ready
                and sequence == self._shortcut_active
            ):
                # Already bound: nothing to do.
                self._shortcut_publish(sequence)
                self._shortcut_report(True, "")
                return True

            print(
                f"[Misty] Rebinding voice shortcut to {sequence}.",
                flush=True,
            )
            self._shortcut_suspended = False
            try:
                self._shortcut_restart(sequence)
            except Exception as error:
                reason = str(error) or "shortcut helper failed to start"
            else:
                if self._helper_ready_event.wait(
                    SHORTCUT_READY_TIMEOUT_MS / 1000.0
                ):
                    self._shortcut_publish(sequence)
                    self._shortcut_report(True, "")
                    return True
                reason = self._helper_error_reason or (
                    "the shortcut helper did not report READY in time"
                )

            print(
                f"[Misty] Shortcut {sequence} was not applied: {reason}",
                flush=True,
            )
            self._shortcut_publish(previous)
            # Roll back: bring the previously saved chord back up.
            if not self._shutting_down:
                try:
                    self._shortcut_restart(previous)
                    if not self._helper_ready_event.wait(
                        SHORTCUT_READY_TIMEOUT_MS / 1000.0
                    ):
                        print(
                            "[Misty] Previous shortcut did not confirm "
                            "READY either.",
                            flush=True,
                        )
                except Exception as error:
                    print(
                        f"[Misty] Could not restore shortcut {previous}: "
                        f"{error}",
                        flush=True,
                    )
            self._shortcut_report(False, reason)
            return False

    def _shortcut_validate(self, sequence):
        if not isinstance(sequence, str):
            return False, "shortcut must be text"
        text = sequence.strip()
        if not text:
            return False, "shortcut is empty"
        if len(text) > 64:
            return False, "shortcut is longer than 64 characters"
        if "," in text:
            return False, (
                "KDE global shortcuts support one chord, not a sequence"
            )
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            return False, "shortcut contains control characters"
        return True, ""

    def _shortcut_normalize(self, sequence):
        try:
            from app.settings import normalize_shortcut

            return normalize_shortcut(sequence.strip())
        except Exception:
            return sequence.strip()

    def _shortcut_report(self, success, reason=""):
        try:
            self.shortcut_ready.emit(bool(success), str(reason or ""))
        except Exception:
            pass

    def _shortcut_publish(self, sequence):
        """Make ``sequence`` the configured chord and tell the popup."""
        self.shortcut = sequence
        self.popup_ipc.send({
            "type": "shortcut",
            "text": sequence,
        })

    def _shortcut_launch_helper(self, sequence):
        """(Re)launch the helper process with ``sequence`` and reset READY."""
        self._helper_ready = False
        self._helper_ready_event.clear()
        self._helper_error_reason = ""
        self._buffer = ""
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("MISTY_SHORTCUT", sequence)
        self.process.setProcessEnvironment(environment)
        self.process.start(str(HELPER), [])

    def _shortcut_stop_helper(self):
        """Terminate the helper without the restart notice."""
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        was_restarting = self._restarting_helper
        self._restarting_helper = True
        try:
            self.process.terminate()
            if not self.process.waitForFinished(2000):
                self.process.kill()
                self.process.waitForFinished(1000)
        finally:
            self._restarting_helper = was_restarting

    def _shortcut_restart(self, sequence):
        with self._shortcut_apply_lock:
            self._restarting_helper = True
            try:
                self._shortcut_stop_helper()
                self._shortcut_launch_helper(sequence)
                if not self.process.waitForStarted(3000):
                    raise RuntimeError(
                        f"Could not start shortcut helper: "
                        f"{self.process.errorString()}"
                    )
                # Pump the event loop briefly so the READY line (or an
                # immediate exit) is observed without blocking the wait below.
                self._helper_ready_event.wait(0)
                self._shortcut_pump(100)
            finally:
                self._restarting_helper = False

    @staticmethod
    def _shortcut_pump(milliseconds):
        """Process pending events for at most ``milliseconds``."""
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(int(milliseconds))
        loop.exec()
        timer.stop()

    def _shortcut_wait_until_ready(self, timeout_ms, pump=True):
        """Wait for READY, processing Qt events so the signal can arrive."""
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while True:
            if self._helper_ready:
                return True
            if self._helper_error_reason:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._helper_ready
            if pump and not self._restarting_helper:
                self._shortcut_pump(min(50, int(remaining * 1000)))
            elif self._helper_ready_event.wait(min(remaining, 0.05)):
                return True

    def suspend_shortcut(self):
        """Stop the global binding (e.g. while the settings UI records a key)."""
        with self._shortcut_apply_lock:
            self._abort_recording(
                "Recording stopped: the shortcut is being changed."
            )
            self._shortcut_suspended = True
            self._shortcut_stop_helper()
            self._helper_ready = False
            self._helper_ready_event.clear()
            self._helper_error_reason = ""
            self._shortcut_active = None
        print("[Misty] Global shortcut suspended.", flush=True)

    def resume_shortcut(self):
        """Re-bind the saved chord after a suspension."""
        with self._shortcut_apply_lock:
            if not self._shortcut_suspended or self._shutting_down:
                return False
            self._shortcut_suspended = False
        return self.set_shortcut(self._shortcut_saved)

    def shortcut_state(self):
        """Return the saved vs active shortcut view for the settings UI."""
        active = self._shortcut_active if self._helper_ready else None
        if self._shortcut_suspended:
            status = "suspended"
        elif active is None:
            status = "inactive"
        elif self._shortcut_same(active, self._shortcut_saved):
            status = "active"
        else:
            status = "other"
        return {
            "saved": self._shortcut_saved,
            "active": active,
            "suspended": self._shortcut_suspended,
            "status": status,
            "needs_apply": status in {"inactive", "other"},
        }

    @staticmethod
    def _shortcut_same(left, right):
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        return left.strip().casefold() == right.strip().casefold()

    def current_request_origin(self):
        """Origin of the request in flight (or just finished): typed/voice."""
        request_id = self._latest_request_id
        if request_id is None:
            return None
        return self._request_origins.get(request_id)

    def _mark_request_origin(self, request_id, origin, text):
        """Record once so later origin reads cannot be rewritten."""
        if not request_id:
            return
        self._request_origins[request_id] = origin
        self._latest_request_id = request_id
        self._request_texts[request_id] = text
        while len(self._request_origins) > 8:
            oldest = next(iter(self._request_origins))
            self._request_origins.pop(oldest, None)
            self._request_texts.pop(oldest, None)

    def _read_helper_output(self):
        self._buffer += bytes(
            self.process.readAllStandardOutput()
        ).decode(errors="replace")

        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            event = line.strip()

            if event == "PRESS":
                self._press()
            elif event == "RELEASE":
                self._release()
            elif event == "CANCEL":
                self.cancel_active_request()
            elif event.startswith("READY"):
                self._helper_ready = True
                sequence = event[len("READY"):].strip()
                if sequence:
                    self.shortcut = self._shortcut_normalize(sequence)
                    self._shortcut_active = self.shortcut
                else:
                    self._shortcut_active = self.shortcut
                self._helper_ready_event.set()
                self._notify_escape_state()
                print(f"[Misty] {event}", flush=True)
            elif event.startswith("ERROR"):
                code = event[len("ERROR"):].strip() or "unknown"
                self._helper_error_reason = (
                    f"the shortcut is not available (helper code {code})"
                )
                self._helper_ready = False
                self._helper_ready_event.clear()
                print(f"[Misty] {event}", flush=True)
            elif event:
                print(f"[Misty] {event}", flush=True)

    def _helper_error(self, error):
        self._helper_lost("shortcut listener error", notify=False)
        if self._restarting_helper or self._shortcut_suspended:
            return
        self._helper_ready = False
        if not self._helper_error_reason:
            self._helper_error_reason = (
                f"shortcut helper error: {self.process.errorString()}"
            )
        if not self._shutting_down:
            print(
                f"[Misty] Shortcut helper error: "
                f"{self.process.errorString()}",
                flush=True,
            )
            self.state_changed.emit("error")
            self.response_changed.emit(
                "The keyboard shortcut listener encountered an error."
            )

    def _helper_finished(self, exit_code, exit_status):
        self._helper_lost(
            (
                f"shortcut listener stopped (code {exit_code})"
                if exit_code not in (None, 0)
                else "shortcut listener stopped"
            ),
            notify=False,
        )
        self._helper_ready = False
        if self._restarting_helper or self._shortcut_suspended:
            return
        if exit_code not in (0, None) and not self._helper_error_reason:
            self._helper_error_reason = (
                f"the shortcut helper exited with code {exit_code}"
            )
        if not self._shutting_down:
            print(
                f"[Misty] Shortcut helper stopped (code {exit_code}).",
                flush=True,
            )
            self.state_changed.emit("error")
            self.response_changed.emit(
                "The keyboard shortcut listener stopped."
            )

    # ------------------------------------------------------------------
    # Recording state machine and abort path
    # ------------------------------------------------------------------

    def _acquire_recording(self):
        """Bump the generation and start the watchdog for one recording."""
        self._recording_generation += 1
        self._cancel_watchdog()
        timer = threading.Timer(
            self.max_recording_seconds,
            self._recording_watchdog,
            args=(self._recording_generation,),
        )
        timer.daemon = True
        self._watchdog_timer = timer
        timer.start()

    def _finish_recording(self):
        """End an owned recording (RELEASE, abort, shutdown)."""
        self._recording_generation += 1
        self._cancel_watchdog()

    def _cancel_watchdog(self):
        timer = self._watchdog_timer
        self._watchdog_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _recording_watchdog(self, generation):
        # Runs on a plain thread: never touch Qt widgets or QTimer here.
        if generation != self._recording_generation:
            return
        self._abort_recording(
            f"Recording stopped after {self.max_recording_seconds:g} "
            "seconds. Release the shortcut, then try again."
        )

    def _helper_lost(self, reason, notify=True):
        """The helper can no longer deliver RELEASE: stop any held recording."""
        if self._recording or self._confirmation_recording:
            self._abort_recording(
                f"Recording stopped: {reason}. "
                "Fix the shortcut and try again."
            )
        elif notify:
            self._abort_recording(reason)

    def _abort_recording(self, reason):
        """Stop and discard an active recording; emit state+response once.

        Safe from any thread (the watchdog timer calls it). Returns True when
        a recording was actually aborted.
        """
        with self._recording_lock:
            active = self._recording or self._confirmation_recording
            confirmation = self._confirmation_recording
            self._recording = False
            self._confirmation_recording = False
            self._confirmation_request_id = None
            self._finish_recording()
            if not active:
                return False
            try:
                self.recorder.discard()
            except Exception as error:
                print(f"[Misty] Recording discard error: {error}", flush=True)
            if confirmation:
                # The pending confirmation is still answerable with the
                # dialog buttons; only the spoken path is aborted.
                message = (
                    "I didn't hear a confirmation. Please use the dialog "
                    "buttons."
                )
            else:
                message = str(reason)
                # A press that never started the request worker left
                # ``_busy`` set; clear it so the next attempt is accepted.
                self._busy = False
        print(f"[Misty] Recording aborted: {reason}", flush=True)
        if self._shutting_down:
            return True
        self.activity_event.emit({
            "kind": "response",
            "title": "Recording stopped",
            "details": message,
            "status": "info",
        })
        if confirmation:
            # The request is still waiting; an aborted spoken answer is not
            # an idle controller or a terminal task failure.
            self.state_changed.emit("confirmation")
        else:
            self.state_changed.emit("error")
        self.response_changed.emit(message)
        self._notify_escape_state()
        if not confirmation and not self._has_pending_window_selection():
            self.ready_reset_requested.emit()
        return True

    def _agent_activity_event(self, event):
        if (
            isinstance(event, dict)
            and event.get("title") == "Clarification needed"
            and isinstance(event.get("details"), str)
            and event["details"].strip()
        ):
            # The agent answered with a question instead of acting. Mark it
            # so the worker can remember the turn for a context follow-up.
            self._clarification_flag.set()
        self._session_observe_event(event)
        self._task_card_observe_event(event)
        self._task_card_forward(event)
        self.activity_event.emit(event)

    def _typed_clarification_context(self, text):
        """Consume a pending clarification for ``text`` (see MistyAgent.run).

        Returns ``(context_or_None, original_transcript, question)``. The
        mapping matches ``MistyAgent.run(transcript, context=...)``:
        ``{"transcript": <original request>, "question": <asked question>}``.
        Plain ``a``/``1st``-style selection answers are not clarification
        answers and pass through untouched.
        """
        pending, original, question = self._clarification_take(text)
        if pending is None:
            return None, None, None
        return {
            "transcript": pending["transcript"],
            "question": pending["question"],
        }, original, question

    def _clarification_take(self, text):
        """Return and consume the raw pending clarification, if usable.

        Returns ``(pending_or_None, original_transcript, question)`` where
        ``pending`` is the stored ``{"transcript", "question", "created"}``
        mapping. Expired, already-answered and selection answers consume
        nothing (or clear it) exactly like the clarification contract.
        """
        with self._clarification_state_lock():
            pending = self._session_state("_pending_clarification", None)
            if not pending:
                return None, None, None
            if time.monotonic() - pending["created"] > (
                CLARIFICATION_TIMEOUT_SECONDS
            ):
                self._pending_clarification = None
                print(
                    "[Misty] Pending clarification expired; starting fresh.",
                    flush=True,
                )
                return None, None, None
            if self._typed_is_selection_answer(text):
                self._pending_clarification = None
                return None, None, None
            self._pending_clarification = None
            return pending, pending["transcript"], pending["question"]

    @staticmethod
    def _typed_is_selection_answer(text):
        import re

        normalized = re.sub(r"[^a-z0-9]", "", str(text).casefold())
        return bool(normalized) and normalized in {
            "1", "2", "3", "one", "two", "three",
            "first", "second", "third", "cancel",
        }

    def _typed_remember_clarification(self, text, answer):
        if not self._clarification_flag.is_set() or self._has_pending_window_selection():
            return
        question = str(answer or "").strip()
        if not question:
            return
        with self._clarification_state_lock():
            # Bound what a follow-up can smuggle into the planner prompt.
            self._pending_clarification = {
                "transcript": str(text)[:2000],
                "question": question[:500],
                "created": time.monotonic(),
            }
        print("[Misty] Clarification pending for the next answer.", flush=True)

    @staticmethod
    def _typed_supports_context(agent):
        try:
            import inspect

            signature = inspect.signature(agent.run)
        except (TypeError, ValueError):
            return False
        parameter = signature.parameters.get("context")
        return parameter is not None and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )

    def _typed_run_agent(self, agent, text):
        """Run the agent with the previous turn's context.

        ``context`` carries the pending clarification in its documented
        flat form and the bounded session context beside it, exactly as
        ``MistyAgent.run`` reads them::

            {"transcript": <original request>, "question": <asked question>,
             "session": {"recent_turns": [...], "last_facts": {...}, ...}}

        ``session`` is absent when there is nothing yet; ``transcript`` and
        ``question`` are absent when no clarification is pending. Agents
        whose ``run`` has no ``context`` parameter get the clarification
        folded into the transcript, as before. The turn is recorded only
        once the run really answered.
        """
        pending, original, question = self._clarification_take(text)
        self._clarification_flag.clear()
        session = self._session_context_payload()
        try:
            answer = self._session_invoke_agent(
                agent, text, pending, session, original, question
            )
        except Exception:
            self._session_discard_turn()
            raise
        self._typed_remember_clarification(text, answer)
        self._session_record_turn(text, answer)
        return answer

    def _session_invoke_agent(
        self, agent, text, pending, session, original, question
    ):
        """Call ``agent.run`` with the context shape it accepts."""
        if self._typed_supports_context(agent):
            context = None
            if pending is not None:
                context = {
                    "transcript": pending["transcript"],
                    "question": pending["question"],
                }
            if session:
                context = dict(context or {})
                context["session"] = session
            return agent.run(text, context=context)
        if pending is not None:
            return agent.run(
                f"Original request: {original}\n"
                f"Clarification asked: {question}\n"
                f"User answer: {text}"
            )
        return agent.run(text)

    # ------------------------------------------------------------------
    # Session context (follow-ups such as "open it" / "the one from May")
    # ------------------------------------------------------------------

    # Hand-built controllers (tests, fakes) may lack the task/session locks,
    # so each is fetched through a tiny accessor that creates it once.

    def _task_state_lock(self):
        return self._state_lock("_task_lock")

    def _clarification_state_lock(self):
        return self._state_lock("_clarification_lock")

    def _session_events_state_lock(self):
        return self._state_lock("_session_events_lock")

    def _state_lock(self, name):
        lock = self.__dict__.get(name)
        if lock is None:
            lock = threading.RLock()
            try:
                object.__setattr__(self, name, lock)
            except Exception:
                pass
        return lock

    def _session_context_payload(self):
        """Compact context for the planner; never raises."""
        context = getattr(self, "session_context", None)
        getter = getattr(context, "next_context", None)
        if not callable(getter):
            return None
        try:
            value = getter()
        except Exception:
            return None
        if not isinstance(value, dict) or not value:
            return None
        # The session block is never itself a permission: it is the planner
        # context only, and the agent re-sanitises it on the way in.
        return value

    def _session_state(self, name, default):
        """Read one task/session field, tolerating a hand-built controller."""
        return self.__dict__.get(name, default)

    def _session_accepts(self, event):
        """True while the event belongs to the request being recorded."""
        if not isinstance(event, dict):
            return False
        with self._session_events_state_lock():
            return self._session_state(
                "_session_turn", _NO_SESSION_TURN
            ) is not _NO_SESSION_TURN

    def _session_observe_event(self, event):
        """Keep agent activity events so the finished turn can be recorded."""
        if not isinstance(event, dict):
            return
        with self._session_events_state_lock():
            if self._session_state("_session_turn", _NO_SESSION_TURN) is (
                _NO_SESSION_TURN
            ):
                return
            events = self._session_state("_session_events", None)
            if events is None:
                events = self.__dict__["_session_events"] = []
            events.append(dict(event))
            if len(events) > 200:
                del events[:-200]

    def _session_begin_turn(self, text):
        """Start collecting events for one request."""
        context = self.__dict__.get("session_context")
        if context is None:
            context = SessionContext(accepts=self._session_accepts)
            self.__dict__["session_context"] = context
        with self._session_events_state_lock():
            self._session_turn = str(text)[:2000]
            self._session_events = []
        begin = getattr(context, "begin_turn", None)
        if callable(begin):
            try:
                begin(str(text))
            except Exception:
                pass

    def _session_take_events(self):
        with self._session_events_state_lock():
            events = list(self._session_state("_session_events", []))
            self._session_events = []
            self._session_turn = _NO_SESSION_TURN
        return events

    def _session_discard_turn(self):
        self._session_take_events()

    def _session_record_turn(self, text, answer):
        """Record the finished turn; never raises into the request path."""
        events = self._session_take_events()
        context = getattr(self, "session_context", None)
        record = getattr(context, "record_turn", None)
        if not callable(record):
            return
        try:
            record(text, answer, events=events)
        except Exception as error:
            print(f"[Misty] Session context error: {error}", flush=True)
        # The session is now visible; the next turn may refer to it.

    def clear_session_context(self):
        """Forget the conversation context (Activity page / Settings)."""
        context = getattr(self, "session_context", None)
        clear = getattr(context, "clear", None)
        if callable(clear):
            try:
                clear()
            except Exception as error:
                print(
                    f"[Misty] Session context clear error: {error}",
                    flush=True,
                )
        self._session_take_events()
        with self._clarification_state_lock():
            self._pending_clarification = None
        self._clarification_flag.clear()
        return True

    def forget_conversation_context(self):
        """User-facing alias of :meth:`clear_session_context`."""
        return self.clear_session_context()

    def _session_shutdown_clear(self):
        """Shutdown hook: forget the context; never raises."""
        try:
            self.clear_session_context()
        except Exception:
            pass

    def _typed_emit_outcome(self, signal, request_id, text, detail):
        """Emit a typed outcome once per request ID (reject/fail/complete)."""
        if request_id is None or request_id in self._typed_outcomes:
            return
        self._typed_outcomes.append(request_id)
        if len(self._typed_outcomes) > 32:
            del self._typed_outcomes[:-16]
        try:
            signal.emit(request_id, text, detail)
        except Exception:
            pass

    def _request_cancelled(self):
        """Read the admitted owner's cancellation state, including pre-worker input."""
        with self._task_state_lock():
            event = self._session_state("_task_cancel_event", None)
        voice_event = getattr(self, "_voice_cancel_event", None)
        return bool((event is not None and event.is_set())
                    or (voice_event is not None and voice_event.is_set()))

    def _request_confirmation(self, name, arguments, description):
        if self._shutting_down or self._request_cancelled():
            return False

        pending = self._confirmation_broker.begin(
            name, arguments, description
        )
        if pending is None:
            return False
        if self._shutting_down or self._request_cancelled():
            # Cancel may have arrived between the first check and begin().
            # Resolve only this admitted prompt; never a newer broker owner.
            self._confirmation_broker.resolve(False, pending["request_id"])
            return False

        self.activity_event.emit({
            "kind": "confirmation",
            "title": f"Approval requested: {name}",
            # Native window/target labels belong in the ephemeral approval UI,
            # not in persistent activity history. Do not weaken the prompt.
            "details": (
                "Review the private native-window request in the confirmation dialog."
                if name in {"kwin_click", "kwin_observe"} else description
            ),
            "status": "pending",
        })
        self.state_changed.emit("confirmation")
        self.confirmation_requested.emit({
            "request_id": pending["request_id"],
            "name": name,
            "arguments": self._confirmation_arguments(name, arguments),
            "description": description,
        })

        approved = self._confirmation_broker.wait(pending)
        # A button approval racing Cancel cannot revive a cancelled owner.
        approved = approved and not self._shutting_down and not self._request_cancelled()
        with self._recording_lock:
            # Serialize with recorder.start(): a prompt resolving while the
            # audio device opens must not leave an orphan recording.
            abort_answer = (self._confirmation_recording
                            and self._confirmation_request_id == pending["request_id"])
        if abort_answer:
            self._abort_recording("The confirmation prompt closed.")

        self.confirmation_resolved.emit(approved)
        if not self._shutting_down:
            # The request owner resumes after approval/denial/timeout. Voice
            # decoding workers must not publish a later stale thinking state.
            self.state_changed.emit("thinking")
        self.activity_event.emit({
            "kind": "confirmation",
            "title": (
                f"Action approved: {name}" if approved
                else f"Action not approved: {name}"
            ),
            "details": (
                "User explicitly approved this action."
                if approved else
                "Denied, timed out, or application shut down. No action executed."
            ),
            "status": "approved" if approved else "denied",
        })
        return approved

    @staticmethod
    def _confirmation_arguments(name, arguments):
        """Return confirmation-safe argument details, never private content."""
        if not isinstance(arguments, dict):
            return {}

        if name == "write_text_file":
            content = arguments.get("content")
            return {
                "folder": arguments.get("folder"),
                "relative_path": arguments.get("relative_path"),
                "content_chars": (
                    len(content) if isinstance(content, str) else 0
                ),
                "overwrite": arguments.get("overwrite") is True,
            }

        if name == "make_directory":
            return {
                "folder": arguments.get("folder"),
                "relative_path": arguments.get("relative_path"),
                "parents": arguments.get("parents") is True,
            }

        if name == "trash_path":
            return {
                "folder": arguments.get("folder"),
                "relative_path": arguments.get("relative_path"),
                "recursive": arguments.get("recursive") is True,
            }

        if name == "write_clipboard":
            text = arguments.get("text")
            return {
                "text_chars": len(text) if isinstance(text, str) else 0,
            }

        return dict(arguments)

    def pending_confirmation_id(self):
        """Return the request ID of the pending confirmation, if any."""
        return self._confirmation_broker.current_request_id()

    def resolve_confirmation(self, approved, request_id):
        """Resolve exactly one confirmation request by its request ID.

        A missing, empty, or mismatched ID never resolves anything. Internal
        failure paths that must deny whatever is pending use
        ``_confirm_pending(False)`` instead.
        """
        if not isinstance(request_id, str) or not request_id.strip():
            print(
                "[Misty] Confirmation response ignored: no request ID.",
                flush=True,
            )
            return False

        if self._shutting_down or self._request_cancelled():
            approved = False

        resolved = self._confirmation_broker.resolve(
            approved, request_id
        )
        if resolved:
            self.confirmation_resolved.emit(bool(approved))
        return resolved

    # ------------------------------------------------------------------
    # Model arbitration (one heavy local model at a time)
    # ------------------------------------------------------------------

    def model_arbiter(self):
        """The single-owner arbiter for the heavy local models.

        Misty runs the reasoning model, the vision model and whisper.cpp one
        at a time on a normal desktop. ``app.brain.model_lease`` decides which
        resource may be loaded and calls the registered load/unload
        callbacks; this controller supplies those callbacks and keeps the
        existing ownership rules (never stop a model another live request is
        using).

        The arbiter is created on first use so a controller built without the
        services (tests, fakes) behaves exactly as before.
        """
        arbiter = self.__dict__.get("_model_arbiter")
        if arbiter is not None:
            return arbiter
        with self._model_arbiter_lock():
            arbiter = self.__dict__.get("_model_arbiter")
            if arbiter is not None:
                return arbiter
            from app.brain.model_lease import ModelLease

            arbiter = ModelLease()
            self._register_model_resources(arbiter)
            self._model_arbiter = arbiter
            return arbiter

    def _model_arbiter_lock(self):
        lock = self.__dict__.get("_model_arbiter_guard")
        if lock is None:
            lock = threading.RLock()
            self._model_arbiter_guard = lock
        return lock

    def _register_model_resources(self, arbiter):
        """Register what this controller can actually load."""
        arbiter.register(
            "reasoning",
            lambda: self._load_reasoning_for_lease(),
            lambda: self._unload_reasoning_for_lease(),
        )

    def _load_reasoning_for_lease(self):
        """Load the chat model for a lease; True only when it really is up."""
        agent = self._agent
        if agent is None:
            return False
        start = getattr(agent, "start", None)
        if not callable(start):
            return False
        try:
            start()
        except Exception as error:
            print(f"[Misty] Model load error: {error}", flush=True)
            return False
        return getattr(agent, "process", None) is not None

    def _unload_reasoning_for_lease(self):
        """Unload the chat model for a lease, honouring request ownership."""
        agent = self._agent
        if agent is None:
            return True
        worker = self.__dict__.get("_worker")
        current = threading.current_thread()
        if (
            worker is not None
            and current is not worker
            and self._worker_alive(exclude=current)
        ):
            # Another live request still owns the model.
            return False
        stop = getattr(agent, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception as error:
                print(f"[Misty] Model unload error: {error}", flush=True)
                return False
        return getattr(agent, "process", None) is None

    def _release_arbiter_leases(self, name):
        """Drop any lease this controller holds for ``name``.

        The vision and transcription hooks unload the chat model directly.
        Without this the arbiter would still believe the resource is loaded
        and would skip a later unload. Only tokens handed out by
        :meth:`hold_model` are tracked; a token is released at most once.
        """
        arbiter = self.__dict__.get("_model_arbiter")
        held = self.__dict__.get("_model_tokens")
        if arbiter is None or not held:
            return False
        token = held.pop(name, None)
        if token is None:
            return False
        try:
            return bool(arbiter.release(token).ok)
        except Exception as error:
            print(f"[Misty] Arbiter sync error: {error}", flush=True)
            return False

    def hold_model(self, name, timeout=None):
        """Acquire ``name`` for the calling thread; return the LeaseResult.

        The caller must ``release`` the result (or use it as a context
        manager). Zero-argument load/unload callbacks come from this
        controller, so nothing private is ever passed to the arbiter.
        """
        arbiter = self.model_arbiter()
        from app.brain.model_lease import DEFAULT_TIMEOUT

        result = arbiter.acquire(
            name, DEFAULT_TIMEOUT if timeout is None else timeout
        )
        if result.ok and result.token is not None:
            held = self.__dict__.get("_model_tokens")
            if held is None:
                held = {}
                self._model_tokens = held
            held[name] = result.token
        return result

    def model_state(self):
        """JSON-safe snapshot of the arbiter, for diagnostics."""
        arbiter = self.__dict__.get("_model_arbiter")
        return {} if arbiter is None else arbiter.state()

    def set_before_transcribe(self, callback):
        """Install a pre-transcription hook on the file transcriber.

        Wired by the app to release Ministral before whisper.cpp runs, so the two
        models never share GPU memory. Safe with no transcriber.
        """
        transcriber = self.file_transcriber
        setter = getattr(transcriber, "set_before_transcribe", None)
        if callable(setter):
            setter(callback)
            return True
        return False

    def _confirm_pending(self, approved):
        """Internal denial path for whatever is pending (no external ID).

        Used only for in-process failures (shutdown, popup unavailable), which
        are always denials; approvals must name a request ID.
        """
        if approved:
            raise ValueError("Approval requires an explicit request ID.")

        resolved = self._confirmation_broker.cancel()
        if resolved:
            self.confirmation_resolved.emit(False)
        return resolved

    def _set_pending_content_selection(self, candidates):
        """Offer numbered content choices; the next utterance opens one."""
        copied = [
            {
                "title": str(item.get("title", "Untitled"))[:200],
                "url": str(item.get("url", ""))[:2048],
            }
            for item in candidates[:3]
            if isinstance(item, dict) and item.get("url")
        ]
        if not copied:
            return
        pending = {
            "kind": "content",
            "action": "open_url",
            "candidates": copied,
            "arguments": {},
            "expires_at": time.monotonic() + WINDOW_SELECTION_TIMEOUT_SECONDS,
        }
        with self._window_selection_lock:
            if self._pending_window_selection_timer is not None:
                self._pending_window_selection_timer.cancel()
            self._pending_window_selection = pending
            timer = threading.Timer(
                WINDOW_SELECTION_TIMEOUT_SECONDS,
                self._expire_pending_window_selection,
                args=(pending,),
            )
            timer.daemon = True
            self._pending_window_selection_timer = timer
            timer.start()
        print(
            "[Misty] Content selection created: "
            f"{[item['title'] for item in copied]}",
            flush=True,
        )
        self.window_selection_requested.emit(copied)

    def _set_pending_window_selection(self, action_name, candidates, extra_arguments=None):
        copied_candidates = [dict(item) for item in candidates]
        pending = {
            "kind": "window",
            "action": action_name,
            "candidates": copied_candidates,
            "arguments": dict(extra_arguments or {}),
            "expires_at": time.monotonic() + WINDOW_SELECTION_TIMEOUT_SECONDS,
        }

        with self._window_selection_lock:
            if self._pending_window_selection_timer is not None:
                self._pending_window_selection_timer.cancel()

            self._pending_window_selection = pending
            timer = threading.Timer(
                WINDOW_SELECTION_TIMEOUT_SECONDS,
                self._expire_pending_window_selection,
                args=(pending,),
            )
            timer.daemon = True
            self._pending_window_selection_timer = timer
            timer.start()

        titles = [item.get("title", "Untitled") for item in copied_candidates]
        print(
            f"[Misty] Pending window selection created: "
            f"action={action_name}, candidates={titles}, timeout=60s",
            flush=True,
        )
        self.window_selection_requested.emit(copied_candidates)

    def _expire_pending_window_selection(self, pending):
        with self._window_selection_lock:
            if self._pending_window_selection is not pending:
                return

            remaining = pending["expires_at"] - time.monotonic()
            if remaining > 0:
                timer = threading.Timer(
                    remaining,
                    self._expire_pending_window_selection,
                    args=(pending,),
                )
                timer.daemon = True
                self._pending_window_selection_timer = timer
                timer.start()
                return

            self._pending_window_selection = None
            self._pending_window_selection_timer = None

        print("[Misty] Pending selection expired after 60 seconds.", flush=True)
        self.window_selection_cleared.emit()
        self.activity_event.emit({
            "kind": "response",
            "title": "Window selection expired",
            "details": "No valid choice was received within 60 seconds.",
            "status": "info",
        })
        self.response_changed.emit(
            "That selection expired. Please repeat your original request."
        )
        self.state_changed.emit("complete")
        self.ready_reset_requested.emit()

    def _refresh_pending_window_selection(self, pending, transcript, reason):
        with self._window_selection_lock:
            if self._pending_window_selection is not pending:
                return

            if self._pending_window_selection_timer is not None:
                self._pending_window_selection_timer.cancel()

            pending["expires_at"] = (
                time.monotonic() + WINDOW_SELECTION_TIMEOUT_SECONDS
            )
            timer = threading.Timer(
                WINDOW_SELECTION_TIMEOUT_SECONDS,
                self._expire_pending_window_selection,
                args=(pending,),
            )
            timer.daemon = True
            self._pending_window_selection_timer = timer
            timer.start()

        print(
            f"[Misty] Window selection not resolved: {reason}; "
            f"transcript={transcript!r}. Timeout refreshed to 60s.",
            flush=True,
        )

    def _clear_pending_window_selection(self, pending=None):
        with self._window_selection_lock:
            if (
                pending is not None
                and self._pending_window_selection is not pending
            ):
                return

            self._pending_window_selection = None
            if self._pending_window_selection_timer is not None:
                self._pending_window_selection_timer.cancel()
                self._pending_window_selection_timer = None

    def _handle_pending_window_selection(self, transcript):
        """Resolve a spoken follow-up locally, without asking Ministral."""
        with self._window_selection_lock:
            pending = self._pending_window_selection

        if pending is None:
            print(
                "[Misty] Follow-up received, but no window selection is pending.",
                flush=True,
            )
            return None

        if time.monotonic() >= pending["expires_at"]:
            self._clear_pending_window_selection(pending)
            print("[Misty] Follow-up arrived after the selection expired.", flush=True)
            return "That selection expired. Please repeat your original request."

        # Pause the timer while this answer is being resolved. It will be
        # restarted if the answer is not a valid choice.
        with self._window_selection_lock:
            if self._pending_window_selection is not pending:
                return None
            if self._pending_window_selection_timer is not None:
                self._pending_window_selection_timer.cancel()
                self._pending_window_selection_timer = None

        print(
            f"[Misty] Resolving follow-up {transcript!r} against "
            f"{[item.get('title', 'Untitled') for item in pending['candidates']]}",
            flush=True,
        )

        import re

        def normalize(value):
            return re.sub(r"[^a-z0-9]", "", str(value).casefold())

        phrase = normalize(transcript)

        if phrase in {
            "cancel", "cancelit", "nevermind", "forgetit", "stop",
            "anuluj", "zrezygnuj", "niewane", "odwoaj",
            "nieteraz", "przerwij",
        }:
            self._clear_pending_window_selection(pending)
            print("[Misty] User cancelled the pending window selection.", flush=True)
            return "Okay, I cancelled the selection."

        # Check ordinal words before cardinal words so "second one"
        # resolves to the second candidate, not the first.
        ordinal_words = {
            "first": 1, "1st": 1,
            "second": 2, "2nd": 2,
            "third": 3, "3rd": 3,
            "fourth": 4, "4th": 4,
            "fifth": 5, "5th": 5,
            "sixth": 6, "6th": 6,
            "seventh": 7, "7th": 7,
            "eighth": 8, "8th": 8,
            "ninth": 9, "9th": 9,
            "tenth": 10, "10th": 10,
            # Polish ordinal forms (all common grammatical genders).
            "pierwszy": 1, "pierwsza": 1, "pierwsze": 1,
            "pierwszego": 1, "pierwszej": 1, "pierwszym": 1,
            "pierwszą": 1,
            "drugi": 2, "druga": 2, "drugie": 2,
            "drugiego": 2, "drugiej": 2, "drugim": 2,
            "drugą": 2,
            "trzeci": 3, "trzecia": 3, "trzecie": 3,
            "trzeciego": 3, "trzeciej": 3, "trzecim": 3,
            "trzecią": 3,
            "czwarty": 4, "czwarta": 4, "czwarte": 4,
            "czwartego": 4, "czwartej": 4, "czwartym": 4,
            "czwartą": 4,
            "piąty": 5, "piąta": 5, "piąte": 5,
            "piątego": 5, "piątej": 5, "piątym": 5,
            "piątą": 5,
            "szósty": 6, "szósta": 6, "szóste": 6,
            "szóstego": 6, "szóstej": 6, "szóstym": 6,
            "szóstą": 6,
            "siódmy": 7, "siódma": 7, "siódme": 7,
            "siódmego": 7, "siódmej": 7, "siódmym": 7,
            "siódmą": 7,
            "ósmy": 8, "ósma": 8, "ósme": 8,
            "ósmego": 8, "ósmej": 8, "ósmym": 8,
            "ósmą": 8,
            "dziewiąty": 9, "dziewiąta": 9, "dziewiąte": 9,
            "dziewiątego": 9, "dziewiątej": 9, "dziewiątym": 9,
            "dziewiątą": 9,
            "dziesiąty": 10, "dziesiąta": 10, "dziesiąte": 10,
            "dziesiątego": 10, "dziesiątej": 10, "dziesiątym": 10,
            "dziesiątą": 10,
            "jeden": 1, "jedna": 1, "jedno": 1,
            "dwa": 2, "dwie": 2, "trzy": 3,
            "cztery": 4, "pięć": 5, "sześć": 6,
            "siedem": 7, "osiem": 8, "dziewięć": 9,
            "dziesięć": 10,
            "eleventh": 11, "11th": 11,
            "twelfth": 12, "12th": 12,
            "thirteenth": 13, "13th": 13,
            "fourteenth": 14, "14th": 14,
            "fifteenth": 15, "15th": 15,
            "sixteenth": 16, "16th": 16,
            "seventeenth": 17, "17th": 17,
            "eighteenth": 18, "18th": 18,
            "nineteenth": 19, "19th": 19,
            "twentieth": 20, "20th": 20,
            "jedenasty": 11, "jedenasta": 11, "jedenaste": 11,
            "dwunasty": 12, "dwunasta": 12, "dwunaste": 12,
            "trzynasty": 13, "trzynasta": 13, "trzynaste": 13,
            "czternasty": 14, "czternasta": 14, "czternaste": 14,
            "piętnasty": 15, "piętnasta": 15, "piętnaste": 15,
            "szesnasty": 16, "szesnasta": 16, "szesnaste": 16,
            "siedemnasty": 17, "siedemnasta": 17, "siedemnaste": 17,
            "osiemnasty": 18, "osiemnasta": 18, "osiemnaste": 18,
            "dziewiętnasty": 19, "dziewiętnasta": 19, "dziewiętnaste": 19,
            "dwudziesty": 20, "dwudziesta": 20, "dwudzieste": 20,
            "eleven": 11, "twelve": 12, "thirteen": 13,
            "fourteen": 14, "fifteen": 15, "sixteen": 16,
            "seventeen": 17, "eighteen": 18, "nineteen": 19,
            "twenty": 20,
            "jedenaście": 11, "dwanaście": 12, "trzynaście": 13,
            "czternaście": 14, "piętnaście": 15, "szesnaście": 16,
            "siedemnaście": 17, "osiemnaście": 18,
            "dziewiętnaście": 19, "dwadzieścia": 20,
            "one": 1, "1": 1,
            "two": 2, "2": 2,
            "three": 3, "3": 3,
            "four": 4, "4": 4,
            "five": 5, "5": 5,
            "six": 6, "6": 6,
            "seven": 7, "7": 7,
            "eight": 8, "8": 8,
            "nine": 9, "9": 9,
            "ten": 10, "10": 10,
        }

        selected_number = None
        for word, number in ordinal_words.items():
            if re.search(
                r"(?:^|[^a-z0-9])" + re.escape(word) + r"(?:$|[^a-z0-9])",
                transcript.casefold(),
            ):
                selected_number = number
                break

        candidates = pending["candidates"]
        matched = []

        # Accept numeric choices of any reasonable length, e.g. "number 12".
        if selected_number is None:
            numeric_match = re.search(
                r"(?<![0-9])([0-9]{1,3})(?:st|nd|rd|th)?(?![0-9])",
                transcript.casefold(),
            )
            if numeric_match:
                selected_number = int(numeric_match.group(1))

        # "Last one" and common Polish forms always refer to the final
        # candidate in the currently displayed list.
        words = set(re.findall(r"[^\W_]+", transcript.casefold()))
        last_words = {
            "last", "final",
            "ostatni", "ostatnia", "ostatnie",
            "ostatniego", "ostatniej", "ostatnim", "ostatnią",
        }

        # Give explicit "last/final" wording priority over cardinal
        # words such as "one" in phrases like "the last one".
        if words.intersection(last_words):
            if candidates:
                matched = [candidates[-1]]
        elif selected_number is not None:
            if 1 <= selected_number <= len(candidates):
                matched = [candidates[selected_number - 1]]
            else:
                self._refresh_pending_window_selection(
                    pending, transcript, "choice number out of range"
                )
                return (
                    f"There are only {len(candidates)} choices. "
                    f"Please choose a number from 1 to {len(candidates)}, "
                    "or say the window title."
                )
        else:
            matched = [
                item for item in candidates
                if any(
                    phrase and phrase in normalize(item.get(field, ""))
                    for field in ("title", "app_id", "resource_name", "resource_class")
                )
            ]

        if not matched:
            choices = "; ".join(
                f"{i}. {item.get('title', 'Untitled')}"
                for i, item in enumerate(candidates, 1)
            )
            self._refresh_pending_window_selection(
                pending, transcript, "no candidate matched"
            )
            return (
                "I couldn't identify that choice. Please say a number or "
                f"title: {choices}. Say cancel to abandon it."
            )

        if len(matched) > 1:
            choices = "; ".join(item.get("title", "Untitled") for item in matched)
            self._refresh_pending_window_selection(
                pending, transcript, "answer matched multiple candidates"
            )
            return f"That matches multiple choices: {choices}. Please be more specific."

        print(
            f"[Misty] Window choice matched: "
            f"{matched[0].get('title', 'Untitled')}",
            flush=True,
        )

        chosen = matched[0]
        if pending.get("kind") == "content":
            self._clear_pending_window_selection(pending)
            url = chosen.get("url")
            if not isinstance(url, str):
                return "That choice has no usable link."
            result = ActionExecutor().open_url(url)
            return str(result.get("message", "Opening the selected link."))

        action = pending["action"]
        allowed = {
            "focus_window", "minimize_window", "maximize_window",
            "restore_window", "close_window", "move_window_to_screen",
        }
        if action not in allowed:
            self._clear_pending_window_selection(pending)
            return "That pending window action is not allowed."

        if self.window_service is None:
            self._refresh_pending_window_selection(
                pending, transcript, "window service unavailable"
            )
            return "I can't verify the selected window right now. Please try again."

        live_result = self.window_service.list_windows()
        if not live_result.get("success"):
            self._refresh_pending_window_selection(
                pending, transcript, "live window lookup failed"
            )
            return live_result.get("message", "I couldn't check open windows.")

        live = {
            item.get("id"): item
            for item in live_result.get("windows", [])
        }
        current = live.get(chosen.get("id"))

        if current is None:
            self._clear_pending_window_selection(pending)
            return "That window is no longer open. Please make a new request."

        arguments = {
            "window_id": current["id"],
            **pending.get("arguments", {}),
        }
        approved = True

        if action == "close_window":
            approved = self._request_confirmation(
                action,
                arguments,
                f"Close the window '{current.get('title', 'Untitled')}'.",
            )

        self._clear_pending_window_selection(pending)

        if not approved:
            return "The close action was not approved. Nothing was closed."

        registry = ToolRegistry(
            executor=ActionExecutor(window_service=self.window_service)
        )
        result = registry.execute(action, arguments, confirmed=approved)
        return result.get("message", "The window action finished.")

    def _build_registry(self):
        return ToolRegistry(
            executor=ActionExecutor(window_service=self.window_service),
            window_selection_callback=self._set_pending_window_selection,
            memory_store=self.memory_store,
            clipboard_service=self.clipboard_bridge,
            vision_service=self.vision_service,
            web_search_client=self.web_search_client,
            knowledge_index=self.knowledge_index,
            browser_session=self.browser_session,
            reminder_store=self.reminder_store,
            file_transfer_engine=self.file_transfer_engine,
            control_activator=self.control_activator,
            media_controller=self.media_controller,
            zip_engine=self.zip_engine,
            text_search_service=self.text_search_service,
            directory_transfer_engine=self.directory_transfer_engine,
            calendar_engine=self.calendar_engine,
            duplicate_finder=self.duplicate_finder,
            file_transcriber=self.file_transcriber,
            active_window_provider=self.active_window_provider,
            input_backend=self._input_backend(),
        )

    def _input_backend(self):
        """The opt-in ydotool backend, or None when it cannot be used.

        Native text entry is the one place Misty types for the user, so the
        backend is only handed to the registry when its device and daemon are
        actually usable. Every failure mode (no ``ydotool``, no ``/dev/uinput``,
        no ``ydotoold``) then surfaces as a clear refusal from the tool instead
        of a silent no-op. See ``docs/DATA_MODEL.md`` and README for setup.
        """
        cached = self.__dict__.get("_input_backend_instance", _UNSET)
        if cached is not _UNSET:
            return cached
        backend = None
        try:
            from app.desktop.ydotool_backend import YdotoolBackend

            cursor_reader = (
                self.window_service.cursor_position
                if self.window_service is not None
                else None
            )
            candidate = YdotoolBackend(cursor_reader=cursor_reader)
            capabilities = candidate.capabilities()
            if capabilities.get("available") is True:
                backend = candidate
            else:
                print(
                    "[Misty] Native text entry unavailable: "
                    f"{capabilities.get('uinput_problem') or 'ydotool'}; "
                    "selection replacement will use accessibility only.",
                    flush=True,
                )
        except Exception as error:
            print(f"[Misty] Input backend error: {error}", flush=True)
        self._input_backend_instance = backend
        return backend

    def submit_text(self, text, on_admit=None):
        """Admit a typed request through the same agent path as voice.

        Returns a :class:`TypedAdmission`. Only a request that is really in
        flight changes the controller's origin bookkeeping; a rejected
        request leaves every in-flight voice request untouched.

        ``on_admit(token)`` runs synchronously on the caller's thread once
        the request is admitted and *before* the worker can emit anything,
        so UI state keyed on the request id cannot lose the race with a
        fast failure.
        """
        if self._shutting_down:
            return self._typed_reject(text, "Misty is shutting down.")
        if not isinstance(text, str):
            return self._typed_reject(
                text, "A request must be text.", echo=False
            )
        text = text.strip()
        if not text:
            return self._typed_reject(
                text, "Please type a request.", echo=False
            )
        if len(text) > MAX_TYPED_TEXT_CHARS:
            return self._typed_reject(
                text,
                "That request is too long. Please keep it under 2000 "
                "characters.",
            )
        # ``has_live_work`` (not the raw flag) is authoritative: a stale
        # ``_busy`` left by an aborted recording must not reject submissions.
        if self.has_live_work():
            return self._typed_reject(
                text, "I'm busy with another request. Please wait a moment."
            )

        request_id = uuid.uuid4().hex
        self._active_typed_request_id = request_id
        self._typed_request_text = text
        self._mark_request_origin(request_id, "typed", text)

        # Typed requests belong to the main chat window, not the voice
        # overlay. Confirmations and selection prompts still show themselves.
        self.popup_ipc.send({"type": "hide"})
        self.transcript_changed.emit(text)
        self.response_changed.emit("Working out what you need...")
        self.activity_event.emit({
            "kind": "request",
            "title": "You typed",
            "details": text,
            "status": "info",
        })
        self.state_changed.emit("thinking")
        token = TypedAdmission(True, "", request_id)
        self._busy = True
        cancel_event = threading.Event()
        with self._task_state_lock():
            self._task_cancel_event = cancel_event
        self._notify_escape_state()
        if on_admit is not None:
            # Before the worker exists: a fast failure cannot outrun the
            # caller's bookkeeping for this request id.
            try:
                on_admit(token)
            except Exception as error:
                print(f"[Misty] Typed admission hook failed: {error}",
                      flush=True)
        self._worker = threading.Thread(
            target=self._process_typed_request,
            args=(text, request_id, cancel_event),
            name="misty-typed-request",
            daemon=True,
        )
        self._worker.start()
        return token

    def _typed_reject(self, text, reason, echo=True):
        echo_text = text.strip() if isinstance(text, str) else ""
        if echo:
            self.response_changed.emit(reason)
        request_id = uuid.uuid4().hex
        # Detail is always the exact user-facing message.
        self._typed_emit_outcome(
            self.typed_rejected, request_id, echo_text, reason
        )
        return TypedAdmission(False, reason, request_id)

    def _process_typed_request(self, text, request_id=None, cancel_event=None):
        self._run_agent_session(
            self._typed_request_body, text, request_id=request_id,
            cancel_event=cancel_event,
        )

    def _typed_request_body(self, text, request_id=None):
        try:
            # A typed "2"/"cancel"/title while numbered choices are shown
            # resolves them exactly like a spoken follow-up.
            selection_answer = self._handle_pending_window_selection(text)
            if selection_answer is not None:
                self.activity_event.emit({
                    "kind": "response",
                    "title": "Selection",
                    "details": selection_answer,
                    "status": "info",
                })
                if self._has_pending_window_selection():
                    self.window_selection_message_requested.emit(
                        str(selection_answer)
                    )
                    self.state_changed.emit("selection")
                else:
                    self.window_selection_cleared.emit()
                    self.response_changed.emit(str(selection_answer))
                    self.state_changed.emit("complete")
                self._typed_emit_outcome(
                    self.typed_completed, request_id, text, "selection"
                )
                # An ambiguous answer leaves the choice list alive; either
                # way the agent must not run for this request.
                return

            answer = self._typed_run_agent(self._agent, text)
            self.activity_event.emit({
                "kind": "response",
                "title": "Misty replied",
                "details": str(answer),
                "status": "success",
            })
            if self._has_pending_window_selection():
                # Keep the choice list; the agent's plain text would replace
                # it. Fold the answer into the popup message instead.
                self.window_selection_message_requested.emit(str(answer))
                self.state_changed.emit("selection")
            else:
                self.response_changed.emit(str(answer))
                self.state_changed.emit("complete")
            self._typed_emit_outcome(
                self.typed_completed, request_id, text, "answer"
            )
        except Exception as error:
            print(f"[Misty] Typed request error: {error}", flush=True)
            message = f"Something went wrong: {error}"
            self.activity_event.emit({
                "kind": "error",
                "title": "Request failed",
                "details": str(error),
                "status": "failure",
            })
            self.response_changed.emit(message)
            # Specific outcome first, so receivers keyed on the request id
            # see it before any generic "error" state reaction.
            self._typed_emit_outcome(
                self.typed_failed, request_id, text, message
            )
            self.state_changed.emit("error")
        finally:
            self._typed_clear_active(request_id)

    # ------------------------------------------------------------------
    # Task card: progress, cancel and retry of the request in flight
    # ------------------------------------------------------------------

    def _task_retry_blocker(self, name, result):
        """Why ``name`` must not be replayed for ``result``, or ``""``."""
        try:
            if not is_retryable(name, self._task_registry_handler(name)):
                return (
                    f"{name} can change something or needs approval, so it "
                    "is not retried."
                )
        except Exception:
            return "The retry policy could not confirm that this is safe."
        if not isinstance(result, dict):
            result = {}
        if result.get("uncertain") is True:
            return (
                f"{name} ended with an uncertain outcome; it is not run "
                "again."
            )
        if result.get("partial") is True:
            return f"{name} only partially completed; it is not run again."
        side_effects = result.get("side_effects")
        if side_effects not in (None, False, [], {}):
            return f"{name} may have changed something; it is not run again."
        return ""

    def _task_registry_handler(self, name):
        """The registry's handler entry for ``name`` (empty on any error)."""
        lookup = getattr(self, "_build_registry", None)
        if not callable(lookup):
            return {}
        try:
            registry = lookup()
            handler = registry.get_handler(name)
        except Exception:
            return {}
        return handler if isinstance(handler, dict) else {}

    def _task_card_observe_event(self, event):
        """Track the in-flight task and re-emit its events for the card.

        Payloads never reach the card: a failed step is reduced to a marker
        (tool, request, retryable, reason). The original arguments are kept
        in memory only, and only for a step the policy allows to retry.
        """
        if not isinstance(event, dict):
            return
        kind = event.get("kind")
        if kind not in ("decision", "tool"):
            return
        title = event.get("title")
        status = event.get("status")
        tool = event.get("tool")
        tool = tool if isinstance(tool, str) and tool else ""

        if kind == "tool" and tool and status in ("success", "failure"):
            sensitive = event.get("sensitive") is True
            with self._task_state_lock():
                event_id = self._session_state("_task_event", None)
                if event_id is None:
                    return
                if status == "failure":
                    arguments = event.get("arguments")
                    if sensitive or not isinstance(arguments, dict):
                        # Nothing is kept for a private payload, so no retry
                        # can ever replay it.
                        arguments = None
                    self._task_failed_step = self._task_failed_marker(
                        tool, arguments, event_id, sensitive
                    )
                else:
                    self._task_failed_step = None
            # The card receives this event from _task_card_forward, which
            # strips details/result/arguments. Emitting the raw event here
            # would hand a card receiver clipboard or screen text.
            return

        if kind == "decision" and isinstance(title, str):
            finished = {
                "Task completed": "completed",
                "Task recovered after a failure": "recovered",
                "Task not fully completed": "failed",
                "Task cancelled": "cancelled",
                "Task plan rejected": "failed",
                "Window target needed": "failed",
                "Clarification needed": "needs_input",
                "No executable actions": "completed",
            }.get(title)
            if finished is not None:
                self._task_card_finish(finished)

    def _task_failed_marker(self, tool, arguments, request_id, sensitive=False):
        """Build the failed-step record; arguments only when retryable."""
        blocker = (
            f"{tool} handles private data; retrying it is not supported yet."
            if sensitive else
            self._task_retry_blocker(tool, {})
        )
        return {
            "tool": tool,
            "arguments": None if blocker else arguments,
            "request_id": request_id,
            "retryable": not blocker,
            "reason": blocker,
        }

    def _task_card_finish(self, status):
        with self._task_state_lock():
            event_id = self._session_state("_task_event", None)
            if event_id is None:
                return
            self._task_event = None
            # The finished task keeps its id for a possible retry.
            self._task_failed_id = event_id
            payload = self._task_card_status_payload(status, event_id)
        self._task_emit(self.task_finished, payload)

    def _task_emit(self, signal, payload):
        try:
            signal.emit(payload)
        except Exception:
            pass

    def _task_card_forward(self, event):
        """Re-emit one agent event for the card while a task is running.

        The event is a card-facing copy: the private payload keys
        (``details``/``result``/``arguments``) are dropped, so a card
        receiver can never be handed clipboard or screen text. Only an
        event's ``sensitive`` flag is passed through, so the card can mark
        a private step without rendering it.
        """
        with self._task_state_lock():
            if self._session_state("_task_event", None) is None:
                return
        self._task_emit(self.task_event, _task_card_event(event))

    def _task_card_status_payload(self, status, event_id=None, detail=""):
        with self._task_state_lock():
            failed = self._session_state("_task_failed_step", None)
            if failed is None or failed.get("request_id") != event_id:
                failed = None
            payload = {
                "status": status,
                "request": self._session_state("_task_request", ""),
                "event_id": event_id,
                "failed_step": (
                    {
                        "tool": failed["tool"],
                        "retryable": bool(failed["retryable"]),
                        "reason": failed["reason"],
                    }
                    if failed else None
                ),
            }
        if detail:
            payload["message"] = detail
        return payload

    def _task_card_retitle(self, text):
        """Refine the card title once the real request text is known."""
        title = str(text or "").strip()
        if not title:
            return
        with self._task_state_lock():
            event_id = self._session_state("_task_event", None)
            if event_id is None:
                return
            self._task_request = title[:2000]
            payload = {"request": self._task_request, "event_id": event_id}
        self._task_emit(self.task_started, payload)

    def _task_card_started(self, text, event_id):
        with self._task_state_lock():
            self._task_request = str(text or "")[:2000]
            self._task_event = event_id
            self._task_failed_step = None
            payload = {
                "request": self._task_request,
                "event_id": event_id,
                "status": "running",
            }
        self._task_emit(self.task_started, payload)

    def _task_card_abort(self, detail=""):
        """Finish a task that ended its request without an outcome event.

        A task the user cancelled is reported as cancelled; anything else
        that ended silently is reported as failed. A later failure is kept
        for a possible retry when the step is retryable.
        """
        with self._task_state_lock():
            event_id = self._session_state("_task_event", None)
            if event_id is None:
                return
            failed = self._session_state("_task_failed_step", None)
            cancelled = (
                failed is None
                and self._session_state("_task_cancelled", None) == event_id
            )
            self._task_event = None
            self._task_failed_id = event_id
            if failed is None or not failed.get("retryable"):
                self._task_failed_step = None
            status = "cancelled" if cancelled else "failed"
            payload = self._task_card_status_payload(
                status, event_id, "" if cancelled else detail
            )
        self._task_emit(self.task_finished, payload)

    @Slot()
    def sync_task_escape(self):
        """Run on Qt's thread; queued old notifications cannot unbind a new owner."""
        self.set_task_escape(bool(
            not self._shutting_down and (
                self._recording or self._confirmation_recording
                or self._busy or self._agent_busy
            )
        ))

    def _notify_escape_state(self):
        # Some offline tests construct a plain controller without Qt signals.
        try:
            self.escape_sync_requested.emit()
        except RuntimeError:
            # Hand-built offline controllers have no initialized QObject.
            pass

    def set_task_escape(self, active):
        """Apply the current recording/request Escape binding on Qt's thread."""
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        command = "CANCEL_ON" if active else "CANCEL_OFF"
        self.process.write(f"{command}\n".encode())

    def cancel_active_request(self):
        """Ask the running request to stop before its next step.

        Sets the cancellation event the agent already honours between plan
        steps and before planning starts. The step that is already
        executing is not interrupted; nothing is rolled back afterwards.
        """
        voice_event = getattr(self, "_voice_cancel_event", None)
        with self._task_state_lock():
            event = self._session_state("_task_cancel_event", None)
        active = (self._recording or self._confirmation_recording or self._busy
                  or getattr(self, "_agent_busy", False) or self.active_task_id is not None)
        if not active:
            return False
        if voice_event is not None:
            voice_event.set()
        if event is not None:
            event.set()
        # Abort held audio before waking an approval waiter, so the abort
        # cannot publish a late confirmation state after the owner resumes.
        # A subsequent RELEASE must not decode an answer/start a request.
        if self._confirmation_recording:
            self._abort_recording("Request cancelled.")
        elif self._recording:
            self._abort_recording("Request cancelled.")
            # Retain ownership until preload cleanup finishes, rather than
            # allowing a new request to overwrite models being loaded.
            self._busy = True
            self._worker = threading.Thread(
                target=self._finish_cancelled_recording,
                args=(voice_event,), name="misty-cancel-recording", daemon=True,
            )
            self._worker.start()
        self._confirm_pending(False)
        self.response_changed.emit("Request cancelled.")
        self._notify_escape_state()
        return True

    def _finish_cancelled_recording(self, cancel_event):
        try:
            # The loader owns its model until it returns. Keep admission
            # closed; do not unload a model concurrently with its preload.
            for thread in (self._whisper_thread, self._agent_thread):
                if thread is not None:
                    thread.join()
            self._cleanup_models()
        finally:
            if getattr(self, "_voice_cancel_event", None) is cancel_event:
                self._voice_cancel_event = None
            self._busy = False
            self._notify_escape_state()
            if not self._shutting_down:
                self.ready_reset_requested.emit()

    @property
    def active_task_id(self):
        """Request/event id of the task the card currently shows (or None)."""
        with self._task_state_lock():
            return self._session_state("_task_event", None)

    def retry_last_failed_step(self, step_tool=None):
        """Re-run the one failed step with its original arguments.

        The replay goes through the same agent runtime, registry and policy
        checks as the original call. It never asks the model to plan again
        and never replays an action that could change something. Returns
        ``"started"`` or a short reason it did not start.
        """
        with self._task_state_lock():
            failed = self._session_state("_task_failed_step", None)
            active = self._session_state("_task_event", None)
            if failed is None:
                return "No failed step to retry."
            if (
                isinstance(step_tool, str)
                and step_tool
                and step_tool != failed["tool"]
            ):
                return f"The failed step is {failed['tool']}, not {step_tool}."
            if active is not None and active != failed["request_id"]:
                # A different task is running now; its failure is not this one.
                return "Misty is still working on the current task."
            if failed["request_id"] != self._session_state(
                "_task_failed_id", None
            ):
                return "That step is no longer the last task's failure."
            if isinstance(step_tool, str) and step_tool and step_tool != failed["tool"]:
                return f"The failed step is {failed['tool']}, not {step_tool}."
            tool = failed["tool"]
            if failed.get("sensitive"):
                return (
                    f"{tool} handles private data; retrying it is not "
                    "supported yet."
                )
            if not failed["retryable"] or not isinstance(
                failed.get("arguments"), dict
            ):
                return failed["reason"] or f"{tool} is not safe to retry."
            if self.has_live_work():
                return "Misty is still finishing the current request."
            blocker = self._task_retry_blocker(tool, {})
            if blocker:
                return blocker
            arguments = dict(failed["arguments"])
            event_id = str(uuid.uuid4())
            self._task_event = event_id
            self._task_failed_step = None
            self._busy = True
            cancel_event = threading.Event()
            self._task_cancel_event = cancel_event
            request = self._task_request
        self.response_changed.emit(
            f"Retrying {tool} with the same arguments."
        )
        self.state_changed.emit("thinking")
        self._notify_escape_state()
        try:
            self._task_emit(self.task_started, {
                "request": request,
                "event_id": event_id,
                "retry": True,
                "tool": tool,
            })
        except Exception:
            pass
        worker = threading.Thread(
            target=self._task_retry_execute,
            args=(event_id, tool, arguments, cancel_event),
            name="misty-task-retry",
            daemon=True,
        )
        self._worker = worker
        worker.start()
        return "started"

    def _task_agent(self):
        """The one agent factory used by requests and retries."""
        return MistyAgent(
            registry=self._build_registry(),
            event_callback=self._agent_activity_event,
            confirmation_callback=self._request_confirmation,
            audit_log=self.audit_log,
            task_journal=self.task_journal,
            content_selection_callback=self._set_pending_content_selection,
            before_visual_callback=self.unload_reasoning_model,
        )

    def _task_retry_execute(self, event_id, tool, arguments, cancel_event=None):
        """Run one replayed step through the agent's normal tool path."""
        success = False
        cancel_event = cancel_event if cancel_event is not None else threading.Event()
        try:
            # Preserve the event created at admission, including a Cancel
            # received before this worker or agent factory started.
            agent = self._task_agent()
            agent.cancel_event = cancel_event
            with self._task_state_lock():
                event_id = self._session_state("_task_event", None)
                if event_id is not None:
                    self._task_cancel_event = cancel_event
            self._agent = agent
            self._agent_busy = True
            try:
                # No _chat() call, so the model is never asked to plan.
                result = agent._execute_with_single_retry(
                    tool, dict(arguments), confirmed=True
                )
                success = isinstance(result, dict) and (
                    result.get("success") is True
                )
            finally:
                self._agent_busy = False
                _stop_if_running(agent)
        except Exception as error:
            print(f"[Misty] Retry error: {error}", flush=True)
        finally:
            with self._task_state_lock():
                if self._session_state("_task_event", None) == event_id:
                    self._task_event = None
                stale = self._session_state("_task_failed_step", None)
                if success:
                    # The replay worked: this step is no longer a failure.
                    if stale is not None and stale["request_id"] == event_id:
                        self._task_failed_step = None
                        stale = None
                    self._task_failed_id = None
                elif stale is None or stale["request_id"] != event_id:
                    # The replay failed again: keep it retryable so the user
                    # can try once more, exactly like the first failure.
                    self._task_failed_step = {
                        "tool": tool,
                        "arguments": dict(arguments),
                        "request_id": event_id,
                        "retryable": True,
                        "reason": "",
                    }
                    self._task_failed_id = event_id
                    stale = self._task_failed_step
            message = (
                f"Retried {tool}: the action succeeded."
                if success else
                f"Retried {tool}: the action failed again."
            )
            try:
                self.activity_event.emit({
                    "kind": "response",
                    "title": "Misty replied",
                    "details": message,
                    "status": "success" if success else "failure",
                })
                self.response_changed.emit(message)
                self.state_changed.emit("complete" if success else "error")
                self._task_emit(self.task_finished, {
                    "status": "completed" if success else "failed",
                    "request": self._task_request,
                    "event_id": event_id,
                    "failed_step": (
                        {
                            "tool": stale["tool"],
                            "retryable": bool(stale["retryable"]),
                            "reason": stale["reason"],
                        }
                        if stale is not None else None
                    ),
                    "message": message,
                })
            except Exception:
                pass
            self._busy = False
            self._notify_escape_state()
            if not self._shutting_down:
                self.ready_reset_requested.emit()

    def _run_agent_session(self, body, *args, request_id=None, cancel_event=None, **kwargs):
        """Own the per-request agent and its model cleanup.

        The worker creates exactly one agent, runs ``body`` against it and is
        the only owner of that generation's cleanup. Shutdown may also run a
        cleanup, but only while no worker is live; it never nulls models a
        live worker is still using.

        The agent gets a fresh cancellation event for this request (so the
        task card's Cancel stops exactly this task), and the turn's activity
        events are collected for the session context.
        """
        cancel_event = cancel_event if cancel_event is not None else threading.Event()
        with self._task_state_lock():
            self._task_cancel_event = cancel_event
        try:
            agent = self._task_agent()
        except Exception:
            # Factory/registry construction is part of admission ownership too.
            # It must not strand _busy before the normal session finally exists.
            try:
                self._cleanup_models()
            finally:
                if getattr(self, "_voice_cancel_event", None) is cancel_event:
                    self._voice_cancel_event = None
                self._agent_busy = False
                self._busy = False
                self._notify_escape_state()
                if not self._shutting_down:
                    self.ready_reset_requested.emit()
            raise
        agent.cancel_event = cancel_event
        turn_text = str(args[0]) if args and isinstance(args[0], str) else ""
        with self._task_state_lock():
            # A new request owns the card: its previous task is replaced.
            self._task_cancel_event = cancel_event
            self._task_event = None
            self._task_failed_step = None
            self._task_failed_id = None
            self._task_cancelled = None
        self._agent = agent
        self._agent_busy = True
        self._session_begin_turn(turn_text)
        if request_id is not None:
            # The body reports per-request outcomes (typed_failed and
            # friends) keyed by this id; dropping it silently disables them.
            kwargs["request_id"] = request_id
        # The card keeps the title the user actually asked; a voice request
        # retitles it with the transcript once decoded.
        event_id = uuid.uuid4().hex
        self._task_failed_id = event_id
        # ``_task_card_started`` records the task and emits ``task_started``;
        # emitting again here duplicated the signal for every request.
        self._task_card_started(turn_text, event_id)
        completed_normally = False
        self._notify_escape_state()
        try:
            if not cancel_event.is_set():
                body(*args, **kwargs)
            completed_normally = True
        finally:
            if cancel_event.is_set():
                self._task_card_finish("cancelled")
            elif completed_normally:
                # The body returned without raising. If no decision event
                # already closed the card (a fake agent, or an agent that
                # returned early), close it here so "finished" is always
                # reported and a failed step stays retryable.
                self._task_card_finish("completed")
            self._task_card_abort()
            self._session_discard_turn()
            self._agent_busy = False
            self._cleanup_models(owner=agent)
            if getattr(self, "_voice_cancel_event", None) is cancel_event:
                self._voice_cancel_event = None
            self._busy = False
            self._notify_escape_state()
            if not self._shutting_down:
                self.ready_reset_requested.emit()

    def _typed_clear_active(self, request_id):
        if request_id is not None and request_id != self._active_typed_request_id:
            return
        self._active_typed_request_id = None
        self._typed_request_text = ""

    def _press(self):
        if (
            self._shutting_down
            or self._recording
            or self._confirmation_recording
        ):
            return

        confirmation_pending = self._confirmation_broker.is_pending()

        if self._busy or self._worker_alive():
            if not confirmation_pending:
                return
            if self._thread_alive(self._confirmation_worker):
                # Whisper's shared transcriber must never decode overlapping
                # spoken answers while this task is waiting for approval.
                return

            # Bind this recording to the confirmation pending right now. Any
            # decision derived from this audio may only resolve that request.
            self._confirmation_request_id = (
                self._confirmation_broker.current_request_id()
            )

            try:
                with self._recording_lock:
                    self.recorder.start()
                    # A button/timeout can resolve approval while the audio
                    # device opens. Bind audio only to a still-pending prompt.
                    if (self._request_cancelled()
                            or not self._confirmation_broker.is_pending()
                            or self._confirmation_broker.current_request_id()
                            != self._confirmation_request_id):
                        self.recorder.discard()
                        self._confirmation_request_id = None
                        return
                    self._confirmation_recording = True
                    self._acquire_recording()
                    self.state_changed.emit("listening")
                    self.response_changed.emit(
                        f"Say confirm or cancel, then release {self.shortcut}."
                    )
                    print(
                        "[Misty] Listening for a spoken confirmation.",
                        flush=True,
                    )
            except Exception as error:
                print(
                    f"[Misty] Confirmation recording error: {error}",
                    flush=True,
                )
                self.response_changed.emit(
                    "I couldn't record that confirmation. "
                    "Use the dialog buttons instead."
                )
            return

        self._completion_timer.stop()
        self._completion_waiting_for_hover = False
        print("[Misty] Shortcut pressed.", flush=True)

        # Capture the user's application before Misty's popup takes focus.
        if self.window_service is not None:
            try:
                capture = self.window_service.capture_active_window()
                if capture.get("success"):
                    active = capture.get("active_window")
                    if active:
                        print(
                            f"[Misty] Captured previous active window: "
                            f"{active['title']}",
                            flush=True,
                        )
                    else:
                        print(
                            "[Misty] No external application was active.",
                            flush=True,
                        )
                else:
                    print(
                        f"[Misty] Window capture warning: {capture.get('message')}",
                        flush=True,
                    )
            except Exception as error:
                print(f"[Misty] Window capture error: {error}", flush=True)

        self.popup_ipc.send({"type": "show"})
        self.state_changed.emit("listening")
        self.response_changed.emit("Listening...")

        self._voice_cancel_event = threading.Event()
        self._start_preloads()

        try:
            self.recorder.start()
            self._recording = True
            self._acquire_recording()
            self._notify_escape_state()
        except Exception as error:
            print(f"[Misty] Recording start error: {error}", flush=True)
            self._busy = True
            self.state_changed.emit("error")
            self.response_changed.emit(
                f"Couldn't start the microphone: {error}"
            )
            self._start_error_thread = threading.Thread(
                target=self._cleanup_after_start_error,
                daemon=True,
            )
            self._worker = self._start_error_thread
            self._worker.start()

    def _start_preloads(self):
        """Start the Whisper and Ministral preloads for one voice request."""
        transcriber = self._transcriber_factory()
        self._transcriber = transcriber
        self._preload_epoch += 1
        epoch = self._preload_epoch
        self._whisper_thread = threading.Thread(
            target=self._preload_transcriber,
            args=(transcriber, epoch),
            name="misty-whisper-preload",
            daemon=True,
        )
        self._whisper_thread.start()

        try:
            agent = MistyAgent(
                registry=self._build_registry(),
                event_callback=self._agent_activity_event,
                confirmation_callback=self._request_confirmation,
                audit_log=self.audit_log,
                task_journal=self.task_journal,
                content_selection_callback=self._set_pending_content_selection,
                before_visual_callback=self.unload_reasoning_model,
            )
        except Exception as error:
            agent = None
            self._agent_errors = [error]
            print(f"[Misty] Agent creation error: {error}", flush=True)
        self._agent = agent
        self._agent_thread = threading.Thread(
            target=self._preload_agent,
            args=(agent,),
            name="misty-qwen-preload",
            daemon=True,
        )
        self._agent_thread.start()

    def _preload_transcriber(self, transcriber, epoch):
        try:
            transcriber.load()
        except Exception as error:
            print(f"[Misty] Whisper preload error: {error}", flush=True)

    def _preload_agent(self, agent=None):
        if agent is None:
            agent = self._agent
        if agent is None:
            return
        try:
            agent.start()
        except Exception as error:
            self._agent_errors.append(error)
            print(f"[Misty] Ministral preload error: {error}", flush=True)

    def _join_preload_before_transcribe(self):
        """Join the Ministral preload (bounded) before loading Whisper."""
        thread = self._agent_thread
        if thread is None or not thread.is_alive():
            return True
        if threading.current_thread() is thread:
            return False
        thread.join(timeout=PRELOAD_JOIN_SECONDS)
        if thread.is_alive():
            print(
                "[Misty] Ministral preload still running; starting Whisper "
                "without waiting further.",
                flush=True,
            )
            return False
        return True

    def unload_reasoning_model(self):
        """Release Ministral when no request is using it (VLM/file-transcribe hook).

        Called synchronously from the tool path (vision inspection, file
        transcription), which runs inside the request body. It therefore
        never stops an agent another thread is executing: it only unloads
        when the calling thread *is* the one running the request.
        """
        agent = self._agent
        if agent is None or not self._agent_busy:
            return False
        current = threading.current_thread()
        if current is not self._worker:
            return False
        if agent.process is None or getattr(agent, "task_held", False):
            return False
        # Keep the arbiter's view truthful: the model is about to be
        # unloaded outside a lease, so any recorded lease for it ends here.
        self._release_arbiter_leases("reasoning")
        try:
            print("[Misty] Releasing Ministral before another local model runs.",
                  flush=True)
            agent.stop()
        except Exception as error:
            print(f"[Misty] Ministral release error: {error}", flush=True)
            return False
        finally:
            # The hook runs *inside* agent.run. A cancelled request must stay
            # cancelled; agent.run itself decides whether to observe it.
            event = getattr(agent, "cancel_event", None)
            if self._shutting_down and event is not None:
                try:
                    event.set()
                except Exception:
                    pass
        return True

    def _obtain_confirmation_transcriber(self):
        """Return a loaded transcriber for confirmation audio, or None.

        Typed requests never preload Whisper, so the transcriber is created
        lazily here. It is stored on the controller like the voice path does,
        so ``_cleanup_models`` unloads it and shutdown stays bounded.
        """
        if self._shutting_down:
            return None

        transcriber = self._transcriber
        if transcriber is None:
            transcriber = self._transcriber_factory()
            self._transcriber = transcriber

        # The voice path preloads Whisper on a thread; finish that first
        # instead of loading the model twice.
        if self._whisper_thread is not None:
            self._whisper_thread.join()

        if self._transcriber is not transcriber:
            # Shutdown cleared the shared transcriber while we waited.
            return None

        if not getattr(transcriber, "ready", False):
            transcriber.load()

        return None if self._shutting_down else transcriber

    def _start_confirmation_worker(self, audio, request_id):
        """Start (and track) decoding of confirmation audio for one request."""
        worker = threading.Thread(
            target=self._process_confirmation_audio,
            args=(audio, request_id),
            name="misty-voice-confirmation",
            daemon=True,
        )
        self._confirmation_worker = worker
        worker.start()
        return worker

    @staticmethod
    def _confirmation_decision(transcript):
        """Return True for approval, False for denial, None if unclear."""
        import re

        normalized = str(transcript or "").casefold()
        normalized = normalized.replace("ł", "l")
        normalized = re.sub(r"[^a-z0-9ąćęłńóśźż]+", " ", normalized)
        normalized = " ".join(normalized.split())

        approve_phrases = {
            "yes", "yeah", "yep", "confirm", "confirmed",
            "approve", "approved", "close it", "close window",
            "yes close it", "yes close the window",
            "do it", "go ahead",
            "tak", "potwierdzam", "zatwierdzam",
            "zgoda", "zamknij", "tak zamknij",
            "tak zamknij okno",
        }

        reject_phrases = {
            "no", "nope", "cancel", "cancel it", "deny",
            "denied", "keep it open", "leave it open",
            "no keep it open", "do not close it",
            "nie", "anuluj", "odmawiam",
            "nie zamykaj", "zostaw otwarte",
            "zostaw okno otwarte",
        }

        if normalized in approve_phrases:
            return True
        if normalized in reject_phrases:
            return False
        return None

    def _process_confirmation_audio(self, audio, request_id):
        """Transcribe confirmation audio and resolve only ``request_id``.

        ``request_id`` is captured when the recording starts and passed in as
        an argument so a late transcript can never approve a newer request.
        """
        try:
            if not isinstance(request_id, str) or not request_id.strip():
                self.response_changed.emit(
                    "I didn't hear a confirmation. Please use the "
                    "dialog buttons."
                )
                return

            if audio is None:
                self.response_changed.emit(
                    "I didn't hear a confirmation. Please try again."
                )
                return

            try:
                transcriber = self._obtain_confirmation_transcriber()
            except Exception as error:
                print(
                    f"[Misty] Confirmation transcriber error: {error}",
                    flush=True,
                )
                transcriber = None

            if transcriber is None:
                self.response_changed.emit(
                    "I couldn't understand that confirmation. "
                    "Please use the dialog buttons."
                )
                return

            result = transcriber.transcribe(audio)
            if not isinstance(result, dict):
                result = {"text": result}
            transcript = str(result.get("text", "")).strip()
            print(
                f"[Misty] Spoken confirmation: {transcript}",
                flush=True,
            )

            decision = self._confirmation_decision(transcript)

            if decision is True:
                resolved = self.resolve_confirmation(
                    True, request_id
                )
                if resolved:
                    self.response_changed.emit("Confirmation accepted.")
                else:
                    self.response_changed.emit(
                        "That confirmation request has already expired."
                    )
                return

            if decision is False:
                resolved = self.resolve_confirmation(
                    False, request_id
                )
                if resolved:
                    self.response_changed.emit("Action cancelled.")
                else:
                    self.response_changed.emit(
                        "That confirmation request has already expired."
                    )
                return

            self.response_changed.emit(
                f"I heard: “{transcript}”. "
                "Please say exactly “confirm” or “cancel”, "
                "or use the dialog buttons."
            )

        except Exception as error:
            print(
                f"[Misty] Voice confirmation error: {error}",
                flush=True,
            )
            self.response_changed.emit(
                "I couldn't understand that confirmation. "
                "Please use the dialog buttons or try again."
            )
        finally:
            # Only an unanswered, still-current prompt may restore its UI.
            # Once resolved, the request owner drives thinking/completion; a
            # late decoder must not overwrite that state or a newer prompt.
            if (not self._shutting_down
                    and isinstance(request_id, str)
                    and self._confirmation_broker.current_request_id() == request_id
                    and self._confirmation_broker.is_pending()):
                self.state_changed.emit("confirmation")

    def _release(self):
        if self._shutting_down:
            return

        if self._confirmation_recording:
            print("[Misty] Confirmation shortcut released; decoding answer.", flush=True)
            self._confirmation_recording = False
            self._finish_recording()
            self.state_changed.emit("transcribing")
            request_id = self._confirmation_request_id
            self._confirmation_request_id = None
            try:
                audio = self.recorder.stop()
            except Exception as error:
                audio = None
                print(
                    f"[Misty] Confirmation recording stop error: {error}",
                    flush=True,
                )

            self._start_confirmation_worker(audio, request_id)
            return

        if not self._recording or self._busy:
            return

        print("[Misty] Shortcut released.", flush=True)
        self._recording = False
        self._finish_recording()
        self._busy = True
        self.state_changed.emit("transcribing")

        try:
            audio = self.recorder.stop()
        except Exception as error:
            audio = None
            print(f"[Misty] Recording stop error: {error}", flush=True)

        cancel_event = getattr(self, "_voice_cancel_event", None)
        if cancel_event is None:
            cancel_event = threading.Event()
            self._voice_cancel_event = cancel_event
        self._notify_escape_state()
        self._worker = threading.Thread(
            target=self._process_request,
            args=(audio, cancel_event),
            name="misty-voice-request",
            daemon=True,
        )
        self._worker.start()

    def _process_request(self, audio, cancel_event=None):
        cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self._run_agent_session(
            self._voice_request_body, audio, cancel_event,
            cancel_event=cancel_event,
        )

    def _voice_request_body(self, audio, cancel_event=None):
        cancel_event = cancel_event if cancel_event is not None else threading.Event()
        try:
            if cancel_event.is_set() or self._shutting_down:
                return
            if audio is None:
                self.response_changed.emit(
                    f"I didn't hear anything. Try holding {self.shortcut} "
                    "a little longer."
                )
                self.state_changed.emit("ready")
                return

            # Ministral's preload is short and happens first, so wait for it
            # (bounded) before Whisper loads: both models must not be
            # resident at the same time if the preload is merely slow.
            self._join_preload_before_transcribe()
            if cancel_event.is_set() or self._shutting_down:
                return
            transcriber = self._transcriber
            if transcriber is None:
                transcriber = self._transcriber_factory()
                self._transcriber = transcriber
                transcriber.load()

            if cancel_event.is_set() or self._shutting_down:
                return
            started = time.perf_counter()
            result = transcriber.transcribe(audio)
            # Decoding is not interruptible. Discard its late result before
            # publishing text, resolving a selection, or running any action.
            if cancel_event.is_set() or self._shutting_down:
                return
            elapsed = time.perf_counter() - started
            transcript = result["text"].strip()

            print("\n========== MISTY TRANSCRIPTION ==========")
            print(f"Text: {transcript}")
            print(f"Language: {result['language'] or 'unknown'}")

            confidence = result.get("language_probability")
            if confidence is not None:
                print(f"Language confidence: {confidence:.2f}")

            print(f"Transcription time: {elapsed:.2f} seconds")
            print("==========================================", flush=True)

            if not transcript:
                self.response_changed.emit(
                    "I didn't catch that. Please try again."
                )
                self.state_changed.emit("ready")
                return

            voice_request_id = uuid.uuid4().hex
            self._mark_request_origin(voice_request_id, "voice", transcript)
            self._session_begin_turn(transcript)
            self._task_card_retitle(transcript)

            self.transcript_changed.emit(transcript)
            self.activity_event.emit({
                "kind": "request",
                "title": "You said",
                "details": transcript,
                "status": "info",
            })
            self.activity_event.emit({
                "kind": "thinking",
                "title": "Misty is processing your request",
                "details": "Local speech recognition and the local Ministral model.",
                "status": "info",
            })
            self.response_changed.emit("Working out what you need...")
            self.state_changed.emit("thinking")

            if self._agent_thread is not None:
                self._agent_thread.join()

            if self._agent_errors:
                print(
                    "[Misty] Ministral preload failed; agent will retry startup.",
                    flush=True,
                )

            if cancel_event.is_set() or self._shutting_down:
                return
            selection_answer = self._handle_pending_window_selection(
                transcript
            )
            if selection_answer is not None:
                self.activity_event.emit({
                    "kind": "response",
                    "title": "Selection",
                    "details": selection_answer,
                    "status": "info",
                })

                if self._has_pending_window_selection():
                    # The answer was unclear or ambiguous. Keep the
                    # numbered choices visible and explain what went wrong.
                    self.window_selection_message_requested.emit(
                        str(selection_answer)
                    )
                    self.state_changed.emit("selection")
                else:
                    self.window_selection_cleared.emit()
                    self.response_changed.emit(str(selection_answer))
                    self.state_changed.emit("complete")
                self._pending_clarification = None
                # No agent runs for a selection answer: the card is done.
                self._task_card_finish("completed")
                return

            if cancel_event.is_set() or self._shutting_down:
                return
            started = time.perf_counter()
            answer = self._typed_run_agent(self._agent, transcript)
            if cancel_event.is_set() or self._shutting_down:
                return

            print(
                f"[Misty] Agent time: "
                f"{time.perf_counter() - started:.2f} seconds",
                flush=True,
            )
            print("\n========== MISTY RESPONSE ==========")
            print(answer)
            print("====================================", flush=True)

            self.activity_event.emit({
                "kind": "response",
                "title": "Misty replied",
                "details": str(answer),
                "status": "success",
            })

            if self._has_pending_window_selection():
                # The tool resolver created a choice list. Do not replace
                # it with the agent's plain-text description.
                self.state_changed.emit("selection")
            else:
                self.response_changed.emit(str(answer))
                self.state_changed.emit("complete")

        except Exception as error:
            if cancel_event.is_set() or self._shutting_down:
                return
            print(
                f"[Misty] Request processing error: {error}",
                flush=True,
            )
            self.activity_event.emit({
                "kind": "error",
                "title": "Request failed",
                "details": str(error),
                "status": "failure",
            })
            self.response_changed.emit(
                f"Something went wrong: {error}"
            )
            self.state_changed.emit("error")
            self._task_card_abort(f"Failed: {error}")

    def _cleanup_after_start_error(self):
        self._cleanup_models()
        self._voice_cancel_event = None
        self._busy = False
        self._notify_escape_state()

    # ------------------------------------------------------------------
    # Work queries and ownership-aware model cleanup
    # ------------------------------------------------------------------

    @staticmethod
    def _thread_alive(thread):
        return thread is not None and thread.is_alive()

    def _worker_alive(self, exclude=None):
        worker = self._worker
        if worker is None or not worker.is_alive():
            return False
        if exclude is not None and exclude is worker:
            return False
        # The cleanup thread created for a failed microphone start is not
        # a request worker: it never runs an agent or a transcriber.
        return worker is not self._start_error_thread

    def has_live_work(self):
        """True while an agent request, decode or held recording is in flight.

        Used by shutdown to decide what it may tear down, and by admission
        so a stale ``_busy`` flag cannot block unrelated requests.
        """
        return bool(
            self._recording
            or self._confirmation_recording
            or self._worker_alive()
            or self._thread_alive(self._confirmation_worker)
        )

    def _cleanup_models(self, owner=None, join_timeout=PRELOAD_JOIN_SECONDS):
        """Unload Whisper/Ministral, but only the generation ``owner`` owns.

        ``owner`` is the agent created for the calling request (``None`` for
        the microphone-start failure path). Cleanup never joins preload
        threads unboundedly and never nulls models that a live worker is
        still using: when another request worker is alive, only the caller's
        own agent is dropped and models stay in place for that worker.
        """
        with self._cleanup_lock:
            caller = _current_thread()
            worker = self._worker
            workers_alive = sum(
                1 for thread in (caller, worker) if _is_worker_thread(self, thread)
            )
            others_running = workers_alive > 1
            agent = self._agent
            owns_agent = owner is not None and owner is agent
            can_drop_models = not others_running and not self._worker_alive(
                exclude=caller
            )

            if can_drop_models:
                self._join_preload_bounded(timeout=join_timeout)
                transcriber = self._transcriber
                if transcriber is not None:
                    try:
                        transcriber.unload()
                    except Exception as error:
                        print(f"[Misty] Whisper cleanup error: {error}",
                              flush=True)
                self._transcriber = None
                self._whisper_thread = None

            if owns_agent:
                _stop_if_running(owner)
                if self._agent is owner:
                    self._agent = None
                    self._agent_thread = None
                agent = None
            if can_drop_models:
                # Ownerless cleanup (idle shutdown, microphone-start
                # failure) releases whatever generation is still recorded.
                if not owns_agent and agent is not None:
                    _stop_if_running(agent)
                    if self._agent is agent:
                        self._agent = None
                        self._agent_thread = None

            if self.window_service is not None and not self.has_live_work():
                try:
                    self.window_service.clear_active_window()
                except Exception:
                    pass

    def _join_preload_bounded(self, timeout=PRELOAD_JOIN_SECONDS):
        if timeout <= 0:
            return
        for name in ("_whisper_thread", "_agent_thread"):
            thread = getattr(self, name, None)
            if not self._thread_alive(thread):
                continue
            if _current_thread() is thread:
                continue
            thread.join(timeout=timeout)
            if thread.is_alive():
                print(
                    f"[Misty] {name[1:]} still running after "
                    f"{timeout:g}s; leaving it to finish.",
                    flush=True,
                )

    def _schedule_ready_reset(self):
        if self._has_pending_window_selection():
            self._completion_timer.stop()
            self._completion_waiting_for_hover = False
            return

        self._completion_waiting_for_hover = True
        if not self.popup_hovered:
            self._completion_timer.start(7000)

    def _handle_popup_hover_changed(self, hovered):
        if not self._completion_waiting_for_hover:
            return

        if hovered:
            self._completion_timer.stop()
        elif not self._shutting_down and not self._recording and not self._busy:
            self._completion_timer.start(7000)

    def _return_to_ready(self):
        if (
            self._shutting_down
            or self._recording
            or self._busy
            or self._has_pending_window_selection()
        ):
            return

        if self.popup_hovered:
            self._completion_waiting_for_hover = True
            return

        self._completion_waiting_for_hover = False
        self.state_changed.emit("ready")
        self.popup_ipc.send({"type": "hide"})

    def shutdown(self):
        """Stop the controller within a short, fixed time budget.

        Rules:
        * flags first (no new recording/confirmation/request is accepted);
        * deny whatever confirmation is pending through the ID-bound path;
        * abort any recording, signal ``cancel_event`` on a running agent,
          stop the helper;
        * only clean models here when no request worker is alive — a live
          worker owns its agent/transcriber and cleans them up itself.
        * never join a thread for more than :data:`SHUTDOWN_TOTAL_BUDGET_SECONDS`
          in total.
        """
        deadline = time.monotonic() + SHUTDOWN_TOTAL_BUDGET_SECONDS
        self._shutting_down = True
        self._confirm_pending(False)
        self._abort_recording("Misty is shutting down.")
        # Conversation context never outlives the process.
        self._session_shutdown_clear()

        # Cooperative cancellation for the in-flight request (agent.run must
        # return promptly once this event is set): the agent's own event plus
        # the per-turn events the task card uses.
        agent = self._agent
        with self._task_state_lock():
            for extra in (self._task_cancel_event,):
                if extra is not None and hasattr(extra, "set"):
                    try:
                        extra.set()
                    except Exception:
                        pass
        event = None
        if agent is not None:
            event = getattr(agent, "cancel_event", None)
        if event is not None and hasattr(event, "set"):
            try:
                event.set()
            except Exception:
                pass

        # Stop the helper with a short wait; it may be mid-RESTART elsewhere.
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.terminate()
            if not self.process.waitForFinished(SHUTDOWN_HELPER_BUDGET_MS):
                self.process.kill()
                self.process.waitForFinished(300)

        for thread in (self._worker, self._confirmation_worker):
            remaining = deadline - time.monotonic()
            if thread is None or not thread.is_alive() or remaining <= 0:
                continue
            if _current_thread() is thread:
                continue
            thread.join(timeout=remaining)
            if thread.is_alive():
                print(
                    "[Misty] A background request is still finishing; "
                    "leaving its models to it.",
                    flush=True,
                )

        self._confirmation_request_id = None

        if not self._worker_alive() and not self._thread_alive(
            self._confirmation_worker
        ):
            # Preload threads get at most what is left of the budget.
            self._cleanup_models(join_timeout=max(0.0, deadline - time.monotonic()))
        else:
            print(
                "[Misty] Live work during shutdown: models are owned by "
                "the running request and will be released when it ends.",
                flush=True,
            )

        self._cancel_watchdog()
        self._clear_pending_window_selection()
        self.recorder.close()
        if self.window_service is not None and not self.has_live_work():
            try:
                self.window_service.clear_active_window()
            except Exception:
                pass
        print("[Misty] Voice controller shut down.", flush=True)
