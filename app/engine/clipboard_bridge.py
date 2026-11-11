"""Thread-safe access to the desktop plain-text clipboard."""

import os
import shutil
import subprocess
import threading
import time

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication


MAX_CLIPBOARD_CHARS = 20_000
CLIPBOARD_TIMEOUT_SECONDS = 5.0


class ClipboardBridge(QObject):
    """Marshal clipboard operations from workers onto the Qt GUI thread."""

    _request_received = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._closed = False
        self._request_received.connect(
            self._handle_request,
            Qt.ConnectionType.QueuedConnection,
        )

    def _submit(self, operation, value=None):
        if self._closed:
            return {
                "success": False,
                "message": "Clipboard access is shutting down.",
            }

        request = {
            "operation": operation,
            "value": value,
            "event": threading.Event(),
            "result": None,
            "expires_at": time.monotonic() + CLIPBOARD_TIMEOUT_SECONDS,
        }

        from PySide6.QtCore import QThread
        if QThread.currentThread() == self.thread():
            self._handle_request(request)
        else:
            self._request_received.emit(request)
            if not request["event"].wait(CLIPBOARD_TIMEOUT_SECONDS + 1):
                return {
                    "success": False,
                    "message": "Clipboard access timed out. Nothing was changed.",
                }

        return request["result"] or {
            "success": False,
            "message": "Clipboard operation did not return a result.",
        }

    @staticmethod
    def _wayland_session():
        return bool(os.environ.get("WAYLAND_DISPLAY"))

    def _read_wayland_text(self):
        executable = shutil.which("wl-paste")
        if not executable:
            return {
                "success": False,
                "message": "Wayland clipboard utility wl-paste is not installed.",
            }

        try:
            completed = subprocess.run(
                [executable, "--no-newline", "--type", "text/plain"],
                capture_output=True,
                timeout=CLIPBOARD_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": "Wayland clipboard read timed out.",
            }
        except OSError:
            return {
                "success": False,
                "message": "Could not start the Wayland clipboard reader.",
            }

        if completed.returncode != 0:
            return {
                "success": False,
                "message": "Wayland clipboard read failed.",
            }

        try:
            text = completed.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "success": False,
                "message": "Clipboard content is not valid UTF-8 plain text.",
            }

        if len(text) > MAX_CLIPBOARD_CHARS:
            return {
                "success": False,
                "length": len(text),
                "message": (
                    f"Clipboard text exceeds the {MAX_CLIPBOARD_CHARS}-character "
                    "limit. Nothing was read."
                ),
            }

        return {
            "success": True,
            "text": text,
            "length": len(text),
            "message": (
                "Read clipboard text."
                if text else "The clipboard contains no plain text."
            ),
        }

    def _write_wayland_text(self, text):
        executable = shutil.which("wl-copy")
        if not executable:
            return {
                "success": False,
                "message": "Wayland clipboard utility wl-copy is not installed.",
            }

        try:
            completed = subprocess.run(
                [executable, "--type", "text/plain"],
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=CLIPBOARD_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": "Wayland clipboard write timed out.",
            }
        except OSError:
            return {
                "success": False,
                "message": "Could not start the Wayland clipboard writer.",
            }

        if completed.returncode != 0:
            return {
                "success": False,
                "message": "Wayland clipboard write failed.",
            }

        return {
            "success": True,
            "length": len(text),
            "message": "Clipboard text was replaced.",
        }

    def _read_qt_text(self):
        # A QCoreApplication (or an application without a GUI backend, e.g.
        # Qt's offscreen platform) returns a clipboard object whose backend
        # is invalid; using it segfaults instead of failing.
        inst = QGuiApplication.instance()
        if not isinstance(inst, QGuiApplication):
            return {
                "success": False,
                "message": "The desktop clipboard is unavailable.",
            }
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return {
                "success": False,
                "message": "The desktop clipboard is unavailable.",
            }
        mime = clipboard.mimeData()

        if mime is None or not mime.hasText():
            text = ""
        else:
            text = mime.text()

        if len(text) > MAX_CLIPBOARD_CHARS:
            return {
                "success": False,
                "length": len(text),
                "message": (
                    f"Clipboard text exceeds the {MAX_CLIPBOARD_CHARS}-character "
                    "limit. Nothing was read."
                ),
            }

        return {
            "success": True,
            "text": text,
            "length": len(text),
            "message": (
                "Read clipboard text."
                if text else "The clipboard contains no plain text."
            ),
        }

    def _write_qt_text(self, text):
        # A QCoreApplication (or an application without a GUI backend, e.g.
        # Qt's offscreen platform) returns a clipboard object whose backend
        # is invalid; using it segfaults instead of failing.
        inst = QGuiApplication.instance()
        if not isinstance(inst, QGuiApplication):
            return {
                "success": False,
                "message": "The desktop clipboard is unavailable.",
            }
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return {
                "success": False,
                "message": "The desktop clipboard is unavailable.",
            }
        clipboard.setText(text)
        return {
            "success": True,
            "length": len(text),
            "message": "Clipboard text was replaced.",
        }

    @Slot(object)
    def _handle_request(self, request):
        try:
            expires_at = request.get("expires_at")
            if (
                isinstance(expires_at, (int, float))
                and time.monotonic() > expires_at
            ):
                result = {
                    "success": False,
                    "message": (
                        "Clipboard access timed out before it could run. "
                        "Nothing was changed."
                    ),
                }
            elif self._closed:
                result = {
                    "success": False,
                    "message": "Clipboard access is shutting down.",
                }
            elif QGuiApplication.instance() is None:
                result = {
                    "success": False,
                    "message": "The desktop clipboard is unavailable.",
                }
            else:
                operation = request["operation"]

                if operation == "read":
                    if self._wayland_session():
                        result = self._read_wayland_text()
                    else:
                        result = self._read_qt_text()

                elif operation == "write":
                    text = request["value"]
                    if not isinstance(text, str):
                        result = {
                            "success": False,
                            "message": "Clipboard content must be text.",
                        }
                    elif len(text) > MAX_CLIPBOARD_CHARS:
                        result = {
                            "success": False,
                            "message": (
                                f"Text exceeds the {MAX_CLIPBOARD_CHARS}-character "
                                "clipboard limit. Nothing was changed."
                            ),
                        }
                    elif self._wayland_session():
                        result = self._write_wayland_text(text)
                    else:
                        result = self._write_qt_text(text)

                else:
                    result = {
                        "success": False,
                        "message": "Unknown clipboard operation.",
                    }

        except Exception:
            result = {
                "success": False,
                "message": "The desktop clipboard operation failed.",
            }

        request["result"] = result
        request["event"].set()

    def read_text(self):
        return self._submit("read")

    def write_text(self, text):
        return self._submit("write", text)

    def close(self):
        self._closed = True
