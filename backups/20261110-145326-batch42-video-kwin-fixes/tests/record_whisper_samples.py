import os
import queue
import subprocess
import threading
import time
import wave

import numpy as np
import sounddevice as sd

RATE = 16000
BLOCK_SIZE = 512

SHORTCUT = os.path.expanduser(
    "~/misty/kde-shortcut/build/misty-shortcut"
)

audio_chunks = []
recording = False
audio_lock = threading.Lock()
events = queue.Queue()


def audio_callback(indata, frames, time_info, status):
    global audio_chunks

    if status:
        print(f"[Audio] {status}", flush=True)

    with audio_lock:
        if recording:
            audio_chunks.append(indata.copy())


def shortcut_reader(process):
    for line in process.stdout:
        event = line.strip().upper()
        if event in ("PRESS", "RELEASE"):
            events.put(event)


def save_recording(language):
    global recording

    with audio_lock:
        recording = False
        chunks = list(audio_chunks)
        audio_chunks.clear()

    if not chunks:
        print("[Misty] No audio captured. Try again.")
        return False

    audio = np.concatenate(chunks, axis=0).flatten()

    if len(audio) < RATE:
        print("[Misty] Recording was shorter than one second. Try again.")
        return False

    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)

    filename = f"tests/{language}_test.wav"

    with wave.open(filename, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(pcm.tobytes())

    print(
        f"[Misty] Saved {filename} "
        f"({len(audio) / RATE:.1f} seconds).",
        flush=True,
    )
    return True


def main():
    global recording, audio_chunks

    if not os.path.isfile(SHORTCUT):
        raise FileNotFoundError(
            f"KDE shortcut helper not found: {SHORTCUT}"
        )

    process = subprocess.Popen(
        [SHORTCUT],
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
    )

    threading.Thread(
        target=shortcut_reader,
        args=(process,),
        daemon=True,
    ).start()

    print("[Misty] Starting microphone...", flush=True)

    try:
        with sd.InputStream(
            samplerate=RATE,
            channels=1,
            dtype="float32",
            blocksize=BLOCK_SIZE,
            callback=audio_callback,
        ):
            print("\nHold F8 to record. Release F8 to finish.")
            print("No sudo required. Press Ctrl+C to exit.")

            for language in ("english", "polish"):
                input(
                    f"\nWhen ready to record your {language} phrase, "
                    "press ENTER here."
                )

                print(
                    f"[Misty] Waiting for F8 ({language})...",
                    flush=True,
                )

                while True:
                    event = events.get()

                    if event == "PRESS":
                        with audio_lock:
                            audio_chunks.clear()
                            recording = True

                        print("[Misty] Recording...", flush=True)

                    elif event == "RELEASE":
                        with audio_lock:
                            was_recording = recording

                        if was_recording:
                            if save_recording(language):
                                break

                            print(
                                "[Misty] Hold F8 and try again.",
                                flush=True,
                            )

    except KeyboardInterrupt:
        print("\n[Misty] Test cancelled.", flush=True)

    finally:
        with audio_lock:
            recording = False

        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

    print("[Misty] Recording test finished.", flush=True)


if __name__ == "__main__":
    main()
