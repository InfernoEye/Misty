"""Local JSON-line IPC between Misty's core and popup processes."""

import json
import os

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def popup_socket_name():
    return f"misty-popup-{os.getuid()}"


def _encode_message(message):
    if not isinstance(message, dict):
        raise TypeError("IPC messages must be dictionaries")
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


def _read_messages(buffer, data):
    buffer += data
    messages = []

    while b"\n" in buffer:
        line, buffer = buffer.split(b"\n", 1)
        if not line.strip():
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(message, dict):
            messages.append(message)

    return buffer, messages


class PopupIpcServer(QObject):
    """Core-side local socket server."""

    message_received = Signal(dict)
    connection_changed = Signal(bool)

    def __init__(self, socket_name=None, parent=None):
        super().__init__(parent)
        self.socket_name = socket_name or popup_socket_name()
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept_connections)
        self.client = None
        self._buffer = b""

    @property
    def is_connected(self):
        return (
            self.client is not None
            and self.client.state()
            == QLocalSocket.LocalSocketState.ConnectedState
        )

    def start(self):
        if self.server.isListening():
            return

        QLocalServer.removeServer(self.socket_name)

        if not self.server.listen(self.socket_name):
            raise RuntimeError(
                f"Could not listen on popup IPC socket "
                f"'{self.socket_name}': {self.server.errorString()}"
            )

    def _accept_connections(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()

            if self.client is not None:
                old_client = self.client

                # Disconnect the old signal before closing it. Its delayed
                # disconnect event must not interfere with the new client.
                old_client.disconnected.disconnect(
                    self._client_disconnected
                )
                old_client.disconnectFromServer()
                old_client.deleteLater()

            self.client = socket
            self._buffer = b""
            socket.readyRead.connect(self._read_client)
            socket.disconnected.connect(self._client_disconnected)
            self.connection_changed.emit(True)

    def _read_client(self):
        socket = self.sender()
        if socket is None or socket is not self.client:
            return

        self._buffer, messages = _read_messages(
            self._buffer, bytes(socket.readAll())
        )

        for message in messages:
            self.message_received.emit(message)

    def _client_disconnected(self):
        socket = self.sender()

        if socket is self.client:
            self.client = None
            self._buffer = b""
            self.connection_changed.emit(False)

        if socket is not None:
            socket.deleteLater()

    def send(self, message):
        if not self.is_connected:
            return False

        self.client.write(_encode_message(message))
        self.client.flush()
        return True

    def close(self):
        if self.client is not None:
            self.client.disconnectFromServer()
            self.client = None

        if self.server.isListening():
            self.server.close()
            QLocalServer.removeServer(self.socket_name)


class PopupIpcClient(QObject):
    """Popup-side local socket client with automatic reconnection."""

    message_received = Signal(dict)
    connection_changed = Signal(bool)

    def __init__(self, socket_name=None, parent=None):
        super().__init__(parent)
        self.socket_name = socket_name or popup_socket_name()
        self.socket = QLocalSocket(self)
        self.socket.readyRead.connect(self._read_server)
        self.socket.connected.connect(self._on_connected)
        self.socket.disconnected.connect(self._on_disconnected)
        self.socket.errorOccurred.connect(self._on_error)
        self._buffer = b""

        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setInterval(1000)
        self.reconnect_timer.timeout.connect(self.connect_to_server)

    @property
    def is_connected(self):
        return (
            self.socket.state()
            == QLocalSocket.LocalSocketState.ConnectedState
        )

    def start(self):
        self.connect_to_server()
        self.reconnect_timer.start()

    def connect_to_server(self):
        if (
            self.socket.state()
            == QLocalSocket.LocalSocketState.UnconnectedState
        ):
            self.socket.connectToServer(self.socket_name)

    def _on_connected(self):
        self._buffer = b""
        self.connection_changed.emit(True)

    def _on_disconnected(self):
        self.connection_changed.emit(False)

    def _on_error(self, _error):
        # The reconnect timer retries after transient connection failures.
        pass

    def _read_server(self):
        self._buffer, messages = _read_messages(
            self._buffer, bytes(self.socket.readAll())
        )

        for message in messages:
            self.message_received.emit(message)

    def send(self, message):
        if not self.is_connected:
            return False

        self.socket.write(_encode_message(message))
        self.socket.flush()
        return True

    def close(self):
        self.reconnect_timer.stop()
        self.socket.disconnectFromServer()
