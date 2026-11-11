import json
import os
import sys

from PySide6.QtCore import QSocketNotifier, QTimer
from PySide6.QtWidgets import QApplication

from app.ui.popup import MistyPopup


app = QApplication(sys.argv)
popup = MistyPopup()

if "--service" not in sys.argv:
    popup.set_state("listening")
    popup.set_transcript("Open my Downloads folder.")
    popup.set_response("I'm listening. Go ahead!")
    popup.show_popup()
    sys.exit(app.exec())


notifier = QSocketNotifier(
    sys.stdin.fileno(),
    QSocketNotifier.Type.Read,
)

buffer = b""


def read_updates(*_):
    global buffer

    try:
        data = os.read(sys.stdin.fileno(), 4096)
    except OSError:
        data = b""

    if not data:
        app.quit()
        return

    buffer += data

    while b"\n" in buffer:
        line, buffer = buffer.split(b"\n", 1)

        try:
            message = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue

        if "state" in message:
            popup.set_state(message["state"])

        if "transcript" in message:
            popup.set_transcript(message["transcript"])

        if "response" in message:
            popup.set_response(message["response"])

        if message.get("show"):
            popup.show_popup()

        if message.get("hide"):
            popup.hide()

        if message.get("state") == "complete":
            QTimer.singleShot(4500, popup.hide)


notifier.activated.connect(read_updates)

print("[Misty UI] Popup service ready.", flush=True)
sys.exit(app.exec())
