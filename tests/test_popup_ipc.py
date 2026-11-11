"""Tests for the bounded popup IPC reader.

No sockets are opened here: ``_read_messages`` is a pure function over
bytes, so every bound can be exercised deterministically. A separate
integration test exercises the real Qt classes on a private socket name.
"""

import json
import os
import unittest
import uuid

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from app.engine.popup_ipc import (
    MAX_BUFFER_BYTES,
    MAX_LINE_BYTES,
    MAX_MESSAGES_PER_READ,
    _read_messages,
)


def _line(payload):
    return (json.dumps(payload) + "\n").encode("utf-8")


class ReadMessagesTests(unittest.TestCase):
    def test_complete_lines_are_parsed_and_remainder_buffered(self):
        buffer, messages = _read_messages(
            b"", b'{"a": 1}\n{"b": 2}\n{"partial"'
        )
        self.assertEqual(messages, [{"a": 1}, {"b": 2}])
        self.assertEqual(buffer, b'{"partial"')

        buffer, messages = _read_messages(buffer, b': 3}\n')
        self.assertEqual(messages, [{"partial": 3}])
        self.assertEqual(buffer, b"")

    def test_invalid_and_non_object_lines_are_skipped(self):
        _, messages = _read_messages(
            b"",
            b"not json\n[1,2]\n\n   \n{\"ok\": true}\n\xff\xfe\n",
        )
        self.assertEqual(messages, [{"ok": True}])

    def test_oversized_line_is_dropped_whole(self):
        data = (
            _line({"good": 1})
            + b'{"big": "' + b"x" * 100 + b'"}\n'
            + _line({"good": 2})
        )
        buffer, messages = _read_messages(
            b"", data, max_line_bytes=64
        )
        self.assertEqual(messages, [{"good": 1}, {"good": 2}])
        self.assertEqual(buffer, b"")

    def test_oversized_line_that_wraps_across_reads_is_dropped(self):
        buffer = b'{"big": "' + b"x" * 40
        buffer, messages = _read_messages(
            buffer, b"y" * 40 + b'"}\n' + _line({"after": 1}),
            max_line_bytes=64,
        )
        self.assertEqual(messages, [{"after": 1}])
        self.assertEqual(buffer, b"")

    def test_missing_newline_growth_is_bounded(self):
        buffer = b""
        chunk = b'{"n": "' + b"z" * 4096
        # Keep feeding fragments until the reader drops the oversized line.
        for _ in range(-(-(MAX_BUFFER_BYTES + 1) // len(chunk))):
            buffer, messages = _read_messages(buffer, chunk)
            self.assertEqual(messages, [])
            self.assertLessEqual(len(buffer), MAX_BUFFER_BYTES)

        # Oversized fragment without a newline is dropped and the buffer reset.
        self.assertEqual(buffer, b"")

        # The stream recovers at the next newline after the reset.
        buffer, messages = _read_messages(buffer, b'{"recovered": 1}\n')
        self.assertEqual(messages, [{"recovered": 1}])

    def test_default_buffer_bound_is_one_mebibyte(self):
        self.assertEqual(MAX_BUFFER_BYTES, 1024 * 1024)
        self.assertEqual(MAX_LINE_BYTES, 1024 * 1024)
        buffer, messages = _read_messages(b"", b"a" * (MAX_BUFFER_BYTES + 1))
        self.assertEqual((buffer, messages), (b"", []))

    def test_many_messages_are_capped_per_read(self):
        count = MAX_MESSAGES_PER_READ * 2 + 7
        data = b"".join(_line({"i": index}) for index in range(count))

        buffer, messages = _read_messages(b"", data)

        self.assertEqual(len(messages), MAX_MESSAGES_PER_READ)
        self.assertEqual(messages[0], {"i": 0})
        self.assertEqual(
            messages[-1], {"i": MAX_MESSAGES_PER_READ - 1}
        )
        self.assertGreater(len(buffer), 0)


        # The remaining lines are drained by later reads. A read only makes
        # progress when it is fed data, so stop when the buffer goes empty.
        delivered = len(messages)
        while buffer:
            buffer, more = _read_messages(buffer, b"")
            self.assertLessEqual(len(more), MAX_MESSAGES_PER_READ)
            delivered += len(more)

        self.assertEqual(delivered, count)


class PopupIpcIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_oversized_message_over_socket_does_not_block_small_ones(self):
        from app.engine.popup_ipc import PopupIpcClient, PopupIpcServer

        name = f"misty-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        server = PopupIpcServer(name)
        client = PopupIpcClient(name)
        self.addCleanup(server.close)
        self.addCleanup(client.close)

        messages = []
        server.message_received.connect(messages.append)
        try:
            server.start()
        except RuntimeError:
            self.skipTest("local sockets unavailable in this environment")

        client.connect_to_server()
        _pump_until(self.app, lambda: client.is_connected)
        if not client.is_connected:
            self.skipTest("popup IPC socket could not connect")

        # The oversized line is sent first and consumes the whole read; the
        # small message that follows must still arrive.
        client.send({"type": "huge", "text": "x" * (MAX_LINE_BYTES + 10)})
        client.send({"type": "set_state", "state": "idle"})
        _pump_until(
            self.app,
            lambda: any(m.get("type") == "set_state" for m in messages),
        )
        self.assertEqual(
            [message.get("type") for message in messages], ["set_state"]
        )


def _pump_until(app, condition, timeout_ms=3000):
    """Process events until ``condition`` is true or the timeout expires."""
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(5)
    poll.timeout.connect(lambda: condition() and loop.quit())
    poll.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    poll.stop()


def _app():
    application = QCoreApplication.instance()
    if application is None:
        application = QCoreApplication([])
    return application


if __name__ == "__main__":
    unittest.main()
