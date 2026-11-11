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


MAX_BUFFER_BYTES = 1024 * 1024
"""Largest buffered fragment of a single unterminated line (1 MiB)."""

MAX_LINE_BYTES = 1024 * 1024
"""Largest single newline-terminated message accepted from a peer."""

MAX_MESSAGES_PER_READ = 1000
"""Upper bound on messages dispatched from one ``readyRead`` burst."""

_buffer_violations = 0


def _log_buffer_violation(reason):
    """Log the first buffer violation per process without spamming."""
    global _buffer_violations
    _buffer_violations += 1
    if _buffer_violations == 1:
        print(
            f"[Misty] Popup IPC dropped an oversized message ({reason}).",
            flush=True,
        )


def _read_messages(
    buffer,
    data,
    *,
    max_buffer_bytes=MAX_BUFFER_BYTES,
    max_line_bytes=MAX_LINE_BYTES,
    max_messages=MAX_MESSAGES_PER_READ,
):
    """Split ``data`` into JSON objects while keeping buffers bounded.

    ``buffer`` is the leftover of an incomplete line from the previous call.
    A line longer than ``max_line_bytes`` is dropped whole and nothing is
    emitted for it; the bytes that follow it up to the next newline are
    discarded too, so the stream always resynchronises at a line boundary. A
    fragment larger than ``max_buffer_bytes`` without a newline is dropped and
    the buffer reset, so neither process can be made to accumulate unbounded
    memory. At most ``max_messages`` objects are returned per call; complete
    lines beyond that stay buffered for the next event-loop turn.
    """
    remaining = bytes(data)
    messages = []

    # The buffer may already hold complete lines from a burst that hit the
    # per-read cap. Drain it even when no new bytes arrived, otherwise a
    # scheduled drain with an empty read could never make progress.
    while remaining or b"\n" in buffer:
        if not remaining:
            # No new bytes: consume the buffered line directly.
            line, __, buffer = buffer.partition(b"\n")
            remaining = b""
        else:
            newline = remaining.find(b"\n")
            if newline < 0:
                pending = buffer + remaining
                if len(pending) > max_buffer_bytes:
                    _log_buffer_violation("unterminated line too large")
                    return b"", messages
                return pending, messages

            line = buffer + remaining[:newline]
            remaining = remaining[newline + 1:]
            buffer = b""

        if len(line) > max_line_bytes:
            _log_buffer_violation("line exceeds line limit")
            continue
        if not line.strip():
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(message, dict):
            messages.append(message)
        if len(messages) >= max_messages:
            # Keep every byte that has not been parsed yet. A buffered line
            # without its newline belongs to ``remaining`` together with the
            # next chunk, so it must not be dropped here.
            leftover = buffer + remaining
            if len(leftover) > max_buffer_bytes:
                _log_buffer_violation("buffer exceeds limit")
                return b"", messages
            return leftover, messages

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
        self._drain_scheduled = False

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
        self._consume(socket)

    def _consume(self, socket):
        """Parse one burst and emit a bounded number of messages."""
        self._buffer, messages = _read_messages(
            self._buffer, bytes(socket.readAll())
        )

        for message in messages:
            self.message_received.emit(message)

        # More complete lines may still be buffered after the per-burst cap;
        # process them on the next event-loop turn (one timer per burst, not
        # one per socket signal).
        if socket.bytesAvailable() > 0 or b"\n" in self._buffer:
            self._schedule_drain(socket)

    def _schedule_drain(self, socket):
        if self._drain_scheduled:
            return
        self._drain_scheduled = True
        QTimer.singleShot(
            0, lambda sock=socket: self._drain(sock)
        )

    def _drain(self, socket):
        self._drain_scheduled = False
        if socket is not self.client:
            return
        self._consume(socket)

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
        self._drain_scheduled = False

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
        self._consume()

    def _consume(self):
        """Parse one burst and emit a bounded number of messages."""
        self._buffer, messages = _read_messages(
            self._buffer, bytes(self.socket.readAll())
        )

        for message in messages:
            self.message_received.emit(message)

        if self.socket.bytesAvailable() > 0 or b"\n" in self._buffer:
            if not self._drain_scheduled:
                self._drain_scheduled = True
                QTimer.singleShot(0, self._drain)

    def _drain(self):
        self._drain_scheduled = False
        self._consume()

    def send(self, message):
        if not self.is_connected:
            return False

        self.socket.write(_encode_message(message))
        self.socket.flush()
        return True

    def close(self):
        self.reconnect_timer.stop()
        self.socket.disconnectFromServer()
