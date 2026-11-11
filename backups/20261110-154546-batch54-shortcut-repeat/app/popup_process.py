"""Standalone process for Misty's desktop popup."""

import sys

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QApplication

from app.engine.popup_ipc import PopupIpcClient
from app.ui.popup import MistyPopup


class PopupProcess:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setApplicationName("Misty Popup")
        self.app.setOrganizationName("Misty")
        self.app.setDesktopFileName("misty-popup")
        self.app.setQuitOnLastWindowClosed(False)

        self.popup = MistyPopup()
        self.ipc = PopupIpcClient(parent=self.app)
        self._confirmation_request_id = None

        self.ipc.message_received.connect(self.handle_message)
        self.ipc.connection_changed.connect(self.handle_connection)
        self.popup.hover_changed.connect(self.handle_hover)
        self.popup.confirmation_action.connect(self.handle_confirmation_action)

        self.app.aboutToQuit.connect(self.ipc.close)

    @Slot(dict)
    def handle_message(self, message):
        message_type = message.get("type")

        if message_type == "set_state":
            self.popup.set_state(str(message.get("state", "ready")))

        elif message_type == "set_transcript":
            self.popup.set_transcript(str(message.get("text", "")))

        elif message_type == "set_response":
            self.popup.set_response(str(message.get("text", "")))

        elif message_type == "shortcut":
            self.popup.set_shortcut(str(message.get("text", "")))

        elif message_type == "show":
            self.popup.show_popup()

        elif message_type == "window_selection":
            self.popup.show_window_selection(
                message.get("candidates", [])
            )

        elif message_type == "window_selection_message":
            self.popup.set_window_selection_message(
                str(message.get("text", "Please choose a window."))
            )

        elif message_type == "window_selection_clear":
            self.popup.clear_window_selection()

        elif message_type == "confirmation":
            request_id = message.get("request_id")
            self._confirmation_request_id = (
                request_id
                if isinstance(request_id, str) and request_id
                else None
            )
            self.popup.show_confirmation(
                str(message.get("title", "Confirm this action?")),
                str(message.get("description", "")),
            )

        elif message_type == "confirmation_clear":
            self._confirmation_request_id = None
            self.popup.clear_confirmation()

        elif message_type == "hide":
            self.popup.hide()

    @Slot(bool)
    def handle_confirmation_action(self, approved):
        self.popup.set_confirmation_busy()
        message = {
            "type": "confirmation_response",
            "approved": bool(approved),
        }
        if self._confirmation_request_id is not None:
            message["request_id"] = self._confirmation_request_id
        self._confirmation_request_id = None
        self.ipc.send(message)

    @Slot(bool)
    def handle_connection(self, connected):
        print(
            f"[Misty Popup] Core connection: "
            f"{'connected' if connected else 'disconnected'}",
            flush=True,
        )

        if connected:
            self.ipc.send({
                "type": "popup_ready",
                "hovered": self.popup.is_hovered,
            })

    @Slot(bool)
    def handle_hover(self, hovered):
        self.ipc.send({
            "type": "hover_changed",
            "hovered": hovered,
        })

    def run(self):
        print("[Misty Popup] Separate popup process started.", flush=True)
        self.ipc.start()
        return self.app.exec()


def main():
    process = PopupProcess()
    raise SystemExit(process.run())


if __name__ == "__main__":
    main()
