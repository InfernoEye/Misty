import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.actions.file_scope import UserFolderResolver
from app.speech.file_transcriber import (
    MODEL_PATH,
    WHISPER_CLI,
    FileTranscriber,
)


MP3_BYTES = (
    b"\xff\xfb\x90\x64" + b"\x00" * 313          # MPEG-1 L3 frame header
    + b"\xff\xfb\x90\x64" + b"\x00" * 313
)


class FakeRunner:
    def __init__(self, text="hello world", fail_second=False, timeout=False):
        self.text = text
        self.fail_second = fail_second
        self.timeout = timeout
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if self.timeout:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 1))
        if command[0] == "/usr/bin/ffmpeg":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if self.fail_second:
            return SimpleNamespace(returncode=1, stdout="", stderr="bad")
        if "-of" in command:
            prefix = Path(command[command.index("-of") + 1])
            prefix.with_suffix(".txt").write_text(self.text, encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout="auto-detected language: en\n",
            stderr="",
        )


class FileTranscriberTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.docs = self.home / "Documents"
        self.docs.mkdir()
        self.path = self.docs / "clip.mp3"
        # A tiny synthetic MPEG frame header: content must look like binary
        # media because playlist manifests are refused before ffmpeg runs.
        self.path.write_bytes(MP3_BYTES)
        self.resolver = UserFolderResolver(
            home=self.home, xdg_lookup=lambda key: None
        )

    def make(self, **kwargs):
        return FileTranscriber(
            resolver=self.resolver,
            ffmpeg_path="/usr/bin/ffmpeg",
            whisper_path=str(WHISPER_CLI),
            model_path=str(MODEL_PATH),
            **kwargs,
        )

    def test_transcribes_with_local_commands(self):
        runner = FakeRunner("hello from file")
        result = self.make(runner=runner).transcribe(
            "Documents", "clip.mp3"
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["text"], "hello from file")
        self.assertEqual(result["language"], "en")
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("-ar", runner.calls[0])
        self.assertIn("-otxt", runner.calls[1])

    def test_rejects_format_and_scope(self):
        (self.docs / "note.txt").write_text("x")
        transcriber = self.make(runner=FakeRunner())
        self.assertEqual(
            transcriber.transcribe("Documents", "note.txt")["code"],
            "unsupported_format",
        )
        self.assertEqual(
            transcriber.transcribe("Documents", "../clip.mp3")["code"],
            "invalid_path",
        )

    def test_failures_and_timeout(self):
        failed = self.make(
            runner=FakeRunner(fail_second=True)
        ).transcribe("Documents", "clip.mp3")
        self.assertEqual(failed["code"], "transcription_failed")

        timed = self.make(
            runner=FakeRunner(timeout=True)
        ).transcribe("Documents", "clip.mp3")
        self.assertEqual(timed["code"], "timeout")

        empty = self.make(
            runner=FakeRunner(text="")
        ).transcribe("Documents", "clip.mp3")
        self.assertEqual(empty["code"], "empty_transcript")

    def test_truncates_long_text(self):
        result = self.make(
            runner=FakeRunner("x" * 200_001)
        ).transcribe("Documents", "clip.mp3")
        self.assertTrue(result["success"])
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["text"]), 200_000)


if __name__ == "__main__":
    unittest.main()
