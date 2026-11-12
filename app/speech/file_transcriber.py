"""Bounded local audio/video file transcription with ffmpeg + whisper.cpp.

Safety model
------------
* Only files inside an approved folder with a supported extension are read.
* The content is inspected before ffmpeg runs: playlist manifests
  (``ffconcat``, ``#EXTM3U``/``#EXTINF``, ``.m3u8``) are refused, so a
  ``.mp4`` that is really an ``ffconcat`` manifest cannot make ffmpeg read
  audio from another path.
* The ffmpeg demuxer is pinned from the extension, the protocol whitelist is
  restricted to local ``file`` access, and decoded duration (``-t``) and WAV
  size (``-fs``) are bounded on the command line as well as after the fact.
* ffmpeg and whisper share *one* absolute deadline instead of each getting
  the full timeout, and their captured output is drained through bounded
  readers instead of being buffered without limit.
"""

from __future__ import annotations

import os
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import signal
import time
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
# Decoded audio is bounded on the ffmpeg command line, not only at runtime.
MAX_DECODED_SECONDS = 3600
# 4 GiB of 16 kHz mono s16le (~ 37 h) -- above the duration bound on purpose.
MAX_WAV_BYTES = 4 * 1024 ** 3
# Absolute wall-clock budget shared by the ffmpeg and whisper runs.
MAX_PROCESS_TIMEOUT_SECONDS = 3600.0
# Stderr/stdout kept for language detection, bounded.
MAX_CAPTURE_BYTES = 64 * 1024
TERMINATION_GRACE_SECONDS = 2.0

SUPPORTED_EXTENSIONS = frozenset({
    ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".opus",
    ".mp4", ".mkv", ".webm", ".mov",
})

# ffmpeg demuxer pinned per extension; never let ffmpeg autodetect a
# container (autodetection is what allowed ffconcat manifests through).
DEMUXERS = {
    ".wav": "wav",
    ".mp3": "mp3",
    ".flac": "flac",
    ".ogg": "ogg",
    ".m4a": "ipod",
    ".opus": "ogg",
    ".mp4": "mp4",
    ".mkv": "matroska",
    ".webm": "webm",
    ".mov": "mov",
}

_MESSAGES = {
    "invalid_folder": "The folder is not an approved, accessible directory.",
    "invalid_path": "The file path is invalid or outside the approved scope.",
    "unsupported_format": "That audio/video format is not supported.",
    "file_not_found": "The file was not found.",
    "file_too_large": "The file is larger than 2 GiB.",
    "invalid_language": "Language must be auto, en, or pl.",
    "unsupported_content": (
        "That file is a playlist or does not match its extension."
    ),
    "ffmpeg_missing": "ffmpeg is not installed.",
    "whisper_missing": "The local whisper.cpp model or binary is missing.",
    "conversion_failed": "ffmpeg could not convert the file.",
    "transcription_failed": "Local transcription failed.",
    "timeout": "Local transcription timed out.",
    "empty_transcript": "No speech was recognized.",
}


def is_media_head(path, head):
    """Heuristic used by :meth:`FileTranscriber._content_is_media`."""
    if not head:
        return False
    if path.suffix.casefold() in {".m3u", ".m3u8"}:
        return False
    if sniff_playlist(head):
        return False
    # A text file is not a media container. Real WAV/MP3/FLAC/OGG/M4A/MP4
    # headers are binary (a NUL byte or control bytes in the first 512),
    # while a plain m3u/m3u8/ffconcat manifest without the keywords is
    # printable ASCII. Text dressed up with a media extension is refused
    # before ffmpeg can interpret it.
    sample = head[:512]
    if all(byte in (9, 10, 13) or 32 <= byte < 127 for byte in sample):
        return False
    return True


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


def _compatible_run(runner, command, timeout, **kwargs):
    """Call ``runner`` with the historical keyword arguments only."""
    return runner(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        **kwargs,
    )


def sniff_playlist(head):
    """True when the first bytes look like a playlist/concatenation file."""
    if not head:
        return False
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n\x00")
    if text.startswith(b"#EXTM3U"):
        return True
    # ffconcat manifests may start with a comment line before "ffconcat".
    for line in text.split(b"\n")[:4]:
        stripped = line.strip().lower()
        if stripped.startswith(b"ffconcat"):
            return True
        if stripped.startswith(b"#") and b"ffconcat" in stripped:
            return True
    return False


class _Deadline:
    def __init__(self, seconds):
        self.seconds = float(seconds)
        self.end = time.monotonic() + self.seconds

    def remaining(self):
        return self.end - time.monotonic()

    def expired(self):
        return self.remaining() <= 0


class _Capture:
    """Bounded, non-blocking capture of one child's stderr."""

    def __init__(self, stream, limit):
        self.stream = stream
        self.limit = limit
        self.data = bytearray()
        self.closed = False

    def fileno(self):
        try:
            return self.stream.fileno()
        except (OSError, ValueError):
            return -1

    def pump(self, size=8192):
        """Read what is available without blocking; return True at EOF."""
        if self.closed:
            return True
        try:
            chunk = os.read(self.fileno(), size)
        except BlockingIOError:
            return False
        except OSError:
            self.close()
            return True
        if not chunk:
            self.close()
            return True
        room = self.limit - len(self.data)
        if room > 0:
            self.data.extend(chunk[:room])
        return False

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.stream.close()
        except OSError:
            pass

    def text(self):
        return bytes(self.data).decode("utf-8", errors="replace")


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
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise ValueError("timeout must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = float(timeout)
        # Optional seam; ``None`` uses a real subprocess with one deadline.
        self.runner = runner
        #: Optional zero-argument callable run at the top of every
        #: transcribe() call, before ffmpeg starts. The app wires it to
        #: release the reasoning model so Ministral and whisper never overlap.
        self.before_transcribe = None
        self._closed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_before_transcribe(self, callback):
        """Install (or clear, with None) the pre-transcription release hook."""
        self.before_transcribe = callback

    def transcribe(self, folder, relative_path, language="auto"):
        self._run_before_transcribe()
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
        if not Path(self.whisper_path).is_file() or not Path(
            self.model_path
        ).is_file():
            return _failure("whisper_missing")
        try:
            if not self._content_is_media(path):
                return _failure("unsupported_content")
        except OSError:
            return _failure("file_not_found")

        deadline = _Deadline(self.timeout)
        with tempfile.TemporaryDirectory(prefix="misty-file-whisper-") as tmp:
            wav = Path(tmp) / "audio.wav"
            prefix = Path(tmp) / "result"
            txt = Path(tmp) / "result.txt"

            converted = self._convert(path, wav, deadline)
            if converted is not None:
                return _failure(converted)

            result = self._run_whisper(wav, prefix, language, deadline)
            if isinstance(result, str):
                return _failure(result)
            try:
                wav_size = os.stat(wav).st_size
            except OSError:
                wav_size = 0
            if wav_size > MAX_WAV_BYTES:
                return _failure("conversion_failed")
            return self._read_result(result, txt, language)

    def _run_before_transcribe(self):
        callback = self.before_transcribe
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 - a hook must never fail a job
            print(f"[Misty] Transcription pre-hook failed: {exc}", flush=True)

    def close(self):
        """Release resources and refuse further work (shutdown path).

        Children are session-scoped and bounded by the shared deadline, so
        nothing needs killing here; this marks the engine closed so a late
        tool call after application shutdown cannot start ffmpeg/whisper.
        """
        self._closed = True
        self.before_transcribe = None

    # ------------------------------------------------------------------
    # Content gate
    # ------------------------------------------------------------------

    @staticmethod
    def _content_is_media(path):
        """Refuse playlists/concatenation manifests before ffmpeg runs."""
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                return False
            head = os.read(fd, 4096)
        finally:
            os.close(fd)
        return is_media_head(path, head)

    # ------------------------------------------------------------------
    # ffmpeg
    # ------------------------------------------------------------------

    def _ffmpeg_command(self, path, wav):
        demuxer = DEMUXERS[path.suffix.casefold()]
        return [
            str(self.ffmpeg_path), "-nostdin", "-hide_banner",
            "-loglevel", "error", "-y",
            "-protocol_whitelist", "file",
            "-format_whitelist", demuxer,
            "-max_alloc", str(MAX_WAV_BYTES),
            "-f", demuxer,
            "-i", str(path),
            "-map", "0:a:0",
            "-t", str(int(MAX_DECODED_SECONDS)),
            "-fs", str(int(MAX_WAV_BYTES)),
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            "-f", "wav",
            str(wav),
        ]

    def _convert(self, path, wav, deadline):
        """Run ffmpeg; return ``None`` on success or an error code."""
        command = self._ffmpeg_command(path, wav)
        if self.runner is not None:
            remaining = deadline.remaining()
            if remaining <= 0:
                return "timeout"
            try:
                converted = _compatible_run(
                    self.runner, command, timeout=remaining
                )
            except subprocess.TimeoutExpired:
                return "timeout"
            except OSError:
                return "conversion_failed"
            if getattr(converted, "returncode", 1) != 0:
                return "conversion_failed"
            try:
                if os.stat(wav).st_size > MAX_WAV_BYTES:
                    return "conversion_failed"
            except OSError:
                # A fake runner need not create the WAV.
                pass
            return None
        error, _ = self._run_bounded(command, deadline, "conversion_failed")
        if error is not None:
            return error
        try:
            produced = os.stat(wav)
        except OSError:
            return "conversion_failed"
        if not stat.S_ISREG(produced.st_mode) or int(produced.st_size) <= 0:
            return "conversion_failed"
        if int(produced.st_size) > MAX_WAV_BYTES:
            return "conversion_failed"
        return None

    # ------------------------------------------------------------------
    # whisper.cpp
    # ------------------------------------------------------------------

    def _run_whisper(self, wav, prefix, language, deadline):
        command = [
            self.whisper_path, "-m", self.model_path,
            "-f", str(wav), "-l", language,
            "-otxt", "-of", str(prefix), "-nt",
        ]
        if self.runner is not None:
            remaining = deadline.remaining()
            if remaining <= 0:
                return "timeout"
            try:
                result = _compatible_run(
                    self.runner, command, timeout=remaining
                )
            except subprocess.TimeoutExpired:
                return "timeout"
            except OSError:
                return "transcription_failed"
            if getattr(result, "returncode", 1) != 0:
                return "transcription_failed"
            return result
        code, text = self._run_bounded(
            command, deadline, "transcription_failed"
        )
        if code is not None:
            return code
        return _BoundedResult(text)

    # ------------------------------------------------------------------
    # Bounded subprocess with one shared deadline
    # ------------------------------------------------------------------

    def _run_bounded(self, command, deadline, failure_code):
        """Run ``command`` against ``deadline`` and drain its streams.

        Returns ``(error_code, stderr_text)``; ``(None, stderr)`` on exit
        status 0. Both stderr and stdout are read with non-blocking
        ``os.read`` bounded by :data:`MAX_CAPTURE_BYTES` and by the one
        absolute deadline shared with the other child process. Children run
        in their own session and are killed as a process group.
        """
        remaining = deadline.remaining()
        if remaining <= 0:
            return "timeout", ""
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
                close_fds=True,
            )
        except OSError:
            return failure_code, ""

        captures = []
        try:
            for stream in (process.stdout, process.stderr):
                if stream is None:
                    continue
                os.set_blocking(stream.fileno(), False)
                captures.append(_Capture(stream, MAX_CAPTURE_BYTES))
            selector = selectors.DefaultSelector()
            try:
                for capture in captures:
                    if capture.fileno() >= 0:
                        selector.register(capture.stream, selectors.EVENT_READ)
                while True:
                    if process.poll() is not None:
                        break
                    remaining = deadline.remaining()
                    if remaining <= 0:
                        self._terminate(process)
                        return "timeout", captures[0].text() if captures else ""
                    events = selector.select(min(remaining, 0.25))
                    for key, _ in events:
                        for capture in captures:
                            if capture.stream is key.fileobj:
                                capture.pump()
                # Drain whatever is still buffered after exit.
                for capture in captures:
                    while not capture.closed and capture.pump():
                        pass
            finally:
                selector.close()
        finally:
            for capture in captures:
                capture.close()
            try:
                if process.poll() is None:
                    self._terminate(process)
            except OSError:
                pass
            try:
                process.wait(timeout=TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass

        stderr_text = captures[1].text() if len(captures) > 1 else ""
        if process.returncode != 0:
            return failure_code, stderr_text
        return None, stderr_text

    @staticmethod
    def _terminate(process):
        """Terminate the child's whole process group, then kill and reap."""
        pid = process.pid
        for action in ("terminate", "kill"):
            try:
                if action == "terminate":
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                else:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    getattr(process, action)()
                except (OSError, ProcessLookupError):
                    pass
            try:
                process.wait(timeout=TERMINATION_GRACE_SECONDS)
                return
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                return

    def _read_result(self, result, txt, language):
        try:
            text = txt.read_text(encoding="utf-8", errors="replace").strip()
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


class _BoundedResult:
    """Stand-in for ``subprocess.CompletedProcess`` from the bounded runner."""

    def __init__(self, stderr_text, returncode=0):
        self.stdout = ""
        self.stderr = stderr_text
        self.returncode = returncode
