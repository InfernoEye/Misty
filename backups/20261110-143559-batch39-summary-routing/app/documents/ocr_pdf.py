"""Bounded local OCR fallback for image-only PDFs using OCRmyPDF.

Only a fixed executable is invoked, with one already-validated PDF path and a
private temporary sidecar. No shell, URL, upload, or user-supplied arguments
are used. OCR runs locally through Tesseract.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TIMEOUT = 120.0
MAX_SIDECAR_BYTES = 2_000_000
MIN_TEXT_CHARS = 20


class OcrPdfError(RuntimeError):
    """Raised when local PDF OCR is unavailable or fails."""


class OcrmypdfConverter:
    def __init__(
        self,
        executable=None,
        timeout=DEFAULT_TIMEOUT,
        languages="eng+pol",
    ):
        if executable is None:
            candidate = PROJECT_ROOT / "data" / "bin" / "ocrmypdf"
            executable = (
                str(candidate)
                if candidate.is_file()
                else shutil.which("ocrmypdf")
            )
        self.executable = executable or None
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            raise ValueError("timeout must be positive.")
        self.timeout = float(timeout)
        if not isinstance(languages, str) or not languages.strip():
            raise ValueError("languages must be non-empty text.")
        self.languages = languages.strip()

    @property
    def available(self) -> bool:
        return bool(self.executable and Path(self.executable).is_file())

    def extract(self, path, max_chars):
        if not self.available:
            raise OcrPdfError("Local PDF OCR is not installed.")
        if not isinstance(max_chars, int) or max_chars <= 0:
            raise OcrPdfError("Invalid output limit.")

        path = Path(path)
        with tempfile.TemporaryDirectory(prefix="misty-ocr-") as directory:
            sidecar = Path(directory) / "sidecar.txt"
            command = [
                self.executable,
                "--output-type", "none",
                "--sidecar", str(sidecar),
                "--language", self.languages,
                str(path),
                "-",
            ]
            try:
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=False,
                    timeout=self.timeout,
                    check=False,
                    start_new_session=True,
                )
            except subprocess.TimeoutExpired as error:
                raise OcrPdfError("Local PDF OCR timed out.") from error
            except OSError as error:
                raise OcrPdfError(
                    "Local PDF OCR could not start."
                ) from error

            if result.returncode != 0:
                raise OcrPdfError("Local PDF OCR failed.")
            if not sidecar.is_file():
                raise OcrPdfError("Local PDF OCR produced no text.")
            if sidecar.stat().st_size > MAX_SIDECAR_BYTES:
                raise OcrPdfError("Local PDF OCR output is too large.")

            text = sidecar.read_bytes().decode("utf-8", errors="replace")
            text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
            return text, truncated
