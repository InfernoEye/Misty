"""Integration test for the standalone popup process."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QTimer
from app.engine.popup_ipc import PopupIpcServer


def main():
    app = QCoreApplication(sys.argv)
    server = PopupIpcServer()
    received = []

    server.message_received.connect(received.append)
    server.start()

    print("[Test] IPC server listening.", flush=True)


    def finish():
        server.close()
        app.quit()


    ready_handled = False


    def check():
        global ready_handled

        if (
            not ready_handled
            and any(item.get("type") == "popup_ready" for item in received)
        ):
            ready_handled = True
            print("[Test] Popup ready message received.", flush=True)

            server.send({"type": "set_state", "state": "thinking"})
            server.send({
                "type": "set_transcript",
                "text": "Testing Misty's separate popup process.",
            })
            server.send({
                "type": "set_response",
                "text": "The popup is responding to IPC updates.",
            })
            server.send({"type": "show"})

            QTimer.singleShot(1500, finish)

        elif time.monotonic() > deadline:
            print("[Test] Timed out waiting for popup.", flush=True)
            finish()


    deadline = time.monotonic() + 15

    poll = QTimer()
    poll.setInterval(100)
    poll.timeout.connect(check)
    poll.start()

    app.exec()

    print("[Test] Messages received:")
    for item in received:
        print(item)

    if not any(item.get("type") == "popup_ready" for item in received):
        raise SystemExit(1)

    print("[Test] Popup IPC integration: OK")


if __name__ == "__main__":
    raise SystemExit(main())
