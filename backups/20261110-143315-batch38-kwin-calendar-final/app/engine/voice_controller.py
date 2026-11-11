"""Connect Misty's hold-to-talk pipeline to the native Qt desktop app."""

import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal, Slot

from app.actions.executor import ActionExecutor
from app.brain.agent import MistyAgent
from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry
from app.engine.confirmations import ConfirmationBroker
from app.speech.recorder import Recorder
from app.speech.transcriber import Transcriber


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "kde-shortcut" / "build" / "misty-shortcut"
WINDOW_SELECTION_TIMEOUT_SECONDS = 60.0


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

    def __init__(self, popup_ipc, parent=None, window_service=None, clipboard_bridge=None, vision_service=None, confirmation_broker=None, audit_log=None, web_search_client=None, knowledge_index=None, task_journal=None, browser_session=None, reminder_store=None, file_transfer_engine=None, control_activator=None, media_controller=None, zip_engine=None, text_search_service=None, directory_transfer_engine=None, calendar_engine=None):
        super().__init__(parent)
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
        self._busy = False
        self._shutting_down = False

        self._transcriber = None
        self._whisper_thread = None
        self._agent = None
        self._agent_thread = None
        self._agent_errors = []
        self._worker = None

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
        self.process.start(str(HELPER), [])

        if not self.process.waitForStarted(3000):
            raise RuntimeError(
                f"Could not start shortcut helper: "
                f"{self.process.errorString()}"
            )

        print("[Misty] Voice controller ready. Hold F8 to talk.", flush=True)

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
            elif event:
                print(f"[Misty] {event}", flush=True)

    def _helper_error(self, error):
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
        if not self._shutting_down:
            print(
                f"[Misty] Shortcut helper stopped (code {exit_code}).",
                flush=True,
            )
            self.state_changed.emit("error")
            self.response_changed.emit(
                "The keyboard shortcut listener stopped."
            )

    def _agent_activity_event(self, event):
        self.activity_event.emit(event)

    def _request_confirmation(self, name, arguments, description):
        if self._shutting_down:
            return False

        pending = self._confirmation_broker.begin(
            name, arguments, description
        )
        if pending is None:
            return False
        if self._shutting_down:
            self._confirmation_broker.cancel()
            return False

        self.activity_event.emit({
            "kind": "confirmation",
            "title": f"Approval requested: {name}",
            "details": description,
            "status": "pending",
        })
        self.confirmation_requested.emit({
            "request_id": pending["request_id"],
            "name": name,
            "arguments": self._confirmation_arguments(name, arguments),
            "description": description,
        })

        approved = self._confirmation_broker.wait(pending)

        self.confirmation_resolved.emit(approved)
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

    def resolve_confirmation(self, approved, request_id=None):
        if self._shutting_down:
            approved = False

        resolved = self._confirmation_broker.resolve(
            approved, request_id=request_id
        )
        if resolved:
            self.confirmation_resolved.emit(bool(approved))
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
        """Resolve a spoken follow-up locally, without asking Qwen."""
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
        )

    @Slot(str)
    def submit_text(self, text):
        """Handle a typed request through the same agent path as voice."""
        if self._shutting_down or not isinstance(text, str):
            return
        text = text.strip()
        if not text:
            return
        if len(text) > 2000:
            self.response_changed.emit(
                "That request is too long. Please keep it under 2000 characters."
            )
            return
        if self._busy or self._recording or self._confirmation_recording:
            self.response_changed.emit(
                "I'm busy with another request. Please wait a moment."
            )
            return

        self.popup_ipc.send({"type": "show"})
        self.transcript_changed.emit(text)
        self.response_changed.emit("Working out what you need...")
        self.activity_event.emit({
            "kind": "request",
            "title": "You typed",
            "details": text,
            "status": "info",
        })
        self.state_changed.emit("thinking")
        self._busy = True
        self._worker = threading.Thread(
            target=self._process_typed_request,
            args=(text,),
            name="misty-typed-request",
            daemon=True,
        )
        self._worker.start()

    def _process_typed_request(self, text):
        try:
            self._agent = MistyAgent(
                registry=self._build_registry(),
                event_callback=self._agent_activity_event,
                confirmation_callback=self._request_confirmation,
                audit_log=self.audit_log,
                task_journal=self.task_journal,
                content_selection_callback=self._set_pending_content_selection,
            )
            answer = self._agent.run(text)
            self.activity_event.emit({
                "kind": "response",
                "title": "Misty replied",
                "details": str(answer),
                "status": "success",
            })
            self.response_changed.emit(str(answer))
            self.state_changed.emit("complete")
        except Exception as error:
            print(f"[Misty] Typed request error: {error}", flush=True)
            self.response_changed.emit(
                f"Something went wrong: {error}"
            )
            self.state_changed.emit("error")
        finally:
            self._cleanup_models()
            self._busy = False
            if not self._shutting_down:
                self.ready_reset_requested.emit()

    def _press(self):
        if (
            self._shutting_down
            or self._recording
            or self._confirmation_recording
        ):
            return

        confirmation_pending = self._confirmation_broker.is_pending()

        if self._busy:
            if not confirmation_pending:
                return

            try:
                self.recorder.start()
                self._confirmation_recording = True
                self.state_changed.emit("listening")
                self.response_changed.emit(
                    "Say confirm or cancel, then release F8."
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

        self._transcriber = Transcriber()
        self._whisper_thread = threading.Thread(
            target=self._transcriber.load,
            name="misty-whisper-preload",
            daemon=True,
        )
        self._whisper_thread.start()

        registry = self._build_registry()
        self._agent = MistyAgent(
            registry=registry,
            event_callback=self._agent_activity_event,
            confirmation_callback=self._request_confirmation,
            audit_log=self.audit_log,
            task_journal=self.task_journal,
            content_selection_callback=self._set_pending_content_selection,
        )
        self._agent_errors = []
        self._agent_thread = threading.Thread(
            target=self._preload_agent,
            name="misty-qwen-preload",
            daemon=True,
        )
        self._agent_thread.start()

        try:
            self.recorder.start()
            self._recording = True
        except Exception as error:
            print(f"[Misty] Recording start error: {error}", flush=True)
            self._busy = True
            self.state_changed.emit("error")
            self.response_changed.emit(
                f"Couldn't start the microphone: {error}"
            )
            self._worker = threading.Thread(
                target=self._cleanup_after_start_error,
                daemon=True,
            )
            self._worker.start()

    def _preload_agent(self):
        try:
            self._agent.start()
        except Exception as error:
            self._agent_errors.append(error)
            print(f"[Misty] Qwen preload error: {error}", flush=True)

    def _process_confirmation_audio(self, audio):
        try:
            if audio is None or self._transcriber is None:
                self.response_changed.emit(
                    "I didn't hear a confirmation. Please try again."
                )
                return

            result = self._transcriber.transcribe(audio)
            transcript = str(result.get("text", "")).strip()
            print(
                f"[Misty] Spoken confirmation: {transcript}",
                flush=True,
            )

            import re

            normalized = transcript.casefold()
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
                resolved = self.resolve_confirmation(True)
                if resolved:
                    self.response_changed.emit("Confirmation accepted.")
                else:
                    self.response_changed.emit(
                        "That confirmation request has already expired."
                    )
                return

            if normalized in reject_phrases:
                resolved = self.resolve_confirmation(False)
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

    def _release(self):
        if self._shutting_down:
            return

        if self._confirmation_recording:
            self._confirmation_recording = False
            try:
                audio = self.recorder.stop()
            except Exception as error:
                audio = None
                print(
                    f"[Misty] Confirmation recording stop error: {error}",
                    flush=True,
                )

            worker = threading.Thread(
                target=self._process_confirmation_audio,
                args=(audio,),
                name="misty-voice-confirmation",
                daemon=True,
            )
            worker.start()
            return

        if not self._recording or self._busy:
            return

        print("[Misty] Shortcut released.", flush=True)
        self._recording = False
        self._busy = True
        self.state_changed.emit("transcribing")

        try:
            audio = self.recorder.stop()
        except Exception as error:
            audio = None
            print(f"[Misty] Recording stop error: {error}", flush=True)

        self._worker = threading.Thread(
            target=self._process_request,
            args=(audio,),
            name="misty-voice-request",
            daemon=True,
        )
        self._worker.start()

    def _process_request(self, audio):
        try:
            if audio is None:
                self.response_changed.emit(
                    "I didn't hear anything. Try holding F8 a little longer."
                )
                self.state_changed.emit("ready")
                return

            if self._whisper_thread is not None:
                self._whisper_thread.join()

            started = time.perf_counter()
            result = self._transcriber.transcribe(audio)
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
                "details": "Local speech recognition and the local Qwen model.",
                "status": "info",
            })
            self.response_changed.emit("Working out what you need...")
            self.state_changed.emit("thinking")

            if self._agent_thread is not None:
                self._agent_thread.join()

            if self._agent_errors:
                print(
                    "[Misty] Qwen preload failed; agent will retry startup.",
                    flush=True,
                )

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
                return

            started = time.perf_counter()
            answer = self._agent.run(transcript)

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

        finally:
            self._cleanup_models()
            self._busy = False

            if not self._shutting_down:
                self.ready_reset_requested.emit()

    def _cleanup_after_start_error(self):
        self._cleanup_models()
        self._busy = False

    def _cleanup_models(self):
        if self._whisper_thread is not None:
            self._whisper_thread.join()

        if self._agent_thread is not None:
            self._agent_thread.join()

        if self._transcriber is not None:
            try:
                self._transcriber.unload()
            except Exception as error:
                print(f"[Misty] Whisper cleanup error: {error}", flush=True)

        if self._agent is not None and self._agent.process is not None:
            try:
                self._agent.stop()
            except Exception as error:
                print(f"[Misty] Qwen cleanup error: {error}", flush=True)

        self._transcriber = None
        self._whisper_thread = None
        self._agent = None
        self._agent_thread = None

        if self.window_service is not None:
            self.window_service.clear_active_window()

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
        self._shutting_down = True
        self.resolve_confirmation(False)

        if self._recording:
            try:
                self.recorder.stop()
            except Exception:
                pass
            self._recording = False

        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.terminate()
            if not self.process.waitForFinished(2000):
                self.process.kill()
                self.process.waitForFinished(1000)

        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=10)

        self._cleanup_models()
        self.recorder.close()
        print("[Misty] Voice controller shut down.", flush=True)
