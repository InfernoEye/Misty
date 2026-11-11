"""Bounded local audio/video file transcription with ffmpeg + whisper.cpp."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.actions.file_scope import FileScopeError, UserFolderResolver


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WHISPER_CLI = Path(os.environ.get(
    "MISTY_WHISPER_CLI",
    PROJECT_ROOT / "third_party/whisper.cpp/build/bin/whisper-cli",
))
MODEL_PATH = Path(os.environ.get(
    "MISTY_WHISPER_MODEL",
    PROJECT_ROOT / "models/ggml-large-v3-turbo-q5_0.bin",
))
MAX_FILE_BYTES = 2 * 1024 ** 3
MAX_TEXT_CHARS = 200_000
SUPPORTED_EXTENSIONS = frozenset({
    ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".opus",
    ".mp4", ".mkv", ".webm", ".mov",
})

_MESSAGES = {
    "invalid_folder": "The folder is not an approved, accessible directory.",
    "invalid_path": "The file path is invalid or outside the approved scope.",
    "unsupported_format": "That audio/video format is not supported.",
    "file_not_found": "The file was not found.",
    "file_too_large": "The file is larger than 2 GiB.",
    "invalid_language": "Language must be auto, en, or pl.",
    "ffmpeg_missing": "ffmpeg is not installed.",
    "whisper_missing": "The local whisper.cpp model or binary is missing.",
    "conversion_failed": "ffmpeg could not convert the file.",
    "transcription_failed": "Local transcription failed.",
    "timeout": "Local transcription timed out.",
    "empty_transcript": "No speech was recognized.",
}


def _failure(code, **extra):
    return {
        "success": False,
        "code": code,
        "message": _MESSAGES.get(code, "Transcription failed."),
        "text": "",
        "language": None,
        "truncated": False,
        **extra,
    }


class FileTranscriber:
    def __init__(
        self,
        resolver=None,
        ffmpeg_path=None,
        whisper_path=None,
        model_path=None,
        timeout=300.0,
        runner=None,
    ):
        self.resolver = resolver or UserFolderResolver()
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg")
        self.whisper_path = str(whisper_path or WHISPER_CLI)
        self.model_path = str(model_path or MODEL_PATH)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = float(timeout)
        self.runner = runner or subprocess.run

    def transcribe(self, folder, relative_path, language="auto"):
        if language not in {"auto", "en", "pl"}:
            return _failure("invalid_language")
        try:
            path = self.resolver.resolve_key(str(folder), str(relative_path))
        except (FileScopeError, OSError, RuntimeError, ValueError, TypeError):
            return _failure("invalid_path")
        if path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
            return _failure("unsupported_format")
        try:
            if path.is_symlink() or not path.is_file():
                return _failure("file_not_found")
            size = path.stat().st_size
        except OSError:
            return _failure("file_not_found")
        if size > MAX_FILE_BYTES:
            return _failure("file_too_large")
        if not self.ffmpeg_path or not Path(self.ffmpeg_path).is_file():
            return _failure("ffmpeg_missing")
        if not Path(self.whisper_path).is_file() or not Path(self.model_path).is_file():
            return _failure("whisper_missing")

        with tempfile.TemporaryDirectory(prefix="misty-file-whisper-") as tmp:
            wav = Path(tmp) / "audio.wav"
            prefix = Path(tmp) / "result"
            txt = Path(tmp) / "result.txt"
            try:
                converted = self.runner(
                    [
                        self.ffmpeg_path, "-nostdin", "-hide_banner",
                        "-loglevel", "error", "-y", "-i", str(path),
                        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                        str(wav),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=min(120.0, self.timeout),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return _failure("timeout")
            except OSError:
                return _failure("conversion_failed")
            if getattr(converted, "returncode", 1) != 0:
                return _failure("conversion_failed")

            try:
                result = self.runner(
                    [
                        self.whisper_path, "-m", self.model_path,
                        "-f", str(wav), "-l", language,
                        "-otxt", "-of", str(prefix), "-nt",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return _failure("timeout")
            except OSError:
                return _failure("transcription_failed")
            if getattr(result, "returncode", 1) != 0:
                return _failure("transcription_failed")
            try:
                text = txt.read_text(encoding="utf-8").strip()
            except OSError:
                return _failure("transcription_failed")
            if not text:
                return _failure("empty_transcript")
            truncated = len(text) > MAX_TEXT_CHARS
            if truncated:
                text = text[:MAX_TEXT_CHARS]
            detected = language
            if language == "auto":
                match = re.search(
                    r"auto-detected language:\s*([A-Za-z-]+)",
                    (getattr(result, "stdout", "") or "")
                    + (getattr(result, "stderr", "") or ""),
                    re.IGNORECASE,
                )
                detected = match.group(1).lower() if match else None
            return {
                "success": True,
                "code": None,
                "message": "Transcription completed locally.",
                "text": text,
                "language": detected,
                "truncated": truncated,
            }
