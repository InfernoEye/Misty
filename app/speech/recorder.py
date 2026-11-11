import queue
import time

import numpy as np
import sounddevice as sd

from app.speech.audio_config import get_input_device

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_SIZE = 512
#: Hard ceiling on buffered audio (~120 s at 512-sample hops/16 kHz). A
#: recording that is never released (helper died, rebind mid-hold) must not
#: grow memory without bound; beyond this the oldest blocks are dropped.
MAX_QUEUED_BLOCKS = (120 * 16000) // BLOCK_SIZE


class Recorder:
    def __init__(self, max_queued_blocks=MAX_QUEUED_BLOCKS):
        if not isinstance(max_queued_blocks, int) or max_queued_blocks <= 0:
            raise ValueError("max_queued_blocks must be a positive integer")
        self.max_queued_blocks = max_queued_blocks
        self._dropped_chunks = 0
        self.queue = self._new_queue()
        # ``audio_queue`` stays the historical attribute name.
        self.audio_queue = self.queue
        self.stream = None
        self.recording = False

    @property
    def dropped_blocks(self):
        return self._dropped_chunks

    def _new_queue(self):
        # maxsize counts put() calls (not samples): the same cap as the
        # block budget, so a stalled consumer can never grow it past ~120 s.
        return queue.Queue(maxsize=self.max_queued_blocks)

    def _clear_queue(self):
        while True:
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                return

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"[Misty Audio] {status}", flush=True)

        if not self.recording:
            return
        block = indata.copy()
        if self.audio_queue.qsize() >= self.max_queued_blocks:
            # Never block the audio thread and never grow without bound.
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                pass
            self._dropped_chunks += 1
        try:
            self.audio_queue.put_nowait(block)
        except queue.Full:
            self._dropped_chunks += 1
            print("[Misty Audio] Audio queue full; dropping a block.", flush=True)

    def queued_seconds(self):
        """Seconds of audio currently buffered (cheap: qsize, no copy)."""
        return self.audio_queue.qsize() * BLOCK_SIZE / SAMPLE_RATE

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

        self._clear_queue()
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

        if self._dropped_chunks:
            print(
                f"[Misty Audio] Dropped {self._dropped_chunks} queued "
                "audio block(s) at the queue cap.",
                flush=True,
            )
            self._dropped_chunks = 0

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

    def discard(self):
        """Stop the stream and throw away everything captured.

        Used when a recording can never be turned into a request (helper
        died, shortcut rebound, shutdown) or when the watchdog fires.
        """
        was_recording = self.recording
        self.recording = False
        if self.stream is not None:
            try:
                if self.stream.active:
                    self.stream.stop()
            except Exception as error:
                print(f"[Misty] Mic discard stop error: {error}", flush=True)
        self._clear_queue()
        self._dropped_chunks = 0
        if was_recording:
            print("[Misty] Recording discarded.", flush=True)
        return was_recording

    def close(self):
        self.recording = False

        if self.stream is not None:
            try:
                if self.stream.active:
                    self.stream.stop()
            finally:
                self.stream.close()
                self.stream = None

        self._clear_queue()
        print("[Misty] Microphone stream closed.", flush=True)
