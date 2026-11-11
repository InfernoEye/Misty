import gc
import re
import subprocess
import tempfile
import wave

from pathlib import Path

import numpy as np


PROJECT_DIR = Path(__file__).resolve().parents[2]

WHISPER_CLI = (
    PROJECT_DIR
    / "third_party"
    / "whisper.cpp"
    / "build"
    / "bin"
    / "whisper-cli"
)

MODEL_PATH = (
    PROJECT_DIR
    / "models"
    / "ggml-large-v3-turbo-q5_0.bin"
)

SAMPLE_RATE = 16000
THREADS = 8
GPU_DEVICE = 0


class Transcriber:
    def __init__(self):
        self.ready = False

    def load(self):
        if not WHISPER_CLI.is_file():
            raise FileNotFoundError(
                f"Whisper executable not found: {WHISPER_CLI}"
            )

        if not MODEL_PATH.is_file():
            raise FileNotFoundError(
                f"Whisper model not found: {MODEL_PATH}"
            )

        self.ready = True
        print(
            "[Misty] Whisper.cpp ready "
            "(large-v3-turbo-q5_0, Vulkan device 0 requested).",
            flush=True,
        )

    def transcribe(self, audio):
        if not self.ready:
            raise RuntimeError("Whisper is not initialized.")

        audio = np.asarray(audio, dtype=np.float32).flatten()

        if audio.size == 0:
            return {
                "text": "",
                "language": None,
                "language_probability": None,
            }

        audio = np.clip(audio, -1.0, 1.0)
        pcm = (audio * 32767).astype(np.int16)

        with tempfile.TemporaryDirectory(prefix="misty-whisper-") as tmp:
            tmp = Path(tmp)
            wav_path = tmp / "input.wav"
            output_prefix = tmp / "result"
            txt_path = tmp / "result.txt"

            with wave.open(str(wav_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(pcm.tobytes())

            command = [
                str(WHISPER_CLI),
                "-m", str(MODEL_PATH),
                "-f", str(wav_path),
                "-l", "auto",
                "-t", str(THREADS),
                "-dev", str(GPU_DEVICE),
                "-otxt",
                "-of", str(output_prefix),
                "-nt",
            ]

            print("[Misty] Transcribing...", flush=True)

            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                raise RuntimeError(
                    "Whisper.cpp failed:\n"
                    + result.stderr[-4000:]
                    + result.stdout[-2000:]
                )

            text = ""
            if txt_path.exists():
                text = txt_path.read_text(
                    encoding="utf-8"
                ).strip()

            log = result.stdout + "\n" + result.stderr

            language_match = re.search(
                r"auto-detected language:\s*([a-z]+)"
                r"\s*\(p\s*=\s*([0-9.]+)\)",
                log,
                re.IGNORECASE,
            )

            language = None
            probability = None

            if language_match:
                language = language_match.group(1).lower()
                probability = float(language_match.group(2))

            print(
                f"[Misty] Transcription: {text}",
                flush=True,
            )

            if language:
                print(
                    f"[Misty] Detected language: {language} "
                    f"({probability:.2f})",
                    flush=True,
                )

            return {
                "text": text,
                "language": language,
                "language_probability": probability,
            }

    def unload(self):
        # whisper-cli is a separate process. It exits after
        # transcription, releasing its model and GPU resources.
        gc.collect()
        print(
            "[Misty] Whisper process finished; model resources released.",
            flush=True,
        )
