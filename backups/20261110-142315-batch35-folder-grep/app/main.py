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
from app.desktop.accessibility.activator import ControlActivator
from app.desktop.media import MediaController
from app.automation.reminders import ReminderStore
from app.engine.kwin_windows import KWinWindowService
from app.desktop.context import DesktopContext
from app.desktop.vision.capture import CaptureService
from app.desktop.vision.ocr import OCRService
from app.desktop.vision.vlm import VisionLanguageService
from app.desktop.vision.coordinator import DesktopVision
from app.engine.audit_log import AuditLog, TaskJournal
from app.engine.clipboard_bridge import ClipboardBridge
from app.engine.process_guard import cleanup_orphan_popup_processes
from app.engine.popup_ipc import PopupIpcServer
from app.engine.voice_controller import VoiceController
from app.knowledge.index import KnowledgeIndex
from app.web.search import SearxngClient
from app.ui.main_window import MainWindow
from app.ui.response_window import MistyResponseWindow, should_auto_open
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
        self.control_activator = ControlActivator()
        self.media_controller = MediaController()
        self.tray = MistyTray(
            self.app, self.main_window, self.response_window
        )
        self._latest_response = ""
        self._latest_transcript = ""
        self._last_shown_pair = None

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
            ),
            ocr_service=OCRService(),
            vlm_service=VisionLanguageService(),
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
            control_activator=self.control_activator,
            media_controller=self.media_controller,
        )
        self.voice_controller.activity_event.connect(
            self.main_window.add_activity_event
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
        self.main_window.text_submitted.connect(
            self.voice_controller.submit_text
        )

        self.app.aboutToQuit.connect(self.shutdown)

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
    def _capture_response_text(self, text):
        self._latest_response = text if isinstance(text, str) else ""

    @Slot(str)
    def _capture_transcript_text(self, text):
        self._latest_transcript = text if isinstance(text, str) else ""

    @Slot(str)
    def _handle_voice_state(self, state):
        if state == "complete":
            QTimer.singleShot(0, self._maybe_show_full_response)

    def _maybe_show_full_response(self):
        response = self._latest_response.strip()
        if not response:
            return
        pair = (self._latest_transcript, response)
        if pair == self._last_shown_pair:
            return
        if should_auto_open(response):
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
                self.voice_controller.resolve_confirmation(False)
                return

            title = f"Close {target.get('title') or 'this window'}?"
            app_id = str(target.get("app_id") or "Unknown application")
            description = (
                f"Application: {app_id}\\n\\n"
                "Closing may interrupt work or cause unsaved changes "
                "to be lost. Misty will request closure; the application "
                "may still prevent it."
            )
        else:
            details = json.dumps(arguments, ensure_ascii=False, indent=2)
            description = (
                f"{description}\\n\\nAction: {name}\\n{details}"
            )

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
            self.voice_controller.resolve_confirmation(False)

    @Slot(dict)
    def _handle_popup_message(self, message):
        if message.get("type") == "confirmation_response":
            request_id = message.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                request_id = None
            self.voice_controller.resolve_confirmation(
                message.get("approved") is True,
                request_id=request_id,
            )

    @Slot(bool)
    def _handle_confirmation_resolved(self, _approved):
        self.popup_server.send({"type": "confirmation_clear"})

    def shutdown(self):
        if self._shutting_down:
            return

        self._shutting_down = True

        self.voice_controller.shutdown()
        try:
            self.browser_session.close()
        except Exception:
            pass
        self.reminder_timer.stop()
        try:
            self.reminder_store.close()
        except Exception:
            pass
        if self.audit_log is not None:
            self.audit_log.close()
        self.clipboard_bridge.close()
        self.window_service.close()
        self.popup_server.send({"type": "hide"})

        if self.popup_process.state() != QProcess.ProcessState.NotRunning:
            self.popup_process.terminate()
            if not self.popup_process.waitForFinished(2000):
                self.popup_process.kill()
                self.popup_process.waitForFinished(1000)

        self.popup_server.close()
        self.main_window.hide()
        self.tray.shutdown()

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
        print("[Misty] Hold F8 to talk.", flush=True)
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
        application = MistyApplication()
        raise SystemExit(application.run())
    finally:
        instance_lock.unlock()


if __name__ == "__main__":
    main()
