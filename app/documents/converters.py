"""Bounded, local-only conversion of PDF and Office documents to text.

The converters are fixed system executables invoked without a shell. They read
one already-validated local file and write text to stdout. No URLs, resource
paths, shell expansion, or user-supplied arguments are permitted.
"""

from __future__ import annotations

import os
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_OUTPUT_BYTES = 2_000_000
READ_CHUNK = 65_536
TERMINATION_GRACE_SECONDS = 2.0

SUPPORTED_EXTERNAL_FORMATS = {
    "pdf": "pdf",
    "docx": "docx",
    "odt": "odt",
}


class ExternalConversionError(RuntimeError):
    """Raised when a local converter is unavailable, fails, or times out."""


class ExternalConverter:
    """Run pdftotext or pandoc with strict bounds."""

    def __init__(
        self,
        pdftotext=None,
        pandoc=None,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
    ):
        self.pdftotext = (
            shutil.which("pdftotext") if pdftotext is None else pdftotext
        )
        self.pandoc = shutil.which("pandoc") if pandoc is None else pandoc
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            raise ValueError("timeout must be positive.")
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or max_output_bytes <= 0
        ):
            raise ValueError("max_output_bytes must be a positive integer.")
        self.timeout = float(timeout)
        self.max_output_bytes = max_output_bytes

    def supports(self, format_name) -> bool:
        if format_name == "pdf":
            return bool(self.pdftotext)
        if format_name in {"docx", "odt"}:
            return bool(self.pandoc)
        return False

    def extract(self, path, format_name, max_chars):
        """Return ``(text, truncated)`` for one local file.

        Output is bounded by ``max_chars`` characters and
        ``max_output_bytes`` bytes. A converter that keeps writing after the
        bound is terminated.
        """
        if format_name not in SUPPORTED_EXTERNAL_FORMATS:
            raise ExternalConversionError("Unsupported external format.")
        if not self.supports(format_name):
            raise ExternalConversionError(
                "The required local document converter is not installed."
            )
        if not isinstance(max_chars, int) or max_chars <= 0:
            raise ExternalConversionError("Invalid output limit.")

        path = Path(path)
        if format_name == "pdf":
            command = [
                self.pdftotext,
                "-enc", "UTF-8",
                "-nopgbrk",
                "-q",
                "--",
                str(path),
                "-",
            ]
            converter = "pdftotext"
        else:
            command = [
                self.pandoc,
                "--sandbox",
                "-f", format_name,
                "-t", "plain",
                "--wrap=none",
                "--",
                str(path),
            ]
            converter = "pandoc"

        text, truncated = self._run(command, max_chars)
        return text, truncated, converter

    def _run(self, command, max_chars):
        limit = min(self.max_output_bytes, max_chars * 4 + 4)
        # One absolute deadline for the whole call: start-up, reads and the
        # reaping of a terminated converter all share it.
        deadline = time.monotonic() + self.timeout

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                start_new_session=True,
            )
        except OSError as error:
            raise ExternalConversionError(
                "The local document converter could not start."
            ) from error

        chunks = []
        total = 0
        truncated = False
        stream = process.stdout
        try:
            if stream is not None:
                # Non-blocking reads: select() only says bytes are available,
                # os.read() takes exactly what is there, so no buffered
                # read(n) can block past the deadline or pull extra bytes.
                os.set_blocking(stream.fileno(), False)
                with selectors.DefaultSelector() as selector:
                    selector.register(stream, selectors.EVENT_READ)
                    while total < limit:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ExternalConversionError(
                                "The local document conversion timed out."
                            )
                        if not selector.select(remaining):
                            raise ExternalConversionError(
                                "The local document conversion timed out."
                            )
                        wanted = min(READ_CHUNK, limit - total)
                        try:
                            chunk = os.read(stream.fileno(), wanted)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)

            if total >= limit:
                truncated = True
                self._terminate(process)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ExternalConversionError(
                        "The local document conversion timed out."
                    )
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired as error:
                    raise ExternalConversionError(
                        "The local document conversion timed out."
                    ) from error
        except ExternalConversionError:
            raise
        except (OSError, subprocess.SubprocessError) as error:
            self._terminate(process)
            raise ExternalConversionError(
                "The local document conversion failed."
            ) from error
        finally:
            if process.poll() is None:
                self._terminate(process)
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

        raw = b"".join(chunks)
        if not raw:
            if process.returncode not in (0, -15, -9):
                raise ExternalConversionError(
                    "The local document converter returned no text."
                )
            return "", truncated

        text = raw.decode("utf-8", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
        return text, truncated

    @staticmethod
    def _terminate(process):
        """Terminate the converter's whole process group, then reap it.

        The child is started with ``start_new_session=True``, so it leads its
        own process group; helpers it spawned (pandoc paging filters, PDF
        subprocesses) are signalled too instead of outliving the deadline.
        """
        pid = process.pid
        for sig, wait in (
            (signal.SIGTERM, TERMINATION_GRACE_SECONDS),
            (signal.SIGKILL, TERMINATION_GRACE_SECONDS),
        ):
            try:
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError):
                try:
                    process.send_signal(sig)
                except (OSError, ProcessLookupError):
                    pass
            try:
                process.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                return
