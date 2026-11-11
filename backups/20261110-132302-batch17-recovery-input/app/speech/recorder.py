import queue
import time

import numpy as np
import sounddevice as sd

from app.speech.audio_config import get_input_device

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_SIZE = 512


class Recorder:
    def __init__(self):
        self.audio_queue = queue.Queue()
        self.stream = None
        self.recording = False

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"[Misty Audio] {status}", flush=True)

        if self.recording:
            self.audio_queue.put(indata.copy())

    def prepare(self):
        if self.stream is not None:
            return

        device = get_input_device()

        self.stream = sd.InputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=BLOCK_SIZE,
            latency="low",
            callback=self._callback,
        )

        print("[Misty] Microphone prepared. Waiting for shortcut.", flush=True)

    def start(self):
        if self.recording:
            return

        if self.stream is None:
            self.prepare()

        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break

        self.recording = True
        started = time.perf_counter()

        try:
            self.stream.start()
        except Exception:
            self.recording = False
            raise

        elapsed = (time.perf_counter() - started) * 1000
        print(
            f"[Misty] Recording started ({elapsed:.1f} ms stream-start time).",
            flush=True,
        )

    def stop(self):
        if self.stream is None or not self.recording:
            return None

        self.recording = False
        self.stream.stop()

        chunks = []

        while True:
            try:
                chunks.append(self.audio_queue.get_nowait())
            except queue.Empty:
                break

        if not chunks:
            print("[Misty] No audio captured.", flush=True)
            return None

        audio = np.concatenate(chunks, axis=0).flatten()

        print(
            f"[Misty] Recording finished: {len(audio) / SAMPLE_RATE:.2f} seconds. "
            f"Captured {len(audio)} samples.",
            flush=True,
        )

        return audio

    def close(self):
        self.recording = False

        if self.stream is not None:
            try:
                if self.stream.active:
                    self.stream.stop()
            finally:
                self.stream.close()
                self.stream = None

        print("[Misty] Microphone stream closed.", flush=True)
