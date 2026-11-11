"""Bounded external conversion: deadlines, output caps, process-group kill."""

import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from app.documents.converters import (
    ExternalConversionError,
    ExternalConverter,
)


def write_stub(directory, body):
    path = Path(directory) / "stub-converter"
    path.write_text(
        "#!" + sys.executable + "\n" + textwrap.dedent(body),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return str(path)


class ExternalConverterBoundsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "sample.pdf"
        self.target.write_bytes(b"%PDF-1.4\n")

    def converter(self, body, **kwargs):
        stub = write_stub(self.root, body)
        return ExternalConverter(pdftotext=stub, pandoc=stub, **kwargs)

    def test_slow_stub_times_out_quickly(self):
        converter = self.converter(
            """
            import os, sys, time
            os.write(1, b"x")
            time.sleep(1.0)
            """,
            timeout=0.1,
        )

        started = time.monotonic()
        with self.assertRaises(ExternalConversionError):
            converter.extract(self.target, "pdf", 1000)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5, elapsed)

    def test_oversized_output_is_truncated_and_converter_terminated(self):
        pid_file = self.root / "pid"
        converter = self.converter(
            f"""
            import os, sys, time
            with open({str(pid_file)!r}, "w") as handle:
                handle.write(str(os.getpid()))
            chunk = b"a" * 1024
            try:
                while True:
                    os.write(1, chunk)
            except OSError:
                pass
            time.sleep(30)
            """,
            timeout=10,
            max_output_bytes=4096,
        )

        started = time.monotonic()
        text, truncated, converter_name = converter.extract(
            self.target, "pdf", 1000
        )
        elapsed = time.monotonic() - started

        self.assertTrue(truncated)
        self.assertLessEqual(len(text), 1000)
        self.assertEqual(converter_name, "pdftotext")
        self.assertLess(elapsed, 5.0, elapsed)
        pid = int(pid_file.read_text().strip())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_child_ignoring_sigterm_is_killed_via_group(self):
        pid_file = self.root / "pid"
        converter = self.converter(
            f"""
            import os, signal, sys, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            grandchild = os.fork()
            if grandchild == 0:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                time.sleep(30)
                os._exit(0)
            with open({str(pid_file)!r}, "w") as handle:
                handle.write(str(grandchild))
            os.write(1, b"y")
            time.sleep(30)
            """,
            timeout=0.2,
        )

        started = time.monotonic()
        with self.assertRaises(ExternalConversionError):
            converter.extract(self.target, "pdf", 1000)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5 + 2 * 2.0 + 1.0, elapsed)
        self.assertTrue(pid_file.exists(), "stub never started")
        grandchild = int(pid_file.read_text().strip())
        gone = False
        for _ in range(50):
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                gone = True
                break
            time.sleep(0.05)
        if not gone:
            self.addCleanup(os.kill, grandchild, 9)
        self.assertTrue(gone, "grandchild escaped the process-group kill")

    def test_normal_output_is_decoded_and_truncated(self):
        converter = self.converter(
            """
            import os
            os.write(1, "Zażółć\\r\\ngęślą\\njaźń".encode("utf-8"))
            """
        )

        text, truncated, name = converter.extract(self.target, "pdf", 500)
        self.assertEqual(name, "pdftotext")
        self.assertEqual(text, "Zażółć\ngęślą\njaźń")
        self.assertFalse(truncated)

        short, short_truncated, _ = converter.extract(self.target, "pdf", 5)
        self.assertEqual(short, "Zażół")
        self.assertTrue(short_truncated)

    def test_unstartable_converter_reports_error(self):
        converter = ExternalConverter(
            pdftotext=str(self.root / "missing"), pandoc=""
        )
        with self.assertRaises(ExternalConversionError):
            converter.extract(self.target, "pdf", 10)


if __name__ == "__main__":
    unittest.main()
