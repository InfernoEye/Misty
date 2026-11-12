import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QLockFile, QObject, QProcess, QTimer, Slot
from PySide6.QtWidgets import (
    QApplication,
    QMessageBox,
    QSystemTrayIcon,
)

from app.browser.session import BrowserSession
from app.actions.file_transfer import FileTransferEngine
from app.actions.archive_zip import ZipEngine
from app.actions.text_search import TextSearchService
from app.actions.directory_transfer import DirectoryTransferEngine
from app.actions.calendar_ics import IcsCalendar
from app.actions.duplicates import DuplicateFinder
from app.speech.file_transcriber import FileTranscriber
from app.desktop.accessibility.activator import ControlActivator
from app.desktop.media import MediaController
from app.automation.reminders import ReminderStore
from app.engine.kwin_windows import KWinWindowService
from app.desktop.context import DesktopContext
from app.desktop.vision.capture import CaptureService
from app.desktop.vision.kwin_capture import KWinWindowCapture
from app.desktop.vision.ocr import OCRService
from app.brain.agent import BASE_URL, MODEL_PATH
from app.desktop.vision.vlm import VisionLanguageService
from app.desktop.vision.coordinator import DesktopVision
from app.engine.audit_log import AuditLog, TaskJournal
from app.engine.clipboard_bridge import ClipboardBridge
from app.engine.process_guard import (
    cleanup_orphan_model_servers,
    cleanup_orphan_popup_processes,
)
from app.engine.popup_ipc import PopupIpcServer
from app.engine.voice_controller import VoiceController
from app.knowledge.index import KnowledgeIndex
from app.web.search import SearxngClient
from app.ui.main_window import MainWindow
from app.ui.response_window import (
    MistyResponseWindow,
    should_show_full_response,
)
from app.ui.tray import MistyTray, make_misty_icon


ROOT = Path(__file__).resolve().parents[1]




class MistyApplication(QObject):
    def __init__(self):
        super().__init__()

        self.app = QApplication(sys.argv)
        self.app.setApplicationName("Misty")
        self.app.setOrganizationName("Misty")
        self.app.setDesktopFileName("misty")
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setWindowIcon(make_misty_icon())

        self._shutting_down = False

        self.main_window = MainWindow()
        self.response_window = MistyResponseWindow()
        self.browser_session = BrowserSession()
        self.reminder_store = ReminderStore(
            ROOT / "data" / "reminders.sqlite3"
        )
        self.file_transfer_engine = FileTransferEngine()
        self.zip_engine = ZipEngine()
        self.text_search_service = TextSearchService()
        self.directory_transfer_engine = DirectoryTransferEngine()
        self.calendar_engine = IcsCalendar()
        self.duplicate_finder = DuplicateFinder()
        self.file_transcriber = FileTranscriber()
        self.control_activator = ControlActivator()
        self.media_controller = MediaController()
        self.tray = MistyTray(
            self.app, self.main_window, self.response_window
        )
        self._latest_response = ""
        self._latest_transcript = ""
        self._last_shown_pair = None
        self._last_chat_pair = None
        # Origin is tracked per admitted request via the controller token;
        # this mirrors the controller for the active request only.
        self._typed_request = False
        self._active_request_id = None
        self._applying_shortcut = None
        self._request_tools = set()

        self.popup_server = PopupIpcServer(parent=self.app)
        self.popup_server.message_received.connect(self._handle_popup_message)
        self.popup_process = QProcess(self.app)
        self.popup_process.setWorkingDirectory(str(ROOT))
        self.popup_process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        self.popup_process.readyReadStandardOutput.connect(
            self._read_popup_output
        )
        self.popup_process.finished.connect(self._popup_finished)
        self.popup_process.errorOccurred.connect(self._popup_error)

        self.window_service = KWinWindowService()
        self.desktop_context = DesktopContext(self.window_service)
        self.vision_service = DesktopVision(
            capture_service=CaptureService(
                context_provider=self.desktop_context.snapshot,
                target_window_provider=self.window_service.get_captured_active_window,
                kwin_capture=KWinWindowCapture(),
            ),
            ocr_service=OCRService(),
            vlm_service=VisionLanguageService(
                base_url=BASE_URL,
                model_name=MODEL_PATH.name,
            ),
        )
        self.clipboard_bridge = ClipboardBridge(parent=self.app)
        try:
            self.knowledge_index = KnowledgeIndex(
                ROOT / "data" / "knowledge.sqlite3"
            )
        except Exception as error:
            self.knowledge_index = None
            print(
                f"[Misty] Local knowledge index unavailable: {error}",
                flush=True,
            )
        try:
            self.task_journal = TaskJournal(
                ROOT / "data" / "tasks.sqlite3"
            )
        except Exception as error:
            self.task_journal = None
            print(
                f"[Misty] Task journal unavailable: {error}",
                flush=True,
            )
        self.web_search_client = None
        searxng_url = (
            os.environ.get("MISTY_SEARXNG_URL", "").strip()
            or "http://127.0.0.1:8080"
        )
        try:
            parts = urlsplit(searxng_url)
            host = parts.hostname
            port = parts.port or (443 if parts.scheme == "https" else 80)
            reachable = False
            if host:
                try:
                    with socket.create_connection(
                        (host, port), timeout=0.4
                    ):
                        reachable = True
                except OSError:
                    reachable = False
            if reachable:
                self.web_search_client = SearxngClient(searxng_url)
                print(
                    f"[Misty] Web search configured: {searxng_url}",
                    flush=True,
                )
            else:
                print(
                    "[Misty] Web search unavailable: local SearXNG is not "
                    "reachable.",
                    flush=True,
                )
        except Exception as error:
            print(
                f"[Misty] Web search disabled: {error}",
                flush=True,
            )
        try:
            self.audit_log = AuditLog(ROOT / "logs" / "audit")
        except Exception as error:
            self.audit_log = None
            print(f"[Misty] Audit log unavailable: {error}", flush=True)
        self.voice_controller = VoiceController(
            self.popup_server,
            parent=self.app,
            window_service=self.window_service,
            clipboard_bridge=self.clipboard_bridge,
            vision_service=self.vision_service,
            audit_log=self.audit_log,
            web_search_client=self.web_search_client,
            knowledge_index=self.knowledge_index,
            task_journal=self.task_journal,
            browser_session=self.browser_session,
            reminder_store=self.reminder_store,
            file_transfer_engine=self.file_transfer_engine,
            zip_engine=self.zip_engine,
            text_search_service=self.text_search_service,
            directory_transfer_engine=self.directory_transfer_engine,
            calendar_engine=self.calendar_engine,
            duplicate_finder=self.duplicate_finder,
            file_transcriber=self.file_transcriber,
            control_activator=self.control_activator,
            media_controller=self.media_controller,
            active_window_provider=self.window_service.get_captured_active_window,
        )
        # Model exclusivity: the single-owner arbiter records which heavy
        # local model may be loaded, and the pre-VLM/pre-whisper hooks still
        # release the chat model (the controller only honours that for its
        # own active request worker). Both are installed so the arbiter's
        # view and the actual process state cannot drift apart.
        unload_reasoning = getattr(
            self.voice_controller, "unload_reasoning_model", None
        )
        start_arbiter = getattr(self.voice_controller, "model_arbiter", None)
        if callable(start_arbiter):
            try:
                # Creating it here registers the reasoning model and makes
                # the arbiter part of the running app rather than dead code.
                start_arbiter()
            except Exception as error:
                print(f"[Misty] Model arbiter setup error: {error}", flush=True)
        if callable(unload_reasoning):
            self.voice_controller.set_before_transcribe(unload_reasoning)
        self.voice_controller.activity_event.connect(
            self.main_window.add_activity_event
        )
        self.voice_controller.activity_event.connect(
            self._track_request_tool
        )
        self.reminder_timer = QTimer(self.app)
        self.reminder_timer.setInterval(20_000)
        self.reminder_timer.timeout.connect(self._check_reminders)
        self.reminder_timer.start()
        QTimer.singleShot(2_000, self._check_reminders)
        self.voice_controller.response_changed.connect(
            self._capture_response_text
        )
        self.voice_controller.transcript_changed.connect(
            self._capture_transcript_text
        )
        self.voice_controller.state_changed.connect(
            self._handle_voice_state
        )
        self.voice_controller.confirmation_requested.connect(
            self._handle_confirmation_request
        )
        self.voice_controller.confirmation_resolved.connect(
            self._handle_confirmation_resolved
        )
        self.main_window.text_submitted.connect(self._submit_typed_text)
        self.main_window.shortcut_changed.connect(self._apply_shortcut)
        self.main_window.shortcut_apply_requested.connect(self._apply_shortcut)
        self.main_window.shortcut_recording_started.connect(
            self.voice_controller.suspend_shortcut
        )
        self.main_window.shortcut_recording_finished.connect(
            self.voice_controller.resume_shortcut
        )
        self.voice_controller.shortcut_ready.connect(
            self._handle_shortcut_ready
        )
        self.voice_controller.typed_rejected.connect(
            self._handle_typed_rejected
        )
        self.voice_controller.typed_failed.connect(self._handle_typed_failed)
        self.voice_controller.typed_completed.connect(
            self._handle_typed_completed
        )
        self.voice_controller.transcript_changed.connect(
            self.main_window.append_voice_transcript
        )
        self._task_card_connect()

        self.app.aboutToQuit.connect(self.shutdown)

    # ------------------------------------------------------------------
    # Task card (progress, cancel, retry) and conversation context
    # ------------------------------------------------------------------

    def _task_card_connect(self):
        """Wire the task card to the controller; every step is optional."""
        card = getattr(self.main_window, "task_card", None)
        self._task_card_visible_for = None
        if card is not None:
            card.cancel_requested.connect(self._task_card_cancel)
            card.retry_requested.connect(self._task_card_retry)
            started = getattr(self.voice_controller, "task_started", None)
            if started is not None:
                started.connect(self._task_card_started)
            events = getattr(self.voice_controller, "task_event", None)
            if events is not None:
                events.connect(self._task_card_event)
            finished = getattr(self.voice_controller, "task_finished", None)
            if finished is not None:
                finished.connect(self._task_card_finished)
            # Keep the global event stream as a fallback source too: the
            # card ignores whatever it already rendered (same status).
            self.voice_controller.activity_event.connect(
                self._task_card_activity
            )
        forget = getattr(self.main_window, "forget_context_requested", None)
        if forget is not None:
            forget.connect(self._forget_conversation_context)

    @Slot(dict)
    def _task_card_started(self, task):
        """Show the card for one request (typed or voice)."""
        card = getattr(self.main_window, "task_card", None)
        if card is None:
            return
        task = task if isinstance(task, dict) else {}
        task_id = task.get("event_id")
        self._task_card_visible_for = task_id
        card.set_task(str(task.get("request") or "Task"), job=task_id or "")
        card.set_retryable_tools(self._task_card_retry_blocked())

    def _task_card_retry_blocked(self):
        """Tools whose registry handler forbids a replay (best effort)."""
        blocked = set()
        build = getattr(self.voice_controller, "_build_registry", None)
        if not callable(build):
            return blocked
        try:
            registry = build()
        except Exception:
            return blocked
        try:
            tools = registry.available_tools()
        except Exception:
            tools = []
        for item in tools or ():
            function = item.get("function") if isinstance(item, dict) else None
            tool = function.get("name") if isinstance(function, dict) else ""
            if not isinstance(tool, str) or not tool:
                # A registry that only exposes the policy's own names.
                tool = item if isinstance(item, str) else ""
            if not tool:
                continue
            try:
                handler = registry.get_handler(tool)
            except Exception:
                handler = None
            if isinstance(handler, dict) and handler.get("idempotent") is False:
                blocked.add(tool)
        return blocked

    @Slot(dict)
    def _task_card_event(self, event):
        card = getattr(self.main_window, "task_card", None)
        if card is not None and isinstance(event, dict):
            card.apply_event(event)

    @Slot(dict)
    def _task_card_activity(self, event):
        """Fallback source when the task_* signals are absent.

        The event is already forwarded to the card through ``task_event``;
        this path only applies when the controller has no such signal, and
        the card ignores an event it already rendered (same status).
        """
        if getattr(self.voice_controller, "task_event", None) is not None:
            return
        self._task_card_event(event)

    @Slot(dict)
    def _task_card_finished(self, task):
        card = getattr(self.main_window, "task_card", None)
        if card is None:
            return
        self._task_card_visible_for = None
        task = task if isinstance(task, dict) else {}
        status = str(task.get("status") or "finished")
        message = task.get("message")
        if isinstance(message, str) and message.strip():
            card.apply_event({
                "kind": "response",
                "title": "Misty replied",
                "details": message,
                "status": "success",
            })
        card.finish(status)

    @Slot()
    def _task_card_cancel(self):
        cancel = getattr(self.voice_controller, "cancel_active_request", None)
        if not callable(cancel):
            return
        if not cancel():
            card = getattr(self.main_window, "task_card", None)
            if card is not None:
                card.apply_event({
                    "kind": "decision",
                    "title": "Task cancelled",
                    "details": "Nothing was running to cancel.",
                    "status": "planned",
                })

    @Slot(str)
    def _task_card_retry(self, tool):
        retry = getattr(
            self.voice_controller, "retry_last_failed_step", None
        )
        card = getattr(self.main_window, "task_card", None)
        if not callable(retry):
            return
        try:
            answer = retry(tool)
        except Exception as error:
            answer = f"The retry could not start: {error}"
        if answer == "started":
            # task_started (retry=True) keeps the same card alive.
            return
        notice = str(answer or "That step cannot be retried.")
        self.main_window.append_assistant_error(notice)
        if card is not None:
            card.apply_event({
                "kind": "response",
                "title": "Retry not started",
                "details": notice,
                "status": "info",
            })

    @Slot()
    def _forget_conversation_context(self):
        clear = getattr(
            self.voice_controller, "clear_session_context", None
        )
        if callable(clear):
            try:
                clear()
            except Exception as error:
                print(f"[Misty] Context clear failed: {error}", flush=True)
        self.main_window.append_assistant_message(
            "Conversation context forgotten. I will not refer to the "
            "previous requests any more."
        )

    @Slot()
    def _check_reminders(self):
        if self._shutting_down:
            return
        try:
            reminders = self.reminder_store.due(limit=20)
        except Exception:
            return
        for reminder in reminders:
            text = str(reminder.get("text", "")).strip()
            if not text:
                continue
            self.tray.show_notification("Misty reminder", text)
            self.main_window.add_activity_event({
                "kind": "reminder",
                "title": "Reminder",
                "details": text,
                "status": "info",
            })
            try:
                self.reminder_store.mark_fired(reminder["id"])
            except Exception:
                pass

    @Slot(str)
    def _submit_typed_text(self, text):
        """Admit a typed request; only acceptance changes request state.

        The chat bubble and input clearing happen after admission, so a
        rejected request (busy/oversize) restores the text instead of being
        silently eaten, and never re-origins an in-flight voice request.
        """
        admission = self.voice_controller.submit_text(text)
        accepted = bool(getattr(admission, "accepted", admission is True))
        request_id = getattr(admission, "request_id", None)

        if accepted:
            self._typed_request = True
            self._active_request_id = request_id
            self.main_window.append_user_message(text)
            self.main_window.focus_chat_reply_target()
            return

        reason = str(getattr(admission, "reason", "") or "")
        self._active_request_id = None
        self.main_window.restore_chat_input(text)
        if reason:
            self.main_window.append_assistant_error(reason)
        return admission

    @Slot(str, str, str)
    def _handle_typed_rejected(self, request_id, text, reason):
        # Rejections are also reported via the admission token; this path
        # only feeds the activity list so a failure is never invisible.
        self.main_window.append_assistant_error(str(reason or ""))

    @Slot(str, str, str)
    def _handle_typed_failed(self, request_id, text, message):
        if request_id != self._active_request_id:
            # A stale failure from a superseded request.
            return
        self.main_window.append_assistant_error(
            str(message or "Something went wrong.")
        )
        self._active_request_id = None
        self._typed_request = False

    @Slot(str, str, str)
    def _handle_typed_completed(self, request_id, text, detail):
        if request_id != self._active_request_id:
            return
        self._active_request_id = None
        self._typed_request = False

    @Slot(bool, str)
    def _handle_shortcut_ready(self, success, reason, sequence=None):
        """Final word from the controller: persist only on real success."""
        if self._applying_shortcut and sequence is None:
            sequence = self._applying_shortcut
        if not self._applying_shortcut:
            # Unsolicited READY/ERROR (startup helper, external restart).
            if not success and not self._shutting_down:
                self.main_window.show_shortcut_failure(str(reason or ""))
            else:
                self.main_window.refresh_shortcut_hint()
            return
        self._applying_shortcut = None
        if success:
            self.main_window.shortcut_applied(sequence)
        elif not self._shutting_down:
            # Regression: persisted before applying. Never again — the file
            # still holds the previous value, and the helper was rolled back.
            self.main_window.show_shortcut_failure(str(reason or ""))

    @Slot(str)
    def _apply_shortcut(self, sequence):
        """Explicit Apply/Retry: re-apply the saved chord, even if unchanged.

        The controller restarts the helper and only reports success once the
        native helper confirmed the chord with READY; ``shortcut_ready``
        completes the flow and persists on success. This method never
        overwrites a failure with an optimistic message.
        """
        self._applying_shortcut = sequence
        applied = self.voice_controller.set_shortcut(sequence)
        if applied:
            self._handle_shortcut_ready(True, "", sequence)
        elif self._applying_shortcut is not None:
            # A controller that returns without emitting shortcut_ready.
            if not self._signals_ready(self.voice_controller):
                self._applying_shortcut = None
                self.main_window.show_shortcut_failure(
                    "the shortcut could not be registered"
                )
        return applied

    @staticmethod
    def _signals_ready(controller):
        try:
            controller.shortcut_ready.connect
        except Exception:
            return False
        return True

    @Slot(str)
    def _capture_response_text(self, text):
        self._latest_response = text if isinstance(text, str) else ""

    @Slot(str)
    def _capture_transcript_text(self, text):
        self._latest_transcript = text if isinstance(text, str) else ""
        self._request_tools = set()

    @Slot(dict)
    def _track_request_tool(self, event):
        if not isinstance(event, dict):
            return
        tool = event.get("tool")
        if isinstance(tool, str) and tool:
            self._request_tools.add(tool)

    @Slot(str)
    def _handle_voice_state(self, state):
        if state == "listening":
            self._typed_request = False
            self._active_request_id = None
        # "error" is deliberately not handled here: a typed request's
        # identity is cleared by its own typed_failed/typed_completed
        # callback, which carries the request id. Clearing on the generic
        # state would drop the bubble for the very request that failed.
        if state == "complete":
            QTimer.singleShot(0, self._append_chat_response)
            QTimer.singleShot(0, self._maybe_show_full_response)

    def _append_chat_response(self):
        response = self._latest_response.strip()
        if not response:
            return
        pair = (self._latest_transcript, response)
        if pair == self._last_chat_pair:
            return
        self._last_chat_pair = pair
        self.main_window.append_assistant_message(response)

    def _current_request_origin(self):
        """Origin of the request that produced the current response.

        Read from the controller so a rejected typed request can never
        change the origin of a voice request that is still in flight.
        """
        origin_for = getattr(self.voice_controller, "current_request_origin", None)
        if callable(origin_for):
            try:
                return origin_for()
            except Exception:
                return None
        return None

    def _maybe_show_full_response(self):
        response = self._latest_response.strip()
        if not response:
            return
        origin = self._current_request_origin()
        if origin is not None:
            if origin != "voice":
                return
        elif self._typed_request:
            return
        pair = (self._latest_transcript, response)
        if pair == self._last_shown_pair:
            return
        if should_show_full_response(response, self._request_tools):
            self._last_shown_pair = pair
            self.response_window.show_response(
                response, self._latest_transcript
            )


    def _read_popup_output(self):
        output = bytes(
            self.popup_process.readAllStandardOutput()
        ).decode(errors="replace")
        if output:
            for line in output.splitlines():
                print(line, flush=True)

    def _popup_error(self, error):
        if not self._shutting_down:
            print(
                f"[Misty] Popup process error: "
                f"{self.popup_process.errorString()}",
                flush=True,
            )

    def _start_popup_process(self):
        if self._shutting_down:
            return

        if self.popup_process.state() != QProcess.ProcessState.NotRunning:
            return

        self.popup_process.start(
            sys.executable,
            ["-m", "app.popup_process"],
        )

    def _popup_finished(self, exit_code, exit_status):
        if self._shutting_down:
            return

        print(
            f"[Misty] Popup process stopped (code {exit_code}); "
            "restarting shortly.",
            flush=True,
        )
        QTimer.singleShot(1500, self._start_popup_process)

    @Slot(dict)
    def _handle_confirmation_request(self, request):
        name = str(request.get("name", "Unknown action"))
        description = str(
            request.get("description", "No description available.")
        )
        arguments = request.get("arguments", {})
        title = "Misty needs your approval"

        if name == "close_window":
            window_id = (
                arguments.get("window_id")
                if isinstance(arguments, dict) else None
            )

            try:
                result = self.window_service.list_windows()
                windows = (
                    result.get("windows", [])
                    if result.get("success") else []
                )
                target = next(
                    (
                        window for window in windows
                        if window.get("id") == window_id
                    ),
                    None,
                )
            except Exception:
                target = None

            if target is None:
                print(
                    "[Misty] Confirmation denied: target window "
                    "could not be verified.",
                    flush=True,
                )
                self.voice_controller.resolve_confirmation(
                    False, request.get("request_id")
                )
                return

            title = f"Close {target.get('title') or 'this window'}?"
            app_id = str(target.get("app_id") or "Unknown application")
            description = (
                f"Application: {app_id}\n\n"
                "Closing may interrupt work or cause unsaved changes "
                "to be lost. Misty will request closure; the application "
                "may still prevent it."
            )
        else:
            details = json.dumps(arguments, ensure_ascii=False)
            description = f"{description}\n\nAction: {name} {details}"

        sent = self.popup_server.send({
            "type": "confirmation",
            "request_id": request.get("request_id"),
            "title": title,
            "description": description,
        })

        if not sent:
            print(
                "[Misty] Confirmation denied: popup is not connected.",
                flush=True,
            )
            self.voice_controller.resolve_confirmation(
                False, request.get("request_id")
            )

    @Slot(dict)
    def _handle_popup_message(self, message):
        if message.get("type") == "confirmation_response":
            request_id = message.get("request_id")
            if not isinstance(request_id, str) or not request_id.strip():
                # A decision that names no request must never be applied to
                # whatever happens to be pending.
                print(
                    "[Misty] Ignoring confirmation response without a "
                    "request ID.",
                    flush=True,
                )
                return
            self.voice_controller.resolve_confirmation(
                message.get("approved") is True,
                request_id,
            )

    @Slot(bool)
    def _handle_confirmation_resolved(self, _approved):
        self.popup_server.send({"type": "confirmation_clear"})

    def shutdown(self):
        if self._shutting_down:
            return

        self._shutting_down = True

        self.voice_controller.shutdown()

        # Timers must never fire again, whatever else is still running.
        self.reminder_timer.stop()
        self.popup_server.send({"type": "hide"})

        # Shared services are closed only when no request is still running;
        # a live worker still holds the browser/clipboard/audit/journal.
        if self._has_live_work():
            print(
                "[Misty] Shutdown: a request is still running; leaving "
                "browser, clipboard, window, reminder and audit services "
                "open until it finishes.",
                flush=True,
            )
        else:
            try:
                self.browser_session.close()
            except Exception:
                pass
            try:
                self.reminder_store.close()
            except Exception:
                pass
            if self.audit_log is not None:
                self.audit_log.close()
            self.clipboard_bridge.close()
            self.window_service.close()
        try:
            self.file_transcriber.close()
        except Exception:
            pass

        if self.popup_process.state() != QProcess.ProcessState.NotRunning:
            self.popup_process.terminate()
            if not self.popup_process.waitForFinished(1000):
                self.popup_process.kill()
                self.popup_process.waitForFinished(300)

        self.popup_server.close()
        self.main_window.hide()
        self.tray.shutdown()

    def _has_live_work(self):
        """True while the controller still owns an in-flight request."""
        check = getattr(self.voice_controller, "has_live_work", None)
        if not callable(check):
            return False
        try:
            return bool(check())
        except Exception:
            return False

    def run(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.warning(
                None,
                "Misty",
                "The system tray is not currently available. "
                "Misty will continue running.",
            )

        try:
            self.popup_server.start()
        except Exception as error:
            QMessageBox.critical(
                None,
                "Misty IPC startup error",
                f"Could not start popup communication:\n\n{error}",
            )
            return 1

        try:
            self.window_service.start()
            print("[Misty] KDE window awareness ready.", flush=True)
        except Exception as error:
            print(f"[Misty] KDE window awareness unavailable: {error}", flush=True)

        self._start_popup_process()

        try:
            self.voice_controller.start()
        except Exception as error:
            QMessageBox.critical(
                None,
                "Misty startup error",
                f"Could not start the voice system:\n\n{error}",
            )
            self.shutdown()
            return 1

        print("[Misty] Desktop application started.", flush=True)
        print(
            f"[Misty] Hold {self.voice_controller.shortcut} to talk.",
            flush=True,
        )
        print("[Misty] Open the tray menu to access Misty.", flush=True)

        return self.app.exec()


def main():
    lock_path = Path("/tmp") / f"misty-{os.getuid()}.lock"
    instance_lock = QLockFile(str(lock_path))

    if not instance_lock.tryLock(0):
        print(
            "[Misty] Another Misty instance is already running. "
            "Not starting a duplicate.",
            flush=True,
        )
        raise SystemExit(1)

    try:
        cleanup_orphan_popup_processes(ROOT)
        # Model servers are detached (start_new_session=True), so a crash or
        # a killed instance leaves them holding gigabytes and a port. Reclaim
        # them before a new model starts, or the new run fights the old one.
        try:
            cleanup_orphan_model_servers(ROOT)
        except Exception as error:
            print(f"[Misty] Model cleanup skipped: {error}", flush=True)
        application = MistyApplication()
        raise SystemExit(application.run())
    finally:
        instance_lock.unlock()


if __name__ == "__main__":
    main()
